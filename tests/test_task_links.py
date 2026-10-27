"""Связи задач (подзадачи), отметка «новая», сортировка, ссылки из разбора встречи на задачи
и предложение «закрыть задачу, она выглядит выполненной»."""

import sqlite3

import pytest
from fastapi.testclient import TestClient

from opsdash import cli, config, proposals, records, store


@pytest.fixture
def client():
    from opsdash.web.app import app

    return TestClient(app, base_url="http://localhost:8765")


def test_subtask_chain_helpers_and_no_loops():
    parent = records.add_commitment(direction="mine", whom="Тимур", title="Отчёт по продажам",
                                    what="Собирать недельный отчёт", due="2026-11-03", source_type="meeting")
    kid = records.add_commitment(direction="to_me", who="Пётр", title="Выгрузка для отчёта", what="Собрать выгрузку",
                                 due="2026-11-05", source_type="meeting", parent_id=parent)
    p, k = records.get_commitment(parent), records.get_commitment(kid)
    assert [c["id"] for c in p["children"]] == [kid] and p["helpers"] == ["Пётр"]  # «Я + Пётр → Тимур»
    assert k["parent"]["id"] == parent and k["parent"]["route"] == "Я → Тимур"
    assert k["late_vs_parent"], "срок подзадачи позже срока задачи — подсветить"

    with pytest.raises(ValueError):
        records.set_parent(parent, kid)  # петля: задача — часть своей подзадачи
    with pytest.raises(ValueError):
        records.add_commitment(direction="mine", what="x", parent_id=999)

    records.update_commitment(kid, status="done")
    p = records.get_commitment(parent)
    assert p["kids_done"] and p["helpers"] == []
    records.update_commitment(kid, parent_id=0)
    assert records.get_commitment(kid)["parent"] is None
    assert records.parse_task_ref("#12 · Отчёт · Я → Тимур") == 12 and records.parse_task_ref("") is None


def test_new_tasks_seen_sort_and_old_rows_already_seen():
    mine = records.add_commitment(direction="mine", what="Своё дело")  # добавлено вручную — уже видела
    first = records.add_commitment(direction="to_me", who="Пётр", what="Прогноз", due="2026-12-01",
                                   source_type="telegram", source_ref="5")
    second = records.add_commitment(direction="to_me", who="Анна", what="Отчёт", due="2026-11-01", source_type="meeting")
    store.execute("UPDATE commitments SET created='2026-10-01T00:00:00+00:00' WHERE id=?", (second,))
    assert records.get_commitment(mine)["seen_at"] and not records.get_commitment(first)["seen_at"]
    assert records.unseen_count() == 2
    assert [c["id"] for c in records.list_commitments(unseen=True)] == [second, first]  # по сроку
    assert [c["id"] for c in records.list_commitments(direction="to_me", sort="created")] == [first, second]
    records.mark_seen(first)
    assert records.unseen_count() == 1 and [c["id"] for c in records.list_commitments(unseen=True)] == [second]

    # база до появления отметки: старые задачи не должны вспыхнуть «новыми»
    with sqlite3.connect(config.DB_FILE) as conn:
        conn.execute("ALTER TABLE commitments DROP COLUMN seen_at")
    store._initialized.clear()
    assert records.unseen_count() == 0


SUMMARY = """# Обзор продаж

## Задачи
| Что | Кто | Срок | Kaiten |
|---|---|---|---|
| Набросок бюджета по направлениям на 2027 (маркетинг, найм) — передать Анне | Тимур | 06.11 | предложено |
| Позвонить в бухгалтерию | Ольга | — | — |

## Договорённости
- **Тимур обещал Анне:** до 06.11 передать набросок бюджета по направлениям [00:24:07]
- Задача «Обновить реестр закупок» выполнена [00:30:20]
"""


def test_meeting_summary_links_to_tasks():
    store.execute("INSERT INTO kaiten_cards(id, title, url) VALUES(777, 'Бюджет', 'https://kaiten.example/777')")
    plan = records.add_commitment(direction="to_me", who="Тимур", title="Бюджет по направлениям 2027",
                                  what="Передать Анне набросок бюджета по направлениям на 2027 (маркетинг, найм)",
                                  source_type="meeting", source_ref="m1", kaiten_card_id=777)
    tracker = records.add_commitment(direction="to_me", who="Тимур", what="Обновить реестр закупок: новые строки",
                                     source_type="meeting", source_ref="m0")
    records.update_commitment(tracker, status="done", note="встреча m1: реестр обновлён [00:30:20]")
    tasks = records.meeting_commitments("m1")
    assert [c["id"] for c in tasks] == [plan, tracker]  # своя задача и закрытая по итогам встречи

    out = records.link_summary(SUMMARY, tasks)
    row = next(line for line in out.splitlines() if line.startswith("| Набросок"))
    assert f"[задача →](/commitments?task={plan})" in row and "[#777](https://kaiten.example/777)" in row
    assert "task=" not in next(line for line in out.splitlines() if "бухгалтерию" in line)
    assert out.count(f"task={plan})") == 2  # строка таблицы и договорённость
    assert f"[✓ выполнена](/commitments?task={tracker})" in out


