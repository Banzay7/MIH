"""Streaming XMLTV parser (spec 19). Times are normalized to UTC, the original kept."""

from __future__ import annotations

import gzip
import io
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from typing import BinaryIO

from defusedxml.ElementTree import iterparse

_TIME_RE = re.compile(
    r"^(\d{4})(\d{2})(\d{2})(\d{2})?(\d{2})?(\d{2})?\s*([+-]\d{2}:?\d{2}|Z|UTC|GMT)?$"
)


class XMLTVFormatError(ValueError):
    """The content is not XMLTV, or it is larger than allowed once unpacked."""


@dataclass
class XMLTVChannel:
    xmltv_id: str
    display_names: list[tuple[str | None, str]] = field(default_factory=list)  # (lang, name)
    icon: str | None = None


@dataclass
class XMLTVProgramme:
    channel: str
    start_utc: datetime
    stop_utc: datetime | None
    start_raw: str
    titles: list[tuple[str | None, str]]
    sub_titles: list[tuple[str | None, str]] = field(default_factory=list)
    descs: list[tuple[str | None, str]] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)
    episode_num: str | None = None
    icon: str | None = None
    rating: str | None = None
    lang: str | None = None


@dataclass
class XMLTVParseResult:
    channels: dict[str, XMLTVChannel]
    programmes: list[XMLTVProgramme]
    errors: list[str]


def parse_time(value: str) -> datetime:
    """XMLTV 'YYYYMMDDhhmmss +hhmm'; missing parts are zero, a missing zone means UTC."""
    m = _TIME_RE.match(value.strip())
    if not m:
        raise ValueError(f"bad time {value!r}")
    y, mo, d, h, mi, s, tz = m.groups()
    offset = timedelta(0)
    if tz and tz not in ("Z", "UTC", "GMT"):
        sign = -1 if tz[0] == "-" else 1
        digits = tz[1:].replace(":", "")
        offset = sign * timedelta(hours=int(digits[:2]), minutes=int(digits[2:]))
    local = datetime(int(y), int(mo), int(d), int(h or 0), int(mi or 0), int(s or 0),
                     tzinfo=timezone(offset))  # fmt: skip
    return local.astimezone(UTC)


def open_content(content: bytes, max_bytes: int) -> BinaryIO:
    """Plain or gzip XMLTV as a stream that refuses to unpack past ``max_bytes``."""
    raw: BinaryIO = io.BytesIO(content)
    if content[:2] == b"\x1f\x8b":
        raw = gzip.GzipFile(fileobj=raw)
    return _Capped(raw, max_bytes)


def parse_xmltv(stream: BinaryIO) -> XMLTVParseResult:
    channels: dict[str, XMLTVChannel] = {}
    programmes: list[XMLTVProgramme] = []
    errors: list[str] = []
    saw_root = False
    try:
        for event, elem in iterparse(stream, events=("start", "end")):
            if event == "start":
                if not saw_root:
                    if elem.tag != "tv":
                        raise XMLTVFormatError("root element is not <tv>")
                    saw_root = True
                continue
            if elem.tag == "channel":
                ch = _channel(elem)
                if ch:
                    channels[ch.xmltv_id] = ch
                elem.clear()
            elif elem.tag == "programme":
                try:
                    programmes.append(_programme(elem))
                except ValueError as exc:
                    errors.append(str(exc))
                elem.clear()
    except XMLTVFormatError:
        raise
    except Exception as exc:  # ParseError, defusedxml refusals, gzip errors
        # A truncated or broken file is refused as a whole: applying half a guide would
        # make the missing channels look as if they had nothing on.
        what = "not XMLTV" if not saw_root else "broken XMLTV"
        raise XMLTVFormatError(f"{what}: {type(exc).__name__}") from exc
    if not saw_root:
        raise XMLTVFormatError("empty document")
    _fill_missing_stops(programmes, errors)
    return XMLTVParseResult(channels, programmes, errors)


def _channel(elem) -> XMLTVChannel | None:
    xmltv_id = (elem.get("id") or "").strip()
    if not xmltv_id:
        return None
    ch = XMLTVChannel(xmltv_id)
    for dn in elem.findall("display-name"):
        if dn.text and dn.text.strip():
            ch.display_names.append((dn.get("lang"), dn.text.strip()))
    icon = elem.find("icon")
    if icon is not None:
        ch.icon = icon.get("src")
    return ch


def _programme(elem) -> XMLTVProgramme:
    channel = (elem.get("channel") or "").strip()
    start_raw = elem.get("start") or ""
    if not channel or not start_raw:
        raise ValueError("programme without channel or start")
    titles = _texts(elem, "title")
    if not titles:
        raise ValueError(f"programme on {channel} at {start_raw} has no title")
    stop_raw = elem.get("stop")
    start = parse_time(start_raw)
    stop = parse_time(stop_raw) if stop_raw else None
    if stop is not None and stop <= start:
        raise ValueError(f"programme on {channel} at {start_raw} ends before it starts")
    episode = None
    for ep in elem.findall("episode-num"):
        if ep.text and ep.text.strip():
            # onscreen ("S02E05") is what people read; xmltv_ns is the fallback.
            episode = ep.text.strip()
            if ep.get("system") == "onscreen":
                break
    icon = elem.find("icon")
    rating = elem.find("rating/value")
    return XMLTVProgramme(
        channel=channel,
        start_utc=start,
        stop_utc=stop,
        start_raw=start_raw,
        titles=titles,
        sub_titles=_texts(elem, "sub-title"),
        descs=_texts(elem, "desc"),
        categories=[t for _, t in _texts(elem, "category")],
        episode_num=episode,
        icon=icon.get("src") if icon is not None else None,
        rating=rating.text.strip() if rating is not None and rating.text else None,
        lang=titles[0][0],
    )


def _texts(elem, tag: str) -> list[tuple[str | None, str]]:
    return [(e.get("lang"), e.text.strip()) for e in elem.findall(tag) if e.text and e.text.strip()]


def _fill_missing_stops(programmes: list[XMLTVProgramme], errors: list[str]) -> None:
    """A programme without stop ends when the next one on its channel starts."""
    by_channel: dict[str, list[XMLTVProgramme]] = {}
    for p in programmes:
        by_channel.setdefault(p.channel, []).append(p)
    drop: set[int] = set()
    for items in by_channel.values():
        items.sort(key=lambda p: p.start_utc)
        for i, p in enumerate(items):
            if p.stop_utc is not None:
                continue
            nxt = next((q for q in items[i + 1 :] if q.start_utc > p.start_utc), None)
            if nxt is None:
                errors.append(f"programme on {p.channel} at {p.start_raw} has no stop time")
                drop.add(id(p))
            else:
                p.stop_utc = nxt.start_utc
    if drop:
        programmes[:] = [p for p in programmes if id(p) not in drop]


class _Capped(io.RawIOBase):
    def __init__(self, inner: BinaryIO, limit: int):
        self._inner, self._left = inner, limit

    def readable(self) -> bool:
        return True

    def readinto(self, b) -> int:
        data = self._inner.read(min(len(b), self._left + 1))
        if len(data) > self._left:
            raise XMLTVFormatError("EPG is larger than MIH_EPG_MAX_BYTES once unpacked")
        self._left -= len(data)
        b[: len(data)] = data
        return len(data)
