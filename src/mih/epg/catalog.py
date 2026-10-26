"""Channels the catalog knows only from a guide.

A guide lists far more channels than the playlists carry: iptvx.one alone has about
4000. Each guide channel that no catalog channel matches by name becomes a catalog
channel without feeds, so the catalog lists it and a playlist that brings a stream
for it later lands on the same channel (by name, or by the directory link).

No feed is created: guide mapping attaches a schedule to feeds, so these channels
get no stored programme until they have a stream (MIH_EPG_SCOPE stays meaningful).
"""

from __future__ import annotations

import json
import sqlite3

from mih.identity.normalize import split_name
from mih.ids import new_id


def add_guide_channels(
    conn: sqlite3.Connection, now: str, *, epg_source_id: str | None = None
) -> int:
    """Create catalog channels for unmatched guide channels. Runs in the caller's
    transaction. Returns how many were created."""
    sql = (
        "SELECT ec.id, ec.display_names, ec.icon_url FROM epg_channels ec"
        " WHERE NOT EXISTS (SELECT 1 FROM epg_mappings m WHERE m.epg_channel_id = ec.id"
        "   AND m.decision != 'rejected')"
        " AND NOT EXISTS (SELECT 1 FROM channel_aliases a WHERE a.kind = 'epg'"
        "   AND a.value = ec.id)"
    )
    params: tuple = ()
    if epg_source_id:
        sql += " AND ec.epg_source_id = ?"
        params = (epg_source_id,)
    created = 0
    for ec in conn.execute(sql + " ORDER BY ec.rowid", params).fetchall():
        names = [n for _, n in json.loads(ec["display_names"]) if n and n.strip()]
        keys = {split_name(n).key: n for n in names}
        keys.pop("", None)
        if not keys:
            continue
        known = conn.execute(
            f"SELECT channel_id FROM channel_aliases WHERE kind = 'name'"
            f" AND value IN ({','.join('?' * len(keys))}) LIMIT 1",
            tuple(keys),
        ).fetchone()
        if known:
            # Remember the guide channel so the next import does not look again.
            channel_id = known["channel_id"]
        else:
            channel_id = new_id()
            conn.execute(
                "INSERT INTO channels (id, canonical_name, logo_url, created_at)"
                " VALUES (?, ?, ?, ?)",
                (channel_id, split_name(names[0]).brand_display, ec["icon_url"], now),
            )
            conn.executemany(
                "INSERT OR IGNORE INTO channel_aliases (channel_id, kind, value, display)"
                " VALUES (?, 'name', ?, ?)",
                [(channel_id, k, n) for k, n in keys.items()],
            )
            created += 1
        conn.execute(
            "INSERT OR IGNORE INTO channel_aliases (channel_id, kind, value, display)"
            " VALUES (?, 'epg', ?, NULL)",
            (channel_id, ec["id"]),
        )
    return created
