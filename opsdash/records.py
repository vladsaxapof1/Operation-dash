"""Люди, задачи и договорённости (в коде — commitments), «нужен ответ», встречи — то, с чем работают
Claude и директор."""

from __future__ import annotations

import json
import math
from datetime import date, datetime, time, timedelta, timezone

from opsdash import config, store

DIRECTIONS = {"mine": "мои задачи", "to_me": "мне должны", "other": "между коллегами"}

# готовые периоды для фильтров по дате: создана — назад от сегодня, срок — вперёд
CREATED_PERIODS = [("today", "сегодня"), ("week", "за 7 дней"), ("month", "за 30 дней"), ("custom", "свой период…")]
DUE_PERIODS = [("overdue", "просрочен"), ("today", "сегодня"), ("week", "ближайшие 7 дней"),
               ("month", "ближайшие 30 дней"), ("none", "не указан"), ("custom", "свой период…")]


def today_local() -> date:
    return config.now().date()


def _as_date(value: str | None) -> date | None:
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None


def period(key: str, frm: str = "", to: str = "", *, ahead: bool = False) -> tuple[date | None, date | None]:
    """Границы периода из фильтра (включительно): today / week / month — от сегодня назад (дата создания)
    или вперёд (срок, ahead); overdue — до вчера; custom — даты «с» и «по» из формы, любую можно не указывать."""
    today = today_local()
    if key in ("week", "month"):
        span = timedelta(days=6 if key == "week" else 29)
        return (today, today + span) if ahead else (today - span, today)
    if key == "today":
        return today, today
    if key == "overdue":
        return None, today - timedelta(days=1)
    if key == "custom":
        return _as_date(frm), _as_date(to)
    return None, None


def utc_day(d: date) -> str:
    """Начало местных суток в UTC — для сравнения с отметками времени в базе."""
    return store.to_utc_iso(datetime.combine(d, time.min, config.tz()))


# --- люди ------------------------------------------------------------------

def list_people() -> list[dict]:
    out = store.rows("SELECT * FROM people ORDER BY name")
    for p in out:
        p["aliases"] = store.loads(p["aliases"], [])
    return out


def find_person(query: str) -> dict | None:
    q = (query or "").strip().lower().lstrip("@")
    if not q:
        return None
    for p in list_people():
        names = [p["name"], *(p["aliases"] or []), p.get("telegram") or "", p.get("email") or "", p.get("ymessenger") or ""]
        if any(q == (n or "").lower().lstrip("@") for n in names):
            return p
    hits = [p for p in list_people() if q in p["name"].lower() or any(q in a.lower() for a in p["aliases"])]
    return hits[0] if len(hits) == 1 else None


def upsert_person(name: str, *, role=None, aliases=None, telegram=None, ymessenger=None, email=None,
                  kaiten_user_id=None, notes=None, person_id: int | None = None) -> int:
    existing = None
    if person_id:
        existing = store.row("SELECT * FROM people WHERE id=?", (person_id,))
    if not existing:
        existing = store.row("SELECT * FROM people WHERE lower(name)=lower(?)", (name,))
    if existing:
        old_aliases = store.loads(existing["aliases"], [])
        merged = list(dict.fromkeys([*old_aliases, *(aliases or [])]))
        store.execute(
            """UPDATE people SET name=?, role=COALESCE(?, role), aliases=?, telegram=COALESCE(?, telegram),
               ymessenger=COALESCE(?, ymessenger), email=COALESCE(?, email),
               kaiten_user_id=COALESCE(?, kaiten_user_id), notes=COALESCE(?, notes), updated=? WHERE id=?""",
            (name or existing["name"], role, store.json_col(merged), telegram, ymessenger, email, kaiten_user_id,
             notes, store.utcnow(), existing["id"]),
        )
        return existing["id"]
    return store.execute(
        """INSERT INTO people(name, role, aliases, telegram, ymessenger, email, kaiten_user_id, notes, updated)
           VALUES(?,?,?,?,?,?,?,?,?)""",
        (name, role, store.json_col(aliases or []), telegram, ymessenger, email, kaiten_user_id, notes, store.utcnow()),
    )


def delete_person(person_id: int) -> None:
    store.execute("DELETE FROM people WHERE id=?", (person_id,))


# --- обещания ----------------------------------------------------------------

