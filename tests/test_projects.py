"""Проекты: задачи с общим результатом — прогресс, на ком стоит, шаги цепочки; подзадачи в проекте основной
задачи; предложение «собрать проект»; вид «Проекты» и страница проекта. Пример — запуск склада в Казани."""

from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient

from opsdash import cli, jobs, office, overview, projects, proposals, records, store


@pytest.fixture
def client():
    from opsdash.web.app import app

    return TestClient(app, base_url="http://localhost:8765")


def _warehouse() -> dict:
    t = {}
    t["archive_sent"] = records.add_commitment(direction="mine", whom="Дмитрий Орлов", title="Передать папку с процессами",
                                               what="Передать Дмитрию папку с процессами", source_type="pachca")
    t["schemes"] = records.add_commitment(direction="to_me", who="Дмитрий Орлов", title="Схемы процессов склада",
                                          what="Схемы процессов", due="2099-10-30", source_type="pachca")
    t["fix"] = records.add_commitment(direction="to_me", who="Дмитрий Орлов", title="Записать замечание по приёмке",
                                      what="Записать замечание", source_type="pachca")
    t["answers"] = records.add_commitment(direction="mine", whom="Дмитрий Орлов", title="Ответить на вопросы по схемам",
                                          what="Ответить на вопросы", source_type="pachca")
    t["contractor"] = records.add_commitment(direction="mine", whom="Подрядчик", title="Описание процесса подрядчику",
                                         what="Передать описание", due="2099-10-30", source_type="pachca")
    t["archive"] = records.add_commitment(direction="mine", whom="Олег Соколов", title="Схемы в карточку проекта",
                                          what="Загрузить архив", due="2099-10-30", source_type="meeting")
    t["agents"] = records.add_commitment(direction="other", who="Мария Кузнецова, Пётр Иванов", whom="Олег Соколов",
                                         title="План найма по этапам запуска", what="План найма", due="2099-11-04")
    records.update_commitment(t["archive_sent"], status="done")
    records.add_wait(t["answers"], waits_for=t["schemes"])
    records.add_wait(t["contractor"], waits_for=t["schemes"])
    records.add_wait(t["contractor"], waits_for=t["answers"])
    records.add_wait(t["archive"], waits_for=t["contractor"])
    records.add_wait(t["agents"], waits_for=t["contractor"])
    return t


def test_progress_steps_and_stuck_on():
    t = _warehouse()
    pid = projects.add(title="Запуск склада в Казани", goal="Склад работает по новым процессам")
    with pytest.raises(ValueError, match="уже есть"):
        projects.add(title="запуск склада в казани")
    projects.assign(pid, list(t.values()))
    other = records.add_commitment(direction="mine", whom="Пётр Иванов", what="Вне проекта")

    p = projects.get(pid)
    assert (p["done"], p["total"], p["percent"]) == (1, 7, 14)
    steps = [[records.headline(c) for c in s] for s in p["steps"]]
    assert steps == [["Схемы процессов склада", "Записать замечание по приёмке"],
                     ["Ответить на вопросы по схемам"], ["Описание процесса подрядчику"],
                     ["Схемы в карточку проекта", "План найма по этапам запуска"]]
    assert [(s["who"], s["task"]["id"]) for s in p["stuck_on"]] == [("Дмитрий Орлов", t["schemes"])]
    assert [w["id"] for w in p["stuck_on"][0]["waiting"]] == [t["answers"], t["contractor"]]
    assert [c["id"] for c in p["finished"]] == [t["archive_sent"]] and p["blocked"] == 4

    # отменённые и дубли не считаются, открытые в архиве — тоже
    records.merge_commitments(t["fix"], t["schemes"])
    records.set_archived(t["agents"], True)
    p = projects.get(pid)
    assert (p["done"], p["total"]) == (1, 5) and [c["id"] for c in p["parked"]] == [t["agents"]]
    records.update_commitment(t["schemes"], status="done")
    p = projects.get(pid)
    assert p["percent"] == 40 and records.headline(p["steps"][0][0]) == "Ответить на вопросы по схемам"

    assert [c["id"] for c in records.list_commitments(project="none")] == [other]
    assert {c["id"] for c in records.list_commitments(project=pid, status="all")} >= {t["answers"], t["contractor"]}
    assert records.get_commitment(t["answers"])["project"]["title"] == "Запуск склада в Казани"


