"""EPG import, mapping and schedule (spec 19; acceptance tests AT-08, AT-09)."""

import gzip
from datetime import UTC, datetime, timedelta

import pytest

from mih.epg import guide
from mih.epg.importer import import_xmltv
from mih.epg.mapping import refresh_mappings, set_manual
from mih.epg.service import create_epg_source
from mih.epg.xmltv import XMLTVFormatError, open_content, parse_time, parse_xmltv
from mih.sources.importer import import_m3u
from tests.conftest import count, fixture_bytes

NOW = datetime(2026, 10, 26, 7, 0, tzinfo=UTC)


@pytest.fixture
def epg_source(conn, settings):
    return create_epg_source(conn, settings, name="guide", url=None)


@pytest.fixture
def catalog(conn, settings, make_source):
    import_m3u(conn, settings, make_source("a"), fixture_bytes("provider_a.m3u"))
    import_m3u(conn, settings, make_source("b"), fixture_bytes("provider_b.m3u"))


def _feed(conn, channel_name_key, offset=0, region=""):
    return conn.execute(
        "SELECT DISTINCT f.id FROM feeds f JOIN channel_aliases a ON a.channel_id = f.channel_id"
        " WHERE a.kind = 'name' AND a.value = ? AND f.offset_minutes = ? AND f.region = ?",
        (channel_name_key, offset, region),
    ).fetchone()["id"]


def _airings(conn, xmltv_id):
    return conn.execute(
        "SELECT a.*, p.title FROM airings a JOIN programmes p ON p.id = a.programme_id"
        " JOIN epg_channels ec ON ec.id = a.epg_channel_id WHERE ec.xmltv_id = ?"
        " ORDER BY a.start_utc",
        (xmltv_id,),
    ).fetchall()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("20261026090000 +0300", "2026-10-26T06:00:00+00:00"),
        ("20261026090000 -0130", "2026-10-26T10:30:00+00:00"),
        ("20261026090000 +03:00", "2026-10-26T06:00:00+00:00"),
        ("20261026090000", "2026-10-26T09:00:00+00:00"),
        ("202610260900 +0700", "2026-10-26T02:00:00+00:00"),
        ("20261026090000 Z", "2026-10-26T09:00:00+00:00"),
    ],
)
def test_xmltv_times_become_utc(raw, expected):
    assert parse_time(raw).isoformat() == expected


def test_parser_reads_channels_programmes_and_reports_bad_ones():
    result = parse_xmltv(open_content(fixture_bytes("guide_a.xml"), 10**7))
    assert set(result.channels) == {"Perviy.ru", "Euronews.fr", "vtv1.vn", "Russia1.ru",
                                    "1tv-plus4"}  # fmt: skip
    vtv = [p for p in result.programmes if p.channel == "vtv1.vn"]
    assert len(vtv) == 1
    assert vtv[0].titles == [("vi", "Thời sự"), ("en", "Evening News")]
    assert vtv[0].episode_num == "E12"
    # One slot ends before it starts, the last Perviy programme has no stop and no successor.
    assert len(result.errors) == 2
    time_pokazhet = next(p for p in result.programmes if p.titles[0][1] == "Время покажет")
    assert time_pokazhet.stop_utc == parse_time("20261026150000 +0300")


def test_at08_utc_offsets_and_repeats(conn, settings, epg_source):
    report = import_xmltv(conn, settings, epg_source, fixture_bytes("guide_a.xml"), now=NOW)
    assert report.status == "applied"
    rows = _airings(conn, "Perviy.ru")
    assert [(r["start_utc"], r["title"]) for r in rows] == [
        ("2026-10-26T03:00:00Z", "Доброе утро"),
        ("2026-10-26T06:00:00Z", "Новости"),
        ("2026-10-26T06:15:00Z", "Жить здорово!"),
        ("2026-10-26T09:00:00Z", "Новости"),
        ("2026-10-26T09:15:00Z", "Время покажет"),
    ]
    assert rows[1]["start_raw"] == "20261026090000 +0300"
    # A repeat is a new airing of the same programme.
    assert rows[1]["id"] != rows[3]["id"]
    assert rows[1]["programme_id"] == rows[3]["programme_id"]
    assert _airings(conn, "vtv1.vn")[0]["start_utc"] == "2026-10-26T12:00:00Z"