def add_commitment(*, direction: str, what: str, who: str | None = None, whom: str | None = None,
                   due: str | None = None, source_type: str = "manual", source_ref: str | None = None,
                   quote: str | None = None, kaiten_card_id: int | None = None, note: str | None = None) -> int:
    if direction not in DIRECTIONS:
        raise ValueError(f"direction должен быть одним из: {', '.join(DIRECTIONS)}")
    if due:
        date.fromisoformat(due)  # проверка формата YYYY-MM-DD
    # защита от дублей: то же обещание из того же источника
    dup = store.row(
        "SELECT id FROM commitments WHERE lower(what)=lower(?) AND COALESCE(who,'')=COALESCE(?,'') AND status='open'",
        (what, who),
    )
    if dup:
        return dup["id"]
    now = store.utcnow()
    return store.execute(
        """INSERT INTO commitments(direction, who, whom, what, due, status, source_type, source_ref, quote,
           kaiten_card_id, note, created, updated) VALUES(?,?,?,?,?,'open',?,?,?,?,?,?,?)""",
        (direction, who, whom, what, due, source_type, source_ref, quote, kaiten_card_id, note, now, now),
    )


def update_commitment(cid: int, **fields) -> None:
    allowed = {"status", "due", "kaiten_card_id", "note", "what", "who", "whom", "direction"}
    sets, params = [], []
    for k, v in fields.items():
        if k in allowed and v is not None:
            sets.append(f"{k}=?")
            params.append(v)
    if not sets:
        return
    if fields.get("status") in ("done", "cancelled"):
        sets.append("closed_at=?")
        params.append(store.utcnow())
    elif fields.get("status") == "open":
        sets.append("closed_at=NULL")
    sets.append("updated=?")
    params.append(store.utcnow())
    store.execute(f"UPDATE commitments SET {', '.join(sets)} WHERE id=?", [*params, cid])


Period = tuple[date | None, date | None]


def _filters(person: str | None, q: str | None, created: Period | None, due: Period | None,
             no_due: bool) -> tuple[str, list]:
    """Общие условия списка и доски: человек, поиск, дата создания, срок."""
    sql, params = "", []
    if q:
        sql += " AND (what LIKE ? OR who LIKE ? OR whom LIKE ? OR quote LIKE ?)"
        params += [f"%{q}%"] * 4
    if person:
        sql += " AND (who LIKE ? OR whom LIKE ?)"
        params += [f"%{person}%", f"%{person}%"]
    frm, to = created or (None, None)
    if frm:
        sql += " AND created >= ?"
        params.append(utc_day(frm))
    if to:
        sql += " AND created < ?"
        params.append(utc_day(to + timedelta(days=1)))
    frm, to = due or (None, None)
    if frm:
        sql += " AND due >= ?"
        params.append(frm.isoformat())
    if to:
        sql += " AND due <= ?"
        params.append(to.isoformat())
    if no_due:
        sql += " AND due IS NULL"
    return sql, params


def edit_commitment(cid: int, *, what: str, direction: str, who: str | None, whom: str | None, due: str | None) -> None:
    """Изменить задачу с карточки: формулировку, тип, кто и кому, срок (пустой срок — без срока)."""
    if direction not in DIRECTIONS:
        raise ValueError(f"direction должен быть одним из: {', '.join(DIRECTIONS)}")
    if not what.strip():
        raise ValueError("Напишите, что сделать")
    if due and not _as_date(due):
        raise ValueError("Срок — дата в виде ГГГГ-ММ-ДД")
    store.execute("UPDATE commitments SET what=?, direction=?, who=?, whom=?, due=?, updated=? WHERE id=?",
                  (what.strip(), direction, (who or "").strip() or None, (whom or "").strip() or None, due or None,
                   store.utcnow(), cid))


def counterpart(c: dict) -> str | None:
    """С кем эта задача: кому обещала директор, кто обещал ей, а между коллегами — исполнитель."""
    return c.get("whom") if c.get("direction") == "mine" else c.get("who")


def add_links(items: list[dict]) -> list[dict]:
    """Кнопка «открыть в Telegram / почте» у задачи: сообщение, из которого она взялась, а если задача
    со встречи или добавлена вручную — личный чат с человеком из справочника."""
    from opsdash import links

    people = list_people()
    for c in items:
        c["link"] = c["link_label"] = None
        msg = None
        if c.get("source_type") in ("telegram", "mail", "ymessenger") and str(c.get("source_ref") or "").isdigit():
            msg = store.row("SELECT source, chat_id, msg_id, sender_id FROM messages WHERE id=?", (int(c["source_ref"]),))
        found = links.original(msg["source"], msg["chat_id"], msg["msg_id"], msg["sender_id"]) if msg else None
        if not found and (name := counterpart(c)):
            person = _person_by_name(name, people)
            username = ((person or {}).get("telegram") or "").strip().lstrip("@")
            if username:
                found = (f"https://t.me/{username}", "Telegram")
        if found:
            c["link"], c["link_title"] = found
            c["link_label"] = "Telegram" if "t.me/" in found[0] or "telegram.org" in found[0] else "Почта"
    return items


