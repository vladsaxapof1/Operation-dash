"""Фоновые задания и расписание.

Три очереди, каждая со своим потоком:
- light — сбор сообщений, Kaiten, календаря, поиск новых записей;
- heavy — расшифровка встреч и плановые запуски Claude;
- interactive — вопросы директора из Telegram (не ждут часовую расшифровку).
"""

from __future__ import annotations

import queue
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone

from opsdash import bot, brain, config, overview, recordings, records, store

_queues: dict[str, queue.Queue] = {}
_pending: set[str] = set()
_pending_lock = threading.Lock()


# --- задания ------------------------------------------------------------------

def collect_source(name: str) -> str:
    from opsdash.sources import calendar, kaiten, mail, pachca, telegram, ymessenger

    fn = {"telegram": telegram.collect, "ymessenger": ymessenger.collect, "pachca": pachca.collect,
          "mail": mail.collect, "gmail": mail.collect_gmail, "calendar": calendar.collect,
          "google_calendar": calendar.google_collect, "kaiten": kaiten.collect}[name]
    return fn()


def job_docs_index(**_) -> str:
    from opsdash import docs

    return docs.index()


def job_recordings(**_) -> str:
    settings = config.load_settings()
    created = []
    for path in recordings.scan(settings):
        created.append(recordings.create_meeting(path))
    for m in store.rows("SELECT id FROM meetings WHERE status='new'"):
        submit("transcribe", kind="heavy", args={"meeting_id": m["id"]})
    return f"новых записей: {len(created)}"


def job_transcribe(meeting_id: str, **_) -> str:
    from opsdash import transcribe

    settings = config.load_settings()
    try:
        # в своём процессе: расшифровка держит GIL, и PULSE переставал отвечать
        meta = transcribe.run_in_child(recordings.transcribe_meeting, meeting_id, settings)
    except transcribe.Interrupted:
        # встреча остаётся «расшифровывается» — при запуске PULSE вернёт её в очередь
        return "прервано остановкой PULSE — продолжится при следующем запуске"
    except Exception as e:
        records.update_meeting(meeting_id, status="failed", error=str(e)[:1000])
        bot.notify(f"❌ Не получилось расшифровать запись <b>{bot.e(meeting_id)}</b>: {bot.e(e)}")
        raise
    if settings["recordings"].get("auto_process", True):
        submit("process-meeting", kind="heavy", args={"meeting_id": meeting_id})
    return meta["title"] + (f": {meta['duration_min']} мин" if meta.get("duration_min") else "")


def job_process_meeting(meeting_id: str, **_) -> str:
    settings = config.load_settings()
    records.update_meeting(meeting_id, status="processing", error="")
    prompt = (
        f"Разбери встречу `{meeting_id}` (папка data/meetings/{meeting_id}/). "
        "Действуй строго по навыку process-meeting (.claude/skills/process-meeting/SKILL.md). "
        "Это фоновый запуск: директор не видит этот чат, связь с ней — только через ./ops notify."
    )
    try:
        brain.run(prompt, model=settings["claude"]["model_meetings"], job="process-meeting")
    except Exception as e:
        records.update_meeting(meeting_id, status="failed", error=str(e)[:1000])
        bot.notify(f"❌ Встреча <b>{bot.e(meeting_id)}</b> расшифрована, но разбор не удался: {bot.e(str(e)[:300])}\n"
                   "Расшифровка лежит в папке встречи, разбор можно перезапустить в админке.")
        raise
    m = records.get_meeting(meeting_id)
    if m and m["status"] != "processed":
        if (records.meeting_dir(meeting_id) / "summary.md").exists():
            records.update_meeting(meeting_id, status="processed")
        else:
            records.update_meeting(meeting_id, status="failed", error="Claude не создал summary.md")
            raise RuntimeError("Claude не создал summary.md")
    return "готово"


