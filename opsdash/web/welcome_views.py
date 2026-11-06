"""Мастер первого запуска `/welcome` (ONB-2) и окно «Первые находки» на Сводке (ONB-3). Логика — opsdash.welcome."""

from __future__ import annotations

from zoneinfo import ZoneInfo

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from opsdash import config, connectors, welcome


def register(app: FastAPI, render, back) -> None:
    from opsdash.web.settings_views import WHISPER_MODELS, panel_context

    @app.get("/welcome", response_class=HTMLResponse)
    def welcome_page(request: Request, step: str = "", open: str = "", sort: str = "recent"):  # noqa: A002
        settings = config.load_settings()
        step = step if step in welcome.IDS else welcome.current(settings)
        # «Помощник»: вход проверяем заново (а не из запомненного на минуту) — после «Войти» нажимают «Проверить»
        assistant_ctx = panel_context("claude", settings) if step == "assistant" else {}
        catalog = connectors.catalog(settings)
        chapters = welcome.chapters(settings, catalog)
        ch = next(c for c in chapters if c["id"] == step)
        card = next((c for c in ch["cards"] if c["id"] == open), None)
        if not card and len(ch["cards"]) == 1:
            card = ch["cards"][0]  # в главе один источник — форма сразу открыта
        ctx = {"chapters": chapters, "ch": ch, "card": card, "catalog": catalog, "whisper_models": WHISPER_MODELS,
               "secrets": {k: config.mask(v) for k, v in config.load_secrets().items()},
               "prev": welcome.prev_id(step), "next": welcome.next_id(step), "first_days": welcome.FIRST_DAYS,
               "tab": card["id"] if card else "", "sort": sort}
        if card:
            ctx.update(panel_context(card["id"], settings, sort))
        if step == "assistant":
            ctx["assistants"] = [c for g in catalog["groups"] for c in g["cards"] if c.get("assistant")]
            ctx.update(assistant_ctx)
        if step == "done":
            from opsdash import doctor

            ctx["health"] = doctor.run(settings, catalog=catalog)
        return render(request, "welcome.html", nav="welcome", **ctx)

    @app.post("/welcome/about")
    def welcome_about(request: Request, name: str = Form(""), timezone: str = Form("Europe/Moscow"),
                      work_start: str = Form("09:00"), work_end: str = Form("20:00"), days: list[str] = Form([])):
        try:
            ZoneInfo(timezone.strip())
        except Exception:  # noqa: BLE001
            return back(request, err=f"Неизвестный часовой пояс: {timezone}", to="/welcome?step=about")
        if not name.strip():
            return back(request, err="Напишите, как вас зовут — так PULSE отличит ваши обещания от чужих",
                        to="/welcome?step=about")
        config.update_settings("owner", {
            "name": name.strip(), "timezone": timezone.strip(),
            "work_hours": {"start": work_start, "end": work_end, "days": [int(d) for d in days] or [1, 2, 3, 4, 5]},
        })
        welcome.mark("about", "visited")
        return RedirectResponse("/welcome?step=assistant", status_code=303)

    @app.post("/welcome/go")
    def welcome_go(step: str = Form(...), action: str = Form("next")):
        """«Дальше», «Пропустить», «Назад» — глава отмечается пройденной или пропущенной."""
        if step not in welcome.IDS:
            return RedirectResponse("/welcome", status_code=303)
        if action == "back":
            return RedirectResponse(f"/welcome?step={welcome.prev_id(step)}", status_code=303)
        welcome.mark(step, "skipped" if action == "skip" else "visited")
        return RedirectResponse(f"/welcome?step={welcome.next_id(step)}", status_code=303)

    @app.post("/welcome/finish")
    def welcome_finish():
        started = welcome.finish()
        msg = ("PULSE читает переписку за 5 дней — первые находки появятся здесь через 5–15 минут" if started
               else "Мастер пройден. Подключите помощника — без него PULSE не разбирает переписку")
        return RedirectResponse("/?" + ("msg=" if started else "err=") + _q(msg), status_code=303)

    @app.post("/welcome/dismiss")
    def welcome_dismiss():
        """«Закрыть мастер»: на чистой установке он больше не открывается сам; вернуться — из «Подключений»."""
        st = welcome.state()
        st["dismissed"] = True
        welcome._save(st)
        return RedirectResponse("/", status_code=303)

    @app.get("/welcome/status")
    def welcome_status():
        """Идёт ли первый разбор — Сводка спрашивает раз в 5 секунд и обновляется, когда готово."""
        from opsdash import store

        return {"status": (store.get_state(welcome.FINDS) or {}).get("status") or ""}

    @app.post("/welcome/seen")
    def welcome_seen():
        welcome.finds_seen()
        return {"ok": True}


def _q(text: str) -> str:
    from urllib.parse import quote

    return quote(text)