def test_task_done_proposal_waits_for_director():
    cid = records.add_commitment(direction="mine", whom="Тимур", title="Отчёт по продажам", what="Собирать отчёт",
                                 source_type="meeting")
    with pytest.raises(proposals.ProposalError):
        proposals.propose_task_done(commitment_id=cid)  # без доводов — нельзя
    p = proposals.propose_task_done(commitment_id=cid, reason="Показал готовый отчёт на встрече",
                                    quote="[00:12:03] вот, вчера уже разослали", source_ref="meeting:m2", batch="meeting:m2")
    assert p["status"] == "pending" and "Отчёт по продажам" in p["title"]
    again = proposals.propose_task_done(commitment_id=cid, reason="ещё раз")
    assert again["id"] == p["id"], "по одной задаче — одно предложение"
    assert records.get_commitment(cid)["done_hint"]["id"] == p["id"]
    assert ("Задача", f"/commitments?task={cid}") in proposals.links(p)
    assert "Почему так думаю: Показал готовый отчёт" in proposals.describe(p)

    done = proposals.approve(p["id"])
    c = records.get_commitment(cid)
    assert done["status"] == "done" and c["status"] == "done" and "Показал готовый отчёт" in c["note"]
    assert proposals.result_text(done) == "✅ Задача закрыта"
    with pytest.raises(proposals.ProposalError):
        proposals.propose_task_done(commitment_id=cid, reason="x")  # уже закрыта


def test_closing_task_by_hand_settles_proposal(settings_with):
    cid = records.add_commitment(direction="to_me", who="Пётр", what="Прислать прогноз")
    p = proposals.propose_task_done(commitment_id=cid, reason="Пётр написал «отправил»", source_ref="telegram:1")
    records.update_commitment(cid, status="done")
    assert proposals.get(p["id"])["status"] == "done", "закрыли сами — предложение не висит в «Ждут вашего да»"

    settings_with(autonomy={"task_done": "auto"})
    other = records.add_commitment(direction="to_me", who="Пётр", what="Прислать бюджет")
    auto = proposals.propose_task_done(commitment_id=other, reason="Пётр прислал бюджет")
    assert auto["status"] == "done" and records.get_commitment(other)["status"] == "done"


def test_web_focus_seen_subtask_and_meeting_links(client):
    parent = records.add_commitment(direction="mine", whom="Тимур", title="Отчёт по продажам", what="Собирать отчёт",
                                    source_type="meeting", source_ref="m1")
    store.execute("INSERT INTO meetings(id, title, status) VALUES('m1', 'Обзор продаж', 'processed')")
    page = client.get("/commitments").text
    assert 'class="chip new"' in page and 'data-new="%d"' % parent in page
    assert '<span class="badge">1</span>' in page  # «Задачи» в меню и кнопка «Новые»

    # подзадача из окна задачи: «+ Подзадача» открывает форму с заполненным полем «Часть задачи»
    form = client.get(f"/commitments?add=to_me&parent={parent}").text
    assert f'value="#{parent} · Отчёт по продажам · Я → Тимур"' in form
    client.post("/commitments", data={"direction": "to_me", "title": "Выгрузка", "who": "Пётр",
                                      "parent": f"#{parent} · Отчёт по продажам · Я → Тимур"})
    kid = store.scalar("SELECT id FROM commitments WHERE title='Выгрузка'")
    assert store.scalar("SELECT parent_id FROM commitments WHERE id=?", (kid,)) == parent
    board = client.get("/commitments").text
    assert "<b>Я</b> + Пётр → Тимур" in board and "<b>Пётр</b> → мне → Тимур" in board
    assert f"#{parent} · Отчёт по продажам" in client.get("/api/task-options").text

    assert f'data-focus-task="{parent}"' in client.get(f"/commitments?task={parent}").text
    assert client.post(f"/commitments/{parent}/seen").json() == {"ok": True, "unseen": 0}
    assert 'class="chip new"' not in client.get("/commitments").text

    created_first = client.get("/commitments?sort=created")
    assert created_first.cookies.get("task_sort") == "created"
    meeting = client.get("/meetings/m1").text
    assert f'href="/commitments?task={parent}"' in meeting and "Отчёт по продажам" in meeting


def test_cli_parent_and_propose_done(capsys):
    parent = records.add_commitment(direction="mine", whom="Тимур", what="Собирать отчёт")
    cli.main(["commitments", "add", "--direction", "to_me", "--who", "Пётр", "--what", "Собрать выгрузку",
              "--source", "meeting:m1", "--parent", str(parent)])
    assert f"как часть задачи [{parent}]" in capsys.readouterr().out
    cli.main(["commitments", "list"])
    listing = capsys.readouterr().out
    assert f"часть задачи [{parent}]" in listing and "подзадачи: [" in listing
    cli.main(["propose", "done", "--commitment", str(parent), "--reason", "Показала готовый отчёт", "--source", "meeting:m2"])
    assert "Закрыть задачу" in capsys.readouterr().out
    assert proposals.list_proposals()[0]["source_ref"] == "meeting:m2"
