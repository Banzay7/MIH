"""Channel categories from weighted signals.

Every source of evidence votes for categories with a weight:

- ``directory``: the iptv-org directory's own categories (curated): 3.
- ``playlist``: a playlist's ``group-title``: 2, and 3 when two or more groups agree.
- ``name``: a word in the channel's names: 2, or 1 for a weak word ("24", "show").
- ``guide``: the share of airtime the guide gives a genre over the stored days:
  40 % and more: 3, 20 %: 2, 10 %: 1. Programme titles count too ("Х/ф ...", "М/ф ...").

A category is kept from a score of ``THRESHOLD`` up. The channel gets the best one as its
primary category and at most ``MAX_SECONDARY`` more that score at least
``SECONDARY_SHARE`` of the primary. «adult» is kept from any signal at all, so filtering
adult channels out stays on the safe side. No AI, no network.
"""

from __future__ import annotations

import json
import re
import sqlite3
from bisect import bisect_left
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime

from mih.identity.normalize import fold, split_name
from mih.search.genres import GENRES
from mih.search.topics import WHOLE_WORD_BELOW

# Canonical category -> folded keywords (ru, en, vi). A keyword matches a whole word
# or the start of one, so "спорт" matches "Спорт HD" and "спортивные".
CATEGORIES: dict[str, tuple[str, ...]] = {
    "news": ("news", "новост", "новин", "информац", "tin tuc", "thoi su", "24", "nachrichten",
             "noticias", "noticiero", "notizie", "nieuws", "nyheder", "nyheter", "haber",
             "tagesschau", "telegiornale", "wiadomosci", "stiri", "info", "franceinfo",
             "zpravy", "spravy", "zinios", "ειδησεισ", "חדשות"),
    "sport": ("sport", "спорт", "футбол", "football", "soccer", "the thao", "матч", "match",
              "futbol", "fussball", "deport", "esporte", "calcio", "eurosport", "хоккей",
              "hockey", "nhl", "nba", "nfl", "mlb", "fifa", "ufc", "wwe", "tennis", "golf",
              "barca", "laliga", "racing", "bong da"),
    "kids": ("kids", "children", "cartoon", "детск", "дети", "мульт", "thieu nhi", "hoat hinh",
             "infantil", "kinder", "jeunesse", "dzieci", "disney", "nick", "nickelodeon",
             "toon", "baby", "junior", "anime", "детям"),
    "movies": ("movie", "film", "cinema", "кино", "кіно", "фильм", "phim", "cine", "kino",
               "pelicul", "axn", "hbo", "cinemax", "action", "thriller", "western", "horror"),
    "series": ("series", "сериал", "серіал", "drama", "novela", "telenovel", "dizi"),
    "music": ("music", "музык", "музик", "am nhac", "mtv", "musik", "musica", "musique",
              "muziek", "muzyk", "rock", "jazz", "hits", "stingray", "vh1", "deejay", "ca nhac"),
    "documentary": ("documentary", "докум", "познават", "science", "наук", "discovery",
                    "histor", "истори", "nature", "природ", "tai lieu", "kham pha", "dokument",
                    "documental", "documentaire", "belgesel", "geographic", "nat geo",
                    "wildlife", "wissen", "storia"),
    "education": ("education", "образоват", "обучени", "giao duc", "educa", "bildung", "school",
                  "школ", "scuola", "escola", "escuela", "ecole", "schul", "universit",
                  "learning"),
    "entertainment": ("entertainment", "развлекат", "юмор", "comedy", "giai tri", "show",
                      "unterhaltung", "divertissement", "intratteniment", "entretenim",
                      "reality"),
    "lifestyle": ("lifestyle", "cooking", "food", "кулинар", "еда", "travel", "путешеств",
                  "мода", "fashion", "doi song", "du lich", "kochen", "cuisine", "reisen",
                  "viaggi", "viajes", "voyage", "pets", "garden", "fishing", "hunting",
                  "outdoor", "horse"),
    "religion": ("religio", "религи", "православ", "church", "ton giao", "gospel", "christian",
                 "jesus", "evangel", "катол", "islam", "quran", "коран", "bible", "biblia",
                 "igreja", "iglesia", "boas novas", "voz de maria", "scientolog", "ewtn",
                 "daystar", "tbn"),
    "business": ("business", "бизнес", "эконом", "финанс", "finance", "kinh te", "bloomberg",
                 "cnbc"),
    "general": ("general", "общие", "общероссийск", "эфирн", "federal", "федеральн",
                "tong hop"),
    "regional": ("regional", "регион", "local", "dia phuong"),
    "shop": ("shop", "магазин", "телемагазин", "mua sam", "teleshop"),
    "radio": ("radio", "радио", "fm", "dab"),
    "adult": ("adult", "xxx", "18+", "+18", "18 плюс", "взросл", "для взрослых", "эрот",
              "erotic", "erotik", "porn", "порн", "sex", "секс", "nsfw", "playboy",
              "hustler", "brazzers", "dorcel", "penthouse", "redlight", "red light",
              "nguoi lon", "интим", "русская ночь", "ню арт", "шалун", "babes tv",
              "barely legal", "private tv", "vivid red", "vivid tv", "xy max", "exxxotica",
              "o la la", "o-la-la", "beate uhse", "evil angel", "fap tv"),
}  # fmt: skip

