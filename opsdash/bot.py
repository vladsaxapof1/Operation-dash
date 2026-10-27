"""Telegram-бот директора: уведомления, кнопки «Да/Нет», вопросы к Claude с телефона.

Работает через long polling — бот сам спрашивает у Telegram новые сообщения,
поэтому не нужны ни сервер, ни туннель, ни открытые порты.
Бот отвечает только одному человеку — тому, кто привязался кодом из админки.
"""

from __future__ import annotations

import html
import json
import random
import threading
import time
from datetime import datetime

import httpx

from opsdash import config, overview, proposals, records, store
from opsdash.sources import kaiten

API = "https://api.telegram.org"
MAX_TEXT = 4000


class BotError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status

    @property
    def permanent(self) -> bool:
        """Ошибка в самом сообщении (повтор не поможет): 400/403, но не 429 и не сеть."""
        return self.status in (400, 403)


class Bot:
    def __init__(self, token: str, transport: httpx.BaseTransport | None = None):
        if not token:
            raise BotError("Не задан токен Telegram-бота")
        self.token = token
        self.http = httpx.Client(timeout=40, transport=transport)

    def call(self, method: str, **params):
        try:
            resp = self.http.post(f"{API}/bot{self.token}/{method}", json=params)
        except httpx.HTTPError as ex:  # в тексте ошибки httpx есть адрес с токеном — не показываем его
            raise BotError(f"{method}: нет связи с Telegram ({type(ex).__name__})") from None
        try:
            data = resp.json() if resp.content else {}
        except ValueError:
            data = {}
        if not data.get("ok"):
            raise BotError(f"{method}: {data.get('description') or resp.status_code}", resp.status_code)
        return data["result"]

    def send(self, chat_id, text: str, buttons: list | None = None, reply_to: int | None = None) -> int:
        params = {"chat_id": chat_id, "text": text[:MAX_TEXT], "parse_mode": "HTML", "disable_web_page_preview": True}
        if buttons:
            params["reply_markup"] = {"inline_keyboard": buttons}
        if reply_to:
            params["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
        try:
            return self.call("sendMessage", **params)["message_id"]
        except BotError as e:
            if "parse" not in str(e).lower():
                raise
            params.pop("parse_mode")  # разметка сломалась — отправим простым текстом
            params["text"] = html.unescape(_strip_tags(params["text"]))
            return self.call("sendMessage", **params)["message_id"]

    def edit(self, chat_id, message_id: int, text: str, buttons: list | None = None) -> None:
        params = {"chat_id": chat_id, "message_id": message_id, "text": text[:MAX_TEXT], "parse_mode": "HTML",
                  "disable_web_page_preview": True, "reply_markup": {"inline_keyboard": buttons or []}}
        try:
            self.call("editMessageText", **params)
        except BotError as e:
            if "not modified" not in str(e):
                raise

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        try:
            self.call("answerCallbackQuery", callback_query_id=callback_id, text=text[:190])
        except BotError:
            pass

    def updates(self, offset: int, timeout: int = 25) -> list[dict]:
        return self.call("getUpdates", offset=offset, timeout=timeout,
                         allowed_updates=["message", "callback_query"])

    def download(self, file_id: str) -> tuple[bytes, str]:
        info = self.call("getFile", file_id=file_id)
        try:
            resp = self.http.get(f"{API}/file/bot{self.token}/{info['file_path']}")
        except httpx.HTTPError as ex:
            raise BotError(f"скачивание файла: нет связи ({type(ex).__name__})") from None
        if resp.status_code != 200:
            raise BotError(f"скачивание файла: ошибка {resp.status_code}", resp.status_code)
        return resp.content, info["file_path"]

    def me(self) -> dict:
        return self.call("getMe")


def _strip_tags(text: str) -> str:
    import re

    return re.sub(r"<[^>]+>", "", text)


def e(s) -> str:
    return html.escape(str(s or ""))


def get_bot(transport: httpx.BaseTransport | None = None) -> Bot:
    return Bot(config.get_secret("TELEGRAM_BOT_TOKEN"), transport=transport)


def owner_chat(settings: dict | None = None) -> str:
    settings = settings or config.load_settings()
    return str(settings["telegram_bot"].get("owner_chat_id") or "")


def pair_code() -> str:
    code = store.get_state("bot_pair_code")
    if not code:
        code = f"{random.randint(100000, 999999)}"
        store.set_state("bot_pair_code", code)
    return code


# --- исходящие уведомления --------------------------------------------------

def proposal_buttons(items: list[dict], batch: str | None = None) -> list:
    if not items:
        return []
    rows = []
    if len(items) <= 6:
        for p in items:
            rows.append([
                {"text": f"✅ {p['id']}", "callback_data": f"p:a:{p['id']}"},
                {"text": f"✖ {p['id']}", "callback_data": f"p:r:{p['id']}"},
            ])
    if batch and len(items) > 1:
        # имя пачки может не влезть в 64 байта кнопки, поэтому в ней — номер любого предложения пачки
        # и наибольший номер из показанных: предложения, добавленные в пачку позже, кнопка не тронет
        ref, top = items[0]["id"], max(p["id"] for p in items)
        rows.append([
            {"text": f"✅ Все ({len(items)})", "callback_data": f"b:a:{ref}:{top}"},
            {"text": "✖ Все", "callback_data": f"b:r:{ref}:{top}"},
        ])
    elif len(items) > 6:
        rows.append([{"text": "📋 Показать по одному", "callback_data": "list:p"}])
    return rows


def proposals_block(items: list[dict]) -> str:
    lines = ["<b>Ждут вашего «да»:</b>"]
    for p in items[:15]:
        lines.append(f"<b>{p['id']}.</b> {e(p['title'])}")
    if len(items) > 15:
        lines.append(f"…и ещё {len(items) - 15}")
    return "\n".join(lines)


def notify(text: str, *, batch: str | None = None, urgent: bool = False, buttons: list | None = None,
           send_now: bool = True) -> int:
    """Поставить сообщение директору в очередь и попытаться отправить сразу.

    text — HTML-разметка Telegram (<b>, <i>, <a>). Если указан batch, к сообщению
    добавятся предложения этой пачки с кнопками «Да/Нет».
    """
    if batch:
        items = proposals.list_proposals("pending", batch=batch)
        if items:
            text = f"{text}\n\n{proposals_block(items)}"
            buttons = (buttons or []) + proposal_buttons(items, batch)
    oid = store.execute(
        "INSERT INTO outbox(text, buttons, urgent, created) VALUES(?,?,?,?)",
        (text, json.dumps(buttons or [], ensure_ascii=False), int(urgent), store.utcnow()),
    )
    if send_now:
        try:
            flush_outbox()
        except Exception as ex:  # noqa: BLE001 — отправится при следующей попытке
            store.log("bot", f"не удалось отправить уведомление: {ex}", "warning")
    return oid


def in_quiet_hours(settings: dict | None = None, at: datetime | None = None) -> bool:
    settings = settings or config.load_settings()
    q = settings["telegram_bot"].get("quiet_hours") or {}
    start, end = config.hhmm(q.get("start")), config.hhmm(q.get("end"))
    if not start or not end:
        return False
    t = (at or config.now(settings)).strftime("%H:%M")
    if start <= end:
        return start <= t < end
    return t >= start or t < end


OUTBOX_TTL_HOURS = 24


def flush_outbox(transport: httpx.BaseTransport | None = None) -> int:
    """Отправить очередь уведомлений. Безопасно вызывать из нескольких потоков и процессов:
    каждое сообщение сначала «захватывается», поэтому не уходит дважды."""
    settings = config.load_settings()
    chat = owner_chat(settings)
    if not settings["telegram_bot"].get("enabled") or not chat or not config.get_secret("TELEGRAM_BOT_TOKEN"):
        return 0
    # старое не досылаем: сводка за позавчера бесполезна, а пачка старых сообщений раздражает
    store.execute("UPDATE outbox SET dead=1, error='устарело, не отправлено' WHERE sent_at IS NULL AND dead=0 AND created < ?",
                  (overview.since(OUTBOX_TTL_HOURS),))
    quiet = in_quiet_hours(settings)
    sql = ("SELECT * FROM outbox WHERE sent_at IS NULL AND dead=0" + (" AND urgent=1" if quiet else "")
           + " ORDER BY id LIMIT 20")
    pending = store.rows(sql)
    if not pending:
        return 0
    bot = get_bot(transport)
    sent = 0
    stale_claim = overview.since(0.05)  # захват старше 3 минут считаем брошенным
    for m in pending:
        claimed = store.execute(
            "UPDATE outbox SET sending_at=? WHERE id=? AND sent_at IS NULL AND dead=0 "
            "AND (sending_at IS NULL OR sending_at < ?)", (store.utcnow(), m["id"], stale_claim))
        if not claimed:
            continue  # отправляет другой поток или процесс
        try:
            mid = bot.send(chat, m["text"], store.loads(m["buttons"], []) or None)
            store.execute("UPDATE outbox SET sent_at=?, tg_message_id=?, error=NULL WHERE id=?",
                          (store.utcnow(), mid, m["id"]))
            sent += 1
        except BotError as ex:
            if ex.permanent:  # сообщение не примут никогда — не блокируем остальные
                store.execute("UPDATE outbox SET dead=1, error=? WHERE id=?", (str(ex)[:500], m["id"]))
                store.log("bot", f"уведомление не принято Telegram: {ex}", "error")
                continue
            store.execute("UPDATE outbox SET sending_at=NULL, error=? WHERE id=?", (str(ex)[:500], m["id"]))
            raise
    return sent


# --- входящие ---------------------------------------------------------------

HELP = (
    "<b>Что я умею</b>\n"
    "/today — сводка на сейчас\n"
    "/approvals — что ждёт вашего «да»\n"
    "/promises — задачи и договорённости (ваши и вам)\n"
    "/inbox — где ждут вашего ответа\n"
    "/stale — задачи Kaiten без движения\n"
    "/meetings — последние встречи\n"
    "/digest — собрать дайджест с ИИ\n"
    "/new — начать новый разговор с Claude\n\n"
    "Можно просто написать или наговорить голосом вопрос или поручение — "
    "например «что мне обещал Петров?» или «поставь Ивановой задачу подготовить отчёт к пятнице».\n"
    "Пришлите файл записи встречи (до 20 МБ) — я его расшифрую и разберу."
)


def handle_update(bot: Bot, upd: dict) -> None:
    settings = config.load_settings()
    owner = owner_chat(settings)
    if "callback_query" in upd:
        cq = upd["callback_query"]
        chat_id = str(cq["message"]["chat"]["id"]) if cq.get("message") else str(cq["from"]["id"])
        if chat_id != owner:
            bot.answer_callback(cq["id"], "Нет доступа")
            return
        handle_callback(bot, cq, chat_id)
        return
    msg = upd.get("message")
    if not msg:
        return
    chat_id = str(msg["chat"]["id"])
    text = (msg.get("text") or msg.get("caption") or "").strip()
    if not owner:
        code = pair_code()
        if text.startswith("/start") and text.split()[-1] == code:
            config.update_settings("telegram_bot", {"owner_chat_id": chat_id})
            store.set_state("bot_pair_code", None)
            bot.send(chat_id, "Готово, бот привязан к вам. Сюда будут приходить сводки и вопросы на подтверждение.\n\n" + HELP)
            store.log("bot", f"бот привязан к чату {chat_id}")
        else:
            bot.send(chat_id, "Это личный бот. Чтобы привязать его, отправьте /start и код из админки (раздел «Telegram-бот»).")
        return
    if chat_id != owner:
        return
    if msg.get("voice") or msg.get("audio") or msg.get("document") or msg.get("video") or msg.get("video_note"):
        handle_media(bot, msg, chat_id)
        return
    if not text:
        return
    handle_text(bot, chat_id, text, msg["message_id"])


def handle_text(bot: Bot, chat_id: str, text: str, message_id: int | None = None) -> None:
    cmd = text.split()[0].split("@")[0].lower() if text.startswith("/") else ""
    if cmd in ("/start", "/help"):
        bot.send(chat_id, HELP)
    elif cmd == "/today":
        bot.send(chat_id, overview.today_text())
    elif cmd == "/approvals":
        send_approvals(bot, chat_id)
    elif cmd == "/promises":
        bot.send(chat_id, promises_text())
    elif cmd == "/inbox":
        send_attention(bot, chat_id)
    elif cmd == "/stale":
        send_stale(bot, chat_id, force=True)
    elif cmd == "/meetings":
        bot.send(chat_id, meetings_text())
    elif cmd == "/digest":
        bot.send(chat_id, "Собираю дайджест, это займёт пару минут…")
        from opsdash import jobs

        jobs.submit("digest", kind="interactive")
    elif cmd == "/new":
        store.set_state("ask_session", None)
        bot.send(chat_id, "Начинаем новый разговор.")
    else:
        if cmd:
            text = text[len(text.split()[0]):].strip() or text
        bot.send(chat_id, "🤔 Думаю…", reply_to=message_id)
        from opsdash import jobs

        jobs.submit("ask", kind="interactive", args={"text": text, "chat_id": chat_id, "reply_to": message_id})


def handle_media(bot: Bot, msg: dict, chat_id: str) -> None:
    media = msg.get("voice") or msg.get("audio") or msg.get("document") or msg.get("video") or msg.get("video_note")
    size = media.get("file_size") or 0
    if size > 20 * 1024 * 1024:
        bot.send(chat_id, "Файл больше 20 МБ — Telegram не даёт ботам скачивать такие. "
                          "Положите запись в папку записей на компьютере, я её подхвачу.")
        return
    try:
        content, path = bot.download(media["file_id"])
    except BotError as ex:
        bot.send(chat_id, f"Не смогла скачать файл: {e(ex)}")
        return
    from opsdash import jobs

    name = media.get("file_name") or path.split("/")[-1]
    stamp = config.now().strftime("%Y-%m-%d_%H%M%S")
    if msg.get("voice") or msg.get("video_note"):
        target = config.DATA_DIR / "voice" / f"{stamp}_{name}"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        bot.send(chat_id, "🎙 Слушаю…", reply_to=msg["message_id"])
        jobs.submit("voice", kind="interactive", args={"path": str(target), "chat_id": chat_id,
                                                 "reply_to": msg["message_id"], "caption": msg.get("caption") or ""})
        return
    from opsdash import recordings

    # своя папка на каждый файл: время в имени не пишем — иначе его примут за начало встречи
    target = config.RECORDINGS_DIR / "telegram" / f"tg{int(time.time())}" / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    if target.suffix.lower() not in recordings.ALL:
        bot.send(chat_id, "Это не похоже на запись встречи. Принимаю аудио, видео и расшифровки (.txt, .vtt, .srt, .docx).")
        return
    meeting_id = recordings.create_meeting(target, title=(msg.get("caption") or None))
    bot.send(chat_id, f"📥 Получила «{e(name)}». Расшифрую и разберу — пришлю итог сюда.", reply_to=msg["message_id"])
    jobs.submit("transcribe", kind="heavy", args={"meeting_id": meeting_id})


def handle_callback(bot: Bot, cq: dict, chat_id: str) -> None:
    data = cq.get("data") or ""
    parts = data.split(":")
    message_id = cq["message"]["message_id"] if cq.get("message") else None
    if parts[0] == "p" and len(parts) == 3:
        pid = int(parts[2])
        p = proposals.approve(pid) if parts[1] == "a" else proposals.reject(pid)
        bot.answer_callback(cq["id"], proposals.result_text(p)[:190] if p else "Не найдено")
        if p:
            bot.send(chat_id, f"{e(p['title'])}\n{e(proposals.result_text(p))}")
        _refresh_buttons(bot, cq)
    elif parts[0] == "b" and len(parts) in (3, 4):
        items = batch_items(parts[2], parts[3] if len(parts) == 4 else parts[2])
        results = [proposals.approve(p["id"]) if parts[1] == "a" else proposals.reject(p["id"]) for p in items]
        bot.answer_callback(cq["id"], f"Обработано: {len(results)}")
        if results:
            bot.send(chat_id, "\n".join(f"{e(p['title'])} — {e(proposals.result_text(p))}" for p in results))
        _refresh_buttons(bot, cq)
    elif data == "list:p":
        bot.answer_callback(cq["id"])
        send_approvals(bot, chat_id, one_by_one=True)
    elif parts[0] == "att" and len(parts) == 3:
        records.close_attention(int(parts[2]), "done" if parts[1] == "d" else "dismissed")
        bot.answer_callback(cq["id"], "Отмечено")
        if message_id:
            bot.edit(chat_id, message_id, cq["message"].get("text", "") + "\n\n✔ отмечено", [])
    elif parts[0] == "c" and len(parts) == 3:
        records.update_commitment(int(parts[2]), status="done")
        bot.answer_callback(cq["id"], "Задача закрыта")
    elif parts[0] == "k" and len(parts) == 3:
        card_id = int(parts[2])
        if parts[1] == "z":
            kaiten.snooze(card_id, 3)
            bot.answer_callback(cq["id"], "Отложено на 3 дня")
        elif parts[1] == "n":
            p = nudge_card(card_id)
            bot.answer_callback(cq["id"], proposals.result_text(p)[:190])
        _refresh_buttons(bot, cq)
    else:
        bot.answer_callback(cq["id"])


def batch_items(ref_id: str, top_id: str) -> list[dict]:
    """Ожидающие предложения пачки, показанные на кнопке: с номерами не больше top_id."""
    if not (ref_id.isdigit() and top_id.isdigit()):
        return []
    ref = proposals.get(int(ref_id))
    if not ref or not ref.get("batch"):
        return []
    return [p for p in proposals.list_proposals("pending", batch=ref["batch"]) if p["id"] <= int(top_id)]


def _card_from(cq: dict) -> str:
    parts = (cq.get("data") or "").split(":")
    return parts[2] if parts[0] == "k" and len(parts) == 3 else ""


def _refresh_buttons(bot: Bot, cq: dict) -> None:
    """Убрать с исходного сообщения кнопки уже решённых предложений."""
    msg = cq.get("message")
    if not msg:
        return
    keyboard = (msg.get("reply_markup") or {}).get("inline_keyboard") or []
    pending_ids = {p["id"] for p in proposals.list_proposals("pending")}
    new_rows = []
    for row in keyboard:
        keep = []
        for b in row:
            d = b.get("callback_data", "")
            if d.startswith("p:"):
                if int(d.split(":")[2]) in pending_ids:
                    keep.append(b)
            elif d.startswith("b:"):
                bits = d.split(":")
                if batch_items(bits[2], bits[3] if len(bits) == 4 else bits[2]):
                    keep.append(b)
            elif d.startswith("k:") and d.split(":")[-1] == str(_card_from(cq)):
                continue  # по этой карточке уже нажали
            else:
                keep.append(b)
        if keep:
            new_rows.append(keep)
    try:
        bot.call("editMessageReplyMarkup", chat_id=msg["chat"]["id"], message_id=msg["message_id"],
                 reply_markup={"inline_keyboard": new_rows})
    except BotError:
        pass


NUDGE_REASON = "напоминание о карточке без движения"


def nudge_card(card_id: int) -> dict:
    """Напомнить исполнителю в Kaiten (через очередь подтверждений — кнопка уже и есть подтверждение).
    Повторное нажатие в течение суток не создаёт второй комментарий."""
    recent = store.row(
        "SELECT id FROM proposals WHERE kind='kaiten.comment' AND reason=? AND status IN ('done','running') "
        "AND payload LIKE ? AND created > ? ORDER BY id DESC LIMIT 1",
        (NUDGE_REASON, f'%"card_id": {int(card_id)},%', overview.since(20)))
    if recent:
        return proposals.get(recent["id"])
    settings = config.load_settings()
    card = store.row("SELECT * FROM kaiten_cards WHERE id=?", (card_id,)) or {}
    user = store.row("SELECT * FROM kaiten_users WHERE id=?", (card.get("responsible_id"),)) if card.get("responsible_id") else None
    since = kaiten.idle_since(card, settings["kaiten"].get("stale_basis", "moved")) if card else None
    days = (datetime.now(since.tzinfo) - since).days if since else settings["kaiten"]["stale_days"]
    template = settings["kaiten"].get("nudge_text") or "Карточка без движения {days} дн. Как продвигается?"
    text = template.format(username=(user or {}).get("username") or "", days=days, title=card.get("title", ""))
    if not (user or {}).get("username"):
        text = text.replace("@,", "").replace("@ ", "").strip()
    p = proposals.propose_kaiten_comment(card_id=card_id, text=text, reason=NUDGE_REASON)
    if p["status"] == "pending":
        p = proposals.approve(p["id"])
    kaiten.snooze(card_id, 2)
    return p


def send_approvals(bot: Bot, chat_id: str, one_by_one: bool = False) -> None:
    items = proposals.list_proposals("pending")
    if not items:
        bot.send(chat_id, "Ничего не ждёт подтверждения 👌")
        return
    if one_by_one or len(items) <= 6:
        for p in reversed(items[:20]):
            bot.send(chat_id, e(proposals.describe(p)), [[
                {"text": "✅ Да", "callback_data": f"p:a:{p['id']}"},
                {"text": "✖ Нет", "callback_data": f"p:r:{p['id']}"},
            ]])
        return
    bot.send(chat_id, proposals_block(items), proposal_buttons(items))


def send_attention(bot: Bot, chat_id: str) -> None:
    items = records.list_attention("open")
    if not items:
        bot.send(chat_id, "Никто не ждёт вашего ответа 👌")
        return
    for a in items[:10]:
        text = f"{'🔴 ' if a['priority'] == 'high' else ''}<b>{e(a['sender_name'] or '')}</b> · {e(a['chat_title'])}\n{e(a['summary'])}"
        if a.get("draft"):
            text += f"\n\n<i>Черновик ответа:</i>\n<code>{e(a['draft'])}</code>"
        if a.get("msg_link"):
            text += f"\n<a href=\"{e(a['msg_link'])}\">Открыть сообщение</a>"
        bot.send(chat_id, text, [[
            {"text": "✔ Ответила", "callback_data": f"att:d:{a['id']}"},
            {"text": "Скрыть", "callback_data": f"att:x:{a['id']}"},
        ]])


def send_stale(bot: Bot, chat_id: str, force: bool = False) -> int:
    settings = config.load_settings()
    cards = kaiten.stale_cards(settings)
    if not force:
        # не пингуем одну и ту же карточку чаще раза в сутки
        recent = {r["card_id"] for r in store.rows(
            "SELECT card_id FROM kaiten_snooze WHERE last_pinged > ?", (overview.since(20),))}
        cards = [c for c in cards if c["id"] not in recent]
    if not cards:
        if force:
            bot.send(chat_id, "Карточек без движения нет 👌")
        return 0
    limit = int(settings["notifications"].get("max_stale_in_ping") or 10)
    shown = cards[:limit]
    lines = [f"<b>🐌 Без движения {settings['kaiten']['stale_days']}+ дн.: {len(cards)}</b>"]
    buttons = []
    for c in shown:
        lines.append(f"<b>#{c['id']}</b> <a href=\"{e(c['url'])}\">{e(c['title'])}</a> — "
                     f"{e(c['responsible_name'] or 'без исполнителя')}, «{e(c['column_title'])}», {c['idle_days']} дн.")
        buttons.append([
            {"text": f"🔔 Напомнить #{c['id']}", "callback_data": f"k:n:{c['id']}"},
            {"text": "💤 3 дня", "callback_data": f"k:z:{c['id']}"},
        ])
    if len(cards) > limit:
        lines.append(f"…и ещё {len(cards) - limit} в админке")
    lines.append("\n«Напомнить» оставит в карточке комментарий с упоминанием исполнителя.")
    bot.send(chat_id, "\n".join(lines), buttons)
    kaiten.mark_pinged([c["id"] for c in shown])
    return len(shown)


def promises_text() -> str:
    items = records.list_commitments(status="open")
    mine = [c for c in items if c["direction"] == "mine"]
    to_me = [c for c in items if c["direction"] == "to_me"]
    lines = [f"<b>🫵 Ваши задачи ({len(mine)})</b>"]
    lines += [f"• {e(c['what'])}{e(' → ' + c['whom'] if c['whom'] else '')}{overview._due(c)}" for c in mine[:15]] or ["—"]
    lines.append(f"\n<b>🤝 Вам должны ({len(to_me)})</b>")
    lines += [f"• {e(c['who'])}: {e(c['what'])}{overview._due(c)}" for c in to_me[:15]] or ["—"]
    return "\n".join(lines)


def meetings_text() -> str:
    items = records.list_meetings(limit=8)
    if not items:
        return "Встреч пока нет."
    status = {"processed": "✅", "failed": "❌", "transcribing": "⏳", "processing": "⏳", "transcribed": "📝", "new": "🆕"}
    lines = ["<b>Последние встречи</b>"]
    for m in items:
        lines.append(f"{status.get(m['status'], '•')} {store.local(m['started_at'])} {e(m['title'] or m['id'])}"
                     + (f"\n   <i>{e(m['headline'])}</i>" if m.get("headline") else ""))
    return "\n".join(lines)


# --- цикл -------------------------------------------------------------------

def run_forever(stop: threading.Event) -> None:
    offset = int(store.get_state("bot_offset", 0) or 0)
    last_flush = 0.0
    while not stop.is_set():
        settings = config.load_settings()
        token = config.get_secret("TELEGRAM_BOT_TOKEN")
        if not settings["telegram_bot"].get("enabled") or not token:
            stop.wait(10)
            continue
        try:
            bot = Bot(token)
            for upd in bot.updates(offset, timeout=20):
                offset = upd["update_id"] + 1
                store.set_state("bot_offset", offset)
                try:
                    handle_update(bot, upd)
                except Exception as ex:  # noqa: BLE001
                    store.log("bot", f"ошибка обработки сообщения: {ex}", "error")
            if time.time() - last_flush > 30:
                flush_outbox()
                last_flush = time.time()
        except Exception as ex:  # noqa: BLE001 — сеть пропала, ноутбук уснул и т.п.
            store.log("bot", f"нет связи с Telegram: {ex}", "warning")
            stop.wait(15)
