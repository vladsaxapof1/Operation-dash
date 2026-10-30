"""«Ждёт»: задачу нельзя закончить, пока не готова другая задача или не случилось событие; объединение дублей;
похожие задачи при добавлении. Пример — запуск склада: схемы Дмитрия → ответы на вопросы → описание
подрядчику → схемы в карточку проекта, а «Процессы по карте склада» — дубль схем."""

from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient

from opsdash import cli, notices, overview, proposals, records, store


@pytest.fixture
def client():
    from opsdash.web.app import app

    return TestClient(app, base_url="http://localhost:8765")


def _warehouse():
    schemes = records.add_commitment(direction="to_me", who="Дмитрий Орлов", title="Схемы процессов склада",
                                     what="Нарисовать схемы процессов склада: приёмка, хранение, отгрузка; собрать в "
                                          "папку регламенты", due="2026-10-30", source_type="pachca", source_ref="101")
    dup = records.add_commitment(direction="to_me", who="Дмитрий Орлов", title="Процессы по карте склада",
                                 what="Изучить карту склада, затем описать процессы по ней: приёмка "
                                      "и отгрузка в первую очередь", source_type="pachca", source_ref="102",
                                 quote="сначала изучаем карту, потом рисуем")
    answers = records.add_commitment(direction="mine", whom="Дмитрий Орлов", title="Ответить на вопросы по схемам",
                                     what="Забрать папку и ответить на открытые вопросы", source_type="pachca")
    contractor = records.add_commitment(direction="mine", whom="Подрядчик", title="Описание процесса подрядчику",
                                    what="Передать финальное описание", due="2026-10-30", source_type="pachca")
    archive = records.add_commitment(direction="mine", whom="Олег Соколов", title="Схемы в карточку проекта",
                                     what="Загрузить архив в карточку", due="2026-10-30", source_type="meeting")
    return schemes, dup, answers, contractor, archive


def test_waits_blocked_holds_and_no_loops():
    schemes, dup, answers, contractor, archive = _warehouse()
    records.add_wait(answers, waits_for=schemes)
    records.add_wait(contractor, waits_for=schemes)
    first = records.add_wait(contractor, waits_for=answers, note="без ответов не отдам")
    assert records.add_wait(contractor, waits_for=answers) == first  # повтор — та же связь
    records.add_wait(archive, waits_for=contractor)

    n = records.get_commitment(contractor)
    assert [w["task"]["id"] for w in n["waits"]] == [schemes, answers] and n["blocked"] and n["waits_open"] == 2
    assert [h["task"]["id"] for h in n["holds"]] == [archive]
    assert [h["task"]["id"] for h in records.get_commitment(schemes)["holds"]] == [answers, contractor]

    with pytest.raises(ValueError, match="круг"):
        records.add_wait(schemes, waits_for=archive)  # архив ждёт… схемы — через цепочку
    with pytest.raises(ValueError, match="сама себя"):
        records.add_wait(contractor, waits_for=contractor)
    with pytest.raises(ValueError):
        records.add_wait(contractor, waits_for=999)
    with pytest.raises(ValueError):
        records.add_wait(contractor)  # ни задачи, ни события
    # основная задача ждёт подзадачи: подзадача не может ждать свою основную, и наоборот
    kid = records.add_commitment(direction="to_me", who="Пётр", what="Выгрузка", parent_id=archive)
    with pytest.raises(ValueError, match="круг"):
        records.add_wait(kid, waits_for=archive)
    with pytest.raises(ValueError, match="круг"):
        records.set_parent(answers, schemes)  # ответы ждут схемы — значит, не могут стать их частью
    records.set_parent(schemes, answers)  # а схемы частью ответов — могут: ответы и так их ждут


