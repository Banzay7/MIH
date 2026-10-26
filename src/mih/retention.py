"""Forget playlist and guide snapshots older than MIH_HISTORY_DAYS.

A snapshot row of a playlist carries a copy of every entry (source_records), and each
changed fetch leaves a file under snapshots/. Without pruning both grow every day even
when nothing else is kept. The latest applied snapshot of a source always stays: the
catalog and ``mih export`` read their attributes from it.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from mih.config import Settings

_TABLES = {
    # table -> source column
    "source_snapshots": "source_id",
    "epg_snapshots": "epg_source_id",
}


def prune_snapshots(
    conn: sqlite3.Connection, settings: Settings, table: str, source_id: str,
    now: datetime | None = None,
) -> int:  # fmt: skip
    """Delete old snapshot rows of one source and the files nothing points to any more.
    Returns the number of rows deleted. Runs in its own transaction."""
    col = _TABLES[table]
    now = now or datetime.now(UTC)
    cutoff = (now - timedelta(days=settings.history_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    latest = conn.execute(
        f"SELECT id FROM {table} WHERE {col} = ? AND status = 'applied'"
        " ORDER BY fetched_at DESC, rowid DESC LIMIT 1",
        (source_id,),
    ).fetchone()
    keep = latest["id"] if latest else ""
    conn.execute("BEGIN")
    try:
        old = conn.execute(
            f"SELECT id, blob_path FROM {table} WHERE {col} = ? AND fetched_at < ? AND id != ?",
            (source_id, cutoff, keep),
        ).fetchall()
        # source_records and match_decisions go with their snapshot (ON DELETE CASCADE).
        conn.executemany(f"DELETE FROM {table} WHERE id = ?", [(r["id"],) for r in old])
        still_used = {
            r[0]
            for r in conn.execute(
                f"SELECT DISTINCT blob_path FROM {table} WHERE blob_path IS NOT NULL"
            )
        }
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    root = settings.snapshots_dir.resolve()
    for path in {r["blob_path"] for r in old if r["blob_path"]} - still_used:
        p = Path(path).resolve()
        if p.is_relative_to(root):
            p.unlink(missing_ok=True)
    return len(old)
