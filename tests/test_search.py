"""Programme search (spec 20; scenario С3, AT-07 text part, AT-11 offline)."""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from mih.epg.importer import import_xmltv
from mih.epg.mapping import set_manual
from mih.epg.service import create_epg_source
from mih.search.engine import SearchRequest, search
from mih.search.genres import genre_match
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
    + _p("VTV1.vn", 2 * H, 30, "Thời sự 12h", "Tin tức trong nước và quốc tế", lang="vi")
    # 19:00 at UTC+7 is 12:00Z
    + _p("VTV1.vn", 5 * H, 120, "Bóng đá: Ngoại hạng Anh", "Trực tiếp", lang="vie")
    + _p("VTV1.vn", 8 * H, 60, "Football Weekly", "Highlights", lang="en")
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
    # "роботы" is a known topic, so the English "robots" counts too.
    assert ("Futuris", "2026-10-26T09:00:00Z") in titles
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


def test_paging_reports_totals_and_slices_each_type(loaded):
    full = _find(loaded, "наука", limit=100)
    assert full["total"]["airing"] == len(_titles(full)) == 2
    first = _find(loaded, "наука", limit=1)
    second = _find(loaded, "наука", limit=1, offset=1)
    assert first["total"] == second["total"] == full["total"]
    assert _titles(first) + _titles(second) == _titles(full)
    assert _titles(_find(loaded, "наука", limit=1, offset=2)) == []
    channels = _find(loaded, "канал", target_types=["channel"], limit=1)
    assert channels["total"]["channel"] >= 1
    assert len([i for i in channels["items"] if i["type"] == "channel"]) == 1


def test_strict_search_takes_whole_words_only(loaded):
    assert _titles(_find(loaded, "роботы"))  # "роботы" also finds "робот…" by stem
    assert _titles(_find(loaded, "роботы", strict=True)) == [("Наука", "2026-10-26T12:00:00Z")]
    assert _titles(_find(loaded, "робот", strict=True)) == [("Наука", "2026-10-27T17:00:00Z")]
    assert _titles(_find(loaded, "оров", strict=True)) == []  # no substring fallback
    assert not _find(loaded, "перв", target_types=["channel"], strict=True)["items"]
    assert _find(loaded, "первый", target_types=["channel"], strict=True)["items"]


def test_min_relevance_keeps_the_best_matches(loaded):
    loose = _titles(_find(loaded, "наука роботы"))
    assert _titles(_find(loaded, "наука роботы", min_relevance=1.0)) == loose[:1]


def test_playable_only_leaves_out_channels_without_streams(loaded):
    assert _titles(_find(loaded, "наука", playable_only=True)) == _titles(_find(loaded, "наука"))
    loaded.execute("UPDATE streams SET status = 'quarantine'")
    assert _titles(_find(loaded, "наука", playable_only=True)) == []
    found = _find(loaded, "первый", target_types=["channel"], playable_only=True)
    assert found["items"] == [] and found["total"] == {"channel": 0}


UTC7 = "Asia/Bangkok"


def test_vietnamese_words_with_or_without_diacritics(loaded):
    for query in ("thời sự", "Thời Sự", "thoi su", "tin tức"):
        assert _titles(_find(loaded, query))[0][0] == "Thời sự 12h", query
    # Vietnamese letters: "the" in "thể thao" is a word here, not an English stopword.
    assert parse_query("thể thao", NOW, ZoneInfo(UTC7)).terms == ["the thao"]
    assert parse_query("bóng đá có gì", NOW, ZoneInfo(UTC7)).terms == ["bong da"]