def test_wait_risk_and_events():
    schemes, _, answers, contractor, archive = _warehouse()
    records.add_wait(archive, waits_for=contractor)
    store.execute("UPDATE commitments SET due='2026-11-05' WHERE id=?", (archive,))
    store.execute("UPDATE commitments SET due='2026-11-07' WHERE id=?", (contractor,))
    assert "позже срока" in records.get_commitment(archive)["wait_risk"]
    store.execute("UPDATE commitments SET due='2020-01-01' WHERE id=?", (contractor,))
    risk = records.get_commitment(archive)["wait_risk"]
    assert "Описание процесса подрядчику" in risk and "прошёл" in risk

    margin = records.add_commitment(direction="mine", whom="Пётр Иванов", title="Бюджет склада на ноябрь", what="Бюджет склада",
                                    due="2099-11-01")
    ev = records.add_wait(margin, event="встреча  Олега с собственником", until="2099-11-02")
    assert records.add_wait(margin, event="Встреча Олега с собственником") == ev  # то же событие
    c = records.get_commitment(margin)
    assert c["waits"][0]["event"] == "встреча Олега с собственником" and c["blocked"] and "позже срока" in c["wait_risk"]
    with pytest.raises(ValueError):
        records.add_wait(margin, event="x", until="02.11")
    records.set_event_done(ev)
    c = records.get_commitment(margin)
    assert not c["blocked"] and c["waits"][0]["done"]
    assert [n["title"] for n in notices.recent()] == ["Можно делать: «Бюджет склада на ноябрь»"]
    records.remove_wait(ev)
    assert records.get_commitment(margin)["waits"] == []


def test_unblocked_notice_only_when_nothing_left_to_wait():
    schemes, _, answers, contractor, _ = _warehouse()
    records.add_wait(answers, waits_for=schemes)
    records.add_wait(contractor, waits_for=schemes)
    records.add_wait(contractor, waits_for=answers)
    records.update_commitment(schemes, status="done")
    found = notices.recent()
    assert [n["title"] for n in found] == ["Можно делать: «Ответить на вопросы по схемам»"]  # подрядчику — ещё ждёт ответы
    assert found[0]["text"] == "Дмитрий Орлов: «Схемы процессов склада» — готово"
    assert found[0]["link"] == f"/commitments?task={answers}"
    records.update_commitment(answers, status="cancelled")  # отменили — больше не ждём
    assert notices.recent()[0]["title"] == "Можно делать: «Описание процесса подрядчику»"
    assert "отменена" in notices.recent()[0]["text"]
    assert [c["id"] for c in records.unblocked()] == [answers, contractor][1:]  # открыта только «подрядчику»
    records.update_commitment(schemes, status="done")  # повторное закрытие — без нового уведомления
    assert len(notices.recent()) == 2


def test_merge_moves_source_links_and_children():
    schemes, dup, answers, contractor, _ = _warehouse()
    store.execute("UPDATE commitments SET due=NULL, kaiten_card_id=NULL WHERE id=?", (schemes,))
    store.execute("UPDATE commitments SET due='2026-11-01', kaiten_card_id=555 WHERE id=?", (dup,))
    records.add_wait(answers, waits_for=dup)
    records.add_wait(dup, waits_for=contractor)
    records.add_wait(schemes, waits_for=dup)  # схемы ждали дубль — после объединения это была бы связь сама с собой
    kid = records.add_commitment(direction="to_me", who="Пётр", what="Выгрузка воронок", parent_id=dup)
    remind = proposals.propose_task_done(commitment_id=dup, reason="прислал")
    pair = proposals.propose_task_merge(commitment_id=dup, into=schemes, reason="одна и та же работа")

    records.merge_commitments(dup, schemes, note="одна и та же работа")
    d, s = records.get_commitment(dup), records.get_commitment(schemes)
    assert d["status"] == "cancelled" and d["duplicate_of"] == schemes and d["duplicate"]["id"] == schemes
    assert "Объединена с дублем «Процессы по карте склада»" in s["note"] and "сначала изучаем карту" in s["note"]
    assert s["due"] == "2026-11-01" and s["kaiten_card_id"] == 555  # чего не было — взяли у дубля
    assert records.get_commitment(kid)["parent_id"] == schemes
    assert [w["task"]["id"] for w in records.get_commitment(answers)["waits"]] == [schemes]
    assert [w["task"]["id"] for w in s["waits"]] == [contractor]  # без «ждёт сама себя»
    assert proposals.get(remind["id"])["status"] == "rejected" and proposals.get(pair["id"])["status"] == "done"

    with pytest.raises(ValueError, match="уже дубль"):
        records.merge_commitments(dup, schemes)
    with pytest.raises(ValueError):
        records.merge_commitments(answers, answers)
    with pytest.raises(ValueError, match="уже дубль"):
        records.merge_commitments(answers, dup)
    # остаётся всегда открытая: в закрытую открытую не сливаем, а закрытую в открытую — можно
    records.update_commitment(contractor, status="done")
    with pytest.raises(ValueError, match="наоборот"):
        records.merge_commitments(answers, contractor)
    records.merge_commitments(contractor, answers)
    assert records.get_commitment(contractor)["duplicate_of"] == answers
    records.update_commitment(dup, status="open")  # «Вернуть» — снова самостоятельная задача
    assert records.get_commitment(dup)["duplicate_of"] is None