def _person_by_name(name: str, people: list[dict]) -> dict | None:
    q = name.strip().lower()
    for p in people:
        if q in (p["name"].lower(), *(a.lower() for a in p["aliases"] or [])):
            return p
    hits = [p for p in people if q and (q in p["name"].lower() or p["name"].lower() in q)]
    return hits[0] if len(hits) == 1 else None


def list_commitments(*, status: str | None = "open", direction: str | None = None, person: str | None = None,
                     overdue: bool = False, q: str | None = None, limit: int = 500, created: Period | None = None,
                     due: Period | None = None, no_due: bool = False) -> list[dict]:
    where, params = _filters(person, q, created, due, no_due)
    sql = "SELECT * FROM commitments WHERE 1=1" + where
    if status and status != "all":
        sql += " AND status=?"
        params.append(status)
    if direction:
        sql += " AND direction=?"
        params.append(direction)
    if overdue:
        sql += " AND due IS NOT NULL AND due < ?"
        params.append(today_local().isoformat())
    sql += " ORDER BY CASE WHEN due IS NULL THEN 1 ELSE 0 END, due, created DESC LIMIT ?"
    params.append(limit)
    return _mark(store.rows(sql, params))


def _mark(items: list[dict]) -> list[dict]:
    """Признаки для отображения: просрочено, сегодня, завтра; сколько дней до срока (в день срока — часов
    до конца дня)."""
    now = config.now()
    today = now.date()
    for c in items:
        c["overdue"] = bool(c["due"] and c["due"] < today.isoformat() and c["status"] == "open")
        c["due_today"] = c["due"] == today.isoformat()
        c["due_tomorrow"] = c["due"] == (today + timedelta(days=1)).isoformat()
        due = _as_date(c["due"])
        c["due_days"] = (due - today).days if due else None
        end_of_day = datetime.combine(today + timedelta(days=1), time.min, now.tzinfo)
        c["due_hours"] = max(1, math.ceil((end_of_day - now).total_seconds() / 3600)) if c["due_days"] == 0 else None
    return items


def commitments_board(*, person: str | None = None, q: str | None = None, done_days: int = 14,
                      created: Period | None = None, due: Period | None = None,
                      no_due: bool = False) -> dict[str, list[dict]]:
    """Доска задач и договорённостей: открытые — по направлению, «Готово» — закрытые за done_days дней."""
    board = {d: list_commitments(status="open", direction=d, person=person, q=q, created=created, due=due,
                                 no_due=no_due) for d in DIRECTIONS}
    since = store.to_utc_iso(datetime.now(timezone.utc) - timedelta(days=done_days))
    where, params = _filters(person, q, created, due, no_due)
    sql = "SELECT * FROM commitments WHERE status='done' AND COALESCE(closed_at, updated) >= ?" + where
    board["done"] = _mark(store.rows(sql + " ORDER BY COALESCE(closed_at, updated) DESC LIMIT 300", [since, *params]))
    return board


# --- нужен ответ ------------------------------------------------------------

def add_attention(*, summary: str, message_id: int | None = None, why: str | None = None, draft: str | None = None,
                  priority: str = "normal") -> int:
    msg = store.row("SELECT * FROM messages WHERE id=?", (message_id,)) if message_id else None
    if message_id:
        dup = store.row("SELECT id FROM attention WHERE message_id=? AND status='open'", (message_id,))
        if dup:
            store.execute(
                "UPDATE attention SET summary=?, why=COALESCE(?, why), draft=COALESCE(?, draft), priority=?, updated=? WHERE id=?",
                (summary, why, draft, priority, store.utcnow(), dup["id"]),
            )
            return dup["id"]
    now = store.utcnow()
    return store.execute(
        """INSERT INTO attention(message_id, source, chat_title, sender_name, summary, why, draft, priority, status,
           created, updated) VALUES(?,?,?,?,?,?,?,?,'open',?,?)""",
        (message_id, (msg or {}).get("source"), (msg or {}).get("chat_title"), (msg or {}).get("sender_name"),
         summary, why, draft, priority, now, now),
    )


