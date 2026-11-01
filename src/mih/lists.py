"""Validated channel lists: only channels that play now, filtered by need.

A stream's level says how sure we are that it plays:

- ``ok``: a counted check or a device started it within ``fresh_hours`` (10 days) and
  nothing has failed since; a working stream is checked weekly, so a live one always
  has a success that recent;
- ``doubtful``: it worked within ``stale_days`` (14 days) but is failing now, or the last
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

Next to the lists, ``catalog.jsonl`` holds everything in one file: every catalog channel
on one line with all its streams (URL and request headers included), levels, the lists
it is in and what is on now. ``catalog.csv`` is the same, one row per stream.
"""

from __future__ import annotations

import csv
import io
import json
import re
import sqlite3
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from mih.epg.publish import GuideEntry, guide_coverage, write_guide
from mih.export import build_catalog, m3u_entry, write_atomic
from mih.health.policy import parse_ts
from mih.identity.normalize import fold
from mih.identity.search_keys import search_keys
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
    # ISO 639-2/3 codes (iptv-org, XMLTV guides) of other common languages.
    "jpn": "ja", "kor": "ko", "ara": "ar", "hin": "hi", "ita": "it", "por": "pt",
    "pol": "pl", "tur": "tr", "nld": "nl", "dut": "nl", "tha": "th", "ind": "id",
    "fas": "fa", "per": "fa", "heb": "he", "ell": "el", "gre": "el", "ces": "cs",
    "cze": "cs", "ron": "ro", "rum": "ro", "hun": "hu", "swe": "sv", "fin": "fi",
    "nor": "no", "nob": "no", "dan": "da", "bul": "bg", "srp": "sr", "hrv": "hr",
    "slk": "sk", "slo": "sk", "slv": "sl", "lit": "lt", "lav": "lv", "est": "et",
    "khm": "km", "lao": "lo", "mya": "my", "bur": "my", "msa": "ms", "may": "ms",
    "fil": "tl", "tgl": "tl", "urd": "ur", "ben": "bn", "tam": "ta", "tel": "te",
    "mon": "mn", "kir": "ky", "tgk": "tg", "tuk": "tk", "mkd": "mk", "mac": "mk",
    "sqi": "sq", "alb": "sq", "bos": "bs", "cat": "ca", "eus": "eu", "baq": "eu",
    "glg": "gl", "isl": "is", "ice": "is", "gle": "ga", "cym": "cy", "wel": "cy",
    "mlt": "mt", "amh": "am", "swa": "sw", "som": "so", "pus": "ps", "kur": "ku",
    "nep": "ne", "sin": "si", "mal": "ml", "kan": "kn", "mar": "mr", "guj": "gu",
    "pan": "pa", "yue": "zh", "cmn": "zh", "hat": "ht", "hau": "ha", "yor": "yo",
    "japanese": "ja", "korean": "ko", "arabic": "ar", "hindi": "hi", "italian": "it",
    "portuguese": "pt", "polish": "pl", "turkish": "tr", "dutch": "nl", "thai": "th",
    "indonesian": "id", "persian": "fa", "hebrew": "he", "greek": "el", "czech": "cs",
    "romanian": "ro", "hungarian": "hu", "swedish": "sv", "finnish": "fi",
    "norwegian": "no", "danish": "da", "bulgarian": "bg", "serbian": "sr",
    "croatian": "hr", "khmer": "km", "malay": "ms", "mongolian": "mn",
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
    fresh_hours: int = 10 * 24
    stale_days: int = 14


DEFAULT_FRESHNESS = Freshness()


@dataclass
class ListResult:
    name: str
    files: list[str]
    channels: int
    entries: int
    excluded: dict[str, int] = field(default_factory=dict)
    # Channels whose guide (<name>.xml.gz) has programmes.
    with_guide: int = 0
    channel_ids: list[str] = field(default_factory=list, repr=False)

    def stats(self) -> dict:
        out = asdict(self)
        del out["channel_ids"]
        return out


def normalize_lang(value: str) -> str:
    """Russian, rus, ru-RU and ru_RU are all ru."""
    v = fold(value).strip().replace("_", "-")
    base = v.split("-", 1)[0] if re.fullmatch(r"[a-z]{2,3}-[a-z0-9]{2,8}", v) else v
    return _LANGS.get(base, base)


