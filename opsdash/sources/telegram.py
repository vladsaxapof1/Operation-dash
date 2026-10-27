"""Telegram: чтение аккаунта директора (только выбранные чаты) и напоминания коллегам — только после «Да».

Используется официальный клиентский API Telegram (библиотека Telethon) —
так же работают десктопные клиенты. Нужны api_id/api_hash с my.telegram.org
и однократный вход по коду. Сессия хранится в secrets/.env и даёт полный доступ
к аккаунту — поэтому папку secrets никому не передаём.
"""

from __future__ import annotations

import asyncio
import threading
from datetime import datetime, timedelta, timezone

from opsdash import config, store

SOURCE = "telegram"
_lock = threading.Lock()  # не открывать две сессии одновременно


class TelegramError(Exception):
    pass


def _client(session: str | None = None, settings: dict | None = None):
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    settings = settings or config.load_settings()
    api_id = str(settings["telegram"].get("api_id") or "").strip()
    api_hash = config.get_secret("TELEGRAM_API_HASH")
    if not api_id.isdigit() or not api_hash:
        raise TelegramError("Укажите api_id и api_hash (my.telegram.org) в настройках Telegram")
    sess = config.get_secret("TELEGRAM_SESSION") if session is None else session
    return TelegramClient(
        StringSession(sess), int(api_id), api_hash,
        device_model="PULSE", system_version="Windows", app_version="1.0",
    )


def _kind(dialog) -> str:
    if dialog.is_user:
        return "private"
    if dialog.is_channel and not dialog.is_group:
        return "channel"
    return "group"


def _media_label(m) -> str:
    if m.voice:
        return "[голосовое сообщение]"
    if m.video_note:
        return "[видеосообщение]"
    if m.photo:
        return "[фото]"
    if m.video:
        return "[видео]"
    if m.document:
        name = getattr(m.file, "name", None) if m.file else None
        return f"[файл: {name}]" if name else "[файл]"
    if m.sticker:
        return "[стикер]"
    return ""


def _link(entity, msg_id: int) -> str | None:
    # ссылка на сообщение бывает только в супергруппах и каналах; t.me/<человек>/<id> в личке не открывается
    if entity.__class__.__name__ != "Channel":
        return None
    username = getattr(entity, "username", None)
    return f"https://t.me/{username}/{msg_id}" if username else f"https://t.me/c/{entity.id}/{msg_id}"


async def _authorized_client(settings: dict):
    client = _client(settings=settings)
    await client.connect()
    if not await client.is_user_authorized():
        await client.disconnect()
        raise TelegramError("Вход в Telegram не выполнен или сессия устарела — войдите заново в админке")
    return client


def _username(ent) -> str:
    """Основной @username; если его нет — первый активный из дополнительных. "" — имени нет (сотрёт старое)."""
    if getattr(ent, "username", None):
        return ent.username
    extra = [u.username for u in (getattr(ent, "usernames", None) or []) if getattr(u, "active", False)]
    return extra[0] if extra else ""


async def _refresh_dialogs(client) -> list[dict]:
    out = []
    async for d in client.iter_dialogs():
        ent = d.entity
        if getattr(ent, "is_self", False):
            continue
        # старая часть группы, превращённой в супергруппу, и удалённые аккаунты: Telegram отдаёт их
        # отдельными диалогами, новых сообщений там не будет, а в списке были двойники и пустые строки
        if getattr(ent, "migrated_to", None) or getattr(ent, "deleted", False):
            store.execute("DELETE FROM chats WHERE source=? AND chat_id=?", (SOURCE, str(d.id)))
            continue
        kind = _kind(d)
        is_bot = bool(getattr(ent, "bot", False))
        store.upsert_chat(SOURCE, str(d.id), d.name, "bot" if is_bot else kind, username=_username(ent),
                          last_ts=store.to_utc_iso(d.date) if getattr(d, "date", None) else None)
        out.append({"dialog": d, "kind": kind, "bot": is_bot})
    return out


