"""The control set (tests/golden_categories.py): every channel gets its right primary
category. Also: guide genres in more languages, one-show channels, sister channels."""

import pytest

from mih.categories import (
    brand_stem,
    brand_votes,
    decide,
    directory_categories,
    programme_categories,
)
from tests.golden_categories import CASES


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.id)
def test_golden_primary(case):
    d = decide(
        names=list(case.names),
        groups=list(case.groups),
        directory=directory_categories(list(case.directory), False),
        guide=case.guide or None,
        one_show=case.one_show,
    )
    assert d.categories[0] == case.expected, (d.categories, d.reasons)


def test_golden_set_is_big_and_varied():
    assert len(CASES) >= 100
    assert len({c.id for c in CASES}) == len(CASES)
    assert len({c.expected for c in CASES}) >= 15


@pytest.mark.parametrize(
    ("genre", "expected"),
    [
        ("Zprávy", "news"),  # cs
        ("Spravodajstvo", "news"),  # sk
        ("Žinios", "news"),  # lt
        ("Ειδήσεις", "news"),  # el
        ("חדשות", "news"),  # he
        ("Animovaný", "kids"),
        ("Pre deti", "kids"),
        ("Vaikams", "kids"),
        ("Παιδικό", "kids"),
        ("ילדים", "kids"),
        ("Dokumentární", "documentary"),
        ("Ντοκιμαντέρ", "documentary"),
        ("תיעודי", "documentary"),
        ("Hudba", "music"),
        ("Muzika", "music"),
        ("Μουσική", "music"),
        ("Zábava", "entertainment"),
        ("Pramogos", "entertainment"),
        ("Ψυχαγωγία", "entertainment"),
        ("בידור", "entertainment"),
        ("Ταινία", "movies"),
        ("Σειρά", "series"),
        ("סדרה", "series"),
        ("Αθλητικά", "sport"),
        ("ספורט", "sport"),
    ],
)
def test_guide_genres_in_more_languages(genre, expected):
    assert expected in programme_categories("", [genre])


def test_one_show_channel_is_series_only_without_other_evidence():
    assert decide(names=["Pluto TV Baywatch"], one_show="Baywatch").categories == ["series"]
    assert decide(names=["Pluto TV Kids"], one_show="Paw Patrol").categories == ["kids"]
    assert decide(names=["Pluto TV"]).categories == ["other"]


def test_radio_in_a_broadcaster_name_is_not_radio():
    assert decide(names=["Radio-Canada INFO"]).categories == ["news"]
    assert decide(names=["Radio Televizija Srbija"]).categories != ["radio"]
    assert decide(names=["BBC Radio 2"]).categories == ["radio"]


def test_brand_stem():
    assert brand_stem("Eurosport 2 HD") == "eurosport"
    assert brand_stem("CNN International") == "cnn"
    assert brand_stem("Fox Sports Asia") == "fox sports"
    assert brand_stem("Rai 3 Trento") == "rai 3 trento"
    assert brand_stem("TV 3") == "tv"


def test_sister_channels_lend_their_category():
    names = {"e1": "Eurosport 1", "e2": "Eurosport 2", "e4": "Eurosport 4K Extra",
             "r3": "Rai 3", "r3t": "Rai 3 Trento", "m1": "MBC 1", "ma": "MBC Action",
             "md": "MBC Drama", "m5": "MBC 5", "t3": "TV 3", "t5": "TV 5"}  # fmt: skip
    primary = {"e1": "sport", "e2": "sport", "r3": "general", "m1": "general",
               "ma": "movies", "md": "series", "t5": "news"}  # fmt: skip
    stems = {cid: brand_stem(n) for cid, n in names.items()}
    got = brand_votes(stems, primary, {"e4", "r3t", "m5", "t3"})
    assert got == {"e4": ("sport", "eurosport"), "r3t": ("general", "rai")}
    # MBC sisters disagree; «TV» is no brand
