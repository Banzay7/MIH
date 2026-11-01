"""Catalog export to a folder (spec 21, ``export``): channels with categories and details.

Writes three files, each replaced atomically so a player or script reading the folder
never sees half a file:

- ``channels.json``: every channel with its categories, languages, feeds, stream health
  and EPG coverage. No stream URLs.
- ``channels.csv``: the same, one row per channel, for a spreadsheet.
- ``playlist.m3u``: one playable entry per feed (the best live stream), grouped by
  category. It carries stream URLs, so it is skipped with ``with_playlist=False``.

Categories come from weighted signals (directory, playlist groups, name, the guide's
airtime by genre), see mih.categories: the primary one first. Raw group titles are kept
as they are in ``groups``.
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

from mih.categories import decide, directory_categories, guide_mix
from mih.identity.normalize import fold
from mih.ids import utc_now
from mih.playback import Target, pick_streams

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


def build_catalog(conn: sqlite3.Connection) -> list[dict]:
    """Every channel with categories and details, sorted by name. No stream URLs."""
    attrs = _current_attrs(conn)
    mixes = guide_mix(conn)
    has_epg = {r[0] for r in conn.execute(
        "SELECT DISTINCT f.channel_id FROM epg_mappings m JOIN feeds f ON f.id = m.feed_id"
        " WHERE m.decision IN ('auto', 'approved')"
    )}  # fmt: skip
    sources = {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM sources")}
    directory = {r["id"]: r for r in conn.execute(
        "SELECT id, country, categories, languages, is_nsfw, closed, logo_url"
        " FROM directory_channels WHERE id IN (SELECT directory_id FROM channels)"
    )}  # fmt: skip

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
        "SELECT id, canonical_name, country, logo_url, status, directory_id FROM channels"
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

        d = directory.get(ch["directory_id"])
        from_dir = []
        if d is not None:
            from_dir = directory_categories(json.loads(d["categories"] or "[]"), d["is_nsfw"])
            langs.update(json.loads(d["languages"] or "[]"))
            if d["country"]:
                countries.add(d["country"])
        mix = mixes.get(ch["id"])
        top_epg = [c for c, _ in mix.raw.most_common(5)] if mix else []
        decision = decide(directory=from_dir, groups=groups,
                          names=[ch["canonical_name"], *names[ch["id"]]],
                          guide=mix.shares() if mix else None)  # fmt: skip
        playable = sum(n for st, n in status_counts.items()
                       if st not in ("quarantine", "archived", "absent"))  # fmt: skip
        out.append({
            "id": ch["id"],
            "name": ch["canonical_name"],
            "categories": decision.categories,
            "category_source": decision.source,
            "category_scores": decision.scores,
            "category_reasons": decision.reasons,
            "groups": groups,
            "epg_categories": top_epg,
            "countries": sorted(countries),
            "languages": sorted(langs),
            "logo_url": ch["logo_url"] or (d["logo_url"] if d is not None else None),
            "directory_id": ch["directory_id"],
            "closed": d["closed"] if d is not None else None,
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
    """ok: a checked stream works; grey: only unchecked or flapping ones; down: none left;
    none: no stream at all (a channel known only from the directory)."""
    if not counts:
        return "none"
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


def m3u_entry(
    conn: sqlite3.Connection, ch: dict, feed: dict, stream_id: str, *, tvg_id: str | None = None
) -> list[str] | None:
    """#EXTINF, player options and URL of one stream of a catalog channel's feed.
    ``tvg_id`` overrides the channel's own, to match a guide written with the list."""
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
    attrs = {"tvg-id": tvg_id or (ch["tvg_ids"][0] if ch["tvg_ids"] else ""),
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


def write_atomic(path: Path, text: str, *, private: bool = False) -> None:
    """Replace ``path`` in one step. Files with stream URLs (.m3u, or ``private``) are not
    readable by other users."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
        os.chmod(tmp, 0o640 if private or path.suffix == ".m3u" else 0o644)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