def store_tags(conn: sqlite3.Connection, catalog: list[dict]) -> int:
    """Write each channel's categories and languages to channel_tags for the search.
    Called after every catalog build, so the tags follow the rules in mih.export."""
    rows = {(ch["id"], "category", c) for ch in catalog for c in ch["categories"] if c != "other"}
    rows |= {(ch["id"], "language", code) for ch in catalog for lang in ch["languages"]
             if (code := normalize_lang(lang)) and len(code) <= 3}  # fmt: skip
    own = not conn.in_transaction
    if own:
        conn.execute("BEGIN")
    try:
        conn.execute("DELETE FROM channel_tags")
        conn.executemany("INSERT OR IGNORE INTO channel_tags (channel_id, kind, value)"
                         " VALUES (?, ?, ?)", sorted(rows))  # fmt: skip
        if own:
            conn.execute("COMMIT")
    except Exception:
        if own:
            conn.execute("ROLLBACK")
        raise
    return len(rows)


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
    streams, last_err = _stream_rows(conn), _last_errors(conn)
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
            ranked = [_rank(s, streams, last_err, order, now, fresh) for s in f["streams"]]
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


def _stream_rows(conn: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    return {r["id"]: r for r in conn.execute(
        "SELECT id, status, availability_reason, last_ok_at, last_device_ok_at,"
        " last_device_fail_at, streak_failures FROM streams"
    )}  # fmt: skip


def _last_errors(conn: sqlite3.Connection) -> dict[str, str | None]:
    return {r["stream_id"]: failure_hint(r["error_kind"], r["http_status"])
            for r in conn.execute(
        "SELECT stream_id, error_kind, http_status FROM ("
        "  SELECT stream_id, error_kind, http_status, row_number() OVER ("
        "    PARTITION BY stream_id ORDER BY checked_at DESC) AS rn"
        "  FROM stream_observations WHERE success = 0 AND counted = 1) WHERE rn = 1"
    )}  # fmt: skip


def _rank(s: dict, streams: dict, last_err: dict, order: dict, now: datetime,
          fresh: Freshness) -> tuple:  # fmt: skip
    """(sort key, stream, level, reason, last error) of one catalog stream."""
    level, reason = stream_level(streams[s["id"]], now, fresh)
    # The last error explains a failing stream, not a DRM or a missing one.
    failing = reason in ("failing", "never_worked", "stale", "device_failed")
    hint = last_err.get(s["id"]) if failing else None
    return (LEVELS.index(level), order.get(s["id"], len(order))), s, level, reason, hint


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


# How far back a list's guide goes; players show it as the past of the timeline.
GUIDE_PAST = timedelta(days=1)
# A list whose channels changed waits this long before its guide is rewritten.
GUIDE_MIN_INTERVAL = timedelta(minutes=30)


def write_list(
    conn: sqlite3.Connection,
    out_dir: Path,
    name: str,
    flt: ListFilter,
    *,
    epg_url: str | None = None,
    with_playlist: bool = True,
    with_guide: bool = True,
    guide_max_age: timedelta = timedelta(0),
    now: datetime | None = None,
    fresh: Freshness = DEFAULT_FRESHNESS,
    catalog: list[dict] | None = None,
) -> ListResult:
    """Write <name>.json, <name>.csv, <name>.xml.gz (the guide of the list, XMLTV) and,
    unless told not to, <name>.m3u (stream URLs). The playlist's tvg-id values are the
    guide's channel ids.

    The guide is rewritten when the file is older than ``guide_max_age``, or when the
    list's channels changed and the file is older than GUIDE_MIN_INTERVAL: stream checks
    rebuild the lists every few minutes, guides change twice a day."""
    if not re.fullmatch(r"[\w.-]+", name):
        raise ValueError("list name may contain letters, digits, '.', '_' and '-' only")
    out_dir.mkdir(parents=True, exist_ok=True)
    doc = build_list(conn, flt, now=now, fresh=fresh, catalog=catalog)
    included = doc.pop("_included")
    entries = guide_entries(included)
    files = [f"{name}.json", f"{name}.csv"]
    since = (now or datetime.now(UTC)) - GUIDE_PAST
    covered: set[str] | None = None
    if with_guide:
        guide_path = out_dir / f"{name}.xml.gz"
        pairs = sorted(f"{e.guide_id}={e.feed_id}" for e in entries)
        signature = ",".join(pairs)
        written = (_read_json(out_dir / files[0]).get("guide") or {}).get("signature")
        age = _age(guide_path) if guide_path.exists() else None
        if (
            age is None
            or age >= guide_max_age
            or (written != signature and age >= GUIDE_MIN_INTERVAL)
        ):
            covered = write_guide(conn, guide_path, entries, since=since).with_guide
            written = signature
        else:
            # The file is the older one: only entries it has count as covered.
            in_file = set((written or "").split(","))
            covered = {e.guide_id for e in entries if f"{e.guide_id}={e.feed_id}" in in_file}
            covered &= guide_coverage(conn, entries, since=since)
        files.append(guide_path.name)
        doc["guide"] = {"file": guide_path.name, "signature": written}
    if covered is None:
        covered = guide_coverage(conn, entries, since=since)
    with_programmes = {e.channel_id for e in entries if e.guide_id in covered}
    for ch in doc["channels"]:
        ch["guide"] = ch["id"] in with_programmes
    doc["with_guide"] = len(with_programmes)
    write_atomic(out_dir / files[0], json.dumps(doc, ensure_ascii=False, indent=2))
    write_atomic(out_dir / files[1], _csv(doc["channels"]))
    if with_playlist:
        write_atomic(out_dir / f"{name}.m3u", playlist(conn, included, epg_url=epg_url))
        files.append(f"{name}.m3u")
    return ListResult(name, files, doc["channels_total"], doc["entries"],
                      doc["excluded_by_reason"], with_guide=len(with_programmes),
                      channel_ids=[x["id"] for x in doc["channels"]])  # fmt: skip


def guide_entries(included: list[dict]) -> list[GuideEntry]:
    """One guide channel per playlist entry. The base feed keeps the channel's tvg-id
    (players and other guides know it); other versions add their region or offset."""
    out, seen = [], set()
    for x in included:
        ch = x["channel"]
        base = ch["tvg_ids"][0] if ch["tvg_ids"] else f"mih.{ch['id']}"
        for f in x["feeds"]:
            feed = f["feed"]
            gid = base
            if feed["region"]:
                gid += "." + re.sub(r"\W+", "-", fold(feed["region"])).strip("-")
            if feed["offset_minutes"]:
                gid += f"{feed['offset_minutes'] // 60:+d}"
            while gid in seen:  # two channels sharing a tvg-id
                gid += "~"
            seen.add(gid)
            out.append(GuideEntry(gid, entry_name(ch, feed), ch["logo_url"], feed["id"],
                                  ch["id"]))  # fmt: skip
            f["guide_id"] = gid
    return out


def entry_name(ch: dict, feed: dict) -> str:
    name = ch["name"]
    if feed["region"] and fold(feed["region"]) not in fold(name):
        name += f" ({feed['region'].upper()})"
    if feed["offset_minutes"]:
        name += f" {feed['offset_minutes'] // 60:+d}"
    return name


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _age(path: Path) -> timedelta:
    return datetime.now(UTC) - datetime.fromtimestamp(path.stat().st_mtime, UTC)


def playlist(conn: sqlite3.Connection, included: list[dict], *, epg_url: str | None) -> str:
    head = "#EXTM3U"
    if epg_url:
        head += f' url-tvg="{epg_url}" x-tvg-url="{epg_url}"'
    lines = [head]
    for x in included:
        for f in x["feeds"]:
            entry = m3u_entry(conn, x["channel"], f["feed"], f["stream_id"],
                              tvg_id=f.get("guide_id"))  # fmt: skip
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
        "guide": False,
        "last_ok_at": max((f["last_ok_at"] or "" for f in x["feeds"]), default="") or None,
        "feeds": [{"id": f["feed"]["id"], "region": f["feed"]["region"],
                   "offset_minutes": f["feed"]["offset_minutes"], "level": f["level"]}
                  for f in x["feeds"]],
    }  # fmt: skip


