"""Background jobs of ``mih serve``: playlist and guide refresh, stream checks, the digest.

Each job's next run, its last result and whether it is running now live in the jobs
table, so the status screen can show what the service is doing and what comes next.
A job can also run on request (API or ``mih run-job``); the same row guards against two
runs of one job at once.
"""

from __future__ import annotations

import json
import logging
import shutil
import sqlite3
import subprocess
import sys
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

from mih import __version__, db
from mih.alerts import alert_line, find_matches, load_rules, run_alerts
from mih.config import Settings
from mih.discovery.harvest import harvest, load_plan
from mih.discovery.scout import pack, scout, summary
from mih.drive import DriveError, sync_drive
from mih.epg.auto import run_autopilot
from mih.epg.coverage import working_coverage
from mih.epg.service import import_epg_from_url
from mih.events import list_events, record_event, t
from mih.health.policy import HealthPolicy
from mih.health.runner import run_checks
from mih.ids import utc_now
from mih.lists import build_presets, load_presets
from mih.report import REPORT_EVERY, report_path, write_report
from mih.sources.fetch import FetchError
from mih.sources.service import SourceError, import_from_url

log = logging.getLogger(__name__)

JOBS = ("sources", "epg", "health", "alerts", "discover", "pack", "drive", "digest")
# A run that started this long ago and never finished belongs to a dead process.
STALE_AFTER = timedelta(hours=6)
# After a start, interval jobs wait this long, so a restart loop does not hammer sources.
FIRST_RUN_DELAY = timedelta(minutes=2)
_DOWN = ("suspect", "quarantine", "archived")
# Between guide imports, a list's guide file is still rewritten this often, so the
# day it starts with moves on.
GUIDE_MAX_AGE = timedelta(hours=6)
# A guide refresh that put new guides on trial runs again this soon to fetch them.
AUTOPILOT_AGAIN = timedelta(minutes=5)


class JobBusy(Exception):
    """The job is already running."""


@dataclass
class Outcome:
    status: str  # ok | warning | error
    level: str  # event level
    title: str
    lines: list[str] = field(default_factory=list)
    data: dict = field(default_factory=dict)
    # Nothing worth a line in the feed (a scan that found nothing new): only the job row
    # is updated, unless someone asked for the run.
    quiet: bool = False
    # Run again this soon instead of at the usual interval (new work for the job).
    again_in: timedelta | None = None


def _fmt(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def next_run(settings: Settings, name: str, after: datetime) -> datetime | None:
    if name == "digest":
        if settings.digest_at is None:
            return None
        tz = ZoneInfo(settings.timezone)
        local = after.astimezone(tz)
        at = local.replace(hour=settings.digest_at.hour, minute=settings.digest_at.minute,
                           second=0, microsecond=0)  # fmt: skip
        if at <= local:
            at += timedelta(days=1)
        return at.astimezone(UTC)
    every = _every(settings, name)
    return after + every if every else None


def _every(settings: Settings, name: str) -> timedelta | None:
    return {"sources": settings.every_sources, "epg": settings.every_epg,
            "health": settings.every_health,
            "alerts": settings.every_alerts if settings.alert_lead else None,
            "discover": settings.every_discover, "pack": settings.every_pack,
            "drive": settings.every_drive if settings.drive_token else None}[name]  # fmt: skip


def sync_jobs(
    conn: sqlite3.Connection, settings: Settings, now: datetime | None = None
) -> list[str]:
    """Create missing job rows and align the plan with the settings. Returns the names of
    runs a previous process left unfinished (their rows are cleared)."""
    now = now or datetime.now(UTC)
    interrupted = []
    for name in JOBS:
        row = conn.execute("SELECT * FROM jobs WHERE name = ?", (name,)).fetchone()
        if name == "digest":
            planned = next_run(settings, name, now)
        else:
            planned = now + FIRST_RUN_DELAY if _every(settings, name) else None
        if row is None:
            conn.execute("INSERT INTO jobs (name, next_run_at) VALUES (?, ?)",
                         (name, _fmt(planned) if planned else None))  # fmt: skip
            continue
        if row["running_since"]:
            interrupted.append(name)
            conn.execute(
                "UPDATE jobs SET running_since = NULL, last_status = 'error',"
                " last_finished_at = ? WHERE name = ?",
                (_fmt(now), name),
            )
        if planned is None or row["next_run_at"] is None:
            # Switched off, or just switched on.
            value = _fmt(planned) if planned else None
            conn.execute("UPDATE jobs SET next_run_at = ? WHERE name = ?", (value, name))
            continue
        current = _parse(row["next_run_at"])
        latest = planned if name == "digest" else now + _every(settings, name)
        if current > latest or (name == "digest" and current != planned and current > now):
            # The interval got shorter or the digest moved: do not wait for the old plan.
            conn.execute("UPDATE jobs SET next_run_at = ? WHERE name = ?", (_fmt(latest), name))
    return interrupted


def due_jobs(conn: sqlite3.Connection, now: datetime) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM jobs WHERE next_run_at IS NOT NULL AND next_run_at <= ?"
        " AND running_since IS NULL ORDER BY next_run_at",
        (_fmt(now),),
    ).fetchall()
    return [r["name"] for r in rows if r["name"] in JOBS]


