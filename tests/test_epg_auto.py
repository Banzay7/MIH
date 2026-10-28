"""Guide coverage of working channels and the guide autopilot (MIH_EPG_AUTO)."""

from datetime import UTC, datetime, timedelta

import pytest

from mih.epg.auto import RETRY_AFTER, run_autopilot
from mih.epg.coverage import working_coverage
from mih.epg.importer import import_xmltv
from mih.epg.service import create_epg_source
from mih.health.policy import format_ts
from mih.sources.importer import import_m3u
from tests.conftest import count

NOW = datetime.now(UTC).replace(microsecond=0)

PLAYLIST = """#EXTM3U
#EXTINF:-1 tvg-country="DE",Das Erste HD
https://a.example/de/1.m3u8
#EXTINF:-1 tvg-country="DE",ZDF
https://a.example/de/2.m3u8
#EXTINF:-1 tvg-country="DE",3sat
https://a.example/de/3.m3u8
#EXTINF:-1 tvg-language="Russian",Первый канал
https://a.example/ru/1.m3u8
#EXTINF:-1 tvg-country="DE",KiKA
https://a.example/de/4.m3u8
"""


def _guide(channels: dict[str, int]) -> bytes:
    """XMLTV with the given display names and that many programmes each."""
    start = NOW.replace(minute=0, second=0)
    out = ['<?xml version="1.0" encoding="UTF-8"?>', "<tv>"]
    for i, name in enumerate(channels):
        out.append(f'<channel id="c{i}"><display-name>{name}</display-name></channel>')
    for i, n in enumerate(channels.values()):
        for k in range(n):
            a = (start + timedelta(hours=k)).strftime("%Y%m%d%H%M%S +0000")
            b = (start + timedelta(hours=k + 1)).strftime("%Y%m%d%H%M%S +0000")
            out.append(f'<programme start="{a}" stop="{b}" channel="c{i}">'
                       f"<title>Show {k}</title></programme>")  # fmt: skip
    out.append("</tv>")
    return "\n".join(out).encode()


@pytest.fixture
def working(conn, settings, make_source):
    import_m3u(conn, settings, make_source("my"), PLAYLIST.encode())
    conn.execute("UPDATE streams SET status = 'active', last_ok_at = ?", (format_ts(NOW),))
    # KiKA does not play: it is not a working channel.
    conn.execute(
        "UPDATE streams SET status = 'quarantine' WHERE id IN (SELECT s.id FROM streams s"
        " JOIN feeds f ON f.id = s.feed_id JOIN channels c ON c.id = f.channel_id"
        " WHERE c.canonical_name = 'KiKA')"
    )


def _auto_source(conn):
    return conn.execute("SELECT * FROM epg_sources WHERE auto = 1").fetchone()


def test_coverage_counts_working_channels_by_country(conn, settings, working):
    cov = working_coverage(conn).to_dict()
    assert (cov["working"], cov["with_guide"]) == (4, 0)
    # The channel without a country tag counts under the country of its language.
    assert cov["by_country"] == [
        {"country": "DE", "working": 3, "with_guide": 0,
         "missing": ["3sat", "Das Erste HD", "ZDF"]},
        {"country": "RU", "working": 1, "with_guide": 0, "missing": ["Первый канал"]},
    ]  # fmt: skip


def test_a_guide_channel_without_programmes_gives_no_guide(conn, settings, working):
    src = create_epg_source(conn, settings, name="guide", url=None)
    import_xmltv(conn, settings, src, _guide({"ZDF": 3, "3sat": 0}))
    cov = working_coverage(conn)
    assert cov.with_guide == 1
    assert cov.by_country["DE"].missing == ["3sat", "Das Erste HD"]