def _csv(channels: list[dict]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["id", "name", "level", "categories", "countries", "languages", "has_epg",
                "guide", "feeds", "last_ok_at"])  # fmt: skip
    for ch in channels:
        w.writerow([ch["id"], ch["name"], ch["level"], ";".join(ch["categories"]),
                    ";".join(ch["countries"]), ";".join(ch["languages"]), int(ch["has_epg"]),
                    int(ch["guide"]), len(ch["feeds"]), ch["last_ok_at"] or ""])  # fmt: skip
    # BOM so Excel opens Cyrillic and Vietnamese correctly.
    return "﻿" + buf.getvalue()


# ---------- Presets ----------

DEFAULT_PRESETS = {"valid": {}}
_PRESET_KEYS = {"country", "lang", "category", "exclude_category", "level", "timeshift",
                "adult", "epg_only", "epg_url"}  # fmt: skip


def load_presets(path: Path | None) -> dict[str, dict]:
    """Named lists from a JSON file, e.g. {"ru-news": {"country": ["RU"], "category":
    ["news"]}}. Without a file there is one list, "valid": every channel that plays."""
    if path is None:
        return DEFAULT_PRESETS
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read lists file: {exc}") from exc
    if not isinstance(data, dict) or not data:
        raise ValueError("lists file must be a JSON object of named lists")
    for name, spec in data.items():
        if not re.fullmatch(r"[\w.-]+", name) or name == CATALOG or not isinstance(spec, dict):
            raise ValueError(f"bad list {name!r}")
        unknown = set(spec) - _PRESET_KEYS
        if unknown:
            raise ValueError(f"list {name!r}: unknown keys {sorted(unknown)}")
        preset_filter(spec)  # validates the level
    return data


