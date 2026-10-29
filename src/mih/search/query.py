"""Rule-based query parsing (spec 20): text terms, a time window and languages.

"что завтра про гуманоидных роботов на русском" becomes terms ["гуманоидных",
"роботов"], time "tomorrow" and languages ["ru"]. Russian, English and Vietnamese time
and language words are understood ("bóng đá tối nay" is "bong da" this evening).
No AI, no network: it works offline.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from mih.epg.importer import iso
from mih.identity.normalize import fold
from mih.search.topics import PHRASES

_WORD_RE = re.compile(r"[\w+]+", re.UNICODE)
# Letters only Vietnamese uses among ru/en/vi: the query is written in Vietnamese.
_VI_LETTERS_RE = re.compile("[ăâđêôơưạảấầẩẫậắằẳẵặẹẻẽếềểễệỉịọỏốồổỗộớờởỡợụủứừửữựỳỵỷỹ]")

_STOPWORDS = {
    # ru
    "что", "про", "о", "об", "обо", "на", "в", "во", "и", "по", "с", "со", "для", "к",
    "какой", "какая", "какие", "какое", "есть", "будет", "будут", "идет", "идут", "покажи",
    "найди", "показывают", "покажут", "смотреть", "посмотреть", "можно", "мне", "там",
    "передача", "передачи", "передачу", "программа", "программу", "эфир", "эфире",
    # en
    "the", "a", "an", "about", "on", "in", "what", "is", "are", "show", "shows", "find",
    "for", "with", "of", "to", "watch", "any", "programme", "program",
}  # fmt: skip
# In a query written in Vietnamese (it has Vietnamese letters) its function words are
# dropped and English ones kept: "the", "an", "to" are Vietnamese words too (thể, ăn, tô).
_VI_STOPWORDS = {
    "ve", "gi", "co", "cua", "cac", "nhung", "xem", "tim", "chuong trinh", "nao", "khong",
}  # fmt: skip
_EN_STOPWORDS_ALSO_VI = {"the", "an", "to", "on", "in", "is", "a"}

# Each time word maps to a named window. Pairs like "завтра вечером" combine.
_DAY_WORDS = {
    "сегодня": 0, "today": 0, "завтра": 1, "tomorrow": 1, "послезавтра": 2,
    "вчера": -1, "yesterday": -1,
    "hom nay": 0, "ngay mai": 1, "ngay kia": 2, "hom qua": -1,
}  # fmt: skip
_PART_WORDS = {
    "утром": (6, 12), "morning": (6, 12), "днем": (12, 18), "afternoon": (12, 18),
    "вечером": (18, 24), "evening": (18, 24), "tonight": (18, 24), "ночью": (0, 6),
    "night": (0, 6),
    "buoi sang": (6, 12), "sang nay": (6, 12), "buoi chieu": (12, 18), "chieu nay": (12, 18),
    "buoi toi": (18, 24), "toi nay": (18, 24), "ban dem": (0, 6), "dem nay": (0, 6),
}  # fmt: skip
_NOW_WORDS = {"сейчас", "now", "live", "bay gio", "luc nay", "hien tai"}

_LANG_PREFIXES = {
    "ru": ("русск", "russian"),
    "en": ("англ", "english"),
    "vi": ("вьетнам", "vietnam", "tieng viet"),
}
_LANG_PHRASES = {"tieng nga": "ru", "tieng anh": "en"}
_LANG_GLUE = {"на", "in", "по", "bang"}

# Words that only mean something together: merged into one token before parsing.
_PHRASES = (PHRASES | {p for p in (*_DAY_WORDS, *_PART_WORDS, *_NOW_WORDS, *_LANG_PHRASES,
                                   *_VI_STOPWORDS) if " " in p} | {"tieng viet"})  # fmt: skip
_LONGEST_PHRASE = max(p.count(" ") + 1 for p in _PHRASES)

# Light suffix stripping for Russian: enough for "роботов" to find "роботы" without a
# full stemmer, which the spec defers until it is measured. Longest suffixes first.
_RU_SUFFIXES = sorted(
    {
        "ного", "ному", "ными", "ных", "ный", "ная", "ное", "ные", "ную", "ней", "ним",
        "ами", "ями", "ого", "его", "ому", "ему", "ыми", "ими", "ых", "их", "ой", "ей", "ий",
        "ый", "ая", "яя", "ое", "ее", "ые", "ие", "ую", "юю", "ом", "ем", "ам", "ям", "ах",
        "ях", "ов", "ев", "ью", "ия", "ья", "ть", "ти", "а", "я", "о", "е", "у", "ю", "ы",
        "и", "ь",
    },
    key=lambda s: (-len(s), s),
)  # fmt: skip


@dataclass
class ParsedQuery:
    text: str
    terms: list[str] = field(default_factory=list)
    languages: list[str] = field(default_factory=list)
    time: str | None = None
    window_start: datetime | None = None
    window_end: datetime | None = None
    past: bool = False

    def as_dict(self) -> dict:
        return {
            "terms": self.terms,
            "languages": self.languages,
            "time": self.time,
            "from": _iso(self.window_start),
            "to": _iso(self.window_end),
        }


def parse_query(text: str, now: datetime, tz: ZoneInfo) -> ParsedQuery:
    q = ParsedQuery(text=text)
    tokens = _join_phrases(_WORD_RE.findall(fold(text).replace("-", " ")))
    stopwords = _STOPWORDS
    if _VI_LETTERS_RE.search(text.casefold()):
        stopwords = (_STOPWORDS - _EN_STOPWORDS_ALSO_VI) | _VI_STOPWORDS
    day: int | None = None
    part: tuple[int, int] | None = None
    is_now = False
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        lang = _language(tok)
        if tok in _DAY_WORDS:
            day = _DAY_WORDS[tok]
        elif tok in _PART_WORDS:
            part = _PART_WORDS[tok]
        elif tok in _NOW_WORDS:
            is_now = True
        elif lang:
            if lang not in q.languages:
                q.languages.append(lang)
        elif tok in _LANG_GLUE and i + 1 < len(tokens) and _language(tokens[i + 1]):
            pass  # "на" in "на русском"
        elif tok not in stopwords and (len(tok) > 1 or tok.isdigit()):
            q.terms.append(tok)
        i += 1

    if is_now:
        q.time, q.window_start, q.window_end = "now", now, now + timedelta(seconds=1)
    elif day is not None or part is not None:
        local_now = now.astimezone(tz)
        d = local_now.date() + timedelta(days=day or 0)
        lo, hi = part or (0, 24)
        if part and day is None and lo < local_now.hour and hi <= local_now.hour:
            d += timedelta(days=1)  # "вечером" asked at 23:30 means tomorrow evening
        start = datetime.combine(d, time(0), tz) + timedelta(hours=lo)
        end = datetime.combine(d, time(0), tz) + timedelta(hours=hi)
        names = {0: "today", 1: "tomorrow", 2: "day_after_tomorrow", -1: "yesterday"}
        q.time = "_".join(x for x in (names.get(day or 0), _part_name(part)) if x)
        q.window_start, q.window_end = start, end
        q.past = end <= now
    return q


def stem(term: str) -> str:
    """Strip one common Russian ending, keeping at least four letters."""
    if not re.search(r"[а-я]", term):
        # English: plural s only; Vietnamese words are short and uninflected.
        return term[:-1] if len(term) > 4 and term.endswith("s") else term
    for suffix in _RU_SUFFIXES:
        if term.endswith(suffix) and len(term) - len(suffix) >= 4:
            return term[: -len(suffix)]
    return term


def _join_phrases(tokens: list[str]) -> list[str]:
    """["bong", "da", "toi", "nay"] -> ["bong da", "toi nay"]: longest known phrase first."""
    out, i = [], 0
    while i < len(tokens):
        for n in range(min(_LONGEST_PHRASE, len(tokens) - i), 1, -1):
            phrase = " ".join(tokens[i : i + n])
            if phrase in _PHRASES:
                out.append(phrase)
                i += n
                break
        else:
            out.append(tokens[i])
            i += 1
    return out


def _language(tok: str) -> str | None:
    if tok in _LANG_PHRASES:
        return _LANG_PHRASES[tok]
    for code, prefixes in _LANG_PREFIXES.items():
        if any(tok.startswith(p) for p in prefixes):
            return code
    return None


def _part_name(part: tuple[int, int] | None) -> str | None:
    return {(6, 12): "morning", (12, 18): "afternoon", (18, 24): "evening",
            (0, 6): "night"}.get(part) if part else None  # fmt: skip


def _iso(dt: datetime | None) -> str | None:
    return iso(dt) if dt else None