MESSAGE_SOURCES = ("telegram", "ymessenger", "pachca", "mail", "gmail")


def collect_messages_now(settings: dict, timeout: float = 180) -> None:
    """Забрать свежие сообщения и дождаться сбора. Для кнопки «Разобрать новые сейчас»: без этого
    Claude видит только собранное по расписанию — после включения компьютера сбор мог ещё не пройти."""
    if not in_daemon():  # из командной строки — сбор идёт в PULSE, второй клиент Telegram ему помешает
        return
    names = {f"collect:{src}" for src in MESSAGE_SOURCES if settings[src].get("enabled")}
    for name in names:
        submit(name)
    deadline = time.time() + timeout
    while time.time() < deadline:
        with _pending_lock:
            if not names & _pending:
                return
        time.sleep(1)


def job_triage(force: bool = False, **_) -> str:
    settings = config.load_settings()
    if force:
        collect_messages_now(settings)
    pending = store.scalar("SELECT COUNT(*) FROM messages WHERE processed=0") or 0
    if not pending:
        return "новых сообщений нет"
    if not force and not in_work_hours(settings):
        return "вне рабочего времени — разберу позже"
    prompt = (
        f"Разбери новые сообщения ({pending} шт.) строго по навыку triage (.claude/skills/triage/SKILL.md). "
        "Это фоновый запуск: директор не видит этот чат, связь с ней — только через ./ops notify."
    )
    brain.run(prompt, model=settings["claude"]["model_triage"], job="triage")
    left = store.scalar("SELECT COUNT(*) FROM messages WHERE processed=0") or 0
    return f"разобрано {pending - left} из {pending}"


def job_digest(when: str | None = None, **_) -> str:
    settings = config.load_settings()
    when = when or ("утренний" if config.now(settings).hour < 14 else "вечерний")
    prompt = (
        f"Подготовь {when} дайджест для директора строго по навыку digest (.claude/skills/digest/SKILL.md) "
        "и отправь его через ./ops notify. Это фоновый запуск."
    )
    try:
        brain.run(prompt, model=settings["claude"]["model_digest"], job="digest", timeout_min=15)
        return f"{when} дайджест отправлен"
    except Exception as e:  # noqa: BLE001 — без ИИ всё равно пришлём сводку
        from opsdash import digests

        text = overview.today_text()
        digests.save(digests.daily_kind(settings), text)
        bot.notify(text)
        store.log("digest", f"Claude недоступен, отправлена простая сводка: {e}", "warning")
        return "отправлена простая сводка (Claude недоступен)"


def job_weekly(day: str | None = None, **_) -> str:
    """Итоги недели — в Telegram и в раздел «Дайджесты». day — день той недели, за которую итоги
    (если компьютер был выключен в пятницу вечером, итоги придут при следующем запуске — за ту же неделю)."""
    settings = config.load_settings()
    prompt = (
        f"Подготовь итоги недели{' (неделя, в которую входит ' + day + ')' if day else ''} строго по навыку weekly "
        f"(.claude/skills/weekly/SKILL.md){' — данные: ./ops week --date ' + day if day else ''}. "
        "Это фоновый запуск: директор узнаёт итог только через ./ops notify."
    )
    brain.run(prompt, model=settings["claude"]["model_digest"], job="weekly", timeout_min=20)
    return "итоги недели готовы"


def job_groups(**_) -> str:
    """Сводка рабочих групп Telegram за сутки — в раздел «Дайджесты» (в Telegram — если включено в настройках)."""
    from opsdash import digests

    settings = config.load_settings()
    chats = digests.group_messages(24)
    if not chats:
        return "в рабочих группах за сутки тихо"
    n = sum(len(c["messages"]) for c in chats)
    prompt = (
        f"Сделай сводку рабочих групп Telegram за сутки ({len(chats)} групп, {n} сообщений) строго по навыку "
        "groups-digest (.claude/skills/groups-digest/SKILL.md). Это фоновый запуск."
    )
    brain.run(prompt, model=settings["claude"]["model_digest"], job="groups", timeout_min=15)
    return f"групп: {len(chats)}, сообщений: {n}"


