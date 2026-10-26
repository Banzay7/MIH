"""Channel directory: what channels exist, apart from whether we have a stream for them.

Source: the iptv-org database (CC0), files ``channels``, ``feeds``, ``logos`` and
``blocklist``, as CSV from the database repository or JSON from its API. It gives each
channel a stable id (``ChannelOne.ru``), alternative names in several scripts, the
country, languages and categories.

Three things use it:

- **linking**: a catalog channel is linked to a directory entry by its tvg-id or,
  failing that, by a name or alternative name that only one directory entry in a
  compatible country carries;
- **merging duplicates**: catalog channels that turn out to be the same channel (one
  tvg-id, or one directory entry) are merged into the oldest, with all their feeds,
  streams and aliases;
- **listing channels without a stream**: for chosen countries, directory channels the
  catalog does not have yet are added as channels without feeds, so the catalog shows
  them and a playlist that carries them later lands on the same channel.

Closed and blocklisted channels are never added; NSFW ones are categorized ``adult``.
"""

from __future__ import annotations

import csv
import io
import json
import sqlite3
from collections import defaultdict
from dataclasses import asdict, dataclass

from mih.config import Settings
from mih.epg.catalog import add_guide_channels
from mih.identity.normalize import split_name, tvg_base
from mih.ids import new_id, utc_now
from mih.lists import normalize_country, normalize_lang
from mih.sources.fetch import fetch_source

DEFAULT_BASE_URL = "https://raw.githubusercontent.com/iptv-org/database/master/data/"
FILES = ("channels", "feeds", "logos", "blocklist")


class DirectoryError(ValueError):
    pass


@dataclass
class DirectoryReport:
    entries: int = 0
    linked: int = 0
    merged: int = 0
    created: int = 0
    from_guides: int = 0

    def stats(self) -> dict:
        return asdict(self)


def fetch_directory(settings: Settings, base_url: str = DEFAULT_BASE_URL) -> dict[str, bytes]:
    """Download the four files; CSV from the database repo or JSON from the API."""
    ext = ".json" if "/api" in base_url else ".csv"
    out = {}
    for name in FILES:
        res = fetch_source(base_url.rstrip("/") + "/" + name + ext, settings)
        out[name] = res.content or b""
    return out


def parse_rows(content: bytes) -> list[dict]:
    """A JSON array or a CSV with a header row. List fields: JSON arrays or ';' in CSV."""
    text = content.decode("utf-8-sig").strip()
    if not text:
        return []
    if text.startswith("["):
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise DirectoryError(f"bad JSON: {exc}") from exc
    return list(csv.DictReader(io.StringIO(text)))


def import_directory(
    conn: sqlite3.Connection, files: dict[str, bytes], *, countries: tuple[str, ...] = ()
) -> DirectoryReport:
    """Replace the directory, link and merge catalog channels, add channels for
    ``countries`` that the catalog does not have yet. One transaction."""
    channels = parse_rows(files.get("channels", b""))
    if not channels:
        raise DirectoryError("channels file is empty")
    if not all("id" in r and "name" in r for r in channels[:5]):
        raise DirectoryError("channels file has no id and name columns")
    langs = _languages(parse_rows(files.get("feeds", b"")))
    logos = _logos(parse_rows(files.get("logos", b"")))
    blocked = {r["channel"]: r.get("reason") or "blocked"
               for r in parse_rows(files.get("blocklist", b""))}  # fmt: skip
    now = utc_now()
    report = DirectoryReport(entries=len(channels))

    conn.execute("BEGIN")
    try:
        conn.execute("DELETE FROM directory_channels")
        for r in channels:
            alt = _list(r.get("alt_names"))
            cats = _list(r.get("categories"))
            nsfw = _bool(r.get("is_nsfw"))
            conn.execute(
                "INSERT OR REPLACE INTO directory_channels (id, name, alt_names, network,"
                " country, categories, languages, is_nsfw, closed, replaced_by, website,"
                " logo_url, blocked, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (r["id"], r["name"], json.dumps(alt, ensure_ascii=False), r.get("network") or None,
                 normalize_country(r.get("country") or "") or None, json.dumps(cats),
                 json.dumps(langs.get(r["id"], [])), int(nsfw), r.get("closed") or None,
                 r.get("replaced_by") or None, r.get("website") or None, logos.get(r["id"]),
                 blocked.get(r["id"]), now),
            )  # fmt: skip
            keys = {split_name(n).key for n in (r["name"], *alt) if n}
            conn.executemany(
                "INSERT OR IGNORE INTO directory_names (key, directory_id) VALUES (?, ?)",
                [(k, r["id"]) for k in keys if k],
            )
        report.from_guides = add_guide_channels(conn, now)
        report.linked, report.merged = link_and_merge(conn)
        if countries:
            report.created = _add_missing(conn, countries, now)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return report