def preset_filter(spec: dict) -> ListFilter:
    def many(key: str) -> tuple[str, ...]:
        v = spec.get(key, [])
        return tuple([v] if isinstance(v, str) else v)

    excluded = many("exclude_category") + (() if spec.get("adult") else ("adult",))
    return ListFilter(countries=many("country"), languages=many("lang"),
                      categories=many("category"), exclude_categories=excluded,
                      min_level=spec.get("level", "ok"), timeshift=bool(spec.get("timeshift")),
                      epg_only=bool(spec.get("epg_only")))  # fmt: skip


def build_presets(
    conn: sqlite3.Connection,
    out_dir: Path,
    presets: dict[str, dict],
    *,
    public_url: str | None = None,
    token: str | None = None,
    guide_max_age: timedelta = timedelta(0),
) -> list[ListResult]:
    """Write every named list into ``out_dir``, then the whole catalog with the lists
    each channel is in. The catalog is built once for all.

    A list's playlist points players to its own guide (url-tvg) when the service knows
    its public address (MIH_PUBLIC_URL), unless the list names another ``epg_url``."""
    catalog = build_catalog(conn)
    store_tags(conn, catalog)
    built = []
    for name, spec in presets.items():
        epg_url = spec.get("epg_url")
        if not epg_url and public_url:
            epg_url = f"{public_url}/lists/{name}.xml.gz" + (f"?token={token}" if token else "")
        built.append(write_list(conn, out_dir, name, preset_filter(spec), epg_url=epg_url,
                                guide_max_age=guide_max_age, catalog=catalog))  # fmt: skip
    members: dict[str, list[str]] = {}
    for r in built:
        for cid in r.channel_ids:
            members.setdefault(cid, []).append(r.name)
    write_catalog(conn, out_dir, catalog, members)
    return built


# ---------- Everything in one file ----------

CATALOG = "catalog"  # catalog.jsonl and catalog.csv; no list may take the name
_HEADER_KEYS = {"user-agent": "user_agent", "referer": "referer", "referrer": "referer"}


def write_catalog(
    conn: sqlite3.Connection,
    out_dir: Path,
    catalog: list[dict],
    members: dict[str, list[str]],
    *,
    now: datetime | None = None,
    fresh: Freshness = DEFAULT_FRESHNESS,
) -> list[str]:
    """Every catalog channel with every stream, one JSON object per line, and the same
    as a CSV with one row per stream. Both carry stream URLs and are kept private."""
    now = now or datetime.now().astimezone()
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = catalog_rows(conn, catalog, members, now=now, fresh=fresh)
    lines = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
    files = [f"{CATALOG}.jsonl", f"{CATALOG}.csv"]
    write_atomic(out_dir / files[0], lines, private=True)
    write_atomic(out_dir / files[1], _catalog_csv(rows), private=True)
    return files


