"""Public guides the guide autopilot may try, by country (see docs/sources/catalog.yaml).

None of them is used unless MIH_EPG_AUTO is on and the install has working channels of
that country without a guide (mih.epg.auto). Order within a country: guides made for
it first, then country files of the worldwide aggregators. Many of these are community
projects without a licence or a promise to stay online; the autopilot measures each one
on this install and drops the ones that give no channel a guide.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class KnownGuide:
    id: str
    name: str
    url: str
    countries: tuple[str, ...]


# Guides made for one country or language.
_REGIONAL = [
    ("ru-iptvx-one", "iptvx.one (lite)", "https://iptvx.one/epg/epg_lite.xml.gz",
     ("RU", "BY", "UA", "KZ")),
    ("ru-teleguide", "teleguide.su", "https://download.teleguide.su/download/new3/xmltv.xml.gz",
     ("RU",)),
    ("ru-programtv", "programtv.ru", "http://programtv.ru/xmltv.xml.gz", ("RU",)),
    ("vn-vnepg-site", "vnepg.site", "https://vnepg.site/epg.xml", ("VN",)),
    ("vn-epg-io", "epg.io.vn", "https://epg.io.vn/epg.xml", ("VN",)),
    ("tdtchannels-epg", "TDTChannels", "https://www.tdtchannels.com/epg/TV.xml.gz", ("ES",)),
    ("xmltvfr", "xmltvfr.fr", "https://xmltvfr.fr/xmltv/xmltv.xml.gz", ("FR", "BE", "CH")),
    ("dp247-freeview", "Freeview-EPG",
     "https://raw.githubusercontent.com/dp247/Freeview-EPG/master/epg.xml", ("GB",)),
    ("cn-yang-epg", "yang-1989", "https://iptv.yang-1989.xyz/epg.xml.gz", ("CN", "HK", "TW")),
    ("cn-fanmingming-epg", "fanmingming", "https://live.fanmingming.cn/e.xml", ("CN",)),
]  # fmt: skip


def _codes(text: str) -> list[str]:
    return text.split()


# epgshare01 country files (epg_ripper_<variant>.xml.gz); the variant is not always <cc>1.
_EPGSHARE01 = {
    "GB": "UK1", "BE": "BE2", "US": "US2", "CA": "CA2",
    **{cc: f"{cc}1" for cc in _codes(
        "AL AR AT AU BA BG BR CH CL CO CR CY CZ DE DK DO EC EG ES FI FR GR HK HR HU ID IE IL"
        " IN IT JM JP KE KR LT LV MT MX MY NG NL NO NZ PA PE PH PK PL PT RO RS SA SE SG SK SV"
        " TR UY VN ZA UA KZ"
    )},
}  # fmt: skip

_EPG_PW = _codes("AU BR CA CN DE FR GB HK ID IN JP MY NZ PH RU SG TW US VN ZA")


def _build() -> list[KnownGuide]:
    out = [KnownGuide(i, n, u, c) for i, n, u, c in _REGIONAL]
    for cc, variant in sorted(_EPGSHARE01.items()):
        out.append(KnownGuide(f"epgshare01-{variant.lower()}", f"epgshare01 {variant}",
                              f"https://epgshare01.online/epgshare01/epg_ripper_{variant}.xml.gz",
                              (cc,)))  # fmt: skip
    for cc in _EPG_PW:
        out.append(KnownGuide(f"epg-pw-{cc.lower()}", f"epg.pw {cc}",
                              f"https://epg.pw/xmltv/epg_{cc}.xml.gz", (cc,)))  # fmt: skip
    return out


KNOWN_GUIDES: list[KnownGuide] = _build()


def guides_for(country: str) -> list[KnownGuide]:
    """Candidates for one country (ISO 3166-1 alpha-2), in the order to try them."""
    cc = country.upper()
    return [g for g in KNOWN_GUIDES if cc in g.countries]
