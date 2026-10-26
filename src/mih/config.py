"""Runtime settings, read from MIH_* environment variables."""

from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass
from pathlib import Path


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

    @property
    def db_path(self) -> Path:
        return self.data_dir / "mih.sqlite3"

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
        )

    def validate_for_serving(self) -> None:
        """Spec 23: never serve without a token, except on loopback for local development."""
        if self.api_token:
            if len(self.api_token) < 24:
                raise ConfigError("MIH_API_TOKEN must be at least 24 characters")
            return
        if not _is_loopback(self.bind_host):
            raise ConfigError(
                "MIH_API_TOKEN is required when MIH_BIND_HOST is not a loopback address"
            )


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False
