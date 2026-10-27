"""Люди, задачи и договорённости (в коде — commitments), «нужен ответ», встречи — то, с чем работают
Claude и директор."""

from __future__ import annotations

import json
import math
import re
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
                   quote: str | None = None, kaiten_card_id: int | None = None, note: str | None = None,
                   title: str | None = None, parent_id: int | None = None) -> int:
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
        if parent_id:
            set_parent(dup["id"], parent_id)
        return dup["id"]
    if parent_id:
        _check_parent(None, parent_id)
    now = store.utcnow()
    # задачи, которые добавил Claude, — «новые», пока директор их не увидит; свои она уже видела
    seen = now if source_type == "manual" else None
    return store.execute(
        """INSERT INTO commitments(direction, who, whom, what, title, due, status, source_type, source_ref, quote,
           kaiten_card_id, note, parent_id, seen_at, created, updated) VALUES(?,?,?,?,?,?,'open',?,?,?,?,?,?,?,?,?)""",
        (direction, who, whom, what, (title or "").strip() or None, due, source_type, source_ref, quote, kaiten_card_id,
         note, parent_id or None, seen, now, now),
    )


def _check_parent(cid: int | None, parent_id: int) -> None:
    """Задача-родитель есть, и связь не замыкается в петлю (задача — часть своей же подзадачи)."""
    pid, chain = parent_id, set()
    while pid and pid not in chain:
        if pid == cid:
            raise ValueError("Задача не может быть частью самой себя или своей подзадачи")
        chain.add(pid)
        r = store.row("SELECT parent_id FROM commitments WHERE id=?", (pid,))
        if not r:
            if pid == parent_id:
                raise ValueError(f"Нет задачи [{parent_id}]")
            break
        pid = r["parent_id"]


def set_parent(cid: int, parent_id: int | None) -> None:
    """Сделать задачу частью другой: директор обещала Тимуру отчёт и передала выгрузку Петру — задача Петра
    становится подзадачей. Пустой parent_id — отвязать."""
    if parent_id:
        _check_parent(cid, parent_id)
    store.execute("UPDATE commitments SET parent_id=?, updated=? WHERE id=?", (parent_id or None, store.utcnow(), cid))


def parse_task_ref(value: str | None) -> int | None:
    """Номер задачи из поля «Часть задачи»: «#34 · Недельный отчёт · Я → Тимур» → 34."""
    m = re.match(r"\s*#?(\d+)", value or "")
    return int(m.group(1)) if m else None


def mark_seen(cid: int) -> None:
    store.execute("UPDATE commitments SET seen_at=? WHERE id=? AND seen_at IS NULL", (store.utcnow(), cid))


def unseen_count() -> int:
    """Сколько открытых задач директор ещё не видела (добавил Claude) — значок у «Задач» в меню."""
    return store.scalar("SELECT COUNT(*) FROM commitments WHERE status='open' AND seen_at IS NULL") or 0


def _settle_done_proposals(cid: int, status: str) -> None:
    """Задачу закрыли сами — предложение Claude «закрыть задачу» больше не ждёт ответа."""
    store.execute(
        "UPDATE proposals SET status=?, decided_at=?, result=? WHERE kind='task.done' AND status='pending' "
        "AND json_extract(payload, '$.commitment_id')=?",
        ("done" if status == "done" else "rejected", store.utcnow(),
         store.json_col({"note": "задачу закрыли вручную"}), cid),
    )


def update_commitment(cid: int, **fields) -> None:
    """Изменить поля задачи. parent_id: номер задачи-родителя, 0 — отвязать."""
    if (parent_id := fields.pop("parent_id", None)) is not None:
        set_parent(cid, int(parent_id) or None)
    allowed = {"status", "due", "kaiten_card_id", "note", "what", "title", "who", "whom", "direction"}
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
    if fields.get("status") in ("done", "cancelled"):
        _settle_done_proposals(cid, fields["status"])


Period = tuple[date | None, date | None]