def job_briefs(uid: str | None = None, **_) -> str:
    """Брифы к встречам на ближайшие сутки-полтора (или к одной — по кнопке «Собрать бриф»)."""
    from opsdash import briefs

    settings = config.load_settings()
    if uid:
        e = store.row("SELECT * FROM events WHERE uid=?", (uid,))
        if not e:
            return "встречи уже нет в календаре"
        briefs.forget(uid)
        items = [dict(e)]
    else:
        items = briefs.pending(settings)
    if not items:
        return "новых встреч для брифа нет"
    listing = "\n".join(f"- {e['uid']} · {store.local(e['start'], '%d.%m %H:%M')} · {e['title'] or 'без названия'}"
                         for e in items)
    prompt = (
        f"Подготовь брифы к встречам строго по навыку meeting-brief (.claude/skills/meeting-brief/SKILL.md):\n{listing}\n"
        + ("Директор сама попросила бриф к этой встрече — собери его, даже если тема не очевидна. " if uid else "")
        + "Это фоновый запуск: директор не видит этот чат."
    )
    try:
        brain.run(prompt, model=settings["claude"].get("model_briefs") or "sonnet", job="briefs", timeout_min=20)
    finally:
        for e in items:  # не записал ни бриф, ни причину — помечаем, чтобы не запускать по кругу
            if not briefs.get(e["uid"]):
                briefs.fail(e["uid"], "бриф не собрался — нажмите «Собрать бриф», чтобы попробовать ещё раз")
    ready = sum(1 for e in items if (briefs.get(e["uid"]) or {}).get("status") == "ready")
    return f"брифов готово: {ready} из {len(items)}"


def job_followup(meeting_id: str, **_) -> str:
    """Текст итогов встречи для участников (followup.md) — по кнопке «Подготовить с Claude» на странице встречи."""
    settings = config.load_settings()
    prompt = (
        f"Подготовь текст итогов встречи `{meeting_id}` для участников по шагу «Текст для участников» навыка "
        "process-meeting (.claude/skills/process-meeting/SKILL.md): прочитай data/meetings/"
        f"{meeting_id}/summary.md и задачи встречи, запиши data/meetings/{meeting_id}/followup.md. "
        "Больше ничего не меняй и директору не пиши. Это фоновый запуск."
    )
    brain.run(prompt, model=settings["claude"]["model_digest"], job="followup", timeout_min=10)
    if not (records.meeting_dir(meeting_id) / "followup.md").exists():
        raise RuntimeError("Claude не создал followup.md")
    return "текст итогов готов"


def job_anthology(**_) -> str:
    settings = config.load_settings()
    prompt = (
        "Сделай недельный обзор для базы знаний строго по навыку anthology (.claude/skills/anthology/SKILL.md): "
        "предложи решения, уроки и удачи недели. Это фоновый запуск: директор узнаёт итог только через ./ops notify."
    )
    brain.run(prompt, model=settings["claude"]["model_digest"], job="anthology", timeout_min=20)
    return "обзор недели для базы знаний готов"


def job_company_context(**_) -> str:
    settings = config.load_settings()
    prompt = (
        "Обнови раздел «О компании» сводки контекста строго по навыку company-context "
        "(.claude/skills/company-context/SKILL.md). Это фоновый запуск."
    )
    brain.run(prompt, model=settings["claude"]["model_digest"], job="company-context", timeout_min=20)
    return "раздел «О компании» обновлён"


def job_stale(**_) -> str:
    settings = config.load_settings()
    if not settings["kaiten"].get("enabled") or not settings["notifications"].get("stale_pings", True):
        return "выключено"
    chat = bot.owner_chat(settings)
    if not chat or not config.get_secret("TELEGRAM_BOT_TOKEN"):
        return "бот не настроен"
    n = bot.send_stale(bot.get_bot(), chat)
    return f"напоминаний: {n}"


