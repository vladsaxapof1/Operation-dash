"""«Поручить агенту» (opsdash.agents): панель в окне задачи и страница /agents?task=<id> — где работать, кто,
сборка промта по ходу, правка, файлы галочками, похожие поручения, «Копировать» и «Открыть в Claude».

Окно задачи подгружает панель кусочком страницы (?part=1, static/agents.js) и шлёт формы фоном (заголовок
X-Agent-Panel) — в ответ тот же кусочек. Без скриптов это обычная страница с формами."""

from __future__ import annotations

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.concurrency import run_in_threadpool

from opsdash import agents, brain, config, records


def register(app: FastAPI, render, templates, back) -> None:
    # «можно поручить агенту», поручение задачи и его сессия — в окне задачи и на карточке (_macros.html,
    # _agent_block.html), куда поручать — в Настройки → Офис
    templates.env.globals.update(agent_hint=agents.agentish, agent_of=agents.of, agent_folders=agents.folders)

    def context(cid: int) -> dict | None:
        c = records.get_commitment(cid)
        if not c:
            return None
        settings = config.load_settings()
        b = agents.for_task(cid)
        sims = (b or {}).get("similar") or agents.similar(agents.task_text(c), limit=3, settings=settings, task=cid)
        choices = agents.folders(settings)
        folder = b["folder"] if b and b["folder"] is not None else agents.default_folder(c, sims, settings)
        if folder and not any(f["path"] == folder for f in choices):
            choices.insert(0, {"path": folder, "label": folder, "root": ""})
        engine = b["engine"] if b else ("codex" if brain.engine(settings) == "chatgpt" else "claude")
        return {"c": c, "b": b, "sims": sims, "folders": choices, "folder": folder, "engine": engine,
                "builder": brain.name(settings), "hint": agents.agentish(c),
                "long": bool(b and b["prompt"] and len(b["prompt"]) > agents.LINK_PROMPT), "limit": agents.LINK_PROMPT}

    def panel(request: Request, cid: int, msg: str = "", err: str = "") -> HTMLResponse:
        ctx = context(cid)
        if ctx is None:
            return HTMLResponse("<p class='muted'>Задачи больше нет.</p>", status_code=404)
        return templates.TemplateResponse(request, "_agent_panel.html", {**ctx, "part": True, "said": msg, "fail": err})

    def answer(request: Request, cid: int, msg: str = "", err: str = ""):
        """Фоновый запрос из окна — кусочек панели; обычная форма — назад на страницу поручения."""
        if request.headers.get("x-agent-panel"):
            return panel(request, cid, msg, err)
        return back(request, msg, err, to=f"/agents?task={cid}")

    @app.get("/agents", response_class=HTMLResponse)
    def agent_page(request: Request, task: int = 0, part: int = 0):
        if part:
            return panel(request, task)
        ctx = context(task)
        if ctx is None:
            return back(request, err="Такой задачи нет", to="/commitments")
        return render(request, "agents.html", nav="commitments", part=False, said="", fail="", **ctx)

    @app.post("/agents/build")
    async def agent_build(request: Request, task: int = Form(...), folder: str = Form(""), engine: str = Form("claude"),
                          fix: str = Form("")):
        try:
            await run_in_threadpool(agents.start, task, folder.strip(), engine, fix)
        except ValueError as e:
            return answer(request, task, err=str(e))
        return answer(request, task)

    @app.post("/agents/{bid}/save")
    def agent_save(request: Request, bid: int, prompt: str = Form("")):
        b = agents.get(bid)
        if not b:
            return JSONResponse({"ok": False, "error": "Поручения нет"}, status_code=404)
        try:
            agents.save(bid, prompt)
        except ValueError as e:
            return JSONResponse({"ok": False, "error": str(e)}) if request.headers.get("x-agent-panel") \
                else answer(request, b["commitment_id"], err=str(e))
        if request.headers.get("x-agent-panel"):
            return JSONResponse({"ok": True})
        return answer(request, b["commitment_id"], "Промт сохранён")

    @app.post("/agents/{bid}/open")
    def agent_open(request: Request, bid: int, prompt: str = Form("")):
        b = agents.get(bid)
        if not b:
            return JSONResponse({"ok": False, "error": "Поручения нет"}, status_code=404)
        try:
            res = agents.launch(bid, prompt if prompt.strip() else None)
        except (ValueError, OSError) as e:
            err = f"Не открылось: {e}"
            return JSONResponse({"ok": False, "error": err}) if request.headers.get("x-agent-panel") \
                else answer(request, b["commitment_id"], err=err)
        said = ("Открыла новую сессию в Claude: промт — в поле ввода. Проверьте и отправьте сами." if res["whole"] else
                "Открыла новую сессию в Claude: промт длинный — в поле ввода короткое поручение со ссылкой на файл "
                "брифа. Проверьте и отправьте сами.")
        if request.headers.get("x-agent-panel"):
            return JSONResponse({"ok": True, "said": said})
        return answer(request, b["commitment_id"], said)

    @app.post("/agents/{bid}/stop")
    def agent_stop(request: Request, bid: int):
        b = agents.get(bid)
        if not b:
            return back(request, err="Поручения нет")
        agents.stop(bid)
        return answer(request, b["commitment_id"], "Остановлено")

    @app.get("/api/agents/{bid}/live")
    def agent_live(bid: int):
        return agents.live(bid)