def run_job(
    conn: sqlite3.Connection,
    settings: Settings,
    name: str,
    *,
    policy: HealthPolicy | None = None,
    manual: bool = False,
    now: datetime | None = None,
    transport: httpx.BaseTransport | None = None,
) -> dict:
    """Run one job now and record its event. Raises JobBusy if it is already running."""
    if name not in JOBS:
        raise ValueError(f"unknown job {name!r}")
    started = now or datetime.now(UTC)
    conn.execute("INSERT OR IGNORE INTO jobs (name) VALUES (?)", (name,))
    stale = _fmt(started - STALE_AFTER)
    claimed = conn.execute(
        "UPDATE jobs SET running_since = ?, last_started_at = ? WHERE name = ?"
        " AND (running_since IS NULL OR running_since < ?)",
        (_fmt(started), _fmt(started), name, stale),
    ).rowcount
    if not claimed:
        raise JobBusy(name)
    try:
        outcome = _RUNNERS[name](conn, settings, policy or HealthPolicy.from_env(), started,
                                 transport)  # fmt: skip
    except Exception as exc:  # a job must never take the scheduler down
        log.exception("job %s failed", name)
        title = t(settings, "job.failed", job=t(settings, f"name.{name}"))
        outcome = Outcome("error", "error", title, [f"{type(exc).__name__}: {exc}"[:500]])
    if manual and outcome.level == "routine":
        outcome.level = "info"  # someone asked for it and waits for the answer
    finished = datetime.now(UTC)
    planned = next_run(settings, name, finished)
    if outcome.again_in is not None and (planned is None or finished + outcome.again_in < planned):
        planned = finished + outcome.again_in
    conn.execute(
        "UPDATE jobs SET running_since = NULL, last_finished_at = ?, last_status = ?,"
        " last_summary = ?, next_run_at = ? WHERE name = ?",
        (_fmt(finished), outcome.status, outcome.title, _fmt(planned) if planned else None, name),
    )
    data = {"job": name, "status": outcome.status, "manual": manual, **outcome.data}
    body = "\n".join(outcome.lines)
    if outcome.quiet and not manual:
        return {"id": None, "at": _fmt(finished), "kind": f"job.{name}", "level": outcome.level,
                "title": outcome.title, "body": body, "data": data, "pushed": None}  # fmt: skip
    return record_event(conn, settings, f"job.{name}", outcome.level, outcome.title, body, data,
                        transport=transport, requested=manual)  # fmt: skip


# ---------- Jobs ----------


def _refresh(conn, settings, table: str, importer: Callable, describe: Callable,
             key: str) -> Outcome:  # fmt: skip
    rows = conn.execute(
        f"SELECT id, name FROM {table} WHERE url IS NOT NULL AND enabled = 1 ORDER BY name"
    ).fetchall()
    if not rows:
        return Outcome("ok", "routine", t(settings, f"{key}.none"))
    ok, notable, problems, lines = 0, False, 0, []
    for row in rows:
        try:
            report = importer(conn, settings, row["id"])
        except (FetchError, SourceError, sqlite3.DatabaseError) as exc:
            # One source failing (even in the database) must not stop the others.
            problems += 1
            lines.append(f"{row['name']}: {t(settings, 'error', error=exc)}")
            continue
        if report is None or report.status == "unchanged":
            ok += 1
            lines.append(f"{row['name']}: {t(settings, 'unchanged')}")
        elif report.status == "rejected":
            problems += 1
            lines.append(f"{row['name']}: {t(settings, 'rejected', reason=report.reason)}")
        else:
            ok += 1
            text, changed = describe(settings, report)
            notable = notable or changed
            lines.append(f"{row['name']}: {text}")
    status = "ok" if not problems else ("error" if not ok else "warning")
    level = {"ok": "info" if notable else "routine", "warning": "warning", "error": "error"}[status]
    return Outcome(status, level, t(settings, key, ok=ok, total=len(rows)), lines,
                   {"ok": ok, "total": len(rows), "problems": problems})  # fmt: skip