def _filters(person: str | None, q: str | None, created: Period | None, due: Period | None,
             no_due: bool, unseen: bool = False) -> tuple[str, list]:
    """Общие условия списка и доски: человек, поиск, дата создания, срок, только новые (директор их не видела)."""
    sql, params = "", []
    if unseen:
        sql += " AND status='open' AND seen_at IS NULL"
    if q:
        sql += " AND (what LIKE ? OR title LIKE ? OR who LIKE ? OR whom LIKE ? OR quote LIKE ?)"
        params += [f"%{q}%"] * 5
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


def edit_commitment(cid: int, *, title: str, what: str, direction: str, who: str | None, whom: str | None,
                    due: str | None, parent_id: int | None = None) -> None:
    """Изменить задачу из её карточки: заголовок, суть, тип, кто и кому, срок (пустой срок — без срока), часть
    какой задачи (пусто — самостоятельная). Без сути заголовок становится и сутью."""
    if direction not in DIRECTIONS:
        raise ValueError(f"direction должен быть одним из: {', '.join(DIRECTIONS)}")
    title, what = title.strip(), what.strip() or title.strip()
    if not what:
        raise ValueError("Напишите, что сделать")
    if due and not _as_date(due):
        raise ValueError("Срок — дата в виде ГГГГ-ММ-ДД")
    if parent_id:
        _check_parent(cid, parent_id)
    now = store.utcnow()
    store.execute("UPDATE commitments SET title=?, what=?, direction=?, who=?, whom=?, due=?, parent_id=?, "
                  "seen_at=COALESCE(seen_at, ?), updated=? WHERE id=?",
                  (title or None, what, direction, (who or "").strip() or None, (whom or "").strip() or None,
                   due or None, parent_id or None, now, now, cid))


def headline(c: dict) -> str:
    """Заголовок задачи для карточек и Ганта. Если Claude или директор не дали короткий заголовок — сокращаем суть:
    до первой скобки, двоеточия, тире или запятой, а длинное без них — по словам."""
    if (c.get("title") or "").strip():
        return c["title"].strip()
    what = " ".join((c.get("what") or "").split())
    if len(what) <= HEADLINE_MAX:
        return what
    cuts = [i for sep in (" (", ": ", " — ", " – ", "; ", ", ") if 15 <= (i := what.find(sep)) <= HEADLINE_MAX]
    if cuts:
        return what[:min(cuts)]
    return what[:HEADLINE_MAX - 3].rsplit(" ", 1)[0].rstrip(",.;:—–- ") + "…"


HEADLINE_MAX = 60


def counterpart(c: dict) -> str | None:
    """С кем эта задача: кому обещала директор, кто обещал ей, а между коллегами — исполнитель."""
    return c.get("whom") if c.get("direction") == "mine" else c.get("who")


def doer(c: dict) -> str:
    """Кто делает: «Я» (директор) или имя."""
    return "Я" if c.get("direction") == "mine" else (c.get("who") or "?")


def route(c: dict) -> str:
    """Кто кому: «Я → Тимур», «Пётр → мне», «Олег → Ирина»."""
    whom = "мне" if c.get("direction") == "to_me" else (c.get("whom") or "—")
    return f"{doer(c)} → {whom}"


def task_options() -> list[dict]:
    """Открытые задачи — подсказки для поля «Часть задачи» (при добавлении и в окне правки)."""
    out = store.rows("SELECT id, direction, who, whom, title, what FROM commitments WHERE status='open' "
                     "ORDER BY created DESC, id DESC")
    for c in out:
        c["label"] = f"#{c['id']} · {headline(c)} · {route(c)}"
    return out


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


SORTS = {"due": "по сроку", "created": "сначала новые"}


def list_commitments(*, status: str | None = "open", direction: str | None = None, person: str | None = None,
                     overdue: bool = False, q: str | None = None, limit: int = 500, created: Period | None = None,
                     due: Period | None = None, no_due: bool = False, unseen: bool = False,
                     sort: str = "due") -> list[dict]:
    """sort: due — ближайший срок сверху, без срока в конце; created — последние добавленные сверху."""
    where, params = _filters(person, q, created, due, no_due, unseen)
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
    if sort == "created":
        sql += " ORDER BY created DESC, id DESC LIMIT ?"
    else:
        sql += " ORDER BY CASE WHEN due IS NULL THEN 1 ELSE 0 END, due, created DESC LIMIT ?"
    params.append(limit)
    return _mark(store.rows(sql, params))


