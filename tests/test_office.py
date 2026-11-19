"""Раздел «Офис»: шаги и итоги фоновых заданий PULSE и доска сессий Claude Code и Codex в папках из настроек."""

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


# --- доска: сессии Claude Code и Codex в папках ------------------------------------------------------

def _line(kind, at, cwd, **extra):
    return {"type": kind, "timestamp": at, "cwd": cwd, "sessionId": "s", **extra}


def _write(path, lines):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for d in lines:
            f.write(json.dumps(d, ensure_ascii=False) + "\n")


def _asked_and_answered(path, cwd, minutes=3, entry="claude-desktop", text="Сделай"):
    _write(path, [_line("user", _ago(minutes=minutes), cwd, entrypoint=entry, message={"content": text}),
                  _line("assistant", _ago(minutes=minutes - 1), cwd, message={"content": [{"type": "text", "text": "**Готово.** Отчёт в папке."}]})])


def _cards(**kw) -> dict:
    b = office.board(**kw)
    return {c["id"]: c for lane in b["lanes"].values() for c in lane}


def _app_session(app, cli, **rec):
    (app / "u" / "w").mkdir(parents=True, exist_ok=True)
    (app / "u" / "w" / f"local_{cli}.json").write_text(json.dumps({"sessionId": f"local_{cli}", "cliSessionId": cli, **rec},
                                                                   ensure_ascii=False), encoding="utf-8")


@pytest.fixture
def claude_home(tmp_path, monkeypatch, settings_with):
    home = tmp_path / "claude"
    root = tmp_path / "Работа"
    for project in ("Проект А", "Личное", "Проект Б"):
        (root / project).mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))  # не читать настоящие треды Codex
    monkeypatch.setattr(office, "desktop_dir", lambda: tmp_path / "no-app")  # не читать сессии настоящего приложения
    from opsdash import agents

    monkeypatch.setattr(agents, "chatgpt_app", lambda: False)
    settings_with(office={"folders": [str(root)], "hide": ["личное"], "days": 7})
    return home / "projects" / "-work", root


def test_board_puts_sessions_into_lanes(claude_home):
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

    b = office.board()
    assert [c["id"] for c in b["lanes"]["now"]] == ["b2"] and [c["id"] for c in b["lanes"]["you"]] == ["a1"]
    assert not b["lanes"]["done"] and b["counts"]["all"] == 2
    a = b["lanes"]["you"][0]
    assert a["title"] == "Отчёт по продажам" and a["project"] == "Проект А"  # по записанному файлу, хотя сессия шла в корне
    assert a["state_title"] == "ответил, ждёт вас" and a["said"] == "Готово, отчёт в папке." and a["model"] == "opus-5-5"
    assert [f["name"] for f in a["files"]] == ["Отчёт.xlsx"] and a["steps"][-1]["t"] == "Пишет «Отчёт.xlsx»"
    w = b["lanes"]["now"][0]
    assert w["step"] == "Сверяет итоги" and w["since"] and w["key"] == "Работа/Проект Б"
    assert [a["title"] for a in w["agents_working"]] == ["Сверить выгрузку"]
    assert {p["name"]: (p["now"], p["you"]) for p in b["projects"]} == {"Проект А": (0, 1), "Проект Б": (1, 0)}
    assert b["projects"][0]["name"] == "Проект А"  # кто ждёт вас — первым в фильтре

    # дописали журнал — дочитываем только новое: команда закончилась, Claude ответил
    _write(journals / "b2.jsonl", [
        _line("user", _ago(seconds=5), str(root / "Проект Б"), message={"content": [{"type": "tool_result", "tool_use_id": "t2"}]}),
        _line("assistant", _ago(seconds=1), str(root / "Проект Б"), message={"content": [{"type": "text", "text": "Сошлось."}]}),
    ])
    (sub / "agent-x1.meta.json").write_text(json.dumps({"description": "Сверить выгрузку", "finished": True}))
    w = _cards()["b2"]
    assert w["lane"] == "you" and w["agents"][0]["state"] == "done" and not w["agents_working"]

    one = office.board(project="Работа/Проект Б")  # фильтр по проекту
    assert one["project"] == "Работа/Проект Б" and [c["id"] for lane in one["lanes"].values() for c in lane] == ["b2"]
    assert office.board(project="нет такого")["project"] == ""


