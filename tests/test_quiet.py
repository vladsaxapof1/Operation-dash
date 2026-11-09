"""Задачи без движения (opsdash.quiet): порог в рабочих днях, что считается движением, что не отмечается, «Не трогать
неделю», метка на карточке, кнопка и окно разбора в «Задачах», строка на Сводке, ./ops и настройка."""

from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from opsdash import cli, config, overview, quiet, records, store, worklog

NOW = datetime(2026, 11, 17, 12, 0)  # вторник; понедельник 09.11 — шесть рабочих дней назад


@pytest.fixture
def tuesday(monkeypatch):
    monkeypatch.setattr(config, "now", lambda s=None: NOW.replace(tzinfo=config.tz(s)))


@pytest.fixture
def client():
    from opsdash.web.app import app

    return TestClient(app, base_url="http://localhost:8765")


def task(title, *, direction="mine", who=None, due="2026-12-01", created="2026-11-09T09:00:00+00:00"):
    cid = records.add_commitment(direction=direction, title=title, what=title, due=due, who=who,
                                 whom="Олег" if direction == "mine" else None)
    store.execute("UPDATE commitments SET created=?, seen_at=? WHERE id=?", (created, created, cid))
    store.execute("UPDATE task_log SET at=? WHERE task_id=?", (created, cid))  # события при создании — тогда же
    return cid


def q(cid):
    return records.get_commitment(cid)["quiet"]


def back_date(cid, at):
    """События PULSE пишутся «сейчас» — переносим последнюю запись на нужный день."""
    store.execute("UPDATE task_log SET at=? WHERE id=(SELECT MAX(id) FROM task_log WHERE task_id=?)", (at, cid))


def test_threshold_in_workdays_and_by_direction(tuesday):
    mine = task("Бюджет склада")  # 6 рабочих дней — порог 5
    fresh = task("Свежая", created="2026-11-11T09:00:00+00:00")  # 4 рабочих дня (выходные не в счёт)
    owed = task("Прайс от Олега", direction="to_me", who="Олег", created="2026-11-12T09:00:00+00:00")  # 3 — порог 3
    theirs = task("Графики", direction="other", who="Пётр")
    got = q(mine)
    assert got["workdays"] == 6 and got["days"] == 8 and got["origin"] and got["since"] == "2026-11-09"
    assert q(fresh) is None and q(owed)["workdays"] == 3 and q(theirs) is None
    assert quiet.line(records.get_commitment(mine)) == "без движения 8 дн. — появилась 09.11, хода не было"
    # порог из настроек; 0 — не отмечать
    config.update_settings("tasks", {"quiet_mine": 7, "quiet_to_me": 0})
    assert q(mine) is None and q(owed) is None
    config.update_settings("tasks", {"quiet_mine": 4})
    assert q(fresh)["workdays"] == 4


def test_what_counts_as_movement(tuesday):
    cid = task("Договор с поставщиком")
    worklog.add(cid, "Пётр: договор на подписи", at="2026-11-10 10:00", by="claude")
    assert q(cid)["text"] == "Пётр: договор на подписи" and q(cid)["days"] == 7
    # перенос срока и приоритет — не движение
    records.set_due(cid, "2026-12-10")
    back_date(cid, "2026-11-16T10:00:00+00:00")
    assert q(cid)["text"] == "Пётр: договор на подписи"
    # взяли в работу — движение
    records.set_in_work(cid, True)
    back_date(cid, "2026-11-16T10:00:00+00:00")
    assert q(cid) is None
    # комментарий директора — тоже
    other = task("Отчёт собственнику")
    worklog.add(other, "Жду цифры от бухгалтерии до пятницы", kind="note", at="2026-11-16 10:00", by="director")
    assert q(other) is None


def test_waiting_tasks_are_not_quiet(tuesday):
    asked = task("Схемы склада", direction="to_me", who="Дмитрий")
    worklog.add(asked, "Спросили Дмитрия, когда будут схемы", kind="ask", at="2026-11-09 10:00", by="claude")
    assert q(asked) is None  # у неё своё «ответа нет 8 дней»
    blocked = task("Описание подрядчику")
    records.add_wait(blocked, waits_for=asked)
    assert q(blocked) is None  # ждёт другую задачу — сейчас не продвинуть
    parent = task("Отчёт по продажам")
    kid = task("Выгрузка", direction="to_me", who="Пётр")
    records.update_commitment(kid, parent_id=parent)
    assert q(parent) is None and q(kid) is not None  # часть передана — стоит она, а не основная
    done = task("Закрытая")
    records.update_commitment(done, status="done")
    assert q(done) is None


def test_snooze_and_queue(tuesday):
    a = task("Давняя", created="2026-11-02T09:00:00+00:00")
    b = task("Поновее", direction="to_me", who="Олег")
    assert [c["id"] for c in quiet.queue()] == [a, b]  # дольше всех стоящие — сверху
    assert quiet.snooze(a) == "2026-11-24"
    assert q(a) is None and [c["id"] for c in quiet.queue()] == [b]
    assert any(e["text"] == "Без движения: не напоминать до 24.11" for e in records.get_commitment(a)["log"])
    store.execute("UPDATE commitments SET quiet_until='2026-11-16' WHERE id=?", (a,))
    assert q(a) is not None  # неделя прошла — снова отмечается
    assert quiet.queue()[1]["remind"] is not None  # «Напомнить» в окне разбора — у «мне должны»


