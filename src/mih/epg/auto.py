"""Guide autopilot: find guides for working channels that have none (MIH_EPG_AUTO).

After each guide refresh it looks at the working channels without a guide, by country
(mih.epg.coverage). For a country with at least MIN_UNCOVERED of them it registers the
next public guide known for that country (mih.epg.known) on trial; the refresh imports
it soon after. The next run judges it: a guide that gives at least one working channel
a guide no other guide gives is kept, any other is deleted with what it stored. One
guide per country is on trial at a time, and a dropped guide is tried again only after
RETRY_AFTER: the public guides come and go.

A guide on trial is only matched against the catalog: the channels it lists that no
playlist carries are not added to the catalog.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from mih.epg.coverage import NO_COUNTRY, Coverage
from mih.epg.known import KnownGuide, guides_for
from mih.epg.service import delete_epg_source
from mih.ids import new_id

# Working channels of a country without a guide before a guide is tried for it.
MIN_UNCOVERED = 3
# New guides per run; each one is fetched by the next refresh.
PER_RUN = 3
# A guide that was fetched for this long without a usable import is broken.
GRACE = timedelta(days=2)
RETRY_AFTER = timedelta(days=30)
# Below the guides a playlist names (50) and those added by hand (100 is the default).
AUTO_PRIORITY = 120


@dataclass
class AutoReport:
    added: list[str] = field(default_factory=list)  # "<guide> (<country>)"
    kept: list[str] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.added or self.kept or self.dropped)

    def to_dict(self) -> dict:
        return {"added": self.added, "kept": self.kept, "dropped": self.dropped}


def _fmt(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def run_autopilot(conn: sqlite3.Connection, settings, cov: Coverage, now: datetime) -> AutoReport:
    report = AutoReport()
    _judge(conn, settings, cov, now, report)
    _add(conn, settings, cov, now, report)
    return report


def _judge(conn, settings, cov: Coverage, now: datetime, report: AutoReport) -> None:
    for row in conn.execute(
        "SELECT * FROM epg_auto WHERE verdict = 'trial' ORDER BY added_at"
    ).fetchall():
        source_id = row["epg_source_id"]
        label = f"{row['guide']} ({row['country']})"
        if source_id is None:  # deleted by hand
            _verdict(conn, row["url"], "useless", 0, now)
            continue
        last = conn.execute(
            "SELECT status FROM epg_snapshots WHERE epg_source_id = ? AND status != 'unchanged'"
            " ORDER BY fetched_at DESC, rowid DESC LIMIT 1",
            (source_id,),
        ).fetchone()
        if last is None or last["status"] != "applied":
            # Not fetched yet, the host did not answer, or the file was refused.
            added = datetime.fromisoformat(row["added_at"].replace("Z", "+00:00"))
            if (last is not None and last["status"] == "rejected") or now - added > GRACE:
                delete_epg_source(conn, settings, source_id)
                _verdict(conn, row["url"], "broken", 0, now)
                report.dropped.append(label)
            continue
        gain = sum(1 for guides in cov.sources.values() if guides == {source_id})
        if gain:
            _verdict(conn, row["url"], "kept", gain, now)
            report.kept.append(f"{label} +{gain}")
        else:
            delete_epg_source(conn, settings, source_id)
            _verdict(conn, row["url"], "useless", 0, now)
            report.dropped.append(label)


def _verdict(conn, url: str, verdict: str, gain: int, now: datetime) -> None:
    conn.execute(
        "UPDATE epg_auto SET verdict = ?, gain = ?, judged_at = ?,"
        " epg_source_id = CASE WHEN ? = 'kept' THEN epg_source_id END WHERE url = ?",
        (verdict, gain, _fmt(now), verdict, url),
    )


def _add(conn, settings, cov: Coverage, now: datetime, report: AutoReport) -> None:
    tried = {r["url"]: r for r in conn.execute("SELECT * FROM epg_auto")}
    on_trial = {r["country"] for r in tried.values() if r["verdict"] == "trial"}
    registered = {r[0] for r in conn.execute("SELECT url FROM epg_sources WHERE url IS NOT NULL")}
    retry_before = _fmt(now - RETRY_AFTER)
    countries = sorted(cov.by_country.items(), key=lambda kv: (-kv[1].uncovered, kv[0]))
    for cc, c in countries:
        if len(report.added) >= PER_RUN:
            break
        if cc == NO_COUNTRY or c.uncovered < MIN_UNCOVERED or cc in on_trial:
            continue
        for g in guides_for(cc):
            if g.url in registered:
                continue
            row = tried.get(g.url)
            if row is not None and (row["verdict"] in ("trial", "kept")
                                    or (row["judged_at"] or "") > retry_before):  # fmt: skip
                continue
            _register(conn, settings, g, cc, now)
            registered.add(g.url)
            report.added.append(f"{g.name} ({cc})")
            break


def _register(conn, settings, g: KnownGuide, country: str, now: datetime) -> None:
    name = f"auto: {g.name}"
    if conn.execute("SELECT 1 FROM epg_sources WHERE name = ?", (name,)).fetchone():
        name += f" {new_id()[:6]}"
    source_id = new_id()
    conn.execute(
        "INSERT INTO epg_sources (id, name, url, priority, auto, created_at)"
        " VALUES (?, ?, ?, ?, 1, ?)",
        (source_id, name, g.url, AUTO_PRIORITY, _fmt(now)),
    )
    conn.execute(
        "INSERT INTO epg_auto (url, guide, country, epg_source_id, verdict, gain, added_at)"
        " VALUES (?, ?, ?, ?, 'trial', 0, ?) ON CONFLICT(url) DO UPDATE SET"
        " guide = excluded.guide, country = excluded.country,"
        " epg_source_id = excluded.epg_source_id, verdict = 'trial', gain = 0,"
        " added_at = excluded.added_at, judged_at = NULL",
        (g.url, g.id, country, source_id, _fmt(now)),
    )
