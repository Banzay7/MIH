"""Catalog export to a folder (spec 21, ``export``): channels with categories and details.

Writes three files, each replaced atomically so a player or script reading the folder
never sees half a file:

- ``channels.json``: every channel with its categories, languages, feeds, stream health
  and EPG coverage. No stream URLs.
- ``channels.csv``: the same, one row per channel, for a spreadsheet.
- ``playlist.m3u``: one playable entry per feed (the best live stream), grouped by
  category. It carries stream URLs, so it is skipped with ``with_playlist=False``.

Categories: a channel gets the canonical categories its playlists' ``group-title`` values
point to, then the ones its name suggests, then the most frequent EPG programme
categories. Raw group titles are kept as they are in ``groups``.
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import sqlite3
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

from mih.identity.normalize import fold
from mih.ids import utc_now
from mih.playback import Target, pick_streams

# Canonical category -> folded keywords (ru, en, vi). A keyword matches a whole word
# or the start of one, so "спорт" matches "Спорт HD" and "спортивные".
CATEGORIES: dict[str, tuple[str, ...]] = {
    "news": ("news", "новост", "информац", "tin tuc", "thoi su", "24"),
    "sport": ("sport", "спорт", "футбол", "football", "soccer", "the thao", "матч", "match"),
    "kids": ("kids", "children", "cartoon", "детск", "дети", "мульт", "thieu nhi", "hoat hinh"),
    "movies": ("movie", "film", "cinema", "кино", "фильм", "phim"),
    "series": ("series", "сериал"),
    "music": ("music", "музык", "am nhac", "mtv"),
    "documentary": ("documentary", "докум", "познават", "science", "наук", "discovery",
                    "history", "истори", "nature", "природ", "tai lieu", "kham pha"),
    "education": ("education", "образоват", "обучени", "giao duc"),
    "entertainment": ("entertainment", "развлекат", "юмор", "comedy", "giai tri", "show"),
    "lifestyle": ("lifestyle", "cooking", "food", "кулинар", "еда", "travel", "путешеств",
                  "мода", "fashion", "doi song", "du lich"),
    "religion": ("religio", "религи", "православ", "church", "ton giao"),
    "business": ("business", "бизнес", "эконом", "финанс", "finance", "kinh te"),
    "general": ("general", "общие", "общероссийск", "эфирн", "federal", "федеральн",
                "tong hop"),
    "regional": ("regional", "регион", "local", "dia phuong"),
    "shop": ("shop", "магазин", "mua sam"),
    "adult": ("adult", "xxx", "18+", "взросл"),
}  # fmt: skip

_SPLIT_RE = re.compile(r"\s*[;|]\s*")


@dataclass
class ExportResult:
    out_dir: Path
    files: list[str]
    channels: int
    playlist_entries: int

    def stats(self) -> dict:
        return {"out_dir": str(self.out_dir), "files": self.files, "channels": self.channels,
                "playlist_entries": self.playlist_entries}  # fmt: skip


def categorize(texts: list[str]) -> list[str]:
    """Canonical categories the texts point to, in CATEGORIES order."""
    words = " " + " ".join(fold(t) for t in texts if t) + " "
    found = []
    for cat, keys in CATEGORIES.items():
        if any(re.search(r"(?<![\w])" + re.escape(fold(k)), words) for k in keys):
            found.append(cat)
    return found


def build_catalog(conn: sqlite3.Connection) -> list[dict]:
    """Every channel with categories and details, sorted by name. No stream URLs."""
    attrs = _current_attrs(conn)
    epg_cats = _epg_categories(conn)
    has_epg = {r[0] for r in conn.execute(
        "SELECT DISTINCT f.channel_id FROM epg_mappings m JOIN feeds f ON f.id = m.feed_id"
        " WHERE m.decision IN ('auto', 'approved')"
    )}  # fmt: skip
    sources = {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM sources")}

    streams_by_feed: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for s in conn.execute(
        "SELECT id, feed_id, source_id, format, quality_label, status, availability_reason,"
        " last_ok_at, last_checked_at FROM streams"
    ):
        streams_by_feed[s["feed_id"]].append(s)
    feeds_by_channel: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for f in conn.execute(
        "SELECT id, channel_id, region, offset_minutes, audio_lang FROM feeds"
        " ORDER BY offset_minutes, region"
    ):
        feeds_by_channel[f["channel_id"]].append(f)
    names: dict[str, list[str]] = defaultdict(list)
    tvg_ids: dict[str, set[str]] = defaultdict(set)
    for a in conn.execute("SELECT channel_id, kind, value, display FROM channel_aliases"):
        if a["kind"] == "name" and a["display"]:
            names[a["channel_id"]].append(a["display"])
        elif a["kind"] == "tvg-id":
            tvg_ids[a["channel_id"]].add(a["display"] or a["value"])

    out = []
    for ch in conn.execute(
        "SELECT id, canonical_name, country, logo_url, status FROM channels"
        " ORDER BY canonical_name COLLATE NOCASE"
    ):
        groups: list[str] = []
        langs: set[str] = set()
        countries: set[str] = {ch["country"]} if ch["country"] else set()
        feeds_out, status_counts = [], Counter()
        src_names: set[str] = set()
        for f in feeds_by_channel[ch["id"]]:
            if f["audio_lang"]:
                langs.add(f["audio_lang"])
            f_streams = []
            for s in streams_by_feed[f["id"]]:
                a = attrs.get(s["id"], {})
                for g in _SPLIT_RE.split(a.get("group-title", "")):
                    if g and g not in groups:
                        groups.append(g)
                for key, bucket in (("tvg-language", langs), ("tvg-country", countries)):
                    bucket.update(v.strip() for v in _SPLIT_RE.split(a.get(key, "")) if v.strip())
                live = s["availability_reason"] is None
                status_counts[s["status"] if live else "absent"] += 1
                src_names.add(sources.get(s["source_id"], s["source_id"]))
                f_streams.append({
                    "id": s["id"], "source": sources.get(s["source_id"], s["source_id"]),
                    "format": s["format"], "quality": s["quality_label"],
                    "status": s["status"] if live else "absent",
                    "last_ok_at": s["last_ok_at"], "last_checked_at": s["last_checked_at"],
                })  # fmt: skip
            feeds_out.append({"id": f["id"], "region": f["region"] or None,
                              "offset_minutes": f["offset_minutes"],
                              "audio_lang": f["audio_lang"], "streams": f_streams})  # fmt: skip

        from_groups = categorize(groups)
        from_name = [c for c in categorize([ch["canonical_name"], *names[ch["id"]]])
                     if c not in from_groups]  # fmt: skip
        top_epg = [c for c, _ in epg_cats.get(ch["id"], Counter()).most_common(5)]
        from_epg = [c for c in categorize(top_epg) if c not in from_groups + from_name]
        categories = from_groups + from_name + from_epg
        playable = sum(n for st, n in status_counts.items()
                       if st not in ("quarantine", "archived", "absent"))  # fmt: skip
        out.append({
            "id": ch["id"],
            "name": ch["canonical_name"],
            "categories": categories or ["other"],
            "category_source": ("playlist" if from_groups else "name" if from_name
                                else "epg" if from_epg else "none"),
            "groups": groups,
            "epg_categories": top_epg,
            "countries": sorted(countries),
            "languages": sorted(langs),
            "logo_url": ch["logo_url"],
            "also_known_as": sorted(set(names[ch["id"]]) - {ch["canonical_name"]}),
            "tvg_ids": sorted(tvg_ids[ch["id"]]),
            "sources": sorted(src_names),
            "has_epg": ch["id"] in has_epg,
            "health": _health(status_counts),
            "streams_playable": playable,
            "streams_by_status": dict(sorted(status_counts.items())),
            "feeds": feeds_out,
        })  # fmt: skip
    return out


def export_catalog(
    conn: sqlite3.Connection, out_dir: Path, *, with_playlist: bool = True
) -> ExportResult:
    out_dir.mkdir(parents=True, exist_ok=True)
    catalog = build_catalog(conn)
    by_cat = Counter(c for ch in catalog for c in ch["categories"])
    doc = {
        "generated_at": utc_now(),
        "channels_total": len(catalog),
        "categories": dict(by_cat.most_common()),
        "channels": catalog,
    }
    files = ["channels.json", "channels.csv"]
    write_atomic(out_dir / "channels.json", json.dumps(doc, ensure_ascii=False, indent=2))
    write_atomic(out_dir / "channels.csv", _csv(catalog))
    entries = 0
    if with_playlist:
        text, entries = _playlist(conn, catalog)
        write_atomic(out_dir / "playlist.m3u", text)
        files.append("playlist.m3u")
    return ExportResult(out_dir, files, len(catalog), entries)


def _health(counts: Counter) -> str:
    """ok: a checked stream works; grey: only unchecked or flapping ones; down: none left."""
    if counts.get("active"):
        return "ok"
    if counts.get("new") or counts.get("unverified") or counts.get("suspect"):
        return "grey"
    return "down"


def _current_attrs(conn: sqlite3.Connection) -> dict[str, dict[str, str]]:
    """M3U attributes of each stream as its source's latest applied snapshot lists them."""
    rows = conn.execute(
        "SELECT r.stream_id, r.normalized_payload FROM source_records r"
        " JOIN source_snapshots sn ON sn.id = r.snapshot_id"
        " WHERE r.stream_id IS NOT NULL AND sn.id = ("
        "   SELECT id FROM source_snapshots WHERE source_id = sn.source_id"
        "   AND status = 'applied' ORDER BY fetched_at DESC, rowid DESC LIMIT 1)"
        " ORDER BY r.position"
    )
    out: dict[str, dict[str, str]] = {}
    for r in rows:
        out.setdefault(r["stream_id"], json.loads(r["normalized_payload"]).get("attrs", {}))
    return out