# Short keywords that are a word of their own ("nick", not "nickname"; "fm", not "fmovies").
WHOLE_KEYS: frozenset[str] = frozenset({
    "24", "nick", "toon", "baby", "nhl", "nba", "nfl", "mlb", "fifa", "ufc", "wwe", "axn",
    "hbo", "cine", "kino", "dizi", "rock", "hits", "jazz", "vh1", "fm", "dab",
    "pets", "tbn", "ewtn", "golf", "radio", "радио", "drama", "info", "action", "school",
})  # fmt: skip

# Big national broadcasters: a word of their name says «general» when nothing more specific
# does («Rai 3 Trento», «NBC WPTV-TV»). Whole words only; «BBC News» still goes to news.
GENERAL_BRANDS: tuple[str, ...] = (
    "rai", "zdf", "ard", "das erste", "ndr", "swr", "wdr", "mdr", "rbb", "orf", "srf", "rtl",
    "sat 1", "prosieben", "tf1", "france 2", "france 3", "tve", "antena 3", "telecinco",
    "rtp", "globo", "sbt", "televisa", "las estrellas", "azteca", "telemundo", "univision",
    "nbc", "abc", "cbs", "fox", "pbs", "itv", "bbc", "rte", "npo", "vrt", "rtbf", "tvp",
    "polsat", "tvn", "markiza", "hrt", "rts", "tvr", "pro tv", "antena 1", "ert", "trt",
    "atv", "kanal d", "star tv", "tvri", "sctv", "rcti", "indosiar", "nhk", "kbs", "mbc",
    "sbs", "cctv", "doordarshan", "ptv", "vtv", "htv", "vtc", "первый канал", "россия 1",
    "нтв", "рен тв", "пятый канал", "твц", "1+1", "інтер", "ictv", "стб", "radio canada",
    "cbc", "ct1", "ct2", "nova",
)  # fmt: skip

# Keywords that also turn up in names of channels of other kinds ("Кино 24", "Talk Show",
# "Match Point"): alone they are not enough for a category.
WEAK_KEYS: frozenset[str] = frozenset({"24", "show", "match", "матч", "local"})

# iptv-org categories -> the catalog's (CATEGORIES above).
CATEGORY_MAP = {
    "animation": "kids", "auto": "lifestyle", "business": "business", "classic": "movies",
    "comedy": "entertainment", "cooking": "lifestyle", "culture": "education",
    "documentary": "documentary", "education": "education", "entertainment": "entertainment",
    "family": "entertainment", "general": "general", "interactive": "entertainment",
    "kids": "kids", "legislative": "news", "lifestyle": "lifestyle", "movies": "movies",
    "music": "music", "news": "news", "outdoor": "lifestyle", "public": "general",
    "relax": "lifestyle", "religious": "religion", "science": "documentary",
    "series": "series", "shop": "shop", "sports": "sport", "travel": "lifestyle",
    "weather": "news", "xxx": "adult",
}  # fmt: skip

