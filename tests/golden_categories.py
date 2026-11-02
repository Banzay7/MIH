"""Control set for channel categories: about a hundred channels with the right primary
category, checked by tests/test_categories_golden.py.

Each case is the evidence the catalog build would hand to ``mih.categories.decide`` for a
real channel: its names, playlist groups, iptv-org directory categories and guide airtime
shares (already mapped to channel categories). Add a case for every channel found in the
wrong category, so a later change cannot bring the mistake back.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Case:
    expected: str  # the primary category
    names: tuple[str, ...]
    groups: tuple[str, ...] = ()
    directory: tuple[str, ...] = ()
    guide: dict[str, float] = field(default_factory=dict)
    one_show: str | None = None  # the guide's one programme title, if any

    @property
    def id(self) -> str:
        return self.names[0]


def C(expected, *names, groups=(), directory=(), guide=None, one_show=None) -> Case:  # noqa: N802
    return Case(expected, names, tuple(groups), tuple(directory), guide or {}, one_show)


CASES: list[Case] = [
    # ---- news ----
    C("news", "Россия 24", groups=["Новости"]),
    C("news", "CNN International", directory=["news"]),
    C("news", "BBC News", groups=["News"]),
    C("news", "Euronews English", directory=["news"], groups=["News"]),
    C("news", "Al Jazeera English", directory=["news"]),
    C("news", "France 24", groups=["Info"]),
    C("news", "franceinfo"),
    C("news", "Radio-Canada INFO"),
    C("news", "RTL Info"),
    C("news", "Sky News", guide={"news": 0.8}),
    C("news", "Rai News 24"),
    C("news", "Tagesschau24"),
    C("news", "TRT Haber"),
    C("news", "Telemundo Noticias"),
    C("news", "Thời sự VTV", groups=["Tin tức"]),
    C("news", "ČT24", guide={"news": 0.7}),
    C("news", "Bloomberg TV", directory=["business", "news"], groups=["News"]),
    # ---- sport ----
    C("sport", "Матч ТВ", groups=["Спорт"]),
    C("sport", "Eurosport 1", directory=["sports"]),
    C("sport", "beIN Sports 1"),
    C("sport", "Sky Sport Calcio"),
    C("sport", "NBA TV"),
    C("sport", "Fox Sports 1", groups=["Sports"]),
    C("sport", "Setanta Sports", guide={"sport": 0.9}),
    C("sport", "Real Madrid TV", groups=["Deportes"]),
    C("sport", "K+ Sport 1", groups=["Thể thao"]),
    C("sport", "Action Sports TV"),
    C("sport", "Hockey TV", guide={"sport": 0.6, "news": 0.1}),
    # ---- kids ----
    C("kids", "Карусель", groups=["Детские"]),
    C("kids", "Cartoon Network", directory=["kids", "animation"]),
    C("kids", "Nickelodeon"),
    C("kids", "Disney Junior"),
    C("kids", "Мультимания"),
    C("kids", "Baby TV"),
    C("kids", "Rai Yoyo", directory=["kids"]),
    C("kids", "KiKA", guide={"kids": 0.85}),
    C("kids", "Boomerang", groups=["Kids"]),
    # ---- movies ----
    C("movies", "Кино ТВ", groups=["Кино"]),
    C("movies", "HBO", directory=["movies"]),
    C("movies", "AXN"),
    C("movies", "MBC Action"),
    C("movies", "MBC Max", groups=["Movies"]),
    C("movies", "Rai Movie"),
    C("movies", "Paramount Action"),
    C("movies", "Dom Kino", groups=["Кино"]),
    C("movies", "TCM", guide={"movies": 0.9}),
    C("movies", "Cine Latino", directory=["movies"]),
    C("movies", "Kino Polska", groups=["Filmy"]),
    C("movies", "Hollywood Thriller"),
    C("movies", "Western TV"),
    # ---- series ----
    C("series", "СТС Love", groups=["Сериалы"]),
    C("series", "Dizi TV"),
    C("series", "Novelas TV", directory=["series"]),
    C("series", "Fox Life", guide={"series": 0.7}),
    C("series", "Pluto TV Baywatch", one_show="Baywatch"),
    C("series", "Pluto TV Star Trek"),
    C("series", "Pluto TV Forensic Files", one_show="Forensic Files"),
    C("series", "Rakuten TV Drama"),
    # ---- music ----
    C("music", "Муз-ТВ", groups=["Музыка"]),
    C("music", "MTV Hits"),
    C("music", "VH1 Classic"),
    C("music", "Stingray Classica", directory=["music"]),
    C("music", "Deejay TV"),
    C("music", "Ru.TV", groups=["Музыкальные"]),
    C("music", "Jazz TV"),
    C("music", "Bridge TV", guide={"music": 0.75}),
    # ---- documentary ----
    C("documentary", "Discovery Channel", directory=["documentary"]),
    C("documentary", "National Geographic"),
    C("documentary", "History Channel"),
    C("documentary", "Моя планета", groups=["Познавательные"]),
    C("documentary", "Animal Planet", guide={"documentary": 0.8}),
    C("documentary", "Rai Storia"),
    C("documentary", "ZDFinfo", guide={"documentary": 0.6, "news": 0.15}),
    C("documentary", "Nat Geo Wild"),
    # ---- education ----
    C("education", "Rai Scuola"),
    C("education", "TV Escola"),
    C("education", "Канал Школа"),
    C("education", "ARD-alpha", directory=["education"]),
    C("education", "Bildungsfernsehen"),
    C("education", "Learning Channel Kids Edu", directory=["education"]),
    # ---- entertainment ----
    C("entertainment", "ТНТ", groups=["Развлекательные"]),
    C("entertainment", "Comedy Central", directory=["comedy"]),
    C("entertainment", "E! Entertainment"),
    C("entertainment", "Юмор ТВ"),
    C("entertainment", "Reality TV Channel"),
    # ---- lifestyle ----
    C("lifestyle", "Food Network", directory=["cooking"]),
    C("lifestyle", "Travel Channel"),
    C("lifestyle", "Кухня ТВ", groups=["Кулинария"]),
    C("lifestyle", "Fashion TV"),
    C("lifestyle", "Охота и рыбалка", directory=["outdoor"]),
    # ---- religion ----
    C("religion", "Спас", directory=["religious"]),
    C("religion", "EWTN"),
    C("religion", "God TV", groups=["Religious"]),
    C("religion", "TBN"),
    # ---- business ----
    C("business", "CNBC", directory=["business"]),
    C("business", "РБК", groups=["Бизнес"]),
    # ---- general ----
    C("general", "Первый канал", directory=["general"], groups=["Общие"]),
    C("general", "Россия 1", groups=["Эфирные", "Общие"]),
    C("general", "Rai 3 Trento"),
    C("general", "ZDF"),
    C("general", "TF1", directory=["general"]),
    C("general", "NBC WPTV-TV"),
    C("general", "ICI Radio-Canada Télé"),
    C("general", "TVRI Nasional"),
    C("general", "Channel 5", guide={"news": 0.15, "series": 0.15, "movies": 0.15}),
    # ---- regional ----
    C("regional", "ОТР", groups=["Региональные"]),
    C("regional", "Local Now", directory=["general"], groups=["Local", "Regional"]),
    # ---- shop ----
    C("shop", "Shop 24", directory=["shop"]),
    C("shop", "QVC", groups=["Shop"]),
    C("shop", "Телемагазин"),
    # ---- radio ----
    C("radio", "Европа Плюс Радио"),
    C("radio", "Radio Record", groups=["Radio"]),
    C("radio", "BBC Radio 1"),
    C("radio", "Hit FM"),
    # ---- adult (primary when nothing else) ----
    C("adult", "Brazzers TV"),
    C("adult", "Русская ночь"),
    C("adult", "Private TV", directory=["xxx"]),
    C("adult", "Brazzers HD"),
    C("adult", "Alba XXX 1"),
    C("adult", "Beate Uhse"),
    C("adult", "Babes TV"),
    # 18+ audit of the stand, 2026-11-01: brands and glued words
    C("adult", "EroLuxe Blonde"),
    C("adult", "PassionXXX HD"),
    C("adult", "FrenchLover"),
    C("adult", "Nu.ART TV"),
    C("adult", "Velilla TV Anal UHD"),
    # ... and adult words in names of ordinary channels
    C("music", "Stingray Pop Adult", directory=["music"]),
    C("kids", "Pluto TV Adult Animation", directory=["animation"]),
    C("series", "VF Секс в большом городе", groups=["Сериалы"]),
    C("sport", "Velilla TV EuroCup 2024", groups=["Sport"]),
    C("regional", "Pervyy Tulskiy", groups=["Regional"]),
    # ---- nothing to go by ----
    C("other", "Talk Show Channel"),
    C("other", "XYZ 7"),
]
