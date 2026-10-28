"""How many working channels have a TV guide, by country.

A working channel is one with a stream at level ``ok`` (mih.lists). It has a guide when
one of its feeds is matched to a guide channel that lists programmes, in an enabled
guide. That is what a guide can give, whatever MIH_EPG_SCOPE stores of it.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime

from mih.lists import ListFilter, build_list

# A channel without a country tag is counted under the country of its language, when
# that language is mostly spoken in one country.
_LANG_COUNTRY = {
    "ru": "RU", "vi": "VN", "uk": "UA", "be": "BY", "kk": "KZ", "de": "DE",
    "fr": "FR", "it": "IT", "pl": "PL", "tr": "TR", "ja": "JP", "ko": "KR",
    "zh": "CN", "th": "TH", "id": "ID", "cs": "CZ", "ro": "RO", "hu": "HU",
    "el": "GR", "nl": "NL", "sv": "SE", "fi": "FI", "da": "DK", "no": "NO",
    "hy": "AM", "ka": "GE", "az": "AZ", "uz": "UZ", "he": "IL", "fa": "IR",
}  # fmt: skip
NO_COUNTRY = "?"
# Uncovered channel names kept per country, to show what is missing.
SAMPLE = 8


@dataclass
class CountryCoverage:
    working: int = 0
    with_guide: int = 0
    missing: list[str] = field(default_factory=list)

    @property
    def uncovered(self) -> int:
        return self.working - self.with_guide


@dataclass
class Coverage:
    working: int
    with_guide: int
    by_country: dict[str, CountryCoverage]
    # Working channel id -> guides (epg_source ids) that cover it.
    sources: dict[str, set[str]] = field(repr=False, default_factory=dict)

    def share(self) -> float | None:
        return round(self.with_guide / self.working, 3) if self.working else None

    def to_dict(self) -> dict:
        countries = sorted(self.by_country.items(), key=lambda kv: (-kv[1].uncovered, kv[0]))
        return {
            "working": self.working,
            "with_guide": self.with_guide,
            "share": self.share(),
            "by_country": [
                {
                    "country": cc,
                    "working": c.working,
                    "with_guide": c.with_guide,
                    "missing": c.missing,
                }
                for cc, c in countries
            ],
        }


def guide_sources(conn: sqlite3.Connection) -> dict[str, set[str]]:
    """Catalog channel id -> enabled guides that list programmes for it."""
    out: dict[str, set[str]] = defaultdict(set)
    for r in conn.execute(
        "SELECT DISTINCT f.channel_id, ec.epg_source_id FROM epg_mappings m"
        " JOIN feeds f ON f.id = m.feed_id JOIN epg_channels ec ON ec.id = m.epg_channel_id"
        " JOIN epg_sources s ON s.id = ec.epg_source_id"
        " WHERE m.decision IN ('auto', 'approved') AND s.enabled = 1"
        " AND (ec.programme_count IS NULL OR ec.programme_count > 0)"
    ):
        out[r["channel_id"]].add(r["epg_source_id"])
    return out


def countries_of(ch: dict) -> list[str]:
    if ch["countries"]:
        return ch["countries"]
    by_lang = sorted({_LANG_COUNTRY[x] for x in ch["languages"] if x in _LANG_COUNTRY})
    return by_lang or [NO_COUNTRY]


def working_coverage(
    conn: sqlite3.Connection, *, now: datetime | None = None, catalog: list[dict] | None = None
) -> Coverage:
    doc = build_list(conn, ListFilter(exclude_categories=()), now=now, catalog=catalog)
    covered = guide_sources(conn)
    by_country: dict[str, CountryCoverage] = defaultdict(CountryCoverage)
    sources: dict[str, set[str]] = {}
    with_guide = 0
    for x in doc["_included"]:
        ch = x["channel"]
        guides = covered.get(ch["id"], set())
        sources[ch["id"]] = guides
        with_guide += bool(guides)
        for cc in countries_of(ch):
            c = by_country[cc]
            c.working += 1
            if guides:
                c.with_guide += 1
            elif len(c.missing) < SAMPLE:
                c.missing.append(ch["name"])
    return Coverage(len(sources), with_guide, dict(by_country), sources)
