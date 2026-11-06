"""Админка: подключения и настройки."""

from __future__ import annotations

import re
import sys

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from opsdash import config, connectors, store

# все вкладки настроек (?tab=…); источники с окном подключения открываются каталогом «Подключений» с их окном
TABS = [
    ("general", "Основное"), ("updates", "Обновления"), ("connections", "Подключения"),
    ("telegram", "Telegram"), ("bot", "Telegram-бот"), ("ymessenger", "Я.Мессенджер"), ("pachca", "Пачка"), ("slack", "Slack"),
    ("mail", "Яндекс Почта"), ("gmail", "Gmail"), ("calendar", "Яндекс Календарь"), ("google_calendar", "Google Календарь"),
    ("imap", "Другая почта"), ("caldav", "Другой календарь"), ("ics_calendar", "Календарь по ссылке"),
    ("kaiten", "Kaiten"), ("recordings", "Записи встреч"), ("knowledge", "Документы"), ("office", "Офис"),
    ("autonomy", "Автономия"), ("schedule", "Расписание"), ("day", "День"), ("claude", "Claude"), ("chatgpt", "ChatGPT"),
    ("data", "Ваши данные"), ("archive", "Архивные разделы"),
]


def settings_menu(settings: dict, catalog: dict) -> list[dict]:
    """Подменю Настроек (3.10): пункты, разделитель перед системными. Источники, записи встреч, документы, Claude
    и ChatGPT — карточками в «Подключениях» (окно поверх каталога): в подменю только «N из M», оранжевым — если
    у какого-то источника ошибка или не хватает шага, или выбранный помощник не работает."""
    from opsdash import __version__

    menu = [{"tab": "general", "title": "Основное"},
            {"tab": "connections", "title": "Подключения", "note": f"{catalog['on']} из {catalog['total']}",
             "warn": bool(catalog["problems"])},
            {"tab": "schedule", "title": "Расписание"}, {"tab": "day", "title": "День"},
            {"tab": "autonomy", "title": "Автономия"}, {"tab": "office", "title": "Офис"}]
    archived = sum(not config.section_on(n, settings) for n in config.SECTIONS)
    menu += [{"sep": True}, {"tab": "data", "title": "Ваши данные"},
             {"tab": "updates", "title": "Обновления", "note": __version__},
             {"tab": "archive", "title": "Архивные разделы", "note": str(archived) if archived else ""}]
    return menu


def archive_rows(settings: dict) -> list[dict]:
    """Строки «Архивных разделов»: включён ли, с какого времени в архиве, сколько данных осталось."""
    from opsdash import anthology, docs

    stamps = settings["sections"].get("archived_at") or {}
    out = []
    for name, sec in config.SECTIONS.items():
        if name == "docs":
            st = docs.stats()
            data = (f"{st['indexed']} {plural(st['indexed'], 'документ', 'документа', 'документов')} в индексе"
                    if st["indexed"] else "индекс пуст")
        else:
            n = sum(v for k, v in anthology.counts().items() if k != "proposed")
            data = f"{n} {plural(n, 'запись', 'записи', 'записей')}" if n else "записей нет"
        out.append({"name": name, **sec, "on": config.section_on(name, settings), "since": stamps.get(name, ""),
                    "data": data})
    return out


def plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(n) % 100
    if 11 <= n <= 19:
        return many
    return one if n % 10 == 1 else few if 2 <= n % 10 <= 4 else many


THEMES = {"system": "Как в системе", "light": "Светлая", "dark": "Тёмная"}  # оформление (dark.css, base.html)
CHAT_KINDS = {"private": "Личные", "group": "Группы", "channel": "Каналы", "bot": "Боты"}

WHISPER_MODELS = [
    ("large-v3-turbo", "large-v3-turbo — лучшее качество/скорость (рекомендую)"),
    ("large-v3", "large-v3 — максимум качества, медленно"),
    ("medium", "medium — для слабых ноутбуков"),
    ("small", "small — быстро, качество ниже"),
]


def lines(text: str) -> list[str]:
    return [x.strip().strip('"') for x in (text or "").replace("\r", "").split("\n") if x.strip()]


def checkbox(value) -> bool:
    return value in ("on", "1", "true", True)


def collect_soon(source: str) -> None:
    """Собрать данные сразу после сохранения настроек — иначе до планового сбора (до получаса)
    на главной висит ошибка прошлой попытки, хотя всё уже исправлено."""
    from opsdash import jobs

    if config.load_settings()[source].get("enabled") and jobs.in_daemon():
        jobs.submit(f"collect:{source}")


def chat_list(source: str, sort: str) -> dict:
    """Чаты для выбора в настройках (Telegram, Пачка): по умолчанию — по последнему сообщению,
    кто писал недавно, тот и сверху; счётчики для фильтров «Читаю / Не читаю» и по типу чата."""
    chats = store.rows("SELECT * FROM chats WHERE source=? ORDER BY last_ts IS NULL, last_ts DESC, title", (source,))
    if sort == "title":  # по алфавиту без учёта кавычек и эмодзи в начале названия
        chats.sort(key=lambda c: re.sub(r"^[\W_]+", "", c["title"] or "").casefold())
    return {
        "sort": "title" if sort == "title" else "recent",
        "chats": chats,
        "chat_counts": {v: sum(c["enabled"] == v for c in chats) for v in (1, 0, -1)},
        "chat_kinds": [(k, title, n) for k, title in CHAT_KINDS.items() if (n := sum(c["kind"] == k for c in chats))],
    }


