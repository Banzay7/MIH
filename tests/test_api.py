import pytest
from fastapi.testclient import TestClient

from mih.api.app import create_app
from mih.config import ConfigError, Settings
from tests.conftest import fixture_bytes

TOKEN = "t" * 32


@pytest.fixture
def client(tmp_path):
    app = create_app(Settings(data_dir=tmp_path, api_token=TOKEN))
    with TestClient(app) as c:
        c.headers["Authorization"] = f"Bearer {TOKEN}"
        yield c


def test_liveness_needs_no_token_but_everything_else_does(client):
    anon = TestClient(client.app)
    assert anon.get("/api/v1/health/live").status_code == 200
    assert anon.get("/api/v1/channels").status_code == 401
    bad = anon.get("/api/v1/channels", headers={"Authorization": "Bearer nope"})
    assert bad.status_code == 401


def test_upload_then_browse_without_leaking_urls(client):
    src = client.post("/api/v1/sources", json={"name": "a"}).json()
    assert src["has_url"] is False

    files = {"file": ("a.m3u", fixture_bytes("provider_a.m3u"), "audio/x-mpegurl")}
    report = client.post(f"/api/v1/sources/{src['id']}/upload", files=files).json()
    assert report["status"] == "applied"

    listing = client.get("/api/v1/channels", params={"q": "первый"}).json()
    assert [c["canonical_name"] for c in listing["items"]] == ["Первый канал HD"]

    detail = client.get(f"/api/v1/channels/{listing['items'][0]['id']}")
    assert detail.status_code == 200
    body = detail.text
    assert "SECRET" not in body and "a.example/live" not in body
    assert {f["offset_minutes"] for f in detail.json()["feeds"]} == {0, 120}

    snaps = client.get(f"/api/v1/sources/{src['id']}/snapshots").json()["items"]
    assert snaps[0]["status"] == "applied"


def test_errors_have_machine_codes(client):
    resp = client.get("/api/v1/channels/does-not-exist")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"
    client.post("/api/v1/sources", json={"name": "dup"})
    dup = client.post("/api/v1/sources", json={"name": "dup"})
    assert dup.status_code == 409


@pytest.mark.parametrize(
    ("host", "token", "ok"),
    [
        ("127.0.0.1", None, True),
        ("0.0.0.0", None, False),
        ("0.0.0.0", "short", False),
        ("0.0.0.0", TOKEN, True),
    ],
)
def test_refuses_to_serve_publicly_without_a_token(tmp_path, host, token, ok):
    s = Settings(data_dir=tmp_path, bind_host=host, api_token=token)
    if ok:
        s.validate_for_serving()
    else:
        with pytest.raises(ConfigError):
            s.validate_for_serving()
