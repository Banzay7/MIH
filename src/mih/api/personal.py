"""Favourites, tracked topics, topic alerts and playback (spec 22), and the web UI's files."""

import hmac
from datetime import UTC, datetime, timedelta
from importlib import resources
from typing import Annotated, Any

from fastapi import FastAPI, Path, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from mih import playback
from mih.alerts import GRACE, best_streams, channel_names, find_matches, load_rules, play_key
from mih.epg.scope import topic_key
from mih.ids import utc_now

ProfileId = Annotated[str, Path(pattern="^[a-z0-9_-]{1,32}$")]
_KINDS = {"channel": "channels", "programme": "programmes"}


class FavoriteIn(BaseModel):
    target_kind: str = Field(pattern="^(channel|programme)$")
    target_id: str = Field(min_length=1, max_length=64)


class TopicIn(BaseModel):
    topic: str = Field(min_length=2, max_length=64)


class PlaybackTargetIn(BaseModel):
    kind: str = Field(default="channel", pattern="^channel$")
    channel_id: str = Field(min_length=1, max_length=64)
    feed_id: str | None = Field(default=None, max_length=64)


class PlaybackIn(BaseModel):
    target: PlaybackTargetIn
    device_id: str = Field(default="default", pattern="^[A-Za-z0-9_.-]{1,64}$")
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=100)


