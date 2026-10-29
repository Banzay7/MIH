"""The event feed: what the service did, written to the database and pushed when it matters.

Levels: routine (feed and digest only), info, warning, error. Everything at or above
MIH_NOTIFY_LEVEL is pushed, except routine, which never is.
"""

from __future__ import annotations

import json
import sqlite3
from collections import OrderedDict
from datetime import UTC, datetime, timedelta

import httpx

from mih import notify
from mih.config import LANGS, LEVELS, Settings
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
    push: bool | None = None,
    click: str | None = None,
    action: str | None = None,
) -> dict:
    """Store one event, push it if its level is high enough, and return it. ``requested``:
    someone asked for this run, so a repeat of an earlier push is still sent. ``push``:
    True pushes whatever MIH_NOTIFY_LEVEL says (a topic alert was asked for)."""
    if level not in LEVELS:
        raise ValueError(f"unknown level {level!r}")
    event = {"id": new_id(), "at": utc_now(), "kind": kind, "level": level, "title": title,
             "body": body, "data": data, "pushed": None}  # fmt: skip
    # The web UI shows the feed in its own language: a copy in each is kept with the event.
    stored = data
    other = translations(settings, title=title, body=body)
    if other:
        stored = {**(data or {}), "i18n": other}
    conn.execute(
        "INSERT INTO events (id, at, kind, level, title, body, data) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (event["id"], event["at"], kind, level, title, body,
         json.dumps(stored, ensure_ascii=False) if stored else None),
    )  # fmt: skip
    if push is None:
        push = level != "routine" and LEVELS.index(level) >= LEVELS.index(settings.notify_level)
    if push:
        if not requested and _pushed_recently(conn, kind, title, event["id"]):
            event["pushed"] = "repeat"  # the same problem every 30 minutes is one push
        else:
            event["pushed"] = notify.send(settings, title, body, level, click=click,
                                          action=action, transport=transport)  # fmt: skip
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
    min_level: str = "routine", lang: str | None = None,
) -> list[dict]:  # fmt: skip
    """Latest first. ``lang``: titles and bodies in that language where a copy was kept."""
    levels = LEVELS[LEVELS.index(min_level) :]
    rows = conn.execute(
        f"SELECT * FROM events WHERE level IN ({','.join('?' * len(levels))})"
        " AND (? IS NULL OR at < ?) ORDER BY at DESC, rowid DESC LIMIT ?",
        (*levels, before, before, limit),
    ).fetchall()
    out = []
    for r in rows:
        data = json.loads(r["data"]) if r["data"] else None
        i18n = data.pop("i18n", None) if data else None
        title = in_lang(r["title"], i18n, lang, "title")
        body = in_lang(r["body"], i18n, lang, "body")
        out.append({**dict(r), "title": title, "body": body, "data": data or None})
    return out


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
    "job.alerts": {
        "en": "Topic alerts: {sent} sent",
        "ru": "Оповещения по темам: отправлено {sent}",
    },
    "job.alerts.none": {
        "en": "Topic alerts: no topics or favourite programmes yet",
        "ru": "Оповещения по темам: темы и любимые передачи не заданы",
    },
    "job.discover": {
        "en": "Playlist scout: {found} new found, {live} live",
        "ru": "Поисковик плейлистов: найдено новых {found}, живых {live}",
    },
    "job.discover.offline": {
        "en": "Playlist discovery skipped: the server has no internet",
        "ru": "Поиск плейлистов пропущен: у сервера нет интернета",
    },
    "job.pack": {
        "en": "Packer: +{channels} channels, +{streams} working streams",
        "ru": "Упаковщик: +{channels} каналов, +{streams} рабочих потоков",
    },
    "job.pack.progress": {
        "en": "Packer: {checked} streams checked, {remaining} to go",
        "ru": "Упаковщик: проверено потоков {checked}, осталось {remaining}",
    },
    "job.pack.idle": {
        "en": "Packer: no live playlists waiting",
        "ru": "Упаковщик: живых плейлистов в очереди нет",
    },
    "job.pack.capped": {
        "en": "Packer: paused, found playlists hold {n} streams (MIH_DISCOVERY_MAX_STREAMS)",
        "ru": "Упаковщик на паузе: в найденных плейлистах уже {n} потоков"
        " (MIH_DISCOVERY_MAX_STREAMS)",
    },
    "job.drive": {
        "en": "Google Drive: {channels} channels uploaded",
        "ru": "Google Диск: выложено каналов {channels}",
    },
    "job.drive.unchanged": {
        "en": "Google Drive: channels unchanged, nothing uploaded",
        "ru": "Google Диск: каналы не изменились, выгружать нечего",
    },
    "job.drive.failed": {
        "en": "Google Drive upload failed",
        "ru": "Не удалось выложить каналы на Google Диск",
    },
    "discover.found": {
        "en": "search: +{n} playlists ({repos} repositories, {pages} pages read)",
        "ru": "поиск: +{n} плейлистов (репозиториев {repos}, страниц {pages})",
    },
    "discover.scouted": {
        "en": "sampled {n}: {statuses}",
        "ru": "проверено выборочно {n}: {statuses}",
    },
    "discover.live": {
        "en": "• {url}: {ok} of {sampled} play, {new} new streams",
        "ru": "• {url}: работают {ok} из {sampled}, новых потоков {new}",
    },
    "digest.discovery": {
        "en": "Playlist discovery: {found} found, {waiting} waiting, {live} live;"
        " from them {channels} channels, {working} streams working",
        "ru": "Грабер: найдено плейлистов {found}, ждут проверки {waiting}, живых {live};"
        " из них каналов {channels}, рабочих потоков {working}",
    },
    "alert.title": {"en": "{time} · {title}", "ru": "{time} · {title}"},
    "alert.started": {"en": "On now · {title}", "ru": "Уже идёт · {title}"},
    "alert.topic": {"en": "topic «{topic}»", "ru": "тема «{topic}»"},
    "alert.favourite": {"en": "favourite programme", "ru": "передача из избранного"},
    "alert.watch": {"en": "Watch", "ru": "Смотреть"},
    "alert.more": {"en": "More on your topics: {n}", "ru": "Ещё по вашим темам: {n}"},
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
    "epg.coverage": {
        "en": "working channels with a guide: {with_guide} of {working}",
        "ru": "рабочих каналов с программой: {with_guide} из {working}",
    },
    "epg.auto.added": {
        "en": "guides on trial: {items}",
        "ru": "гиды на пробу: {items}",
    },
    "epg.auto.kept": {
        "en": "guides kept (+channels): {items}",
        "ru": "гиды оставлены (+каналов): {items}",
    },
    "epg.auto.dropped": {
        "en": "guides dropped (nothing new): {items}",
        "ru": "гиды убраны (ничего не дали): {items}",
    },
    "health.failed": {"en": "not responding: {failed}", "ru": "не отвечают: {failed}"},
    "health.down": {"en": "became unavailable: {n}", "ru": "стали недоступны: {n}"},
    "health.up": {"en": "came back: {n}", "ru": "снова работают: {n}"},
    "health.lists": {
        "en": "lists (channels, with a guide): {lists}",
        "ru": "списки (каналов, с программой): {lists}",
    },
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
    "digest.alerts": {
        "en": "On your topics in the next 24 h:",
        "ru": "По вашим темам в ближайшие сутки:",
    },
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
    "name.alerts": {"en": "topic alerts", "ru": "оповещения по темам"},
    "name.discover": {"en": "playlist scout", "ru": "поисковик плейлистов"},
    "name.pack": {"en": "playlist packer", "ru": "упаковщик плейлистов"},
    "name.drive": {"en": "Google Drive upload", "ru": "выгрузка на Google Диск"},
}


