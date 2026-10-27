import json
import socket
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from mih.config import Settings
from mih.discovery.harvest import (
    REPO_QUERIES,
    SEED_PLAYLISTS,
    SEED_REPOS,
    Plan,
    extract_links,
    harvest,
    load_plan,
    search_period,
)
from mih.discovery.safety import drm_urls, screen, url_problem
from mih.discovery.scout import pack, scout, summary
from mih.health.policy import HealthPolicy, format_ts
from mih.scheduler import run_job
from mih.sources.importer import import_m3u
from mih.sources.m3u import parse_m3u

from .conftest import count

NOW = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)
POLICY = HealthPolicy(canary_urls=())
MEDIA = b"#EXTM3U\n#EXT-X-TARGETDURATION:6\n#EXTINF:6.0,\nseg1.ts\n"


@pytest.fixture
def fake_dns(monkeypatch):
    def getaddrinfo(host, port, *args, **kwargs):
        if not host.endswith(".example"):
            raise socket.gaierror("unknown host")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port or 0))]

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)


def web(routes: dict, seen: list | None = None) -> httpx.MockTransport:
    """Routes by host + path; a stream named live* plays, anything else is 404."""

    def handler(request: httpx.Request) -> httpx.Response:
        key = request.url.host + request.url.path
        if seen is not None:
            seen.append(key)
        if key in routes:
            value = routes[key]
            return value(request) if callable(value) else value
        name = request.url.path.rsplit("/", 1)[-1]
        if name.startswith("live") and name.endswith(".m3u8"):
            return httpx.Response(200, content=MEDIA)
        if name == "seg1.ts":
            return httpx.Response(206, content=b"\x47" * 100)
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def playlist(*urls: str) -> bytes:
    lines = ["#EXTM3U"]
    for i, url in enumerate(urls):
        lines += [f'#EXTINF:-1 tvg-id="Ch{i}.example",Channel {i}', url]
    return ("\n".join(lines) + "\n").encode()


def add_candidate(conn, url: str, origin: str = "github") -> None:
    conn.execute(
        "INSERT INTO discovered_playlists (id, url, origin, first_seen_at, next_scout_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (url[-12:], url, origin, format_ts(NOW), format_ts(NOW)),
    )


def row(conn, url: str):
    return conn.execute("SELECT * FROM discovered_playlists WHERE url = ?", (url,)).fetchone()


# --- safety ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "problem"),
    [
        ("https://cdn.example/live/ch1/index.m3u8", None),
        ("https://cdn.example/hls/ch.m3u8?token=abc", None),
        ("https://cdn.example/hls/live/598695/racing/1500.m3u8", None),
        ("https://cdn.example/live/webplayer/livestream/1.m3u8", None),
        ("https://cdn.example/get.php?x=CNC3", None),
        ("http://wowza.example:1935/live/app/stream/2.m3u8", None),
        ("https://ant.example:5443/LiveApp/streams/0492297943.m3u8", None),
        ("http://panel.example/live/john/secret/12345.ts", "credentials"),
        ("http://panel.example:8080/get.php?username=a&password=b&type=m3u", "credentials"),
        ("http://panel.example:8080/live/john/secret/12345.ts", "credentials"),
        ("http://panel.example:25461/john/secret/12345", "credentials"),
        ("http://cdn.example/x.m3u8?user=a&pass=b", "credentials"),
        ("http://john:secret@cdn.example/x.m3u8", "credentials"),
        ("udp://@239.0.0.1:1234", "unsupported"),
        ("https://tv.paid.example/ch.m3u8", "denied_host"),
    ],
)
def test_leaked_accounts_and_blocked_hosts(url, problem):
    assert url_problem(url, frozenset({"paid.example"})) == problem


def test_a_playlist_of_mostly_leaks_is_rejected_whole():
    leak = "http://p.example:8080/live/u/p/{}.ts"
    leaks = [leak.format(i) for i in range(3)]
    parsed = parse_m3u(playlist("https://a.example/live1.m3u8", *leaks))
    kept, reject = screen(parsed.entries)
    assert reject == "credentials" and len(kept) == 1

    drm = (
        b"#EXTM3U\n#KODIPROP:inputstream.adaptive.license_key=https://k.example/x\n"
        b"#EXTINF:-1,Paid\nhttps://a.example/paid.mpd\n#EXTINF:-1,Free\n"
        b"https://a.example/live1.m3u8\n#EXTINF:-1,Free 2\nhttps://a.example/live2.m3u8\n"
    )
    kept, reject = screen(parse_m3u(drm).entries, drm=drm_urls(drm))
    assert reject is None and [e.name for e in kept] == ["Free", "Free 2"]


# --- search ---------------------------------------------------------------------------


