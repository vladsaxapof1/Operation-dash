"""Ход задачи: комментарии директора, апдейты из переписки и встреч (пишет Claude), события PULSE; строка «Сейчас»
на карточке — последняя запись, у вопроса без ответа — «ответа нет N дней». Пример — договор с поставщиком упаковки: директор спросила
Петра в Пачке, кто поможет; тот ответил, что этим занимался Олег; второй вопрос — без ответа; Олег в Telegram
сказал, что вернётся в среду."""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from opsdash import cli, config, jobs, overview, records, store, worklog
from opsdash.sources import kaiten
from tests.fake_kaiten import FakeKaiten


@pytest.fixture
def client():
    from opsdash.web.app import app

    return TestClient(app, base_url="http://localhost:8765")


def _msg(source: str, chat: str, msg_id: str, text: str, ts: str, *, sender: str = "", outgoing: bool = False) -> int:
    store.add_message({"source": source, "chat_id": chat, "chat_title": sender or chat, "msg_id": msg_id,
                       "sender_name": "Я" if outgoing else sender, "is_outgoing": outgoing, "text": text, "ts": ts})
    return store.scalar("SELECT id FROM messages WHERE source=? AND chat_id=? AND msg_id=?", (source, chat, msg_id))


def _phones() -> tuple[int, dict]:
    ask1 = _msg("pachca", "77", "1", "Кто может помочь с договором поставщика?", "2026-11-03T12:45:00+00:00",
                sender="Пётр", outgoing=True)
    ans = _msg("pachca", "77", "2", "Этим Олег преимущественно занимался", "2026-11-03T13:48:00+00:00", sender="Пётр")
    ask2 = _msg("pachca", "77", "3", "А есть понимание, когда он вернётся?", "2026-11-03T13:51:00+00:00",
                sender="Пётр", outgoing=True)
    oleg = _msg("telegram", "88", "9", "Вернусь в среду. До 16 подпишу договор и согласую цены",
                "2026-11-05T09:45:00+00:00", sender="Олег")
    cid = records.add_commitment(direction="to_me", who="Пётр", title="Договор с поставщиком упаковки",
                                 what="Сверить условия, согласовать цены, подписать договор", source_type="pachca",
                                 source_ref=str(ans), due="2026-11-30")
    return cid, {"ask1": ask1, "ans": ans, "ask2": ask2, "oleg": oleg}


def test_log_order_origin_dedupe_and_now(monkeypatch):
    monkeypatch.setattr(config, "now", lambda settings=None: datetime(2026, 11, 5, 15, 0, tzinfo=config.tz()))
    cid, m = _phones()
    worklog.add(cid, "Спросили Петра, кто может помочь с договором поставщика", kind="ask", source=f"pachca:{m['ask1']}")
    worklog.add(cid, "Пётр: этим занимался Олег", source=f"pachca:{m['ans']}")
    lid, new = worklog.add(cid, "Спросили Петра, когда вернётся Олег", kind="ask", source=f"pachca:{m['ask2']}")
    assert new
    # повторный разбор того же сообщения — та же запись, текст обновлён
    again, new = worklog.add(cid, "Спросили Петра: когда вернётся Олег?", kind="ask", source=f"pachca:{m['ask2']}")
    assert again == lid and not new

    c = records.get_commitment(cid)
    kinds = [e["kind"] for e in c["log"]]
    # «появилась» — на время сообщения, из которого задача взялась (а не разбора), записи — по времени сообщений
    assert kinds == ["ask", "origin", "update", "ask"]  # «появилась» и ответ — одно сообщение: сначала «появилась»
    assert c["log_count"] == 3
    assert c["log"][-1]["text"] == "Спросили Петра: когда вернётся Олег?"
    assert c["log"][-1]["at"].startswith("2026-11-03T13:51")
    assert c["log"][-1]["src"] == "Пачка · Пётр"
    # последний — вопрос без ответа: «ответа нет 2 дня»
    assert c["now"]["text"].startswith("Спросили Петра: когда") and c["now"]["waiting"] == "ответа нет 2 дня"
    assert c["now"]["waiting_short"] == "без ответа 2 дн."

    # ответ пришёл — «ждём ответа» снимается само
    worklog.add(cid, "Олег вернётся в среду 11.11; до 16.11 подпишет договор и согласует цены",
                source=f"telegram:{m['oleg']}", quote="До 16 подпишу договор и согласую цены")
    c = records.get_commitment(cid)
    assert c["now"]["text"].startswith("Олег вернётся") and c["now"]["waiting"] is None
    assert c["now"]["src"] == "Telegram · Олег"
    assert "сейчас (05.11, Telegram · Олег): Олег вернётся в среду 11.11" in worklog.now_text(c)

    # закрытая задача — без «Сейчас», ход остаётся
    records.update_commitment(cid, status="done")
    c = records.get_commitment(cid)
    assert c["now"] is None and c["log"][-1]["kind"] == "event" and c["log"][-1]["text"] == "Выполнена"


