"""Channel categories from weighted signals (mih.categories)."""

from datetime import UTC, datetime, timedelta

from mih.categories import decide, guide_mix, programme_categories
from mih.epg.importer import import_xmltv
from mih.epg.service import create_epg_source
from mih.export import build_catalog
from mih.sources.importer import import_m3u
from tests.conftest import fixture_bytes

NOW = datetime(2026, 10, 26, 7, 0, tzinfo=UTC)


def test_weak_word_alone_is_not_a_category():
    assert decide(names=["Кино 24"]).categories == ["movies"]
    assert decide(names=["Talk Show Channel"]).categories == ["other"]
    assert decide(names=["Новости 24"]).categories == ["news"]


def test_directory_outweighs_a_stray_name_word():
    d = decide(directory=["general"], names=["Первый канал"], groups=["Общие"])
    assert d.categories == ["general"]
    assert d.source == "directory"
    assert d.scores["general"] == 5.0  # directory 3 + playlist 2


def test_primary_first_and_at_most_two_more():
    groups = ["Познавательные", "Кино", "Фильмы", "Музыка", "Music", "Спорт", "Sport"]
    d = decide(directory=["documentary"], groups=groups, names=["Discovery Science"])
    assert d.categories == ["documentary", "sport", "movies"]
    # one stray playlist group is too weak next to a strong primary (7 x 0.3 > 2)
    d = decide(directory=["documentary"], groups=["Познавательные", "Кино"],
               names=["Discovery Science"])  # fmt: skip
    assert d.categories == ["documentary"]


def test_agreeing_playlists_weigh_more():
    assert decide(groups=["News", "Новости"]).scores["news"] == 3.0
    assert decide(groups=["News"]).scores["news"] == 2.0


def test_adult_is_kept_from_any_signal():
    d = decide(directory=["movies"], groups=["Кино", "Фильмы"], guide={"adult": 0.12})
    assert d.categories == ["movies", "adult"]
    assert decide(names=["Brazzers TV"]).categories == ["adult"]


def test_guide_share_steps():
    assert decide(guide={"movies": 0.55}).categories == ["movies"]  # 3
    assert decide(guide={"movies": 0.25}).categories == ["movies"]  # 2
    assert decide(guide={"movies": 0.12}).categories == ["other"]  # 1 is not enough
    d = decide(guide={"movies": 0.6, "news": 0.25}, names=["Кино ТВ"])
    assert d.categories == ["movies", "news"]
    assert d.reasons["movies"] == ["name", "guide 60%"]


def test_programme_categories_read_titles_and_genres():
    assert programme_categories("Х/ф «Ирония судьбы»", []) == {"movies"}
    assert programme_categories("М/с «Смешарики»", []) == {"kids"}
    assert programme_categories("Вечерний выпуск", ["Новости"]) == {"news"}
    # fiction genres count as movies unless a series mark says otherwise
    assert programme_categories("Тайны следствия", ["Детектив"]) == {"movies"}
    assert programme_categories("Т/с «Тайны следствия»", ["Детектив"]) == {"series"}
    assert programme_categories("Bóng đá: Ngoại hạng Anh", []) == {"sport"}


def _p(channel, start_h, minutes, title, category=""):
    s = NOW + timedelta(hours=start_h)
    e = s + timedelta(minutes=minutes)
    cat = f"<category>{category}</category>" if category else ""
    return (f'<programme start="{s:%Y%m%d%H%M%S} +0000" stop="{e:%Y%m%d%H%M%S} +0000"'
            f' channel="{channel}"><title>{title}</title>{cat}</programme>')  # fmt: skip


def test_catalog_uses_the_guide_airtime(conn, settings, make_source):
    import_m3u(conn, settings, make_source("a"), fixture_bytes("provider_a.m3u"))
    # VTV1: a news name, but the guide is mostly football.
    guide = "".join(_p("VTV1.vn", h, 90, "Bóng đá: trực tiếp") for h in range(0, 12, 2))
    guide += _p("VTV1.vn", 12, 30, "Thời sự 19h")
    src = create_epg_source(conn, settings, name="g", url=None)
    assert import_xmltv(conn, settings, src, f"<tv>{guide}</tv>".encode(), now=NOW).status \
        == "applied"  # fmt: skip

    mixes = guide_mix(conn)
    vtv = next(c for c in build_catalog(conn) if c["name"].startswith("VTV1"))
    mix = mixes[vtv["id"]]
    assert mix.airings == 7
    assert round(mix.shares()["sport"], 2) == round(540 / 570, 2)
    assert vtv["categories"] == ["sport", "news"]
    assert vtv["category_source"] == "epg"
    assert vtv["category_reasons"]["news"] == ["name"]

    euronews = next(c for c in build_catalog(conn) if c["name"].startswith("Euronews"))
    assert euronews["categories"] == ["news"]
    assert euronews["category_source"] == "playlist"


