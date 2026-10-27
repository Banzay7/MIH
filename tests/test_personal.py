import pytest
from fastapi.testclient import TestClient

from mih import db
from mih.api.app import create_app
from mih.config import Settings
from tests.conftest import fixture_bytes

TOKEN = "t" * 32


@pytest.fixture
def client(tmp_path):
    app = create_app(Settings(data_dir=tmp_path, api_token=TOKEN))
    with TestClient(app) as c:
        c.headers["Authorization"] = f"Bearer {TOKEN}"
        src = c.post("/api/v1/sources", json={"name": "a"}).json()
        files = {"file": ("a.m3u", fixture_bytes("provider_a.m3u"), "audio/x-mpegurl")}
        c.post(f"/api/v1/sources/{src['id']}/upload", files=files)
        yield c


def _channel(client, q):
    return client.get("/api/v1/channels", params={"q": q}).json()["items"][0]["id"]


def test_favorites_keep_order_survive_repeats_and_can_be_removed(client):
    perviy, euronews = _channel(client, "первый"), _channel(client, "euronews")
    url = "/api/v1/profiles/default/favorites"
    for cid in (euronews, perviy, euronews):  # the repeat is a no-op, not a second row
        resp = client.post(url, json={"target_kind": "channel", "target_id": cid})
        assert resp.status_code == 201
    items = client.get(url).json()["items"]
    assert [i["target_id"] for i in items] == [euronews, perviy]
    assert items[1]["name"] == "Первый канал HD"
    assert items[1]["logo_url"] == "https://img.example/1tv.png"

    missing = client.post(url, json={"target_kind": "channel", "target_id": "nope"})
    assert missing.status_code == 404
    assert client.post(url, json={"target_kind": "stream", "target_id": perviy}).status_code == 422
    assert client.get("/api/v1/profiles/Bad Id/favorites").status_code == 422

    assert client.delete(f"{url}/channel/{euronews}").status_code == 204
    assert [i["target_id"] for i in client.get(url).json()["items"]] == [perviy]
    assert TestClient(client.app).get(url).status_code == 401


def test_play_picks_a_stream_without_leaking_its_url_and_says_no_adapter(client):
    perviy = _channel(client, "первый")
    body = {"target": {"channel_id": perviy}, "idempotency_key": "key-0001"}
    resp = client.post("/api/v1/playback/commands", json=body)
    assert resp.status_code == 202
    cmd = resp.json()
    assert cmd["state"] == "failed" and cmd["error"] == "no_adapter"
    assert cmd["stream_id"] and "SECRET" not in resp.text and "a.example" not in resp.text

    # The base feed's stream is picked, not the +2 one.
    detail = client.get(f"/api/v1/channels/{perviy}").json()
    base = next(f for f in detail["feeds"] if f["offset_minutes"] == 0)
    assert cmd["stream_id"] in {s["id"] for s in base["streams"]}

    again = client.post("/api/v1/playback/commands", json=body)
    assert again.status_code == 200 and again.json()["id"] == cmd["id"]
    assert client.get(f"/api/v1/playback/commands/{cmd['id']}").json() == cmd
    assert client.get("/api/v1/playback/commands/nope").status_code == 404


def test_play_names_feed_and_reports_missing_streams(client, tmp_path):
    perviy = _channel(client, "первый")
    detail = client.get(f"/api/v1/channels/{perviy}").json()
    plus2 = next(f for f in detail["feeds"] if f["offset_minutes"] == 120)
    cmd = client.post(
        "/api/v1/playback/commands", json={"target": {"channel_id": perviy, "feed_id": plus2["id"]}}
    ).json()
    assert cmd["stream_id"] in {s["id"] for s in plus2["streams"]}
    assert cmd["target"] == {"kind": "channel", "channel_id": perviy, "feed_id": plus2["id"]}

    other = _channel(client, "euronews")
    wrong = client.post(
        "/api/v1/playback/commands", json={"target": {"channel_id": other, "feed_id": plus2["id"]}}
    )
    assert wrong.status_code == 422
    unknown = client.post("/api/v1/playback/commands", json={"target": {"channel_id": "x"}})
    assert unknown.status_code == 404

    conn = db.connect(tmp_path / "mih.sqlite3")
    conn.execute("UPDATE streams SET status = 'quarantine'")
    conn.close()
    dead = client.post("/api/v1/playback/commands", json={"target": {"channel_id": perviy}}).json()
    assert dead["error"] == "no_stream" and dead["stream_id"] is None


def test_web_ui_is_served_without_a_token_and_api_still_needs_one(client):
    anon = TestClient(client.app)
    page = anon.get("/")
    assert page.status_code == 200 and "MIRTVONLINE" in page.text
    assert anon.get("/app.js").status_code == 200
    assert anon.get("/manifest.webmanifest").status_code == 200
    assert anon.get("/api/v1/channels").status_code == 401
    assert anon.get("/api/docs").status_code == 200