def test_add_validation_and_meeting_time():
    cid = records.add_commitment(direction="mine", whom="Тимур", what="Закупка упаковки")
    with pytest.raises(ValueError, match="Напишите"):
        worklog.add(cid, "  ")
    with pytest.raises(ValueError, match="Нет задачи"):
        worklog.add(999, "что-то")
    with pytest.raises(ValueError, match="Источник"):
        worklog.add(cid, "что-то", source="whatsapp:1")  # такого источника нет
    store.execute("INSERT INTO meetings(id, title, started_at, status) VALUES('m1', 'Трекинг МСБ', "
                  "'2026-11-04T08:00:00+00:00', 'processed')")
    worklog.add(cid, "По встрече: решили менять поставщика", source="meeting:m1", quote="[00:12:30] меняем поставщика")
    e = next(e for e in records.get_commitment(cid)["log"] if e["kind"] == "update")
    assert e["at"] == "2026-11-04T08:12:30+00:00" and e["src"] == "встреча «Трекинг МСБ»"
    # свой момент — --at: встаёт раньше (задача добавлена вручную только что — «появилась» последней)
    worklog.add(cid, "Звонил Тимуру", kind="note", at="2026-11-01 10:00")
    assert [e["kind"] for e in records.get_commitment(cid)["log"]] == ["note", "update", "origin"]


def test_events_from_pulse(monkeypatch):
    cid = records.add_commitment(direction="to_me", who="Пётр", title="Договор упаковки", what="Договор упаковки",
                                 due="2026-11-30")
    other = records.add_commitment(direction="to_me", who="Олег", title="Новые цены", what="Согласовать цены")
    records.set_due(cid, "2026-11-16")
    records.set_in_work(cid, True)
    records.set_in_work(cid, True)  # уже в работе — второй раз не пишем
    records.add_wait(cid, waits_for=other, note="без цен договор не подписать")
    records.add_wait(cid, event="Олег вернётся", until="2026-11-11")
    link = records.find_waits(cid, event="Олег")[0]
    records.set_event_done(link["id"])
    records.add_items(cid, "Цены; Сроки поставки")
    item = records.find_items(cid, "Цены")[0]
    records.set_item_done(item["id"], True, "Олег прислал прайс")
    texts = [e["text"] for e in records.get_commitment(cid)["log"] if e["kind"] == "event"]
    assert texts == ["Срок: 30.11 → 16.11", "Взяли в работу",
                     "Ждёт «Новые цены» (Олег). без цен договор не подписать", "Ждёт: Олег вернётся — к 11.11",
                     "Случилось: Олег вернётся", "Пункт готов: Цены — Олег прислал прайс"]
    # события — не статус: «Сейчас» пусто, пока нет записи директора или Claude
    assert records.get_commitment(cid)["now"] is None

    # закрыли «Новые цены» — у договора «Можно делать»
    records.update_commitment(other, status="done", note="цены согласованы")
    log = records.get_commitment(cid)["log"]
    assert log[-1]["text"].startswith("Можно делать — Олег: «Новые цены» — готово")
    assert records.get_commitment(other)["log"][-1]["text"] == "Выполнена. цены согласованы"


