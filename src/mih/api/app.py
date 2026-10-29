"""REST API v1 (spec 22): health, sources, imports, the channel catalog and the EPG.

Favourites, playback and the web UI live in mih.api.personal."""

from __future__ import annotations

import hmac
import json
import queue
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
from mih.api.cache import SharedCache, data_version, respond
from mih.config import Settings
from mih.epg import guide
from mih.epg.coverage import working_coverage
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
from mih.report import read_report, report_path, write_report
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
    offset: int = Field(default=0, ge=0, le=10000)
    strict: bool = False  # whole words only
    min_relevance: float = Field(default=0.0, ge=0.0, le=1.0)
    playable_only: bool = False
    ai: bool = False  # spec 20: AI_OFF; accepted for forward compatibility, ignored


class MappingIn(BaseModel):
    # null means "this guide channel is none of my feeds".
    feed_id: str | None


class SourceIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    url: str | None = Field(default=None, max_length=4000)
    kind: str = "m3u"


_bearer = HTTPBearer(auto_error=False)


class ConnPool:
    """Connections kept open between requests: opening one costs a file open and four
    PRAGMAs, more than most of the queries behind the web UI."""

    def __init__(self, path, keep: int = 16):
        self.path, self.keep = path, keep
        self._idle: queue.SimpleQueue[sqlite3.Connection] = queue.SimpleQueue()

    def take(self) -> sqlite3.Connection:
        try:
            return self._idle.get_nowait()
        except queue.Empty:
            return db.connect(self.path)

    def give(self, c: sqlite3.Connection) -> None:
        if c.in_transaction:  # a request that failed half way
            c.execute("ROLLBACK")
        if self._idle.qsize() < self.keep:
            self._idle.put(c)
        else:
            c.close()


