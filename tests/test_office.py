"""Раздел «Офис»: шаги и итоги фоновых заданий PULSE и сессии Claude Code в папках из настроек."""

import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from opsdash import jobs, office, store


@pytest.fixture
def client():
    from opsdash.web.app import app

    return TestClient(app, base_url="http://localhost:8765")


def _ago(**delta) -> str:
    return (datetime.now(timezone.utc) - timedelta(**delta)).isoformat().replace("+00:00", "Z")


# --- офис PULSE ---------------------------------------------------------------------------------

def test_job_run_keeps_claude_steps_and_result(monkeypatch):
    def anthology(**_):
        step = office.step_recorder()  # так brain.stream получает шаги Claude
        step("Bash", {"command": './ops kb search "склад"'})
        step("Bash", {"command": './ops kb search "склад"'})  # подряд одинаковые — один шаг
        step("Read", {"file_path": "data/knowledge/Антология.md"})
        return "обзор недели для базы знаний готов"

    monkeypatch.setitem(jobs.REGISTRY, "anthology", anthology)
    jobs.run_job("anthology")
    run = store.row("SELECT * FROM agent_runs")
    assert run["status"] == "ok" and run["finished"]
    assert [s["t"] for s in json.loads(run["steps"])] == ["Ищет в базе знаний: «склад»", "Читает «Антология.md»"]
    assert run["title"] == "Обзор недели для базы знаний готов" and run["link"] == "/kb"
    desk = next(d for d in office.pulse() if d["key"] == "kb")
    assert desk["state"] == "idle" and desk["last"]["title"] == run["title"] and len(desk["history"]) == 1


def test_no_steps_outside_tracked_jobs(monkeypatch):
    assert office.step_recorder(None) is None  # не задание — шаги никуда не пишем, лишних вызовов нет
    monkeypatch.setitem(jobs.REGISTRY, "housekeeping", lambda **_: "готово")
    jobs.run_job("housekeeping")
    assert not store.rows("SELECT * FROM agent_runs")


def test_failed_run_shows_on_the_desk(monkeypatch):
    def triage(**_):
        raise RuntimeError("Claude: не выполнен вход")

    monkeypatch.setitem(jobs.REGISTRY, "triage", triage)
    with pytest.raises(RuntimeError):
        jobs.run_job("triage")
    desk = next(d for d in office.pulse() if d["key"] == "triage")
    assert desk["state"] == "error"
    assert desk["last"]["title"] == "Не получилось: Разбор сообщений (Claude)"


def test_running_job_shows_live_steps_and_restart_interrupts_it():
    store.execute("INSERT INTO jobs(name, last_run, running) VALUES('process-meeting', ?, 1)", (store.utcnow(),))
    rid = store.execute("INSERT INTO agent_runs(job, args, started, steps) VALUES('process-meeting', '{}', ?, ?)",
                        (store.utcnow(), json.dumps([{"t": "Читает расшифровку встречи", "at": store.utcnow()}])))
    desk = next(d for d in office.pulse() if d["key"] == "meetings")
    assert desk["state"] == "working" and desk["current"]["steps"][0]["t"] == "Читает расшифровку встречи"
    office.interrupted()
    assert store.row("SELECT status FROM agent_runs WHERE id=?", (rid,))["status"] == "error"


def test_next_digest_time_from_schedule(settings_with):
    settings = settings_with(schedule={"digest_morning": "09:00", "digest_evening": "18:30"},
                             owner={"work_hours": {"start": "09:00", "end": "20:00", "days": [1, 2, 3, 4, 5, 6, 7]}})
    text = office._next("digests", settings)
    assert text.startswith(("утренний дайджест — ", "вечерний дайджест — ")) and ("09:00" in text or "18:30" in text)


# --- офис в папке: сессии Claude Code --------------------------------------------------------------

def _line(kind, at, cwd, **extra):
    return {"type": kind, "timestamp": at, "cwd": cwd, "sessionId": "s", **extra}


def _write(path, lines):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for d in lines:
            f.write(json.dumps(d, ensure_ascii=False) + "\n")


