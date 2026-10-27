"""Two steps after the search: the scout samples a playlist, the packer checks it fully.

Scout (job ``discover``): fetches a few found playlists per run, drops leaked accounts
and DRM (mih.discovery.safety), keeps only streams the catalog does not have yet and
probes a small random sample of them. One stream that plays makes the playlist 'live'.

Packer (job ``pack``): takes one live playlist at a time and probes its new streams in
small batches over several runs. When all are checked, the streams that played are
imported as a source of their own ("auto: ...") and count as checked right away; the
dead ones never reach the catalog. The usual stream check takes over from there.

A probe result is kept per stream URL for a week, so forks of one playlist cost nothing.
"""

from __future__ import annotations

import hashlib
import random
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import unquote, urlsplit

import httpx

from mih.config import Settings
from mih.discovery.harvest import Plan
from mih.discovery.safety import drm_urls, screen, url_problem
from mih.health.policy import HealthPolicy, Observation, format_ts
from mih.health.probe import ProbeResult, probe_stream
from mih.health.runner import record, vantage_online
from mih.ids import new_id, utc_now
from mih.sources.fetch import FetchError, fetch_source
from mih.sources.importer import import_m3u
from mih.sources.m3u import M3UEntry, M3UFormatError, parse_m3u
from mih.sources.service import SourceError, create_source

SAMPLE = 10
RESCOUT = timedelta(days=7)
# A playlist that is unreachable or dead this many scouts in a row is given up.
MAX_MISSES = 3
# A stream URL's probe result is trusted this long.
URL_FRESH = timedelta(days=7)
URL_KEEP = timedelta(days=30)
MAX_ENTRIES = 20000
# Live playlists waiting for the packer (each keeps a copy on disk); the scout pauses above.
LIVE_QUEUE_MAX = 50
PROBE_WORKERS = 4
_LIVE_STATES = ("unverified", "new", "active", "suspect")


@dataclass
class ScoutReport:
    scouted: int = 0
    probes: int = 0
    by_status: dict[str, int] = field(default_factory=dict)
    # Playlists that turned live in this run: (url, sample_ok, sampled, new streams).
    live: list[tuple[str, int, int, int]] = field(default_factory=list)
    offline: bool = False

    def stats(self) -> dict:
        return asdict(self)


@dataclass
class PackReport:
    playlist: str | None = None
    probes: int = 0
    checked: int = 0
    remaining: int = 0
    packed: bool = False
    streams_ok: int = 0
    channels_created: int = 0
    streams_created: int = 0
    capped: bool = False
    offline: bool = False

    def stats(self) -> dict:
        return asdict(self)


def url_hash(url: str) -> str:
    # The same hash the importer uses for streams.locator_hash.
    return hashlib.sha256(url.encode()).hexdigest()


# ---------- Scout ----------


def scout(
    conn: sqlite3.Connection,
    settings: Settings,
    plan: Plan,
    policy: HealthPolicy,
    now: datetime,
    *,
    transport: httpx.BaseTransport | None = None,
) -> ScoutReport:
    report = ScoutReport()
    conn.execute("DELETE FROM discovery_urls WHERE checked_at < ?",
                 (format_ts(now - URL_KEEP),))  # fmt: skip
    waiting = conn.execute(
        "SELECT COUNT(*) FROM discovered_playlists WHERE status = 'live'"
    ).fetchone()[0]
    if waiting >= LIVE_QUEUE_MAX:
        return report  # the packer is behind: no point in sampling more for now
    rows = conn.execute(
        "SELECT * FROM discovered_playlists WHERE next_scout_at IS NOT NULL"
        " AND next_scout_at <= ? AND status != 'live'"
        " ORDER BY status != 'new', next_scout_at LIMIT ?",
        (format_ts(now), settings.discovery_scout),
    ).fetchall()
    if not rows:
        return report
    with _client(policy, transport) as client:
        if not vantage_online(client, policy):
            report.offline = True
            return report
        for row in rows:
            status = _scout_one(conn, settings, plan, policy, client, row, now, report,
                                transport)  # fmt: skip
            report.scouted += 1
            report.by_status[status] = report.by_status.get(status, 0) + 1
    return report


