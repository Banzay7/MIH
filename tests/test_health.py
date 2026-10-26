import socket
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from mih.api.app import create_app
from mih.config import Settings
from mih.health.policy import HealthPolicy, Observation, StreamState, age, decide
from mih.health.probe import probe_stream
from mih.health.runner import run_checks
from mih.sources.importer import import_m3u
from tests.conftest import fixture_bytes

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
POLICY = HealthPolicy(canary_urls=())

MASTER = b"""#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=5000000,RESOLUTION=1920x1080,CODECS="avc1.640028,mp4a.40.2"
hd/index.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=640x360
sd/index.m3u8
"""
MEDIA = b"""#EXTM3U
#EXT-X-TARGETDURATION:6
#EXTINF:6.0,
seg100.ts
#EXTINF:6.0,
seg101.ts
"""
DRM_MEDIA = b"""#EXTM3U
#EXT-X-KEY:METHOD=SAMPLE-AES,URI="skd://key",KEYFORMAT="com.apple.streamingkeydelivery"
#EXTINF:6.0,
seg1.ts
"""
AES_MEDIA = b"""#EXTM3U
#EXT-X-KEY:METHOD=AES-128,URI="https://a.example/key"
#EXTINF:6.0,
seg1.ts
"""


# --- lifecycle rules (AT-10) ---------------------------------------------------------


def obs(minutes: float, success: bool, vantage: str = "server", **kw) -> Observation:
    return Observation(at=T0 + timedelta(minutes=minutes), vantage=vantage, success=success, **kw)


def run(state: StreamState, *observations: Observation) -> StreamState:
    for o in observations:
        state = decide(state, o, POLICY).state
    return state


def test_first_success_activates_and_single_failure_is_only_an_event():
    s = run(StreamState(), obs(0, True))
    assert s.status == "active"
    d = decide(s, obs(60, False), POLICY)
    assert d.state.status == "active" and d.counted and d.cause is None
    assert d.state.streak_failures == 1


def test_suspect_needs_three_failures_in_two_windows():
    s = run(StreamState(status="active"), obs(0, False), obs(1, False), obs(2, False))
    assert s.status == "active"  # three quick failures inside one window: a blip
    s = run(s, obs(70, False))
    assert s.status == "suspect"


def test_quarantine_after_a_week_archive_after_a_month_and_recovery():
    s = run(StreamState(status="active"), obs(0, False), obs(60, False), obs(120, False))
    assert s.status == "suspect"
    s = run(s, obs(6 * 24 * 60, False))
    assert s.status == "suspect"
    s = run(s, obs(7 * 24 * 60, False))
    assert s.status == "quarantine"
    assert age(s, s.status_changed_at + timedelta(days=29), POLICY).status == "quarantine"
    s = age(s, s.status_changed_at + timedelta(days=30), POLICY)
    assert s.status == "archived"
    s = run(s, obs(60 * 24 * 60, True))
    assert s.status == "active" and s.streak_failures == 0


def test_never_working_stream_goes_new_then_suspect():
    s = run(StreamState(), obs(0, False))
    assert s.status == "new"
    s = run(s, obs(60, False), obs(120, False))
    assert s.status == "suspect"


def test_uncounted_result_changes_nothing():
    s = StreamState(status="active")
    d = decide(s, obs(0, False, uncounted="source_outage"), POLICY)
    assert not d.counted and d.note == "source_outage"
    assert d.state == s


def test_device_playback_outranks_server_probe():
    s = run(StreamState(status="active"), obs(0, True, vantage="device"))
    d = decide(s, obs(60, False), POLICY)
    assert not d.counted and d.note == "device_recent_ok"
    # Once the device result is old, the probe counts again.
    assert decide(s, obs(73 * 60, False), POLICY).counted

    s = run(StreamState(status="suspect", streak_failures=3), obs(0, False, vantage="device"))
    d = decide(s, obs(60, True), POLICY)
    assert not d.counted and d.note == "device_recent_fail"
    assert run(s, obs(90, True, vantage="device")).status == "active"


def test_drm_is_a_reason_not_a_death():
    s = StreamState(status="active")
    d = decide(s, obs(0, False, reason="drm_unsupported"), POLICY)
    assert d.state.status == "active" and d.state.availability_reason == "drm_unsupported"
    assert d.state.streak_failures == 0
    assert run(d.state, obs(10, True)).availability_reason is None
    absent = replace(s, availability_reason="source_absent")
    assert run(absent, obs(10, True)).availability_reason == "source_absent"


# --- probe ---------------------------------------------------------------------------


@pytest.fixture
def fake_dns(monkeypatch):
    table = {
        "a.example": "93.184.216.34",
        "b.example": "93.184.216.35",
        "lan.example": "192.168.1.5",
    }

    def getaddrinfo(host, port, *args, **kwargs):
        if host not in table:
            raise socket.gaierror("unknown host")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (table[host], port or 0))]

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)


