"""MCP server (Model Context Protocol) over the catalog, for an AI agent.

Two transports, same tools (mih.mcp.tools):

- HTTP: ``POST /mcp`` on its own port (MIH_MCP_PORT), Streamable HTTP without sessions:
  every request gets one JSON answer, nothing is streamed. Bearer token MIH_MCP_TOKEN
  (or MIH_API_TOKEN). It listens on loopback unless told otherwise.
- stdio: ``mih mcp``, one JSON-RPC message per line, for an agent on the same host
  (``docker exec -i <container> mih mcp``). No network at all.

MIH_MCP_DAILY_LIMIT caps tool calls per UTC day (0 = no cap), so a shared server can
hand out tokens later; for one household it stays off.
"""

from __future__ import annotations

import hmac
import json
import sys
import threading
from datetime import UTC, datetime
from typing import Any, TextIO

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from mih import __version__, db
from mih.config import Settings
from mih.mcp.tools import TOOLS, Toolbox, ToolError

PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
INSTRUCTIONS = (
    "TV catalog. To turn on a channel: channels(q) -> play(id) -> start the first stream "
    "-> report(stream_id, ok). To pick something to watch: search('футбол вечером') or "
    "now(). Times are local (tz in answers): 'HH:MM' today, 'MM-DD HH:MM' other days."
)

# JSON-RPC error codes.
PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS = -32700, -32600, -32601, -32602
LIMIT_REACHED = -32000


class DailyLimit:
    def __init__(self, limit: int):
        self.limit = limit
        self._day, self._used = "", 0
        self._lock = threading.Lock()

    def take(self) -> bool:
        if self.limit <= 0:
            return True
        with self._lock:
            day = datetime.now(UTC).strftime("%Y-%m-%d")
            if day != self._day:
                self._day, self._used = day, 0
            if self._used >= self.limit:
                return False
            self._used += 1
            return True


class McpServer:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.toolbox = Toolbox(settings)
        self.limit = DailyLimit(settings.mcp_daily_limit)
        conn = db.connect(settings.db_path)
        try:
            db.migrate(conn)
        finally:
            conn.close()

    def handle(self, message: Any) -> Any:
        """One JSON-RPC message (or a batch) in, the answer out; None for notifications."""
        if isinstance(message, list):
            out = [r for r in (self._one(m) for m in message) if r is not None]
            return out or None
        return self._one(message)

    def _one(self, msg: Any) -> dict | None:
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or "method" not in msg:
            if isinstance(msg, dict) and ("result" in msg or "error" in msg):
                return None  # an answer to a request we never send
            return _error(None, INVALID_REQUEST, "not a JSON-RPC 2.0 request")
        rid, method, params = msg.get("id"), msg["method"], msg.get("params") or {}
        if "id" not in msg:
            return None  # notifications/initialized, notifications/cancelled, ...
        try:
            result = self._dispatch(method, params)
        except _RpcError as exc:
            return _error(rid, exc.code, exc.message)
        return {"jsonrpc": "2.0", "id": rid, "result": result}

    def _dispatch(self, method: str, params: dict) -> dict:
        if method == "initialize":
            asked = params.get("protocolVersion")
            return {
                "protocolVersion": asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "mirtvonline", "version": __version__},
                "instructions": INSTRUCTIONS,
            }
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": [
                {"name": t.name, "description": t.description, "inputSchema": t.schema,
                 "annotations": {"readOnlyHint": t.read_only, "openWorldHint": False}}
                for t in TOOLS
            ]}  # fmt: skip
        if method == "tools/call":
            name, args = params.get("name"), params.get("arguments") or {}
            if not isinstance(name, str) or not isinstance(args, dict):
                raise _RpcError(INVALID_PARAMS, "tools/call needs name and arguments")
            if not self.limit.take():
                raise _RpcError(LIMIT_REACHED, "daily limit of tool calls reached")
            return self.call_tool(name, args)
        raise _RpcError(METHOD_NOT_FOUND, f"method {method!r} not supported")

    def call_tool(self, name: str, args: dict) -> dict:
        conn = db.connect(self.settings.db_path)
        try:
            res = self.toolbox.call(conn, name, args)
        except ToolError as exc:
            return {"content": [{"type": "text", "text": str(exc)}], "isError": True}
        finally:
            conn.close()
        text = json.dumps(res, ensure_ascii=False, separators=(",", ":"))
        return {"content": [{"type": "text", "text": text}], "isError": False}


class _RpcError(Exception):
    def __init__(self, code: int, message: str):
        self.code, self.message = code, message


def _error(rid: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


# ---------- HTTP ----------


def create_mcp_app(settings: Settings) -> FastAPI:
    server = McpServer(settings)
    token = settings.mcp_token or settings.api_token
    app = FastAPI(title="MIRTVONLINE MCP", version=__version__, docs_url=None,
                  redoc_url=None, openapi_url=None)  # fmt: skip
    app.state.mcp = server

    def _denied(request: Request) -> Response | None:
        # A web page in a browser on this host must not reach the server (DNS rebinding).
        origin = request.headers.get("origin")
        if origin and not _local_origin(origin):
            return JSONResponse({"error": "origin not allowed"}, status_code=403)
        if token is None:
            return None  # loopback-only development mode, as for the API
        given = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
        if not given or not hmac.compare_digest(given, token):
            return JSONResponse({"error": "invalid or missing token"}, status_code=401,
                                headers={"WWW-Authenticate": "Bearer"})  # fmt: skip
        return None

    @app.post("/mcp")
    async def mcp_post(request: Request) -> Response:
        if (denied := _denied(request)) is not None:
            return denied
        try:
            message = json.loads(await request.body())
        except (json.JSONDecodeError, UnicodeDecodeError):
            return JSONResponse(_error(None, PARSE_ERROR, "invalid JSON"), status_code=400)
        # The tools read SQLite: run them off the event loop.
        answer = await run_in_threadpool(server.handle, message)
        if answer is None:
            return Response(status_code=202)
        return Response(json.dumps(answer, ensure_ascii=False, separators=(",", ":")),
                        media_type="application/json")  # fmt: skip

    @app.get("/mcp")
    def mcp_get() -> Response:
        # No server-initiated messages: there is no stream to open.
        return Response(status_code=405, headers={"Allow": "POST"})

    @app.delete("/mcp")
    def mcp_delete() -> Response:
        return Response(status_code=405, headers={"Allow": "POST"})

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    return app


def _local_origin(origin: str) -> bool:
    from urllib.parse import urlsplit

    host = (urlsplit(origin).hostname or "").lower()
    return host in ("localhost", "127.0.0.1", "::1")


# ---------- stdio ----------


def run_stdio(settings: Settings, stdin: TextIO = sys.stdin, stdout: TextIO = sys.stdout) -> int:
    server = McpServer(settings)
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            answer = server.handle(json.loads(line))
        except json.JSONDecodeError:
            answer = _error(None, PARSE_ERROR, "invalid JSON")
        if answer is not None:
            stdout.write(json.dumps(answer, ensure_ascii=False, separators=(",", ":")) + "\n")
            stdout.flush()
    return 0