def _scout_one(conn, settings, plan: Plan, policy, client, row, now: datetime,
               report: ScoutReport, transport) -> str:  # fmt: skip
    if row["status"] == "packed" and row["source_id"] and _source_gone(conn, row["source_id"]):
        conn.execute("UPDATE sources SET enabled = 0 WHERE id = ?", (row["source_id"],))
        return _set(conn, row, "retired", "no_live_streams", next_at=None)
    problem = url_problem(row["url"], plan.deny_hosts)
    if problem:
        return _set(conn, row, "rejected", problem, next_at=None)
    try:
        result = fetch_source(row["url"], settings, transport=transport)
    except FetchError as exc:
        return _miss(conn, row, now, exc.code)
    content = result.content or b""
    content_hash = hashlib.sha256(content).hexdigest()
    conn.execute("UPDATE discovered_playlists SET content_hash = ? WHERE id = ?",
                 (content_hash, row["id"]))  # fmt: skip
    if row["content_hash"] == content_hash and row["status"] in ("packed", "known", "duplicate"):
        return _set(conn, row, row["status"], row["reason"], next_at=now + RESCOUT)
    twin = conn.execute(
        "SELECT 1 FROM discovered_playlists WHERE content_hash = ? AND id != ?"
        " AND status IN ('live', 'packed') LIMIT 1",
        (content_hash, row["id"]),
    ).fetchone()
    if twin:
        return _set(conn, row, "duplicate", None, next_at=now + RESCOUT)
    try:
        parsed = parse_m3u(content)
    except M3UFormatError:
        return _set(conn, row, "rejected", "not_m3u", next_at=None)
    if not parsed.entries:
        # A single stream's HLS manifest also lands here: it lists segments, not channels.
        return _set(conn, row, "rejected", "empty", next_at=None)
    if len(parsed.entries) > MAX_ENTRIES:
        return _set(conn, row, "rejected", "too_big", next_at=None)
    kept, reject = screen(parsed.entries, plan.deny_hosts, drm_urls(content))
    if reject:
        return _set(conn, row, "rejected", reject, next_at=None)
    fresh = _new_entries(conn, kept)
    conn.execute("UPDATE discovered_playlists SET entries = ?, new_entries = ? WHERE id = ?",
                 (len(parsed.entries), len(fresh), row["id"]))  # fmt: skip
    if not fresh:
        status = "packed" if row["status"] == "packed" else "known"
        return _set(conn, row, status, None, next_at=now + RESCOUT)

    known = _known_results(conn, [url_hash(e.url) for e in fresh], now)
    ok = sum(1 for e in fresh if known.get(url_hash(e.url)) == 1)
    sampled = sum(1 for e in fresh if url_hash(e.url) in known)
    if not ok:
        unknown = [e for e in fresh if url_hash(e.url) not in known]
        sample = random.sample(unknown, min(SAMPLE, len(unknown)))
        results = _probe(client, sample, policy)
        report.probes += len(sample)
        _remember(conn, sample, results, now)
        ok = sum(1 for r in results if r.success)
        sampled += len(sample)
    conn.execute("UPDATE discovered_playlists SET sampled = ?, sample_ok = ? WHERE id = ?",
                 (sampled, ok, row["id"]))  # fmt: skip
    if not ok and row["status"] == "packed":
        # Only the additions are dead; what was packed before stays.
        return _set(conn, row, "packed", "no_live_sample", next_at=now + RESCOUT)
    if not ok:
        return _miss(conn, row, now, "no_live_sample")
    _content_path(settings, row["id"]).parent.mkdir(parents=True, exist_ok=True)
    _content_path(settings, row["id"]).write_bytes(content)
    report.live.append((row["url"], ok, sampled, len(fresh)))
    conn.execute("UPDATE discovered_playlists SET misses = 0, checked = 0 WHERE id = ?",
                 (row["id"],))  # fmt: skip
    return _set(conn, row, "live", None, next_at=None)


