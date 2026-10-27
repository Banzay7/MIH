"""REST API v1 (spec 22): health, sources, imports, the channel catalog and the EPG.

Favourites, playback and the web UI live in mih.api.personal."""

from __future__ import annotations

import hmac
import json
import sqlite3
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Annotated
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import Depends, FastAPI, File, HTTPException, Path, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field
from starlette.middleware.gzip import GZipMiddleware

from mih import __version__, db
from mih.api import personal
from mih.config import Settings
from mih.epg import guide
from mih.epg.importer import EPGImportReport, import_xmltv, iso
from mih.epg.mapping import refresh_mappings, set_manual
from mih.epg.service import (
    create_epg_source,
    delete_epg_source,
    get_epg_source,
    import_epg_from_url,
    set_epg_source_enabled,
)
from mih.health.policy import HealthPolicy
from mih.identity.normalize import fold
from mih.ids import utc_now
from mih.search.engine import SearchRequest, search
from mih.search.index import ensure_index
from mih.sources.fetch import FetchError
from mih.sources.importer import ImportReport, import_m3u
from mih.sources.service import SourceError, create_source, get_source, import_from_url

_STATUS = {"unauthorized": 401, "not_found": 404, "conflict": 409, "validation_error": 422,
           "destination_blocked": 422, "source_unavailable": 502, "too_large": 413}  # fmt: skip


class ApiError(Exception):
    def __init__(self, code: str, message: str):
        self.code, self.message = code, message


class EPGSourceIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    url: str | None = Field(default=None, max_length=4000)


class EPGSourcePatch(BaseModel):
    enabled: bool


class SearchFilters(BaseModel):
    languages: list[str] = Field(default_factory=list, max_length=10)
    time: str | None = Field(default=None, pattern="^(now|today|tomorrow|tonight|yesterday)$")
    start: datetime | None = Field(default=None, alias="from")
    end: datetime | None = Field(default=None, alias="to")
    channel_ids: list[str] = Field(default_factory=list, max_length=500)


class SearchIn(BaseModel):
    query: str = Field(default="", max_length=500)
    target_types: list[str] = Field(default_factory=lambda: ["airing", "channel"])
    filters: SearchFilters = Field(default_factory=SearchFilters)
    tz: str | None = Field(default=None, max_length=64)
    limit: int = Field(default=20, ge=1, le=100)
    ai: bool = False  # spec 20: AI_OFF; accepted for forward compatibility, ignored


class MappingIn(BaseModel):
    # null means "this guide channel is none of my feeds".
    feed_id: str | None


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


def require_token_or_query(
    request: Request,
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    token: Annotated[str | None, Query()] = None,
) -> None:
    """Like require_token, but a player that cannot send headers may pass ?token=."""
    if token is not None and request.app.state.settings.api_token is not None:
        if hmac.compare_digest(token, request.app.state.settings.api_token):
            return
        raise HTTPException(status_code=401, detail="invalid or missing token")
    require_token(request, creds)


Conn = Annotated[sqlite3.Connection, Depends(get_conn)]


