import pytest
from fastapi.testclient import TestClient

from mih import db
from mih.api.app import create_app
from mih.config import ConfigError, Settings
from tests.conftest import fixture_bytes

TOKEN = "t" * 32


@pytest.fixture
def client(tmp_path):
    # The fixture guides are dated 2026-10-26: keep them past the 3-day retention.
    app = create_app(Settings(data_dir=tmp_path, api_token=TOKEN, epg_retention_days=36500))
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
    # Nothing is tracked yet, so the default scope keeps every channel in the catalog:
    # Россия 1 has no feed here and its programme is not stored.
    assert snaps[0]["stats"]["airings_created"] == 8
    assert snaps[0]["stats"]["channels_skipped"] == 1

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

    # One page of what is on: on_air, soon, playable_only, limit/offset, total.
    page = client.get("/api/v1/now", params={"timeshift": True, "on_air": True, "limit": 1}).json()
    assert page["total"] == 1 and page["items"][0]["now"]["title"] == "Сейчас"
    assert page["channels"] >= page["guided"] == 1
    assert page["items"][0]["streams"] >= 1 and "logo_url" in page["items"][0]
    soon = client.get("/api/v1/now", params={"timeshift": True, "soon": 45}).json()
    assert [i["next"]["title"] for i in soon["items"]] == ["Потом"]
    assert client.get("/api/v1/now", params={"soon": 10}).json()["total"] == 0
    rest = client.get("/api/v1/now", params={"timeshift": True, "limit": 1, "offset": 1}).json()
    assert rest["total"] == 2 and len(rest["items"]) == 1


def test_search_endpoint(client):
    _upload_catalog_and_guide(client, fixture_bytes("guide_a.xml"))
    body = {"query": "новости", "filters": {"from": "2026-10-26T00:00:00Z",
            "to": "2026-10-27T00:00:00Z"}, "tz": "Europe/Moscow"}  # fmt: skip
    result = client.post("/api/v1/search", json=body).json()
    airings = [i for i in result["items"] if i["type"] == "airing"]
    # "новости" also finds the Vietnamese news ("Thời sự"), in start order.
    assert [(a["title"], a["more_airings"]) for a in airings] == [("Thời sự", 0), ("Новости", 1)]
    assert result["query"]["time"] == "custom"

    bad_tz = client.post("/api/v1/search", json={"query": "x", "tz": "Mars/Base"})
    assert bad_tz.status_code == 422
    bad_type = client.post("/api/v1/search", json={"query": "x", "target_types": ["stream"]})
    assert bad_type.status_code == 422
    assert "SECRET" not in client.post("/api/v1/search", json={"query": "первый"}).text


def test_epg_source_can_be_switched_off_and_deleted(client, tmp_path):
    epg_id = _upload_catalog_and_guide(client, fixture_bytes("guide_a.xml"))
    blobs = tmp_path / "snapshots" / "epg" / epg_id
    assert blobs.exists()
    assert client.get("/api/v1/metrics/summary").json()["channels_with_epg"] > 0

    off = client.patch(f"/api/v1/epg/sources/{epg_id}", json={"enabled": False}).json()
    assert off["enabled"] is False

    gone = client.delete(f"/api/v1/epg/sources/{epg_id}").json()["deleted"]
    assert gone["guide_channels"] == 5 and gone["programmes"] > 0
    assert client.get("/api/v1/epg/sources").json()["items"] == []
    assert client.get("/api/v1/metrics/summary").json()["channels_with_epg"] == 0
    assert client.delete(f"/api/v1/epg/sources/{epg_id}").status_code == 404
    assert not blobs.exists()


def test_api_answers_are_gzipped_and_the_whole_catalog_fits_one_page(client):
    src = client.post("/api/v1/sources", json={"name": "a"}).json()
    files = {"file": ("a.m3u", fixture_bytes("provider_a.m3u"), "audio/x-mpegurl")}
    client.post(f"/api/v1/sources/{src['id']}/upload", files=files)
    r = client.get("/api/v1/channels?limit=10000", headers={"Accept-Encoding": "gzip"})
    assert r.status_code == 200 and {i["streams"] for i in r.json()["items"]} >= {1}
    big = client.get("/api/v1/openapi.json", headers={"Accept-Encoding": "gzip"})
    assert big.headers.get("content-encoding") == "gzip"  # answers over 1 KB
    # The web UI's script is compressed too; playlists and guides never are (players).
    ui = client.get("/app.js", headers={"Accept-Encoding": "gzip"})
    assert ui.headers.get("content-encoding") == "gzip"


