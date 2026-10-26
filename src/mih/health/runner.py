"""Health-check runs: pick due streams, probe them in parallel, apply the lifecycle."""

from __future__ import annotations

import json
import sqlite3
import threading
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta

import httpx

from mih.config import Settings
from mih.health.policy import (
    HealthPolicy,
    Observation,
    StreamState,
    decide,
    format_ts,
    next_check,
    parse_ts,
)
from mih.health.probe import ProbeResult, probe_stream
from mih.ids import new_id

# A whole playlist failing at once is the provider (or the account), not its streams.
SOURCE_OUTAGE_MIN_STREAMS = 3
# Most of a batch failing at the network level is the prober's own connection.
VANTAGE_OUTAGE_MIN_STREAMS = 10
VANTAGE_OUTAGE_SHARE = 0.8

_STATE_COLS = ("status, availability_reason, status_changed_at, last_ok_at, first_fail_at,"
               " streak_failures, streak_windows, last_fail_window, last_device_ok_at,"
               " last_device_fail_at")  # fmt: skip


@dataclass
class RunReport:
    run_id: str
    status: str = "done"  # done | vantage_offline
    vantage: str = "server"
    checked: int = 0
    ok: int = 0
    failed: int = 0
    uncounted: int = 0
    transitions: dict[str, int] = field(default_factory=dict)
    source_outages: list[str] = field(default_factory=list)
    errors: dict[str, int] = field(default_factory=dict)

    def stats(self) -> dict:
        return asdict(self)


def run_checks(
    conn: sqlite3.Connection,
    settings: Settings,
    policy: HealthPolicy,
    *,
    stream_ids: list[str] | None = None,
    limit: int | None = None,
    now: datetime | None = None,
    transport: httpx.BaseTransport | None = None,
) -> RunReport:
    """Probe the streams that are due (or the ones named) and record what was seen."""
    now = now or datetime.now(UTC)
    report = RunReport(new_id(), vantage=policy.vantage)
    conn.execute(
        "INSERT INTO health_runs (id, started_at, vantage, status) VALUES (?, ?, ?, 'running')",
        (report.run_id, format_ts(now), policy.vantage),
    )
    _age_all(conn, now, policy)
    rows = _interleave(_due(conn, now, limit or policy.batch_limit, stream_ids))

    timeout = httpx.Timeout(policy.read_timeout_s, connect=policy.connect_timeout_s)
    limits = httpx.Limits(max_connections=policy.concurrency * 2)
    with httpx.Client(timeout=timeout, limits=limits, follow_redirects=False,
                      transport=transport) as client:  # fmt: skip
        if rows and not _vantage_online(client, policy):
            # Without a network nothing we see says anything about the streams.
            report.status = "vantage_offline"
            _finish_run(conn, report)
            return report
        results = _probe_all(client, rows, settings, policy)

    outages = _source_outages(rows, results)
    report.source_outages = sorted(outages)
    vantage_down = _vantage_outage(results)

    conn.execute("BEGIN")
    try:
        for row, res in zip(rows, results, strict=True):
            uncounted = res.uncounted
            if not res.success and not uncounted and not res.reason:
                if vantage_down and res.network_level:
                    uncounted = "vantage_outage"
                elif row["source_id"] in outages:
                    uncounted = "source_outage"
            obs = Observation(at=now, vantage=policy.vantage, success=res.success,
                              reason=res.reason, uncounted=uncounted)  # fmt: skip
            decision = record(conn, row["id"], obs, policy, kind="probe", result=res,
                              run_id=report.run_id)  # fmt: skip
            report.checked += 1
            if not decision.counted:
                report.uncounted += 1
            elif res.success:
                report.ok += 1
            else:
                report.failed += 1
            if res.error_kind:
                report.errors[res.error_kind] = report.errors.get(res.error_kind, 0) + 1
            if decision.cause:
                key = f"{row['status']}->{decision.state.status}"
                report.transitions[key] = report.transitions.get(key, 0) + 1
        cutoff = format_ts(now - timedelta(days=policy.observation_retention_days))
        conn.execute("DELETE FROM stream_observations WHERE checked_at < ?", (cutoff,))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    _finish_run(conn, report)
    return report