def test_merge_moves_log_and_kaiten_move(settings_with, monkeypatch):
    keep = records.add_commitment(direction="mine", whom="Тимур", what="Закупка упаковки в ноябре")
    drop = records.add_commitment(direction="mine", whom="Тимур", what="Договор с поставщиком упаковки")
    worklog.add(drop, "Олег вернётся в среду")
    records.merge_commitments(drop, keep)
    log = records.get_commitment(keep)["log"]
    assert [e["text"] for e in log if e["kind"] != "origin"][:2] == ["Олег вернётся в среду",
                                                                     "Объединена с дублем «Договор с поставщиком упаковки»"]
    assert records.get_commitment(drop)["log_count"] == 0

    fk = FakeKaiten()
    settings_with(kaiten={"enabled": True, "domain": "https://demo.kaiten.ru/",
                          "spaces": [{"id": 10, "title": "Операционка", "track": True}]})
    config.set_secret("KAITEN_TOKEN", "good-token")
    monkeypatch.setattr(kaiten, "client", lambda settings=None, transport=None: kaiten.Kaiten(
        "demo.kaiten.ru", "good-token", transport=fk.transport()))
    card = fk.add_card(10, "Договор упаковки", responsible=2)
    kaiten.collect()
    records.update_commitment(keep, kaiten_card_id=card)
    fk.cards[card]["column"] = {"title": "Готово"}
    kaiten.collect()
    events = [e["text"] for e in records.get_commitment(keep)["log"] if e["kind"] == "event"]
    assert events[-2:] == [f"Карточка Kaiten #{card}", "Kaiten: карточка перешла из «В работе» в «Готово»"]
    kaiten.collect()  # колонка та же — второй раз не пишем
    assert len([e for e in records.get_commitment(keep)["log"] if e["kind"] == "event"]) == len(events)


def test_cli_log_show_and_list_line(capsys, monkeypatch):
    cid, m = _phones()
    monkeypatch.setenv("OPSDASH_AGENT", "1")
    cli.main(["commitments", "log", str(cid), "--text", "Спросили Петра, когда вернётся Олег", "--ask",
              "--source", f"pachca:{m['ask2']}"])
    assert "Записано: ход задачи" in capsys.readouterr().out
    cli.main(["commitments", "log", str(cid), "--text", "Спросили Петра, когда вернётся Олег?", "--ask",
              "--source", f"pachca:{m['ask2']}"])
    assert "Уже было — запись обновлена" in capsys.readouterr().out
    e = records.get_commitment(cid)["log"][-1]
    assert e["author"] == "claude"
    monkeypatch.delenv("OPSDASH_AGENT")
    cli.main(["commitments", "log", str(cid), "--text", "Звонил Олегу", "--kind", "note", "--by", "director"])
    capsys.readouterr()

    cli.main(["commitments", "show", str(cid)])
    shown = capsys.readouterr().out
    assert "Ход задачи" in shown and "появилась · Пачка" in shown
    assert f"[pachca:{m['ask2']}]" in shown and "спросили, ждём ответа" in shown
    assert "комментарий директора: Звонил Олегу" in shown

    cli.main(["commitments", "list", "--person", "Пётр"])
    assert "сейчас (" in capsys.readouterr().out and "Звонил Олегу" in shown
    assert "сейчас (" in overview.overview_markdown()

    note = records.get_commitment(cid)["log"][-1]["id"]
    cli.main(["commitments", "log", str(cid), "--remove", str(note)])
    assert "убрана" in capsys.readouterr().out
    assert all(e["id"] != note for e in records.get_commitment(cid)["log"])