def _epg_categories(conn: sqlite3.Connection) -> dict[str, Counter]:
    out: dict[str, Counter] = defaultdict(Counter)
    for r in conn.execute(
        "SELECT f.channel_id, p.categories FROM airings a"
        " JOIN programmes p ON p.id = a.programme_id"
        " JOIN epg_mappings m ON m.epg_channel_id = a.epg_channel_id"
        " JOIN feeds f ON f.id = m.feed_id"
        " WHERE m.decision IN ('auto', 'approved') AND a.status = 'scheduled'"
    ):
        for cat in json.loads(r["categories"] or "[]"):
            text = cat[1] if isinstance(cat, list) else cat
            if text:
                out[r["channel_id"]][text] += 1
    return out


def _csv(catalog: list[dict]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["id", "name", "categories", "groups", "countries", "languages", "health",
                "streams_playable", "has_epg", "sources", "tvg_ids", "logo_url"])  # fmt: skip
    for ch in catalog:
        w.writerow([ch["id"], ch["name"], ";".join(ch["categories"]), ";".join(ch["groups"]),
                    ";".join(ch["countries"]), ";".join(ch["languages"]), ch["health"],
                    ch["streams_playable"], int(ch["has_epg"]), ";".join(ch["sources"]),
                    ";".join(ch["tvg_ids"]), ch["logo_url"] or ""])  # fmt: skip
    # BOM so Excel opens Cyrillic and Vietnamese correctly.
    return "﻿" + buf.getvalue()