# Vietnamese, for the web UI's language (the server's own language is MIH_LANG).
_VI = {
    "service.started": "MIRTVONLINE đã khởi động (v{version})",
    "service.stopped": "MIRTVONLINE đã dừng",
    "job.interrupted": "Tác vụ «{job}» bị gián đoạn do khởi động lại",
    "job.failed": "Tác vụ «{job}» bị lỗi",
    "job.sources": "Danh sách phát: cập nhật {ok}/{total}",
    "job.sources.none": "Danh sách phát: không có nguồn nào có địa chỉ",
    "job.epg": "Lịch phát sóng: cập nhật {ok}/{total}",
    "job.epg.none": "Lịch phát sóng: không có nguồn nào có địa chỉ",
    "job.health": "Kiểm tra luồng: {ok}/{checked} hoạt động",
    "job.health.idle": "Kiểm tra luồng: không có gì cần kiểm tra",
    "job.health.offline": "Bỏ qua kiểm tra luồng: máy chủ không có internet",
    "job.digest": "MIRTVONLINE: tóm tắt trong ngày",
    "job.alerts": "Thông báo theo chủ đề: đã gửi {sent}",
    "job.alerts.none": "Thông báo theo chủ đề: chưa có chủ đề hay chương trình yêu thích",
    "job.discover": "Dò danh sách phát: tìm thấy {found} mới, {live} còn sống",
    "job.discover.offline": "Bỏ qua tìm danh sách phát: máy chủ không có internet",
    "job.pack": "Đóng gói: +{channels} kênh, +{streams} luồng hoạt động",
    "job.pack.progress": "Đóng gói: đã kiểm tra {checked} luồng, còn {remaining}",
    "job.pack.idle": "Đóng gói: không có danh sách phát sống nào đang chờ",
    "job.pack.capped": "Đóng gói tạm dừng: các danh sách tìm được đã có {n} luồng"
    " (MIH_DISCOVERY_MAX_STREAMS)",
    "job.drive": "Google Drive: đã tải lên {channels} kênh",
    "job.drive.unchanged": "Google Drive: danh sách kênh không đổi, không tải lên",
    "job.drive.failed": "Không tải được danh sách kênh lên Google Drive",
    "discover.found": "tìm kiếm: +{n} danh sách phát ({repos} kho, đã đọc {pages} trang)",
    "discover.scouted": "đã thử {n}: {statuses}",
    "discover.live": "• {url}: {ok}/{sampled} phát được, {new} luồng mới",
    "digest.discovery": "Dò danh sách phát: tìm thấy {found}, chờ {waiting}, sống {live};"
    " từ đó {channels} kênh, {working} luồng hoạt động",
    "alert.title": "{time} · {title}",
    "alert.started": "Đang phát · {title}",
    "alert.topic": "chủ đề «{topic}»",
    "alert.favourite": "chương trình yêu thích",
    "alert.watch": "Xem",
    "alert.more": "Thêm theo chủ đề của bạn: {n}",
    "export": "Xuất: đã ghi {channels} kênh",
    "import": "Nhập «{source}»: {status}",
    "notify.test": "Thông báo thử từ MIRTVONLINE",
    "notify.test.body": "Thông báo hoạt động.",
    "unchanged": "không đổi",
    "applied": "đã cập nhật",
    "rejected": "bị từ chối ({reason}), giữ phiên bản trước",
    "error": "lỗi: {error}",
    "src.changes": "+{channels} kênh, +{streams} luồng, mất {absent}",
    "epg.changes": "{programmes} chương trình, +{created} lượt phát, thay đổi {changed}",
    "epg.coverage": "kênh hoạt động có lịch phát sóng: {with_guide}/{working}",
    "epg.auto.added": "lịch đang thử: {items}",
    "epg.auto.kept": "lịch được giữ (+kênh): {items}",
    "epg.auto.dropped": "lịch bị bỏ (không thêm gì): {items}",
    "health.failed": "không phản hồi: {failed}",
    "health.down": "không còn truy cập được: {n}",
    "health.up": "hoạt động trở lại: {n}",
    "health.lists": "danh sách (kênh, có lịch phát sóng): {lists}",
    "digest.catalog": "Danh mục: {channels} kênh, luồng hoạt động {active}/{streams}",
    "digest.day": "24 giờ qua: {runs} lần chạy tác vụ, {problems} sự cố",
    "digest.health": "Đã kiểm tra luồng: {checked}, không phản hồi {failed}",
    "digest.problems": "Sự cố:",
    "digest.alerts": "Theo chủ đề của bạn trong 24 giờ tới:",
    "digest.plan": "Kế hoạch:",
    "every": "mỗi {n} phút",
    "digest.scheduler_off": "Không có kế hoạch: bộ lập lịch đang tắt",
    "digest.noplan": "Không có gì trong 24 giờ tới",
    "name.sources": "cập nhật danh sách phát",
    "name.epg": "cập nhật lịch phát sóng",
    "name.health": "kiểm tra luồng",
    "name.digest": "tóm tắt trong ngày",
    "name.alerts": "thông báo theo chủ đề",
    "name.discover": "dò danh sách phát",
    "name.pack": "đóng gói danh sách phát",
    "name.drive": "tải lên Google Drive",
}
for _key, _text in _VI.items():
    TEXTS[_key]["vi"] = _text

