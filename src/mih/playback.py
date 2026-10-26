"""Playback commands (spec 12 C4): pick the best live stream and hand it to a device.

No device adapter exists yet: the HA bridge comes later. Until then a command is
recorded, a stream is picked, and the command fails with ``no_adapter``, so the UI
and the API contract can be built and tested now.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from mih.ids import new_id, utc_now

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
    error = "no_adapter" if streams else "no_stream"
    cmd_id = new_id()
    conn.execute(
        "INSERT INTO playback_commands (id, device_id, target, stream_id, state,"
        " idempotency_key, created_at, finished_at, error)"
        " VALUES (?, ?, ?, ?, 'failed', ?, ?, ?, ?)",
        (cmd_id, device_id, json.dumps(target.as_dict()), streams[0] if streams else None,
         idempotency_key, now, now, error),
    )  # fmt: skip
    return get_command(conn, cmd_id), True


def get_command(conn: sqlite3.Connection, cmd_id: str) -> dict:
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
