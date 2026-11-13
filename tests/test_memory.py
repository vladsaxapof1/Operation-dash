"""«Память» (opsdash.memory): факты пачкой, смена по ключу, псевдонимы и объединение, личное и секреты, страницы."""

import json

import pytest

from opsdash import cli, memory, proposals, recall, records, store


def meeting(mid="m1", title="Планёрка", private=0, started="2026-11-03T08:00:00+00:00", transcript=""):
    store.execute("INSERT INTO meetings(id, title, started_at, status, private) VALUES(?,?,?,?,?)",
                  (mid, title, started, "processed", private))
    d = records.meeting_dir(mid)
    d.mkdir(parents=True, exist_ok=True)
    if transcript:
        (d / "transcript.md").write_text(transcript, encoding="utf-8")


def batch(*facts, source="meeting:m1", entities=None):
    return {"source": source, "entities": entities or [], "facts": list(facts)}


def F(text, about=("Склад",), kind="state", key=None, quote="[00:11:37] дословно", **kw):
    return {"kind": kind, "text": text, "about": list(about), "key": key, "quote": quote, **kw}


def test_import_writes_with_source_date_and_entities():
    meeting()
    rep = memory.import_facts(batch(F("Переезд склада назначен на март", key="срок переезда"),
                                    F("Олег согласует только письменно", about=["Олег"], kind="trait"),
                                    entities=[{"name": "Склад", "kind": "project", "aliases": ["складской проект"]}]))
    assert len(rep["added"]) == 2 and not rep["errors"]
    f = memory.fact(rep["added"][0])
    assert f["happened"] == "2026-11-03" and f["source_type"] == "meeting" and f["quote"].startswith("[00:11:37]")
    e = memory.entity("складской проект")
    assert e["name"] == "Склад" and e["kind"] == "project"
    assert "встреча «Планёрка» [00:11:37]" in memory.fact_line(f)


def test_rerun_of_same_source_does_not_duplicate():
    meeting()
    memory.import_facts(batch(F("Переезд склада назначен на март", key="срок переезда")))
    rep = memory.import_facts(batch(F("Переезд склада назначили на март месяц", key="срок переезда")))
    assert rep["updated"] and not rep["added"]
    assert store.scalar("SELECT COUNT(*) FROM facts") == 1


def test_same_key_replaces_previous_with_history():
    meeting("m1", started="2026-10-20T08:00:00+00:00")
    meeting("m2", started="2026-11-03T08:00:00+00:00")
    memory.import_facts(batch(F("Конверсия в оплату — 10%", kind="metric", key="конверсия в оплату")))
    rep = memory.import_facts(batch(F("Конверсия в оплату — 12%", kind="metric", key="Конверсия в оплату"),
                                    source="meeting:m2"))
    old, new = rep["replaced"][0]
    assert store.scalar("SELECT status FROM facts WHERE id=?", (old,)) == "replaced"
    line = memory.fact_line(memory.fact(new))
    assert "12%" in line and "(было: Конверсия в оплату — 10%, 20.10)" in line
    page = memory.page_text("Склад")
    assert "было: Конверсия в оплату — 10% (20.10)" in page
    # старая встреча, разобранная позже, не сменяет свежее — уходит в историю
    meeting("m0", started="2026-10-01T08:00:00+00:00")
    rep = memory.import_facts(batch(F("Конверсия в оплату — 8%", kind="metric", key="конверсия в оплату"),
                                    source="meeting:m0"))
    assert rep["history"] and store.scalar("SELECT status FROM facts WHERE id=?", (new,)) == "active"


def test_director_fact_is_not_replaced_by_claude_but_proposed():
    meeting()
    memory.add_fact(text="Бюджет склада — 2 млн", kind="metric", key="бюджет", about="Склад", by="director",
                    source="manual", when="2026-10-01")
    rep = memory.import_facts(batch(F("Бюджет склада — 1,5 млн", kind="metric", key="бюджет")))
    fid = rep["proposed"][0]
    assert store.scalar("SELECT status FROM facts WHERE id=?", (fid,)) == "proposed"
    p = store.row("SELECT * FROM proposals WHERE kind='memory.fact'")
    proposals.approve(p["id"])
    assert store.scalar("SELECT status FROM facts WHERE id=?", (fid,)) == "active"
    assert store.scalar("SELECT status FROM facts WHERE author='director'") == "replaced"


