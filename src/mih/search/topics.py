"""Common TV topics in Russian, English and Vietnamese, so a query in one language also
finds programmes described in another: "футбол" finds "Football" and "Bóng đá".

Every form is folded (mih.identity.normalize.fold: lower case, no diacritics outside
Cyrillic, đ -> d, ё -> е) and is used as a word prefix, so "новост" covers "новости" and
"новостей". Multi-word forms are phrases ("bong da"). No AI, no network: a short list
of what guides actually call their programmes, grown as gaps show up.
"""

from __future__ import annotations

_TOPICS: list[tuple[str, ...]] = [
    ("новост", "news", "thoi su", "tin tuc", "ban tin"),
    ("футбол", "football", "soccer", "bong da"),
    ("спорт", "sport", "the thao"),
    ("хоккей", "hockey", "khuc con cau"),
    ("теннис", "tennis", "quan vot"),
    ("баскетбол", "basketball", "bong ro"),
    ("волейбол", "volleyball", "bong chuyen"),
    ("бокс", "boxing", "quyen anh"),
    ("фильм", "кино", "film", "movie", "cinema", "phim"),
    ("сериал", "series", "phim bo", "phim truyen"),
    ("мультф", "мультик", "мультсериал", "cartoon", "animation", "anime", "hoat hinh"),
    ("документальн", "documentar", "phim tai lieu"),
    ("погод", "weather", "thoi tiet", "du bao thoi tiet"),
    ("музык", "music", "am nhac", "ca nhac"),
    ("концерт", "concert", "hoa nhac", "dem nhac"),
    ("детск", "для детей", "kids", "children", "thieu nhi", "tre em"),
    ("кулинар", "кухн", "cooking", "cookery", "recipe", "am thuc", "nau an"),
    ("путешеств", "travel", "du lich"),
    ("истори", "history", "historical", "lich su"),
    ("наук", "научн", "science", "khoa hoc"),
    ("природ", "nature", "wildlife", "thien nhien"),
    ("животн", "animal", "dong vat"),
    ("комеди", "юмор", "comedy", "humor", "humour", "hai kich"),
    ("экономик", "бизнес", "economy", "economic", "business", "kinh te"),
    ("здоров", "медицин", "health", "medical", "suc khoe", "y te"),
    ("политик", "politic", "chinh tri"),
    ("технолог", "technology", "tech", "cong nghe"),
    ("робот", "robot", "ro bot", "ro-bot"),
    ("автомобил", "car", "cars", "motor", "o to"),
    ("мода", "моды", "моде", "fashion", "thoi trang"),
    ("реалити", "reality", "truyen hinh thuc te"),
    ("ток-шоу", "ток шоу", "talk show", "talkshow"),
    ("викторин", "quiz", "game show", "gameshow", "tro choi"),
    ("религи", "religio", "ton giao"),
]

# Short forms ("car", "tech", "y te") stand for whole words only; longer ones may grow
# by an ending ("новост" -> "новостей"), but not by a whole second word.
WHOLE_WORD_BELOW = 4
_MAX_ENDING = 6


def _norm(form: str) -> str:
    return form.replace("-", " ")


_FORMS: list[tuple[str, int]] = [(_norm(f), i) for i, group in enumerate(_TOPICS) for f in group]

# Multi-word forms, so the query parser keeps "bong da" together as one term.
PHRASES: frozenset[str] = frozenset(f for f, _ in _FORMS if " " in f)


def variants(term: str) -> list[str]:
    """The term's own spelling first, then every form of each topic it names."""
    term = _norm(term)
    out = [term]
    for form, i in _FORMS:
        if _names(term, form):
            out += [_norm(f) for f in _TOPICS[i] if _norm(f) not in out]
    return out


def _names(term: str, form: str) -> bool:
    if term == form:
        return True
    if len(form) < WHOLE_WORD_BELOW or " " in form:
        return False
    return term.startswith(form) and len(term) - len(form) <= _MAX_ENDING
