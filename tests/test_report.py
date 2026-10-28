"""The coverage report of the web UI's «Покрытие» tab (mih.report)."""

import json
import os
from datetime import timedelta

from fastapi.testclient import TestClient

from mih import scheduler
from mih.api.app import create_app
from mih.epg.importer import import_xmltv
from mih.epg.service import create_epg_source
from mih.report import build_report, read_report, report_path, write_report
from tests.test_epg_auto import NOW, _guide, working  # noqa: F401  (fixture)


def test_report_counts_the_catalog_funnel_by_country(conn, settings, working):  # noqa: F811
    src = create_epg_source(conn, settings, name="guide", url=None)
    import_xmltv(conn, settings, src, _guide({"ZDF": 3, "KiKA": 3}))
    r = build_report(conn, now=NOW)
    # KiKA has a guide but does not play: it is not counted as working with a guide.
    assert r["funnel"] == {"all": 5, "streams": 5, "working": 4, "guide": 1}
    de = next(c for c in r["by_country"] if c["country"] == "DE")
    assert de == {"country": "DE", "all": 4, "streams": 4, "working": 3, "guide": 1}
    assert sum(r["not_working"].values()) == 1
    assert r["sources"] == [{"name": "my", "auto": False, "streams": 5, "working": 4,
                             "channels": 5}]  # fmt: skip
    assert r["new_streams"][0]["streams"] == 5 and r["guides"]["sources"] == 1
    assert "url" not in json.dumps(r["sources"])


def test_report_file_is_throttled_and_keeps_one_point_a_day(conn, settings, working):  # noqa: F811
    path = report_path(settings.data_dir)
    first = write_report(conn, path, now=NOW)
    assert first and [p["day"] for p in first["history"]] == [NOW.strftime("%Y-%m-%d")]
    # Fresh enough: not rebuilt.
    assert write_report(conn, path, now=NOW) is None
    # A later run on the same day replaces that day's point; a new day adds one.
    later = NOW + timedelta(days=1)
    os.utime(path, (0, 0))
    second = write_report(conn, path, now=later)
    assert len(second["history"]) == 2
    os.utime(path, (0, 0))
    third = write_report(conn, path, now=later)
    assert len(third["history"]) == 2 and read_report(path)["funnel"]["working"] == 4


def test_jobs_refresh_the_report_after_their_work(conn, settings, working):  # noqa: F811
    path = report_path(settings.data_dir)
    scheduler._rebuild_lists(conn, settings, {})
    assert read_report(path)["funnel"]["all"] == 5


def test_report_api_builds_the_file_when_there_is_none(settings, conn, working):  # noqa: F811
    with TestClient(create_app(settings)) as c:
        r = c.get("/api/v1/report")
        assert r.status_code == 200 and r.json()["funnel"]["working"] == 4
        assert c.get("/api/v1/epg/coverage").json()["working"] == 4
    assert report_path(settings.data_dir).exists()


def test_report_command_prints_the_funnel(settings, conn, working, capsys, monkeypatch):  # noqa: F811
    from mih import cli

    monkeypatch.setenv("MIH_DATA_DIR", str(settings.data_dir))
    assert cli.main(["report"]) in (0, None)
    assert json.loads(capsys.readouterr().out)["funnel"]["all"] == 5