def test_adult_words_in_addresses_are_whole_words():
    from mih.categories import url_is_adult

    assert url_is_adult("http://cdn.example/xxx/relax/index.m3u8")
    assert url_is_adult("https://hot-porn.example/live.m3u8")
    assert not url_is_adult("http://tv.example/sussex/news.m3u8")
    assert not url_is_adult("http://adultswim.example/live")  # a cartoon channel
    assert not url_is_adult(None)


NIGHT = """#EXTM3U
#EXTINF:-1 group-title="Ночь",Brazzers TV Europe
http://a.example/1.m3u8
#EXTINF:-1 group-title="Ночь",Playboy TV
http://a.example/2.m3u8
#EXTINF:-1 group-title="Ночь",Dorcel TV
http://a.example/3.m3u8
#EXTINF:-1 group-title="Ночь",Blue Moon
http://a.example/4.m3u8
#EXTINF:-1 group-title="Разное",Relax Lounge
http://cdn.example/xxx/relax.m3u8
#EXTINF:-1 group-title="Разное",Garden Life
http://cdn.example/garden.m3u8
"""


def test_harmless_names_are_caught_by_neighbours_and_addresses(conn, settings, make_source):
    import_m3u(conn, settings, make_source("night"), NIGHT.encode())
    by_name = {c["name"]: c for c in build_catalog(conn)}
    moon = by_name["Blue Moon"]
    assert "adult" in moon["categories"]
    assert moon["category_reasons"]["adult"] == ["mostly adult: night / Ночь"]
    relax = by_name["Relax Lounge"]
    assert relax["categories"] == ["adult"] and relax["category_source"] == "url"
    assert by_name["Garden Life"]["categories"] == ["lifestyle"]  # not adult
    assert "http" not in str(relax["category_reasons"])  # addresses never leave the server


def test_words_from_other_languages_and_brands():
    cases = {
        "Futbol TV UZ": ["sport"], "TGRT Belgesel TR": ["documentary"],
        "Viva Telenovelas IL": ["series"], "Pluto TV El Reino Infantil": ["kids"],
        "Stingray Classica HD DE": ["music"], "tagesschau24": ["news"],
        "Nickelodeon": ["kids"], "Radio BOB! Emo": ["radio"], "Hír FM": ["radio"],
    }  # fmt: skip
    for name, cats in cases.items():
        assert decide(names=[name]).categories == cats, name
    # short words count only as words of their own
    for name in ("Fmovies", "Nickname TV", "Serie A TV", "Raid TV"):
        assert decide(names=[name]).categories == ["other"], name


def test_a_big_broadcaster_is_general_only_when_nothing_else_says_more():
    d = decide(names=["Rai 3 Trentino-Alto Adige Trento"])
    assert (d.categories, d.source) == (["general"], "name")
    assert d.reasons["general"] == ["broadcaster: rai"]
    assert decide(names=["Fox Sports 1"]).categories == ["sport"]
    assert decide(names=["BBC News"]).categories == ["news"]


def test_a_guide_of_many_small_kinds_is_a_general_channel():
    mixed = {"news": 0.15, "series": 0.15, "movies": 0.12, "documentary": 0.05}
    d = decide(guide=mixed)
    assert (d.categories, d.source, d.reasons["general"]) == (["general"], "epg", ["guide: mixed"])
    assert decide(guide={"news": 0.15, "series": 0.12}).categories == ["other"]


def test_guide_genres_in_other_languages():
    assert programme_categories("Tagesschau", ["Nachrichten"]) == {"news"}
    assert programme_categories("Planet Erde", ["Dokumentation"]) == {"documentary"}
    assert "kids" in programme_categories("Peppa", ["Dessin animé", "Jeunesse"])
