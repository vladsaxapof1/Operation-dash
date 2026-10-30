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

class DuplicateError(ValueError):
    """Такая задача уже есть: existing — её id."""

    def __init__(self, message: str, existing: int):
        super().__init__(message)
        self.existing = existing


def add_commitment(*, direction: str, what: str, who: str | None = None, whom: str | None = None,
                   due: str | None = None, source_type: str = "manual", source_ref: str | None = None,
                   quote: str | None = None, kaiten_card_id: int | None = None, note: str | None = None,
                   title: str | None = None, parent_id: int | None = None, force: bool = False,
                   project_id: int | None = None) -> int:
    """force — добавить, даже если у той же карточки Kaiten уже есть открытая задача (обычно это дубль).
    project_id — проект; у подзадачи без своего проекта — проект основной задачи."""
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
        if project_id and not store.scalar("SELECT project_id FROM commitments WHERE id=?", (dup["id"],)):
            set_project(dup["id"], project_id)
        return dup["id"]
    if kaiten_card_id and not force:
        same = store.row("SELECT * FROM commitments WHERE kaiten_card_id=? AND status='open' AND archived_at IS NULL",
                         (kaiten_card_id,))
        if same:
            raise DuplicateError(f"У карточки Kaiten #{kaiten_card_id} уже есть задача [{same['id']}] «{headline(same)}» "
                                 f"({route(same)}). Допиши в неё: ./ops commitments set {same['id']} --what/--due/--note; "
                                 "если это всё же другая задача — повтори с --force", same["id"])
    if parent_id:
        _check_parent(None, parent_id)
        project_id = project_id or store.scalar("SELECT project_id FROM commitments WHERE id=?", (parent_id,))
    if project_id:
        _check_project(project_id)
    now = store.utcnow()
    # задачи, которые добавил Claude, — «новые», пока директор их не увидит; свои она уже видела
    seen = now if source_type == "manual" else None
    return store.execute(
        """INSERT INTO commitments(direction, who, whom, what, title, due, status, source_type, source_ref, quote,
           kaiten_card_id, note, parent_id, project_id, seen_at, created, updated)
           VALUES(?,?,?,?,?,?,'open',?,?,?,?,?,?,?,?,?,?)""",
        (direction, who, whom, what, (title or "").strip() or None, due, source_type, source_ref, quote, kaiten_card_id,
         note, parent_id or None, project_id or None, seen, now, now),
    )


def _check_parent(cid: int | None, parent_id: int) -> None:
    """Задача-родитель есть, и связь не замыкается в петлю (задача — часть своей же подзадачи или ждёт свою
    основную задачу: основная ждёт подзадачи, получился бы круг)."""
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
    if cid and _reaches(_waits_graph(), cid, parent_id):
        raise ValueError("Задача ждёт ту, частью которой её делают, — получится замкнутый круг")


def set_parent(cid: int, parent_id: int | None) -> None:
    """Сделать задачу частью другой: директор обещала Тимуру отчёт и передала выгрузку Петру — задача Петра
    становится подзадачей. Пустой parent_id — отвязать."""
    if parent_id:
        _check_parent(cid, parent_id)
    store.execute("UPDATE commitments SET parent_id=?, updated=? WHERE id=?", (parent_id or None, store.utcnow(), cid))
    project = parent_id and store.scalar("SELECT project_id FROM commitments WHERE id=?", (parent_id,))
    if project and not store.scalar("SELECT project_id FROM commitments WHERE id=?", (cid,)):
        set_project(cid, project)  # подзадача — в проекте основной задачи


def _check_project(project_id: int) -> None:
    if not store.row("SELECT 1 FROM projects WHERE id=?", (project_id,)):
        raise ValueError(f"Нет проекта [{project_id}] (./ops projects list)")


def set_project(cid: int, project_id: int | None) -> None:
    """Задача — в проекте (пусто — ни в каком). Её подзадачи идут следом, если они были там же, где она,
    или без проекта."""
    if project_id:
        _check_project(project_id)
    old = store.scalar("SELECT project_id FROM commitments WHERE id=?", (cid,))
    now, todo, seen = store.utcnow(), [cid], set()
    while todo:
        tid = todo.pop()
        if tid in seen:
            continue
        seen.add(tid)
        store.execute("UPDATE commitments SET project_id=?, updated=? WHERE id=?", (project_id or None, now, tid))
        todo += [r["id"] for r in store.rows(
            "SELECT id FROM commitments WHERE parent_id=? AND (project_id IS NULL OR project_id IS ?)", (tid, old))]


def parse_task_ref(value: str | None) -> int | None:
    """Номер задачи из поля «Часть задачи»: «#34 · Недельный отчёт · Я → Тимур» → 34."""
    m = re.match(r"\s*#?(\d+)", value or "")
    return int(m.group(1)) if m else None


def mark_seen(cid: int) -> None:
    store.execute("UPDATE commitments SET seen_at=? WHERE id=? AND seen_at IS NULL", (store.utcnow(), cid))


def unseen_count() -> int:
    """Сколько открытых задач директор ещё не видела (добавил Claude) — значок у «Задач» в меню."""
    return store.scalar(
        "SELECT COUNT(*) FROM commitments WHERE status='open' AND seen_at IS NULL AND archived_at IS NULL") or 0


def set_in_work(cid: int, on: bool) -> None:
    """«В работе»: директор взялась за задачу — карточка сверху своей колонки и зелёная. Снять — on=False."""
    now = store.utcnow()
    store.execute("UPDATE commitments SET in_work_at=?, seen_at=COALESCE(seen_at, ?), updated=? WHERE id=?",
                  (now if on else None, now, now, cid))


def set_archived(cid: int, on: bool) -> None:
    """Убрать задачу в архив (или вернуть): статус не меняется, задача только пропадает с доски, из списков,
    напоминаний и дайджестов. Архив — в списке задач, фильтр «Архив»."""
    now = store.utcnow()
    store.execute("UPDATE commitments SET archived_at=?, seen_at=COALESCE(seen_at, ?), updated=? WHERE id=?",
                  (now if on else None, now, now, cid))
    if on:  # задача больше не в работе; предложения Claude по ней («закрыть», «напомнить») не ждут ответа
        store.execute("UPDATE commitments SET in_work_at=NULL WHERE id=?", (cid,))
        store.execute(
            "UPDATE proposals SET status='rejected', decided_at=?, result=? WHERE kind IN ('task.done', 'telegram.remind') "
            "AND status='pending' AND json_extract(payload, '$.commitment_id')=?",
            (now, store.json_col({"note": "задачу убрали в архив"}), cid))


def _settle_done_proposals(cid: int, status: str) -> None:
    """Задачу закрыли сами — предложение Claude «закрыть задачу» больше не ждёт ответа."""
    store.execute(
        "UPDATE proposals SET status=?, decided_at=?, result=? WHERE kind='task.done' AND status='pending' "
        "AND json_extract(payload, '$.commitment_id')=?",
        ("done" if status == "done" else "rejected", store.utcnow(),
         store.json_col({"note": "задачу закрыли вручную"}), cid),
    )