def _describe_playlist(settings, r) -> tuple[str, bool]:
    text = t(settings, "src.changes", channels=r.channels_created, streams=r.streams_created,
             absent=r.streams_absent)  # fmt: skip
    return text, bool(r.channels_created or r.streams_absent)


def _describe_guide(settings, r) -> tuple[str, bool]:
    text = t(settings, "epg.changes", programmes=r.programmes, created=r.airings_created,
             changed=r.airings_changed)  # fmt: skip
    return text, False  # a new day of guide is routine


def _rebuild_lists(conn, settings, data: dict, guide_max_age: timedelta = GUIDE_MAX_AGE) -> str:
    """Every finished part (a playlist, a guide, a batch of checks) is merged into the
    catalog by its importer; the lists are rebuilt right after, so they grow as the
    work goes instead of waiting for a full pass. A list's guide file is rewritten
    after a guide import, and otherwise only now and then (see mih.lists.write_list)."""
    try:
        built = build_presets(conn, settings.lists_dir, load_presets(settings.lists_file),
                              public_url=settings.public_url, token=settings.api_token,
                              guide_max_age=guide_max_age)  # fmt: skip
    except (ValueError, OSError) as exc:
        return t(settings, "error", error=exc)
    data["lists"] = {r.name: r.channels for r in built}
    data["lists_with_guide"] = {r.name: r.with_guide for r in built}
    _refresh_report(conn, settings, timedelta(0) if guide_max_age == timedelta(0) else REPORT_EVERY)
    return t(settings, "health.lists",
             lists=", ".join(f"{r.name} {r.channels} ({r.with_guide})" for r in built))  # fmt: skip


def _refresh_report(conn, settings, every: timedelta) -> None:
    """The «Покрытие» tab's numbers (mih.report): after a guide import at once, after
    other work at most every REPORT_EVERY. A failure here never fails the job."""
    try:
        write_report(conn, report_path(settings.data_dir), every=every)
    except (sqlite3.Error, OSError, ValueError, KeyError):
        log.exception("coverage report failed")


def _job_sources(conn, settings, policy, now, transport) -> Outcome:
    out = _refresh(conn, settings, "sources", import_from_url, _describe_playlist, "job.sources")
    out.lines.append(_rebuild_lists(conn, settings, out.data))
    return out


def _job_epg(conn, settings, policy, now, transport) -> Outcome:
    out = _refresh(conn, settings, "epg_sources", import_epg_from_url, _describe_guide, "job.epg")
    if settings.epg_auto:
        _guide_autopilot(conn, settings, now, out)
    out.lines.append(_rebuild_lists(conn, settings, out.data, guide_max_age=timedelta(0)))
    return out


def _guide_autopilot(conn, settings, now, out: Outcome) -> None:
    """Measure how many working channels have a guide; try guides for those without."""
    cov = working_coverage(conn)
    auto = run_autopilot(conn, settings, cov, now)
    if auto.dropped:  # what a dropped guide covered is gone
        cov = working_coverage(conn)
    out.data["coverage"] = {"working": cov.working, "with_guide": cov.with_guide}
    out.data["autopilot"] = auto.to_dict()
    out.lines.append(t(settings, "epg.coverage", with_guide=cov.with_guide, working=cov.working))
    for key in ("added", "kept", "dropped"):
        items = getattr(auto, key)
        if items:
            out.lines.append(t(settings, f"epg.auto.{key}", items=", ".join(items)))
    if auto.added:
        # The new guides are fetched by the next run, which also judges them.
        out.again_in = AUTOPILOT_AGAIN
    if auto.kept:
        out.level = "info" if out.level == "routine" else out.level


def _job_health(conn, settings, policy, now, transport) -> Outcome:
    report = run_checks(conn, settings, policy, now=now, transport=transport)
    data = report.stats()
    data.pop("run_id", None)
    if report.status == "vantage_offline":
        return Outcome("warning", "warning", t(settings, "job.health.offline"), data=data)
    lists_line = _rebuild_lists(conn, settings, data)
    if not report.checked:
        return Outcome("ok", "routine", t(settings, "job.health.idle"), data=data)
    down = sum(n for k, n in report.transitions.items() if k.split("->")[1] in _DOWN
               and k.split("->")[0] not in _DOWN)  # fmt: skip
    up = sum(n for k, n in report.transitions.items() if k.split("->")[0] in _DOWN
             and k.split("->")[1] not in _DOWN)  # fmt: skip
    lines = [t(settings, "health.failed", failed=report.failed)]
    if down:
        lines.append(t(settings, "health.down", n=down))
    if up:
        lines.append(t(settings, "health.up", n=up))
    lines.append(lists_line)
    return Outcome("ok", "routine", t(settings, "job.health", ok=report.ok, checked=report.checked),
                   lines, data)  # fmt: skip