def panel_context(tab: str, settings: dict, sort: str = "recent") -> dict:
    """Что нужно форме источника (connectors/<tab>.html): окну в каталоге «Подключений» и главе мастера первого
    запуска (opsdash.welcome) — вход в Telegram, списки чатов, пространства Kaiten, вход помощника, расход."""
    from opsdash import bot, brain
    from opsdash.sources import telegram

    out: dict = {}
    if tab == "telegram":
        flow = telegram.login_flow
        out["tg_login_state"] = ("qr" if flow.qr_state else "code" if flow.client and flow.phone_code_hash else "")
        out["tg_qr"] = flow
        out["tg_me"] = store.get_state("telegram_me")
        out.update(chat_list("telegram", sort))
    elif tab == "bot":
        out["pair_code"] = bot.pair_code() if not settings["telegram_bot"].get("owner_chat_id") else ""
        out["bot_username"] = store.get_state("bot_username")
    elif tab == "ymessenger":
        out["chats"] = store.rows("SELECT * FROM chats WHERE source='ymessenger' ORDER BY title")
    elif tab == "pachca":
        out["pachca_me"] = store.get_state("pachca_me")
        out.update(chat_list("pachca", sort))
    elif tab == "slack":
        from opsdash.sources import slack

        out["slack_me"] = store.get_state("slack_me")
        out["slack_manifest"] = slack.manifest_json()
        out.update(chat_list("slack", sort))
    elif tab == "kaiten":
        out["boards"] = store.rows("SELECT * FROM kaiten_boards ORDER BY title")
        out["kaiten_me"] = store.get_state("kaiten_me")
    elif tab in ("calendar", "google_calendar", "ics_calendar"):
        from opsdash.sources import calendar

        out["google_urls"] = len(calendar.google_urls())
        out["ics_urls"] = len(calendar.ics_urls())
        if tab == "calendar":
            out["calendar_names"] = calendar.write_calendar_names(settings)[0]
    elif tab in ("claude", "chatgpt"):
        from opsdash import codex

        out["claude_path"] = brain.find_claude(settings)
        out["claude_login"] = brain.login_status(out["claude_path"], settings) if out["claude_path"] else None
        out["engine"], out["engines"], out["engine_busy"] = brain.engine(settings), brain.ENGINES, brain.busy()
        out["codex_path"] = codex.find(settings)
        out["codex_login"] = codex.login_status(out["codex_path"], settings) if out["codex_path"] else None
        out["codex_models"] = codex.models()
        from opsdash import usage

        out["usage"], out["limits"] = usage.summary(), usage.limits()
    elif tab == "recordings":
        from opsdash import recordings

        from opsdash import voice

        out["existing"] = [str(p) for p in recordings.watch_folders(settings)]
        out["voice_samples"] = len(voice.samples())
    return out