def job_ask(text: str, chat_id: str, reply_to: int | None = None, **_) -> str:
    settings = config.load_settings()
    sess = store.get_state("ask_session") or {}
    resume = None
    if sess.get("id") and sess.get("at") and time.time() - float(sess["at"]) < 30 * 60:
        resume = sess["id"]
    n = int(store.get_state("ask_counter", 0) or 0) + 1
    store.set_state("ask_counter", n)
    batch = f"ask:{n}"
    prompt = (
        f"Директор пишет тебе в Telegram (с телефона): «{text}»\n\n"
        f"Действуй по навыку ask (.claude/skills/ask/SKILL.md). Если создаёшь предложения "
        f"(./ops propose ...), указывай --batch {batch}. Твой последний ответ целиком уйдёт ей в Telegram — "
        "пиши коротко, простым текстом, без Markdown-таблиц."
    )
    b = bot.get_bot()
    try:
        res = brain.run(prompt, model=settings["claude"]["model_ask"], resume=resume, job="ask", timeout_min=10)
    except Exception as e:
        if resume:  # старая сессия могла пропасть — пробуем с чистого листа
            store.set_state("ask_session", None)
            res = brain.run(prompt, model=settings["claude"]["model_ask"], job="ask", timeout_min=10)
        else:
            b.send(chat_id, f"Не получилось ответить: {bot.e(str(e)[:500])}", reply_to=reply_to)
            raise
    store.set_state("ask_session", {"id": res.get("session_id"), "at": time.time()})
    answer = res["text"] or "Готово."
    items = [p for p in store.rows("SELECT id FROM proposals WHERE batch=? AND status='pending'", (batch,))]
    if items:
        from opsdash import proposals

        pending = proposals.list_proposals("pending", batch=batch)
        b.send(chat_id, bot.e(answer) + "\n\n" + bot.proposals_block(pending),
               bot.proposal_buttons(pending, batch), reply_to=reply_to)
    else:
        b.send(chat_id, bot.e(answer), reply_to=reply_to)
    return "ответ отправлен"


def job_voice(path: str, chat_id: str, reply_to: int | None = None, caption: str = "", **_) -> str:
    from pathlib import Path

    from opsdash import transcribe

    text = transcribe.transcribe_short(Path(path))
    b = bot.get_bot()
    if not text:
        b.send(chat_id, "Не разобрала слов в голосовом 🤷", reply_to=reply_to)
        return "пусто"
    b.send(chat_id, f"🎙 <i>{bot.e(text)}</i>", reply_to=reply_to)
    return job_ask(f"{caption}\n{text}".strip(), chat_id, reply_to)


def job_housekeeping(**_) -> str:
    from opsdash import transcribe

    transcribe.release_if_idle()
    try:
        bot.flush_outbox()
    except Exception:  # noqa: BLE001
        pass
    store.execute("DELETE FROM outbox WHERE sent_at IS NOT NULL AND created < ?", (overview.since(24 * 30),))
    return "готово"


def job_update_check(**_) -> str:
    from opsdash import updates

    res = updates.check()
    return f'последняя версия {res["latest"]["sha"][:7]}' + (" — доступно обновление" if res["available"] else " — уже стоит")


def job_update(**_) -> str:
    """Поставить новую версию и перезапустить PULSE (кнопка «Обновить» в настройках)."""
    from opsdash import updates

    result = updates.apply()
    store.log("update", f"обновлено: {result}; перезапуск")
    updates.restart(in_daemon=in_daemon())
    return result


