"""Acceptance tests AT-01…AT-05 from the spec (section 25)."""

from mih.sources.importer import import_m3u
from tests.conftest import count, fixture_bytes


def _channel(conn, name):
    return conn.execute("SELECT * FROM channels WHERE canonical_name = ?", (name,)).fetchone()


def test_at01_reimporting_the_same_playlist_creates_nothing_new(conn, settings, make_source):
    src = make_source("a")
    first = import_m3u(conn, settings, src, fixture_bytes("provider_a.m3u"))
    assert first.status == "applied"
    before = {t: count(conn, t) for t in ("channels", "feeds", "streams", "channel_aliases")}

    again = import_m3u(conn, settings, src, fixture_bytes("provider_a.m3u"))
    assert again.status == "unchanged"
    forced = import_m3u(conn, settings, src, fixture_bytes("provider_a.m3u"), force=True)
    assert forced.status == "applied"
    assert forced.channels_created == 0 and forced.streams_created == 0

    after = {t: count(conn, t) for t in before}
    assert after == before


def test_at02_same_channel_from_three_sources_is_one_channel(conn, settings, make_source):
    for name, fixture in [("a", "provider_a.m3u"), ("b", "provider_b.m3u"),
                          ("c", "provider_c.m3u")]:  # fmt: skip
        assert import_m3u(conn, settings, make_source(name), fixture_bytes(fixture)).status == (
            "applied"
        )

    perviy = conn.execute(
        "SELECT DISTINCT f.channel_id FROM feeds f JOIN channel_aliases a"
        " ON a.channel_id = f.channel_id WHERE a.kind = 'name' AND a.value = 'первый канал'"
    ).fetchall()
    assert len(perviy) == 1
    channel_id = perviy[0]["channel_id"]
    base_streams = conn.execute(
        "SELECT COUNT(*) FROM streams s JOIN feeds f ON f.id = s.feed_id"
        " WHERE f.channel_id = ? AND f.offset_minutes = 0",
        (channel_id,),
    ).fetchone()[0]
    assert base_streams == 4  # a: HD, b: plain, c: tvg-id match + tvg-name match


def test_at03_timeshift_and_regional_versions_stay_separate_feeds(conn, settings, make_source):
    import_m3u(conn, settings, make_source("a"), fixture_bytes("provider_a.m3u"))
    import_m3u(conn, settings, make_source("b"), fixture_bytes("provider_b.m3u"))

    perviy = _channel(conn, "Первый канал HD")
    offsets = sorted(
        r[0] for r in conn.execute(
            "SELECT offset_minutes FROM feeds WHERE channel_id = ?", (perviy["id"],)
        )
    )  # fmt: skip
    assert offsets == [0, 120]

    euronews_ids = {
        r[0] for r in conn.execute(
            "SELECT channel_id FROM channel_aliases WHERE kind = 'name' AND value = 'euronews'"
        )
    }  # fmt: skip
    assert len(euronews_ids) == 1
    regions = sorted(
        r[0] for r in conn.execute(
            "SELECT region FROM feeds WHERE channel_id = ?", (euronews_ids.pop(),)
        )
    )  # fmt: skip
    assert regions == ["asia", "europe"]


def test_at04_broken_or_empty_response_keeps_the_catalog(conn, settings, make_source):
    src = make_source("a")
    import_m3u(conn, settings, src, fixture_bytes("provider_a.m3u"))
    before = {t: count(conn, t) for t in ("channels", "feeds", "streams")}

    for content, reason in [
        (b"", "not_m3u"),
        (b"<html>502 Bad Gateway</html>", "not_m3u"),
        (b"#EXTM3U\n", "empty"),
        (b"#EXTM3U\n#EXTINF:-1,Only one\nhttp://x.example/1\n", "too_few_entries"),
    ]:
        report = import_m3u(conn, settings, src, content)
        assert (report.status, report.reason) == ("rejected", reason)

    assert {t: count(conn, t) for t in before} == before
    absent = conn.execute(
        "SELECT COUNT(*) FROM streams WHERE availability_reason = 'source_absent'"
    ).fetchone()[0]
    assert absent == 0
    statuses = [r[0] for r in conn.execute("SELECT status FROM source_snapshots")]
    assert statuses.count("rejected") == 4


