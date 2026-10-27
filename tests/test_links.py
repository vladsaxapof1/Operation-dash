from fastapi.testclient import TestClient

from opsdash import links, records, store


def test_original_links(settings_with):
    settings_with(gmail={"email": "anna@gmail.com"})
    store.upsert_chat("telegram", "-1001234", "Склад", "group", username="sklad_team")
    store.upsert_chat("telegram", "-1005678", "Закрытая", "group", username="")
    store.upsert_chat("telegram", "42", "Тимур", "private", username="timur_k")
    store.upsert_chat("telegram", "43", "Без ника", "private", username="")
    store.upsert_chat("telegram", "-4900000001", "Проект Сайт", "group", username="")

    assert links.original("telegram", "-1001234", 77) == ("https://t.me/sklad_team/77", "К сообщению")
    assert links.original("telegram", "-1005678", 77) == ("https://t.me/c/5678/77", "К сообщению")
    assert links.original("telegram", "42", 77) == ("https://t.me/timur_k", "Открыть чат")  # в личке — только чат
    assert links.original("telegram", "43", 77) == ("https://web.telegram.org/a/#43", "Чат в Telegram Web")
    assert links.original("telegram", "-4900000001", 77) == (
        "https://web.telegram.org/a/#-4900000001", "Группа в Telegram Web")  # у обычных групп ссылок нет
    assert links.original("telegram", "-1005678") == ("https://web.telegram.org/a/#-1005678", "Чат в Telegram Web")

    url, label = links.original("mail", "gmail:INBOX", "<CAF+abc@mail.gmail.com>", "maria@x.ru")
    assert url == "https://mail.google.com/mail/u/anna%40gmail.com/#search/rfc822msgid%3ACAF%2Babc%40mail.gmail.com"
    assert label == "Письмо в Gmail"
    assert links.original("mail", "INBOX", "<1@x>", "maria@x.ru") == (
        "https://mail.yandex.ru/#search?request=maria%40x.ru", "В Яндекс Почте")
    assert links.original("ymessenger", "0/0/abc", 5) is None


def test_attention_and_chat_show_original_button():
    from opsdash.web.app import app

    store.upsert_chat("telegram", "-4900000001", "Проект Сайт", "group", enabled=1, username="")
    store.add_message({"source": "telegram", "chat_id": "-4900000001", "chat_title": "Проект Сайт", "msg_id": "694666",
                       "sender_id": "7", "sender_name": "Валерия", "is_outgoing": False, "text": "Согласуем метрики?",
                       "ts": "2026-10-26T09:00:00+00:00", "link": None})
    mid = store.scalar("SELECT id FROM messages WHERE msg_id='694666'")
    records.add_attention(summary="Согласовать метрики", message_id=mid)
    client = TestClient(app, base_url="http://localhost:8765")
    page = client.get("/inbox").text
    assert 'href="https://web.telegram.org/a/#-4900000001"' in page and "↗ Группа в Telegram Web" in page
    chat = client.get("/inbox?chat=telegram|-4900000001").text
    assert chat.count("Группа в Telegram Web") == 2  # и у карточки, и у выбранного чата


def test_broken_private_links_cleared():
    store.add_message({"source": "telegram", "chat_id": "100000042", "chat_title": "Тимур", "msg_id": "1",
                       "sender_id": "1", "sender_name": "Х", "is_outgoing": False, "text": "привет",
                       "ts": "2026-10-26T09:00:00+00:00", "link": "https://t.me/timur_k/1"})
    store._initialized.clear()  # как при новом запуске PULSE
    assert store.scalar("SELECT link FROM messages WHERE msg_id='1'") is None