def _playlist(conn: sqlite3.Connection, catalog: list[dict]) -> tuple[str, int]:
    lines, n = ["#EXTM3U"], 0
    for ch in catalog:
        for f in ch["feeds"]:
            ids = pick_streams(conn, Target("channel", ch["id"], f["id"]))
            # pick_streams ranks the named feed first; only take a stream of this feed.
            mine = {s["id"] for s in f["streams"]}
            best = next((i for i in ids if i in mine), None)
            entry = m3u_entry(conn, ch, f, best) if best else None
            if entry:
                lines.extend(entry)
                n += 1
    return "\n".join(lines) + "\n", n


def m3u_entry(conn: sqlite3.Connection, ch: dict, feed: dict, stream_id: str) -> list[str] | None:
    """#EXTINF, player options and URL of one stream of a catalog channel's feed."""
    loc = conn.execute(
        "SELECT url, headers FROM stream_locators WHERE stream_id = ?", (stream_id,)
    ).fetchone()
    if loc is None:
        return None
    name = ch["name"]
    if feed["region"] and fold(feed["region"]) not in fold(name):
        name += f" ({feed['region'].upper()})"
    if feed["offset_minutes"]:
        name += f" {feed['offset_minutes'] // 60:+d}"
    attrs = {"tvg-id": ch["tvg_ids"][0] if ch["tvg_ids"] else "",
             "tvg-name": name, "tvg-logo": ch["logo_url"] or "",
             "tvg-country": ";".join(ch["countries"]),
             "tvg-language": ";".join(ch["languages"]),
             "group-title": ";".join(ch["categories"])}  # fmt: skip
    attr_s = " ".join(f'{k}="{_attr(v)}"' for k, v in attrs.items() if v)
    lines = [f"#EXTINF:-1 {attr_s},{name}"]
    for k, v in json.loads(loc["headers"] or "{}").items():
        opt = {"user-agent": "http-user-agent", "referer": "http-referrer",
               "referrer": "http-referrer"}.get(k.casefold())  # fmt: skip
        if opt:
            lines.append(f"#EXTVLCOPT:{opt}={v}")
    lines.append(loc["url"])
    return lines


def _attr(value: str) -> str:
    return value.replace('"', "'").replace("\n", " ")


def write_atomic(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
        os.chmod(tmp, 0o640 if path.suffix == ".m3u" else 0o644)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