def link_and_merge(conn: sqlite3.Connection) -> tuple[int, int]:
    """Link unlinked channels to the directory, then merge channels that are one.

    Runs inside the caller's transaction. Returns (linked, merged)."""
    linked = 0
    known = {r["lid"]: r["id"] for r in conn.execute(
        "SELECT lower(id) AS lid, id FROM directory_channels"
    )}  # fmt: skip
    dir_country = {r["id"]: r["country"] for r in conn.execute(
        "SELECT id, country FROM directory_channels"
    )}  # fmt: skip
    unlinked = conn.execute(
        "SELECT id, country FROM channels WHERE directory_id IS NULL"
    ).fetchall()
    for ch in unlinked if known else []:
        target = None
        for a in conn.execute(
            "SELECT value FROM channel_aliases WHERE channel_id = ? AND kind = 'tvg-id'",
            (ch["id"],),
        ):
            target = known.get(tvg_base(a["value"]))
            if target:
                break
        if target is None:
            cands = {r["directory_id"] for r in conn.execute(
                "SELECT n.directory_id FROM channel_aliases a JOIN directory_names n"
                " ON n.key = a.value WHERE a.channel_id = ? AND a.kind = 'name'", (ch["id"],)
            )}  # fmt: skip
            cands = {d for d in cands if _same_country(dir_country[d], ch["country"])}
            if len(cands) == 1:
                target = cands.pop()
        if target:
            conn.execute("UPDATE channels SET directory_id = ? WHERE id = ?", (target, ch["id"]))
            linked += 1

    merged = 0
    while True:
        # A merge moves aliases, which can join two groups: repeat until nothing moves.
        groups: dict[str, set[str]] = defaultdict(set)
        for r in conn.execute(
            "SELECT directory_id AS k, id FROM channels WHERE directory_id IS NOT NULL"
            " UNION SELECT 'tvg:' || value, channel_id FROM channel_aliases"
            " WHERE kind = 'tvg-id'"
        ):
            groups[r["k"]].add(r["id"])
        dup = next((ids for ids in groups.values() if len(ids) > 1), None)
        if dup is None:
            break
        rows = conn.execute(
            f"SELECT id FROM channels WHERE id IN ({','.join('?' * len(dup))})"
            " ORDER BY created_at, rowid",
            tuple(dup),
        ).fetchall()
        for r in rows[1:]:
            merge_channels(conn, rows[0]["id"], r["id"])
            merged += 1
    return linked, merged