# Programme genre (mih.search.genres.GENRES) -> channel category. Fiction genres with
# no «фильм» or «сериал» mark count as movies.
GENRE_TO_CATEGORY: dict[str, str] = {
    "news": "news", "sport": "sport", "movie": "movies", "series": "series",
    "cartoon": "kids", "kids": "kids", "romance": "movies", "comedy": "movies",
    "drama": "movies", "action": "movies", "crime": "movies", "horror": "movies",
    "scifi": "movies", "documentary": "documentary", "nature": "documentary",
    "science": "documentary", "history": "documentary", "music": "music",
    "entertainment": "entertainment", "cooking": "lifestyle", "travel": "lifestyle",
    "health": "lifestyle", "religion": "religion", "adult": "adult",
}  # fmt: skip
_FICTION = {"romance", "comedy", "drama", "action", "crime", "horror", "scifi"}

ORDER = [*CATEGORIES, "other"]
THRESHOLD = 2.0
MAX_SECONDARY = 2
SECONDARY_SHARE = 0.3
# A guide shorter than this says too little about a channel.
GUIDE_MIN_MINUTES = 6 * 60
GUIDE_MIN_AIRINGS = 5
GUIDE_STEPS = ((0.4, 3.0), (0.2, 2.0), (0.1, 1.0))

_NON_WORD = re.compile(r"[^\w+]+")


def _pattern(key: str) -> str:
    return r"(?<![\w])" + re.escape(fold(key)) + (r"(?![\w])" if key in WHOLE_KEYS else "")


_BRANDS_RE = re.compile("|".join(r"(?<![\w+])" + re.escape(fold(b)) + r"(?![\w+])"
                                 for b in GENERAL_BRANDS))  # fmt: skip


_KEY_RE = {cat: (re.compile("|".join(_pattern(k) for k in keys if k not in WEAK_KEYS)),
                 re.compile("|".join(_pattern(k) for k in keys if k in WEAK_KEYS) or "(?!)"))
           for cat, keys in CATEGORIES.items()}  # fmt: skip


def _genre_re(forms: tuple[str, ...]) -> re.Pattern:
    parts = []
    for f in forms:
        f = fold(f).replace("-", " ")
        whole = len(f) < WHOLE_WORD_BELOW or " " in f
        parts.append(r"(?<!\w)" + re.escape(f) + (r"(?!\w)" if whole else ""))
    return re.compile("|".join(parts))


_GENRE_RE = {gid: _genre_re(forms) for gid, forms in GENRES.items()}


# Broadcaster names with «radio» that are television: «Radio-Canada», «Radio Televizija
# Srbija», «Radiotelevisione svizzera». Taken out before keywords are looked up.
_NOT_RADIO_RE = re.compile(r"(?<!\w)(radio[\s\-]+(canada|tele\w*|y television|e televis\w*)"
                           r"|radiotele\w*)")  # fmt: skip


def keyword_hits(texts: list[str]) -> dict[str, str]:
    """Category -> "strong" or "weak": the keywords found in the texts."""
    words = _NOT_RADIO_RE.sub(" ", " " + " ".join(fold(t) for t in texts if t) + " ")
    out = {}
    for cat, (strong, weak) in _KEY_RE.items():
        if strong.search(words):
            out[cat] = "strong"
        elif weak.search(words):
            out[cat] = "weak"
    return out


def categorize(texts: list[str]) -> list[str]:
    """Canonical categories any keyword in the texts points to, in CATEGORIES order."""
    return list(keyword_hits(texts))


