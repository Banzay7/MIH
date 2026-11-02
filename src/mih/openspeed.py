"""How fast channels open on the TV, and the channels the household opened lately.

Every launch that can be timed leaves a row in ``open_times``: the Home Assistant
script returned (the player is in front and got "Play"), the agent reported the
picture with its start-up time, or a player reported it itself. A launch still loading
when the wait ran out (the agent's 30 s timeout, an HA timeout) is kept as ``slow``.

A channel's speed is the median of its last SAMPLE opens. It is *slow* (bad to play)
when that median is above the threshold (``open_slow_s`` in app_settings, set in the
web UI; MIH_OPEN_SLOW_S or DEFAULT_SLOW_S otherwise), *good* otherwise. The grade on
top of it: ``fast`` up to 40 % of the threshold, ``ok`` up to it, ``slow`` up to twice
it, ``very_slow`` beyond. Grades are computed when read, so a new threshold re-grades
the whole history at once. Slow channels go to the end of channel lists ("slow last").

"Recently opened" lists the launches since the user last cleared it (``recent_cleared_at``);
clearing it keeps the open times, so speed grades survive.
"""

from __future__ import annotations

import os
import sqlite3
import statistics
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta

from mih.ids import utc_now

DEFAULT_SLOW_S = 5.0
MIN_SLOW_S, MAX_SLOW_S = 1.0, 60.0
SAMPLE = 10
KEEP = timedelta(days=90)  # owner's rule: history is kept 3 months
KEY_SLOW = "open_slow_s"
KEY_CLEARED = "recent_cleared_at"
GRADES = ("fast", "ok", "slow", "very_slow")


def _get(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def _put(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, ?)"
        " ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (key, value, utc_now()),
    )


def default_slow_s() -> float:
    try:
        v = float(os.environ.get("MIH_OPEN_SLOW_S", DEFAULT_SLOW_S))
    except ValueError:
        return DEFAULT_SLOW_S
    return min(max(v, MIN_SLOW_S), MAX_SLOW_S)


def threshold_s(conn: sqlite3.Connection) -> float:
    value = _get(conn, KEY_SLOW)
    try:
        return min(max(float(value), MIN_SLOW_S), MAX_SLOW_S) if value else default_slow_s()
    except ValueError:
        return default_slow_s()


def set_threshold(conn: sqlite3.Connection, seconds: float) -> float:
    seconds = round(min(max(float(seconds), MIN_SLOW_S), MAX_SLOW_S), 1)
    _put(conn, KEY_SLOW, str(seconds))
    return seconds


def grade(ms: int, threshold_ms: float) -> str:
    if ms <= 0.4 * threshold_ms:
        return "fast"
    if ms <= threshold_ms:
        return "ok"
    if ms <= 2 * threshold_ms:
        return "slow"
    return "very_slow"


def record(
    conn: sqlite3.Connection, channel_id: str | None, ms: int | None, *, source: str,
    outcome: str = "opened", stream_id: str | None = None, command_id: str | None = None,
    now: datetime | None = None,
) -> None:  # fmt: skip
    """Keep one timed open. A launch with no time or no channel is not kept. One command
    is timed once: a later report for it replaces the first one."""
    if not channel_id or ms is None or ms < 0:
        return
    now = now or datetime.now(UTC)
    if command_id:
        conn.execute("DELETE FROM open_times WHERE command_id = ?", (command_id,))
    conn.execute(
        "INSERT INTO open_times (channel_id, stream_id, command_id, source, outcome, open_ms, at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (channel_id, stream_id, command_id, source, outcome, int(ms), _iso(now)),
    )
    conn.execute("DELETE FROM open_times WHERE channel_id = ? AND at < ?",
                 (channel_id, _iso(now - KEEP)))  # fmt: skip


def channel_of_stream(conn: sqlite3.Connection, stream_id: str) -> str | None:
    row = conn.execute(
        "SELECT f.channel_id FROM streams s JOIN feeds f ON f.id = s.feed_id WHERE s.id = ?",
        (stream_id,),
    ).fetchone()
    return row["channel_id"] if row else None