def get_commitment(cid: int) -> dict | None:
    found = _mark(store.rows("SELECT * FROM commitments WHERE id=?", (cid,)))
    return found[0] if found else None


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
    _relations(items)
    return items


def _relations(items: list[dict]) -> None:
    """Связи для карточек: часть какой задачи (parent), подзадачи (children), кто ещё в деле (helpers —
    «Я + Пётр → Тимур»), срок подзадачи позже срока задачи (late_vs_parent); предложение Claude закрыть
    задачу, если по переписке или встрече она выглядит выполненной (done_hint)."""
    if not items:
        return
    ids = [c["id"] for c in items]
    parents = list({c["parent_id"] for c in items if c.get("parent_id")})
    related = store.rows(
        "SELECT id, direction, who, whom, title, what, due, status, parent_id FROM commitments WHERE status != 'cancelled' "
        f"AND (parent_id IN ({','.join('?' * len(ids))}) OR id IN ({','.join('?' * len(parents)) or 'NULL'}))",
        [*ids, *parents])
    by_id = {r["id"]: r for r in related}
    kids: dict[int, list[dict]] = {}
    for r in related:
        r["headline"], r["route"] = headline(r), route(r)
        if r["parent_id"]:
            kids.setdefault(r["parent_id"], []).append(r)
    hints = {}
    for p in store.rows("SELECT * FROM proposals WHERE kind='task.done' AND status='pending' ORDER BY id"):
        p["payload"] = store.loads(p["payload"], {})
        hints[p["payload"].get("commitment_id")] = p
    for c in items:
        c["parent"] = by_id.get(c.get("parent_id"))
        c["children"] = sorted(kids.get(c["id"], []), key=lambda r: (r["status"] != "open", r["due"] or "9999"))
        mine = doer(c)
        c["helpers"] = list(dict.fromkeys(doer(k) for k in c["children"] if k["status"] == "open" and doer(k) != mine))
        c["kids_done"] = bool(c["children"]) and all(k["status"] == "done" for k in c["children"])
        c["late_vs_parent"] = bool(c["parent"] and c["status"] == "open" and c["due"] and c["parent"]["due"]
                                   and c["due"] > c["parent"]["due"])
        c["done_hint"] = hints.get(c["id"]) if c["status"] == "open" else None


def commitments_board(*, person: str | None = None, q: str | None = None, done_days: int = 14,
                      created: Period | None = None, due: Period | None = None,
                      no_due: bool = False, unseen: bool = False, sort: str = "due") -> dict[str, list[dict]]:
    """Доска задач и договорённостей: открытые — по направлению, «Готово» — закрытые за done_days дней
    (последние закрытые сверху при любой сортировке)."""
    board = {d: list_commitments(status="open", direction=d, person=person, q=q, created=created, due=due,
                                 no_due=no_due, unseen=unseen, sort=sort) for d in DIRECTIONS}
    since = store.to_utc_iso(datetime.now(timezone.utc) - timedelta(days=done_days))
    where, params = _filters(person, q, created, due, no_due, unseen)
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


def meeting_commitments(meeting_id: str) -> list[dict]:
    """Задачи встречи для её страницы: появились на ней (все, и закрытые) и закрытые по её итогам —
    у таких в заметке id встречи («встреча <id>: …»), они идут после."""
    return _mark(store.rows(
        "SELECT * FROM commitments WHERE (source_type='meeting' AND source_ref=?) OR note LIKE ? "
        "ORDER BY CASE WHEN source_ref=? THEN 0 ELSE 1 END, id", (meeting_id, f"%{meeting_id}%", meeting_id)))


# --- ссылки из разбора встречи на задачи PULSE ----------------------------------------------

_STOP = {"для", "что", "это", "как", "его", "она", "они", "при", "все", "или", "так", "уже", "про", "без", "над",
         "под", "чтобы", "после", "потом", "сначала", "через", "нужно", "надо", "будет", "есть", "если", "обещал",
         "обещала", "передать", "сделать"}


