"""Genres for the search filter: «Фильмы», «Мультфильмы», «Для взрослых» and so on.

A guide marks a programme's genre in its categories, and often only in the title
("Х/ф ...", "М/ф ..."). A genre is therefore a set of folded forms (as in topics.py)
looked up in the title and categories columns of the text index, in Russian, English and
Vietnamese. Forms of four letters and more are word prefixes; shorter forms and phrases
are whole words. No AI, no network.
"""

from __future__ import annotations

from mih.search.topics import WHOLE_WORD_BELOW

# id -> forms. The ids are what the web UI sends; its labels are translated there.
GENRES: dict[str, tuple[str, ...]] = {
    "news": ("новост", "news", "thoi su", "tin tuc", "ban tin"),
    "sport": ("спорт", "футбол", "хоккей", "sport", "football", "soccer", "the thao", "bong da"),
    "movie": ("фильм", "кино", "х ф", "film", "movie", "cinema", "phim"),
    "series": ("сериал", "т с", "series", "phim bo", "phim truyen"),
    "cartoon": ("мульт", "м ф", "м с", "cartoon", "animation", "anime", "hoat hinh"),
    "kids": ("детск", "детям", "для детей", "kids", "children", "thieu nhi", "tre em"),
    "romance": ("мелодрам", "романтич", "romance", "romantic", "lang man", "tinh cam"),
    "comedy": ("комеди", "юмор", "comedy", "humor", "humour", "hai kich", "hai huoc"),
    "drama": ("драма", "драмы", "drama", "chinh kich"),
    "action": ("боевик", "приключен", "action", "adventure", "hanh dong", "phieu luu"),
    "crime": ("детектив", "триллер", "криминал", "thriller", "crime", "detective", "trinh tham"),
    "horror": ("ужас", "хоррор", "horror", "kinh di"),
    "scifi": ("фантаст", "фэнтези", "sci fi", "science fiction", "fantasy", "vien tuong"),
    "documentary": ("документальн", "познавательн", "documentar", "phim tai lieu"),
    "music": ("музык", "концерт", "music", "concert", "am nhac", "ca nhac"),
    "entertainment": ("развлекат", "шоу", "юмористич", "entertainment", "show", "giai tri"),
    "cooking": ("кулинар", "кухн", "cooking", "cookery", "am thuc", "nau an"),
    "travel": ("путешеств", "travel", "du lich"),
    "nature": ("природ", "животн", "nature", "wildlife", "animal", "thien nhien", "dong vat"),
    "science": ("наук", "научн", "science", "khoa hoc", "технолог", "technology"),
    "history": ("истори", "history", "historical", "lich su"),
    "health": ("здоров", "медицин", "health", "medical", "suc khoe"),
    "religion": ("религи", "religio", "ton giao"),
    "adult": ("для взрослых", "эрот", "adult", "erotic", "xxx", "nguoi lon"),
}


# Genre -> the catalog's channel categories (mih.export.CATEGORIES) it also lists channels
# of: «Для взрослых» finds adult channels even when they have no guide. Genres with no
# category of their own list programmes only.
GENRE_CATEGORIES: dict[str, tuple[str, ...]] = {
    "news": ("news",), "sport": ("sport",), "movie": ("movies",), "series": ("series",),
    "cartoon": ("kids",), "kids": ("kids",), "documentary": ("documentary",),
    "music": ("music",), "entertainment": ("entertainment",), "religion": ("religion",),
    "science": ("education", "documentary"), "adult": ("adult",),
}  # fmt: skip


def genre_categories(ids: list[str]) -> list[str]:
    return sorted({c for gid in ids for c in GENRE_CATEGORIES.get(gid, ())})


def genre_match(ids: list[str]) -> str:
    """An FTS5 query for programmes of any of these genres, in title or categories.
    Raises ValueError for an unknown id."""
    parts = []
    for gid in ids:
        if gid not in GENRES:
            raise ValueError(f"unknown genre {gid!r}")
        for form in GENRES[gid]:
            quoted = '"' + form.replace('"', '""') + '"'
            prefix = len(form) >= WHOLE_WORD_BELOW and " " not in form
            parts.append(quoted + ("*" if prefix else ""))
    return "{title categories} : (" + " OR ".join(parts) + ")"
