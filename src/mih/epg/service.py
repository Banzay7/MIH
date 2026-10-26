"""EPG source registry and the fetch-then-import flow used by the API and CLI."""

from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

from mih.config import Settings
from mih.epg.importer import EPGImportReport, current_scope_key, import_xmltv
from mih.ids import new_id, utc_now
from mih.search.index import drop_orphans
from mih.sources.fetch import check_destination, fetch_source
from mih.sources.service import SourceError


def create_epg_source(
    conn: sqlite3.Connection, settings: Settings, *, name: str, url: str | None
) -> str:
    if url:
        check_destination(url, allow_private=settings.allow_private_sources)
    source_id = new_id()
    try:
        conn.execute(
            "INSERT INTO epg_sources (id, name, url, created_at) VALUES (?, ?, ?, ?)",
            (source_id, name, url, utc_now()),
        )
    except sqlite3.IntegrityError as exc:
        raise SourceError("conflict", f"an EPG source named {name!r} already exists") from exc
    return source_id


def get_epg_source(conn: sqlite3.Connection, source_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM epg_sources WHERE id = ?", (source_id,)).fetchone()
    if row is None:
        raise SourceError("not_found", "EPG source not found")
    return row


def import_epg_from_url(
    conn: sqlite3.Connection, settings: Settings, source_id: str, *, force: bool = False
) -> EPGImportReport | None:
    """Fetch the guide and import it. Returns None when the server says nothing changed."""
    source = get_epg_source(conn, source_id)
    if not source["url"]:
        raise SourceError("validation_error", "EPG source has no URL; upload the file instead")
    result = fetch_source(
        source["url"],
        settings,
        etag=None if force else source["etag"],
        last_modified=None if force else source["last_modified"],
    )
    if result.content is not None:
        report = import_xmltv(conn, settings, source_id, result.content, force=force)
        if report.status == "applied":
            conn.execute(
                "UPDATE epg_sources SET etag = ?, last_modified = ? WHERE id = ?",
                (result.etag, result.last_modified, source_id),
            )
        if report.status != "unchanged":
            return report
    # The guide did not change, but the catalog may have: channels newly matched to it,
    # or channels that joined or left the lists. Then the stored copy is imported again.
    return _rescope(conn, settings, source_id)


def _rescope(
    conn: sqlite3.Connection, settings: Settings, source_id: str
) -> EPGImportReport | None:
    last = conn.execute(
        "SELECT blob_path, stats FROM epg_snapshots WHERE epg_source_id = ? AND status = 'applied'"
        " ORDER BY fetched_at DESC, rowid DESC LIMIT 1",
        (source_id,),
    ).fetchone()
    if last is None or not last["blob_path"] or not Path(last["blob_path"]).is_file():
        return None
    stored = json.loads(last["stats"] or "{}").get("scope_key")
    if stored == current_scope_key(conn, settings, source_id):
        return None
    content = Path(last["blob_path"]).read_bytes()
    return import_xmltv(conn, settings, source_id, content, rescope=True)


def delete_epg_source(conn: sqlite3.Connection, settings: Settings, source_id: str) -> dict:
    """Forget a guide: its snapshots, guide channels, mappings and schedule, and the
    stored copies of its files. Programmes no other guide airs go with it."""
    get_epg_source(conn, source_id)
    conn.execute("BEGIN")
    try:
        channels = [r[0] for r in conn.execute(
            "SELECT id FROM epg_channels WHERE epg_source_id = ?", (source_id,))]  # fmt: skip
        conn.executemany(
            "DELETE FROM channel_aliases WHERE kind = 'epg' AND value = ?",
            [(c,) for c in channels],
        )
        # Snapshots, guide channels, their mappings and airings go by ON DELETE CASCADE.
        conn.execute("DELETE FROM epg_sources WHERE id = ?", (source_id,))
        programmes = conn.execute(
            "DELETE FROM programmes WHERE NOT EXISTS"
            " (SELECT 1 FROM airings a WHERE a.programme_id = programmes.id)"
            " AND id NOT IN (SELECT target_id FROM favorites WHERE target_kind = 'programme')"
        ).rowcount
        drop_orphans(conn)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    shutil.rmtree(settings.snapshots_dir / "epg" / source_id, ignore_errors=True)
    return {"id": source_id, "guide_channels": len(channels), "programmes": programmes}


def set_epg_source_enabled(conn: sqlite3.Connection, source_id: str, enabled: bool) -> None:
    get_epg_source(conn, source_id)
    conn.execute("UPDATE epg_sources SET enabled = ? WHERE id = ?", (int(enabled), source_id))
