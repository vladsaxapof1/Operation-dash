"""Приоритет задач (opsdash.priorities): важность × срочность, поправки «ждёт» и «её ждут», ручной уровень, порядок
«по приоритету», чип и меню на доске, строка в окне задачи, ./ops и оценка важности Claude."""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from opsdash import config, jobs, priorities, projects, records, skills, store, task_meetings, worklog

NOW = datetime(2026, 11, 10, 12, 0)  # вторник: «скоро» — до пятницы 13.11, следующий рабочий день — среда 11.11


@pytest.fixture
def tuesday(monkeypatch):
    monkeypatch.setattr(config, "now", lambda s=None: NOW.replace(tzinfo=config.tz(s)))


@pytest.fixture
def client():
    from opsdash.web.app import app

    return TestClient(app, base_url="http://localhost:8765")


def task(title, *, due=None, direction="mine", importance=None, who=None, **kw):
    cid = records.add_commitment(direction=direction, title=title, what=title, due=due, who=who,
                                 whom=None if direction != "mine" else "Олег", **kw)
    if importance:
        priorities.set_importance(cid, importance, f"почему {title}")
    return cid


def prio(cid):
    return records.get_commitment(cid)["prio"]


def test_importance_times_urgency(tuesday):
    late = task("Просрочена", due="2026-11-09")
    today_high = task("Сегодня важная", due="2026-11-10", importance="high")
    friday = task("К пятнице", due="2026-11-13")
    friday_high = task("К пятнице важная", due="2026-11-13", importance="high")
    later_high = task("Через неделю важная", due="2026-11-17", importance="high")
    none_low = task("Когда будет время", importance="low")
    late_low = task("Просрочена неважная", due="2026-11-06", importance="low")
    assert [prio(c)["key"] for c in (late, today_high, friday, friday_high, later_high, none_low, late_low)] == [
        "high", "urgent", "normal", "high", "high", "low", "normal"]
    assert prio(late)["why"] == "просрочено 1 день" and prio(late)["title"] == "Высокий"
    assert prio(today_high)["why"] == "срок сегодня · высокая важность: почему Сегодня важная"
    assert prio(friday)["why"] == "срок 13.11" and prio(task("Завтра", due="2026-11-11"))["why"] == "срок завтра"
    # не оценена — обычная; закрытые и архивные — без приоритета
    records.update_commitment(late, status="done")
    records.set_archived(friday, True)
    assert prio(late) is None and prio(friday) is None


def test_project_and_meeting_make_it_sooner(tuesday):
    pid = projects.add(title="Запуск склада", due="2026-11-16")
    cid = task("Регламент приёмки")
    records.set_project(cid, pid)
    assert prio(cid)["key"] == "normal" and prio(cid)["why"] == "проект «Запуск склада» до 16.11"
    priorities.set_importance(cid, "high")
    assert prio(cid)["key"] == "high"


def test_meeting_soon_is_burning():
    """Встреча по задаче не позже следующего рабочего дня — горит (время — настоящее: «прошла ли» считается по часам)."""
    cid = task("Подготовить цифры к встрече", importance="high")
    start = datetime.now(timezone.utc) + timedelta(hours=1)
    task_meetings.link(cid, "uid-1", title="Бюджет", start=store.to_utc_iso(start),
                       end=store.to_utc_iso(start + timedelta(hours=1)))
    p = prio(cid)
    assert p["key"] == "urgent" and "встреча по задаче" in p["why"]


def test_waiting_tasks_and_what_they_wait_for(tuesday):
    # моя срочная задача ждёт часть коллеги: сама — не выше «Обычного», а то, чего она ждёт, — «Срочно»
    report = task("Отчёт собственнику", due="2026-11-10", importance="high")
    data = task("Выгрузка из CRM", direction="to_me", who="Пётр")
    records.add_wait(report, waits_for=data)
    assert prio(report)["key"] == "normal" and "ждёт «Выгрузка из CRM»" in prio(report)["why"]
    assert prio(data)["key"] == "urgent" and "без неё не сделать «Отчёт собственнику» (срочно)" in prio(data)["why"]
    # задачу ждут другие — важность на ступень выше: без срока — «Высокий»
    base = task("Шаблон договора")
    task_waits = task("Договор с клиентом", due="2026-11-30")
    records.add_wait(task_waits, waits_for=base)
    assert prio(base)["key"] == "high" and "её ждёт ваша задача" in prio(base)["why"]
    # часть передана коллеге: основная ждёт, а часть — не ниже основной
    deck = task("Презентация совету", due="2026-11-10", importance="high")
    slides = records.add_commitment(direction="to_me", who="Ирина", title="Слайды по рынку", what="Слайды",
                                    parent_id=deck)
    assert prio(deck)["key"] == "normal" and "передано: Ирина" in prio(deck)["why"]
    assert prio(slides)["key"] == "urgent" and "часть задачи «Презентация совету»" in prio(slides)["why"]
    # спросили и ждём ответа — тоже «сейчас не продвинуть»
    asked = task("Согласовать смету", due="2026-11-09")
    worklog.add(asked, "Спросили Олега, ждём ответа", kind="ask")
    assert prio(asked)["key"] == "normal" and "ответа" in prio(asked)["why"]