def close_attention(aid: int, status: str = "done") -> None:
    store.execute("UPDATE attention SET status=?, updated=? WHERE id=?", (status, store.utcnow(), aid))


def list_attention(status: str = "open") -> list[dict]:
    items = store.rows(
        """SELECT a.*, m.ts AS msg_ts, m.text AS msg_text, m.link AS msg_link, m.chat_id AS chat_id,
                  m.source AS msg_source, m.msg_id AS msg_ext_id, m.sender_id AS msg_sender
           FROM attention a LEFT JOIN messages m ON m.id = a.message_id
           WHERE (? = 'all' OR a.status = ?)
           ORDER BY CASE a.priority WHEN 'high' THEN 0 WHEN 'normal' THEN 1 ELSE 2 END, a.created DESC""",
        (status, status),
    )
    from opsdash import links

    for a in items:  # кнопка «открыть в оригинале»: сообщение, чат или письмо
        a["orig_url"], a["orig_label"] = links.original(
            a["msg_source"] or a["source"], a["chat_id"], a["msg_ext_id"], a["msg_sender"]) or (None, None)
    return items


def autoclose_answered() -> int:
    """Закрыть «нужен ответ», если директор ответила: в личном чате — любым сообщением
    после вопроса, в группе — только ответом (reply) именно на это сообщение.
    Остальное закрывает Claude при разборе или сама директор."""
    closed = 0
    for a in store.rows(
        """SELECT a.id, m.source, m.chat_id, m.ts, m.msg_id, c.kind FROM attention a
           JOIN messages m ON m.id=a.message_id
           LEFT JOIN chats c ON c.source=m.source AND c.chat_id=m.chat_id
           WHERE a.status='open'"""
    ):
        if a["kind"] == "private":
            answered = store.scalar(
                "SELECT 1 FROM messages WHERE source=? AND chat_id=? AND is_outgoing=1 AND ts > ? LIMIT 1",
                (a["source"], a["chat_id"], a["ts"]),
            )
        elif a["source"] == "mail":
            # ответ на письмо лежит в «Отправленных» с In-Reply-To = Message-ID исходного
            answered = store.scalar(
                "SELECT 1 FROM messages WHERE source='mail' AND is_outgoing=1 AND reply_to=? LIMIT 1", (a["msg_id"],))
        else:
            answered = store.scalar(
                "SELECT 1 FROM messages WHERE source=? AND chat_id=? AND is_outgoing=1 AND reply_to=? LIMIT 1",
                (a["source"], a["chat_id"], a["msg_id"]),
            )
        if answered:
            close_attention(a["id"], "done")
            closed += 1
    return closed


# --- встречи ----------------------------------------------------------------

def meeting_dir(meeting_id: str):
    return config.MEETINGS_DIR / meeting_id


def list_meetings(limit: int = 100, days: int | None = None) -> list[dict]:
    sql = "SELECT * FROM meetings"
    params: list = []
    if days:
        sql += " WHERE started_at >= ?"
        params.append(store.to_utc_iso(datetime.now().astimezone() - timedelta(days=days)))
    sql += " ORDER BY started_at DESC LIMIT ?"
    params.append(limit)
    out = store.rows(sql, params)
    for m in out:
        m["participants"] = store.loads(m["participants"], [])
    return out


def get_meeting(meeting_id: str) -> dict | None:
    m = store.row("SELECT * FROM meetings WHERE id=?", (meeting_id,))
    if m:
        m["participants"] = store.loads(m["participants"], [])
    return m


def update_meeting(meeting_id: str, **fields) -> None:
    allowed = {"title", "status", "progress", "event_uid", "participants", "headline", "error", "duration_sec",
               "started_at", "source_file"}
    sets, params = [], []
    for k, v in fields.items():
        if k in allowed and v is not None:
            if k == "participants" and not isinstance(v, str):
                v = json.dumps(v, ensure_ascii=False)
            sets.append(f"{k}=?")
            params.append(v)
    if not sets:
        return
    sets.append("updated=?")
    params.append(store.utcnow())
    store.execute(f"UPDATE meetings SET {', '.join(sets)} WHERE id=?", [*params, meeting_id])


def read_meeting_file(meeting_id: str, name: str) -> str:
    path = meeting_dir(meeting_id) / name
    return path.read_text(encoding="utf-8") if path.exists() else ""