def test_reimport_is_idempotent(conn, settings, epg_source):
    content = fixture_bytes("guide_a.xml")
    import_xmltv(conn, settings, epg_source, content, now=NOW)
    before = {t: count(conn, t) for t in ("airings", "programmes", "epg_channels")}
    assert import_xmltv(conn, settings, epg_source, content, now=NOW).status == "unchanged"
    forced = import_xmltv(conn, settings, epg_source, content, force=True, now=NOW)
    assert forced.airings_created == forced.airings_changed == forced.airings_cancelled == 0
    assert forced.airings_unchanged == 9
    assert {t: count(conn, t) for t in before} == before


def test_schedule_changes_version_and_cancel_airings(conn, settings, epg_source):
    content = fixture_bytes("guide_a.xml")
    import_xmltv(conn, settings, epg_source, content, now=NOW)
    changed = content.replace("Жить здорово!".encode(), "Модный приговор".encode())
    changed = changed.replace(b'<programme start="20261026120000 +0300"',
                              b'<programme start="20261026120500 +0300"')  # fmt: skip
    report = import_xmltv(conn, settings, epg_source, changed, now=NOW)
    assert (report.airings_changed, report.airings_cancelled, report.airings_created) == (1, 1, 1)

    rows = {r["start_utc"]: r for r in _airings(conn, "Perviy.ru")}
    assert rows["2026-10-26T06:15:00Z"]["title"] == "Модный приговор"
    assert rows["2026-10-26T06:15:00Z"]["version"] == 2
    assert rows["2026-10-26T09:00:00Z"]["status"] == "cancelled"
    assert rows["2026-10-26T09:05:00Z"]["status"] == "scheduled"
    # The programme nobody airs any more is gone; the shared "Новости" one stays.
    titles = {r[0] for r in conn.execute("SELECT title FROM programmes")}
    assert "Жить здорово!" not in titles and "Новости" in titles


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        (b"", "not_xmltv"),
        (b"<html>502 Bad Gateway</html>", "not_xmltv"),
        (b"<tv></tv>", "empty"),
        (
            b'<tv><programme start="20261026090000" stop="20261026100000" channel="x"><title>cut',
            "not_xmltv",
        ),  # fmt: skip
        (b'<!DOCTYPE tv [<!ENTITY a "aaaa">]><tv>&a;</tv>', "not_xmltv"),
    ],
)
def test_broken_or_empty_guide_keeps_the_schedule(conn, settings, epg_source, content, reason):
    import_xmltv(conn, settings, epg_source, fixture_bytes("guide_a.xml"), now=NOW)
    before = count(conn, "airings")
    report = import_xmltv(conn, settings, epg_source, content, now=NOW)
    assert (report.status, report.reason) == ("rejected", reason)
    assert count(conn, "airings") == before


def test_guide_that_lost_most_channels_is_rejected(conn, settings, epg_source):
    import_xmltv(conn, settings, epg_source, fixture_bytes("guide_a.xml"), now=NOW)
    one = (b'<tv><programme start="20261026090000" stop="20261026100000" channel="Perviy.ru">'
           b"<title>Only</title></programme></tv>")  # fmt: skip
    report = import_xmltv(conn, settings, epg_source, one, now=NOW)
    assert (report.status, report.reason) == ("rejected", "too_few_channels")
    assert import_xmltv(conn, settings, epg_source, one, force=True, now=NOW).status == "applied"


def test_gzip_guide_and_unpacked_size_cap(conn, settings, epg_source):
    packed = gzip.compress(fixture_bytes("guide_a.xml"))
    assert import_xmltv(conn, settings, epg_source, packed, now=NOW).status == "applied"

    bomb = gzip.compress(b"<tv>" + b" " * 2_000_000 + b"</tv>")
    with pytest.raises(XMLTVFormatError):
        parse_xmltv(open_content(bomb, 1_000_000))
    small = settings.__class__(data_dir=settings.data_dir, epg_max_bytes=1_000_000)
    report = import_xmltv(conn, small, epg_source, bomb, force=True, now=NOW)
    assert (report.status, report.reason) == ("rejected", "too_large")


