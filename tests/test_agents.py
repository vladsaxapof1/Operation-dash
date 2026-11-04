"""«Поручить агенту»: индекс сессий Claude Code, похожие поручения, контекст и сборка промта, ссылка запуска."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from opsdash import agents, cli, config, jobs, notices, office, records, store


def _ago(**delta) -> str:
    return (datetime.now(timezone.utc) - timedelta(**delta)).isoformat().replace("+00:00", "Z")


def _line(kind, at, cwd, **extra):
    return {"type": kind, "timestamp": at, "cwd": cwd, "sessionId": "s", **extra}


def _write(path, lines):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for d in lines:
            f.write(json.dumps(d, ensure_ascii=False) + "\n")


def _say(at, cwd, text):
    return _line("user", at, cwd, message={"role": "user", "content": text})


def _tool(at, cwd, tid, name, **params):
    return _line("assistant", at, cwd, message={"content": [{"type": "tool_use", "id": tid, "name": name, "input": params}]})


def _answer(at, cwd, text):
    return _line("assistant", at, cwd, message={"content": [{"type": "text", "text": text}]})


@pytest.fixture
def work(tmp_path, monkeypatch, settings_with):
    """Папка «Офиса» с проектами и журналы Claude Code в отдельной папке (не настоящие)."""
    home = tmp_path / "claude"
    root = tmp_path / "Работа"
    for project in ("Отчёты", "Финмодель", "Личное"):
        (root / project).mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home))
    monkeypatch.setattr(office, "desktop_dir", lambda: tmp_path / "no-app")
    settings_with(office={"folders": [str(root)], "hide": ["личное"], "days": 7})
    return home / "projects" / "-work", root


def _kpi_session(journals, root, name="k1.jsonl", days=3):
    cwd = str(root / "Отчёты")
    csv = root / "Отчёты" / "Выгрузка CRM — сентябрь.csv"
    xlsx = root / "Отчёты" / "KPI продаж — сентябрь.xlsx"
    csv.write_text("менеджер;выручка\n")
    xlsx.write_text("x")
    first = "Собери сводку KPI отдела продаж за сентябрь из выгрузки CRM: выручка, конверсия, средний чек. " + "Подробно. " * 60
    _write(journals / name, [
        {"type": "custom-title", "customTitle": "Сводка KPI продаж за сентябрь", "sessionId": "k1"},
        _say(_ago(days=days, minutes=50), cwd, first),
        _tool(_ago(days=days, minutes=49), cwd, "t1", "Read", file_path=str(csv)),
        _tool(_ago(days=days, minutes=48), cwd, "t2", "Skill", skill="xlsx"),
        _tool(_ago(days=days, minutes=47), cwd, "t3", "Read", file_path=str(root.parent / "secrets" / ".env")),
        _tool(_ago(days=days, minutes=46), cwd, "t4", "Write", file_path=str(xlsx), content="…"),
        _answer(_ago(days=days, minutes=45), cwd, "Готово, сводка в «KPI продаж — сентябрь.xlsx»."),
        _say(_ago(days=days, minutes=40), cwd, "Считай без НДС"),
        _say(_ago(days=days, minutes=30), cwd, "Добавь разбивку по менеджерам на отдельном листе"),
        _answer(_ago(days=days, minutes=29), cwd, "Пересчитала без НДС, добавила лист «По менеджерам»."),
    ])
    return csv, xlsx, first


# --- индекс сессий ---------------------------------------------------------------------------------

def test_index_keeps_prompts_files_and_outcome(work, monkeypatch, settings_with):
    journals, root = work
    csv, xlsx, first = _kpi_session(journals, root)
    _write(journals / "b2.jsonl", [_say(_ago(days=1), str(root / "Финмодель"), "Три сценария бюджета Q4"),
                                   _answer(_ago(days=1), str(root / "Финмодель"), "Готово")])
    _write(journals / "h3.jsonl", [_say(_ago(days=1), str(root / "Личное"), "Личное дело")])  # скрытый проект
    _write(journals / "o4.jsonl", [_say(_ago(days=1), "/elsewhere", "Чужая папка")])
    # фоновый запуск самого PULSE (claude -p в папке программы) — не опыт агента
    _write(journals / "p5.jsonl", [_say(_ago(days=1), str(config.APP_DIR), "Разбери сообщения") | {"entrypoint": "sdk-cli"}])
    settings_with(office={"folders": [str(root), str(config.APP_DIR)]})

    assert agents.index() == "сессий в индексе: 2, обновлено: 2"
    s = agents.show("k1")
    assert s["title"] == "Сводка KPI продаж за сентябрь" and s["room"] == "Отчёты" and s["home"] == str(root / "Отчёты")
    assert s["first_prompt"] == first.strip() and len(s["first_prompt"]) > 300  # «Офису» хватает 300 символов, индексу — нет
    assert s["prompts"] == ["Считай без НДС", "Добавь разбивку по менеджерам на отдельном листе"]
    assert s["n_prompts"] == 3 and s["skills"] == ["xlsx"]
    assert s["files_read"] == [str(csv)] and s["files_written"] == [str(xlsx)]  # секреты — не в индексе
    assert s["summary"].startswith("Пересчитала без НДС") and [f["name"] for f in s["made"]] == ["KPI продаж — сентябрь.xlsx"]
    assert not agents.show("h3") and not agents.show("o4") and not agents.show("p5")

    # журнал не менялся — не перечитываем; дописали — перечитываем только его
    parsed = []
    real = office._session
    monkeypatch.setattr(office, "_session", lambda path, sub=False: parsed.append(path.stem) or real(path, sub))
    assert agents.index().endswith("обновлено: 0") and parsed == []
    _write(journals / "b2.jsonl", [_say(_ago(minutes=1), str(root / "Финмодель"), "Сценарий −10% — на отдельный слайд")])
    assert agents.index().endswith("обновлено: 1") and parsed == ["b2"]
    assert agents.show("b2")["prompts"] == ["Сценарий −10% — на отдельный слайд"]

    # Claude Code удалил старый журнал — опыт остаётся; старше полугода — убираем
    (journals / "k1.jsonl").unlink()
    agents.index()
    assert agents.show("k1")
    store.execute("UPDATE agent_sessions SET last=?, started=? WHERE id='k1'", (_ago(days=200), _ago(days=200)))
    agents.index()
    assert not agents.show("k1") and not store.rows("SELECT * FROM agent_sessions_fts WHERE id='k1'")


def test_office_still_shows_sessions_with_shared_parser(work):
    journals, root = work
    _kpi_session(journals, root, days=1)
    snap = office.snapshot("f0")
    s = snap["rooms"][0]["sessions"][0]
    assert s["title"] == "Сводка KPI продаж за сентябрь" and s["prompts"] == 3 and s["room"] == "Отчёты"


# --- похожие поручения ------------------------------------------------------------------------------

def test_similar_by_words_folder_and_hidden_rooms(work, settings_with):
    journals, root = work
    _kpi_session(journals, root)
    _write(journals / "b2.jsonl", [_say(_ago(days=1), str(root / "Финмодель"), "Сделай три сценария бюджета Q4"),
                                   _answer(_ago(days=1), str(root / "Финмодель"), "Готово")])
    agents.index()
    found = agents.similar("Сводка KPI продаж за октябрь: выручка и конверсия по менеджерам")
    assert [s["id"] for s in found] == ["k1"]
    assert {"сводк", "выруч"} <= set(found[0]["common"])
    assert agents.similar("сводка KPI продаж", folder=str(root / "Финмодель")) == []
    assert [s["id"] for s in agents.similar("сценарии бюджета Q4")] == ["b2"]
    assert agents.similar("погода в Казани") == []  # одно случайное общее слово — не похоже
    settings_with(office={"hide": ["отчёты"]})  # проект скрыли — его сессии и в поручения не попадают
    assert agents.similar("Сводка KPI продаж за октябрь") == []


# --- промт по задаче ----------------------------------------------------------------------------------

def _meeting_task(root):
    store.execute("INSERT INTO meetings(id, title, status, started_at) VALUES('m1', 'Планёрка', 'processed', ?)",
                  (store.utcnow(),))
    folder = records.meeting_dir("m1")
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "transcript.md").write_text(
        "# Расшифровка\n[00:05:00] Спикер 2: По складу всё по плану.\n"
        "[00:21:40] Спикер 1: Олег, к сверке плана я соберу сводку KPI продаж за октябрь по менеджерам.\n",
        encoding="utf-8")
    return records.add_commitment(direction="mine", who="Анна", whom="Олег Соколов", title="Сводка KPI продаж за октябрь",
                                  what="Собрать сводку KPI продаж за октябрь: выручка, конверсия, средний чек",
                                  source_type="meeting", source_ref="m1", quote="соберу сводку KPI продаж за октябрь")


def test_context_has_source_time_similar_files_and_save_command(work):
    journals, root = work
    csv, xlsx, _ = _kpi_session(journals, root)
    agents.index()
    cid = _meeting_task(root)
    (root / "Отчёты" / "about project.md").write_text("# Отчёты\nСуммы — без НДС.", encoding="utf-8")
    bid = store.execute("INSERT INTO task_agents(commitment_id, folder, engine, status) VALUES(?,?,'claude','new')",
                        (cid, str(root / "Отчёты")))
    text = agents.context(bid)
    assert "Задача [" in text and "«Сводка KPI продаж за октябрь»" in text
    assert "[00:21:40] Спикер 1: Олег, к сверке плана" in text  # место в расшифровке — со временем
    assert "Суммы — без НДС." in text  # паспорт проекта
    assert "- k1 · " in text and "создали: KPI продаж — сентябрь.xlsx" in text
    assert f"- {csv} — использовали в похожей сессии" in text
    assert f"- {xlsx} — создали в сессии «Сводка KPI продаж за сентябрь»" in text
    assert f"./ops agents brief-save {bid} --file data/agent-briefs/{cid}-сводка-kpi-продаж-за-октябрь.md" in text
    assert agents.agentish(records.get_commitment(cid))  # «сводка» — похоже на работу для агента


def test_save_adds_service_line_and_parses_files(work):
    journals, root = work
    csv, xlsx, _ = _kpi_session(journals, root)
    cid = _meeting_task(root)
    bid = store.execute("INSERT INTO task_agents(commitment_id, status) VALUES(?, 'running')", (cid,))
    b = agents.save(bid, f"# Задача: Сводка KPI\n\n## Файлы\n- {csv} — выгрузка за сентябрь\n- `{xlsx}` — образец\n"
                         "- /нет/такого.xlsx — ошибка\n\n## Как сделать\n- /не/файл — не из раздела «Файлы»\n")
    assert b["status"] == "ready" and b["prompt"].splitlines()[:2] == ["# Задача: Сводка KPI", f"PULSE: задача #{cid}"]
    assert [(f["name"], f["why"], f["exists"], f["on"]) for f in b["files"]] == [
        ("Выгрузка CRM — сентябрь.csv", "выгрузка за сентябрь", True, True),  # тире в имени файла — не разделитель
        ("KPI продаж — сентябрь.xlsx", "образец", True, True), ("такого.xlsx", "ошибка", False, True)]
    path = agents.brief_path(b)
    assert path == config.DATA_DIR / "agent-briefs" / f"{cid}-сводка-kpi-продаж-за-октябрь.md"
    assert path.read_text(encoding="utf-8").startswith("# Задача: Сводка KPI\nPULSE: задача #")
    # правка директора: убрала строку с файлом — галочка снята, список файлов прежний
    edited = "\n".join(line for line in b["prompt"].splitlines() if str(csv) not in line)
    b = agents.save(bid, edited)
    assert [f["on"] for f in b["files"]] == [False, True, True] and len(b["files"]) == 3


def test_build_runs_skill_and_keeps_steps(work, monkeypatch):
    from opsdash import brain

    journals, root = work
    _kpi_session(journals, root)
    cid = _meeting_task(root)
    submitted = []
    monkeypatch.setattr(jobs, "submit", lambda name, kind="light", args=None: submitted.append((name, kind, args)))
    bid = agents.start(cid, str(root / "Отчёты"), "claude")
    assert submitted == [("agent-brief", "chat", {"brief_id": bid})] and agents.live(bid)["status"] == "running"
    with pytest.raises(ValueError):
        agents.start(cid)  # уже собирается
    with pytest.raises(ValueError):
        agents.start(cid + 1)

    def claude(prompt, **kw):
        assert f"./ops agents context {bid}" in prompt and kw["job"] == "agent-brief"
        kw["on_step"]("Bash", {"command": f"./ops agents context {bid}"})
        kw["on_step"]("Bash", {"command": './ops agents similar "сводка KPI"'})
        agents.save(bid, "# Задача: Сводка KPI\n\n## Что нужно получить\nТаблица.")  # навык: ./ops agents brief-save
        return {"text": "Готово", "session_id": "x"}

    monkeypatch.setattr(brain, "stream", claude)
    assert jobs.REGISTRY["agent-brief"](brief_id=bid) == "промт готов"
    b = agents.for_task(cid)
    assert b["status"] == "ready" and b["steps"] == ["Собирает всё о задаче", "Ищет похожие поручения: «сводка KPI»"]
    assert [s["id"] for s in b["similar"]] == ["k1"]  # индекс обновлён перед сборкой, похожие — запомнены
    n = notices.summary("agent-brief", {"brief_id": bid}, "промт готов", None)
    assert n["title"] == "Промт для агента готов" and n["link"] == f"/agents?task={cid}"

    # Claude ничего не сохранил — поручение «не собралось», кнопка снова доступна
    monkeypatch.setattr(brain, "stream", lambda prompt, **kw: {"text": ""})
    monkeypatch.setattr(agents, "brief_path", lambda b: Path("/нет/файла.md"))
    agents.start(cid, "", "codex", fix="добавь прогноз Q4")
    with pytest.raises(RuntimeError):
        agents.build(bid)
    b = agents.get(bid)
    assert b["status"] == "failed" and b["fix"] == "добавь прогноз Q4" and b["engine"] == "codex"
    agents.start(cid)  # после ошибки — можно ещё раз
    agents.interrupted()
    assert agents.live(bid)["status"] == "failed"


# --- ссылка «Открыть в Claude» ----------------------------------------------------------------------

def _brief(prompt, folder="/Работа/Отчёты"):
    cid = records.add_commitment(direction="mine", what="Сводка KPI продаж за октябрь", title="Сводка KPI")
    bid = store.execute("INSERT INTO task_agents(commitment_id, folder, status) VALUES(?,?, 'running')", (cid, folder))
    return agents.save(bid, prompt)


def test_link_puts_prompt_into_new_session_and_long_one_into_file():
    b = _brief("# Задача: Сводка KPI\n\nСобери сводку.")
    url, whole = agents.claude_link(b)
    q = parse_qs(urlparse(url).query)
    assert url.startswith("claude://code/new?") and whole
    assert q["q"] == [b["prompt"]] and q["folder"] == ["/Работа/Отчёты"] and "file" not in q
    assert "%20" in url and "+" not in url  # пробелы — %20: приложение читает ссылку как URL

    long = _brief("# Задача: Большая\n\n" + "Очень подробно. " * 1000)
    url, whole = agents.claude_link(long)
    q = parse_qs(urlparse(url).query)
    path = str(agents.brief_path(long))
    assert not whole and len(long["prompt"]) > agents.LINK_PROMPT
    assert path in q["q"][0] and f"PULSE: задача #{long['commitment_id']}" in q["q"][0] and len(q["q"][0]) < 600
    assert q["file"] == [path] and Path(path).read_text(encoding="utf-8").startswith("# Задача: Большая")

    # Windows: длинную ссылку система может не передать — уже средний промт идёт файлом; без папки — без folder
    mid = _brief("# Задача: Средняя\n\n" + "Текст. " * 300, folder="")
    assert agents.claude_link(mid, windows=False)[1] and not agents.claude_link(mid, windows=True)[1]
    assert "folder" not in parse_qs(urlparse(agents.claude_link(mid)[0]).query)


def test_launch_saves_edit_opens_link_and_marks_time(monkeypatch):
    opened = []
    monkeypatch.setattr(agents, "open_url", opened.append)
    b = _brief("# Задача: Сводка KPI\n\nСобери сводку.")
    res = agents.launch(b["id"], "# Задача: Сводка KPI\n\nСобери сводку без НДС.")
    assert res["whole"] and opened == [res["url"]] and "%D0%B1%D0%B5%D0%B7%20%D0%9D%D0%94%D0%A1" in res["url"]
    assert agents.get(b["id"])["launched_at"] and "без НДС" in agents.brief_path(b).read_text(encoding="utf-8")


# --- окно задачи и страница поручения ---------------------------------------------------------------

@pytest.fixture
def client():
    from opsdash.web.app import app

    return TestClient(app, base_url="http://localhost:8765")


def test_panel_build_edit_and_open(client, work, monkeypatch):
    journals, root = work
    _kpi_session(journals, root)
    agents.index()
    cid = _meeting_task(root)
    page = client.get(f"/commitments?task={cid}").text
    assert f'data-agent-open="{cid}"' in page and "agent-hint" in page  # кнопка в окне задачи и подсказка

    page = client.get(f"/agents?task={cid}").text
    assert "Поручить агенту" in page and "Собрать промт" in page and "Сводка KPI продаж за сентябрь" in page
    assert f'value="{root / "Отчёты"}" selected' in page  # папка — где делали похожее
    part = client.get(f"/agents?task={cid}&part=1").text
    assert "<html" not in part and "data-agent-panel" in part  # кусочек для окна — без страницы вокруг

    submitted = []
    monkeypatch.setattr(jobs, "submit", lambda name, kind="light", args=None: submitted.append(args))
    r = client.post("/agents/build", data={"task": cid, "folder": "/etc", "engine": "claude"}, headers={"X-Agent-Panel": "1"})
    assert "папка — только из Настройки → Офис" in r.text and not submitted
    r = client.post("/agents/build", data={"task": cid, "folder": str(root / "Отчёты"), "engine": "claude"},
                    headers={"X-Agent-Panel": "1"})
    bid = submitted[0]["brief_id"]
    assert f'data-agent-live="{bid}"' in r.text and "собирает промт" in r.text
    assert client.get(f"/api/agents/{bid}/live").json() == {"status": "running", "steps": [], "error": ""}

    agents.save(bid, "# Задача: Сводка KPI\n\n## Файлы\n- /нет/файла.csv — выгрузка\n")
    part = client.get(f"/agents?task={cid}&part=1").text
    assert "data-agent-text" in part and "Открыть в Claude" in part and "Пересобрать" in part and "файла нет" in part
    r = client.post(f"/agents/{bid}/save", data={"prompt": "# Задача: Сводка KPI\n\nБез файлов."}, headers={"X-Agent-Panel": "1"})
    assert r.json() == {"ok": True} and "Без файлов." in agents.get(bid)["prompt"]

    opened = []
    monkeypatch.setattr(agents, "open_url", opened.append)
    r = client.post(f"/agents/{bid}/open", data={"prompt": "# Задача: Сводка KPI\n\nПоехали."}, headers={"X-Agent-Panel": "1"})
    assert r.json()["ok"] and "отправьте сами" in r.json()["said"]
    assert opened[0].startswith("claude://code/new?q=") and "folder=" in opened[0]
    r = client.post(f"/agents/{bid}/open", data={"prompt": "x"}, follow_redirects=False)  # без скриптов — назад на страницу
    assert r.status_code == 303 and r.headers["location"].startswith(f"/agents?task={cid}&msg=")


# --- команды для навыка --------------------------------------------------------------------------------

def test_cli_similar_show_and_brief_save(work, capsys, tmp_path):
    journals, root = work
    csv, _, _ = _kpi_session(journals, root)
    cli.main(["agents", "index"])
    cli.main(["agents", "similar", "сводка KPI продаж"])
    out = capsys.readouterr().out
    assert "сессий в индексе: 1" in out and "k1 · " in out and "«Сводка KPI продаж за сентябрь»" in out
    cli.main(["agents", "show", "k1"])
    out = capsys.readouterr().out
    assert "## Что человек дописал потом" in out and "2. Считай без НДС" in out and f"- {csv}" in out
    cid = _meeting_task(root)
    bid = store.execute("INSERT INTO task_agents(commitment_id, status) VALUES(?, 'running')", (cid,))
    draft = tmp_path / "бриф.md"
    draft.write_text("# Задача: Сводка\n\n## Файлы\n- /нет/файла.csv — выгрузка\n", encoding="utf-8")
    cli.main(["agents", "brief-save", str(bid), "--file", str(draft)])
    out = capsys.readouterr().out
    assert "Промт сохранён" in out and "Этих файлов нет" in out and agents.get(bid)["status"] == "ready"


# --- этап 2: запущенная сессия на задаче ----------------------------------------------------------------

def _launched(root, folder="Отчёты", minutes=10, title="Сводка KPI за октябрь"):
    """Поручение с готовым промтом, открытое в Claude minutes минут назад."""
    cid = records.add_commitment(direction="mine", what=f"Собрать: {title}", title=title)
    bid = store.execute("INSERT INTO task_agents(commitment_id, folder, status) VALUES(?,?, 'running')",
                        (cid, str(root / folder) if folder else ""))
    agents.save(bid, "# Задача: Сводка KPI\n\nСобери сводку.")
    store.execute("UPDATE task_agents SET launched_at=? WHERE id=?", (_ago(minutes=minutes).replace("Z", "+00:00"), bid))
    return cid, bid


def _app(monkeypatch, sessions):
    monkeypatch.setattr(office, "desktop_sessions", lambda: sessions)


@pytest.fixture(autouse=True)
def fresh_marks():
    agents._marks.clear()  # метки поручений кэшируются на несколько секунд — между тестами база другая


def test_link_finds_session_by_service_line_or_brief_file(work, monkeypatch):
    journals, root = work
    _app(monkeypatch, {})
    cid, bid = _launched(root)
    other, _ = _launched(root, minutes=60, title="Сценарии бюджета")
    cwd = str(root / "Отчёты")
    path = agents.brief_path(agents.get(bid))
    _write(journals / "old.jsonl", [_say(_ago(minutes=30), cwd, f"PULSE: задача #{cid}\nСтарое")])  # до «Открыть»
    _write(journals / "alien.jsonl", [_say(_ago(minutes=5), cwd, f"PULSE: задача #{cid}0\nЧужая задача")])
    _write(journals / "ours.jsonl", [_say(_ago(minutes=8), cwd, f"# Задача: Сводка KPI\nPULSE: задача #{cid}\nСобери"),
                                     _tool(_ago(minutes=7), cwd, "t1", "Write", file_path=str(root / "Отчёты" / "KPI.xlsx"))])
    # промт ушёл файлом, служебную строку стёрли — находим по имени брифа
    _write(journals / "file.jsonl", [_say(_ago(minutes=40), cwd, f"Полный бриф — в файле {agents.brief_path(agents.for_task(other))}")])
    assert agents.link() == 2
    assert agents.for_task(cid)["session_id"] == "ours" and agents.for_task(other)["session_id"] == "file"
    assert agents.show("ours")["title"] == "Сводка KPI"  # сессия сразу и в опыте агентов, название — без «# Задача:»
    assert path.name in str(agents.brief_path(agents.get(bid)))
    assert agents.link() == 0  # уже нашли — не перепривязываем
    marked = {x["id"]: (x["task"] or {}).get("id") for x in office.snapshot("f0")["rooms"][0]["sessions"]}
    assert marked == {"ours": cid, "file": other, "alien": None, "old": None}  # пометка «по задаче «…»» в «Офисе»


def test_state_block_chip_and_done_proposal(work, monkeypatch, client):
    from opsdash import proposals

    journals, root = work
    cid, bid = _launched(root)
    cwd = str(root / "Отчёты")
    made = root / "Отчёты" / "KPI продаж — октябрь.xlsx"
    made.write_text("x")
    _write(journals / "s1.jsonl", [_say(_ago(minutes=8), cwd, f"PULSE: задача #{cid}\nСобери"),
                                   _tool(_ago(minutes=7), cwd, "t1", "Write", file_path=str(made))])
    app = {"s1": {"id": "local_s1", "archived": False, "status": "", "needs": "", "seen": False}}
    _app(monkeypatch, app)
    assert agents.check() == "нашла сессий: 1, предложила закрыть: 0"
    st = agents.of(cid)["st"]
    assert st["state"] == "blocked" and st["title"] == "ждёт разрешения?"  # инструмент давно без ответа
    assert st["link"] == "claude://code/continue?session=local_s1"
    assert [f["name"] for f in st["files"]] == ["KPI продаж — октябрь.xlsx"]

    # ответил и ждёт; приложение говорит, что нужно от вас
    _write(journals / "s1.jsonl", [_line("user", _ago(minutes=2), cwd, message={"content": [{"type": "tool_result", "tool_use_id": "t1"}]}),
                                   _answer(_ago(minutes=1), cwd, "Сделала таблицу. Добавить лист по регионам?")])
    app["s1"].update(status="blocked", needs="ответить, нужен ли лист по регионам")
    agents._marks.clear()
    st = agents.of(cid)["st"]
    assert st["state"] == "waiting" and st["title"] == "ждёт вашего ответа" and st["needs"].startswith("ответить")
    page = client.get(f"/commitments?task={cid}").text
    assert "Агент — ждёт вашего ответа" in page and "KPI продаж — октябрь.xlsx" in page and "local_s1" in page
    assert 'class="agent-chip waiting"' in page  # метка с роботом на карточке доски
    assert 'class="agent-chip waiting"' in client.get("/commitments?view=list").text

    # приложение: готово — предложение закрыть задачу с файлами, один раз
    app["s1"].update(status="completed", needs="")
    assert agents.check() == "нашла сессий: 0, предложила закрыть: 1"
    p = proposals.list_proposals()[0]
    assert p["kind"] == "task.done" and p["title"] == "Закрыть задачу «Сводка KPI за октябрь» — агент сделал: KPI продаж — октябрь.xlsx"
    assert p["source_ref"] == "agent:s1" and ("Сессия агента", "claude://code/continue?session=local_s1") in proposals.links(p)
    assert agents.check().endswith("предложила закрыть: 0") and not agents.watching()
    agents._marks.clear()
    assert 'class="agent-chip done"' in client.get("/commitments").text
    proposals.approve(p["id"])
    assert records.get_commitment(cid)["status"] == "done"


def test_launched_block_waits_for_session_and_watch_is_scheduled(work, monkeypatch, client):
    journals, root = work
    _app(monkeypatch, {})
    cid, _ = _launched(root, minutes=1)
    page = client.get(f"/commitments?task={cid}").text
    assert "Промт открыт в Claude" in page and f"PULSE: задача #{cid}" in page and "agent-chip" not in page
    submitted = []
    monkeypatch.setattr(jobs, "submit", lambda name, kind="light", args=None: submitted.append(name) or True)
    jobs.tick()
    assert "agents-watch" in submitted
    store.execute("UPDATE commitments SET status='done' WHERE id=?", (cid,))  # задачу закрыли — следить не за чем
    submitted.clear()
    store.execute("DELETE FROM jobs WHERE name='agents-watch'")
    jobs.tick()
    assert "agents-watch" not in submitted


def test_default_folder_setting(work, client, settings_with):
    journals, root = work
    cid = records.add_commitment(direction="mine", what="Совсем новая работа", title="Новая работа")
    c = records.get_commitment(cid)
    assert agents.default_folder(c, []) == str(root)  # без похожих и без проекта — первая папка «Офиса»
    page = client.get("/settings?tab=office").text
    assert "Папка по умолчанию для поручений агенту" in page and f'value="{root / "Финмодель"}"' in page
    client.post("/settings/office", data={"folders": str(root), "agent_folder": str(root / "Финмодель")})
    assert agents.default_folder(c, []) == str(root / "Финмодель")
    client.post("/settings/office", data={"folders": str(root), "agent_folder": "/etc"})  # не из «Офиса» — не сохраняем
    assert config.load_settings()["office"]["agent_folder"] == ""


def test_own_session_is_not_similar_to_its_task(work, monkeypatch):
    journals, root = work
    _app(monkeypatch, {})
    cid, _ = _launched(root, title="Сводка KPI продаж за октябрь")
    _write(journals / "own.jsonl", [_say(_ago(minutes=5), str(root / "Отчёты"), f"PULSE: задача #{cid}\nСводка KPI продаж за октябрь")])
    agents.link()
    text = agents.task_text(records.get_commitment(cid))
    assert [s["id"] for s in agents.similar(text)] == ["own"]
    assert agents.similar(text, task=cid) == []
