"""Раздел «Память» (фича «Память»): лента фактов, страницы знаний, карта, «От агентов», «Уроки и удачи» (база
знаний), Настройки → Память. Логика — opsdash.memory, opsdash.recall, opsdash.embed.

/memory — лента: что PULSE узнал сегодня и за неделю; ?tab=pages — страницы, /memory/page/<id> — страница;
?tab=map — «Созвездие», «Сущности», «Память в цифрах»; ?tab=agents — что прислали агенты."""

from __future__ import annotations

from datetime import datetime, timedelta
from urllib.parse import quote

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from opsdash import config, memory, store

TABS = [("feed", "Лента"), ("pages", "Страницы"), ("map", "Карта"), ("agents", "От агентов"), ("kb", "Уроки и удачи")]
MONTHS_GEN = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября",
              "декабря"]
SOURCES = {"": "Все источники", "meeting": "Встречи", "messages": "Переписка", "director": "От вас", "agent": "От агентов"}


def _day_title(day: str) -> str:
    today = config.now().date()
    d = datetime.fromisoformat(day).date()
    if d == today:
        return "Сегодня"
    if d == today - timedelta(days=1):
        return "Вчера"
    return f"{d.day} {MONTHS_GEN[d.month - 1]}"


def feed(kind: str = "", source: str = "", q: str = "", status: str = "active", days: int = 7,
         about: str = "") -> list[tuple[str, list[dict]]]:
    """Факты по дням записи — новые сверху."""
    since = (config.now().date() - timedelta(days=days)).isoformat() if days and not q and not about else None
    rows = memory.facts(kind=kind or None, q=q or None, status=status, about=about or None, limit=400)
    if since:
        rows = [f for f in rows if store.local(f["created"], "%Y-%m-%d") >= since]
    if source == "meeting":
        rows = [f for f in rows if f["source_type"] == "meeting"]
    elif source == "messages":
        rows = [f for f in rows if f["source_type"] in memory.MESSAGE_SOURCES]
    elif source in ("director", "agent"):
        rows = [f for f in rows if f["author"] == source]
    rows.sort(key=lambda f: (f["created"] or "", f["id"]), reverse=True)
    groups: list[tuple[str, list[dict]]] = []
    for f in rows:
        day = store.local(f["created"], "%Y-%m-%d")
        if not groups or groups[-1][0] != day:
            groups.append((day, []))
        groups[-1][1].append(f)
    return [(_day_title(d), items) for d, items in groups]


def _messages_for(rows: list[dict]) -> dict[str, dict]:
    ids = [int(f["source_ref"]) for f in rows if f.get("source_type") in memory.MESSAGE_SOURCES
           and str(f.get("source_ref") or "").isdigit()]
    if not ids:
        return {}
    return {str(m["id"]): m for m in store.rows(
        f"SELECT id, chat_title, link, ts FROM messages WHERE id IN ({','.join('?' * len(ids))})", ids)}


