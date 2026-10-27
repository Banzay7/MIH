"""The tools an agent calls: few of them, fast, and with small answers.

Every answer is compact JSON: short field names, local times (MIH_TIMEZONE) without
seconds, empty fields left out, a hard cap on items. An agent pays for every token it
reads, so nothing here dumps a table: a list is at most ``MAX_ITEMS`` long and says how
many more there were.

Times: "19:10" is today, "10-28 19:10" another day (month-day), in the zone named by
``tz`` in the answer of ``now``/``schedule``/``search``.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from mih.alerts import GRACE, best_streams, find_matches, load_rules, play_url
from mih.config import Settings
from mih.epg import guide
from mih.epg.scope import topic_key
from mih.health.policy import HealthPolicy, Observation
from mih.identity.search_keys import compact, search_keys
from mih.ids import utc_now
from mih.lists import LEVELS
from mih.playback import PlaybackError, finish_command, set_offered, take_commands
from mih.search.engine import SearchRequest, search

MAX_ITEMS = 50
PROFILE = "default"


class ToolError(Exception):
    """A mistake in the call; the agent sees the message and can retry."""


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    schema: dict
    read_only: bool = True


def _obj(props: dict, required: tuple[str, ...] = ()) -> dict:
    return {"type": "object", "properties": props, "required": list(required),
            "additionalProperties": False}  # fmt: skip


_LIMIT = {"type": "integer", "minimum": 1, "maximum": MAX_ITEMS}

TOOLS = [
    Tool(
        "channels",
        "Find TV channels by name as a person says it (any spelling, Cyrillic or Latin: "
        "'россия 24', 'rossiya24', 'первый'). Playing channels first. Returns id, name, "
        "lvl (ok = played within 48 h; doubtful; unknown; dead) and what is on now.",
        _obj(
            {
                "q": {"type": "string", "description": "channel name or part of it"},
                "limit": _LIMIT | {"default": 5},
            },
            ("q",),
        ),  # fmt: skip
    ),
    Tool(
        "search",
        "Find programmes in the TV guide. Free text in Russian or English with optional "
        "time words: 'футбол сегодня вечером', 'мультфильмы завтра', 'news now'. Only "
        "channels that play are returned unless all=true. Returns title, channel, cid, "
        "start/end in local time, more = further showings of the same programme.",
        _obj(
            {
                "q": {"type": "string"},
                "limit": _LIMIT | {"default": 10},
                "all": {"type": "boolean", "default": False},
            },
            ("q",),
        ),  # fmt: skip
    ),
    Tool(
        "now",
        "What is on now and next. By channel ids, by a named list, or (neither given) on "
        "every playing channel that has a guide, sorted by name.",
        _obj(
            {
                "ids": {"type": "array", "items": {"type": "string"}, "maxItems": MAX_ITEMS},
                "list": {"type": "string", "description": "a list name from 'status'"},
                "limit": _LIMIT | {"default": 20},
            }
        ),  # fmt: skip
    ),
    Tool(
        "schedule",
        "A channel's programme guide from a moment on: day 'today' (from now), "
        "'tomorrow' (from 00:00 local) or an ISO time; hours ahead, at most 48.",
        _obj(
            {
                "id": {"type": "string"},
                "day": {"type": "string", "default": "today"},
                "hours": {"type": "integer", "minimum": 1, "maximum": 48, "default": 6},
            },
            ("id",),
        ),  # fmt: skip
    ),
    Tool(
        "play",
        "Streams to start a channel, best first (at most 3). Pass ua/ref to the player as "
        "User-Agent/Referer when present, or it gets HTTP 403. After the start call "
        "'report' with the stream_id. m3u: a link for players that take a playlist.",
        _obj({"id": {"type": "string"}}, ("id",)),
    ),
    Tool(
        "report",
        "Tell the server that a stream did NOT play on the TV: error, black screen or "
        "nothing within 15 s (error: timeout, http_403, player_error, no_video). Report "
        "each stream that failed, then try the next one. Silence means it plays: no "
        "report is needed for success (ok=true is accepted and closes a command at "
        "once). Pass cmd for a command from 'queue'.",
        _obj(
            {
                "stream_id": {"type": "string"},
                "ok": {"type": "boolean"},
                "ms": {"type": "integer", "minimum": 0, "description": "start-up time"},
                "error": {"type": "string", "maxLength": 64},
                "cmd": {"type": "string", "description": "command id from 'queue'"},
            },
            ("stream_id", "ok"),
        ),
        read_only=False,
    ),
    Tool(
        "queue",
        "Channels the household asked to start on the TV with the web UI's 'На ТВ' "
        "button. Each command is handed out once, with up to 3 streams like 'play'. "
        "Start the first on the TV; if it does not play, report(stream_id, ok=false, "
        "cmd) and try the next. Silence for 60 s means it plays. wait>0 holds the call "
        "up to that many seconds until a command arrives (long poll); a command nobody "
        "takes within 30 s fails on the UI.",
        _obj({"wait": {"type": "integer", "minimum": 0, "maximum": 50, "default": 0}}),
        read_only=False,
    ),
    Tool(
        "topics",
        "Followed topics ('футбол', 'formula 1'): the server pushes a notice before a "
        "matching programme starts. add/remove change the list; the answer is the list "
        "and what matches in the next 'hours' on playing channels.",
        _obj(
            {
                "add": {"type": "string", "minLength": 2, "maxLength": 64},
                "remove": {"type": "string", "maxLength": 64},
                "hours": {"type": "integer", "minimum": 1, "maximum": 72, "default": 24},
            }
        ),
        read_only=False,
    ),
    Tool(
        "status",
        "Catalog summary: how many channels play, named lists, when data was refreshed.",
        _obj({}),
    ),
]
BY_NAME = {t.name: t for t in TOOLS}


class Toolbox:
    """Runs a tool against the database. One instance per server: it caches the
    channel name index between calls and rebuilds it when the catalog changes."""

    def __init__(self, settings: Settings, policy: HealthPolicy | None = None):
        self.settings = settings
        self.policy = policy or HealthPolicy.from_env()
        self.tz = ZoneInfo(settings.timezone)
        self._index: tuple[tuple, dict[str, set[str]], dict[str, str]] | None = None
        self._lists: dict[Path, tuple[float, list[str]]] = {}

    def call(self, conn: sqlite3.Connection, name: str, args: dict[str, Any],
             now: datetime | None = None) -> dict:  # fmt: skip
        if name not in BY_NAME:
            raise ToolError(f"unknown tool {name!r}")
        fn = getattr(self, "_" + name)
        try:
            return fn(conn, now or datetime.now(UTC), **(args or {}))
        except TypeError as exc:  # unknown or missing argument
            raise ToolError(str(exc).split(") ", 1)[-1]) from exc

    # ---------- tools ----------

    def _channels(self, conn, now, q: str, limit: int = 5) -> dict:
        limit = _cap(limit)
        ids = self.find_channels(conn, q)
        if not ids:
            return {"items": [], "hint": "nothing found; try another spelling or 'search'"}
        picks = best_streams(conn, ids, now)
        names = self._index[2] if self._index else {}
        rank = {c: (LEVELS.index(picks[c][0].level) if c in picks else len(LEVELS)) for c in ids}
        order = sorted(ids, key=lambda c: (rank[c], ids[c], names.get(c, "").casefold()))
        chosen = order[:limit]
        on_air = self._on_air(conn, now, chosen)
        items = []
        for c in chosen:
            item = {"id": c, "name": names.get(c), "lvl": picks[c][0].level if c in picks
                    else "dead"}  # fmt: skip
            item.update(on_air.get(c, {}))
            items.append(item)
        return _page(items, len(order))

    def _search(self, conn, now, q: str, limit: int = 10, all: bool = False) -> dict:  # noqa: A002
        limit = _cap(limit)
        req = SearchRequest(query=q, target_types=["airing"], tz=self.settings.timezone,
                            limit=MAX_ITEMS * 2)  # fmt: skip
        found = search(conn, req, now)
        items = [i for i in found["items"] if all or i["streams_available"]]
        out = [_drop_empty({"title": i["title"], "sub": i["sub_title"], "ch": i["channel_name"],
                            "cid": i["channel_id"], "at": self._t(i["start"], now),
                            "end": self._t(i["stop"], now),
                            "more": i["more_airings"] or None})
               for i in items[:limit]]  # fmt: skip
        res = _page(out, len(items))
        res["tz"] = self.settings.timezone
        if not out:
            res["hint"] = "no showings; the guide covers about 7 days ahead"
        return res

    def _now(self, conn, now, ids: list[str] | None = None, list: str | None = None,  # noqa: A002
             limit: int = 20) -> dict:  # fmt: skip
        limit = _cap(limit)
        if list:
            ids = self._list_ids(list)
        if ids is None:
            picks = best_streams(conn, _all_channels(conn), now)
            ids = [c for c, p in picks.items() if p[0].level == "ok"]
        names = _names(conn, set(ids))
        on_air = self._on_air(conn, now, ids)
        rows = [{"id": c, "name": names.get(c), **on_air[c]} for c in ids if c in on_air]
        rows.sort(key=lambda r: (r["name"] or "").casefold())
        res = _page(rows[:limit], len(rows))
        res["tz"] = self.settings.timezone
        return res

    def _schedule(self, conn, now, id: str, day: str = "today", hours: int = 6) -> dict:  # noqa: A002
        hours = max(1, min(int(hours), 48))
        start = self._day_start(day, now)
        end = start + timedelta(hours=hours)
        guides = [g for g in guide.feed_guides(conn, [id]).values()]
        if not guides:
            if not _names(conn, {id}):
                raise ToolError("no such channel; find the id with 'channels'")
            return {"id": id, "items": [], "hint": "this channel has no guide"}
        g = min(guides, key=lambda g: (g.shift_minutes != 0, g.shift_minutes))
        rows = guide.schedule(conn, g, start, end)
        items = [_drop_empty({"at": self._t(r["start"], now), "title": r["title"],
                              "sub": r["sub_title"]}) for r in rows]  # fmt: skip
        res = _page(items[:MAX_ITEMS], len(items))
        res.update(id=id, tz=self.settings.timezone)
        return res

    def _play(self, conn, now, id: str) -> dict:  # noqa: A002
        picks = [p for p in best_streams(conn, {id}, now).get(id, []) if p.url]
        if not picks:
            if not _names(conn, {id}):
                raise ToolError("no such channel; find the id with 'channels'")
            return {"id": id, "streams": [], "hint": "no stream plays now; try another channel"}
        streams = []
        for p in picks[:3]:
            s = {"stream_id": p.stream_id, "url": p.url, "lvl": p.level}
            for k, v in p.headers.items():
                key = {"user-agent": "ua", "referer": "ref"}.get(k.lower())
                if key and v:
                    s[key] = v
            streams.append(s)
        res = {"id": id, "streams": streams}
        link = play_url(self.settings, id)
        if link:
            res["m3u"] = link.replace(f"/play/{id}?", f"/play/{id}.m3u?", 1)
        return res

    def _report(self, conn, now, stream_id: str, ok: bool, ms: int | None = None,
                error: str | None = None, cmd: str | None = None) -> dict:  # fmt: skip
        # Imported here: the runner pulls in the probe and its HTTP client.
        from mih.health.runner import record

        if conn.execute("SELECT 1 FROM streams WHERE id = ?", (stream_id,)).fetchone() is None:
            raise ToolError("no such stream; use a stream_id from 'play'")
        obs = Observation(at=now, vantage="device", success=bool(ok))
        conn.execute("BEGIN")
        try:
            d = record(conn, stream_id, obs, self.policy, kind="playback",
                       startup_ms=ms, error_kind=(error or None) if not ok else None)  # fmt: skip
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        out = {"stream_id": stream_id, "status": d.state.status}
        if cmd:
            try:
                out["cmd"] = finish_command(conn, cmd, bool(ok), stream_id, error, now)["state"]
            except PlaybackError as exc:
                raise ToolError("no such command; use a cmd from 'queue'") from exc
        return out

    def _queue(self, conn, now, wait: int = 0) -> dict:
        deadline = time.monotonic() + max(0, min(int(wait), 50))
        while True:
            cmds = take_commands(conn, now)
            if cmds or time.monotonic() >= deadline:
                break
            time.sleep(0.5)
            now = datetime.now(UTC)
        items = []
        for c in cmds:
            cid = c["target"]["channel_id"]
            item = {"cmd": c["id"], "id": cid, "name": _names(conn, {cid}).get(cid)}
            item["streams"] = self._play(conn, now, cid)["streams"]
            set_offered(conn, c["id"], [x["stream_id"] for x in item["streams"]])
            items.append(item)
        return {"items": items}

    def _topics(self, conn, now, add: str | None = None, remove: str | None = None,
                hours: int = 24) -> dict:  # fmt: skip
        if add:
            key = topic_key(add)
            if len(key) < 2:
                raise ToolError("topic is too short")
            conn.execute("INSERT INTO tracked_topics (profile_id, topic, created_at)"
                         " VALUES (?, ?, ?) ON CONFLICT DO NOTHING",
                         (PROFILE, key, utc_now()))  # fmt: skip
        if remove:
            conn.execute("DELETE FROM tracked_topics WHERE profile_id = ? AND topic = ?",
                         (PROFILE, topic_key(remove)))  # fmt: skip
        topics = [
            r[0]
            for r in conn.execute(
                "SELECT topic FROM tracked_topics WHERE profile_id = ? ORDER BY topic", (PROFILE,)
            )
        ]
        hours = max(1, min(int(hours), 72))
        found = find_matches(conn, now - GRACE, now + timedelta(hours=hours), now,
                             load_rules(conn, PROFILE))  # fmt: skip
        soon = [
            _drop_empty(
                {
                    "at": self._t(m.start, now),
                    "title": m.title,
                    "sub": m.sub_title,
                    "ch": m.names.get(m.picks[0].channel_id),
                    "cid": m.picks[0].channel_id,
                }
            )
            for m in found[:MAX_ITEMS]
        ]
        return {"topics": topics, "soon": soon, "tz": self.settings.timezone}

    def _status(self, conn, now) -> dict:
        picks = best_streams(conn, _all_channels(conn), now)
        lvls = {lvl: 0 for lvl in LEVELS[:3]}
        for p in picks.values():
            lvls[p[0].level] = lvls.get(p[0].level, 0) + 1
        lists = []
        for f in sorted(self.settings.lists_dir.glob("*.json")):
            try:
                doc = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            lists.append({"name": f.stem, "channels": doc.get("channels_total"),
                          "at": self._t(doc.get("generated_at"), now)})  # fmt: skip
        last = {r["name"]: r["last_finished_at"] for r in conn.execute(
            "SELECT name, last_finished_at FROM jobs WHERE name IN ('sources', 'epg', 'health')"
        )}  # fmt: skip
        return _drop_empty({
            "channels": conn.execute("SELECT COUNT(*) FROM channels").fetchone()[0],
            "playing": lvls, "lists": lists,
            "refreshed": {k: self._t(v, now) for k, v in last.items() if v} or None,
            "tz": self.settings.timezone,
        })  # fmt: skip

    # ---------- helpers ----------

    def find_channels(self, conn: sqlite3.Connection, q: str) -> dict[str, int]:
        """Channel id -> match rank (0 exact key, 1 key starts with it, 2 contains it)."""
        key = compact(q)
        if not key:
            raise ToolError("empty query")
        index, _ = self._channel_index(conn)
        out: dict[str, int] = {c: 0 for c in index.get(key, ())}
        if len(out) < MAX_ITEMS:
            for k, ids in index.items():
                if key in k and k != key:
                    rank = 1 if k.startswith(key) else 2
                    for c in ids:
                        if out.get(c, 9) > rank:
                            out[c] = rank
        return out

    def _channel_index(self, conn) -> tuple[dict[str, set[str]], dict[str, str]]:
        sig = tuple(conn.execute(
            "SELECT (SELECT COUNT(*) FROM channels), (SELECT MAX(rowid) FROM channels),"
            " (SELECT COUNT(*) FROM channel_aliases), (SELECT MAX(rowid) FROM channel_aliases)"
        ).fetchone())  # fmt: skip
        if self._index and self._index[0] == sig:
            return self._index[1], self._index[2]
        names: dict[str, list[str]] = {}
        tvg: dict[str, list[str]] = {}
        canon = {}
        for r in conn.execute("SELECT id, canonical_name FROM channels"):
            canon[r["id"]] = r["canonical_name"]
            names[r["id"]] = [r["canonical_name"]]
        for r in conn.execute("SELECT channel_id, kind, value, display FROM channel_aliases"
                              " WHERE kind IN ('name', 'tvg-id')"):  # fmt: skip
            if r["kind"] == "name":
                names.setdefault(r["channel_id"], []).append(r["display"] or r["value"])
            else:
                tvg.setdefault(r["channel_id"], []).append(r["value"])
        index: dict[str, set[str]] = {}
        for cid, ns in names.items():
            for k in search_keys(dict.fromkeys(ns), tvg.get(cid, ())):
                index.setdefault(compact(k), set()).add(cid)
        self._index = (sig, index, canon)
        return index, canon

    def _on_air(self, conn, now: datetime, ids) -> dict[str, dict]:
        """Channel id -> {now, till, next, at} from its base feed's guide."""
        guides = guide.feed_guides(conn, list(ids))
        best: dict[str, guide.FeedGuide] = {}
        for g in sorted(guides.values(), key=lambda g: (g.shift_minutes != 0, g.shift_minutes)):
            best.setdefault(g.channel_id, g)
        if not best:
            return {}
        state = guide.now_and_next(conn, now, {g.feed_id: g for g in best.values()})
        out = {}
        for c, g in best.items():
            cur, nxt = state[g.feed_id]["now"], state[g.feed_id]["next"]
            out[c] = _drop_empty({
                "now": cur["title"] if cur else None,
                "till": self._t(cur["stop"], now) if cur else None,
                "next": nxt["title"] if nxt else None,
                "at": self._t(nxt["start"], now) if nxt else None,
            })  # fmt: skip
        return out

    def _list_ids(self, name: str) -> list[str]:
        path = self.settings.lists_dir / f"{name}.json"
        try:
            mtime = path.stat().st_mtime
        except OSError as exc:
            raise ToolError(f"no list {name!r}; see 'status'") from exc
        cached = self._lists.get(path)
        if cached and cached[0] == mtime:
            return cached[1]
        doc = json.loads(path.read_text(encoding="utf-8"))
        ids = [c["id"] for c in doc.get("channels", [])]
        self._lists[path] = (mtime, ids)
        return ids

    def _day_start(self, day: str, now: datetime) -> datetime:
        local = now.astimezone(self.tz)
        if day in ("", "today", "now", "сегодня"):
            return now
        if day in ("tomorrow", "завтра"):
            return datetime.combine(local.date() + timedelta(days=1), datetime.min.time(),
                                    self.tz)  # fmt: skip
        try:
            dt = datetime.fromisoformat(day.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ToolError("day: 'today', 'tomorrow' or an ISO time") from exc
        return dt if dt.tzinfo else dt.replace(tzinfo=self.tz)

    def _t(self, value: str | None, now: datetime) -> str | None:
        if not value:
            return None
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        local = dt.astimezone(self.tz)
        if local.date() == now.astimezone(self.tz).date():
            return local.strftime("%H:%M")
        return local.strftime("%m-%d %H:%M")


def _cap(limit: int) -> int:
    return max(1, min(int(limit), MAX_ITEMS))


def _page(items: list, total: int) -> dict:
    res: dict = {"items": items}
    if total > len(items):
        res["more"] = total - len(items)
    return res


def _drop_empty(d: dict) -> dict:
    return {k: v for k, v in d.items() if v is not None and v != "" and v != []}


def _all_channels(conn) -> list[str]:
    return [r[0] for r in conn.execute("SELECT id FROM channels")]


def _names(conn, ids: set[str]) -> dict[str, str]:
    if not ids:
        return {}
    rows = conn.execute(
        f"SELECT id, canonical_name FROM channels WHERE id IN ({','.join('?' * len(ids))})",
        list(ids),
    ).fetchall()
    return {r[0]: r[1] for r in rows}
