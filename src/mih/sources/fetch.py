"""Download a source with SSRF limits, size caps and conditional GET (spec 23)."""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

import httpx

from mih.config import Settings

MAX_REDIRECTS = 5


class FetchError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass
class FetchResult:
    content: bytes | None  # None when the server answered 304 Not Modified
    etag: str | None
    last_modified: str | None


def fetch_source(
    url: str,
    settings: Settings,
    *,
    etag: str | None = None,
    last_modified: str | None = None,
    transport: httpx.BaseTransport | None = None,
) -> FetchResult:
    headers = {"User-Agent": "MIRTVONLINE/0.1"}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified

    try:
        return _fetch(url, settings, headers, etag, last_modified, transport)
    except httpx.HTTPError as exc:
        # A source that hangs or drops the connection is that source's problem: the
        # caller reports it and goes on with the next source.
        raise FetchError("network", f"{type(exc).__name__}: {exc}") from exc


def _fetch(url, settings, headers, etag, last_modified, transport) -> FetchResult:
    with httpx.Client(
        timeout=settings.fetch_timeout_s, follow_redirects=False, transport=transport
    ) as client:
        # Redirects are followed by hand so every hop passes the same address check.
        for _ in range(MAX_REDIRECTS + 1):
            check_destination(url, allow_private=settings.allow_private_sources)
            with client.stream("GET", url, headers=headers) as resp:
                if resp.status_code == 304:
                    return FetchResult(None, etag, last_modified)
                if resp.status_code in (301, 302, 303, 307, 308):
                    location = resp.headers.get("location")
                    if not location:
                        raise FetchError("bad_redirect", "redirect without Location")
                    url = urljoin(url, location)
                    continue
                if resp.status_code >= 400:
                    raise FetchError("source_unavailable", f"HTTP {resp.status_code}")
                body = bytearray()
                for chunk in resp.iter_bytes():
                    body.extend(chunk)
                    if len(body) > settings.fetch_max_bytes:
                        raise FetchError("too_large", "source exceeds MIH_FETCH_MAX_BYTES")
                return FetchResult(
                    bytes(body), resp.headers.get("etag"), resp.headers.get("last-modified")
                )
    raise FetchError("too_many_redirects", f"more than {MAX_REDIRECTS} redirects")


def check_destination(url: str, *, allow_private: bool) -> None:
    """Reject URLs that point at this machine, the LAN or cloud metadata endpoints.

    The name is resolved here and every address must be public. A DNS answer can still
    change between this check and the connection; pinning the resolved address is a
    follow-up (spec 23, DNS rebinding).
    """
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise FetchError("validation_error", "only http and https sources are supported")
    if not parts.hostname:
        raise FetchError("validation_error", "URL has no host")
    if parts.username or parts.password:
        # Allowed by HTTP, but such URLs end up in logs too easily.
        raise FetchError("validation_error", "put credentials in the path or query, not user:pass@")
    try:
        infos = socket.getaddrinfo(parts.hostname, parts.port or None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise FetchError("source_unavailable", f"cannot resolve {parts.hostname}") from exc
    for info in infos:
        addr = ipaddress.ip_address(info[4][0])
        if _always_blocked(addr) or (not allow_private and not addr.is_global):
            raise FetchError("destination_blocked", f"{parts.hostname} resolves to {addr}")


def _always_blocked(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    # Loopback, link-local (incl. 169.254.169.254 metadata) and unspecified addresses
    # stay blocked even when private LAN sources are allowed.
    return addr.is_loopback or addr.is_link_local or addr.is_unspecified or addr.is_multicast
