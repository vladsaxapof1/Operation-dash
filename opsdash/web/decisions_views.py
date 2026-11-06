"""Раздел «Решения» — реестр решений (MEM-3). Логика — opsdash.decisions."""

from __future__ import annotations

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse

from opsdash import decisions, records


def register(app: FastAPI, render, back) -> None:

    @app.get("/decisions", response_class=HTMLResponse)
    def decisions_page(request: Request, stage: str = "", project: str = "", q: str = "", orphan: str = ""):
        rows = decisions.items(stage=stage if stage in decisions.STAGES else None, project=project or None,
                               q=q.strip() or None, orphan=bool(orphan))
        groups: dict[str, list[dict]] = {}
        for r in rows:  # по направлениям: самые свежие направления — выше
            groups.setdefault(r.get("project") or "Без направления", []).append(r)
        meetings = {m["id"]: m["title"] for m in records.list_meetings(limit=500)}
        return render(request, "decisions.html", nav="decisions", groups=groups, total=len(rows),
                      summary=decisions.summary(), stages=decisions.STAGES, f_stage=stage, f_project=project, q=q,
                      f_orphan=bool(orphan), meetings=meetings, orphan_days=decisions.ORPHAN_DAYS)

    @app.post("/decisions/{did}")
    def decision_save(request: Request, did: int, stage: str = Form(None), metric: str = Form(None),
                      before: str = Form(None), after: str = Form(None), waiting: str = Form(None)):
        try:
            decisions.update(did, stage=stage, metric=metric, before=before, after=after, waiting=waiting)
        except decisions.DecisionError as e:
            return back(request, err=str(e))
        msg = f"Стадия: {decisions.STAGES[stage]}" if stage and metric is None else "Сохранено"
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
        anthology.update(did, status="outdated", note="Устарело — отметила директор в реестре решений")
        return back(request, "Решение отмечено устаревшим — из реестра оно ушло, в базе знаний осталось")

    @app.post("/decisions-run")
    def decisions_run(request: Request):
        from opsdash import jobs

        jobs.submit("decisions", "heavy")
        return back(request, "Запустила: помощник свяжет решения с задачами и заполнит «было → стало» — ход в «Офисе»")
