"""Проекты: задачи, объединённые одним результатом («Запуск склада в Казани» — схемы процессов от Дмитрия,
ответы на вопросы, описание подрядчику, схемы в карточку проекта, план найма).

У задачи один проект; подзадачи — в проекте основной задачи. Прогресс — сколько задач проекта выполнено
из всех (отменённые и дубли не считаются, открытые в архиве — тоже); открытая задача с пунктами идёт долей
готовых пунктов: «Схемы процессов склада» с 6 схемами из 9 — это 2/3 задачи. Цепочка — задачи по шагам: сначала
то, что можно делать сейчас, дальше — то, что ждёт предыдущее (связи «ждёт» и подзадачи, см. records).
"""

from __future__ import annotations

from datetime import date

from opsdash import records, store

STATUSES = {"active": "в работе", "paused": "на паузе", "done": "завершён"}


def _as_date(value: str | None) -> date | None:
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None


def add(*, title: str, goal: str | None = None, owner: str | None = None, due: str | None = None,
        kaiten_card_id: int | None = None, by: str = "director") -> int:
    title = " ".join((title or "").split())
    if not title:
        raise ValueError("Назовите проект")
    if due and not _as_date(due):
        raise ValueError("Срок — дата в виде ГГГГ-ММ-ДД")
    if store.row("SELECT 1 FROM projects WHERE lower(title)=lower(?) AND status!='done'", (title,)):
        raise ValueError(f"Проект «{title}» уже есть")
    now = store.utcnow()
    return store.execute(
        "INSERT INTO projects(title, goal, owner, due, status, kaiten_card_id, added_by, created, updated) "
        "VALUES(?,?,?,?,'active',?,?,?,?)",
        (title, (goal or "").strip() or None, (owner or "").strip() or None, due or None, kaiten_card_id or None,
         "claude" if by == "claude" else "director", now, now))


def update(pid: int, **fields) -> None:
    """Изменить проект: title, goal, owner, due, kaiten_card_id, status. Пустая строка — очистить поле."""
    p = store.row("SELECT * FROM projects WHERE id=?", (pid,))
    if not p:
        raise ValueError(f"Нет проекта [{pid}]")
    sets, params = [], []
    for key in ("title", "goal", "owner", "due", "kaiten_card_id", "status"):
        if key not in fields or fields[key] is None:
            continue
        value = fields[key]
        if isinstance(value, str):
            value = " ".join(value.split()) if key == "title" else value.strip()
        if key == "title" and not value:
            raise ValueError("Назовите проект")
        if key == "due" and value and not _as_date(value):
            raise ValueError("Срок — дата в виде ГГГГ-ММ-ДД")
        if key == "status":
            if value not in STATUSES:
                raise ValueError(f"Статус проекта — один из: {', '.join(STATUSES)}")
            sets.append("closed_at=?")
            params.append(store.utcnow() if value == "done" else None)
        sets.append(f"{key}=?")
        params.append(value or None)
    if sets:
        store.execute(f"UPDATE projects SET {', '.join(sets)}, updated=? WHERE id=?", [*params, store.utcnow(), pid])


def delete(pid: int) -> None:
    """Удалить проект: задачи остаются, просто ни в каком проекте."""
    store.execute("UPDATE commitments SET project_id=NULL WHERE project_id=?", (pid,))
    store.execute("DELETE FROM projects WHERE id=?", (pid,))


def assign(pid: int, task_ids: list[int]) -> list[int]:
    """Добавить задачи в проект (вместе с их подзадачами). Возвращает те, что нашлись."""
    found = []
    for tid in task_ids:
        if store.row("SELECT 1 FROM commitments WHERE id=?", (tid,)):
            records.set_project(tid, pid)
            found.append(tid)
    return found


def options() -> list[dict]:
    """Проекты для выбора в окне задачи и в фильтре: сначала в работе, потом остальные."""
    return store.rows("SELECT id, title, status FROM projects ORDER BY status='done', status='paused', title")


def _tasks(pid: int) -> list[dict]:
    return records.list_commitments(status="all", archived=True, project=pid, limit=2000)


def _counted(c: dict) -> bool:
    """Идёт ли задача в прогресс: отменённые, дубли и убранные в архив открытые — нет."""
    return c["status"] != "cancelled" and not (c["status"] == "open" and c["archived"])


