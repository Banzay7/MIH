"""Countries and subdivisions from playlist group names and channel names."""

from mih.places import country_of_group, place_in_names, subdivision_of_group


def test_country_groups():
    assert country_of_group("Italy") == "IT"
    assert country_of_group("USA") == "US"
    assert country_of_group("United States") == "US"
    assert country_of_group("українські") == "UA"
    assert country_of_group("ישראלי") == "IL"
    assert country_of_group("Turkey") == "TR"
    assert country_of_group("Asturias, Principado de") == "ES"
    assert country_of_group("HD") is None
    assert country_of_group("другие") is None


def test_subdivision_groups():
    assert subdivision_of_group("California") == "california"
    assert subdivision_of_group("Busan-gwangyeoksi") == "busan gwangyeoksi"
    assert subdivision_of_group("Asturias, Principado de") == "asturias"
    assert subdivision_of_group("Georgia") is None  # a country too
    assert subdivision_of_group("Italy") is None
    for g in ("Sport", "News", "Music", "Kids", "Movies", "Local", "Centre", "Nord"):
        assert subdivision_of_group(g) is None


def test_place_in_names_needs_the_channel_country():
    assert place_in_names(["TV Prato"], ["IT"]) == "prato"
    assert place_in_names(["TV Prato"], ["it"]) == "prato"
    assert place_in_names(["TV Prato"], ["DE"]) is None
    assert place_in_names(["Most TV"], ["CZ"]) is None  # an ordinary word
    assert place_in_names(["TV Prato"], []) is None