def register(app: FastAPI, render, back) -> None:

    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request, tab: str = "general", sort: str = "recent"):
        settings = config.load_settings()
        secrets = config.load_secrets()
        tab = {"connection": "connections"}.get(tab, tab)
        catalog = connectors.catalog(settings)
        # вкладка источника (?tab=telegram — старые ссылки, бот, возврат после «Сохранить») — это каталог
        # «Подключений» с окном этого источника поверх него (CON-3)
        panel = tab if tab in connectors.panel_ids() else ""
        ctx = {"tab": tab, "menu_tab": "connections" if panel else tab, "secrets": {k: config.mask(v) for k, v in secrets.items()},
               "whisper_models": WHISPER_MODELS, "themes": THEMES, "catalog": catalog,
               "conn": next((c for g in catalog["groups"] for c in g["cards"] if c["id"] == panel), None)}
        if tab == "mcp":  # окно MCP-подключения (opsdash.mcp): подключённый сервер (?id=) или витрина (?preset=)
            from opsdash import brain, mcp

            ctx["conn"] = mcp.conn(request.query_params.get("id", ""), request.query_params.get("preset", ""), settings)
            ctx["menu_tab"] = "connections"
            ctx["mcp_engine"] = brain.engine(settings)
            if not ctx["conn"]:
                ctx["tab"] = "connections"
        if tab == "archive":
            ctx["sections"] = archive_rows(settings)
        if tab == "data":
            ctx["data"] = connectors.data_overview(settings)
        if tab == "claude":
            from opsdash import chat

            ctx["efforts"] = chat.EFFORTS
        ctx["menu"] = settings_menu(settings, catalog)
        if ctx["menu_tab"] == "connections":
            from opsdash import doctor

            ctx["health"] = doctor.run(settings, catalog=catalog)  # блок «Здоровье» над каталогом (CON-5)
        if tab == "updates":
            from opsdash import updates

            ctx["upd"], ctx["installed"] = updates.last_check(), updates.installed()
            ctx["updating"] = request.query_params.get("updating") or ""  # «1» — обновление, «restart» — перезапуск
        ctx.update(panel_context(tab, settings, sort))
        return render(request, "settings.html", nav="settings", **ctx)

    # --- проверки подключений ---

    @app.post("/settings/check/{what}")
    async def settings_check(what: str):
        from opsdash import bot, brain, codex, sources

        def bot_check():
            me = bot.get_bot().me()
            store.set_state("bot_username", me.get("username"))
            return f"бот @{me.get('username')}"

        checks = {"claude": brain.check, "chatgpt": codex.check, "bot": bot_check,
                  **{k: sources.checker(k) for k in config.SOURCES}}
        if what.startswith("mcp-"):  # MCP-подключение: подключиться и запомнить инструменты
            from opsdash import mcp

            checks[what] = lambda: mcp.check(what[4:])
        if what not in checks:
            return JSONResponse({"ok": False, "message": "неизвестная проверка"})
        try:
            message = await run_in_threadpool(checks[what])
            return JSONResponse({"ok": True, "message": message})
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"ok": False, "message": str(e)[:500]})

    # --- основное ---

    @app.post("/settings/general")
    def save_general(request: Request, name: str = Form(""), timezone: str = Form("Europe/Moscow"),
                     work_start: str = Form("09:00"), work_end: str = Form("20:00"), days: list[str] = Form([]),
                     port: int = Form(8765)):
        from zoneinfo import ZoneInfo

        try:
            ZoneInfo(timezone)
        except Exception:  # noqa: BLE001
            return back(request, err=f"Неизвестный часовой пояс: {timezone}")
        config.update_settings("owner", {
            "name": name.strip(), "timezone": timezone.strip(),
            "work_hours": {"start": work_start, "end": work_end, "days": [int(d) for d in days] or [1, 2, 3, 4, 5]},
        })
        config.update_settings("web", {"port": int(port)})
        return back(request, "Сохранено")

    @app.post("/settings/day")
    def save_day(request: Request, focus_titles: str = Form(""), buffer_after_min: int = Form(10),
                 buffer_before_min: int = Form(10), min_window_min: int = Form(25), lunch_on: str = Form(None),
                 lunch_start: str = Form("13:00"), lunch_end: str = Form("14:00"), reply_min: int = Form(5)):
        """«Реальность дня»: фокус-блоки, буферы, минимальное окно, обед, минуты на ответ (opsdash.day)."""
        values = {"buffer_after_min": buffer_after_min, "buffer_before_min": buffer_before_min,
                  "min_window_min": min_window_min, "reply_min": reply_min}
        if any(not 0 <= v <= 120 for v in values.values()):
            return back(request, err="Минуты — от 0 до 120", to="/settings?tab=day")
        lunch = ""
        if checkbox(lunch_on):
            if not lunch_start or not lunch_end or lunch_end <= lunch_start:
                return back(request, err="Обед: конец должен быть позже начала", to="/settings?tab=day")
            lunch = f"{lunch_start}-{lunch_end}"
        titles = [t.strip() for t in focus_titles.replace("\n", ",").split(",") if t.strip()]
        settings = config.load_settings()
        settings["day"] = {**settings["day"], **values, "lunch": lunch, "focus_titles": titles}
        config.save_settings(settings)  # целиком: список фокус-блоков заменяется, а не дополняется
        return back(request, "Сохранено", to="/settings?tab=day")

    @app.post("/settings/sections/{name}")
    def save_section(request: Request, name: str, on: str = Form("")):
        """Включить архивный раздел или вернуть его в архив. Данные в data/ при этом не трогаются."""
        from opsdash import daemon

        if name not in config.SECTIONS:
            return back(request, err="Нет такого раздела", to="/settings?tab=archive")
        settings = config.load_settings()
        stamps = dict(settings["sections"].get("archived_at") or {})
        if on:
            stamps.pop(name, None)
        else:
            stamps[name] = store.utcnow()
        settings["sections"] = {**settings["sections"], name: "on" if on else "archived", "archived_at": stamps}
        config.save_settings(settings)
        daemon.sync_claude_settings()
        title = config.SECTIONS[name]["title"]
        return back(request, f"Раздел «{title}» включён — он снова в меню, его задания работают" if on
                    else f"Раздел «{title}» в архиве. Данные сохранены", to="/settings?tab=archive")

    @app.post("/settings/theme")
    def save_theme(request: Request, theme: str = Form("system")):
        if theme not in THEMES:
            return back(request, err="Неизвестная тема")
        config.update_settings("web", {"theme": theme})
        return back(request, to="/settings?tab=general#theme")

    @app.post("/settings/updates")
    def save_updates(request: Request, repo: str = Form(""), branch: str = Form(""), token: str = Form(""),
                     auto_check: str = Form(None)):
        repo = repo.strip().removeprefix("https://github.com/").strip("/").removesuffix(".git")
        if repo and not re.fullmatch(r"[\w.-]+/[\w.-]+", repo):
            return back(request, err="Репозиторий — в виде «владелец/название», например vladsaxapof1/Operation-dash",
                        to="/settings?tab=updates")
        config.update_settings("updates", {"repo": repo, "branch": branch.strip() or "main",
                                           "auto_check": checkbox(auto_check)})
        if token.strip():
            config.set_secret("GITHUB_TOKEN", token.strip())
        return back(request, "Сохранено", to="/settings?tab=updates")

    # --- Telegram (аккаунт) ---

    @app.post("/settings/telegram/api")
    def tg_api(request: Request, api_id: str = Form(...), api_hash: str = Form("")):
        config.update_settings("telegram", {"api_id": api_id.strip()})
        if api_hash.strip():
            config.set_secret("TELEGRAM_API_HASH", api_hash.strip())
        return back(request, "Ключи сохранены — теперь войдите по номеру телефона")

    @app.post("/settings/telegram/login")
    async def tg_login(request: Request, phone: str = Form(...)):
        from opsdash.sources import telegram

        await run_in_threadpool(telegram.login_flow.start, phone)
        return back(request, "Telegram отправил код в приложение — введите его ниже")

    @app.post("/settings/telegram/code")
    async def tg_code(request: Request, code: str = Form(...)):
        from opsdash.sources import telegram

        result = await run_in_threadpool(telegram.login_flow.code, code)
        if result == "password":
            return back(request, "Включена двухэтапная проверка — введите облачный пароль Telegram", to="/settings?tab=telegram&need=password")
        await run_in_threadpool(_after_tg_login)
        return back(request, "Вход выполнен. Отметьте чаты, которые нужно читать", to="/settings?tab=telegram")

    @app.post("/settings/telegram/password")
    async def tg_password(request: Request, password: str = Form(...)):
        from opsdash.sources import telegram

        await run_in_threadpool(telegram.login_flow.password, password)
        await run_in_threadpool(_after_tg_login)
        return back(request, "Вход выполнен. Отметьте чаты, которые нужно читать", to="/settings?tab=telegram")

    @app.post("/settings/telegram/qr")
    async def tg_qr(request: Request):
        from opsdash.sources import telegram

        await run_in_threadpool(telegram.login_flow.start_qr)
        return back(request, to="/settings?tab=telegram")

    @app.get("/settings/telegram/qr.svg")
    def tg_qr_svg():
        import io

        import segno

        from opsdash.sources import telegram

        url = telegram.login_flow.qr_url()
        if not url:
            return Response(status_code=404)
        buf = io.BytesIO()
        segno.make(url, error="m").save(buf, kind="svg", scale=6, border=2)
        return Response(buf.getvalue(), media_type="image/svg+xml", headers={"Cache-Control": "no-store"})

    @app.get("/settings/telegram/qr/status")
    def tg_qr_status():
        from opsdash.sources import telegram

        flow = telegram.login_flow
        return {"state": flow.qr_state, "version": flow.qr_version}

    @app.post("/settings/telegram/qr/done")
    async def tg_qr_done(request: Request):
        from opsdash.sources import telegram

        if not await run_in_threadpool(telegram.login_flow.finish_qr):
            return back(request, err="QR-код ещё не отсканирован — попробуйте снова", to="/settings?tab=telegram")
        await run_in_threadpool(_after_tg_login)
        return back(request, "Вход выполнен. Отметьте чаты, которые нужно читать", to="/settings?tab=telegram")

    def _after_tg_login():
        from opsdash.sources import telegram

        try:
            telegram.check()
            telegram.refresh_dialogs()
        except Exception as e:  # noqa: BLE001
            store.log("telegram", f"после входа: {e}", "warning")

    @app.post("/settings/telegram/cancel")
    def tg_cancel(request: Request):
        from opsdash.sources import telegram

        telegram.login_flow.close()
        return back(request, "Вход отменён")

    @app.post("/settings/telegram/logout")
    def tg_logout(request: Request):
        from opsdash.sources import telegram

        telegram.logout()
        store.set_state("telegram_me", None)
        return back(request, "Сессия Telegram удалена с этого компьютера")

    @app.post("/settings/telegram/dialogs")
    async def tg_dialogs(request: Request):
        from opsdash.sources import telegram

        n = await run_in_threadpool(telegram.refresh_dialogs)
        return back(request, f"Список чатов обновлён: {n}")

    @app.post("/settings/telegram/options")
    def tg_options(request: Request, enabled: str = Form(None), include_all_private: str = Form(None),
                   backfill_days: int = Form(5)):
        config.update_settings("telegram", {"enabled": checkbox(enabled), "include_all_private": checkbox(include_all_private),
                                            "backfill_days": int(backfill_days)})
        collect_soon("telegram")
        return back(request, "Сохранено")

    @app.post("/settings/chats")
    async def chats_save(request: Request):
        form = await request.form(max_fields=50_000)  # у директора бывает больше тысячи чатов
        source = form.get("source")
        n = 0
        for key, value in form.multi_items():
            if key.startswith("chat__"):
                chat_id = key[len("chat__"):]
                store.execute("UPDATE chats SET enabled=? WHERE source=? AND chat_id=?", (int(value), source, chat_id))
                n += 1
            elif key.startswith("title__") and str(value).strip():
                chat_id = key[len("title__"):]
                store.execute("UPDATE chats SET title=? WHERE source=? AND chat_id=?", (str(value).strip(), source, chat_id))
        if form.get("ajax"):  # переключатель в списке чатов сохраняет сразу, без перезагрузки страницы
            return JSONResponse({"ok": True, "saved": n})
        return back(request, f"Сохранено ({n} чатов)")

    @app.post("/settings/voice/forget")
    def voice_forget(request: Request):
        from opsdash import voice

        voice.forget()
        return back(request, "Голос забыт", to="/settings?tab=recordings")

    # --- Telegram-бот ---

    @app.post("/settings/bot")
    async def bot_save(request: Request, token: str = Form(""), enabled: str = Form(None),
                       quiet_start: str = Form(""), quiet_end: str = Form("")):
        from opsdash import bot

        if token.strip():
            config.set_secret("TELEGRAM_BOT_TOKEN", token.strip())
            try:
                me = await run_in_threadpool(lambda: bot.get_bot().me())
                store.set_state("bot_username", me.get("username"))
            except Exception as e:  # noqa: BLE001
                return back(request, err=f"Токен не подошёл: {e}")
        config.update_settings("telegram_bot", {
            "enabled": checkbox(enabled), "quiet_hours": {"start": quiet_start, "end": quiet_end},
        })
        return back(request, "Сохранено")

    @app.post("/settings/bot/unpair")
    def bot_unpair(request: Request):
        config.update_settings("telegram_bot", {"owner_chat_id": ""})
        store.set_state("bot_pair_code", None)
        return back(request, "Бот отвязан. Привяжите заново по новому коду")

    @app.post("/settings/bot/test")
    async def bot_test(request: Request):
        from opsdash import bot

        chat = bot.owner_chat()
        if not chat:
            return back(request, err="Бот ещё не привязан — отправьте ему /start с кодом")
        try:  # напрямую, мимо очереди: проверяем именно связь
            await run_in_threadpool(lambda: bot.get_bot().send(chat, "✅ Проверка связи: PULSE на связи."))
        except Exception as e:  # noqa: BLE001
            return back(request, err=f"Не отправилось: {e}")
        return back(request, "Сообщение отправлено — проверьте Telegram")

    # --- Я.Мессенджер ---

    @app.post("/settings/ymessenger")
    def ym_save(request: Request, token: str = Form(""), enabled: str = Form(None), my_login: str = Form("")):
        if token.strip():
            config.set_secret("YM_BOT_TOKEN", token.strip())
        config.update_settings("ymessenger", {"enabled": checkbox(enabled), "my_login": my_login.strip()})
        collect_soon("ymessenger")
        return back(request, "Сохранено")

    # --- свои подключения через MCP (opsdash.mcp, MORE-1) ---

    def _mcp_page(sid: str = "", preset: str = "", msg: str = "", err: str = "") -> RedirectResponse:
        from urllib.parse import quote

        where = f"id={quote(sid)}" if sid else f"preset={quote(preset or 'custom')}"
        tail = (f"&msg={quote(msg)}" if msg else "") + (f"&err={quote(err)}" if err else "")
        return RedirectResponse(f"/settings?tab=mcp&{where}{tail}", status_code=303)

    @app.post("/settings/mcp/save")
    async def mcp_save(request: Request):
        """Подключить сервер из витрины или свой; у подключённого — обновить ключи. Сразу проверяем: список
        инструментов нужен, чтобы помощник знал, что ему можно."""
        import shlex

        from opsdash import mcp

        form = await request.form()
        sid, preset_id = (form.get("id") or "").strip(), (form.get("preset") or "").strip()
        existing = mcp.server(sid) if sid else None
        p = mcp.preset(preset_id or (existing or {}).get("preset") or "")
        values = {k[2:]: v for k, v in form.items() if k.startswith("f:")}
        custom_fields = None
        try:
            if not p:  # свой сервер: команда или адрес, переменные и заголовки строками «ИМЯ=значение» / «Имя: значение»
                transport = form.get("transport") or "stdio"
                command_line = (form.get("command") or "").strip()
                parts = shlex.split(command_line, posix=sys.platform != "win32") if command_line else []
                custom_fields = []
                for line in lines(form.get("env") or ""):
                    if "=" in line:
                        k, _, v = line.partition("=")
                        custom_fields.append({"name": k.strip(), "kind": "env", "secret": True, "label": k.strip()})
                        values[k.strip()] = v.strip()
                for line in lines(form.get("headers") or ""):
                    if ":" in line:
                        k, _, v = line.partition(":")
                        custom_fields.append({"name": k.strip(), "kind": "header", "header": k.strip(), "format": "{}",
                                              "secret": True, "label": k.strip()})
                        values[k.strip()] = v.strip()
                if existing and not custom_fields:
                    custom_fields = None  # поля не трогали — оставляем прежние (значения секретов — в secrets)
                sid = sid or mcp.new_id(form.get("title") or "", *(reversed(parts) if parts else ()),
                                        form.get("url") or "")
                mcp.save(sid, title=form.get("title") or "", transport=transport, command=parts[0] if parts else "",
                         args=parts[1:] if parts else None, url=form.get("url") or "", values=values,
                         custom_fields=custom_fields, enabled=checkbox(form.get("enabled")) or not existing)
            else:
                sid = sid or mcp.new_id(p["id"])
                mcp.save(sid, preset_id=p["id"], title=form.get("title") or "", values=values,
                         enabled=checkbox(form.get("enabled")) or not existing)
        except mcp.MCPError as e:
            return _mcp_page(sid if existing else "", preset_id, err=str(e))
        st = mcp.status(sid)
        if st["kind"] == "setup" and not st["label"].endswith("нужна проверка"):
            return _mcp_page(sid, msg="Сохранено", err=st["label"][:1].upper() + st["label"][1:])
        try:
            found = await run_in_threadpool(mcp.check, sid)
        except mcp.MCPError as e:
            return _mcp_page(sid, err=f"Сохранено, но сервер не ответил: {e}")
        return _mcp_page(sid, msg=f"Подключено — {found}. Отметьте, что помощнику можно читать")

    @app.post("/settings/mcp/{sid}/tools")
    async def mcp_tools(request: Request, sid: str):
        from opsdash import mcp

        form = await request.form()
        if not mcp.server(sid):
            return back(request, err="Нет такого подключения")
        mcp.set_tools(sid, read_on=form.getlist("read"), writes=checkbox(form.get("writes")))
        return _mcp_page(sid, msg="Сохранено — помощник учтёт со следующего запуска")

    @app.post("/settings/mcp/{sid}/enabled")
    def mcp_enabled(request: Request, sid: str, enabled: str = Form(None)):
        from opsdash import mcp

        s = mcp.server(sid)
        if not s:
            return back(request, err="Нет такого подключения")
        mcp.save(sid, preset_id=s.get("preset") or "", enabled=checkbox(enabled))
        return _mcp_page(sid, msg="Включено" if checkbox(enabled) else "Выключено — помощник не видит этот сервис")

    @app.post("/settings/mcp/{sid}/remove")
    def mcp_remove(request: Request, sid: str):
        from opsdash import mcp

        mcp.remove(sid)
        return RedirectResponse("/settings?tab=connections&msg=" + __import__("urllib.parse").parse.quote(
            "Подключение удалено вместе с ключами"), status_code=303)

    # --- Slack ---

    @app.post("/settings/slack")
    async def slack_save(request: Request, token: str = Form(""), enabled: str = Form(None)):
        from opsdash.sources import slack

        token = token.strip()
        if token and not token.startswith("xoxp-"):
            return back(request, err="Нужен User OAuth Token — он начинается с xoxp-. Bot Token (xoxb-) не видит ваших "
                                     "личных сообщений")
        first = bool(token) and not config.get_secret("SLACK_TOKEN")  # первый токен — читать сразу
        config.update_settings("slack", {"enabled": checkbox(enabled) or first})
        msg = "Сохранено"
        if token:
            config.set_secret("SLACK_TOKEN", token)
            try:  # сразу показать каналы для выбора
                n = await run_in_threadpool(slack.refresh_chats)
            except Exception as e:  # noqa: BLE001
                return back(request, err=f"Токен сохранён, но список каналов не получен: {e}")
            msg = (f"Сохранено{', чтение Slack включено' if first else ''}. Чатов в Slack: {n} — личные сообщения "
                   "PULSE читает сам, отметьте, какие каналы читать")
        collect_soon("slack")
        return back(request, msg)

    @app.post("/settings/slack/chats")
    async def slack_chats(request: Request):
        from opsdash.sources import slack

        try:
            n = await run_in_threadpool(slack.refresh_chats)
        except Exception as e:  # noqa: BLE001
            return back(request, err=f"Не получилось: {e}")
        return back(request, f"Список чатов обновлён: {n}")

    # --- Пачка ---

    @app.post("/settings/pachca")
    async def pachca_save(request: Request, token: str = Form(""), enabled: str = Form(None)):
        from opsdash.sources import pachca

        # первый токен — директор подключает Пачку: читать сразу, а не ждать, пока заметит галочку
        first = bool(token.strip()) and not config.get_secret("PACHCA_TOKEN")
        config.update_settings("pachca", {"enabled": checkbox(enabled) or first})
        msg = "Сохранено"
        if token.strip():
            config.set_secret("PACHCA_TOKEN", token.strip())
            try:  # сразу показать чаты для выбора
                n = await run_in_threadpool(pachca.refresh_chats)
            except Exception as e:  # noqa: BLE001
                return back(request, err=f"Токен сохранён, но список чатов не получен: {e}")
            msg = (f"Сохранено{', чтение Пачки включено' if first else ''}. Чатов в Пачке: {n} — "
                   "отметьте, какие читать")
        collect_soon("pachca")
        return back(request, msg)

    @app.post("/settings/pachca/chats")
    async def pachca_chats(request: Request):
        from opsdash.sources import pachca

        try:
            n = await run_in_threadpool(pachca.refresh_chats)
        except Exception as e:  # noqa: BLE001
            return back(request, err=f"Не получилось: {e}")
        return back(request, f"Список чатов обновлён: {n}")

    # --- почта ---

    @app.post("/settings/mail")
    def mail_save(request: Request, email: str = Form(""), password: str = Form(""), enabled: str = Form(None),
                  folders: str = Form("INBOX"), include_sent: str = Form(None), backfill_days: int = Form(5),
                  ignore_senders: str = Form(""), host: str = Form("imap.yandex.ru")):
        if password.strip():
            config.set_secret("MAIL_APP_PASSWORD", password.strip().replace(" ", ""))
        config.update_settings("mail", {
            "enabled": checkbox(enabled), "email": email.strip(), "host": host.strip() or "imap.yandex.ru",
            "folders": lines(folders) or ["INBOX"], "include_sent": checkbox(include_sent),
            "backfill_days": int(backfill_days), "ignore_senders": lines(ignore_senders),
        })
        collect_soon("mail")
        return back(request, "Сохранено")

    @app.post("/settings/gmail")
    def gmail_save(request: Request, email: str = Form(""), password: str = Form(""), enabled: str = Form(None),
                   folders: str = Form("INBOX"), include_sent: str = Form(None), primary_only: str = Form(None),
                   backfill_days: int = Form(5)):
        if password.strip():
            config.set_secret("GMAIL_APP_PASSWORD", password.strip().replace(" ", ""))  # Google показывает его с пробелами
        config.update_settings("gmail", {
            "enabled": checkbox(enabled), "email": email.strip(), "folders": lines(folders) or ["INBOX"],
            "include_sent": checkbox(include_sent), "primary_only": checkbox(primary_only),
            "backfill_days": int(backfill_days),
        })
        collect_soon("gmail")
        return back(request, "Сохранено", to="/settings?tab=gmail")

    # --- календарь ---

    @app.post("/settings/calendar")
    def calendar_save(request: Request, login: str = Form(""), password: str = Form(""), enabled: str = Form(None),
                      calendars: str = Form(""), write_calendar: str = Form(""), days_ahead: int = Form(14),
                      url: str = Form("https://caldav.yandex.ru")):
        if password.strip():
            config.set_secret("CALDAV_APP_PASSWORD", password.strip().replace(" ", ""))
        config.update_settings("calendar", {
            "enabled": checkbox(enabled), "login": login.strip(), "calendars": lines(calendars),
            "write_calendar": write_calendar.strip(), "days_ahead": int(days_ahead), "url": url.strip(),
        })
        collect_soon("calendar")
        return back(request, "Сохранено")

    @app.post("/settings/google_calendar")
    def google_calendar_save(request: Request, urls: str = Form(""), enabled: str = Form(None), forget: str = Form(None)):
        new = lines(urls)
        bad = [u for u in new if not u.startswith("https://")]
        if bad:
            return back(request, err="Это не похоже на адрес iCal (должен начинаться с https://): " + bad[0][:80],
                        to="/settings?tab=google_calendar")
        if new or checkbox(forget):
            config.set_secret("GOOGLE_CALENDAR_ICAL", " ".join(new) or None)
        config.update_settings("google_calendar", {"enabled": checkbox(enabled)})
        collect_soon("google_calendar")
        return back(request, "Сохранено", to="/settings?tab=google_calendar")

    # --- «другой сервер» (MORE-2): любая почта по IMAP, любой календарь по CalDAV или по ссылке .ics ---

    @app.post("/settings/imap")
    def imap_save(request: Request, email: str = Form(""), login: str = Form(""), host: str = Form(""),
                  port: int = Form(993), password: str = Form(""), enabled: str = Form(None),
                  folders: str = Form("INBOX"), include_sent: str = Form(None), backfill_days: int = Form(5)):
        host = host.strip().removeprefix("imaps://").removeprefix("imap://").strip("/")
        if password.strip():
            config.set_secret("IMAP_PASSWORD", password.strip())
        config.update_settings("imap", {
            "enabled": checkbox(enabled), "email": email.strip(), "login": login.strip(), "host": host,
            "port": int(port or 993), "folders": lines(folders) or ["INBOX"], "include_sent": checkbox(include_sent),
            "backfill_days": int(backfill_days),
        })
        collect_soon("imap")
        return back(request, "Сохранено", to="/settings?tab=imap")

    @app.post("/settings/caldav")
    def caldav_save(request: Request, url: str = Form(""), login: str = Form(""), password: str = Form(""),
                    enabled: str = Form(None), calendars: str = Form("")):
        url = url.strip()
        if url and not url.startswith(("https://", "http://")):
            return back(request, err="Адрес CalDAV должен начинаться с https://", to="/settings?tab=caldav")
        if password.strip():
            config.set_secret("CALDAV_OTHER_PASSWORD", password.strip())
        config.update_settings("caldav", {"enabled": checkbox(enabled), "url": url, "login": login.strip(),
                                          "calendars": lines(calendars)})
        collect_soon("caldav")
        return back(request, "Сохранено", to="/settings?tab=caldav")

    @app.post("/settings/ics_calendar")
    def ics_calendar_save(request: Request, urls: str = Form(""), enabled: str = Form(None), forget: str = Form(None)):
        new = lines(urls)
        bad = [u for u in new if not u.startswith(("https://", "webcal://"))]
        if bad:
            return back(request, err="Это не похоже на ссылку на календарь (https:// или webcal://): " + bad[0][:80],
                        to="/settings?tab=ics_calendar")
        if new or checkbox(forget):
            config.set_secret("ICS_CALENDAR_URLS", " ".join(new) or None)
        config.update_settings("ics_calendar", {"enabled": checkbox(enabled)})
        collect_soon("ics_calendar")
        return back(request, "Сохранено", to="/settings?tab=ics_calendar")

    # --- Kaiten ---

    @app.post("/settings/kaiten")
    def kaiten_save(request: Request, domain: str = Form(""), token: str = Form(""), enabled: str = Form(None),
                    stale_days: int = Form(3), stale_states: list[str] = Form([]), stale_basis: str = Form("moved"),
                    nudge_text: str = Form(""), exclude_boards: list[str] = Form([])):
        from opsdash.sources import kaiten

        if token.strip():
            config.set_secret("KAITEN_TOKEN", token.strip())
        config.update_settings("kaiten", {
            "enabled": checkbox(enabled), "domain": kaiten.normalize_domain(domain),
            "stale_days": max(1, int(stale_days)), "stale_states": [int(s) for s in stale_states] or [2],
            "stale_basis": stale_basis, "nudge_text": nudge_text.strip() or config.DEFAULTS["kaiten"]["nudge_text"],
            "exclude_boards": [int(b) for b in exclude_boards],
        })
        collect_soon("kaiten")
        return back(request, "Сохранено")

    @app.get("/api/kaiten/spaces")
    async def kaiten_spaces():
        from opsdash.sources import kaiten

        try:
            spaces = await run_in_threadpool(lambda: kaiten.client().spaces())
            return {"ok": True, "spaces": [{"id": s["id"], "title": s.get("title")} for s in spaces]}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "message": str(e)}

    @app.get("/api/kaiten/boards")
    async def kaiten_boards(space: int):
        from opsdash.sources import kaiten

        try:
            boards = await run_in_threadpool(lambda: kaiten.client().boards(space))
            return {"ok": True, "boards": [{
                "id": b["id"], "title": b.get("title"),
                "columns": sorted([{"id": c["id"], "title": c.get("title"), "sort": c.get("sort_order") or 0}
                                   for c in b.get("columns") or []], key=lambda c: c["sort"]),
                "lanes": sorted([{"id": ln["id"], "title": ln.get("title"), "sort": ln.get("sort_order") or 0}
                                 for ln in b.get("lanes") or [] if ln.get("condition", 1) == 1], key=lambda x: x["sort"]),
            } for b in boards]}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "message": str(e)}

    @app.post("/settings/kaiten/spaces")
    async def kaiten_spaces_save(request: Request):
        form = await request.form()
        tracked = {str(v) for k, v in form.multi_items() if k == "track"}
        spaces = []
        for k, v in form.multi_items():
            if k.startswith("title__"):
                sid = k[len("title__"):]
                spaces.append({"id": int(sid), "title": str(v), "track": sid in tracked})
        config.update_settings("kaiten", {"spaces": spaces})
        return back(request, f"Отслеживаю пространств: {len(tracked)}. Синхронизация — во вкладке «Журнал» или через минуту")

    @app.post("/settings/kaiten/targets/add")
    def kaiten_target_add(request: Request, name: str = Form(...), space_id: int = Form(...), board_id: int = Form(...),
                          column_id: str = Form(""), lane_id: str = Form(""), path: str = Form("")):
        settings = config.load_settings()
        targets = [t for t in settings["kaiten"].get("targets") or [] if t["name"].lower() != name.strip().lower()]
        targets.append({
            "name": name.strip(), "space_id": int(space_id), "board_id": int(board_id),
            "column_id": int(column_id) if column_id.strip().isdigit() else None,
            "lane_id": int(lane_id) if lane_id.strip().isdigit() else None, "path": path.strip(),
        })
        update = {"targets": targets}
        if not settings["kaiten"].get("default_target"):
            update["default_target"] = name.strip()
        config.update_settings("kaiten", update)
        return back(request, f"Направление «{name.strip()}» добавлено")

    @app.post("/settings/kaiten/targets/delete")
    def kaiten_target_delete(request: Request, name: str = Form(...)):
        settings = config.load_settings()
        targets = [t for t in settings["kaiten"].get("targets") or [] if t["name"] != name]
        update = {"targets": targets}
        if settings["kaiten"].get("default_target") == name:
            update["default_target"] = targets[0]["name"] if targets else ""
        config.update_settings("kaiten", update)
        return back(request, "Удалено")

    @app.post("/settings/kaiten/targets/default")
    def kaiten_target_default(request: Request, name: str = Form(...)):
        config.update_settings("kaiten", {"default_target": name})
        return back(request, f"По умолчанию: «{name}»")

    # --- записи ---

    @app.post("/settings/recordings")
    def recordings_save(request: Request, folders: str = Form(""), whisper_model: str = Form("large-v3-turbo"),
                        device: str = Form("cpu"), engine: str = Form("auto"), language: str = Form("ru"),
                        diarization: str = Form(None),
                        diarization_threshold: float = Form(0.9), auto_process: str = Form(None),
                        hf_endpoint: str = Form("")):
        config.update_settings("recordings", {
            "folders": lines(folders) or ["data/recordings"], "whisper_model": whisper_model, "device": device,
            "engine": engine if engine in ("auto", "faster-whisper") else "auto",
            "language": language.strip() or "ru", "diarization": checkbox(diarization),
            "diarization_threshold": float(diarization_threshold), "auto_process": checkbox(auto_process),
            "hf_endpoint": hf_endpoint.strip(),
        })
        missing = [f for f in lines(folders) if not config.resolve_path(f).exists()]
        if missing:
            return back(request, err="Сохранено, но этих папок нет: " + "; ".join(missing))
        return back(request, "Сохранено")

    # --- документы ---

    @app.post("/settings/knowledge")
    def knowledge_save(request: Request, folders: str = Form(""), exclude: str = Form(None)):
        from opsdash import daemon, jobs

        config.update_settings("knowledge", {"folders": lines(folders),
                                             **({"exclude": lines(exclude)} if exclude is not None else {})})
        if lines(folders) and jobs.in_daemon() and config.section_on("docs"):
            jobs.submit("docs-index")  # сразу, а не через полчаса
        daemon.sync_claude_settings()
        missing = [f for f in lines(folders) if not config.resolve_path(f).exists()]
        if missing:
            return back(request, err="Сохранено, но этих папок нет: " + "; ".join(missing))
        return back(request, "Сохранено")

    # --- офис ---

    @app.post("/settings/office")
    def office_save(request: Request, folders: str = Form(""), hide: str = Form(""), days: int = Form(7),
                    agent_folder: str = Form("")):
        from opsdash import agents

        config.update_settings("office", {"folders": lines(folders), "hide": lines(hide), "days": max(1, min(days, 60))})
        config.update_settings("office", {"agent_folder": agent_folder if agents.allowed_folder(agent_folder) else ""})
        missing = [f for f in lines(folders) if not config.resolve_path(f).exists()]
        if missing:
            return back(request, err="Сохранено, но этих папок нет: " + "; ".join(missing))
        return back(request, "Сохранено")

    # --- автономия ---

    @app.post("/settings/autonomy")
    async def autonomy_save(request: Request):
        form = await request.form()
        values = {k: ("auto" if v == "auto" else "confirm") for k, v in form.items() if k in config.DEFAULTS["autonomy"]}
        config.update_settings("autonomy", values)
        return back(request, "Сохранено")

    # --- расписание ---

    @app.post("/settings/schedule")
    def schedule_save(request: Request, collect_minutes: int = Form(15), triage_minutes: int = Form(120),
                      triage_batch: int | None = Form(None),
                      kaiten_minutes: int = Form(30), calendar_minutes: int = Form(30), digest_morning: str = Form(""),
                      digest_evening: str = Form(""), stale_check: str = Form(""), meeting_processed: str = Form(None),
                      urgent_immediately: str = Form(None), stale_pings: str = Form(None),
                      anthology_weekly: str = Form(None), context_weekly: str = Form(None),
                      projects_weekly: str = Form(None)):
        config.update_settings("schedule", {
            "collect_minutes": max(5, collect_minutes), "triage_minutes": max(15, triage_minutes),
            **({"triage_batch": max(0, triage_batch)} if triage_batch is not None else {}),
            "kaiten_minutes": max(10, kaiten_minutes), "calendar_minutes": max(10, calendar_minutes),
            "digest_morning": digest_morning, "digest_evening": digest_evening, "stale_check": stale_check,
            **({"anthology_weekly": anthology_weekly} if anthology_weekly is not None else {}),
            **({"context_weekly": context_weekly} if context_weekly is not None else {}),
            **({"projects_weekly": projects_weekly} if projects_weekly is not None else {}),
        })
        config.update_settings("notifications", {
            "meeting_processed": checkbox(meeting_processed), "urgent_immediately": checkbox(urgent_immediately),
            "stale_pings": checkbox(stale_pings),
        })
        return back(request, "Сохранено")

    @app.post("/settings/digests")
    def digests_save(request: Request, weekly_day: int = Form(5), weekly_time: str = Form(""),
                     groups_digest: str = Form(""), groups_to_telegram: str = Form(None), briefs_enabled: str = Form(None),
                     briefs_hours: int = Form(36), briefs_ignore: str = Form(""), reminder_template: str = Form("")):
        """Брифы к встречам, итоги недели, сводка групп и текст напоминаний коллегам."""
        template = reminder_template.strip() or config.DEFAULTS["reminders"]["template"]
        try:
            template.format(**{"имя": "Пётр", "задача": "отчёт", "срок": "до 30.10"})
        except (KeyError, IndexError, ValueError):
            return back(request, err="В тексте напоминания можно использовать только {имя}, {задача} и {срок}")
        config.update_settings("schedule", {"weekly_day": min(7, max(1, weekly_day)), "weekly_time": weekly_time,
                                            "groups_digest": groups_digest})
        config.update_settings("notifications", {"groups_digest": checkbox(groups_to_telegram)})
        settings = config.load_settings()
        settings["briefs"] = {**settings["briefs"], "enabled": checkbox(briefs_enabled),
                              "hours_ahead": min(168, max(2, briefs_hours)),
                              "ignore": [w.strip() for w in briefs_ignore.split(",") if w.strip()]}
        settings["reminders"] = {**settings["reminders"], "template": template}
        config.save_settings(settings)
        return back(request, "Сохранено")

    # --- Claude ---

    @app.post("/settings/claude")
    def claude_save(request: Request, path: str = Form(""), git_bash_path: str = Form(""),
                    model_meetings: str = Form("opus"), model_triage: str = Form("sonnet"),
                    model_digest: str = Form("sonnet"), model_ask: str = Form("sonnet"), timeout_minutes: int = Form(30),
                    chat_effort: str = Form("")):
        from opsdash import chat

        config.update_settings("claude", {
            "path": path.strip().strip('"'), "git_bash_path": git_bash_path.strip().strip('"'),
            "model_meetings": model_meetings, "model_triage": model_triage, "model_digest": model_digest,
            "model_ask": model_ask, "timeout_minutes": max(5, int(timeout_minutes)),
            "chat_effort": chat_effort if chat_effort in chat.EFFORTS else "",
        })
        return back(request, "Сохранено")

    @app.post("/settings/claude/limits")
    async def claude_limits(request: Request):
        """«Обновить» у лимитов подписки: самый короткий запуск Haiku — Claude Code сообщит заполненность лимитов."""
        from opsdash import brain

        try:
            await run_in_threadpool(brain.run, "Ответь одним словом: ок", model="haiku", job="limits", timeout_min=3,
                                    who="claude")  # лимиты подписки Claude — даже если сейчас работает ChatGPT
        except brain.BrainError as e:
            return back(request, err=str(e), to="/settings?tab=claude#usage")
        return back(request, "Лимиты обновлены", to="/settings?tab=claude#usage")

    @app.post("/settings/ai")
    def ai_save(request: Request, engine: str = Form("claude")):
        """Кто работает в PULSE. Запуски, которые уже идут, доработает прежний; новые — у выбранного (brain.turn)."""
        from opsdash import brain

        if engine not in brain.ENGINES:
            return back(request, err="Неизвестный выбор")
        was = brain.busy()
        config.update_settings("ai", {"engine": engine})
        note = (f" — {brain.ENGINES[was]} доработает начатое, дальше работает {brain.ENGINES[engine]}"
                if was and was != engine else "")
        # назад — откуда переключали: каталог «Подключений» или окно помощника
        return back(request, f"Теперь в PULSE работает {brain.ENGINES[engine]}{note}")

    @app.post("/settings/chatgpt")
    def chatgpt_save(request: Request, path: str = Form(""), model: str = Form("")):
        config.update_settings("chatgpt", {"path": path.strip().strip('"'), "model": model.strip()})
        return back(request, "Сохранено", to="/settings?tab=chatgpt")

    @app.post("/settings/chatgpt/login")
    async def chatgpt_login(request: Request):
        from opsdash import brain, codex

        try:
            await run_in_threadpool(codex.open_login)
        except brain.BrainError as e:
            return back(request, err=str(e), to="/settings?tab=chatgpt")
        where = "Терминал" if sys.platform == "darwin" else "окно PowerShell"
        return back(request, f"Открыла {where}: войдите в ChatGPT в браузере, затем обновите эту страницу",
                    to="/settings?tab=chatgpt")

    @app.post("/settings/claude/login")
    async def claude_login(request: Request):
        from opsdash import brain

        try:
            await run_in_threadpool(brain.open_login)
        except brain.BrainError as e:
            return back(request, err=str(e), to="/settings?tab=claude")
        where = "Терминал" if sys.platform == "darwin" else "окно PowerShell"
        return back(request, f"Открыла {where}: войдите в браузере, затем обновите эту страницу", to="/settings?tab=claude")