def _steps(tasks: list[dict]) -> list[list[dict]]:
    """Открытые задачи по шагам. Шаг задачи — 0, если ей нечего ждать; иначе на один больше самого дальнего
    шага того, что она ждёт внутри проекта (задачи «ждёт» и её подзадачи). Ждёт что-то вне проекта или
    событие — не раньше второго шага."""
    live = {c["id"]: c for c in tasks if c["status"] == "open" and not c["archived"]}
    deps, outside = {}, {}
    for c in live.values():
        inner = {w["task"]["id"] for w in c["waits"] if not w["done"] and w["task"] and w["task"]["id"] in live}
        inner |= {k["id"] for k in c["children"] if k["id"] in live}
        deps[c["id"]] = inner
        outside[c["id"]] = any(not w["done"] and (not w["task"] or w["task"]["id"] not in live) for w in c["waits"])
    depth: dict[int, int] = {}

    def walk(cid: int, path: frozenset) -> int:
        if cid in depth:
            return depth[cid]
        inner = [walk(d, path | {cid}) for d in deps[cid] if d not in path]
        depth[cid] = max([1 if outside[cid] else 0, *(d + 1 for d in inner)])
        return depth[cid]

    for cid in live:
        walk(cid, frozenset())
    steps: list[list[dict]] = [[] for _ in range(max(depth.values(), default=-1) + 1)]
    for cid, d in depth.items():
        steps[d].append(live[cid])
    for step in steps:
        step.sort(key=lambda c: (c["due"] is None, c["due"] or "", c["id"]))
    return [s for s in steps if s]


def _stats(p: dict, tasks: list[dict]) -> dict:
    counted = [c for c in tasks if _counted(c)]
    done = [c for c in counted if c["status"] == "done"]
    live = [c for c in counted if c["status"] == "open"]
    p["total"], p["done"] = len(counted), len(done)
    p["progress"] = sum(c["share"] for c in counted)  # выполненные — целиком, открытые с пунктами — долями
    p["percent"] = round(p["progress"] * 100 / len(counted)) if counted else 0
    p["partial"] = sum(1 for c in counted if c["status"] == "open" and c["items_total"])
    p["open"] = len(live)
    p["blocked"] = sum(1 for c in live if c["blocked"])
    p["overdue"] = sum(1 for c in live if c["overdue"])
    p["at_risk"] = sum(1 for c in live if c["wait_risk"])
    ids = {c["id"] for c in live}
    # на ком стоит проект: открытые задачи, которым нечего ждать, а их ждут другие задачи проекта
    holders = [c for c in live if not c["blocked"] and any(h["task"]["id"] in ids for h in c["holds"])]
    for c in holders:
        c["headline"] = records.headline(c)
    p["stuck_on"] = [{"who": records.doer(c), "task": c, "waiting": [h["task"] for h in c["holds"]
                                                                    if h["task"]["id"] in ids]} for c in holders]
    for s in p["stuck_on"]:
        s["whom"] = "вас" if s["who"] == "Я" else s["who"]  # «стоит на вас» — в текстах для директора
    p["ready"] = [c for c in live if not c["blocked"]]
    due = _as_date(p["due"])
    today = records.today_local()
    p["due_days"] = (due - today).days if due else None
    p["late"] = bool(due and due < today and p["status"] == "active")
    p["status_title"] = STATUSES.get(p["status"], p["status"])
    return p


def get(pid: int) -> dict | None:
    """Проект целиком: цифры прогресса, на ком стоит, задачи по шагам цепочки и выполненные."""
    p = store.row("SELECT * FROM projects WHERE id=?", (pid,))
    if not p:
        return None
    tasks = _tasks(pid)
    _stats(p, tasks)
    p["steps"] = _steps(tasks)
    p["finished"] = sorted((c for c in tasks if c["status"] == "done"), key=lambda c: c.get("closed_at") or "",
                           reverse=True)
    p["parked"] = [c for c in tasks if c["status"] == "open" and c["archived"]]
    p["card"] = store.row("SELECT id, title, url FROM kaiten_cards WHERE id=?", (p["kaiten_card_id"],)) \
        if p["kaiten_card_id"] else None
    return p


def list_projects(status: str = "active") -> list[dict]:
    """Проекты с цифрами для вида «Проекты»: status — active (в работе и на паузе) или all."""
    sql = "SELECT * FROM projects" + ("" if status == "all" else " WHERE status!='done'")
    out = []
    for p in store.rows(sql + " ORDER BY status='paused', due IS NULL, due, title"):
        out.append(_stats(p, _tasks(p["id"])))
    return out