REGISTRY = {
    "collect:telegram": lambda **kw: collect_source("telegram"),
    "collect:ymessenger": lambda **kw: collect_source("ymessenger"),
    "collect:pachca": lambda **kw: collect_source("pachca"),
    "collect:mail": lambda **kw: collect_source("mail"),
    "collect:gmail": lambda **kw: collect_source("gmail"),
    "collect:calendar": lambda **kw: collect_source("calendar"),
    "collect:google_calendar": lambda **kw: collect_source("google_calendar"),
    "collect:kaiten": lambda **kw: collect_source("kaiten"),
    "recordings": job_recordings,
    "docs-index": job_docs_index,
    "transcribe": job_transcribe,
    "process-meeting": job_process_meeting,
    "triage": job_triage,
    "digest": job_digest,
    "anthology": job_anthology,
    "weekly": job_weekly,
    "groups": job_groups,
    "briefs": job_briefs,
    "followup": job_followup,
    "company-context": job_company_context,
    "stale": job_stale,
    "ask": job_ask,
    "voice": job_voice,
    "housekeeping": job_housekeeping,
    "update-check": job_update_check,
    "update": job_update,
}

TITLES = {
    "collect:telegram": "Сбор: Telegram",
    "collect:ymessenger": "Сбор: Я.Мессенджер",
    "collect:pachca": "Сбор: Пачка",
    "collect:mail": "Сбор: почта",
    "collect:gmail": "Сбор: Gmail",
    "collect:calendar": "Сбор: календарь",
    "collect:google_calendar": "Сбор: Google Календарь",
    "collect:kaiten": "Сбор: Kaiten",
    "recordings": "Поиск новых записей",
    "docs-index": "Индекс документов",
    "transcribe": "Расшифровка встречи",
    "process-meeting": "Разбор встречи (Claude)",
    "triage": "Разбор сообщений (Claude)",
    "digest": "Дайджест (Claude)",
    "anthology": "База знаний: обзор недели (Claude)",
    "weekly": "Итоги недели (Claude)",
    "groups": "Сводка рабочих групп Telegram (Claude)",
    "briefs": "Брифы к встречам (Claude)",
    "followup": "Текст итогов встречи (Claude)",
    "company-context": "Сводка «О компании» для Claude",
    "stale": "Напоминания: Kaiten без движения",
    "ask": "Вопрос из Telegram (Claude)",
    "voice": "Голосовое из Telegram",
    "housekeeping": "Обслуживание",
    "update-check": "Проверка обновлений PULSE",
    "update": "Обновление PULSE",
}


def run_job(name: str, args: dict | None = None) -> str:
    fn = REGISTRY[name]
    started = time.time()
    store.execute(
        "INSERT INTO jobs(name, last_run, running) VALUES(?,?,1) "
        "ON CONFLICT(name) DO UPDATE SET last_run=excluded.last_run, running=1",
        (name, store.utcnow()),
    )
    try:
        result = fn(**(args or {}))
        store.execute(
            "UPDATE jobs SET last_ok=?, last_error=NULL, last_duration=?, running=0 WHERE name=?",
            (store.utcnow(), round(time.time() - started, 1), name),
        )
        store.log(name, f"ок: {result}")
        return result
    except Exception as e:
        store.execute(
            "UPDATE jobs SET last_error=?, last_duration=?, running=0 WHERE name=?",
            (str(e)[:1000], round(time.time() - started, 1), name),
        )
        store.log(name, f"ошибка: {e}\n{traceback.format_exc()[-1500:]}", "error")
        raise


def _key(name: str, args: dict | None) -> str:
    return name + ("|" + repr(sorted((args or {}).items())) if args else "")


def in_daemon() -> bool:
    """Работают ли очереди PULSE (а не разовый запуск из командной строки или тестов)."""
    return bool(_queues)


