"""REST endpoints for stream health (spec 17, 22). Mounted by mih.api.app.create_app."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from mih.api.app import ApiError, Conn
from mih.config import Settings
from mih.health.policy import HealthPolicy, device_report
from mih.health.runner import record, run_checks

_STREAM_COLS = ("id, feed_id, source_id, status, availability_reason, status_changed_at,"
                " last_checked_at, next_check_at, last_ok_at, first_fail_at, streak_failures,"
                " last_device_ok_at, last_device_fail_at")  # fmt: skip


class CheckIn(BaseModel):
    stream_ids: list[str] | None = Field(default=None, max_length=500)
    limit: int | None = Field(default=None, ge=1, le=500)


class PlaybackIn(BaseModel):
    # What the player saw: the stream started, or it failed to.
    result: Literal["started", "failed"]
    vantage: Literal["device", "home"] = "device"
    startup_ms: int | None = Field(default=None, ge=0)
    error: str | None = Field(default=None, max_length=64)


def build_router(settings: Settings, policy: HealthPolicy, auth: list) -> APIRouter:
    r = APIRouter(prefix="/api/v1", dependencies=auth)

    @r.post("/streams/check")
    def check(body: CheckIn, conn: Conn) -> dict:
        """Probe due streams now (or the ones named). Blocks until the batch is done."""
        return run_checks(conn, settings, policy, stream_ids=body.stream_ids,
                          limit=body.limit).stats()  # fmt: skip

    @r.post("/streams/{stream_id}/playback")
    def report_playback(stream_id: str, body: PlaybackIn, conn: Conn) -> dict:
        """A real playback result from a TV or player: the strongest observation."""
        _require_stream(conn, stream_id)
        obs = device_report(datetime.now(UTC), body.result == "started", body.error,
                            body.vantage)  # fmt: skip
        conn.execute("BEGIN")
        decision = record(conn, stream_id, obs, policy, kind="playback",
                          startup_ms=body.startup_ms, error_kind=body.error)  # fmt: skip
        conn.execute("COMMIT")
        return {"stream_id": stream_id, "status": decision.state.status,
                "availability_reason": decision.state.availability_reason,
                "counted": decision.counted}  # fmt: skip

    @r.get("/streams/{stream_id}/health")
    def stream_health(
        stream_id: str, conn: Conn, limit: Annotated[int, Query(ge=1, le=200)] = 20
    ) -> dict:
        row = _require_stream(conn, stream_id)
        observations = conn.execute(
            "SELECT checked_at, vantage, kind, success, counted, note, error_kind, http_status,"
            " stage, startup_ms, resolution, codec, bitrate FROM stream_observations"
            " WHERE stream_id = ? ORDER BY checked_at DESC, rowid DESC LIMIT ?",
            (stream_id, limit),
        ).fetchall()
        events = conn.execute(
            "SELECT at, from_status, to_status, cause FROM stream_health_events"
            " WHERE stream_id = ? ORDER BY at DESC, rowid DESC LIMIT ?",
            (stream_id, limit),
        ).fetchall()
        return {
            **dict(row),
            "observations": [
                {**dict(o), "success": bool(o["success"]), "counted": bool(o["counted"])}
                for o in observations
            ],
            "events": [dict(e) for e in events],
        }

    @r.get("/streams/health")
    def summary(conn: Conn) -> dict:
        by_status = dict(conn.execute("SELECT status, COUNT(*) FROM streams GROUP BY status"))
        by_reason = dict(conn.execute(
            "SELECT availability_reason, COUNT(*) FROM streams"
            " WHERE availability_reason IS NOT NULL GROUP BY availability_reason"
        ))  # fmt: skip
        run = conn.execute(
            "SELECT id, started_at, finished_at, vantage, status, stats FROM health_runs"
            " ORDER BY started_at DESC, rowid DESC LIMIT 1"
        ).fetchone()
        return {"by_status": by_status, "by_reason": by_reason, "vantage": policy.vantage,
                "last_run": dict(run) if run else None}  # fmt: skip

    @r.get("/channels/{channel_id}/availability")
    def availability(channel_id: str, conn: Conn) -> dict:
        """Spec 17: a feed is playable when at least one of its streams is active."""
        if conn.execute("SELECT 1 FROM channels WHERE id = ?", (channel_id,)).fetchone() is None:
            raise ApiError("not_found", "channel not found")
        feeds = conn.execute(
            "SELECT id FROM feeds WHERE channel_id = ? ORDER BY offset_minutes, region",
            (channel_id,),
        ).fetchall()
        return {"channel_id": channel_id,
                "feeds": [_feed_availability(conn, f["id"]) for f in feeds]}  # fmt: skip

    return r


def _feed_availability(conn: sqlite3.Connection, feed_id: str) -> dict:
    rows = conn.execute(
        "SELECT id, status, availability_reason FROM streams WHERE feed_id = ?"
        # Best first: what a device last played, then the freshest successful probe.
        " ORDER BY (status = 'active' AND availability_reason IS NULL) DESC,"
        " last_device_ok_at IS NULL, last_device_ok_at DESC, last_ok_at IS NULL,"
        " last_ok_at DESC",
        (feed_id,),
    ).fetchall()
    playable = [r for r in rows if r["status"] == "active" and r["availability_reason"] is None]
    return {
        "feed_id": feed_id,
        "playable": bool(playable),
        "best_stream_id": playable[0]["id"] if playable else None,
        "streams": [dict(r) for r in rows],
    }


def _require_stream(conn: sqlite3.Connection, stream_id: str) -> sqlite3.Row:
    row = conn.execute(f"SELECT {_STREAM_COLS} FROM streams WHERE id = ?", (stream_id,)).fetchone()
    if row is None:
        raise ApiError("not_found", "stream not found")
    return row
