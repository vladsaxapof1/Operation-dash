"""«Сделать задачей» у строки «Ответить» на Сводке: ответа мало — по сообщению нужна работа."""
from urllib.parse import unquote

from fastapi.testclient import TestClient

from opsdash import config, proposals, records, store, worklog

SUMMARY = ("Ольга просит подтвердить или опровергнуть: можно ли брать в конверсию онбординга заявки «в процессе» "
           "(считать успехом, раз оплата уже есть)")


def _ask(text: str = "Можно ли считать заявки «в процессе» успехом? Оплата же есть", priority: str = "high") -> int:
    store.upsert_chat("telegram", "-1001", "Онбординг", "group", enabled=1)
    store.add_message({"source": "telegram", "chat_id": "-1001", "chat_title": "Онбординг", "msg_id": "501",
                       "sender_id": "7", "sender_name": "Ольга Петрова", "is_outgoing": False, "text": text,
                       "ts": "2026-11-10T07:00:00+00:00", "link": None})
    mid = store.scalar("SELECT id FROM messages WHERE msg_id='501'")
    return records.add_attention(summary=SUMMARY, message_id=mid, priority=priority, draft="Оля, проверю и вернусь")


def test_attention_becomes_task_with_message_as_source():
    aid = _ask()
    store.execute("INSERT INTO proposals(kind, title, payload, status, created) VALUES('attention.done', 'похоже, ответили', ?, "
                  "'pending', '2026-11-10T08:00:00+00:00')", (store.json_col({"attention_id": aid}),))
    cid = records.attention_to_task(aid, title="Конверсия онбординга: заявки «в процессе»", whom="Ольга Петрова",
                                    due="2026-11-12")
    c = store.row("SELECT * FROM commitments WHERE id=?", (cid,))
    mid = store.scalar("SELECT id FROM messages WHERE msg_id='501'")
    assert (c["direction"], c["title"], c["what"], c["whom"], c["who"], c["due"]) == (
        "mine", "Конверсия онбординга: заявки «в процессе»", SUMMARY, "Ольга Петрова", None, "2026-11-12")
    assert (c["source_type"], c["source_ref"]) == ("telegram", str(mid)), "сообщение — источник задачи"
    assert c["quote"].startswith("Можно ли считать") and c["seen_at"], "директор её уже видела — не «новая»"
    assert (c["importance"], c["importance_why"]) == ("high", "срочно в переписке")
    assert store.scalar("SELECT status FROM attention WHERE id=?", (aid,)) == "task"
    assert records.list_attention("open") == []
    # подсказку «похоже, ответили» не считаем «Нет» — уроки из отказов её не увидят
    assert store.scalar("SELECT status FROM proposals WHERE kind='attention.done'") == "done"
    origin, made = worklog.entries([cid], [worklog._origin(c)])[cid]
    assert origin["kind"] == "origin" and origin["src"].startswith("Telegram") and origin["quote"] == c["quote"]
    assert made["kind"] == "event" and "Сделали задачей" in made["text"]


def test_old_message_task_is_not_quiet_right_away():
    """Сообщение ждало ответа две недели — задача из него всё равно свежая: «без движения» считаем от «Сделали задачей»."""
    from opsdash import quiet

    aid = _ask()
    store.execute("UPDATE messages SET ts='2026-10-20T07:00:00+00:00'")
    cid = records.attention_to_task(aid, title="Конверсия онбординга")
    c = store.row("SELECT * FROM commitments WHERE id=?", (cid,))
    worklog.decorate([c])
    assert quiet.last_move(c)["kind"] == "event"
    assert quiet.compute(c, quiet._local_day(store.utcnow()), {"mine": 5, "to_me": 3}, config.load_settings()) is None


def test_keep_answer_row_and_long_quote():
    aid = _ask("слово " * 200, priority="normal")
    cid = records.attention_to_task(aid, title="", what="")  # пусто — суть сообщения
    c = store.row("SELECT * FROM commitments WHERE id=?", (cid,))
    assert c["what"] == SUMMARY and c["title"] is None and c["importance"] is None, "обычную оценит Claude"
    assert len(c["quote"]) <= records.ATTENTION_QUOTE_MAX + 1 and c["quote"].endswith("…")
    records.close_attention(aid, "open")  # «Вернуть» из истории отметок
    again = records.attention_to_task(aid, title="Другое", close=False)
    assert again == cid and store.scalar("SELECT COUNT(*) FROM commitments") == 1, "второй раз — та же задача, без дубля"
    assert store.scalar("SELECT status FROM attention WHERE id=?", (aid,)) == "open", "ответить всё равно нужно"


def test_home_offers_make_task_and_route_creates_it():
    from opsdash.web.app import app

    aid = _ask()
    client = TestClient(app, base_url="http://localhost:8765")
    page = client.get("/").text
    assert f'data-dlg-open="att-task-{aid}"' in page and "Сделать задачей" in page
    assert f'id="att-task-{aid}"' in page and f'action="/attention/{aid}/task"' in page
    assert 'value="Ольга просит подтвердить или опровергнуть"' in page, "заголовок — коротко из сути"
    assert 'name="whom" list="att-people" value="Ольга Петрова"' in page

    r = client.post(f"/attention/{aid}/task", data={"direction": "mine", "title": "Ответ по конверсии онбординга",
                                                    "what": "", "whom": "Ольга Петрова", "due": "", "close": "1"},
                    headers={"referer": "http://localhost:8765/"}, follow_redirects=False)
    assert r.status_code == 303
    assert "Задача «Ответ по конверсии онбординга» — в «Мои задачи»" in unquote(r.headers["location"])
    # общий /attention/{id}/{action} не перехватил «task» как «Скрыть»
    assert store.scalar("SELECT status FROM attention WHERE id=?", (aid,)) == "task"
    assert store.scalar("SELECT COUNT(*) FROM commitments WHERE source_type='telegram'") == 1
    assert "Сделать задачей" not in client.get("/").text, "строка ушла из «Ответить»"
    assert "сделали задачей" in client.get("/chats?chat=telegram|-1001").text, "в истории отметок"

    r = client.post("/attention/99999/task", data={"title": "x"}, headers={"referer": "http://localhost:8765/"},
                    follow_redirects=False)
    assert "err=" in r.headers["location"]


def test_close_attention_dismiss_still_rejects_hint():
    aid = _ask()
    p = proposals.propose_answered(attention_id=aid, reason="ответила", quote="проверю",
                                   source_ref=f"telegram:{store.scalar('SELECT id FROM messages')}")
    records.close_attention(aid, "dismissed")
    assert store.scalar("SELECT status FROM proposals WHERE id=?", (p["id"],)) == "rejected"
