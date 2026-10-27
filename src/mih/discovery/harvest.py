"""The scout's search: where new public playlists come from.

Three kinds of places, each step bounded so a run stays small:

- seeds: known public playlists and the GitHub repositories that publish them;
- GitHub: repository search (topic:iptv and similar), then each repository's file tree
  for .m3u/.m3u8 files; with MIH_GITHUB_TOKEN also code search for files with #EXTINF;
- pages: web pages and public Telegram channels (t.me/s/<name>) from MIH_DISCOVERY_FILE,
  read for links to playlists and to GitHub repositories.

Everything found goes into discovered_playlists as 'new'; whether it is any good is the
scout's next step (mih.discovery.scout).
"""

from __future__ import annotations

import html
import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

import httpx

from mih.config import Settings
from mih.health.policy import format_ts, parse_ts
from mih.ids import new_id
from mih.sources.fetch import FetchError, fetch_source

API = "https://api.github.com"
RAW = "https://raw.githubusercontent.com"
PLAYLIST_EXT = (".m3u", ".m3u8")
# A playlist file bigger than this in a repository is an archive dump, not a list.
MAX_FILE_BYTES = 20 * 1024 * 1024
FILES_PER_REPO = 40
FILES_PER_SEED_REPO = 400
REPO_RESCAN = timedelta(days=30)
# How often seeds, pages and searches are gone through again, when MIH_DISCOVERY_SEARCH_EVERY
# is not set: daily while the install is new, monthly once the first AUTO_DAILY_FOR are over
# (by then the searches have little new to give).
AUTO_DAILY_FOR = timedelta(days=60)
AUTO_EARLY = timedelta(days=1)
AUTO_LATER = timedelta(days=30)
PAGES_PER_RUN = 3
# Search only goes deeper when fewer repositories than this wait for a scan.
REPO_QUEUE_LOW = 20
SEARCH_PAGE_SIZE = 50
# GitHub returns at most 1000 results per search.
SEARCH_MAX_RESULTS = 1000
# GitHub API calls per run. The scout runs every few minutes and an unauthenticated client
# gets 60 calls an hour.
CALLS_PER_RUN = 4
CALLS_PER_RUN_TOKEN = 20

# Public free-to-air playlists from docs/sources/catalog.yaml.
SEED_PLAYLISTS = (
    "https://raw.githubusercontent.com/Free-TV/IPTV/master/playlist.m3u8",
    "https://raw.githubusercontent.com/Tundrak/IPTV-Italia/main/iptvitaplus.m3u",
    "https://raw.githubusercontent.com/YanG-1989/m3u/main/Gather.m3u",
    "https://romaxa55.github.io/world_ip_tv/output/index.m3u",
)
# Repositories whose every playlist file is a candidate.
SEED_REPOS = (
    "iptv-org/iptv",
    "Free-TV/IPTV",
    "jnk22/kodinerds-iptv",
    "Tundrak/IPTV-Italia",
    "fanmingming/live",
    "YanG-1989/m3u",
    "khanh71/All-In-One-IPTV",
    "Romaxa55/world_ip_tv",
)
REPO_QUERIES = (
    "topic:iptv",
    "topic:m3u",
    "topic:iptv-playlist",
    "topic:m3u8",
    "iptv m3u in:name,description",
    "iptv playlist in:name,description",
    "iptv russia in:name,description,readme",
    "русское iptv",
)
CODE_QUERIES = ("EXTINF extension:m3u", "EXTINF extension:m3u8")

_LINK = re.compile(r"""https?://[^\s"'<>()\[\]{}\\]+""")
_GITHUB_BLOB = re.compile(r"^https://github\.com/([^/]+/[^/]+)/(?:blob|raw)/(.+)$")
_GITHUB_REPO = re.compile(r"^https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/?$")


@dataclass
class Plan:
    """What MIH_DISCOVERY_FILE adds to the built-in seeds."""

    playlists: tuple[str, ...] = ()
    repos: tuple[str, ...] = ()
    pages: tuple[str, ...] = ()
    deny_hosts: frozenset[str] = frozenset()


def load_plan(path: Path | None) -> Plan:
    """{"playlists": [url], "repos": ["owner/name"], "pages": [url], "telegram": [name],
    "deny_hosts": [host]}; every key optional."""
    if path is None:
        return Plan()
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("MIH_DISCOVERY_FILE must hold a JSON object")
    pages = [str(p) for p in data.get("pages", [])]
    for name in data.get("telegram", []):
        name = str(name).strip().lstrip("@").removeprefix("https://t.me/").strip("/")
        if name:
            pages.append(f"https://t.me/s/{name}")
    return Plan(
        playlists=tuple(str(u) for u in data.get("playlists", [])),
        repos=tuple(str(r) for r in data.get("repos", [])),
        pages=tuple(pages),
        deny_hosts=frozenset(str(h).lower() for h in data.get("deny_hosts", [])),
    )