def summary(conn: sqlite3.Connection, ids: Iterable[str] | None = None) -> dict[str, dict]:
    """channel id -> {ms (median of the last SAMPLE), n, grade, slow} for timed channels."""
    params: list = []
    where = ""
    if ids is not None:
        ids = list(ids)
        if not ids:
            return {}
        where = f" WHERE channel_id IN ({','.join('?' * len(ids))})"
        params = ids
    rows = conn.execute(
        "SELECT channel_id, open_ms FROM (SELECT channel_id, open_ms, ROW_NUMBER() OVER ("
        "  PARTITION BY channel_id ORDER BY at DESC, id DESC) AS rn FROM open_times"
        f"{where}) WHERE rn <= ?",
        (*params, SAMPLE),
    ).fetchall()
    by: dict[str, list[int]] = {}
    for r in rows:
        by.setdefault(r["channel_id"], []).append(r["open_ms"])
    thr = threshold_s(conn) * 1000
    out = {}
    for cid, values in by.items():
        ms = int(statistics.median(values))
        out[cid] = {"ms": ms, "n": len(values), "grade": grade(ms, thr), "slow": ms > thr}
    return out


def slow_ids(conn: sqlite3.Connection) -> set[str]:
    return {c for c, s in summary(conn).items() if s["slow"]}


def slow_last[T](items: list[T], key: Callable[[T], str | None], slow: set[str]) -> list[T]:
    """The same items with slow channels moved to the end, both parts in their order."""
    if not slow:
        return items
    return [i for i in items if key(i) not in slow] + [i for i in items if key(i) in slow]


def history(conn: sqlite3.Connection, channel_id: str, limit: int = 20) -> list[dict]:
    thr = threshold_s(conn) * 1000
    rows = conn.execute(
        "SELECT at, open_ms, outcome, source FROM open_times WHERE channel_id = ?"
        " ORDER BY at DESC, id DESC LIMIT ?",
        (channel_id, limit),
    ).fetchall()
    return [{**dict(r), "grade": grade(r["open_ms"], thr)} for r in rows]


def recent(conn: sqlite3.Connection, limit: int = 30) -> list[dict]:
    """Channels started with "На ТВ" since the list was last cleared, newest first, one
    row per channel with its last launch and its speed."""
    since = _get(conn, KEY_CLEARED) or ""
    rows = conn.execute(
        "SELECT json_extract(target, '$.channel_id') AS channel_id, MAX(created_at) AS at,"
        " COUNT(*) AS times FROM playback_commands WHERE created_at > ?"
        " GROUP BY channel_id ORDER BY at DESC LIMIT ?",
        (since, limit),
    ).fetchall()
    ids = [r["channel_id"] for r in rows if r["channel_id"]]
    if not ids:
        return []
    marks = ",".join("?" * len(ids))
    names = {r["id"]: r for r in conn.execute(
        f"SELECT id, canonical_name, logo_url FROM channels WHERE id IN ({marks})", ids,
    ).fetchall()}  # fmt: skip
    speed = summary(conn, ids)
    out = []
    for r in rows:
        ch = names.get(r["channel_id"])
        if ch:
            out.append({"channel_id": r["channel_id"], "channel_name": ch["canonical_name"],
                        "logo_url": ch["logo_url"], "at": r["at"], "times": r["times"],
                        "open": speed.get(r["channel_id"])})  # fmt: skip
    return out


def clear_recent(conn: sqlite3.Connection) -> None:
    _put(conn, KEY_CLEARED, utc_now())


def ms_between(stages: dict, start: str, end: str) -> int | None:
    try:
        a = datetime.fromisoformat(stages[start].replace("Z", "+00:00"))
        b = datetime.fromisoformat(stages[end].replace("Z", "+00:00"))
    except (KeyError, AttributeError, ValueError):
        return None
    return max(0, int((b - a).total_seconds() * 1000))


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