def test_tool_without_answer_for_long_looks_like_permission_wait(claude_home):
    journals, root = claude_home
    _write(journals / "p.jsonl", [
        _line("user", _ago(minutes=12), str(root), message={"content": "Удали старые выгрузки"}),
        _line("assistant", _ago(minutes=11), str(root), message={"content": [
            {"type": "tool_use", "id": "t", "name": "Bash", "input": {"command": "rm old/*.csv"}}]}),
    ])
    s = _cards()["p"]
    assert s["lane"] == "you" and s["state_title"] == "ждёт разрешения?" and s["steps"][-1]["t"] == "Выполняет команду rm"


def test_office_page_is_a_board_and_opens_only_inside_office_folders(client, claude_home, monkeypatch):
    journals, root = claude_home
    _write(journals / "a.jsonl", [_line("user", _ago(minutes=1), str(root / "Проект А"), message={"content": "Привет"})])
    _asked_and_answered(journals / "z.jsonl", str(root / "Проект Б"), minutes=400, entry="cli")
    page = client.get("/office").text
    for part in ("Ждут вас", "Работают сейчас", "Закончили", "PULSE в фоне", "Секретарь", "Проект А", "Привет", "Новая сессия"):
        assert part in page, part
    assert "<canvas" not in page and "office.js" not in page and "data-office-json" not in page  # картинки офиса нет
    assert 'data-office-tab="Работа/Проект А"' in page  # фильтр по проектам
    assert 'href="claude://code/new?folder=' in page  # новая сессия в папке проекта
    live = client.get("/office/live?p=Работа/Проект Б").text
    assert "Привет" not in live and "Продолжить в Терминале" in live and 'data-office-active="0"' in live
    api = client.get("/api/office").json()
    assert api["pulse"][0]["key"] == "triage" and set(api["lanes"]) == {"now", "you", "done"}

    from opsdash.web import app as web

    opened = []
    monkeypatch.setattr(web, "_open_local", lambda path, reveal: opened.append((path, reveal)))
    inside = root / "Проект А"
    r = client.post("/office/open", data={"path": str(inside), "reveal": "1"}, follow_redirects=False)
    assert r.status_code == 303 and opened == [(str(inside), True)]
    r = client.post("/office/open", data={"path": "/etc/hosts"}, follow_redirects=False)
    assert "err=" in r.headers["location"] and len(opened) == 1


def test_office_page_without_folders_says_what_to_do(client, settings_with):
    settings_with(office={"folders": []})
    page = client.get("/office").text
    assert "Добавьте папки, где вы работаете с Claude Code или Codex" in page and "Секретарь" in page


def test_classic_design_renders_the_same_board(client, claude_home, settings_with):
    journals, root = claude_home
    _write(journals / "a.jsonl", [_line("user", _ago(minutes=1), str(root / "Проект А"), message={"content": "Привет"})])
    settings_with(web={"design": "classic"})
    page = client.get("/office").text
    assert "Работают сейчас" in page and "Привет" in page and 'class="topline"' in page and "<canvas" not in page


def test_open_in_claude_link_archive_and_pulse_own_runs_hidden(client, claude_home, tmp_path, monkeypatch):
    journals, root = claude_home
    from opsdash import config

    pulse_dir = root / "Пульт"  # PULSE лежит внутри папки офиса — его фоновые запуски не дублируем
    pulse_dir.mkdir()
    monkeypatch.setattr(config, "APP_DIR", pulse_dir)
    other = root / "Проект Б" / "pulse-copy"  # другая установка PULSE (рабочая копия кода) — тоже служебное
    (other / "opsdash").mkdir(parents=True)
    (other / "opsdash" / "office.py").write_text("")
    app = tmp_path / "app-sessions"
    monkeypatch.setattr(office, "desktop_dir", lambda: app)
    _app_session(app, "mine", title="Отчёт для правления", isStarred=True)
    _app_session(app, "gone", isArchived=True)
    _asked_and_answered(journals / "mine.jsonl", str(root / "Проект А"), minutes=9)
    _write(journals / "gone.jsonl", [_line("user", _ago(minutes=9), str(root / "Проект Б"), entrypoint="claude-desktop", message={"content": "Старое"})])
    _write(journals / "bg.jsonl", [_line("user", _ago(minutes=5), str(pulse_dir), entrypoint="sdk-cli", message={"content": "Разбери сообщения"})])
    _write(journals / "bg2.jsonl", [_line("user", _ago(minutes=5), str(other), entrypoint="sdk-cli", message={"content": "Дайджест"})])
    _asked_and_answered(journals / "sdk.jsonl", str(root / "Проект Б"), minutes=9, entry="sdk-cli")
    b = office.board()
    cards = {c["id"]: c for lane in b["lanes"].values() for c in lane}
    assert set(cards) == {"mine"}  # PULSE и его копия — в «PULSE в фоне»; запуск без человека после работы не нужен
    assert b["counts"]["archived"] == 1  # в архиве приложения — не показываем, только считаем
    mine = cards["mine"]
    assert mine["link"] == "claude://code/continue?session=local_mine" and not mine["resume"]
    assert mine["title"] == "Отчёт для правления" and mine["starred"] and mine["lane"] == "you"
    page = client.get("/office").text
    assert 'href="claude://code/continue?session=local_mine"' in page and "Открыть в Claude" in page