def update_commitment(cid: int, **fields) -> None:
    """Изменить поля задачи. parent_id: номер задачи-родителя, 0 — отвязать; project_id: проект, 0 — убрать из проекта."""
    if (parent_id := fields.pop("parent_id", None)) is not None:
        set_parent(cid, int(parent_id) or None)
    if (project_id := fields.pop("project_id", None)) is not None:
        set_project(cid, int(project_id) or None)
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
        sets.append("duplicate_of=NULL")  # вернули дубль — снова самостоятельная задача
    sets.append("updated=?")
    params.append(store.utcnow())
    was = store.scalar("SELECT status FROM commitments WHERE id=?", (cid,))
    store.execute(f"UPDATE commitments SET {', '.join(sets)} WHERE id=?", [*params, cid])
    if fields.get("status") in ("done", "cancelled"):
        _settle_done_proposals(cid, fields["status"])
        if was == "open":
            _notify_unblocked(cid)


# --- «ждёт» и дубли ---------------------------------------------------------------------
# «Ждёт» — задачу нельзя закончить, пока не сделана другая («Схемы в карточку проекта» ждёт «Описание процесса
# подрядчику») или не случилось событие («встреча Олега с собственником 02.11»). Подзадача — другое: часть той же работы,
# переданная дальше; основная задача ждёт свои подзадачи без отдельной связи.

def _waits_graph() -> dict[int, set[int]]:
    """Кто кого ждёт: связи «ждёт» и подзадачи (основная задача ждёт свои части)."""
    graph: dict[int, set[int]] = {}
    for r in store.rows("SELECT task_id, waits_for FROM task_links WHERE waits_for IS NOT NULL"):
        graph.setdefault(r["task_id"], set()).add(r["waits_for"])
    for r in store.rows("SELECT id, parent_id FROM commitments WHERE parent_id IS NOT NULL"):
        graph.setdefault(r["parent_id"], set()).add(r["id"])
    return graph


def _reaches(graph: dict[int, set[int]], start: int, target: int) -> bool:
    seen, todo = set(), [start]
    while todo:
        node = todo.pop()
        if node == target:
            return True
        if node not in seen:
            seen.add(node)
            todo.extend(graph.get(node, ()))
    return False


def check_wait(task_id: int, waits_for: int | None = None, event: str | None = None, until: str | None = None) -> None:
    """Можно ли связать: задачи есть, задача не ждёт сама себя и не ждёт то, что (напрямую или через другие
    задачи и подзадачи) ждёт её. Для события — есть текст, дата в виде ГГГГ-ММ-ДД."""
    if not store.row("SELECT 1 FROM commitments WHERE id=?", (task_id,)):
        raise ValueError(f"Нет задачи [{task_id}]")
    if until and not _as_date(until):
        raise ValueError("Дата события — в виде ГГГГ-ММ-ДД")
    if waits_for:
        if int(waits_for) == int(task_id):
            raise ValueError("Задача не может ждать сама себя")
        if not store.row("SELECT 1 FROM commitments WHERE id=?", (waits_for,)):
            raise ValueError(f"Нет задачи [{waits_for}]")
        if _reaches(_waits_graph(), int(waits_for), int(task_id)):
            raise ValueError("Получится замкнутый круг: та задача уже ждёт эту — напрямую, через другие или как подзадача")
    elif not (event or "").strip():
        raise ValueError("Укажите задачу, которую ждёт эта, или событие")


def add_wait(task_id: int, *, waits_for: int | None = None, event: str | None = None, until: str | None = None,
             note: str | None = None, source_ref: str | None = None, by: str = "director") -> int:
    """Задача ждёт другую задачу (waits_for) или событие (event, until — когда его ждём). Такая связь уже есть —
    обновляем её. by: director — поставила директор, claude — Claude по встрече или переписке, kaiten — блокировка
    из Kaiten (sync_kaiten_waits)."""
    check_wait(task_id, waits_for, event, until)
    event = " ".join((event or "").split()) or None
    if waits_for:
        old = store.row("SELECT id FROM task_links WHERE task_id=? AND waits_for=?", (task_id, waits_for))
    else:
        old = store.row("SELECT id FROM task_links WHERE task_id=? AND waits_for IS NULL AND lower(event)=lower(?)",
                        (task_id, event))
    if old:
        store.execute("UPDATE task_links SET until=COALESCE(?, until), note=COALESCE(?, note), done_at=NULL WHERE id=?",
                      (until or None, note or None, old["id"]))
        return old["id"]
    return store.execute(
        "INSERT INTO task_links(task_id, waits_for, event, until, note, source_ref, added_by, created) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (task_id, waits_for or None, None if waits_for else event, None if waits_for else (until or None),
         (note or "").strip() or None, source_ref, by if by in ("claude", "kaiten") else "director", store.utcnow()))


def find_waits(task_id: int, *, waits_for: int | None = None, event: str | None = None) -> list[dict]:
    """Связи «ждёт» у задачи: с этой задачей или с событием, где есть этот текст (для ./ops)."""
    if waits_for:
        return store.rows("SELECT * FROM task_links WHERE task_id=? AND waits_for=?", (task_id, waits_for))
    q = " ".join((event or "").split()).lower()
    return [r for r in store.rows("SELECT * FROM task_links WHERE task_id=? AND waits_for IS NULL", (task_id,))
            if q and q in (r["event"] or "").lower()]


def remove_wait(link_id: int) -> None:
    store.execute("DELETE FROM task_links WHERE id=?", (link_id,))


def set_event_done(link_id: int, done: bool = True) -> None:
    """Событие, которого ждала задача, случилось (или нет — вернуть ожидание)."""
    link = store.row("SELECT * FROM task_links WHERE id=? AND waits_for IS NULL", (link_id,))
    if not link:
        raise ValueError("Нет такого события у задачи")
    store.execute("UPDATE task_links SET done_at=? WHERE id=?", (store.utcnow() if done else None, link_id))
    if done and not link["done_at"]:
        _notify_unblocked(None, task_id=link["task_id"], event=link["event"])


KAITEN_REF = "kaiten:blocker:"  # source_ref связи, которую поставила блокировка из Kaiten