def test_similar_open_and_card_guard():
    schemes, dup, *_ = _warehouse()
    hits = records.similar_open(direction="to_me", who="Дмитрий", whom=None, title="Процессы по карте склада",
                                what="Изучить карту склада, затем описать процессы по ней: приёмка "
                                     "и отгрузка в первую очередь", exclude=dup)
    assert [c["id"] for c in hits] == [schemes]
    assert records.similar_open(direction="to_me", who="Пётр Иванов", whom=None, title="Процессы по карте склада",
                                what="процессы склада", exclude=dup) == []  # другой человек

    store.execute("UPDATE commitments SET kaiten_card_id=4242 WHERE id=?", (schemes,))
    with pytest.raises(records.DuplicateError) as err:
        records.add_commitment(direction="mine", what="Сверка остатков склада", kaiten_card_id=4242)
    assert err.value.existing == schemes and "--force" in str(err.value)
    forced = records.add_commitment(direction="mine", what="Сверка остатков склада", kaiten_card_id=4242, force=True)
    assert forced != schemes


def test_cli_wait_merge_and_similar(capsys, monkeypatch):
    schemes, dup, answers, contractor, _ = _warehouse()
    cli.main(["commitments", "add", "--direction", "to_me", "--who", "Дмитрий Орлов", "--title", "Схемы склада",
              "--what", "Схемы процессов склада: приёмка и хранение, всё в папку", "--source", "pachca:103"])
    added = capsys.readouterr().out
    assert "проверь, не дубль ли" in added and f"[{schemes}] «Схемы процессов склада»" in added
    new_id = store.scalar("SELECT MAX(id) FROM commitments")

    cli.main(["commitments", "wait", str(contractor), "--for", str(answers), "--note", "без ответов не отдам"])
    assert "«Ответить на вопросы по схемам»" in capsys.readouterr().out
    cli.main(["commitments", "wait", str(contractor), "--event", "встреча с Петром", "--until", "2026-11-03"])
    capsys.readouterr()
    cli.main(["commitments", "list"])
    listing = capsys.readouterr().out
    assert f"ждёт [{answers}] «Ответить на вопросы по схемам» (Я)" in listing and "ждёт событие «встреча с Петром»" in listing
    assert f"её ждут: [{contractor}]" in listing
    cli.main(["commitments", "wait", str(contractor), "--event", "петром", "--happened"])
    cli.main(["commitments", "wait", str(contractor), "--for", str(answers), "--remove"])
    capsys.readouterr()
    assert [w["done"] for w in records.get_commitment(contractor)["waits"]] == [True]
    with pytest.raises(SystemExit):
        cli.main(["commitments", "wait", str(contractor), "--for", str(contractor)])

    # в фоне Claude объединяет сам только свою свежую задачу, которую директор ещё не видела
    monkeypatch.setenv("OPSDASH_AGENT", "1")
    cli.main(["commitments", "merge", str(new_id), "--into", str(schemes)])
    assert "закрыта как дубль" in capsys.readouterr().out
    records.mark_seen(dup)
    with pytest.raises(SystemExit):
        cli.main(["commitments", "merge", str(dup), "--into", str(schemes)])
    assert "propose merge" in capsys.readouterr().err
    cli.main(["commitments", "wait", str(answers), "--for", str(schemes), "--source", "pachca:104"])
    assert records.get_commitment(answers)["waits"][0]["by_claude"]


