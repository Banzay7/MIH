"""Answers that are the same for everyone, built once and shared for a few seconds.

The channel list and "on air now" are hundreds of KB of JSON that every visitor asks
for. Built per request, each costs a database pass, a JSON dump and a gzip; a few dozen
people at once queue behind one another. Here each is built once per ``ttl``, kept
already compressed, and answered with an ETag, so a browser that has it gets a 304.

When an entry is stale, one request rebuilds it and the others keep getting the old
one meanwhile: nobody waits on a rebuild except the very first visitor.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from fastapi import Request
from fastapi.responses import Response

# Distinct queries kept (``/now?channel_id=...`` varies with favourites).
MAX_ENTRIES = 256


@dataclass(frozen=True)
class Entry:
    body: bytes
    gz: bytes
    etag: str
    built: float
    ttl: float


class SharedCache:
    def __init__(self) -> None:
        self._items: dict[str, Entry] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def get(self, key: str, ttl: float, build: Callable[[], dict]) -> Entry:
        entry = self._items.get(key)
        if entry and time.monotonic() - entry.built < entry.ttl:
            return entry
        lock = self._lock(key)
        # Someone is already rebuilding: the stale copy is good enough for now.
        if entry and not lock.acquire(blocking=False):
            return entry
        if not entry:
            lock.acquire()
        try:
            fresh = self._items.get(key)
            if fresh and time.monotonic() - fresh.built < fresh.ttl:
                return fresh
            body = json.dumps(build(), ensure_ascii=False, separators=(",", ":")).encode()
            etag = '"' + hashlib.blake2b(body, digest_size=12).hexdigest() + '"'
            entry = Entry(body, gzip.compress(body, 6), etag, time.monotonic(), ttl)
            with self._guard:
                if len(self._items) >= MAX_ENTRIES and key not in self._items:
                    self._items.pop(next(iter(self._items)))
                self._items[key] = entry
            return entry
        finally:
            lock.release()

    def clear(self) -> None:
        with self._guard:
            self._items.clear()

    def _lock(self, key: str) -> threading.Lock:
        with self._guard:
            if len(self._locks) > MAX_ENTRIES * 2:
                self._locks = {k: v for k, v in self._locks.items() if k in self._items}
            return self._locks.setdefault(key, threading.Lock())


def respond(request: Request, entry: Entry, max_age: int) -> Response:
    # private: the API needs a token, so only the browser keeps a copy, never a proxy.
    headers = {"ETag": entry.etag, "Cache-Control": f"private, max-age={max_age}",
               "Vary": "Accept-Encoding, Authorization"}  # fmt: skip
    if entry.etag in request.headers.get("if-none-match", ""):
        return Response(status_code=304, headers=headers)
    if "gzip" in request.headers.get("accept-encoding", ""):
        headers["Content-Encoding"] = "gzip"
        return Response(entry.gz, media_type="application/json", headers=headers)
    return Response(entry.body, media_type="application/json", headers=headers)