def test_old_airings_are_pruned(conn, settings, epg_source):
    import_xmltv(conn, settings, epg_source, fixture_bytes("guide_a.xml"), now=NOW)
    later = NOW + timedelta(days=settings.epg_retention_days + 1)
    other = (b'<tv><programme start="20261115090000" stop="20261115100000" channel="Perviy.ru">'
             b"<title>Later</title></programme></tv>")  # fmt: skip
    report = import_xmltv(conn, settings, epg_source, other, force=True, now=later)
    assert report.airings_pruned == 9
    assert count(conn, "airings") == 1 and count(conn, "programmes") == 1


def test_mapping_by_id_name_review_and_unmapped(conn, settings, catalog, epg_source):
    report = import_xmltv(conn, settings, epg_source, fixture_bytes("guide_a.xml"), now=NOW)
    # Perviy and VTV1 by tvg-id; Euronews has two regional feeds; Россия 1 and the +4
    # guide channel have no feed in the catalog.
    assert (report.mapped, report.needs_review, report.unmapped) == (2, 1, 2)

    mappings = {r["xmltv_id"]: r for r in conn.execute(
        "SELECT ec.xmltv_id, m.feed_id, m.decision, m.rule FROM epg_channels ec"
        " LEFT JOIN epg_mappings m ON m.epg_channel_id = ec.id"
    )}  # fmt: skip
    assert mappings["Perviy.ru"]["feed_id"] == _feed(conn, "первый канал")
    assert mappings["vtv1.vn"]["rule"] == "xmltv-id"
    assert mappings["Euronews.fr"]["decision"] == "needs_review"
    assert mappings["Euronews.fr"]["feed_id"] is None
    assert mappings["Russia1.ru"]["decision"] is None


def test_timeshift_feed_borrows_the_base_schedule(conn, settings, catalog, epg_source):
    import_xmltv(conn, settings, epg_source, fixture_bytes("guide_a.xml"), now=NOW)
    plus2 = _feed(conn, "первый канал", offset=120)
    guides = guide.feed_guides(conn)
    assert guides[plus2].shift_minutes == 120
    assert guides[plus2].borrowed_from == _feed(conn, "первый канал")

    items = guide.schedule(conn, guides[plus2], NOW, NOW + timedelta(hours=3))
    news = next(a for a in items if a["title"] == "Новости")
    assert news["start"] == "2026-10-26T08:00:00Z"  # 06:00Z on the base feed, +2 h

    state = guide.now_and_next(conn, NOW, guides)
    assert state[plus2]["now"]["title"] == "Доброе утро"  # 03:00Z–06:00Z, +2 h covers 07:00Z
    assert state[_feed(conn, "первый канал")]["now"]["title"] == "Жить здорово!"
    assert state[_feed(conn, "первый канал")]["next"]["start"] == "2026-10-26T09:00:00Z"


def test_name_mapping_respects_timeshift(conn, settings, catalog, epg_source):
    guide_xml = (
        '<tv><channel id="p2"><display-name>Первый канал +2</display-name></channel>'
        '<programme start="20261026090000" stop="20261026100000" channel="p2">'
        "<title>Сдвинутые новости</title></programme></tv>"
    ).encode()
    import_xmltv(conn, settings, epg_source, guide_xml, now=NOW)
    plus2 = _feed(conn, "первый канал", offset=120)
    g = guide.feed_guides(conn)[plus2]
    assert g.shift_minutes == 0 and g.borrowed_from is None  # its own guide wins


def test_playlist_imported_after_the_guide_gets_mapped(conn, settings, make_source, epg_source):
    report = import_xmltv(conn, settings, epg_source, fixture_bytes("guide_a.xml"), now=NOW)
    assert report.mapped == 0
    import_m3u(conn, settings, make_source("a"), fixture_bytes("provider_a.m3u"))
    assert conn.execute(
        "SELECT COUNT(*) FROM epg_mappings WHERE decision = 'auto'"
    ).fetchone()[0] == 2  # fmt: skip


