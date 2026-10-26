import json

import pytest

from mih.cli import main
from mih.directory import DirectoryError, import_directory, parse_rows
from mih.export import build_catalog
from mih.sources.importer import import_m3u
from tests.conftest import FIXTURES, count, fixture_bytes


def _files(fmt: str = "csv") -> dict[str, bytes]:
    out = {}
    for name in ("channels", "feeds", "logos", "blocklist"):
        raw = (FIXTURES / "directory" / f"{name}.csv").read_bytes()
        if fmt == "json":
            rows = parse_rows(raw)
            for r in rows:  # the API's shape: real lists and booleans
                for k in ("alt_names", "categories", "languages"):
                    if k in r:
                        r[k] = [x for x in r[k].split(";") if x]
                for k in ("is_nsfw", "is_main", "in_use"):
                    if k in r:
                        r[k] = r[k] == "TRUE"
            raw = json.dumps(rows).encode()
        out[name] = raw
    return out


def _by_name(conn):
    return {c["name"]: c for c in build_catalog(conn)}


def test_feed_tagged_tvg_ids_are_one_channel(conn, settings, make_source):
    import_m3u(conn, settings, make_source("iptv-org"), fixture_bytes("provider_iptvorg.m3u"))
    names = [c["name"] for c in build_catalog(conn)]
    # ChannelOne.ru@SD and @HD are one channel, without any directory loaded.
    assert len(names) == 2
    one = next(c for c in build_catalog(conn) if c["tvg_ids"][0].startswith("ChannelOne"))
    assert len(one["feeds"]) == 1 and len(one["feeds"][0]["streams"]) == 2


@pytest.mark.parametrize("fmt", ["csv", "json"])
def test_directory_links_merges_and_adds_missing(conn, settings, make_source, fmt):
    import_m3u(conn, settings, make_source("iptv-org"), fixture_bytes("provider_iptvorg.m3u"))
    import_m3u(conn, settings, make_source("free"), fixture_bytes("provider_freetv.m3u"))
    # "Первый канал" (no tvg-id) and Channel One are still apart, NTV is separate.
    assert count(conn, "channels") == 4

    report = import_directory(conn, _files(fmt), countries=("RU",))
    assert report.entries == 7
    # Первый канал and НТВ are found by alternative name (RU), the rest by tvg-id.
    assert report.linked == 4
    assert report.merged == 1
    # Added from the directory: Spice (adult). Not closed EurekaHD, not blocked Pirate,
    # not NTV (already in the catalog), not US Channel One (not asked for).
    assert report.created == 1

    cat = _by_name(conn)
    assert set(cat) == {"Channel One", "VTV1", "НТВ", "Spice"}
    one = cat["Channel One"]
    assert one["directory_id"] == "ChannelOne.ru"
    assert sorted(one["sources"]) == ["free", "iptv-org"]
    assert one["categories"][0] == "general" and one["category_source"] == "directory"
    assert one["countries"] == ["RU"] and one["languages"] == ["ru"]
    assert cat["VTV1"]["languages"] == ["en", "vi"]
    assert cat["НТВ"]["logo_url"] == "https://img.example/ntv.png"
    assert cat["Spice"]["categories"] == ["adult"]
    assert cat["Spice"]["health"] == "none" and cat["Spice"]["feeds"] == []

    # A later playlist with the directory id lands on the added channel.
    late = b'#EXTM3U\n#EXTINF:-1 tvg-id="Spice.ru@SD",Spice TV\nhttps://f.example/s.m3u8\n'
    import_m3u(conn, settings, make_source("late"), late)
    assert count(conn, "channels") == 4


def test_merge_keeps_favorites_and_guides(conn, settings, make_source):
    import_m3u(conn, settings, make_source("iptv-org"), fixture_bytes("provider_iptvorg.m3u"))
    import_m3u(conn, settings, make_source("free"), fixture_bytes("provider_freetv.m3u"))
    perviy = conn.execute(
        "SELECT id FROM channels WHERE canonical_name = 'Первый канал'"
    ).fetchone()["id"]
    conn.execute(
        "INSERT INTO favorites (profile_id, target_kind, target_id, position, created_at)"
        " VALUES ('default', 'channel', ?, 1, 'x')",
        (perviy,),
    )
    import_directory(conn, _files())
    keep = conn.execute("SELECT id FROM channels WHERE directory_id = 'ChannelOne.ru'").fetchone()
    fav = conn.execute("SELECT target_id FROM favorites").fetchone()
    assert fav["target_id"] == keep["id"]
    # Both base feeds became one; all three streams are on it.
    feeds = conn.execute("SELECT id FROM feeds WHERE channel_id = ?", (keep["id"],)).fetchall()
    assert len(feeds) == 1
    assert count(conn, "streams") == 5


def test_ambiguous_name_is_not_linked(conn, settings, make_source):
    # "Channel One" without a country exists in RU and US: no guess.
    import_m3u(conn, settings, make_source("x"),
               b"#EXTM3U\n#EXTINF:-1,Channel One\nhttps://g.example/1.m3u8\n")  # fmt: skip
    report = import_directory(conn, _files())
    assert report.linked == 0


def test_bad_directory_is_rejected_and_cli(conn, settings, tmp_path, monkeypatch):
    with pytest.raises(DirectoryError):
        import_directory(conn, {"channels": b""})
    with pytest.raises(DirectoryError):
        import_directory(conn, {"channels": b"a,b\n1,2\n"})
    assert count(conn, "directory_channels") == 0

    monkeypatch.setenv("MIH_DATA_DIR", str(settings.data_dir))
    assert main(["import-directory", "--from", str(FIXTURES / "directory"),
                 "--country", "VN"]) == 0  # fmt: skip
    assert count(conn, "directory_channels") == 7
    assert main(["import-directory", "--from", str(tmp_path)]) == 1