def directory_categories(raw: list[str], nsfw: bool) -> list[str]:
    cats = []
    for c in raw:
        mapped = CATEGORY_MAP.get(c.casefold())
        if mapped and mapped not in cats:
            cats.append(mapped)
    if nsfw and "adult" not in cats:
        cats.append("adult")
    return cats


def programme_categories(title: str, categories: list[str]) -> set[str]:
    """Channel categories one programme stands for, from its title and guide genres."""
    text = " " + _NON_WORD.sub(" ", fold(" ".join([title or "", *categories]))) + " "
    genres = {gid for gid, rx in _GENRE_RE.items() if rx.search(text)}
    cats = {GENRE_TO_CATEGORY[g] for g in genres if g in GENRE_TO_CATEGORY
            and not (g in _FICTION and genres & {"movie", "series", "cartoon"})}  # fmt: skip
    cats |= set(categorize(categories))  # the guide's own words: «Информационные» and so on
    return cats


@dataclass
class GuideMix:
    minutes: float = 0.0
    airings: int = 0
    by_category: Counter = field(default_factory=Counter)  # minutes
    titles: Counter = field(default_factory=Counter)  # minutes by programme title
    raw: Counter = field(default_factory=Counter)  # the guide's own genre words, by airing

    def shares(self) -> dict[str, float]:
        if self.minutes < GUIDE_MIN_MINUTES or self.airings < GUIDE_MIN_AIRINGS:
            return {}
        return {c: m / self.minutes for c, m in self.by_category.items()}

    def one_show(self) -> str | None:
        """The title that fills most of the airtime: a channel of one series."""
        if self.minutes < GUIDE_MIN_MINUTES or self.airings < GUIDE_MIN_AIRINGS or not self.titles:
            return None
        title, m = self.titles.most_common(1)[0]
        return title if m / self.minutes >= ONE_SHOW_SHARE else None


def guide_mix(conn: sqlite3.Connection) -> dict[str, GuideMix]:
    """Channel id -> airtime per category over the scheduled airings the guide keeps."""
    cache: dict[str, tuple[set[str], list[str]]] = {}
    out: dict[str, GuideMix] = defaultdict(GuideMix)
    for r in conn.execute(
        "SELECT DISTINCT f.channel_id, a.id, a.programme_id, a.start_utc, a.stop_utc,"
        " p.title, p.categories FROM airings a"
        " JOIN programmes p ON p.id = a.programme_id"
        " JOIN epg_mappings m ON m.epg_channel_id = a.epg_channel_id"
        " JOIN feeds f ON f.id = m.feed_id"
        " WHERE m.decision IN ('auto', 'approved') AND a.status = 'scheduled'"
    ):
        hit = cache.get(r["programme_id"])
        if hit is None:
            raw = json.loads(r["categories"] or "[]")
            texts = [t for c in raw if (t := c[1] if isinstance(c, list) else c)]
            hit = cache[r["programme_id"]] = (programme_categories(r["title"], texts), texts)
        cats, texts = hit
        try:
            minutes = (datetime.fromisoformat(r["stop_utc"].replace("Z", "+00:00"))
                       - datetime.fromisoformat(r["start_utc"].replace("Z", "+00:00"))
                       ).total_seconds() / 60  # fmt: skip
        except (AttributeError, ValueError):
            continue
        if not 0 < minutes <= 24 * 60:
            continue
        mix = out[r["channel_id"]]
        mix.minutes += minutes
        mix.airings += 1
        mix.raw.update(texts)
        if r["title"]:
            mix.titles[r["title"].strip()] += minutes
        for c in cats:
            mix.by_category[c] += minutes
    return out


@dataclass
class Decision:
    categories: list[str]  # primary first; ["other"] when nothing reached the threshold
    source: str  # where the primary came from (a _SOURCE_RANK key) or none
    scores: dict[str, float]
    reasons: dict[str, list[str]]
    fallback: bool = False  # only a weak rule spoke: one show, mixed guide, a broadcaster