@pytest.fixture
def claude_home(tmp_path, monkeypatch, settings_with):
    home = tmp_path / "claude"
    root = tmp_path / "Работа"
    for project in ("Проект А", "Личное", "Проект Б"):
        (root / project).mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home))
    monkeypatch.setattr(office, "desktop_dir", lambda: tmp_path / "no-app")  # не читать сессии настоящего приложения
    settings_with(office={"folders": [str(root)], "hide": ["личное"], "days": 7})
    return home / "projects" / "-work", root


def test_folder_office_reads_sessions_by_project(claude_home):
    journals, root = claude_home
    report = root / "Проект А" / "Отчёт.xlsx"
    report.write_text("x")
    cwd = str(root)
    # ответила и ждёт: вопрос → запись файла → результат → текст
    _write(journals / "a1.jsonl", [
        {"type": "custom-title", "customTitle": "Отчёт по продажам", "sessionId": "a1"},
        _line("user", _ago(minutes=30), cwd, message={"role": "user", "content": "Собери отчёт по продажам"}),
        _line("assistant", _ago(minutes=29), cwd, message={"model": "claude-opus-5-5", "content": [
            {"type": "tool_use", "id": "t1", "name": "Write", "input": {"file_path": str(report), "content": "…"}}]}),
        _line("user", _ago(minutes=28), cwd, message={"content": [{"type": "tool_result", "tool_use_id": "t1"}]}),
        _line("assistant", _ago(minutes=27), cwd, message={"content": [{"type": "text", "text": "Готово, отчёт в папке."}]}),
    ])
    # работает прямо сейчас: команда запущена только что, плюс субагент
    _write(journals / "b2.jsonl", [
        _line("user", _ago(minutes=2), str(root / "Проект Б"), message={"content": "Проверь цифры"}),
        _line("assistant", _ago(seconds=20), str(root / "Проект Б"), message={"content": [
            {"type": "tool_use", "id": "t2", "name": "Bash", "input": {"command": "python3 check.py", "description": "Сверяет итоги"}}]}),
    ])
    sub = journals / "b2" / "subagents"
    _write(sub / "agent-x1.jsonl", [_line("assistant", _ago(seconds=10), str(root), isSidechain=True, message={"content": [
        {"type": "tool_use", "id": "t3", "name": "Read", "input": {"file_path": str(root / "Проект Б" / "данные.csv")}}]})])
    (sub / "agent-x1.meta.json").write_text(json.dumps({"description": "Сверить выгрузку", "agentType": "general-purpose",
                                                        "finished": False}))
    # скрытый проект, чужая папка, давняя сессия — не видно
    _write(journals / "c3.jsonl", [_line("user", _ago(minutes=5), str(root / "Личное"), message={"content": "Личное"})])
    _write(journals / "d4.jsonl", [_line("user", _ago(minutes=5), "/elsewhere", message={"content": "Чужое"})])
    _write(journals / "e5.jsonl", [_line("user", _ago(days=20), cwd, message={"content": "Давнее"})])

    snap = office.snapshot("f0")
    assert snap["kind"] == "folder" and snap["name"] == "Работа"
    rooms = {r["name"]: r for r in snap["rooms"]}
    assert set(rooms) == {"Проект А", "Проект Б"}  # Проект А — по записанному файлу, хотя сессия шла в корне
    a = rooms["Проект А"]["sessions"][0]
    assert a["title"] == "Отчёт по продажам" and a["state"] == "waiting" and a["model"] == "claude-opus-5-5"
    assert [f["name"] for f in a["files"]] == ["Отчёт.xlsx"] and a["steps"][-1]["t"] == "Пишет «Отчёт.xlsx»"
    b = rooms["Проект Б"]["sessions"][0]
    assert b["state"] == "working" and b["steps"][-1]["t"] == "Сверяет итоги"
    assert b["agents"][0]["title"] == "Сверить выгрузку" and b["agents"][0]["state"] == "working"
    assert snap["counts"] == {"sessions": 2, "working": 1, "waiting": 1, "agents": 1, "agents_working": 1, "tools": 2}

    # дописали журнал — дочитываем только новое: команда закончилась, Claude ответил
    _write(journals / "b2.jsonl", [
        _line("user", _ago(seconds=5), str(root / "Проект Б"), message={"content": [{"type": "tool_result", "tool_use_id": "t2"}]}),
        _line("assistant", _ago(seconds=1), str(root / "Проект Б"), message={"content": [{"type": "text", "text": "Сошлось."}]}),
    ])
    (sub / "agent-x1.meta.json").write_text(json.dumps({"description": "Сверить выгрузку", "finished": True}))
    b = next(r for r in office.snapshot("f0")["rooms"] if r["name"] == "Проект Б")["sessions"][0]
    assert b["state"] == "waiting" and b["agents"][0]["state"] == "done"


