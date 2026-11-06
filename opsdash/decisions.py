"""Решения по темам (MEM-3, 2.0.21): по каждой теме — что сейчас действует и как к этому пришли.

Решения — записи базы знаний вида decision (opsdash.anthology): их пишет разбор встреч (`./ops kb add`) и импорт
заметок-решений из документов. Здесь к ним добавлено:

- **тема** — первая часть поля project до « / » («KPI», «Логистика»), остальное — подтема («Склад»);
- **цепочка** — решение ссылается на прежнее (prev_id) и говорит, как с ним связано (relation): меняет, уточняет,
  продолжает или отменяет. Прежнее решение, которое заменили или отменили, больше не действует, но остаётся в
  цепочке — видно, как к нынешнему пришли;
- **статус** — принято или ждёт решения (открытый вопрос), **следующий шаг / чего ждём** (waiting_for);
- метрика «было → стало» и задачи, которые решение исполняют (commitments.decision_id).

Сигналы для директора (сводка → дайджест): ждёт решения, а следующего шага нет; решение поменяли или отменили
за последние дни — что на что.
Раздел «Решения» работает, даже когда «База знаний» в архиве.
"""

from __future__ import annotations

import re
from datetime import date, timedelta

from opsdash import config, records, store

STAGES = {"discussing": "ждёт решения", "decided": "принято"}
RELATIONS = {"changes": "меняет", "refines": "уточняет", "follows": "продолжает", "cancels": "отменяет"}
# что стало с прежним решением — для подписи в цепочке
RELATIONS_PAST = {"changes": "заменено", "refines": "уточнено", "follows": "продолжено", "cancels": "отменено"}
SUPERSEDING = {"changes", "cancels"}  # после них прежнее решение не действует
CHANGED_DAYS = 3  # решение поменяли или отменили за эти дни — в сводке для дайджеста
NO_TOPIC = "Без темы"
# стадии 2.0.20: «ждёт» — это «принято» + «чего ждём», «внедрено» — «принято»
LEGACY_STAGES = {"waiting": "decided", "implemented": "decided"}


class DecisionError(Exception):
    pass


def split_topic(project: str | None) -> tuple[str, str]:
    """«Планирование 2027 / Логистика / Склад» → («Планирование 2027», «Логистика / Склад»)."""
    parts = [p.strip() for p in (project or "").split(" / ") if p.strip()]
    return (parts[0], " / ".join(parts[1:])) if parts else (NO_TOPIC, "")


def _short(d: dict) -> dict:
    return {"id": d["id"], "title": d["title"], "when": d["when"], "topic": d["topic"]}


def _load() -> list[dict]:
    """Все актуальные решения (устаревшие и отклонённые — нет) с задачами, темой, цепочкой и сигналами."""
    rows = store.rows("SELECT * FROM anthology WHERE kind='decision' AND status='accepted' "
                      "ORDER BY COALESCE(happened, substr(created, 1, 10)) DESC, id DESC")
    if not rows:
        return rows
    ids = [r["id"] for r in rows]
    tasks: dict[int, list[dict]] = {}
    for c in store.rows(f"SELECT * FROM commitments WHERE decision_id IN ({','.join('?' * len(ids))}) "
                        "AND status != 'cancelled' ORDER BY status='done', due IS NULL, due, id", ids):
        tasks.setdefault(c["decision_id"], []).append(
            {"id": c["id"], "title": records.headline(c), "status": c["status"], "who": c.get("who") or "",
             "due": c.get("due") or "", "direction": c["direction"]})
    by_id = {r["id"]: r for r in rows}
    for r in rows:
        stage = r.get("stage") or "decided"
        r["stage"] = LEGACY_STAGES.get(stage, stage) if stage in STAGES or stage in LEGACY_STAGES else "decided"
        r["stage_title"] = STAGES[r["stage"]]
        r["people"] = store.loads(r.get("people"), [])
        r["tasks"] = tasks.get(r["id"], [])
        r["open_tasks"] = sum(t["status"] == "open" for t in r["tasks"])
        r["when"] = r.get("happened") or (r.get("created") or "")[:10]
        r["topic"], r["subtopic"] = split_topic(r.get("project"))
        r["next"] = []
    for r in rows:  # прежнее решение — только среди актуальных; ссылка на устаревшее не рвёт показ
        prev = by_id.get(r.get("prev_id") or 0)
        rel = r.get("relation") if r.get("relation") in RELATIONS else "refines"
        r["relation"] = rel if prev else None
        r["relation_title"] = RELATIONS[rel] if prev else ""
        r["prev"] = _short(prev) if prev else None
        if prev:
            prev["next"].append({**_short(r), "relation": rel, "relation_title": RELATIONS[rel]})
    for r in rows:
        r["next"].sort(key=lambda x: (x["when"], x["id"]))
        stop = next((n for n in r["next"] if n["relation"] in SUPERSEDING), None)
        r["superseded_by"] = stop
        r["current"] = stop is None
        r["state_title"] = (f"{RELATIONS_PAST[stop['relation']]} {_ddmm(stop['when'])}" if stop else r["stage_title"])
        r["no_step"] = r["current"] and r["stage"] == "discussing" and not (r.get("waiting_for") or "").strip()
        r["attention"] = r["no_step"]
    return rows


