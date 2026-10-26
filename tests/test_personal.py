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
