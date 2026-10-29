from pathlib import Path

import httpx
import pytest

from mih import quality
from mih.config import Settings

MASTER = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=640x360
low/index.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=2500000,RESOLUTION=1280x720
mid/index.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=6000000,RESOLUTION=1920x1080
hi/index.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=64000,CODECS="mp4a.40.2"
audio/index.m3u8
"""


@pytest.fixture(autouse=True)
def _no_dns(monkeypatch):
    monkeypatch.setattr(quality, "check_destination", lambda url, allow_private: None)
    quality._cache.clear()


def serve(routes: dict, seen: list | None = None) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        body = routes.get(str(request.url))
        if body is None:
            return httpx.Response(404)
        if isinstance(body, httpx.Response):
            return body
        return httpx.Response(200, text=body)

    return httpx.MockTransport(handler)


def resolve(url, q, routes, headers=None, seen=None):
    return quality.resolve(
        Settings(data_dir=Path(".")), url, headers, q, transport=serve(routes, seen)
    )


def test_the_cap_picks_the_variant_that_fits():
    url = "https://a.example/live/master.m3u8"
    routes = {url: MASTER}
    assert resolve(url, "720", routes) == "https://a.example/live/mid/index.m3u8"
    quality._cache.clear()
    assert resolve(url, "1080", routes) == "https://a.example/live/hi/index.m3u8"
    quality._cache.clear()
    assert resolve(url, "best", routes) == "https://a.example/live/hi/index.m3u8"
    # auto hands the master and reads nothing.
    seen = []
    assert resolve(url, "auto", routes, seen=seen) == url and seen == []


def test_nothing_fits_takes_the_lightest_video():
    url = "https://a.example/m.m3u8"
    only_hd = "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=6000000,RESOLUTION=1920x1080\nhi.m3u8\n"
    assert resolve(url, "720", {url: only_hd}) == "https://a.example/hi.m3u8"


def test_the_master_stays_when_in_doubt():
    url = "https://a.example/m.m3u8"
    # Not a master: a media playlist.
    media = "#EXTM3U\n#EXTINF:6,\nseg1.ts\n"
    assert resolve(url, "720", {url: media}) == url
    quality._cache.clear()
    # No heights to go by.
    bare = "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\na.m3u8\n#EXT-X-STREAM-INF:BANDWIDTH=2\nb.m3u8\n"
    assert resolve(url, "720", {url: bare}) == url
    quality._cache.clear()
    # An error or a token minted for the server.
    assert resolve(url, "720", {}) == url
    quality._cache.clear()
    tokened = MASTER.replace("mid/index.m3u8", "mid/index.m3u8?token=abc")
    assert resolve(url, "720", {url: tokened}) == url


def test_redirects_and_headers_are_followed():
    url = "https://a.example/ch/1"
    moved = "https://b.example/live/master.m3u8"
    seen = []
    routes = {url: httpx.Response(302, headers={"location": moved}), moved: MASTER}
    got = resolve(url, "720", routes, headers={"user-agent": "UA", "referer": "R"}, seen=seen)
    assert got == "https://b.example/live/mid/index.m3u8"
    assert seen[0].headers["user-agent"] == "UA" and seen[0].headers["referer"] == "R"


def test_a_launch_reads_once_then_remembers():
    url = "https://a.example/m.m3u8"
    seen = []
    for _ in range(2):
        resolve(url, "720", {url: MASTER}, seen=seen)
    assert len(seen) == 1
    many = quality.resolve_many(
        Settings(data_dir=Path(".")), [(url, None), ("rtmp://x", None)], "720"
    )
    assert many == ["https://a.example/mid/index.m3u8", "rtmp://x"]