def merge_channels(conn: sqlite3.Connection, keep: str, drop: str) -> None:
    """Move everything of ``drop`` onto ``keep`` and delete ``drop``."""
    conn.execute(
        "INSERT OR IGNORE INTO channel_aliases (channel_id, kind, value, display, source_id)"
        " SELECT ?, kind, value, display, source_id FROM channel_aliases WHERE channel_id = ?",
        (keep, drop),
    )
    for f in conn.execute(
        "SELECT id, region, offset_minutes FROM feeds WHERE channel_id = ?", (drop,)
    ).fetchall():
        twin = conn.execute(
            "SELECT id FROM feeds WHERE channel_id = ? AND region = ? AND offset_minutes = ?",
            (keep, f["region"], f["offset_minutes"]),
        ).fetchone()
        if twin is None:
            conn.execute("UPDATE feeds SET channel_id = ? WHERE id = ?", (keep, f["id"]))
            continue
        conn.execute("UPDATE streams SET feed_id = ? WHERE feed_id = ?", (twin["id"], f["id"]))
        conn.execute(
            "INSERT OR IGNORE INTO feed_aliases (feed_id, kind, value)"
            " SELECT ?, kind, value FROM feed_aliases WHERE feed_id = ?",
            (twin["id"], f["id"]),
        )
        conn.execute("UPDATE epg_mappings SET feed_id = ? WHERE feed_id = ?", (twin["id"], f["id"]))
        conn.execute("DELETE FROM feeds WHERE id = ?", (f["id"],))
    conn.execute(
        "UPDATE channels SET"
        " country = COALESCE(country, (SELECT country FROM channels WHERE id = :drop)),"
        " logo_url = COALESCE(logo_url, (SELECT logo_url FROM channels WHERE id = :drop)),"
        " directory_id = COALESCE(directory_id,"
        "   (SELECT directory_id FROM channels WHERE id = :drop))"
        " WHERE id = :keep",
        {"keep": keep, "drop": drop},
    )
    conn.execute(
        "UPDATE OR IGNORE favorites SET target_id = ?"
        " WHERE target_kind = 'channel' AND target_id = ?",
        (keep, drop),
    )
    conn.execute("DELETE FROM favorites WHERE target_kind = 'channel' AND target_id = ?", (drop,))
    conn.execute(
        "UPDATE match_decisions SET target_id = ? WHERE target_kind = 'channel' AND target_id = ?",
        (keep, drop),
    )
    conn.execute("DELETE FROM channels WHERE id = ?", (drop,))


def _add_missing(conn: sqlite3.Connection, countries: tuple[str, ...], now: str) -> int:
    wanted = {normalize_country(c) for c in countries}
    have = {r[0] for r in conn.execute(
        "SELECT directory_id FROM channels WHERE directory_id IS NOT NULL"
    )}  # fmt: skip
    created = 0
    for d in conn.execute(
        "SELECT id, name, alt_names, country, logo_url FROM directory_channels"
        " WHERE closed IS NULL AND blocked IS NULL ORDER BY id"
    ).fetchall():
        if d["country"] not in wanted or d["id"] in have:
            continue
        cid = new_id()
        conn.execute(
            "INSERT INTO channels (id, canonical_name, country, logo_url, created_at,"
            " directory_id) VALUES (?, ?, ?, ?, ?, ?)",
            (cid, d["name"], d["country"], d["logo_url"], now, d["id"]),
        )
        aliases = [(cid, "tvg-id", d["id"].casefold(), d["id"])]
        for n in (d["name"], *json.loads(d["alt_names"] or "[]")):
            key = split_name(n).key
            if key:
                aliases.append((cid, "name", key, n))
        conn.executemany(
            "INSERT OR IGNORE INTO channel_aliases (channel_id, kind, value, display)"
            " VALUES (?, ?, ?, ?)",
            aliases,
        )
        created += 1
    return created


def _languages(feeds: list[dict]) -> dict[str, list[str]]:
    """Languages of each channel's feeds, the main feed's first, as ISO 639-1 if known."""
    out: dict[str, list[str]] = defaultdict(list)
    for f in sorted(feeds, key=lambda f: not _bool(f.get("is_main"))):
        for code in _list(f.get("languages")):
            lang = normalize_lang(code)
            if lang not in out[f["channel"]]:
                out[f["channel"]].append(lang)
    return out


def _logos(rows: list[dict]) -> dict[str, str]:
    """One logo per channel: the first that is not tied to a single feed."""
    out: dict[str, str] = {}
    for r in rows:
        if r.get("feed") or not r.get("url"):
            continue
        if "in_use" in r and not _bool(r["in_use"]):
            continue
        out.setdefault(r["channel"], r["url"])
    return out


def _same_country(a: str | None, b: str | None) -> bool:
    return not a or not b or normalize_country(a) == normalize_country(b)


def _list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v) for v in value if v]
    return [v.strip() for v in str(value).split(";") if v.strip()]


def _bool(value) -> bool:
    return value is True or str(value).strip().upper() in ("TRUE", "1")
