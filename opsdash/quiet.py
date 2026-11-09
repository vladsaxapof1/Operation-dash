"""Задачи без движения: срок, может, ещё не прошёл, но по задаче давно ничего не происходит — её забыли.

Отдельный признак, не часть приоритета: «просрочено» — про срок, «без движения» — про ход.

Движение — запись хода от Claude или директора (что произошло, вопрос, комментарий) и события PULSE, которые
двигают задачу: взяли в работу, готов пункт, дождалась, напомнили, карточка Kaiten перешла в другую колонку,
встреча по задаче. Не движение — перенос срока (это скорее признак, что задача буксует), смена приоритета, «ждёт»,
архив. Если движения не было вовсе — считаем от появления задачи.

Порог — рабочие дни из Настроек → День («Задачи без движения»): ваши задачи — 5, «мне должны» — 3; 0 — не отмечать.
Договорённости между коллегами не отмечаем: их ведут не вы. Не отмечаем и то, что сейчас не продвинуть: задача ждёт
другую задачу или событие, часть передана коллеге, спросили и ждём ответа (у неё своё «ответа нет N дней»).

«Не трогать неделю» (или новый срок из окна разбора) — commitments.quiet_until: до этой даты задача не отмечается.
"""

from __future__ import annotations

from datetime import date, timedelta

from opsdash import config, store

DIRECTIONS = {"mine": "quiet_mine", "to_me": "quiet_to_me"}
SNOOZE_DAYS = 7
# события PULSE, которые задачу не двигают (начало текста записи, records/worklog.event)
NOT_MOVES = ("Срок", "Приоритет", "Убрана в архив", "Больше не напоминать", "Пункт снова не готов", "Снова ждёт",
             "Ждёт", "Kaiten: ждёт", "Без движения")


def limits(settings: dict | None = None) -> dict[str, int]:
    """Порог в рабочих днях по типу задачи: {"mine": 5, "to_me": 3}; 0 — не отмечать."""
    settings = settings or config.load_settings()
    t = {**config.DEFAULTS["tasks"], **(settings.get("tasks") or {})}
    out = {}
    for direction, key in DIRECTIONS.items():
        try:
            out[direction] = max(0, int(t.get(key)))
        except (TypeError, ValueError):
            out[direction] = config.DEFAULTS["tasks"][key]
    return out


def workdays_between(start: date, end: date, settings: dict) -> int:
    """Сколько рабочих дней прошло после start по end включительно (сам start не считаем)."""
    days = (settings["owner"].get("work_hours") or {}).get("days") or [1, 2, 3, 4, 5]
    n, d = 0, start
    while d < end:
        d += timedelta(days=1)
        n += d.isoweekday() in days
    return n


def is_move(e: dict) -> bool:
    """Запись хода, которая двигает задачу (см. описание модуля). «Появилась» — тоже: от неё считаем, если больше
    ничего не было."""
    if e.get("kind") != "event":
        return True
    return not (e.get("text") or "").startswith(NOT_MOVES)


def last_move(c: dict) -> dict | None:
    """Последнее движение по задаче — из её хода (worklog.decorate кладёт его в c["log"])."""
    return next((e for e in reversed(c.get("log") or []) if is_move(e)), None)


def _local_day(ts: str | None) -> date | None:
    dt = store.parse_ts(ts)
    return dt.astimezone(config.tz()).date() if dt else None


def compute(c: dict, today: date, lim: dict[str, int], settings: dict) -> dict | None:
    """Без движения ли задача: {days — календарных дней с последнего движения, workdays, since — его дата, text —
    что было, src — откуда, origin — движения не было с появления}. None — нет (или её сейчас не продвинуть)."""
    if c.get("status") != "open" or c.get("archived_at") or c.get("archived"):
        return None
    limit = lim.get(c.get("direction"), 0)
    if not limit:
        return None
    if c.get("quiet_until") and c["quiet_until"] >= today.isoformat():
        return None
    if c.get("blocked") or (c.get("now") or {}).get("waiting"):
        return None
    if any(k["status"] == "open" and not k.get("archived_at") for k in c.get("children") or []):
        return None  # часть передана — ждём её (у части — свой счёт)
    e = last_move(c)
    since = _local_day(e["at"] if e else c.get("created"))
    if not since:
        return None
    work = workdays_between(since, today, settings)
    if work < limit:
        return None
    origin = not e or e.get("kind") == "origin"
    return {"days": (today - since).days, "workdays": work, "since": since.isoformat(), "origin": origin,
            "text": "задача появилась" if origin else (e.get("text") or ""), "src": None if origin else e.get("src")}


def decorate(items: list[dict], settings: dict | None = None) -> None:
    """c["quiet"] — без движения ли задача (для карточки, окна задачи, фильтра и Сводки). Нужен ход (worklog.decorate)
    и связи (records._relations)."""
    if not items:
        return
    settings = settings or config.load_settings()
    lim, today = limits(settings), config.now(settings).date()
    for c in items:
        c["quiet"] = compute(c, today, lim, settings)


def snooze(cid: int, days: int = SNOOZE_DAYS, *, log: bool = True) -> str:
    """«Не трогать неделю»: до этой даты задача не отмечается, потом — снова, если ничего не случилось. log=False —
    без записи в ход (новый срок из окна разбора: перенос там и так виден)."""
    if not store.row("SELECT 1 FROM commitments WHERE id=?", (cid,)):
        raise ValueError(f"Нет задачи [{cid}]")
    until = (config.now().date() + timedelta(days=max(1, days))).isoformat()
    store.execute("UPDATE commitments SET quiet_until=?, seen_at=COALESCE(seen_at, ?) WHERE id=?",
                  (until, store.utcnow(), cid))
    if log:
        from opsdash import worklog

        worklog.event(cid, f"Без движения: не напоминать до {until[8:10]}.{until[5:7]}")
    return until


def queue(limit: int = 50) -> list[dict]:
    """Ваши задачи и «мне должны» без движения — дольше всех стоящие сверху (Сводка, окно «Разобрать по одной»,
    ./ops overview и дайджест)."""
    from opsdash import records

    items = [c for d in DIRECTIONS for c in records.list_commitments(status="open", direction=d) if c.get("quiet")]
    items.sort(key=lambda c: (-c["quiet"]["days"], c.get("due") or "9999-99-99"))
    return records.with_reminders(items[:limit])  # «Напомнить» в окне разбора — у «мне должны»


def line(c: dict) -> str:
    """«без движения 9 дн. — последнее 31.10: Олег прислал прайс» — для ./ops и дайджеста."""
    q = c.get("quiet")
    if not q:
        return ""
    when = f"{q['since'][8:10]}.{q['since'][5:7]}"
    last = f"появилась {when}, хода не было" if q["origin"] else f"последнее {when}: {q['text'][:120]}"
    return f"без движения {q['days']} дн. — {last}"