def test_colleagues_agreements_stay_normal_unless_you_need_them(tuesday):
    theirs = task("Графики на декабрь", direction="other", who="Пётр", due="2026-11-02", importance="high")
    assert prio(theirs)["key"] == "normal"  # просрочено и важно — но это не ваша задача
    mine = task("Сводка графиков", due="2026-11-10", importance="high")
    records.add_wait(mine, waits_for=theirs)
    assert prio(theirs)["key"] == "urgent"  # ваша срочная задача без неё не сделать


def test_manual_level_sticks_and_auto_returns(tuesday):
    cid = task("Заявка на отпуск", due="2026-11-09", importance="low")
    assert prio(cid)["key"] == "normal"
    assert priorities.set_level(cid, "urgent") and not priorities.set_level(cid, "urgent")
    p = prio(cid)
    assert (p["key"], p["manual"], p["auto"], p["auto_title"]) == ("urgent", True, "normal", "Обычный")
    priorities.set_importance(cid, "high")  # оценка Claude ручной уровень не меняет
    assert prio(cid)["key"] == "urgent"
    priorities.set_level(cid, None)
    assert prio(cid)["key"] == "urgent" and not prio(cid)["manual"]  # авто: просрочено и теперь важная
    log = [e["text"] for e in records.get_commitment(cid)["log"]]
    assert "Приоритет: Срочно — поставили сами" in log and "Приоритет снова считается сам" in log
    with pytest.raises(ValueError):
        priorities.set_level(cid, "top")
    assert priorities.parse_level("Срочно") == "urgent" and priorities.parse_level("auto") is None


def test_priority_sort(tuesday):
    """«В работе» — сверху, между собой по уровню; остальные — по сроку, в один день — по уровню; без срока — в конце."""
    far = task("Через месяц", due="2026-12-10")
    no_due = task("Без срока, срочная")
    priorities.set_level(no_due, "urgent")
    tomorrow = task("Завтра обычная", due="2026-11-11")
    tomorrow_high = task("Завтра важная", due="2026-11-11", importance="high")  # скоро × важная — Высокий
    tomorrow_urgent = task("Завтра срочная", due="2026-11-11")
    priorities.set_level(tomorrow_urgent, "urgent")
    low = task("Неважная сегодня", due="2026-11-10", importance="low")
    urgent = task("Горит", due="2026-11-10", importance="high")
    late = task("Просрочена", due="2026-11-05")
    today = task("Сегодня", due="2026-11-10")
    work_far = task("В работе, срочная через месяц", due="2026-12-10")
    priorities.set_level(work_far, "urgent")
    work_today = task("В работе, сегодня неважная", due="2026-11-10", importance="low")
    for cid in (work_today, work_far):
        records.set_in_work(cid, True)
    order = [c["id"] for c in records.list_commitments(direction="mine", sort="priority")]
    assert order == [work_far, work_today,  # закреплённые: Срочно выше Обычного, хоть срок и позже
                     late,  # просрочена — раньше всех по сроку
                     urgent, today, low,  # сегодня: Срочно → Высокий → Обычный
                     tomorrow_urgent, tomorrow_high, tomorrow,  # завтра: так же
                     far, no_due]  # дальше по сроку, без срока — в конце
    # закрытые — после открытых
    records.update_commitment(late, status="done")
    assert [c["id"] for c in records.list_commitments(status="all", direction="mine", sort="priority")][-1] == late
    # «по сроку» осталось для выборок самого PULSE: «в работе» первыми
    assert records.list_commitments(direction="mine", sort="due")[0]["id"] in (work_far, work_today)
    assert list(records.SORTS) == ["priority", "created"]


