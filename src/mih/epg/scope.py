"""Which guide channels and programmes are worth storing (MIH_EPG_SCOPE).

A worldwide guide lists thousands of channels; storing all of them makes the database
the size of the guide, not of what the household watches. ``tracked`` keeps the whole
schedule of favourite channels and, on other channels found in the catalog, only the
programmes a profile follows by topic or as a favourite programme.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass

from mih.epg.xmltv import XMLTVProgramme
from mih.identity.normalize import fold

FULL, FILTERED = "full", "filtered"


@dataclass
class Scope:
    # epg_channel_id -> FULL (keep every programme) or FILTERED (only matches).
    # A guide channel missing here gets no schedule stored.
    channels: dict[str, str]
    topics: re.Pattern | None = None
    titles: frozenset[str] = frozenset()

    def keeps(self, p: XMLTVProgramme) -> bool:
        if fold(p.titles[0][1]) in self.titles:
            return True
        if self.topics is None:
            return False
        text = " ".join([t for _, t in p.titles] + [t for _, t in p.sub_titles] + p.categories)
        return bool(self.topics.search(fold(text)))


def build_scope(conn: sqlite3.Connection, mode: str, epg_channel_ids: list[str]) -> Scope:
    if mode == "all":
        return Scope({cid: FULL for cid in epg_channel_ids})

    # needs_review channels count as in the catalog: one of their candidates is.
    in_catalog = {
        r["epg_channel_id"]: r["feed_id"]
        for r in conn.execute(
            "SELECT epg_channel_id, feed_id FROM epg_mappings WHERE decision != 'rejected'"
        )
    }
    mapped = [cid for cid in epg_channel_ids if cid in in_catalog]
    if mode == "mapped":
        return Scope({cid: FULL for cid in mapped})

    fav_channels = {
        r[0] for r in conn.execute("SELECT target_id FROM favorites WHERE target_kind = 'channel'")
    }
    titles = frozenset(
        fold(r[0])
        for r in conn.execute(
            "SELECT p.title FROM favorites f JOIN programmes p ON p.id = f.target_id"
            " WHERE f.target_kind = 'programme'"
        )
    )
    topics = sorted({r[0] for r in conn.execute("SELECT topic FROM tracked_topics")})
    if not (fav_channels or titles or topics):
        return Scope({cid: FULL for cid in mapped})

    feed_channel = dict(conn.execute("SELECT id, channel_id FROM feeds").fetchall())
    channels = {
        cid: FULL if feed_channel.get(in_catalog[cid]) in fav_channels else FILTERED
        for cid in mapped
    }
    pattern = (
        re.compile(r"(?<!\w)(?:" + "|".join(re.escape(t) for t in topics) + ")") if topics else None
    )
    return Scope(channels, pattern, titles)


def topic_key(topic: str) -> str:
    return " ".join(fold(topic).split())
