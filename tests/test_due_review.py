"""«Уточнить срок»: задачи без срока по одной на Сводке и в «Задачах», быстрые сроки, «оставить без срока»;
переключатели «Сегодня», «Новые» и «Без срока» в «Задачах»."""

from datetime import date, timedelta

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


def test_tasks_toggle_today(client):
    """«Сегодня» — первая кнопка ряда: открытые задачи со сроком сегодня и просроченные."""
    today = records.today_local()
    records.add_commitment(direction="mine", whom="Тимур", title="Сценарий", what="Собрать сценарий",
                           due=(today - timedelta(days=2)).isoformat())
    records.add_commitment(direction="to_me", who="Пётр", title="Прогноз", what="Прислать прогноз", due=today.isoformat())
    records.add_commitment(direction="mine", whom="Анна", title="Бюджет", what="Бюджет на декабрь",
                           due=(today + timedelta(days=1)).isoformat())
    records.add_commitment(direction="mine", whom="Анна", title="Регламент", what="Написать регламент")
    records.add_commitment(direction="other", who="Олег", whom="Ирина", title="Графики", what="Графики",
                           due=today.isoformat())
    closed = records.add_commitment(direction="mine", whom="Анна", title="Отчёт", what="Отчёт", due=today.isoformat())
    records.update_commitment(closed, status="done")

    page = client.get("/commitments").text  # между коллегами скрыты — и в счётчике их нет
    row = page.split('class="tgl-row"')[1]
    assert ">Сегодня<span class=\"badge\">2</span>" in row and 'href="?due=now&view=board"' in row
    assert row.index(">Сегодня<") < row.index(">Без срока<")  # самая левая
    assert '>Сегодня<span class="badge">3</span>' in client.get("/commitments?others=1").text

    on = client.get("/commitments?due=now").text
    assert 'class="fchip sm on" href="?&view=board" aria-pressed="true"' in on  # второй щелчок снимает
    assert "Собрать сценарий" in on and "Прислать прогноз" in on
    assert "Бюджет на декабрь" not in on and "Написать регламент" not in on
    assert "срок: сегодня и просроченные" not in on  # кнопка вместо «× снять»
    assert '<option value="now" selected>' in on  # фильтр не теряется, когда меняют другие
    # «Без срока» сменяет «Сегодня», а не добавляется к нему
    assert 'href="?due=none&view=board"' in on
    listing = client.get("/commitments?due=now&view=list").text
    assert "Собрать сценарий" in listing and "Бюджет на декабрь" not in listing


def test_wait_mark_says_waits(client):
    mine, to_me, _, _ = _tasks()
    records.add_wait(mine, waits_for=to_me)
    page = client.get("/commitments").text
    assert 'class="task-wait"' in page and "ждёт</span>" in page


