"""Programme search (spec 20; scenario С3, AT-07 text part, AT-11 offline)."""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from mih.epg.importer import import_xmltv
from mih.epg.mapping import set_manual
from mih.epg.service import create_epg_source
from mih.search.engine import SearchRequest, search
from mih.search.index import ensure_index
from mih.search.query import parse_query, stem
from mih.sources.importer import import_m3u
from tests.conftest import count, fixture_bytes

# 10:00 in Moscow on 26 October 2026.
NOW = datetime(2026, 10, 26, 7, 0, tzinfo=UTC)
MSK = "Europe/Moscow"


def _p(channel, start, minutes, title, desc="", lang="ru", category=""):
    s = (NOW + start).strftime("%Y%m%d%H%M%S +0000")
    e = (NOW + start + timedelta(minutes=minutes)).strftime("%Y%m%d%H%M%S +0000")
    lang_attr = f' lang="{lang}"' if lang else ""
    desc_xml = f"<desc{lang_attr}>{desc}</desc>" if desc else ""
    cat_xml = f"<category>{category}</category>" if category else ""
    return (f'<programme start="{s}" stop="{e}" channel="{channel}">'
            f"<title{lang_attr}>{title}</title>{desc_xml}{cat_xml}</programme>")  # fmt: skip


H = timedelta(hours=1)
GUIDE = (
    "<tv>"
    + _p("Perviy.ru", -H, 30, "Наука", "Как работают промышленные роботы")  # past
    + _p("Perviy.ru", 0 * H, 60, "Жить здорово!", "Медицинское ток-шоу")  # now
    + _p("Perviy.ru", 3 * H, 60, "Ёлки", "Новогодняя комедия", category="Кино")
    + _p("Perviy.ru", 5 * H, 60, "Наука", "Как работают промышленные роботы")  # repeat
    # tomorrow 20:00 MSK
    + _p("Perviy.ru", 34 * H, 60, "Наука", "Гуманоидный робот учится ходить")
    + _p("Euronews.fr", 2 * H, 30, "Futuris", "Humanoid robots in European labs", lang="en")
    + _p("Euronews.fr", 3 * H, 30, "Business Line", "Markets and the economy", lang="")
    + "</tv>"
).encode()


@pytest.fixture
def loaded(conn, settings, make_source):
    import_m3u(conn, settings, make_source("a"), fixture_bytes("provider_a.m3u"))
    src = create_epg_source(conn, settings, name="g", url=None)
    report = import_xmltv(conn, settings, src, GUIDE, now=NOW)
    assert report.status == "applied"
    # Euronews has two regional feeds; pick one so its programmes are searchable.
    feed = conn.execute(
        "SELECT f.id FROM feeds f WHERE f.region = 'europe'"
    ).fetchone()["id"]  # fmt: skip
    ec = conn.execute("SELECT id FROM epg_channels WHERE xmltv_id = 'Euronews.fr'").fetchone()
    set_manual(conn, ec["id"], feed, "2026-10-26T07:00:00Z")
    return conn


def _find(conn, query, **kw):
    return search(conn, SearchRequest(query=query, tz=MSK, **kw), NOW)


def _titles(result, kind="airing"):
    return [(i["title"], i["start"]) for i in result["items"] if i["type"] == kind]


def test_parse_query_extracts_time_language_and_terms():
    q = parse_query("Что завтра про гуманоидных роботов на русском?", NOW, ZoneInfo(MSK))
    assert q.terms == ["гуманоидных", "роботов"]
    assert q.languages == ["ru"] and q.time == "tomorrow"
    assert q.as_dict()["from"] == "2026-10-26T21:00:00Z"  # midnight in Moscow
    assert [stem(t) for t in q.terms] == ["гуманоид", "робот"]


