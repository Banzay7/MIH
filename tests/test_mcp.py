"""MCP server: compact tools for an agent, over HTTP on its own port and over stdio."""

import io
import json

import pytest
from fastapi.testclient import TestClient

from mih import db
from mih.config import ConfigError, Settings
from mih.mcp.server import McpServer, create_mcp_app, run_stdio
from mih.mcp.tools import Toolbox
from tests.test_alerts import NOW, M, _follow, _guide, _load, _p

TOKEN = "m" * 32


@pytest.fixture
def settings(tmp_path):
    return Settings(data_dir=tmp_path, epg_scope="all", timezone="Europe/Moscow",
                    mcp_token=TOKEN)  # fmt: skip


@pytest.fixture
def conn(settings):
    c = db.connect(settings.db_path)
    db.migrate(c)
    _load(c, settings, _guide(
        _p("Perviy.ru", -10 * M, 40, "Новости"),
        _p("Perviy.ru", 30 * M, 90, "Футбол. Премьер-лига", sub="Зенит — Спартак"),
        _p("Perviy.ru", 26 * 60 * M, 60, "Футбол. Обзор"),
        _p("Euronews.fr", -5 * M, 30, "Euronews Live"),
    ))  # fmt: skip
    # Every stream played an hour ago: level ok.
    c.execute("UPDATE streams SET status = 'active', last_ok_at = ?",
              ((NOW - 60 * M).strftime("%Y-%m-%dT%H:%M:%SZ"),))  # fmt: skip
    yield c
    c.close()


def _ch(box, conn, q):
    return box.call(conn, "channels", {"q": q}, NOW)["items"][0]


def test_channels_by_spoken_name_with_now_and_next(conn, settings):
    box = Toolbox(settings)
    first = _ch(box, conn, "первый")
    # 16:00 UTC is 19:00 in Moscow: "till" is local time, today without a date.
    assert first == {"id": first["id"], "name": "Первый канал HD", "lvl": "ok",
                     "now": "Новости", "till": "19:30",
                     "next": "Футбол. Премьер-лига", "at": "19:30"}  # fmt: skip
    assert _ch(box, conn, "1tv")["id"] == first["id"]  # a popular name
    assert _ch(box, conn, "euronews")["name"].startswith("Euronews")
    assert box.call(conn, "channels", {"q": "нет такого"}, NOW)["items"] == []


def test_search_play_report_round_trip(conn, settings):
    box = Toolbox(settings)
    found = box.call(conn, "search", {"q": "футбол"}, NOW)
    assert found["tz"] == "Europe/Moscow"
    titles = [(i["title"], i["at"]) for i in found["items"]]
    assert ("Футбол. Премьер-лига", "19:30") in titles
    assert ("Футбол. Обзор", "10-27 21:00") in titles  # another day: month-day first
    item = found["items"][0]
    assert set(item) <= {"title", "sub", "ch", "cid", "at", "end", "more"}

    played = box.call(conn, "play", {"id": item["cid"]}, NOW)
    best = played["streams"][0]
    assert best["url"].startswith("https://a.example/live/1tv/") and "ua" not in best
    assert len(played["streams"]) <= 3 and "m3u" not in played  # no MIH_PUBLIC_URL

    out = box.call(conn, "report", {"stream_id": best["stream_id"], "ok": False,
                                    "error": "timeout"}, NOW)  # fmt: skip
    assert out["stream_id"] == best["stream_id"]
    n = conn.execute("SELECT COUNT(*) FROM stream_observations WHERE stream_id = ?"
                     " AND kind = 'playback'", (best["stream_id"],)).fetchone()[0]  # fmt: skip
    assert n == 1


def test_headers_are_handed_to_the_player(conn, settings):
    box = Toolbox(settings)
    vtv = _ch(box, conn, "vtv1")
    s = box.call(conn, "play", {"id": vtv["id"]}, NOW)["streams"][0]
    assert s["ua"] == "SomePlayer/1.0" and s["ref"] == "https://a.example/"