def test_links_in_pages_and_telegram_posts():
    page = (
        '<a href="https://github.com/someone/tv/blob/main/lists/ru.m3u">list</a> '
        "https://a.example/play.m3u8, see https://github.com/other/iptv-ru and "
        "https://t.me/s/x?before=1 https://a.example/q?a=1&amp;b=2 "
        "https://github.com/topics/iptv"
    )
    assert extract_links(page) == [
        ("https://raw.githubusercontent.com/someone/tv/main/lists/ru.m3u", None),
        ("https://a.example/play.m3u8", None),
        (None, "other/iptv-ru"),
    ]


def test_plan_file(tmp_path):
    path = tmp_path / "discovery.json"
    path.write_text(json.dumps({"telegram": ["@freetv", "https://t.me/other"],
                                "pages": ["https://forum.example/t/1"],
                                "deny_hosts": ["Paid.example"]}))  # fmt: skip
    plan = load_plan(path)
    assert plan.pages == ("https://forum.example/t/1", "https://t.me/s/freetv",
                          "https://t.me/s/other")  # fmt: skip
    assert plan.deny_hosts == {"paid.example"}
    assert load_plan(None) == Plan()


def test_harvest_seeds_repo_trees_and_search(conn, settings):
    tree = {"tree": [
        {"type": "blob", "path": "streams/ru.m3u", "size": 1000},
        {"type": "blob", "path": "README.md", "size": 10},
        {"type": "blob", "path": "huge.m3u", "size": 10**9},
    ]}  # fmt: skip
    calls = []

    def github(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("/git/trees/HEAD"):
            return httpx.Response(200, json=tree)
        if request.url.path == "/search/repositories":
            return httpx.Response(200, json={"total_count": 1, "items": [
                {"full_name": "fresh/iptv", "pushed_at": "2026-10-20T00:00:00Z"}]})  # fmt: skip
        return httpx.Response(404)

    def go(at=NOW):
        return harvest(conn, settings, Plan(), at, transport=httpx.MockTransport(github))

    report = go()
    # Seed playlists, then one file from each of the first 4 seed repositories (4 API calls).
    assert report.found["seed"] == len(SEED_PLAYLISTS) + 4
    assert report.api_calls == 4 and "/search/repositories" not in calls
    assert row(conn, "https://raw.githubusercontent.com/iptv-org/iptv/HEAD/streams/ru.m3u")

    # The other 4 seeds, then the search, then the repository it found.
    assert len(SEED_REPOS) == 8 and go().found == {"seed": 4}
    go()
    assert calls[-1] == "/search/repositories"
    assert go().found == {"github": 1}
    assert row(conn, "https://raw.githubusercontent.com/fresh/iptv/HEAD/streams/ru.m3u")

    # The other queries, one page each; then the pass is over until the next day.
    runs = 0
    while go(NOW + timedelta(hours=1)).api_calls:
        runs += 1
        assert runs < 20
    assert calls.count("/search/repositories") == len(REPO_QUERIES)
    assert go(NOW + timedelta(days=1, minutes=1)).api_calls == 4


def test_search_runs_daily_at_first_then_monthly(conn, settings):
    assert search_period(conn, settings, NOW) == timedelta(days=1)
    assert search_period(conn, settings, NOW + timedelta(days=59)) == timedelta(days=1)
    assert search_period(conn, settings, NOW + timedelta(days=61)) == timedelta(days=30)
    fixed = Settings(data_dir=settings.data_dir, discovery_search_every=timedelta(days=7))
    assert search_period(conn, fixed, NOW) == timedelta(days=7)


def test_github_rate_limit_stops_the_run(conn, settings):
    def limited(request):
        return httpx.Response(403, headers={"x-ratelimit-remaining": "0"})

    report = harvest(conn, settings, Plan(), NOW, transport=httpx.MockTransport(limited))
    assert report.api_calls == 1 and "github: rate limit" in report.notes


# --- scout and packer -----------------------------------------------------------------


def test_scout_samples_then_packer_imports_only_what_plays(conn, settings, fake_dns,
                                                           make_source):  # fmt: skip
    known = make_source("provider")
    import_m3u(conn, settings, known, playlist("https://a.example/live0.m3u8"))
    url = "https://lists.example/ru.m3u"
    content = playlist(
        "https://a.example/live0.m3u8",  # already in the catalog
        "https://a.example/live1.m3u8",
        "https://a.example/dead.m3u8",
        "http://p.example:8080/live/u/p/1.ts",  # leaked account, never probed
    )
    seen = []
    transport = web({"lists.example/ru.m3u": httpx.Response(200, content=content)}, seen)
    add_candidate(conn, url)

    report = scout(conn, settings, Plan(), POLICY, NOW, transport=transport)
    assert report.by_status == {"live": 1} and report.probes == 2
    r = row(conn, url)
    assert (r["entries"], r["new_entries"], r["sampled"], r["sample_ok"]) == (4, 2, 2, 1)
    assert not any("p.example" in s for s in seen)

    seen.clear()
    out = pack(conn, settings, Plan(), POLICY, NOW, transport=transport)
    assert out.packed and out.probes == 0  # the sample already covered both
    assert (out.streams_ok, out.streams_created) == (1, 1)
    r = row(conn, url)
    assert r["status"] == "packed" and r["source_id"]
    stream = conn.execute(
        "SELECT s.status, l.url FROM streams s JOIN stream_locators l ON l.stream_id = s.id"
        " WHERE s.source_id = ?", (r["source_id"],),
    ).fetchall()  # fmt: skip
    assert [(s["url"], s["status"] != "unverified") for s in stream] == [
        ("https://a.example/live1.m3u8", True)]  # fmt: skip
    assert summary(conn)["auto_working"] == 1


def test_packer_works_in_small_batches(conn, settings, fake_dns):
    settings = Settings(data_dir=settings.data_dir, discovery_pack_batch=4)
    urls = [f"https://a.example/{'live' if i % 3 == 0 else 'dead'}{i}.m3u8" for i in range(16)]
    transport = web({"lists.example/big.m3u": httpx.Response(200, content=playlist(*urls))})
    add_candidate(conn, "https://lists.example/big.m3u")
    scout(conn, settings, Plan(), POLICY, NOW, transport=transport)
    assert row(conn, "https://lists.example/big.m3u")["status"] == "live"

    runs = []
    while True:
        out = pack(conn, settings, Plan(), POLICY, NOW, transport=transport)
        runs.append((out.probes, out.remaining, out.streams_ok))
        if out.done or out.playlist is None:
            break
    # Working streams reach the catalog after each batch, not only at the end.
    assert [r[:2] for r in runs] == [(4, 2), (2, 0)]
    assert 0 < runs[0][2] <= runs[1][2] == 6
    assert count(conn, "streams") == 6


def test_forks_and_dead_playlists(conn, settings, fake_dns):
    content = playlist("https://a.example/live1.m3u8")
    transport = web({
        "lists.example/a.m3u": httpx.Response(200, content=content),
        "lists.example/fork.m3u": httpx.Response(200, content=content),
        "lists.example/dead.m3u": httpx.Response(200, content=playlist("https://a.example/x.m3u8")),
        "lists.example/stream.m3u8": httpx.Response(200, content=MEDIA),
    })  # fmt: skip
    for name in ("a.m3u", "dead.m3u", "stream.m3u8"):
        add_candidate(conn, f"https://lists.example/{name}")
    scout(conn, settings, Plan(), POLICY, NOW, transport=transport)
    assert row(conn, "https://lists.example/dead.m3u")["status"] == "dead"
    assert row(conn, "https://lists.example/stream.m3u8")["status"] == "rejected"
    add_candidate(conn, "https://lists.example/fork.m3u")
    scout(conn, settings, Plan(), POLICY, NOW, transport=transport)
    assert row(conn, "https://lists.example/fork.m3u")["status"] == "duplicate"

    # A dead playlist is retried weekly and given up after three misses.
    for week in range(1, 4):
        scout(conn, settings, Plan(), POLICY, NOW + timedelta(days=7 * week), transport=transport)
    r = row(conn, "https://lists.example/dead.m3u")
    assert (r["status"], r["misses"], r["next_scout_at"]) == ("dead", 3, None)


def test_a_packed_playlist_whose_streams_all_died_is_retired(conn, settings, fake_dns):
    url = "https://lists.example/a.m3u"
    transport = web({"lists.example/a.m3u": httpx.Response(
        200, content=playlist("https://a.example/live1.m3u8"))})  # fmt: skip
    add_candidate(conn, url)
    scout(conn, settings, Plan(), POLICY, NOW, transport=transport)
    pack(conn, settings, Plan(), POLICY, NOW, transport=transport)
    source_id = row(conn, url)["source_id"]

    later = NOW + timedelta(days=8)
    scout(conn, settings, Plan(), POLICY, later, transport=transport)
    assert row(conn, url)["status"] == "packed"
    conn.execute("UPDATE streams SET status = 'archived' WHERE source_id = ?", (source_id,))
    scout(conn, settings, Plan(), POLICY, later + timedelta(days=8), transport=transport)
    assert row(conn, url)["status"] == "retired"
    assert conn.execute("SELECT enabled FROM sources WHERE id = ?", (source_id,)).fetchone()[0] == 0


def test_jobs_run_quietly_from_the_scheduler(conn, settings, fake_dns):
    settings = Settings(data_dir=settings.data_dir, every_discover=timedelta(minutes=30),
                        every_pack=timedelta(minutes=15))  # fmt: skip
    event = run_job(conn, settings, "pack", policy=POLICY,
                    transport=httpx.MockTransport(lambda r: httpx.Response(404)))  # fmt: skip
    assert event["data"]["status"] == "ok" and event["id"] is None  # nothing to say
    event = run_job(conn, settings, "discover", policy=POLICY,
                    transport=httpx.MockTransport(lambda r: httpx.Response(404)))  # fmt: skip
    assert event["data"]["status"] == "ok"
    assert event["data"]["totals"]["found"] >= len(SEED_PLAYLISTS)