def submit(name: str, kind: str = "light", args: dict | None = None) -> bool:
    """Поставить задание в очередь PULSE. Без PULSE (из командной строки) — выполнить сразу."""
    if name not in REGISTRY:
        raise KeyError(name)
    if not _queues:
        threading.Thread(target=_safe_run, args=(name, args), daemon=False).start()
        return True
    key = _key(name, args)
    with _pending_lock:
        if key in _pending:
            return False
        _pending.add(key)
    _queues[kind].put((name, args, key))
    return True


def _safe_run(name: str, args: dict | None) -> None:
    try:
        run_job(name, args)
    except Exception:  # noqa: BLE001 — уже записано в журнал
        pass


def _worker(kind: str, stop: threading.Event) -> None:
    q = _queues[kind]
    while not stop.is_set():
        try:
            name, args, key = q.get(timeout=1)
        except queue.Empty:
            continue
        try:
            _safe_run(name, args)
        finally:
            with _pending_lock:
                _pending.discard(key)


def queued() -> list[str]:
    with _pending_lock:
        return sorted(_pending)


# --- расписание -----------------------------------------------------------------

def in_work_hours(settings: dict, at: datetime | None = None) -> bool:
    at = at or config.now(settings)
    wh = settings["owner"].get("work_hours") or {}
    days = wh.get("days") or [1, 2, 3, 4, 5]
    t = at.strftime("%H:%M")
    return at.isoweekday() in days and config.hhmm(wh.get("start"), "00:00") <= t < config.hhmm(wh.get("end"), "23:59")


def _due(name: str, minutes: float) -> bool:
    j = store.row("SELECT last_run FROM jobs WHERE name=?", (name,))
    last = store.parse_ts(j["last_run"]) if j else None
    return not last or datetime.now(timezone.utc) - last >= timedelta(minutes=minutes)


def _daily(key: str, at: str, settings: dict, workdays_only: bool = True) -> bool:
    """Ежедневное задание: пора, если время прошло и сегодня ещё не запускали.
    Если ноутбук был выключен утром — запустится при включении (но не позже чем через 4 часа)."""
    now = config.now(settings)
    if workdays_only and now.isoweekday() not in (settings["owner"].get("work_hours") or {}).get("days", [1, 2, 3, 4, 5]):
        return False
    try:
        hh, mm = (int(x) for x in config.hhmm(at).split(":"))
    except (ValueError, AttributeError):
        return False
    target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if not (target <= now < target + timedelta(hours=4)):
        return False
    stamp = f"daily:{key}:{now.date().isoformat()}"
    if store.get_state(stamp):
        return False
    store.set_state(stamp, True)
    return True


def _weekly(key: str, weekday: int, at: str, settings: dict) -> str | None:
    """Еженедельное задание (weekday: 1 — понедельник … 7 — воскресенье). Если в назначенный момент компьютер был
    выключен — запускается при первом включении после него. Возвращает дату назначенного дня (за какую неделю),
    None — не пора. Недели до того, как задание появилось, не догоняем."""
    now = config.now(settings)
    try:
        hh, mm = (int(x) for x in config.hhmm(at).split(":"))
        weekday = int(weekday)
    except (ValueError, AttributeError, TypeError):
        return None
    target = (now - timedelta(days=(now.isoweekday() - weekday) % 7)).replace(hour=hh, minute=mm, second=0,
                                                                              microsecond=0)
    if target > now:
        target -= timedelta(days=7)
    since = store.get_state(f"weekly:{key}:since")
    if not since:
        store.set_state(f"weekly:{key}:since", now.isoformat())
        since = now.isoformat()
    if target < datetime.fromisoformat(since):
        return None
    stamp = f"weekly:{key}:{target.date().isoformat()}"
    if store.get_state(stamp):
        return None
    store.set_state(stamp, True)
    return target.date().isoformat()