@dataclass
class HarvestReport:
    found: dict[str, int] = field(default_factory=dict)  # by origin
    api_calls: int = 0
    repos_scanned: int = 0
    pages_read: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return sum(self.found.values())


def harvest(
    conn: sqlite3.Connection,
    settings: Settings,
    plan: Plan,
    now: datetime,
    *,
    transport: httpx.BaseTransport | None = None,
) -> HarvestReport:
    report = HarvestReport()
    period = search_period(conn, settings, now)
    for url in (*SEED_PLAYLISTS, *plan.playlists):
        _add(conn, report, url, "seed", None, now)
    for repo in (*SEED_REPOS, *plan.repos):
        _add_repo(conn, repo, "seed", None, now)
    _read_pages(conn, settings, plan, now, period, report, transport)

    budget = CALLS_PER_RUN_TOKEN if settings.github_token else CALLS_PER_RUN
    with _github(settings, transport) as gh:
        try:
            for row in _repos_due(conn, now, period, budget):
                if report.api_calls >= budget:
                    break
                _scan_repo(conn, gh, row, now, report)
            queued = conn.execute(
                "SELECT COUNT(*) FROM discovered_repos WHERE scanned_at IS NULL"
            ).fetchone()[0]
            if report.api_calls < budget and queued < REPO_QUEUE_LOW:
                _search(conn, gh, "repos", REPO_QUERIES, now, period, report)
            if settings.github_token and report.api_calls < budget:
                _search(conn, gh, "code", CODE_QUERIES, now, period, report)
        except _RateLimited:
            pass  # the next run goes on where this one stopped
    return report


# ---------- GitHub ----------


def _github(settings: Settings, transport) -> httpx.Client:
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "MIRTVONLINE/0.1",
               "X-GitHub-Api-Version": "2022-11-28"}  # fmt: skip
    if settings.github_token:
        headers["Authorization"] = f"Bearer {settings.github_token}"
    return httpx.Client(base_url=API, headers=headers, timeout=settings.fetch_timeout_s,
                        transport=transport)  # fmt: skip


class _RateLimited(Exception):
    pass


def _get(gh: httpx.Client, report: HarvestReport, path: str, **params) -> dict | None:
    report.api_calls += 1
    try:
        resp = gh.get(path, params=params or None)
    except httpx.HTTPError as exc:
        report.notes.append(f"github: {type(exc).__name__}")
        return None
    if resp.status_code in (403, 429) and resp.headers.get("x-ratelimit-remaining") == "0":
        report.notes.append("github: rate limit")
        raise _RateLimited
    if resp.status_code >= 400:
        report.notes.append(f"github {path}: HTTP {resp.status_code}")
        return None
    return resp.json()


def search_period(conn: sqlite3.Connection, settings: Settings, now: datetime) -> timedelta:
    """How long one pass over seeds, pages and searches lasts before the next begins."""
    if settings.discovery_search_every:
        return settings.discovery_search_every
    first = _state(conn, "first_harvest_at")
    if first is None:
        _set_state(conn, "first_harvest_at", format_ts(now))
        return AUTO_EARLY
    return AUTO_EARLY if now - parse_ts(first) < AUTO_DAILY_FOR else AUTO_LATER


def _repos_due(conn: sqlite3.Connection, now: datetime, period: timedelta,
               limit: int) -> list[sqlite3.Row]:  # fmt: skip
    seed_before = format_ts(now - period)
    other_before = format_ts(now - REPO_RESCAN)
    return conn.execute(
        "SELECT * FROM discovered_repos WHERE scanned_at IS NULL"
        " OR (origin = 'seed' AND scanned_at < ?)"
        " OR (scanned_at < ? AND pushed_at > scanned_at)"
        " ORDER BY scanned_at IS NOT NULL, origin != 'seed', found_at LIMIT ?",
        (seed_before, other_before, limit),
    ).fetchall()


def _scan_repo(conn, gh, row, now: datetime, report: HarvestReport) -> None:
    full = row["full_name"]
    data = _get(gh, report, f"/repos/{full}/git/trees/HEAD", recursive="1")
    files = 0
    if data:
        cap = FILES_PER_SEED_REPO if row["origin"] == "seed" else FILES_PER_REPO
        origin = "seed" if row["origin"] == "seed" else "github"
        for item in data.get("tree", []):
            path = item.get("path", "")
            if (item.get("type") != "blob" or not path.lower().endswith(PLAYLIST_EXT)
                    or (item.get("size") or 0) > MAX_FILE_BYTES):  # fmt: skip
                continue
            _add(conn, report, f"{RAW}/{full}/HEAD/{quote(path)}", origin, full, now)
            files += 1
            if files >= cap:
                break
    report.repos_scanned += 1
    conn.execute("UPDATE discovered_repos SET scanned_at = ?, files = ? WHERE full_name = ?",
                 (format_ts(now), files, full))  # fmt: skip