def test_lanes_follow_the_claude_app(claude_home, tmp_path, monkeypatch):
    """«Ждут вас» — когда приложение Claude считает, что нужно действие человека (даже если сессию уже открывали),
    или Claude ответил, а вы ещё не смотрели; всё готово или просмотрено — «Закончили» с итогом хода одной строкой."""
    journals, root = claude_home
    app = tmp_path / "app"
    monkeypatch.setattr(office, "desktop_dir", lambda: app)
    now_ms = datetime.now(timezone.utc).timestamp() * 1000
    cases = {  # журнал → (итог хода по оценке приложения, открывали ли после ответа)
        "seen": ("blocked", now_ms), "fresh": ("blocked", 0), "done": ("completed", 0), "review": ("review_ready", now_ms),
        "old": ("blocked", 0)}
    for cli, (status, focused) in cases.items():
        _app_session(app, cli, lastActivityAt=now_ms - 60000, lastFocusedAt=focused,
                     lastAssistantUuid="u1" if cli != "old" else "u2", postTurnSummaryFor="u1",
                     postTurnSummary={"status_category": status, "needs_action": "Создать MR и отправить коллеге",
                                      "status_detail": "PULSE 2.0.61 выкатил, тесты зелёные"})
        _asked_and_answered(journals / f"{cli}.jsonl", str(root / "Проект А"))
    b = office.board()
    got = {c["id"]: (c["lane"], c["state_title"], c["needs"], c["seen"]) for lane in b["lanes"].values() for c in lane}
    assert got["seen"] == ("you", "нужно от вас", "Создать MR и отправить коллеге", True)
    assert got["fresh"] == ("you", "нужно от вас", "Создать MR и отправить коллеге", False)
    assert got["done"] == ("done", "готово", "", False)
    assert got["review"] == ("done", "на проверку", "", True)
    assert got["old"] == ("you", "ответил, ждёт вас", "", False)  # итог от прошлого хода — не верим, решает журнал
    done = _cards()["done"]
    assert done["detail"] == "PULSE 2.0.61 выкатил, тесты зелёные" and done["day"] == "Сегодня"
    assert _cards()["old"]["detail"] == ""
    assert [c["id"] for c in b["lanes"]["done"]] == ["done", "review"]  # не открывали — сверху
    assert b["counts"]["unseen"] == 1


def test_dismiss_hides_finished_session_until_its_next_turn(client, claude_home, tmp_path, monkeypatch):
    journals, root = claude_home
    app = tmp_path / "app"
    monkeypatch.setattr(office, "desktop_dir", lambda: app)
    now_ms = datetime.now(timezone.utc).timestamp() * 1000
    _app_session(app, "f", lastActivityAt=now_ms - 60000, lastFocusedAt=now_ms, lastAssistantUuid="u1",
                 postTurnSummaryFor="u1", postTurnSummary={"status_category": "completed"})
    _asked_and_answered(journals / "f.jsonl", str(root / "Проект А"), minutes=50)
    _write(journals / "w.jsonl", [_line("user", _ago(minutes=1), str(root / "Проект Б"), message={"content": "Работай"})])
    r = client.post("/office/dismiss", data={"sid": "w"}, follow_redirects=False)  # работающую — нельзя
    assert "err=" in r.headers["location"]
    r = client.post("/office/dismiss", data={"sid": "f"}, follow_redirects=False)
    assert r.status_code == 303 and "msg=" in r.headers["location"]
    b = office.board()
    assert "f" not in [c["id"] for c in b["lanes"]["done"]] and b["counts"]["dismissed"] == 1
    assert "Убрано с доски: 1" in client.get("/office/live").text
    # новый ход — снова на доске
    _asked_and_answered(journals / "f.jsonl", str(root / "Проект А"), minutes=2, text="Ещё одно")
    assert "f" in _cards() and office.board()["counts"]["dismissed"] == 0
    office.dismiss("f")
    r = client.post("/office/restore", follow_redirects=False)
    assert "msg=" in r.headers["location"] and "f" in _cards() and not store.get_state("office_dismissed")


