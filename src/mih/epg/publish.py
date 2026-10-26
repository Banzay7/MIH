"""The guide of a channel list as one XMLTV file, so a player gets playlist and guide
from the same place.

Each playlist entry carries a ``tvg-id`` that the file uses as its channel id, so
the player matches them without guessing by name. A +N feed without a guide of its
own gets the base schedule shifted, as in the API. Only what is stored goes out:
past airings within MIH_EPG_RETENTION_DAYS and whatever the guides list ahead.
"""

from __future__ import annotations

import gzip
import json
import os
import re
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

from mih.epg import guide
from mih.epg.guide import iso

_XMLTV_NS = re.compile(r"^\d*\s*\.\s*\d*\s*\.\s*\d*(/\d+)?$")


@dataclass(frozen=True)
class GuideEntry:
    guide_id: str  # the playlist entry's tvg-id
    name: str
    logo: str | None
    feed_id: str
    channel_id: str


@dataclass
class GuideResult:
    channels: int  # entries with at least one programme
    programmes: int
    with_guide: set[str]  # guide_ids that have programmes


def write_guide(
    conn: sqlite3.Connection, path: Path, entries: list[GuideEntry], *, since: datetime
) -> GuideResult:
    """Write ``path`` (gzip XMLTV, replaced atomically) for the entries."""
    guides = guide.feed_guides(conn, {e.channel_id for e in entries})
    result = GuideResult(0, 0, set())
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as gz:
            gz.write(b'<?xml version="1.0" encoding="UTF-8"?>\n'
                     b'<tv generator-info-name="MIH">\n')  # fmt: skip
            for e in entries:
                lines = [f"  <channel id={quoteattr(e.guide_id)}>",
                         f"    <display-name>{escape(e.name)}</display-name>"]  # fmt: skip
                if e.logo:
                    lines.append(f"    <icon src={quoteattr(e.logo)}/>")
                lines.append("  </channel>\n")
                gz.write("\n".join(lines).encode())
            for e in entries:
                g = guides.get(e.feed_id)
                if g is None:
                    continue
                n = 0
                for row in _airings(conn, g, since):
                    gz.write(_programme(e.guide_id, row, g.shift_minutes).encode())
                    n += 1
                if n:
                    result.channels += 1
                    result.programmes += n
                    result.with_guide.add(e.guide_id)
            gz.write(b"</tv>\n")
        os.chmod(tmp, 0o640)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return result


def guide_coverage(
    conn: sqlite3.Connection, entries: list[GuideEntry], *, since: datetime
) -> set[str]:
    """guide_ids that ``write_guide`` would give programmes, without writing."""
    guides = guide.feed_guides(conn, {e.channel_id for e in entries})
    # Each +N shift moves the window, so the query runs per shift, not per entry.
    by_shift: dict[int, set[str]] = {}
    for g in guides.values():
        by_shift.setdefault(g.shift_minutes, set()).add(g.epg_channel_id)
    live: set[tuple[int, str]] = set()
    for shift, ids in by_shift.items():
        marks = ",".join("?" * len(ids))
        for r in conn.execute(
            f"SELECT DISTINCT epg_channel_id FROM airings WHERE epg_channel_id IN ({marks})"
            " AND status = 'scheduled' AND stop_utc > ?",
            (*ids, iso(since - timedelta(minutes=shift))),
        ):
            live.add((shift, r[0]))
    out = set()
    for e in entries:
        g = guides.get(e.feed_id)
        if g and (g.shift_minutes, g.epg_channel_id) in live:
            out.add(e.guide_id)
    return out


def _airings(conn: sqlite3.Connection, g: guide.FeedGuide, since: datetime):
    return conn.execute(
        "SELECT a.start_utc, a.stop_utc, p.localized, p.categories, p.episode_num, p.icon_url,"
        " p.rating FROM airings a JOIN programmes p ON p.id = a.programme_id"
        " WHERE a.epg_channel_id = ? AND a.status = 'scheduled' AND a.stop_utc > ?"
        " ORDER BY a.start_utc",
        (g.epg_channel_id, iso(since - timedelta(minutes=g.shift_minutes))),
    )


def _time(value: str, shift: int) -> str:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00")) + timedelta(minutes=shift)
    return dt.strftime("%Y%m%d%H%M%S +0000")


def _texts(tag: str, items: list) -> list[str]:
    out = []
    for lang, text in items:
        if not text:
            continue
        attr = f" lang={quoteattr(lang)}" if lang else ""
        out.append(f"    <{tag}{attr}>{escape(text)}</{tag}>")
    return out


def _programme(guide_id: str, row: sqlite3.Row, shift: int) -> str:
    loc = json.loads(row["localized"])
    lines = [f'  <programme start="{_time(row["start_utc"], shift)}"'
             f' stop="{_time(row["stop_utc"], shift)}" channel={quoteattr(guide_id)}>']  # fmt: skip
    lines += _texts("title", loc.get("titles", []))
    lines += _texts("sub-title", loc.get("sub_titles", []))
    lines += _texts("desc", loc.get("descs", []))
    lines += [f"    <category>{escape(c)}</category>" for c in json.loads(row["categories"]) if c]
    if row["episode_num"]:
        system = "xmltv_ns" if _XMLTV_NS.match(row["episode_num"]) else "onscreen"
        lines.append(f'    <episode-num system="{system}">{escape(row["episode_num"])}'
                     "</episode-num>")  # fmt: skip
    if row["icon_url"]:
        lines.append(f"    <icon src={quoteattr(row['icon_url'])}/>")
    if row["rating"]:
        lines.append(f"    <rating><value>{escape(row['rating'])}</value></rating>")
    lines.append("  </programme>\n")
    return "\n".join(lines)