def _search(conn, gh, kind: str, queries: tuple[str, ...], now: datetime, period: timedelta,
            report: HarvestReport) -> None:  # fmt: skip
    """One page of one query; the next run takes the next page or the next query."""
    for _ in range(len(queries)):
        index = int(_state(conn, f"{kind}:query") or 0) % len(queries)
        query = queries[index]
        paused = _state(conn, f"{kind}:done:{query}")
        if paused and parse_ts(paused) > now - period:
            _set_state(conn, f"{kind}:query", str(index + 1))
            continue
        page = int(_state(conn, f"{kind}:page:{query}") or 1)
        params = {"q": query, "per_page": SEARCH_PAGE_SIZE, "page": page}
        if kind == "repos":
            params |= {"sort": "updated", "order": "desc"}
        data = _get(gh, report, f"/search/{'repositories' if kind == 'repos' else 'code'}",
                    **params)  # fmt: skip
        items = (data or {}).get("items", [])
        for item in items:
            if kind == "repos":
                _add_repo(conn, item["full_name"], "github", item.get("pushed_at"), now)
            else:
                full = item["repository"]["full_name"]
                path = item["path"]
                if path.lower().endswith(PLAYLIST_EXT):
                    _add(conn, report, f"{RAW}/{full}/HEAD/{quote(path)}", "code", full, now)
        total = min((data or {}).get("total_count", 0), SEARCH_MAX_RESULTS)
        if not items or page * SEARCH_PAGE_SIZE >= total:
            _set_state(conn, f"{kind}:page:{query}", "1")
            _set_state(conn, f"{kind}:done:{query}", format_ts(now))
            _set_state(conn, f"{kind}:query", str(index + 1))
        else:
            _set_state(conn, f"{kind}:page:{query}", str(page + 1))
        return


# ---------- Pages and Telegram ----------


def _read_pages(conn, settings, plan: Plan, now: datetime, period: timedelta,
                report: HarvestReport, transport) -> None:  # fmt: skip
    read = 0
    for page in plan.pages:
        if read >= PAGES_PER_RUN:
            break
        last = _state(conn, f"page:{page}")
        if last and parse_ts(last) > now - period:
            continue
        read += 1
        _set_state(conn, f"page:{page}", format_ts(now))
        try:
            result = fetch_source(page, settings, transport=transport)
        except FetchError as exc:
            report.notes.append(f"{page}: {exc.code}")
            continue
        report.pages_read += 1
        text = (result.content or b"").decode("utf-8", "replace")
        for url, repo in extract_links(text):
            if url:
                _add(conn, report, url, "page", page, now)
            if repo:
                _add_repo(conn, repo, "page", None, now)


def extract_links(text: str) -> list[tuple[str | None, str | None]]:
    """(playlist URL, None) or (None, "owner/repo") for every useful link in a page."""
    out, seen = [], set()
    for raw in _LINK.findall(html.unescape(text)):
        url = raw.rstrip(".,;:!?*_`'")
        if url in seen:
            continue
        seen.add(url)
        blob = _GITHUB_BLOB.match(url)
        if blob:
            url = f"{RAW}/{blob.group(1)}/{blob.group(2)}"
        path = unquote(urlsplit(url).path).lower()
        if path.endswith(PLAYLIST_EXT):
            out.append((url, None))
            continue
        repo = _GITHUB_REPO.match(url)
        if repo and not repo.group(1).lower().startswith(("orgs/", "topics/", "features/")):
            out.append((None, repo.group(1)))
    return out


# ---------- Storage ----------


def _add(conn, report: HarvestReport, url: str, origin: str, via: str | None,
         now: datetime) -> None:  # fmt: skip
    added = conn.execute(
        "INSERT OR IGNORE INTO discovered_playlists (id, url, origin, found_via, first_seen_at,"
        " next_scout_at) VALUES (?, ?, ?, ?, ?, ?)",
        (new_id(), url.strip(), origin, via, format_ts(now), format_ts(now)),
    ).rowcount
    if added:
        report.found[origin] = report.found.get(origin, 0) + 1


def _add_repo(conn, full_name: str, origin: str, pushed_at: str | None, now: datetime) -> None:
    conn.execute(
        "INSERT INTO discovered_repos (full_name, origin, found_at, pushed_at)"
        " VALUES (?, ?, ?, ?) ON CONFLICT(full_name) DO UPDATE SET"
        " pushed_at = coalesce(excluded.pushed_at, pushed_at)",
        (full_name, origin, format_ts(now), pushed_at),
    )


def _state(conn, key: str) -> str | None:
    row = conn.execute("SELECT value FROM discovery_state WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def _set_state(conn, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO discovery_state (key, value) VALUES (?, ?)"
        " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
