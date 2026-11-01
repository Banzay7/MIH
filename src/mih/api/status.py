"""Status screen, event feed, running a job on request and a test push (spec 22).

Mounted by mih.api.app.create_app, which also starts the scheduler with the app.
"""

from __future__ import annotations

import threading
from typing import Annotated

from fastapi import APIRouter, Path, Query

from mih import db, notify
from mih.api.app import ApiError, Conn
from mih.config import LANGS, LEVELS, Settings
from mih.events import list_events, record_event, t
from mih.health.policy import HealthPolicy
from mih.scheduler import JOBS, JobBusy, job_process, run_job, status_snapshot

UiLang = Annotated[str | None, Query(pattern=f"^({'|'.join(LANGS)})$")]
JobName = Annotated[str, Path(pattern=f"^({'|'.join(JOBS)})$")]


def build_router(settings: Settings, policy: HealthPolicy, auth: list,
                 started_at: str) -> APIRouter:  # fmt: skip
    r = APIRouter(prefix="/api/v1", dependencies=auth)

    @r.get("/status")
    def status(conn: Conn, lang: UiLang = None) -> dict:
        """What the service is doing now, the latest events and the plan for 24 hours.
        ``lang``: summaries and events in the web UI's language."""
        return status_snapshot(conn, settings, started_at=started_at, lang=lang)

    @r.get("/events")
    def events(
        conn: Conn,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        before: Annotated[str | None, Query(max_length=32)] = None,
        min_level: Annotated[str, Query(pattern=f"^({'|'.join(LEVELS)})$")] = "routine",
        lang: UiLang = None,
    ) -> dict:
        return {"items": list_events(conn, limit=limit, before=before, min_level=min_level,
                                     lang=lang)}  # fmt: skip

    @r.post("/jobs/{name}/run", status_code=202)
    def run_now(name: JobName, conn: Conn) -> dict:
        """Start a job in the background; its result arrives in the feed (and as a push)."""
        running = conn.execute("SELECT running_since FROM jobs WHERE name = ?", (name,)).fetchone()
        if running and running["running_since"]:
            raise ApiError("conflict", f"job {name} is already running")

        if settings.scheduler_mode == "process":
            # Its own Python, like the scheduler's, so the web UI keeps its pace.
            proc = job_process("run-job", name)
            threading.Thread(target=proc.wait, name=f"mih-job-{name}", daemon=True).start()
            return {"job": name, "accepted": True}

        def work() -> None:
            c = db.connect(settings.db_path)
            try:
                run_job(c, settings, name, policy=policy, manual=True)
            except JobBusy:
                pass
            finally:
                c.close()

        threading.Thread(target=work, name=f"mih-job-{name}", daemon=True).start()
        return {"job": name, "accepted": True}

    @r.post("/notifications/test")
    def test_push(conn: Conn) -> dict:
        if not settings.ntfy_url:
            return {"result": "off"}
        title, body = t(settings, "notify.test"), t(settings, "notify.test.body")
        result = notify.send(settings, title, body, "info")
        record_event(conn, settings, "notify.test", "routine", title, body, {"result": result})
        return {"result": result}

    return r