def test_near_duplicate_known_and_hint_for_similar():
    meeting("m1")
    meeting("m2")
    memory.import_facts(batch(F("Олег согласует договоры только письменно", about=["Олег"], kind="trait")))
    rep = memory.import_facts(batch(F("Олег согласует договоры только письменно!", about=["Олег"], kind="trait"),
                                    F("Олег согласует только письменно, устно — нет", about=["Олег"], kind="trait"),
                                    source="meeting:m2"))
    assert len(rep["known"]) == 1 and len(rep["added"]) == 1 and "похож на" in rep["hints"][0]


def test_secrets_private_and_quotes_are_enforced_by_code():
    meeting("m1")
    meeting("m9", "Личное", private=1)
    store.upsert_chat("telegram", "77", "Семья", "private")
    store.execute("UPDATE chats SET memory_off=1 WHERE chat_id='77'")
    store.add_message({"source": "telegram", "chat_id": "77", "chat_title": "Семья", "msg_id": "1",
                       "text": "Склад", "ts": "2026-11-03T08:00:00+00:00"})
    mid = store.scalar("SELECT id FROM messages")
    rep = memory.import_facts(batch(
        F("Пароль от Kaiten: Qwerty123"),
        F("Карта для оплаты 4111 1111 1111 1111"),
        F("Ключ sk-abcdefghijklmnop1234"),
        F("Паспорт Олега 45 12 345678"),
        F("Склад переезжает", quote=""),
        F("Склад закрыт на ремонт", source="meeting:m9"),
        F("Склад закрыт на ремонт", source=f"telegram:{mid}"),
        F("Склад работает до 20:00")))
    assert len(rep["secrets"]) == 4 and len(rep["private"]) == 2 and len(rep["added"]) == 1
    assert any("нет цитаты" in e for e in rep["errors"])
    assert memory.looks_secret("Номер договора 4111-1111-1111-1112") is None  # не карта: не сходится Луна


def test_cap_per_run():
    meeting()
    words = ["аренда", "охрана", "уборка", "отопление", "погрузчик", "стеллажи", "рампа", "весы", "сканеры", "пропуска",
             "кондиционер", "кровля", "ворота", "освещение", "парковка", "камеры", "сигнализация", "ремонт", "лифт", "вода"]
    rep = memory.import_facts(batch(*[F(f"{w.capitalize()}: договор продлён до {i + 1}.12") for i, w in enumerate(words)]),
                              cap=15)
    assert len(rep["added"]) == 15 and rep["over"] == 5


def test_wrong_hides_fact_everywhere_and_reason_goes_to_lessons():
    from opsdash import skills

    meeting()
    rep = memory.import_facts(batch(F("Склад переезжает в Подольск", key="адрес склада")))
    fid = rep["added"][0]
    assert "Подольск" in recall.recall("склад Подольск")
    memory.set_status(fid, "wrong", "переезжает в Химки, а не в Подольск")
    assert "Подольск" not in recall.recall("склад Подольск").split("Встречи")[0]
    assert "Подольск" not in (memory.page_text("Склад") or "")
    lesson = [r for r in skills.rejected(14) if r["kind"] == "memory.fact"]
    assert lesson and lesson[0]["reject_note"] == "переезжает в Химки, а не в Подольск"
    # тот же факт из того же источника повторный разбор не вернёт
    rep = memory.import_facts(batch(F("Склад переезжает в Подольск", key="адрес склада")))
    assert rep["skipped"] and store.scalar("SELECT status FROM facts WHERE id=?", (fid,)) == "wrong"


def test_forget_removes_fact_and_index_row():
    meeting()
    fid = memory.import_facts(batch(F("Склад работает до 20:00")))["added"][0]
    memory.forget_fact(fid)
    assert not store.scalar("SELECT COUNT(*) FROM facts") and not store.scalar("SELECT COUNT(*) FROM facts_fts")


def test_aliases_merge_and_chunk_links():
    meeting("m1", transcript="[00:01:00] Олег: по РМШ отгрузка в пятницу\n")
    memory.ensure_entity("Ромашка", "company", ["ООО «Ромашка»"])
    dup, _ = memory.ensure_entity("РМШ", "company")
    recall.refresh()
    assert store.scalar("SELECT COUNT(*) FROM chunk_entities WHERE entity_id=?", (dup,)) == 1
    memory.import_facts(batch(F("РМШ платит с отсрочкой 30 дней", about=["РМШ"], kind="trait")))
    res = memory.merge_entities(dup, memory.entity("Ромашка")["id"])
    assert res["into"] == "Ромашка" and memory.entity("рмш")["name"] == "Ромашка"
    assert memory.entity("ооо ромашка")["name"] == "Ромашка"  # кавычки и регистр не важны
    assert "отсрочкой" in memory.page_text("РМШ")
    memory.link_chunks()
    assert store.scalar("SELECT COUNT(*) FROM chunk_entities WHERE entity_id=?", (memory.entity("Ромашка")["id"],)) == 1


