"""Свои подключения через MCP (MORE-1): клиент, чтение и изменения, файл подключений для Claude, предложения «Да»,
окно в «Подключениях»."""

import json
import sys

import httpx
import pytest
from starlette.testclient import TestClient

from opsdash import brain, config, mcp, proposals, store

REF = "http://localhost:8765"

FAKE_SERVER = r'''
import json, os, sys
for line in sys.stdin:
    msg = json.loads(line)
    rid, method = msg.get("id"), msg.get("method")
    if rid is None:
        continue
    if method == "initialize":
        res = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": "fake"}}
    elif method == "tools/list":
        res = {"tools": [
            {"name": "search_issues", "description": "Найти задачи", "annotations": {"readOnlyHint": True}},
            {"name": "get_issue", "description": "Задача по номеру"},
            {"name": "create_issue", "description": "Создать задачу"},
            {"name": "sync", "description": "Непонятно что делает"},
        ]}
    elif method == "tools/call":
        p = msg["params"]
        if p["name"] == "create_issue":
            res = {"content": [{"type": "text", "text": "создана задача " + p["arguments"]["title"]
                                + " ключом " + os.environ.get("FAKE_TOKEN", "?")}]}
        else:
            res = {"content": [{"type": "text", "text": "нельзя"}], "isError": True}
    else:
        print(json.dumps({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "no"}}), flush=True)
        continue
    print(json.dumps({"jsonrpc": "2.0", "id": rid, "result": res}), flush=True)
'''


@pytest.fixture
def fake(tmp_path):
    script = tmp_path / "fake_mcp.py"
    script.write_text(FAKE_SERVER, encoding="utf-8")
    return script


@pytest.fixture
def client():
    from opsdash.web.app import app

    return TestClient(app, base_url=REF)


def add_fake(fake, sid="tracker"):
    return mcp.save(sid, title="Трекер", transport="stdio", command=sys.executable, args=[str(fake)],
                    custom_fields=[{"name": "FAKE_TOKEN", "kind": "env", "secret": True, "label": "FAKE_TOKEN"}],
                    values={"FAKE_TOKEN": "tok-123"})


def test_read_or_write():
    assert mcp.is_read({"name": "anything", "annotations": {"readOnlyHint": True}})
    assert not mcp.is_read({"name": "get_all", "annotations": {"readOnlyHint": False}})  # подсказка сервера главнее
    assert mcp.is_read({"name": "get_issue"}) and mcp.is_read({"name": "searchPages"})
    assert mcp.is_read({"name": "hubspot-list-objects"})
    assert not mcp.is_read({"name": "create_issue"}) and not mcp.is_read({"name": "get_or_create"})
    assert not mcp.is_read({"name": "sync"})  # непонятное — изменение: помощник сам не вызовет


def test_check_tools_and_claude_config(fake):
    add_fake(fake)
    assert "tok-123" not in json.dumps(config.load_settings()["mcp"])  # значение ключа — не в настройках
    assert config.get_secret(mcp.secret_key("tracker", "FAKE_TOKEN")) == "tok-123"
    assert mcp.status("tracker")["label"] == "нужна проверка"
    assert mcp.check("tracker") == "инструментов: 4 — чтение 2, изменения 2"
    st = mcp.status("tracker")
    assert st["kind"] == "ok" and st["read_count"] == 2 and st["write_count"] == 2
    assert mcp.allowed_tools() == ["mcp__tracker__search_issues", "mcp__tracker__get_issue"]
    mcp.set_tools("tracker", read_on=["search_issues"], writes=True)  # директор сняла одну галочку
    assert mcp.allowed_tools() == ["mcp__tracker__search_issues"]
    path = mcp.claude_config()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert path.parent == config.SECRETS_DIR  # ключи — в папке, которую помощнику читать запрещено
    env = data["mcpServers"]["tracker"]["env"]
    assert env["FAKE_TOKEN"] == "tok-123" and env["PATH"].startswith(str(__import__("pathlib").Path(sys.executable).parent))
    lines = "\n".join(mcp.summary_lines())
    assert "читает сам (mcp__tracker__…): search_issues" in lines and "create_issue, sync" in lines


def test_brain_passes_mcp_only_to_chat_jobs(fake, monkeypatch):
    add_fake(fake)
    mcp.check("tracker")
    settings = config.load_settings()
    monkeypatch.setattr("opsdash.context.write", lambda s: (_ for _ in ()).throw(RuntimeError("без сводки")))
    cmd = brain._command(settings, "claude", "chat", model=None, resume=None)
    assert "--strict-mcp-config" in cmd and str(config.SECRETS_DIR / "mcp.json") in cmd
    allowed = cmd[cmd.index("--allowedTools") + 1:cmd.index("--disallowedTools")]
    assert "mcp__tracker__get_issue" in allowed and not any("create_issue" in a for a in allowed)
    cmd = brain._command(settings, "claude", "triage", model=None, resume=None)
    assert "--mcp-config" not in cmd  # частые фоновые разборы — без MCP
    mcp.save("tracker", enabled=False)
    assert "--mcp-config" not in brain._command(config.load_settings(), "claude", "chat", model=None, resume=None)


