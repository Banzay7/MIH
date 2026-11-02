import httpx
import pytest
from fastapi.testclient import TestClient

from mih import db, openspeed, playback
from mih.api.app import create_app
from mih.config import Settings
from tests.conftest import fixture_bytes

TOKEN = "t" * 32


def _client(tmp_path, **extra):
    app = create_app(Settings(data_dir=tmp_path, api_token=TOKEN, **extra))
    if "ha_url" in extra:
        app.state.ha_transport = httpx.MockTransport(lambda r: httpx.Response(200, json=[]))
    c = TestClient(app)
    c.__enter__()
    c.headers["Authorization"] = f"Bearer {TOKEN}"
    src = c.post("/api/v1/sources", json={"name": "a"}).json()
    files = {"file": ("a.m3u", fixture_bytes("provider_a.m3u"), "audio/x-mpegurl")}
    c.post(f"/api/v1/sources/{src['id']}/upload", files=files)
    return c


@pytest.fixture
def client(tmp_path):
    c = _client(tmp_path)
    yield c
    c.__exit__(None, None, None)


def _channel(client, q):
    return client.get("/api/v1/channels", params={"q": q}).json()["items"][0]["id"]


def _agent_launch(client, channel_id, ms=None, *, ok=True, error=None):
    body = {"target": {"channel_id": channel_id}, "adapter": "agent"}
    client.post("/api/v1/playback/commands", json=body)
    conn = db.connect(client.app.state.settings.db_path)
    (taken,) = playback.take_commands(conn)
    out = playback.finish_command(conn, taken["id"], ok, taken["stream_id"], error, ms=ms)
    conn.close()
    return out


def test_agent_success_report_times_the_open_and_slow_channels_go_last(client):
    perviy, euronews = _channel(client, "первый"), _channel(client, "euronews")
    names = [c["id"] for c in client.get("/api/v1/channels").json()["items"]]
    assert names.index(euronews) < names.index(perviy)  # by name: E before П

    assert _agent_launch(client, euronews, ms=9000)["state"] == "started"
    _agent_launch(client, perviy, ms=1500)
    items = client.get("/api/v1/channels").json()["items"]
    by_id = {c["id"]: c for c in items}
    assert by_id[euronews]["open"] == {"ms": 9000, "n": 1, "grade": "slow", "slow": True}
    assert by_id[perviy]["open"]["grade"] == "fast"
    assert [c["id"] for c in items][-1] == euronews  # slow: last

    # A higher threshold re-grades the history: euronews is good again, back in name order.
    resp = client.put("/api/v1/settings/open-speed", json={"slow_s": 10})
    assert resp.json()["slow_s"] == 10.0 and resp.json()["default_s"] == 5.0
    items = client.get("/api/v1/channels").json()["items"]
    ids = [c["id"] for c in items]
    assert ids.index(euronews) < ids.index(perviy)
    assert {c["id"]: c for c in items}[euronews]["open"]["slow"] is False
    assert client.put("/api/v1/settings/open-speed", json={"slow_s": 0.1}).status_code == 422

    detail = client.get(f"/api/v1/channels/{euronews}").json()
    assert detail["open_slow_s"] == 10.0
    assert [(h["open_ms"], h["source"], h["outcome"]) for h in detail["open_history"]] == [
        (9000, "agent", "opened")
    ]


def test_slow_start_is_kept_as_slow_and_a_silent_start_is_not_timed(client):
    perviy = _channel(client, "первый")
    conn = db.connect(client.app.state.settings.db_path)
    body = {"target": {"channel_id": perviy}, "adapter": "agent"}
    cmd = client.post("/api/v1/playback/commands", json=body).json()
    (taken,) = playback.take_commands(conn)
    playback.set_offered(conn, taken["id"], [taken["stream_id"]])
    out = playback.finish_command(conn, cmd["id"], False, taken["stream_id"], "timeout", ms=30000)
    assert out["detail"] == "unconfirmed"
    assert [h["outcome"] for h in openspeed.history(conn, perviy)] == ["slow"]
    assert openspeed.summary(conn)[perviy]["grade"] == "very_slow"

    # The agent leaves it playing and says nothing: no time is known, nothing is kept.
    euronews = _channel(client, "euronews")
    client.post("/api/v1/playback/commands",
                json={"target": {"channel_id": euronews}, "adapter": "agent"})  # fmt: skip
    playback.take_commands(conn)
    assert euronews not in openspeed.summary(conn)
    conn.close()


def test_home_assistant_launch_is_timed_from_the_button(tmp_path):
    c = _client(tmp_path, ha_url="http://ha.local:8123", ha_token="h" * 40,
                ha_script="mih_play")  # fmt: skip
    perviy = _channel(c, "первый")
    cmd = c.post("/api/v1/playback/commands",
                 json={"target": {"channel_id": perviy}, "adapter": "ha"}).json()  # fmt: skip
    assert cmd["state"] == "started"
    (h,) = c.get(f"/api/v1/channels/{perviy}").json()["open_history"]
    assert h["source"] == "ha" and h["outcome"] == "opened" and h["open_ms"] < 5000
    c.__exit__(None, None, None)


def test_player_report_with_startup_time_is_kept(client):
    perviy = _channel(client, "первый")
    stream = client.get(f"/api/v1/channels/{perviy}").json()["feeds"][0]["streams"][0]["id"]
    client.post(f"/api/v1/streams/{stream}/playback",
                json={"result": "started", "startup_ms": 2300})  # fmt: skip
    client.post(f"/api/v1/streams/{stream}/playback", json={"result": "failed", "error": "x"})
    (h,) = client.get(f"/api/v1/channels/{perviy}").json()["open_history"]
    assert (h["open_ms"], h["source"]) == (2300, "device")


def test_recent_list_keeps_newest_first_and_clears_without_losing_speed(client):
    perviy, euronews = _channel(client, "первый"), _channel(client, "euronews")
    _agent_launch(client, perviy, ms=1200)
    _agent_launch(client, euronews, ms=7000)
    _agent_launch(client, perviy, ms=1400)
    items = client.get("/api/v1/recent").json()["items"]
    assert {i["channel_id"] for i in items} == {perviy, euronews}
    assert {i["channel_id"]: i["times"] for i in items}[perviy] == 2
    assert {i["channel_id"]: i["open"]["ms"] for i in items}[perviy] == 1300  # median

    assert client.delete("/api/v1/recent").status_code == 204
    assert client.get("/api/v1/recent").json()["items"] == []
    assert client.get(f"/api/v1/channels/{perviy}").json()["open"]["n"] == 2


def test_grade_scale_follows_the_threshold():
    assert [openspeed.grade(ms, 5000) for ms in (2000, 2001, 5000, 10000, 10001)] == [
        "fast", "ok", "ok", "slow", "very_slow",
    ]  # fmt: skip
    assert openspeed.slow_last([1, 2, 3, 4], str, {"1", "3"}) == [2, 4, 1, 3]