def _ddmm(iso: str) -> str:
    return f"{iso[8:10]}.{iso[5:7]}" if iso and len(iso) >= 10 else iso or ""


def items(*, stage: str | None = None, project: str | None = None, topic: str | None = None, q: str | None = None,
          meeting: str | None = None, attention: bool = False, current: bool = True, limit: int = 500) -> list[dict]:
    """Решения, новые сверху. По умолчанию — только действующие (не заменённые и не отменённые);
    current=False — с историей; meeting — решения одной встречи."""
    rows = _load()
    if q:
        from opsdash import anthology

        found = {r["id"] for r in anthology.items(kind="decision", status="accepted", q=q, limit=limit)}
        rows = [r for r in rows if r["id"] in found]
    if current:
        rows = [r for r in rows if r["current"]]
    if stage:
        stage = LEGACY_STAGES.get(stage, stage)
        rows = [r for r in rows if r["stage"] == stage]
    if project:
        rows = [r for r in rows if (r.get("project") or "") == project]
    if topic:
        rows = [r for r in rows if r["topic"] == topic]
    if meeting:
        rows = [r for r in rows if r.get("source_type") == "meeting" and r.get("source_ref") == meeting]
    if attention:
        rows = [r for r in rows if r["attention"]]
    return rows[:limit]


def get(did: int) -> dict | None:
    return next((r for r in _load() if r["id"] == did), None)


