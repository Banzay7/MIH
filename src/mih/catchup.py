"""Catch-up (play a running or past programme from its start) from the playlist's own
``catchup*`` attributes. MIRTVONLINE builds the URL only when the source gave a rule for
it; otherwise the TV plays the live stream and the command says so."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

_TEMPLATE_MODES = {"default", "append", "shift", "flussonic", "fs", "xc"}


def _ts(dt: datetime) -> int:
    return int(dt.astimezone(UTC).timestamp())


def _fill(template: str, start: datetime, stop: datetime, now: datetime) -> str:
    start_ts, stop_ts, now_ts = _ts(start), _ts(stop), _ts(now)
    values = {
        "utc": start_ts, "start": start_ts, "timestamp": start_ts, "utcstart": start_ts,
        "utcend": stop_ts, "end": stop_ts,
        "lutc": now_ts, "now": now_ts,
        "offset": max(0, now_ts - start_ts),
        "duration": max(0, stop_ts - start_ts),
    }  # fmt: skip
    out = template
    for key, value in values.items():
        out = out.replace("${" + key + "}", str(value)).replace("{" + key + "}", str(value))
    return out


def build_url(
    attrs: dict[str, str], live_url: str, start: datetime, stop: datetime, now: datetime
) -> str | None:
    """The catch-up URL for a programme that runs ``start``..``stop``, or None when the
    source gave no usable rule or the programme is older than ``catchup-days``."""
    mode = (attrs.get("catchup") or attrs.get("catchup-type") or "").strip().lower()
    source = (attrs.get("catchup-source") or "").strip()
    if not mode and not source:
        return None
    if mode and mode not in _TEMPLATE_MODES:
        return None
    try:
        days = float(attrs.get("catchup-days") or 0)
    except ValueError:
        days = 0
    if days and (now - start).total_seconds() > days * 86400:
        return None
    if mode in ("flussonic", "fs") and not source:
        base = live_url.split("?", 1)[0].rsplit("/", 1)
        if len(base) != 2 or not base[1]:
            return None
        duration = max(0, _ts(stop) - _ts(start))
        return f"{base[0]}/video-{_ts(start)}-{duration}.m3u8"
    if mode == "shift" and not source:
        sep = "&" if "?" in live_url else "?"
        return f"{live_url}{sep}utc={_ts(start)}&lutc={_ts(now)}"
    if not source:
        return None
    url = _fill(source, start, stop, now)
    if mode == "append" or (not url.startswith(("http://", "https://")) and mode != "default"):
        return live_url + url
    if not url.startswith(("http://", "https://")):
        return None
    return url


def stream_attrs(conn: sqlite3.Connection, stream_id: str) -> dict[str, str]:
    """The playlist attributes of the stream's latest record."""
    row = conn.execute(
        "SELECT r.normalized_payload FROM source_records r"
        " JOIN source_snapshots s ON s.id = r.snapshot_id"
        " WHERE r.stream_id = ? ORDER BY s.fetched_at DESC LIMIT 1",
        (stream_id,),
    ).fetchone()
    if row is None:
        return {}
    try:
        attrs = json.loads(row["normalized_payload"]).get("attrs") or {}
    except (ValueError, AttributeError):
        return {}
    return {str(k): str(v) for k, v in attrs.items()}