def _miss(conn, row, now: datetime, reason: str) -> str:
    misses = row["misses"] + 1
    conn.execute("UPDATE discovered_playlists SET misses = ? WHERE id = ?", (misses, row["id"]))
    if row["status"] == "packed":
        # The upstream file is gone or broken; the imported streams live on, and the
        # next scout still checks whether they all died.
        return _set(conn, row, "packed", reason, next_at=now + RESCOUT)
    return _set(conn, row, "dead", reason,
                next_at=now + RESCOUT if misses < MAX_MISSES else None)  # fmt: skip


def _set(conn, row, status: str, reason: str | None, *, next_at: datetime | None) -> str:
    conn.execute(
        "UPDATE discovered_playlists SET status = ?, reason = ?, scouted_at = ?,"
        " next_scout_at = ? WHERE id = ?",
        (status, reason, utc_now(), format_ts(next_at), row["id"]),
    )
    return status


def _source_gone(conn, source_id: str) -> bool:
    marks = ",".join("?" * len(_LIVE_STATES))
    row = conn.execute(
        f"SELECT 1 FROM streams WHERE source_id = ? AND status IN ({marks})"
        " AND coalesce(availability_reason, '') != 'source_absent' LIMIT 1",
        (source_id, *_LIVE_STATES),
    ).fetchone()
    return row is None


# ---------- Packer ----------


def pack(
    conn: sqlite3.Connection,
    settings: Settings,
    plan: Plan,
    policy: HealthPolicy,
    now: datetime,
    *,
    transport: httpx.BaseTransport | None = None,
) -> PackReport:
    report = PackReport()
    if auto_streams(conn) >= settings.discovery_max_streams:
        report.capped = True
        return report
    row = conn.execute(
        "SELECT * FROM discovered_playlists WHERE status = 'live'"
        " ORDER BY CAST(sample_ok AS REAL) / max(sampled, 1) DESC, first_seen_at LIMIT 1"
    ).fetchone()
    if row is None:
        return report
    report.playlist = row["url"]
    path = _content_path(settings, row["id"])
    try:
        content = path.read_bytes()
        parsed = parse_m3u(content)
    except (OSError, M3UFormatError):
        # The saved copy is gone (restored backup, cleaned disk): scout it again.
        _set(conn, row, "new", None, next_at=now)
        return report
    kept, _ = screen(parsed.entries, plan.deny_hosts, drm_urls(content))
    own = _own_hashes(conn, row["source_id"])
    unique = _unique(kept)
    elsewhere = _in_catalog(conn, [url_hash(e.url) for e in unique]) - own
    candidates = [e for e in unique if url_hash(e.url) not in elsewhere]
    known = _known_results(conn, [url_hash(e.url) for e in candidates], now)
    to_check = [e for e in candidates if url_hash(e.url) not in own
                and url_hash(e.url) not in known]  # fmt: skip
    batch = to_check[: settings.discovery_pack_batch]
    results: dict[str, ProbeResult] = {}
    if batch:
        with _client(policy, transport) as client:
            if not vantage_online(client, policy):
                report.offline = True
                return report
            probed = _probe(client, batch, policy)
        _remember(conn, batch, probed, now)
        results = {url_hash(e.url): r for e, r in zip(batch, probed, strict=True)}
        report.probes = len(batch)
    checked = len(candidates) - len(own) - len(to_check) + len(batch)
    report.checked, report.remaining = checked, len(to_check) - len(batch)
    conn.execute("UPDATE discovered_playlists SET checked = ? WHERE id = ?",
                 (checked, row["id"]))  # fmt: skip
    if report.remaining:
        return report

    known = _known_results(conn, [url_hash(e.url) for e in candidates], now)
    keep = [e for e in candidates if url_hash(e.url) in own or known.get(url_hash(e.url)) == 1]
    fresh = [e for e in keep if url_hash(e.url) not in own]
    _content_path(settings, row["id"]).unlink(missing_ok=True)
    if not keep:
        _miss(conn, row, now, "no_live")
        return report
    report.streams_ok = len(keep)
    if fresh:
        source_id = row["source_id"] or _make_source(conn, settings, row["url"])
        imported = import_m3u(conn, settings, source_id, _playlist(keep), force=True)
        report.channels_created = imported.channels_created
        report.streams_created = imported.streams_created
        _record_ok(conn, policy, source_id, fresh, results, now)
        conn.execute(
            "UPDATE discovered_playlists SET source_id = ?, packed_at = ? WHERE id = ?",
            (source_id, format_ts(now), row["id"]),
        )
        report.packed = True
    conn.execute("UPDATE discovered_playlists SET packed_ok = ?, misses = 0 WHERE id = ?",
                 (len(keep), row["id"]))  # fmt: skip
    _set(conn, row, "packed", None, next_at=now + RESCOUT)
    return report


