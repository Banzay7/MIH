"""Validated channel lists: only channels that play now, filtered by need.

A stream's level says how sure we are that it plays:

- ``ok``: a counted check or a device started it within ``fresh_hours`` (48 h) and
  nothing has failed since;
- ``doubtful``: it worked within ``stale_days`` (7 days) but is failing now, or the last
  success is older than ``fresh_hours``;
- ``unknown``: never checked yet;
- ``dead``: never worked, failing for longer, quarantined, gone from its playlist, or
  needs something we do not do (DRM, a login, a country).

A feed's level is that of its best stream and a channel's that of its best feed. A list
takes the channels at or above ``min_level`` and, for each of their feeds, the best
stream. Channels that match the filter but did not make it are reported with the
reason, so a missing channel is explained rather than silently gone.

Geo blocks: a server outside the channel's country often gets HTTP 403 or no answer
at all. Those count as failures like any other: the viewer is usually outside that
country too, so a stream the server cannot open would not play at home either. The
report marks them ``geo_suspect`` so they can be told apart from streams that are gone.
"""

from __future__ import annotations

import csv
import io
import json
import re
import sqlite3
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from mih.export import build_catalog, m3u_entry, write_atomic
from mih.health.policy import parse_ts
from mih.identity.normalize import fold
from mih.ids import utc_now
from mih.playback import Target, pick_streams

LEVELS = ("ok", "doubtful", "unknown", "dead")

# Availability reasons -> report reason.
_REASONS = {
    "source_absent": "absent",
    "drm_unsupported": "drm",
    "authentication_required": "login",
    "blocked_by_geo": "geo",
    "policy_excluded": "excluded",
}

# Language names playlists use -> ISO 639-1. Codes pass through as they are.
_LANGS = {
    "russian": "ru", "русский": "ru", "rus": "ru",
    "english": "en", "eng": "en",
    "vietnamese": "vi", "tieng viet": "vi", "vie": "vi",
    "ukrainian": "uk", "украинский": "uk", "ukr": "uk",
    "belarusian": "be", "белорусский": "be", "bel": "be",
    "kazakh": "kk", "казахский": "kk", "kaz": "kk",
    "german": "de", "deu": "de", "ger": "de",
    "french": "fr", "fra": "fr", "fre": "fr",
    "spanish": "es", "spa": "es",
    "chinese": "zh", "zho": "zh", "chi": "zh",
    "tatar": "tt", "татарский": "tt", "tat": "tt",
    "armenian": "hy", "hye": "hy", "arm": "hy",
    "georgian": "ka", "kat": "ka", "geo": "ka",
    "azerbaijani": "az", "aze": "az",
    "uzbek": "uz", "uzb": "uz",
}  # fmt: skip

# iptv-org ids look like "Perviykanal.ru@SD": the suffix is the channel's country.
_TVG_COUNTRY = re.compile(r"^[^.@]+\.([a-z]{2})(?:@|$)", re.IGNORECASE)


@dataclass(frozen=True)
class ListFilter:
    countries: tuple[str, ...] = ()
    languages: tuple[str, ...] = ()
    categories: tuple[str, ...] = ()
    exclude_categories: tuple[str, ...] = ("adult",)
    min_level: str = "ok"
    timeshift: bool = False
    epg_only: bool = False

    def __post_init__(self) -> None:
        if self.min_level not in LEVELS[:3]:
            raise ValueError("min_level must be ok, doubtful or unknown")


@dataclass(frozen=True)
class Freshness:
    fresh_hours: int = 48
    stale_days: int = 7


DEFAULT_FRESHNESS = Freshness()


@dataclass
class ListResult:
    name: str
    files: list[str]
    channels: int
    entries: int
    excluded: dict[str, int] = field(default_factory=dict)

    def stats(self) -> dict:
        return asdict(self)


def normalize_lang(value: str) -> str:
    v = fold(value).strip()
    return _LANGS.get(v, v)


def normalize_country(value: str) -> str:
    v = value.strip().upper()
    return "GB" if v == "UK" else v


