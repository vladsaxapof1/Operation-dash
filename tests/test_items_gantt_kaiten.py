"""Пункты внутри задачи и прогресс проекта по ним; стрелки «ждёт» на Ганте; блокировки в Kaiten — поставить из
PULSE и подтянуть из Kaiten в «ждёт»."""

import pytest
from fastapi.testclient import TestClient

from opsdash import cli, config, notices, overview, projects, proposals, records, store
from opsdash.sources import kaiten
from tests.fake_kaiten import FakeKaiten

PARTS = ["Приёмка", "Хранение", "Ответственное хранение", "Комплектация", "Отгрузка", "Возвраты", "Инвентаризация",
           "Маркировка", "Утилизация"]


@pytest.fixture
def client():
    from opsdash.web.app import app

    return TestClient(app, base_url="http://localhost:8765")


@pytest.fixture
def fake(settings_with, monkeypatch):
    fk = FakeKaiten()
    settings_with(kaiten={"enabled": True, "domain": "demo.kaiten.ru",
                          "spaces": [{"id": 10, "title": "Операционка", "track": True}]})
    config.set_secret("KAITEN_TOKEN", "good-token")
    monkeypatch.setattr(kaiten, "client", lambda settings=None, transport=None: kaiten.Kaiten(
        "demo.kaiten.ru", "good-token", transport=fk.transport()))
    return fk


def test_items_progress_and_merge():
    schemes = records.add_commitment(direction="to_me", who="Дмитрий Орлов", title="Схемы процессов склада",
                                     what="Схемы процессов")
    assert len(records.add_items(schemes, PARTS)) == 9
    assert records.add_items(schemes, "Хранение; Кросс-докинг;  ") and records.get_commitment(schemes)["items_total"] == 10
    for name in PARTS[:6]:
        records.set_item_done(records.find_items(schemes, name)[0]["id"], note="Пачка 30.10")
    assert [r["text"] for r in records.find_items(schemes, "хранение")] == ["Хранение"]  # точное — только оно
    assert len(records.find_items(schemes, "хранен")) == 2  # «Хранение» и «Ответственное хранение»
    records.add_items(schemes, "Невозвратная тара")
    assert [r["text"] for r in records.find_items(schemes, "возврат")] == ["Возвраты"]  # не «Не-возврат-ная тара»
    records.remove_item(records.find_items(schemes, "Невозвратная")[0]["id"])
    c = records.get_commitment(schemes)
    assert (c["items_done"], c["items_total"]) == (6, 10) and c["share"] == 0.6 and c["checklist"][0]["note"] == "Пачка 30.10"
    with pytest.raises(ValueError):
        records.add_items(999, "x")

    pid = projects.add(title="Запуск склада")
    done = records.add_commitment(direction="mine", whom="Дмитрий", what="Передать архив", project_id=pid)
    records.update_commitment(done, status="done")
    records.set_project(schemes, pid)
    empty = records.add_commitment(direction="mine", whom="Подрядчик", what="Описание подрядчику", project_id=pid)
    p = projects.get(pid)
    assert (p["done"], p["total"], p["percent"], p["partial"]) == (1, 3, 53, 1)  # (1 + 0,6 + 0) / 3

    # дубль с пунктами: пункты переходят, такие же не повторяются
    dup = records.add_commitment(direction="to_me", who="Дмитрий Орлов", what="Процессы по карте склада")
    records.add_items(dup, ["Приёмка", "Повторная приёмка"])
    records.merge_commitments(dup, schemes)
    assert [r["text"] for r in records.get_commitment(schemes)["checklist"]][-1] == "Повторная приёмка"
    assert records.get_commitment(schemes)["items_total"] == 11 and records.get_commitment(empty)["share"] == 0


def test_items_cli_web_and_overview(capsys, client):
    tid = records.add_commitment(direction="to_me", who="Дмитрий Орлов", title="Схемы процессов склада",
                                 what="Схемы", source_type="pachca")
    cli.main(["commitments", "items", str(tid), "--add", "Приёмка; Хранение", "--add", "Отгрузка"])
    assert "— 0 из 3:" in capsys.readouterr().out
    cli.main(["commitments", "items", str(tid), "--done", "Хранение", "--note", "прислал схему 29.10"])
    shown = capsys.readouterr().out
    assert "✓ Хранение — прислал схему 29.10" in shown and "☐ Отгрузка" in shown
    with pytest.raises(SystemExit):
        cli.main(["commitments", "items", str(tid), "--done", "Возвраты"])
    cli.main(["commitments", "items", str(tid), "--undo", "Хранение", "--remove", "Отгрузка"])
    capsys.readouterr()
    assert records.get_commitment(tid)["items_total"] == 2 and records.get_commitment(tid)["items_done"] == 0
    cli.main(["commitments", "list"])
    assert "пункты 0/2: ☐Приёмка, ☐Хранение" in capsys.readouterr().out

    r = client.post(f"/commitments/{tid}/items", data={"text": "Возвраты; Маркировка"},
                    headers={"referer": "http://localhost:8765/commitments?view=list&task=1"})
    assert f"/commitments?view=list&task={tid}&msg=" in str(r.url)  # окно задачи откроется снова
    item = records.find_items(tid, "Возвраты")[0]["id"]
    client.post(f"/commitments/{tid}/items/{item}/done", data={"on": "1"})
    assert records.get_commitment(tid)["items_done"] == 1
    page = client.get("/commitments").text
    assert 'class="task-items"' in page and "1 из 4" in page and 'class="chk"' in page and "Ещё пункт" in page
    client.post(f"/commitments/{tid}/items/{item}/remove")
    assert records.get_commitment(tid)["items_total"] == 3
    other = records.add_commitment(direction="mine", what="Без пунктов")
    assert "разбить задачу на части" in client.get(f"/commitments?task={other}").text
    assert f"[commitment {tid}]" in overview.overview_markdown() and "пункты 0/3" in overview.overview_markdown()


