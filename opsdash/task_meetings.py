"""Задачи и встречи календаря: встреча, посвящённая задаче («собраться ещё раз по лид-продаже» →
«Лид-продажа» в четверг в 15:00).

Связь видна на карточке задачи (метка со временем встречи) и в её окне, попадает в бриф к встрече
и в разбор записи: по итогам Claude предлагает закрыть или обновить задачу. Задача ждёт встречи
открытой. Похожие встречи PULSE находит сам после обновления календаря и предлагает связать —
или связывает сразу, если так разрешено в Настройки → Автономия.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from opsdash import config, records, store

AHEAD_DAYS = 14  # среди каких встреч искать посвящённые задаче и что предлагать в «Выбрать из календаря»
WEEKDAYS = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")
# слова, которые есть в названии любой встречи и задачи «встретиться»: по ним встречи не сопоставляем
_GENERIC = {"встре", "созво", "звоно", "обсуд", "обсуж", "собра", "синк", "sync", "meeti", "call", "еще", "раз",
            "вопро"}


def _words(text: str | None) -> set[str]:
    return records._stems(text) - _GENERIC


def _snapshot(e: dict) -> str:
    return store.json_col({"title": e.get("title") or "", "start": e.get("start"), "end": e.get("end")})


def link(commitment_id: int, uid: str, *, title: str | None = None, start: str | None = None,
         end: str | None = None) -> None:
    """Связать задачу со встречей. Название и время запоминаем: встреча, только что созданная из PULSE, появится
    в календаре PULSE со следующей синхронизацией, а прошедшие со временем из него уходят."""
    e = store.row('SELECT title, start, "end" FROM events WHERE uid=?', (uid,)) or {"title": title, "start": start,
                                                                                 "end": end}
    store.execute("UPDATE commitments SET event_uid=?, event_info=?, updated=? WHERE id=?",
                  (uid, _snapshot(e), store.utcnow(), commitment_id))


def unlink(commitment_id: int) -> None:
    store.execute("UPDATE commitments SET event_uid=NULL, event_info=NULL, updated=? WHERE id=?",
                  (store.utcnow(), commitment_id))


def when(start: datetime, end: datetime | None = None) -> str:
    """Коротко для метки на карточке: «сегодня 15:00», «завтра 15:00», «чт 15:00», «07.11 15:00», «была 27.10»."""
    now = config.now()
    local = start.astimezone(now.tzinfo)
    if (end or start) < now:
        return "была сегодня" if local.date() == now.date() else f"была {local:%d.%m}"
    days = (local.date() - now.date()).days
    day = {0: "сегодня", 1: "завтра"}.get(days) or (WEEKDAYS[local.weekday()] if days < 7 else f"{local:%d.%m}")
    return f"{day} {local:%H:%M}"


def decorate(items: list[dict]) -> None:
    """c["meeting"] — встреча по задаче (название, время, участники, прошла ли, есть ли запись и разбор);
    c["meeting_hint"] — предложение связать её со встречей, которое ждёт директора."""
    uids = list({c["event_uid"] for c in items if c.get("event_uid")})
    marks = ",".join("?" * len(uids))
    live = {e["uid"]: e for e in store.rows(f'SELECT uid, title, start, "end", attendees, url FROM events '
                                            f"WHERE uid IN ({marks})", uids)} if uids else {}
    recorded = {m["event_uid"]: m["id"] for m in store.rows(
        f"SELECT id, event_uid FROM meetings WHERE event_uid IN ({marks})", uids)} if uids else {}
    hints = {}
    for p in store.rows("SELECT * FROM proposals WHERE kind='task.meeting' AND status='pending' ORDER BY id"):
        p["payload"] = store.loads(p["payload"], {})
        hints[p["payload"].get("commitment_id")] = p
    now = datetime.now(timezone.utc)
    for c in items:
        c["meeting"] = c["meeting_hint"] = None
        uid = c.get("event_uid")
        if uid:
            e = live.get(uid) or store.loads(c.get("event_info"), {}) or {}
            start, end = store.parse_ts(e.get("start")), store.parse_ts(e.get("end"))
            if start:
                who = [a.get("name") or a.get("email") for a in store.loads(e.get("attendees"), [])]
                c["meeting"] = {"uid": uid, "title": e.get("title") or "Без названия", "start": e["start"],
                                "end": e.get("end"), "when": when(start, end), "past": (end or start) < now,
                                "people": [w for w in who if w], "url": e.get("url"), "recorded": recorded.get(uid)}
        elif c["status"] == "open":
            c["meeting_hint"] = hints.get(c["id"])


def upcoming(days: int = AHEAD_DAYS) -> list[dict]:
    from opsdash import overview

    now = config.now()
    return [e for e in overview.events_between(now, now + timedelta(days=days)) if not e["all_day"]]


def label(e: dict) -> str:
    """Подпись встречи в «Выбрать из календаря»; по ней же встреча находится обратно (find)."""
    return f"{store.local(e['start'], '%d.%m %H:%M')} · {e['title'] or 'Без названия'}"


def options() -> list[dict]:
    return [{"label": label(e), "uid": e["uid"]} for e in upcoming()]


def find(text: str) -> str | None:
    """uid встречи по подписи из списка (или по самому uid)."""
    text = (text or "").strip()
    if not text:
        return None
    if store.row("SELECT 1 FROM events WHERE uid=?", (text,)):
        return text
    return next((o["uid"] for o in options() if o["label"] == text), None)


def tasks_for(uid: str, status: str | None = "open") -> list[dict]:
    """Задачи, которым посвящена встреча."""
    sql = "SELECT * FROM commitments WHERE event_uid=?" + (" AND status=?" if status else "") + " ORDER BY id"
    return store.rows(sql, (uid, status) if status else (uid,))


def for_events(events: list[dict]) -> list[dict]:
    """e["tasks"] — задачи, которым посвящена встреча (для карточки встречи в календаре)."""
    uids = [e["uid"] for e in events]
    found: dict[str, list[dict]] = {}
    if uids:
        for c in store.rows(f"SELECT * FROM commitments WHERE status != 'cancelled' AND event_uid IN "
                            f"({','.join('?' * len(uids))}) ORDER BY id", uids):
            found.setdefault(c["event_uid"], []).append({"id": c["id"], "headline": records.headline(c),
                                                         "route": records.route(c), "status": c["status"]})
    for e in events:
        e["tasks"] = found.get(e["uid"], [])
    return events


def suggest() -> int:
    """После обновления календаря: найти встречи, посвящённые открытым задачам без встречи, и предложить связать.
    Сравниваем название встречи с задачей (общие слова) и участников с исполнителем. Вернуть, сколько предложено."""
    from opsdash import proposals

    events = upcoming()
    if not events:
        return 0
    tasks = store.rows("SELECT * FROM commitments WHERE status='open' AND archived_at IS NULL AND event_uid IS NULL")
    asked = set()
    for p in store.rows("SELECT payload FROM proposals WHERE kind='task.meeting'"):
        pl = store.loads(p["payload"], {})
        asked.add((pl.get("commitment_id"), pl.get("event_uid")))
    scored = []
    for e in events:
        names = records._stems(" ".join(a.get("name") or "" for a in e["attendees"]))
        scored.append((e, _words(e["title"]), names))
    made = 0
    for c in tasks:
        words = _words(f"{c['title'] or ''} {c['what']}")
        people = records._stems(f"{c['who'] or ''} {c['whom'] or ''}")
        best = None
        for e, title, names in scored:
            common, met = words & title, bool(people & names)
            if len(common) < 2 and not (common and met):
                continue
            score = len(common) + (0.5 if met else 0)
            if not best or score > best[0]:  # при равенстве — более ранняя (события идут по времени)
                best = (score, e, met)
        if not best or (c["id"], best[1]["uid"]) in asked:
            continue
        e = best[1]
        reason = f"Название встречи «{e['title']}» перекликается с задачей" + (", и участники те же" if best[2] else "")
        try:
            proposals.propose_task_meeting(commitment_id=c["id"], event_uid=e["uid"], reason=reason)
            made += 1
        except proposals.ProposalError:
            pass
    if made:
        store.log("task_meetings", f"предложено связать задачи со встречами: {made}")
    return made
