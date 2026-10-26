"""Keys to find a channel by what a person says, not by how a playlist spells it.

"Россия 24", "Россия-24", "россия24", "Rossiya 24" and "Russia 24" must all find the
same channel, and so must the short names people use: "Первый", "РТР", "НТВ". A key
is the folded name without quality words and time shift, then the same with no spaces
(``compact``), in Latin letters, without generic words such as "канал", and the popular
names of the best-known channels. Match a spoken query with ``compact(query)`` against
the keys.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from mih.identity.normalize import fold, split_name, tvg_base

_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ж": "zh", "з": "z",
    "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p",
    "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "kh", "ц": "ts", "ч": "ch",
    "ш": "sh", "щ": "shch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
    "і": "i", "ї": "yi", "є": "ye", "ґ": "g",
}  # fmt: skip

# Words with an English name people use instead of a transliteration.
_ENGLISH = {"россия": "russia"}

# Words that only say "this is a channel"; dropping them gives the short name.
_GENERIC = {"канал", "телеканал", "тв", "tv", "channel", "телевидение", "television"}

# Compact key -> popular names that neither spelling nor translit would give.
POPULAR = {
    "первыйканал": ("первый", "1канал", "1tv", "channelone", "орт"),
    "россия1": ("ртр", "россия", "russia1"),
    "россия24": ("вести24", "russia24"),
    "россиякультура": ("культура", "россияк"),
    "россияк": ("культура", "россиякультура"),
    "нтв": ("ntv",),
    "стс": ("sts", "ctc"),
    "тнт": ("tnt",),
    "пятыйканал": ("пятый", "5канал", "channel5"),
    "рентв": ("рен", "ren", "rentv"),
    "матчтв": ("матч", "match", "matchtv"),
    "твцентр": ("твц", "tvc"),
    "тв3": ("tv3",),
    "че": ("перец",),
    "муз": ("музтв", "muztv"),
    "музтв": ("муз", "muz"),
    "карусель": ("karusel",),
    "мир": ("mir",),
    "отр": ("otr",),
}  # fmt: skip

_NON_WORD = re.compile(r"[^\w]+", re.UNICODE)


def compact(text: str) -> str:
    """Folded text without spaces, hyphens or punctuation: "Россия-24" -> "россия24"."""
    return _NON_WORD.sub("", fold(text)).replace("_", "")


def translit(text: str) -> str:
    return "".join(_TRANSLIT.get(ch, ch) for ch in text)


def search_keys(names: Iterable[str], tvg_ids: Iterable[str] = ()) -> list[str]:
    keys: set[str] = set()
    for name in names:
        words = split_name(name).key.split()
        if not words:
            continue
        variants = [words, [_ENGLISH.get(w, w) for w in words]]
        short = [w for w in words if w not in _GENERIC]
        if short and short != words:
            variants.append(short)
        for v in variants:
            keys.add(" ".join(v))
            joined = "".join(v)
            keys.add(joined)
            keys.add(translit(joined))
        keys.update(POPULAR.get("".join(words), ()))
    for tid in tvg_ids:
        # "PerviyKanal.ru@HD" -> "perviykanal"
        keys.add(compact(tvg_base(tid).rsplit(".", 1)[0]))
    return sorted(k for k in keys if k)