def serve(routes: dict, seen: list | None = None) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        action = routes.get(request.url.path)
        if action is None:
            return httpx.Response(404)
        if isinstance(action, Exception):
            raise action
        if callable(action):
            return action(request)
        return action

    return httpx.MockTransport(handler)


def probe(routes, url="https://a.example/live/index.m3u8", seen=None, **kw):
    with httpx.Client(transport=serve(routes, seen)) as client:
        return probe_stream(client, url, kw.pop("headers", None), POLICY,
                            allow_private=kw.pop("allow_private", False))  # fmt: skip


def test_hls_master_then_lightest_variant_then_newest_segment(fake_dns):
    seen = []
    res = probe(
        {
            "/live/index.m3u8": httpx.Response(200, content=MASTER),
            "/live/sd/index.m3u8": httpx.Response(200, content=MEDIA),
            "/live/sd/seg101.ts": httpx.Response(206, content=b"\x47" * 1000),
        },
        seen=seen,
        headers={"User-Agent": "SomePlayer/1.0"},
    )
    assert res.success and res.stage == "segment"
    assert res.resolution == "1920x1080" and res.bitrate == 5000000
    assert [r.url.path for r in seen][-1] == "/live/sd/seg101.ts"
    assert seen[-1].headers["Range"] == f"bytes=0-{POLICY.segment_bytes - 1}"
    assert all(r.headers["User-Agent"] == "SomePlayer/1.0" for r in seen)


def test_drm_is_reported_and_keys_are_never_fetched(fake_dns):
    seen = []
    res = probe({"/live/index.m3u8": httpx.Response(200, content=DRM_MEDIA)}, seen=seen)
    assert not res.success and res.reason == "drm_unsupported"
    assert len(seen) == 1

    res = probe({"/live/index.m3u8": httpx.Response(200, content=AES_MEDIA),
                 "/live/seg1.ts": httpx.Response(200, content=b"x")})  # fmt: skip
    assert res.success


@pytest.mark.parametrize(
    ("response", "kind", "reason", "uncounted"),
    [
        (httpx.Response(404), "not_found", None, None),
        (httpx.Response(401), "http_401", "authentication_required", None),
        (httpx.Response(403), "http_403", None, None),
        (httpx.Response(429), "rate_limited", None, "rate_limited"),
        (httpx.Response(503), "server_error", None, None),
        (httpx.Response(200, html="<html>login</html>"), "bad_content", None, None),
        (httpx.ConnectError("refused"), "connect", None, None),
        (httpx.ReadTimeout("slow"), "timeout", None, None),
    ],
)
def test_error_classification(fake_dns, response, kind, reason, uncounted):
    res = probe({"/live/index.m3u8": response})
    assert (res.success, res.error_kind, res.reason, res.uncounted) == (
        False, kind, reason, uncounted)  # fmt: skip


def test_lan_streams_are_not_judged_from_a_remote_server(fake_dns):
    res = probe({}, url="http://lan.example/stream.ts")
    assert res.uncounted == "unreachable_here"
    res = probe({"/stream.ts": httpx.Response(200, content=b"\x47")},
                url="http://lan.example/stream.ts", allow_private=True)  # fmt: skip
    assert res.success


def test_redirect_is_followed_and_url_never_appears_in_result(fake_dns):
    res = probe({
        "/live/index.m3u8": httpx.Response(302, headers={"location": "/cdn/x.ts?token=SECRET"}),
        "/cdn/x.ts": httpx.Response(200, content=b"\x47" * 10),
    })  # fmt: skip
    assert res.success
    res = probe({"/live/index.m3u8": httpx.Response(404)},
                url="https://a.example/live/index.m3u8?token=SECRET")  # fmt: skip
    assert "SECRET" not in repr(res)


# --- runs ----------------------------------------------------------------------------


@pytest.fixture
def catalog(conn, settings, make_source):
    a, b = make_source("a"), make_source("b")
    import_m3u(conn, settings, a, fixture_bytes("provider_a.m3u"))
    import_m3u(conn, settings, b, fixture_bytes("provider_b.m3u"))
    return {"a": a, "b": b}


def statuses(conn) -> dict[str, str]:
    rows = conn.execute(
        "SELECT l.url, s.status FROM streams s JOIN stream_locators l ON l.stream_id = s.id"
    )
    return {r["url"].split("?")[0]: r["status"] for r in rows}


OK_TS = httpx.Response(200, content=b"\x47" * 100)


