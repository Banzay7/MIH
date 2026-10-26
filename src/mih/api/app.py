"""REST API v1 (spec 22): health, sources, imports and the channel catalog."""

from __future__ import annotations

import hmac
import json
import sqlite3
from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from mih import __version__, db
from mih.config import Settings
from mih.identity.normalize import fold
from mih.sources.fetch import FetchError
from mih.sources.importer import ImportReport, import_m3u
from mih.sources.service import SourceError, create_source, get_source, import_from_url

_STATUS = {"not_found": 404, "conflict": 409, "validation_error": 422,
           "destination_blocked": 422, "source_unavailable": 502, "too_large": 413}  # fmt: skip


class ApiError(Exception):
    def __init__(self, code: str, message: str):
        self.code, self.message = code, message


class SourceIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    url: str | None = Field(default=None, max_length=4000)
    kind: str = "m3u"


_bearer = HTTPBearer(auto_error=False)


def get_conn(request: Request) -> Iterator[sqlite3.Connection]:
    c = db.connect(request.app.state.settings.db_path)
    try:
        yield c
    finally:
        c.close()


def require_token(
    request: Request,
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> None:
    token = request.app.state.settings.api_token
    if token is None:
        return  # loopback-only development mode, enforced by Settings.validate_for_serving
    if creds is None or not hmac.compare_digest(creds.credentials, token):
        raise HTTPException(status_code=401, detail="invalid or missing token")


Conn = Annotated[sqlite3.Connection, Depends(get_conn)]


def create_app(settings: Settings) -> FastAPI:
    conn = db.connect(settings.db_path)
    db.migrate(conn)
    conn.close()

    app = FastAPI(title="MIRTVONLINE API", version=__version__, docs_url="/api/docs",
                  openapi_url="/api/v1/openapi.json")  # fmt: skip
    app.state.settings = settings
    auth = [Depends(require_token)]

    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(
            status_code=_STATUS.get(exc.code, 400),
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    @app.get("/api/v1/health/live")
    def live() -> dict:
        return {"status": "ok", "version": __version__}

    @app.get("/api/v1/health/ready", dependencies=auth)
    def ready(conn: Conn) -> dict:
        conn.execute("SELECT 1 FROM schema_migrations LIMIT 1")
        return {"status": "ready"}

    @app.post("/api/v1/sources", status_code=201, dependencies=auth)
    def add_source(body: SourceIn, conn: Conn) -> dict:
        try:
            source_id = create_source(conn, settings, name=body.name, url=body.url, kind=body.kind)
        except (SourceError, FetchError) as exc:
            raise ApiError(exc.code, str(exc)) from exc
        return _source_out(get_source(conn, source_id))

    @app.get("/api/v1/sources", dependencies=auth)
    def list_sources(conn: Conn) -> dict:
        rows = conn.execute("SELECT * FROM sources ORDER BY name").fetchall()
        return {"items": [_source_out(r) for r in rows]}

    @app.get("/api/v1/sources/{source_id}/snapshots", dependencies=auth)
    def list_snapshots(source_id: str, conn: Conn) -> dict:
        _require_source(conn, source_id)
        rows = conn.execute(
            "SELECT id, fetched_at, content_hash, item_count, status, reason, stats"
            " FROM source_snapshots WHERE source_id = ? ORDER BY fetched_at DESC, rowid DESC",
            (source_id,),
        ).fetchall()
        return {"items": [{**dict(r), "stats": json.loads(r["stats"] or "{}")} for r in rows]}

    @app.post("/api/v1/sources/{source_id}/import", dependencies=auth)
    def run_import(source_id: str, conn: Conn, force: bool = False) -> dict:
        _require_source(conn, source_id)
        try:
            report = import_from_url(conn, settings, source_id, force=force)
        except (SourceError, FetchError) as exc:
            raise ApiError(exc.code, str(exc)) from exc
        if report is None:
            return {"status": "not_modified"}
        return _report_out(report)

    @app.post("/api/v1/sources/{source_id}/upload", dependencies=auth)
    def upload(
        source_id: str, conn: Conn, file: Annotated[UploadFile, File()], force: bool = False
    ) -> dict:
        _require_source(conn, source_id)
        content = file.file.read(settings.fetch_max_bytes + 1)
        if len(content) > settings.fetch_max_bytes:
            raise ApiError("too_large", "file exceeds MIH_FETCH_MAX_BYTES")
        return _report_out(import_m3u(conn, settings, source_id, content, force=force))

    @app.get("/api/v1/channels", dependencies=auth)
    def list_channels(
        conn: Conn,
        q: str | None = None,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> dict:
        where, params = "", []
        if q:
            where = (" WHERE c.id IN (SELECT channel_id FROM channel_aliases"
                     " WHERE kind = 'name' AND value LIKE ? ESCAPE '\\')")  # fmt: skip
            params.append("%" + _like_escape(fold(q)) + "%")
        rows = conn.execute(
            "SELECT c.id, c.canonical_name, c.country, c.logo_url,"
            " (SELECT COUNT(*) FROM feeds f WHERE f.channel_id = c.id) AS feeds,"
            " (SELECT COUNT(*) FROM streams s JOIN feeds f ON f.id = s.feed_id"
            "   WHERE f.channel_id = c.id AND s.availability_reason IS NULL) AS streams"
            f" FROM channels c{where} ORDER BY c.canonical_name COLLATE NOCASE"
            " LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ).fetchall()
        return {"items": [dict(r) for r in rows], "limit": limit, "offset": offset}

    @app.get("/api/v1/channels/{channel_id}", dependencies=auth)
    def channel_detail(channel_id: str, conn: Conn) -> dict:
        ch = conn.execute("SELECT * FROM channels WHERE id = ?", (channel_id,)).fetchone()
        if ch is None:
            raise ApiError("not_found", "channel not found")
        feeds = conn.execute(
            "SELECT id, region, offset_minutes, audio_lang FROM feeds WHERE channel_id = ?"
            " ORDER BY offset_minutes, region",
            (channel_id,),
        ).fetchall()
        out_feeds = []
        for f in feeds:
            # Locators are deliberately not selected: URLs never leave the server here.
            streams = conn.execute(
                "SELECT s.id, s.source_id, s.format, s.quality_label, s.status,"
                " s.availability_reason, s.first_seen_at, s.last_seen_at"
                " FROM streams s WHERE s.feed_id = ? ORDER BY s.first_seen_at",
                (f["id"],),
            ).fetchall()
            out_feeds.append({**dict(f), "streams": [dict(s) for s in streams]})
        aliases = conn.execute(
            "SELECT kind, display FROM channel_aliases WHERE channel_id = ? ORDER BY kind",
            (channel_id,),
        ).fetchall()
        return {**dict(ch), "aliases": [dict(a) for a in aliases], "feeds": out_feeds}

    return app


def _require_source(conn: sqlite3.Connection, source_id: str) -> None:
    try:
        get_source(conn, source_id)
    except SourceError as exc:
        raise ApiError(exc.code, str(exc)) from exc


def _source_out(row: sqlite3.Row) -> dict:
    # The URL can embed a subscriber token, so only say whether one is set.
    return {"id": row["id"], "kind": row["kind"], "name": row["name"],
            "has_url": bool(row["url"]), "enabled": bool(row["enabled"]),
            "priority": row["priority"], "created_at": row["created_at"]}  # fmt: skip


def _report_out(report: ImportReport) -> dict:
    out = report.stats()
    out["parse_errors_sample"] = report.parse_errors[:20]
    return out


def _like_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