def catalog_rows(
    conn: sqlite3.Connection,
    catalog: list[dict],
    members: dict[str, list[str]],
    *,
    now: datetime,
    fresh: Freshness = DEFAULT_FRESHNESS,
) -> list[dict]:
    # Imported here: the guide importer imports the directory, which imports this module.
    from mih.epg.guide import feed_guides, now_and_next

    streams, last_err = _stream_rows(conn), _last_errors(conn)
    locators = {r["stream_id"]: r for r in conn.execute(
        "SELECT stream_id, url, headers FROM stream_locators")}  # fmt: skip
    guides = feed_guides(conn)
    on_air = now_and_next(conn, now, guides) if guides else {}
    out = []
    for ch in catalog:
        enrich(ch)
        order = {sid: i for i, sid in enumerate(pick_streams(conn, Target("channel", ch["id"])))}
        ranked = sorted(
            (_rank(s, streams, last_err, order, now, fresh) + (f,)
             for f in ch["feeds"] for s in f["streams"]),
            key=lambda r: r[0],
        )  # fmt: skip
        best = ranked[0] if ranked else None
        # What is on now, from the first feed without a time shift that has a guide.
        by_shift = sorted(ch["feeds"], key=lambda f: (f["offset_minutes"] != 0,
                                                      f["offset_minutes"]))  # fmt: skip
        guide = next((on_air[f["id"]] for f in by_shift if f["id"] in on_air), None)
        cur, nxt = (guide or {}).get("now"), (guide or {}).get("next")
        out.append({
            "channel_id": ch["id"],
            "name": ch["name"],
            "aliases": ch["also_known_as"],
            "search_keys": search_keys([ch["name"], *ch["also_known_as"]], ch["tvg_ids"]),
            "tvg_ids": ch["tvg_ids"],
            "country": ch["countries"][0] if ch["countries"] else None,
            "countries": ch["countries"],
            "languages": ch["languages"],
            "categories": ch["categories"],
            "logo_url": ch["logo_url"],
            "level": best[2] if best else "dead",
            "reason": (best[3] if best else "no_stream"),
            "last_error": best[4] if best else None,
            "last_ok_at": max((r[1]["last_ok_at"] or "" for r in ranked), default="") or None,
            "lists": members.get(ch["id"], []),
            "epg_available": ch["has_epg"],
            "now": {"title": cur["title"], "start": cur["start"], "stop": cur["stop"]}
            if cur else None,
            "next": {"title": nxt["title"], "start": nxt["start"]} if nxt else None,
            "streams": [_stream_out(r, streams[r[1]["id"]], locators.get(r[1]["id"]))
                        for r in ranked],
        })  # fmt: skip
    return out


def _stream_out(ranked: tuple, row: sqlite3.Row, loc: sqlite3.Row | None) -> dict:
    _, s, level, reason, hint, f = ranked
    headers = {"user_agent": None, "referer": None}
    for k, v in json.loads((loc["headers"] if loc else None) or "{}").items():
        key = k.casefold()
        headers[_HEADER_KEYS.get(key, key.replace("-", "_"))] = v
    return {
        "stream_id": s["id"],
        "feed_id": f["id"],
        "region": f["region"],
        "timeshift_min": f["offset_minutes"],
        "quality": s["quality"],
        "format": s["format"],
        "source": s["source"],
        "level": level,
        "reason": reason,
        "last_error": hint,
        "last_ok_at": s["last_ok_at"],
        # Last time a TV or player at home reported that it started.
        "home_ok_at": row["last_device_ok_at"],
        "url": loc["url"] if loc else None,
        "headers": headers,
        # A player that cannot send these headers gets HTTP 403 from such a stream.
        "requires_headers": any(headers.values()),
    }


_CATALOG_COLS = ["channel_id", "name", "aliases", "country", "languages", "categories",
                 "channel_level", "reason", "lists", "epg_available", "now", "stream_id",
                 "level", "last_error", "last_ok_at", "timeshift_min", "quality", "format",
                 "requires_headers", "url", "user_agent", "referer"]  # fmt: skip


def _catalog_csv(rows: list[dict]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(_CATALOG_COLS)
    for ch in rows:
        head = [ch["channel_id"], ch["name"], ";".join(ch["aliases"]), ch["country"] or "",
                ";".join(ch["languages"]), ";".join(ch["categories"]), ch["level"],
                ch["reason"] or "", ";".join(ch["lists"]), int(ch["epg_available"]),
                ch["now"]["title"] if ch["now"] else ""]  # fmt: skip
        for s in ch["streams"] or [None]:
            w.writerow(head + ([s["stream_id"], s["level"], s["last_error"] or s["reason"] or "",
                                s["last_ok_at"] or "", s["timeshift_min"], s["quality"] or "",
                                s["format"] or "", int(s["requires_headers"]), s["url"] or "",
                                s["headers"]["user_agent"] or "", s["headers"]["referer"] or ""]
                               if s else [""] * 11))  # fmt: skip
    return "\ufeff" + buf.getvalue()
