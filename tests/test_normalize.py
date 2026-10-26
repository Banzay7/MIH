import pytest

from mih.identity.normalize import fold, split_name


@pytest.mark.parametrize(
    ("name", "key", "offset", "region", "quality"),
    [
        ("Первый канал HD", "первый канал", 0, "", "hd"),
        ("Первый Канал +2", "первый канал", 120, "", None),
        ("Первый канал (+4)", "первый канал", 240, "", None),
        ("Euronews (Europe)", "euronews", 0, "europe", None),
        ("EURONEWS (Europe) [1080p]", "euronews", 0, "europe", "fhd"),
        ("VTV1 Thời sự FHD", "vtv1 thoi su", 0, "", "fhd"),
        ("Канал+ 4K", "канал+", 0, "", "4k"),
        ("HD", "hd", 0, "", "hd"),
    ],
)
def test_split_name(name, key, offset, region, quality):
    parts = split_name(name)
    assert (parts.key, parts.offset_minutes, parts.region, parts.quality) == (
        key, offset, region, quality,
    )  # fmt: skip


def test_brand_display_drops_offset_marker_only():
    assert split_name("Первый канал (+2)").brand_display == "Первый канал"
    assert split_name("Первый канал HD").brand_display == "Первый канал HD"


def test_fold_keeps_russian_letters_and_strips_latin_diacritics():
    assert fold("Ёлки и Йошкар-Ола") == "елки и йошкар-ола"
    assert fold("Đài Truyền hình") == "dai truyen hinh"
