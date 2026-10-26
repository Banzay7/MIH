import csv
import json
from datetime import UTC, datetime, timedelta

import pytest

from mih.cli import main
from mih.health.policy import format_ts
from mih.lists import ListFilter, build_list, failure_hint, stream_level, write_list
from mih.sources.importer import import_m3u
from tests.conftest import fixture_bytes

NOW = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)


def _row(**kw):
    base = {"status": "active", "availability_reason": None, "last_ok_at": None,
            "last_device_fail_at": None, "streak_failures": 0}  # fmt: skip
    return {**base, **kw}


def _ago(**kw) -> str:
    return format_ts(NOW - timedelta(**kw))


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        (_row(last_ok_at=_ago(hours=3)), ("ok", None)),
        (_row(last_ok_at=_ago(hours=60)), ("doubtful", "stale")),
        (_row(last_ok_at=_ago(hours=3), streak_failures=2), ("doubtful", "failing")),
        (_row(last_ok_at=_ago(hours=3), last_device_fail_at=_ago(hours=1)),
         ("doubtful", "device_failed")),
        (_row(last_ok_at=_ago(days=8), status="suspect"), ("dead", "failing")),
        (_row(status="unverified"), ("unknown", "unchecked")),
        (_row(status="new"), ("dead", "never_worked")),
        (_row(status="quarantine", last_ok_at=_ago(hours=1)), ("dead", "failing")),
        (_row(last_ok_at=_ago(hours=1), availability_reason="drm_unsupported"), ("dead", "drm")),
        (_row(last_ok_at=_ago(hours=1), availability_reason="source_absent"), ("dead", "absent")),
    ],
)  # fmt: skip
def test_stream_level(row, expected):
    assert stream_level(row, NOW) == expected


def test_failure_hint():
    assert failure_hint("http_403", 403) == "geo_suspect"
    assert failure_hint("timeout", None) == "no_answer"
    assert failure_hint("not_found", 404) == "gone"
    assert failure_hint("dns", None) == "gone"
    assert failure_hint("server_error", 503) == "server_error"


def _load(conn, settings, make_source):
    import_m3u(conn, settings, make_source("a"), fixture_bytes("provider_a.m3u"))


def _set(conn, name_like: str, **cols):
    ids = [r[0] for r in conn.execute(
        "SELECT s.id FROM streams s JOIN feeds f ON f.id = s.feed_id"
        " JOIN channels c ON c.id = f.channel_id WHERE c.canonical_name LIKE ?"
        " ORDER BY f.offset_minutes", (name_like,)
    )]  # fmt: skip
    for sid in ids:
        sets = ", ".join(f"{k} = ?" for k in cols)
        conn.execute(f"UPDATE streams SET {sets} WHERE id = ?", (*cols.values(), sid))
    return ids


def _fail(conn, stream_id, error_kind, http_status):
    conn.execute(
        "INSERT INTO stream_observations (id, stream_id, checked_at, vantage, kind, success,"
        " counted, error_kind, http_status) VALUES (?, ?, ?, 'server', 'probe', 0, 1, ?, ?)",
        (f"o-{stream_id}", stream_id, _ago(hours=1), error_kind, http_status),
    )


def test_list_keeps_working_channels_and_explains_the_rest(conn, settings, make_source):
    _load(conn, settings, make_source)
    _set(conn, "Первый%", status="active", last_ok_at=_ago(hours=2))
    euronews = _set(conn, "Euronews%", status="new")
    for sid in euronews:
        _fail(conn, sid, "http_403", 403)
    # VTV1 stays unverified.

    doc = build_list(conn, ListFilter(), now=NOW)
    names = [c["name"] for c in doc["channels"]]
    assert len(names) == 1 and names[0].startswith("Первый")
    # The +2 copy is left out by default.
    assert [f["offset_minutes"] for f in doc["channels"][0]["feeds"]] == [0]
    assert doc["channels"][0]["countries"] == ["RU"]
    reasons = {e["name"].split()[0]: (e["reason"], e["last_error"]) for e in doc["excluded"]}
    assert reasons["Euronews"] == ("never_worked", "geo_suspect")
    assert reasons["VTV1"] == ("unchecked", None)
    assert doc["excluded_by_reason"] == {"geo_suspect": 1, "unchecked": 1}

    with_shift = build_list(conn, ListFilter(timeshift=True), now=NOW)
    assert len(with_shift["channels"][0]["feeds"]) == 2
    loose = build_list(conn, ListFilter(min_level="unknown"), now=NOW)
    assert {c["name"].split()[0] for c in loose["channels"]} == {"Первый", "VTV1"}