def _chains(rows: list[dict]) -> list[list[dict]]:
    """Цепочки — связные группы по prev_id, внутри по дате; свежие цепочки выше. Одиночные решения — не цепочки."""
    parent = {r["id"]: r["id"] for r in rows}

    def root(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for r in rows:
        if r["prev"] and r["prev"]["id"] in parent:
            parent[root(r["id"])] = root(r["prev"]["id"])
    groups: dict[int, list[dict]] = {}
    for r in rows:
        groups.setdefault(root(r["id"]), []).append(r)
    chains = [sorted(g, key=lambda r: (r["when"], r["id"])) for g in groups.values() if len(g) > 1]
    return sorted(chains, key=lambda c: (c[-1]["when"], c[-1]["id"]), reverse=True)


def _counts(rows: list[dict]) -> dict:
    cur = [r for r in rows if r["current"]]
    return {"current": len(cur), "total": len(rows),
            "open": sum(r["stage"] == "discussing" for r in cur),
            "decided": sum(r["stage"] == "decided" for r in cur),
            "no_step": sum(r["no_step"] for r in cur),
            "attention": sum(r["attention"] for r in cur),
            "with_metric": sum(bool(r.get("metric") or r.get("after_value")) for r in cur)}


def topics() -> list[dict]:
    """Темы: сколько решений действует, ждут решения, сколько заменено, цепочки; свежие темы выше."""
    rows = _load()
    by_topic: dict[str, list[dict]] = {}
    for r in rows:
        by_topic.setdefault(r["topic"], []).append(r)
    out = []
    for name, rs in by_topic.items():
        last = max(rs, key=lambda r: (r["when"], r["id"]))
        out.append({"name": name, **_counts(rs), "chains": len(_chains(rs)), "last": last,
                    "subtopics": sorted({r["subtopic"] for r in rs if r["subtopic"]})})
    out.sort(key=lambda t: (t["last"]["when"], t["last"]["id"]), reverse=True)
    return sorted(out, key=lambda t: t["name"] == NO_TOPIC)  # «Без темы» — в конце


def topic_view(name: str) -> dict | None:
    """Страница темы: «Сейчас по теме» (действующие: сначала ждут решения, потом принятые — свежие выше) и цепочки.
    В цепочку попадают и решения других тем, если они связаны с решениями этой."""
    rows = _load()
    mine = [r for r in rows if r["topic"] == name]
    if not mine:
        return None
    ids = {r["id"] for r in mine}
    linked = [r for r in rows if r["id"] not in ids and (
        (r["prev"] and r["prev"]["id"] in ids) or any(n["id"] in ids for n in r["next"]))]
    now = sorted((r for r in mine if r["current"]), key=lambda r: (r["when"], r["id"]), reverse=True)
    now.sort(key=lambda r: r["stage"] != "discussing")  # открытые вопросы — сверху
    return {"name": name, "now": now, "chains": _chains(mine + linked), "counts": _counts(mine),
            "subtopics": sorted({r["subtopic"] for r in mine if r["subtopic"]}),
            "history": sorted((r for r in mine if not r["current"]), key=lambda r: (r["when"], r["id"]), reverse=True)}


def summary() -> dict:
    """Для шапки раздела, меню и сводки: темы, действующие решения, ждут решения, без шага."""
    rows = _load()
    c = _counts(rows)
    return {**c, "topics": len({r["topic"] for r in rows}),
            "projects": sorted({r["project"] for r in rows if r.get("project")}),
            "topic_names": sorted({r["topic"] for r in rows if r["topic"] != NO_TOPIC})}


def update(did: int, *, stage: str | None = None, metric: str | None = None,
           before: str | None = None, after: str | None = None, waiting: str | None = None,
           project: str | None = None, prev: int | None = None, relation: str | None = None) -> None:
    """Поля решения. Пустая строка — очистить поле, None — не трогать; prev=0 — убрать связь с прежним."""
    d = get(did)
    if not d:
        raise DecisionError(f"Нет решения [{did}]")
    sets: dict = {}
    if stage is not None:
        stage = LEGACY_STAGES.get(stage, stage)
        if stage not in STAGES:
            raise DecisionError(f"Статус — один из: {', '.join(STAGES)}")
        sets["stage"], sets["stage_at"] = stage, store.utcnow()
    for col, val in (("metric", metric), ("before_value", before), ("after_value", after), ("waiting_for", waiting)):
        if val is not None:
            sets[col] = " ".join(val.split())[:300] or None
    if project is not None:
        sets["project"] = " / ".join(p.strip() for p in project.split("/") if p.strip())[:120] or None
    if relation is not None and relation not in RELATIONS:
        raise DecisionError(f"Связь с прежним — одна из: {', '.join(RELATIONS)}")
    if prev is not None:
        if prev:
            _check_prev(did, prev)
            sets["prev_id"], sets["relation"] = prev, relation or d.get("relation") or "refines"
        else:
            sets["prev_id"], sets["relation"] = None, None
    elif relation is not None:
        if not d["prev"]:
            raise DecisionError("Сначала укажите прежнее решение (--prev)")
        sets["relation"] = relation
    if sets:
        store.execute(f"UPDATE anthology SET {', '.join(f'{k}=?' for k in sets)}, updated=? WHERE id=?",
                      [*sets.values(), store.utcnow(), did])
        if "project" in sets:
            from opsdash import anthology

            with store.db() as conn:
                anthology._fts(conn, did)


def _check_prev(did: int, prev: int) -> None:
    if prev == did:
        raise DecisionError("Решение не может ссылаться само на себя")
    p = store.row("SELECT id, prev_id FROM anthology WHERE id=? AND kind='decision'", (prev,))
    if not p:
        raise DecisionError(f"Нет решения [{prev}] (./ops decisions list --all)")
    seen = set()
    while p and p["prev_id"] and p["id"] not in seen:  # не замыкаем цепочку в круг
        if p["prev_id"] == did:
            raise DecisionError(f"Решение [{prev}] само идёт после [{did}] — связь замкнула бы цепочку в круг")
        seen.add(p["id"])
        p = store.row("SELECT id, prev_id FROM anthology WHERE id=?", (p["prev_id"],))


def rename_topic(old: str, new: str) -> int:
    """Переименовать тему или влить её в другую: подтемы сохраняются. Возвращает, сколько решений перенесено."""
    new = " / ".join(p.strip() for p in (new or "").split("/") if p.strip())
    if not new:
        raise DecisionError("Новое название темы — одной строкой")
    n = 0
    for r in _load():
        if r["topic"] == old:
            update(r["id"], project=f"{new} / {r['subtopic']}" if r["subtopic"] else new)
            n += 1
    if not n:
        raise DecisionError(f"Нет темы «{old}» (./ops decisions topics)")
    return n


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
    """«Создать задачу» у решения: ваша задача (или «мне должны», если указан кто), сразу связанная с решением."""
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


def superseded() -> dict[int, dict]:
    """Решения, которые заменили или отменили: id → что их сменило. Для `./ops kb` — чтобы помощник не опирался
    на прежнее решение как на действующее."""
    return {r["id"]: r["superseded_by"] for r in _load() if r["superseded_by"]}


def attention_lines(limit: int = 6) -> list[str]:
    """Для ./ops overview (а через неё — для дайджеста): ждёт решения, а следующего шага нет. Самые давние сверху."""
    rows = sorted(items(attention=True), key=lambda r: r["when"])[:limit]
    return [f"[decision {r['id']}] {r['when']} · {r['topic']} · {r['title']} — ждёт решения, следующего шага нет"
            for r in rows]


def changed_lines(days: int = CHANGED_DAYS, limit: int = 6) -> list[str]:
    """Для ./ops overview: решения, которые за последние дни поменяли или отменили, — что на что."""
    since = (date.today() - timedelta(days=days)).isoformat()
    rows = [r for r in items() if r["prev"] and r["relation"] in SUPERSEDING and r["when"] >= since]
    return [f"[decision {r['id']}] {r['when']} · {r['topic']} · «{r['title']}» — {r['relation_title']} решение от "
            f"{r['prev']['when']} «{r['prev']['title']}»" for r in rows[:limit]]


def ref(r: dict) -> str:
    """Ссылка на решение в его теме — для разбора встречи и блока «Решения встречи»."""
    from urllib.parse import quote

    return f"/decisions?topic={quote(r['topic'])}#d{r['id']}"


def ref_label(r: dict) -> str:
    """Подпись ссылки: что с решением сейчас и как оно связано с прежним."""
    if r["superseded_by"]:
        return f"{r['state_title']} →"
    if r["prev"]:
        return f"{r['relation_title']} решение от {_ddmm(r['prev']['when'])} →"
    return "решение →"


def link_summary(text: str, rows: list[dict]) -> str:
    """Ссылки из разбора встречи на решения: у пунктов раздела «Решения» — «решение →», «меняет решение от 27.10 →»
    или «заменено 30.10 →». Пункт и решение сопоставляем по смыслу (как задачи — records.link_summary), поэтому
    работает и для старых разборов."""
    if not text or not rows:
        return text
    for r in rows:
        r["_stems"] = records._stems(f"{r['title']} {r.get('details') or ''}")
    lines, section, items = text.split("\n"), "", []
    for n, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("#"):
            section = stripped.lstrip("#").strip().lower()
        elif section.startswith("решени") and re.match(r"([-*]|\d+\.)\s", line):  # пункты первого уровня
            items.append((n, records._stems(stripped), set()))
    for n, r in records._match(items, rows).items():
        lines[n] = f"{lines[n].rstrip()} [{ref_label(r)}]({ref(r)})"
    return "\n".join(lines)


def topic_text(name: str) -> str:
    """Тема текстом — для `./ops decisions topic` и копирования: что сейчас действует и цепочки."""
    v = topic_view(name)
    if not v:
        return f"Нет темы «{name}»"
    out = [f"# {name}", "", "## Сейчас по теме"]
    for r in v["now"]:
        metric = (f" · {r.get('metric') or 'метрика'}: {r.get('before_value') or '—'} → {r.get('after_value') or '—'}"
                  if r.get("metric") or r.get("before_value") or r.get("after_value") else "")
        step = f" · следующий шаг: {r['waiting_for']}" if r.get("waiting_for") else ""
        prev = f" · {r['relation_title']} решение от {_ddmm(r['prev']['when'])}" if r["prev"] else ""
        out.append(f"- [decision {r['id']}] {_ddmm(r['when'])} · {r['stage_title']} · {r['title']}{metric}{step}{prev}")
    if v["chains"]:
        out += ["", "## Как к этому пришли"]
        for i, chain in enumerate(v["chains"], 1):
            out.append(f"Цепочка {i}:")
            for r in chain:
                rel = f"{r['relation_title']} [{r['prev']['id']}] → " if r["prev"] else ""
                state = f" ({r['state_title']})" if not r["current"] else ""
                other = f" · тема «{r['topic']}»" if r["topic"] != name else ""
                out.append(f"  {_ddmm(r['when'])} {rel}[decision {r['id']}] {r['title']}{state}{other}")
    return "\n".join(out)