# A text t() made, in the server's language -> the same text in every language. Jobs build
# their summaries from these texts (lines, "name: text"), so record_event can store a copy
# of each event in every language for the web UI, which may be in another one.
_SAID: OrderedDict[str, dict[str, str]] = OrderedDict()
_SAID_MAX = 4096


def t(settings: Settings, key: str, **values) -> str:
    text = TEXTS[key]
    out = {}
    for lang in LANGS:
        # A value that is itself a text from t() (a job's name) goes in the same language.
        vals = {k: _SAID.get(v, {}).get(lang, v) if isinstance(v, str) else v
                for k, v in values.items()}  # fmt: skip
        out[lang] = text.get(lang, text["en"]).format(**vals)
    said = out[settings.lang]
    if any(v != said for v in out.values()):
        _SAID[said] = out
        _SAID.move_to_end(said)
        while len(_SAID) > _SAID_MAX:
            _SAID.popitem(last=False)
    return said


def localize(text: str, lang: str) -> str:
    """The text in another language, line by line. A line t() made is swapped whole; in a
    line built around such texts ("Source: unchanged") each of them is swapped."""
    lines = []
    for line in text.split("\n"):
        whole = _SAID.get(line)
        if whole is not None:
            lines.append(whole[lang])
            continue
        for said in sorted((k for k in _SAID if len(k) >= 4 and k in line), key=len, reverse=True):
            line = line.replace(said, _SAID[said][lang])
        lines.append(line)
    return "\n".join(lines)


def translations(settings: Settings, **texts: str) -> dict[str, dict[str, str]]:
    """{lang: {name: text}} for each other language the texts read differently in."""
    out = {}
    for lang in LANGS:
        if lang == settings.lang:
            continue
        other = {name: localize(text, lang) for name, text in texts.items()}
        if other != texts:
            out[lang] = other
    return out


def in_lang(text: str, i18n: dict | None, lang: str | None, name: str) -> str:
    """A stored text in the asked language, when a copy in it was kept."""
    return ((i18n or {}).get(lang) or {}).get(name, text) if lang else text
