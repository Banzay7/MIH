"""What the API needs once it is reachable from the internet (docs/public.md).

Every request is counted against the address it came from: past MIH_RATE_LIMIT requests a
minute the address gets 429 for the rest of that minute, and after MIH_AUTH_FAILS wrong
tokens in 15 minutes it gets 429 for 15 minutes, so a token cannot be guessed and one
client cannot hold the server for everyone else. Behind a reverse proxy (Tailscale Funnel,
Cloudflare Tunnel, Caddy) the address is the one the proxy puts into X-Forwarded-For; that
header is believed only from the addresses in MIH_TRUSTED_PROXIES.

Every answer also carries the headers a public site wants: no framing, no MIME sniffing,
no referrer (a /play link holds its key), HSTS once the request came over HTTPS.

Plain ASGI, like the other middlewares: one dict lookup per request, no task or queue.
The counters live in the process; with MIH_WORKERS > 1 each worker keeps its own."""

from __future__ import annotations

import ipaddress
import time

from mih.config import parse_networks

FAIL_WINDOW_S = 15 * 60
# Addresses kept at most; past it the ones that went quiet are forgotten first.
MAX_TRACKED = 20000

_HEADERS = [
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"no-referrer"),
    (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
    (b"cross-origin-opener-policy", b"same-origin"),
]
_HSTS = (b"strict-transport-security", b"max-age=31536000")
_LOOPBACK = parse_networks("127.0.0.0/8, ::1/128")


def client_address(scope, trusted: tuple) -> str:
    peer = (scope.get("client") or ("", 0))[0]
    if not trusted or not _within(peer, trusted):
        return peer
    forwarded = _header(scope, b"x-forwarded-for")
    if not forwarded:
        return peer
    # The proxy appends the address it saw; walk back past our own proxies.
    for hop in reversed([h.strip() for h in forwarded.split(",") if h.strip()]):
        if not _within(hop, trusted):
            return hop
    return peer


def _within(addr: str, networks: tuple) -> bool:
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    return any(ip in n for n in networks if n.version == ip.version)


def _header(scope, name: bytes) -> str:
    for k, v in scope.get("headers", ()):
        if k == name:
            return v.decode("latin-1")
    return ""


class _Client:
    __slots__ = ("minute", "count", "fails", "blocked_until")

    def __init__(self):
        self.minute, self.count = 0, 0
        self.fails: list[float] = []
        self.blocked_until = 0.0


class Guard:
    def __init__(self, app, *, rate_limit: int, auth_fails: int, trusted_proxies: str,
                 clock=time.monotonic):  # fmt: skip
        self.app, self.rate_limit, self.auth_fails = app, rate_limit, auth_fails
        self.trusted, self.clock = parse_networks(trusted_proxies), clock
        self.clients: dict[str, _Client] = {}

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        now = self.clock()
        addr = client_address(scope, self.trusted)
        c = self._client(addr, now)
        # The container's own healthcheck and `mih loadtest` come from loopback.
        retry = self._refused(c, now, cap=not _within(addr, _LOOPBACK))
        https = scope.get("scheme") == "https" or (
            _within((scope.get("client") or ("", 0))[0], self.trusted)
            and _header(scope, b"x-forwarded-proto") == "https"
        )
        extra = [*_HEADERS, _HSTS] if https else _HEADERS
        if retry:
            return await _too_many(send, retry, extra)

        status = 0

        async def send_with_headers(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                message["headers"] = [*message.get("headers", []), *extra]
            await send(message)

        await self.app(scope, receive, send_with_headers)
        if status == 401 and self.auth_fails:
            c.fails = [t for t in c.fails if now - t < FAIL_WINDOW_S] + [now]
            if len(c.fails) >= self.auth_fails:
                c.blocked_until, c.fails = now + FAIL_WINDOW_S, []

    def _client(self, addr: str, now: float) -> _Client:
        c = self.clients.get(addr)
        if c is None:
            if len(self.clients) >= MAX_TRACKED:
                self._forget(now)
            c = self.clients[addr] = _Client()
        return c

    def _refused(self, c: _Client, now: float, cap: bool) -> int:
        """Seconds to wait, or 0 when the request may go on."""
        if c.blocked_until > now:
            return int(c.blocked_until - now) + 1
        if not self.rate_limit or not cap:
            return 0
        minute = int(now // 60)
        if minute != c.minute:
            c.minute, c.count = minute, 0
        c.count += 1
        if c.count > self.rate_limit:
            return 60 - int(now % 60)
        return 0

    def _forget(self, now: float) -> None:
        minute = int(now // 60)
        quiet = [a for a, c in self.clients.items() if c.minute < minute
                 and c.blocked_until <= now and not c.fails]  # fmt: skip
        for a in quiet or list(self.clients)[: MAX_TRACKED // 2]:
            del self.clients[a]


async def _too_many(send, retry: int, extra: list) -> None:
    body = b'{"error":{"code":"too_many_requests","message":"too many requests, try later"}}'
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode()),
        (b"retry-after", str(retry).encode()),
        (b"cache-control", b"no-store"),
        *extra,
    ]
    await send({"type": "http.response.start", "status": 429, "headers": headers})
    await send({"type": "http.response.body", "body": body})