def test_write_through_proposal(fake):
    add_fake(fake)
    mcp.check("tracker")
    with pytest.raises(proposals.ProposalError):
        proposals.propose_mcp_call(server_id="tracker", tool="get_issue", args={"id": 1})  # чтение — вызывай сам
    with pytest.raises(proposals.ProposalError):
        proposals.propose_mcp_call(server_id="tracker", tool="nope", args={})
    p = proposals.propose_mcp_call(server_id="tracker", tool="create_issue", args={"title": "Прогноз"},
                                   title="Создать задачу «Прогноз» в трекере", reason="договорились на встрече")
    assert p["status"] == "pending" and "• title: Прогноз" in proposals.describe(p)
    done = proposals.approve(p["id"])
    assert done["status"] == "done" and done["result"]["text"] == "создана задача Прогноз ключом tok-123"
    assert proposals.result_text(done).startswith("✅ Выполнено: создана задача")
    mcp.set_tools("tracker", read_on=[], writes=False)
    with pytest.raises(proposals.ProposalError, match="только чтение"):
        proposals.propose_mcp_call(server_id="tracker", tool="create_issue", args={"title": "Ещё"})


def test_cli(fake, capsys):
    from opsdash import cli

    add_fake(fake)
    cli.main(["mcp", "check", "tracker"])
    cli.main(["mcp", "tools", "tracker"])
    out = capsys.readouterr().out
    assert "get_issue — читаешь сам" in out and "create_issue — изменение — ./ops propose mcp" in out
    cli.main(["propose", "mcp", "--server", "tracker", "--tool", "create_issue", "--args", '{"title": "Склад"}',
              "--title", "Задача «Склад»"])
    assert "Задача «Склад» — ждёт подтверждения" in capsys.readouterr().out


def test_http_server(fake):
    """Сервер по адресу: ключ — в заголовке, ответ — JSON или поток событий, сессия — в заголовке."""
    seen = []

    def handler(request):
        msg = json.loads(request.content)
        seen.append((msg.get("method"), request.headers.get("authorization"), request.headers.get("mcp-session-id")))
        if "id" not in msg:
            return httpx.Response(202)
        if msg["method"] == "initialize":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": msg["id"], "result": {}},
                                  headers={"Mcp-Session-Id": "s1"})
        body = json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": {"tools": [{"name": "list_projects"}]}})
        return httpx.Response(200, text=f"event: message\ndata: {body}\n\n", headers={"content-type": "text/event-stream"})

    mcp.save("linear", preset_id="linear", values={"token": "lin_api_1"})
    assert mcp.check("linear", transport=httpx.MockTransport(handler)) == "инструментов: 1 — чтение 1, изменения 0"
    assert seen[0] == ("initialize", "Bearer lin_api_1", None) and seen[-1][2] == "s1"

    def denied(request):
        return httpx.Response(401)

    with pytest.raises(mcp.MCPError, match="не принял ключ"):
        mcp.check("linear", transport=httpx.MockTransport(denied))
    assert mcp.status("linear")["kind"] == "error"


def test_catalog_and_window(client, fake):
    page = client.get("/settings?tab=connections").text
    assert "Сервисы через MCP" in page and "/settings?tab=mcp&amp;preset=linear" in page and "Свой сервер" in page
    page = client.get("/settings?tab=mcp&preset=notion").text
    assert "Токен интеграции" in page and "Подключить" in page and "Официальный сервер" in page
    for p in mcp.catalog():  # окно каждой карточки витрины открывается
        r = client.get(f"/settings?tab=mcp&preset={p['id']}")
        assert r.status_code == 200 and p["fields"][0]["label"] in r.text, p["id"]
    mcp.save("atlassian", preset_id="atlassian", values={"JIRA_URL": "https://acme.atlassian.net", "JIRA_API_TOKEN": "t"})
    page = client.get("/settings?tab=mcp&id=atlassian").text
    assert 'value="https://acme.atlassian.net"' in page and "сохранён:" in page  # обычное поле — видно, ключ — скрыт
    assert mcp.launch(mcp.server("atlassian"))["env"]["JIRA_URL"] == "https://acme.atlassian.net"
    mcp.remove("atlassian")
    r = client.post("/settings/mcp/save", data={"preset": "", "title": "Мой трекер", "transport": "stdio",
                                                "command": f'"{sys.executable}" "{fake}"', "env": "FAKE_TOKEN=tok-9"},
                    follow_redirects=False)
    assert r.status_code == 303 and "tab=mcp&id=" in r.headers["location"] and "msg=" in r.headers["location"]
    sid = r.headers["location"].split("id=")[1].split("&")[0]
    assert mcp.status(sid)["kind"] == "ok"
    page = client.get(f"/settings?tab=mcp&id={sid}").text
    assert "Читать — помощник делает сам" in page and "get_issue" in page and "create_issue" in page
    assert "tok-9" not in page  # ключ не показываем
    client.post(f"/settings/mcp/{sid}/tools", data={"read": ["get_issue"]})
    assert mcp.allowed_tools() == [f"mcp__{sid}__get_issue"] and not mcp.server(sid)["writes"]
    page = client.get("/settings?tab=connections").text
    assert "Мой трекер" in page and "подключено · 1 инструмент чтения" in page
    client.post(f"/settings/mcp/{sid}/remove")
    assert mcp.server(sid) is None and not config.get_secret(mcp.secret_key(sid, "FAKE_TOKEN"))
    assert store.get_state(f"mcp_tools:{sid}") is None


def test_missing_program(monkeypatch):
    monkeypatch.setattr(mcp, "which", lambda name: None)
    mcp.save("notion", preset_id="notion", values={"NOTION_TOKEN": "ntn_1"})
    assert mcp.status("notion")["label"] == "нужна программа Node.js"
    with pytest.raises(mcp.MCPError, match="установите Node.js"):
        mcp.check("notion")
