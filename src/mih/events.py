"""The event feed: what the service did, written to the database and pushed when it matters.

Levels: routine (feed and digest only), info, warning, error. Everything at or above
MIH_NOTIFY_LEVEL is pushed, except routine, which never is.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta

import httpx

from mih import notify
from mih.config import LEVELS, Settings
from mih.ids import new_id, utc_now

# A job that keeps reporting the same thing is pushed once in this window.
REPEAT_WINDOW = timedelta(hours=6)


def record_event(
    conn: sqlite3.Connection,
    settings: Settings,
    kind: str,
    level: str,
    title: str,
    body: str = "",
    data: dict | None = None,
    *,
    transport: httpx.BaseTransport | None = None,
    requested: bool = False,
) -> dict:
    """Store one event, push it if its level is high enough, and return it. ``requested``:
    someone asked for this run, so a repeat of an earlier push is still sent."""
    if level not in LEVELS:
        raise ValueError(f"unknown level {level!r}")
    event = {"id": new_id(), "at": utc_now(), "kind": kind, "level": level, "title": title,
             "body": body, "data": data, "pushed": None}  # fmt: skip
    conn.execute(
        "INSERT INTO events (id, at, kind, level, title, body, data) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (event["id"], event["at"], kind, level, title, body,
         json.dumps(data, ensure_ascii=False) if data else None),
    )  # fmt: skip
    if level != "routine" and LEVELS.index(level) >= LEVELS.index(settings.notify_level):
        if not requested and _pushed_recently(conn, kind, title, event["id"]):
            event["pushed"] = "repeat"  # the same problem every 30 minutes is one push
        else:
            event["pushed"] = notify.send(settings, title, body, level, transport=transport)
        conn.execute("UPDATE events SET pushed = ? WHERE id = ?", (event["pushed"], event["id"]))
    cutoff = (datetime.now(UTC) - timedelta(days=settings.history_days)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    conn.execute("DELETE FROM events WHERE at < ?", (cutoff,))
    return event


def _pushed_recently(conn: sqlite3.Connection, kind: str, title: str, own_id: str) -> bool:
    if not kind.startswith("job.") or kind == "job.digest":
        return False
    since = (datetime.now(UTC) - REPEAT_WINDOW).strftime("%Y-%m-%dT%H:%M:%SZ")
    row = conn.execute(
        "SELECT 1 FROM events WHERE kind = ? AND title = ? AND id != ? AND at >= ?"
        " AND pushed IN ('sent', 'repeat') LIMIT 1",
        (kind, title, own_id, since),
    ).fetchone()
    return row is not None


def list_events(
    conn: sqlite3.Connection, *, limit: int = 50, before: str | None = None,
    min_level: str = "routine",
) -> list[dict]:  # fmt: skip
    levels = LEVELS[LEVELS.index(min_level) :]
    rows = conn.execute(
        f"SELECT * FROM events WHERE level IN ({','.join('?' * len(levels))})"
        " AND (? IS NULL OR at < ?) ORDER BY at DESC, rowid DESC LIMIT ?",
        (*levels, before, before, limit),
    ).fetchall()
    return [{**dict(r), "data": json.loads(r["data"]) if r["data"] else None} for r in rows]


# Texts of pushes and feed entries. Values of {placeholders} come from the job.
TEXTS = {
    "service.started": {
        "en": "MIRTVONLINE started (v{version})",
        "ru": "MIRTVONLINE запущен (v{version})",
    },
    "service.stopped": {"en": "MIRTVONLINE stopped", "ru": "MIRTVONLINE остановлен"},
    "job.interrupted": {
        "en": "Job «{job}» was interrupted by a restart",
        "ru": "Задача «{job}» прервана перезапуском",
    },
    "job.failed": {"en": "Job «{job}» failed", "ru": "Задача «{job}» упала с ошибкой"},
    "job.sources": {
        "en": "Playlists: {ok} of {total} updated",
        "ru": "Плейлисты: обновлено {ok} из {total}",
    },
    "job.sources.none": {
        "en": "Playlists: no sources with a URL",
        "ru": "Плейлисты: нет источников с адресом",
    },
    "job.epg": {
        "en": "TV guide: {ok} of {total} updated",
        "ru": "Телепрограмма: обновлено {ok} из {total}",
    },
    "job.epg.none": {
        "en": "TV guide: no sources with a URL",
        "ru": "Телепрограмма: нет источников с адресом",
    },
    "job.health": {
        "en": "Stream check: {ok} of {checked} work",
        "ru": "Проверка потоков: работают {ok} из {checked}",
    },
    "job.health.idle": {
        "en": "Stream check: nothing due",
        "ru": "Проверка потоков: проверять нечего",
    },
    "job.health.offline": {
        "en": "Stream check skipped: the server has no internet",
        "ru": "Проверка потоков пропущена: у сервера нет интернета",
    },
    "job.digest": {"en": "MIRTVONLINE: daily summary", "ru": "MIRTVONLINE: сводка за сутки"},
    "export": {
        "en": "Export: {channels} channels written",
        "ru": "Экспорт: выгружено каналов {channels}",
    },
    "import": {"en": "Import «{source}»: {status}", "ru": "Импорт «{source}»: {status}"},
    "notify.test": {"en": "Test push from MIRTVONLINE", "ru": "Тестовый пуш от MIRTVONLINE"},
    "notify.test.body": {"en": "Notifications work.", "ru": "Уведомления работают."},
    # Line fragments.
    "unchanged": {"en": "unchanged", "ru": "без изменений"},
    "applied": {"en": "updated", "ru": "обновлён"},
    "rejected": {
        "en": "rejected ({reason}), the previous version stays",
        "ru": "отклонён ({reason}), осталась прежняя версия",
    },
    "error": {"en": "error: {error}", "ru": "ошибка: {error}"},
    "src.changes": {
        "en": "+{channels} channels, +{streams} streams, {absent} gone",
        "ru": "+{channels} каналов, +{streams} потоков, пропало {absent}",
    },
    "epg.changes": {
        "en": "{programmes} programmes, +{created} airings, {changed} changed",
        "ru": "передач {programmes}, эфиров +{created}, изменено {changed}",
    },
    "health.failed": {"en": "not responding: {failed}", "ru": "не отвечают: {failed}"},
    "health.down": {"en": "became unavailable: {n}", "ru": "стали недоступны: {n}"},
    "health.up": {"en": "came back: {n}", "ru": "снова работают: {n}"},
    "health.lists": {"en": "lists: {lists}", "ru": "списки: {lists}"},
    "digest.catalog": {
        "en": "Catalog: {channels} channels, streams working {active} of {streams}",
        "ru": "Каталог: каналов {channels}, потоков работает {active} из {streams}",
    },
    "digest.day": {
        "en": "Last 24 h: {runs} job runs, problems {problems}",
        "ru": "За сутки: запусков задач {runs}, проблем {problems}",
    },
    "digest.health": {
        "en": "Streams checked: {checked}, not responding {failed}",
        "ru": "Проверено потоков: {checked}, не отвечают {failed}",
    },
    "digest.problems": {"en": "Problems:", "ru": "Проблемы:"},
    "digest.plan": {"en": "Planned:", "ru": "Запланировано:"},
    "every": {"en": "every {n} min", "ru": "каждые {n} мин"},
    "digest.scheduler_off": {
        "en": "Nothing planned: the scheduler is off",
        "ru": "Ничего не запланировано: планировщик выключен",
    },
    "digest.noplan": {
        "en": "Nothing planned in the next 24 hours",
        "ru": "На ближайшие сутки ничего не запланировано",
    },
    # Job names.
    "name.sources": {"en": "playlist refresh", "ru": "обновление плейлистов"},
    "name.epg": {"en": "TV guide refresh", "ru": "обновление телепрограммы"},
    "name.health": {"en": "stream check", "ru": "проверка потоков"},
    "name.digest": {"en": "daily summary", "ru": "сводка за сутки"},
}


def t(settings: Settings, key: str, **values) -> str:
    text = TEXTS[key]
    return text.get(settings.lang, text["en"]).format(**values)
