"""The coverage report behind the web UI's «Покрытие» tab: how much of the catalog
plays, how much of it has a guide, by country and by source, and what the playlist
scout and packer brought in.

Building it walks the whole catalog (as a list build does), which takes seconds on a
big one. So the background jobs write it to ``<data>/report.json`` after their work,
at most every ``REPORT_EVERY``, and the API only reads the file. The file also keeps
one point a day, so the tab can show how the catalog grows.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

from mih.discovery.scout import summary
from mih.epg.coverage import countries_of, guide_sources
from mih.export import build_catalog
from mih.lists import ListFilter, build_list, enrich

# The jobs that change the catalog run every few minutes; the report is that fresh.
REPORT_EVERY = timedelta(minutes=30)
# Days of daily points kept (general history is kept 3 months).
HISTORY_DAYS = 92
# Days of "new streams by day" shown.
NEW_STREAMS_DAYS = 30
_STAGES = ("all", "streams", "working", "guide")


def report_path(data_dir: Path) -> Path:
    return data_dir / "report.json"


def build_report(conn: sqlite3.Connection, *, now: datetime | None = None,
                 catalog: list[dict] | None = None) -> dict:  # fmt: skip
    """Counts only: no URLs, no channel lists."""
    now = now or datetime.now(UTC)
    catalog = build_catalog(conn) if catalog is None else catalog
    for ch in catalog:
        enrich(ch)
    doc = build_list(conn, ListFilter(exclude_categories=()), now=now, catalog=catalog)
    working = {x["channel"]["id"] for x in doc["_included"]}
    guides = guide_sources(conn)

    funnel: Counter = Counter()
    by_country: dict[str, Counter] = defaultdict(Counter)
    for ch in catalog:
        steps = {"all": True, "streams": any(f["streams"] for f in ch["feeds"]),
                 "working": ch["id"] in working}  # fmt: skip
        steps["guide"] = steps["working"] and bool(guides.get(ch["id"]))
        for key in _STAGES:
            funnel[key] += steps[key]
            for cc in countries_of(ch):
                by_country[cc][key] += steps[key]

    countries = [{"country": cc, **{k: c[k] for k in _STAGES}} for cc, c in by_country.items()]
    countries.sort(key=lambda r: (-r["all"], r["country"]))
    disc = summary(conn)
    return {
        "generated_at": now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "funnel": {k: funnel[k] for k in _STAGES},
        "by_country": countries,
        "not_working": doc["excluded_by_reason"],
        "sources": _sources(conn),
        "new_streams": _new_streams(conn, now),
        "discovery": disc,
        "guides": _guides(conn),
    }


def _sources(conn: sqlite3.Connection) -> list[dict]:
    """Streams per playlist; the scout's playlists ("auto: ...") are one row."""
    rows = conn.execute(
        "SELECT src.name, src.name LIKE 'auto:%' AS auto, COUNT(s.id) AS streams,"
        " SUM(s.status = 'active') AS working, COUNT(DISTINCT f.channel_id) AS channels"
        " FROM sources src JOIN streams s ON s.source_id = src.id"
        " JOIN feeds f ON f.id = s.feed_id GROUP BY src.id"
    ).fetchall()
    out, auto = [], {"name": None, "auto": True, "playlists": 0, "streams": 0, "working": 0,
                     "channels": 0}  # fmt: skip
    for r in rows:
        if r["auto"]:
            auto["playlists"] += 1
            for k in ("streams", "working", "channels"):
                auto[k] += r[k] or 0
        else:
            out.append({"name": r["name"], "auto": False, "streams": r["streams"],
                        "working": r["working"] or 0, "channels": r["channels"]})  # fmt: skip
    if auto["playlists"]:
        out.append(auto)
    out.sort(key=lambda r: -r["streams"])
    return out


def _new_streams(conn: sqlite3.Connection, now: datetime) -> list[dict]:
    since = (now - timedelta(days=NEW_STREAMS_DAYS)).strftime("%Y-%m-%d")
    return [dict(r) for r in conn.execute(
        "SELECT substr(first_seen_at, 1, 10) AS day, COUNT(*) AS streams,"
        " SUM(status = 'active') AS working FROM streams"
        " WHERE substr(first_seen_at, 1, 10) >= ? GROUP BY 1 ORDER BY 1", (since,))]  # fmt: skip


def _guides(conn: sqlite3.Connection) -> dict:
    enabled = conn.execute("SELECT COUNT(*) FROM epg_sources WHERE enabled = 1").fetchone()[0]
    verdicts = dict(
        conn.execute("SELECT verdict, COUNT(*) FROM epg_auto GROUP BY verdict").fetchall()
    )
    return {"sources": enabled, "autopilot": verdicts}


def _point(report: dict) -> dict:
    return {"day": report["generated_at"][:10], **report["funnel"],
            "auto_channels": report["discovery"].get("auto_channels", 0)}  # fmt: skip


def read_report(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def write_report(
    conn: sqlite3.Connection,
    path: Path,
    *,
    now: datetime | None = None,
    every: timedelta = REPORT_EVERY,
    catalog: list[dict] | None = None,
) -> dict | None:
    """Rebuild the report unless the file is younger than ``every``. Returns the new
    report, or None when the file was fresh enough."""
    now = now or datetime.now(UTC)
    try:
        age = now.timestamp() - path.stat().st_mtime
    except OSError:
        age = None
    # A clock a few seconds behind the file system still counts as fresh.
    if age is not None and -60 < age < every.total_seconds():
        return None
    report = build_report(conn, now=now, catalog=catalog)
    # One point a day: the latest numbers of the day replace the earlier ones.
    old = (read_report(path) or {}).get("history", [])
    point = _point(report)
    cutoff = (now - timedelta(days=HISTORY_DAYS)).strftime("%Y-%m-%d")
    report["history"] = [
        p for p in old if p.get("day") != point["day"] and p.get("day", "") >= cutoff
    ]
    report["history"].append(point)
    report["history"].sort(key=lambda p: p["day"])
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(report, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    os.replace(tmp, path)
    return report
