"""Ссылки «открыть в оригинале»: сообщение или чат в Telegram и Пачке, письмо в Gmail, поиск в Яндекс Почте.

Telegram даёт ссылку на конкретное сообщение только в супергруппах и каналах. На личную переписку —
только ссылку на человека, а у обычных групп ссылок нет совсем: их открываем в Telegram Web по id.
"""

from __future__ import annotations

from urllib.parse import quote

from opsdash import config, store

TELEGRAM_WEB = "https://web.telegram.org/a/#{}"


def original(source: str | None, chat_id, msg_id=None, sender_id: str | None = None) -> tuple[str, str] | None:
    """(адрес, подпись кнопки) или None, если оригинал открыть нельзя (например, Я.Мессенджер)."""
    if not chat_id:
        return None
    if source == "telegram":
        return _telegram(str(chat_id), msg_id)
    if source == "mail":
        return _mail(str(chat_id), msg_id, sender_id)
    if source == "pachca":
        return _pachca(str(chat_id), msg_id)
    return None


def _telegram(chat_id: str, msg_id) -> tuple[str, str]:
    chat = store.row("SELECT username FROM chats WHERE source='telegram' AND chat_id=?", (chat_id,)) or {}
    username = (chat.get("username") or "").strip()
    if chat_id.startswith("-100"):  # супергруппа или канал
        if msg_id:
            base = f"https://t.me/{username}" if username else f"https://t.me/c/{chat_id[4:]}"
            return f"{base}/{msg_id}", "К сообщению"
        return (f"https://t.me/{username}", "Открыть чат") if username else (
            TELEGRAM_WEB.format(chat_id), "Чат в Telegram Web")
    if not chat_id.startswith("-"):  # личная переписка
        return (f"https://t.me/{username}", "Открыть чат") if username else (
            TELEGRAM_WEB.format(chat_id), "Чат в Telegram Web")
    return TELEGRAM_WEB.format(chat_id), "Группа в Telegram Web"  # обычная группа


def _mail(chat_id: str, msg_id, sender_id: str | None) -> tuple[str, str]:
    if chat_id.startswith("gmail:"):
        account = quote(config.load_settings()["gmail"].get("email") or "0")
        message = str(msg_id or "").strip().strip("<>")
        if "@" in message:  # Message-ID: Gmail находит письмо поиском rfc822msgid
            return (f"https://mail.google.com/mail/u/{account}/#search/rfc822msgid%3A{quote(message, safe='')}",
                    "Письмо в Gmail")
        return f"https://mail.google.com/mail/u/{account}/", "Открыть Gmail"
    # Яндекс Почта: по Message-ID письмо не открыть — показываем письма этого отправителя
    if sender_id and "@" in sender_id:
        return f"https://mail.yandex.ru/#search?request={quote(sender_id)}", "В Яндекс Почте"
    return "https://mail.yandex.ru/", "Открыть Яндекс Почту"


def _pachca(chat_id: str, msg_id) -> tuple[str, str]:
    # Пачка сама отдаёт ссылку на сообщение (у ответа в треде она ведёт в тред) — берём сохранённую
    if msg_id:
        saved = store.scalar("SELECT link FROM messages WHERE source='pachca' AND chat_id=? AND msg_id=?",
                             (chat_id, str(msg_id)))
        return saved or f"https://app.pachca.com/chats/{chat_id}?message={msg_id}", "К сообщению"
    return f"https://app.pachca.com/chats/{chat_id}", "Открыть в Пачке"