def sync_kaiten_waits(card_ids: list[int]) -> None:
    """Блокировки из Kaiten (зеркало kaiten_cards.blockers) → «ждёт» у открытых задач с этими карточками: карточку
    блокирует карточка другой открытой задачи — задача ждёт ту задачу; другая карточка или причина текстом —
    событие. Сняли блокировку в Kaiten — связь, которую поставил Kaiten, уходит, событие «случилось»."""
    if not card_ids:
        return
    marks = ",".join("?" * len(card_ids))
    tasks = store.rows(f"SELECT id, kaiten_card_id FROM commitments WHERE status='open' AND archived_at IS NULL "
                       f"AND kaiten_card_id IN ({marks})", card_ids)
    by_card = {r["kaiten_card_id"]: r["id"] for r in store.rows(
        "SELECT id, kaiten_card_id FROM commitments WHERE status='open' AND kaiten_card_id IS NOT NULL ORDER BY id DESC")}
    blockers = {r["id"]: store.loads(r["blockers"], []) or [] for r in store.rows(
        f"SELECT id, blockers FROM kaiten_cards WHERE id IN ({marks})", card_ids)}
    for t in tasks:
        active = set()
        for b in blockers.get(t["kaiten_card_id"], []):
            ref = f"{KAITEN_REF}{b['id']}"
            active.add(ref)
            if store.row("SELECT 1 FROM task_links WHERE task_id=? AND source_ref=?", (t["id"], ref)):
                continue
            other = by_card.get(b.get("blocker_card_id"))
            try:
                if other and other != t["id"]:
                    if not find_waits(t["id"], waits_for=other):
                        add_wait(t["id"], waits_for=other, note=b.get("reason"), source_ref=ref, by="kaiten")
                else:
                    event = (f"карточка Kaiten «{b.get('blocker_card_title') or b.get('blocker_card_id')}»"
                             if b.get("blocker_card_id") else f"в Kaiten: {b.get('reason') or 'блокировка'}")
                    add_wait(t["id"], event=event, note=b.get("reason") if b.get("blocker_card_id") else None,
                             source_ref=ref, by="kaiten")
            except ValueError:  # замкнутый круг или задачи уже нет — эту блокировку не переносим
                continue
        for link in store.rows("SELECT * FROM task_links WHERE task_id=? AND source_ref LIKE ?",
                               (t["id"], KAITEN_REF + "%")):
            if link["source_ref"] in active:
                continue
            if link["waits_for"]:
                remove_wait(link["id"])
                if not _open_waits(t["id"]):
                    _notify_unblocked(None, task_id=t["id"], why="В Kaiten сняли блокировку")
            elif not link["done_at"]:
                set_event_done(link["id"])


def _open_waits(task_id: int) -> int:
    """Сколько ещё не готово из того, что ждёт задача: открытые задачи и не случившиеся события."""
    return store.scalar(
        "SELECT COUNT(*) FROM task_links l LEFT JOIN commitments c ON c.id=l.waits_for WHERE l.task_id=? "
        "AND ((l.waits_for IS NOT NULL AND c.status='open') OR (l.waits_for IS NULL AND l.done_at IS NULL))",
        (task_id,)) or 0


def _notify_unblocked(closed_id: int | None, *, task_id: int | None = None, event: str | None = None,
                      why: str | None = None) -> None:
    """Готово то, что ждали, — уведомление в углу экрана «Можно делать» по каждой задаче, которой больше нечего
    ждать. closed_id — закрытая задача; task_id и event — у этой задачи случилось событие."""
    from opsdash import notices

    if closed_id:
        closed = store.row("SELECT * FROM commitments WHERE id=?", (closed_id,))
        waiting = [r["task_id"] for r in store.rows("SELECT task_id FROM task_links WHERE waits_for=?", (closed_id,))]
        why = (f"{doer(closed)}: «{headline(closed)}» — "
               + ("готово" if closed["status"] == "done" else "отменена, больше не ждём"))
    else:
        waiting, why = [task_id], why or f"Случилось: {event}"
    for tid in dict.fromkeys(waiting):
        t = store.row("SELECT * FROM commitments WHERE id=? AND status='open' AND archived_at IS NULL", (tid,))
        if t and not _open_waits(tid):
            notices.add("tasks", f"Можно делать: «{headline(t)}»", why.replace("Я:", "Вы:", 1),
                        link=f"/commitments?task={tid}")


def unblocked(days: int = 2) -> list[dict]:
    """Открытые задачи, которые ждали другое, и всё это готово за последние days дней — «можно делать»
    в сводке и дайджесте. c["freed_by"] — что было готово последним."""
    since = store.to_utc_iso(datetime.now(timezone.utc) - timedelta(days=days))
    out = []
    for c in list_commitments(status="open"):
        if c["waits"] and not c["blocked"]:
            done = [(w["task"]["closed_at"] if w["task"] else w["done_at"]) or "" for w in c["waits"]]
            last = max(done)
            if last >= since:
                c["freed_by"] = c["waits"][done.index(last)]
                out.append(c)
    return out


def check_merge(drop_id: int, keep_id: int) -> tuple[dict, dict]:
    """Можно ли объединить: остаётся открытая задача (иначе живая работа пропала бы с доски), дубль — открытый
    или уже закрытый (закрыли раньше, а работа продолжается в keep), но не дубль чего-то ещё."""
    drop = store.row("SELECT * FROM commitments WHERE id=?", (drop_id,))
    keep = store.row("SELECT * FROM commitments WHERE id=?", (keep_id,))
    if not drop or not keep:
        raise ValueError(f"Нет задачи [{drop_id if not drop else keep_id}]")
    if drop["id"] == keep["id"]:
        raise ValueError("Задачу нельзя объединить саму с собой")
    for c in (keep, drop):
        if c["duplicate_of"]:
            raise ValueError(f"«{headline(c)}» уже дубль другой задачи [{c['duplicate_of']}]")
    if keep["status"] != "open":
        if drop["status"] == "open":
            raise ValueError(f"«{headline(keep)}» уже закрыта, а эта — в работе. Объедините наоборот: откройте "
                             f"«{headline(keep)}» и выберите «Это дубль задачи…» → эту")
        raise ValueError("Обе задачи закрыты — объединять незачем")
    return drop, keep


