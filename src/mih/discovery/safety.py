"""What the discovery never takes: subscriber accounts, DRM and hosts the owner blocked.

Found playlists are public files, but some of them are leaks: an IPTV subscription's
login inside every URL, or a paid channel with its licence key. Such entries are dropped
before anything is probed, and a playlist made mostly of them is rejected as a whole.
"""

from __future__ import annotations

import re
from collections import Counter
from urllib.parse import parse_qsl, urlsplit

from mih.sources.m3u import M3UEntry

# A playlist where at least this share of entries is excluded is rejected.
REJECT_SHARE = 0.5

_CREDENTIAL_KEYS = {"username", "user", "password", "pass", "passwd", "pwd", "login"}
# Xtream Codes panels: /live/<user>/<password>/<id>.ts at the server root, or the short
# form /<user>/<password>/<id> on its own port. Ordinary servers have look-alike HLS paths
# (Wowza /live/<app>/<stream>/2.m3u8, CDNs /live/<a>/<b>/1.m3u8), so only the .ts and the
# bare-id endings count; an .m3u8 leak gets through here and is left to the probe.
_XTREAM = re.compile(r"^/(?:live|movie|series|timeshift)/[^/]+/[^/]+/\d+(?:\.ts)?$")
_XTREAM_SHORT = re.compile(r"^/[^/]+/[^/]+/\d+(?:\.ts)?$")


def url_problem(url: str, deny_hosts: frozenset[str] = frozenset()) -> str | None:
    """Why this URL must not be used, or None."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return "bad_url"
    if parts.scheme not in ("http", "https"):
        # udp/rtp multicast only works inside the provider's own network.
        return "unsupported"
    host = (parts.hostname or "").lower()
    if not host:
        return "bad_url"
    if parts.username or parts.password:
        return "credentials"
    if any(host == d or host.endswith("." + d) for d in deny_hosts):
        return "denied_host"
    path = parts.path
    if {k.lower() for k, _ in parse_qsl(parts.query)} & _CREDENTIAL_KEYS:
        return "credentials"  # get.php?username=..., player_api.php?password=...
    if _XTREAM.match(path) or (parts.port and _XTREAM_SHORT.match(path)):
        return "credentials"
    return None


def drm_urls(content: bytes) -> set[str]:
    """Stream URLs that come with a licence key (#KODIPROP before the URL). The M3U parser
    skips those lines, so they are read here from the raw text."""
    out, licensed = set(), False
    for line in content.decode("utf-8", "replace").splitlines():
        line = line.strip()
        low = line.lower()
        if low.startswith("#kodiprop") and ("license_key" in low or "license_type" in low):
            licensed = True
        elif line and not line.startswith("#"):
            if licensed:
                out.add(line)
            licensed = False
    return out


def screen(entries: list[M3UEntry], deny_hosts: frozenset[str] = frozenset(),
           drm: set[str] | frozenset[str] = frozenset()
           ) -> tuple[list[M3UEntry], str | None]:  # fmt: skip
    """Entries that may be used, and the reason to reject the whole playlist (or None)."""
    kept, reasons = [], Counter()
    for entry in entries:
        problem = "drm" if entry.url in drm else url_problem(entry.url, deny_hosts)
        if problem:
            reasons[problem] += 1
        else:
            kept.append(entry)
    excluded = sum(reasons.values())
    if entries and excluded / len(entries) >= REJECT_SHARE:
        return kept, reasons.most_common(1)[0][0]
    return kept, None
