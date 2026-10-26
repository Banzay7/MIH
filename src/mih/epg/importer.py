"""XMLTV snapshot import: one guide fetch becomes one atomic schedule update (spec 16, 19)."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from mih.config import Settings
from mih.directory import link_and_merge
from mih.epg.catalog import add_guide_channels
from mih.epg.mapping import refresh_mappings
from mih.epg.scope import FILTERED, build_scope, listed_channels
from mih.epg.xmltv import XMLTVFormatError, XMLTVProgramme, open_content, parse_xmltv
from mih.identity.normalize import fold
from mih.ids import new_id, utc_now
from mih.retention import prune_snapshots
from mih.search.index import drop_orphans, index_programme


@dataclass
class EPGImportReport:
    snapshot_id: str
    status: str  # applied | unchanged | rejected
    reason: str | None = None
    channels: int = 0
    programmes: int = 0
    airings_created: int = 0
    airings_changed: int = 0
    airings_unchanged: int = 0
    airings_cancelled: int = 0
    airings_pruned: int = 0
    # Out of MIH_EPG_SCOPE: guide channels with nothing stored, programmes left out.
    channels_skipped: int = 0
    programmes_skipped: int = 0
    snapshots_pruned: int = 0
    mapped: int = 0
    needs_review: int = 0
    unmapped: int = 0
    # Catalog channels created for guide channels no playlist carries.
    channels_added: int = 0
    # Which guide channels were stored and how (see scope_key): a later run with the
    # same guide but other mappings or lists imports it again.
    scope_key: str | None = None
    parse_errors: list[str] = field(default_factory=list)

    def stats(self) -> dict:
        data = asdict(self)
        data["parse_errors"] = len(self.parse_errors)
        return data


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def import_xmltv(
    conn: sqlite3.Connection,
    settings: Settings,
    epg_source_id: str,
    content: bytes,
    *,
    force: bool = False,
    rescope: bool = False,
    now: datetime | None = None,
) -> EPGImportReport:
    """Import a guide snapshot. Like playlists, a broken, empty or much smaller guide is
    recorded as rejected and the schedule already stored stays in place.

    ``rescope``: the content is the one already imported, but what is stored of it
    changed (new mappings, other channels in the lists); import it again."""
    now_dt = now or datetime.now(UTC)
    now_s = iso(now_dt) if now else utc_now()
    content_hash = hashlib.sha256(content).hexdigest()
    last = conn.execute(
        "SELECT content_hash, channel_count FROM epg_snapshots WHERE epg_source_id = ?"
        " AND status = 'applied' ORDER BY fetched_at DESC, rowid DESC LIMIT 1",
        (epg_source_id,),
    ).fetchone()
    if last and last["content_hash"] == content_hash and not (force or rescope):
        return _store_snapshot(conn, epg_source_id, now_s, content_hash,
                               EPGImportReport(new_id(), "unchanged"))  # fmt: skip

    # Two passes: the first only lists channels and counts programmes, so the safety
    # checks and the scope come first and only in-scope programmes are ever built.
    try:
        scan = parse_xmltv(open_content(content, settings.epg_max_bytes), keep=lambda _: False)
    except XMLTVFormatError as exc:
        reason = "too_large" if "MIH_EPG_MAX_BYTES" in str(exc) else "not_xmltv"
        return _store_snapshot(conn, epg_source_id, now_s, content_hash,
                               EPGImportReport(new_id(), "rejected", reason))  # fmt: skip

    counts = scan.counts
    report = EPGImportReport(new_id(), "applied", channels=len(counts),
                             programmes=sum(counts.values()))  # fmt: skip
    if not report.programmes:
        report.status, report.reason = "rejected", "empty"
        return _store_snapshot(conn, epg_source_id, now_s, content_hash, report)
    if (
        last
        and not force
        and last["channel_count"] > 0
        and len(counts) < (1 - settings.max_drop_ratio) * last["channel_count"]
    ):
        report.status, report.reason = "rejected", "too_few_channels"
        return _store_snapshot(conn, epg_source_id, now_s, content_hash, report)

    blob_path = _write_blob(settings.snapshots_dir / "epg", epg_source_id, content_hash, content)
    conn.execute("BEGIN")
    try:
        conn.execute(
            "INSERT INTO epg_snapshots (id, epg_source_id, fetched_at, content_hash,"
            " channel_count, programme_count, status, blob_path)"
            " VALUES (?, ?, ?, ?, ?, ?, 'applied', ?)",
            (report.snapshot_id, epg_source_id, now_s, content_hash, len(counts),
             report.programmes, str(blob_path)),
        )  # fmt: skip
        ids = {xid: _upsert_channel(conn, epg_source_id, xid, scan.channels.get(xid), now_s)
               for xid in set(scan.channels) | set(counts)}  # fmt: skip
        # Map first, so a guide channel the catalog gained since the last import is
        # already in scope for this one.
        mapped = refresh_mappings(conn, now_s, epg_source_id=epg_source_id)
        scope = build_scope(conn, settings.epg_scope, list(ids.values()),
                            listed_channels(settings.lists_dir))  # fmt: skip
        wanted = {xid for xid in counts if ids[xid] in scope.channels}
        report.scope_key = scope_key(scope, ids.values())
        parsed = parse_xmltv(open_content(content, settings.epg_max_bytes),
                             keep=wanted.__contains__)  # fmt: skip
        report.parse_errors = parsed.errors
        by_channel: dict[str, list[XMLTVProgramme]] = {}
        for p in parsed.programmes:
            by_channel.setdefault(p.channel, []).append(p)
        del parsed
        for xid, n in counts.items():
            cid = ids[xid]
            mode = scope.channels.get(cid)
            items = by_channel.pop(xid, [])
            if mode is None:
                report.channels_skipped += 1
                report.programmes_skipped += n
                # Dropped from scope: forget what is still to come, keep the past.
                conn.execute(
                    "DELETE FROM airings WHERE epg_channel_id = ? AND start_utc >= ?",
                    (cid, now_s),
                )
                continue
            if not items:  # every programme of it was malformed
                continue
            if mode == FILTERED:
                kept = [p for p in items if scope.keeps(p)]
                report.programmes_skipped += len(items) - len(kept)
                _apply_schedule(conn, report, cid, kept, now_s, window=items, missing="delete")
            else:
                _apply_schedule(conn, report, cid, items, now_s)

        cutoff = iso(now_dt - timedelta(days=settings.epg_retention_days))
        report.airings_pruned = conn.execute(
            "DELETE FROM airings WHERE stop_utc < ?", (cutoff,)
        ).rowcount
        # A favourite programme stays even between airings: its title is what
        # MIH_EPG_SCOPE=tracked looks for in the next guide.
        conn.execute(
            "DELETE FROM programmes WHERE NOT EXISTS"
            " (SELECT 1 FROM airings a WHERE a.programme_id = programmes.id)"
            " AND id NOT IN (SELECT target_id FROM favorites WHERE target_kind = 'programme')"
        )
        drop_orphans(conn)
        # Guide channels no playlist carries still belong in the catalog.
        report.channels_added = add_guide_channels(conn, now_s, epg_source_id=epg_source_id)
        link_and_merge(conn)

        report.mapped = mapped["auto"] + mapped["approved"]
        report.needs_review, report.unmapped = mapped["needs_review"], mapped["unmapped"]
        conn.execute(
            "UPDATE epg_snapshots SET stats = ? WHERE id = ?",
            (json.dumps(report.stats()), report.snapshot_id),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    report.snapshots_pruned = prune_snapshots(conn, settings, "epg_snapshots", epg_source_id,
                                              now_dt)  # fmt: skip
    return report


def scope_key(scope, epg_channel_ids) -> str:
    """A fingerprint of what an import of this guide stores: channels and their mode,
    and the topics and titles that filter the rest."""
    parts = sorted(f"{cid}:{scope.channels[cid]}" for cid in epg_channel_ids
                   if cid in scope.channels)  # fmt: skip
    parts.append(scope.topics.pattern if scope.topics else "")
    parts.extend(sorted(scope.titles))
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()[:16]


def current_scope_key(conn: sqlite3.Connection, settings: Settings, epg_source_id: str) -> str:
    """scope_key an import of this guide would have now, after mapping its channels to
    the catalog as it is now."""
    refresh_mappings(conn, utc_now(), epg_source_id=epg_source_id)
    ids = [r[0] for r in conn.execute(
        "SELECT id FROM epg_channels WHERE epg_source_id = ?", (epg_source_id,))]  # fmt: skip
    scope = build_scope(conn, settings.epg_scope, ids, listed_channels(settings.lists_dir))
    return scope_key(scope, ids)


def _upsert_channel(conn, epg_source_id: str, xmltv_id: str, ch, now: str) -> str:
    names = json.dumps(ch.display_names if ch else [], ensure_ascii=False)
    icon = ch.icon if ch else None
    row = conn.execute(
        "SELECT id FROM epg_channels WHERE epg_source_id = ? AND xmltv_id = ?",
        (epg_source_id, xmltv_id),
    ).fetchone()
    if row:
        conn.execute(
            "UPDATE epg_channels SET display_names = ?, icon_url = COALESCE(?, icon_url),"
            " last_seen_at = ? WHERE id = ?",
            (names, icon, now, row["id"]),
        )
        return row["id"]
    channel_id = new_id()
    conn.execute(
        "INSERT INTO epg_channels (id, epg_source_id, xmltv_id, display_names, icon_url,"
        " first_seen_at, last_seen_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (channel_id, epg_source_id, xmltv_id, names, icon, now, now),
    )
    return channel_id


def _apply_schedule(
    conn: sqlite3.Connection,
    report: EPGImportReport,
    epg_channel_id: str,
    items: list[XMLTVProgramme],
    now: str,
    *,
    window: list[XMLTVProgramme] | None = None,
    missing: str = "cancel",
) -> None:
    """Replace one channel's schedule inside the time window this snapshot covers.

    A slot is keyed by its start time. Same programme and stop: unchanged. Anything
    else at that start: a new version of the airing. A stored airing inside the window
    that the guide no longer lists is cancelled, not deleted, so history stays honest.
    Airings outside the window (older days, a longer horizon from before) are left alone.

    With ``missing="delete"`` (a topic-filtered channel) ``items`` is the part of
    ``window`` worth keeping; upcoming airings that are not kept are deleted instead,
    since leaving them out was a choice, not a guide change.
    """
    window = window or items
    window_start = iso(min(p.start_utc for p in window))
    window_stop = iso(max(p.stop_utc for p in window))
    existing = {
        r["start_utc"]: r
        for r in conn.execute(
            "SELECT id, start_utc, stop_utc, programme_id, status FROM airings"
            " WHERE epg_channel_id = ? AND start_utc >= ? AND start_utc < ?",
            (epg_channel_id, window_start, window_stop),
        )
    }
    seen: set[str] = set()
    for p in items:
        start, stop = iso(p.start_utc), iso(p.stop_utc)
        if start in seen:
            report.parse_errors.append(f"two programmes on {p.channel} at {p.start_raw}")
            continue
        seen.add(start)
        programme_id = _upsert_programme(conn, p, now)
        old = existing.get(start)
        if old is None:
            conn.execute(
                "INSERT INTO airings (id, epg_channel_id, programme_id, start_utc, stop_utc,"
                " start_raw, snapshot_id, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (new_id(), epg_channel_id, programme_id, start, stop, p.start_raw,
                 report.snapshot_id, now),
            )  # fmt: skip
            report.airings_created += 1
        elif (old["programme_id"], old["stop_utc"], old["status"]) == (
            programme_id, stop, "scheduled"
        ):  # fmt: skip
            report.airings_unchanged += 1
        else:
            conn.execute(
                "UPDATE airings SET programme_id = ?, stop_utc = ?, start_raw = ?,"
                " status = 'scheduled', version = version + 1, snapshot_id = ?, updated_at = ?"
                " WHERE id = ?",
                (programme_id, stop, p.start_raw, report.snapshot_id, now, old["id"]),
            )
            report.airings_changed += 1
    for start, old in existing.items():
        if start in seen:
            continue
        if missing == "delete":
            if start >= now:
                conn.execute("DELETE FROM airings WHERE id = ?", (old["id"],))
        elif old["status"] == "scheduled":
            conn.execute(
                "UPDATE airings SET status = 'cancelled', version = version + 1,"
                " snapshot_id = ?, updated_at = ? WHERE id = ?",
                (report.snapshot_id, now, old["id"]),
            )
            report.airings_cancelled += 1


def _upsert_programme(conn: sqlite3.Connection, p: XMLTVProgramme, now: str) -> str:
    title = p.titles[0][1]
    sub_title = p.sub_titles[0][1] if p.sub_titles else None
    desc = p.descs[0][1] if p.descs else None
    key = json.dumps([fold(title), fold(sub_title or ""), p.episode_num or "",
                      fold(desc or "")], ensure_ascii=False)  # fmt: skip
    fingerprint = hashlib.sha256(key.encode()).hexdigest()
    row = conn.execute("SELECT id FROM programmes WHERE fingerprint = ?", (fingerprint,)).fetchone()
    if row:
        return row["id"]
    programme_id = new_id()
    localized = {"titles": p.titles, "sub_titles": p.sub_titles, "descs": p.descs}
    conn.execute(
        "INSERT INTO programmes (id, fingerprint, title, sub_title, description, localized,"
        " categories, episode_num, lang, icon_url, rating, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (programme_id, fingerprint, title, sub_title, desc,
         json.dumps(localized, ensure_ascii=False), json.dumps(p.categories, ensure_ascii=False),
         p.episode_num, p.lang, p.icon, p.rating, now),
    )  # fmt: skip
    index_programme(conn, programme_id)
    return programme_id


def _store_snapshot(
    conn: sqlite3.Connection, epg_source_id: str, now: str, content_hash: str,
    report: EPGImportReport,
) -> EPGImportReport:  # fmt: skip
    conn.execute(
        "INSERT INTO epg_snapshots (id, epg_source_id, fetched_at, content_hash, channel_count,"
        " programme_count, status, reason, stats) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (report.snapshot_id, epg_source_id, now, content_hash, report.channels,
         report.programmes, report.status, report.reason, json.dumps(report.stats())),
    )  # fmt: skip
    return report


def _write_blob(root: Path, source_id: str, content_hash: str, content: bytes) -> Path:
    suffix = ".xml.gz" if content[:2] == b"\x1f\x8b" else ".xml"
    path = root / source_id / f"{content_hash}{suffix}"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(content)
        tmp.replace(path)
    return path
