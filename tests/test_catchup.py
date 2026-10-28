import json
from datetime import UTC, datetime, timedelta

import httpx

from mih import catchup, db, playback
from tests.test_personal import TOKEN, _channel, _ha_client  # noqa: F401

START = datetime(2026, 10, 29, 10, 0, tzinfo=UTC)
STOP = START + timedelta(hours=1)
NOW = START + timedelta(minutes=20)
LIVE = "http://s.example/live/1.m3u8"


def test_template_with_start_and_duration():
    attrs = {"catchup": "default",
             "catchup-source": "http://s.example/arch?utc=${start}&dur=${duration}"}  # fmt: skip
    url = catchup.build_url(attrs, LIVE, START, STOP, NOW)
    assert url == f"http://s.example/arch?utc={int(START.timestamp())}&dur=3600"


def test_append_adds_the_query_to_the_live_url():
    attrs = {"catchup": "append", "catchup-source": "?offset=${offset}"}
    assert catchup.build_url(attrs, LIVE, START, STOP, NOW) == LIVE + "?offset=1200"


def test_flussonic_builds_the_archive_path():
    url = catchup.build_url({"catchup": "flussonic"}, LIVE, START, STOP, NOW)
    assert url == f"http://s.example/live/video-{int(START.timestamp())}-3600.m3u8"


def test_no_rule_or_too_old_gives_none():
    assert catchup.build_url({}, LIVE, START, STOP, NOW) is None
    odd = {"catchup": "unknown", "catchup-source": "x"}
    assert catchup.build_url(odd, LIVE, START, STOP, NOW) is None
    old = {"catchup": "shift", "catchup-days": "1"}
    assert catchup.build_url(old, LIVE, START, STOP, START + timedelta(days=3)) is None
    assert catchup.build_url(old, LIVE, START, STOP, NOW).startswith(LIVE + "?utc=")


def _run(c, perviy, start, stop):
    calls = []
    c.app.state.ha_transport = httpx.MockTransport(
        lambda r: calls.append(json.loads(r.content)) or httpx.Response(200, json=[])
    )
    settings = c.app.state.settings
    conn = db.connect(settings.db_path)
    target = playback.Target("channel", perviy, None, start.isoformat(), stop.isoformat(), "News")
    cmd, _ = playback.create_command(conn, target, mode="ha")
    out = playback.run_ha(conn, settings, cmd["id"], "Первый", transport=c.app.state.ha_transport)
    conn.commit()
    conn.close()
    return out, calls[0]


def test_ha_command_for_a_running_programme_passes_the_catchup_url(tmp_path):
    c = _ha_client(tmp_path, lambda r: httpx.Response(200, json=[]))
    perviy = _channel(c, "первый")
    conn = db.connect(c.app.state.settings.db_path)
    for row in conn.execute("SELECT id, normalized_payload FROM source_records").fetchall():
        p = json.loads(row["normalized_payload"])
        p.setdefault("attrs", {}).update({"catchup": "append", "catchup-source": "?utc=${start}"})
        conn.execute("UPDATE source_records SET normalized_payload = ? WHERE id = ?",
                     (json.dumps(p), row["id"]))  # fmt: skip
    conn.commit()
    conn.close()
    now = datetime.now(UTC)
    out, sent = _run(c, perviy, now - timedelta(minutes=20), now + timedelta(minutes=40))
    assert out["state"] == "started" and out["detail"] is None
    assert sent["program"] == "News" and sent["start_ts"]
    assert sent["catchup_url"].startswith(sent["url"]) and "?utc=" in sent["catchup_url"]
    c.__exit__(None, None, None)


def test_ha_command_without_a_catchup_rule_plays_live_and_says_so(tmp_path):
    c = _ha_client(tmp_path, lambda r: httpx.Response(200, json=[]))
    perviy = _channel(c, "первый")
    now = datetime.now(UTC)
    out, sent = _run(c, perviy, now - timedelta(minutes=20), now + timedelta(minutes=40))
    assert out["state"] == "started" and out["detail"] == "live_no_catchup"
    assert "catchup_url" not in sent and sent["url"]
    # A programme that has not started yet is not a catch-up: plain live.
    out, sent = _run(c, perviy, now + timedelta(hours=1), now + timedelta(hours=2))
    assert out["detail"] is None and "catchup_url" not in sent
    c.__exit__(None, None, None)


def test_unknown_airing_is_not_found(tmp_path):
    c = _ha_client(tmp_path, lambda r: httpx.Response(200, json=[]))
    perviy = _channel(c, "первый")
    target = {"channel_id": perviy, "airing_id": "nope"}
    resp = c.post("/api/v1/playback/commands", json={"target": target, "adapter": "ha"})
    assert resp.status_code == 404
    c.__exit__(None, None, None)
