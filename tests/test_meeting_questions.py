"""«Сделать задачей» у пунктов «Открытых вопросов» в разборе встречи: у вопроса есть исполнитель и результат — это
работа, а не вопрос. Пункт, который уже стал задачей, — ссылкой на неё."""
from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient

from opsdash import records, store, worklog

SUMMARY = """# Воронка и KPI

## Задачи
| Что | Кто | Срок | Kaiten |
|---|---|---|---|
| Сетка KPI с шагом 5 п.п. | Директор → Ольга | 11.11 | — |

## Договорённости
- **Директор обещала:** пересчитать сетку KPI с шагом 5 п.п. к 11.11 [00:22:33]

## Открытые вопросы
- Цель без мусорных заявок: пересчитать можно, но команда не уверена, что менеджеры не начнут сливать лиды [00:17:09] (решают Пётр и Ольга)
- Дашборд воронки (конверсия и скорость между этапами): директор хочет собрать, срок не назван [00:33:00]
  - вложенный пункт — не отдельный вопрос

## Связь с текущими задачами
- «Проверка мусорных сделок» — цель пока не пересчитываем
"""

TRANSCRIPT = """# Расшифровка: Воронка и KPI

[00:17:09] Директор: Но мы же можем исключить нецелевые, пересчитайте
[00:33:00] Директор: Я хочу дашборд собрать, который будет показывать конверсию из этапа в этап. Скорость перехода из этапа в этап.
[00:33:13] Ольга Петрова: там никакой нет скорости
"""


@pytest.fixture
def meeting():
    store.execute("INSERT INTO meetings(id, title, status, started_at) VALUES('m1', 'Воронка и KPI', 'processed', "
                  "'2026-11-10T12:43:00+00:00')")
    folder = records.meeting_dir("m1")
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "summary.md").write_text(SUMMARY, encoding="utf-8")
    (folder / "transcript.md").write_text(TRANSCRIPT, encoding="utf-8")
    return "m1"


@pytest.fixture
def client():
    from opsdash.web.app import app

    return TestClient(app, base_url="http://localhost:8765")


def test_open_questions_get_button_and_task_link(meeting):
    grid = records.add_commitment(direction="mine", whom="Ольга Петрова", title="Сетка KPI с шагом 5 п.п.",
                                  what="Пересчитать сетку KPI с шагом 5 п.п.", source_type="meeting", source_ref="m1")
    tasks = records.meeting_commitments("m1")
    text, questions = records.link_questions(records.link_summary(SUMMARY, tasks), tasks)
    lines = text.splitlines()
    goal = next(line for line in lines if line.startswith("- Цель без мусорных"))
    dash = next(line for line in lines if line.startswith("- Дашборд воронки"))
    assert goal.endswith(f"[Сделать задачей](#q-task-{lines.index(goal)})")
    assert dash.endswith(f"[Сделать задачей](#q-task-{lines.index(dash)})")
    assert "Сделать задачей" not in next(line for line in lines if "вложенный" in line), "только пункты первого уровня"
    assert text.count(f"task={grid})") == 2, "задачу из «Задач» к открытому вопросу второй раз не ставим"
    assert [q["title"] for q in questions] == ["Цель без мусорных заявок", "Дашборд воронки"]
    assert [(q["text"], q["tc"]) for q in questions] == [
        ("Цель без мусорных заявок: пересчитать можно, но команда не уверена, что менеджеры не начнут сливать лиды "
         "(решают Пётр и Ольга)", "00:17:09"),
        ("Дашборд воронки (конверсия и скорость между этапами): директор хочет собрать, срок не назван", "00:33:00")]

    # завели задачу по пункту — в следующий раз у него ссылка на неё, а не кнопка
    cid = records.question_to_task("m1", title="Дашборд воронки", what=questions[1]["text"], whom="Ольга Петрова",
                                   tc=questions[1]["tc"])
    tasks = records.meeting_commitments("m1")
    text, questions = records.link_questions(records.link_summary(SUMMARY, tasks), tasks)
    dash = next(line for line in text.splitlines() if line.startswith("- Дашборд воронки"))
    assert dash.endswith(f"[задача →](/commitments?task={cid})") and len(questions) == 1


def test_question_becomes_task_with_meeting_quote(meeting):
    cid = records.question_to_task("m1", title="Дашборд воронки", what="Дашборд: конверсия и скорость между этапами",
                                   whom="Ольга Петрова", tc="00:33:00")
    c = records.get_commitment(cid)
    assert (c["direction"], c["title"], c["whom"], c["who"], c["due"]) == ("mine", "Дашборд воронки", "Ольга Петрова",
                                                                           None, None)
    assert (c["source_type"], c["source_ref"]) == ("meeting", "m1"), "источник — встреча"
    assert c["quote"] == ("[00:33:00] Я хочу дашборд собрать, который будет показывать конверсию из этапа в этап. "
                          "Скорость перехода из этапа в этап."), "цитата — реплика из записи, без имени говорящего"
    assert c["seen_at"], "директор её уже видела — не «новая»"
    _, made = worklog.entries([cid], [worklog._origin(c)])[cid]
    assert made["kind"] == "event" and "Открытых вопросов" in made["text"]

    # отметки нет в расшифровке — задача без цитаты; пустая — ошибка
    other = records.question_to_task("m1", title="Цель без мусорных заявок", tc="00:59:59")
    assert records.get_commitment(other)["quote"] is None and records.get_commitment(other)["what"] == "Цель без мусорных заявок"
    with pytest.raises(ValueError):
        records.question_to_task("m1", title=" ", what="")
    with pytest.raises(ValueError):
        records.question_to_task("нет-такой", title="x")


def test_meeting_page_shows_button_and_creates_task(meeting, client):
    page = client.get("/meetings/m1").text
    assert 'class="q-task" href="#q-task-' in page and 'data-dlg-open="q-task-' in page
    assert page.count('<dialog class="dlg dlg-att-task" id="q-task-') == 2
    assert "Я хочу дашборд собрать" in page, "в окне — реплика из записи"
    assert 'value="Дашборд воронки"' in page, "заголовок — коротко, до скобки"

    r = client.post("/meetings/m1/question-task", data={"title": "Дашборд воронки", "what": "Конверсия по этапам",
                                                        "direction": "mine", "whom": "Ольга Петрова", "due": "2026-11-14",
                                                        "tc": "00:33:00"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/meetings/m1?msg=")
    assert "Дашборд воронки" in unquote(r.headers["location"])
    c = store.row("SELECT * FROM commitments WHERE title='Дашборд воронки'")
    assert (c["due"], c["source_ref"]) == ("2026-11-14", "m1")
    page = client.get("/meetings/m1").text
    assert page.count('<dialog class="dlg dlg-att-task" id="q-task-') == 1, "у ставшего задачей пункта окна больше нет"
    assert f'href="/commitments?task={c["id"]}"' in page

    r = client.post("/meetings/m1/question-task", data={"title": "", "what": ""}, follow_redirects=False)
    assert "err=" in r.headers["location"]