def get_conn(request: Request) -> Iterator[sqlite3.Connection]:
    pool: ConnPool = request.app.state.pool
    c = pool.take()
    try:
        yield c
    except BaseException:
        c.close()
        raise
    else:
        pool.give(c)


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
        if settings.workers > 1:
            yield  # ``mih serve`` runs the jobs once for all its workers
            return
        # Imported here: the scheduler pulls in every job, the API module stays light.
        from mih.scheduler import background

        with background(settings, health_policy):
            yield

    app = FastAPI(title="MIRTVONLINE API", version=__version__, docs_url="/api/docs",
                  openapi_url="/api/v1/openapi.json", lifespan=lifespan)  # fmt: skip
    app.state.settings = settings
    app.state.pool = pool = ConnPool(settings.db_path)

    def probe() -> str:
        c = pool.take()
        try:
            return data_version(c)
        finally:
            pool.give(c)

    app.state.cache = cache = SharedCache(probe)
    app.add_middleware(_ForgetOnChange, cache=cache)
    app.add_middleware(_Gzip)

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

    @app.get("/api/v1/data-version")
    def data_version_now() -> JSONResponse:
        """The web UI asks this on every screen: while it is the same, the copies it keeps
        are still good. Says nothing about the data, so no token is needed."""
        return JSONResponse({"data": cache.check()}, headers={"Cache-Control": "no-store"})

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
        request: Request,
        conn: Conn,
        q: str | None = None,
        limit: Annotated[int, Query(ge=1, le=10000)] = 100,
        offset: Annotated[int, Query(ge=0)] = 0,
    ):
        if q:
            return _channels(conn, q, limit, offset)
        # The whole catalog is what every visitor loads first: one copy for everyone.
        entry = cache.get(f"channels:{limit}:{offset}", CHANNELS_TTL,
                          lambda: _channels(conn, None, limit, offset))  # fmt: skip
        return respond(request, entry)

    def _channels(conn: sqlite3.Connection, q: str | None, limit: int, offset: int) -> dict:
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

    @app.get("/api/v1/epg/coverage", dependencies=auth)
    def epg_coverage(request: Request, conn: Conn):
        """Working channels with a guide, by country (most channels without one first),
        and the public guides the autopilot tried (MIH_EPG_AUTO). It walks the whole
        catalog, so one copy is kept for COVERAGE_TTL."""
        entry = cache.get("epg-coverage", COVERAGE_TTL, lambda: _coverage(conn))
        return respond(request, entry)

    def _coverage(conn: sqlite3.Connection) -> dict:
        tried = conn.execute(
            "SELECT guide, country, verdict, gain, added_at, judged_at FROM epg_auto"
            " ORDER BY added_at DESC"
        ).fetchall()
        return {**working_coverage(conn).to_dict(), "autopilot": settings.epg_auto,
                "guides_tried": [dict(r) for r in tried]}  # fmt: skip

    @app.get("/api/v1/report", dependencies=auth)
    def coverage_report(request: Request, conn: Conn):
        """The «Покрытие» tab: what plays, what has a guide, by country and source, and
        what the playlist scout brought in (mih.report). The background jobs keep the
        file current; only a service that never wrote one builds it here."""
        entry = cache.get("report", CHANNELS_TTL, lambda: _report(conn))
        return respond(request, entry)

    def _report(conn: sqlite3.Connection) -> dict:
        path = report_path(settings.data_dir)
        return read_report(path) or write_report(conn, path, every=timedelta(0)) or {}

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
        request: Request,
        conn: Conn,
        channel_id: Annotated[list[str] | None, Query()] = None,
        timeshift: bool = False,
        on_air: bool = False,
        soon: Annotated[int | None, Query(ge=1, le=24 * 60)] = None,
        playable_only: bool = False,
        limit: Annotated[int | None, Query(ge=1, le=10000)] = None,
        offset: Annotated[int, Query(ge=0)] = 0,
    ):
        """What is on now and next, per feed. +N feeds are left out unless asked for.

        on_air keeps feeds with something on now; soon=N keeps those whose next show
        starts within N minutes, soonest first; limit/offset page the result, and total
        counts it. The UI asks for one page of "on air" instead of the whole guide."""
        ids = ",".join(sorted(channel_id)) if channel_id else "*"
        paged = on_air or soon or playable_only or limit is not None or offset
        if not paged:
            key = f"now:{timeshift}:{ids}"
            entry = cache.get(key, NOW_TTL, lambda: _on_now(conn, channel_id, timeshift))
            return respond(request, entry)
        key = f"now:{timeshift}:{ids}:{on_air}:{soon}:{playable_only}:{limit}:{offset}"
        base = f"now-base:{timeshift}:{ids}"

        def build() -> dict:
            data = cache.remember(base, NOW_TTL, lambda: _on_now(conn, channel_id, timeshift))
            items = data["items"]
            if on_air:
                items = [i for i in items if i["now"]]
            if playable_only:
                items = [i for i in items if i["streams"]]
            if soon:
                horizon = iso(datetime.now(UTC) + timedelta(minutes=soon))
                items = sorted((i for i in items if i["next"] and i["next"]["start"] <= horizon),
                               key=lambda i: i["next"]["start"])  # fmt: skip
            end = None if limit is None else offset + limit
            return {"items": items[offset:end], "total": len(items), "offset": offset,
                    "channels": data["channels"], "guided": data["guided"]}  # fmt: skip

        entry = cache.get(key, NOW_TTL, build)
        return respond(request, entry)

    def _on_now(conn: sqlite3.Connection, channel_id: list[str] | None,
                timeshift: bool) -> dict:  # fmt: skip
        guides = guide.feed_guides(conn, channel_id)
        if not timeshift:
            guides = {k: g for k, g in guides.items() if g.shift_minutes == 0}
        state = guide.now_and_next(conn, datetime.now(UTC), guides)
        info = _channel_cards(conn, {g.channel_id for g in guides.values()})
        items = [
            {"channel_id": g.channel_id, "channel_name": info.get(g.channel_id, {}).get("name"),
             "logo_url": info.get(g.channel_id, {}).get("logo_url"),
             "streams": info.get(g.channel_id, {}).get("streams", 0),
             "feed_id": g.feed_id, **state[g.feed_id]}
            for g in guides.values()
        ]  # fmt: skip
        items.sort(key=lambda i: ((i["channel_name"] or "").casefold(), i["feed_id"]))
        channels = conn.execute("SELECT COUNT(*) FROM channels").fetchone()[0]
        return {"items": items, "channels": channels,
                "guided": len({g.channel_id for g in guides.values()})}  # fmt: skip

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
                            channel_ids=f.channel_ids, tz=tz, limit=body.limit,
                            offset=body.offset, strict=body.strict,
                            min_relevance=body.min_relevance,
                            playable_only=body.playable_only)  # fmt: skip
        return search(conn, req, datetime.now(UTC))

    @app.get("/api/v1/metrics/summary", dependencies=auth)
    def metrics_summary(request: Request, conn: Conn):
        entry = cache.get("metrics", CHANNELS_TTL, lambda: _metrics(conn))
        return respond(request, entry)

    def _metrics(conn: sqlite3.Connection) -> dict:
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