def test_web_dialog_comment_remove_and_collect(client, monkeypatch):
    cid, m = _phones()
    worklog.add(cid, "Пётр: этим занимался Олег", source=f"pachca:{m['ans']}")
    page = client.get(f"/commitments?task={cid}").text
    assert "Ход задачи" in page and "Пётр: этим занимался Олег" in page and "data-log-form" in page
    assert "Сейчас" in page

    r = client.post(f"/commitments/{cid}/log", data={"text": "Спросил Тимура про цены", "ask": "1"},
                    headers={"Accept": "application/json"})
    res = r.json()
    assert res["ok"] and "Спросил Тимура про цены" in res["list"] and "ответа пока нет" in res["now"]
    assert res["count"] == 2
    e = records.get_commitment(cid)["log"][-1]
    assert e["kind"] == "ask" and e["author"] == "director" and e["src"] == "вы"

    r = client.post(f"/commitments/{cid}/log", data={"text": " "}, headers={"Accept": "application/json"})
    assert r.status_code == 400 and not r.json()["ok"]

    r = client.post(f"/commitments/{cid}/log/{e['id']}/remove", headers={"Accept": "application/json"})
    assert r.json()["ok"] and "Спросил Тимура" not in r.json()["list"]

    # без скриптов — обычная форма: назад с открытым окном задачи
    r = client.post(f"/commitments/{cid}/log", data={"text": "Позвонил Олегу"}, follow_redirects=False,
                    headers={"Referer": "http://localhost:8765/commitments?view=list"})
    assert r.status_code == 303 and f"task={cid}" in r.headers["location"]

    sent = []
    monkeypatch.setattr(jobs, "submit", lambda name, kind="light", args=None: sent.append((name, kind, args)) or True)
    client.post(f"/commitments/{cid}/log/collect", follow_redirects=False)
    assert sent == [("task-log", "chat", {"task_id": cid})]


def test_task_log_job_counts_new_entries(monkeypatch):
    cid, m = _phones()
    from opsdash import brain

    def fake_run(prompt, **kw):
        assert f"[{cid}]" in prompt and "task-log" in prompt and kw["job"] == "task-log"
        worklog.add(cid, "Пётр: этим занимался Олег", source=f"pachca:{m['ans']}", by="claude")
        return {"text": "ok"}

    monkeypatch.setattr(brain, "run", fake_run)
    assert jobs.job_task_log(cid) == "записей в ходе: +1"
    monkeypatch.setattr(brain, "run", lambda prompt, **kw: {"text": ""})
    assert jobs.job_task_log(cid) == "нового по задаче не нашлось"
    from opsdash import notices

    s = notices.summary("task-log", {"task_id": cid}, "записей в ходе: +1", None)
    assert s["title"] == "Собрал ход задачи" and s["link"] == f"/commitments?task={cid}"


def test_messages_chat_finds_latin_title(capsys):
    _msg("telegram", "88", "9", "Вернусь в среду", datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
         sender="Oleg Sokolov")
    cli.main(["messages", "chat", "Олег", "--days", "3"])
    assert "Вернусь в среду" in capsys.readouterr().out
    assert cli._latin("Пётр Иванов") == "Petr Ivanov"


def test_triage_announces_task_log(monkeypatch):
    """Разбор сообщений дописал ход задач — в углу экрана «Обновил ход задачи», в итоге разбора — «ход задач: +N»."""
    from opsdash import brain, notices

    cid, m = _phones()
    other = records.add_commitment(direction="to_me", who="Олег", title="Новые цены", what="Согласовать цены")
    store.execute("UPDATE messages SET processed=0")
    monkeypatch.setattr(jobs, "in_work_hours", lambda settings, at=None: True)

    def fake_triage(prompt, **kw):
        worklog.add(cid, "Пётр: этим занимался Олег", source=f"pachca:{m['ans']}", by="claude")
        worklog.add(cid, "Олег вернётся в среду 11.11", source=f"telegram:{m['oleg']}", by="claude")
        worklog.add(cid, "Звонил Олегу", kind="note", by="director")  # директор сама — не объявляем
        records.set_due(other, "2026-11-20")  # событие PULSE — тоже нет
        store.execute("UPDATE messages SET processed=1")
        return {"text": ""}

    monkeypatch.setattr(brain, "run", fake_triage)
    assert "ход задач: +2" in jobs.job_triage()
    n = store.row("SELECT * FROM notices WHERE title LIKE 'Обновил ход%' ORDER BY id DESC")
    assert n["title"] == "Обновил ход задачи «Договор с поставщиком упаковки»"
    assert n["text"] == "Олег вернётся в среду 11.11" and n["link"] == f"/commitments?task={cid}"

    # две задачи — список и ссылка на доску
    since = worklog.last_id()
    worklog.add(other, "Олег прислал прайс", by="claude")
    worklog.add(cid, "Пётр: договор на подписи", by="claude")
    assert worklog.announce(since, "process-meeting", "— встреча «Закупки»") == 2
    n = store.row("SELECT * FROM notices ORDER BY id DESC")
    assert n["title"] == "Обновил ход задач: 2 — встреча «Закупки»" and n["link"] == "/commitments"
    assert "«Договор с поставщиком упаковки» — Пётр: договор на подписи" in n["text"]
    assert worklog.announce(worklog.last_id(), "triage") == 0