def test_c3_tomorrow_humanoid_robots_in_russian(loaded):
    result = _find(loaded, "что завтра про гуманоидных роботов на русском")
    [hit] = [i for i in result["items"] if i["type"] == "airing"]
    assert (hit["title"], hit["start"]) == ("Наука", "2026-10-27T17:00:00Z")
    assert hit["channel_name"] == "Первый канал HD"
    assert hit["why_matched"][:2] == [
        {"field": "description", "term": "гуманоидных", "matched": "гуманоидный"},
        {"field": "description", "term": "роботов", "matched": "робот"},
    ]
    assert {"field": "time", "term": "tomorrow"} in hit["why_matched"]
    assert hit["streams_available"] == 2


def test_without_time_words_upcoming_matches_rank_by_relevance(loaded):
    result = _find(loaded, "роботы")
    titles = _titles(result)
    # Past airing excluded; the 12:00Z repeat collapses into the 12:00 one.
    assert ("Наука", "2026-10-26T12:00:00Z") in titles
    repeat = next(i for i in result["items"] if i["start"] == "2026-10-26T12:00:00Z")
    assert repeat["more_airings"] == 0  # the earlier showing is in the past
    assert ("Наука", "2026-10-26T06:00:00Z") not in titles
    assert ("Futuris", "2026-10-26T09:00:00Z") not in titles  # "robots" ≠ "роботы"
    assert ("Наука", "2026-10-27T17:00:00Z") in titles  # different description


def test_language_filter_uses_guide_lang_or_title_script(loaded):
    en = _titles(_find(loaded, "robots in english"))
    assert en == [("Futuris", "2026-10-26T09:00:00Z")]
    # No lang attribute on Business Line: the Latin title counts as English.
    assert _titles(_find(loaded, "markets", languages=["en"])) == [
        ("Business Line", "2026-10-26T10:00:00Z")
    ]
    assert _titles(_find(loaded, "markets", languages=["ru"])) == []


def test_now_lists_what_is_on(loaded):
    assert _titles(_find(loaded, "что сейчас")) == [("Жить здорово!", "2026-10-26T07:00:00Z")]


def test_yo_and_substring_and_category(loaded):
    assert _titles(_find(loaded, "елки"))[0][0] == "Ёлки"
    assert _titles(_find(loaded, "кино"))[0][0] == "Ёлки"
    # "оров" is not a word prefix; the trigram index finds it inside "здорово".
    hit = _find(loaded, "оров")["items"][0]
    assert hit["title"] == "Жить здорово!"
    assert hit["why_matched"][0]["matched"] == "substring"


def test_channels_are_found_by_name_too(loaded):
    result = _find(loaded, "первый")
    [channel] = [i for i in result["items"] if i["type"] == "channel"]
    assert channel["channel_name"] == "Первый канал HD"
    only_airings = _find(loaded, "первый", target_types=["airing"])
    assert all(i["type"] == "airing" for i in only_airings["items"])


def test_explicit_window_beats_words(loaded):
    start = NOW + 30 * H
    result = _find(loaded, "наука", start=start, end=start + 10 * H)
    assert _titles(result) == [("Наука", "2026-10-27T17:00:00Z")]


def test_index_follows_programme_changes_and_rebuilds(conn, settings, loaded):
    src = conn.execute("SELECT id FROM epg_sources").fetchone()["id"]
    changed = GUIDE.replace("Ёлки".encode(), "Ирония судьбы".encode())
    import_xmltv(conn, settings, src, changed, now=NOW)
    assert _titles(_find(conn, "елки")) == []
    assert _titles(_find(conn, "ирония"))[0][0] == "Ирония судьбы"
    assert count(conn, "programme_fts") == count(conn, "programmes")

    conn.execute("DELETE FROM programme_fts")
    assert ensure_index(conn) is True
    assert ensure_index(conn) is False
    assert _titles(_find(conn, "ирония"))[0][0] == "Ирония судьбы"


def test_odd_input_is_safe(loaded):
    for query in ['"', "*", "AND OR NOT", 'роб"от*', "", "   ", "a"]:
        _find(loaded, query)  # no FTS syntax error leaks out
