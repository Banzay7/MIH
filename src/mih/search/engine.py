"""Universal search over airings and channels (spec 20, POST /search).

Filters come first (time window, language, channel), then BM25 over folded text with
per-field weights. Every result says why it matched; availability is reported as the
stream status the catalog knows, never assumed.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from mih.epg import guide
from mih.epg.importer import iso
from mih.search.genres import genre_match
from mih.search.query import ParsedQuery, parse_query, stem
from mih.search.topics import WHOLE_WORD_BELOW, variants

# bm25() column weights: title, sub_title, description, categories.
_WEIGHTS = (10.0, 4.0, 1.0, 3.0)
_FIELDS = ("title", "sub_title", "description", "categories")
_CANDIDATES = 1000
_DEFAULT_HORIZON = timedelta(days=7)
_BROWSE_HORIZON = timedelta(hours=6)
_PLAYABLE = "s.availability_reason IS NULL AND s.status NOT IN ('quarantine', 'archived')"


@dataclass
class SearchRequest:
    query: str
    target_types: list[str] = field(default_factory=lambda: ["airing", "channel"])
    languages: list[str] = field(default_factory=list)
    time: str | None = None
    start: datetime | None = None
    end: datetime | None = None
    channel_ids: list[str] = field(default_factory=list)
    tz: str = "UTC"
    limit: int = 20
    offset: int = 0
    # Whole words only: no stemming, no prefix and no substring fallback.
    strict: bool = False
    # 0..1: drop programmes scoring below this share of the best match's score.
    min_relevance: float = 0.0
    # Leave out programmes and channels with no stream that can be played.
    playable_only: bool = False
    # Programmes of any of these genres (genres.py); channels are then left out.
    genres: list[str] = field(default_factory=list)


def search(conn: sqlite3.Connection, req: SearchRequest, now: datetime) -> dict:
    tz = ZoneInfo(req.tz)
    parsed = parse_query(req.query, now, tz)
    if req.time:  # an explicit filter beats words in the text
        parsed = _override_time(parsed, req.time, now, tz)
    if req.start or req.end:
        parsed.time = "custom"
        parsed.window_start = req.start or now
        parsed.window_end = req.end or (parsed.window_start + _DEFAULT_HORIZON)
        parsed.past = parsed.window_end <= now
    if req.languages and parsed.language_from_script:  # a chosen language wins
        parsed.languages, parsed.language_from_script = [], False
    for lang in req.languages:
        if lang not in parsed.languages:
            parsed.languages.append(lang)

    # Each type is paged on its own: the UI shows channels and programmes as two lists,
    # each with its own "show more"; total says how many there are in all.
    out: dict = {"query": parsed.as_dict(), "items": [], "total": {},
                 "limit": req.limit, "offset": req.offset}  # fmt: skip
    # Vietnamese letters look among Vietnamese programmes and channels first; when that
    # finds nothing of a kind, that kind is searched in every language ("widened").
    widened = parsed.language_from_script and _everywhere(parsed)
    if "airing" in req.target_types and not parsed.channels_only:
        airings = _airings(conn, parsed, req, now)
        if not airings and widened:
            airings = _airings(conn, widened, req, now)
            out["query"].setdefault("widened", []).append("airing")
        out["total"]["airing"] = len(airings)
        out["items"] += airings[req.offset : req.offset + req.limit]
    # No words: channels only when nothing narrows to programmes (all of them, by name).
    browse = not parsed.terms and not req.genres and parsed.time is None
    if "channel" in req.target_types and (parsed.terms or parsed.channels_only or browse):
        channels, total = _channels(conn, parsed, req)
        if not total and widened:
            channels, total = _channels(conn, widened, req)
            out["query"].setdefault("widened", []).append("channel")
        out["total"]["channel"] = total
        out["items"] += channels
    return out


def _everywhere(parsed: ParsedQuery) -> ParsedQuery:
    return replace(parsed, languages=[], language_from_script=False)


def _override_time(parsed: ParsedQuery, name: str, now: datetime, tz: ZoneInfo) -> ParsedQuery:
    words = {"now": "сейчас", "today": "сегодня", "tomorrow": "завтра", "tonight": "вечером",
             "yesterday": "вчера"}  # fmt: skip
    if name not in words:
        raise ValueError(f"unknown time filter {name!r}")
    t = parse_query(words[name], now, tz)
    parsed.time, parsed.window_start, parsed.window_end, parsed.past = (
        t.time, t.window_start, t.window_end, t.past
    )  # fmt: skip
    return parsed


def _airings(
    conn: sqlite3.Connection, parsed: ParsedQuery, req: SearchRequest, now: datetime
) -> list[dict]:
    start = parsed.window_start or now
    # No words, no genre and no time: "everything" is what is on now and in the next hours.
    browse = not parsed.terms and not req.genres and parsed.time is None
    end = parsed.window_end or now + (_BROWSE_HORIZON if browse else _DEFAULT_HORIZON)
    # Only feeds with a guide of their own: a +2 copy of the same show is noise here.
    guides = {g.epg_channel_id: g for g in guide.feed_guides(conn, req.channel_ids or None)
              .values() if g.shift_minutes == 0}  # fmt: skip
    if not guides:
        return []

    scores: dict[int, float] | None = None
    if parsed.terms:
        scores = _text_matches(conn, parsed.terms, req.strict)
        if not scores:
            return []
        if req.min_relevance > 0:
            # bm25 ranks are negative, best first; keep those close enough to the best.
            floor = min(scores.values()) * req.min_relevance
            scores = {k: v for k, v in scores.items() if v <= floor}

    marks = ",".join("?" * len(guides))
    sql = (
        f"SELECT {guide.AIRING_COLS}, p.rowid AS prow, p.description"
        " FROM airings a JOIN programmes p ON p.id = a.programme_id"
        f" WHERE a.epg_channel_id IN ({marks}) AND a.status = 'scheduled'"
        " AND a.start_utc < ? AND a.stop_utc > ?"
    )
    params: list = [*guides, iso(end), iso(start)]
    if scores is not None:
        sql += f" AND p.rowid IN ({','.join('?' * len(scores))})"
        params += list(scores)
    if req.genres:
        sql += " AND p.rowid IN (SELECT rowid FROM programme_fts WHERE programme_fts MATCH ?)"
        params.append(genre_match(req.genres))
    sql += " ORDER BY a.start_utc"
    rows = conn.execute(sql, params).fetchall()

    stream_counts = _stream_counts(conn, {g.channel_id for g in guides.values()})
    names = _names(conn, {g.channel_id for g in guides.values()})
    seen: dict[tuple[str, str], dict] = {}
    for r in rows:
        lang = _lang_code(r["lang"]) or _guess_lang(r["title"])
        if parsed.languages and lang not in parsed.languages:
            continue
        g = guides[r["epg_channel_id"]]
        if req.playable_only and not stream_counts.get(g.channel_id):
            continue
        key = (r["programme_id"], g.channel_id)
        if key in seen:  # repeats of the same show on the same channel collapse
            seen[key]["more_airings"] += 1
            continue
        item = guide.airing_out(r, g)
        item.update(
            type="airing",
            channel_name=names.get(g.channel_id),
            lang=lang,
            score=round(-(scores or {}).get(r["prow"], 0.0), 4),
            why_matched=_why(conn, r["prow"], parsed, lang, req.strict),
            streams_available=stream_counts.get(g.channel_id, 0),
            more_airings=0,
        )
        seen[key] = item
    items = list(seen.values())
    if parsed.time is None and scores is not None:
        # No time asked: best match first, sooner first among equals ("discovery").
        items.sort(key=lambda i: (-i["score"], i["start"]))
    elif parsed.past:
        items.sort(key=lambda i: i["start"], reverse=True)  # "history": latest first
    # Otherwise ("future", "now") they stay in start order.
    return items


def _text_matches(
    conn: sqlite3.Connection, terms: list[str], strict: bool = False
) -> dict[int, float]:
    """rowid -> bm25 (lower is better). All terms must match; trigram as a fallback,
    except in strict mode, where every term is a whole word as typed. Otherwise a term
    that names a common topic also matches it in the other languages (topics.py)."""
    expr = _quote if strict else _term_expr
    match = " AND ".join(expr(t) for t in terms)
    rows = conn.execute(
        "SELECT rowid, bm25(programme_fts, ?, ?, ?, ?) AS rank FROM programme_fts"
        " WHERE programme_fts MATCH ? ORDER BY rank LIMIT ?",
        (*_WEIGHTS, match, _CANDIDATES),
    ).fetchall()
    if rows or strict:
        return {r["rowid"]: r["rank"] for r in rows}
    long_terms = [t for t in terms if len(t) >= 3]
    if not long_terms:
        return {}
    match = " AND ".join(_quote(stem(t)) for t in long_terms)
    rows = conn.execute(
        "SELECT rowid, bm25(programme_trigram) AS rank FROM programme_trigram"
        " WHERE programme_trigram MATCH ? ORDER BY rank LIMIT ?",
        (match, _CANDIDATES),
    ).fetchall()
    return {r["rowid"]: r["rank"] for r in rows}


def _term_expr(term: str) -> str:
    parts = [_quote(f) + ("*" if prefix else "") for f, prefix in _forms(term)]
    return parts[0] if len(parts) == 1 else "(" + " OR ".join(parts) + ")"


def _forms(term: str) -> list[tuple[str, bool]]:
    """(form, is word prefix): the term, its stem and its translations. A short
    translation such as "car" is a whole word, or it would also match "card"."""
    out = [(term, True)]
    s = stem(term)
    if s != term:
        out.append((s, True))
    out += [(v, len(v) >= WHOLE_WORD_BELOW or " " in v) for v in variants(term)[1:]]
    return out


def _quote(text: str) -> str:
    return '"' + text.replace('"', '""') + '"'


def _why(
    conn: sqlite3.Connection, prow: int, parsed: ParsedQuery, lang: str | None, strict: bool = False
) -> list:
    row = conn.execute(
        "SELECT title, sub_title, description, categories FROM programme_fts WHERE rowid = ?",
        (prow,),
    ).fetchone()
    why = []
    for term in parsed.terms:
        forms = [(term, False)] if strict else _forms(term)
        for f in _FIELDS:
            hit = _hit(row[f] or "", forms)
            if hit:
                why.append({"field": f, "term": term, "matched": hit})
                break
        else:
            # Only the trigram index matched: a substring inside a title word.
            why.append({"field": "title", "term": term, "matched": "substring"})
    if parsed.time:
        why.append({"field": "time", "term": parsed.time})
    if parsed.languages:
        why.append({"field": "language", "term": lang})
    return why


def _hit(text: str, forms: list[tuple[str, bool]]) -> str | None:
    """The word (or phrase) of text that one of the forms matched."""
    words = re.findall(r"[\w+]+", text)
    for form, prefix in forms:
        n = form.count(" ") + 1
        for i in range(len(words) - n + 1):
            *head, last = words[i : i + n]
            *fhead, flast = form.split(" ")
            if head == fhead and (last.startswith(flast) if prefix else last == flast):
                return " ".join(words[i : i + n])
    return None


def _channels(
    conn: sqlite3.Connection, parsed: ParsedQuery, req: SearchRequest
) -> tuple[list[dict], int]:
    clauses, params = [], []
    for term in parsed.terms:
        t = _like_escape(term)
        if req.strict:  # the term is a whole word of the name
            clauses.append("EXISTS (SELECT 1 FROM channel_aliases a WHERE a.channel_id = c.id"
                           " AND a.kind = 'name' AND (a.value = ?"
                           " OR a.value LIKE ? ESCAPE '\\' OR a.value LIKE ? ESCAPE '\\'"
                           " OR a.value LIKE ? ESCAPE '\\'))")  # fmt: skip
            params += [term, t + " %", "% " + t + " %", "% " + t]
        else:
            # "спорт" also finds "Eurosport": longer words match inside a name. A short
            # one ("bay", "gio") only starts a word, or "gio" would find "Regional".
            forms = [t] + [_like_escape(v) for v in variants(term)[1:]
                           if len(v) >= WHOLE_WORD_BELOW]  # fmt: skip
            likes = []
            for f in forms:
                if len(f) >= WHOLE_WORD_BELOW:
                    likes.append("%" + f + "%")
                else:
                    likes += [f + "%", "% " + f + "%"]
            ors = " OR ".join(["a.value LIKE ? ESCAPE '\\'"] * len(likes))
            clauses.append("EXISTS (SELECT 1 FROM channel_aliases a WHERE a.channel_id = c.id"
                           f" AND a.kind = 'name' AND ({ors}))")  # fmt: skip
            params += likes
    countries = sorted({c for lang in parsed.languages for c in _LANG_COUNTRIES.get(lang, ())})
    if (parsed.channels_only or parsed.language_from_script) and countries:
        clauses.append(f"UPPER(c.country) IN ({','.join('?' * len(countries))})")
        params += countries
    if req.playable_only:
        clauses.append(f"EXISTS (SELECT 1 FROM streams s JOIN feeds f ON f.id = s.feed_id"
                       f" WHERE f.channel_id = c.id AND {_PLAYABLE})")  # fmt: skip
    where = " AND ".join(clauses) or "1"
    total = conn.execute(f"SELECT COUNT(*) FROM channels c WHERE {where}", params).fetchone()[0]
    rows = conn.execute(
        f"SELECT c.id, c.canonical_name, c.country FROM channels c WHERE {where}"
        " ORDER BY c.canonical_name COLLATE NOCASE LIMIT ? OFFSET ?",
        (*params, req.limit, req.offset),
    ).fetchall()
    counts = _stream_counts(conn, {r["id"] for r in rows})
    items = [
        {"type": "channel", "channel_id": r["id"], "channel_name": r["canonical_name"],
         "country": r["country"], "streams_available": counts.get(r["id"], 0),
         "why_matched": [{"field": "channel_name", "term": t} for t in parsed.terms]
         + ([{"field": "country", "term": r["country"]}] if parsed.channels_only else [])}
        for r in rows
    ]  # fmt: skip
    return items, total


def _stream_counts(conn: sqlite3.Connection, channel_ids: set[str]) -> dict[str, int]:
    if not channel_ids:
        return {}
    marks = ",".join("?" * len(channel_ids))
    rows = conn.execute(
        "SELECT f.channel_id, COUNT(*) AS n FROM streams s JOIN feeds f ON f.id = s.feed_id"
        f" WHERE f.channel_id IN ({marks}) AND {_PLAYABLE} GROUP BY f.channel_id",
        tuple(channel_ids),
    ).fetchall()
    return {r["channel_id"]: r["n"] for r in rows}


def _names(conn: sqlite3.Connection, ids: set[str]) -> dict[str, str]:
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT id, canonical_name FROM channels WHERE id IN ({marks})", tuple(ids)
    ).fetchall()
    return {r["id"]: r["canonical_name"] for r in rows}


_VI_LETTERS = set("ăâđêôơưạảấầẩẫậắằẳẵặẹẻẽếềểễệỉịọỏốồổỗộớờởỡợụủứừửữựỳỵỷỹ")


# Countries whose channels speak the language, for "вьетнамские каналы".
_LANG_COUNTRIES = {"vi": ("VN",), "ru": ("RU", "BY", "KZ", "KG"),
                   "en": ("US", "GB", "CA", "AU", "IE", "NZ")}  # fmt: skip

_LANG_CODES = {"rus": "ru", "eng": "en", "vie": "vi"}


def _lang_code(lang: str | None) -> str | None:
    """Guides write "ru", "rus", "vi-VN" or "en_GB"; filters use the two-letter code."""
    if not lang:
        return None
    base = lang.replace("_", "-").split("-", 1)[0].strip().casefold()
    return _LANG_CODES.get(base, base) or None


def _guess_lang(title: str) -> str | None:
    """Guides often omit lang; the script of the title is a fair hint."""
    text = title.casefold()
    if re.search(r"[а-яё]", text):
        return "ru"
    if any(ch in _VI_LETTERS for ch in text):
        return "vi"
    if re.search(r"[a-z]", text):
        return "en"
    return None


def _like_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