def _job_alerts(conn, settings, policy, now, transport) -> Outcome:
    stats = run_alerts(conn, settings, now, transport=transport)
    titles = stats.pop("titles")
    if not stats["rules"]:
        return Outcome("ok", "routine", t(settings, "job.alerts.none"), data=stats, quiet=True)
    return Outcome("ok", "routine", t(settings, "job.alerts", sent=stats["sent"]), titles, stats,
                   quiet=not stats["sent"])  # fmt: skip


def _job_discover(conn, settings, policy, now, transport) -> Outcome:
    plan = load_plan(settings.discovery_file)
    found = harvest(conn, settings, plan, now, transport=transport)
    report = scout(conn, settings, plan, policy, now, transport=transport)
    data = {"found": found.total, "found_by": found.found, "api_calls": found.api_calls,
            **report.stats(), "totals": summary(conn)}  # fmt: skip
    if report.offline:
        return Outcome("warning", "warning", t(settings, "job.discover.offline"), data=data)
    lines = [t(settings, "discover.found", n=found.total, repos=found.repos_scanned,
               pages=found.pages_read)]  # fmt: skip
    if report.scouted:
        statuses = ", ".join(f"{k} {v}" for k, v in sorted(report.by_status.items()))
        lines.append(t(settings, "discover.scouted", n=report.scouted, statuses=statuses))
    lines += [t(settings, "discover.live", url=url, ok=ok, sampled=sampled, new=new)
              for url, ok, sampled, new in report.live]  # fmt: skip
    lines += found.notes[:3]
    title = t(settings, "job.discover", found=found.total, live=len(report.live))
    return Outcome("ok", "routine", title, lines, data,
                   quiet=not (found.total or report.live))  # fmt: skip


def _job_pack(conn, settings, policy, now, transport) -> Outcome:
    report = pack(conn, settings, load_plan(settings.discovery_file), policy, now,
                  transport=transport)  # fmt: skip
    data = report.stats()
    if report.capped:
        title = t(settings, "job.pack.capped", n=settings.discovery_max_streams)
        return Outcome("ok", "routine", title, data=data, quiet=True)
    if report.offline:
        return Outcome("warning", "warning", t(settings, "job.discover.offline"), data=data)
    if report.playlist is None:
        return Outcome("ok", "routine", t(settings, "job.pack.idle"), data=data, quiet=True)
    if not report.packed:
        title = t(settings, "job.pack.progress", checked=report.checked,
                  remaining=report.remaining)  # fmt: skip
        return Outcome("ok", "routine", title, [report.playlist], data,
                       quiet=bool(report.remaining))  # fmt: skip
    title = t(settings, "job.pack", channels=report.channels_created,
              streams=report.streams_created)  # fmt: skip
    lines = [report.playlist]
    if report.remaining:
        lines.append(t(settings, "job.pack.progress", checked=report.checked,
                       remaining=report.remaining))  # fmt: skip
    lines.append(_rebuild_lists(conn, settings, data))
    return Outcome("ok", "routine", title, lines, data)


def _job_drive(conn, settings, policy, now, transport) -> Outcome:
    try:
        report = sync_drive(conn, settings)
    except DriveError as exc:
        return Outcome("error", "error", t(settings, "job.drive.failed"), [str(exc)[:500]])
    data = {"drive": report.status, "channels": report.channels}
    if report.status == "unchanged":
        return Outcome("ok", "routine", t(settings, "job.drive.unchanged"), data=data, quiet=True)
    return Outcome("ok", "routine", t(settings, "job.drive", channels=report.channels),
                   [", ".join(report.files)], data)  # fmt: skip


