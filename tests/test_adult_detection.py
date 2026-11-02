"""18+ channels the stand missed or caught wrongly (audit of 2026-11-01)."""

import json

from mih.categories import adult_by_name, adult_name_keys, decide, programme_categories


def test_adult_brands_and_explicit_words_in_names():
    names = (
        "EroLuxe Cinema", "Velilla TV Milfy HD", "Velilla TV Anal UHD", "Extasy 4K",
        "FrenchLover", "Nu.ART TV", "PassionXXX HD", "TVPlayXXX", "Some BDSM Channel",
        "Velilla TV My Pervy Family UHD",
    )  # fmt: skip
    for name in names:
        assert "adult" in decide(names=[name]).categories, name


def test_adult_words_that_are_not_adult_channels():
    names = (
        "Stingray Pop Adult", "Adult Contemporary Hits", "Pluto TV Adult Animation",
        "Adult Swim", "Velilla TV EuroCup 2024", "Canal 13", "Analog TV", "Pervyy Tulskiy",
        "VF Секс в большом городе", "Sussex TV", "Essex Live",
    )  # fmt: skip
    for name in names:
        assert "adult" not in decide(names=[name]).categories, name
    # the series word does not hide a real adult word next to it
    assert "adult" in decide(names=["Sex and the City XXX"]).categories


def test_erotic_programmes_in_other_languages():
    for title, genres in (("Nocne kino", ["program erotyczny"]), ("Film", ["Erotischer Film"]),
                          ("Notte", ["film erotico"])):  # fmt: skip
        assert "adult" in programme_categories(title, genres), genres


def _directory(conn, rows):
    for i, (name, nsfw) in enumerate(rows):
        conn.execute(
            "INSERT INTO directory_channels (id, name, alt_names, is_nsfw, blocked, updated_at)"
            " VALUES (?, ?, ?, ?, ?, '2026-11-01')",
            (f"D{i}.xx", name, json.dumps([]), int(nsfw), "nsfw" if nsfw else None),
        )


def test_directory_adult_names(conn):
    _directory(conn, [("French Lover", True), ("Happy Channel", True), ("Cinema +", True),
                      ("Cinema", False)])  # fmt: skip
    keys = adult_name_keys(conn)
    assert "frenchlover" in keys and "happychannel" in keys
    assert "cinema" not in keys  # an ordinary directory channel has that name too
    kw = {"keys": keys, "linked_ordinary": False, "guide": None}
    assert adult_by_name(["FrenchLover"], **kw)  # spaces do not matter
    assert adult_by_name(["Happy Channel"], **kw)
    # a guide long enough with no adult programme at all speaks against a bare name
    assert not adult_by_name(["Happy Channel"], keys=keys, linked_ordinary=False,
                             guide={"series": 0.8})  # fmt: skip
    assert adult_by_name(["Happy Channel"], keys=keys, linked_ordinary=False,
                         guide={"movies": 0.7, "adult": 0.2})  # fmt: skip
    # the directory links the channel to an ordinary entry: its own word wins
    assert not adult_by_name(["Happy Channel"], keys=keys, linked_ordinary=True, guide=None)


# ---------- thin 18+ evidence goes to the owner's review (re-check of 2026-11-02) ----------

from mih.categories import (  # noqa: E402
    Decision,
    adult_brands,
    adult_review,
    brand_word,
    explicit_adult_name,
)


def test_explicit_adult_names():
    for name in ("Brazzers TV", "PassionXXX HD", "Hot Erotic Nights", "Dorcel TV"):
        assert explicit_adult_name([name]), name
    for name in ("VF Юмор 18+", "Music Box Sexy", "Sextavision", "Adult Channel"):
        assert not explicit_adult_name([name]), name


def _adult(reasons):
    return Decision(["adult"], "name", {"adult": 2.0}, {"adult": reasons})


def test_which_adult_calls_need_review():
    # firm: the directory says adult, an explicit word, an adult playlist group
    assert adult_review(_adult(["directory"]), nsfw=True, linked_ordinary=False,
                        names=["X"]) is None  # fmt: skip
    assert adult_review(_adult(["name"]), nsfw=False, linked_ordinary=False,
                        names=["Brazzers TV"]) is None  # fmt: skip
    assert adult_review(_adult(["playlist: взрослые"]), nsfw=False, linked_ordinary=False,
                        names=["Shot TV"]) is None  # fmt: skip
    # thin: an ambiguous word, or the directory knows it as an ordinary channel
    assert adult_review(_adult(["name"]), nsfw=False, linked_ordinary=False,
                        names=["VF Юмор 18+"])[0][0] == "weak"  # fmt: skip
    assert adult_review(_adult(["playlist: взрослые"]), nsfw=False, linked_ordinary=True,
                        names=["Shot TV"])[0][0] == "directory-ordinary"  # fmt: skip
    assert adult_review(Decision(["movies"], "epg", {}, {}), nsfw=False,
                        linked_ordinary=True, names=["Kino"]) is None  # fmt: skip


def test_mostly_adult_brands():
    words = {"a": brand_word("Bang Bros HD"), "b": brand_word("Bang Bus"),
             "c": brand_word("Bang Bang TV"), "d": brand_word("Wicked"),
             "e": brand_word("Wicked Tuna"), "f": brand_word("Wicked Tuna Germany")}  # fmt: skip
    brands = adult_brands(words, adult={"a", "b", "d"})
    assert brands == {"bang": (2, 3)}  # one adult «Wicked» among three is not enough