def create_app(settings: Settings, health_policy: HealthPolicy | None = None) -> FastAPI:
    conn = db.connect(settings.db_path)
    db.migrate(conn)
    ensure_index(conn)
    conn.close()
    health_policy = health_policy or HealthPolicy.from_env()
    started_at = utc_now()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        # Imported here: the scheduler pulls in every job, the API module stays light.
        from mih.scheduler import Scheduler, service_event

        scheduler = Scheduler(settings, health_policy) if settings.scheduler_enabled else None
        service_event(settings, "service.started")
        if scheduler:
            scheduler.start()
        try:
            yield
        finally:
            if scheduler:
                scheduler.stop()
            service_event(settings, "service.stopped")

    app = FastAPI(title="MIRTVONLINE API", version=__version__, docs_url="/api/docs",
                  openapi_url="/api/v1/openapi.json", lifespan=lifespan)  # fmt: skip
    app.state.settings = settings
    app.add_middleware(_ApiGzip)
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
        limit: Annotated[int, Query(ge=1, le=10000)] = 100,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> dict:
        where, params = "", []
        if q:
            where = (" WHERE c.id IN (SELECT channel_id FROM channel_aliases"
                     " WHERE kind = 'name' AND value LIKE ? ESCAPE '\\')")  # fmt: skip
            params.append("%" + _like_escape(fold(q)) + "%")
        # Counts are grouped once and joined: a subquery per row made every page cost
        # as much as all the rows before it, seconds for a catalog of a few thousand.
        rows = conn.execute(
            "SELECT c.id, c.canonical_name, c.country, c.logo_url,"
            " COALESCE(fc.n, 0) AS feeds, COALESCE(sc.n, 0) AS streams,"
            f" c.id IN ({_EPG_CHANNELS}) AS has_epg FROM channels c"
            " LEFT JOIN (SELECT channel_id, COUNT(*) AS n FROM feeds GROUP BY channel_id) fc"
            "   ON fc.channel_id = c.id"
            " LEFT JOIN (SELECT f.channel_id, COUNT(*) AS n FROM streams s"
            "   JOIN feeds f ON f.id = s.feed_id WHERE s.availability_reason IS NULL"
            "   GROUP BY f.channel_id) sc ON sc.channel_id = c.id"
            f"{where} ORDER BY c.canonical_name COLLATE NOCASE LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ).fetchall()
        items = [{**dict(r), "has_epg": bool(r["has_epg"])} for r in rows]
        return {"items": items, "limit": limit, "offset": offset}

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
        guides = guide.feed_guides(conn, [channel_id])
        out_feeds = []
        for f in feeds:
            # Locators are deliberately not selected: URLs never leave the server here.
            streams = conn.execute(
                "SELECT s.id, s.source_id, s.format, s.quality_label, s.status,"
                " s.availability_reason, s.first_seen_at, s.last_seen_at"
                " FROM streams s WHERE s.feed_id = ? ORDER BY s.first_seen_at",
                (f["id"],),
            ).fetchall()
            out_feeds.append({**dict(f), "epg": _epg_out(guides.get(f["id"])),
                              "streams": [dict(s) for s in streams]})  # fmt: skip
        aliases = conn.execute(
            "SELECT kind, display FROM channel_aliases WHERE channel_id = ? ORDER BY kind",
            (channel_id,),
        ).fetchall()
        return {**dict(ch), "aliases": [dict(a) for a in aliases], "feeds": out_feeds}

    @app.post("/api/v1/epg/sources", status_code=201, dependencies=auth)
    def add_epg_source(body: EPGSourceIn, conn: Conn) -> dict:
        try:
            source_id = create_epg_source(conn, settings, name=body.name, url=body.url)
        except (SourceError, FetchError) as exc:
            raise ApiError(exc.code, str(exc)) from exc
        return _source_out(get_epg_source(conn, source_id), kind="xmltv")

    @app.get("/api/v1/epg/sources", dependencies=auth)
    def list_epg_sources(conn: Conn) -> dict:
        rows = conn.execute("SELECT * FROM epg_sources ORDER BY name").fetchall()
        return {"items": [_source_out(r, kind="xmltv") for r in rows]}

    @app.patch("/api/v1/epg/sources/{source_id}", dependencies=auth)
    def patch_epg_source(source_id: str, body: EPGSourcePatch, conn: Conn) -> dict:
        """Switch a guide off (kept, not refreshed, not used for schedules) or on again."""
        _require_epg_source(conn, source_id)
        set_epg_source_enabled(conn, source_id, body.enabled)
        return _source_out(get_epg_source(conn, source_id), kind="xmltv")

    @app.delete("/api/v1/epg/sources/{source_id}", dependencies=auth)
    def remove_epg_source(source_id: str, conn: Conn) -> dict:
        _require_epg_source(conn, source_id)
        return {"deleted": delete_epg_source(conn, settings, source_id)}

    @app.get("/api/v1/epg/sources/{source_id}/snapshots", dependencies=auth)
    def list_epg_snapshots(source_id: str, conn: Conn) -> dict:
        _require_epg_source(conn, source_id)
        rows = conn.execute(
            "SELECT id, fetched_at, content_hash, channel_count, programme_count, status,"
            " reason, stats FROM epg_snapshots WHERE epg_source_id = ?"
            " ORDER BY fetched_at DESC, rowid DESC",
            (source_id,),
        ).fetchall()
        return {"items": [{**dict(r), "stats": json.loads(r["stats"] or "{}")} for r in rows]}

    @app.post("/api/v1/epg/sources/{source_id}/import", dependencies=auth)
    def run_epg_import(source_id: str, conn: Conn, force: bool = False) -> dict:
        _require_epg_source(conn, source_id)
        try:
            report = import_epg_from_url(conn, settings, source_id, force=force)
        except (SourceError, FetchError) as exc:
            raise ApiError(exc.code, str(exc)) from exc
        if report is None:
            return {"status": "not_modified"}
        return _report_out(report)

    @app.post("/api/v1/epg/sources/{source_id}/upload", dependencies=auth)
    def upload_epg(
        source_id: str, conn: Conn, file: Annotated[UploadFile, File()], force: bool = False
    ) -> dict:
        _require_epg_source(conn, source_id)
        content = file.file.read(settings.fetch_max_bytes + 1)
        if len(content) > settings.fetch_max_bytes:
            raise ApiError("too_large", "file exceeds MIH_FETCH_MAX_BYTES")
        return _report_out(import_xmltv(conn, settings, source_id, content, force=force))

    @app.get("/api/v1/epg/channels", dependencies=auth)
    def list_epg_channels(
        conn: Conn,
        mapping: Annotated[
            str | None, Query(pattern="^(auto|needs_review|approved|rejected|unmapped)$")
        ] = None,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> dict:
        where, params = "", []
        if mapping == "unmapped":
            where = " WHERE m.decision IS NULL"
        elif mapping:
            where, params = " WHERE m.decision = ?", [mapping]
        rows = conn.execute(
            "SELECT ec.id, ec.epg_source_id, ec.xmltv_id, ec.display_names, ec.last_seen_at,"
            " m.feed_id, m.rule, m.decision, m.decided_by, m.evidence"
            " FROM epg_channels ec LEFT JOIN epg_mappings m ON m.epg_channel_id = ec.id"
            f"{where} ORDER BY ec.xmltv_id LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ).fetchall()
        items = []
        for r in rows:
            evidence = json.loads(r["evidence"]) if r["evidence"] else {}
            items.append({
                "id": r["id"], "epg_source_id": r["epg_source_id"], "xmltv_id": r["xmltv_id"],
                "display_names": [n for _, n in json.loads(r["display_names"])],
                "last_seen_at": r["last_seen_at"], "feed_id": r["feed_id"],
                "rule": r["rule"], "decision": r["decision"] or "unmapped",
                "decided_by": r["decided_by"], "candidates": evidence.get("candidates", []),
            })  # fmt: skip
        return {"items": items, "limit": limit, "offset": offset}

    @app.put("/api/v1/epg/channels/{epg_channel_id}/mapping", dependencies=auth)
    def set_epg_mapping(epg_channel_id: str, body: MappingIn, conn: Conn) -> dict:
        _require_epg_channel(conn, epg_channel_id)
        if (
            body.feed_id
            and not conn.execute("SELECT 1 FROM feeds WHERE id = ?", (body.feed_id,)).fetchone()
        ):
            raise ApiError("validation_error", "feed not found")
        set_manual(conn, epg_channel_id, body.feed_id, _now_s())
        return {"epg_channel_id": epg_channel_id, "feed_id": body.feed_id,
                "decision": "approved" if body.feed_id else "rejected"}  # fmt: skip

    @app.delete("/api/v1/epg/channels/{epg_channel_id}/mapping", dependencies=auth)
    def reset_epg_mapping(epg_channel_id: str, conn: Conn) -> dict:
        """Forget a person's decision and let the automatic rules map it again."""
        _require_epg_channel(conn, epg_channel_id)
        conn.execute("BEGIN")
        conn.execute("DELETE FROM epg_mappings WHERE epg_channel_id = ?", (epg_channel_id,))
        refresh_mappings(conn, _now_s())
        conn.execute("COMMIT")
        row = conn.execute(
            "SELECT feed_id, decision FROM epg_mappings WHERE epg_channel_id = ?",
            (epg_channel_id,),
        ).fetchone()
        return {"epg_channel_id": epg_channel_id, "feed_id": row["feed_id"] if row else None,
                "decision": row["decision"] if row else "unmapped"}  # fmt: skip

    @app.get("/api/v1/now", dependencies=auth)
    def on_now(
        conn: Conn,
        channel_id: Annotated[list[str] | None, Query()] = None,
        timeshift: bool = False,
    ) -> dict:
        """What is on now and next, per feed. +N feeds are left out unless asked for."""
        guides = guide.feed_guides(conn, channel_id)
        if not timeshift:
            guides = {k: g for k, g in guides.items() if g.shift_minutes == 0}
        state = guide.now_and_next(conn, datetime.now(UTC), guides)
        names = _channel_names(conn, {g.channel_id for g in guides.values()})
        items = [
            {"channel_id": g.channel_id, "channel_name": names.get(g.channel_id),
             "feed_id": g.feed_id, **state[g.feed_id]}
            for g in guides.values()
        ]  # fmt: skip
        items.sort(key=lambda i: ((i["channel_name"] or "").casefold(), i["feed_id"]))
        return {"items": items}

    @app.get("/api/v1/airings", dependencies=auth)
    def list_airings(
        conn: Conn,
        channel_id: str,
        start: Annotated[datetime | None, Query(alias="from")] = None,
        end: Annotated[datetime | None, Query(alias="to")] = None,
    ) -> dict:
        start = _aware(start) if start else datetime.now(UTC)
        end = _aware(end) if end else start + timedelta(hours=24)
        if end <= start or end - start > timedelta(days=8):
            raise ApiError("validation_error", "'to' must be after 'from', at most 8 days")
        if conn.execute("SELECT 1 FROM channels WHERE id = ?", (channel_id,)).fetchone() is None:
            raise ApiError("not_found", "channel not found")
        guides = guide.feed_guides(conn, [channel_id])
        feeds = [{"feed_id": g.feed_id, **_epg_out(g),
                  "airings": guide.schedule(conn, g, start, end)}
                 for g in guides.values()]  # fmt: skip
        return {"channel_id": channel_id, "from": iso(start), "to": iso(end), "feeds": feeds}

    @app.get("/api/v1/programmes/{programme_id}", dependencies=auth)
    def programme_detail(programme_id: str, conn: Conn) -> dict:
        p = conn.execute("SELECT * FROM programmes WHERE id = ?", (programme_id,)).fetchone()
        if p is None:
            raise ApiError("not_found", "programme not found")
        guides = guide.feed_guides(conn)
        by_epg: dict[str, list] = {}
        for g in guides.values():
            by_epg.setdefault(g.epg_channel_id, []).append(g)
        rows = conn.execute(
            f"SELECT {guide.AIRING_COLS} FROM airings a JOIN programmes p"
            " ON p.id = a.programme_id WHERE a.programme_id = ? AND a.status = 'scheduled'"
            " ORDER BY a.start_utc",
            (programme_id,),
        ).fetchall()
        airings = [
            guide.airing_out(r, g) for r in rows for g in by_epg.get(r["epg_channel_id"], [])
        ]
        airings.sort(key=lambda a: a["start"])
        localized = json.loads(p["localized"])
        return {
            "id": p["id"], "title": p["title"], "sub_title": p["sub_title"],
            "description": p["description"], "episode_num": p["episode_num"],
            "categories": json.loads(p["categories"]), "lang": p["lang"],
            "icon_url": p["icon_url"], "rating": p["rating"],
            "titles": [{"lang": lang, "text": t} for lang, t in localized["titles"]],
            "airings": airings,
        }  # fmt: skip

    @app.post("/api/v1/search", dependencies=auth)
    def run_search(body: SearchIn, conn: Conn) -> dict:
        tz = body.tz or settings.timezone
        try:
            ZoneInfo(tz)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ApiError("validation_error", f"unknown time zone {tz!r}") from exc
        unknown = set(body.target_types) - {"airing", "channel"}
        if unknown:
            raise ApiError("validation_error", f"unsupported target_types: {sorted(unknown)}")
        f = body.filters
        req = SearchRequest(query=body.query, target_types=body.target_types,
                            languages=f.languages, time=f.time,
                            start=_aware(f.start) if f.start else None,
                            end=_aware(f.end) if f.end else None,
                            channel_ids=f.channel_ids, tz=tz, limit=body.limit)  # fmt: skip
        return search(conn, req, datetime.now(UTC))

    @app.get("/api/v1/metrics/summary", dependencies=auth)
    def metrics_summary(conn: Conn) -> dict:
        channels = conn.execute("SELECT COUNT(*) FROM channels").fetchone()[0]
        with_epg = conn.execute(f"SELECT COUNT(*) FROM ({_EPG_CHANNELS})").fetchone()[0]
        review = conn.execute(
            "SELECT COUNT(*) FROM epg_mappings WHERE decision = 'needs_review'"
        ).fetchone()[0]
        return {"channels": channels, "channels_with_epg": with_epg,
                "epg_coverage": round(with_epg / channels, 3) if channels else None,
                "epg_mappings_needs_review": review}  # fmt: skip

    @app.get("/api/v1/lists", dependencies=auth)
    def lists_index() -> dict:
        """Named lists built so far (MIH_LISTS_FILE), rebuilt after every stream check."""
        out = []
        for f in sorted(settings.lists_dir.glob("*.json")):
            try:
                doc = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            out.append({"name": f.stem, "generated_at": doc.get("generated_at"),
                        "channels": doc.get("channels_total"), "entries": doc.get("entries"),
                        "by_level": doc.get("by_level"), "playlist": f"/lists/{f.stem}.m3u",
                        "guide": f"/lists/{f.stem}.xml.gz" if doc.get("guide") else None,
                        "with_guide": doc.get("with_guide"),
                        "excluded_by_reason": doc.get("excluded_by_reason")})  # fmt: skip
        return {"items": out}

    @app.get("/lists/{file}", dependencies=[Depends(require_token_or_query)])
    def list_file(file: Annotated[str, Path(pattern=r"^[\w.-]+\.(m3u|jsonl?|csv|xml\.gz)$")]):
        path = settings.lists_dir / file
        if not path.is_file():
            raise ApiError("not_found", "no such list; see GET /api/v1/lists")
        if file.endswith(".xml.gz"):
            # Players unpack the guide themselves; a gzip Content-Encoding would make
            # some of them get plain XML under a .gz name.
            return FileResponse(path, media_type="application/gzip",
                                headers={"Cache-Control": "no-store"})  # fmt: skip
        media = {"m3u": "audio/x-mpegurl", "json": "application/json",
                 "jsonl": "application/x-ndjson", "csv": "text/csv"}  # fmt: skip
        media = media[file.rsplit(".", 1)[1]]
        return FileResponse(path, media_type=media + "; charset=utf-8",
                            headers={"Cache-Control": "no-store"})  # fmt: skip

    # Imported here because the health routes reuse this module's error and connection.
    from mih.api import status
    from mih.health.api import build_router

    app.include_router(build_router(settings, health_policy, auth))
    app.include_router(status.build_router(settings, health_policy, auth, started_at))
    personal.register(app, auth=auth, conn=Conn, error=ApiError)
    personal.mount_ui(app)  # last: it catches every path the API did not
    return app


class _ApiGzip:
    """Gzip for /api answers only: the channel list and "now" are hundreds of KB of JSON.
    Playlists and guides keep their bytes as they are; some players mishandle an encoding."""

    def __init__(self, app):
        self.app, self.gzip = app, GZipMiddleware(app, minimum_size=1024)

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"].startswith("/api/"):
            return await self.gzip(scope, receive, send)
        return await self.app(scope, receive, send)


# A channel has EPG when any of its feeds has a guide of its own; +N feeds borrow it.
_EPG_CHANNELS = (
    "SELECT DISTINCT f.channel_id FROM epg_mappings m JOIN feeds f ON f.id = m.feed_id"
    " WHERE m.decision IN ('auto', 'approved')"
)


def _epg_out(g: guide.FeedGuide | None) -> dict:
    if g is None:
        return {"available": False}
    return {"available": True, "timeshift_minutes": g.shift_minutes,
            "borrowed_from_feed_id": g.borrowed_from}  # fmt: skip


def _channel_names(conn: sqlite3.Connection, ids: set[str]) -> dict[str, str]:
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT id, canonical_name FROM channels WHERE id IN ({marks})", tuple(ids)
    ).fetchall()
    return {r["id"]: r["canonical_name"] for r in rows}


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _now_s() -> str:
    return iso(datetime.now(UTC))


def _require_epg_source(conn: sqlite3.Connection, source_id: str) -> None:
    try:
        get_epg_source(conn, source_id)
    except SourceError as exc:
        raise ApiError(exc.code, str(exc)) from exc


def _require_epg_channel(conn: sqlite3.Connection, epg_channel_id: str) -> None:
    row = conn.execute("SELECT 1 FROM epg_channels WHERE id = ?", (epg_channel_id,)).fetchone()
    if row is None:
        raise ApiError("not_found", "EPG channel not found")


def _require_source(conn: sqlite3.Connection, source_id: str) -> None:
    try:
        get_source(conn, source_id)
    except SourceError as exc:
        raise ApiError(exc.code, str(exc)) from exc


def _source_out(row: sqlite3.Row, kind: str | None = None) -> dict:
    # The URL can embed a subscriber token, so only say whether one is set.
    return {"id": row["id"], "kind": kind or row["kind"], "name": row["name"],
            "has_url": bool(row["url"]), "enabled": bool(row["enabled"]),
            "priority": row["priority"], "created_at": row["created_at"]}  # fmt: skip


def _report_out(report: ImportReport | EPGImportReport) -> dict:
    out = report.stats()
    out["parse_errors_sample"] = report.parse_errors[:20]
    return out


def _like_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
