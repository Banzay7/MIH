"""Push notifications through ntfy (https://ntfy.sh or a self-hosted ntfy server).

The phone app subscribes to a topic; the service publishes to it. Sending never raises:
a push that cannot be delivered must not break the job that produced it.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import urlsplit

import httpx

from mih.config import ConfigError, Settings

log = logging.getLogger(__name__)

_PRIORITY = {"info": 3, "warning": 4, "error": 5}
_TAGS = {"info": ["tv"], "warning": ["warning"], "error": ["rotating_light"]}
_TOPIC = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def ntfy_target(url: str) -> tuple[str, str]:
    """Split https://server[/prefix]/topic into the publish endpoint and the topic."""
    parts = urlsplit(url)
    base, _, topic = parts.path.rstrip("/").rpartition("/")
    if parts.scheme not in ("https", "http") or not parts.netloc or not _TOPIC.match(topic):
        raise ConfigError("MIH_NTFY_URL must look like https://ntfy.sh/<topic>")
    return f"{parts.scheme}://{parts.netloc}{base}", topic


def send(
    settings: Settings,
    title: str,
    body: str,
    level: str = "info",
    *,
    transport: httpx.BaseTransport | None = None,
) -> str:
    """Publish one push. Returns "sent", "failed" or "off" (no channel configured)."""
    if not settings.ntfy_url:
        return "off"
    try:
        endpoint, topic = ntfy_target(settings.ntfy_url)
        # JSON publishing keeps a non-ASCII title intact; headers would need RFC 2047.
        payload = {"topic": topic, "title": title[:250], "message": body[:3500] or title,
                   "priority": _PRIORITY.get(level, 3), "tags": _TAGS.get(level, [])}  # fmt: skip
        if settings.public_url:
            payload["click"] = f"{settings.public_url}/#/status"
        headers = {"Authorization": f"Bearer {settings.ntfy_token}"} if settings.ntfy_token else {}
        with httpx.Client(timeout=10.0, transport=transport) as client:
            resp = client.post(endpoint, json=payload, headers=headers)
        if resp.status_code >= 300:
            log.warning("ntfy answered %s", resp.status_code)
            return "failed"
        return "sent"
    except (httpx.HTTPError, ConfigError) as exc:
        log.warning("push not sent: %s", exc)
        return "failed"