def _stems(text: str | None) -> set[str]:
    """Основы слов для сравнения формулировок: без регистра, «ё», коротких и служебных слов; окончания отрезаем
    грубо — первые 5 букв («прогнозы» и «прогноза» совпадут)."""
    words = re.findall(r"[a-zа-я0-9]+", (text or "").lower().replace("ё", "е"))
    return {w[:5] for w in words if len(w) >= 3 and w not in _STOP}


def _match(lines: list[tuple[int, set[str], set[str]]], tasks: list[dict]) -> dict[int, dict]:
    """Строка разбора → задача: по общим словам с заголовком и сутью задачи, плюс совпадение исполнителя.
    Каждая задача — не больше одной строки, лучшие пары первыми."""
    pairs = []
    for n, words, people in lines:
        for c in tasks:
            common = len(words & c["_stems"])
            if common < 2 or not words:
                continue
            score = common / min(len(words), len(c["_stems"]))
            if people and _stems(c.get("who")) & people:
                score += 0.2
            if score >= 0.45:
                pairs.append((score, n, c["id"]))
    out, used = {}, set()
    by_id = {c["id"]: c for c in tasks}
    for _, n, cid in sorted(pairs, reverse=True):
        if n not in out and cid not in used:
            out[n] = by_id[cid]
            used.add(cid)
    return out


def _task_ref(c: dict) -> str:
    label = {"done": "✓ выполнена", "cancelled": "отменена"}.get(c["status"], "задача →")
    return f"[{label}](/commitments?task={c['id']})"


def link_summary(text: str, tasks: list[dict]) -> str:
    """Ссылки из разбора встречи на задачи PULSE: в таблице «Задачи» — у строки задачи (а «предложено» в колонке
    Kaiten — номер созданной карточки), в «Договорённостях» — в конце пункта. Строку и задачу сопоставляем по
    смыслу, поэтому работает и для старых разборов, где ссылок не было."""
    if not text or not tasks:
        return text
    for c in tasks:
        c["_stems"] = _stems(f"{c.get('title') or ''} {c.get('what') or ''}")
    cards = {r["id"]: r["url"] for r in store.rows(
        f"SELECT id, url FROM kaiten_cards WHERE id IN ({','.join('?' * len(tasks))})",
        [c.get("kaiten_card_id") or 0 for c in tasks])}
    lines = text.split("\n")
    section, rows, bullets = "", [], []
    for n, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("#"):
            section = stripped.lstrip("#").strip().lower()
        elif "задач" in section and stripped.startswith("|"):
            cells = _cells(stripped)
            is_head = n == 0 or not lines[n - 1].strip().startswith("|")
            if not is_head and not all(set(x) <= set("-: ") for x in cells):
                cols = _table_header(lines, n)
                who = cells[cols.index("кто")] if "кто" in cols and cols.index("кто") < len(cells) else ""
                rows.append((n, _stems(cells[0]), _stems(who)))
        elif ("задач" in section or "договор" in section) and re.match(r"([-*]|\d+\.)\s", stripped):
            bullets.append((n, _stems(stripped), _stems(stripped)))
    for n, c in _match(rows, tasks).items():
        cells, cols = _cells(lines[n].strip()), _table_header(lines, n)
        cells[0] = f"{cells[0]} {_task_ref(c)}"
        if "kaiten" in cols and c.get("kaiten_card_id"):
            k = cols.index("kaiten")
            if k < len(cells) and (cells[k] in ("", "—", "-") or cells[k].lower().startswith("предлож")):
                card = c["kaiten_card_id"]
                cells[k] = f"[#{card}]({cards[card]})" if cards.get(card) else f"#{card}"
        lines[n] = "| " + " | ".join(cells) + " |"
    for n, c in _match(bullets, tasks).items():
        lines[n] = f"{lines[n].rstrip()} {_task_ref(c)}"
    return "\n".join(lines)


def _cells(row: str) -> list[str]:
    return [x.strip() for x in row.strip("|").split("|")]


def _table_header(lines: list[str], n: int) -> list[str]:
    """Названия колонок таблицы, в которой стоит строка n (первая строка таблицы)."""
    i = n
    while i > 0 and lines[i - 1].strip().startswith("|"):
        i -= 1
    return [x.lower() for x in _cells(lines[i].strip())]