def test_now_schedule_topics_status(conn, settings):
    box = Toolbox(settings)
    now = box.call(conn, "now", {}, NOW)
    assert {i["name"]: i["now"] for i in now["items"]}["Первый канал HD"] == "Новости"
    first = _ch(box, conn, "первый")["id"]
    sched = box.call(conn, "schedule", {"id": first, "hours": 3}, NOW)
    assert [i["title"] for i in sched["items"]] == ["Новости", "Футбол. Премьер-лига"]
    tomorrow = box.call(conn, "schedule", {"id": first, "day": "tomorrow", "hours": 24}, NOW)
    assert [i["at"] for i in tomorrow["items"]] == ["10-27 21:00"]

    _follow(conn, "хоккей")
    t = box.call(conn, "topics", {"add": "ФУТБОЛ", "remove": "хоккей"}, NOW)
    assert t["topics"] == ["футбол"]
    assert [s["title"] for s in t["soon"]] == ["Футбол. Премьер-лига"]

    st = box.call(conn, "status", {}, NOW)
    assert st["channels"] >= 3 and st["playing"]["ok"] >= 3


def test_http_needs_token_and_answers_json_rpc(conn, settings):
    client = TestClient(create_mcp_app(settings))
    rpc = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
           "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                      "clientInfo": {"name": "t", "version": "1"}}}  # fmt: skip
    assert client.post("/mcp", json=rpc).status_code == 401
    h = {"Authorization": f"Bearer {TOKEN}"}
    init = client.post("/mcp", json=rpc, headers=h).json()["result"]
    assert init["protocolVersion"] == "2025-06-18" and "tools" in init["capabilities"]
    note = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    assert client.post("/mcp", json=note, headers=h).status_code == 202
    tools = client.post("/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                        headers=h).json()["result"]["tools"]  # fmt: skip
    assert [t["name"] for t in tools] == ["channels", "search", "now", "schedule", "play",
                                          "report", "topics", "status"]  # fmt: skip
    bad = client.post("/mcp", json={"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                    "params": {"name": "schedule", "arguments": {"id": "x"}}},
                      headers=h).json()["result"]  # fmt: skip
    assert bad["isError"] and "channels" in bad["content"][0]["text"]
    # A page in a browser on the same host cannot talk to it.
    evil = client.post("/mcp", json=rpc, headers={**h, "Origin": "https://evil.example"})
    assert evil.status_code == 403
    assert client.get("/mcp", headers=h).status_code == 405


def test_stdio_and_daily_limit(conn, settings):
    lim = Settings(**{**settings.__dict__, "mcp_daily_limit": 1})
    lines = [
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": "channels", "arguments": {"q": "первый", "limit": 1}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "status", "arguments": {}}},
    ]  # fmt: skip
    out = io.StringIO()
    run_stdio(lim, io.StringIO("".join(json.dumps(x) + "\n" for x in lines)), out)
    first, second = (json.loads(x) for x in out.getvalue().splitlines())
    items = json.loads(first["result"]["content"][0]["text"])["items"]
    assert items[0]["name"] == "Первый канал HD"
    assert second["error"]["code"] == -32000  # the day's one call is spent


def test_index_follows_catalog_changes(conn, settings):
    server = McpServer(settings)
    box = server.toolbox
    assert box.find_channels(conn, "первый")
    conn.execute(
        "INSERT INTO channels (id, canonical_name, created_at) VALUES ('ch_x', 'Звезда', 'x')"
    )
    assert "ch_x" in box.find_channels(conn, "звезда")


def test_mcp_off_loopback_needs_a_token(tmp_path):
    s = Settings(data_dir=tmp_path, mcp_port=8766, mcp_host="0.0.0.0")
    with pytest.raises(ConfigError):
        s.validate_for_serving()
    Settings(data_dir=tmp_path, mcp_port=8766).validate_for_serving()  # loopback: fine