def merge_commitments(drop_id: int, keep_id: int, *, note: str | None = None) -> None:
    """Объединить дубли: drop закрывается как дубль keep. Откуда drop и цитата — в заметку keep; подзадачи, связи
    «ждёт», а также срок, карточка Kaiten, встреча и основная задача, если у keep их нет, — переходят к keep.
    Вернуть дубль — «Вернуть» у него (снова самостоятельная задача; перенесённое остаётся у keep)."""
    from opsdash import proposals

    drop, keep = check_merge(drop_id, keep_id)
    now = store.utcnow()
    # откуда известно — в заметку остающейся задачи, чтобы источник дубля не потерялся
    source = proposals._source_text(f"{drop['source_type']}:{drop['source_ref']}") if drop["source_ref"] else ""
    line = f"Объединена с дублем «{headline(drop)}»" + (f" ({source})" if source else "")
    if drop["quote"]:
        line += f": «{drop['quote']}»"
    if note:
        line += f". {note.strip()}"
    keep_note = "\n".join(filter(None, [keep["note"], line]))
    # чего нет у остающейся — берём у дубля
    take = {"due": keep["due"] or drop["due"], "kaiten_card_id": keep["kaiten_card_id"] or drop["kaiten_card_id"],
            "project_id": keep["project_id"] or drop["project_id"],
            "event_uid": keep["event_uid"] or drop["event_uid"],
            "event_info": keep["event_info"] if keep["event_uid"] else drop["event_info"]}
    parent = keep["parent_id"]
    if parent == drop_id:  # остающаяся была подзадачей дубля — теперь часть того же, что и он
        parent = drop["parent_id"]
    elif not parent and drop["parent_id"] and drop["parent_id"] != keep_id:
        parent = drop["parent_id"]
    store.execute("UPDATE commitments SET note=?, due=?, kaiten_card_id=?, event_uid=?, event_info=?, parent_id=?, "
                  "project_id=?, updated=? WHERE id=?",
                  (keep_note, take["due"], take["kaiten_card_id"], take["event_uid"], take["event_info"], parent,
                   take["project_id"], now, keep_id))
    ancestors, pid = set(), parent
    while pid and pid not in ancestors:
        ancestors.add(pid)
        pid = store.scalar("SELECT parent_id FROM commitments WHERE id=?", (pid,))
    for kid in store.rows("SELECT id FROM commitments WHERE parent_id=? AND id!=?", (drop_id, keep_id)):
        # подзадачи дубля — теперь подзадачи keep (кроме той, частью которой keep сама оказалась)
        store.execute("UPDATE commitments SET parent_id=?, updated=? WHERE id=?",
                      (drop["parent_id"] if kid["id"] in ancestors else keep_id, now, kid["id"]))
    # связи «ждёт»: что ждал дубль — теперь ждёт keep, кто ждал дубль — теперь ждёт keep; без связей «сама с собой»,
    # повторов и замкнутых кругов
    for r in store.rows("SELECT * FROM task_links WHERE task_id=? OR waits_for=?", (drop_id, drop_id)):
        task, target = (keep_id if r["task_id"] == drop_id else r["task_id"]), (
            keep_id if r["waits_for"] == drop_id else r["waits_for"])
        clash = target and (task == target or store.row(
            "SELECT 1 FROM task_links WHERE task_id=? AND waits_for=? AND id!=?", (task, target, r["id"]))
            or _reaches(_waits_graph(), target, task))
        if clash:
            store.execute("DELETE FROM task_links WHERE id=?", (r["id"],))
        else:
            store.execute("UPDATE task_links SET task_id=?, waits_for=? WHERE id=?", (task, target, r["id"]))
    # пункты дубля — в конец списка keep (такие же не повторяем, отметки «готово» сохраняются)
    have = {r["text"].lower() for r in store.rows("SELECT text FROM task_items WHERE task_id=?", (keep_id,))}
    for r in store.rows("SELECT * FROM task_items WHERE task_id=? ORDER BY sort, id", (drop_id,)):
        if r["text"].lower() in have:
            store.execute("DELETE FROM task_items WHERE id=?", (r["id"],))
        else:
            store.execute("UPDATE task_items SET task_id=?, sort=sort+1000 WHERE id=?", (keep_id, r["id"]))
    store.execute("UPDATE commitments SET status='cancelled', duplicate_of=?, closed_at=COALESCE(closed_at, ?), "
                  "in_work_at=NULL, seen_at=COALESCE(seen_at, ?), updated=? WHERE id=?", (keep_id, now, now, now, drop_id))
    # предложения Claude по дублю больше не ждут ответа; предложение объединить эту пару — выполнено
    store.execute(
        "UPDATE proposals SET status='rejected', decided_at=?, result=? WHERE status='pending' "
        "AND kind IN ('task.done', 'telegram.remind', 'task.meeting', 'task.wait') "
        "AND json_extract(payload, '$.commitment_id')=?", (now, store.json_col({"note": "задача — дубль"}), drop_id))
    store.execute(
        "UPDATE proposals SET status='done', decided_at=?, result=? WHERE status='pending' AND kind='task.merge' "
        "AND json_extract(payload, '$.commitment_id')=? AND json_extract(payload, '$.into')=?",
        (now, store.json_col({"note": "объединили вручную"}), drop_id, keep_id))


def similar_open(*, direction: str, who: str | None, whom: str | None, title: str | None, what: str | None,
                 kaiten_card_id: int | None = None, exclude: int | None = None, limit: int = 5) -> list[dict]:
    """Открытые задачи того же человека и того же типа, похожие по словам, — чтобы при добавлении увидеть
    возможный дубль: Claude сам решает, то же это или нет. c["similarity"] — доля общих слов, та же карточка
    Kaiten — 1. Человек: кому обещала директор (mine) или кто делает."""
    person = _stems(whom if direction == "mine" else who)
    words = _stems(f"{title or ''} {what or ''}")
    out = []
    for c in store.rows("SELECT * FROM commitments WHERE status='open' AND archived_at IS NULL AND direction=? "
                        "AND id!=?", (direction, exclude or 0)):
        same_card = bool(kaiten_card_id and c["kaiten_card_id"] == kaiten_card_id)
        if not same_card and not (person and person & _stems(c["whom"] if direction == "mine" else c["who"])):
            continue
        other = _stems(f"{c['title'] or ''} {c['what'] or ''}")
        score = 1.0 if same_card else (len(words & other) / min(len(words), len(other)) if words and other else 0)
        if score >= 0.2:
            c["similarity"], c["headline"], c["route"] = round(score, 2), headline(c), route(c)
            out.append(c)
    return sorted(out, key=lambda c: -c["similarity"])[:limit]


# --- пункты внутри задачи ---------------------------------------------------------------
# Большая задача из частей («Схемы процессов склада»: приёмка, хранение, отгрузка, …): пункты показывают,
# сколько готово, и проект считает такую задачу по долям — 6 из 9 воронок, а не «не сделано».

def _item_texts(texts: list[str] | str) -> list[str]:
    """«Приёмка; Хранение» или список таких строк → чистые тексты пунктов без пустых и повторов."""
    parts = [p for t in ([texts] if isinstance(texts, str) else texts) for p in re.split(r"[;\n]", t or "")]
    return list(dict.fromkeys(" ".join(p.split()) for p in parts if p.strip()))


def add_items(task_id: int, texts: list[str] | str, *, by: str = "director") -> list[int]:
    """Добавить пункты в конец списка; такой пункт уже есть — не повторяем. Возвращает id новых."""
    if not store.row("SELECT 1 FROM commitments WHERE id=?", (task_id,)):
        raise ValueError(f"Нет задачи [{task_id}]")
    have = {r["text"].lower() for r in store.rows("SELECT text FROM task_items WHERE task_id=?", (task_id,))}
    sort = store.scalar("SELECT COALESCE(MAX(sort), 0) FROM task_items WHERE task_id=?", (task_id,)) or 0
    out = []
    for text in _item_texts(texts):
        if text.lower() in have:
            continue
        sort += 1
        have.add(text.lower())
        out.append(store.execute("INSERT INTO task_items(task_id, text, sort, added_by, created) VALUES(?,?,?,?,?)",
                                 (task_id, text[:200], sort, "claude" if by == "claude" else "director",
                                  store.utcnow())))
    return out