def stream_level(
    s: sqlite3.Row | dict, now: datetime, fresh: Freshness = DEFAULT_FRESHNESS
) -> tuple[str, str | None]:
    """(level, reason). The reason says what keeps the stream below ``ok``."""
    reason = s["availability_reason"]
    if reason:
        return "dead", _REASONS.get(reason, reason)
    if s["status"] in ("quarantine", "archived"):
        return "dead", "failing"
    ok = parse_ts(s["last_ok_at"])
    if ok is None:
        return ("unknown", "unchecked") if s["status"] == "unverified" else ("dead", "never_worked")
    dev_fail = parse_ts(s["last_device_fail_at"])
    age = now - ok
    if age > timedelta(days=fresh.stale_days):
        return "dead", "failing"
    if dev_fail is not None and dev_fail > ok:
        return "doubtful", "device_failed"
    if s["streak_failures"]:
        return "doubtful", "failing"
    if age > timedelta(hours=fresh.fresh_hours):
        return "doubtful", "stale"
    return "ok", None


def failure_hint(error_kind: str | None, http_status: int | None) -> str | None:
    """What the last failed check most likely means."""
    if http_status in (403, 451):
        return "geo_suspect"
    if error_kind in ("timeout", "connect", "network"):
        return "no_answer"
    if error_kind == "dns" or http_status in (404, 410):
        return "gone"
    return error_kind


def build_list(
    conn: sqlite3.Connection,
    flt: ListFilter,
    *,
    now: datetime | None = None,
    fresh: Freshness = DEFAULT_FRESHNESS,
    catalog: list[dict] | None = None,
) -> dict:
    """The list document: included channels with the stream picked per feed, and the
    channels that match the filter but were left out, with the reason. No URLs."""
    now = now or datetime.now().astimezone()
    catalog = build_catalog(conn) if catalog is None else catalog
    streams = {r["id"]: r for r in conn.execute(
        "SELECT id, status, availability_reason, last_ok_at, last_device_fail_at,"
        " streak_failures FROM streams"
    )}  # fmt: skip
    last_err = {r["stream_id"]: failure_hint(r["error_kind"], r["http_status"])
                for r in conn.execute(
        "SELECT stream_id, error_kind, http_status FROM ("
        "  SELECT stream_id, error_kind, http_status, row_number() OVER ("
        "    PARTITION BY stream_id ORDER BY checked_at DESC) AS rn"
        "  FROM stream_observations WHERE success = 0 AND counted = 1) WHERE rn = 1"
    )}  # fmt: skip
    min_rank = LEVELS.index(flt.min_level)

    included, excluded = [], []
    for ch in catalog:
        enrich(ch)
        if not matches(ch, flt):
            continue
        order = {sid: i for i, sid in enumerate(pick_streams(conn, Target("channel", ch["id"])))}
        feeds_out, best = [], None
        for f in ch["feeds"]:
            if f["offset_minutes"] and not flt.timeshift:
                continue
            ranked = []
            for s in f["streams"]:
                level, reason = stream_level(streams[s["id"]], now, fresh)
                # The last error explains a failing stream, not a DRM or a missing one.
                failing = reason in ("failing", "never_worked", "stale", "device_failed")
                hint = last_err.get(s["id"]) if failing else None
                key = (LEVELS.index(level), order.get(s["id"], len(order)))
                ranked.append((key, s, level, reason, hint))
            if not ranked:
                continue
            ranked.sort(key=lambda r: r[0])
            top = ranked[0]
            if best is None or top[0] < best[0]:
                best = top
            if top[0][0] <= min_rank:
                feeds_out.append({"feed": f, "stream_id": top[1]["id"], "level": top[2],
                                  "last_ok_at": top[1]["last_ok_at"]})  # fmt: skip
        if feeds_out:
            level = min((x["level"] for x in feeds_out), key=LEVELS.index)
            included.append({"channel": ch, "level": level, "feeds": feeds_out})
        else:
            reason = best[3] if best else "no_stream"
            excluded.append({"id": ch["id"], "name": ch["name"],
                             "level": best[2] if best else "dead", "reason": reason,
                             "last_error": best[4] if best else None})  # fmt: skip

    return {
        "generated_at": utc_now(),
        "filter": asdict(flt),
        "freshness": asdict(fresh),
        "channels_total": len(included),
        "entries": sum(len(x["feeds"]) for x in included),
        "by_level": dict(Counter(x["level"] for x in included)),
        "excluded_by_reason": dict(
            Counter(e["last_error"] or e["reason"] for e in excluded).most_common()
        ),  # fmt: skip
        "channels": [_channel_out(x) for x in included],
        "excluded": excluded,
        "_included": included,
    }