def register(app: FastAPI, render, back) -> None:

    @app.get("/memory", response_class=HTMLResponse)
    def memory_page(request: Request, tab: str = "feed", kind: str = "", source: str = "", q: str = "",
                    status: str = "active", days: int = 7, ek: str = "", about: str = ""):
        if tab == "kb":
            return RedirectResponse("/kb", status_code=303)
        tab = tab if tab in dict(TABS) else "feed"
        ctx = dict(nav="memory", tab=tab, tabs=TABS, st=memory.stats(), kinds=memory.FACT_KINDS,
                   ekinds=memory.ENTITY_KINDS, sources=SOURCES, kind=kind, source=source, q=q, status=status,
                   days=days, ek=ek, about=about, backfill=memory.backfill_state())
        if tab == "feed":
            groups = feed(kind, source, q, status, days, about)
            ctx.update(groups=groups, msgs=_messages_for([f for _, g in groups for f in g]),
                       recall=_recall_stats())
        elif tab == "pages":
            ctx.update(pages=memory.page_list(300, q=q or None, kind=ek or None))
        elif tab == "map":
            from opsdash import embed

            ctx.update(model=embed.status(), periods=[("7", "Неделя"), ("30", "Месяц"), ("90", "3 месяца"),
                                                      ("0", "Всё время")],
                       live=memory.page_list(12), recent=_recent_sources(), nums=memory.numbers(),
                       entities=memory.page_list(200), link_kinds=memory.LINK_KINDS)
        elif tab == "agents":
            ctx.update(agents=_agent_facts())
        return render(request, "memory.html", **ctx)

    @app.get("/memory/page/{eid}", response_class=HTMLResponse)
    def memory_entity(request: Request, eid: int):
        d = memory.page_data(eid)
        if not d:
            return RedirectResponse("/memory?tab=pages&err=" + quote("Такой страницы нет"), status_code=303)
        if d["entity"]["id"] != eid:  # влита в другую — к основной
            return RedirectResponse(f"/memory/page/{d['entity']['id']}", status_code=303)
        try:
            memory.write_page(eid)
        except Exception:  # noqa: BLE001 — файл страницы — удобство, не условие
            pass
        others = [r for r in memory.page_list(400) if r["id"] != eid]
        return render(request, "memory_page.html", nav="memory", d=d, kinds=memory.FACT_KINDS,
                      ekinds=memory.ENTITY_KINDS, others=others, msgs=_messages_for(d["facts"]),
                      tab="pages", tabs=TABS)

    @app.post("/memory/fact")
    def memory_add(request: Request, text: str = Form(...), about: str = Form(""), kind: str = Form("state"),
                   key: str = Form("")):
        rep = memory._new_report()
        fid = memory.add_fact(text=text, kind=kind, about=about, key=key or None, by="director", source="manual",
                              report=rep)
        if not fid:
            return back(request, err=memory.report_text(rep))
        return back(request, "Запомнено" + (" — сменил прежний факт" if rep["replaced"] else ""))

    @app.post("/memory/fact/{fid}")
    def memory_fact_set(request: Request, fid: int, status: str = Form(...), reason: str = Form("")):
        try:
            if status == "forget":
                memory.forget_fact(fid)
                return back(request, "Забыто совсем")
            memory.set_status(fid, status, reason)
        except memory.MemoryError as e:
            return back(request, err=str(e))
        return back(request, {"wrong": "Отмечено «Неверно» — факт убран из ответов и страниц",
                              "outdated": "Отмечено «Устарело»", "active": "Факт снова действует"}.get(status, "Сохранено"))

    @app.post("/memory/entity/{eid}")
    def memory_entity_set(request: Request, eid: int, name: str = Form(""), alias: str = Form(""),
                          kind: str = Form(""), into: str = Form(""), action: str = Form("save")):
        try:
            if action == "forget":
                e = memory.entity(eid)
                n = memory.forget_entity(eid)
                return RedirectResponse("/memory?tab=pages&msg=" + quote(f"Забыто всё о «{e['name']}»: фактов {n}"),
                                        status_code=303)
            if action == "merge":
                target = memory.entity(int(into)) if into.isdigit() else memory.entity(into)
                if not target:
                    return back(request, err="Выберите, с какой страницей объединить")
                res = memory.merge_entities(eid, target["id"])
                return RedirectResponse(f"/memory/page/{res['id']}?msg=" + quote(f"«{res['from']}» влита сюда"),
                                        status_code=303)
            if name.strip():
                memory.rename_entity(eid, name)
            if alias.strip():
                memory.add_aliases(eid, [a.strip() for a in alias.split(",") if a.strip()])
            if kind in memory.ENTITY_KINDS:
                store.execute("UPDATE entities SET kind=?, updated=? WHERE id=?", (kind, store.utcnow(), eid))
        except memory.MemoryError as e:
            return back(request, err=str(e))
        return back(request, "Сохранено")

    @app.post("/memory/forget")
    def memory_forget(request: Request, eid: int = Form(...)):
        e = memory.entity(eid)
        if not e:
            return back(request, err="Такой страницы нет")
        n = memory.forget_entity(e["id"])
        return back(request, f"Забыто всё о «{e['name']}»: фактов {n}")

    @app.post("/memory/backfill")
    def memory_backfill(request: Request, action: str = Form(...)):
        from opsdash import jobs

        if action == "stop":
            store.set_state("memory_backfill", "stopped")
            jobs.unqueue("memory-backfill")
            return back(request, "Наполнение остановлено — продолжить можно здесь же")
        store.set_state("memory_backfill", "running")
        jobs.submit("memory-backfill", "heavy")
        return back(request, "Наполнение из разборов запущено: по 10 встреч за раз")

    @app.post("/memory/agents")
    def memory_agents(request: Request, action: str = Form(...), ids: str = Form("")):
        from opsdash import proposals

        chosen = [int(x) for x in ids.split(",") if x.strip().isdigit()]
        done = 0
        for p in proposals.list_proposals("pending"):
            if p["kind"] != "memory.fact" or (chosen and p["id"] not in chosen):
                continue
            (proposals.approve if action == "accept" else proposals.reject)(p["id"])
            done += 1
        return back(request, f"{'Принято' if action == 'accept' else 'Отклонено'}: {done}")

    @app.get("/api/memory/task/{cid}", response_class=HTMLResponse)
    def memory_task(request: Request, cid: int):
        """«Что известно по теме» в окне задачи: факты о людях и сущностях задачи — без запуска Claude."""
        c = store.row("SELECT * FROM commitments WHERE id=?", (cid,))
        if not c:
            return HTMLResponse("")
        eids = memory.entities_for([c["title"] or "", c["what"] or "", c["quote"] or ""],
                                   [n for n in (c["who"], c["whom"]) if n])
        rows = memory.known_about(eids, limit=8)
        if not rows:
            return HTMLResponse('<p class="muted small">Фактов о людях и сущностях этой задачи пока нет.</p>')
        names = {e["id"]: e["name"] for e in store.rows(
            f"SELECT id, name FROM entities WHERE id IN ({','.join('?' * len(eids))})", eids)}
        from html import escape

        items = "".join(
            f'<li>{escape(f["text"])} <span class="muted">· на {memory._ddmm(f["happened"])} · '
            + ", ".join(f'<a href="/memory/page/{e["id"]}">{escape(e["name"])}</a>' for e in f["entities"] if e["id"] in names)
            + (f' · было: {escape(f["prev"]["text"][:60])}' if f.get("prev") else "") + "</span></li>" for f in rows)
        return HTMLResponse(f'<ul class="mem-known">{items}</ul>')

    @app.get("/api/memory/graph")
    def memory_graph(view: str = "facts", days: int = 30, kind: str = "", entity: str = "", source: str = "",
                     limit: int = 300):
        if view == "entities":
            return JSONResponse(memory.entity_graph(kind=kind or None, limit=min(max(limit, 10), 600)))
        return JSONResponse(memory.fact_graph(days=days, kind=kind or None, about=entity or None,
                                              source=source or None, limit=min(max(limit, 10), 2000)))

    @app.get("/api/memory/fact/{fid}")
    def memory_fact_api(fid: int):
        return JSONResponse(memory.fact_card(fid) or {"error": "нет факта"})

    @app.get("/api/memory/entity/{eid}")
    def memory_entity_api(eid: int):
        d = memory.page_data(eid)
        if not d:
            return JSONResponse({"error": "нет страницы"})
        return JSONResponse({"id": d["entity"]["id"], "name": d["entity"]["name"], "kind": d["kind_title"],
                             "aliases": d["entity"]["aliases"], "lead": (d["lead"] or {}).get("lead") or "",
                             "facts": [{"id": f["id"], "text": f["text"], "day": memory._ddmm(f["happened"]),
                                        "kind": f["kind_title"]} for f in d["facts"][:12]],
                             "tasks": [{"id": c["id"], "title": c["title"] or c["what"][:80]} for c in d["tasks"][:6]],
                             "meetings": [{"id": m["id"], "title": m["title"]} for m in d["meetings"][:5]]})

    @app.get("/api/memory/near")
    def memory_near(q: str = "", k: int = 50):
        """Ближайшие по смыслу — для ./ops recall и MCP: модель держит только работающий PULSE (opsdash.embed)."""
        from opsdash import embed

        if not q.strip() or not embed.enabled() or not embed.ready():
            return JSONResponse({"ok": False})
        try:
            return JSONResponse({"ok": True, **embed.near_local(q, min(max(k, 1), 200))})
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": str(e)[:200]})

    @app.post("/settings/memory")
    def memory_settings(request: Request, day: str = Form(""), day_time: str = Form("18:00"),
                        max_facts_meeting: int = Form(15), max_facts_day: int = Form(40),
                        agents: str = Form("confirm"), embeddings: str = Form(""), enabled: str = Form("")):
        config.update_settings("memory", {"enabled": bool(enabled), "day": bool(day), "day_time": day_time.strip(),
                                          "max_facts_meeting": max(1, min(max_facts_meeting, 40)),
                                          "max_facts_day": max(1, min(max_facts_day, 100)),
                                          "embeddings": bool(embeddings)})
        config.update_settings("autonomy", {"memory_agents": "auto" if agents == "auto" else "confirm"})
        return back(request, "Сохранено")

    @app.post("/settings/memory/chats")
    def memory_chats(request: Request, off: list[str] = Form([])):
        store.execute("UPDATE chats SET memory_off=0")
        for key in off:
            src, _, cid = key.partition(":")
            store.execute("UPDATE chats SET memory_off=1 WHERE source=? AND chat_id=?", (src, cid))
        return back(request, f"Не запоминать: {len(off)} " + ("чат" if len(off) == 1 else "чатов"))

    @app.post("/settings/memory/model")
    def memory_model(request: Request, action: str = Form("download")):
        from opsdash import embed, jobs

        if action == "download":
            jobs.submit("memory-model", "light")
            return back(request, "Модель скачивается — около 135 МБ; поиск и связи по смыслу включатся сами")
        embed.remove()
        return back(request, "Модель удалена: поиск — только по словам, связей по смыслу нет")