def test_subtasks_follow_project_and_outside_waits():
    pid = projects.add(title="Регламент продаж на ноябрь")
    main = records.add_commitment(direction="mine", whom="Пётр Иванов", what="Регламент и открытые вопросы продаж",
                                  project_id=pid)
    kid = records.add_commitment(direction="to_me", who="Мария", what="Сверка по регионам", parent_id=main)
    assert records.get_commitment(kid)["project_id"] == pid  # подзадача — в проекте основной
    loose = records.add_commitment(direction="to_me", who="Пётр", what="Выгрузка")
    records.set_parent(loose, main)
    assert records.get_commitment(loose)["project_id"] == pid
    margin = records.add_commitment(direction="mine", whom="Пётр Иванов", what="Бюджет склада на ноябрь", project_id=pid)
    records.add_wait(margin, event="встреча Олега с собственником", until="2099-11-02")
    p = projects.get(pid)
    # основная ждёт подзадачи; ждущая событие — не на первом шаге
    assert [sorted(c["id"] for c in s) for s in p["steps"]] == [sorted([kid, loose]), sorted([main, margin])]

    other = projects.add(title="Бюджет 2027")
    records.set_project(main, other)  # подзадачи переходят вместе с основной
    assert {records.get_commitment(i)["project_id"] for i in (main, kid, loose)} == {other}
    records.update_commitment(main, project_id=0)
    assert records.get_commitment(kid)["project_id"] is None
    with pytest.raises(ValueError, match="Нет проекта"):
        records.add_commitment(direction="mine", what="x", project_id=999)

    projects.update(pid, status="done", due="2099-11-30", goal="")
    assert store.row("SELECT status, closed_at, goal FROM projects WHERE id=?", (pid,))["closed_at"]
    assert [p["id"] for p in projects.list_projects()] == [other]
    with pytest.raises(ValueError):
        projects.update(pid, status="closed")
    projects.delete(other)
    assert records.get_commitment(margin)["project_id"] == pid and not projects.get(other)


def test_propose_project_and_cli(capsys, monkeypatch):
    t = _warehouse()
    with pytest.raises(proposals.ProposalError, match="две задачи"):
        proposals.propose_project(title="Запуск склада", tasks=[t["schemes"]], reason="x")
    with pytest.raises(proposals.ProposalError, match="Нет задач"):
        proposals.propose_project(title="Запуск склада", tasks=[t["schemes"], 999], reason="x")
    with pytest.raises(proposals.ProposalError, match="reason"):
        proposals.propose_project(title="Запуск склада", tasks=[t["schemes"], t["contractor"]])
    cli.main(["propose", "project", "--title", "Запуск склада в Казани", "--goal", "Склад работает по новым процессам",
              "--tasks", ",".join(str(i) for i in t.values()), "--reason", "Одна цепочка: схемы → описание → архив",
              "--batch", "projects:2026-11-02"])
    assert "Проект «Запуск склада в Казани»: 7 задач" in capsys.readouterr().out
    p = proposals.list_proposals()[0]
    assert "Описание процесса подрядчику — Я → Подрядчик" in proposals.describe(p)
    done = proposals.approve(p["id"])
    pid = done["result"]["project_id"]
    assert done["status"] == "done" and ("Проект", f"/projects/{pid}") in proposals.links(done)
    assert projects.get(pid)["total"] == 7 and store.scalar("SELECT added_by FROM projects WHERE id=?", (pid,)) == "claude"
    with pytest.raises(proposals.ProposalError, match="уже есть"):
        proposals.propose_project(title="Запуск склада в Казани", tasks=[t["schemes"], t["fix"]], reason="x")
    no = proposals.propose_project(title="Разное с Дмитрием", tasks=[t["schemes"], t["fix"]], reason="один человек")
    proposals.reject(no["id"])
    with pytest.raises(proposals.ProposalError, match="«Нет»"):
        proposals.propose_project(title="разное с Дмитрием", tasks=[t["schemes"], t["fix"]], reason="снова")

    cli.main(["projects", "list"])
    line = capsys.readouterr().out
    assert f"[{pid}] «Запуск склада в Казани» — 1 из 7 (14%)" in line
    assert f"стоит на: Дмитрий Орлов — [{t['schemes']}] «Схемы процессов склада»" in line
    cli.main(["projects", "show", str(pid)])
    shown = capsys.readouterr().out
    assert "Шаг 1 — можно делать сейчас" in shown and "Шаг 4" in shown and "Выполнено:" in shown
    cli.main(["commitments", "list"])
    assert f"проект [{pid}] «Запуск склада в Казани»" in capsys.readouterr().out

    extra = records.add_commitment(direction="mine", whom="Пётр Иванов", what="Показать схемы Петру")
    cli.main(["commitments", "set", str(extra), "--project", str(pid)])
    assert records.get_commitment(extra)["project_id"] == pid
    cli.main(["projects", "set", str(pid), "--due", "2099-11-15", "--owner", "Анна", "--status", "paused"])
    assert store.row("SELECT due, owner, status FROM projects WHERE id=?", (pid,)) == {
        "due": "2099-11-15", "owner": "Анна", "status": "paused"}
    capsys.readouterr()
    cli.main(["projects", "add", "--title", "Найм в регион", "--tasks", str(extra)])
    assert "создан, задач: 1" in capsys.readouterr().out
    monkeypatch.setenv("OPSDASH_AGENT", "1")
    with pytest.raises(SystemExit):
        cli.main(["projects", "add", "--title", "Ещё проект"])
    assert "propose project" in capsys.readouterr().err