def test_collect_progress_edit_and_corner(client, monkeypatch):
    """«Собрать с Claude» без перезагрузки: состояние для окна задачи (в очереди → собирает и что делает → готово,
    лента и итог), «Сейчас» в углу экрана называет задачу; «Изменить» у записи."""
    from opsdash import notices

    cid, m = _phones()
    monkeypatch.setattr(jobs, "submit", lambda name, kind="light", args=None: True)
    monkeypatch.setattr(jobs, "pending", lambda name, args=None: args == {"task_id": cid})
    r = client.post(f"/commitments/{cid}/log/collect", headers={"Accept": "application/json"}).json()
    assert r["ok"] and r["state"] == "queued"
    assert records.get_commitment(cid)["collecting"] == "queued"

    run = store.execute("INSERT INTO agent_runs(job, args, started, status, steps) VALUES('task-log', ?, ?, 'running', ?)",
                        (store.json_col({"task_id": cid}), store.utcnow(),
                         store.json_col([{"t": "Ищет в переписке: «упаков»", "at": store.utcnow()}])))
    st = client.get(f"/api/commitments/{cid}/log").json()
    assert st["state"] == "running" and st["step"] == "Ищет в переписке: «упаков»" and "list" not in st
    page = client.get(f"/commitments?task={cid}").text
    assert 'data-log-state="running"' in page and "собирает ход задачи" in page
    store.execute("UPDATE jobs SET running=0")
    store.execute("INSERT OR REPLACE INTO jobs(name, running) VALUES('task-log', 1)")
    corner = [x for x in notices.running() if x["name"] == "task-log"]
    assert corner and corner[0]["detail"] == "«Договор с поставщиком упаковки»"

    worklog.add(cid, "Пётр: этим занимался Олег", source=f"pachca:{m['ans']}", by="claude")
    store.execute("UPDATE agent_runs SET status='ok', result='записей в ходе: +1' WHERE id=?", (run,))
    monkeypatch.setattr(jobs, "pending", lambda name, args=None: False)
    st = client.get(f"/api/commitments/{cid}/log").json()
    assert st["state"] == "idle" and st["result"] == "записей в ходе: +1" and st["ok"]
    assert "Пётр: этим занимался Олег" in st["list"] and st["count"] == 1
    assert records.get_commitment(cid)["collecting"] is None

    eid = records.get_commitment(cid)["log"][-1]["id"]
    r = client.post(f"/commitments/{cid}/log/{eid}/edit", data={"text": "Пётр: договором занимался Олег"},
                    headers={"Accept": "application/json"}).json()
    assert r["ok"] and "договором занимался Олег" in r["list"]
    assert worklog.get(eid)["source_ref"] == str(m["ans"])  # откуда — как было
    r = client.post(f"/commitments/{cid}/log/{eid}/edit", data={"text": " "}, headers={"Accept": "application/json"})
    assert r.status_code == 400
    r = client.post(f"/commitments/{cid}/log/999/edit", data={"text": "x"}, headers={"Accept": "application/json"})
    assert r.status_code == 400