def record(
    conn: sqlite3.Connection,
    stream_id: str,
    obs: Observation,
    policy: HealthPolicy,
    *,
    kind: str,
    result: ProbeResult | None = None,
    run_id: str | None = None,
    startup_ms: int | None = None,
    error_kind: str | None = None,
):
    """Store one observation and move the stream's state. Runs inside the caller's
    transaction when there is one."""
    row = conn.execute(f"SELECT {_STATE_COLS} FROM streams WHERE id = ?", (stream_id,)).fetchone()
    before = _state(row)
    decision = decide(before, obs, policy)
    res = result or ProbeResult(obs.success, error_kind=error_kind, startup_ms=startup_ms)
    obs_id = new_id()
    conn.execute(
        "INSERT INTO stream_observations (id, stream_id, run_id, checked_at, vantage, kind,"
        " success, counted, note, error_kind, http_status, stage, startup_ms, resolution,"
        " codec, bitrate) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (obs_id, stream_id, run_id, format_ts(obs.at), obs.vantage, kind, int(obs.success),
         int(decision.counted), decision.note, res.error_kind, res.http_status, res.stage,
         res.startup_ms, res.resolution, res.codec, res.bitrate),
    )  # fmt: skip
    s = decision.state
    checked = obs.vantage != "device"
    conn.execute(
        "UPDATE streams SET status = ?, availability_reason = ?, status_changed_at = ?,"
        " last_ok_at = ?, first_fail_at = ?, streak_failures = ?, streak_windows = ?,"
        " last_fail_window = ?, last_device_ok_at = ?, last_device_fail_at = ?,"
        " last_checked_at = CASE WHEN ? THEN ? ELSE last_checked_at END,"
        " next_check_at = CASE WHEN ? THEN ? ELSE next_check_at END WHERE id = ?",
        (s.status, s.availability_reason, format_ts(s.status_changed_at),
         format_ts(s.last_ok_at), format_ts(s.first_fail_at), s.streak_failures,
         s.streak_windows, s.last_fail_window, format_ts(s.last_device_ok_at),
         format_ts(s.last_device_fail_at), checked, format_ts(obs.at), checked,
         format_ts(next_check(s, obs.at, policy, counted=decision.counted)), stream_id),
    )  # fmt: skip
    if decision.cause:
        conn.execute(
            "INSERT INTO stream_health_events (id, stream_id, at, from_status, to_status, cause,"
            " observation_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (new_id(), stream_id, format_ts(obs.at), before.status, s.status, decision.cause,
             obs_id),
        )  # fmt: skip
    return decision


def _due(conn, now: datetime, limit: int, stream_ids: list[str] | None) -> list[sqlite3.Row]:
    cols = f"s.id, s.source_id, {_STATE_COLS}, l.url, l.headers"
    base = (
        f"SELECT {cols} FROM streams s JOIN stream_locators l ON l.stream_id = s.id"
        " JOIN sources src ON src.id = s.source_id AND src.enabled = 1"
        # A stream the playlist no longer lists is the importer's business, not ours.
        " WHERE coalesce(s.availability_reason, '') NOT IN ('source_absent', 'policy_excluded')"
    )
    if stream_ids:
        marks = ",".join("?" * len(stream_ids))
        return conn.execute(f"{base} AND s.id IN ({marks}) LIMIT ?",
                            (*stream_ids, limit)).fetchall()  # fmt: skip
    return conn.execute(
        f"{base} AND (s.next_check_at IS NULL OR s.next_check_at <= ?)"
        " ORDER BY s.next_check_at IS NOT NULL, s.next_check_at, s.first_seen_at LIMIT ?",
        (format_ts(now), limit),
    ).fetchall()


