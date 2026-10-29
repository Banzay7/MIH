"""Answers that are the same for everyone, built once and shared for a few seconds.

The channel list and "on air now" are hundreds of KB of JSON that every visitor asks
for. Built per request, each costs a database pass, a JSON dump and a gzip; a few dozen
people at once queue behind one another. Here each is built once per ``ttl``, kept
already compressed, and answered with an ETag, so a browser that has it gets a 304.

When an entry is stale, one request rebuilds it and the others keep getting the old
one meanwhile: nobody waits on a rebuild except the very first visitor.

Everything is also dropped when the data version moves: a playlist or guide import, a
stream check, or a favourite or topic changed. The browser gets that version with every
answer and keeps its own copies until it moves, so opening a tab again costs nothing.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from fastapi import Request
from fastapi.responses import Response

# Distinct queries kept (``/now?channel_id=...`` varies with favourites).
MAX_ENTRIES = 256
# How often the data version is read from the database (seconds).
VERSION_EVERY = 2.0

# What the shown data depends on: the data jobs' last finish, the newest import and stream
# check (also when run from the command line), and the personal lists.
_VERSION_SQL = (
    "SELECT (SELECT group_concat(name || ':' || COALESCE(last_finished_at, ''), ',') FROM"
    "  (SELECT name, last_finished_at FROM jobs WHERE name IN ('sources', 'epg', 'health', 'pack')"
    "   ORDER BY name)),"
    " (SELECT max(rowid) FROM source_snapshots), (SELECT max(rowid) FROM epg_snapshots),"
    " (SELECT max(rowid) FROM health_runs),"
    " (SELECT count(*) || '/' || COALESCE(max(rowid), 0) FROM favorites),"
    " (SELECT count(*) || '/' || COALESCE(max(rowid), 0) FROM tracked_topics)"
)


def data_version(conn: sqlite3.Connection) -> str:
    row = conn.execute(_VERSION_SQL).fetchone()
    return hashlib.blake2b(repr(tuple(row)).encode(), digest_size=6).hexdigest()


@dataclass(frozen=True)
class Entry:
    body: bytes
    gz: bytes
    etag: str
    built: float
    ttl: float


class SharedCache:
    def __init__(self, probe: Callable[[], str] | None = None) -> None:
        self._probe = probe  # reads the data version
        self.version = ""
        self._probed = float("-inf")
        self._items: dict[str, Entry] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()
        # Parsed objects that several cached answers are cut from; cleared with them.
        self.memo: dict[str, tuple[float, object]] = {}

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

    def check(self, *, now: bool = False) -> str:
        """The data version, read at most every VERSION_EVERY; a new one drops everything."""
        if self._probe and (now or time.monotonic() - self._probed >= VERSION_EVERY):
            self._probed = time.monotonic()
            try:
                version = self._probe()
            except sqlite3.Error:
                return self.version
            if version != self.version:
                if self.version:
                    self.clear()
                self.version = version
        return self.version

    def clear(self) -> None:
        with self._guard:
            self._items.clear()
            self.memo.clear()

    def remember(self, key: str, ttl: float, build: Callable[[], object]) -> object:
        """A parsed object kept for ``ttl``: the base that paged answers are cut from."""
        hit = self.memo.get(key)
        if hit and time.monotonic() - hit[0] < ttl:
            return hit[1]
        value = build()
        self.memo[key] = (time.monotonic(), value)
        return value

    def _lock(self, key: str) -> threading.Lock:
        with self._guard:
            if len(self._locks) > MAX_ENTRIES * 2:
                self._locks = {k: v for k, v in self._locks.items() if k in self._items}
            return self._locks.setdefault(key, threading.Lock())


def respond(request: Request, entry: Entry) -> Response:
    # private: the API needs a token, so only the browser keeps a copy, never a proxy.
    # no-cache: the web UI decides itself when a copy is still good (by the data version);
    # anything else asks again and gets a 304 while the answer is the same.
    headers = {"ETag": entry.etag, "Cache-Control": "private, no-cache",
               "Vary": "Accept-Encoding, Authorization"}  # fmt: skip
    if entry.etag in request.headers.get("if-none-match", ""):
        return Response(status_code=304, headers=headers)
    if "gzip" in request.headers.get("accept-encoding", ""):
        headers["Content-Encoding"] = "gzip"
        return Response(entry.gz, media_type="application/json", headers=headers)
    return Response(entry.body, media_type="application/json", headers=headers)