async def _collect(settings: dict) -> str:
    from telethon import utils

    client = await _authorized_client(settings)
    try:
        me = await client.get_me()
        store.set_state("telegram_me", {"id": me.id, "name": utils.get_display_name(me), "username": me.username})
        dialogs = await _refresh_dialogs(client)
        chats = {r["chat_id"]: r["enabled"] for r in store.rows("SELECT chat_id, enabled FROM chats WHERE source=?", (SOURCE,))}
        include_private = bool(settings["telegram"].get("include_all_private"))
        backfill = datetime.now(timezone.utc) - timedelta(days=int(settings["telegram"].get("backfill_days") or 3))
        new_total, read_chats = 0, 0
        for item in dialogs:
            d = item["dialog"]
            chat_id = str(d.id)
            flag = chats.get(chat_id, 0)
            wanted = flag == 1 or (include_private and item["kind"] == "private" and not item["bot"] and flag != -1)
            if not wanted:
                continue
            read_chats += 1
            last_id = int(store.get_state(f"tg_last:{chat_id}", 0) or 0)
            if d.message and d.message.id <= last_id:
                continue
            # от старых к новым и не больше 500 за раз: если накопилось больше (ноутбук долго был
            # выключен), остальное заберём в следующий проход, ничего не пропуская
            if last_id:
                history = client.iter_messages(d.entity, min_id=last_id, reverse=True, limit=500)
            else:
                history = client.iter_messages(d.entity, offset_date=backfill, reverse=True, limit=300)
            batch = [m async for m in history]
            max_id = last_id
            for m in batch:
                max_id = max(max_id, m.id)
                text = (m.message or "").strip()
                label = _media_label(m)
                if label:
                    text = f"{label} {text}".strip()
                if not text or m.action:
                    continue
                sender = m.sender
                new = store.add_message({
                    "source": SOURCE,
                    "chat_id": chat_id,
                    "chat_title": d.name,
                    "msg_id": m.id,
                    "sender_id": str(m.sender_id or ""),
                    "sender_name": "Я" if m.out else (utils.get_display_name(sender) if sender else ""),
                    "is_outgoing": m.out,
                    "text": text[:8000],
                    "ts": store.to_utc_iso(m.date),
                    "reply_to": str(m.reply_to_msg_id) if m.reply_to_msg_id else None,
                    "link": _link(d.entity, m.id),
                })
                new_total += int(new)
            store.set_state(f"tg_last:{chat_id}", max_id)
        return f"чатов прочитано: {read_chats}, новых сообщений: {new_total}"
    finally:
        await client.disconnect()


def collect(settings: dict | None = None) -> str:
    settings = settings or config.load_settings()
    with _lock:
        return asyncio.run(_collect(settings))


async def _send(settings: dict, target: str, text: str) -> int:
    client = await _authorized_client(settings)
    try:
        entity = target
        if not target.startswith("@"):
            # по id: сессия не помнит собеседников — находим чат среди диалогов, как при сборе
            entity = None
            async for d in client.iter_dialogs():
                if str(d.id) == str(target):
                    entity = d.entity
                    break
            if entity is None:
                raise TelegramError(f"Чат {target} не найден среди диалогов Telegram")
        msg = await client.send_message(entity, text)
        return msg.id
    finally:
        await client.disconnect()


def send(target: str, text: str, settings: dict | None = None) -> int:
    """Отправить сообщение от имени директора: target — @username или id личного чата. Вызывается только
    из подтверждённого предложения (proposals.execute, «Напомнить коллеге») — сам Claude не пишет никому."""
    settings = settings or config.load_settings()
    if not text.strip():
        raise TelegramError("Пустое сообщение")
    with _lock:
        return asyncio.run(_send(settings, target, text.strip()))


async def _check(settings: dict) -> str:
    from telethon import utils

    client = await _authorized_client(settings)
    try:
        me = await client.get_me()
        return f"вход как {utils.get_display_name(me)}"
    finally:
        await client.disconnect()


def check(settings: dict | None = None) -> str:
    settings = settings or config.load_settings()
    with _lock:
        return asyncio.run(_check(settings))


async def _list_dialogs(settings: dict) -> int:
    client = await _authorized_client(settings)
    try:
        return len(await _refresh_dialogs(client))
    finally:
        await client.disconnect()


def refresh_dialogs(settings: dict | None = None) -> int:
    """Обновить список чатов для выбора в админке."""
    settings = settings or config.load_settings()
    with _lock:
        return asyncio.run(_list_dialogs(settings))


# --- вход по QR-коду или по коду (вызывается из админки) ----------------------

QR_LIFETIME = 300  # секунд показываем QR (Telegram сам меняет токен каждые ~30 с), потом — «нажмите снова»