def _interleave(rows: list[sqlite3.Row]) -> list[sqlite3.Row]:
    """Round-robin over sources so workers are not all queued behind one playlist."""
    rank: Counter = Counter()
    keyed = []
    for i, row in enumerate(rows):
        keyed.append((rank[row["source_id"]], i, row))
        rank[row["source_id"]] += 1
    return [row for _, _, row in sorted(keyed, key=lambda k: k[:2])]


def _probe_all(client, rows, settings: Settings, policy: HealthPolicy) -> list[ProbeResult]:
    per_source: dict[str, threading.Semaphore] = defaultdict(
        lambda: threading.BoundedSemaphore(policy.per_source_concurrency)
    )
    for row in rows:
        per_source[row["source_id"]]  # create up front, outside the worker threads

    def one(row) -> ProbeResult:
        with per_source[row["source_id"]]:
            headers = json.loads(row["headers"]) if row["headers"] else None
            return probe_stream(client, row["url"], headers, policy,
                                allow_private=settings.allow_private_sources)  # fmt: skip

    with ThreadPoolExecutor(max_workers=policy.concurrency) as pool:
        return list(pool.map(one, rows))


def _vantage_online(client: httpx.Client, policy: HealthPolicy) -> bool:
    if not policy.canary_urls:
        return True
    for url in policy.canary_urls:
        try:
            if client.head(url).status_code < 500:
                return True
        except httpx.HTTPError:
            continue
    return False


def _source_outages(rows, results: list[ProbeResult]) -> set[str]:
    total, failing = Counter(), Counter()
    for row, res in zip(rows, results, strict=True):
        if res.uncounted or res.reason:
            continue
        total[row["source_id"]] += 1
        failing[row["source_id"]] += not res.success
    return {sid for sid, n in total.items() if n >= SOURCE_OUTAGE_MIN_STREAMS and failing[sid] == n}


def _vantage_outage(results: list[ProbeResult]) -> bool:
    judged = [r for r in results if not r.uncounted]
    if len(judged) < VANTAGE_OUTAGE_MIN_STREAMS:
        return False
    return sum(r.network_level for r in judged) >= VANTAGE_OUTAGE_SHARE * len(judged)


def _age_all(conn: sqlite3.Connection, now: datetime, policy: HealthPolicy) -> None:
    """Archive streams that sat in quarantine too long, probed or not (see policy.age)."""
    cutoff = format_ts(now - timedelta(days=policy.archive_after_days))
    ids = [r["id"] for r in conn.execute(
        "SELECT id FROM streams WHERE status = 'quarantine' AND status_changed_at <= ?",
        (cutoff,),
    )]  # fmt: skip
    for sid in ids:
        conn.execute(
            "UPDATE streams SET status = 'archived', status_changed_at = ? WHERE id = ?",
            (format_ts(now), sid),
        )
        conn.execute(
            "INSERT INTO stream_health_events (id, stream_id, at, from_status, to_status,"
            " cause) VALUES (?, ?, ?, 'quarantine', 'archived', 'quarantine_expired')",
            (new_id(), sid, format_ts(now)),
        )


def _finish_run(conn: sqlite3.Connection, report: RunReport) -> None:
    conn.execute(
        "UPDATE health_runs SET finished_at = ?, status = ?, stats = ? WHERE id = ?",
        (format_ts(datetime.now(UTC)), report.status, json.dumps(report.stats()),
         report.run_id),
    )  # fmt: skip


def _state(row: sqlite3.Row) -> StreamState:
    return StreamState(
        status=row["status"],
        availability_reason=row["availability_reason"],
        status_changed_at=parse_ts(row["status_changed_at"]),
        last_ok_at=parse_ts(row["last_ok_at"]),
        first_fail_at=parse_ts(row["first_fail_at"]),
        streak_failures=row["streak_failures"],
        streak_windows=row["streak_windows"],
        last_fail_window=row["last_fail_window"],
        last_device_ok_at=parse_ts(row["last_device_ok_at"]),
        last_device_fail_at=parse_ts(row["last_device_fail_at"]),
    )