# How long a shared answer is reused (seconds). Catalog answers only change with the data
# version, which drops them anyway; "now" moves on programme boundaries.
CHANNELS_TTL = 3600.0
COVERAGE_TTL = 3600.0
NOW_TTL = 30.0


def app_from_env() -> FastAPI:
    """For ``mih serve`` with MIH_WORKERS > 1: each worker process builds its own app."""
    return create_app(Settings.from_env())


class _ForgetOnChange:
    """A change made through the API shows at once; the background jobs' changes show
    once the data version moves. Every API answer carries that version (X-MIH-Data), so
    the web UI knows when its own copies are out of date. Plain ASGI: Starlette's
    @middleware("http") costs every request a task and a queue."""

    def __init__(self, app, cache: SharedCache):
        self.app, self.cache = app, cache

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope["path"].startswith("/api/"):
            return await self.app(scope, receive, send)
        version = self.cache.check().encode()

        async def send_version(message):
            if message["type"] == "http.response.start" and version:
                message["headers"] = [*message.get("headers", []), (b"x-mih-data", version)]
            await send(message)

        await self.app(scope, receive, send_version)
        if scope["method"] not in ("GET", "HEAD", "OPTIONS"):
            self.cache.clear()
            self.cache.check(now=True)


class _Gzip:
    """Gzip for the API and the web UI's files. Playlists and guides keep their bytes as
    they are; some players mishandle an encoding. Level 6: level 9 costs several times
    the CPU for a few percent less."""

    def __init__(self, app):
        self.app = app
        self.gzip = GZipMiddleware(app, minimum_size=1024, compresslevel=6)

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and not scope["path"].startswith(("/lists/", "/play/")):
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


def _channel_cards(conn: sqlite3.Connection, ids: set[str]) -> dict[str, dict]:
    """Name, logo and playable stream count: what a card shows next to the guide."""
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    rows = conn.execute(
        "SELECT c.id, c.canonical_name, c.logo_url, COALESCE(sc.n, 0) AS streams FROM channels c"
        " LEFT JOIN (SELECT f.channel_id, COUNT(*) AS n FROM streams s"
        "   JOIN feeds f ON f.id = s.feed_id WHERE s.availability_reason IS NULL"
        f"   AND f.channel_id IN ({marks}) GROUP BY f.channel_id) sc ON sc.channel_id = c.id"
        f" WHERE c.id IN ({marks})",
        (*ids, *ids),
    ).fetchall()
    return {r["id"]: {"name": r["canonical_name"], "logo_url": r["logo_url"],
                      "streams": r["streams"]} for r in rows}  # fmt: skip


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
