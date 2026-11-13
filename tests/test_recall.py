"""«Вспомнить» (opsdash.recall): нарезка, основы слов, индекс, поиск по всему, бюджет выдачи, личное."""

import json

from opsdash import anthology, cli, recall, records, store


def msg(i, chat, text, ts, who="Олег", source="telegram"):
    store.add_message({"source": source, "chat_id": chat, "chat_title": f"Чат {chat}", "msg_id": str(i),
                       "sender_name": who, "text": text, "ts": ts})


def meeting(mid, title, transcript="", summary="", private=0):
    store.execute("INSERT INTO meetings(id, title, started_at, status, private) VALUES(?,?,?,?,?)",
                  (mid, title, "2026-11-03T08:00:00+00:00", "processed", private))
    d = records.meeting_dir(mid)
    d.mkdir(parents=True, exist_ok=True)
    if transcript:
        (d / "transcript.md").write_text(transcript, encoding="utf-8")
    if summary:
        (d / "summary.md").write_text(summary, encoding="utf-8")


def test_stems_and_query_take_any_word_form():
    assert recall.stems("Переезда склада по плану") == ["переезд", "склад", "план"]
    assert recall.fts_query("переезд склада КП") == '"переезд"* OR "склад"* OR "кп"'
    assert recall.fts_query("и по на") == ""


def test_message_windows_break_on_pause_size_and_long_messages():
    rows = [{"id": 1, "source": "telegram", "chat_id": "1", "chat_title": "Склад", "sender_name": "Олег",
             "text": "Схемы пришлю в пятницу", "ts": "2026-11-03T08:00:00+00:00"},
            {"id": 2, "source": "telegram", "chat_id": "1", "chat_title": "Склад", "sender_name": None,
             "is_outgoing": 1, "text": "Ок", "ts": "2026-11-03T08:10:00+00:00"},
            {"id": 3, "source": "telegram", "chat_id": "1", "chat_title": "Склад", "sender_name": "Олег",
             "text": "Через час уже другое окно", "ts": "2026-11-03T09:30:00+00:00"},
            {"id": 4, "source": "telegram", "chat_id": "1", "chat_title": "Склад", "sender_name": "Олег",
             "text": "Длинное. " * 300, "ts": "2026-11-03T09:31:00+00:00"}]
    out = recall.message_chunks(rows)
    assert [c["msg_ids"] for c in out[:2]] == [[1, 2], [3]]
    assert out[0]["text"] == "Олег: Схемы пришлю в пятницу\nВы: Ок" and out[0]["pos"] == "[1]"
    long = [c for c in out if c["msg_ids"] == [4]]
    assert len(long) >= 3 and all(len(c["text"]) <= recall.PIECE + 10 for c in long)


def test_meeting_windows_keep_timecodes_and_summary_sections():
    lines = "\n".join(f"[00:0{i}:00] Спикер {i}: " + "слово " * 60 for i in range(6))
    out = recall.meeting_chunks("m", "Планёрка", None, lines, "# Планёрка\n\n## Решения\n1. Переезд склада переносим на март [00:11:37]\n"
                                                         "## Задачи\n| Что | Кто |\n|---|---|\n| Схемы | Олег |\n")
    meet = [c for c in out if c["kind"] == "meet"]
    assert meet[0]["pos"] == "[00:00:00]" and len(meet) >= 2
    assert meet[1]["text"].splitlines()[0] == meet[0]["text"].splitlines()[-1]  # одна реплика внахлёст
    assert [c["pos"] for c in out if c["kind"] == "sum"] == ["Решения", "Задачи"]


def test_refresh_is_incremental_and_keeps_unchanged_chunks():
    msg(1, "1", "Схемы процессов склада пришлю в пятницу", "2026-11-03T08:00:00+00:00")
    msg(2, "2", "Бюджет на декабрь согласован", "2026-11-03T08:00:00+00:00")
    first = recall.refresh()
    assert first["added"] == 2 and first["chats"] == 2
    keep = store.scalar("SELECT id FROM memory_chunks WHERE src='telegram:2'")
    msg(3, "1", "И отгрузку тоже", "2026-11-03T08:05:00+00:00")
    again = recall.refresh()
    assert again["chats"] == 1 and again["added"] == 1 and again["removed"] == 1  # окно чата 1 выросло
    assert store.scalar("SELECT id FROM memory_chunks WHERE src='telegram:2'") == keep
    assert recall.refresh()["added"] == 0