def test_autopilot_tries_a_guide_keeps_it_when_it_helps(conn, settings, working):
    report = run_autopilot(conn, settings, working_coverage(conn), NOW)
    # Only DE has enough channels without a guide; one guide per country on trial.
    assert report.added == ["epgshare01 DE1 (DE)"]
    src = _auto_source(conn)
    assert src["url"].endswith("epg_ripper_DE1.xml.gz") and src["priority"] == 120

    # Not fetched yet: nothing to judge, nothing new for DE.
    again = run_autopilot(conn, settings, working_coverage(conn), NOW)
    assert not again.changed

    channels = count(conn, "channels")
    import_xmltv(conn, settings, src["id"], _guide({"Das Erste": 4, "ZDF": 4, "Arte": 4}))
    # A guide on trial only finds guides for channels the catalog has.
    assert count(conn, "channels") == channels

    cov = working_coverage(conn)
    assert cov.with_guide == 2
    report = run_autopilot(conn, settings, cov, NOW)
    assert report.kept == ["epgshare01-de1 (DE) +2"]
    assert report.added == []  # 1 DE channel left without a guide: below the minimum
    row = conn.execute("SELECT verdict, gain, epg_source_id FROM epg_auto").fetchone()
    assert (row["verdict"], row["gain"], row["epg_source_id"]) == ("kept", 2, src["id"])


def test_autopilot_drops_a_useless_guide_and_tries_the_next(conn, settings, working):
    run_autopilot(conn, settings, working_coverage(conn), NOW)
    src = _auto_source(conn)
    import_xmltv(conn, settings, src["id"], _guide({"Arte": 4}))

    report = run_autopilot(conn, settings, working_coverage(conn), NOW)
    assert report.dropped == ["epgshare01-de1 (DE)"]
    assert report.added == ["epg.pw DE (DE)"]
    assert conn.execute("SELECT 1 FROM epg_sources WHERE id = ?", (src["id"],)).fetchone() is None
    assert count(conn, "epg_channels") == 0

    # A rejected file is broken; with no candidates left nothing else is tried for DE.
    src = _auto_source(conn)
    import_xmltv(conn, settings, src["id"], b"<html>moved</html>")
    report = run_autopilot(conn, settings, working_coverage(conn), NOW)
    assert report.dropped == ["epg-pw-de (DE)"] and report.added == []
    verdicts = dict(conn.execute("SELECT guide, verdict FROM epg_auto").fetchall())
    assert verdicts == {"epgshare01-de1": "useless", "epg-pw-de": "broken"}

    # A month later the dropped guides get another chance.
    later = run_autopilot(conn, settings, working_coverage(conn), NOW + RETRY_AFTER
                          + timedelta(days=1))  # fmt: skip
    assert later.added == ["epgshare01 DE1 (DE)"]


def test_autopilot_drops_a_guide_that_never_arrives(conn, settings, working):
    run_autopilot(conn, settings, working_coverage(conn), NOW)
    report = run_autopilot(conn, settings, working_coverage(conn), NOW + timedelta(days=3))
    assert report.dropped == ["epgshare01-de1 (DE)"]


def test_guide_job_reports_coverage_and_comes_back_for_new_guides(conn, settings, working):
    from dataclasses import replace

    from mih.scheduler import run_job

    auto = replace(settings, epg_auto=True)
    event = run_job(conn, auto, "epg")
    assert event["data"]["coverage"] == {"working": 4, "with_guide": 0}
    assert event["data"]["autopilot"]["added"] == ["epgshare01 DE1 (DE)"]
    nxt = conn.execute("SELECT next_run_at FROM jobs WHERE name = 'epg'").fetchone()[0]
    assert datetime.fromisoformat(nxt.replace("Z", "+00:00")) < datetime.now(UTC) + timedelta(
        minutes=10
    )
    # Off: the guide refresh does not look for guides.
    event = run_job(conn, settings, "epg")
    assert "coverage" not in event["data"]


def test_temporary_files_go_next_to_the_database(conn, settings):
    tmp = conn.execute("PRAGMA temp_store_directory").fetchone()[0]
    assert tmp == str(settings.db_path.parent / "tmp")


def test_a_guide_failing_in_the_database_does_not_stop_the_others(conn, settings, monkeypatch):
    import sqlite3

    from mih import scheduler

    for name in ("a", "b"):
        conn.execute(
            "INSERT INTO epg_sources (id, name, url, created_at) VALUES (?, ?, ?, ?)",
            (name, name, f"https://{name}.example/guide.xml", format_ts(NOW)),
        )
    seen = []

    def importer(conn, settings, source_id):
        seen.append(source_id)
        if source_id == "a":
            raise sqlite3.OperationalError("database or disk is full")

    monkeypatch.setattr(scheduler, "import_epg_from_url", importer)
    event = scheduler.run_job(conn, settings, "epg")
    assert seen == ["a", "b"]
    assert event["data"]["problems"] == 1 and event["data"]["ok"] == 1
