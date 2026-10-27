"""Command line: run the server, import a playlist or guide file, print catalog stats."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from mih import db, notify
from mih.config import ConfigError, Settings
from mih.directory import (
    DEFAULT_BASE_URL,
    FILES,
    DirectoryError,
    fetch_directory,
    import_directory,
)
from mih.discovery.scout import summary
from mih.epg.importer import import_xmltv
from mih.epg.service import create_epg_source
from mih.events import record_event, t
from mih.export import export_catalog
from mih.health.policy import HealthPolicy
from mih.health.runner import run_checks
from mih.lists import ListFilter, build_presets, load_presets, write_list
from mih.scheduler import JOBS, JobBusy, run_job, status_snapshot
from mih.search.engine import SearchRequest, search
from mih.search.index import ensure_index
from mih.sources.fetch import FetchError
from mih.sources.importer import import_m3u
from mih.sources.service import SourceError, create_source


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mih", description="MIRTVONLINE core")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve", help="run the HTTP API")
    sub.add_parser("migrate", help="create or upgrade the database")
    imp = sub.add_parser("import-file", help="import a local M3U file into a named source")
    imp.add_argument("path", type=Path)
    imp.add_argument("--source", required=True, help="source name; created if missing")
    imp.add_argument("--force", action="store_true", help="skip the size-drop safety check")
    epg = sub.add_parser("import-epg-file", help="import a local XMLTV file (.xml or .xml.gz)")
    epg.add_argument("path", type=Path)
    epg.add_argument("--source", required=True, help="EPG source name; created if missing")
    epg.add_argument("--force", action="store_true", help="skip the size-drop safety check")
    dr = sub.add_parser(
        "import-directory", help="import the iptv-org channel directory (no streams)"
    )
    dr.add_argument("--from", dest="src", type=Path,
                    help="folder with channels/feeds/logos/blocklist .csv or .json")  # fmt: skip
    dr.add_argument("--base-url", default=DEFAULT_BASE_URL, help="where to download from")
    dr.add_argument("--country", action="append", default=[],
                    help="add channels of this country the catalog lacks; repeatable")  # fmt: skip
    hc = sub.add_parser("health-check", help="probe streams that are due for a check")
    hc.add_argument(
        "--limit", type=int, help="at most this many streams (default MIH_HEALTH_BATCH)"
    )
    hc.add_argument("--stream", action="append", help="check this stream id now; repeatable")
    find = sub.add_parser("search", help='search programmes and channels: "роботы завтра"')
    find.add_argument("query")
    find.add_argument("--tz", help="IANA time zone for today/tomorrow (default MIH_TIMEZONE)")
    find.add_argument("--limit", type=int, default=10)
    exp = sub.add_parser("export", help="write channels with categories to a folder")
    exp.add_argument("--out", type=Path, help="folder (default MIH_EXPORT_DIR)")
    exp.add_argument(
        "--no-playlist",
        action="store_true",
        help="skip playlist.m3u, the only file with stream URLs",
    )
    lst = sub.add_parser("list", help="write a list of channels that play now, filtered by need")
    lst.add_argument("--name", default="valid", help="file name without extension")
    lst.add_argument("--out", type=Path, help="folder (default MIH_EXPORT_DIR)")
    lst.add_argument("--country", action="append", default=[], help="RU, VN...; repeatable")
    lst.add_argument("--lang", action="append", default=[], help="ru, en...; repeatable")
    lst.add_argument("--category", action="append", default=[], help="news, kids...; repeatable")
    lst.add_argument(
        "--level",
        choices=("ok", "doubtful", "unknown"),
        default="ok",
        help="ok: played within 48 h (default); doubtful: within 7 days; unknown: not checked",
    )
    lst.add_argument("--timeshift", action="store_true", help="include +N hour copies")
    lst.add_argument("--adult", action="store_true", help="include adult channels")
    lst.add_argument("--epg-only", action="store_true", help="only channels with a guide")
    lst.add_argument("--epg-url", help="guide URL for the playlist header (url-tvg)")
    lst.add_argument("--no-playlist", action="store_true", help="skip <name>.m3u (stream URLs)")
    lst.add_argument("--presets", action="store_true",
                     help="build every list of MIH_LISTS_FILE into <export>/lists")  # fmt: skip
    sub.add_parser("stats", help="print catalog counts")
    sub.add_parser("status", help="what the service is doing, recent events, the plan")
    job = sub.add_parser("run-job", help="run a background job now; the result is pushed")
    job.add_argument("name", choices=JOBS)
    sub.add_parser("notify-test", help="send a test push to MIH_NTFY_URL")
    dsc = sub.add_parser("discovery", help="what the playlist scout found and packed")
    dsc.add_argument("--status", help="list the playlists in this state (live, packed, dead...)")
    dsc.add_argument("--limit", type=int, default=50)
    args = parser.parse_args(argv)

    settings = Settings.from_env()
    if args.cmd == "serve":
        return _serve(settings)

    conn = db.connect(settings.db_path)
    db.migrate(conn)
    ensure_index(conn)
    if args.cmd == "migrate":
        print("database is up to date")
    elif args.cmd == "import-file":
        row = conn.execute("SELECT id FROM sources WHERE name = ?", (args.source,)).fetchone()
        try:
            source_id = (
                row["id"] if row else create_source(conn, settings, name=args.source, url=None)
            )
        except SourceError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        report = import_m3u(conn, settings, source_id, args.path.read_bytes(), force=args.force)
        _import_event(conn, settings, args.source, report)
        print(json.dumps(report.stats(), ensure_ascii=False, indent=2))
        return 0 if report.status != "rejected" else 2
    elif args.cmd == "import-epg-file":
        row = conn.execute("SELECT id FROM epg_sources WHERE name = ?", (args.source,)).fetchone()
        try:
            source_id = (
                row["id"] if row else create_epg_source(conn, settings, name=args.source, url=None)
            )
        except SourceError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        report = import_xmltv(conn, settings, source_id, args.path.read_bytes(), force=args.force)
        _import_event(conn, settings, args.source, report)
        print(json.dumps(report.stats(), ensure_ascii=False, indent=2))
        return 0 if report.status != "rejected" else 2
    elif args.cmd == "import-directory":
        try:
            files = _local_directory(args.src) if args.src else fetch_directory(
                settings, args.base_url)  # fmt: skip
            report = import_directory(conn, files, countries=tuple(args.country))
        except (DirectoryError, FetchError, OSError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(json.dumps(report.stats(), indent=2))
    elif args.cmd == "health-check":
        report = run_checks(conn, settings, HealthPolicy.from_env(), stream_ids=args.stream,
                            limit=args.limit)  # fmt: skip
        print(json.dumps(report.stats(), ensure_ascii=False, indent=2))
        return 0 if report.status == "done" else 3
    elif args.cmd == "search":
        req = SearchRequest(query=args.query, tz=args.tz or settings.timezone, limit=args.limit)
        print(json.dumps(search(conn, req, datetime.now(UTC)), ensure_ascii=False, indent=2))
    elif args.cmd == "export":
        result = export_catalog(conn, args.out or settings.export_dir,
                                with_playlist=not args.no_playlist)  # fmt: skip
        stats = result.stats()
        title = t(settings, "export", channels=stats["channels"])
        record_event(conn, settings, "export", "info", title, data=stats)
        print(json.dumps(stats, ensure_ascii=False, indent=2))
    elif args.cmd == "list" and args.presets:
        try:
            built = build_presets(conn, args.out or settings.lists_dir,
                                  load_presets(settings.lists_file))  # fmt: skip
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(json.dumps([r.stats() for r in built], ensure_ascii=False, indent=2))
    elif args.cmd == "list":
        flt = ListFilter(countries=tuple(args.country), languages=tuple(args.lang),
                         categories=tuple(args.category),
                         exclude_categories=() if args.adult else ("adult",),
                         min_level=args.level, timeshift=args.timeshift,
                         epg_only=args.epg_only)  # fmt: skip
        try:
            result = write_list(conn, args.out or settings.export_dir, args.name, flt,
                                epg_url=args.epg_url,
                                with_playlist=not args.no_playlist)  # fmt: skip
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(json.dumps(result.stats(), ensure_ascii=False, indent=2))
    elif args.cmd == "stats":
        counts = {
            t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in ("sources", "channels", "feeds", "streams", "epg_sources", "programmes",
                      "airings")
        }  # fmt: skip
        counts["needs_review"] = conn.execute(
            "SELECT COUNT(*) FROM match_decisions WHERE decision = 'needs_review'"
        ).fetchone()[0]
        print(json.dumps(counts, indent=2))
    elif args.cmd == "status":
        print(json.dumps(status_snapshot(conn, settings), ensure_ascii=False, indent=2))
    elif args.cmd == "run-job":
        try:
            event = run_job(conn, settings, args.name, manual=True)
        except JobBusy:
            print(f"error: job {args.name} is already running", file=sys.stderr)
            return 4
        print(event["title"])
        if event["body"]:
            print(event["body"])
        return 0 if event["data"]["status"] != "error" else 3
    elif args.cmd == "discovery" and args.status:
        rows = conn.execute(
            "SELECT url, origin, reason, entries, new_entries, sampled, sample_ok, checked,"
            " packed_ok, scouted_at FROM discovered_playlists WHERE status = ?"
            " ORDER BY scouted_at DESC LIMIT ?",
            (args.status, args.limit),
        ).fetchall()
        print(json.dumps([dict(r) for r in rows], ensure_ascii=False, indent=2))
    elif args.cmd == "discovery":
        print(json.dumps(summary(conn), ensure_ascii=False, indent=2))
    elif args.cmd == "notify-test":
        if not settings.ntfy_url:
            print("error: MIH_NTFY_URL is not set", file=sys.stderr)
            return 1
        result = notify.send(settings, t(settings, "notify.test"),
                             t(settings, "notify.test.body"))  # fmt: skip
        print(result)
        return 0 if result == "sent" else 5
    return 0


def _local_directory(folder: Path) -> dict[str, bytes]:
    out = {}
    for name in FILES:
        for ext in (".csv", ".json"):
            if (folder / f"{name}{ext}").exists():
                out[name] = (folder / f"{name}{ext}").read_bytes()
                break
    return out


def _import_event(conn, settings: Settings, source: str, report) -> None:
    if report.status == "rejected":
        status, level = t(settings, "rejected", reason=report.reason), "warning"
    else:
        status, level = t(settings, report.status), "info"
    record_event(conn, settings, "import", level, t(settings, "import", source=source,
                 status=status), data=report.stats())  # fmt: skip


def _serve(settings: Settings) -> int:
    try:
        settings.validate_for_serving()
    except ConfigError as exc:
        print(f"refusing to start: {exc}", file=sys.stderr)
        return 1
    import uvicorn

    from mih.api.app import create_app

    uvicorn.run(create_app(settings), host=settings.bind_host, port=settings.bind_port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