def register(app: FastAPI, *, auth: list, conn: Any, error: type[Exception]) -> None:
    """Add the routes. ``conn`` is the connection dependency, ``error`` the ApiError class."""
    Conn = conn  # noqa: N806 - a type alias, as in app.py

    @app.get("/api/v1/profiles/{profile_id}/favorites", dependencies=auth)
    def list_favorites(profile_id: ProfileId, conn: Conn) -> dict:
        rows = conn.execute(
            "SELECT f.target_kind, f.target_id, f.position, f.created_at,"
            " c.canonical_name AS channel_name, c.logo_url, c.country,"
            " p.title AS programme_title"
            " FROM favorites f"
            " LEFT JOIN channels c ON f.target_kind = 'channel' AND c.id = f.target_id"
            " LEFT JOIN programmes p ON f.target_kind = 'programme' AND p.id = f.target_id"
            " WHERE f.profile_id = ? ORDER BY f.position",
            (profile_id,),
        ).fetchall()
        items = []
        for r in rows:
            item = {"target_kind": r["target_kind"], "target_id": r["target_id"],
                    "position": r["position"], "created_at": r["created_at"]}  # fmt: skip
            if r["target_kind"] == "channel":
                # None means the channel is gone for now; the favourite is kept.
                item.update(name=r["channel_name"], logo_url=r["logo_url"], country=r["country"])
            else:
                item.update(name=r["programme_title"])
            items.append(item)
        return {"profile_id": profile_id, "items": items}

    @app.post("/api/v1/profiles/{profile_id}/favorites", status_code=201, dependencies=auth)
    def add_favorite(profile_id: ProfileId, body: FavoriteIn, conn: Conn) -> dict:
        table = _KINDS[body.target_kind]
        if not conn.execute(f"SELECT 1 FROM {table} WHERE id = ?", (body.target_id,)).fetchone():
            raise error("not_found", f"{body.target_kind} not found")
        conn.execute("BEGIN IMMEDIATE")
        try:
            pos = conn.execute(
                "SELECT COALESCE(MAX(position), 0) + 1 FROM favorites WHERE profile_id = ?",
                (profile_id,),
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO favorites (profile_id, target_kind, target_id, position, created_at)"
                " VALUES (?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
                (profile_id, body.target_kind, body.target_id, pos, utc_now()),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        return {"profile_id": profile_id, "target_kind": body.target_kind,
                "target_id": body.target_id}  # fmt: skip

    @app.delete(
        "/api/v1/profiles/{profile_id}/favorites/{target_kind}/{target_id}",
        status_code=204, dependencies=auth,
    )  # fmt: skip
    def remove_favorite(
        profile_id: ProfileId,
        target_kind: Annotated[str, Path(pattern="^(channel|programme)$")],
        target_id: str,
        conn: Conn,
    ) -> None:
        conn.execute(
            "DELETE FROM favorites WHERE profile_id = ? AND target_kind = ? AND target_id = ?",
            (profile_id, target_kind, target_id),
        )

    # Topics decide which programmes the guide import keeps on channels that are not
    # favourites (MIH_EPG_SCOPE=tracked); a change applies from the next import.
    @app.get("/api/v1/profiles/{profile_id}/topics", dependencies=auth)
    def list_topics(profile_id: ProfileId, conn: Conn) -> dict:
        rows = conn.execute(
            "SELECT topic, created_at FROM tracked_topics WHERE profile_id = ? ORDER BY topic",
            (profile_id,),
        ).fetchall()
        return {"profile_id": profile_id, "items": [dict(r) for r in rows]}

    @app.post("/api/v1/profiles/{profile_id}/topics", status_code=201, dependencies=auth)
    def add_topic(profile_id: ProfileId, body: TopicIn, conn: Conn) -> dict:
        topic = topic_key(body.topic)
        if len(topic) < 2:
            raise error("invalid_topic", "topic is too short")
        conn.execute(
            "INSERT INTO tracked_topics (profile_id, topic, created_at) VALUES (?, ?, ?)"
            " ON CONFLICT DO NOTHING",
            (profile_id, topic, utc_now()),
        )
        return {"profile_id": profile_id, "topic": topic}

    @app.delete("/api/v1/profiles/{profile_id}/topics/{topic}", status_code=204,
                dependencies=auth)  # fmt: skip
    def remove_topic(profile_id: ProfileId, topic: str, conn: Conn) -> None:
        conn.execute(
            "DELETE FROM tracked_topics WHERE profile_id = ? AND topic = ?",
            (profile_id, topic_key(topic)),
        )

    @app.get("/api/v1/profiles/{profile_id}/alerts/upcoming", dependencies=auth)
    def upcoming_alerts(
        profile_id: ProfileId, conn: Conn, hours: Annotated[int, Query(ge=1, le=72)] = 24
    ) -> dict:
        """What the profile's topics and favourite programmes match in the coming hours,
        on channels that play: what the alerts job will announce."""
        now = datetime.now(UTC)
        rules = load_rules(conn, profile_id)
        found = find_matches(conn, now - GRACE, now + timedelta(hours=hours), now, rules)
        return {"profile_id": profile_id, "rules": len(rules),
                "items": [m.as_dict() for m in found]}  # fmt: skip

    # A link in a push: the channel's best stream when it is tapped. The key opens this
    # one channel only (mih.alerts.play_key); the API token works too.
    @app.get("/play/{channel_id}.m3u", include_in_schema=False)
    def play_m3u(request: Request, channel_id: str, conn: Conn,
                 key: str | None = None, token: str | None = None) -> Response:  # fmt: skip
        picks = _play_picks(request, conn, channel_id, key, token)
        name = channel_names(conn, {channel_id}).get(channel_id, "")
        lines = ["#EXTM3U"]
        for p in picks[:3]:  # spares for a player that moves on when one fails
            lines.append(f"#EXTINF:-1,{name.replace(',', ' ')}")
            for k, v in p.headers.items():
                opt = {"user-agent": "http-user-agent", "referer": "http-referrer"}.get(k.lower())
                if opt and v:
                    lines.append(f"#EXTVLCOPT:{opt}={v}")
            lines.append(p.url)
        return PlainTextResponse("\n".join(lines) + "\n", media_type="audio/x-mpegurl",
                                 headers={"Cache-Control": "no-store"})  # fmt: skip

    @app.get("/play/{channel_id}", include_in_schema=False)
    def play_redirect(request: Request, channel_id: str, conn: Conn,
                      key: str | None = None, token: str | None = None) -> Response:  # fmt: skip
        picks = _play_picks(request, conn, channel_id, key, token)
        # A phone's browser or player cannot send special headers: such streams go last.
        best = next((p for p in picks if not p.requires_headers), picks[0])
        return RedirectResponse(best.url, status_code=302, headers={"Cache-Control": "no-store"})

    def _play_picks(request: Request, conn, channel_id: str, key: str | None,
                    token: str | None) -> list:  # fmt: skip
        settings = request.app.state.settings
        if settings.api_token is not None:
            bearer = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
            allowed = (key and hmac.compare_digest(key, play_key(settings, channel_id))) or any(
                v and hmac.compare_digest(v, settings.api_token) for v in (token, bearer)
            )
            if not allowed:
                raise error("unauthorized", "invalid or missing key")
        picks = [p for p in best_streams(conn, {channel_id}, datetime.now(UTC)).get(channel_id, [])
                 if p.url]  # fmt: skip
        if not picks:
            raise error("not_found", "the channel has no stream that plays now")
        return picks

    @app.post("/api/v1/playback/commands", status_code=202, dependencies=auth)
    def play(request: Request, body: PlaybackIn, conn: Conn) -> JSONResponse:
        t = body.target
        settings = request.app.state.settings
        try:
            cmd, created = playback.create_command(
                conn, playback.Target(t.kind, t.channel_id, t.feed_id),
                device_id=body.device_id, idempotency_key=body.idempotency_key,
                mode=settings.playback,
            )  # fmt: skip
        except playback.PlaybackError as exc:
            raise error(exc.code, str(exc)) from exc
        if created and cmd["state"] == "accepted":
            name = channel_names(conn, {t.channel_id}).get(t.channel_id)
            playback.wake_agent(settings, cmd, name)
        return JSONResponse(status_code=202 if created else 200, content=cmd)

    @app.get("/api/v1/playback/commands/{command_id}", dependencies=auth)
    def playback_status(command_id: str, conn: Conn) -> dict:
        try:
            return playback.get_command(conn, command_id)
        except playback.PlaybackError as exc:
            raise error(exc.code, str(exc)) from exc


def mount_ui(app: FastAPI) -> None:
    """The PWA is static files; it holds no data, so it is served without a token."""
    static = resources.files("mih.ui") / "static"
    app.mount("/", StaticFiles(directory=str(static), html=True), name="ui")
