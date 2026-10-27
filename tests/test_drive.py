import json
import subprocess
from dataclasses import replace

import pytest

from mih import scheduler
from mih.config import Settings
from mih.drive import DriveError, check_no_secrets, sync_drive, write_rclone_conf
from mih.scheduler import run_job
from mih.sources.importer import import_m3u
from tests.conftest import fixture_bytes

TOKEN = json.dumps({"access_token": "a1", "token_type": "Bearer", "refresh_token": "r1",
                    "expiry": "2026-10-27T10:00:00Z"})  # fmt: skip


@pytest.fixture
def drive(settings):
    return replace(settings, drive_token=TOKEN, drive_folder_id="FOLDER1")


class Rclone:
    def __init__(self, code=0):
        self.calls, self.code = [], code

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        return subprocess.CompletedProcess(cmd, self.code, "", "ERROR : quota exceeded\n")


def _load(conn, settings, make_source):
    import_m3u(conn, settings, make_source("a"), fixture_bytes("provider_a.m3u"))


def test_uploads_only_channels_json_and_csv_then_skips_unchanged(conn, drive, make_source):
    _load(conn, drive, make_source)
    rc = Rclone()
    report = sync_drive(conn, drive, run=rc)
    assert report.status == "uploaded" and report.channels > 0
    targets = [c[-1] for c in rc.calls]
    assert targets == ["mihdrive:channels.json", "mihdrive:channels.csv"]
    assert all(c[0] == "rclone" and "copyto" in c for c in rc.calls)
    assert not (drive.data_dir / "drive" / "out" / "playlist.m3u").exists()

    assert sync_drive(conn, drive, run=rc).status == "unchanged"
    assert len(rc.calls) == 2
    assert sync_drive(conn, drive, run=rc, force=True).status == "uploaded"
    assert len(rc.calls) == 4


def test_rclone_conf_is_private_and_keeps_a_refreshed_token(drive):
    path = write_rclone_conf(drive)
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    text = path.read_text()
    assert "root_folder_id = FOLDER1" in text and "scope = drive" in text
    # rclone refreshed the access token: the same refresh token must not overwrite it
    path.write_text(text.replace('"a1"', '"a2"'))
    write_rclone_conf(drive)
    assert '"a2"' in path.read_text()
    # a new token from the owner replaces it
    new = replace(drive, drive_token=TOKEN.replace("r1", "r2"))
    write_rclone_conf(new)
    assert '"r2"' in path.read_text() and '"a2"' not in path.read_text()


def test_bad_token_and_missing_settings_are_errors(conn, settings, drive):
    with pytest.raises(DriveError, match="not set"):
        sync_drive(conn, settings, run=Rclone())
    with pytest.raises(DriveError, match="rclone authorize"):
        write_rclone_conf(replace(drive, drive_token="not json"))


def test_a_stream_url_or_the_api_token_stops_the_upload(conn, drive, make_source):
    _load(conn, drive, make_source)
    url = conn.execute("SELECT url FROM stream_locators LIMIT 1").fetchone()[0]
    with pytest.raises(DriveError, match="stream URL"):
        check_no_secrets(conn, drive, {"channels.json": f'{{"x": "{url}"}}'})
    with pytest.raises(DriveError, match="stream URL"):
        check_no_secrets(conn, drive, {"channels.csv": "a,https://x.example/live?token=abc\n"})
    tok = replace(drive, api_token="t" * 30)
    with pytest.raises(DriveError, match="API token"):
        check_no_secrets(conn, tok, {"channels.csv": "t" * 30})
    check_no_secrets(conn, drive, {"channels.csv": "a,https://logo.example/a.png\n"})


def test_drive_job_reports_failures_and_is_off_without_a_token(conn, drive, make_source,
                                                               monkeypatch):  # fmt: skip
    _load(conn, drive, make_source)
    monkeypatch.setattr(scheduler, "sync_drive", lambda c, s: sync_drive(c, s, run=Rclone(1)))
    event = run_job(conn, drive, "drive")
    assert event["level"] == "error" and "quota exceeded" in event["body"]
    assert scheduler._every(drive, "drive") is not None
    assert scheduler._every(Settings(data_dir=drive.data_dir), "drive") is None
