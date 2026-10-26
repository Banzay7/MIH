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


def _upload_catalog_and_guide(client, guide: bytes) -> str:
    src = client.post("/api/v1/sources", json={"name": "a"}).json()
    files = {"file": ("a.m3u", fixture_bytes("provider_a.m3u"), "audio/x-mpegurl")}
    client.post(f"/api/v1/sources/{src['id']}/upload", files=files)
    epg = client.post("/api/v1/epg/sources", json={"name": "g"}).json()
    assert epg["kind"] == "xmltv" and epg["has_url"] is False
    files = {"file": ("g.xml", guide, "application/xml")}
    report = client.post(f"/api/v1/epg/sources/{epg['id']}/upload", files=files).json()
    assert report["status"] == "applied"
    return epg["id"]


def _channel_id(client, q):
    return client.get("/api/v1/channels", params={"q": q}).json()["items"][0]["id"]


def test_epg_schedule_programme_and_coverage(client):
    epg_id = _upload_catalog_and_guide(client, fixture_bytes("guide_a.xml"))
    snaps = client.get(f"/api/v1/epg/sources/{epg_id}/snapshots").json()["items"]
    assert snaps[0]["stats"]["airings_created"] == 9

    perviy = _channel_id(client, "первый")
    resp = client.get("/api/v1/airings", params={"channel_id": perviy,
                      "from": "2026-10-26T05:00:00Z", "to": "2026-10-26T10:00:00Z"})  # fmt: skip
    feeds = {f["timeshift_minutes"]: f for f in resp.json()["feeds"]}
    # Anything overlapping the window counts, so the morning show running since 03:00Z is in.
    assert [a["title"] for a in feeds[0]["airings"]] == [
        "Доброе утро", "Новости", "Жить здорово!", "Новости", "Время покажет"
    ]  # fmt: skip
    assert feeds[120]["airings"][0]["start"] == "2026-10-26T05:00:00Z"  # Доброе утро +2 h

    programme = client.get(f"/api/v1/programmes/{feeds[0]['airings'][1]['programme_id']}").json()
    assert programme["description"] == "Выпуск новостей"
    assert len(programme["airings"]) == 4  # two showings on the base feed and on +2

    listing = {c["canonical_name"]: c["has_epg"] for c in client.get("/api/v1/channels").json()[
        "items"]}  # fmt: skip
    # AT-09: a channel without EPG is still in the catalog, marked as such.
    assert listing["Первый канал HD"] is True and listing["Euronews (Europe)"] is False
    detail = client.get(f"/api/v1/channels/{_channel_id(client, 'euronews')}").json()
    assert all(f["epg"] == {"available": False} for f in detail["feeds"])

    summary = client.get("/api/v1/metrics/summary").json()
    assert summary["channels_with_epg"] == 2 and summary["epg_mappings_needs_review"] == 1


def test_epg_review_queue_and_manual_mapping(client):
    _upload_catalog_and_guide(client, fixture_bytes("guide_a.xml"))
    queue = client.get("/api/v1/epg/channels", params={"mapping": "needs_review"}).json()
    [euronews] = queue["items"]
    assert euronews["xmltv_id"] == "Euronews.fr" and len(euronews["candidates"]) == 2

    put = client.put(f"/api/v1/epg/channels/{euronews['id']}/mapping",
                     json={"feed_id": euronews["candidates"][0]})  # fmt: skip
    assert put.json()["decision"] == "approved"
    bad = client.put(f"/api/v1/epg/channels/{euronews['id']}/mapping", json={"feed_id": "x"})
    assert bad.status_code == 422
    assert client.get("/api/v1/epg/channels", params={"mapping": "needs_review"}).json()[
        "items"] == []  # fmt: skip

    reset = client.delete(f"/api/v1/epg/channels/{euronews['id']}/mapping").json()
    assert reset["decision"] == "needs_review"


def test_now_and_next(client):
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC).replace(second=0, microsecond=0)

    def t(minutes):
        return (now + timedelta(minutes=minutes)).strftime("%Y%m%d%H%M%S +0000")

    guide_xml = (
        f'<tv><programme start="{t(-30)}" stop="{t(30)}" channel="Perviy.ru">'
        "<title>Сейчас</title></programme>"
        f'<programme start="{t(30)}" stop="{t(90)}" channel="Perviy.ru">'
        "<title>Потом</title></programme></tv>"
    ).encode()
    _upload_catalog_and_guide(client, guide_xml)
    items = client.get("/api/v1/now").json()["items"]
    assert [(i["now"]["title"], i["next"]["title"]) for i in items] == [("Сейчас", "Потом")]
    shifted = client.get("/api/v1/now", params={"timeshift": True}).json()["items"]
    assert len(shifted) == 2  # the +2 feed is two hours behind: nothing yet, "Сейчас" next
    plus2 = next(i for i in shifted if i["now"] is None)
    assert plus2["next"]["title"] == "Сейчас"