def test_board_filter_mark_dialog_and_actions(client, tuesday):
    still = task("Бюджет склада")
    owed = task("Прайс от Олега", direction="to_me", who="Олег")
    moving = task("Живая задача")
    worklog.add(moving, "Вчера созвонились", at="2026-11-16 10:00", by="claude")
    board = client.get("/commitments").text
    assert board.count('class="quiet-mark"') == 2 and "без движения 8 дн." in board
    assert '<span class="badge">2</span>' in board.split("Без движения")[1][:300]  # кнопка со счётчиком
    page = client.get("/commitments?quiet=1").text
    assert f'data-task="{still}"' in page and f'data-task="{owed}"' in page and f'data-task="{moving}"' not in page
    assert 'id="quiet-dlg"' in page and "Разобрать по одной" in page and 'name="quiet" value="1"' in page
    assert "1 из 2" in page and 'title="Напомнить: Олег">Напомнить</button>' in page and 'class="task full' in page
    # окно задачи: подсказка с «Не трогать неделю», остаётся открытым
    assert "quiet-hint" in client.get(f"/commitments?task={still}").text
    r = client.post(f"/commitments/{still}/quiet", data={"act": "snooze", "at": "dlg"},
                    headers={"referer": "http://localhost:8765/commitments"}, follow_redirects=False)
    assert f"task={still}" in r.headers["location"] and q(still) is None
    # окно разбора: новый срок (и неделю не отмечать), «уже сделано», ошибки — JSON
    js = {"accept": "application/json"}
    assert client.post(f"/commitments/{owed}/quiet", data={"act": "due"}, headers=js).json() == {
        "ok": False, "err": "Выберите новый срок"}
    assert client.post(f"/commitments/{owed}/quiet", data={"act": "due", "due": "2026-11-27"}, headers=js).json()["ok"]
    c = records.get_commitment(owed)
    assert c["due"] == "2026-11-27" and c["quiet"] is None and c["quiet_until"] == "2026-11-24"
    assert not any(e["text"].startswith("Без движения") for e in c["log"])  # перенос и так в ходе
    assert client.post(f"/commitments/{moving}/quiet", data={"act": "done"}, headers=js).json()["ok"]
    assert records.get_commitment(moving)["status"] == "done"
    assert client.post("/commitments/999/quiet", data={"act": "snooze"}, headers=js).json()["ok"] is False
    # записать, что сейчас — запись директора в ходе, задача больше не отмечается
    other = task("Отчёт собственнику")
    assert client.post(f"/commitments/{other}/log", data={"text": "Жду цифры"}, headers=js).json()["ok"]
    store.execute("UPDATE task_log SET at='2026-11-17T09:00:00+00:00' WHERE task_id=? AND kind='note'", (other,))
    assert q(other) is None
    assert "Без движения" not in client.get("/commitments").text.split("tgl-row")[1][:3000]  # не осталось — кнопки нет


def test_home_feed_row_and_settings(client, tuesday, monkeypatch):
    from opsdash import welcome

    monkeypatch.setattr(welcome, "should_open", lambda settings: False)
    cid = task("Бюджет склада")
    home = client.get("/").text
    assert 'data-kind="quiet"' in home and "Без движения · 1 задача" in home and 'id="quiet-dlg"' in home
    assert f'href="/commitments?task={cid}">Бюджет склада</a>' in home
    assert 'data-feed-filter="quiet">Без движения · 1' in home
    # в счётчик «Сводки» в меню не входит — как и «Уточнить срок»
    from opsdash.web.app import needs_from_you

    feed = needs_from_you(due=True)
    assert feed["counts"]["quiet"] == 1 and feed["total"] == sum(feed["counts"].values()) - feed["counts"]["due"] - 1
    page = client.get("/settings?tab=day").text
    assert "Задачи без движения" in page and 'name="quiet_mine" value="5"' in page
    client.post("/settings/quiet", data={"quiet_mine": "0", "quiet_to_me": "2"})
    assert config.load_settings()["tasks"] == {"quiet_mine": 0, "quiet_to_me": 2}
    assert q(cid) is None
    r = client.post("/settings/quiet", data={"quiet_mine": "100", "quiet_to_me": "2"}, follow_redirects=False)
    assert "err=" in r.headers["location"]


def test_cli_and_overview(capsys, tuesday):
    cid = task("Бюджет склада")
    task("Живая задача", created="2026-11-16T09:00:00+00:00")
    cli.main(["commitments", "list", "--quiet"])
    out = capsys.readouterr().out.strip().splitlines()
    assert len(out) == 1 and out[0].startswith(f"[{cid}]") and "без движения 8 дн. — появилась 09.11" in out[0]
    md = overview.overview_markdown()
    assert "## Без движения" in md and f"[commitment {cid}] Бюджет склада · Я · без движения 8 дн." in md
    assert "🐢 Без движения: 1" in overview.today_text() and "Бюджет склада — ваша, 8 дн." in overview.today_text()