def find_items(task_id: int, text: str) -> list[dict]:
    """Пункты задачи по тексту (без регистра) — для ./ops: точное совпадение — только он; иначе те, где с этого
    текста начинается слово («Склад» — «Склад в Казани», но не «Подсклад»)."""
    q = " ".join((text or "").split()).lower().replace("ё", "е")
    rows = store.rows("SELECT * FROM task_items WHERE task_id=? ORDER BY sort, id", (task_id,))
    norm = {r["id"]: r["text"].lower().replace("ё", "е") for r in rows}
    exact = [r for r in rows if norm[r["id"]] == q]
    start = re.compile(r"(?<![a-zа-я0-9])" + re.escape(q))
    return exact or [r for r in rows if q and start.search(norm[r["id"]])]


def set_item_done(item_id: int, done: bool = True, note: str | None = None) -> None:
    store.execute("UPDATE task_items SET done_at=?, note=? WHERE id=?",
                  (store.utcnow() if done else None, (note or "").strip() or None if done else None, item_id))


def remove_item(item_id: int) -> None:
    store.execute("DELETE FROM task_items WHERE id=?", (item_id,))


def _items(items: list[dict]) -> None:
    """c["checklist"] — пункты задачи по порядку (не «items»: в шаблонах c.items — метод словаря);
    c["items_done"] / c["items_total"]; c["share"] — какая доля задачи готова: выполненная — 1, открытая
    с пунктами — доля готовых пунктов, без пунктов — 0."""
    ids = [c["id"] for c in items]
    rows = store.rows(f"SELECT * FROM task_items WHERE task_id IN ({','.join('?' * len(ids))}) ORDER BY sort, id", ids)
    by_task: dict[int, list[dict]] = {}
    for r in rows:
        r["done"] = bool(r["done_at"])
        by_task.setdefault(r["task_id"], []).append(r)
    for c in items:
        c["checklist"] = by_task.get(c["id"], [])
        c["items_total"] = len(c["checklist"])
        c["items_done"] = sum(1 for r in c["checklist"] if r["done"])
        c["share"] = 1.0 if c["status"] == "done" else (
            c["items_done"] / c["items_total"] if c["items_total"] else 0.0)


Period = tuple[date | None, date | None]


def _filters(person: str | None, q: str | None, created: Period | None, due: Period | None,
             no_due: bool, unseen: bool = False, project: int | str | None = None) -> tuple[str, list]:
    """Общие условия списка и доски: человек, поиск, дата создания, срок, только новые (директор их не видела),
    проект (id; "none" — задачи вне проектов)."""
    sql, params = "", []
    if project == "none":
        sql += " AND project_id IS NULL"
    elif project:
        sql += " AND project_id=?"
        params.append(int(project))
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
                    due: str | None, parent_id: int | None = None, project_id: int | None = -1) -> None:
    """Изменить задачу из её карточки: заголовок, суть, тип, кто и кому, срок (пустой срок — без срока), часть
    какой задачи (пусто — самостоятельная), проект (None — ни в каком, −1 — не менять). Без сути заголовок
    становится и сутью."""
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
    if project_id and project_id != -1:
        _check_project(project_id)
    store.execute("UPDATE commitments SET title=?, what=?, direction=?, who=?, whom=?, due=?, parent_id=?, "
                  "seen_at=COALESCE(seen_at, ?), updated=? WHERE id=?",
                  (title or None, what, direction, (who or "").strip() or None, (whom or "").strip() or None,
                   due or None, parent_id or None, now, now, cid))
    if project_id != -1:
        set_project(cid, project_id or None)


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
                     "AND archived_at IS NULL ORDER BY created DESC, id DESC")
    for c in out:
        c["label"] = f"#{c['id']} · {headline(c)} · {route(c)}"
    return out


def add_links(items: list[dict]) -> list[dict]:
    """Кнопка «открыть в Telegram / Пачке / почте» у задачи: сообщение, из которого она взялась, а если задача
    со встречи или добавлена вручную — личный чат с человеком из справочника."""
    from opsdash import links

    people = list_people()
    for c in items:
        c["link"] = c["link_label"] = None
        msg = None
        if (c.get("source_type") in ("telegram", "mail", "ymessenger", "pachca")
                and str(c.get("source_ref") or "").isdigit()):
            msg = store.row("SELECT source, chat_id, msg_id, sender_id FROM messages WHERE id=?", (int(c["source_ref"]),))
        found = links.original(msg["source"], msg["chat_id"], msg["msg_id"], msg["sender_id"]) if msg else None
        if not found and (name := counterpart(c)):
            person = _person_by_name(name, people)
            username = ((person or {}).get("telegram") or "").strip().lstrip("@")
            if username:
                found = (f"https://t.me/{username}", "Telegram")
        if found:
            c["link"], c["link_title"] = found
            c["link_label"] = ("Telegram" if "t.me/" in found[0] or "telegram.org" in found[0]
                               else "Пачка" if "pachca.com" in found[0] else "Почта")
    return with_reminders(items, people)


# --- напомнить коллеге ---------------------------------------------------------------

REMINDER_TEMPLATE = "{имя}, привет! Напоминаю про «{задача}» ({срок}). Как продвигается?"


def _words(text: str | None) -> set[str]:
    return {w[:5] for w in re.findall(r"[a-zа-я0-9]+", (text or "").lower().replace("ё", "е")) if len(w) >= 3}


def recipient(c: dict, people: list[dict] | None = None) -> dict | None:
    """Кому писать напоминание по задаче «мне должны»: личный чат Telegram с исполнителем. target — @username
    или id чата (для отправки), link — открыть чат. None — чат не нашли: останется «Копировать»."""
    name = (c.get("who") or "").strip()
    if not name or c.get("direction") != "to_me":
        return None
    people = list_people() if people is None else people
    person = _person_by_name(name, people)
    label = person["name"] if person else name
    username = ((person or {}).get("telegram") or "").strip().lstrip("@")
    chat = None
    if not username:
        chats = store.rows("SELECT chat_id, title, username FROM chats WHERE source='telegram' AND kind='private'")
        for n in [name, *([person["name"], *person["aliases"]] if person else [])]:
            hits = [ch for ch in chats if _words(n) and _words(n) <= _words(ch["title"])]
            if len(hits) == 1:
                chat = hits[0]
                break
        if not chat and c.get("source_type") == "telegram" and str(c.get("source_ref") or "").isdigit():
            chat = store.row(
                "SELECT m.chat_id, ch.username FROM messages m JOIN chats ch ON ch.source=m.source AND ch.chat_id=m.chat_id "
                "WHERE m.id=? AND ch.kind='private'", (int(c["source_ref"]),))
        username = ((chat or {}).get("username") or "").lstrip("@")
    if username:
        return {"target": "@" + username, "link": f"https://t.me/{username}", "label": label}
    if chat:  # без @имени: отправить можно по id чата, открыть — ссылкой приложения Telegram
        return {"target": str(chat["chat_id"]), "link": f"tg://user?id={chat['chat_id']}", "label": label}
    return None