def test_manual_mapping_survives_reimport(conn, settings, catalog, epg_source):
    content = fixture_bytes("guide_a.xml")
    import_xmltv(conn, settings, epg_source, content, now=NOW)
    euronews = conn.execute(
        "SELECT id FROM epg_channels WHERE xmltv_id = 'Euronews.fr'"
    ).fetchone()["id"]
    europe = _feed(conn, "euronews", region="europe")
    set_manual(conn, euronews, europe, "2026-10-26T07:00:00Z")
    perviy = conn.execute("SELECT id FROM epg_channels WHERE xmltv_id = 'Perviy.ru'").fetchone()
    set_manual(conn, perviy["id"], None, "2026-10-26T07:00:00Z")

    import_xmltv(conn, settings, epg_source, content, force=True, now=NOW)
    refresh_mappings(conn, "2026-10-26T08:00:00Z")
    rows = {r["epg_channel_id"]: r for r in conn.execute("SELECT * FROM epg_mappings")}
    assert (rows[euronews]["feed_id"], rows[euronews]["decision"]) == (europe, "approved")
    assert (rows[perviy["id"]]["feed_id"], rows[perviy["id"]]["decision"]) == (None, "rejected")


def test_xml_entities_and_external_references_are_refused():
    xxe = b'<!DOCTYPE tv [<!ENTITY x SYSTEM "file:///etc/passwd">]><tv>&x;</tv>'
    with pytest.raises(XMLTVFormatError):
        parse_xmltv(open_content(xxe, 10**6))


def test_guide_channels_join_the_catalog_without_a_schedule(conn, settings, catalog, epg_source):
    before = conn.execute("SELECT COUNT(*) FROM channels").fetchone()[0]
    report = import_xmltv(conn, settings, epg_source, fixture_bytes("guide_a.xml"), now=NOW)
    # Россия 1 is new; "Первый канал +4" is a version of a channel the catalog has.
    assert report.channels_added == 1
    names = [r[0] for r in conn.execute("SELECT canonical_name FROM channels")]
    assert len(names) == before + 1 and "Россия 1" in names
    added = conn.execute("SELECT id FROM channels WHERE canonical_name = 'Россия 1'").fetchone()
    # No feed, so no guide mapping (MIH_EPG_SCOPE=tracked then stores no schedule).
    assert not conn.execute("SELECT 1 FROM feeds WHERE channel_id = ?", (added["id"],)).fetchone()

    again = import_xmltv(conn, settings, epg_source, fixture_bytes("guide_a.xml"), now=NOW,
                         force=True)  # fmt: skip
    assert again.channels_added == 0

    # A playlist that brings a stream for it lands on the same channel.
    import_m3u(conn, settings, make_source_c(conn, settings),
               "#EXTM3U\n#EXTINF:-1,Россия 1 HD\nhttps://r.example/r1.m3u8\n".encode())  # fmt: skip
    assert conn.execute("SELECT COUNT(*) FROM channels").fetchone()[0] == before + 1
    assert conn.execute("SELECT 1 FROM feeds WHERE channel_id = ?", (added["id"],)).fetchone()


def make_source_c(conn, settings):
    from mih.sources.service import create_source

    return create_source(conn, settings, name="r", url=None)


def test_only_programmes_in_scope_are_built(conn, settings, catalog, monkeypatch):
    from dataclasses import replace

    from mih.epg import xmltv

    built = []
    real = xmltv._programme

    def spy(elem):
        built.append(elem.get("channel"))
        return real(elem)

    monkeypatch.setattr(xmltv, "_programme", spy)
    mapped_only = replace(settings, epg_scope="mapped")
    source = create_epg_source(conn, mapped_only, name="g2", url=None)
    import_xmltv(conn, mapped_only, source, fixture_bytes("guide_a.xml"), now=NOW)
    # Россия 1 has no feed, so its programmes are counted but never built.
    assert built and "Russia1.ru" not in built