def test_word_forms_link_chunks_to_people():
    records.upsert_person("Олег Петров", aliases=["Олег"])
    memory.ensure_entity("Олег Петров")
    meeting("m1", transcript="[00:01:00] Анна: спроси у Олега про склад\n")
    recall.refresh()
    assert store.scalar("SELECT COUNT(*) FROM chunk_entities") == 1


def test_recall_shows_page_and_facts_first():
    meeting("m1", transcript="[00:11:37] Олег: переезд склада в марте\n")
    memory.import_facts(batch(F("Переезд склада назначен на март", key="срок переезда"),
                              entities=[{"name": "Склад", "kind": "project"}]))
    text = recall.recall("переезд склада")
    assert text.startswith("Страница: «Склад» (проект, фактов: 1) — ./ops page \"Склад\"")
    assert text.index("Факты (") < text.index("Встречи (")
    mcp = recall.recall("переезд склада", mcp=True)
    assert 'page("Склад")' in mcp


def test_mcp_facts_from_chats_have_no_quote_or_chat_name():
    store.upsert_chat("telegram", "5", "Олег Петров", "private")
    store.add_message({"source": "telegram", "chat_id": "5", "chat_title": "Олег Петров", "msg_id": "1",
                       "text": "Склад переедет в марте", "ts": "2026-11-03T08:00:00+00:00"})
    mid = store.scalar("SELECT id FROM messages")
    memory.import_facts(batch(F("Склад переедет в марте", quote="Склад переедет в марте"), source=f"telegram:{mid}"))
    f = memory.facts()[0]
    assert "«Олег Петров»" in memory.fact_line(f)
    assert memory.fact_line(f, mcp=True).endswith("переписка, 03.11")


def test_known_lines_for_chat_peer_skip_director(settings_with):
    settings_with(owner={"name": "Анна Иванова"})
    meeting()
    memory.import_facts(batch(F("Олег согласует только письменно", about=["Олег"], kind="trait"),
                              F("Анна Иванова ведёт склад", about=["Анна Иванова"], kind="role")))
    lines = memory.known_lines(["Олег, когда пришлёшь схемы?"], ["Анна Иванова"])
    assert len(lines) == 1 and "письменно" in lines[0]


def test_cli_import_add_list_page(tmp_path, capsys):
    meeting()
    path = tmp_path / "facts.json"
    path.write_text(json.dumps(batch(F("Склад работает до 20:00", key="часы склада"))), encoding="utf-8")
    cli.main(["facts", "import", str(path)])
    assert "Записано фактов: 1" in capsys.readouterr().out
    cli.main(["facts", "add", "--text", "Склад закрыт 31.12", "--about", "Склад", "--by", "director"])
    assert "Запомнено" in capsys.readouterr().out
    cli.main(["facts", "list", "--about", "Склад"])
    out = capsys.readouterr().out
    assert "от вас" in out and "20:00" in out
    cli.main(["page", "Склад"])
    assert "# Склад" in capsys.readouterr().out
    assert (memory.pages_dir() / "Склад.md").exists()
    cli.main(["page"])
    assert "«Склад»" in capsys.readouterr().out


def test_memory_day_parts_and_known(settings_with):
    for i in range(300):
        store.add_message({"source": "telegram", "chat_id": str(i % 5), "chat_title": f"Чат {i % 5}",
                           "msg_id": str(i), "sender_name": "Олег",
                           "text": f"Сообщение {i}: по складу всё идёт по плану, полки " + "длинно " * 30,
                           "ts": f"2026-11-03T{8 + i // 60:02d}:{i % 60:02d}:00+00:00"})
    store.add_message({"source": "telegram", "chat_id": "0", "chat_title": "Чат 0", "msg_id": "x",
                       "text": "ок", "ts": "2026-11-03T08:00:30+00:00"})
    first = memory.day_text("2026-11-03")
    assert "сообщений 300" in first and "Дальше: ./ops memory day --date 2026-11-03 --part 2" in first
    assert len(first.encode("utf-8")) <= 20_000
    store.execute("UPDATE chats SET memory_off=1 WHERE chat_id='1'")
    assert "Чат 1" not in memory.day_text("2026-11-03")


