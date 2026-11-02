"""Countries and their subdivisions (states, provinces, regions) by name.

Playlists sort channels into groups named after a country («Italy», «USA», «українські»)
or a part of one («California», «Asturias, Principado de», «Busan-gwangyeoksi»). Such a
group says where a channel is from, not what it shows; a group or a channel name with a
subdivision of the channel's own country says the channel is a local one.

The names come from iptv-org/database (places.csv next to this file); a few more are the
words playlists use in their own languages.
"""

from __future__ import annotations

import csv
import re
from functools import cache
from pathlib import Path

from mih.identity.normalize import fold

# Group names in other languages and short forms -> country code.
COUNTRY_ALIASES: dict[str, str] = {
    "usa": "US", "us": "US", "united states of america": "US", "america": "US",
    "uk": "GB", "great britain": "GB", "england": "GB", "britain": "GB",
    "turkey": "TR", "türkiye": "TR", "türk": "TR", "turk": "TR", "türkçe": "TR",
    "czechia": "CZ", "korea": "KR", "holland": "NL", "uae": "AE",
    "україна": "UA", "українські": "UA", "украина": "UA", "украинские": "UA",
    "россия": "RU", "российские": "RU", "русские": "RU", "беларусь": "BY", "беларускія": "BY",
    "белорусские": "BY", "казахстан": "KZ", "қазақстан": "KZ", "казахские": "KZ",
    "ישראלי": "IL", "ישראל": "IL", "moldovenească": "MD", "moldova": "MD",
    "հայկական": "AM", "ქართული": "GE", "azərbaycan": "AZ", "o'zbek": "UZ", "ўзбек": "UZ",
    "точик": "TJ", "тоҷик": "TJ", "кыргыз": "KG", "türkmen": "TM", "italia": "IT",
    "deutschland": "DE", "españa": "ES", "polska": "PL", "việt nam": "VN",
}  # fmt: skip

# Subdivision names that are ordinary words too: never read as a place.
_COMMON = frozenset({
    "centre", "center", "central", "centro", "capital", "nord", "norte", "north", "sud",
    "sur", "south", "est", "east", "este", "ouest", "oeste", "west", "western", "eastern",
    "northern", "southern", "islands", "island", "littoral", "plateau", "sahel", "lacs",
    "lagunes", "savanes", "montagnes", "distrito federal", "most", "meta", "para", "pool",
    "paul", "sal", "cesar", "sucre", "toledo", "kent", "maine", "georgia", "jersey",
    "victoria", "queensland", "canterbury", "central river", "upper", "lower", "coast",
    "bay", "lake", "lakes", "mountain", "valley", "highland", "highlands", "metro",
    "metropolitana", "national", "federal", "orange", "nelson", "southland", "western cape",
    "amazonas", "florida", "guadeloupe", "martinique", "reunion", "mayotte",
})  # fmt: skip
# A place name in a channel name must be at least this long.
NAME_MIN = 5


def _key(text: str) -> str:
    return " ".join(re.sub(r"[^\w']+", " ", fold(text)).split())


@cache
def _load() -> tuple[dict[str, str], dict[str, set[str]]]:
    """(country name -> code, subdivision name -> countries)."""
    countries: dict[str, str] = {}
    subs: dict[str, set[str]] = {}
    path = Path(__file__).with_name("places.csv")
    with path.open(encoding="utf-8") as f:
        for r in csv.DictReader(line for line in f if not line.startswith("#")):
            k = _key(r["name"])
            if not k:
                continue
            if r["kind"] == "c":
                countries[k] = r["country"]
            else:
                subs.setdefault(k, set()).add(r["country"])
    for name, code in COUNTRY_ALIASES.items():
        countries[_key(name)] = code
    for k in list(subs):
        if k in countries or k in _COMMON:
            del subs[k]
    return countries, subs


def _group_keys(group: str) -> list[str]:
    """«Asturias, Principado de» -> the whole name and the part before the comma."""
    whole = _key(group)
    head = _key(group.split(",")[0].split("(")[0])
    return list(dict.fromkeys(k for k in (whole, head) if k))


def country_of_group(group: str) -> str | None:
    """The country a playlist group is named after: «Italy» -> IT; a subdivision
    group gives its country when only one country has a part of that name."""
    countries, subs = _load()
    for k in _group_keys(group):
        if k in countries:
            return countries[k]
        if len(subs.get(k, ())) == 1:
            return next(iter(subs[k]))
    return None


def subdivision_of_group(group: str) -> str | None:
    """The subdivision name a playlist group is, or None: «California», «Gorj»."""
    _, subs = _load()
    for k in _group_keys(group):
        if k in subs:
            return k
    return None


@cache
def _name_re(country: str) -> re.Pattern | None:
    _, subs = _load()
    names = sorted((k for k, cs in subs.items() if country in cs and len(k) >= NAME_MIN),
                   key=len, reverse=True)  # fmt: skip
    if not names:
        return None
    return re.compile(r"(?<!\w)(" + "|".join(re.escape(n) for n in names) + r")(?!\w)")


def place_in_names(names: list[str], countries: list[str]) -> str | None:
    """A subdivision of one of the channel's countries named in its names, or None:
    «TV 1 Oberösterreich» (AT), «Ultra TV Puebla» (MX)."""
    text = " ".join(_key(n) for n in names if n)
    for c in countries:
        c = c.strip().upper()
        rx = _name_re("GB" if c == "UK" else c)
        hit = rx.search(text) if rx else None
        if hit:
            return hit.group(1)
    return None