def _recall_stats() -> dict:
    from opsdash import recall

    return recall.log_stats(7)


def _recent_sources(limit: int = 8) -> list[dict]:
    rows = store.rows("SELECT source_type, source_ref, MAX(created) AS at, COUNT(*) AS n FROM facts "
                      "WHERE status='active' GROUP BY source_type, source_ref ORDER BY at DESC LIMIT ?", (limit,))
    for r in rows:
        r["label"] = memory.source_label({"source_type": r["source_type"], "source_ref": r["source_ref"],
                                          "author": None, "happened": r["at"][:10]})
    return rows


def _agent_facts() -> list[dict]:
    from opsdash import proposals

    out = []
    for p in proposals.list_proposals("pending"):
        if p["kind"] != "memory.fact":
            continue
        f = memory.fact(p["payload"].get("fact_id"))
        if f:
            out.append({"p": p, "f": f})
    return out


def chat_rows() -> list[dict]:
    """Чаты для «Не запоминать» в Настройки → Память: те, что PULSE читает (есть сообщения)."""
    return store.rows("SELECT c.source, c.chat_id, c.title, c.kind, COALESCE(c.memory_off, 0) AS off, "
                      "(SELECT COUNT(*) FROM messages m WHERE m.source=c.source AND m.chat_id=c.chat_id) AS n "
                      "FROM chats c WHERE c.enabled=1 OR EXISTS(SELECT 1 FROM messages m WHERE m.source=c.source "
                      "AND m.chat_id=c.chat_id) ORDER BY off DESC, c.last_ts DESC LIMIT 300")