def reminder_text(c: dict, settings: dict | None = None, name: str | None = None) -> str:
    """Готовый текст напоминания по шаблону из настроек: {имя}, {задача}, {срок}."""
    settings = settings or config.load_settings()
    due, today = _as_date(c.get("due")), today_local()
    if not due:
        when = "без срока"
    elif due < today:
        when = f"срок был {due:%d.%m}"
    elif due == today:
        when = "договаривались на сегодня"
    elif due == today + timedelta(days=1):
        when = "договаривались на завтра"
    else:
        when = f"договаривались до {due:%d.%m}"
    values = {"имя": (name or c.get("who") or "").split(" ")[0], "задача": headline(c), "срок": when}
    try:
        return (settings["reminders"].get("template") or REMINDER_TEMPLATE).format(**values)
    except (KeyError, IndexError, ValueError):  # шаблон с ошибкой — не ломаем страницу
        return REMINDER_TEMPLATE.format(**values)


def with_reminders(items: list[dict], people: list[dict] | None = None) -> list[dict]:
    """У открытых задач «мне должны» — c["remind"]: текст напоминания и чат исполнителя (to; None — не нашли)."""
    settings = None
    for c in items:
        c["remind"] = None
        if c.get("direction") == "to_me" and c.get("status") == "open" and c.get("who"):
            people = list_people() if people is None else people
            settings = settings or config.load_settings()
            to = recipient(c, people)
            c["remind"] = {"to": to, "text": reminder_text(c, settings, to["label"] if to else None)}
    return items


def reminders() -> list[dict]:
    """«Напомните коллегам» на сводке: задачи «мне должны» со сроком сегодня, завтра или уже просроченные.
    Кому напомнили меньше суток назад — не показываем."""
    since = store.to_utc_iso(datetime.now(timezone.utc) - timedelta(hours=20))
    items = _mark(store.rows(
        "SELECT * FROM commitments WHERE status='open' AND archived_at IS NULL AND direction='to_me' "
        "AND due IS NOT NULL AND due <= ? AND (reminded_at IS NULL OR reminded_at < ?) ORDER BY due, id",
        ((today_local() + timedelta(days=1)).isoformat(), since)))
    return add_links(items)


def mark_reminded(cid: int) -> None:
    store.execute("UPDATE commitments SET reminded_at=?, updated=? WHERE id=?", (store.utcnow(), store.utcnow(), cid))


# --- загрузка людей ---------------------------------------------------------------------

def workload() -> list[dict]:
    """Кто сколько делает: открытые задачи, где человек исполнитель (свои — «Я»), и его карточки Kaiten
    не в «Готово». Сначала вы, потом самые загруженные. person_q / kaiten_q — для ссылок на доски."""
    people = list_people()
    me_kaiten = (store.get_state("kaiten_me") or {}).get("id")
    out: dict[str, dict] = {}

    def slot(name: str, person: dict | None, me: bool = False) -> dict:
        key = "me" if me else (f"p{person['id']}" if person else name.lower())
        return out.setdefault(key, {"key": key, "name": "Я" if me else (person["name"] if person else name), "me": me,
                                    "raw": set(), "kaiten_names": set(), "open": 0, "overdue": 0, "week": 0,
                                    "kaiten": 0, "kaiten_overdue": 0})

    for c in list_commitments(status="open"):
        if c["direction"] == "mine":
            s = slot("", None, me=True)
        elif (who := (c.get("who") or "").strip()):
            s = slot(who, _person_by_name(who, people))
            s["raw"].add(who)
        else:
            continue
        s["open"] += 1
        s["overdue"] += int(c["overdue"])
        s["week"] += int(c["due_days"] is not None and 0 <= c["due_days"] <= 6)
    today = today_local().isoformat()
    for k in store.rows("SELECT responsible_id, responsible_name, due_date FROM kaiten_cards "
                        "WHERE COALESCE(state, 1) != 3 AND COALESCE(condition, 1) = 1 AND responsible_name IS NOT NULL"):
        me = bool(me_kaiten and k["responsible_id"] == me_kaiten)
        person = None if me else (next((p for p in people if p.get("kaiten_user_id") == k["responsible_id"]), None)
                                  or _person_by_name(k["responsible_name"], people))
        s = slot(k["responsible_name"], person, me)
        s["kaiten"] += 1
        s["kaiten_overdue"] += int(bool(k["due_date"]) and k["due_date"][:10] < today)
        s["kaiten_names"].add(k["responsible_name"])
    rows = sorted(out.values(), key=lambda s: (not s["me"], -(s["open"] + s["kaiten"]), s["name"]))
    for s in rows:
        s["person_q"] = min(s["raw"], key=len) if s["raw"] else s["name"]
        s["kaiten_q"] = min(s["kaiten_names"], key=len) if s["kaiten_names"] else ""
    return rows


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
                     sort: str = "due", archived: bool = False, project: int | str | None = None) -> list[dict]:
    """sort: due — ближайший срок сверху, без срока в конце; created — последние добавленные сверху.
    Задачи «в работе» — всегда первыми. Архивные — только с archived=True (галочка «Показать архивные»),
    в конце; status="archived" — только архив, любой статус (./ops commitments list)."""
    where, params = _filters(person, q, created, due, no_due, unseen, project)
    sql = "SELECT * FROM commitments WHERE 1=1" + where
    if status == "archived":
        sql += " AND archived_at IS NOT NULL"
    elif not archived:
        sql += " AND archived_at IS NULL"
    if status and status not in ("all", "archived"):
        sql += " AND status=?"
        params.append(status)
    if direction:
        sql += " AND direction=?"
        params.append(direction)
    if overdue:
        sql += " AND due IS NOT NULL AND due < ?"
        params.append(today_local().isoformat())
    first = ("CASE WHEN archived_at IS NOT NULL THEN 2 WHEN status='open' AND in_work_at IS NOT NULL THEN 0 "
             "ELSE 1 END")
    if status == "archived":
        sql += " ORDER BY archived_at DESC, id DESC LIMIT ?"
    elif sort == "created":
        sql += f" ORDER BY {first}, created DESC, id DESC LIMIT ?"
    else:
        sql += f" ORDER BY {first}, CASE WHEN due IS NULL THEN 1 ELSE 0 END, due, created DESC LIMIT ?"
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
        c["archived"] = bool(c.get("archived_at"))
        c["overdue"] = bool(c["due"] and c["due"] < today.isoformat() and c["status"] == "open" and not c["archived"])
        c["in_work"] = bool(c.get("in_work_at") and c["status"] == "open")
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
    hints, merge_hints, wait_hints = {}, {}, {}
    for p in store.rows("SELECT * FROM proposals WHERE kind IN ('task.done', 'task.merge', 'task.wait') "
                        "AND status='pending' ORDER BY id"):
        p["payload"] = store.loads(p["payload"], {})
        cid = p["payload"].get("commitment_id")
        if p["kind"] == "task.done":
            hints[cid] = p
        elif p["kind"] == "task.merge":
            into = store.row("SELECT id, direction, who, whom, title, what FROM commitments WHERE id=?",
                             (p["payload"].get("into"),))
            if into:
                into["headline"], into["route"] = headline(into), route(into)
            p["into_task"] = into
            merge_hints.setdefault(cid, p)
        else:
            wait_hints.setdefault(cid, []).append(p)
    _waits(items)
    _items(items)
    pids = list({c["project_id"] for c in items if c.get("project_id")})
    titles = {r["id"]: r for r in store.rows(
        f"SELECT id, title, status FROM projects WHERE id IN ({','.join('?' * len(pids)) or 'NULL'})", pids)}
    for c in items:
        c["project"] = titles.get(c.get("project_id"))
        c["merge_hint"] = merge_hints.get(c["id"]) if c["status"] == "open" else None
        c["wait_hints"] = wait_hints.get(c["id"], []) if c["status"] == "open" else []
    for c in items:
        c["parent"] = by_id.get(c.get("parent_id"))
        c["children"] = sorted(kids.get(c["id"], []), key=lambda r: (r["status"] != "open", r["due"] or "9999"))
        mine = doer(c)
        c["helpers"] = list(dict.fromkeys(doer(k) for k in c["children"] if k["status"] == "open" and doer(k) != mine))
        c["kids_done"] = bool(c["children"]) and all(k["status"] == "done" for k in c["children"])
        c["late_vs_parent"] = bool(c["parent"] and c["status"] == "open" and c["due"] and c["parent"]["due"]
                                   and c["due"] > c["parent"]["due"])
        c["done_hint"] = hints.get(c["id"]) if c["status"] == "open" else None
    from opsdash import task_meetings

    task_meetings.decorate(items)