def test_backfill_progress():
    meeting("m1")
    meeting("m2")
    (records.meeting_dir("m1") / "summary.md").write_text("# a", encoding="utf-8")
    (records.meeting_dir("m2") / "summary.md").write_text("# b", encoding="utf-8")
    assert [m["id"] for m in memory.backfill_next(10)] == ["m1", "m2"]
    memory.backfill_done(["m1"])
    st = memory.backfill_state()
    assert st["done"] == 1 and st["todo"] == ["m2"]


def test_entity_forget_all():
    meeting()
    memory.import_facts(batch(F("Склад работает до 20:00"), F("Олег и склад: ключи у Олега", about=["Склад", "Олег"])))
    n = memory.forget_entity(memory.entity("Склад")["id"])
    assert n == 1 and memory.entity("Склад") is None
    assert store.scalar("SELECT COUNT(*) FROM facts") == 1  # общий с Олегом факт остался


def test_errors():
    with pytest.raises(memory.MemoryError):
        memory.parse_source("chatgpt:1")
    rep = memory.import_facts({"facts": [F("Склад", kind="мнение", source="manual")]})
    assert rep["errors"]


# --- веб --------------------------------------------------------------------------------------------------------

@pytest.fixture
def client():
    from starlette.testclient import TestClient

    from opsdash.web.app import app

    return TestClient(app, base_url="http://localhost:8765")


def test_memory_pages_render_and_actions(client):
    meeting("m1", transcript="[00:11:37] Олег: переезд склада в марте\n")
    rep = memory.import_facts(batch(F("Переезд склада назначен на март", key="срок переезда"),
                                    F("Олег согласует только письменно", about=["Олег"], kind="trait"),
                                    entities=[{"name": "Склад", "kind": "project"}]))
    page = client.get("/memory").text
    assert "Переезд склада назначен на март" in page and 'href="/memory"' in page and "Сегодня" in page
    assert "Переезд" in client.get("/memory", params={"q": "переезда"}).text
    pages = client.get("/memory", params={"tab": "pages"}).text
    eid = memory.entity("Склад")["id"]
    assert f'/memory/page/{eid}' in pages
    one = client.get(f"/memory/page/{eid}").text
    assert "Склад" in one and "Переезд склада назначен на март" in one and "Забыть всё о «Склад»" in one
    fid = rep["added"][0]
    r = client.post(f"/memory/fact/{fid}", data={"status": "wrong", "reason": "мелочь"},
                    headers={"referer": "http://localhost:8765/memory"}, follow_redirects=False)
    assert r.status_code == 303 and store.scalar("SELECT status FROM facts WHERE id=?", (fid,)) == "wrong"
    client.post("/memory/fact", data={"text": "Склад закрыт 31.12", "about": "Склад", "kind": "event"},
                headers={"referer": "http://localhost:8765/memory"})
    assert store.scalar("SELECT author FROM facts WHERE text='Склад закрыт 31.12'") == "director"
    olga, _ = memory.ensure_entity("Склад №2", "project")
    client.post(f"/memory/entity/{olga}", data={"action": "merge", "into": str(eid)},
                headers={"referer": "http://localhost:8765/memory"})
    assert memory.entity("Склад №2")["id"] == eid
    client.post("/memory/forget", data={"eid": eid}, headers={"referer": "http://localhost:8765/settings?tab=memory"})
    assert memory.entity("Склад") is None


def test_meeting_block_task_known_and_settings(client):
    meeting("m1", transcript="[00:11:37] Олег: переезд склада в марте\n")
    memory.import_facts(batch(F("Олег согласует только письменно", about=["Олег"], kind="trait")))
    assert "Запомнено из встречи" in client.get("/meetings/m1").text
    tid = records.add_commitment(direction="to_me", who="Олег", what="Прислать схемы склада", title="Схемы склада")
    html = client.get(f"/api/memory/task/{tid}").text
    assert "письменно" in html
    s = client.get("/settings", params={"tab": "memory"}).text
    assert "Не запоминать" in s and "Забыть всё о" in s
    client.post("/settings/memory", data={"enabled": "1", "day": "", "day_time": "19:00", "max_facts_meeting": "10",
                                          "max_facts_day": "30", "agents": "auto"},
                headers={"referer": "http://localhost:8765/settings?tab=memory"})
    from opsdash import config

    st = config.load_settings()
    assert st["memory"]["day"] is False and st["memory"]["max_facts_meeting"] == 10
    assert st["autonomy"]["memory_agents"] == "auto"


