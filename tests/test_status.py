import json
import time
from datetime import UTC, datetime, timedelta
from datetime import time as clock

import httpx
import pytest
from fastapi.testclient import TestClient

from mih import notify, scheduler
from mih.api.app import create_app
from mih.config import ConfigError, Settings, parse_every
from mih.events import list_events, record_event
from mih.ids import new_id
from mih.scheduler import JobBusy, Scheduler, next_run, run_job, status_snapshot, sync_jobs
from mih.sources.importer import ImportReport

NTFY = "https://ntfy.example/mih-test-topic"
NOW = datetime(2026, 10, 26, 10, 0, tzinfo=UTC)


class Recorder:
    """An ntfy server that remembers what it was sent."""

    def __init__(self, status: int = 200):
        self.status, self.requests = status, []

    def transport(self) -> httpx.MockTransport:
        def handle(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return httpx.Response(self.status, json={"id": "x"})

        return httpx.MockTransport(handle)

    def payloads(self) -> list[dict]:
        return [json.loads(r.content) for r in self.requests]


@pytest.fixture
def pushing(tmp_path):
    return Settings(data_dir=tmp_path, ntfy_url=NTFY, ntfy_token="tk_secret", lang="ru",
                    scheduler_enabled=True, timezone="Europe/Berlin")  # fmt: skip


# ---------- Settings ----------


def test_durations_and_clock_from_env():
    assert parse_every("30m", "X") == timedelta(minutes=30)
    assert parse_every("6h", "X") == timedelta(hours=6)
    assert parse_every("off", "X") is None
    for bad in ("1m", "6x", "h"):
        with pytest.raises(ConfigError):
            parse_every(bad, "X")
    s = Settings.from_env({"MIH_DIGEST_AT": "07:30", "MIH_EVERY_EPG": "off", "MIH_LANG": "ru"})
    assert s.digest_at == clock(7, 30) and s.every_epg is None and s.scheduler_enabled
    assert Settings.from_env({"MIH_SCHEDULER": "0"}).scheduler_enabled is False
    with pytest.raises(ConfigError):
        Settings.from_env({"MIH_NOTIFY_LEVEL": "routine"})


# ---------- ntfy ----------


def test_ntfy_target_splits_server_and_topic():
    assert notify.ntfy_target("https://ntfy.sh/abc_123") == ("https://ntfy.sh", "abc_123")
    assert notify.ntfy_target("https://h.example/ntfy/t") == ("https://h.example/ntfy", "t")
    for bad in ("ntfy.sh/abc", "https://ntfy.sh/", "ftp://ntfy.sh/abc", "https://ntfy.sh/a b"):
        with pytest.raises(ConfigError):
            notify.ntfy_target(bad)


def test_send_publishes_json_with_priority_and_token(pushing):
    srv = Recorder()
    assert notify.send(pushing, "Проверка потоков", "текст", "error",
                       transport=srv.transport()) == "sent"  # fmt: skip
    (req,) = srv.requests
    assert str(req.url) == "https://ntfy.example"
    assert req.headers["Authorization"] == "Bearer tk_secret"
    body = srv.payloads()[0]
    assert body["topic"] == "mih-test-topic" and body["title"] == "Проверка потоков"
    assert body["priority"] == 5 and "click" not in body


def test_send_never_raises(pushing, tmp_path):
    assert notify.send(pushing, "t", "b", transport=Recorder(500).transport()) == "failed"

    def boom(request):
        raise httpx.ConnectError("down")

    assert notify.send(pushing, "t", "b", transport=httpx.MockTransport(boom)) == "failed"
    assert notify.send(Settings(data_dir=tmp_path), "t", "b") == "off"


# ---------- Events ----------


def test_only_events_at_the_notify_level_are_pushed(conn, pushing):
    srv = Recorder()
    record_event(conn, pushing, "x", "routine", "тихо", transport=srv.transport())
    e = record_event(conn, pushing, "x", "info", "громко", transport=srv.transport())
    assert e["pushed"] == "sent" and len(srv.requests) == 1
    quiet = Settings(data_dir=pushing.data_dir, ntfy_url=NTFY, notify_level="warning")
    assert (
        record_event(conn, quiet, "x", "info", "мимо", transport=srv.transport())["pushed"] is None
    )
    assert record_event(conn, quiet, "x", "error", "важно", transport=srv.transport())["pushed"]
    assert len(srv.requests) == 2
    assert [e["title"] for e in list_events(conn, min_level="warning")] == ["важно"]
    assert len(list_events(conn)) == 4


def test_a_repeating_problem_is_pushed_once(conn, pushing):
    srv = Recorder()
    first = record_event(conn, pushing, "job.health", "warning", "нет интернета",
                         transport=srv.transport())  # fmt: skip
    again = record_event(conn, pushing, "job.health", "warning", "нет интернета",
                         transport=srv.transport())  # fmt: skip
    other = record_event(conn, pushing, "job.epg", "warning", "нет интернета",
                         transport=srv.transport())  # fmt: skip
    assert (first["pushed"], again["pushed"], other["pushed"]) == ("sent", "repeat", "sent")
    assert len(srv.requests) == 2


def test_old_events_are_forgotten(conn, settings):
    conn.execute("INSERT INTO events (id, at, kind, level, title)"
                 " VALUES (?, ?, 'x', 'info', 'old')",
                 (new_id(), "2000-01-01T00:00:00Z"))  # fmt: skip
    record_event(conn, settings, "x", "info", "new")
    assert [e["title"] for e in list_events(conn)] == ["new"]


# ---------- Plan ----------


def test_next_run_of_digest_is_local_time(pushing):
    # 09:00 in Berlin on 26 Oct 2026 (CET, UTC+1) is 08:00 UTC: already past at 10:00 UTC.
    assert next_run(pushing, "digest", NOW) == datetime(2026, 10, 27, 8, 0, tzinfo=UTC)
    assert next_run(pushing, "health", NOW) == NOW + timedelta(minutes=10)
    off = Settings(data_dir=pushing.data_dir, digest_at=None, every_epg=None)
    assert next_run(off, "digest", NOW) is None and next_run(off, "epg", NOW) is None


def test_sync_keeps_the_plan_across_restarts(conn, pushing):
    sync_jobs(conn, pushing, NOW)
    plan = {r["name"]: r["next_run_at"] for r in conn.execute("SELECT * FROM jobs")}
    assert plan["sources"] == "2026-10-26T10:02:00Z"
    assert plan["digest"] == "2026-10-27T08:00:00Z"
    conn.execute("UPDATE jobs SET next_run_at = '2026-10-26T15:00:00Z' WHERE name = 'sources'")
    sync_jobs(conn, pushing, NOW + timedelta(minutes=10))
    row = conn.execute("SELECT next_run_at FROM jobs WHERE name = 'sources'").fetchone()
    assert row[0] == "2026-10-26T15:00:00Z"  # a restart does not pull the run forward
    shorter = Settings(data_dir=pushing.data_dir, every_sources=timedelta(hours=1))
    sync_jobs(conn, shorter, NOW)
    row = conn.execute("SELECT next_run_at FROM jobs WHERE name = 'sources'").fetchone()
    assert row[0] == "2026-10-26T11:00:00Z"


def test_restart_reports_an_unfinished_run(conn, pushing):
    sync_jobs(conn, pushing, NOW)
    conn.execute("UPDATE jobs SET running_since = ? WHERE name = 'epg'", ("2026-10-26T09:59:00Z",))
    assert sync_jobs(conn, pushing, NOW) == ["epg"]
    row = conn.execute("SELECT running_since, last_status FROM jobs WHERE name = 'epg'").fetchone()
    assert row["running_since"] is None and row["last_status"] == "error"


# ---------- Jobs ----------


def _add_source(conn, name: str, url: str) -> str:
    sid = new_id()
    conn.execute("INSERT INTO sources (id, kind, name, url, created_at) VALUES (?, 'm3u', ?, ?, ?)",
                 (sid, name, url, "2026-10-26T00:00:00Z"))  # fmt: skip
    return sid


def test_sources_job_reports_each_playlist(conn, pushing, monkeypatch):
    _add_source(conn, "a", "http://127.0.0.1:9/a.m3u")  # blocked: a private address
    _add_source(conn, "b", "https://example.org/b.m3u")

    real = scheduler.import_from_url

    def fake_import(c, s, source_id):
        if c.execute("SELECT name FROM sources WHERE id = ?", (source_id,)).fetchone()[0] == "a":
            return real(c, s, source_id)
        return ImportReport("snap", "applied", channels_created=5, streams_created=7)

    monkeypatch.setattr(scheduler, "import_from_url", fake_import)

    srv = Recorder()
    event = run_job(conn, pushing, "sources", transport=srv.transport())
    assert event["level"] == "warning" and event["pushed"] == "sent"
    assert event["title"] == "Плейлисты: обновлено 1 из 2"
    assert "b: +5 каналов, +7 потоков, пропало 0" in event["body"]
    assert event["body"].startswith("a: ошибка")
    job = conn.execute("SELECT * FROM jobs WHERE name = 'sources'").fetchone()
    assert job["last_status"] == "warning" and job["running_since"] is None
    assert job["next_run_at"] > "2026-10-26"


def test_quiet_runs_stay_in_the_feed_unless_asked_for(conn, pushing):
    srv = Recorder()
    event = run_job(conn, pushing, "health", transport=srv.transport())
    assert event["title"] == "Проверка потоков: проверять нечего"
    assert event["level"] == "routine" and not srv.requests
    event = run_job(conn, pushing, "epg", manual=True, transport=srv.transport())
    assert event["level"] == "info" and len(srv.requests) == 1


def test_a_crashing_job_becomes_an_error_event(conn, pushing, monkeypatch):
    def boom(*a):
        raise RuntimeError("disk full")

    monkeypatch.setitem(scheduler._RUNNERS, "epg", boom)
    event = run_job(conn, pushing, "epg", transport=Recorder().transport())
    assert event["level"] == "error" and "disk full" in event["body"]
    assert conn.execute("SELECT running_since FROM jobs WHERE name = 'epg'").fetchone()[0] is None


def test_one_job_runs_once_at_a_time(conn, pushing):
    sync_jobs(conn, pushing, NOW)
    now = datetime.now(UTC)
    conn.execute("UPDATE jobs SET running_since = ? WHERE name = 'health'",
                 (now.strftime("%Y-%m-%dT%H:%M:%SZ"),))  # fmt: skip
    with pytest.raises(JobBusy):
        run_job(conn, pushing, "health")


def test_digest_lists_problems_and_the_plan(conn, pushing):
    sync_jobs(conn, pushing, datetime.now(UTC))
    record_event(conn, Settings(data_dir=pushing.data_dir), "job.epg", "error", "EPG сломался")
    event = run_job(conn, pushing, "digest", transport=Recorder().transport())
    assert event["title"] == "MIRTVONLINE: сводка за сутки" and event["level"] == "info"
    body = event["body"]
    assert "Каталог: каналов 0" in body and "• EPG сломался" in body
    assert "Запланировано:" in body and "• проверка потоков: каждые 10 мин" in body


def test_scheduler_tick_runs_what_is_due(pushing):
    from mih import db

    c = db.connect(pushing.db_path)
    db.migrate(c)
    sync_jobs(c, pushing, NOW)
    s = Scheduler(Settings(data_dir=pushing.data_dir, scheduler_enabled=True))
    assert sorted(s.tick(NOW + timedelta(minutes=3))) == ["epg", "health", "sources"]
    assert s.tick(NOW + timedelta(minutes=4)) == []
    snap = status_snapshot(c, pushing)
    assert {j["name"]: j["last_status"] for j in snap["jobs"]}["health"] == "ok"
    assert snap["plan"][0]["next_at"] > snap["now"]
    c.close()


# ---------- API ----------


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(Settings(data_dir=tmp_path))) as c:
        yield c


def test_status_api_shows_start_and_jobs(client):
    s = client.get("/api/v1/status").json()
    assert s["notifications"]["channel"] is None and s["scheduler"] is False
    assert s["events"][0]["kind"] == "service.started"
    assert "ntfy" not in json.dumps(s["notifications"]).replace('"channel"', "")
    assert client.post("/api/v1/notifications/test").json() == {"result": "off"}


def test_job_runs_on_request_in_the_background(client):
    assert client.post("/api/v1/jobs/digest/run").status_code == 202
    assert client.post("/api/v1/jobs/nope/run").status_code == 422
    for _ in range(50):
        items = client.get("/api/v1/events", params={"min_level": "info"}).json()["items"]
        if any(e["kind"] == "job.digest" for e in items):
            break
        time.sleep(0.05)
    else:
        pytest.fail("digest did not run")
    job = {j["name"]: j for j in client.get("/api/v1/status").json()["jobs"]}["digest"]
    assert job["last_status"] == "ok"
