import csv
import json

from mih.cli import main
from mih.export import build_catalog, categorize, export_catalog
from mih.sources.importer import import_m3u
from tests.conftest import fixture_bytes


def test_categorize_matches_ru_en_vi_words_and_prefixes():
    assert categorize(["Новости 24"]) == ["news"]
    assert categorize(["Спортивные"]) == ["sport"]
    assert categorize(["Phim truyện"]) == ["movies"]
    assert categorize(["Kids"]) == ["kids"]
    # "show" must be a word start, not any substring
    assert categorize(["Moshow"]) == []


def _load(conn, settings, make_source):
    src = make_source("a")
    import_m3u(conn, settings, src, fixture_bytes("provider_a.m3u"))


def test_catalog_has_categories_groups_and_no_urls(conn, settings, make_source):
    _load(conn, settings, make_source)
    by_name = {c["name"]: c for c in build_catalog(conn)}
    euronews = next(c for n, c in by_name.items() if n.startswith("Euronews"))
    assert "news" in euronews["categories"]
    assert euronews["category_source"] == "playlist"
    assert "News" in euronews["groups"]
    vtv = next(c for n, c in by_name.items() if n.startswith("VTV1"))
    assert vtv["countries"] == ["VN"]
    assert vtv["health"] == "grey"  # imported, never checked
    assert vtv["sources"] == ["a"]
    assert "SECRET" not in json.dumps(list(by_name.values()))


def test_export_writes_json_csv_and_playlist(conn, settings, make_source, tmp_path):
    _load(conn, settings, make_source)
    out = tmp_path / "out"
    result = export_catalog(conn, out)
    assert sorted(result.files) == ["channels.csv", "channels.json", "playlist.m3u"]

    doc = json.loads((out / "channels.json").read_text(encoding="utf-8"))
    assert doc["channels_total"] == result.channels == len(doc["channels"])
    assert doc["categories"]["news"] >= 1
    assert "SECRET" not in (out / "channels.json").read_text(encoding="utf-8")

    rows = list(csv.DictReader((out / "channels.csv").read_text(encoding="utf-8-sig")
                               .splitlines()))  # fmt: skip
    assert len(rows) == result.channels

    m3u = (out / "playlist.m3u").read_text(encoding="utf-8")
    assert m3u.startswith("#EXTM3U")
    assert m3u.count("#EXTINF") == result.playlist_entries >= result.channels
    assert "#EXTVLCOPT:http-user-agent=SomePlayer/1.0" in m3u
    assert 'group-title="news' in m3u
    assert not list(out.glob(".*"))  # no temp files left behind


def test_export_without_playlist_and_cli(conn, settings, make_source, tmp_path, monkeypatch):
    _load(conn, settings, make_source)
    result = export_catalog(conn, tmp_path / "x", with_playlist=False)
    assert result.files == ["channels.json", "channels.csv"]
    assert not (tmp_path / "x" / "playlist.m3u").exists()

    monkeypatch.setenv("MIH_DATA_DIR", str(settings.data_dir))
    monkeypatch.setenv("MIH_EXPORT_DIR", str(tmp_path / "cli"))
    assert main(["export"]) == 0
    assert (tmp_path / "cli" / "channels.json").exists()
