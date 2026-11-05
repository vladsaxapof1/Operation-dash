"""Ход задачи: что по ней происходило — от появления до закрытия.

Записи трёх видов:
- комментарий директора (note) — сама дописала в окне задачи или попросила бота «запиши в задачу…»;
- что произошло (update) и «спросили — ждём ответа» (ask) — Claude, разбирая переписку и встречи: «Пётр: этим
  занимался Олег», «Спросили Петра, когда вернётся Олег», «Олег вернётся в среду 11.11, до 16.11 подпишет
  договор»;
- события PULSE (event) — без Claude: перенесли срок, взяли в работу, ждёт, напомнили, карточка Kaiten перешла
  в другую колонку.

Последняя запись директора или Claude — «Сейчас» на карточке: статус обновляется сам, собирать его по чатам
не нужно. Если последняя — вопрос (ask), пока после него ничего не пришло, у задачи «ответа нет N дней».
По одному сообщению или встрече у задачи одна запись: повторный разбор её обновляет, а не дублирует.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone

from opsdash import config, store

KINDS = ("note", "update", "ask", "event")
MESSAGE_SOURCES = ("telegram", "ymessenger", "pachca", "mail")
SOURCES = (*MESSAGE_SOURCES, "meeting", "kaiten", "agent", "manual")
TEXT_MAX = 1500
SOURCE_NAMES = {"telegram": "Telegram", "ymessenger": "Я.Мессенджер", "pachca": "Пачка", "mail": "Почта",
                "kaiten": "Kaiten", "agent": "сессия агента"}
KIND_FOR_CLAUDE = {"note": "комментарий директора", "update": "", "ask": "спросили, ждём ответа", "event": "событие"}

_TIMECODE = re.compile(r"\[(\d{1,2}):(\d{2})(?::(\d{2}))?\]")


def author() -> str:
    """Кто пишет: фоновый Claude или директор (дашборд, ./ops по её просьбе в чате)."""
    return "claude" if os.environ.get("OPSDASH_AGENT") else "director"


def _plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(n) % 100
    if 11 <= n <= 14:
        return many
    return one if n % 10 == 1 else few if 2 <= n % 10 <= 4 else many


def parse_source(source: str | None) -> tuple[str | None, str | None]:
    """«pachca:4040» → ("pachca", "4040"); «manual» или пусто → ("manual", None) / (None, None)."""
    st, _, ref = (source or "").strip().partition(":")
    st, ref = st.strip().lower() or None, ref.strip() or None
    if st == "manual":
        ref = None
    if st and st not in SOURCES:
        raise ValueError(f"Источник — один из: {', '.join(SOURCES)} (например, pachca:4040 или meeting:<id>)")
    return st, ref


def _when(st: str | None, ref: str | None, quote: str | None, at: str | None) -> str:
    """Когда это случилось: указанное время; время сообщения; начало встречи плюс отметка [чч:мм:сс] из цитаты;
    иначе — сейчас."""
    if at:
        dt = store.parse_local(at)
        if not dt:
            raise ValueError("Время — ГГГГ-ММ-ДД или ГГГГ-ММ-ДД ЧЧ:ММ")
        return store.to_utc_iso(dt)
    if st in MESSAGE_SOURCES and str(ref or "").isdigit():
        ts = store.scalar("SELECT ts FROM messages WHERE id=?", (int(ref),))
        if ts:
            return store.to_utc_iso(store.parse_ts(ts))
    if st == "meeting" and ref:
        dt = store.parse_ts(store.scalar("SELECT started_at FROM meetings WHERE id=?", (ref,)))
        if dt:
            m = _TIMECODE.search(quote or "")
            if m:
                a, b, c = m.groups()
                dt += timedelta(hours=int(a), minutes=int(b), seconds=int(c)) if c else timedelta(minutes=int(a),
                                                                                                  seconds=int(b))
            return store.to_utc_iso(dt)
    return store.utcnow()


def add(task_id: int, text: str, *, kind: str = "update", source: str | None = None, quote: str | None = None,
        at: str | None = None, by: str | None = None) -> tuple[int, bool]:
    """Записать в ход задачи. source — «pachca:4040», «meeting:<id>», «manual»; at — когда случилось (по умолчанию
    время сообщения или встречи). Вернуть (id записи, новая ли она): по тому же сообщению или встрече у задачи одна
    запись — повтор обновляет её текст."""
    if kind not in KINDS:
        raise ValueError(f"Вид записи — один из: {', '.join(KINDS)}")
    text = re.sub(r"\n{3,}", "\n\n", (text or "").strip())[:TEXT_MAX]
    if not text:
        raise ValueError("Напишите, что произошло")
    if not store.row("SELECT 1 FROM commitments WHERE id=?", (task_id,)):
        raise ValueError(f"Нет задачи [{task_id}]")
    st, ref = parse_source(source)
    by = by if by in ("director", "claude", "pulse") else ("pulse" if kind == "event" else author())
    when = _when(st, ref, quote, at)
    quote = " ".join((quote or "").split())[:500] or None
    if kind != "event":
        old = None
        if st and ref:
            old = store.row("SELECT id FROM task_log WHERE task_id=? AND source_type=? AND source_ref=? "
                            "AND kind!='event'", (task_id, st, ref))
        old = old or store.row("SELECT id FROM task_log WHERE task_id=? AND kind=? AND lower(text)=lower(?)",
                               (task_id, kind, text))
        if old:
            store.execute("UPDATE task_log SET text=?, kind=?, quote=COALESCE(?, quote), at=? WHERE id=?",
                          (text, kind, quote, when, old["id"]))
            return old["id"], False
    lid = store.execute(
        "INSERT INTO task_log(task_id, at, kind, text, source_type, source_ref, quote, author, created) "
        "VALUES(?,?,?,?,?,?,?,?,?)", (task_id, when, kind, text, st, ref, quote, by, store.utcnow()))
    return lid, True


def event(task_id: int | None, text: str, *, source: str | None = None) -> None:
    """Событие PULSE в ходе задачи: срок перенесли, взяли в работу, ждёт, напомнили. Одно и то же событие дважды
    за 10 минут не пишем (щёлкнули туда-обратно). Ошибка записи не мешает самому действию."""
    if not task_id:
        return
    try:
        parse_source(source)
    except ValueError:  # источник непонятного вида — событие всё равно пишем, без него
        source = None
    try:
        last = store.row("SELECT text, created FROM task_log WHERE task_id=? AND kind='event' ORDER BY id DESC LIMIT 1",
                         (task_id,))
        if last and last["text"] == text and (store.parse_ts(last["created"]) or datetime.min.replace(
                tzinfo=timezone.utc)) > datetime.now(timezone.utc) - timedelta(minutes=10):
            return
        add(task_id, text, kind="event", source=source, by="pulse")
    except Exception as e:  # noqa: BLE001
        store.log("worklog", f"событие для задачи [{task_id}] не записано: {e}", "warning")


def get(entry_id: int) -> dict | None:
    return store.row("SELECT * FROM task_log WHERE id=?", (entry_id,))


def remove(entry_id: int) -> None:
    store.execute("DELETE FROM task_log WHERE id=?", (entry_id,))


def move(drop_id: int, keep_id: int) -> None:
    """Дубль объединили — его ход переходит к остающейся задаче."""
    store.execute("UPDATE task_log SET task_id=? WHERE task_id=?", (keep_id, drop_id))


def last_id() -> int:
    """Последняя запись хода — до разбора сообщений или встречи: потом объявим то, что появилось после неё."""
    return store.scalar("SELECT COALESCE(MAX(id), 0) FROM task_log") or 0


def announce(since: int, kind: str, where: str = "") -> int:
    """После разбора сообщений или встречи — уведомление в углу экрана «Обновил ход задачи»: какие задачи
    сдвинулись и что по ним теперь (последняя новая запись). Одна задача — ссылка на неё, несколько — на доску.
    Объявляем только записи Claude (не события PULSE и не то, что директор попросила записать сама).
    Вернуть, сколько новых записей."""
    from opsdash import notices, records

    rows = store.rows("SELECT l.task_id, l.text, c.title, c.what FROM task_log l JOIN commitments c ON c.id = l.task_id "
                      "WHERE l.id > ? AND l.kind != 'event' AND l.author = 'claude' ORDER BY l.at, l.id", (since,))
    if not rows:
        return 0
    last: dict[int, dict] = {}
    for r in rows:
        last.pop(r["task_id"], None)
        last[r["task_id"]] = r  # последняя по времени — в конце
    tasks = list(reversed(last.values()))
    short = lambda t: t if len(t) <= 120 else t[:119].rsplit(" ", 1)[0] + "…"  # noqa: E731
    if len(tasks) == 1:
        t = tasks[0]
        title = f"Обновил ход задачи «{records.headline(t)}»"
        text, link = short(t["text"]), f"/commitments?task={t['task_id']}"
    else:
        title = f"Обновил ход задач: {len(tasks)}"
        text = "; ".join(f"«{records.headline(t)}» — {short(t['text'])}" for t in tasks[:3])
        text += f"; и ещё {len(tasks) - 3}" if len(tasks) > 3 else ""
        link = "/commitments"
    notices.add(kind, title + (f" {where}" if where else ""), text, link)
    return len(rows)


# --- чтение -----------------------------------------------------------------------------------

def entries(task_ids: list[int], extra: list[dict] = ()) -> dict[int, list[dict]]:
    """Ход задач: {id задачи: записи от старых к новым}. У записи — src (откуда, по-человечески: «Пачка · Пётр»,
    «встреча «Трекинг»», «вы», «Claude»), when (ДД.ММ ЧЧ:ММ). extra — записи «появилась» (origin): встают на время
    сообщения или встречи, из которых задача взялась."""
    if not task_ids:
        return {}
    marks = ",".join("?" * len(task_ids))
    rows = store.rows(f"SELECT * FROM task_log WHERE task_id IN ({marks}) ORDER BY at, id", list(task_ids)) + list(extra)
    msg_ids = [int(r["source_ref"]) for r in rows if r["source_type"] in MESSAGE_SOURCES and str(r["source_ref"]).isdigit()]
    msgs = {m["id"]: m for m in store.rows(
        f"SELECT id, source, chat_id, chat_title, topic, msg_id, sender_id, sender_name, is_outgoing, ts, link FROM messages "
        f"WHERE id IN ({','.join('?' * len(msg_ids)) or 'NULL'})", msg_ids)}
    meet_ids = list({r["source_ref"] for r in rows if r["source_type"] == "meeting" and r["source_ref"]})
    meets = {m["id"]: m for m in store.rows(
        f"SELECT id, title, started_at FROM meetings WHERE id IN ({','.join('?' * len(meet_ids)) or 'NULL'})", meet_ids)}
    out: dict[int, list[dict]] = {}
    for r in rows:
        _label(r, msgs, meets)
        out.setdefault(r["task_id"], []).append(r)
    for log in out.values():
        log.sort(key=lambda r: (r["at"], r["kind"] != "origin", r["id"] or 0))
    return out


def _label(r: dict, msgs: dict, meets: dict) -> None:
    st, ref = r["source_type"], r["source_ref"]
    r["msg"] = None
    if st in MESSAGE_SOURCES:
        m = msgs.get(int(ref)) if str(ref or "").isdigit() else None
        r["msg"] = m
        chat = " · ".join(filter(None, ((m or {}).get("chat_title"), (m or {}).get("topic"))))
        r["src"] = " · ".join(filter(None, (SOURCE_NAMES.get(st, st), chat)))
        if m and r["kind"] == "origin":
            r["at"] = store.to_utc_iso(store.parse_ts(m["ts"]))
    elif st == "meeting":
        m = meets.get(ref)
        r["src"] = f"встреча «{m['title'] or ref}»" if m else "встреча"
        if m and m["started_at"] and r["kind"] == "origin":
            r["at"] = store.to_utc_iso(store.parse_ts(m["started_at"]))
    elif st in SOURCE_NAMES:
        r["src"] = SOURCE_NAMES[st]
    elif r["kind"] == "origin":
        r["src"] = "добавлена вручную"
    else:
        r["src"] = {"director": "вы", "claude": "Claude", "pulse": "PULSE"}.get(r["author"], "")
    r["when"] = store.local(r["at"], "%d.%m %H:%M")
    r["day"] = store.local(r["at"], "%d.%m")


def _origin(c: dict) -> dict:
    """Запись «появилась» — первая в ходе задачи. В базе её нет: она из самой задачи (источник, цитата, когда)."""
    return {"id": None, "task_id": c["id"], "at": c.get("created") or store.utcnow(), "kind": "origin",
            "text": "Задача появилась", "source_type": c.get("source_type"), "source_ref": c.get("source_ref"),
            "quote": c.get("quote"), "author": None, "created": c.get("created")}


def current(log: list[dict]) -> dict | None:
    """«Сейчас» — последняя запись директора или Claude (события PULSE и «появилась» статусом не считаем).
    У вопроса без ответа — waiting: «ответа пока нет» / «ответа нет 2 дня»."""
    for e in reversed(log):
        if e["kind"] in ("event", "origin"):
            continue
        now = dict(e)
        now["waiting"] = None
        if e["kind"] == "ask":
            dt = store.parse_ts(e["at"])
            days = (config.now().date() - dt.astimezone(config.tz()).date()).days if dt else 0
            now["waiting"] = ("ответа пока нет" if days <= 0
                              else f"ответа нет {days} {_plural(days, 'день', 'дня', 'дней')}")
            now["waiting_short"] = "без ответа" + (f" {days} дн." if days > 0 else "")
            e["waiting"] = now["waiting"]  # в ходе задачи у этого вопроса — «ответа нет …»
        return now
    return None


def decorate(items: list[dict]) -> None:
    """c["log"] — ход задачи (старые сверху, первая — «появилась»), c["log_count"] — сколько в нём записей
    (без «появилась»), c["now"] — что по задаче сейчас (у открытых)."""
    if not items:
        return
    by_task = entries([c["id"] for c in items], extra=[_origin(c) for c in items])
    for c in items:
        c["log"] = by_task.get(c["id"], [])
        c["log_count"] = sum(1 for e in c["log"] if e["kind"] != "origin")
        c["now"] = current(c["log"]) if c.get("status") == "open" else None


def add_links(items: list[dict]) -> None:
    """У записей из переписки — ссылка на сообщение (link, link_label), у встреч — на страницу встречи."""
    from opsdash import links

    tg = list({e["msg"]["chat_id"] for c in items for e in c.get("log") or []
               if e.get("msg") and e["msg"]["source"] == "telegram"})
    usernames = {r["chat_id"]: r["username"] for r in store.rows(
        f"SELECT chat_id, username FROM chats WHERE source='telegram' AND chat_id IN ({','.join('?' * len(tg)) or 'NULL'})",
        tg)}  # одним запросом: на доске сотни записей хода
    for c in items:
        for e in c.get("log") or []:
            e["link"] = e["link_label"] = None
            m = e.get("msg")
            if m:
                extra = ({"username": usernames.get(m["chat_id"])} if m["source"] == "telegram" else
                         {"saved": m.get("link")} if m["source"] == "pachca" else {})
                found = links.original(m["source"], m["chat_id"], m["msg_id"], m["sender_id"], **extra)
                if found:
                    e["link"], e["link_label"] = found
            elif e["source_type"] == "meeting" and e["source_ref"]:
                e["link"], e["link_label"] = f"/meetings/{e['source_ref']}", "Открыть встречу"
            elif e["source_type"] == "kaiten" and str(e["source_ref"] or "").isdigit():
                url = store.scalar("SELECT url FROM kaiten_cards WHERE id=?", (int(e["source_ref"]),))
                if url:
                    e["link"], e["link_label"] = url, "Карточка в Kaiten"


# --- для Claude -----------------------------------------------------------------------------------

def now_text(c: dict, limit: int = 220) -> str:
    """«сейчас (05.11, Telegram · Олег): …» для строки задачи в ./ops — Claude видит статус в любом списке."""
    now = c.get("now")
    if not now:
        return ""
    text = " ".join(now["text"].split())
    text = text if len(text) <= limit else text[:limit - 1].rsplit(" ", 1)[0] + "…"
    who = ", ".join(filter(None, (now["day"], now["src"] if now["src"] not in ("вы", "Claude") else
                                  ("директор" if now["author"] == "director" else ""))))
    return f"сейчас ({who}): {text}" + (f" — {now['waiting']}" if now["waiting"] else "")


def lines(c: dict) -> list[str]:
    """Ход задачи целиком для ./ops commitments show и контекста агентов: от появления до последней записи."""
    out = [f"Ход задачи [{c['id']}] (старые сверху; записей {c.get('log_count', 0)}):"]
    for e in c.get("log") or []:
        ref = f" [{e['source_type']}:{e['source_ref']}]" if e["source_ref"] else ""
        if e["kind"] == "origin":
            out.append(f"- {e['when']} · появилась · {e['src'] or '—'}{ref}")
            continue
        kind = KIND_FOR_CLAUDE.get(e["kind"], "")
        line = f"- {e['when']} · {e['src'] or '—'}{ref} · запись {e['id']}{' · ' + kind if kind else ''}: {e['text']}"
        if e["quote"]:
            line += f" — «{e['quote']}»"
        out.append(line)
    if c.get("now") and c["now"]["waiting"]:
        out.append(f"Сейчас: последний вопрос без ответа — {c['now']['waiting']}")
    return out
