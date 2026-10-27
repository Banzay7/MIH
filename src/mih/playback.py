"""Playback commands (spec 12 C4): pick the best live stream and hand it to a device.

The household's agent starts the TV (MIH_PLAYBACK=agent): a command waits as
``accepted`` until the agent takes it over MCP (``take_commands``), starts the channel
its own way and reports the result (``finish_command``). A command nobody takes within
PICKUP fails with ``no_agent``, one taken but never reported within RUN with
``no_report``. MIH_PLAYBACK=off only picks the stream and fails with ``no_adapter``.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from mih.config import Settings
from mih.ids import new_id, utc_now

PICKUP = timedelta(seconds=30)
RUN = timedelta(seconds=90)

# Lower is better. quarantine and archived streams are never played.
_STATUS_RANK = {"active": 0, "new": 1, "unverified": 2, "suspect": 3}


class PlaybackError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Target:
    kind: str  # 'channel'
    channel_id: str
    feed_id: str | None = None

    def as_dict(self) -> dict:
        out = {"kind": self.kind, "channel_id": self.channel_id}
        if self.feed_id:
            out["feed_id"] = self.feed_id
        return out


def pick_streams(conn: sqlite3.Connection, target: Target) -> list[str]:
    """Playable stream ids for the target, best first. The base feed wins unless one is named."""
    rows = conn.execute(
        "SELECT s.id, s.status, s.last_seen_at, f.id AS feed_id, f.offset_minutes"
        " FROM streams s JOIN feeds f ON f.id = s.feed_id"
        " WHERE f.channel_id = ? AND s.availability_reason IS NULL"
        " AND s.status NOT IN ('quarantine', 'archived')",
        (target.channel_id,),
    ).fetchall()
    rows = sorted(rows, key=lambda r: r["last_seen_at"], reverse=True)
    rows.sort(key=lambda r: (
        0 if r["feed_id"] == target.feed_id else 1,
        r["offset_minutes"] != 0,
        _STATUS_RANK.get(r["status"], 9),
    ))  # fmt: skip
    return [r["id"] for r in rows]


def create_command(
    conn: sqlite3.Connection,
    target: Target,
    device_id: str = "default",
    idempotency_key: str | None = None,
    mode: str = "off",
) -> tuple[dict, bool]:
    """Returns (command, created). A repeated idempotency key returns the first command."""
    if idempotency_key:
        row = conn.execute(
            "SELECT * FROM playback_commands WHERE idempotency_key = ?", (idempotency_key,)
        ).fetchone()
        if row is not None:
            return command_out(row), False
    ch = conn.execute("SELECT 1 FROM channels WHERE id = ?", (target.channel_id,)).fetchone()
    if ch is None:
        raise PlaybackError("not_found", "channel not found")
    if (
        target.feed_id
        and not conn.execute(
            "SELECT 1 FROM feeds WHERE id = ? AND channel_id = ?",
            (target.feed_id, target.channel_id),
        ).fetchone()
    ):
        raise PlaybackError("validation_error", "feed does not belong to the channel")

    streams = pick_streams(conn, target)
    now = utc_now()
    waits = bool(streams) and mode == "agent"
    error = None if waits else ("no_adapter" if streams else "no_stream")
    cmd_id = new_id()
    conn.execute(
        "INSERT INTO playback_commands (id, device_id, target, stream_id, state,"
        " idempotency_key, created_at, finished_at, error)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (cmd_id, device_id, json.dumps(target.as_dict()), streams[0] if streams else None,
         "accepted" if waits else "failed", idempotency_key, now,
         None if waits else now, error),
    )  # fmt: skip
    return get_command(conn, cmd_id), True


def expire(conn: sqlite3.Connection, now: datetime) -> None:
    """Fail the commands nobody took, or took and never reported, in time."""
    conn.execute(
        "UPDATE playback_commands SET state = 'failed', finished_at = ?,"
        " error = CASE WHEN taken_at IS NULL THEN 'no_agent' ELSE 'no_report' END"
        " WHERE state = 'accepted' AND ((taken_at IS NULL AND created_at < ?)"
        " OR taken_at < ?)",
        (_iso(now), _iso(now - PICKUP), _iso(now - RUN)),
    )


def take_commands(conn: sqlite3.Connection, now: datetime | None = None) -> list[dict]:
    """The commands waiting for the agent, oldest first; each is handed out once."""
    now = now or datetime.now(UTC)
    conn.execute("BEGIN IMMEDIATE")
    try:
        expire(conn, now)
        rows = conn.execute(
            "SELECT * FROM playback_commands WHERE state = 'accepted' AND taken_at IS NULL"
            " ORDER BY created_at"
        ).fetchall()
        conn.executemany("UPDATE playback_commands SET taken_at = ? WHERE id = ?",
                         [(_iso(now), r["id"]) for r in rows])  # fmt: skip
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return [command_out(r) for r in rows]


def finish_command(
    conn: sqlite3.Connection, cmd_id: str, ok: bool, stream_id: str | None,
    error: str | None = None, now: datetime | None = None,
) -> dict:  # fmt: skip
    """The agent's result. A failed stream may be followed by a spare that starts, so a
    later report replaces an earlier one, except that a failure never undoes a start."""
    get_command(conn, cmd_id)
    now = now or datetime.now(UTC)
    conn.execute(
        "UPDATE playback_commands SET state = ?, stream_id = COALESCE(?, stream_id),"
        " finished_at = ?, error = ?, taken_at = COALESCE(taken_at, ?)"
        " WHERE id = ? AND (state != 'started' OR ?)",
        ("started" if ok else "failed", stream_id, _iso(now),
         None if ok else (error or "player_error"), _iso(now), cmd_id, ok),
    )  # fmt: skip
    return get_command(conn, cmd_id)


def get_command(conn: sqlite3.Connection, cmd_id: str) -> dict:
    expire(conn, datetime.now(UTC))
    row = conn.execute("SELECT * FROM playback_commands WHERE id = ?", (cmd_id,)).fetchone()
    if row is None:
        raise PlaybackError("not_found", "playback command not found")
    return command_out(row)


def command_out(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"], "device_id": row["device_id"], "target": json.loads(row["target"]),
        "stream_id": row["stream_id"], "state": row["state"], "error": row["error"],
        "created_at": row["created_at"], "finished_at": row["finished_at"],
    }  # fmt: skip


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def wake_agent(settings: Settings, cmd: dict, channel_name: str | None) -> None:
    """Tell the agent at MIH_AGENT_WEBHOOK that a command waits, without making the
    button wait for it. Best effort: the agent can also poll the MCP tool."""
    if not settings.agent_webhook:
        return
    payload = {"event": "play", "command_id": cmd["id"],
               "channel_id": cmd["target"]["channel_id"], "channel": channel_name}  # fmt: skip
    headers = {}
    if settings.agent_webhook_token:
        headers["Authorization"] = f"Bearer {settings.agent_webhook_token}"

    def _send() -> None:
        import httpx

        # On failure the command still waits; the agent's next poll finds it.
        with contextlib.suppress(httpx.HTTPError):
            httpx.post(settings.agent_webhook, json=payload, headers=headers, timeout=5)

    threading.Thread(target=_send, name="wake-agent", daemon=True).start()