_SOURCE_RANK = {
    "directory": 0,
    "directory-name": 1,
    "playlist": 2,
    "url": 3,
    "epg": 4,
    "neighbours": 5,
    "name": 6,
}


def decide(*, directory: list[str] = (), groups: list[str] = (), names: list[str] = (),
           guide: dict[str, float] | None = None,
           extra: list[tuple[str, str, float, str]] = (),
           one_show: str | None = None) -> Decision:  # fmt: skip
    """``extra``: more votes as (category, source, weight, reason), e.g. adult evidence.
    ``one_show``: the title that fills most of the channel's guide, if one does."""
    votes: dict[str, dict[str, float]] = defaultdict(dict)  # category -> source -> weight
    reasons: dict[str, list[str]] = defaultdict(list)

    for c in directory:
        votes[c]["directory"] = 3.0
        reasons[c].append("directory")

    agree: dict[str, list[str]] = defaultdict(list)
    for g in groups:
        for c in categorize([g]):
            agree[c].append(g)
    for c, gs in agree.items():
        votes[c]["playlist"] = 3.0 if len(gs) > 1 else 2.0
        reasons[c].append("playlist: " + "; ".join(gs[:3]))

    for c, kind in keyword_hits(list(names)).items():
        votes[c]["name"] = 2.0 if kind == "strong" else 1.0
        reasons[c].append("name" if kind == "strong" else "name (weak)")

    for c, share in (guide or {}).items():
        w = next((w for floor, w in GUIDE_STEPS if share >= floor), 0.0)
        if w:
            votes[c]["epg"] = w
            reasons[c].append(f"guide {round(share * 100)}%")

    for c, src, w, why in extra:
        votes[c][src] = max(w, votes[c].get(src, 0.0))
        reasons[c].append(why)

    scores = {c: sum(v.values()) for c, v in votes.items()}
    ranked = sorted((c for c in scores if c != "adult" or scores[c] >= THRESHOLD),
                    key=lambda c: (-scores[c], ORDER.index(c)))  # fmt: skip
    kept = [c for c in ranked if scores[c] >= THRESHOLD]
    if kept:
        top = scores[kept[0]]
        kept = kept[:1] + [c for c in kept[1:] if scores[c] >= top * SECONDARY_SHARE
                           ][:MAX_SECONDARY]  # fmt: skip
    if "adult" in scores and "adult" not in kept:
        kept.append("adult")
    if not kept:
        fallback = _fallback(names, guide or {}, one_show)
        if fallback:
            cat, src, why = fallback
            return Decision([cat], src, {cat: THRESHOLD}, {cat: [why]}, fallback=True)
        return Decision(["other"], "none", scores, dict(reasons))
    primary = votes[kept[0]]
    source = min(primary, key=lambda s: (-primary[s], _SOURCE_RANK[s]))
    return Decision(kept, source, {c: scores[c] for c in kept}, {c: reasons[c] for c in kept})


# A guide that spreads its airtime over many kinds of programmes, none of them big enough
# for a category of its own, is a general channel: news, series, shows and films.
MIXED_MIN_KINDS = 3
MIXED_KIND_SHARE = 0.1


# Free streaming services run many channels that each show one programme all day long
# («Pluto TV Baywatch», «Pluto TV Star Trek»): such a channel is a series channel.
FAST_BRANDS = ("pluto tv", "samsung tv plus", "rakuten tv", "plex", "xumo", "stirr",
               "roku channel", "tubi", "lg channels", "tcl channel")  # fmt: skip
_FAST_RE = re.compile("|".join(r"(?<!\w)" + re.escape(b) + r"(?!\w)" for b in FAST_BRANDS))
# A guide where one title fills this share of the airtime is a one-show channel.
ONE_SHOW_SHARE = 0.6


