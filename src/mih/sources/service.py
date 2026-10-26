"""Source registry and the fetch-then-import flow used by the API and CLI."""

from __future__ import annotations

import sqlite3

from mih.config import Settings
from mih.ids import new_id, utc_now
from mih.sources.fetch import FetchResult, check_destination, fetch_source
from mih.sources.importer import ImportReport, import_m3u


class SourceError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def create_source(
    conn: sqlite3.Connection, settings: Settings, *, name: str, url: str | None, kind: str = "m3u"
) -> str:
    if kind != "m3u":
        raise SourceError("validation_error", f"unsupported source kind: {kind}")
    if url:
        check_destination(url, allow_private=settings.allow_private_sources)
    source_id = new_id()
    try:
        conn.execute(
            "INSERT INTO sources (id, kind, name, url, created_at) VALUES (?, ?, ?, ?, ?)",
            (source_id, kind, name, url, utc_now()),
        )
    except sqlite3.IntegrityError as exc:
        raise SourceError("conflict", f"a source named {name!r} already exists") from exc
    return source_id


def get_source(conn: sqlite3.Connection, source_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
    if row is None:
        raise SourceError("not_found", "source not found")
    return row


def import_from_url(
    conn: sqlite3.Connection, settings: Settings, source_id: str, *, force: bool = False
) -> ImportReport | None:
    """Fetch the source and import it. Returns None when the server says nothing changed."""
    source = get_source(conn, source_id)
    if not source["url"]:
        raise SourceError("validation_error", "source has no URL; upload the playlist instead")
    result: FetchResult = fetch_source(
        source["url"],
        settings,
        etag=None if force else source["etag"],
        last_modified=None if force else source["last_modified"],
    )
    if result.content is None:
        return None
    report = import_m3u(conn, settings, source_id, result.content, force=force)
    if report.status == "applied":
        conn.execute(
            "UPDATE sources SET etag = ?, last_modified = ? WHERE id = ?",
            (result.etag, result.last_modified, source_id),
        )
    return report
