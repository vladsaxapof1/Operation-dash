"""PULSE как MCP-сервер только для чтения — для сессий Claude Code в любой папке (opsdash.mcp_server)."""

import io
import json
import os
import sqlite3
import subprocess

import pytest
from starlette.testclient import TestClient

from opsdash import anthology, brain, mcp_server, records, store


@pytest.fixture(autouse=True)
def writable_after(monkeypatch):
    monkeypatch.setattr(store, "_query_only", False)  # serve() включает «только чтение» — другим тестам он не нужен


@pytest.fixture
def data():
    store.execute("INSERT INTO meetings(id, title, started_at, status, headline) "
                  "VALUES('m1', 'Склад: бюджет на декабрь', '2026-11-10T09:00:00+00:00', 'processed', "
                  "'Бюджет склада урезаем')")
    store.execute("INSERT INTO meetings(id, title, started_at, status) "
                  "VALUES('m2', 'Планёрка', '2026-11-11T09:00:00+00:00', 'processed')")
    records.meeting_dir("m1").mkdir(parents=True, exist_ok=True)
    (records.meeting_dir("m1") / "summary.md").write_text(
        "# Склад\n\n- Бюджета склада на декабрь хватит только на аренду [00:03:10]\n- Про отпуск — не по теме\n",
        encoding="utf-8")
    records.meeting_dir("m2").mkdir(parents=True, exist_ok=True)
    (records.meeting_dir("m2") / "summary.md").write_text("# Планёрка\n\nОбщее\n", encoding="utf-8")
    (records.meeting_dir("m2") / "transcript.md").write_text("[00:01:00] А что с бюджетом склада?\n" + "x" * 30_000,
                                                              encoding="utf-8")
    tid = records.add_commitment(direction="to_me", who="Олег", what="Прислать бюджет склада на декабрь",
                                 title="Бюджет склада", source_type="meeting", source_ref="m1")
    anthology.add(kind="decision", title="Бюджет склада режем на 10%", project="Склад", source_type="meeting",
                  source_ref="m1")
    return tid


def rpc(*msgs: dict) -> list[dict]:
    out = io.StringIO()
    mcp_server.serve(io.StringIO("".join(json.dumps(m, ensure_ascii=False) + "\n" for m in msgs)), out)
    return [json.loads(line) for line in out.getvalue().splitlines()]


def test_protocol_and_read_only(data):
    replies = rpc(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "task", "arguments": {"id": data}}},
        {"jsonrpc": "2.0", "id": 4, "method": "nope"},
    )
    assert [r["id"] for r in replies] == [1, 2, 3, 4]  # на уведомление ответа нет
    init = replies[0]["result"]
    assert init["serverInfo"]["name"] == "pulse" and "Только чтение" in init["instructions"]
    tools = replies[1]["result"]["tools"]
    assert {t["name"] for t in tools} == {"recall", "search", "meeting", "meetings", "task", "tasks", "decisions",
                                          "projects"}
    assert all(t["annotations"]["readOnlyHint"] for t in tools)
    task = replies[2]["result"]
    assert not task["isError"] and "Бюджет склада" in task["content"][0]["text"]
    assert replies[3]["error"]["code"] == -32601
    with pytest.raises(sqlite3.OperationalError):  # после serve() процесс базу только читает
        store.set_state("probe", 1)


def test_recall_finds_meetings_tasks_decisions_by_word_forms(data):
    text, err = mcp_server.call("recall", {"query": "бюджета складов"})
    assert not err
    head, meetings = text.split("Встречи (")
    assert f"[{data}]" in head and "Решения (1)" in head and "режем на 10%" in head
    assert "m1 ·" in meetings and "разбор" in meetings and "[00:03:10]" in meetings
    assert "m2 ·" in meetings and "расшифровка [00:01:00]" in meetings  # в разборе нет — нашлось в расшифровке
    assert "отпуск" not in meetings  # показаны только строки со словами запроса
    assert mcp_server.call("search", {"query": "бюджет склада"})[0] == text.replace("бюджета складов", "бюджет склада") \
        or "m1 ·" in mcp_server.call("search", {"query": "бюджет склада"})[0]  # search — короткое имя recall
    text, err = mcp_server.call("recall", {"query": "логистика"})
    assert not err and "ничего не нашлось" in text
    assert mcp_server.call("recall", {"query": "по и"}) == ("нужны слова для поиска — например, «переезд склада»", True)


def test_private_meeting_hidden_from_agents(data):
    records.set_private("m1", True)
    text, _ = mcp_server.call("recall", {"query": "бюджет склада"})
    assert "m1 ·" not in text and "m2 ·" in text
    assert mcp_server.call("meeting", {"id": "m1"}) == ("Это личная встреча — PULSE её агентам не отдаёт.", True)
    assert "m1" not in mcp_server.call("meetings", {})[0] and "m2" in mcp_server.call("meetings", {})[0]