def test_proposals_wait_and_merge(capsys):
    schemes, dup, answers, contractor, _ = _warehouse()
    cli.main(["propose", "wait", "--commitment", str(contractor), "--for", str(schemes), "--reason",
              "Описание собирается из схем Дмитрия", "--source", "pachca:105"])
    assert "ждёт «Схемы процессов склада»" in capsys.readouterr().out
    p = proposals.list_proposals()[0]
    assert records.get_commitment(contractor)["wait_hints"][0]["id"] == p["id"]
    assert "Можно делать" in proposals.describe(p)
    assert proposals.approve(p["id"])["status"] == "done"
    assert records.get_commitment(contractor)["waits"][0]["by_claude"]
    with pytest.raises(proposals.ProposalError, match="уже ждёт"):
        proposals.propose_task_wait(commitment_id=contractor, waits_for=schemes)
    ev = proposals.propose_task_wait(commitment_id=contractor, event="ответ Петра", reason="ждём")
    proposals.reject(ev["id"])
    with pytest.raises(proposals.ProposalError, match="«Нет»"):
        proposals.propose_task_wait(commitment_id=contractor, event="Ответ Петра")

    with pytest.raises(proposals.ProposalError, match="reason"):
        proposals.propose_task_merge(commitment_id=dup, into=schemes)
    m = proposals.propose_task_merge(commitment_id=dup, into=schemes, reason="Та же работа Дмитрия",
                                     quote="схемы процессов")
    assert records.get_commitment(dup)["merge_hint"]["into_task"]["id"] == schemes
    assert ("Дубль чего", f"/commitments?task={schemes}") in proposals.links(m)
    proposals.reject(m["id"])
    with pytest.raises(proposals.ProposalError, match="разные"):
        proposals.propose_task_merge(commitment_id=schemes, into=dup, reason="снова")
    m2 = proposals.propose_task_merge(commitment_id=answers, into=schemes, reason="проверка")
    assert proposals.approve(m2["id"])["status"] == "done" and records.get_commitment(answers)["duplicate_of"] == schemes


def test_web_waits_merge_and_overview(client):
    schemes, dup, answers, contractor, archive = _warehouse()
    r = client.post(f"/commitments/{contractor}/wait", data={"target": f"#{schemes} · Схемы · Дмитрий → мне"})
    assert r.status_code == 200
    client.post(f"/commitments/{archive}/wait", data={"target": f"#{contractor} · Описание"})
    client.post(f"/commitments/{archive}/wait", data={"target": "02.11 встреча с Олегом", "until": "2026-11-02"})
    a = records.get_commitment(archive)
    assert [w["task"]["id"] if w["task"] else w["event"] for w in a["waits"]] == [contractor, "02.11 встреча с Олегом"]
    assert "круг" in unquote(str(client.post(f"/commitments/{schemes}/wait", data={"target": f"#{archive}"}).url))

    board = client.get("/commitments").text
    assert 'class="chip wait"' in board and "её ждут" in board and "Связать с другой задачей" in board
    assert f'action="/commitments/{archive}/wait/' in board and "✓ Случилось" in board
    assert "срок под угрозой" in board
    assert 'class="chip wait"' in client.get("/commitments?view=list").text
    assert 'class="chip wait"' in client.get("/commitments?view=gantt").text

    event = a["waits"][1]["id"]
    client.post(f"/commitments/{archive}/wait/{event}/done", data={"on": "1"})
    assert records.get_commitment(archive)["waits"][1]["done"]
    client.post(f"/commitments/{archive}/wait/{event}/remove")
    assert len(records.get_commitment(archive)["waits"]) == 1

    r = client.post(f"/commitments/{dup}/merge", data={"into": f"#{schemes} · Схемы"},
                    headers={"referer": "http://localhost:8765/commitments?view=list&task=7"})
    assert f"/commitments?view=list&task={schemes}&msg=" in str(r.url)
    assert records.get_commitment(dup)["duplicate_of"] == schemes
    assert "Выберите задачу" in unquote(str(client.post(f"/commitments/{answers}/merge", data={"into": "Схемы"}).url))

    records.update_commitment(schemes, status="done")
    text = overview.overview_markdown()
    assert f"[commitment {contractor}]" in text.split("## Можно делать")[1].split("##")[0]
    assert f"ждёт [commitment {contractor}] (Я)" in text
    assert "Можно делать: 1" in overview.today_text()
