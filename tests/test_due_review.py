"""«Уточнить срок»: задачи без срока по одной на Сводке и в «Задачах», быстрые сроки, «оставить без срока»;
переключатели «Новые» и «Без срока» в «Задачах»."""

from datetime import date

import pytest
from fastapi.testclient import TestClient

from opsdash import records, store


@pytest.fixture
def client():
    from opsdash.web.app import app

    return TestClient(app, base_url="http://localhost:8765")


def _tasks():
    other = records.add_commitment(direction="other", who="Пётр", whom="Мария", title="Свести графики",
                                   what="Свести графики отгрузок", source_type="meeting")
    to_me = records.add_commitment(direction="to_me", who="Олег", title="Бюджет склада", what="Прислать бюджет склада", source_type="meeting")
    mine = records.add_commitment(direction="mine", whom="Тимур", title="Отчёт по продажам", what="Собрать отчёт", source_type="meeting")
    dated = records.add_commitment(direction="mine", whom="Тимур", title="План найма", what="Составить план найма",
                                   due="2026-11-20", source_type="meeting")
    return mine, to_me, other, dated


ALL = ["Сегодня", "Завтра", "Послезавтра", "До конца недели", "До конца месяца"]


@pytest.mark.parametrize("today, labels, expected", [
    (date(2026, 11, 3), ALL, ["2026-11-03", "2026-11-04", "2026-11-05", "2026-11-06", "2026-11-30"]),  # вторник
    # пятница — неделя кончается сегодня: «до конца недели» — та же дата, что «сегодня», второй кнопки нет
    (date(2026, 11, 6), ["Сегодня", "Завтра", "Послезавтра", "До конца месяца"],
     ["2026-11-06", "2026-11-07", "2026-11-08", "2026-11-30"]),
    (date(2026, 11, 7), ALL, ["2026-11-07", "2026-11-08", "2026-11-09", "2026-11-13", "2026-11-30"]),  # суббота
    (date(2026, 12, 30), ["Сегодня", "Завтра", "Послезавтра"], ["2026-12-30", "2026-12-31", "2027-01-01"]),
    (date(2027, 2, 10), ["Сегодня", "Завтра", "Послезавтра", "До конца месяца"],
     ["2027-02-10", "2027-02-11", "2027-02-12", "2027-02-28"]),
])
def test_due_choices(today, labels, expected):
    choices = records.due_choices(today)
    assert [label for label, _ in choices] == labels
    assert [d.isoformat() for _, d in choices] == expected


def test_queue_order_set_and_skip():
    mine, to_me, other, dated = _tasks()
    done = records.add_commitment(direction="mine", whom="Тимур", title="Старое", what="Старая задача")
    records.update_commitment(done, status="done")
    archived = records.add_commitment(direction="mine", whom="Тимур", title="В архиве", what="Задача в архиве")
    records.set_archived(archived, True)
    # сначала ваши задачи, потом что вам должны; между коллегами, со сроком, закрытые и архивные — не в разборе
    assert [c["id"] for c in records.due_queue()] == [mine, to_me]
    assert records.no_due_count() == 3  # а в фильтре «Без срока» договорённость коллег есть

    records.set_due(mine, "2026-11-06")
    c = records.get_commitment(mine)
    assert c["due"] == "2026-11-06" and c["seen_at"] and not c["no_due_at"]
    records.set_due(to_me, None)  # «оставить без срока» — больше не спрашивать, но в фильтре «Без срока» она есть
    c = records.get_commitment(to_me)
    assert c["due"] is None and c["no_due_at"] and c["seen_at"]
    assert records.due_queue() == [] and records.no_due_count() == 2
    with pytest.raises(ValueError):
        records.set_due(other, "06.11")


