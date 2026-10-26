"""Attach guide channels to catalog feeds (spec 19, mapping order).

Order: a person's decision → a mapping established earlier → the guide id seen as
tvg-id on a feed → the display name with the same region and timeshift → the catalog
channel the guide channel belongs to, found by its id without the feed part, the
guide channel recorded on it, a name, a directory name or, last, a transliterated
name; the channel's feed is then picked by timeshift and region. Several candidates
mean a person chooses; one similar-looking name in a different version is never
enough.

Playlists and guides name one channel differently: iptv-org writes "NTV (Russia)",
a Russian guide "НТВ". The directory's alternative names and the transliteration
bridge them; "(Russia)" is not a feed region a guide would repeat, so a guide name
without a region also takes a channel's only regional feed.
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field

from mih.identity.normalize import split_name, translit_key, tvg_base

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
    index: _Index | None = None
    for row in conn.execute(sql, params).fetchall():
        decision = row["decision"]
        if decision in KEEP or (decision == "auto" and row["feed_id"]):
            counts[decision] += 1
            continue
        index = index or _Index.build(conn)
        names = json.loads(row["display_names"])
        feeds, rule = _candidates(conn, row["xmltv_id"], names)
        if not feeds:
            feeds, rule = index.by_channel(row["id"], row["xmltv_id"], names)
        if not feeds:
            conn.execute("DELETE FROM epg_mappings WHERE epg_channel_id = ?", (row["id"],))
            counts["unmapped"] += 1
            continue
        if len(feeds) == 1:
            decision, feed_id, score = "auto", feeds[0], _SCORES.get(rule, 1.0)
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


_SCORES = {"tvg-base": 0.95, "channel-epg": 0.9, "channel-name": 0.9,
           "directory-name": 0.8, "translit": 0.7}  # fmt: skip


@dataclass
class _Index:
    """The catalog side of channel-level matching, read once per refresh."""

    feeds: dict[str, list[tuple[str, str, int]]] = field(default_factory=dict)
    tvg: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    epg: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    names: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    translit: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    directory: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))

    @classmethod
    def build(cls, conn: sqlite3.Connection) -> _Index:
        ix = cls(feeds=defaultdict(list))
        for f in conn.execute("SELECT id, channel_id, region, offset_minutes FROM feeds"):
            ix.feeds[f["channel_id"]].append((f["id"], f["region"], f["offset_minutes"]))
        # Only channels with a feed can carry a schedule.
        for a in conn.execute("SELECT channel_id, kind, value FROM channel_aliases"):
            if a["channel_id"] not in ix.feeds:
                continue
            if a["kind"] == "tvg-id":
                ix.tvg[a["value"]].add(a["channel_id"])
            elif a["kind"] == "epg":
                ix.epg[a["value"]].add(a["channel_id"])
            elif a["kind"] == "name":
                ix.names[a["value"]].add(a["channel_id"])
                ix.translit[translit_key(a["value"])].add(a["channel_id"])
        for r in conn.execute(
            "SELECT n.key, c.id FROM directory_names n"
            " JOIN channels c ON c.directory_id = n.directory_id"
        ):
            if r["id"] in ix.feeds:
                ix.directory[r["key"]].add(r["id"])
        return ix

    def by_channel(
        self, epg_channel_id: str, xmltv_id: str, names: list[list[str | None]]
    ) -> tuple[list[str], str]:
        """Feeds of the catalog channel this guide channel belongs to."""
        parts = [split_name(n) for _, n in names if n and n.strip()]
        parts = [p for p in parts if p.key]
        steps = [
            ("tvg-base", lambda: self.tvg.get(tvg_base(xmltv_id), set())),
            ("channel-epg", lambda: self.epg.get(epg_channel_id, set())),
            ("channel-name", lambda: _union(self.names, [p.key for p in parts])),
            ("directory-name", lambda: _union(self.directory, [p.key for p in parts])),
            ("translit", lambda: _union(self.translit, [translit_key(p.key) for p in parts
                                                        if _long_enough(p.key)])),
        ]  # fmt: skip
        # The guide name says which version: "+2" or a region. None given: the base feed.
        offset = parts[0].offset_minutes if parts else 0
        region = parts[0].region if parts else ""
        for rule, find in steps:
            channels = find()
            if not channels:
                continue
            feeds = sorted({f for cid in channels for f in self._pick(cid, offset, region)})
            if feeds:
                return feeds, rule
        return [], "name"

    def _pick(self, channel_id: str, offset: int, region: str) -> list[str]:
        same = [f for f in self.feeds[channel_id] if f[2] == offset]
        if region:
            return [f[0] for f in same if f[1] == region]
        plain = [f[0] for f in same if not f[1]]
        if plain:
            return plain
        # "NTV (Russia)": one regional feed is the channel's only version. Several
        # regional ones: a person chooses.
        return [f[0] for f in same]


def _union(index: dict[str, set[str]], keys: list[str]) -> set[str]:
    out: set[str] = set()
    for k in keys:
        out |= index.get(k, set())
    return out


def _long_enough(key: str) -> bool:
    # "ntv" or "ren" transliterate into too many other channels' names.
    return len(key.replace(" ", "")) >= 4


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