def test_run_marks_working_streams_active_and_schedules_next(fake_dns, conn, settings, catalog):
    routes = {"/live/euronews_eu.m3u8": httpx.Response(200, content=MEDIA),
              "/live/seg101.ts": OK_TS, "/ch/101.ts": OK_TS}  # fmt: skip
    report = run_checks(conn, settings, POLICY, now=T0, transport=serve(routes))
    assert report.checked == 7 and report.ok == 2 and report.failed == 5
    st = statuses(conn)
    assert st["https://a.example/live/euronews_eu.m3u8"] == "active"
    assert st["http://b.example/ch/101.ts"] == "active"
    assert st["https://a.example/live/vtv1.m3u8"] == "new"

    # Nothing is due right away; failures come back after one window, successes in a day.
    again = run_checks(conn, settings, POLICY, now=T0 + timedelta(minutes=5),
                       transport=serve(routes))  # fmt: skip
    assert again.checked == 0
    later = run_checks(conn, settings, POLICY, now=T0 + timedelta(minutes=61),
                       transport=serve(routes))  # fmt: skip
    assert later.checked == 5


def test_whole_source_failing_is_an_outage_not_dead_streams(fake_dns, conn, settings, catalog):
    routes = {"/ch/101.ts": OK_TS, "/ch/202.m3u8": httpx.Response(200, content=MEDIA),
              "/ch/seg101.ts": OK_TS}  # fmt: skip
    for hour in range(5):
        report = run_checks(conn, settings, POLICY, now=T0 + timedelta(hours=hour),
                            transport=serve(routes))  # fmt: skip
    assert report.source_outages == [catalog["a"]]
    st = statuses(conn)
    assert {u: s for u, s in st.items() if "a.example" in u} == dict.fromkeys(
        [u for u in st if "a.example" in u], "unverified")  # fmt: skip
    assert st["http://b.example/ch/202.m3u8"] == "active"


def test_prober_offline_judges_nothing(fake_dns, conn, settings, catalog):
    policy = replace(POLICY, canary_urls=("https://b.example/canary",))
    report = run_checks(conn, settings, policy, now=T0,
                        transport=serve({"/canary": httpx.ConnectError("down")}))  # fmt: skip
    assert report.status == "vantage_offline" and report.checked == 0
    assert set(statuses(conn).values()) == {"unverified"}
    assert conn.execute("SELECT COUNT(*) FROM stream_observations").fetchone()[0] == 0


def test_absent_and_disabled_streams_are_skipped(fake_dns, conn, settings, catalog):
    conn.execute("UPDATE sources SET enabled = 0 WHERE id = ?", (catalog["b"],))
    conn.execute(
        "UPDATE streams SET availability_reason = 'source_absent' WHERE id ="
        " (SELECT stream_id FROM stream_locators WHERE url LIKE '%vtv1%')"
    )
    report = run_checks(conn, settings, POLICY, now=T0, transport=serve({}))
    assert report.checked == 4


def test_drm_reason_survives_reimport(fake_dns, conn, settings, catalog):
    routes = {"/live/vtv1.m3u8": httpx.Response(200, content=DRM_MEDIA)}
    run_checks(conn, settings, POLICY, now=T0, transport=serve(routes))
    import_m3u(conn, settings, catalog["a"], fixture_bytes("provider_a.m3u"), force=True)
    reason = conn.execute(
        "SELECT s.availability_reason FROM streams s JOIN stream_locators l"
        " ON l.stream_id = s.id WHERE l.url LIKE '%vtv1%'"
    ).fetchone()[0]
    assert reason == "drm_unsupported"


# --- API -----------------------------------------------------------------------------

TOKEN = "t" * 32


def test_playback_report_and_availability(fake_dns, tmp_path):
    app = create_app(Settings(data_dir=tmp_path, api_token=TOKEN), health_policy=POLICY)
    with TestClient(app) as c:
        c.headers["Authorization"] = f"Bearer {TOKEN}"
        src = c.post("/api/v1/sources", json={"name": "a"}).json()
        files = {"file": ("a.m3u", fixture_bytes("provider_a.m3u"), "audio/x-mpegurl")}
        c.post(f"/api/v1/sources/{src['id']}/upload", files=files)
        channel = c.get("/api/v1/channels", params={"q": "vtv1"}).json()["items"][0]

        avail = c.get(f"/api/v1/channels/{channel['id']}/availability").json()
        feed = avail["feeds"][0]
        assert feed["playable"] is False
        stream_id = feed["streams"][0]["id"]

        out = c.post(f"/api/v1/streams/{stream_id}/playback",
                     json={"result": "started", "startup_ms": 900}).json()  # fmt: skip
        assert out["status"] == "active"
        avail = c.get(f"/api/v1/channels/{channel['id']}/availability").json()
        assert avail["feeds"][0]["playable"] and avail["feeds"][0]["best_stream_id"] == stream_id

        health = c.get(f"/api/v1/streams/{stream_id}/health").json()
        assert health["events"][0]["cause"] == "playback_ok"
        assert health["observations"][0]["vantage"] == "device"
        assert "url" not in health

        summary = c.get("/api/v1/streams/health").json()
        assert summary["by_status"]["active"] == 1

        assert (
            c.post("/api/v1/streams/nope/playback", json={"result": "started"}).status_code == 404
        )
        anon = TestClient(app)
        assert anon.get("/api/v1/streams/health").status_code == 401
