"""Playback commands (spec 12 C4): pick the best live stream and hand it to a device.

The household's agent starts the TV (MIH_PLAYBACK=agent): a command waits as
``accepted`` until the agent takes it over MCP (``take_commands``) with up to three
streams (``set_offered``) and starts the channel its own way. The agent reports only
what did not play (``finish_command``); silence means it plays. So RUN after it took
the command, the first offered stream with no failure counts as started, and as a
playback seen on the TV. Every offered stream failed: the command failed, unless the
last one only timed out (a slow HD start): then it is started, ``unconfirmed``. A command
nobody takes within PICKUP fails with ``no_agent``. MIH_PLAYBACK=off only picks the
stream and fails with ``no_adapter``.

MIH_PLAYBACK=ha, or ``adapter: "ha"`` in a request, skips the agent: MIH calls the
user's Home Assistant script with the stream URLs and waits for it (mih.ha). The
script ran = started, it raised or HA did not answer = failed with an ``ha_*`` error.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from mih.config import Settings
from mih.health.policy import is_slow_start
from mih.ids import new_id, utc_now

PICKUP = timedelta(seconds=30)
# Time the agent has to try the streams (up to 30 s each) and report the ones that failed.
RUN = timedelta(seconds=100)

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
    # A programme to start from its beginning: UTC ISO times and the title.
    start: str | None = None
    stop: str | None = None
    title: str | None = None
    # The picture to start with (mih.quality); none = MIH_PLAY_QUALITY.
    quality: str | None = None

    def as_dict(self) -> dict:
        out = {"kind": self.kind, "channel_id": self.channel_id}
        if self.feed_id:
            out["feed_id"] = self.feed_id
        if self.quality:
            out["quality"] = self.quality
        if self.start:
            out.update({"start": self.start, "stop": self.stop, "title": self.title})
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
    waits = bool(streams) and mode in ("agent", "ha")
    error = None if waits else ("no_adapter" if streams else "no_stream")
    cmd_id = new_id()
    # An HA command is never queued for the agent: it counts as taken at once and
    # run_ha closes it.
    conn.execute(
        "INSERT INTO playback_commands (id, device_id, target, stream_id, state,"
        " idempotency_key, created_at, finished_at, error, adapter, taken_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (cmd_id, device_id, json.dumps(target.as_dict()), streams[0] if streams else None,
         "accepted" if waits else "failed", idempotency_key, now,
         None if waits else now, error, mode if mode != "off" else None,
         now if mode == "ha" else None),
    )  # fmt: skip
    return get_command(conn, cmd_id), True


def expire(conn: sqlite3.Connection, now: datetime) -> None:
    """Close the commands whose time is up: not taken (no_agent), or taken with no
    failure left unanswered, which means the stream plays."""
    conn.execute(
        "UPDATE playback_commands SET state = 'failed', finished_at = ?, error = 'no_agent'"
        " WHERE state = 'accepted' AND taken_at IS NULL AND created_at < ?",
        (_iso(now), _iso(now - PICKUP)),
    )
    for row in conn.execute(
        "SELECT id, offered, failed, stream_id FROM playback_commands"
        " WHERE state = 'accepted' AND taken_at < ?",
        (_iso(now - RUN),),
    ).fetchall():
        failed = set(json.loads(row["failed"] or "[]"))
        playing = next((s for s in json.loads(row["offered"] or "[]") if s not in failed), None)
        if playing is None and row["stream_id"] not in failed:
            playing = row["stream_id"]
        conn.execute(
            "UPDATE playback_commands SET state = ?, stream_id = COALESCE(?, stream_id),"
            " finished_at = ?, error = ? WHERE id = ?",
            ("started" if playing else "failed", playing, _iso(now),
             None if playing else "all_failed", row["id"]),
        )  # fmt: skip
        if playing:
            _seen_playing(conn, playing, now)


def _seen_playing(conn: sqlite3.Connection, stream_id: str, now: datetime) -> None:
    # Imported here: the health runner pulls in the prober and its HTTP client.
    from mih.health.policy import HealthPolicy, Observation
    from mih.health.runner import record

    if conn.execute("SELECT 1 FROM streams WHERE id = ?", (stream_id,)).fetchone():
        record(conn, stream_id, Observation(at=now, vantage="device", success=True),
               HealthPolicy.from_env(), kind="playback", error_kind=None)  # fmt: skip


def set_offered(conn: sqlite3.Connection, cmd_id: str, stream_ids: list[str]) -> None:
    conn.execute("UPDATE playback_commands SET offered = ? WHERE id = ?",
                 (json.dumps(stream_ids), cmd_id))  # fmt: skip


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
    """A report from the agent. ok=True closes the command as started at once. A failure
    is noted; the command fails when every offered stream has failed, otherwise the
    agent is trying a spare and silence until RUN means that one plays. When the last
    one only timed out, the TV is still loading it: the command is started, unconfirmed."""
    row = conn.execute("SELECT * FROM playback_commands WHERE id = ?", (cmd_id,)).fetchone()
    if row is None:
        raise PlaybackError("not_found", "playback command not found")
    now = now or datetime.now(UTC)
    taken = row["taken_at"] or _iso(now)
    if ok:
        conn.execute(
            "UPDATE playback_commands SET state = 'started', stream_id = COALESCE(?, stream_id),"
            " finished_at = ?, error = NULL, taken_at = ? WHERE id = ?",
            (stream_id, _iso(now), taken, cmd_id),
        )
        return get_command(conn, cmd_id)
    if row["state"] == "started":
        return command_out(row)  # a late failure of a spare never undoes a start
    failed = json.loads(row["failed"] or "[]")
    if stream_id and stream_id not in failed:
        failed.append(stream_id)
    offered = json.loads(row["offered"] or "[]") or [row["stream_id"]]
    done = all(s in failed for s in offered)
    if done and is_slow_start(error):
        conn.execute(
            "UPDATE playback_commands SET state = 'started', stream_id = COALESCE(?, stream_id),"
            " failed = ?, finished_at = ?, error = NULL, detail = 'unconfirmed', taken_at = ?"
            " WHERE id = ?",
            (stream_id, json.dumps(failed), _iso(now), taken, cmd_id),
        )  # fmt: skip
        return get_command(conn, cmd_id)
    conn.execute(
        "UPDATE playback_commands SET failed = ?, error = ?, taken_at = ?, state = ?,"
        " finished_at = ? WHERE id = ?",
        (json.dumps(failed), error or "player_error", taken,
         "failed" if done else row["state"], _iso(now) if done else None, cmd_id),
    )  # fmt: skip
    return get_command(conn, cmd_id)


def run_ha(
    conn: sqlite3.Connection, settings: Settings, cmd_id: str, channel_name: str | None,
    *, transport=None,
) -> dict:  # fmt: skip
    """Carry out an HA command: hand the script the best stream URLs, wait for it, close
    the command. Streams that need special headers go last: a TV player rarely sends them."""
    from mih import ha
    from mih.alerts import play_url

    row = conn.execute("SELECT * FROM playback_commands WHERE id = ?", (cmd_id,)).fetchone()
    target = json.loads(row["target"])
    order = pick_streams(conn, Target(target["kind"], target["channel_id"], target.get("feed_id")))
    locators = {r["stream_id"]: r for r in conn.execute(
        f"SELECT stream_id, url, headers FROM stream_locators"
        f" WHERE stream_id IN ({','.join('?' * len(order))})", order,
    ).fetchall()} if order else {}  # fmt: skip
    usable = [s for s in order if locators.get(s) and locators[s]["url"]]
    usable.sort(key=lambda s: any(json.loads(locators[s]["headers"] or "{}").values()))
    picked = usable[: settings.ha_streams]
    error = detail = None
    catchup_url = None
    if picked and target.get("start") and target.get("stop"):
        from mih import catchup

        now = datetime.now(UTC)
        start = datetime.fromisoformat(target["start"])
        stop = datetime.fromisoformat(target["stop"])
        if start <= now:
            for s in picked:
                catchup_url = catchup.build_url(
                    catchup.stream_attrs(conn, s), locators[s]["url"], start, stop, now
                )
                if catchup_url:
                    break
            if not catchup_url:
                detail = "live_no_catchup"
    if not picked:
        error = "no_stream"
    else:
        from mih.quality import resolve_many

        urls = resolve_many(settings, [(locators[s]["url"],
                                        json.loads(locators[s]["headers"] or "{}"))
                                       for s in picked],
                            target.get("quality") or settings.play_quality)  # fmt: skip
        try:
            ha.run_script(settings, {
                "url": urls[0],
                "urls": urls,
                "channel": channel_name,
                "channel_id": target["channel_id"],
                "stream_id": picked[0],
                "command_id": cmd_id,
                "play_url": play_url(settings, target["channel_id"]),
                "program": target.get("title"),
                "start_ts": target.get("start"),
                "catchup_url": catchup_url,
            }, transport=transport)  # fmt: skip
        except ha.HaError as exc:
            error, detail = exc.code, exc.detail
    conn.execute(
        "UPDATE playback_commands SET state = ?, stream_id = COALESCE(?, stream_id), offered = ?,"
        " finished_at = ?, error = ?, detail = ? WHERE id = ?",
        ("failed" if error else "started", picked[0] if picked else None, json.dumps(picked),
         utc_now(), error, detail, cmd_id),
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
        "taken": row["taken_at"] is not None, "adapter": row["adapter"],
        "detail": row["detail"],
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
