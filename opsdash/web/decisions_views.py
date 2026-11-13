"""Раздел «Решения» — темы → вопросы → что решено сейчас и история (MEM-3). Логика — opsdash.decisions.

/decisions — «Изменилось за неделю» и темы; ?topic=<тема> — тема: о чём, ждут решения, что решено;
?q=… — поиск, ?f=attention — ждут решения без следующего шага (по всем темам)."""

from __future__ import annotations

from urllib.parse import quote

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from opsdash import decisions, records


def _tsv(name: str, qs: list[dict]) -> str:
    """«Скопировать таблицей»: тема, вопрос, что решено сейчас, статус, чего ждём, с какого числа, сколько раз меняли,
    метрика «было → стало». Вставляется в Google Таблицы и Excel по колонкам."""
    head = ["Тема", "Вопрос", "Сейчас", "Статус", "Чего ждём", "С", "Изменений", "Метрика", "Было", "Стало"]
    out = ["\t".join(head)]
    for q in qs:
        r = q["latest"]
        topic = name + (f" / {q['subtopic']}" if q["subtopic"] else "")
        cells = [topic, q["label"], q["now"], r["stage_title"], q["waiting"], decisions._ddmm(q["since"]),
                 q["changes"] or "", r.get("metric") or "", r.get("before_value") or "", r.get("after_value") or ""]
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
            options = sorted((r for r in decisions.items(topic=topic, current=False)),
                             key=lambda r: (r["when"], r["id"]), reverse=True)
            return render(request, "decision_topic.html", **common, v=view, options=options,
                          tsv=_tsv(topic, view["open"] + view["decided"]), topic=topic)
        if q.strip() or f == "attention":
            rows = (decisions.items(q=q.strip(), current=False) if q.strip() else decisions.items(attention=True))
            groups: dict[str, list[dict]] = {}
            for r in rows:
                groups.setdefault(r["topic"], []).append(r)
            return render(request, "decisions.html", **common, mode="list", groups=groups, q=q, f=f,
                          topics=[], total=len(rows), changed=[], week_days=decisions.WEEK_DAYS)
        return render(request, "decisions.html", **common, mode="topics", topics=decisions.topics(), q="", f="",
                      groups={}, total=0, changed=decisions.changed(), week_days=decisions.WEEK_DAYS)

    @app.post("/decisions/topic-rename")
    def decision_topic_rename(request: Request, old: str = Form(""), new: str = Form(""), about: str = Form(None)):
        """«Тема»: о чём (одна строка) и название — переименовать или влить в другую."""
        name, msg = old, "Сохранено"
        try:
            if about is not None:
                decisions.set_about(old, about)
            if new.strip() and decisions.split_topic(new)[0] != old:
                n = decisions.rename_topic(old, new)
                name, msg = decisions.split_topic(new)[0], f"Тема переименована — решений: {n}"
        except decisions.DecisionError as e:
            return back(request, err=str(e))
        return RedirectResponse(f"/decisions?topic={quote(name)}&msg={quote(msg)}", status_code=303)

    @app.post("/decisions/{did}")
    def decision_save(request: Request, did: int, stage: str = Form(None), metric: str = Form(None),
                      before: str = Form(None), after: str = Form(None), waiting: str = Form(None),
                      project: str = Form(None), prev: str = Form(None), relation: str = Form(None),
                      question: str = Form(None), answer: str = Form(None)):
        try:
            decisions.update(did, stage=stage, metric=metric, before=before, after=after,
                             waiting=waiting, project=project, question=question, answer=answer,
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
        anthology.update(did, status="outdated", note="Устарело — отмечено в разделе «Решения»")
        return back(request, "Решение отмечено устаревшим — из раздела оно ушло, в базе знаний осталось")

    @app.post("/decisions-run")
    def decisions_run(request: Request):
        from opsdash import jobs

        jobs.submit("decisions", "heavy")
        return back(request, "Запущено: помощник сформулирует вопросы и «о чём» тем и свяжет решения — ход в «Офисе»")
