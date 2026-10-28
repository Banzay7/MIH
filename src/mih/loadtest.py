"""``mih loadtest``: many people opening the web UI at once, against a running server.

Each simulated person does what the home screen does: the app shell, the channel list,
favourites and "on air now", then waits a while (``think``) and opens it again. A browser
keeps what it got and asks again with If-None-Match; the simulated one does the same.
The report gives requests per second and response times per address.
"""

from __future__ import annotations

import asyncio
import os
import random
import time
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field

import httpx

# What the home screen loads, in order.
HOME = ("/", "/app.js", "/app.css", "/api/v1/channels?limit=10000&offset=0",
        "/api/v1/profiles/default/favorites", "/api/v1/now")  # fmt: skip


@dataclass
class Report:
    users: int
    seconds: float
    times: dict[str, list[float]] = field(default_factory=lambda: defaultdict(list))
    errors: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    not_modified: int = 0
    received: int = 0

    def to_dict(self) -> dict:
        total = sum(len(v) for v in self.times.values())
        paths = {}
        for path, ts in sorted(self.times.items()):
            ts = sorted(ts)
            paths[path] = {"count": len(ts), "p50_ms": _pct(ts, 50), "p95_ms": _pct(ts, 95),
                           "p99_ms": _pct(ts, 99), "max_ms": round(ts[-1] * 1000)}  # fmt: skip
        everything = sorted(t for ts in self.times.values() for t in ts)
        return {
            "users": self.users, "seconds": round(self.seconds, 1), "requests": total,
            "rps": round(total / self.seconds, 1) if self.seconds else None,
            "p50_ms": _pct(everything, 50), "p95_ms": _pct(everything, 95),
            "p99_ms": _pct(everything, 99), "errors": dict(self.errors),
            "not_modified": self.not_modified, "wire_mb": round(self.received / 1e6, 1),
            "paths": paths,
        }  # fmt: skip


def _pct(ts: list[float], p: int) -> int | None:
    if not ts:
        return None
    return round(ts[min(len(ts) - 1, int(len(ts) * p / 100))] * 1000)


async def _person(client: httpx.AsyncClient, token: str | None, report: Report,
                  until: float, think: float, fresh: bool) -> None:  # fmt: skip
    etags: dict[str, str] = {}
    # People do not all arrive in the same millisecond.
    await asyncio.sleep(random.uniform(0, min(think, 5.0)))
    while time.monotonic() < until:
        for path in HOME:
            headers = {"Accept-Encoding": "gzip"}
            if token and path.startswith("/api/"):
                headers["Authorization"] = f"Bearer {token}"
            if path in etags:
                headers["If-None-Match"] = etags[path]
            key = path.split("?")[0]
            t0 = time.monotonic()
            try:
                # Bytes as they came over the wire: unpacking them is the browser's work,
                # and here it would only slow the load generator down.
                async with client.stream("GET", path, headers=headers) as resp:
                    body = b"".join([chunk async for chunk in resp.aiter_raw()])
            except httpx.HTTPError as exc:
                report.errors[type(exc).__name__] += 1
                continue
            report.times[key].append(time.monotonic() - t0)
            report.received += len(body)
            if resp.status_code == 304:
                report.not_modified += 1
            elif resp.status_code >= 400:
                report.errors[str(resp.status_code)] += 1
            elif resp.headers.get("etag") and not fresh:
                etags[path] = resp.headers["etag"]
        await asyncio.sleep(random.uniform(think * 0.5, think * 1.5))


def run(base_url: str, token: str | None, users: int, seconds: float, think: float,
        fresh: bool = False, procs: int | None = None) -> Report:  # fmt: skip
    """The people are spread over several processes: one Python process cannot make
    a thousand people's requests and read their answers fast enough on its own."""
    procs = max(1, min(procs or os.cpu_count() or 1, users))
    if procs == 1:
        return asyncio.run(run_async(base_url, token, users, seconds, think, fresh))
    shares = [users // procs + (i < users % procs) for i in range(procs)]
    with ProcessPoolExecutor(procs) as pool:
        parts = list(pool.map(_run_part, [(base_url, token, n, seconds, think, fresh)
                                          for n in shares]))  # fmt: skip
    total = Report(users=users, seconds=max(p.seconds for p in parts))
    for p in parts:
        for k, v in p.times.items():
            total.times[k].extend(v)
        for k, v in p.errors.items():
            total.errors[k] += v
        total.not_modified += p.not_modified
        total.received += p.received
    return total


def _run_part(args: tuple) -> Report:
    report = asyncio.run(run_async(*args))
    # defaultdicts with a lambda do not pickle back to the parent.
    report.times, report.errors = dict(report.times), dict(report.errors)
    return report


async def run_async(base_url: str, token: str | None, users: int, seconds: float,
              think: float, fresh: bool = False) -> Report:  # fmt: skip
    """``fresh``: every visit downloads everything, like a first visit."""
    report = Report(users=users, seconds=seconds)
    limits = httpx.Limits(max_connections=users, max_keepalive_connections=users)
    timeout = httpx.Timeout(60.0)
    async with httpx.AsyncClient(base_url=base_url, limits=limits, timeout=timeout) as client:
        start = time.monotonic()
        until = start + seconds
        await asyncio.gather(*(_person(client, token, report, until, think, fresh)
                               for _ in range(users)))  # fmt: skip
        report.seconds = time.monotonic() - start
    return report
