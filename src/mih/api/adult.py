"""Channels 18+ in the web UI: shown, hidden, or hidden until a PIN opens them.

The mode lives on the server (table app_settings), so a child cannot switch it in the
browser. ``hide`` (the default) leaves adult channels out of the channel list, the
"on now" page, schedules, programme pages and the search, refuses their channel card and
does not play them. ``pin`` does the same until the browser sends a valid unlock token
(header ``X-MIH-Adult``), which ``POST /api/v1/adult/unlock`` gives for the right PIN and
which lasts ``UNLOCK_HOURS``. ``show`` shows everything. Changing the mode needs the PIN
once one is set. Adult means the channel's category ``adult`` (mih.categories), set
automatically or by hand.

Wrong PINs are counted per address: ``MAX_FAILS`` in ``FAIL_WINDOW_S`` lock the unlock for
that window, so a four-digit PIN cannot be guessed."""

import base64
import hashlib
import hmac
import os
import secrets
import sqlite3
import time
from typing import Any

from fastapi import FastAPI, Request
from pydantic import BaseModel, Field

from mih.api.guard import client_address
from mih.config import parse_networks
from mih.ids import utc_now

MODES = ("show", "hide", "pin")
DEFAULT_MODE = "hide"
UNLOCK_HOURS = 4
HEADER = "x-mih-adult"
MAX_FAILS = 5
FAIL_WINDOW_S = 15 * 60
_PIN_RE = r"^[0-9]{4,8}$"


class AdultSettingsIn(BaseModel):
    mode: str = Field(pattern="^(show|hide|pin)$")
    pin: str | None = Field(default=None, pattern=_PIN_RE)  # a new PIN
    current_pin: str | None = Field(default=None, max_length=8)


class UnlockIn(BaseModel):
    pin: str = Field(max_length=8)


def _get(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def _put(conn: sqlite3.Connection, key: str, value: str | None) -> None:
    if value is None:
        conn.execute("DELETE FROM app_settings WHERE key = ?", (key,))
        return
    conn.execute(
        "INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, ?)"
        " ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (key, value, utc_now()),
    )


def mode(conn: sqlite3.Connection) -> str:
    m = _get(conn, "adult_mode")
    return m if m in MODES else DEFAULT_MODE


def _hash_pin(pin: str, salt: bytes | None = None) -> str:
    salt = salt or os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", pin.encode(), salt, 200_000)
    return base64.b64encode(salt).decode() + "$" + base64.b64encode(digest).decode()


def pin_ok(conn: sqlite3.Connection, pin: str | None) -> bool:
    stored = _get(conn, "adult_pin")
    if not stored or not pin:
        return False
    salt, _, _ = stored.partition("$")
    return hmac.compare_digest(_hash_pin(pin, base64.b64decode(salt)), stored)


def _secret(conn: sqlite3.Connection) -> bytes:
    """Signs unlock tokens. A new PIN makes a new secret, so old tokens stop working."""
    s = _get(conn, "adult_secret")
    if s is None:
        s = secrets.token_hex(32)
        _put(conn, "adult_secret", s)
    return s.encode()


def make_token(conn: sqlite3.Connection, now: float | None = None) -> tuple[str, int]:
    exp = int((now or time.time()) + UNLOCK_HOURS * 3600)
    sig = hmac.new(_secret(conn), str(exp).encode(), "sha256").hexdigest()[:32]
    return f"{exp}.{sig}", exp


def token_ok(conn: sqlite3.Connection, token: str | None, now: float | None = None) -> bool:
    if not token or "." not in token:
        return False
    exp, _, sig = token.partition(".")
    if not exp.isdigit() or int(exp) < (now or time.time()):
        return False
    good = hmac.new(_secret(conn), exp.encode(), "sha256").hexdigest()[:32]
    return hmac.compare_digest(sig, good)


def visible(request: Request, conn: sqlite3.Connection) -> bool:
    """Whether this request may see adult channels."""
    m = mode(conn)
    if m == "show":
        return True
    return m == "pin" and token_ok(conn, request.headers.get(HEADER))


def adult_ids(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT channel_id FROM channel_tags WHERE kind = 'category' AND value = 'adult'"
    )
    return {r[0] for r in rows}


def hidden_ids(request: Request, conn: sqlite3.Connection) -> set[str]:
    """Channel ids this request must not see (empty when it may see them all)."""
    return set() if visible(request, conn) else adult_ids(conn)


class _Fails:
    def __init__(self) -> None:
        self.by_addr: dict[str, list[float]] = {}

    def locked(self, addr: str, now: float) -> bool:
        recent = [t for t in self.by_addr.get(addr, []) if now - t < FAIL_WINDOW_S]
        self.by_addr[addr] = recent
        return len(recent) >= MAX_FAILS

    def add(self, addr: str, now: float) -> None:
        self.by_addr.setdefault(addr, []).append(now)
        if len(self.by_addr) > 10000:  # forget the quiet ones
            self.by_addr = {a: v for a, v in self.by_addr.items() if now - v[-1] < FAIL_WINDOW_S}


def register(app: FastAPI, *, auth: list, conn: Any, error: type[Exception]) -> None:
    Conn = conn  # noqa: N806 - a type alias, as in app.py
    fails = _Fails()
    trusted = parse_networks(app.state.settings.trusted_proxies)

    def state(request: Request, c: sqlite3.Connection) -> dict:
        return {"mode": mode(c), "has_pin": _get(c, "adult_pin") is not None,
                "visible": visible(request, c), "unlock_hours": UNLOCK_HOURS}  # fmt: skip

    def guard(request: Request, c: sqlite3.Connection, pin: str | None) -> None:
        addr = client_address(request.scope, trusted)
        now = time.time()
        if fails.locked(addr, now):
            raise error("too_many_attempts", "too many wrong PINs, try again in 15 minutes")
        if not pin_ok(c, pin):
            fails.add(addr, now)
            raise error("wrong_pin", "wrong PIN")

    @app.get("/api/v1/settings/adult", dependencies=auth)
    def adult_settings(request: Request, conn: Conn) -> dict:
        return state(request, conn)

    @app.put("/api/v1/settings/adult", dependencies=auth)
    def adult_settings_set(request: Request, body: AdultSettingsIn, conn: Conn) -> dict:
        """Change the mode. Once a PIN is set, every change needs it (current_pin)."""
        has_pin = _get(conn, "adult_pin") is not None
        if has_pin:
            guard(request, conn, body.current_pin)
        if body.mode == "pin" and not (body.pin or has_pin):
            raise error("validation_error", "the pin mode needs a PIN of 4 to 8 digits")
        conn.execute("BEGIN")
        try:
            _put(conn, "adult_mode", body.mode)
            if body.pin:
                _put(conn, "adult_pin", _hash_pin(body.pin))
                _put(conn, "adult_secret", secrets.token_hex(32))  # old unlocks end
            elif body.mode == "show":
                _put(conn, "adult_pin", None)  # nothing left to protect
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        return state(request, conn)

    @app.post("/api/v1/adult/unlock", dependencies=auth)
    def adult_unlock(request: Request, body: UnlockIn, conn: Conn) -> dict:
        if mode(conn) != "pin":
            raise error("validation_error", "adult channels are not PIN-protected")
        guard(request, conn, body.pin)
        token, exp = make_token(conn)
        return {"token": token, "expires_at": exp}
