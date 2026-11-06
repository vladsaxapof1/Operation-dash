"""Раздел «Решения» — решения по темам и цепочки (MEM-3). Логика — opsdash.decisions.

/decisions — темы; ?topic=<тема> — страница темы: ждут решения, цепочки «как к этому пришли», отдельные решения;
?q=… — поиск, ?f=attention — что требует внимания (по всем темам)."""

from __future__ import annotations

from urllib.parse import quote

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from opsdash import decisions, records


def _tsv(rows: list[dict]) -> str:
    """«Скопировать таблицей»: тема, метрика, решение, было, стало, статус, следующий шаг, дата, что поменяло.
    Вставляется в Google Таблицы и Excel по колонкам."""
    head = ["Тема", "Метрика", "Решение", "Было", "Стало", "Статус", "Следующий шаг / чего ждём", "Дата",
            "Связь с прежним"]
    out = ["\t".join(head)]
    for r in rows:
        topic = r["topic"] + (f" / {r['subtopic']}" if r["subtopic"] else "")
        cells = [topic, r.get("metric") or "", r["title"], r.get("before_value") or "", r.get("after_value") or "",
                 r["stage_title"], r.get("waiting_for") or "", decisions._ddmm(r["when"]),
                 f"{r['relation_title']} решение от {decisions._ddmm(r['prev']['when'])} «{r['prev']['title']}»"
                 if r["prev"] else ""]
        out.append("\t".join(" ".join(str(c).split()) for c in cells))
    return "\n".join(out)


def register(app: FastAPI, render, back) -> None:

    @app.get("/decisions", response_class=HTMLResponse)
    def decisions_page(request: Request, topic: str = "", q: str = "", f: str = ""):
        meetings = {m["id"]: m["title"] for m in records.list_meetings(limit=500)}
        common = dict(nav="decisions", summary=decisions.summary(), stages=decisions.STAGES,
                      relations=decisions.RELATIONS, meetings=meetings)
        if topic:
            view = decisions.topic_view(topic)
            if not view:  # тему переименовали или её последнее решение устарело — к списку тем (не back: зациклится)
                return RedirectResponse(f"/decisions?err={quote(f'Нет темы «{topic}» — её переименовали или решений в ней не осталось')}",
                                        status_code=303)
            chained = {r["id"] for chain in view["chains"] for r in chain}
            single = [r for r in view["now"] if r["id"] not in chained and r["stage"] == "decided"]
            options = sorted((r for r in decisions.items(topic=topic, current=False)),
                             key=lambda r: (r["when"], r["id"]), reverse=True)
            return render(request, "decision_topic.html", **common, v=view, single=single, options=options,
                          tsv=_tsv(view["now"]), topic=topic)
        if q.strip() or f == "attention":
            rows = (decisions.items(q=q.strip(), current=False) if q.strip() else decisions.items(attention=True))
            groups: dict[str, list[dict]] = {}
            for r in rows:
                groups.setdefault(r["topic"], []).append(r)
            return render(request, "decisions.html", **common, mode="list", groups=groups, q=q, f=f,
                          topics=[], total=len(rows))
        return render(request, "decisions.html", **common, mode="topics", topics=decisions.topics(), q="", f="",
                      groups={}, total=0)

    @app.post("/decisions/topic-rename")
    def decision_topic_rename(request: Request, old: str = Form(""), new: str = Form("")):
        try:
            n = decisions.rename_topic(old, new)
        except decisions.DecisionError as e:
            return back(request, err=str(e))
        name = decisions.split_topic(new)[0]
        return RedirectResponse(f"/decisions?topic={quote(name)}&msg={quote(f'Тема переименована — решений: {n}')}",
                                status_code=303)

    @app.post("/decisions/{did}")
    def decision_save(request: Request, did: int, stage: str = Form(None), metric: str = Form(None), before: str = Form(None), after: str = Form(None),
                      waiting: str = Form(None), project: str = Form(None), prev: str = Form(None),
                      relation: str = Form(None)):
        try:
            decisions.update(did, stage=stage, metric=metric, before=before, after=after,
                             waiting=waiting, project=project,
                             prev=None if prev is None else int(prev or 0), relation=relation or None)
        except (decisions.DecisionError, ValueError) as e:
            return back(request, err=str(e))
        if stage and metric is None:
            msg = f"Статус: {decisions.STAGES.get(decisions.LEGACY_STAGES.get(stage, stage), stage)}"
        else:
            msg = "Сохранено"
        return back(request, msg)

    @app.post("/decisions/{did}/link")
    def decision_link(request: Request, did: int, task: str = Form("")):
        tid = records.parse_task_ref(task)
        if not tid:
            return back(request, err="Выберите задачу из подсказок — «#номер · название»")
        try:
            decisions.link(did, tid)
        except decisions.DecisionError as e:
            return back(request, err=str(e))
        return back(request, "Задача связана с решением")

    @app.post("/decisions/unlink/{task_id}")
    def decision_unlink(request: Request, task_id: int):
        decisions.unlink(task_id)
        return back(request, "Связь убрана")

    @app.post("/decisions/{did}/task")
    def decision_task(request: Request, did: int, what: str = Form(""), who: str = Form(""), due: str = Form("")):
        try:
            cid = decisions.add_task(did, what, who.strip() or None, due.strip() or None)
        except (decisions.DecisionError, ValueError) as e:
            return back(request, err=str(e))
        return back(request, f"Задача создана и связана с решением — #{cid}")

    @app.post("/decisions/{did}/outdated")
    def decision_outdated(request: Request, did: int):
        from opsdash import anthology

        if not decisions.get(did):
            return back(request, err="Нет такого решения")
        anthology.update(did, status="outdated", note="Устарело — отметила директор в разделе «Решения»")
        return back(request, "Решение отмечено устаревшим — из раздела оно ушло, в базе знаний осталось")

    @app.post("/decisions-run")
    def decisions_run(request: Request):
        from opsdash import jobs

        jobs.submit("decisions", "heavy")
        return back(request, "Запустила: помощник соберёт цепочки решений по темам — ход в «Офисе»")