def test_tool_without_answer_for_long_looks_like_permission_wait(claude_home):
    journals, root = claude_home
    _write(journals / "p.jsonl", [
        _line("user", _ago(minutes=12), str(root), message={"content": "Удали старые выгрузки"}),
        _line("assistant", _ago(minutes=11), str(root), message={"content": [
            {"type": "tool_use", "id": "t", "name": "Bash", "input": {"command": "rm old/*.csv"}}]}),
    ])
    s = office.snapshot("f0")["rooms"][0]["sessions"][0]
    assert s["state"] == "blocked" and s["steps"][-1]["t"] == "Выполняет команду rm"


def test_office_pages_and_open_only_inside_office_folders(client, claude_home, monkeypatch):
    journals, root = claude_home
    _write(journals / "a.jsonl", [_line("user", _ago(minutes=1), str(root / "Проект А"), message={"content": "Привет"})])
    assert "Секретарь" in client.get("/office").text
    page = client.get("/office").text
    assert "Проект А" in page and "Привет" in page
    assert "Проект А" in client.get("/office/live").text and "Секретарь" in client.get("/office/live").text
    assert client.get("/api/office?o=pulse").json()["desks"][0]["key"] == "triage"
    assert [o["kind"] for o in client.get("/api/office").json()["offices"]] == ["pulse", "folder"]

    from opsdash.web import app as web

    opened = []
    monkeypatch.setattr(web, "_open_local", lambda path, reveal: opened.append((path, reveal)))
    inside = root / "Проект А"
    r = client.post("/office/open", data={"path": str(inside), "reveal": "1"}, follow_redirects=False)
    assert r.status_code == 303 and opened == [(str(inside), True)]
    r = client.post("/office/open", data={"path": "/etc/hosts"}, follow_redirects=False)
    assert "err=" in r.headers["location"] and len(opened) == 1


def test_scene_for_pulse_says_what_workers_do():
    store.execute("INSERT INTO jobs(name, last_run, running) VALUES('triage', ?, 1)", (store.utcnow(),))
    store.execute("INSERT INTO agent_runs(job, args, started, steps) VALUES('triage', '{}', ?, ?)",
                  (store.utcnow(), json.dumps([{"t": "Ищет в переписке: «склад»", "at": store.utcnow()}])))
    scene = office.snapshot("pulse")["scene"]
    desks = {d["id"]: d for d in scene["desks"]}
    assert len(desks) == 9 and desks["desk:triage"]["state"] == "working"
    assert desks["desk:triage"]["say"] == "Ищет в переписке: «склад»" and desks["desk:triage"]["label"] == "Секретарь"
    assert scene["board"][1] == "работают: 1"


