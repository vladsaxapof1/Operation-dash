"""Первый день: мастер первого запуска (ONB-2) и «Первые находки» (ONB-3).

Мастер `/welcome` ведёт по главам: О вас → Помощник → Уведомления → Переписка → Календарь → Задачи → Записи → Готово.
Главы с источниками показывают те же карточки и формы, что каталог «Подключений» (connectors.yaml,
templates/connectors/). Глава сделана по тем же шагам, что и карточка (connectors.CHECKS). Прогресс — в store
(«welcome»): какие главы пропущены и пройдены, когда мастер закончен. Сам мастер открывается только на чистой
установке: нет имени и ничего не подключено (решение 06.11.2026).

«Готово» запускает первый сбор и разбор переписки — не дожидаясь расписания и рабочего времени. Переписка и почта
при первом подключении загружаются за 5 дней (config: backfill_days). Когда разбор закончен — окно «Что PULSE уже
нашёл» на Сводке и то же коротко в Telegram.
"""

from __future__ import annotations

from opsdash import config, store

STATE = "welcome"
FINDS = "first_finds"
FIRST_DAYS = 5

CHAPTERS = [
    {"id": "about", "title": "О вас", "required": True,
     "what": "Как вас зовут и когда вы работаете. По имени PULSE отличает ваши обещания от чужих, по рабочим часам — "
             "когда разбирать сообщения и сколько времени есть на задачи между встречами."},
    {"id": "assistant", "title": "Помощник", "required": True,
     "what": "Claude или ChatGPT — тот, кто разбирает встречи и переписку, пишет дайджесты и черновики. Работает по "
             "вашей подписке на этом компьютере. Без него PULSE только собирает данные."},
    {"id": "notifications", "title": "Уведомления", "connectors": ["bot"],
     "what": "Telegram-бот присылает дайджесты, итоги встреч и кнопки «Да / Нет» — подтверждать можно с телефона. "
             "Нет Telegram — пропустите: всё то же видно здесь, на Сводке."},
    {"id": "messages", "title": "Переписка", "connectors": ["telegram", "slack", "gmail", "imap", "pachca", "mail", "ymessenger"],
     "what": "Откуда PULSE берёт договорённости и просьбы: «пришлю до пятницы» в чате становится задачей со сроком, "
             "а письмо, где ждут вашего ответа, — строкой на Сводке с черновиком. Подключите хотя бы одно."},
    {"id": "calendar", "title": "Календарь", "connectors": ["google_calendar", "ics_calendar", "calendar", "caldav"],
     "what": "Встречи дня на Сводке, брифы к встречам и окна между ними — под задачи."},
    {"id": "tasks", "title": "Задачи", "connectors": ["kaiten"],
     "what": "Kaiten — если команда ведёт задачи в нём: PULSE свяжет договорённости с карточками и предложит новые. "
             "Без трекера задачи живут на доске PULSE — эту главу можно пропустить."},
    {"id": "recordings", "title": "Записи встреч", "connectors": ["recordings"],
     "what": "Папка, куда Zoom или другая программа сохраняет записи. Расшифровка идёт на этом компьютере, итоги "
             "встречи — через несколько минут после конца."},
    {"id": "done", "title": "Готово",
     "what": "Проверим, что всё работает, и запустим первый разбор."},
]
IDS = [c["id"] for c in CHAPTERS]


def state() -> dict:
    return {"skipped": [], "visited": [], "finished_at": None, **(store.get_state(STATE) or {})}


def _save(st: dict) -> None:
    store.set_state(STATE, st)


def should_open(settings: dict | None = None) -> bool:
    """Открыть мастер самому — только на чистой установке: мастер не пройден и не закрыт, имени нет, ничего не
    подключено и в базе ещё ничего нет (ни задач, ни переписки, ни встреч)."""
    settings = settings or config.load_settings()
    st = state()
    if st["finished_at"] or st.get("dismissed") or (settings["owner"].get("name") or "").strip():
        return False
    if config.any_enabled(settings) or settings["telegram_bot"].get("enabled"):
        return False
    return not store.scalar("SELECT EXISTS(SELECT 1 FROM commitments) OR EXISTS(SELECT 1 FROM messages) "
                            "OR EXISTS(SELECT 1 FROM meetings) OR EXISTS(SELECT 1 FROM attention) "
                            "OR EXISTS(SELECT 1 FROM proposals) OR EXISTS(SELECT 1 FROM events)")


def _assistant_ready(settings: dict) -> bool:
    from opsdash import brain, connectors

    a = connectors.assistant(brain.engine(settings), settings)
    return a["found"] and a["logged"] is not False


def chapters(settings: dict | None = None, catalog: dict | None = None) -> list[dict]:
    """Главы с состоянием: done — сделано, skipped — пропущено, todo — ещё нет."""
    from opsdash import connectors

    settings = settings or config.load_settings()
    catalog = catalog or connectors.catalog(settings)
    cards = {c["id"]: c for g in catalog["groups"] for c in g["cards"]}
    st = state()
    out = []
    for n, ch in enumerate(CHAPTERS, 1):
        item = {**ch, "n": n, "cards": [cards[i] for i in ch.get("connectors", []) if i in cards]}
        if ch["id"] == "about":
            done = bool((settings["owner"].get("name") or "").strip())
        elif ch["id"] == "assistant":
            done = _assistant_ready(settings)
        elif ch["id"] == "recordings":
            done = ch["id"] in st["visited"]  # папка по умолчанию есть всегда — сделано, когда посмотрели
        elif ch["id"] == "done":
            done = bool(st["finished_at"])
        else:
            done = any(c["kind"] in ("ok", "pending") for c in item["cards"])
        item["status"] = "done" if done else ("skipped" if ch["id"] in st["skipped"] else "todo")
        out.append(item)
    return out


