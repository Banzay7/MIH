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