def test_list_filters(conn, settings, make_source):
    _load(conn, settings, make_source)
    _set(conn, "%", status="active", last_ok_at=_ago(hours=2))

    def names(**kw):
        return sorted(c["name"].split()[0] for c in build_list(conn, ListFilter(**kw),
                                                               now=NOW)["channels"])  # fmt: skip

    assert names() == ["Euronews", "VTV1", "Первый"]
    assert names(countries=("vn",)) == ["VTV1"]
    # Euronews.fr: the country comes from the iptv-org id suffix.
    assert names(countries=("FR",)) == ["Euronews"]
    assert names(categories=("news",)) == ["Euronews", "VTV1"]
    assert names(exclude_categories=("news", "adult")) == ["Первый"]
    assert names(epg_only=True) == []
    with pytest.raises(ValueError):
        ListFilter(min_level="dead")


def test_write_list_files_and_cli(conn, settings, make_source, tmp_path, monkeypatch):
    _load(conn, settings, make_source)
    _set(conn, "%", status="active", last_ok_at=format_ts(datetime.now(UTC)))
    out = tmp_path / "lists"
    result = write_list(conn, out, "ru", ListFilter(countries=("RU",)),
                        epg_url="https://epg.example/g.xml")  # fmt: skip
    assert result.files == ["ru.json", "ru.csv", "ru.m3u"]
    assert result.channels == 1 and result.entries == 1

    m3u = (out / "ru.m3u").read_text(encoding="utf-8")
    assert m3u.startswith('#EXTM3U url-tvg="https://epg.example/g.xml"')
    assert m3u.count("#EXTINF") == 1 and "SECRET_A1" in m3u and "SECRET_A2" not in m3u
    doc_text = (out / "ru.json").read_text(encoding="utf-8")
    assert "SECRET" not in doc_text
    assert json.loads(doc_text)["channels"][0]["level"] == "ok"
    rows = list(csv.DictReader((out / "ru.csv").read_text(encoding="utf-8-sig").splitlines()))
    assert rows[0]["level"] == "ok"
    with pytest.raises(ValueError):
        write_list(conn, out, "../x", ListFilter())

    monkeypatch.setenv("MIH_DATA_DIR", str(settings.data_dir))
    monkeypatch.setenv("MIH_EXPORT_DIR", str(tmp_path / "cli"))
    conn.commit()
    assert main(["list", "--name", "news", "--category", "news", "--no-playlist"]) == 0
    assert (tmp_path / "cli" / "news.json").exists()
    assert not (tmp_path / "cli" / "news.m3u").exists()


def test_presets_file(tmp_path):
    from mih.lists import DEFAULT_PRESETS, load_presets, preset_filter

    assert load_presets(None) == DEFAULT_PRESETS
    f = tmp_path / "lists.json"
    f.write_text('{"ru-news": {"country": "RU", "category": ["news"], "level": "doubtful"}}')
    flt = preset_filter(load_presets(f)["ru-news"])
    assert flt.countries == ("RU",) and flt.min_level == "doubtful"
    assert flt.exclude_categories == ("adult",)
    for bad in ('{"a b": {}}', '{"x": {"colour": 1}}', '{"x": {"level": "dead"}}', "[]", "{"):
        f.write_text(bad)
        with pytest.raises(ValueError):
            load_presets(f)


def test_health_job_rebuilds_lists_and_api_serves_them(tmp_path):
    from fastapi.testclient import TestClient

    from mih import db
    from mih.api.app import create_app
    from mih.config import Settings
    from mih.scheduler import run_job
    from mih.sources.service import create_source

    token = "t" * 32
    (tmp_path / "lists.json").write_text('{"ru": {"country": ["RU"]}, "all": {}}')
    settings = Settings(data_dir=tmp_path, api_token=token, lists_file=tmp_path / "lists.json")
    conn = db.connect(settings.db_path)
    db.migrate(conn)
    import_m3u(conn, settings, create_source(conn, settings, name="a", url=None),
               fixture_bytes("provider_a.m3u"))  # fmt: skip
    _set(conn, "Первый%", status="active", last_ok_at=format_ts(datetime.now(UTC)))
    _set(conn, "VTV1%", status="active", last_ok_at=format_ts(datetime.now(UTC)))
    conn.execute("UPDATE streams SET next_check_at = '2999-01-01'")

    event = run_job(conn, settings, "health")
    assert event["data"]["lists"] == {"ru": 1, "all": 2}

    with TestClient(create_app(settings)) as c:
        assert c.get("/lists/ru.m3u").status_code == 401
        assert c.get("/lists/ru.m3u", params={"token": "nope"}).status_code == 401
        m3u = c.get("/lists/ru.m3u", params={"token": token})
        assert m3u.status_code == 200 and "SECRET_A1" in m3u.text
        assert m3u.headers["content-type"].startswith("audio/x-mpegurl")
        auth = {"Authorization": f"Bearer {token}"}
        index = c.get("/api/v1/lists", headers=auth).json()["items"]
        assert [(i["name"], i["channels"]) for i in index] == [("all", 2), ("ru", 1)]
        assert c.get("/lists/nope.m3u", headers=auth).status_code == 404
        everything = c.get("/lists/catalog.jsonl", headers=auth)
        assert everything.headers["content-type"].startswith("application/x-ndjson")
        assert len(everything.text.splitlines()) == 3 and "SECRET_A1" in everything.text
        assert c.get("/lists/..%2Fmih.sqlite3", headers=auth).status_code in (404, 422)
    conn.close()


