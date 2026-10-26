"""What the guide import stores (MIH_EPG_SCOPE) and how long history is kept."""

import dataclasses
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from mih.epg.importer import import_xmltv
from mih.epg.service import create_epg_source
from mih.sources.importer import import_m3u
from tests.conftest import count, fixture_bytes

NOW = datetime(2026, 10, 26, 7, 0, tzinfo=UTC)


@pytest.fixture
def tracked(settings):
    return dataclasses.replace(settings, epg_scope="tracked")


@pytest.fixture
def epg_source(conn, settings):
    return create_epg_source(conn, settings, name="guide", url=None)


@pytest.fixture
def catalog(conn, settings, make_source):
    import_m3u(conn, settings, make_source("a"), fixture_bytes("provider_a.m3u"))
    import_m3u(conn, settings, make_source("b"), fixture_bytes("provider_b.m3u"))


def _stored(conn):
    return sorted(
        (r["xmltv_id"], r["title"])
        for r in conn.execute(
            "SELECT ec.xmltv_id, p.title FROM airings a JOIN programmes p ON p.id = a.programme_id"
            " JOIN epg_channels ec ON ec.id = a.epg_channel_id"
        )
    )


def _favourite(conn, kind, target_id):
    conn.execute(
        "INSERT INTO favorites (profile_id, target_kind, target_id, position, created_at)"
        " VALUES ('default', ?, ?, 1, '2026-10-26T00:00:00Z')",
        (kind, target_id),
    )


def _topic(conn, topic):
    conn.execute(
        "INSERT INTO tracked_topics (profile_id, topic, created_at)"
        " VALUES ('default', ?, '2026-10-26T00:00:00Z')",
        (topic,),
    )


def _channel_of(conn, xmltv_id):
    return conn.execute(
        "SELECT f.channel_id FROM epg_channels ec JOIN epg_mappings m ON m.epg_channel_id = ec.id"
        " JOIN feeds f ON f.id = m.feed_id WHERE ec.xmltv_id = ?",
        (xmltv_id,),
    ).fetchone()[0]


def test_nothing_tracked_keeps_every_channel_in_the_catalog(conn, tracked, catalog, epg_source):
    report = import_xmltv(conn, tracked, epg_source, fixture_bytes("guide_a.xml"), now=NOW)
    # Россия 1 has no feed in the catalog; Euronews waits for a person to pick a region
    # but is in the catalog, so its schedule is kept for when they do.
    assert report.channels_skipped == 1
    xmltv_ids = {x for x, _ in _stored(conn)}
    assert xmltv_ids == {"Perviy.ru", "Euronews.fr", "vtv1.vn"}


def test_favourite_channel_topic_and_programme(conn, tracked, catalog, epg_source):
    guide = fixture_bytes("guide_a.xml")
    import_xmltv(conn, tracked, epg_source, guide, now=NOW)
    _favourite(conn, "channel", _channel_of(conn, "Perviy.ru"))
    _topic(conn, "business")
    euronews_now = conn.execute(
        "SELECT id FROM programmes WHERE title = 'Euronews Now'"
    ).fetchone()[0]  # fmt: skip
    _favourite(conn, "programme", euronews_now)

    report = import_xmltv(conn, tracked, epg_source, guide, force=True, now=NOW)
    stored = _stored(conn)
    # The favourite channel keeps its whole day; elsewhere only the topic and the
    # favourite programme; VTV1 matches neither and loses its upcoming programme.
    assert [t for x, t in stored if x == "Euronews.fr"] == ["Business Line", "Euronews Now"]
    assert len([t for x, t in stored if x == "Perviy.ru"]) == 5
    assert [t for x, t in stored if x == "vtv1.vn"] == []
    assert report.programmes_skipped >= 2


def test_topic_matches_categories_and_keeps_the_past(conn, tracked, catalog, epg_source):
    guide = fixture_bytes("guide_a.xml")
    import_xmltv(conn, tracked, epg_source, guide, now=NOW)
    _topic(conn, "новости")
    import_xmltv(conn, tracked, epg_source, guide, force=True, now=NOW)
    perviy = [t for x, t in _stored(conn) if x == "Perviy.ru"]
    # Before 07:00Z everything stays (it is history now); later only news is kept.
    assert sorted(perviy) == ["Доброе утро", "Жить здорово!", "Новости", "Новости"]


def test_favourite_programme_outlives_its_airings(conn, settings, epg_source):
    import_xmltv(conn, settings, epg_source, fixture_bytes("guide_a.xml"), now=NOW)
    pid = conn.execute("SELECT id FROM programmes WHERE title = 'Вести'").fetchone()[0]
    _favourite(conn, "programme", pid)
    later = NOW + timedelta(days=settings.epg_retention_days + 1)
    other = (b'<tv><programme start="20261115090000" stop="20261115100000" channel="Perviy.ru">'
             b"<title>Later</title></programme></tv>")  # fmt: skip
    import_xmltv(conn, settings, epg_source, other, force=True, now=later)
    assert conn.execute("SELECT 1 FROM programmes WHERE id = ?", (pid,)).fetchone()
    assert count(conn, "airings") == 1


def test_old_playlist_snapshots_and_files_are_dropped(conn, settings, make_source):
    source = make_source("a")
    import_m3u(conn, settings, source, fixture_bytes("provider_a.m3u"))
    old = conn.execute("SELECT id, blob_path FROM source_snapshots").fetchone()
    long_ago = (datetime.now(UTC) - timedelta(days=settings.history_days + 1)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    conn.execute("UPDATE source_snapshots SET fetched_at = ?", (long_ago,))
    # The only snapshot is the catalog's current one: it stays however old it is.
    import_m3u(conn, settings, source, fixture_bytes("provider_a.m3u"))
    assert conn.execute("SELECT 1 FROM source_snapshots WHERE id = ?", (old["id"],)).fetchone()

    report = import_m3u(conn, settings, source, fixture_bytes("provider_c.m3u"), force=True)
    assert report.status == "applied"
    assert not conn.execute("SELECT 1 FROM source_snapshots WHERE id = ?", (old["id"],)).fetchone()
    assert not conn.execute(
        "SELECT 1 FROM source_records WHERE snapshot_id = ?", (old["id"],)
    ).fetchone()  # fmt: skip
    assert not Path(old["blob_path"]).exists()
    assert count(conn, "source_records") > 0


def test_settings_from_env():
    from mih.config import ConfigError, Settings

    s = Settings.from_env({})
    assert (s.history_days, s.epg_retention_days, s.epg_scope) == (90, 3, "tracked")
    with pytest.raises(ConfigError):
        Settings.from_env({"MIH_EPG_SCOPE": "everything"})
