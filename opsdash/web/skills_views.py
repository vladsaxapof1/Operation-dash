"""Раздел «Навыки» (SK-1 – SK-4) и «Нет» с причиной (MOD-3). Логика — opsdash.skills."""

from __future__ import annotations

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse

from opsdash import config, skills

AREA_ICONS = {"messages": "chats", "meetings": "meetings", "days": "digests", "tasks": "commitments", "help": "ask",
              "knowledge": "kb", "learning": "skills"}


def register(app: FastAPI, render, back) -> None:

    @app.get("/skills", response_class=HTMLResponse)
    def skills_page(request: Request):
        return render(request, "skills.html", nav="skills", cat=skills.catalog(), area_icons=AREA_ICONS)

    @app.get("/skills/{sid}", response_class=HTMLResponse)
    def skill_page(request: Request, sid: str):
        settings = config.load_settings()
        cat = skills.catalog(settings)
        sk = next((s for a in cat["areas"] for s in a["skills"] if s["id"] == sid), None)
        if not sk:
            return back(request, err="Нет такого навыка", to="/skills")
        meta, body = skills.split_text(sid)
        return render(request, "skill.html", nav="skills", sk=sk, cat=cat, area_icons=AREA_ICONS,
                      rules=skills.rules(sid), runs=skills.runs(sid), text=body, meta=meta)

    @app.post("/skills/{sid}/rules")
    def skill_rule_add(request: Request, sid: str, text: str = Form("")):
        try:
            skills.add_rule(sid, text)
        except ValueError as e:
            return back(request, err=str(e), to=f"/skills/{sid}#rules")
        return back(request, "Правило добавлено — действует со следующего запуска", to=f"/skills/{sid}#rules")

    @app.post("/skills/rules/{rule_id}")
    def skill_rule_edit(request: Request, rule_id: int, action: str = Form(...), text: str = Form("")):
        from opsdash import store

        rule = store.row("SELECT * FROM skill_rules WHERE id=?", (rule_id,))
        if not rule:
            return back(request, err="Нет такого правила")
        to = f"/skills/{rule['skill']}#rules"
        if action == "delete":
            skills.delete_rule(rule_id)
            return back(request, "Правило удалено", to=to)
        if action in ("on", "off"):
            skills.set_rule(rule_id, enabled=action == "on")
            return back(request, "Правило включено" if action == "on" else "Правило выключено", to=to)
        if action == "edit":
            skills.set_rule(rule_id, text_=text)
            return back(request, "Сохранено", to=to)
        return back(request, err="Неизвестное действие", to=to)

    @app.post("/skills/{sid}/run")
    def skill_run(request: Request, sid: str):
        from opsdash import jobs

        sk = skills.get(sid)
        if not sk or not sk.get("run"):
            return back(request, err="Этот навык вручную не запускается", to=f"/skills/{sid}")
        jobs.submit(sk["run"]["job"], "heavy")
        return back(request, f"Запустила «{sk['title']}» — ход виден в «Офисе»", to=f"/skills/{sid}")

    @app.post("/skills/{sid}/restore")
    def skill_restore(request: Request, sid: str):
        try:
            skills.restore(sid)
        except (ValueError, RuntimeError) as e:
            return back(request, err=f"Не получилось вернуть: {e}", to=f"/skills/{sid}")
        return back(request, "Навык снова такой, как в этой версии PULSE", to=f"/skills/{sid}")

    @app.post("/why/{pid}")
    def proposal_why(pid: int, reason: str = Form(""), note: str = Form("")):
        """Почему «Нет» (MOD-3): из полосы после отказа — материал для уроков (навык lessons)."""
        ok = skills.set_reason(pid, reason, note)
        return JSONResponse({"ok": ok, "text": skills.REASONS.get(reason, "")})