def test_gantt_bars_carry_waits(client):
    a = records.add_commitment(direction="to_me", who="Дмитрий", title="Схемы", what="Схемы", due="2099-11-01")
    b = records.add_commitment(direction="mine", whom="Подрядчик", title="Описание", what="Описание", due="2099-11-03")
    records.add_wait(b, waits_for=a)
    page = client.get("/commitments?view=gantt").text
    assert f'data-waits="{a}" class="gantt-bar' in page and "Стрелка — «ждёт»" in page
    records.update_commitment(a, status="done")
    assert "data-waits" not in client.get("/commitments?view=gantt").text  # дождались — стрелки нет


def test_kaiten_block_from_pulse_and_sync_back(fake, client):
    kaiten.collect()
    card_a, card_b, card_c = sorted(fake.cards)[:3]
    schemes = records.add_commitment(direction="to_me", who="Дмитрий", title="Схемы", what="Схемы", kaiten_card_id=card_a)
    contractor = records.add_commitment(direction="mine", whom="Подрядчик", title="Описание", what="Описание",
                                    kaiten_card_id=card_b)
    link = records.add_wait(contractor, waits_for=schemes)
    w = records.get_commitment(contractor)["waits"][0]
    assert w["kaiten"] == "can"
    assert "Заблокировать в Kaiten" in client.get(f"/commitments?task={contractor}").text

    # директор нажала «Заблокировать в Kaiten» — блокировка ставится сразу, зеркало обновляется
    client.post(f"/commitments/{contractor}/wait/{link}/kaiten")
    assert [(b["blocker_card_id"], b["reason"]) for b in fake.blockers[card_b]] == [(card_a, "Ждёт «Схемы»")]
    assert records.get_commitment(contractor)["waits"][0]["kaiten"] == "on"
    with pytest.raises(proposals.ProposalError, match="уже блокирует"):
        proposals.propose_kaiten_block(card_id=card_b, blocker_card=card_a)
    with pytest.raises(proposals.ProposalError):
        proposals.propose_kaiten_block(card_id=card_b)
    p = proposals.propose_kaiten_block(card_id=card_a, text="Ждём доступ к складской программе", reason="из встречи")
    assert p["status"] == "pending" and "заблокировать причиной «Ждём доступ к складской программе»" in p["title"]

    # в Kaiten поставили блокировки сами: карточкой третьей задачи и причиной текстом → «ждёт» в PULSE
    third = records.add_commitment(direction="mine", whom="Олег Соколов", title="Архив", what="Архив", kaiten_card_id=card_c)
    fake.blockers[card_c] = [
        {"id": 1, "card_id": card_c, "blocker_card_id": card_b, "blocker_card_title": "Описание", "reason": None,
         "released": False},
        {"id": 2, "card_id": card_c, "blocker_card_id": None, "reason": "Нет доступа к складской программе", "released": False},
        {"id": 3, "card_id": card_c, "blocker_card_id": None, "reason": "старое", "released": True}]
    kaiten.collect()
    waits = records.get_commitment(third)["waits"]
    assert [(w["task"]["id"] if w["task"] else w["event"], w["by_kaiten"]) for w in waits] == [
        (contractor, True), ("в Kaiten: Нет доступа к складской программе", True)]
    kaiten.collect()  # повторная синхронизация — без дублей
    assert len(records.get_commitment(third)["waits"]) == 2
    assert 'class="chip wait"' in client.get("/tasks").text and "заблокирована" in client.get("/tasks").text

    # в Kaiten сняли блокировки → связь уходит, событие «случилось», уведомление «Можно делать»
    for b in fake.blockers[card_c]:
        b["released"] = True
    kaiten.collect()
    c = records.get_commitment(third)
    assert [w["event"] for w in c["waits"]] == ["в Kaiten: Нет доступа к складской программе"] and not c["blocked"]
    assert notices.recent()[0]["title"] == "Можно делать: «Архив»"


def test_kaiten_tariff_without_blockers(fake, capsys):
    fake.blockers_tariff = False
    kaiten.collect()
    a = records.add_commitment(direction="mine", what="x", kaiten_card_id=sorted(fake.cards)[0])
    kaiten.collect()  # 402 — запоминаем и неделю не спрашиваем
    assert store.get_state("kaiten_blockers_off") and not kaiten.blockers_supported()
    calls = len(fake.requests)
    kaiten.collect()
    assert not any(path.endswith("/blockers") for _, path in fake.requests[calls:])
    with pytest.raises(SystemExit):
        cli.main(["propose", "kaiten-block", "--card", str(sorted(fake.cards)[0]), "--text", "ждём"])
    assert "тарифе" in capsys.readouterr().err
    b = records.add_commitment(direction="mine", what="y", kaiten_card_id=sorted(fake.cards)[1])
    records.add_wait(a, waits_for=b)
    assert records.get_commitment(a)["waits"][0]["kaiten"] is None  # кнопки «Заблокировать в Kaiten» нет
