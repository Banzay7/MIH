"""Topic alerts: a push shortly before a followed programme starts, with a link to play it."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from mih import db
from mih.alerts import find_matches, load_rules, play_key, run_alerts
from mih.api.app import create_app
from mih.config import Settings
from mih.epg.importer import import_xmltv
from mih.epg.scope import topic_key
from mih.epg.service import create_epg_source
from mih.events import list_events
from mih.scheduler import run_job
from mih.sources.importer import import_m3u
from tests.conftest import fixture_bytes
from tests.test_status import NTFY, Recorder

NOW = datetime(2026, 10, 26, 16, 0, tzinfo=UTC)
M = timedelta(minutes=1)
TOKEN = "t" * 32


def _p(channel, start, minutes, title, category="", sub="", base=NOW):
    s = (base + start).strftime("%Y%m%d%H%M%S +0000")
    e = (base + start + timedelta(minutes=minutes)).strftime("%Y%m%d%H%M%S +0000")
    cat = f"<category>{category}</category>" if category else ""
    sub = f"<sub-title>{sub}</sub-title>" if sub else ""
    return (f'<programme start="{s}" stop="{e}" channel="{channel}">'
            f'<title lang="ru">{title}</title>{sub}{cat}</programme>')  # fmt: skip


def _guide(*programmes) -> bytes:
    return ("<tv>" + "".join(programmes) + "</tv>").encode()


def _load(conn, settings, guide: bytes, now=NOW) -> None:
    src = conn.execute("SELECT id FROM sources WHERE name = 'a'").fetchone()
    if src is None:
        from mih.sources.service import create_source

        sid = create_source(conn, settings, name="a", url=None)
        import_m3u(conn, settings, sid, fixture_bytes("provider_a.m3u"))
    epg = create_epg_source(conn, settings, name=f"g{now:%H%M%S%f}", url=None)
    assert import_xmltv(conn, settings, epg, guide, now=now).status == "applied"


def _follow(conn, *topics, profile="default"):
    for topic in topics:
        conn.execute(
            "INSERT INTO tracked_topics (profile_id, topic, created_at) VALUES (?, ?, 'x')",
            (profile, topic_key(topic)),
        )


@pytest.fixture
def pushing(tmp_path):
    return Settings(data_dir=tmp_path, ntfy_url=NTFY, lang="ru", timezone="Europe/Moscow",
                    epg_scope="all", notify_level="error")  # fmt: skip


@pytest.fixture
def conn(pushing):
    c = db.connect(pushing.db_path)
    db.migrate(c)
    yield c
    c.close()


def _channel(conn, tvg_id: str) -> str:
    return conn.execute(
        "SELECT f.channel_id FROM feed_aliases a JOIN feeds f ON f.id = a.feed_id"
        " WHERE a.value = ? AND f.offset_minutes = 0", (tvg_id.lower(),),
    ).fetchone()[0]  # fmt: skip


def test_a_topic_is_announced_once_before_it_starts(conn, pushing):
    _load(conn, pushing, _guide(
        _p("Perviy.ru", 10 * M, 60, "Футбол. Премьер-лига", sub="Зенит — Спартак"),
        _p("Perviy.ru", 70 * M, 60, "Новости"),
        _p("Perviy.ru", 3 * 60 * M, 60, "Хоккей", category="Спорт"),
    ))  # fmt: skip
    _follow(conn, "ФУТБОЛ", "спорт")
    srv = Recorder()
    stats = run_alerts(conn, pushing, NOW, transport=srv.transport())
    assert stats["sent"] == 1  # the hockey starts in 3 hours, not within MIH_ALERT_LEAD
    (push,) = srv.payloads()
    # 16:10 UTC is 19:10 in Moscow; pushed although MIH_NOTIFY_LEVEL is "error".
    assert push["title"] == "19:10 · Футбол. Премьер-лига"
    assert push["message"].splitlines() == ["Первый канал HD", "Зенит — Спартак", "тема «футбол»"]
    # No MIH_PUBLIC_URL: the link is the stream itself, the base feed before the +2 copy.
    assert push["click"] == "https://a.example/live/1tv/index.m3u8?token=SECRET_A1"
    assert push["actions"][0] == {"action": "view", "label": "Смотреть", "url": push["click"]}

    assert run_alerts(conn, pushing, NOW + 5 * M, transport=srv.transport())["sent"] == 0
    later = run_alerts(conn, pushing, NOW + 170 * M, transport=srv.transport())
    assert later["sent"] == 1 and srv.payloads()[-1]["message"].endswith("тема «спорт»")
    assert [e["kind"] for e in list_events(conn)] == ["alert.topic", "alert.topic"]


def test_category_word_prefix_and_favourite_programme(conn, pushing):
    _load(conn, pushing, _guide(
        _p("Perviy.ru", 5 * M, 30, "Вечерний Ургант"),
        _p("Perviy.ru", 6 * M, 30, "Суперфутболист"),  # not at the start of a word
        _p("Perviy.ru", 7 * M, 30, "Кубок", category="Футбольный матч"),
    ))  # fmt: skip
    _follow(conn, "футбол")
    pid = conn.execute("SELECT id FROM programmes WHERE title = 'Вечерний Ургант'").fetchone()[0]
    conn.execute("INSERT INTO favorites VALUES ('default', 'programme', ?, 1, 'x')", (pid,))
    found = find_matches(conn, NOW, NOW + 15 * M, NOW, load_rules(conn))
    assert [m.title for m in found] == ["Вечерний Ургант", "Кубок"]
    assert found[0].reasons[0].kind == "programme"


def test_series_back_to_back_and_dead_channels_are_skipped(conn, pushing):
    _load(conn, pushing, _guide(
        _p("Perviy.ru", 2 * M, 30, "Сериал «След»"),
        _p("Perviy.ru", 32 * M, 30, "Сериал «След»"),
        _p("VTV1.vn", 5 * M, 30, "Сериал «След» на VTV"),
    ))  # fmt: skip
    _follow(conn, "след")
    vtv = _channel(conn, "VTV1.vn")
    conn.execute("UPDATE streams SET status = 'quarantine' WHERE feed_id IN"
                 " (SELECT id FROM feeds WHERE channel_id = ?)", (vtv,))  # fmt: skip
    srv = Recorder()
    assert run_alerts(conn, pushing, NOW, transport=srv.transport())["sent"] == 1
    assert run_alerts(conn, pushing, NOW + 25 * M, transport=srv.transport())["sent"] == 0
    assert len(srv.requests) == 1


def test_many_matches_end_in_one_summary_push(conn, pushing):
    _load(conn, pushing, _guide(*[_p("Perviy.ru", (i + 1) * M, 1, f"Новости {i}")
                                  for i in range(8)]))  # fmt: skip
    _follow(conn, "новости")
    srv = Recorder()
    assert run_alerts(conn, pushing, NOW, transport=srv.transport())["sent"] == 8
    titles = [p["title"] for p in srv.payloads()]
    assert len(titles) == 6 and titles[-1] == "Ещё по вашим темам: 3"
    assert srv.payloads()[-1]["message"].count("•") == 3


def test_a_stream_that_needs_headers_is_no_direct_link(conn, pushing):
    _load(conn, pushing, _guide(_p("VTV1.vn", 5 * M, 30, "Thời sự")))
    _follow(conn, "thoi su")  # folded: no diacritics needed
    srv = Recorder()
    assert run_alerts(conn, pushing, NOW, transport=srv.transport())["sent"] == 1
    assert "click" not in srv.payloads()[0]


def test_the_job_is_quiet_without_news(conn, pushing):
    s = Settings(**{**pushing.__dict__, "scheduler_enabled": True})
    event = run_job(conn, s, "alerts", now=NOW)
    assert event["id"] is None and event["title"].startswith("Оповещения по темам: темы")
    assert list_events(conn) == []
    assert run_job(conn, s, "alerts", now=NOW, manual=True)["id"] is not None


# ---------- Links and the API ----------


@pytest.fixture
def client(tmp_path):
    s = Settings(data_dir=tmp_path, api_token=TOKEN, public_url="https://tv.example",
                 epg_scope="all")  # fmt: skip
    c = db.connect(s.db_path)
    db.migrate(c)
    now = datetime.now(UTC).replace(microsecond=0)
    _load(c, s, _guide(_p("Perviy.ru", 20 * M, 60, "Футбол", base=now)), now=now)
    _follow(c, "футбол")
    c.close()
    with TestClient(create_app(s)) as tc:
        tc.settings = s
        yield tc


def test_upcoming_lists_what_alerts_will_announce(client):
    h = {"Authorization": f"Bearer {TOKEN}"}
    data = client.get("/api/v1/profiles/default/alerts/upcoming?hours=2", headers=h).json()
    assert data["rules"] == 1
    (item,) = data["items"]
    assert item["title"] == "Футбол" and item["channels"][0]["name"] == "Первый канал HD"
    assert item["reasons"] == [{"profile_id": "default", "kind": "topic", "value": "футбол"}]
    assert client.get("/api/v1/profiles/default/alerts/upcoming").status_code == 401
    capped = client.get("/api/v1/profiles/default/alerts/upcoming?hours=2&limit=1", headers=h)
    assert capped.json()["total"] == 1 and len(capped.json()["items"]) == 1
    # A new topic clears the kept answer: the next read sees it at once.
    client.post("/api/v1/profiles/default/topics", json={"topic": "кино"}, headers=h)
    assert client.get("/api/v1/profiles/default/alerts/upcoming?hours=2",
                      headers=h).json()["rules"] == 2  # fmt: skip


def test_play_link_opens_one_channel_with_its_key(client):
    c = db.connect(client.settings.db_path)
    channel = _channel(c, "Perviy.ru")
    c.close()
    key = play_key(client.settings, channel)
    r = client.get(f"/play/{channel}?key={key}", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "https://a.example/live/1tv/index.m3u8?token=SECRET_A1"
    m3u = client.get(f"/play/{channel}.m3u?key={key}")
    assert m3u.headers["content-type"].startswith("audio/x-mpegurl")
    stream = "https://a.example/live/1tv/index.m3u8?token=SECRET_A1"
    assert m3u.text.splitlines()[:3] == ["#EXTM3U", "#EXTINF:-1,Первый канал HD", stream]
    assert client.get(f"/play/{channel}?key=wrong", follow_redirects=False).status_code == 401
    assert client.get(f"/play/{channel}?token={TOKEN}", follow_redirects=False).status_code == 302
    other = play_key(client.settings, "another-channel")
    assert client.get(f"/play/{channel}?key={other}").status_code == 401


def test_play_link_goes_into_the_push(conn, pushing):
    s = Settings(**{**pushing.__dict__, "public_url": "https://tv.example", "api_token": TOKEN})
    _load(conn, s, _guide(_p("Perviy.ru", 5 * M, 30, "Футбол")))
    _follow(conn, "футбол")
    srv = Recorder()
    run_alerts(conn, s, NOW, transport=srv.transport())
    channel = _channel(conn, "Perviy.ru")
    assert srv.payloads()[0]["click"] == (
        f"https://tv.example/play/{channel}?key={play_key(s, channel)}")  # fmt: skip


def test_digest_lists_the_day_ahead(conn, pushing):
    _load(conn, pushing, _guide(_p("Perviy.ru", 5 * 60 * M, 30, "Футбол")))
    _follow(conn, "футбол")
    event = run_job(conn, pushing, "digest", now=NOW, transport=Recorder().transport())
    lines = event["body"].splitlines()
    i = lines.index("По вашим темам в ближайшие сутки:")
    assert lines[i + 1] == "• 00:00 Футбол — Первый канал HD"
    assert json.dumps(event["data"])
