"""Topic alerts: a push shortly before a programme the household follows starts.

A profile follows topics (words such as "футбол" or "formula 1", see tracked_topics)
and favourite programmes. Every few minutes (MIH_EVERY_ALERTS) the alerts job looks at
the guide a little ahead (MIH_ALERT_LEAD), finds airings whose title, sub-title or
category matches, keeps those on channels with a stream that plays, and pushes each
showing once, with a link that starts the channel (MIH_ALERT_LINK).

Matching is the same as the guide scope's (mih.epg.scope): a topic matches at the start
of a word, case and ё folded. So what the tracked scope stores is what alerts can find.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx

from mih.config import Settings
from mih.epg import guide
from mih.epg.scope import topic_key
from mih.events import record_event, t
from mih.lists import LEVELS, stream_level

# A programme that started this long ago is still worth a push: a scan may run late.
GRACE = timedelta(minutes=5)
# Episodes of one series back to back: the first one is announced, not each of them.
SERIES_GAP = timedelta(hours=3)
# Pushes per scan; more matches arrive as one summary push.
MAX_PUSHES = 5
# Channels named in one push.
MAX_CHANNELS = 3


@dataclass(frozen=True)
class Rule:
    profile_id: str
    kind: str  # topic | programme
    value: str  # the folded topic, or the folded title of a favourite programme
    pattern: re.Pattern | None = None

    def matches(self, title_key: str, text: str) -> bool:
        if self.kind == "programme":
            return title_key == self.value
        return bool(self.pattern and self.pattern.search(text))

    def as_dict(self) -> dict:
        return {"profile_id": self.profile_id, "kind": self.kind, "value": self.value}


@dataclass(frozen=True)
class Pick:
    """A stream that plays, for a link."""

    channel_id: str
    stream_id: str
    level: str
    url: str | None
    headers: dict = field(default_factory=dict)

    @property
    def requires_headers(self) -> bool:
        return any(self.headers.values())


@dataclass
class Match:
    key: str
    title_key: str
    title: str
    sub_title: str | None
    programme_id: str
    start: str
    stop: str
    categories: list[str]
    channel_ids: list[str]
    reasons: list[Rule]
    # Filled by find_matches: the channels with a stream that plays, best first.
    picks: list[Pick] = field(default_factory=list)
    names: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "title": self.title,
            "sub_title": self.sub_title,
            "programme_id": self.programme_id,
            "start": self.start,
            "stop": self.stop,
            "categories": self.categories,
            "channels": [
                {"channel_id": p.channel_id, "name": self.names.get(p.channel_id), "level": p.level}
                for p in self.picks
            ],  # fmt: skip
            "reasons": [r.as_dict() for r in self.reasons],
        }


def load_rules(conn: sqlite3.Connection, profile_id: str | None = None) -> list[Rule]:
    rules = []
    for r in conn.execute(
        "SELECT profile_id, topic FROM tracked_topics WHERE ? IS NULL OR profile_id = ?"
        " ORDER BY profile_id, topic",
        (profile_id, profile_id),
    ):
        pattern = re.compile(r"(?<!\w)" + re.escape(r["topic"]))
        rules.append(Rule(r["profile_id"], "topic", r["topic"], pattern))
    for r in conn.execute(
        "SELECT f.profile_id, p.title FROM favorites f JOIN programmes p ON p.id = f.target_id"
        " WHERE f.target_kind = 'programme' AND (? IS NULL OR f.profile_id = ?)"
        " ORDER BY f.position",
        (profile_id, profile_id),
    ):
        rules.append(Rule(r["profile_id"], "programme", topic_key(r["title"])))
    return list(dict.fromkeys(rules))


def find_matches(
    conn: sqlite3.Connection,
    start: datetime,
    end: datetime,
    now: datetime,
    rules: list[Rule],
) -> list[Match]:
    """Showings that start in (start, end], match a rule and can be played, soonest first.
    One showing on several channels at once is one match."""
    if not rules:
        return []
    # Feeds with a guide of their own: a +2 copy of the same show is not news.
    guides = {g.epg_channel_id: g for g in guide.feed_guides(conn).values()
              if g.shift_minutes == 0}  # fmt: skip
    if not guides:
        return []
    rows = conn.execute(
        f"SELECT {guide.AIRING_COLS}, p.localized FROM airings a"
        " JOIN programmes p ON p.id = a.programme_id"
        " WHERE a.status = 'scheduled' AND a.start_utc > ? AND a.start_utc <= ?"
        " ORDER BY a.start_utc",
        (guide.iso(start), guide.iso(end)),
    ).fetchall()
    found: dict[str, Match] = {}
    for r in rows:
        g = guides.get(r["epg_channel_id"])
        if g is None:
            continue
        title_key = topic_key(r["title"])
        key = f"{title_key}|{r['start_utc']}"
        if key in found:
            if g.channel_id not in found[key].channel_ids:
                found[key].channel_ids.append(g.channel_id)
            continue
        localized = json.loads(r["localized"] or "{}")
        categories = json.loads(r["categories"])
        text = topic_key(" ".join(
            [r["title"], r["sub_title"] or ""] + [x for _, x in localized.get("titles", [])]
            + [x for _, x in localized.get("sub_titles", [])] + categories
        ))  # fmt: skip
        hits = [rule for rule in rules if rule.matches(title_key, text)]
        if hits:
            found[key] = Match(key, title_key, r["title"], r["sub_title"], r["programme_id"],
                               r["start_utc"], r["stop_utc"], categories, [g.channel_id],
                               hits)  # fmt: skip
    if not found:
        return []
    picks = best_streams(conn, {c for m in found.values() for c in m.channel_ids}, now)
    names = channel_names(conn, set(picks))
    out = []
    for m in found.values():
        m.picks = sorted((picks[c][0] for c in m.channel_ids if c in picks),
                         key=lambda p: LEVELS.index(p.level))  # fmt: skip
        if m.picks:
            m.names = {p.channel_id: names.get(p.channel_id, "") for p in m.picks}
            out.append(m)
    return out


def best_streams(
    conn: sqlite3.Connection, channel_ids: set[str] | list[str], now: datetime
) -> dict[str, list[Pick]]:
    """Per channel, its streams that are not dead (mih.lists levels), best first: the
    surest level, the base feed before +N copies, no special headers, the freshest."""
    ids = list(channel_ids)
    if not ids:
        return {}
    rows = conn.execute(
        "SELECT s.id, s.status, s.availability_reason, s.last_ok_at, s.last_device_fail_at,"
        " s.streak_failures, f.channel_id, f.offset_minutes, l.url, l.headers"
        " FROM streams s JOIN feeds f ON f.id = s.feed_id"
        " LEFT JOIN stream_locators l ON l.stream_id = s.id"
        f" WHERE f.channel_id IN ({','.join('?' * len(ids))})"
        " ORDER BY s.last_ok_at DESC",
        ids,
    ).fetchall()
    ranked: dict[str, list[tuple]] = {}
    for r in rows:
        level, _ = stream_level(r, now)
        if level == "dead":
            continue
        pick = Pick(r["channel_id"], r["id"], level, r["url"], json.loads(r["headers"] or "{}"))
        order = (LEVELS.index(level), r["offset_minutes"] != 0, pick.requires_headers)
        ranked.setdefault(r["channel_id"], []).append((order, pick))
    # sorted() is stable: among equals the freshest stays first.
    return {c: [p for _, p in sorted(items, key=lambda x: x[0])] for c, items in ranked.items()}


def channel_names(conn: sqlite3.Connection, ids: set[str]) -> dict[str, str]:
    if not ids:
        return {}
    rows = conn.execute(
        f"SELECT id, canonical_name FROM channels WHERE id IN ({','.join('?' * len(ids))})",
        list(ids),
    ).fetchall()
    return {r["id"]: r["canonical_name"] for r in rows}


# ---------- Links ----------


def play_key(settings: Settings, channel_id: str) -> str:
    """The key of /play/<channel>: it opens that channel and nothing else, so a link in a
    push does not carry the API token."""
    secret = (settings.api_token or "").encode()
    return hmac.new(secret, f"play:{channel_id}".encode(), hashlib.sha256).hexdigest()[:24]


def play_url(settings: Settings, channel_id: str) -> str | None:
    if not settings.public_url:
        return None
    return f"{settings.public_url}/play/{quote(channel_id)}?key={play_key(settings, channel_id)}"


def link_for(settings: Settings, picks: list[Pick]) -> str | None:
    mode = settings.alert_link
    if mode == "auto":
        mode = "play" if settings.public_url else "stream"
    if mode == "play" and picks:
        return play_url(settings, picks[0].channel_id)
    if mode == "stream":
        # A phone opening the link cannot send a User-Agent or Referer.
        return next((p.url for p in picks if p.url and not p.requires_headers), None)
    return None


# ---------- The job ----------


def run_alerts(
    conn: sqlite3.Connection,
    settings: Settings,
    now: datetime,
    *,
    transport: httpx.BaseTransport | None = None,
) -> dict:
    """Push the matches that start soon and were not announced yet."""
    cutoff = guide.iso(now - timedelta(days=max(settings.epg_retention_days, 1)))
    conn.execute("DELETE FROM alerts_sent WHERE sent_at < ?", (cutoff,))
    rules = load_rules(conn)
    stats = {"rules": len(rules), "matches": 0, "sent": 0, "titles": []}
    if not rules or settings.alert_lead is None:
        return stats
    matches = find_matches(conn, now - GRACE, now + settings.alert_lead, now, rules)
    stats["matches"] = len(matches)
    fresh = [m for m in matches if not _announced(conn, m)]
    tz = ZoneInfo(settings.timezone)
    for i, m in enumerate(fresh):
        title, body = alert_text(settings, m, now, tz)
        pushed = "summary"
        if i < MAX_PUSHES:
            event = record_event(
                conn, settings, "alert.topic", "info", title, body,
                {"programme_id": m.programme_id, "start": m.start,
                 "channel_ids": [p.channel_id for p in m.picks],
                 "reasons": [r.as_dict() for r in m.reasons]},
                push=True, click=link_for(settings, m.picks),
                action=t(settings, "alert.watch"), transport=transport,
            )  # fmt: skip
            pushed = event["pushed"]
        conn.execute(
            "INSERT OR IGNORE INTO alerts_sent (key, title_key, title, start_utc, channel_ids,"
            " reasons, sent_at, pushed) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (m.key, m.title_key, m.title, m.start, json.dumps([p.channel_id for p in m.picks]),
             json.dumps([r.as_dict() for r in m.reasons], ensure_ascii=False), guide.iso(now),
             pushed),
        )  # fmt: skip
        stats["titles"].append(title)
    if len(fresh) > MAX_PUSHES:
        rest = fresh[MAX_PUSHES:]
        lines = [alert_line(settings, m, tz) for m in rest]
        click = f"{settings.public_url}/#/favorites" if settings.public_url else None
        record_event(conn, settings, "alert.more", "info",
                     t(settings, "alert.more", n=len(rest)), "\n".join(lines),
                     push=True, click=click, transport=transport)  # fmt: skip
    stats["sent"] = len(fresh)
    return stats


def _announced(conn: sqlite3.Connection, m: Match) -> bool:
    since = guide.iso(_parse(m.start) - SERIES_GAP)
    row = conn.execute(
        "SELECT 1 FROM alerts_sent WHERE key = ? OR (title_key = ? AND start_utc >= ?"
        " AND start_utc < ?) LIMIT 1",
        (m.key, m.title_key, since, m.start),
    ).fetchone()
    return row is not None


def alert_text(settings: Settings, m: Match, now: datetime, tz: ZoneInfo) -> tuple[str, str]:
    start = _parse(m.start)
    if start <= now:
        title = t(settings, "alert.started", title=m.title)
    else:
        title = t(settings, "alert.title", time=f"{start.astimezone(tz):%H:%M}", title=m.title)
    names = [m.names[p.channel_id] for p in m.picks[:MAX_CHANNELS]]
    if len(m.picks) > MAX_CHANNELS:
        names.append(f"+{len(m.picks) - MAX_CHANNELS}")
    lines = [", ".join(names)]
    if m.sub_title:
        lines.append(m.sub_title)
    lines.append(why_text(settings, m.reasons))
    return title, "\n".join(lines)


def alert_line(settings: Settings, m: Match, tz: ZoneInfo) -> str:
    start = _parse(m.start).astimezone(tz)
    return f"• {start:%H:%M} {m.title} — {m.names[m.picks[0].channel_id]}"


def why_text(settings: Settings, reasons: list[Rule]) -> str:
    parts = []
    for r in reasons:
        text = (t(settings, "alert.topic", topic=r.value) if r.kind == "topic"
                else t(settings, "alert.favourite"))  # fmt: skip
        if text not in parts:
            parts.append(text)
    return ", ".join(parts)


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
