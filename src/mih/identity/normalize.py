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
    "240p", "360p", "480i", "480p", "540p", "576i", "576p", "720p", "1080i", "1080p", "1440p",
    "2160p", "50fps", "60fps",
    "orig", "original", "backup", "reserve", "резерв",
}  # fmt: skip
_QUALITY_ORDER = ["sd", "hd", "fhd", "uhd", "4k", "8k"]
_RESOLUTION = {
    "240p": "sd", "360p": "sd", "480i": "sd", "576i": "sd",
    "480p": "sd", "540p": "sd", "576p": "sd", "720p": "hd", "1080i": "fhd",
    "1080p": "fhd", "1440p": "uhd", "2160p": "uhd",
}  # fmt: skip

_OFFSET_RE = re.compile(r"(?<![\w+])\+\s?(\d{1,2})(?:\s?(?:h|ч))?(?![\w])", re.IGNORECASE)
# Labels iptv-org playlists append to names: "Channel One (576i) [Geo-blocked]".
_LABEL_RE = re.compile(r"\[\s*(?:geo-?blocked|not 24/7)\s*\]", re.IGNORECASE)
# Free-TV marks names with circled letters (Ⓖ geo-blocked, Ⓨ YouTube...); NFKC would
# turn them into plain letters that become part of the name.
_CIRCLED_RE = re.compile("[\u24b6-\u24e9]")
# Any "576i", "406p", "1080p" is a resolution, whatever the exact number.
_RES_RE = re.compile(r"^(\d{3,4})[pi]$")
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


def tvg_base(tvg_id: str) -> str:
    """Channel part of a tvg-id: iptv-org's "ChannelOne.ru@HD" names a feed of ChannelOne.ru."""
    return tvg_id.split("@", 1)[0].strip().casefold()


def _is_cyrillic(ch: str) -> bool:
    return "CYRILLIC" in unicodedata.name(ch, "")


def split_name(name: str) -> NameParts:
    name = _CIRCLED_RE.sub(" ", _LABEL_RE.sub(" ", name))
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
        rest = [t for t in tokens if not _is_quality(t)]
        if rest:
            regions.append(" ".join(rest))
    text = _PAREN_RE.sub(" ", text)

    tokens = [t for t in _SPLIT_RE.split(text) if t and t != "+"]
    quality = _best_quality(quality, tokens)
    core = [t for t in tokens if not _is_quality(t)]
    if not core:  # a name made only of quality words, keep it rather than lose it
        core = tokens

    brand = _OFFSET_RE.sub(" ", unicodedata.normalize("NFKC", name))
    brand = _PAREN_RE.sub(lambda m: " " if _only_quality(m.group(1)) else m.group(0), brand)
    return NameParts(
        key=" ".join(core),
        display=name.strip(),
        brand_display=" ".join(brand.split()) or name.strip(),
        offset_minutes=offset,
        region=" ".join(regions),
        quality=quality,
    )


def _only_quality(inner: str) -> bool:
    return all(_is_quality(t) for t in _SPLIT_RE.split(fold(inner)) if t)


def _is_quality(token: str) -> bool:
    return token in _QUALITY or bool(_RES_RE.match(token))


def _res_class(token: str) -> str:
    lines = int(_RES_RE.match(token).group(1))
    return "uhd" if lines >= 1440 else "fhd" if lines >= 1080 else "hd" if lines >= 720 else "sd"


def _best_quality(current: str | None, tokens: list[str]) -> str | None:
    found = [_RESOLUTION.get(t, t) if t in _QUALITY else _res_class(t)
             for t in tokens if _is_quality(t)]  # fmt: skip
    found = [q for q in found if q in _QUALITY_ORDER]
    if current:
        found.append(current)
    if not found:
        return None
    return max(found, key=_QUALITY_ORDER.index)


_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ж": "zh", "з": "z",
    "и": "i", "й": "i", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p",
    "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "c", "ч": "ch",
    "ш": "sh", "щ": "sh", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
    "є": "e", "і": "i", "ї": "i", "ґ": "g", "ў": "u", "қ": "k", "ң": "n", "ү": "u",
    "ұ": "u", "ө": "o", "һ": "h", "ә": "a", "ғ": "g",
}  # fmt: skip
# Spellings Latin transliterations of one Cyrillic name differ in: "Perviy"/"Pervyy",
# "Pyatnitsa"/"Piatnitsa", "Kukhnya"/"Kuhnya", "2x2"/"2х2".
_SPELLING = (("shch", "sh"), ("kh", "h"), ("x", "h"), ("ts", "c"), ("tz", "c"), ("j", "i"),
             ("y", "i"), ("w", "v"))  # fmt: skip
_REPEAT_RE = re.compile(r"(\w)\1+")


def translit_key(key: str) -> str:
    """A name key (``split_name().key``) in a Latin spelling that the usual
    transliterations of one Cyrillic name share: "первый канал" and "perviy kanal"
    both give "pervi kanal". Used only as a last resort when matching names."""
    text = "".join(_TRANSLIT.get(ch, ch) for ch in key)
    for old, new in _SPELLING:
        text = text.replace(old, new)
    return _REPEAT_RE.sub(r"\1", text)