def test_a_topic_in_one_language_finds_it_in_the_others(loaded):
    titles = {t for t, _ in _titles(_find(loaded, "футбол"))}
    assert titles == {"Bóng đá: Ngoại hạng Anh", "Football Weekly"}
    titles = {t for t, _ in _titles(_find(loaded, "bong da"))}
    assert titles == {"Bóng đá: Ngoại hạng Anh", "Football Weekly"}
    hit = next(i for i in _find(loaded, "новости")["items"] if i["type"] == "airing")
    assert hit["title"] == "Thời sự 12h"
    assert hit["why_matched"][0] == {"field": "title", "term": "новости", "matched": "thoi su"}
    # Channel names too: "новости" finds "VTV1 Thời sự".
    assert any(i["channel_name"] == "VTV1 Thời sự FHD"
               for i in _find(loaded, "новости", target_types=["channel"])["items"])  # fmt: skip
    # Strict mode is the words as typed, with no translations.
    assert _titles(_find(loaded, "футбол", strict=True)) == []


def test_vietnamese_time_and_language_words():
    q = parse_query("bóng đá tối nay tiếng Việt", NOW, ZoneInfo(UTC7))
    assert q.terms == ["bong da"] and q.time == "today_evening" and q.languages == ["vi"]
    assert q.as_dict()["from"] == "2026-10-26T11:00:00Z"  # 18:00 at UTC+7
    q = parse_query("phim ngày mai bằng tiếng Anh", NOW, ZoneInfo(UTC7))
    assert q.terms == ["phim"] and q.time == "tomorrow" and q.languages == ["en"]
    assert parse_query("thời sự bây giờ", NOW, ZoneInfo(UTC7)).time == "now"


def test_language_filter_reads_three_letter_and_regional_codes(loaded):
    assert _titles(_find(loaded, "bóng đá", languages=["vi"])) == [
        ("Bóng đá: Ngoại hạng Anh", "2026-10-26T12:00:00Z")
    ]
    assert _titles(_find(loaded, "football", languages=["en"])) == [
        ("Football Weekly", "2026-10-26T15:00:00Z")
    ]


def test_short_syllables_start_a_word_of_a_channel_name(loaded):
    loaded.execute("UPDATE channels SET canonical_name = 'NT Bayern Regional'"
                   " WHERE canonical_name LIKE 'Euronews%'")  # fmt: skip
    loaded.execute(
        "INSERT INTO channel_aliases (channel_id, kind, value) SELECT id, 'name',"
        " 'nt bayern regional' FROM channels WHERE canonical_name = 'NT Bayern Regional'"
    )
    # "gio" is inside "regional" and "bay" starts "bayern": not a match for "bay gio".
    assert not _find(loaded, "bay gio", target_types=["channel"])["items"]
    assert _find(loaded, "vtv", target_types=["channel"])["items"]  # a word start still is


def test_language_with_a_channel_word_lists_that_countrys_channels(loaded):
    for query in ("вьетнамские каналы", "vietnamese channels", "kênh tiếng Việt"):
        result = _find(loaded, query)
        assert result["query"]["channels_only"], query
        assert [i["channel_name"] for i in result["items"]] == ["VTV1 Thời sự FHD"], query
    # Without a language, "канал" is an ordinary word of a name.
    assert _find(loaded, "первый канал", target_types=["channel"])["items"]


def test_a_genre_alone_filters_by_category_or_title(loaded):
    # «Ёлки» is marked «Кино» in its category; the football match only in its title.
    assert [t for t, _ in _titles(_find(loaded, "", genres=["movie"]))] == ["Ёлки"]
    sport = [t for t, _ in _titles(_find(loaded, "", genres=["sport"]))]
    assert sport == ["Bóng đá: Ngoại hạng Anh", "Football Weekly"]
    # Several genres: any of them; words and a genre: both.
    both = _find(loaded, "", genres=["movie", "news"])
    assert {t for t, _ in _titles(both)} == {"Ёлки", "Thời sự 12h"}
    assert _titles(_find(loaded, "football", genres=["movie"])) == []
    assert not [i for i in both["items"] if i["type"] == "channel"]  # programmes only