class LoginFlow:
    """Вход: QR-код (сканировать в Telegram на телефоне) или телефон → код из Telegram;
    затем, если включена, облачный пароль."""

    def __init__(self):
        self.client = None
        self.phone = None
        self.phone_code_hash = None
        self.loop = None
        self.thread = None
        self.qr = None
        self.qr_task = None
        self.qr_state = ""  # waiting → ok | password | expired | error
        self.qr_error = ""
        self.qr_version = 0  # растёт при каждой смене токена — страница обновляет картинку

    def _run(self, coro):
        # у входа свой постоянный цикл в отдельном потоке: между вводом телефона
        # и кода проходит время, соединение должно жить
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout=90)

    def _open(self) -> None:
        self.close()
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True, name="tg-login")
        self.thread.start()
        self.client = self._run(self._make())
        self._run(self.client.connect())

    def start(self, phone: str) -> None:
        self._open()
        self.phone = phone.strip()
        sent = self._run(self.client.send_code_request(self.phone))
        self.phone_code_hash = sent.phone_code_hash

    def start_qr(self) -> None:
        self._open()
        self.qr = self._run(self.client.qr_login())
        self.qr_state, self.qr_error, self.qr_version = "waiting", "", 1
        self.qr_task = asyncio.run_coroutine_threadsafe(self._wait_qr(), self.loop)

    async def _wait_qr(self) -> None:
        # ждём, пока QR отсканируют; истёкший токен меняем на новый
        from telethon.errors import SessionPasswordNeededError

        deadline = self.loop.time() + QR_LIFETIME
        while True:
            try:
                try:
                    user = await self.qr.wait()
                except asyncio.TimeoutError:
                    if self.loop.time() > deadline:
                        self.qr_state = "expired"
                        return
                    # если уведомление о скане потерялось, о пароле скажет уже выпуск нового токена
                    await self.qr.recreate()
                    self.qr_version += 1
                    continue
            except SessionPasswordNeededError:
                self.qr_state = "password"
                return
            except Exception as e:  # noqa: BLE001
                self.qr_state, self.qr_error = "error", str(e)
                return
            if getattr(user, "phone", None):
                self.phone = "+" + user.phone.lstrip("+")
            self.qr_state = "ok"
            return

    def qr_url(self) -> str:
        return self.qr.url if self.qr and self.qr_state == "waiting" else ""

    def finish_qr(self) -> bool:
        """Сохранить сессию после сканирования. Вызывать не из потока входа."""
        if self.qr_state != "ok" or not self.client:
            return False
        self._save()
        return True

    @staticmethod
    async def _make():
        return _client(session="")

    def code(self, code: str) -> str:
        """Вернёт 'ok' или 'password' (нужен облачный пароль)."""
        from telethon.errors import SessionPasswordNeededError

        if not self.client:
            raise TelegramError("Сначала запросите код")
        try:
            self._run(self.client.sign_in(phone=self.phone, code=code.strip(), phone_code_hash=self.phone_code_hash))
        except SessionPasswordNeededError:
            return "password"
        self._save()
        return "ok"

    def password(self, password: str) -> str:
        if not self.client:
            raise TelegramError("Сначала запросите код")
        self._run(self.client.sign_in(password=password))
        self._save()
        return "ok"

    def _save(self) -> None:
        config.set_secret("TELEGRAM_SESSION", self.client.session.save())
        config.update_settings("telegram", {"enabled": True, **({"phone": self.phone} if self.phone else {})})
        store.log(SOURCE, "вход в Telegram выполнен")
        self.close()

    def close(self) -> None:
        if self.qr_task:
            self.qr_task.cancel()
        if self.client and self.loop:
            try:
                self._run(self.client.disconnect())
            except Exception:  # noqa: BLE001
                pass
        if self.loop:
            self.loop.call_soon_threadsafe(self.loop.stop)
            if self.thread:
                self.thread.join(timeout=5)
            self.loop.close()
        self.client = None
        self.loop = None
        self.thread = None
        self.phone_code_hash = None
        self.qr = None
        self.qr_task = None
        self.qr_state = self.qr_error = ""


login_flow = LoginFlow()


def logout() -> None:
    config.set_secret("TELEGRAM_SESSION", None)
    config.update_settings("telegram", {"enabled": False})