def _job_digest(conn, settings, policy, now, transport) -> Outcome:
    since = _fmt(now - timedelta(days=1))
    lines = []
    streams = conn.execute(
        "SELECT COUNT(*) AS n, SUM(status IN ('active', 'new')) AS active FROM streams"
        " WHERE status != 'archived'"
    ).fetchone()
    channels = conn.execute("SELECT COUNT(*) FROM channels").fetchone()[0]
    lines.append(t(settings, "digest.catalog", channels=channels, active=streams["active"] or 0,
                   streams=streams["n"]))  # fmt: skip
    runs = conn.execute(
        "SELECT level, title FROM events WHERE kind LIKE 'job.%' AND kind != 'job.digest'"
        " AND at >= ? ORDER BY at", (since,),
    ).fetchall()  # fmt: skip
    problems = [r["title"] for r in runs if r["level"] in ("warning", "error")]
    lines.append(t(settings, "digest.day", runs=len(runs), problems=len(problems)))
    checked = failed = 0
    for r in conn.execute("SELECT stats FROM health_runs WHERE started_at >= ?", (since,)):
        stats = json.loads(r["stats"] or "{}")
        checked += stats.get("checked", 0)
        failed += stats.get("failed", 0)
    if checked:
        lines.append(t(settings, "digest.health", checked=checked, failed=failed))
    found = summary(conn)
    if found["found"]:
        st = found["by_status"]
        lines.append(t(settings, "digest.discovery", found=found["found"],
                       waiting=st.get("new", 0), live=st.get("live", 0) + st.get("packed", 0),
                       channels=found["auto_channels"], working=found["auto_working"]))  # fmt: skip
    if problems:
        lines.append(t(settings, "digest.problems"))
        lines += [f"• {p}" for p in dict.fromkeys(problems)][:10]
    ahead = find_matches(conn, now, now + timedelta(days=1), now, load_rules(conn))
    if ahead:
        lines.append(t(settings, "digest.alerts"))
        lines += [alert_line(settings, m, ZoneInfo(settings.timezone)) for m in ahead[:10]]
    plan = [p for p in plan_by_job(conn, settings, now) if p["job"] != "digest"]
    if plan:
        tz = ZoneInfo(settings.timezone)
        lines.append(t(settings, "digest.plan"))
        for p in plan:
            name = t(settings, "name." + p["job"])
            if p["every_minutes"] and len(p["runs"]) > 4:
                when = t(settings, "every", n=p["every_minutes"])
            else:
                when = ", ".join(f"{_parse(at).astimezone(tz):%H:%M}" for at in p["runs"])
            lines.append(f"• {name}: {when}")
    else:
        key = "digest.noplan" if settings.scheduler_enabled else "digest.scheduler_off"
        lines.append(t(settings, key))
    return Outcome("ok" if not problems else "warning", "info", t(settings, "job.digest"), lines,
                   {"runs": len(runs), "problems": len(problems)})  # fmt: skip


_RUNNERS = {"sources": _job_sources, "epg": _job_epg, "health": _job_health,
            "alerts": _job_alerts, "discover": _job_discover, "pack": _job_pack,
            "drive": _job_drive, "digest": _job_digest}  # fmt: skip


# ---------- Status ----------


def planned_runs(conn: sqlite3.Connection, settings: Settings, *, until: datetime,
                 now: datetime | None = None) -> list[dict]:  # fmt: skip
    """Upcoming runs up to ``until``, repeating interval jobs, soonest first. A run that
    is overdue is shown at ``now``: it starts on the next tick."""
    if not settings.scheduler_enabled:
        return []
    out = []
    for row in conn.execute("SELECT name, next_run_at FROM jobs WHERE next_run_at IS NOT NULL"):
        if row["name"] not in JOBS:
            continue
        at = _parse(row["next_run_at"])
        if now is not None and at < now:
            at = now
        runs = 0
        # Capped per job: a job every 5 minutes must not crowd the others out of the plan.
        while at <= until and runs < 300:
            out.append({"job": row["name"], "at": _fmt(at)})
            runs += 1
            nxt = next_run(settings, row["name"], at)
            if nxt is None or nxt <= at:
                break
            at = nxt
    return sorted(out, key=lambda p: p["at"])