def test_due_moved_by_task_log(client, capsys):
    """Срок сдвинулся по ходу задачи: в переписке назвали новую дату (commitments log --new-due) или задача ждёт событие
    позже срока — PULSE срок не меняет, а предлагает новый: «Уточнить срок» на Сводке, окно задачи, отметка на доске.
    Перенести — одной кнопкой; «оставить прежний» — эту дату больше не предлагать."""
    from opsdash import cli, day

    cid = records.add_commitment(direction="to_me", who="Пётр", title="Сообщить о подписании",
                                 what="После ответа поставщика написать, когда подписываем", due="2026-11-13")
    # задача ждёт событие позже срока — подсказка сама
    records.add_wait(cid, event="ответ поставщика", until="2026-11-20", by="claude")
    c = records.get_commitment(cid)
    assert c["due"] == "2026-11-13" and c["due_hint"] == "2026-11-20" and c["due_hint_from"] == "wait"
    assert c["due_moved"] and "ждёт: ответ поставщика — к 20.11" in c["due_hint_why"]
    assert [t["id"] for t in records.due_queue()] == [cid]
    cli.main(["commitments", "show", str(cid)])
    assert "предложен новый срок 2026-11-20 (ждёт: ответ поставщика — к 20.11), ждёт директора" in capsys.readouterr().out
    # проверка окон подсказку из хода не снимает
    day.check_due(cid)
    assert records.get_commitment(cid)["due_hint"] == "2026-11-20"
    # событие случилось — подсказка ожидания уходит
    link = records.find_waits(cid, event="поставщика")[0]
    records.set_event_done(link["id"])
    assert not records.get_commitment(cid)["due_hint"] and records.due_queue() == []

    # в ходе назвали новую дату
    cli.main(["commitments", "log", str(cid), "--text", "Пётр: поставщик ответил, подписываем 18.11",
              "--new-due", "2026-11-18"])
    out = capsys.readouterr().out
    assert "Новый срок 18.11 предложен директору" in out and "отметь: ./ops commitments wait" not in out
    c = records.get_commitment(cid)
    assert (c["due_hint"], c["due_hint_from"]) == ("2026-11-18", "log") and c["due"] == "2026-11-13"
    assert c["due_hint_why"] == "Пётр: поставщик ответил, подписываем 18.11"

    # окно задачи: «Похоже, срок сдвинулся» с кнопками; доска — отметка; Сводка — строка «Уточнить срок»
    page = client.get(f"/commitments?task={cid}").text
    assert "Похоже, срок сдвинулся.</b> Пётр: поставщик ответил, подписываем 18.11. Новый срок" in page
    assert ">Перенести на 18.11</button>" in page and ">Оставить 13.11</button>" in page
    assert ">срок → 18.11?</span>" in page
    home = client.get("/").text
    assert "Уточнить срок · 1 задача</div>" in home and "срок сдвинулся — 1" in home and "срок под угрозой —" not in home
    assert "Похоже, срок 13.11 сдвинулся: Пётр: поставщик ответил, подписываем 18.11" in home and ">Новый срок<span>" in home

    # «Оставить 13.11» — дату больше не предлагаем
    client.post(f"/commitments/{cid}/due", data={"act": "skip"}, headers={"Accept": "application/json"})
    c = records.get_commitment(cid)
    assert c["due"] == "2026-11-13" and not c["due_hint"] and c["due_hint_no"] == "2026-11-18" and not c["no_due_at"]
    assert records.suggest_due(cid, "2026-11-18", "снова 18.11") == "declined"
    assert records.suggest_due(cid, "2026-11-12", "раньше") == "same"
    # другая дата — снова предложение; «Перенести» — срок новый, подсказки нет, в ходе — событие переноса
    assert records.suggest_due(cid, "2026-11-25", "Пётр: юристы просят ещё неделю") == "set"
    client.post(f"/commitments/{cid}/due", data={"due": "2026-11-25"}, headers={"Accept": "application/json"})
    c = records.get_commitment(cid)
    assert c["due"] == "2026-11-25" and not c["due_hint"] and not c["due_moved"]
    assert c["log"][-1]["text"] == "Срок: 13.11 → 25.11"
    # срок перенесли сами (редактирование) не раньше подсказки — подсказка выполнена
    records.suggest_due(cid, "2026-11-27", "ещё два дня")
    records.update_commitment(cid, due="2026-11-28")
    assert not records.get_commitment(cid)["due_hint"]

    # без срока, между коллегами, закрытая — не предложение
    no_due = records.add_commitment(direction="to_me", who="Олег", title="Прайс", what="Прислать прайс")
    other = records.add_commitment(direction="other", who="Олег", whom="Мария", title="Графики", what="Графики",
                                   due="2026-11-13")
    assert records.suggest_due(no_due, "2026-11-20", "…") == "no_due"
    assert records.suggest_due(other, "2026-11-20", "…") == "other"
    cli.main(["commitments", "log", str(no_due), "--text", "Олег пришлёт 20.11", "--new-due", "2026-11-20"])
    assert f"./ops commitments set {no_due} --due 2026-11-20" in capsys.readouterr().out
