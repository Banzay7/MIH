"""Runtime settings, read from MIH_* environment variables."""

from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass
from datetime import time, timedelta
from pathlib import Path

EPG_SCOPES = ("tracked", "mapped", "all")
LANGS = ("en", "ru")
ALERT_LINKS = ("auto", "play", "stream", "none")
# Event levels, lowest first. "routine" events only go to the feed and the daily digest.
LEVELS = ("routine", "info", "warning", "error")


class ConfigError(ValueError):
    """Settings that would make the service unsafe or unusable."""


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    bind_host: str = "127.0.0.1"
    bind_port: int = 8765
    api_token: str | None = None
    # Spec 16, step 8: refuse a snapshot that drops more than this share of records.
    max_drop_ratio: float = 0.3
    fetch_timeout_s: float = 30.0
    fetch_max_bytes: int = 50 * 1024 * 1024
    allow_private_sources: bool = False
    # A guide is usually gzip; this caps the unpacked XML (decompression bombs).
    epg_max_bytes: int = 256 * 1024 * 1024
    # Past airings are kept this many days; other history (playlist and guide
    # snapshots) for history_days.
    epg_retention_days: int = 3
    history_days: int = 90
    # Which guide channels get a stored schedule: tracked (favourite channels and the
    # channels of the named lists, plus programmes matching a tracked topic or favourite
    # programme), mapped (every guide channel matched to the catalog) or all. tracked
    # behaves as mapped while nothing is tracked or listed yet, so a fresh install still
    # has a guide.
    epg_scope: str = "tracked"
    # IANA zone for "today", "tomorrow", "evening" in search when a request names none.
    timezone: str = "UTC"
    # Where ``mih export`` writes; None means <data_dir>/export.
    export_dir_override: Path | None = None
    # Named lists (JSON, see mih.lists.load_presets); None = one list of every valid channel.
    lists_file: Path | None = None
    # Language of push notifications and of the event feed.
    lang: str = "en"
    # ntfy topic URL (https://ntfy.sh/<topic> or a self-hosted server); None = no pushes.
    # The topic name works as a password: anyone who knows it can read the pushes.
    ntfy_url: str | None = None
    ntfy_token: str | None = None
    # Lowest level that is pushed; lower ones stay in the feed and the digest.
    notify_level: str = "info"
    # Where the web UI is reachable, for the "open" link in a push. Optional.
    public_url: str | None = None
    # Built-in scheduler of ``mih serve``. Off in this dataclass so tests and one-off CLI
    # commands never start background work; from_env turns it on unless MIH_SCHEDULER=0.
    scheduler_enabled: bool = False
    # How often each job runs; None = never on its own (still runs on request).
    every_sources: timedelta | None = timedelta(hours=6)
    every_epg: timedelta | None = timedelta(hours=12)
    every_health: timedelta | None = timedelta(minutes=10)
    # Topic alerts: how often the guide is scanned, and how long before the start a
    # programme is announced. None = no alerts.
    every_alerts: timedelta | None = timedelta(minutes=5)
    alert_lead: timedelta | None = timedelta(minutes=15)
    # What a topic alert opens: play (MIH_PUBLIC_URL/play/<channel>, a signed link that
    # picks the best stream when tapped), stream (the stream URL itself, sent to ntfy),
    # none, or auto (play when MIH_PUBLIC_URL is set, otherwise stream).
    alert_link: str = "auto"
    # Local time (MIH_TIMEZONE) of the daily digest push; None = no digest.
    digest_at: time | None = time(9, 0)
    # Playlist discovery (docs/discovery.md): how often the scout looks for new public
    # playlists and the packer checks the streams of a live one. None = off. Off in this
    # dataclass, so tests never search the internet; from_env sets 5m and 15m.
    every_discover: timedelta | None = None
    every_pack: timedelta | None = None
    # Playlists the scout samples per run, and streams the packer probes per run.
    discovery_scout: int = 5
    discovery_pack_batch: int = 60
    # The packer stops adding streams once auto-found sources hold this many.
    discovery_max_streams: int = 30000
    # How often the search for new playlists starts over (seeds, pages, GitHub searches).
    # None = daily for the first 60 days of the install, then monthly.
    discovery_search_every: timedelta | None = None
    # Optional: GitHub code search needs a token (any token, no scopes); without one
    # only repository search runs, at 60 API calls an hour.
    github_token: str | None = None
    # Optional JSON with extra pages, Telegram channels and blocked hosts.
    discovery_file: Path | None = None
    # Google Drive upload of channels.json/csv (docs/drive.md): the JSON token that
    # rclone authorize "drive" prints, the target folder's ID and how often the upload
    # runs (only when the channels changed). No token = no upload.
    drive_token: str | None = None
    drive_folder_id: str | None = None
    every_drive: timedelta | None = timedelta(hours=6)
    # MCP server for an AI agent (docs/agent.md): its own port, None = off. Loopback by
    # default; any other address needs a token (MIH_MCP_TOKEN, else MIH_API_TOKEN).
    # A daily cap on tool calls, 0 = none.
    mcp_port: int | None = None
    mcp_host: str = "127.0.0.1"
    mcp_token: str | None = None
    mcp_daily_limit: int = 0

    @property
    def db_path(self) -> Path:
        return self.data_dir / "mih.sqlite3"

    @property
    def export_dir(self) -> Path:
        return self.export_dir_override or self.data_dir / "export"

    @property
    def lists_dir(self) -> Path:
        return self.export_dir / "lists"

    @property
    def snapshots_dir(self) -> Path:
        return self.data_dir / "snapshots"

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Settings:
        env = dict(os.environ if env is None else env)
        return cls(
            data_dir=Path(env.get("MIH_DATA_DIR", "./data")),
            bind_host=env.get("MIH_BIND_HOST", "127.0.0.1"),
            bind_port=int(env.get("MIH_BIND_PORT", "8765")),
            api_token=env.get("MIH_API_TOKEN") or None,
            max_drop_ratio=float(env.get("MIH_MAX_DROP_RATIO", "0.3")),
            fetch_timeout_s=float(env.get("MIH_FETCH_TIMEOUT_S", "30")),
            fetch_max_bytes=int(env.get("MIH_FETCH_MAX_BYTES", str(50 * 1024 * 1024))),
            allow_private_sources=env.get("MIH_ALLOW_PRIVATE_SOURCES", "") == "1",
            epg_max_bytes=int(env.get("MIH_EPG_MAX_BYTES", str(256 * 1024 * 1024))),
            epg_retention_days=int(env.get("MIH_EPG_RETENTION_DAYS", "3")),
            history_days=int(env.get("MIH_HISTORY_DAYS", "90")),
            epg_scope=_choice(env.get("MIH_EPG_SCOPE", "tracked"), "MIH_EPG_SCOPE", EPG_SCOPES),
            timezone=env.get("MIH_TIMEZONE", "UTC"),
            export_dir_override=Path(env["MIH_EXPORT_DIR"]) if env.get("MIH_EXPORT_DIR") else None,
            lists_file=Path(env["MIH_LISTS_FILE"]) if env.get("MIH_LISTS_FILE") else None,
            lang=_choice(env.get("MIH_LANG", "en"), "MIH_LANG", LANGS),
            ntfy_url=env.get("MIH_NTFY_URL") or None,
            ntfy_token=env.get("MIH_NTFY_TOKEN") or None,
            notify_level=_choice(
                env.get("MIH_NOTIFY_LEVEL", "info"), "MIH_NOTIFY_LEVEL", LEVELS[1:]
            ),  # fmt: skip
            public_url=(env.get("MIH_PUBLIC_URL") or "").rstrip("/") or None,
            scheduler_enabled=env.get("MIH_SCHEDULER", "1") != "0",
            every_sources=parse_every(env.get("MIH_EVERY_SOURCES", "6h"), "MIH_EVERY_SOURCES"),
            every_epg=parse_every(env.get("MIH_EVERY_EPG", "12h"), "MIH_EVERY_EPG"),
            every_health=parse_every(env.get("MIH_EVERY_HEALTH", "10m"), "MIH_EVERY_HEALTH"),
            every_alerts=parse_every(env.get("MIH_EVERY_ALERTS", "5m"), "MIH_EVERY_ALERTS"),
            alert_lead=parse_every(env.get("MIH_ALERT_LEAD", "15m"), "MIH_ALERT_LEAD"),
            alert_link=_choice(env.get("MIH_ALERT_LINK", "auto"), "MIH_ALERT_LINK", ALERT_LINKS),
            digest_at=_parse_clock(env.get("MIH_DIGEST_AT", "09:00"), "MIH_DIGEST_AT"),
            every_discover=parse_every(env.get("MIH_EVERY_DISCOVER", "30m"), "MIH_EVERY_DISCOVER"),
            every_pack=parse_every(env.get("MIH_EVERY_PACK", "15m"), "MIH_EVERY_PACK"),
            discovery_scout=max(1, int(env.get("MIH_DISCOVERY_SCOUT", "5"))),
            discovery_pack_batch=max(1, int(env.get("MIH_DISCOVERY_PACK_BATCH", "60"))),
            discovery_max_streams=int(env.get("MIH_DISCOVERY_MAX_STREAMS", "30000")),
            discovery_search_every=_search_every(env.get("MIH_DISCOVERY_SEARCH_EVERY", "auto")),
            github_token=env.get("MIH_GITHUB_TOKEN") or None,
            discovery_file=(
                Path(env["MIH_DISCOVERY_FILE"]) if env.get("MIH_DISCOVERY_FILE") else None
            ),
            drive_token=env.get("MIH_DRIVE_TOKEN") or None,
            drive_folder_id=env.get("MIH_DRIVE_FOLDER_ID") or None,
            every_drive=parse_every(env.get("MIH_EVERY_DRIVE", "6h"), "MIH_EVERY_DRIVE"),
            mcp_port=int(env["MIH_MCP_PORT"]) if env.get("MIH_MCP_PORT") else None,
            mcp_host=env.get("MIH_MCP_HOST", "127.0.0.1"),
            mcp_token=env.get("MIH_MCP_TOKEN") or None,
            mcp_daily_limit=int(env.get("MIH_MCP_DAILY_LIMIT", "0")),
        )

    def validate_for_serving(self) -> None:
        """Spec 23: never serve without a token, except on loopback for local development."""
        if self.mcp_token and len(self.mcp_token) < 24:
            raise ConfigError("MIH_MCP_TOKEN must be at least 24 characters")
        no_token = not (self.mcp_token or self.api_token)
        if self.mcp_port is not None and no_token and not _is_loopback(self.mcp_host):
            raise ConfigError("MIH_MCP_TOKEN or MIH_API_TOKEN is required when"
                              " MIH_MCP_HOST is not a loopback address")  # fmt: skip
        if self.api_token:
            if len(self.api_token) < 24:
                raise ConfigError("MIH_API_TOKEN must be at least 24 characters")
            return
        if not _is_loopback(self.bind_host):
            raise ConfigError(
                "MIH_API_TOKEN is required when MIH_BIND_HOST is not a loopback address"
            )