def _fallback(names, guide: dict[str, float], one_show: str | None
              ) -> tuple[str, str, str] | None:  # fmt: skip
    """(category, source, reason) when nothing reached the threshold."""
    if one_show:
        return "series", "epg", f"guide: one show «{one_show}»"
    if (
        sum(share >= MIXED_KIND_SHARE for c, share in guide.items() if c != "adult")
        >= MIXED_MIN_KINDS
    ):
        return "general", "epg", "guide: mixed"
    text = " " + _NON_WORD.sub(" ", " ".join(fold(n) for n in names if n)) + " "
    hit = _FAST_RE.search(text)
    if hit and text.replace(hit.group(0), " ").strip():  # «Pluto TV» alone is the service
        return "series", "name", "streaming service: one show"
    hit = _BRANDS_RE.search(text)
    if hit:
        return "general", "name", "broadcaster: " + hit.group(0).strip()
    return None


# ---------- Sister channels of one brand ----------

# Words at the end of a name that tell sister channels apart: «Eurosport 2», «CNN
# International», «Rai 3», «Fox Sports Asia». Taken off to find the brand.
BRAND_TAIL = frozenset({
    "tv", "channel", "канал", "телеканал", "international", "int", "intl", "world",
    "global", "europe", "asia", "africa", "america", "latino", "latam", "arabia",
    "usa", "us", "uk", "east", "west", "north", "south", "plus", "extra", "max", "live",
    "en", "de", "fr", "es", "it", "pt", "ru", "ar", "tr", "pl",
})  # fmt: skip
# Stems too common to be a brand.
GENERIC_STEMS = frozenset({"tv", "channel", "канал", "телеканал", "radio", "радио", "the",
                           "live", "canal", "kanal", "channel one", "tele"})  # fmt: skip
# Share of a brand's categorised channels that must agree on one primary category.
BRAND_AGREE = 2 / 3


def brand_stem(name: str) -> str:
    """The brand part of a channel name: «Eurosport 2 HD» -> «eurosport»."""
    tokens = split_name(name).key.split()
    while len(tokens) > 1 and (tokens[-1].isdigit() or len(tokens[-1]) == 1
                               or tokens[-1] in BRAND_TAIL):  # fmt: skip
        tokens.pop()
    return " ".join(tokens)


def brand_votes(stems: dict[str, str], primary: dict[str, str], todo: set[str]
                ) -> dict[str, tuple[str, str]]:  # fmt: skip
    """Channel -> (category, brand) for the channels in ``todo``, from their sister channels.

    ``stems``: channel -> brand_stem of its name; ``primary``: channel -> primary category
    for the channels whose own evidence decided it. A channel looks at the channels whose
    stem is its own stem or starts with it, then drops the stem's last word and looks
    again, until it finds categorised sisters. They must agree (``BRAND_AGREE``), or it
    keeps what it had: «MBC» runs news, film and drama channels alike.
    """
    by_stem: dict[str, list[str]] = defaultdict(list)
    for cid, st in stems.items():
        if st:
            by_stem[st].append(cid)
    ordered = sorted(by_stem)
    out = {}
    for cid in todo:
        tokens = stems.get(cid, "").split()
        for n in range(len(tokens), 0, -1):
            stem = " ".join(tokens[:n])
            if stem in GENERIC_STEMS or len(stem) < 3:
                break
            cats: Counter = Counter()
            i = bisect_left(ordered, stem)
            while i < len(ordered) and (ordered[i] == stem or ordered[i].startswith(stem + " ")):
                cats.update(primary[s] for s in by_stem[ordered[i]] if s != cid and s in primary)
                i += 1
            if not cats:
                continue
            cat, k = cats.most_common(1)[0]
            if k / cats.total() >= BRAND_AGREE:
                out[cid] = (cat, stem)
            break
    return out


# ---------- Adult channels behind harmless names ----------

