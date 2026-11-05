"""«Задачи»: договорённости между коллегами по умолчанию скрыты; «Показать: Между коллегами, Готово» и
«Кратко / Подробно» — переключатели под заголовком, которые запоминает браузер."""

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from opsdash import projects, records, worklog


@pytest.fixture
def client():
    from opsdash.web.app import app

    return TestClient(app, base_url="http://localhost:8765")


def _due(days: int) -> str:
    return (records.today_local() + timedelta(days=days)).isoformat()


def test_colleague_tasks_hidden_until_shown(client):
    records.add_commitment(direction="mine", whom="Тимур", title="Отчёт по продажам", what="Собрать отчёт", due=_due(3))
    other = records.add_commitment(direction="other", who="Пётр", whom="Мария", title="Графики на декабрь",
                                   what="Свести графики", due=_due(-2))  # просрочена — но это не ваша задача
    shut = records.add_commitment(direction="other", who="Пётр", whom="Мария", title="Старые графики", what="Графики")
    records.update_commitment(shut, status="done")

    page = client.get("/commitments").text
    assert "Отчёт по продажам" in page and "Графики на декабрь" not in page and 'data-col="other"' not in page
    assert 'style="--cols: 2"' in page
    # шапка и меню считают только то, что видно: одна активная, ничего не просрочено
    assert "1 активная" in page and "просрочен" not in page.split('<div class="tgl-row">')[0]
    assert '<span class="lbl">Задачи</span><span class="badge acc">1</span>' in page
    assert '>Между коллегами<span class="badge">1</span>' in page and 'href="?others=1&view=board"' in page
    listing = client.get("/commitments?view=list").text
    assert "Графики на декабрь" not in listing and "Между коллегами ·" not in listing
    assert "Графики на декабрь" not in client.get("/commitments?view=gantt").text
    done = client.get("/commitments?done=1").text
    assert 'data-col="done"' in done and "Старые графики" not in done and 'style="--cols: 3"' in done
    client.get("/commitments?done=0")

    # ищете сами — видно и их; не нашлись — колонки нет
    assert "Графики на декабрь" in client.get("/commitments?q=Графики").text
    assert 'data-col="other"' not in client.get("/commitments?q=Отчёт").text
    assert "Графики на декабрь" in client.get("/commitments?person=Пётр").text
    assert "Графики на декабрь" in client.get("/commitments?view=list&direction=other").text
    # ссылка на задачу коллеги открывает её окно и когда они скрыты
    assert f'id="task-{other}"' in client.get(f"/commitments?task={other}").text

    # включили — видно на доске, в списке, на Ганте, в «Готово»; выбор запомнился
    client.get("/commitments?others=1")
    page = client.get("/commitments").text
    assert 'data-col="other"' in page and "Графики на декабрь" in page and "2 активных · 1 просрочена" in page
    assert 'href="?others=0&view=board"' in page and '<span class="badge acc">2</span>' in page
    assert "Между коллегами · 1" in client.get("/commitments?view=list").text
    assert "Графики на декабрь" in client.get("/commitments?view=gantt").text
    assert "Старые графики" in client.get("/commitments?done=1").text
    assert "Графики на декабрь" not in client.get("/commitments?others=0").text  # выключили


def test_new_and_no_due_counters_skip_hidden(client):
    records.add_commitment(direction="mine", whom="Тимур", title="Отчёт", what="Отчёт", source_type="meeting")
    records.add_commitment(direction="other", who="Пётр", whom="Мария", title="Графики", what="Графики",
                           source_type="meeting")
    page = client.get("/commitments").text
    assert '>Новые<span class="badge acc" data-unseen>1</span>' in page and '>Без срока<span class="badge">1</span>' in page
    page = client.get("/commitments?others=1").text
    assert '>Новые<span class="badge acc" data-unseen>2</span>' in page and '>Без срока<span class="badge">2</span>' in page


def test_cards_short_and_full(client):
    pid = projects.add(title="Запуск склада")
    parent = records.add_commitment(direction="mine", whom="Тимур", title="Описание для подрядчика", what="Описание")
    task = records.add_commitment(direction="to_me", who="Пётр", title="Схемы процессов", what="Схемы процессов склада",
                                  due=_due(5), source_type="meeting", source_ref="2026-11-03-sklad", parent_id=parent,
                                  project_id=pid)
    first, *_ = records.add_items(task, "Приёмка; Хранение; Отгрузка")
    records.set_item_done(first)
    worklog.add(task, "Пётр прислал приёмку, остальное — к пятнице", source="manual")

    def card(page: str) -> str:
        return page.split(f'data-task="{task}"')[1].split("<dialog")[0]

    short = card(client.get("/commitments").text)  # по умолчанию — кратко: подробности по щелчку
    assert 'class="task-full"' not in short and "· 1 из 3" in short
    assert "Пётр прислал приёмку" in short.split('class="task-more"')[1]

    page = client.get("/commitments?cards=full").text
    head, more = card(page).split('class="task-more"')
    assert '<div class="task full' in page and 'class="task-full"' in head
    assert "Пётр прислал приёмку" in head and "часть" in head and "встреча 03.11" in head
    assert "«Запуск склада»" in head and "1 из 3</span>" in head and "· 1 из 3" not in head
    assert "task-dep" not in more  # по щелчку — только кнопки, без повторов
    assert 'class="task full' in client.get("/commitments").text  # запомнилось
    client.get("/commitments?cards=short")
    assert 'class="task full' not in client.get("/commitments").text