def test_at04_forced_small_playlist_is_accepted(conn, settings, make_source):
    src = make_source("a")
    import_m3u(conn, settings, src, fixture_bytes("provider_a.m3u"))
    small = b"#EXTM3U\n#EXTINF:-1,Only one\nhttp://x.example/1\n"
    assert import_m3u(conn, settings, src, small, force=True).status == "applied"


def test_at05_url_change_updates_the_stream_only(conn, settings, make_source):
    src = make_source("b")
    import_m3u(conn, settings, src, fixture_bytes("provider_b.m3u"))
    channels_before = count(conn, "channels")

    moved = fixture_bytes("provider_b.m3u").replace(b"/ch/101.ts", b"/ch/101-new.ts")
    report = import_m3u(conn, settings, src, moved)

    assert report.channels_created == 0
    assert report.streams_created == 1
    assert report.streams_absent == 1
    assert count(conn, "channels") == channels_before


def test_absent_stream_returns_when_it_reappears(conn, settings, make_source):
    src = make_source("b")
    original = fixture_bytes("provider_b.m3u")
    import_m3u(conn, settings, src, original)
    import_m3u(conn, settings, src, original.replace(b"/ch/101.ts", b"/ch/101-new.ts"))
    import_m3u(conn, settings, src, original)
    flagged = conn.execute(
        "SELECT COUNT(*) FROM streams WHERE availability_reason = 'source_absent'"
    ).fetchone()[0]
    assert flagged == 1  # the temporary URL, not the original one


def test_ambiguous_name_goes_to_review_and_stays_stable(conn, settings, make_source):
    src_ru = make_source("ru")
    src_by = make_source("by")
    import_m3u(
        conn,
        settings,
        src_ru,
        b'#EXTM3U\n#EXTINF:-1 tvg-country="RU",News 24\nhttp://ru.example/n24\n',
    )
    import_m3u(
        conn,
        settings,
        src_by,
        b'#EXTM3U\n#EXTINF:-1 tvg-country="BY",News 24\nhttp://by.example/n24\n',
    )
    assert count(conn, "channels") == 2  # same name, different countries

    src_x = make_source("x")
    unknown = b"#EXTM3U\n#EXTINF:-1,News 24\nhttp://x.example/n24\n"
    first = import_m3u(conn, settings, src_x, unknown)
    assert first.needs_review == 1
    channels = count(conn, "channels")

    again = import_m3u(conn, settings, src_x, unknown, force=True)
    assert again.needs_review == 0 and again.channels_created == 0
    assert count(conn, "channels") == channels


def test_secrets_stay_in_the_locator_table(conn, settings, make_source):
    import_m3u(conn, settings, make_source("a"), fixture_bytes("provider_a.m3u"))
    for table in ("streams", "channels", "feeds", "channel_aliases", "match_decisions"):
        for row in conn.execute(f"SELECT * FROM {table}"):
            assert "SECRET" not in " ".join(str(v) for v in tuple(row))
    urls = [r[0] for r in conn.execute("SELECT url FROM stream_locators")]
    assert any("SECRET_A1" in u for u in urls)


def test_add_source_command_imports_now_and_never_prints_the_url(tmp_path, monkeypatch, capsys):
    from mih import cli
    from mih.sources import service
    from mih.sources.fetch import FetchResult

    secret = "http://iptv.example/playlists/uplist/0123secret/playlist.m3u8"
    body = b'#EXTM3U\n#EXTINF:-1 tvg-id="VTV1.vn",VTV1\nhttp://cdn.example/vtv1.m3u8\n'
    monkeypatch.setattr(service, "check_destination", lambda url, allow_private: None)
    monkeypatch.setattr(service, "fetch_source", lambda url, settings, **kw: FetchResult(
        content=body, etag=None, last_modified=None))  # fmt: skip
    monkeypatch.setenv("MIH_DATA_DIR", str(tmp_path))
    assert cli.main(["add-source", "my list", secret]) == 0
    assert cli.main(["add-source", "my list", secret]) == 1  # the name is taken
    out = capsys.readouterr()
    assert '"channels_created": 1' in out.out and "conflict" in out.err
    assert "0123secret" not in out.out + out.err
