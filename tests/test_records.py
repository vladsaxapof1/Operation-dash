from datetime import timedelta

from opsdash import config, records, store


def test_commitments_dedupe_overdue_and_close():
    yesterday = (config.now().date() - timedelta(days=1)).isoformat()
    a = records.add_commitment(direction="to_me", who="Пётр", what="Прислать прогноз", due=yesterday)
    b = records.add_commitment(direction="to_me", who="Пётр", what="прислать прогноз")
    assert a == b, "то же открытое обещание не дублируется"
    items = records.list_commitments(direction="to_me")
    assert len(items) == 1 and items[0]["overdue"]
    assert records.list_commitments(overdue=True)[0]["id"] == a

    records.update_commitment(a, status="done")
    assert records.list_commitments() == []
    done = records.list_commitments(status="done")[0]
    assert done["closed_at"] and not done["overdue"]


def test_commitment_rejects_bad_direction_and_date():
    import pytest

    with pytest.raises(ValueError):
        records.add_commitment(direction="maybe", what="x")
    with pytest.raises(ValueError):
        records.add_commitment(direction="mine", what="x", due="30.10.2026")


def test_people_upsert_merges_aliases_and_find():
    pid = records.upsert_person("Пётр Иванов", aliases=["Петя"], telegram="@p_ivanov")
    same = records.upsert_person("Пётр Иванов", aliases=["Иванов"], role="продажи")
    assert pid == same
    p = records.find_person("петя")
    assert p["id"] == pid and set(p["aliases"]) == {"Петя", "Иванов"} and p["role"] == "продажи"
    assert records.find_person("@p_ivanov")["id"] == pid
    assert records.find_person("Иван")["id"] == pid  # частичное совпадение, единственный кандидат
    assert records.find_person("Сидоров") is None


def test_attention_autoclose_in_group_only_by_reply():
    store.upsert_chat("telegram", "-5", "Операционка", "group", enabled=1)
    store.add_message({"source": "telegram", "chat_id": "-5", "chat_title": "Операционка", "msg_id": "1",
                       "sender_name": "Олег", "text": "Анна, что ответить клиенту?", "ts": "2026-10-25T08:00:00+00:00"})
    mid = store.row("SELECT id FROM messages WHERE msg_id='1'")["id"]
    records.add_attention(message_id=mid, summary="Олег ждёт ответа клиенту")
    store.add_message({"source": "telegram", "chat_id": "-5", "chat_title": "Операционка", "msg_id": "2",
                       "sender_name": "Я", "is_outgoing": True, "text": "Гляну после обеда", "ts": "2026-10-25T08:05:00+00:00"})
    assert records.autoclose_answered() == 0, "обычное сообщение в группе — не ответ"
    store.add_message({"source": "telegram", "chat_id": "-5", "chat_title": "Операционка", "msg_id": "3",
                       "sender_name": "Я", "is_outgoing": True, "text": "14 дней", "ts": "2026-10-25T09:00:00+00:00",
                       "reply_to": "1"})
    assert records.autoclose_answered() == 1


def test_attention_autoclose_after_reply():
    store.upsert_chat("telegram", "1", "Пётр", "private", enabled=1)
    store.add_message({"source": "telegram", "chat_id": "1", "chat_title": "Пётр", "msg_id": "10",
                       "sender_name": "Пётр", "text": "Согласуете?", "ts": "2026-10-25T09:00:00+00:00"})
    mid = store.row("SELECT id FROM messages")["id"]
    aid = records.add_attention(message_id=mid, summary="Пётр ждёт согласования", priority="high")
    assert records.add_attention(message_id=mid, summary="обновлено") == aid  # без дублей
    assert records.autoclose_answered() == 0
    store.add_message({"source": "telegram", "chat_id": "1", "chat_title": "Пётр", "msg_id": "11",
                       "sender_name": "Я", "is_outgoing": True, "text": "Да", "ts": "2026-10-25T09:05:00+00:00"})
    assert records.autoclose_answered() == 1
    assert records.list_attention("open") == []


def test_settings_roundtrip_and_hhmm(settings_with):
    s = settings_with(owner={"name": "Анна"})
    assert s["owner"]["name"] == "Анна" and s["owner"]["timezone"] == "Europe/Moscow"
    assert config.hhmm(1110) == "18:30"
    assert config.hhmm("9:05") == "09:05"
    config.set_secret("KAITEN_TOKEN", "abc\n")
    assert config.get_secret("KAITEN_TOKEN") == "abc"
    assert config.mask("1234567890abc") == "123••••abc"