def test_messages_search_finds_word_forms_best_first(capsys):
    msg(1, "1", "Переезд склада переносим на март", "2026-11-01T08:00:00+00:00")
    msg(2, "2", "Про склад потом", "2026-11-12T08:00:00+00:00")
    msg(3, "3", "Обед в час", "2026-11-12T09:00:00+00:00")
    cli.main(["messages", "search", "переезды складов", "--days", "3650"])
    lines = capsys.readouterr().out.strip().splitlines()
    assert "[1]" in lines[0] and "[2]" in lines[1] and len(lines) == 2  # больше общих слов — выше, хоть и старше
    cli.main(["messages", "search", "@oleg_s", "--days", "3650"])
    assert capsys.readouterr().out.strip() == "Ничего не нашлось."
    msg(4, "1", "Пиши @oleg_s", "2026-11-12T10:00:00+00:00")
    cli.main(["messages", "search", "@oleg_s", "--days", "3650"])
    assert "[4]" in capsys.readouterr().out  # не нашлось по словам — точная подстрока, как раньше


def test_private_meeting_not_indexed_and_not_found(capsys):
    meeting("m1", "Склад", "[00:01:00] Олег: переезд склада в марте\n")
    meeting("m2", "Личное", "[00:01:00] Олег: переезд квартиры и склада вещей\n")
    recall.refresh()
    assert {r["src"] for r in store.rows("SELECT src FROM memory_chunks")} == {"m1", "m2"}
    records.set_private("m2", True)
    assert {r["src"] for r in store.rows("SELECT src FROM memory_chunks")} == {"m1"}
    recall.refresh()  # подпись встречи сменилась — снова не попадает
    assert {r["src"] for r in store.rows("SELECT src FROM memory_chunks")} == {"m1"}
    text = recall.recall("переезд склада")
    assert "m1 ·" in text and "m2" not in text
    cli.main(["meetings", "search", "переезд"])
    out = capsys.readouterr().out
    assert "m1/transcript.md [00:01:00]" in out and "m2" not in out


def test_private_backfill_from_summary_title():
    meeting("m3", "Созвон", "[00:00:01] А: привет\n", "# Личная встреча\n\nНе о работе.\n")
    recall.refresh()
    assert store.scalar("SELECT private FROM meetings WHERE id='m3'") == 1
    assert not store.scalar("SELECT COUNT(*) FROM memory_chunks WHERE src='m3'")


def test_recall_everything_in_order_within_budget_and_logged(monkeypatch):
    monkeypatch.setenv("OPSDASH_JOB", "chat")
    tid = records.add_commitment(direction="to_me", who="Олег", what="Прислать схемы процессов склада",
                                 title="Схемы процессов склада")
    anthology.add(kind="decision", title="Подрядчика по складу выбираем после схем процессов", project="Склад")
    meeting("m1", "Планёрка", "[00:11:37] Олег: схемы процессов склада будут в пятницу\n",
            "# Планёрка\n\n## Решения\n1. Склад: подрядчика выбираем после схем процессов [00:11:37]\n")
    for i in range(40):
        msg(100 + i, str(i), f"Схемы процессов склада: версия {i}, " + "подробности " * 40,
            f"2026-11-0{1 + i % 9}T08:00:00+00:00")
    text = recall.recall("схемы процессов склада")
    order = [text.index(h) for h in ("Задачи (", "Решения (", "Встречи (", "Переписка (")]
    assert order == sorted(order)
    assert f"[{tid}]" in text and "m1 ·" in text and "[00:11:37]" in text or "Решения" in text
    assert len(text.encode("utf-8")) <= recall.BUDGETS["mid"]
    assert "Ещё:" in text and "--kind messages" in text
    big = recall.recall("схемы процессов склада", kinds=["messages"], budget="high")
    assert len(big.encode("utf-8")) <= recall.BUDGETS["high"] and big.count("[1") > 4
    log = store.rows("SELECT who, query, found, mode FROM recall_log ORDER BY id")
    assert log[0]["who"] == "chat" and log[0]["mode"] == "слова"
    assert json.loads(log[0]["found"])["messages"] == 40


def test_recall_about_and_since_narrow_down():
    msg(1, "1", "Склад: Олег пришлёт схемы", "2026-11-03T08:00:00+00:00", who="Анна")
    msg(2, "2", "Склад: Пётр пришлёт смету", "2026-11-10T08:00:00+00:00", who="Анна")
    assert "Олег" in recall.recall("склад", about="Олег") and "Пётр" not in recall.recall("склад", about="Олег")
    text = recall.recall("склад", since="2026-11-05")
    assert "смету" in text and "схемы" not in text


def test_recall_errors():
    import pytest

    with pytest.raises(recall.RecallError):
        recall.recall("и на")
    with pytest.raises(recall.RecallError):
        recall.recall("склад", budget="huge")
