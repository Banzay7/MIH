"""Attach guide channels to catalog feeds (spec 19, mapping order).

Order: a person's decision → a mapping established earlier → the guide id seen as
tvg-id on a feed → the display name with the same region and timeshift. Several
candidates mean a person chooses; one similar-looking name in a different version
is never enough.
"""

from __future__ import annotations

import json
import sqlite3

from mih.identity.normalize import split_name

KEEP = ("approved", "rejected")


def refresh_mappings(
    conn: sqlite3.Connection, now: str, *, epg_source_id: str | None = None
) -> dict[str, int]:
    """Map every guide channel that has no settled mapping. Returns decision counts."""
    sql = (
        "SELECT ec.id, ec.xmltv_id, ec.display_names, m.feed_id, m.decision"
        " FROM epg_channels ec LEFT JOIN epg_mappings m ON m.epg_channel_id = ec.id"
    )
    params: tuple = ()
    if epg_source_id:
        sql += " WHERE ec.epg_source_id = ?"
        params = (epg_source_id,)
    counts = {"auto": 0, "needs_review": 0, "approved": 0, "rejected": 0, "unmapped": 0}
    for row in conn.execute(sql, params).fetchall():
        decision = row["decision"]
        if decision in KEEP or (decision == "auto" and row["feed_id"]):
            counts[decision] += 1
            continue
        feeds, rule = _candidates(conn, row["xmltv_id"], json.loads(row["display_names"]))
        if not feeds:
            conn.execute("DELETE FROM epg_mappings WHERE epg_channel_id = ?", (row["id"],))
            counts["unmapped"] += 1
            continue
        if len(feeds) == 1:
            decision, feed_id, score = "auto", feeds[0], 1.0
        else:
            decision, feed_id, score = "needs_review", None, 0.5
        _store(conn, row["id"], feed_id, rule, score, {"candidates": feeds}, decision, now)
        counts[decision] += 1
    return counts


def set_manual(
    conn: sqlite3.Connection, epg_channel_id: str, feed_id: str | None, now: str
) -> None:
    decision = "approved" if feed_id else "rejected"
    _store(conn, epg_channel_id, feed_id, "manual", 1.0, None, decision, now, decided_by="user")


def _candidates(
    conn: sqlite3.Connection, xmltv_id: str, names: list[list[str | None]]
) -> tuple[list[str], str]:
    rows = conn.execute(
        "SELECT DISTINCT f.id, f.offset_minutes FROM feed_aliases fa JOIN feeds f"
        " ON f.id = fa.feed_id WHERE fa.kind = 'tvg-id' AND fa.value = ?",
        (xmltv_id.casefold(),),
    ).fetchall()
    if rows:
        # Playlists often reuse the base id on their +N feeds. The guide lists the
        # base schedule, and timeshift feeds borrow it shifted (see guide.feed_guides).
        base = [r["id"] for r in rows if r["offset_minutes"] == 0]
        return sorted(base or [r["id"] for r in rows]), "xmltv-id"

    found: set[str] = set()
    for _, name in names:
        parts = split_name(name)
        for r in conn.execute(
            "SELECT DISTINCT f.id FROM channel_aliases a"
            " JOIN feeds f ON f.channel_id = a.channel_id"
            " WHERE a.kind = 'name' AND a.value = ? AND f.region = ? AND f.offset_minutes = ?",
            (parts.key, parts.region, parts.offset_minutes),
        ):
            found.add(r["id"])
    return sorted(found), "name"


def _store(
    conn: sqlite3.Connection,
    epg_channel_id: str,
    feed_id: str | None,
    rule: str,
    score: float,
    evidence: dict | None,
    decision: str,
    now: str,
    *,
    decided_by: str = "system",
) -> None:
    conn.execute(
        "INSERT INTO epg_mappings (epg_channel_id, feed_id, rule, score, evidence, decision,"
        " decided_by, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(epg_channel_id) DO UPDATE SET feed_id = excluded.feed_id,"
        " rule = excluded.rule, score = excluded.score, evidence = excluded.evidence,"
        " decision = excluded.decision, decided_by = excluded.decided_by,"
        " updated_at = excluded.updated_at",
        (epg_channel_id, feed_id, rule, score,
         json.dumps(evidence) if evidence else None, decision, decided_by, now),
    )  # fmt: skip