def enrich(ch: dict) -> None:
    """Normalized countries and languages, with the country an iptv-org id carries."""
    countries = {normalize_country(c) for c in ch["countries"] if c}
    for tid in ch["tvg_ids"]:
        m = _TVG_COUNTRY.match(tid)
        if m:
            countries.add(normalize_country(m.group(1)))
    ch["countries"] = sorted(countries)
    ch["languages"] = sorted({normalize_lang(x) for x in ch["languages"] if x})


def matches(ch: dict, flt: ListFilter) -> bool:
    if flt.countries and not {normalize_country(c) for c in flt.countries} & set(ch["countries"]):
        return False
    if flt.languages and not {normalize_lang(x) for x in flt.languages} & set(ch["languages"]):
        return False
    cats = set(ch["categories"])
    if flt.categories and not set(flt.categories) & cats:
        return False
    if set(flt.exclude_categories) & cats:
        return False
    return not (flt.epg_only and not ch["has_epg"])


def write_list(
    conn: sqlite3.Connection,
    out_dir: Path,
    name: str,
    flt: ListFilter,
    *,
    epg_url: str | None = None,
    with_playlist: bool = True,
    now: datetime | None = None,
    fresh: Freshness = DEFAULT_FRESHNESS,
) -> ListResult:
    """Write <name>.json, <name>.csv and, unless told not to, <name>.m3u (stream URLs)."""
    if not re.fullmatch(r"[\w.-]+", name):
        raise ValueError("list name may contain letters, digits, '.', '_' and '-' only")
    out_dir.mkdir(parents=True, exist_ok=True)
    doc = build_list(conn, flt, now=now, fresh=fresh)
    included = doc.pop("_included")
    files = [f"{name}.json", f"{name}.csv"]
    write_atomic(out_dir / files[0], json.dumps(doc, ensure_ascii=False, indent=2))
    write_atomic(out_dir / files[1], _csv(doc["channels"]))
    if with_playlist:
        write_atomic(out_dir / f"{name}.m3u", playlist(conn, included, epg_url=epg_url))
        files.append(f"{name}.m3u")
    return ListResult(name, files, doc["channels_total"], doc["entries"],
                      doc["excluded_by_reason"])  # fmt: skip


def playlist(conn: sqlite3.Connection, included: list[dict], *, epg_url: str | None) -> str:
    head = "#EXTM3U"
    if epg_url:
        head += f' url-tvg="{epg_url}" x-tvg-url="{epg_url}"'
    lines = [head]
    for x in included:
        for f in x["feeds"]:
            entry = m3u_entry(conn, x["channel"], f["feed"], f["stream_id"])
            if entry:
                lines.extend(entry)
    return "\n".join(lines) + "\n"


def _channel_out(x: dict) -> dict:
    ch = x["channel"]
    return {
        "id": ch["id"],
        "name": ch["name"],
        "level": x["level"],
        "categories": ch["categories"],
        "countries": ch["countries"],
        "languages": ch["languages"],
        "logo_url": ch["logo_url"],
        "tvg_ids": ch["tvg_ids"],
        "has_epg": ch["has_epg"],
        "last_ok_at": max((f["last_ok_at"] or "" for f in x["feeds"]), default="") or None,
        "feeds": [{"id": f["feed"]["id"], "region": f["feed"]["region"],
                   "offset_minutes": f["feed"]["offset_minutes"], "level": f["level"]}
                  for f in x["feeds"]],
    }  # fmt: skip


def _csv(channels: list[dict]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["id", "name", "level", "categories", "countries", "languages", "has_epg",
                "feeds", "last_ok_at"])  # fmt: skip
    for ch in channels:
        w.writerow([ch["id"], ch["name"], ch["level"], ";".join(ch["categories"]),
                    ";".join(ch["countries"]), ";".join(ch["languages"]), int(ch["has_epg"]),
                    len(ch["feeds"]), ch["last_ok_at"] or ""])  # fmt: skip
    # BOM so Excel opens Cyrillic and Vietnamese correctly.
    return "﻿" + buf.getvalue()