def auto_streams(conn: sqlite3.Connection) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM streams WHERE source_id IN"
        " (SELECT source_id FROM discovered_playlists WHERE source_id IS NOT NULL)"
    ).fetchone()[0]


def _make_source(conn, settings: Settings, url: str) -> str:
    parts = urlsplit(url)
    tail = "/".join(unquote(parts.path).strip("/").split("/")[-3:])
    name = f"auto: {parts.hostname}/{tail}"[:90]
    try:
        # No URL: the packed copy is imported by the packer, never by the playlist refresh.
        return create_source(conn, settings, name=name, url=None)
    except SourceError:
        return create_source(conn, settings, name=f"{name} {new_id()[:6]}", url=None)


def _playlist(entries: list[M3UEntry]) -> bytes:
    # The playlist's url-tvg is left out: a found guide can be huge, and the guides the
    # install already has match these channels by tvg-id and name.
    return ("#EXTM3U\n" + "\n".join(e.raw for e in entries) + "\n").encode()


def _record_ok(conn, policy: HealthPolicy, source_id: str, entries: list[M3UEntry],
               results: dict[str, ProbeResult], now: datetime) -> None:  # fmt: skip
    """The packer's own probe counts as the stream's first check."""
    hashes = [url_hash(e.url) for e in entries]
    conn.execute("BEGIN")
    try:
        for chunk in _chunks(hashes):
            marks = ",".join("?" * len(chunk))
            for s in conn.execute(
                f"SELECT id, locator_hash FROM streams WHERE source_id = ?"
                f" AND locator_hash IN ({marks})", (source_id, *chunk),
            ).fetchall():  # fmt: skip
                obs = Observation(at=now, vantage=policy.vantage, success=True)
                record(conn, s["id"], obs, policy, kind="probe",
                       result=results.get(s["locator_hash"]))  # fmt: skip
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


# ---------- Shared ----------


def _client(policy: HealthPolicy, transport) -> httpx.Client:
    timeout = httpx.Timeout(policy.read_timeout_s, connect=policy.connect_timeout_s)
    return httpx.Client(timeout=timeout, follow_redirects=False, transport=transport)


