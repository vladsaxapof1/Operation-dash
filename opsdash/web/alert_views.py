"""Карточка «скоро встреча» поверх всех окон (opsdash/meeting_alert.py): страница для окна macOS, кнопки
на ней и настройки в окне «Записи встреч»."""

from __future__ import annotations

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse

from opsdash import config, meeting_alert

# страница для окна, которому нечего показать: окно читает заголовок и закрывается
CLOSE_PAGE = "<!doctype html><html><head><meta charset='utf-8'><title>pulse:close</title></head><body></body></html>"


def register(app: FastAPI, templates, back) -> None:

    @app.get("/alert/{key}", response_class=HTMLResponse)
    def alert_page(request: Request, key: str):
        from opsdash.web.app import MOTIONS

        settings = config.load_settings()
        c = meeting_alert.card(key, settings)
        if not c:
            return HTMLResponse(CLOSE_PAGE)
        theme = settings["web"].get("theme") or "system"
        motion = (settings.get("web") or {}).get("motion") or ""
        return templates.TemplateResponse(request, "meeting_alert.html", {
            "c": c, "motion": motion if motion in MOTIONS else "",
            "v3_theme": ("light" if theme in ("light", "graphite-light") else "auto" if theme == "system" else "dark"),
        })

    @app.post("/alert/{key}/{action}")
    def alert_action(key: str, action: str):
        settings = config.load_settings()
        if action == "brief":
            from opsdash import daemon

            c = meeting_alert.card(key, settings)
            if c and c["brief"]:
                meeting_alert.open_call(f"http://127.0.0.1:{daemon.running_port(settings)}{c['brief']['href']}")
            return JSONResponse({"ok": True})
        try:
            return JSONResponse(meeting_alert.act(key, action, settings))
        except (ValueError, LookupError) as e:
            return JSONResponse({"error": str(e), "step": "done"}, status_code=404)

    @app.post("/settings/meeting-alert")
    def meeting_alert_save(request: Request, enabled: str = Form(None), minutes: int = Form(2)):
        config.update_settings("meeting_alert", {
            "enabled": enabled is not None, "minutes": minutes if minutes in meeting_alert.MINUTES else 2})
        return back(request, "Сохранено")

    @app.post("/settings/meeting-alert/test")
    def meeting_alert_test(request: Request):
        if not meeting_alert.is_supported():
            return back(request, err="Карточка поверх окон пока работает только на Mac")
        if not meeting_alert.show(meeting_alert.DEMO):
            return back(request, err="Не получилось открыть карточку: подробности в «Журнале»")
        return back(request, "Пример карточки показан в правом верхнем углу экрана")
