"""Реестр решений (беклог MEM-3): направление — что решили — метрика «было → стало» — стадия — чем исполняется —
чего ждём. Сигнал «решение без исполнения»: принято больше недели назад, а ни одной задачи под него нет.

Решения — записи базы знаний вида decision (opsdash.anthology): их пишет разбор встреч (`./ops kb add`) и импорт
заметок-решений из документов. Здесь к ним добавлены стадия, метрика, «чего ждём» и связь с задачами
(commitments.decision_id). Раздел «Решения» работает, даже когда «База знаний» в архиве.
"""

from __future__ import annotations

from datetime import date, timedelta

from opsdash import config, records, store

STAGES = {"discussing": "обсуждается", "decided": "принято", "waiting": "ждёт", "implemented": "внедрено"}
ORPHAN_DAYS = 7  # принято раньше — и до сих пор ни одной задачи: «решение без исполнения»


class DecisionError(Exception):
    pass


def _stage(r: dict) -> str:
    return r.get("stage") or "decided"


def _decorate(rows: list[dict]) -> list[dict]:
    if not rows:
        return rows
    ids = [r["id"] for r in rows]
    tasks: dict[int, list[dict]] = {}
    for c in store.rows(f"SELECT * FROM commitments WHERE decision_id IN ({','.join('?' * len(ids))}) "
                        "AND status != 'cancelled' ORDER BY status='done', due IS NULL, due, id", ids):
        tasks.setdefault(c["decision_id"], []).append(
            {"id": c["id"], "title": records.headline(c), "status": c["status"], "who": c.get("who") or "",
             "due": c.get("due") or "", "direction": c["direction"]})
    cutoff = (date.today() - timedelta(days=ORPHAN_DAYS)).isoformat()
    for r in rows:
        r["stage"] = _stage(r)
        r["stage_title"] = STAGES[r["stage"]]
        r["people"] = store.loads(r.get("people"), [])
        r["tasks"] = tasks.get(r["id"], [])
        r["open_tasks"] = sum(t["status"] == "open" for t in r["tasks"])
        when = r.get("happened") or (r.get("created") or "")[:10]
        r["when"] = when
        r["orphan"] = r["stage"] == "decided" and not r["tasks"] and bool(when) and when <= cutoff
        # все задачи закрыты, а решение всё ещё «принято» — подсказка «отметить «внедрено»?»
        r["all_done"] = r["stage"] == "decided" and bool(r["tasks"]) and not r["open_tasks"]
    return rows


def items(*, stage: str | None = None, project: str | None = None, q: str | None = None, orphan: bool = False,
          limit: int = 500) -> list[dict]:
    """Актуальные решения (устаревшие и отклонённые — нет), новые сверху; stage — одна стадия, orphan — только
    «без исполнения»."""
    if q:
        from opsdash import anthology

        ids = [r["id"] for r in anthology.items(kind="decision", status="accepted", q=q, limit=limit)]
        if not ids:
            return []
        rows = store.rows(f"SELECT * FROM anthology WHERE id IN ({','.join('?' * len(ids))})", ids)
        rows.sort(key=lambda r: ids.index(r["id"]))
    else:
        sql, params = "SELECT * FROM anthology WHERE kind='decision' AND status='accepted'", []
        if project:
            sql += " AND project=?"
            params.append(project)
        sql += " ORDER BY COALESCE(happened, substr(created, 1, 10)) DESC, id DESC LIMIT ?"
        params.append(limit)
        rows = store.rows(sql, params)
    rows = _decorate(rows)
    if stage:
        rows = [r for r in rows if r["stage"] == stage]
    if orphan:
        rows = [r for r in rows if r["orphan"]]
    return rows


def get(did: int) -> dict | None:
    r = store.row("SELECT * FROM anthology WHERE id=? AND kind='decision'", (did,))
    return _decorate([r])[0] if r else None


def summary() -> dict:
    """Сколько решений на каждой стадии и сколько «без исполнения» — для шапки реестра, меню и сводки."""
    rows = items()
    out = {k: 0 for k in STAGES}
    for r in rows:
        out[r["stage"]] += 1
    return {"total": len(rows), "stages": out, "orphans": sum(r["orphan"] for r in rows),
            "all_done": sum(r["all_done"] for r in rows),
            "projects": sorted({r["project"] for r in rows if r.get("project")})}


def update(did: int, *, stage: str | None = None, metric: str | None = None, before: str | None = None,
           after: str | None = None, waiting: str | None = None) -> None:
    """Поля реестра. Пустая строка — очистить поле, None — не трогать."""
    if not get(did):
        raise DecisionError(f"Нет решения [{did}]")
    sets: dict = {}
    if stage is not None:
        if stage not in STAGES:
            raise DecisionError(f"Стадия — одна из: {', '.join(STAGES)}")
        sets["stage"], sets["stage_at"] = stage, store.utcnow()
    for col, val in (("metric", metric), ("before_value", before), ("after_value", after), ("waiting_for", waiting)):
        if val is not None:
            sets[col] = " ".join(val.split())[:300] or None
    if sets:
        store.execute(f"UPDATE anthology SET {', '.join(f'{k}=?' for k in sets)}, updated=? WHERE id=?",
                      [*sets.values(), store.utcnow(), did])


def link(did: int, task_id: int) -> None:
    """Задача исполняет решение. У задачи одно решение: прежняя связь заменяется."""
    if not get(did):
        raise DecisionError(f"Нет решения [{did}]")
    if not store.row("SELECT 1 FROM commitments WHERE id=?", (task_id,)):
        raise DecisionError(f"Нет задачи [{task_id}] (./ops commitments list)")
    store.execute("UPDATE commitments SET decision_id=?, updated=? WHERE id=?", (did, store.utcnow(), task_id))


def unlink(task_id: int) -> None:
    store.execute("UPDATE commitments SET decision_id=NULL, updated=? WHERE id=?", (store.utcnow(), task_id))


def add_task(did: int, what: str, who: str | None = None, due: str | None = None) -> int:
    """«Создать задачу» у решения без исполнения: ваша задача, сразу связанная с решением."""
    d = get(did)
    if not d:
        raise DecisionError(f"Нет решения [{did}]")
    what = " ".join((what or "").split())
    if not what:
        raise DecisionError("Что сделать — одной строкой")
    owner = (config.load_settings()["owner"].get("name") or "").strip() or None
    cid = records.add_commitment(direction="mine" if not who else "to_me", what=what, who=who or owner, whom=owner,
                                 due=due or None, source_type="manual",
                                 note=f"Исполняет решение «{d['title']}»")
    link(did, cid)
    return cid


def orphan_lines(limit: int = 5) -> list[str]:
    """Для ./ops overview (а через неё — для дайджеста): решения без исполнения, самые давние сверху."""
    rows = sorted(items(orphan=True), key=lambda r: r["when"])[:limit]
    return [f"[decision {r['id']}] {r['when']} · {r.get('project') or 'без направления'} · {r['title']}" for r in rows]
