"""Tolerant parser for extended M3U playlists (#EXTM3U / #EXTINF)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_ATTR_RE = re.compile(r'([A-Za-z0-9_-]+)="([^"]*)"')
_URL_SCHEMES = ("http://", "https://", "rtmp://", "rtsp://", "udp://", "rtp://")


@dataclass
class M3UEntry:
    name: str
    url: str
    position: int
    attrs: dict[str, str] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)
    raw: str = ""

    @property
    def tvg_id(self) -> str | None:
        return self.attrs.get("tvg-id") or None

    @property
    def group(self) -> str | None:
        return self.attrs.get("group-title") or None


@dataclass
class M3UParseResult:
    entries: list[M3UEntry]
    errors: list[str]
    has_header: bool


class M3UFormatError(ValueError):
    """The content is not an M3U playlist at all."""


def parse_m3u(content: bytes | str) -> M3UParseResult:
    text = content.decode("utf-8", errors="replace") if isinstance(content, bytes) else content
    text = text.lstrip("﻿")
    lines = [line.strip() for line in text.splitlines()]

    has_header = bool(lines) and lines[0].upper().startswith("#EXTM3U")
    entries: list[M3UEntry] = []
    errors: list[str] = []

    pending_info: str | None = None
    pending_raw: list[str] = []
    headers: dict[str, str] = {}
    group_override: str | None = None

    for lineno, line in enumerate(lines, start=1):
        if not line or line.upper().startswith("#EXTM3U"):
            continue
        if line.upper().startswith("#EXTINF:"):
            if pending_info is not None:
                errors.append(f"line {lineno}: #EXTINF without URL before it")
            pending_info, pending_raw, headers, group_override = line, [line], {}, None
            continue
        if line.upper().startswith("#EXTVLCOPT:"):
            pending_raw.append(line)
            key, _, value = line[len("#EXTVLCOPT:") :].partition("=")
            key = key.strip().lower()
            if key == "http-user-agent":
                headers["User-Agent"] = value.strip()
            elif key == "http-referrer":
                headers["Referer"] = value.strip()
            continue
        if line.upper().startswith("#EXTGRP:"):
            pending_raw.append(line)
            group_override = line[len("#EXTGRP:") :].strip() or None
            continue
        if line.startswith("#"):
            continue
        if not line.lower().startswith(_URL_SCHEMES):
            errors.append(f"line {lineno}: unsupported locator, skipped")
            pending_info = None
            continue

        attrs, name = _parse_extinf(pending_info) if pending_info else ({}, "")
        if group_override and "group-title" not in attrs:
            attrs["group-title"] = group_override
        if not name:
            name = attrs.get("tvg-name", "")
        if not name:
            errors.append(f"line {lineno}: entry without a name, skipped")
        else:
            entries.append(
                M3UEntry(
                    name=name,
                    url=line,
                    position=len(entries),
                    attrs=attrs,
                    headers=dict(headers),
                    raw="\n".join([*pending_raw, line]),
                )
            )
        pending_info, pending_raw, headers, group_override = None, [], {}, None

    if not has_header and not entries:
        raise M3UFormatError("content is not an M3U playlist")
    return M3UParseResult(entries=entries, errors=errors, has_header=has_header)


def _parse_extinf(line: str) -> tuple[dict[str, str], str]:
    body = line[len("#EXTINF:") :]
    # The display name follows the last comma that sits outside quoted attributes.
    in_quotes, split_at = False, -1
    for i, ch in enumerate(body):
        if ch == '"':
            in_quotes = not in_quotes
        elif ch == "," and not in_quotes:
            split_at = i
    head, name = (body[:split_at], body[split_at + 1 :]) if split_at >= 0 else (body, "")
    attrs = {k.lower(): v.strip() for k, v in _ATTR_RE.findall(head)}
    return attrs, name.strip()
