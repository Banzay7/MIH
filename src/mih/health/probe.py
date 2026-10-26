"""One stream probe (spec 17): HTTP, then a bounded read of the manifest and one segment.

The probe never downloads a stream, never fetches decryption keys and never tries to
get past DRM or a login: a protected stream is reported as such and left alone.
Error messages never contain the URL, which can carry a subscriber token.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from urllib.parse import urljoin

import httpx

from mih.health.policy import HealthPolicy
from mih.sources.fetch import FetchError, check_destination

MAX_REDIRECTS = 5
MAX_PLAYLIST_DEPTH = 3
USER_AGENT = "MIRTVONLINE/0.1 (stream health check)"

# Errors that describe the path between the prober and the host more than the stream.
NETWORK_ERRORS = ("dns", "connect", "timeout", "network")

_ATTR = re.compile(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)')
_DRM_KEYFORMAT = ("com.apple.streamingkeydelivery", "urn:uuid:", "com.microsoft.playready")


@dataclass
class ProbeResult:
    success: bool
    error_kind: str | None = None
    http_status: int | None = None
    # Availability reason when the stream exists but cannot be played here.
    reason: str | None = None
    stage: str | None = None  # http | manifest | segment
    startup_ms: int | None = None
    resolution: str | None = None
    codec: str | None = None
    bitrate: int | None = None
    # Not a verdict on the stream at all (rate limit, address the prober may not reach).
    uncounted: str | None = None

    @property
    def network_level(self) -> bool:
        return not self.success and self.error_kind in NETWORK_ERRORS


class _Stop(Exception):
    def __init__(self, result: ProbeResult):
        self.result = result


@dataclass
class _Response:
    url: str
    status: int
    content_type: str
    body: bytes


def probe_stream(
    client: httpx.Client,
    url: str,
    headers: dict[str, str] | None,
    policy: HealthPolicy,
    *,
    allow_private: bool,
) -> ProbeResult:
    started = time.monotonic()
    request_headers = {"User-Agent": USER_AGENT, **(headers or {})}
    probe = _Probe(client, request_headers, policy, allow_private, started)
    try:
        return probe.run(url)
    except _Stop as stop:
        return stop.result


class _Probe:
    def __init__(self, client, headers, policy, allow_private, started):
        self.client, self.headers, self.policy = client, headers, policy
        self.allow_private, self.started = allow_private, started
        self.resolution = self.codec = None
        self.bitrate = None

    def run(self, url: str) -> ProbeResult:
        resp = self.get(url, self.policy.manifest_max_bytes, stage="http")
        for _ in range(MAX_PLAYLIST_DEPTH):
            head = resp.body.lstrip(b"\xef\xbb\xbf \t\r\n")[:4096]
            if head.startswith(b"#EXTM3U"):
                next_url, is_segment = self.hls(resp)
                stage = "segment" if is_segment else "manifest"
                limit = self.policy.segment_bytes if is_segment else self.policy.manifest_max_bytes
                resp = self.get(next_url, limit, stage=stage, ranged=is_segment)
                if is_segment:
                    return self.media_ok(resp, "segment")
                continue
            if b"<MPD" in head:
                return self.dash(resp)
            if "text/html" in resp.content_type:
                # An error or login page served with 200.
                self.fail("bad_content", stage="http", status=resp.status)
            return self.media_ok(resp, "segment")
        self.fail("bad_manifest", stage="manifest")

    def hls(self, resp: _Response) -> tuple[str, bool]:
        text = resp.body.decode("utf-8", "replace")
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        variants, segments = [], []
        pending_variant = None
        for ln in lines:
            if ln.startswith("#EXT-X-STREAM-INF:"):
                pending_variant = dict(
                    (k, v.strip('"')) for k, v in _ATTR.findall(ln.split(":", 1)[1])
                )
            elif ln.startswith(("#EXT-X-KEY:", "#EXT-X-SESSION-KEY:")):
                attrs = dict((k, v.strip('"')) for k, v in _ATTR.findall(ln.split(":", 1)[1]))
                if _is_drm(attrs):
                    self.fail("drm", stage="manifest", reason="drm_unsupported")
            elif not ln.startswith("#"):
                if pending_variant is not None:
                    variants.append((pending_variant, urljoin(resp.url, ln)))
                    pending_variant = None
                else:
                    segments.append(urljoin(resp.url, ln))
        if variants:
            best = max(variants, key=lambda v: _int(v[0].get("BANDWIDTH")))
            self.resolution = best[0].get("RESOLUTION")
            self.codec = best[0].get("CODECS")
            self.bitrate = _int(best[0].get("BANDWIDTH")) or None
            # The lightest variant is enough to see that the stream is alive.
            lightest = min(variants, key=lambda v: _int(v[0].get("BANDWIDTH")))
            return lightest[1], False
        if not segments:
            self.fail("bad_manifest", stage="manifest")
        # In a live window the oldest segments expire first; the newest is safest.
        return segments[-1], True

    def dash(self, resp: _Response) -> ProbeResult:
        if b"ContentProtection" in resp.body:
            self.fail("drm", stage="manifest", reason="drm_unsupported")
        # Resolving a DASH segment means walking SegmentTemplate rules; a valid live
        # manifest is accepted for now and ffprobe can confirm it later.
        return self.result(True, stage="manifest")

    def media_ok(self, resp: _Response, stage: str) -> ProbeResult:
        if not resp.body:
            self.fail("empty", stage=stage, status=resp.status)
        return self.result(True, stage=stage, status=resp.status)

    def get(self, url: str, max_bytes: int, *, stage: str, ranged: bool = False) -> _Response:
        headers = dict(self.headers)
        if ranged:
            headers["Range"] = f"bytes=0-{max_bytes - 1}"
        for _ in range(MAX_REDIRECTS + 1):
            try:
                check_destination(url, allow_private=self.allow_private)
            except FetchError as exc:
                if exc.code == "destination_blocked":
                    # On a remote server the user's LAN is out of reach: no verdict.
                    self.fail("destination_blocked", stage=stage, uncounted="unreachable_here")
                if exc.code == "source_unavailable":
                    self.fail("dns", stage=stage)
                self.fail("bad_url", stage=stage)
            try:
                with self.client.stream("GET", url, headers=headers) as r:
                    if r.status_code in (301, 302, 303, 307, 308):
                        location = r.headers.get("location")
                        if not location:
                            self.fail("bad_redirect", stage=stage, status=r.status_code)
                        url = urljoin(url, location)
                        continue
                    if r.status_code >= 400:
                        self.http_error(r.status_code, stage)
                    body = bytearray()
                    for chunk in r.iter_bytes():
                        body.extend(chunk)
                        if len(body) >= max_bytes:
                            break
                        if time.monotonic() - self.started > self.policy.probe_deadline_s:
                            self.fail("timeout", stage=stage)
                    return _Response(str(r.url), r.status_code,
                                     r.headers.get("content-type", "").lower(),
                                     bytes(body[:max_bytes]))  # fmt: skip
            except httpx.TimeoutException:
                self.fail("timeout", stage=stage)
            except httpx.ConnectError:
                self.fail("connect", stage=stage)
            except httpx.TransportError:
                self.fail("network", stage=stage)
        self.fail("bad_redirect", stage=stage)

    def http_error(self, status: int, stage: str) -> None:
        if status == 401:
            self.fail("http_401", stage=stage, status=status, reason="authentication_required")
        if status == 451:
            self.fail("http_451", stage=stage, status=status, reason="blocked_by_geo")
        if status == 429:
            self.fail("rate_limited", stage=stage, status=status, uncounted="rate_limited")
        if status in (404, 410):
            self.fail("not_found", stage=stage, status=status)
        if status >= 500:
            self.fail("server_error", stage=stage, status=status)
        # 403 is ambiguous (geo, expired token, hotlink check): an ordinary failure.
        self.fail(f"http_{status}", stage=stage, status=status)

    def fail(self, kind: str, *, stage: str, status: int | None = None,
             reason: str | None = None, uncounted: str | None = None):  # fmt: skip
        raise _Stop(ProbeResult(False, error_kind=kind, http_status=status, reason=reason,
                                stage=stage, uncounted=uncounted,
                                resolution=self.resolution, codec=self.codec,
                                bitrate=self.bitrate))  # fmt: skip

    def result(self, success: bool, *, stage: str, status: int | None = None) -> ProbeResult:
        return ProbeResult(success, http_status=status, stage=stage,
                           startup_ms=int((time.monotonic() - self.started) * 1000),
                           resolution=self.resolution, codec=self.codec,
                           bitrate=self.bitrate)  # fmt: skip


def _is_drm(attrs: dict[str, str]) -> bool:
    method = attrs.get("METHOD", "").upper()
    if method in ("", "NONE", "AES-128"):
        # AES-128 is plain segment encryption with a key URL every player handles.
        return False
    keyformat = attrs.get("KEYFORMAT", "").lower()
    return method.startswith("SAMPLE-AES") or keyformat.startswith(_DRM_KEYFORMAT)


def _int(value: str | None) -> int:
    try:
        return int(value or 0)
    except ValueError:
        return 0
