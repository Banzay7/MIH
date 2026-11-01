"""Channels 18+ in the web UI: hidden, shown or opened with a PIN (mih.api.adult)."""

import pytest
from fastapi.testclient import TestClient

from mih.api.app import create_app
from mih.config import Settings
from mih.export import build_catalog
from mih.lists import store_tags

TOKEN = "t" * 32
M3U = b"""#EXTM3U
#EXTINF:-1 group-title="News",Daily News
http://a.example/news.m3u8
#EXTINF:-1 group-title="Relax",Blue Lagoon
http://cdn.example/xxx/lagoon.m3u8
"""


@pytest.fixture
def client(tmp_path):
    app = create_app(Settings(data_dir=tmp_path, api_token=TOKEN))
    # A proxy's address, as on the stand: wrong PINs are counted per forwarded address.
    with TestClient(app, client=("10.0.0.2", 50000)) as c:
        c.headers["Authorization"] = f"Bearer {TOKEN}"
        src = c.post("/api/v1/sources", json={"name": "a"}).json()
        c.post(f"/api/v1/sources/{src['id']}/upload",
               files={"file": ("a.m3u", M3U, "audio/x-mpegurl")})  # fmt: skip
        pool = app.state.pool
        conn = pool.take()
        try:
            store_tags(conn, build_catalog(conn))
        finally:
            pool.give(conn)
        yield c


def _names(client, **headers):
    items = client.get("/api/v1/channels", params={"limit": 1000}, headers=headers).json()
    return {i["canonical_name"]: i["id"] for i in items["items"]}


def test_hidden_by_default(client):
    assert client.get("/api/v1/settings/adult").json()["mode"] == "hide"
    names = _names(client)
    assert "Daily News" in names and "Blue Lagoon" not in names
    lagoon = client.get("/api/v1/channels", params={"q": "lagoon"}).json()["items"]
    assert lagoon == []

    client.put("/api/v1/settings/adult", json={"mode": "show"})
    lagoon_id = _names(client)["Blue Lagoon"]
    client.put("/api/v1/settings/adult", json={"mode": "hide"})
    r = client.get(f"/api/v1/channels/{lagoon_id}")
    assert r.status_code == 403 and r.json()["error"]["code"] == "adult_locked"
    play = client.post("/api/v1/playback/commands",
                       json={"target": {"channel_id": lagoon_id}})  # fmt: skip
    assert play.status_code == 403


def test_pin_opens_for_a_while_and_guards_the_setting(client):
    assert client.put("/api/v1/settings/adult", json={"mode": "pin"}).status_code == 422
    r = client.put("/api/v1/settings/adult", json={"mode": "pin", "pin": "1234"}).json()
    assert r == {"mode": "pin", "has_pin": True, "visible": False, "unlock_hours": 4}
    assert "Blue Lagoon" not in _names(client)

    bad = client.post("/api/v1/adult/unlock", json={"pin": "0000"})
    assert bad.status_code == 403 and bad.json()["error"]["code"] == "wrong_pin"
    token = client.post("/api/v1/adult/unlock", json={"pin": "1234"}).json()["token"]
    assert "Blue Lagoon" in _names(client, **{"X-MIH-Adult": token})
    assert "Blue Lagoon" not in _names(client, **{"X-MIH-Adult": token + "0"})

    # Without the PIN the mode stays as it is.
    assert client.put("/api/v1/settings/adult", json={"mode": "show"}).status_code == 403
    ok = client.put("/api/v1/settings/adult", json={"mode": "show", "current_pin": "1234"})
    assert ok.json()["mode"] == "show" and ok.json()["has_pin"] is False


def test_guessing_the_pin_is_stopped(client):
    client.put("/api/v1/settings/adult", json={"mode": "pin", "pin": "4321"})
    fwd = {"X-Forwarded-For": "203.0.113.7"}
    codes = [client.post("/api/v1/adult/unlock", json={"pin": f"{i:04d}"}, headers=fwd).status_code
             for i in range(6)]  # fmt: skip
    assert codes[:5] == [403] * 5 and codes[5] == 429
    # even the right PIN waits now; another address does not
    assert client.post("/api/v1/adult/unlock", json={"pin": "4321"}, headers=fwd).status_code == 429
    other = {"X-Forwarded-For": "198.51.100.9"}
    assert (
        client.post("/api/v1/adult/unlock", json={"pin": "4321"}, headers=other).status_code == 200
    )