def test_board_chip_menu_and_dialog_row(client, tuesday):
    urgent = task("Горящий отчёт", due="2026-11-10", importance="high")
    plain = task("Обычная задача", due="2026-12-10")
    page = client.get("/commitments").text
    assert page.index("Горящий отчёт") < page.index("Обычная задача")  # по умолчанию — по приоритету
    assert 'class="prio p-urgent"' in page and 'class="prio p-normal"' in page
    assert 'title="Срочно — авто: срок сегодня · высокая важность: почему Горящий отчёт. Щёлкните, чтобы изменить"' in page
    assert f'action="/commitments/{plain}/priority"' in page and ">Авто · Обычный" in page
    assert 'class="est-row prio-row"' in page and '<input type="hidden" name="at" value="dlg">' in page
    # из меню на карточке — назад на доску; из окна — окно остаётся открытым
    r = client.post(f"/commitments/{plain}/priority", data={"level": "low"},
                    headers={"referer": "http://localhost:8765/commitments?view=board"}, follow_redirects=False)
    assert r.status_code == 303 and "task=" not in r.headers["location"] and "msg=" in r.headers["location"]
    assert prio(plain)["key"] == "low" and prio(plain)["manual"]
    page = client.get("/commitments").text
    assert 'class="prio p-low manual"' in page and "поставили вы" in page
    r = client.post(f"/commitments/{plain}/priority", data={"level": "auto", "at": "dlg"},
                    headers={"referer": "http://localhost:8765/commitments?view=board"}, follow_redirects=False)
    assert f"task={plain}" in r.headers["location"] and not prio(plain)["manual"]
    r = client.post(f"/commitments/{plain}/priority", data={"level": "top"},
                    headers={"referer": "http://localhost:8765/commitments"}, follow_redirects=False)
    assert "err=" in r.headers["location"]
    # список: чип только у необычных; выбранный порядок запоминается
    listing = client.get("/commitments?view=list").text
    assert 'class="prio p-urgent"' in listing and 'class="prio p-normal"' not in listing
    client.get("/commitments?sort=created")
    assert client.cookies.get("task_order") == "created"
    assert '<option value="created" selected>' in client.get("/commitments").text  # запомнилось


def test_old_remembered_sort_resets_to_priority(client, tuesday):
    """Порядок, запомненный до 2.0.26 («сначала новые»), не действует: по умолчанию — по приоритету."""
    urgent = task("Горящий отчёт", due="2026-11-10", importance="high")
    task("Обычная задача", due="2026-12-10")
    client.cookies.set("task_sort", "created")
    r = client.get("/commitments")
    assert r.text.index("Горящий отчёт") < r.text.index("Обычная задача")
    assert 'task_sort=""' in r.headers.get("set-cookie", "") and urgent


def test_cli_importance_priority_and_list(capsys, tuesday):
    from opsdash import cli

    a = task("Бюджет собственнику", due="2026-11-10")
    b = task("Заявка на пропуск", due="2026-11-12")
    capsys.readouterr()
    cli.main(["commitments", "importance"])
    out = capsys.readouterr().out
    assert f"[{a}] «Бюджет собственнику»" in out and f"[{b}]" in out
    cli.main(["commitments", "importance", "--set", f"{a}=high:обещано собственнику; [{b}]=low"])
    assert "Важность сохранена: 2" in capsys.readouterr().out
    cli.main(["commitments", "importance"])
    assert "важность оценена" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        cli.main(["commitments", "importance", "--set", f"{a}=огромная"])
    cli.main(["commitments", "list", "--direction", "mine", "--sort", "priority"])
    lines = capsys.readouterr().out.strip().splitlines()
    assert lines[0].startswith(f"[{a}]") and "приоритет Срочно — срок сегодня · высокая важность: обещано собственнику" in lines[0]
    assert "важность" not in lines[0].split("высокая важность")[1]  # без повтора
    cli.main(["commitments", "set", str(b), "--priority", "срочно"])
    assert "Приоритет: Срочно — закреплён" in capsys.readouterr().out
    cli.main(["commitments", "show", str(b)])
    assert "приоритет Срочно (поставлен директором) · важность низкая" in capsys.readouterr().out
    cli.main(["commitments", "set", str(b), "--priority", "auto"])
    assert "снова считается сам" in capsys.readouterr().out
    cli.main(["commitments", "add", "--direction", "to_me", "--who", "Пётр", "--title", "Договор клиенту",
              "--what", "Прислать договор клиенту", "--importance", "high", "--importance-why", "клиент ждёт"])
    assert "Важность: высокая · приоритет сейчас: Высокий" in capsys.readouterr().out


def test_importance_job_and_schedule(monkeypatch):
    from opsdash import brain

    assert skills.skill_for_job("importance") == "priorities"  # «Ваши правила» навыка попадут в запуск
    assert priorities.job_importance() == "оценивать нечего"
    cid = task("Прогноз по клиентам")
    task("Чужая договорённость", direction="other", who="Пётр")  # между коллегами — не оцениваем
    assert [r["id"] for r in priorities.unassessed()] == [cid]
    runs = []

    def fake_run(prompt, **kw):
        runs.append((prompt, kw))
        priorities.set_importance(cid, "high", "клиенты")

    monkeypatch.setattr(brain, "run", fake_run)
    assert priorities.job_importance() == "оценено задач: 1 из 1"
    prompt, kw = runs[0]
    assert kw["job"] == "importance" and kw["model"] == "haiku" and "навыку priorities" in prompt
    assert "./ops commitments importance --set" in prompt

    task("Ещё одна")
    started = []
    monkeypatch.setattr(jobs, "submit", lambda name, kind="light", args=None: started.append(name) or True)
    monkeypatch.setattr(jobs, "in_work_hours", lambda *a, **k: True)
    jobs.tick()
    assert "importance" in started
    started.clear()
    store.execute("INSERT INTO jobs(name, last_run) VALUES('importance', ?)", (store.utcnow(),))
    jobs.tick()
    assert "importance" not in started  # не чаще раза в два часа
