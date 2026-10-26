"""Turn a playlist display name into a channel key plus feed markers.

"Первый канал HD +2" and "Первый Канал (+2)" must land on the same channel and the
same +2 feed, while "Первый канал" without an offset is a different feed of that
channel (spec 3.1: never merge timeshift or regional versions).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

_QUALITY = {
    "sd", "hd", "fhd", "uhd", "hq", "4k", "8k", "hevc", "h264", "h265", "avc",
    "480p", "540p", "576p", "720p", "1080i", "1080p", "1440p", "2160p", "50fps", "60fps",
    "orig", "original", "backup", "reserve", "резерв",
}  # fmt: skip
_QUALITY_ORDER = ["sd", "hd", "fhd", "uhd", "4k", "8k"]
_RESOLUTION = {
    "480p": "sd", "540p": "sd", "576p": "sd", "720p": "hd", "1080i": "fhd",
    "1080p": "fhd", "1440p": "uhd", "2160p": "uhd",
}  # fmt: skip

_OFFSET_RE = re.compile(r"(?<![\w+])\+\s?(\d{1,2})(?:\s?(?:h|ч))?(?![\w])", re.IGNORECASE)
_PAREN_RE = re.compile(r"[\(\[]([^\)\]]*)[\)\]]")
_SPLIT_RE = re.compile(r"[^\w+]+", re.UNICODE)


@dataclass(frozen=True)
class NameParts:
    key: str
    display: str
    brand_display: str
    offset_minutes: int
    region: str
    quality: str | None


def fold(text: str) -> str:
    """Case, width and diacritics folding used for every comparison key."""
    text = unicodedata.normalize("NFKC", text).casefold()
    # й is a separate letter in Russian, so fold diacritics only outside Cyrillic.
    out = []
    for ch in unicodedata.normalize("NFD", text):
        if unicodedata.combining(ch) and out and not _is_cyrillic(out[-1]):
            continue
        out.append(ch)
    text = unicodedata.normalize("NFC", "".join(out))
    # Playlists spell the same name with and without ё.
    return text.replace("đ", "d").replace("ё", "е")


def _is_cyrillic(ch: str) -> bool:
    return "CYRILLIC" in unicodedata.name(ch, "")


def split_name(name: str) -> NameParts:
    text = fold(name)

    offset = 0
    m = _OFFSET_RE.search(text)
    if m:
        offset = int(m.group(1)) * 60
        text = text[: m.start()] + " " + text[m.end() :]

    regions, quality = [], None
    for inner in _PAREN_RE.findall(text):
        tokens = [t for t in _SPLIT_RE.split(inner) if t]
        quality = _best_quality(quality, tokens)
        rest = [t for t in tokens if t not in _QUALITY]
        if rest:
            regions.append(" ".join(rest))
    text = _PAREN_RE.sub(" ", text)

    tokens = [t for t in _SPLIT_RE.split(text) if t and t != "+"]
    quality = _best_quality(quality, tokens)
    core = [t for t in tokens if t not in _QUALITY]
    if not core:  # a name made only of quality words, keep it rather than lose it
        core = tokens

    brand = _OFFSET_RE.sub(" ", unicodedata.normalize("NFKC", name))
    brand = re.sub(r"[\(\[]\s*[\)\]]", " ", brand)
    return NameParts(
        key=" ".join(core),
        display=name.strip(),
        brand_display=" ".join(brand.split()) or name.strip(),
        offset_minutes=offset,
        region=" ".join(regions),
        quality=quality,
    )


def _best_quality(current: str | None, tokens: list[str]) -> str | None:
    found = [_RESOLUTION.get(t, t) for t in tokens if t in _QUALITY]
    found = [q for q in found if q in _QUALITY_ORDER]
    if current:
        found.append(current)
    if not found:
        return None
    return max(found, key=_QUALITY_ORDER.index)