def test_an_empty_query_lists_what_is_on_now_and_next_and_every_channel(loaded):
    result = _find(loaded, "", limit=50)
    titles = [t for t, _ in _titles(result)]
    # From now on, in start order, for the next hours; the past and tomorrow are left out.
    starts = [s for _, s in _titles(result)]
    assert titles[0] == "Жить здорово!" and starts == sorted(starts)
    assert max(starts) < (NOW + 6 * H).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert result["total"]["channel"] == count(loaded, "channels")


def test_an_unknown_genre_is_refused():
    with pytest.raises(ValueError):
        genre_match(["nope"])


def test_vietnamese_letters_look_among_vietnamese_programmes_and_channels(loaded):
    # Written with Vietnamese letters: the Vietnamese showing only, not the English one.
    result = _find(loaded, "bóng đá")
    assert result["query"]["languages"] == ["vi"] and result["query"]["language_from_script"]
    assert _titles(result) == [("Bóng đá: Ngoại hạng Anh", "2026-10-26T12:00:00Z")]
    assert "airing" not in result["query"].get("widened", [])
    # "bây giờ" is what is on now in Vietnamese: nothing at 07:00Z, so everything on now.
    now = _find(loaded, "bây giờ")
    assert now["query"]["time"] == "now" and now["query"]["widened"] == ["airing"]
    assert _titles(now) == [("Жить здорово!", "2026-10-26T07:00:00Z")]
    # Channels too: "thời sự" names a Vietnamese channel; a word no Vietnamese channel
    # has is looked for everywhere.
    found = _find(loaded, "thời sự", target_types=["channel"])["items"]
    assert [i["channel_name"] for i in found] == ["VTV1 Thời sự FHD"]
    found = _find(loaded, "Euronews Việt", target_types=["channel"])
    assert not found["items"]  # "viet" is in no name: nothing, also when widened
    # A language chosen in the filter wins over the letters.
    assert _titles(_find(loaded, "bóng đá", languages=["en"])) == [
        ("Football Weekly", "2026-10-26T15:00:00Z")
    ]


ADULT = (
    "#EXTM3U\n"
    '#EXTINF:-1 group-title="Для взрослых",Ночной канал\nhttps://x.example/1.m3u8\n'
    '#EXTINF:-1 group-title="XXX",Night One\nhttps://x.example/2.m3u8\n'
    "#EXTINF:-1,Playboy TV\nhttps://x.example/3.m3u8\n"
    '#EXTINF:-1 tvg-language="Japanese" group-title="Anime",NHK Anime\nhttps://x.example/4.m3u8\n'
).encode()


def test_adult_genre_lists_adult_channels_without_a_guide(loaded, settings, make_source):
    from mih.export import build_catalog
    from mih.lists import store_tags

    import_m3u(loaded, settings, make_source("adult"), ADULT)
    store_tags(loaded, build_catalog(loaded))
    found = _find(loaded, "", genres=["adult"], limit=50)
    names = sorted(i["channel_name"] for i in found["items"] if i["type"] == "channel")
    assert names == ["Night One", "Playboy TV", "Ночной канал"]
    assert found["total"]["channel"] == 3
    # A genre without a channel category of its own still lists programmes only.
    assert not [i for i in _find(loaded, "", genres=["drama"])["items"] if i["type"] == "channel"]


def test_language_filter_lists_channels_in_any_language(loaded, settings, make_source):
    from mih.export import build_catalog
    from mih.lists import store_tags
    from mih.search.engine import languages

    import_m3u(loaded, settings, make_source("adult"), ADULT)
    store_tags(loaded, build_catalog(loaded))
    found = _find(loaded, "", languages=["ja"], target_types=["channel"])
    assert [i["channel_name"] for i in found["items"]] == ["NHK Anime"]
    # Channels the catalog gives no language: by the country, as before.
    ru = _find(loaded, "", languages=["ru"], target_types=["channel"])
    assert {i["channel_name"] for i in ru["items"]} >= {"Первый канал HD"}
    codes = [x["code"] for x in languages(loaded)]
    assert {"ru", "en", "vi", "ja"} <= set(codes)