def test_kb_is_a_memory_tab(client):
    page = client.get("/kb").text
    assert "Уроки и удачи" in page and 'href="/memory?tab=feed"' in page


def test_private_meeting_menu_toggle(client):
    meeting("m1", transcript="[00:01:00] Олег: склад\n")
    recall.refresh()
    client.post("/meetings/m1/private", data={"on": "1"}, headers={"referer": "http://localhost:8765/meetings/m1"})
    assert store.scalar("SELECT private FROM meetings WHERE id='m1'") == 1
    assert not store.scalar("SELECT COUNT(*) FROM memory_chunks WHERE src='m1'")
    assert "личная" in client.get("/meetings/m1").text


def test_backfill_goes_through_all_portions_in_the_queue(monkeypatch):
    """Наполнение ставит само себя на следующую порцию. В очереди PULSE такая просьба приходит, пока задание ещё
    идёт: раньше очередь её отбрасывала, и после первых 10 встреч наполнение молча останавливалось."""
    import queue
    import threading
    import time

    from opsdash import jobs

    for i in range(5):
        meeting(f"m{i}", started=f"2026-11-0{i + 1}T08:00:00+00:00")
        (records.meeting_dir(f"m{i}") / "summary.md").write_text("# Планёрка\n", encoding="utf-8")
    calls = []

    def fake_claude(prompt, **_):
        calls.append(prompt)
        memory.backfill_done(memory.backfill_state()["todo"][:2])

    monkeypatch.setattr(jobs.brain, "run", fake_claude)
    monkeypatch.setattr(jobs, "_queues", {"heavy": queue.Queue()})
    stop = threading.Event()
    worker = threading.Thread(target=jobs._worker, args=("heavy", stop), daemon=True)
    worker.start()
    try:
        store.set_state("memory_backfill", "running")
        assert jobs.submit("memory-backfill", "heavy")
        deadline = time.time() + 15
        while store.get_state("memory_backfill") != "done" and time.time() < deadline:
            time.sleep(0.05)
    finally:
        stop.set()
        worker.join(5)
    assert len(calls) == 3 and not memory.backfill_state()["todo"]
    assert store.get_state("memory_backfill") == "done" and not jobs._pending and not jobs._again


def test_running_job_is_queued_again_only_when_it_asks(monkeypatch):
    import queue

    from opsdash import jobs

    key = jobs._key("memory-index", None)
    monkeypatch.setattr(jobs, "_queues", {"light": queue.Queue()})
    monkeypatch.setattr(jobs, "_pending", {key})
    monkeypatch.setattr(jobs, "_running", {key})
    monkeypatch.setattr(jobs, "_again", {})
    assert not jobs.submit("memory-index")  # попросил кто-то другой — как и раньше, второй раз не встаёт
    assert jobs.submit("memory-index", again=True) and jobs._again[key] == ("memory-index", "light", None)
    assert jobs._queues["light"].empty()  # в очередь встанет, когда закончит (jobs._worker)


def test_stalled_backfill_is_resumed_by_tick(monkeypatch, settings_with):
    """Наполнение «идёт», а задания нет — PULSE перезапустили или Claude упал: планировщик продолжает сам."""
    from opsdash import jobs

    settings_with(schedule={"digest_morning": "", "digest_evening": "", "stale_check": ""}, memory={"day": False})
    meeting("m1")
    (records.meeting_dir("m1") / "summary.md").write_text("# Планёрка\n", encoding="utf-8")
    submitted = []
    monkeypatch.setattr(jobs, "submit", lambda name, kind="light", args=None: submitted.append(name) or True)
    store.set_state("memory_backfill", "running")
    assert "memory-backfill" in jobs.tick()
    submitted.clear()
    store.execute("INSERT INTO jobs(name, last_run) VALUES('memory-backfill', ?) "
                  "ON CONFLICT(name) DO UPDATE SET last_run=excluded.last_run", (store.utcnow(),))
    assert "memory-backfill" not in jobs.tick()  # только что запускали — ждём BACKFILL_RETRY минут
    store.set_state("memory_backfill", "stopped")
    store.execute("UPDATE jobs SET last_run='2026-01-01T00:00:00+00:00' WHERE name='memory-backfill'")
    assert "memory-backfill" not in jobs.tick()  # остановлено пользователем — не трогаем
