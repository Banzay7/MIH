"""Read side of the EPG: which guide a feed uses, what is on now, a feed's schedule."""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

AIRING_COLS = (
    "a.id AS airing_id, a.epg_channel_id, a.start_utc, a.stop_utc, a.status, a.version,"
    " p.id AS programme_id, p.title, p.sub_title, p.episode_num, p.categories, p.lang,"
    " p.icon_url, p.rating"
)


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class FeedGuide:
    feed_id: str
    channel_id: str
    epg_channel_id: str
    # Minutes to add to the guide's times: non-zero for a +N feed that has no guide of
    # its own and borrows the base feed's schedule.
    shift_minutes: int
    borrowed_from: str | None


def feed_guides(
    conn: sqlite3.Connection, channel_ids: Iterable[str] | None = None
) -> dict[str, FeedGuide]:
    sql = "SELECT id, channel_id, region, offset_minutes FROM feeds"
    params: list = []
    if channel_ids is not None:
        ids = list(channel_ids)
        sql += f" WHERE channel_id IN ({','.join('?' * len(ids))})"
        params = ids
    feeds = conn.execute(sql, params).fetchall()

    # The first source by priority wins, but a guide with nothing stored for the feed
    # (a guide that stopped listing it, or one out of MIH_EPG_SCOPE) gives way to one
    # that has a schedule.
    direct: dict[str, str] = {}
    for r in conn.execute(
        "SELECT m.feed_id, m.epg_channel_id FROM epg_mappings m"
        " JOIN epg_channels ec ON ec.id = m.epg_channel_id"
        " JOIN epg_sources es ON es.id = ec.epg_source_id"
        " WHERE m.feed_id IS NOT NULL AND m.decision IN ('auto', 'approved') AND es.enabled = 1"
        " ORDER BY NOT EXISTS (SELECT 1 FROM airings a WHERE a.epg_channel_id = ec.id),"
        " es.priority, es.name"
    ):
        direct.setdefault(r["feed_id"], r["epg_channel_id"])

    base = {(f["channel_id"], f["region"]): f["id"] for f in feeds if f["offset_minutes"] == 0}
    out: dict[str, FeedGuide] = {}
    for f in feeds:
        if f["id"] in direct:
            out[f["id"]] = FeedGuide(f["id"], f["channel_id"], direct[f["id"]], 0, None)
            continue
        base_id = base.get((f["channel_id"], f["region"]))
        if f["offset_minutes"] and base_id in direct:
            out[f["id"]] = FeedGuide(f["id"], f["channel_id"], direct[base_id],
                                     f["offset_minutes"], base_id)  # fmt: skip
    return out


def airing_out(row: sqlite3.Row, guide: FeedGuide) -> dict:
    shift = timedelta(minutes=guide.shift_minutes)
    return {
        "airing_id": row["airing_id"],
        "programme_id": row["programme_id"],
        "feed_id": guide.feed_id,
        "channel_id": guide.channel_id,
        "title": row["title"],
        "sub_title": row["sub_title"],
        "episode_num": row["episode_num"],
        "categories": json.loads(row["categories"]),
        "lang": row["lang"],
        "start": _shift(row["start_utc"], shift),
        "stop": _shift(row["stop_utc"], shift),
        "status": row["status"],
        "timeshift_minutes": guide.shift_minutes,
        "borrowed_from_feed_id": guide.borrowed_from,
    }


def now_and_next(
    conn: sqlite3.Connection,
    now: datetime,
    guides: dict[str, FeedGuide],
) -> dict[str, dict]:
    """For each feed with a guide: {"now": airing | None, "next": airing | None}."""
    by_shift: dict[int, list[FeedGuide]] = defaultdict(list)
    for g in guides.values():
        by_shift[g.shift_minutes].append(g)
    out: dict[str, dict] = {}
    for shift, items in by_shift.items():
        t = iso(now - timedelta(minutes=shift))
        epg_ids = sorted({g.epg_channel_id for g in items})
        marks = ",".join("?" * len(epg_ids))
        current = {
            r["epg_channel_id"]: r
            for r in conn.execute(
                f"SELECT {AIRING_COLS} FROM airings a JOIN programmes p ON p.id = a.programme_id"
                f" WHERE a.epg_channel_id IN ({marks}) AND a.status = 'scheduled'"
                " AND a.start_utc <= ? AND a.stop_utc > ?",
                (*epg_ids, t, t),
            )
        }
        # Bare columns with MIN() come from the row holding the minimum (SQLite rule).
        upcoming = {
            r["epg_channel_id"]: r
            for r in conn.execute(
                f"SELECT {AIRING_COLS}, MIN(a.start_utc) FROM airings a"
                " JOIN programmes p ON p.id = a.programme_id"
                f" WHERE a.epg_channel_id IN ({marks}) AND a.status = 'scheduled'"
                " AND a.start_utc > ? GROUP BY a.epg_channel_id",
                (*epg_ids, t),
            )
        }
        for g in items:
            cur, nxt = current.get(g.epg_channel_id), upcoming.get(g.epg_channel_id)
            out[g.feed_id] = {
                "now": airing_out(cur, g) if cur else None,
                "next": airing_out(nxt, g) if nxt else None,
            }
    return out


def schedule(
    conn: sqlite3.Connection, guide: FeedGuide, start: datetime, end: datetime
) -> list[dict]:
    shift = timedelta(minutes=guide.shift_minutes)
    rows = conn.execute(
        f"SELECT {AIRING_COLS} FROM airings a JOIN programmes p ON p.id = a.programme_id"
        " WHERE a.epg_channel_id = ? AND a.status = 'scheduled'"
        " AND a.start_utc < ? AND a.stop_utc > ? ORDER BY a.start_utc",
        (guide.epg_channel_id, iso(end - shift), iso(start - shift)),
    ).fetchall()
    return [airing_out(r, guide) for r in rows]


def _shift(value: str, shift: timedelta) -> str:
    if not shift:
        return value
    return iso(datetime.fromisoformat(value.replace("Z", "+00:00")) + shift)