def test_recall_from_mcp_logged_through_inbox_not_db(data):
    from opsdash import memory

    store.read_only()
    try:
        mcp_server.call("recall", {"query": "бюджет склада"})
    finally:
        store._query_only = False
    assert not store.scalar("SELECT COUNT(*) FROM recall_log")
    assert memory.absorb_inbox() == {"recall": 1}
    row = store.row("SELECT who, query FROM recall_log")
    assert row == {"who": "mcp", "query": "бюджет склада"}


def test_meeting_transcript_in_chunks_and_errors(data):
    first, err = mcp_server.call("meeting", {"id": "m2", "transcript": True})
    assert not err and first.startswith("[00:01:00]") and "offset=25000" in first
    rest, _ = mcp_server.call("meeting", {"id": "m2", "transcript": True, "offset": 25000})
    assert "дальше ещё" not in rest
    summary, _ = mcp_server.call("meeting", {"id": "m1"})
    assert "Бюджета склада на декабрь" in summary
    assert mcp_server.call("meeting", {"id": "nope"}) == ("нет встречи nope", True)
    assert mcp_server.call("task", {"id": 999}) == ("нет задачи [999]", True)
    text, err = mcp_server.call("task", {"idx": 1})
    assert err and text.startswith("Неверные параметры")
    tasks, _ = mcp_server.call("tasks", {"direction": "to_me", "search": "склад"})
    assert "Бюджет склада" in tasks
    assert mcp_server.call("tasks", {"direction": "mine"}) == ("Задач нет.", False)
    assert "Склад" in mcp_server.call("decisions", {})[0]


def fake_claude(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(brain, "find_claude", lambda *a, **k: "claude")
    calls = []

    def run(cmd, **kw):
        calls.append(cmd[2:])
        path = tmp_path / ".claude.json"
        data = json.loads(path.read_text()) if path.exists() else {}
        servers = data.setdefault("mcpServers", {})
        if cmd[2] == "add-json":
            servers[cmd[-2]] = json.loads(cmd[-1])
        elif cmd[2] == "remove":
            servers.pop(cmd[-1], None)
        path.write_text(json.dumps(data))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(mcp_server.subprocess, "run", run)
    return calls


def test_connect_status_and_disconnect(monkeypatch, tmp_path):
    calls = fake_claude(monkeypatch, tmp_path)
    assert mcp_server.status() == {"on": False, "stale": False}
    mcp_server.connect()
    assert calls[-1][:4] == ["add-json", "--scope", "user", "pulse"]
    s = json.loads(calls[-1][-1])
    assert s["args"] == ["-m", "opsdash", "mcp", "serve"] and s["env"]["OPSDASH_HOME"] == os.environ["OPSDASH_HOME"]
    assert mcp_server.status() == {"on": True, "stale": False}
    # PULSE перенесли: в настройке Claude Code путь к старой папке — переподключить (remove + add-json)
    data = json.loads((tmp_path / ".claude.json").read_text())
    data["mcpServers"]["pulse"]["command"] = "/old/place/.venv/bin/python"
    (tmp_path / ".claude.json").write_text(json.dumps(data))
    assert mcp_server.status() == {"on": True, "stale": True}
    mcp_server.connect()
    assert [c[0] for c in calls[-2:]] == ["remove", "add-json"] and not mcp_server.status()["stale"]
    mcp_server.disconnect()
    assert calls[-1] == ["remove", "--scope", "user", "pulse"] and not mcp_server.status()["on"]


def test_block_on_top_of_connections(monkeypatch, tmp_path):
    """Блок «PULSE в вашем Claude Code» — наверху «Подключений», над «Здоровьем» и отдельно от «Сервисов через MCP»
    (там PULSE читает сервисы, здесь Claude Code читает PULSE); в «Офисе» его нет."""
    fake_claude(monkeypatch, tmp_path)
    from opsdash.web.app import app

    client = TestClient(app, base_url="http://localhost:8765")
    conn = "http://localhost:8765/settings?tab=connections"
    page = client.get("/settings?tab=connections").text
    assert "PULSE в вашем Claude Code" in page and "не подключено" in page and ">Подключить<" in page
    assert "claude mcp add-json --scope user pulse" in page
    top = page.index('id="pulse-mcp"')
    assert top < page.index('id="conn-health-h"') and top < page.index(">Сервисы через MCP<")
    assert 'id="pulse-mcp"' in client.get("/settings?tab=telegram").text  # и под окном источника
    assert 'id="pulse-mcp"' not in client.get("/settings?tab=office").text
    r = client.post("/settings/pulse-mcp", data={"on": "1"}, follow_redirects=False, headers={"referer": conn})
    assert r.headers["location"].startswith("/settings?tab=connections&msg=")
    page = client.get("/settings?tab=connections").text
    assert "conn-pulse is-on" in page and "У Claude есть инструменты PULSE: recall, search" in page and ">Отключить<" in page
    client.post("/settings/pulse-mcp", data={}, headers={"referer": conn})
    assert not mcp_server.status()["on"]
    monkeypatch.setattr(brain, "find_claude", lambda *a, **k: None)
    r = client.post("/settings/pulse-mcp", data={"on": "1"}, follow_redirects=False, headers={"referer": conn})
    assert "err=" in r.headers["location"]  # без Claude Code — понятная ошибка, а не падение
