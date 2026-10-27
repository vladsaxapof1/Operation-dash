import pytest

from opsdash import config, proposals, records, store
from opsdash.sources import kaiten
from tests.fake_kaiten import FakeKaiten


@pytest.fixture
def fake(settings_with, monkeypatch):
    fk = FakeKaiten()
    settings_with(kaiten={
        "enabled": True, "domain": "https://demo.kaiten.ru/", "spaces": [{"id": 10, "title": "Операционка", "track": True}],
        "targets": [{"name": "Операционка", "space_id": 10, "board_id": 100, "column_id": 1001, "lane_id": 501}],
        "default_target": "Операционка", "stale_days": 3,
    })
    config.set_secret("KAITEN_TOKEN", "good-token")
    monkeypatch.setattr(kaiten, "client", lambda settings=None, transport=None: kaiten.Kaiten(
        "demo.kaiten.ru", "good-token", transport=fk.transport()))
    return fk


def test_collect_paginates_and_mirrors(fake):
    result = kaiten.collect()
    assert "105" in result
    assert store.scalar("SELECT COUNT(*) FROM kaiten_cards") == 105
    assert store.get_state("kaiten_me")["id"] == 1
    card = store.row("SELECT * FROM kaiten_cards WHERE title='Карточка 3'")
    assert card["responsible_name"] == "Пётр Иванов"
    assert card["url"] == f"https://demo.kaiten.ru/space/10/card/{card['id']}"
    assert store.scalar("SELECT COUNT(*) FROM kaiten_boards") == 1
    # карточка ушла в архив → исчезает из зеркала
    gone = next(iter(fake.cards))
    fake.cards[gone]["condition"] = 2
    kaiten.collect()
    assert store.scalar("SELECT COUNT(*) FROM kaiten_cards") == 104


def test_stale_respects_days_states_and_snooze(fake):
    kaiten.collect()
    stale = kaiten.stale_cards()
    assert stale and all(c["state"] == 2 and c["idle_days"] >= 3 for c in stale)
    assert stale == sorted(stale, key=lambda c: -c["idle_days"])
    first = stale[0]["id"]
    kaiten.snooze(first, 3)
    assert first not in {c["id"] for c in kaiten.stale_cards()}
    config.update_settings("kaiten", {"stale_states": [1, 2], "stale_days": 9})
    assert {c["idle_days"] for c in kaiten.stale_cards()} == {9}


def test_bad_token_is_clear_error(settings_with):
    fk = FakeKaiten()
    kc = kaiten.Kaiten("demo.kaiten.ru", "wrong", transport=fk.transport())
    with pytest.raises(kaiten.KaitenError, match="401"):
        kc.current_user()


def test_propose_card_waits_for_approval_then_creates(fake):
    kaiten.collect()
    cid = records.add_commitment(direction="to_me", who="Пётр Иванов", what="Отчёт по возвратам")
    p = proposals.propose_kaiten_card(title="Отчёт по возвратам", responsible="Пётр", due="2026-10-30",
                                      commitment_id=cid, batch="meeting:x")
    assert p["status"] == "pending" and p["payload"]["responsible_id"] == 2
    assert "Пётр Иванов" in p["title"] and "30.10" in p["title"]
    assert ("POST", "/api/latest/cards") not in fake.requests  # ничего не создано без «Да»
    # повторное предложение — без дубля
    assert proposals.propose_kaiten_card(title="Отчёт по возвратам", responsible="Пётр", due="2026-10-30")["id"] == p["id"]

    done = proposals.approve(p["id"])
    assert done["status"] == "done" and "/card/" in done["result"]["url"]
    created = done["result"]["card_id"]
    assert fake.cards[created]["due_date"] == "2026-10-30T12:00:00.000Z"
    assert (created, 2, 2) in fake.members  # назначен ответственным
    assert records.list_commitments()[0]["kaiten_card_id"] == created
    assert proposals.approve(p["id"])["status"] == "done"  # повторное нажатие ничего не делает
    assert sum(1 for r in fake.requests if r == ("POST", "/api/latest/cards")) == 1


def test_autonomy_auto_executes_immediately(fake):
    kaiten.collect()
    config.update_settings("autonomy", {"kaiten_create_for_me": "auto"})
    p = proposals.propose_kaiten_card(title="Себе: подготовить повестку")
    assert p["status"] == "done"
    other = proposals.propose_kaiten_card(title="Марии: вакансии", responsible="Мария Кузнецова")
    assert other["status"] == "pending"  # на коллег — по-прежнему с подтверждением


def test_unknown_target_and_unresolved_person(fake):
    kaiten.collect()
    with pytest.raises(proposals.ProposalError, match="Нет направления"):
        proposals.propose_kaiten_card(title="x", target="Маркетинг")
    p = proposals.propose_kaiten_card(title="Задача для подрядчика", responsible="Подрядчик Вася")
    assert p["payload"]["responsible_unresolved"] == "Подрядчик Вася"
    assert "не найден" in proposals.describe(p)


def test_comment_update_and_failure_recorded(fake):
    kaiten.collect()
    card_id = next(iter(fake.cards))
    c = proposals.propose_kaiten_comment(card_id=card_id, text="Как дела?")
    assert "Карточка 0" in c["title"]
    assert proposals.approve(c["id"])["status"] == "done"
    assert fake.comments == [(card_id, "Как дела?")]

    u = proposals.propose_kaiten_update(card_id=card_id, due="2026-11-15", responsible="Мария")
    assert proposals.approve(u["id"])["status"] == "done"
    assert fake.cards[card_id]["due_date"].startswith("2026-11-15")

    bad = proposals.propose_kaiten_comment(card_id=999999, text="нет такой")
    res = proposals.approve(bad["id"])
    assert res["status"] == "failed" and "Не получилось" in proposals.result_text(res)


def test_batch_approve_and_reject(fake):
    kaiten.collect()
    a = proposals.propose_kaiten_card(title="A", batch="b1")
    b = proposals.propose_kaiten_card(title="B", batch="b1")
    proposals.propose_kaiten_card(title="C", batch="b2")
    results = proposals.approve_batch("b1")
    assert {r["id"] for r in results} == {a["id"], b["id"]} and all(r["status"] == "done" for r in results)
    assert [p["title"] for p in proposals.list_proposals("pending")] == [proposals.list_proposals("pending")[0]["title"]]
    assert proposals.reject(proposals.list_proposals("pending")[0]["id"])["status"] == "rejected"


def test_resolve_user_variants(fake):
    kaiten.collect()
    assert kaiten.resolve_user("@petr")["id"] == 2
    assert kaiten.resolve_user("Иванов Пётр")["id"] == 2
    assert kaiten.resolve_user("3")["id"] == 3
    records.upsert_person("Маша Кузнецова", aliases=["Маша"], kaiten_user_id=3)
    assert kaiten.resolve_user("Маша")["id"] == 3
    assert kaiten.resolve_user("Неизвестный") is None
