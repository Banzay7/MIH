"""Attach a playlist entry to a Channel and Feed, creating them when needed (spec 15.1)."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from mih.identity.normalize import NameParts
from mih.ids import new_id


@dataclass(frozen=True)
class Resolution:
    channel_id: str
    feed_id: str
    rule: str
    decision: str  # 'auto' or 'needs_review'
    created_channel: bool


def resolve(
    conn: sqlite3.Connection,
    *,
    parts: NameParts,
    tvg_id: str | None,
    country: str | None,
    logo_url: str | None,
    source_id: str,
    now: str,
) -> Resolution:
    channel_id, rule, decision = _find_channel(conn, parts, tvg_id, country)
    created = channel_id is None
    if created:
        channel_id = new_id()
        conn.execute(
            "INSERT INTO channels (id, canonical_name, country, logo_url, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (channel_id, parts.brand_display, country, logo_url, now),
        )
    elif country or logo_url:
        conn.execute(
            "UPDATE channels SET country = COALESCE(country, ?), logo_url = COALESCE(logo_url, ?)"
            " WHERE id = ?",
            (country, logo_url, channel_id),
        )

    _add_alias(conn, channel_id, "name", parts.key, parts.display, source_id)
    if tvg_id:
        _add_alias(conn, channel_id, "tvg-id", tvg_id.casefold(), tvg_id, source_id)

    feed_id = _ensure_feed(conn, channel_id, parts, now)
    return Resolution(channel_id, feed_id, rule, decision, created)


def _find_channel(
    conn: sqlite3.Connection, parts: NameParts, tvg_id: str | None, country: str | None
) -> tuple[str | None, str, str]:
    if tvg_id:
        rows = conn.execute(
            "SELECT a.channel_id, c.country FROM channel_aliases a"
            " JOIN channels c ON c.id = a.channel_id"
            " WHERE a.kind = 'tvg-id' AND a.value = ?",
            (tvg_id.casefold(),),
        ).fetchall()
        rows = [r for r in rows if _country_compatible(r["country"], country)]
        if len(rows) == 1:
            return rows[0]["channel_id"], "tvg-id", "auto"

    rows = conn.execute(
        "SELECT DISTINCT a.channel_id, c.country FROM channel_aliases a"
        " JOIN channels c ON c.id = a.channel_id"
        " WHERE a.kind = 'name' AND a.value = ?",
        (parts.key,),
    ).fetchall()
    rows = [r for r in rows if _country_compatible(r["country"], country)]
    if len(rows) == 1:
        return rows[0]["channel_id"], "name+country", "auto"
    if len(rows) > 1:
        # Several channels share this name and nothing tells them apart: keep the entry
        # separate and ask a person instead of guessing (spec 3.1).
        return None, "name-ambiguous", "needs_review"
    return None, "new", "auto"


def _country_compatible(a: str | None, b: str | None) -> bool:
    return not a or not b or a.casefold() == b.casefold()


def _add_alias(
    conn: sqlite3.Connection, channel_id: str, kind: str, value: str, display: str, source_id: str
) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO channel_aliases (channel_id, kind, value, display, source_id)"
        " VALUES (?, ?, ?, ?, ?)",
        (channel_id, kind, value, display, source_id),
    )


def _ensure_feed(conn: sqlite3.Connection, channel_id: str, parts: NameParts, now: str) -> str:
    row = conn.execute(
        "SELECT id FROM feeds WHERE channel_id = ? AND region = ? AND offset_minutes = ?",
        (channel_id, parts.region, parts.offset_minutes),
    ).fetchone()
    if row:
        return row["id"]
    feed_id = new_id()
    conn.execute(
        "INSERT INTO feeds (id, channel_id, region, offset_minutes, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (feed_id, channel_id, parts.region, parts.offset_minutes, now),
    )
    return feed_id


def record_decision(
    conn: sqlite3.Connection,
    *,
    resolution: Resolution,
    record_id: str,
    parts: NameParts,
    tvg_id: str | None,
    now: str,
) -> None:
    evidence = {"key": parts.key, "tvg_id": tvg_id, "region": parts.region,
                "offset_minutes": parts.offset_minutes}  # fmt: skip
    conn.execute(
        "INSERT INTO match_decisions"
        " (id, target_kind, target_id, record_id, rule, score, evidence, decision, created_at)"
        " VALUES (?, 'channel', ?, ?, ?, ?, ?, ?, ?)",
        (
            new_id(),
            resolution.channel_id,
            record_id,
            resolution.rule,
            1.0 if resolution.decision == "auto" else 0.5,
            json.dumps(evidence, ensure_ascii=False),
            resolution.decision,
            now,
        ),
    )
