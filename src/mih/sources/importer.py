"""Snapshot import: one playlist fetch becomes one atomic catalog update (spec 16)."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass, field
from pathlib import Path

from mih.config import Settings
from mih.epg.mapping import refresh_mappings
from mih.identity.normalize import NameParts, split_name
from mih.identity.resolver import Resolution, record_decision, resolve
from mih.ids import new_id, utc_now
from mih.retention import prune_snapshots
from mih.sources.m3u import M3UEntry, M3UFormatError, parse_m3u


@dataclass
class ImportReport:
    snapshot_id: str
    status: str  # applied | unchanged | rejected
    reason: str | None = None
    entries: int = 0
    channels_created: int = 0
    streams_created: int = 0
    streams_absent: int = 0
    needs_review: int = 0
    parse_errors: list[str] = field(default_factory=list)

    def stats(self) -> dict:
        data = asdict(self)
        data["parse_errors"] = len(self.parse_errors)
        return data


def import_m3u(
    conn: sqlite3.Connection,
    settings: Settings,
    source_id: str,
    content: bytes,
    *,
    force: bool = False,
) -> ImportReport:
    """Import a playlist snapshot for a source.

    A broken, empty or suspiciously small response never replaces the catalog: it is
    recorded as a rejected snapshot and the previous state stays active. ``force``
    skips the size-drop check and the unchanged-content shortcut, for an admin who
    has confirmed the new playlist.
    """
    now = utc_now()
    content_hash = hashlib.sha256(content).hexdigest()
    last = conn.execute(
        "SELECT content_hash, item_count FROM source_snapshots"
        " WHERE source_id = ? AND status = 'applied' ORDER BY fetched_at DESC, rowid DESC LIMIT 1",
        (source_id,),
    ).fetchone()

    if last and last["content_hash"] == content_hash and not force:
        return _store_snapshot(conn, source_id, now, content_hash, last["item_count"],
                               ImportReport(new_id(), "unchanged"))  # fmt: skip

    try:
        parsed = parse_m3u(content)
    except M3UFormatError:
        return _store_snapshot(conn, source_id, now, content_hash, 0,
                               ImportReport(new_id(), "rejected", "not_m3u"))  # fmt: skip

    report = ImportReport(new_id(), "applied", entries=len(parsed.entries),
                          parse_errors=parsed.errors)  # fmt: skip
    if not parsed.entries:
        report.status, report.reason = "rejected", "empty"
        return _store_snapshot(conn, source_id, now, content_hash, 0, report)
    if (
        last
        and not force
        and last["item_count"] > 0
        and len(parsed.entries) < (1 - settings.max_drop_ratio) * last["item_count"]
    ):
        report.status, report.reason = "rejected", "too_few_entries"
        return _store_snapshot(conn, source_id, now, content_hash, len(parsed.entries), report)

    blob_path = _write_blob(settings.snapshots_dir, source_id, content_hash, content)

    conn.execute("BEGIN")
    try:
        conn.execute(
            "INSERT INTO source_snapshots"
            " (id, source_id, fetched_at, content_hash, item_count, status, blob_path)"
            " VALUES (?, ?, ?, ?, ?, 'applied', ?)",
            (report.snapshot_id, source_id, now, content_hash, len(parsed.entries),
             str(blob_path)),
        )  # fmt: skip
        seen: set[str] = set()
        for entry in parsed.entries:
            seen.add(_import_entry(conn, report, source_id, entry, now))

        report.streams_absent = _mark_absent(conn, source_id, seen)
        # New feeds may be what a guide channel was waiting for.
        refresh_mappings(conn, now)
        conn.execute(
            "UPDATE source_snapshots SET stats = ? WHERE id = ?",
            (json.dumps(report.stats()), report.snapshot_id),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    prune_snapshots(conn, settings, "source_snapshots", source_id)
    return report


def _import_entry(
    conn: sqlite3.Connection, report: ImportReport, source_id: str, entry: M3UEntry, now: str
) -> str:
    parts = split_name(entry.name)
    tvg_id = entry.tvg_id
    country = _first_code(entry.attrs.get("tvg-country"))
    locator_hash = hashlib.sha256(entry.url.encode()).hexdigest()

    record_id = new_id()
    conn.execute(
        "INSERT INTO source_records"
        " (id, snapshot_id, position, external_key, raw_fingerprint, normalized_payload)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (
            record_id,
            report.snapshot_id,
            entry.position,
            tvg_id or parts.key,
            hashlib.sha256(entry.raw.encode()).hexdigest(),
            json.dumps(
                {
                    "name": entry.name,
                    "attrs": entry.attrs,
                    "key": parts.key,
                    "region": parts.region,
                    "offset_minutes": parts.offset_minutes,
                },
                ensure_ascii=False,
            ),  # fmt: skip
        ),
    )

    existing = conn.execute(
        "SELECT s.id, s.feed_id, f.channel_id FROM streams s JOIN feeds f ON f.id = s.feed_id"
        " WHERE s.source_id = ? AND s.locator_hash = ?",
        (source_id, locator_hash),
    ).fetchone()

    feed_id = None
    if existing and _still_same_channel(conn, existing["channel_id"], parts, tvg_id):
        # Spec 15.1, step 2: a mapping established earlier wins over re-matching, so a
        # re-import never re-shuffles the catalog.
        feed_id = existing["feed_id"]
        if (
            conn.execute(
                "SELECT 1 FROM feeds WHERE id = ? AND region = ? AND offset_minutes = ?",
                (feed_id, parts.region, parts.offset_minutes),
            ).fetchone()
            is None
        ):
            feed_id = None

    if feed_id is None:
        resolution = resolve(conn, parts=parts, tvg_id=tvg_id, country=country,
                             logo_url=entry.attrs.get("tvg-logo") or None,
                             source_id=source_id, now=now)  # fmt: skip
        feed_id = resolution.feed_id
        _count_resolution(report, resolution)
        record_decision(conn, resolution=resolution, record_id=record_id, parts=parts,
                        tvg_id=tvg_id, now=now)  # fmt: skip

    if tvg_id:
        conn.execute(
            "INSERT OR IGNORE INTO feed_aliases (feed_id, kind, value) VALUES (?, 'tvg-id', ?)",
            (feed_id, tvg_id.casefold()),
        )
    stream_id = _upsert_stream(conn, existing["id"] if existing else None, feed_id, source_id,
                               locator_hash, entry, parts, now)  # fmt: skip
    if not existing:
        report.streams_created += 1
    conn.execute("UPDATE source_records SET stream_id = ? WHERE id = ?", (stream_id, record_id))
    return stream_id


def _still_same_channel(
    conn: sqlite3.Connection, channel_id: str, parts: NameParts, tvg_id: str | None
) -> bool:
    if conn.execute(
        "SELECT 1 FROM channel_aliases WHERE channel_id = ? AND kind = 'name' AND value = ?",
        (channel_id, parts.key),
    ).fetchone():
        return True
    return bool(
        tvg_id
        and conn.execute(
            "SELECT 1 FROM channel_aliases WHERE channel_id = ? AND kind = 'tvg-id' AND value = ?",
            (channel_id, tvg_id.casefold()),
        ).fetchone()
    )


def _count_resolution(report: ImportReport, resolution: Resolution) -> None:
    if resolution.created_channel:
        report.channels_created += 1
    if resolution.decision == "needs_review":
        report.needs_review += 1


def _upsert_stream(
    conn: sqlite3.Connection,
    stream_id: str | None,
    feed_id: str,
    source_id: str,
    locator_hash: str,
    entry: M3UEntry,
    parts: NameParts,
    now: str,
) -> str:
    fmt = _guess_format(entry.url)
    if stream_id:
        conn.execute(
            "UPDATE streams SET feed_id = ?, format = ?, quality_label = ?, last_seen_at = ?,"
            # Back in the playlist; reasons found by the health check (DRM, geo) stay.
            " availability_reason = nullif(availability_reason, 'source_absent') WHERE id = ?",
            (feed_id, fmt, parts.quality, now, stream_id),
        )
    else:
        stream_id = new_id()
        conn.execute(
            "INSERT INTO streams (id, feed_id, source_id, locator_hash, format, quality_label,"
            " first_seen_at, last_seen_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (stream_id, feed_id, source_id, locator_hash, fmt, parts.quality, now, now),
        )
    conn.execute(
        "INSERT INTO stream_locators (stream_id, url, headers) VALUES (?, ?, ?)"
        " ON CONFLICT(stream_id) DO UPDATE SET url = excluded.url, headers = excluded.headers",
        (stream_id, entry.url, json.dumps(entry.headers) if entry.headers else None),
    )
    return stream_id


def _mark_absent(conn: sqlite3.Connection, source_id: str, seen: set[str]) -> int:
    """Streams missing from this snapshot are flagged, never deleted (spec 17)."""
    rows = conn.execute(
        "SELECT id FROM streams WHERE source_id = ?"
        " AND (availability_reason IS NULL OR availability_reason != 'source_absent')",
        (source_id,),
    ).fetchall()
    absent = [r["id"] for r in rows if r["id"] not in seen]
    conn.executemany(
        "UPDATE streams SET availability_reason = 'source_absent' WHERE id = ?",
        [(sid,) for sid in absent],
    )
    return len(absent)


def _store_snapshot(
    conn: sqlite3.Connection,
    source_id: str,
    now: str,
    content_hash: str,
    item_count: int,
    report: ImportReport,
) -> ImportReport:
    conn.execute(
        "INSERT INTO source_snapshots"
        " (id, source_id, fetched_at, content_hash, item_count, status, reason, stats)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (report.snapshot_id, source_id, now, content_hash, item_count, report.status,
         report.reason, json.dumps(report.stats())),
    )  # fmt: skip
    return report


def _write_blob(root: Path, source_id: str, content_hash: str, content: bytes) -> Path:
    path = root / source_id / f"{content_hash}.m3u"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(content)
        tmp.replace(path)
    return path


def _guess_format(url: str) -> str | None:
    path = url.split("?", 1)[0].lower()
    if path.endswith(".m3u8"):
        return "hls"
    if path.endswith(".mpd"):
        return "dash"
    if path.endswith(".ts"):
        return "mpegts"
    return None


def _first_code(value: str | None) -> str | None:
    if not value:
        return None
    first = value.replace(";", ",").split(",")[0].strip()
    return first.upper() or None