def test_web_projects_view_page_and_dialog(client):
    t = _warehouse()
    r = client.post("/projects", data={"title": "Запуск склада в Казани", "goal": "Склад работает по новым процессам"})
    pid = store.scalar("SELECT id FROM projects")
    assert f"/projects/{pid}" in str(r.url)
    for tid in t.values():
        client.post(f"/projects/{pid}/tasks", data={"task": f"#{tid} · задача"})
    assert "Выберите задачу" in unquote(str(client.post(f"/projects/{pid}/tasks", data={"task": "Схемы"}).url))

    listing = client.get("/commitments?view=projects").text
    assert "Запуск склада в Казани" in listing and "14%" in listing and "стоит на" in listing
    assert "Собрать проекты с Claude" in listing and 'name="person"' not in listing  # фильтры доски не нужны

    page = client.get(f"/projects/{pid}").text
    assert "Можно делать сейчас" in page and "Шаг 4" in page and "Выполнено" in page
    assert "Дмитрий Орлов" in page and 'class="chip proj"' not in page  # на странице проекта — без метки проекта
    assert f'id="task-{t["contractor"]}"' in page

    assert 'class="chip proj"' in client.get("/commitments").text
    board = client.get(f"/commitments?project={pid}").text
    assert 'class="chip proj"' not in board and "Проект: Запуск склада" in board  # фильтр по проекту — метка лишняя
    assert "Вне проектов" in board
    assert f'value="#{pid} · Запуск склада в Казани"' in board  # поле «Проект» в окне задачи
    assert client.get("/api/project-options").json() == [f"#{pid} · Запуск склада в Казани"]
    records.add_commitment(direction="mine", whom="Пётр Иванов", title="Задача вне проекта", what="Вне проекта")
    gantt = client.get(f"/commitments?view=gantt&project={pid}").text
    assert "Описание процесса подрядчику" in gantt and "Задача вне проекта" not in gantt

    # правка задачи: убрать из проекта и вернуть, поле не прислали — не трогаем
    form = {"title": "Ответить на вопросы", "what": "Ответить", "direction": "mine", "whom": "Дмитрий Орлов"}
    client.post(f"/commitments/{t['answers']}/edit", data={**form, "has_project": "1", "project": ""})
    assert records.get_commitment(t["answers"])["project_id"] is None
    client.post(f"/commitments/{t['answers']}/edit", data={**form, "has_project": "1", "project": f"#{pid} · Запуск"})
    client.post(f"/commitments/{t['answers']}/edit", data=form)
    assert records.get_commitment(t["answers"])["project_id"] == pid
    client.post("/commitments", data={"direction": "mine", "title": "Показать Петру", "project": str(pid)})
    assert projects.get(pid)["total"] == 8

    client.post(f"/projects/{pid}/edit", data={"title": "Запуск склада", "goal": "", "owner": "Анна",
                                               "due": "2099-11-15", "card": "#4343"})
    assert store.row("SELECT title, goal, kaiten_card_id FROM projects WHERE id=?", (pid,)) == {
        "title": "Запуск склада", "goal": None, "kaiten_card_id": 4343}
    client.post(f"/projects/{pid}/status", data={"status": "done"})
    assert "Показать завершённые" in client.get("/commitments?view=projects").text
    assert "Запуск склада" in client.get("/commitments?view=projects&done_projects=1").text
    client.post(f"/projects/{pid}/delete")
    assert not projects.get(pid) and records.get_commitment(t["contractor"])["project_id"] is None
    assert client.get("/commitments?view=projects").status_code == 200


def test_overview_bot_and_job():
    t = _warehouse()
    pid = projects.add(title="Запуск склада")
    projects.assign(pid, list(t.values()))
    text = overview.overview_markdown()
    assert f"- [project {pid}] Запуск склада — 1 из 7 (14%)" in text and f"стоит на: [commitment {t['schemes']}]" in text
    assert f"проект [project {pid}]" in text
    assert "📁 Проекты: 1" in overview.today_text() and "стоит на: Дмитрий Орлов" in overview.today_text()
    assert "projects" in jobs.REGISTRY and "projects" in jobs.TITLES
    assert "projects" in next(d for d in office.DESKS if d["key"] == "triage")["jobs"]