def test_shared_answers_carry_an_etag_and_change_after_an_import(client):
    first = client.get("/api/v1/channels?limit=10000")
    assert first.json()["items"] == [] and first.headers["cache-control"].startswith("private")
    again = client.get("/api/v1/channels?limit=10000",
                       headers={"If-None-Match": first.headers["etag"]})  # fmt: skip
    assert again.status_code == 304 and again.content == b""
    src = client.post("/api/v1/sources", json={"name": "a"}).json()
    files = {"file": ("a.m3u", fixture_bytes("provider_a.m3u"), "audio/x-mpegurl")}
    client.post(f"/api/v1/sources/{src['id']}/upload", files=files)
    after = client.get("/api/v1/channels?limit=10000",
                       headers={"If-None-Match": first.headers["etag"]})  # fmt: skip
    assert after.status_code == 200 and after.json()["items"]
    # A search is per query: never shared.
    assert "etag" not in client.get("/api/v1/channels?q=perviy").headers


def test_a_stale_shared_answer_is_served_while_another_request_rebuilds_it():
    import threading

    from mih.api.cache import SharedCache

    cache, builds, release = SharedCache(), [], threading.Event()

    def build(n):
        def go():
            builds.append(n)
            if n == 2:
                release.wait(5)
            return {"n": n}

        return go

    assert cache.get("k", 0.0, build(1)).body == b'{"n":1}'
    rebuild = threading.Thread(target=lambda: cache.get("k", 0.0, build(2)))
    rebuild.start()
    while builds[-1] != 2:
        pass
    # The rebuild is under way: the old answer comes back at once, nothing is built.
    assert cache.get("k", 0.0, build(3)).body == b'{"n":1}'
    release.set()
    rebuild.join()
    assert builds == [1, 2]


def test_the_data_version_moves_with_background_changes_and_drops_shared_answers(
    client, monkeypatch
):
    from mih.api import cache as cache_mod

    monkeypatch.setattr(cache_mod, "VERSION_EVERY", 0.0)
    anon = TestClient(client.app)
    v1 = anon.get("/api/v1/data-version").json()["data"]
    assert v1 and anon.get("/api/v1/data-version").json()["data"] == v1  # stable while idle
    first = client.get("/api/v1/channels?limit=10000")
    assert first.headers["x-mih-data"] == v1
    assert first.headers["cache-control"] == "private, no-cache"
    shared = client.app.state.cache
    assert shared._items
    # A job finishing in the scheduler's own process: the API only sees the database.
    other = db.connect(client.app.state.settings.db_path)
    with other:
        other.execute("INSERT OR REPLACE INTO jobs (name, last_finished_at) VALUES ('epg', 'x')")
    other.close()
    v2 = client.get("/api/v1/metrics/summary").headers["x-mih-data"]
    assert v2 != v1 and not any(k.startswith("channels:") for k in shared._items)
    # Personal lists are part of it: another device sees the change.
    assert client.post("/api/v1/profiles/default/topics", json={"topic": "футбол"}).is_success
    v3 = anon.get("/api/v1/data-version").json()["data"]
    assert v3 != v2
    # A new release on the same data: answers kept by browsers may be out of date.
    monkeypatch.setattr(cache_mod, "CODE_VERSION", "next-release")
    assert anon.get("/api/v1/data-version").json()["data"] != v3


def test_the_channel_list_carries_countries_even_before_the_first_report(client):
    src = client.post("/api/v1/sources", json={"name": "a"}).json()
    files = {"file": ("a.m3u", fixture_bytes("provider_a.m3u"), "audio/x-mpegurl")}
    client.post(f"/api/v1/sources/{src['id']}/upload", files=files)
    from mih.report import countries_path

    path = countries_path(client.app.state.settings.data_dir)
    path.unlink(missing_ok=True)
    items = client.get("/api/v1/channels?limit=10000").json()["items"]
    assert path.exists() and any(i["countries"] for i in items)
