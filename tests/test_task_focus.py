"""«Задачи»: задачи со сроком больше чем через две недели — в конце колонки доски (группы списка, Ганта)
под пунктирной плашкой «Ещё N задач со сроком после …»."""

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from opsdash import records


@pytest.fixture
def client():
    from opsdash.web.app import app

    return TestClient(app, base_url="http://localhost:8765")


def _due(days: int) -> str:
    return (records.today_local() + timedelta(days=days)).isoformat()


def _tasks():
    far = records.add_commitment(direction="mine", whom="Тимур", title="План на следующий год", what="План",
                                 due=_due(30), source_type="meeting")  # добавил Claude со встречи — новая
    edge = records.add_commitment(direction="mine", whom="Тимур", title="Отчёт за квартал", what="Отчёт", due=_due(14))
    near = records.add_commitment(direction="mine", whom="Тимур", title="Сверка с банком", what="Сверка", due=_due(2))
    no_due = records.add_commitment(direction="mine", whom="Тимур", title="Регламент склада", what="Регламент")
    busy = records.add_commitment(direction="mine", whom="Тимур", title="Бюджет на зиму", what="Бюджет", due=_due(40))
    records.set_in_work(busy, True)  # взялась за задачу — она в фокусе, как бы далеко ни был срок
    other = records.add_commitment(direction="other", who="Пётр", whom="Мария", title="Графики на декабрь",
                                   what="Графики", due=_due(20))
    return far, edge, near, no_due, busy, other


def _later_box(page: str, box: str, end: str) -> str:
    """Что спрятано под плашкой: от скрытого блока до конца группы."""
    return page.split(f'id="{box}" hidden>')[1].split(end)[0]


def test_board_folds_far_tasks_to_the_bottom(client):
    far, edge, near, no_due, busy, other = _tasks()
    after = (records.today_local() + timedelta(days=14)).strftime("%d.%m")
    for sort in ("due", "created"):
        page = client.get(f"/commitments?sort={sort}").text
        mine = page[page.index('data-col="mine"'):page.index('data-col="to_me"')]
        tile = mine.index('aria-controls="later-mine"')
        # срок через 14 дней и ближе, без срока и «в работе» — над плашкой; дальше — под ней, скрыты
        for title in ("Отчёт за квартал", "Сверка с банком", "Регламент склада", "Бюджет на зиму"):
            assert mine.index(title) < tile
        assert mine.index("План на следующий год") > tile and 'id="later-mine" hidden' in mine
        assert f"Ещё 1 задача со сроком после {after}" in mine and f"Скрыть задачи со сроком после {after}" in mine
        assert 'title="Среди них есть новые"' in mine  # новая задача среди скрытых — синяя точка на плашке
        # у каждой колонки — своя плашка
        assert 'aria-controls="later-other"' in page and 'aria-controls="later-to_me"' not in page
    records.mark_seen(far)
    assert 'title="Среди них есть новые"' not in client.get("/commitments").text.split('data-col="to_me"')[0]


def test_list_and_gantt_fold_far_tasks(client):
    far, *_ = _tasks()
    page = client.get("/commitments?view=list").text
    assert 'class="later-row"' in page and 'aria-controls="later-l1"' in page
    box = _later_box(page, "later-l1", "</tbody>")
    assert "План на следующий год" in box and "Сверка с банком" not in box
    assert 'aria-controls="later-l3"' in page  # «Между коллегами» — третья группа
    gantt = client.get("/commitments?view=gantt").text
    assert 'class="gantt-later"' in gantt and 'id="later-g-mine" hidden' in gantt
    assert f'data-task-open="{far}"' in _later_box(gantt, "later-g-mine", "</section>")


def test_search_and_due_filter_show_everything(client):
    _tasks()
    # искали или выбрали срок сами — показываем всё как есть, без плашки
    for url in ("/commitments?q=План", "/commitments?due=month", "/commitments?view=list&due=custom&due_from=2026-01-01"):
        assert "data-later" not in client.get(url).text
    assert "data-later" not in client.get("/commitments?due=none").text  # без срока дальних нет