def test_tracked_topics_are_folded_and_removable(client):
    url = "/api/v1/profiles/default/topics"
    assert client.post(url, json={"topic": "  Футбол  Ёлки "}).json()["topic"] == "футбол елки"
    client.post(url, json={"topic": "ФУТБОЛ ЕЛКИ"})  # same topic once folded
    assert [i["topic"] for i in client.get(url).json()["items"]] == ["футбол елки"]
    assert client.delete(f"{url}/Футбол Ёлки").status_code == 204
    assert client.get(url).json()["items"] == []
    assert client.post(url, json={"topic": "x"}).status_code == 422


def _ha_client(tmp_path, handler, **kw):
    import httpx

    tmp_path.mkdir(exist_ok=True)
    settings = Settings(data_dir=tmp_path, api_token=TOKEN, playback="agent",
                        ha_url="http://ha.test:8123", ha_token="h" * 40, **kw)  # fmt: skip
    app = create_app(settings)
    app.state.ha_transport = httpx.MockTransport(handler)
    c = TestClient(app)
    c.__enter__()
    c.headers["Authorization"] = f"Bearer {TOKEN}"
    src = c.post("/api/v1/sources", json={"name": "a"}).json()
    files = {"file": ("a.m3u", fixture_bytes("provider_a.m3u"), "audio/x-mpegurl")}
    c.post(f"/api/v1/sources/{src['id']}/upload", files=files)
    return c


def test_play_through_home_assistant_runs_the_script_with_stream_urls(tmp_path):
    import json

    import httpx

    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=[])

    c = _ha_client(tmp_path, handler, ha_fields={"channel": "name", "stream_id": ""})
    assert c.get("/api/v1/playback/adapters").json() == {
        "default": "agent",
        "available": ["agent", "ha"],
        "ha_script": "script.mih_play",
    }
    perviy = _channel(c, "первый")
    resp = c.post("/api/v1/playback/commands",
                  json={"target": {"channel_id": perviy}, "adapter": "ha"})  # fmt: skip
    cmd = resp.json()
    assert cmd["state"] == "started" and cmd["adapter"] == "ha" and cmd["error"] is None
    # The stream URL goes to Home Assistant, never back to the browser.
    assert "a.example" not in resp.text
    (req,) = calls
    assert req.url.path == "/api/services/script/mih_play"
    assert req.headers["authorization"] == "Bearer " + "h" * 40
    sent = json.loads(req.content)
    assert sent["name"] == "Первый канал HD" and "channel" not in sent and "stream_id" not in sent
    assert sent["url"] == sent["urls"][0] and sent["channel_id"] == perviy
    assert sent["command_id"] == cmd["id"]
    # The agent never sees an HA command.
    from mih import playback

    conn = db.connect(c.app.state.settings.db_path)
    assert playback.take_commands(conn) == []
    conn.close()
    c.__exit__(None, None, None)


@pytest.mark.parametrize(("status", "error"), [
    (401, "ha_unauthorized"), (404, "ha_script_not_found"), (500, "ha_script_failed"),
    (None, "ha_unreachable"),
])  # fmt: skip
def test_play_through_home_assistant_reports_its_failure(tmp_path, status, error):
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        if status is None:
            raise httpx.ConnectError("refused")
        return httpx.Response(status)

    c = _ha_client(tmp_path, handler)
    body = {"target": {"channel_id": _channel(c, "первый")}, "adapter": "ha"}
    cmd = c.post("/api/v1/playback/commands", json=body).json()
    assert (cmd["state"], cmd["error"]) == ("failed", error)
    check = c.post("/api/v1/playback/ha/check").json()
    assert check["ok"] is False and check["error"] == error.replace("_script_failed", "_error")
    c.__exit__(None, None, None)


def test_home_assistant_check_and_unset_bridge(tmp_path, client):
    import httpx

    c = _ha_client(tmp_path / "ha", lambda r: httpx.Response(200, json={"state": "off"}))
    assert c.post("/api/v1/playback/ha/check").json() == {
        "configured": True,
        "script": "script.mih_play",
        "ok": True,
        "error": None,
    }
    c.__exit__(None, None, None)
    # Without MIH_HA_URL/MIH_HA_TOKEN the bridge is not offered and cannot be asked for.
    assert client.get("/api/v1/playback/adapters").json()["available"] == ["agent"]
    perviy = _channel(client, "первый")
    resp = client.post("/api/v1/playback/commands",
                       json={"target": {"channel_id": perviy}, "adapter": "ha"})  # fmt: skip
    assert resp.status_code == 422