def _waits(items: list[dict]) -> None:
    """«Ждёт» на карточках. c["waits"] — что ждёт задача: другие задачи (task) и события (event, until); done —
    уже готово. c["holds"] — открытые задачи, которые ждут эту. c["blocked"] — ещё есть что ждать, c["waits_open"] —
    сколько. c["wait_risk"] — почему срок под угрозой: то, что ждём, просрочено или придёт позже срока, или срок
    уже сегодня. c["duplicate"] — задача, дублем которой оказалась эта."""
    ids = [c["id"] for c in items]
    marks = ",".join("?" * len(ids))
    links = store.rows(f"SELECT * FROM task_links WHERE task_id IN ({marks}) OR waits_for IN ({marks}) ORDER BY id",
                       [*ids, *ids])
    want = {l["waits_for"] for l in links if l["waits_for"]} | {l["task_id"] for l in links} | {
        c["duplicate_of"] for c in items if c.get("duplicate_of")}
    tasks = {r["id"]: r for r in store.rows(
        "SELECT id, direction, who, whom, title, what, due, status, closed_at, archived_at, kaiten_card_id "
        f"FROM commitments WHERE id IN ({','.join('?' * len(want)) or 'NULL'})", list(want))}
    # блокировки в Kaiten у карточек этих задач: есть ли там уже то, что задача ждёт (kaiten.sync_blockers)
    cards = [c["kaiten_card_id"] for c in items if c.get("kaiten_card_id")]
    in_kaiten = {r["id"]: {b.get("blocker_card_id") for b in store.loads(r["blockers"], []) or []}
                 for r in store.rows(f"SELECT id, blockers FROM kaiten_cards WHERE id IN ({','.join('?' * len(cards)) or 'NULL'})",
                                     cards)}
    today = today_local()
    for r in tasks.values():
        r["headline"], r["route"], r["doer"] = headline(r), route(r), doer(r)
        r["overdue"] = bool(r["status"] == "open" and r["due"] and r["due"] < today.isoformat())
    for c in items:
        c["waits"], c["holds"] = [], []
        c["duplicate"] = tasks.get(c.get("duplicate_of"))
    by_id = {c["id"]: c for c in items}
    for l in links:
        if (c := by_id.get(l["task_id"])) is not None:
            t = tasks.get(l["waits_for"])
            if l["waits_for"] and not t:
                continue
            c["waits"].append({
                "id": l["id"], "task": t, "event": l["event"], "until": l["until"], "done_at": l["done_at"],
                "done": (t["status"] != "open") if t else bool(l["done_at"]), "note": l["note"],
                "by_claude": l["added_by"] == "claude", "by_kaiten": l["added_by"] == "kaiten",
                # в Kaiten: on — карточку уже блокирует карточка той задачи, can — у обеих есть карточки, можно поставить
                "kaiten": _kaiten_state(c, t, l, in_kaiten),
                "late": bool(t and t["overdue"]) or bool(not t and not l["done_at"] and l["until"]
                                                          and l["until"] < today.isoformat())})
        if (c := by_id.get(l["waits_for"])) is not None and (t := tasks.get(l["task_id"])) and t["status"] == "open":
            c["holds"].append({"id": l["id"], "task": t})
    for c in items:
        c["waits"].sort(key=lambda w: w["done"])
        pending = [w for w in c["waits"] if not w["done"]]
        c["blocked"], c["waits_open"] = bool(pending) and c["status"] == "open", len(pending)
        c["wait_risk"] = _wait_risk(c, pending, today) if c["blocked"] else None


def _kaiten_state(c: dict, t: dict | None, link: dict, in_kaiten: dict) -> str | None:
    if not t or not c.get("kaiten_card_id") or not t.get("kaiten_card_id"):
        return None
    if link["added_by"] == "kaiten" or t["kaiten_card_id"] in in_kaiten.get(c["kaiten_card_id"], set()):
        return "on"
    if c["kaiten_card_id"] not in in_kaiten:  # карточки нет в зеркале — блокировку не поставить
        return None
    from opsdash.sources import kaiten

    return "can" if kaiten.blockers_supported() else None


def _wait_risk(c: dict, pending: list[dict], today: date) -> str | None:
    for w in pending:
        name = f"«{w['task']['headline']}»" if w["task"] else w["event"]
        if w["late"]:
            return f"{name} — срок уже прошёл, а задача его ждёт"
        later = (w["task"] or {}).get("due") if w["task"] else w["until"]
        if c["due"] and later and later > c["due"]:
            return f"{name} — к {_as_date(later):%d.%m}, позже срока этой задачи"
    if c["due"] and c["due"] <= today.isoformat():
        return "срок " + ("сегодня" if c["due"] == today.isoformat() else "прошёл") + ", а задача ещё ждёт"
    return None