# Words in a stream or logo address that mean an adult channel. Addresses are split into
# words, so "sussex" does not match "sex".
ADULT_URL_WORDS = (
    "xxx",
    "porn",
    "adult",
    "erotic",
    "erotik",
    "eroti",
    "sex",
    "sexy",
    "nsfw",
    "18plus",
    "playboy",
    "hustler",
    "brazzers",
    "dorcel",
    "penthouse",
    "redlight",
    "vivid",
    "xxl18",
    "fap",
    "nude",
    "naked",
    "milf",
    "hentai",
)
_URL_RE = re.compile(r"(?<![a-z0-9])(" + "|".join(ADULT_URL_WORDS) + r")(?![a-z])")
# A playlist group, or a whole playlist, where most known channels are adult makes its
# other channels adult too: playlists put such channels together under a harmless title.
NEIGHBOUR_SHARE = 0.5
NEIGHBOUR_MIN = 3
SOURCE_SHARE = 0.7
SOURCE_MIN = 5


def url_is_adult(url: str | None) -> bool:
    return bool(url) and bool(_URL_RE.search(url.casefold()))


def adult_name_keys(conn: sqlite3.Connection) -> set[str]:
    """Name keys of the directory's adult channels, blocked ones included: a playlist
    channel with such a name is adult even when it is not linked to the directory."""
    from mih.identity.normalize import split_name

    keys = set()
    for r in conn.execute(
        "SELECT name, alt_names FROM directory_channels WHERE is_nsfw = 1 OR blocked = 'nsfw'"
    ):
        for n in (r["name"], *json.loads(r["alt_names"] or "[]")):
            k = split_name(n).key if n else ""
            if k and len(k) >= 4:  # too short a key would catch ordinary channels
                keys.add(k)
    return keys


def neighbour_votes(members: dict[str, set[str]], adult: set[str], *, share: float,
                    least: int) -> dict[str, str]:  # fmt: skip
    """Channel -> the group that makes it adult. ``members``: group -> channels."""
    out = {}
    for group, chans in members.items():
        n = len(chans & adult)
        if n >= least and n / len(chans) >= share:
            for c in chans - adult:
                out.setdefault(c, group)
    return out


# ---------- Categories set by hand (web UI) ----------

MAX_MANUAL = 4


def load_overrides(conn: sqlite3.Connection) -> dict[str, list[str]]:
    rows = conn.execute("SELECT channel_id, categories FROM channel_category_overrides")
    return {r["channel_id"]: json.loads(r["categories"]) for r in rows}


def check_manual(categories: list[str]) -> list[str]:
    """The list without repeats; ValueError for an unknown category or a wrong count."""
    out = list(dict.fromkeys(categories))
    unknown = [c for c in out if c not in CATEGORIES]
    if unknown:
        raise ValueError(f"unknown category {unknown[0]!r}")
    if not 1 <= len(out) <= MAX_MANUAL:
        raise ValueError(f"from 1 to {MAX_MANUAL} categories")
    return out


def set_override(conn: sqlite3.Connection, channel_id: str, categories: list[str],
                 now: str) -> list[str]:  # fmt: skip
    """Store the owner's categories and put them into the search tags at once."""
    cats = check_manual(categories)
    conn.execute("BEGIN")
    try:
        conn.execute(
            "INSERT INTO channel_category_overrides (channel_id, categories, updated_at)"
            " VALUES (?, ?, ?) ON CONFLICT (channel_id) DO UPDATE SET"
            " categories = excluded.categories, updated_at = excluded.updated_at",
            (channel_id, json.dumps(cats), now),
        )
        conn.execute("DELETE FROM channel_tags WHERE channel_id = ?"
                     " AND kind IN ('category', 'primary')", (channel_id,))  # fmt: skip
        conn.executemany("INSERT INTO channel_tags (channel_id, kind, value) VALUES (?, ?, ?)",
                         [(channel_id, "primary", cats[0]),
                          *((channel_id, "category", c) for c in cats)])  # fmt: skip
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return cats


def clear_override(conn: sqlite3.Connection, channel_id: str) -> bool:
    """Back to the automatic categories; the tags follow at the next catalog build."""
    n = conn.execute("DELETE FROM channel_category_overrides WHERE channel_id = ?",
                     (channel_id,)).rowcount  # fmt: skip
    return bool(n)