def test_catalog_file_has_everything_in_one_place(conn, settings, make_source, tmp_path):
    from mih.epg.importer import import_xmltv
    from mih.epg.service import create_epg_source
    from mih.lists import build_presets

    _load(conn, settings, make_source)
    now = datetime.now(UTC)
    _set(conn, "Первый%", status="active", last_ok_at=format_ts(now))
    _set(conn, "VTV1%", status="active", last_ok_at=format_ts(now))
    guide_now = datetime(2026, 10, 26, 4, 0, tzinfo=UTC)  # Доброе утро 03:00-06:00Z
    import_xmltv(conn, settings, create_epg_source(conn, settings, name="g", url=None),
                 fixture_bytes("guide_a.xml"), now=guide_now)  # fmt: skip
    out = tmp_path / "lists"
    build_presets(conn, out, {"ru": {"country": ["RU"]}, "vn": {"country": ["VN"]}})

    rows = [json.loads(line) for line in (out / "catalog.jsonl").read_text("utf-8").splitlines()]
    by_name = {r["name"]: r for r in rows}
    assert len(rows) == conn.execute("SELECT COUNT(*) FROM channels").fetchone()[0]

    perviy = by_name["Первый канал HD"]
    assert perviy["lists"] == ["ru"] and perviy["level"] == "ok" and perviy["epg_available"]
    assert {"первый", "первыйканал", "pervyykanal", "1tv"} <= set(perviy["search_keys"])
    # The +2 copy is a stream of its own with its shift, never merged into the base one.
    assert [(s["timeshift_min"], "SECRET_A1" in s["url"]) for s in perviy["streams"]] == [
        (0, True), (120, False)]  # fmt: skip
    assert all(s["stream_id"] and not s["requires_headers"] for s in perviy["streams"])

    vtv = next(r for r in rows if r["name"].startswith("VTV1"))
    assert vtv["lists"] == ["vn"]
    assert vtv["streams"][0]["headers"] == {"user_agent": "SomePlayer/1.0",
                                            "referer": "https://a.example/"}  # fmt: skip
    assert vtv["streams"][0]["requires_headers"] is True

    euronews = next(r for r in rows if r["name"].startswith("Euronews"))
    assert euronews["lists"] == [] and euronews["level"] == "unknown"
    assert euronews["reason"] == "unchecked"

    table = list(csv.DictReader((out / "catalog.csv").read_text("utf-8-sig").splitlines()))
    assert sum(r["channel_id"] == perviy["channel_id"] for r in table) == 2
    assert {r["user_agent"] for r in table} >= {"SomePlayer/1.0"}
    assert (out / "catalog.jsonl").stat().st_mode & 0o777 == 0o640

    from mih.export import build_catalog
    from mih.lists import catalog_rows

    on_air = {r["name"]: r for r in catalog_rows(conn, build_catalog(conn), {}, now=guide_now)}
    assert on_air["Первый канал HD"]["now"]["title"] == "Доброе утро"
    assert on_air["Первый канал HD"]["next"] == {"title": "Новости",
                                                 "start": "2026-10-26T06:00:00Z"}  # fmt: skip


def test_search_keys_cover_spelling_latin_and_popular_names():
    from mih.identity.search_keys import compact, search_keys

    keys = set(search_keys(["Россия-24"], ["Russia24.ru"]))
    assert {"россия24", "rossiya24", "russia24", "россия 24"} <= keys
    assert compact("Россия 24") in keys and compact("РОССИЯ-24") in keys
    assert "ртр" in search_keys(["Россия 1 HD"])
    assert {"нтв", "ntv"} <= set(search_keys(["НТВ"]))


def test_catalog_name_is_reserved(tmp_path):
    from mih.lists import load_presets

    f = tmp_path / "lists.json"
    f.write_text('{"catalog": {}}')
    with pytest.raises(ValueError):
        load_presets(f)
