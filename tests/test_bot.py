import json
from datetime import datetime

import httpx
import pytest

from opsdash import bot, config, jobs, proposals, records, store


class FakeTelegram:
    def __init__(self):
        self.sent = []
        self.calls = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content or b"{}")
        self.calls.append((method, body))
        if method == "sendMessage":
            self.sent.append(body)
            return httpx.Response(200, json={"ok": True, "result": {"message_id": len(self.sent)}})
        if method == "getMe":
            return httpx.Response(200, json={"ok": True, "result": {"username": "pult_bot"}})
        return httpx.Response(200, json={"ok": True, "result": True})


@pytest.fixture
def tg(settings_with, monkeypatch):
    fake = FakeTelegram()
    config.set_secret("TELEGRAM_BOT_TOKEN", "123:abc")
    settings_with(telegram_bot={"enabled": True, "quiet_hours": {"start": "", "end": ""}})
    real = bot.Bot

    def make(token, transport=None):
        return real(token, transport=httpx.MockTransport(fake.handler))

    monkeypatch.setattr(bot, "Bot", make)
    monkeypatch.setattr(bot, "get_bot", lambda transport=None: make("123:abc"))
    return fake


def msg(chat_id, text):
    return {"update_id": 1, "message": {"message_id": 7, "chat": {"id": chat_id}, "text": text}}


def test_pairing_only_with_code(tg):
    b = bot.get_bot()
    bot.handle_update(b, msg(555, "/start 000000"))
    assert config.load_settings()["telegram_bot"]["owner_chat_id"] == ""
    assert "личный бот" in tg.sent[-1]["text"]
    bot.handle_update(b, msg(555, f"/start {bot.pair_code()}"))
    assert config.load_settings()["telegram_bot"]["owner_chat_id"] == "555"
    assert "привязан" in tg.sent[-1]["text"]
    # чужой чат игнорируется
    n = len(tg.sent)
    bot.handle_update(b, msg(999, "/today"))
    assert len(tg.sent) == n


def test_commands_and_ask_goes_to_queue(tg, monkeypatch):
    config.update_settings("telegram_bot", {"owner_chat_id": "555"})
    b = bot.get_bot()
    records.add_commitment(direction="to_me", who="Пётр", what="Прогноз по клиентам", due="2026-10-30")
    bot.handle_update(b, msg(555, "/promises"))
    assert "Прогноз по клиентам" in tg.sent[-1]["text"]
    bot.handle_update(b, msg(555, "/today"))
    assert "Сводка" in tg.sent[-1]["text"]
    submitted = []
    monkeypatch.setattr(jobs, "submit", lambda name, kind="light", args=None: submitted.append((name, args)) or True)
    bot.handle_update(b, msg(555, "что мне обещал Петров?"))
    assert submitted == [("ask", {"text": "что мне обещал Петров?", "chat_id": "555", "reply_to": 7})]


def test_notify_with_batch_buttons_and_callback_approve(tg, monkeypatch):
    config.update_settings("telegram_bot", {"owner_chat_id": "555"})
    pid = store.execute(
        "INSERT INTO proposals(kind, title, payload, batch, status, created) VALUES('kaiten.comment','Комментарий',?, 'meeting:m1','pending',?)",
        (json.dumps({"card_id": 1, "text": "x"}), store.utcnow()))
    bot.notify("<b>Разобрала встречу</b>", batch="meeting:m1")
    sent = tg.sent[-1]
    assert "Ждут вашего «да»" in sent["text"]
    datas = [b["callback_data"] for row in sent["reply_markup"]["inline_keyboard"] for b in row]
    assert f"p:a:{pid}" in datas
    assert store.scalar("SELECT COUNT(*) FROM outbox WHERE sent_at IS NOT NULL") == 1

    monkeypatch.setattr(proposals, "execute", lambda p: {"card_id": 1})
    cb = {"update_id": 2, "callback_query": {"id": "cb1", "data": f"p:a:{pid}", "from": {"id": 555},
                                             "message": {"message_id": 1, "chat": {"id": 555}, "reply_markup": sent["reply_markup"]}}}
    bot.handle_update(bot.get_bot(), cb)
    assert proposals.get(pid)["status"] == "done"
    # чужой callback не выполняется
    pid2 = store.execute(
        "INSERT INTO proposals(kind, title, payload, status, created) VALUES('kaiten.comment','Другой','{}','pending',?)",
        (store.utcnow(),))
    cb["callback_query"].update({"data": f"p:a:{pid2}", "message": {"message_id": 1, "chat": {"id": 1}}})
    bot.handle_update(bot.get_bot(), cb)
    assert proposals.get(pid2)["status"] == "pending"


def test_quiet_hours_hold_non_urgent(tg):
    config.update_settings("telegram_bot", {"owner_chat_id": "555"})
    assert bot.in_quiet_hours({"telegram_bot": {"quiet_hours": {"start": "21:00", "end": "08:30"}}},
                              datetime(2026, 10, 25, 23, 0, tzinfo=config.tz()))
    assert not bot.in_quiet_hours({"telegram_bot": {"quiet_hours": {"start": "21:00", "end": "08:30"}}},
                                  datetime(2026, 10, 25, 12, 0, tzinfo=config.tz()))
    now = config.now().strftime("%H:%M")
    config.update_settings("telegram_bot", {"quiet_hours": {"start": "00:00", "end": "23:59"}})
    if "00:00" <= now < "23:59":
        bot.notify("обычное")
        bot.notify("срочное", urgent=True)
        texts = [s["text"] for s in tg.sent]
        assert "срочное" in texts and "обычное" not in texts


def test_nudge_creates_comment_with_mention(tg, monkeypatch):
    store.execute("INSERT INTO kaiten_users(id, full_name, username) VALUES(2, 'Пётр Иванов', 'petr')")
    store.execute(
        "INSERT INTO kaiten_cards(id, title, state, condition, responsible_id, last_moved_at) VALUES(77, 'Склад', 2, 1, 2, ?)",
        ("2026-10-01T10:00:00+00:00",))
    executed = []
    monkeypatch.setattr(proposals, "execute", lambda p: executed.append(p["payload"]) or {"card_id": 77})
    p = bot.nudge_card(77)
    assert p["status"] == "done"
    assert executed[0]["text"].startswith("@petr, карточка без движения")
    assert store.row("SELECT until FROM kaiten_snooze WHERE card_id=77")["until"]
