"""Регрессии по итогам ревью: каждое исправление — отдельный тест."""

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from opsdash import bot, cli, config, proposals, store, transcribe
from tests.test_bot import FakeTelegram


def add_proposal(title, batch=None, kind="kaiten.comment", payload=None):
    return store.execute(
        "INSERT INTO proposals(kind, title, payload, batch, status, created) VALUES(?,?,?,?,'pending',?)",
        (kind, title, json.dumps(payload or {"card_id": 1, "text": "x"}), batch, store.utcnow()))


@pytest.fixture
def tg(settings_with, monkeypatch):
    fake = FakeTelegram()
    config.set_secret("TELEGRAM_BOT_TOKEN", "123:abc")
    settings_with(telegram_bot={"enabled": True, "owner_chat_id": "555", "quiet_hours": {"start": "", "end": ""}})
    real = bot.Bot
    make = lambda token, transport=None: real(token, transport=httpx.MockTransport(fake.handler))  # noqa: E731
    monkeypatch.setattr(bot, "Bot", make)
    monkeypatch.setattr(bot, "get_bot", lambda transport=None: make("123:abc"))
    monkeypatch.setattr(proposals, "execute", lambda p: {"card_id": 1})
    return fake


def test_batch_button_approves_only_what_was_shown(tg):
    a = add_proposal("A", "triage:d")
    b = add_proposal("B", "triage:d")
    bot.notify("утро", batch="triage:d")
    markup = tg.sent[-1]["reply_markup"]
    all_btn = next(btn["callback_data"] for row in markup["inline_keyboard"] for btn in row if btn["text"].startswith("✅ Все"))
    later = add_proposal("C — добавлено позже", "triage:d")
    cb = {"update_id": 9, "callback_query": {"id": "1", "data": all_btn, "from": {"id": 555},
                                             "message": {"message_id": 1, "chat": {"id": 555}, "reply_markup": markup}}}
    bot.handle_update(bot.get_bot(), cb)
    assert proposals.get(a)["status"] == proposals.get(b)["status"] == "done"
    assert proposals.get(later)["status"] == "pending"


def test_web_batch_respects_top():
    a = add_proposal("A", "meeting:m")
    later = add_proposal("B", "meeting:m")
    from opsdash.web.app import app

    c = TestClient(app, base_url="http://localhost:8765")
    original = proposals.execute
    proposals.execute = lambda p: {"card_id": 1}
    try:
        c.post("/proposals/batch", data={"batch": "meeting:m", "top": str(a)})
    finally:
        proposals.execute = original
    assert proposals.get(a)["status"] == "done" and proposals.get(later)["status"] == "pending"


def test_unresolved_person_is_not_for_me(settings_with):
    settings_with(kaiten={"targets": [{"name": "Опер", "board_id": 1}], "default_target": "Опер"},
                  autonomy={"kaiten_create_for_me": "auto", "kaiten_create_for_others": "confirm"})
    store.set_state("kaiten_me", {"id": 1})
    p = proposals.propose_kaiten_card(title="Задача", responsible="Иванова")
    assert p["status"] == "pending", "не найденный в Kaiten человек — коллега, нужно подтверждение"


def test_outbox_dead_letter_and_no_double_send(tg, monkeypatch):
    oid_bad = store.execute("INSERT INTO outbox(text, buttons, created) VALUES('', '[]', ?)", (store.utcnow(),))
    calls = {"n": 0}
    real_handler = tg.handler

    def handler(request):
        body = json.loads(request.content or b"{}")
        if request.url.path.endswith("sendMessage") and not body.get("text"):
            calls["n"] += 1
            return httpx.Response(400, json={"ok": False, "description": "Bad Request: message text is empty"})
        return real_handler(request)

    monkeypatch.setattr(tg, "handler", handler)
    bot.notify("второе")
    assert store.row("SELECT dead FROM outbox WHERE id=?", (oid_bad,))["dead"] == 1
    assert [m["text"] for m in tg.sent] == ["второе"], "битое сообщение не блокирует очередь"
    # захваченное другим отправителем сообщение не уходит второй раз
    oid = store.execute("INSERT INTO outbox(text, buttons, created, sending_at) VALUES('занято', '[]', ?, ?)",
                        (store.utcnow(), store.utcnow()))
    bot.flush_outbox()
    assert store.row("SELECT sent_at FROM outbox WHERE id=?", (oid,))["sent_at"] is None


def test_old_outbox_is_expired_not_sent(tg):
    store.execute("INSERT INTO outbox(text, buttons, created) VALUES('позавчера', '[]', '2020-01-01T00:00:00+00:00')")
    bot.flush_outbox()
    assert tg.sent == []
    assert store.row("SELECT dead FROM outbox")["dead"] == 1


def test_nudge_twice_posts_one_comment(tg, monkeypatch):
    executed = []
    monkeypatch.setattr(proposals, "execute", lambda p: executed.append(p["id"]) or {"card_id": 77})
    store.execute("INSERT INTO kaiten_cards(id, title, state, condition, last_moved_at) VALUES(77, 'Склад', 2, 1, ?)",
                  ("2026-10-01T10:00:00+00:00",))
    bot.nudge_card(77)
    bot.nudge_card(77)
    assert len(executed) == 1


def test_background_claude_cannot_approve(monkeypatch, capsys):
    pid = add_proposal("X")
    monkeypatch.setenv("OPSDASH_AGENT", "1")
    with pytest.raises(SystemExit):
        cli.main(["--json", "proposals", "approve", str(pid)])
    assert proposals.get(pid)["status"] == "pending"
    assert "фоновом режиме" in capsys.readouterr().err


def test_markdown_escapes_html_and_sets_csp():
    from opsdash.web.app import app, f_md

    html = f_md('Привет <img src=x onerror="alert(1)"> и [ссылка](javascript:alert(2))\n\n> цитата')
    assert "<img" not in html and "&lt;img" in html
    assert "javascript:" not in html and "<blockquote>" in html
    r = TestClient(app, base_url="http://localhost:8765").get("/health")
    assert "script-src 'self'" in r.headers["content-security-policy"] and r.headers["x-frame-options"] == "DENY"


def test_text_transcript_in_cp1251():
    assert transcribe.read_text_any("Пётр: склад".encode("cp1251")) == "Пётр: склад"
    assert transcribe.read_text_any("﻿Анна".encode("utf-8")) == "Анна"
    assert transcribe.read_text_any("Олег".encode("utf-16")) == "Олег"


def test_bot_errors_hide_token():
    def boom(request):
        raise httpx.ConnectError("connect failed for https://api.telegram.org/bot123:SECRET/getMe")

    b = bot.Bot.__new__(bot.Bot)
    b.token = "123:SECRET"
    b.http = httpx.Client(transport=httpx.MockTransport(boom))
    with pytest.raises(bot.BotError) as e:
        b.me()
    assert "SECRET" not in str(e.value)