def test_home_feed_row_and_dialog(client):
    mine, to_me, other, _ = _tasks()
    page = client.get("/").text
    assert "Уточнить срок · 2 задачи без срока" in page and 'data-feed-filter="due">Уточнить срок · 2' in page
    assert "ваших — 1 · вам должны — 1</p>" in page and "между коллегами —" not in page
    assert 'id="due-dlg"' in page and f'action="/commitments/{mine}/due"' in page
    assert page.index(f"/commitments/{mine}/due") < page.index(f"/commitments/{to_me}/due")
    assert f"/commitments/{other}/due" not in page  # сроки договорённостей коллег директор не разбирает
    assert 'data-due-pick="' in page and ">Сегодня<span>" in page and ">Завтра<span>" in page
    assert "Всё · 2" in page
    # в меню у «Сводки» задачи без срока не считаются: там — то, что ждёт действия
    from opsdash.web.app import needs_from_you

    assert needs_from_you()["total"] == 0 and needs_from_you(due=True)["counts"]["due"] == 2


def test_due_endpoint_json_and_form(client):
    mine, to_me, other, _ = _tasks()
    js = {"Accept": "application/json"}
    r = client.post(f"/commitments/{mine}/due", data={"due": "2026-11-06", "act": "save"}, headers=js)
    assert r.status_code == 200 and r.json() == {"ok": True, "err": ""}
    assert records.get_commitment(mine)["due"] == "2026-11-06"
    r = client.post(f"/commitments/{to_me}/due", data={"due": "", "act": "save"}, headers=js)
    assert r.status_code == 400 and not r.json()["ok"] and "Выберите срок" in r.json()["err"]
    r = client.post(f"/commitments/{to_me}/due", data={"act": "skip"}, headers=js)
    assert r.json()["ok"] and records.get_commitment(to_me)["no_due_at"]
    r = client.post("/commitments/999/due", data={"due": "2026-11-06"}, headers=js)
    assert r.status_code == 400 and r.json()["err"] == "Такой задачи нет"
    # без скриптов — обычная форма и возврат на страницу
    r = client.post(f"/commitments/{other}/due", data={"due": "2026-11-30", "act": "save"},
                    headers={"Referer": "http://localhost:8765/"}, follow_redirects=False)
    assert r.status_code == 303 and "msg=" in r.headers["location"]
    assert records.get_commitment(other)["due"] == "2026-11-30"
    assert "Уточнить срок" not in client.get("/").text  # всё разобрано — строки в ленте нет


def test_tasks_toggles_new_and_no_due(client):
    mine, to_me, other, dated = _tasks()
    records.mark_seen(other)
    page = client.get("/commitments").text  # договорённости между коллегами скрыты — и в «Без срока» их нет
    assert ">Новые<span class=\"badge acc\" data-unseen>3</span>" in page and ">Без срока<span class=\"badge\">2</span>" in page
    assert 'href="?new=1&view=board"' in page and 'href="?due=none&view=board"' in page
    assert f'data-new="{mine}"' in page and f'data-new="{other}"' not in page  # новая — синяя точка на карточке

    new = client.get("/commitments?new=1").text
    assert 'class="fchip sm on"' in new and "Свести графики" not in new and "План найма" in new
    assert 'aria-pressed="true"' in new and 'name="new" value="1"' in new  # фильтр не теряется при поиске

    no_due = client.get("/commitments?due=none").text
    assert "План найма" not in no_due and "Разобрать сроки по одной" in no_due and 'id="due-dlg"' in no_due
    assert "Свести графики" not in no_due
    no_due = client.get("/commitments?due=none&others=1").text
    assert "Свести графики" in no_due and f'action="/commitments/{other}/due"' not in no_due  # коллег — не в разборе
    # в разборе на странице «Задач» — все показанные без срока, и те, что уже оставляли без срока
    records.set_due(to_me, None)
    no_due = client.get("/commitments?due=none&view=list").text
    assert f'action="/commitments/{to_me}/due"' in no_due and "вы уже оставляли её без срока" in no_due
    assert "Разобрать сроки" not in client.get("/commitments").text


def test_wait_mark_says_waits(client):
    mine, to_me, _, _ = _tasks()
    records.add_wait(mine, waits_for=to_me)
    page = client.get("/commitments").text
    assert 'class="task-wait"' in page and "ждёт</span>" in page