def test_scene_for_folder_puts_busy_sessions_first_and_caps_desks(client, claude_home, monkeypatch):
    journals, root = claude_home
    monkeypatch.setattr(office, "SCENE_DESKS", 2)
    for i in range(3):  # три давних сессии в «Проекте А» и одна, что ждёт ответа, в «Проекте Б»
        _write(journals / f"old{i}.jsonl", [_line("user", _ago(hours=5 + i), str(root / "Проект А"), message={"content": f"Задача {i}"}),
                                           _line("assistant", _ago(hours=5 + i), str(root / "Проект А"), message={"content": [{"type": "text", "text": "Готово"}]})])
    _write(journals / "w.jsonl", [_line("user", _ago(minutes=3), str(root / "Проект Б"), message={"content": "Сверь цифры"}),
                                  _line("assistant", _ago(minutes=2), str(root / "Проект Б"), message={"content": [{"type": "text", "text": "Сверил"}]})])
    _write(journals / "old.jsonl", [_line("user", _ago(hours=30), str(root / "Проект Б"), message={"content": "Позавчерашнее"})])
    scene = office.snapshot("f0")["scene"]
    shown = [(z["name"], d["state"]) for z in scene["zones"] for d in z["desks"]]
    assert ("Проект Б", "waiting") in shown and len(shown) == 2  # сессия старше суток — только в списке
    assert scene["board"][-1] == "ещё в списке: 3"
    page = client.get("/office").text  # один офис: PULSE и папка — на одной картинке, списками под ней
    data = json.loads(page.split('data-office-json>')[1].split("</script>")[0])
    assert [a["kind"] for a in data["areas"]] == ["pulse", "folder"]
    assert 'data-obj="sess:w"' in page and 'data-obj="desk:triage"' in page and "/static/office.js" in page
    assert page.count('class="office-group"') == 2


def test_open_in_claude_link_and_pulse_own_runs_hidden(client, claude_home, tmp_path, monkeypatch):
    journals, root = claude_home
    from opsdash import config

    pulse_dir = root / "Пульт"  # PULSE лежит внутри папки офиса — его фоновые запуски не дублируем
    pulse_dir.mkdir()
    monkeypatch.setattr(config, "APP_DIR", pulse_dir)
    app = tmp_path / "app-sessions"
    monkeypatch.setattr(office, "desktop_dir", lambda: app)
    for local, cli, archived in (("local_aaa-1", "mine", False), ("local_bbb-2", "gone", True)):
        (app / "u" / "w").mkdir(parents=True, exist_ok=True)
        (app / "u" / "w" / f"{local}.json").write_text(json.dumps({"sessionId": local, "cliSessionId": cli, "isArchived": archived}))
    answered = [{"type": "text", "text": "Готово"}]
    _write(journals / "mine.jsonl", [_line("user", _ago(minutes=9), str(root / "Проект А"), entrypoint="claude-desktop", message={"content": "Сделай"}),
                                     _line("assistant", _ago(minutes=8), str(root / "Проект А"), message={"content": answered})])
    _write(journals / "gone.jsonl", [_line("user", _ago(minutes=9), str(root / "Проект Б"), entrypoint="claude-desktop", message={"content": "Старое"})])
    _write(journals / "bg.jsonl", [_line("user", _ago(minutes=5), str(pulse_dir), entrypoint="sdk-cli", message={"content": "Разбери сообщения"})])
    _write(journals / "sdk.jsonl", [_line("user", _ago(minutes=9), str(root / "Проект Б"), entrypoint="sdk-cli", message={"content": "Скрипт"}),
                                    _line("assistant", _ago(minutes=8), str(root / "Проект Б"), message={"content": answered})])
    sessions = {s["id"]: s for r in office.snapshot("f0")["rooms"] for s in r["sessions"]}
    assert "bg" not in sessions  # разбор сообщений PULSE — в офисе PULSE, не в папке
    assert sessions["mine"]["link"] == "claude://code/continue?session=local_aaa-1" and sessions["mine"]["state"] == "waiting"
    assert sessions["gone"]["link"] == ""  # в приложении — в архиве
    assert sessions["sdk"]["state"] == "rest"  # запуск без человека ответа не ждёт
    page = client.get("/office").text
    assert 'href="claude://code/continue?session=local_aaa-1"' in page and "Открыть в Claude" in page


def test_office_settings_saved(client):
    r = client.post("/settings/office", data={"folders": "/нет/такой\n", "hide": "Личное", "days": 100},
                    follow_redirects=False)
    assert "err=" in r.headers["location"]
    from opsdash import config

    s = config.load_settings()["office"]
    assert s == {"folders": ["/нет/такой"], "hide": ["Личное"], "days": 60}
