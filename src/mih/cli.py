"""Command line: run the server, import a playlist file, print catalog stats."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from mih import db
from mih.config import ConfigError, Settings
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
    sub.add_parser("stats", help="print catalog counts")
    args = parser.parse_args(argv)

    settings = Settings.from_env()
    if args.cmd == "serve":
        return _serve(settings)

    conn = db.connect(settings.db_path)
    db.migrate(conn)
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
        print(json.dumps(report.stats(), ensure_ascii=False, indent=2))
        return 0 if report.status != "rejected" else 2
    elif args.cmd == "stats":
        counts = {
            t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in ("sources", "channels", "feeds", "streams")
        }
        counts["needs_review"] = conn.execute(
            "SELECT COUNT(*) FROM match_decisions WHERE decision = 'needs_review'"
        ).fetchone()[0]
        print(json.dumps(counts, indent=2))
    return 0


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
