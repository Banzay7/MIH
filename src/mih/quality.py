"""Which picture quality a TV starts with (Settings → «Качество», MIH_PLAY_QUALITY).

A heavy HD variant often buffers or takes long to start on a home line. When a stream
is an HLS master playlist, the server reads it just before the launch and hands the
player the variant that fits the cap (``720``: the best one up to 720 lines) instead of
the master. ``auto`` hands the master and lets the player choose; ``best`` the richest
variant. Anything that does not work out (no master, no heights, a timeout, a variant
that needs a token the master did not carry) keeps the original URL.
"""

from __future__ import annotations

import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qsl, urljoin, urlsplit

import httpx

from mih.config import Settings
from mih.sources.fetch import FetchError, check_destination

QUALITIES = ("auto", "720", "1080", "best")
MAX_BYTES = 256 * 1024
MAX_REDIRECTS = 3
DEADLINE_S = 4.0
# A master's variants rarely change; a launch should not wait for the same read twice.
CACHE_TTL_S = 600.0

_ATTR = re.compile(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)')
_cache: dict[tuple[str, str], tuple[float, str]] = {}
_lock = threading.Lock()


def normalize(value: str | None, default: str = "720") -> str:
    value = (value or "").strip().lower().removesuffix("p")
    return value if value in QUALITIES else default


def variants(text: str, base: str) -> list[tuple[int, int, str]]:
    """(height, bandwidth, absolute url) of an HLS master's video variants."""
    out, pending = [], None
    for ln in (x.strip() for x in text.splitlines()):
        if ln.startswith("#EXT-X-STREAM-INF:"):
            pending = {k: v.strip('"') for k, v in _ATTR.findall(ln.split(":", 1)[1])}
        elif ln and not ln.startswith("#") and pending is not None:
            m = re.match(r"(\d+)x(\d+)$", pending.get("RESOLUTION", ""))
            bw = pending.get("BANDWIDTH", "0")
            out.append((int(m.group(2)) if m else 0, int(bw) if bw.isdigit() else 0,
                        urljoin(base, ln)))  # fmt: skip
            pending = None
    return out


def choose(found: list[tuple[int, int, str]], quality: str) -> str | None:
    if quality == "best" and found:
        return max(found, key=lambda v: (v[0], v[1]))[2]
    video = [v for v in found if v[0]]
    if not video:
        return None  # no heights: the player knows better than a guess
    cap = int(quality)
    fits = [v for v in video if v[0] <= cap]
    if fits:
        return max(fits, key=lambda v: (v[0], v[1]))[2]
    return min(video, key=lambda v: (v[0], v[1]))[2]


def resolve(settings: Settings, url: str, headers: dict | None, quality: str,
            *, transport: httpx.BaseTransport | None = None) -> str:  # fmt: skip
    """The URL to hand the player for this quality."""
    if quality == "auto" or not url.lower().startswith(("http://", "https://")):
        return url
    key = (url, quality)
    with _lock:
        hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < CACHE_TTL_S:
        return hit[1]
    picked = _resolve(settings, url, headers or {}, quality, transport) or url
    with _lock:
        _cache[key] = (time.monotonic(), picked)
    return picked


def resolve_many(settings: Settings, items: list[tuple[str, dict | None]], quality: str,
                 *, transport: httpx.BaseTransport | None = None) -> list[str]:  # fmt: skip
    """``resolve`` for a launch's few streams at once, so the player waits for one read."""
    if quality == "auto" or len(items) < 2:
        return [resolve(settings, u, h, quality, transport=transport) for u, h in items]
    with ThreadPoolExecutor(max_workers=len(items)) as pool:
        return list(pool.map(lambda x: resolve(settings, x[0], x[1], quality,
                                               transport=transport), items))  # fmt: skip


def _resolve(settings, url, headers, quality, transport) -> str | None:
    started = time.monotonic()
    request_headers = {}
    for k, v in headers.items():
        if v and k.lower() in ("user-agent", "referer", "user_agent"):
            request_headers["User-Agent" if "agent" in k.lower() else "Referer"] = v
    timeout = httpx.Timeout(DEADLINE_S, connect=2.0)
    try:
        with httpx.Client(timeout=timeout, transport=transport) as client:
            for _ in range(MAX_REDIRECTS + 1):
                check_destination(url, allow_private=settings.allow_private_sources)
                with client.stream("GET", url, headers=request_headers) as r:
                    if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
                        url = urljoin(url, r.headers["location"])
                        continue
                    if r.status_code >= 400:
                        return None
                    body = bytearray()
                    for chunk in r.iter_bytes():
                        body.extend(chunk)
                        if len(body) >= MAX_BYTES or time.monotonic() - started > DEADLINE_S:
                            break
                    final = str(r.url)
                break
            else:
                return None
    except (httpx.HTTPError, FetchError):
        return None
    text = bytes(body).decode("utf-8", "replace")
    if "#EXT-X-STREAM-INF" not in text:
        return None
    picked = choose(variants(text, final), quality)
    if picked is None or _adds_token(final, picked):
        return None
    return picked


def _adds_token(master: str, variant: str) -> bool:
    """A variant with query keys the master lacks may carry a token minted for the
    server's address: the TV would get 403. Then the TV gets the master."""
    have = {k for k, _ in parse_qsl(urlsplit(master).query)}
    return any(k not in have for k, _ in parse_qsl(urlsplit(variant).query))
