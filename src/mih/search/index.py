"""Keep the full-text index of programmes in step with the programmes table."""

from __future__ import annotations

import json
import sqlite3

from mih.identity.normalize import fold


def index_programme(conn: sqlite3.Connection, programme_id: str) -> None:
    row = conn.execute(
        "SELECT rowid, title, sub_title, description, categories, localized"
        " FROM programmes WHERE id = ?",
        (programme_id,),
    ).fetchone()
    _insert(conn, row)


def drop_orphans(conn: sqlite3.Connection) -> None:
    for table in ("programme_fts", "programme_trigram"):
        conn.execute(f"DELETE FROM {table} WHERE rowid NOT IN (SELECT rowid FROM programmes)")


def ensure_index(conn: sqlite3.Connection) -> bool:
    """Rebuild when the index and the table disagree (first start after migration 0003,
    or a crash between writes). Returns True when it rebuilt."""
    programmes = conn.execute("SELECT COUNT(*) FROM programmes").fetchone()[0]
    indexed = conn.execute("SELECT COUNT(*) FROM programme_fts").fetchone()[0]
    if programmes == indexed:
        return False
    conn.execute("BEGIN")
    try:
        conn.execute("DELETE FROM programme_fts")
        conn.execute("DELETE FROM programme_trigram")
        for row in conn.execute(
            "SELECT rowid, title, sub_title, description, categories, localized FROM programmes"
        ).fetchall():
            _insert(conn, row)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return True


def _insert(conn: sqlite3.Connection, row: sqlite3.Row) -> None:
    localized = json.loads(row["localized"])
    # Every language variant is searchable, not only the first title.
    titles = " ".join(t for _, t in localized.get("titles", [])) or row["title"]
    subs = " ".join(t for _, t in localized.get("sub_titles", [])) or (row["sub_title"] or "")
    descs = " ".join(t for _, t in localized.get("descs", [])) or (row["description"] or "")
    cats = " ".join(json.loads(row["categories"]))
    conn.execute(
        "INSERT INTO programme_fts (rowid, title, sub_title, description, categories)"
        " VALUES (?, ?, ?, ?, ?)",
        (row["rowid"], fold(titles), fold(subs), fold(descs), fold(cats)),
    )
    conn.execute(
        "INSERT INTO programme_trigram (rowid, title) VALUES (?, ?)", (row["rowid"], fold(titles))
    )