def _probe(client: httpx.Client, entries: list[M3UEntry], policy: HealthPolicy
           ) -> list[ProbeResult]:  # fmt: skip
    # Found playlists are public: a stream on a LAN address is not ours to probe.
    def one(e: M3UEntry) -> ProbeResult:
        return probe_stream(client, e.url, e.headers, policy, allow_private=False)

    workers = max(1, min(PROBE_WORKERS, policy.concurrency, len(entries)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(one, entries))


def _remember(conn, entries: list[M3UEntry], results: list[ProbeResult], now: datetime) -> None:
    rows = [
        (url_hash(e.url), int(r.success), r.error_kind, format_ts(now))
        for e, r in zip(entries, results, strict=True)
        if r.success or r.uncounted != "rate_limited"  # a 429 says nothing; ask again later
    ]
    conn.executemany(
        "INSERT INTO discovery_urls (url_hash, ok, error_kind, checked_at) VALUES (?, ?, ?, ?)"
        " ON CONFLICT(url_hash) DO UPDATE SET ok = excluded.ok,"
        " error_kind = excluded.error_kind, checked_at = excluded.checked_at",
        rows,
    )


def _known_results(conn, hashes: list[str], now: datetime) -> dict[str, int]:
    since = format_ts(now - URL_FRESH)
    out = {}
    for chunk in _chunks(hashes):
        marks = ",".join("?" * len(chunk))
        for r in conn.execute(
            f"SELECT url_hash, ok FROM discovery_urls WHERE checked_at >= ?"
            f" AND url_hash IN ({marks})", (since, *chunk),
        ):  # fmt: skip
            out[r["url_hash"]] = r["ok"]
    return out


def _in_catalog(conn, hashes: list[str]) -> set[str]:
    out = set()
    for chunk in _chunks(hashes):
        marks = ",".join("?" * len(chunk))
        sql = f"SELECT locator_hash FROM streams WHERE locator_hash IN ({marks})"
        out |= {r[0] for r in conn.execute(sql, chunk)}
    return out


def _new_entries(conn, entries: list[M3UEntry]) -> list[M3UEntry]:
    unique = _unique(entries)
    have = _in_catalog(conn, [url_hash(e.url) for e in unique])
    return [e for e in unique if url_hash(e.url) not in have]


def _own_hashes(conn, source_id: str | None) -> set[str]:
    if not source_id:
        return set()
    return {r[0] for r in conn.execute(
        "SELECT locator_hash FROM streams WHERE source_id = ?", (source_id,))}  # fmt: skip


def _unique(entries: list[M3UEntry]) -> list[M3UEntry]:
    seen, out = set(), []
    for e in entries:
        if e.url not in seen:
            seen.add(e.url)
            out.append(e)
    return out


def _chunks(items: list[str], size: int = 500):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def _content_path(settings: Settings, playlist_id: str) -> Path:
    return settings.data_dir / "discovery" / f"{playlist_id}.m3u"


def summary(conn: sqlite3.Connection) -> dict:
    """How far the discovery got: for the status screen, the digest and ``mih discovery``."""
    by_status = {r["status"]: r["n"] for r in conn.execute(
        "SELECT status, COUNT(*) AS n FROM discovered_playlists GROUP BY status")}  # fmt: skip
    by_origin = {r["origin"]: r["n"] for r in conn.execute(
        "SELECT origin, COUNT(*) AS n FROM discovered_playlists GROUP BY origin")}  # fmt: skip
    marks = ",".join("?" * len(_LIVE_STATES))
    auto = conn.execute(
        f"SELECT COUNT(*) AS streams, SUM(s.status IN ({marks})) AS working,"
        " COUNT(DISTINCT f.channel_id) AS channels FROM streams s"
        " JOIN feeds f ON f.id = s.feed_id WHERE s.source_id IN"
        " (SELECT source_id FROM discovered_playlists WHERE source_id IS NOT NULL)",
        _LIVE_STATES,
    ).fetchone()
    return {
        "found": sum(by_status.values()),
        "by_status": by_status,
        "by_origin": by_origin,
        "repos": conn.execute("SELECT COUNT(*) FROM discovered_repos").fetchone()[0],
        "repos_waiting": conn.execute(
            "SELECT COUNT(*) FROM discovered_repos WHERE scanned_at IS NULL"
        ).fetchone()[0],
        "urls_checked": conn.execute("SELECT COUNT(*) FROM discovery_urls").fetchone()[0],
        "auto_streams": auto["streams"],
        "auto_working": auto["working"] or 0,
        "auto_channels": auto["channels"],
    }