def next_id(cid: str) -> str:
    i = IDS.index(cid) if cid in IDS else -1
    return IDS[min(i + 1, len(IDS) - 1)]


def prev_id(cid: str) -> str:
    i = IDS.index(cid) if cid in IDS else 0
    return IDS[max(i - 1, 0)]


def mark(cid: str, how: str) -> None:
    """how: visited (нажали «Дальше») или skipped («Пропустить»)."""
    st = state()
    for key in ("visited", "skipped"):
        if cid in st[key]:
            st[key].remove(cid)
    st[how].append(cid)
    _save(st)


def current(settings: dict | None = None) -> str:
    """С какой главы продолжить: первая не сделанная и не пропущенная."""
    for ch in chapters(settings):
        if ch["status"] == "todo" and ch["id"] != "done":
            return ch["id"]
    return "done"


def finish() -> bool:
    """«Готово»: мастер пройден, первый сбор и разбор — сразу. False — помощника нет, разбирать некому."""
    from opsdash import jobs

    st = state()
    st["finished_at"] = store.utcnow()
    _save(st)
    if not _assistant_ready(config.load_settings()):
        return False
    store.set_state(FINDS, {"status": "queued", "queued": store.utcnow(), "days": FIRST_DAYS})
    jobs.submit("first-finds", "heavy")
    return True


# --- «Первые находки» (ONB-3) --------------------------------------------------------------------------------

def findings(since: str) -> dict:
    """Что нашёл первый разбор: ждут ответа, задачи директора и что ей должны — появившиеся после since;
    ближайшие встречи."""
    from opsdash import records

    attention = store.rows("SELECT * FROM attention WHERE status='open' ORDER BY priority='high' DESC, id DESC")
    mine = store.rows("SELECT * FROM commitments WHERE status='open' AND direction='mine' AND created >= ? "
                      "ORDER BY due IS NULL, due, id DESC", (since,))
    to_me = store.rows("SELECT * FROM commitments WHERE status='open' AND direction='to_me' AND created >= ? "
                       "ORDER BY due IS NULL, due, id DESC", (since,))
    events = store.rows("SELECT uid, title, start FROM events WHERE start >= ? AND all_day=0 ORDER BY start LIMIT 3",
                        (store.utcnow(),))

    def task(c: dict) -> dict:
        return {"id": c["id"], "title": records.headline(c), "who": c.get("who") or "", "whom": c.get("whom") or "",
                "due": c.get("due") or ""}

    return {
        "attention": len(attention),
        "attention_top": [{"id": a["id"], "summary": a["summary"], "sender": a.get("sender_name") or "",
                           "chat": a.get("chat_title") or ""} for a in attention[:3]],
        "mine": len(mine), "mine_top": [task(c) for c in mine[:3]],
        "to_me": len(to_me), "to_me_top": [task(c) for c in to_me[:3]],
        "events": [{"uid": e["uid"], "title": e["title"], "start": e["start"]} for e in events],
        "messages": store.scalar("SELECT COUNT(*) FROM messages") or 0,
    }


def job_first_finds() -> str:
    """Первый разбор после мастера: собрать переписку, разобрать её сразу и показать находки."""
    from opsdash import jobs

    st = {**(store.get_state(FINDS) or {}), "status": "running", "started": store.utcnow(), "days": FIRST_DAYS}
    store.set_state(FINDS, st)
    try:
        result = jobs.job_triage(force=True)
    except Exception as e:
        store.set_state(FINDS, {**st, "status": "failed", "error": str(e)[:300]})
        raise
    found = findings(st["started"])
    store.set_state(FINDS, {**st, "status": "done", "finished": store.utcnow(), "result": result, "found": found,
                            "seen": False})
    _tell(found)
    return f"ждут ответа: {found['attention']}, ваших задач: {found['mine']}, вам должны: {found['to_me']}"


def _tell(found: dict) -> None:
    """То же коротко — в Telegram, если бот привязан."""
    from opsdash import bot

    tb = config.load_settings()["telegram_bot"]
    if not (tb.get("enabled") and tb.get("owner_chat_id")):
        return
    if not found["messages"]:
        text = "<b>PULSE готов к работе.</b> Переписка пока не подключена — находки появятся, когда подключите Telegram или почту."
    else:
        text = (f"<b>PULSE разобрал переписку за {FIRST_DAYS} дней.</b>\n"
                f"Ждут вашего ответа: {found['attention']}\nВаши задачи: {found['mine']} · вам должны: {found['to_me']}\n"
                "Подробнее — на Сводке. Первые подсказки могут промахиваться: «Нет» убирает подсказку, и она не вернётся.")
    try:
        bot.notify(text)
    except Exception as e:  # noqa: BLE001 — находки видны и на Сводке
        store.log("welcome", f"не отправил первые находки в Telegram: {e}", "warning")


def finds_for_home() -> dict | None:
    """Для Сводки: идёт первый разбор (полоса «PULSE читает переписку…») или готовы находки, ещё не показанные."""
    st = store.get_state(FINDS) or {}
    if st.get("status") in ("queued", "running"):
        return st
    if st.get("status") == "done" and not st.get("seen"):
        return st
    return None


def finds_seen() -> None:
    st = store.get_state(FINDS) or {}
    if st:
        store.set_state(FINDS, {**st, "seen": True})