def test_resume_in_terminal_for_sessions_started_there(client, claude_home, monkeypatch):
    journals, root = claude_home
    from opsdash import brain

    cwd = root / "Проект Б"
    _asked_and_answered(journals / "t1.jsonl", str(cwd), minutes=300, entry="cli")
    c = _cards()["t1"]
    assert c["link"] == "" and c["resume"] and c["lane"] == "done"
    ran = []
    monkeypatch.setattr(office, "_terminal", lambda folder, cmd: ran.append((folder, cmd)))
    monkeypatch.setattr(brain, "find_claude", lambda settings=None: "/opt/claude")
    r = client.post("/office/resume", data={"sid": "t1"}, follow_redirects=False)
    assert "msg=" in r.headers["location"] and ran == [(str(cwd), ["/opt/claude", "--resume", "t1"])]
    r = client.post("/office/resume", data={"sid": "../../etc"}, follow_redirects=False)  # только сессии из папок «Офиса»
    assert "err=" in r.headers["location"] and len(ran) == 1
    monkeypatch.setattr(brain, "find_claude", lambda settings=None: None)
    r = client.post("/office/resume", data={"sid": "t1"}, follow_redirects=False)
    assert "err=" in r.headers["location"] and "Claude%20Code" in r.headers["location"]


def test_terminal_window_on_mac_runs_command_in_session_folder(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(office.sys, "platform", "darwin")
    monkeypatch.setattr(office.subprocess, "run", lambda cmd, **kw: calls.append(cmd))
    office._terminal(str(tmp_path / "Мой проект"), ["/opt/claude", "--resume", "abc"])
    script = calls[0][1]
    assert calls[0][0] == "open" and script.endswith("resume.command")
    text = open(script, encoding="utf-8").read()
    assert f"cd '{tmp_path / 'Мой проект'}' || exit 1" in text and "exec /opt/claude --resume abc" in text


def test_worktree_session_belongs_to_its_project(claude_home):
    journals, root = claude_home
    wt = root / ".claude" / "worktrees" / "feature-x"
    wt.mkdir(parents=True)
    _asked_and_answered(journals / "wt.jsonl", str(wt), minutes=5)
    c = _cards()["wt"]
    assert c["project"] == "Работа" and c["room"] == "" and c["worktree"] == "feature-x"


def test_codex_sessions_on_the_board(claude_home, tmp_path, monkeypatch):
    from opsdash import agents

    journals, root = claude_home
    day = tmp_path / "codex" / "sessions" / "2026" / "11" / "19"
    day.mkdir(parents=True)
    tid = "019a0000-1111-2222-3333-444455556666"
    j = day / f"rollout-2026-11-19T10-00-00-{tid}.jsonl"
    _write(j, [{"type": "session_meta", "timestamp": _ago(minutes=20),
                "payload": {"id": tid, "cwd": str(root / "Проект А"), "source": "vscode", "thread_source": "user"}},
               {"type": "response_item", "timestamp": _ago(minutes=20),
                "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Сверь выгрузку"}]}},
               {"type": "event_msg", "timestamp": _ago(minutes=19), "payload": {"type": "task_complete"}},
               {"type": "response_item", "timestamp": _ago(minutes=19),
                "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Сверил, расхождений нет"}]}}])
    parsed = []
    real = agents._codex_parse
    monkeypatch.setattr(agents, "_codex_parse", lambda path: parsed.append(path) or real(path))
    c = _cards()[f"codex-{tid}"]
    assert c["engine"] == "codex" and c["project"] == "Проект А" and c["said"] == "Сверил, расхождений нет"
    assert c["link"] == "" and c["resume"]  # без приложения ChatGPT — Codex CLI в Терминале
    _cards()
    assert len(parsed) == 1  # неизменившийся журнал не перечитываем
    monkeypatch.setattr(agents, "chatgpt_app", lambda: True)
    assert _cards()[f"codex-{tid}"]["link"] == f"codex://threads/{tid}"


def test_office_settings_saved(client):
    r = client.post("/settings/office", data={"folders": "/нет/такой\n", "hide": "Личное", "days": 100},
                    follow_redirects=False)
    assert "err=" in r.headers["location"]
    from opsdash import config

    s = config.load_settings()["office"]
    assert s == {"folders": ["/нет/такой"], "hide": ["Личное"], "days": 60, "agent_folder": ""}
