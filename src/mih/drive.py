"""Google Drive upload (docs/drive.md): the channel export, kept fresh in one Drive folder.

Only ``channels.json`` and ``channels.csv`` ever leave the server: channels with categories
and health, no stream URLs. Playlists and catalog files carry stream URLs and provider
tokens, so they are not on the list, and before an upload both files are scanned for any
stream URL or the API token; a hit stops the upload.

The upload goes through rclone (installed in the image). The owner gets a Drive token once
with ``rclone authorize "drive"`` on their own computer and puts it into MIH_DRIVE_TOKEN;
rclone keeps the refreshed token in <data>/drive/rclone.conf. Files are updated in place,
so a link to them keeps working.
"""

from __future__ import annotations

import configparser
import hashlib
import json
import os
import re
import sqlite3
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from mih.config import Settings
from mih.export import export_catalog
from mih.ids import utc_now

# The only files that may be uploaded. Never add playlist.m3u or catalog.* here.
UPLOAD = ("channels.json", "channels.csv")
REMOTE = "mihdrive"
_URL_RE = re.compile(r"[a-z][a-z0-9+.-]*://[^\s\"',;<>]+", re.I)
TIMEOUT_S = 600


class DriveError(Exception):
    """The upload could not run (no rclone, no token, rclone failed) or was refused."""


@dataclass
class DriveReport:
    status: str  # uploaded | unchanged
    channels: int = 0
    files: list[str] = field(default_factory=list)


def drive_dir(settings: Settings) -> Path:
    return settings.data_dir / "drive"


def sync_drive(
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    force: bool = False,
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> DriveReport:
    """Export the channels and upload them when they changed since the last upload."""
    if not settings.drive_token or not settings.drive_folder_id:
        raise DriveError("MIH_DRIVE_TOKEN and MIH_DRIVE_FOLDER_ID are not set")
    base = drive_dir(settings)
    out = base / "out"
    result = export_catalog(conn, out, with_playlist=False)
    texts = {name: (out / name).read_text(encoding="utf-8") for name in UPLOAD}
    check_no_secrets(conn, settings, texts)

    state_path = base / "state.json"
    state = _read_json(state_path)
    digest = _fingerprint(texts)
    if not force and state.get("fingerprint") == digest:
        return DriveReport("unchanged", result.channels)

    conf = write_rclone_conf(settings)
    for name in UPLOAD:
        _rclone(run, conf, "copyto", str(out / name), f"{REMOTE}:{name}")
    state.update(fingerprint=digest, uploaded_at=utc_now(), channels=result.channels)
    state_path.write_text(json.dumps(state), encoding="utf-8")
    return DriveReport("uploaded", result.channels, list(UPLOAD))


def check_no_secrets(conn: sqlite3.Connection, settings: Settings, texts: dict[str, str]) -> None:
    """Refuse to upload text that holds a stream URL or the API token."""
    urls = {r[0] for r in conn.execute("SELECT url FROM stream_locators")}
    for name, text in texts.items():
        if settings.api_token and settings.api_token in text:
            raise DriveError(f"{name} contains the API token; upload refused")
        for found in _URL_RE.findall(text):
            if found in urls or "token=" in found.lower():
                raise DriveError(f"{name} contains a stream URL; upload refused")


def write_rclone_conf(settings: Settings) -> Path:
    """rclone.conf with the Drive remote. The token rclone refreshed is kept until the
    owner puts a new one (another refresh token) into MIH_DRIVE_TOKEN."""
    try:
        token = json.loads(settings.drive_token or "")
        refresh = token["refresh_token"]
    except (ValueError, TypeError, KeyError):
        raise DriveError(
            'MIH_DRIVE_TOKEN must be the JSON that rclone authorize "drive" prints'
        ) from None
    path = drive_dir(settings) / "rclone.conf"
    cp = configparser.ConfigParser(interpolation=None)
    if path.exists():
        cp.read(path, encoding="utf-8")
    section = cp[REMOTE] if cp.has_section(REMOTE) else None
    try:
        kept = json.loads(section["token"]) if section else {}
    except (ValueError, KeyError):
        kept = {}
    if (
        section is not None
        and kept.get("refresh_token") == refresh
        and section.get("root_folder_id") == settings.drive_folder_id
    ):
        return path
    cp[REMOTE] = {
        "type": "drive",
        "scope": "drive",
        "root_folder_id": settings.drive_folder_id or "",
        "token": json.dumps(token),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        cp.write(f)
    return path


def _rclone(run: Callable[..., subprocess.CompletedProcess], conf: Path, *args: str) -> None:
    cmd = ["rclone", "--config", str(conf), "--cache-dir", "/tmp/mih-rclone",
           "--retries", "3", "--low-level-retries", "5", *args]  # fmt: skip
    try:
        done = run(cmd, capture_output=True, text=True, timeout=TIMEOUT_S, check=False)
    except FileNotFoundError:
        raise DriveError("rclone is not installed") from None
    except subprocess.TimeoutExpired:
        raise DriveError(f"rclone {args[0]} took longer than {TIMEOUT_S} s") from None
    if done.returncode != 0:
        lines = [x for x in (done.stderr or "").strip().splitlines() if x.strip()]
        raise DriveError(f"rclone {args[0]} failed: {lines[-1] if lines else done.returncode}")


def _fingerprint(texts: dict[str, str]) -> str:
    """Content without the export time, so an unchanged catalog is not uploaded again."""
    h = hashlib.sha256()
    for name in UPLOAD:
        text = texts[name]
        if name == "channels.json":
            doc = json.loads(text)
            doc.pop("generated_at", None)
            text = json.dumps(doc, ensure_ascii=False, sort_keys=True)
        h.update(name.encode() + b"\0" + text.encode("utf-8"))
    return h.hexdigest()


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