def tick(settings: dict | None = None) -> list[str]:
    """Один проход планировщика: что пора запустить."""
    settings = settings or config.load_settings()
    sch = settings["schedule"]
    started = []

    def go(name: str, kind: str = "light", args: dict | None = None):
        if submit(name, kind, args):
            started.append(name)

    for src, minutes in (("telegram", sch["collect_minutes"]), ("ymessenger", sch["collect_minutes"]),
                         ("pachca", sch["collect_minutes"]),
                         ("mail", sch["collect_minutes"]), ("gmail", sch["collect_minutes"]),
                         ("calendar", sch["calendar_minutes"]), ("google_calendar", sch["calendar_minutes"]),
                         ("kaiten", sch["kaiten_minutes"])):
        if settings[src].get("enabled") and _due(f"collect:{src}", minutes):
            go(f"collect:{src}")
    if _due("recordings", 1):
        go("recordings")
    if settings["knowledge"].get("folders") and _due("docs-index", sch.get("docs_minutes") or 30):
        go("docs-index")
    if _due("housekeeping", 5):
        go("housekeeping")
    # раз в сутки (и при первом запуске) — не вышла ли новая версия PULSE; ставит её директор кнопкой
    if settings["updates"].get("auto_check") and settings["updates"].get("repo") and _due("update-check", 24 * 60):
        go("update-check")
    if (_due("triage", sch["triage_minutes"]) and in_work_hours(settings)
            and (store.scalar("SELECT COUNT(*) FROM messages WHERE processed=0") or 0) > 0):
        go("triage", "heavy")
    if sch.get("digest_morning") and _daily("digest_morning", sch["digest_morning"], settings):
        go("digest", "heavy", {"when": "утренний"})
    if sch.get("digest_evening") and _daily("digest_evening", sch["digest_evening"], settings):
        go("digest", "heavy", {"when": "вечерний"})
    if sch.get("stale_check") and settings["kaiten"].get("enabled") and _daily("stale", sch["stale_check"], settings):
        go("stale")
    if sch.get("weekly_time") and (day := _weekly("summary", sch.get("weekly_day") or 5, sch["weekly_time"], settings)):
        go("weekly", "heavy", {"day": day})
    if sch.get("groups_digest") and settings["telegram"].get("enabled") and _daily("groups", sch["groups_digest"], settings):
        go("groups", "heavy")
    if settings["briefs"].get("enabled") and in_work_hours(settings) and _due("briefs", sch.get("briefs_minutes") or 60):
        from opsdash import briefs

        if briefs.pending(settings, limit=1):
            go("briefs", "heavy")
    if (sch.get("anthology_weekly") and config.now(settings).weekday() == 4  # пятница
            and _daily("anthology", sch["anthology_weekly"], settings)):
        go("anthology", "heavy")
    if (sch.get("context_weekly") and config.now(settings).weekday() == 0  # понедельник, до утреннего дайджеста
            and _daily("company-context", sch["context_weekly"], settings)):
        go("company-context", "heavy")
    return started


def start_workers(stop: threading.Event) -> list[threading.Thread]:
    for kind in ("light", "heavy", "interactive"):
        _queues[kind] = queue.Queue()
    # задания, прерванные выключением компьютера, — продолжаем
    store.execute("UPDATE jobs SET running=0")
    for m in store.rows("SELECT id, status FROM meetings WHERE status IN ('transcribing','processing')"):
        records.update_meeting(m["id"], status="new" if m["status"] == "transcribing" else "transcribed")
        if m["status"] == "processing":
            submit("process-meeting", "heavy", {"meeting_id": m["id"]})
    threads = []
    for kind in ("light", "heavy", "interactive"):
        t = threading.Thread(target=_worker, args=(kind, stop), daemon=True, name=f"worker-{kind}")
        t.start()
        threads.append(t)

    def scheduler():
        while not stop.is_set():
            try:
                tick()
            except Exception as e:  # noqa: BLE001
                store.log("scheduler", f"ошибка планировщика: {e}", "error")
            stop.wait(20)

    t = threading.Thread(target=scheduler, daemon=True, name="scheduler")
    t.start()
    threads.append(t)
    return threads