def parse_every(value: str, name: str) -> timedelta | None:
    """ "30m", "6h", "1d" or "off"."""
    value = value.strip().lower()
    if value in ("off", "0", ""):
        return None
    units = {"m": "minutes", "h": "hours", "d": "days"}
    try:
        amount, unit = int(value[:-1]), units[value[-1]]
    except (ValueError, KeyError):
        raise ConfigError(f"{name} must look like 30m, 6h, 1d or off") from None
    every = timedelta(**{unit: amount})
    if every < timedelta(minutes=5):
        raise ConfigError(f"{name} must be at least 5m")
    return every


def _search_every(value: str) -> timedelta | None:
    if value.strip().lower() in ("auto", ""):
        return None
    every = parse_every(value, "MIH_DISCOVERY_SEARCH_EVERY")
    if every is None:
        raise ConfigError("MIH_DISCOVERY_SEARCH_EVERY must be auto or look like 1d, 30d")
    return every


def _parse_clock(value: str, name: str) -> time | None:
    value = value.strip().lower()
    if value in ("off", ""):
        return None
    try:
        return time.fromisoformat(value)
    except ValueError:
        raise ConfigError(f"{name} must be HH:MM or off") from None


def _choice(value: str, name: str, allowed: tuple[str, ...]) -> str:
    if value not in allowed:
        raise ConfigError(f"{name} must be one of: {', '.join(allowed)}")
    return value


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False
