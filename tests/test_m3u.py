import pytest

from mih.sources.m3u import M3UFormatError, parse_m3u
from tests.conftest import fixture_bytes


def test_parses_attributes_name_and_headers():
    result = parse_m3u(fixture_bytes("provider_a.m3u"))
    assert result.has_header
    assert len(result.entries) == 5
    first = result.entries[0]
    assert first.name == "Первый канал HD"
    assert first.tvg_id == "Perviy.ru"
    assert first.attrs["tvg-country"] == "RU"
    vtv = result.entries[4]
    assert vtv.headers == {"User-Agent": "SomePlayer/1.0", "Referer": "https://a.example/"}


def test_comma_inside_quoted_attribute_does_not_split_the_name():
    result = parse_m3u(fixture_bytes("provider_a.m3u"))
    euronews = result.entries[2]
    assert euronews.group == "News, Europe"
    assert euronews.name == "Euronews (Europe)"


def test_falls_back_to_tvg_name_when_display_name_is_empty():
    result = parse_m3u(fixture_bytes("provider_c.m3u"))
    assert result.entries[1].name == "Первый канал"


def test_bom_and_crlf_are_accepted():
    content = "﻿#EXTM3U\r\n#EXTINF:-1,One\r\nhttp://x.example/1\r\n".encode()
    result = parse_m3u(content)
    assert [e.name for e in result.entries] == ["One"]


def test_skips_unsupported_locators_and_reports_them():
    content = b"#EXTM3U\n#EXTINF:-1,Local\nfile:///etc/passwd\n#EXTINF:-1,Ok\nhttp://x.example/ok\n"
    result = parse_m3u(content)
    assert [e.name for e in result.entries] == ["Ok"]
    assert len(result.errors) == 1


@pytest.mark.parametrize("content", [b"", b"<html><body>502 Bad Gateway</body></html>"])
def test_non_playlist_content_is_an_error(content):
    with pytest.raises(M3UFormatError):
        parse_m3u(content)