def plan_by_job(conn: sqlite3.Connection, settings: Settings, now: datetime) -> list[dict]:
    """The next 24 hours per job: its runs and, for interval jobs, the interval."""
    runs = planned_runs(conn, settings, until=now + timedelta(days=1), now=now)
    out = []
    for name in JOBS:
        mine = [p["at"] for p in runs if p["job"] == name]
        if mine:
            every = _every(settings, name) if name != "digest" else None
            minutes = int(every.total_seconds() // 60) if every else None
            out.append({"job": name, "next_at": mine[0], "runs": mine, "every_minutes": minutes})
    return sorted(out, key=lambda p: p["next_at"])


def status_snapshot(conn: sqlite3.Connection, settings: Settings, *,
                    started_at: str | None = None) -> dict:  # fmt: skip
    """What the service is doing, what it did and what it will do: the status screen."""
    now = datetime.now(UTC)
    jobs = []
    for row in conn.execute("SELECT * FROM jobs"):
        if row["name"] not in JOBS:
            continue
        jobs.append(
            {
                "name": row["name"],
                "running_since": row["running_since"],
                "next_run_at": row["next_run_at"] if settings.scheduler_enabled else None,
                "last_started_at": row["last_started_at"],
                "last_finished_at": row["last_finished_at"],
                "last_status": row["last_status"],
                "last_summary": row["last_summary"],
            }
        )
    jobs.sort(key=lambda j: JOBS.index(j["name"]))
    streams = {r["status"]: r["n"] for r in conn.execute(
        "SELECT status, COUNT(*) AS n FROM streams GROUP BY status")}  # fmt: skip
    return {
        "version": __version__,
        "now": _fmt(now),
        "started_at": started_at,
        "scheduler": settings.scheduler_enabled,
        "notifications": {
            "channel": "ntfy" if settings.ntfy_url else None,
            "level": settings.notify_level,
            "lang": settings.lang,
        },  # fmt: skip
        "running": [
            {"job": j["name"], "since": j["running_since"]} for j in jobs if j["running_since"]
        ],  # fmt: skip
        "jobs": jobs,
        "plan": plan_by_job(conn, settings, now),
        "catalog": {
            "channels": conn.execute("SELECT COUNT(*) FROM channels").fetchone()[0],
            "streams": streams,
        },  # fmt: skip
        "discovery": summary(conn),
        "events": list_events(conn, limit=30),
    }


# ---------- Background thread ----------


class Scheduler:
    """Runs due jobs one at a time in a daemon thread, with its own database connection."""

    def __init__(self, settings: Settings, policy: HealthPolicy | None = None,
                 tick_s: float = 30.0):  # fmt: skip
        self.settings, self.policy, self.tick_s = settings, policy, tick_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        conn = db.connect(self.settings.db_path)
        try:
            for name in sync_jobs(conn, self.settings):
                title = t(self.settings, "job.interrupted", job=t(self.settings, f"name.{name}"))
                record_event(conn, self.settings, "job.interrupted", "warning", title,
                             data={"job": name})  # fmt: skip
        finally:
            conn.close()
        self._thread = threading.Thread(target=self._loop, name="mih-scheduler", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                log.exception("scheduler tick failed")
            self._stop.wait(self.tick_s)

    def tick(self, now: datetime | None = None) -> list[str]:
        conn = db.connect(self.settings.db_path)
        ran = []
        try:
            for name in due_jobs(conn, now or datetime.now(UTC)):
                if self._stop.is_set():
                    break
                try:
                    run_job(conn, self.settings, name, policy=self.policy)
                    ran.append(name)
                except JobBusy:
                    pass  # started on request a moment ago
        finally:
            conn.close()
        return ran


def job_process(*args: str) -> subprocess.Popen:
    """``mih <args>`` in a Python of its own, at a lower priority than the web server:
    a stream check or a guide import never makes the web UI wait, even on one CPU."""
    cmd = [sys.executable, "-m", "mih.cli", *args]
    nice = shutil.which("nice")
    return subprocess.Popen([nice, "-n", "10", *cmd] if nice else cmd)


@contextmanager
def background(settings: Settings, policy: HealthPolicy | None = None) -> Iterator[None]:
    """The jobs of ``mih serve`` while the server runs, and its start and stop events."""
    scheduler = child = None
    if settings.scheduler_enabled and settings.scheduler_mode == "thread":
        scheduler = Scheduler(settings, policy)
    service_event(settings, "service.started")
    if scheduler:
        scheduler.start()
    elif settings.scheduler_enabled:
        child = job_process("scheduler")
    try:
        yield
    finally:
        if scheduler:
            scheduler.stop()
        if child:
            child.terminate()
            try:
                child.wait(15)
            except subprocess.TimeoutExpired:
                child.kill()
        service_event(settings, "service.stopped")


def service_event(settings: Settings, kind: str) -> None:
    conn = db.connect(settings.db_path)
    try:
        record_event(conn, settings, kind, "info", t(settings, kind, version=__version__),
                     data={"at": utc_now()})  # fmt: skip
    finally:
        conn.close()
