"""Яндекс Мессенджер: бот организации в Яндекс 360.

Ограничение платформы: бот видит только чаты, в которые его добавили
(и личные сообщения самому боту). Личную переписку директора с коллегами
официальный API не отдаёт. Бота создаёт администратор Яндекс 360.
Документация: https://yandex.ru/dev/messenger/doc/ru/
"""

from __future__ import annotations

from datetime import datetime, timezone

import httpx

from opsdash import config, store

SOURCE = "ymessenger"
API = "https://botapi.messenger.yandex.net/bot/v1"


class YMError(Exception):
    pass


def _http(transport: httpx.BaseTransport | None = None) -> httpx.Client:
    token = config.get_secret("YM_BOT_TOKEN")
    if not token:
        raise YMError("Не задан токен бота Яндекс Мессенджера")
    return httpx.Client(base_url=API, headers={"Authorization": f"OAuth {token}"}, timeout=30, transport=transport)


def _get_updates(http: httpx.Client, offset: int, limit: int = 100) -> list[dict]:
    resp = http.get("/messages/getUpdates/", params={"limit": limit, "offset": offset})
    data = resp.json() if resp.content else {}
    if resp.status_code >= 400 or not data.get("ok", False):
        raise YMError(f"getUpdates: {resp.status_code} {data.get('description') or resp.text[:200]}")
    return data.get("updates") or []


def _text(u: dict) -> str:
    if u.get("text"):
        return u["text"]
    if u.get("file"):
        return f"[файл: {u['file'].get('name', '')}]"
    if u.get("images"):
        return "[изображение]"
    if u.get("sticker"):
        return "[стикер]"
    if u.get("forwarded_messages"):
        parts = [f"↪ {f.get('from', {}).get('display_name', '')}: {f.get('text', '')}" for f in u["forwarded_messages"]]
        return "\n".join(parts)
    return ""


def parse_update(u: dict, my_login: str = "") -> dict | None:
    chat = u.get("chat") or {}
    sender = u.get("from") or {}
    text = _text(u)
    if not text:
        return None
    if chat.get("type") == "private" or not chat.get("id"):
        chat_id = f"private:{sender.get('login') or sender.get('id')}"
        title = f"Личный: {sender.get('display_name') or sender.get('login') or ''}".strip()
        kind = "private"
    else:
        chat_id = str(chat["id"])
        title = chat.get("title") or f"Чат {chat_id[-6:]}"
        kind = chat.get("type") or "group"
    ts = datetime.fromtimestamp(int(u.get("timestamp") or 0), tz=timezone.utc)
    login = (sender.get("login") or "").lower()
    return {
        "source": SOURCE,
        "chat_id": chat_id,
        "chat_title": title,
        "kind": kind,
        "msg_id": str(u.get("message_id") or u.get("update_id")),
        "sender_id": login or str(sender.get("id") or ""),
        "sender_name": sender.get("display_name") or sender.get("login") or "",
        "is_outgoing": bool(my_login and login == my_login.lower()),
        "text": text[:8000],
        "ts": ts.replace(microsecond=0).isoformat(),
        "reply_to": str(u["thread_id"]) if u.get("thread_id") else None,
        "link": None,
    }


def collect(settings: dict | None = None, transport: httpx.BaseTransport | None = None) -> str:
    settings = settings or config.load_settings()
    my_login = settings["ymessenger"].get("my_login") or ""
    offset = int(store.get_state("ym_offset", 0) or 0)
    known = {r["chat_id"]: r for r in store.rows("SELECT * FROM chats WHERE source=?", (SOURCE,))}
    new_total = 0
    with _http(transport) as http:
        for _ in range(50):
            updates = _get_updates(http, offset)
            if not updates:
                break
            for u in updates:
                offset = max(offset, int(u.get("update_id", 0)) + 1)
                msg = parse_update(u, my_login)
                if not msg:
                    continue
                if msg["chat_id"] not in known:
                    # бота добавили в чат сознательно — читаем по умолчанию
                    store.upsert_chat(SOURCE, msg["chat_id"], msg["chat_title"], msg["kind"], enabled=1)
                    known[msg["chat_id"]] = {"enabled": 1, "title": msg["chat_title"]}
                if known[msg["chat_id"]].get("enabled") != 1:
                    continue
                msg["chat_title"] = known[msg["chat_id"]].get("title") or msg["chat_title"]
                new_total += int(store.add_message(msg))
            store.set_state("ym_offset", offset)
            if len(updates) < 100:
                break
    return f"новых сообщений: {new_total}"


def check(settings: dict | None = None, transport: httpx.BaseTransport | None = None) -> str:
    offset = int(store.get_state("ym_offset", 0) or 0)
    with _http(transport) as http:
        _get_updates(http, offset, limit=1)
    return "токен принят"