def commitments_board(*, person: str | None = None, q: str | None = None, done_days: int = 14,
                      created: Period | None = None, due: Period | None = None,
                      no_due: bool = False, unseen: bool = False, sort: str = "due",
                      archived: bool = False, project: int | str | None = None) -> dict[str, list[dict]]:
    """Доска задач и договорённостей: открытые — по направлению, «Готово» — закрытые за done_days дней
    (последние закрытые сверху при любой сортировке). archived — добавить архивные в конец колонок:
    открытые — в свою, выполненные и отменённые — в «Готово», за любой срок."""
    board = {d: list_commitments(status="open", direction=d, person=person, q=q, created=created, due=due,
                                 no_due=no_due, unseen=unseen, sort=sort, archived=archived, project=project)
             for d in DIRECTIONS}
    since = store.to_utc_iso(datetime.now(timezone.utc) - timedelta(days=done_days))
    where, params = _filters(person, q, created, due, no_due, unseen, project)
    sql = ("SELECT * FROM commitments WHERE status='done' AND archived_at IS NULL "
           "AND COALESCE(closed_at, updated) >= ?" + where)
    board["done"] = _mark(store.rows(sql + " ORDER BY COALESCE(closed_at, updated) DESC LIMIT 300", [since, *params]))
    if archived:
        board["done"] += _mark(store.rows(
            "SELECT * FROM commitments WHERE status IN ('done', 'cancelled') AND archived_at IS NOT NULL" + where
            + " ORDER BY archived_at DESC LIMIT 300", params))
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
    msg = msg or {}
    chat = " · ".join(filter(None, (msg.get("chat_title"), msg.get("topic"))))  # «Отдел продаж · Итоги дня»
    return store.execute(
        """INSERT INTO attention(message_id, source, chat_title, sender_name, summary, why, draft, priority, status,
           created, updated) VALUES(?,?,?,?,?,?,?,?,'open',?,?)""",
        (message_id, msg.get("source"), chat or None, msg.get("sender_name"), summary, why, draft, priority, now, now),
    )


def close_attention(aid: int, status: str = "done") -> None:
    now = store.utcnow()
    if status == "open":  # «Вернуть»: директор не согласна, что ответила
        store.execute("UPDATE attention SET status='open', reopened_at=?, updated=? WHERE id=?", (now, now, aid))
        return
    store.execute("UPDATE attention SET status=?, updated=? WHERE id=?", (status, now, aid))
    # закрыли сами — предложение Claude «похоже, вы ответили» больше не ждёт ответа
    store.execute(
        "UPDATE proposals SET status=?, decided_at=?, result=? WHERE kind='attention.done' AND status='pending' "
        "AND json_extract(payload, '$.attention_id')=?",
        ("done" if status == "done" else "rejected", now, store.json_col({"note": "закрыли вручную"}), aid))


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

    hints = {}
    for p in store.rows("SELECT * FROM proposals WHERE kind='attention.done' AND status='pending' ORDER BY id"):
        p["payload"] = store.loads(p["payload"], {})
        hints[p["payload"].get("attention_id")] = p
    for a in items:  # кнопка «открыть в оригинале»: сообщение, чат или письмо
        a["orig_url"], a["orig_label"] = links.original(
            a["msg_source"] or a["source"], a["chat_id"], a["msg_ext_id"], a["msg_sender"]) or (None, None)
        a["done_hint"] = hints.get(a["id"]) if a["status"] == "open" else None
    return items


def replies_after(a: dict, limit: int = 5) -> list[dict]:
    """Что директор писала в том же чате после вопроса — чтобы Claude по смыслу решил, ответила ли она
    (сам по себе факт сообщения ничего не значит: она могла писать о другом). В почте — только ответ
    на это письмо. is_reply — ответ (reply) именно на это сообщение."""
    if not a.get("msg_ts"):
        return []
    if a.get("msg_source") == "mail":
        rows = store.rows("SELECT id, ts, text, reply_to FROM messages WHERE source='mail' AND is_outgoing=1 "
                          "AND reply_to=? ORDER BY ts LIMIT ?", (a["msg_ext_id"], limit))
    else:
        rows = store.rows("SELECT id, ts, text, reply_to FROM messages WHERE source=? AND chat_id=? AND is_outgoing=1 "
                          "AND ts > ? ORDER BY ts LIMIT ?", (a["msg_source"], a["chat_id"], a["msg_ts"], limit))
    for r in rows:
        r["is_reply"] = bool(r["reply_to"]) and r["reply_to"] == a.get("msg_ext_id")
    return rows


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


_TIMECODE = re.compile(r"\s*[\[(]\d{1,2}:\d{2}(?::\d{2})?[\])](?:\s*[–-]\s*[\[(]\d{1,2}:\d{2}(?::\d{2})?[\])])?,?")


def _plain(line: str) -> str:
    """Пункт разбора для текста коллегам: без маркера списка, отметок времени и Markdown-выделения."""
    line = re.sub(r"^\s*(?:[-*•]|\d+[.)])\s+", "", line)
    line = _TIMECODE.sub("", line).replace("**", "").replace("__", "")
    return re.sub(r"\s{2,}", " ", line).strip(" ,;")


def _short_what(c: dict) -> str:
    what = " ".join((c.get("what") or "").split())
    cuts = [i for sep in (": ", "; ") if 12 <= (i := what.find(sep))]
    what = what[:min(cuts)] if cuts else what
    return what if len(what) <= 160 else what[:157].rsplit(" ", 1)[0] + "…"


def followup_text(meeting_id: str) -> tuple[str, bool]:
    """Текст итогов встречи для участников — что решили, следующие шаги, задачи (кто кому, срок). Готовит Claude
    при разборе (followup.md); у старых встреч собираем из разбора и задач. Второе значение — собран ли сам."""
    own = read_meeting_file(meeting_id, "followup.md").strip()
    if own:
        return own, False
    summary = read_meeting_file(meeting_id, "summary.md")
    if not summary:
        return "", True
    sections: dict[str, list[str]] = {}
    current = None
    for line in summary.splitlines():
        if line.startswith("## "):
            current = sections.setdefault(line[3:].strip().lower(), [])
        elif current is not None and re.match(r"\s*(?:[-*•]|\d+[.)])\s+", line):
            current.append(_plain(line))
    pick = lambda *keys: [x for k, v in sections.items() if k.startswith(keys) for x in v if x]  # noqa: E731
    m = get_meeting(meeting_id) or {}
    owner = config.load_settings()["owner"].get("name") or "Я"
    tasks = [c for c in meeting_commitments(meeting_id) if c["source_ref"] == meeting_id and c["status"] != "cancelled"]
    out = [f"Итоги встречи «{m.get('title') or meeting_id}» {store.local(m.get('started_at'), '%d.%m')}".strip()]
    for title, items in (("Решили", pick("решени")), ("Следующие шаги", pick("следующие шаги", "открытые вопросы"))):
        if items:
            out += ["", f"{title}:", *[f"• {x}" for x in items]]
    if tasks:
        out += ["", "Задачи:"]
        for c in tasks:
            who = c.get("who") or (owner if c["direction"] == "mine" else "?")
            whom = c.get("whom") or (owner if c["direction"] == "to_me" else "")
            due = f" — до {c['due'][8:10]}.{c['due'][5:7]}" if c.get("due") else ""
            out.append(f"• {who}{' → ' + whom if whom else ''}: {_short_what(c)}{due}")
    return "\n".join(out), True


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
