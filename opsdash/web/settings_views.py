"""Админка: подключения и настройки."""

from __future__ import annotations

import re

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from opsdash import config, store

TABS = [
    ("general", "Основное"),
    ("telegram", "Telegram"),
    ("bot", "Telegram-бот"),
    ("ymessenger", "Я.Мессенджер"),
    ("mail", "Почта"),
    ("calendar", "Календарь"),
    ("kaiten", "Kaiten"),
    ("recordings", "Записи встреч"),
    ("knowledge", "Документы"),
    ("autonomy", "Автономия"),
    ("schedule", "Расписание"),
    ("claude", "Claude"),
]
TAB_NOTES = {"telegram": "личный аккаунт", "bot": "уведомления"}  # приписка серым — чтобы не путать два Telegram
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


def register(app: FastAPI, render, back) -> None:

    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request, tab: str = "general", sort: str = "recent"):
        from opsdash import bot, brain
        from opsdash.sources import telegram

        settings = config.load_settings()
        secrets = config.load_secrets()
        ctx = {"tab": tab, "tabs": TABS, "tab_notes": TAB_NOTES, "secrets": {k: config.mask(v) for k, v in secrets.items()},
               "whisper_models": WHISPER_MODELS}
        if tab == "general":
            from opsdash import updates

            ctx["upd"], ctx["installed"] = updates.last_check(), updates.installed()
            ctx["updating"] = bool(request.query_params.get("updating"))
        if tab == "telegram":
            flow = telegram.login_flow
            ctx["tg_login_state"] = ("qr" if flow.qr_state else "code" if flow.client and flow.phone_code_hash else "")
            ctx["tg_qr"] = flow
            ctx["tg_me"] = store.get_state("telegram_me")
            # по умолчанию — по последнему сообщению: кто писал недавно, тот и сверху
            ctx["sort"] = "title" if sort == "title" else "recent"
            chats = store.rows("SELECT * FROM chats WHERE source='telegram' ORDER BY last_ts IS NULL, last_ts DESC, title")
            if sort == "title":  # по алфавиту без учёта кавычек и эмодзи в начале названия
                chats.sort(key=lambda c: re.sub(r"^[\W_]+", "", c["title"] or "").casefold())
            ctx["chats"] = chats
            ctx["chat_counts"] = {v: sum(c["enabled"] == v for c in chats) for v in (1, 0, -1)}
            ctx["chat_kinds"] = [(k, title, n) for k, title in CHAT_KINDS.items()
                                 if (n := sum(c["kind"] == k for c in chats))]
        elif tab == "bot":
            ctx["pair_code"] = bot.pair_code() if not settings["telegram_bot"].get("owner_chat_id") else ""
            ctx["bot_username"] = store.get_state("bot_username")
        elif tab == "ymessenger":
            ctx["chats"] = store.rows("SELECT * FROM chats WHERE source='ymessenger' ORDER BY title")
        elif tab == "kaiten":
            ctx["boards"] = store.rows("SELECT * FROM kaiten_boards ORDER BY title")
            ctx["kaiten_me"] = store.get_state("kaiten_me")
        elif tab == "calendar":
            from opsdash.sources import calendar

            ctx["google_urls"] = len(calendar.google_urls())
            ctx["calendar_names"] = calendar.write_calendar_names(settings)[0]
        elif tab == "claude":
            ctx["claude_path"] = brain.find_claude(settings)
            ctx["claude_login"] = brain.login_status(ctx["claude_path"], settings) if ctx["claude_path"] else None
        elif tab == "recordings":
            from opsdash import recordings

            from opsdash import voice

            ctx["existing"] = [str(p) for p in recordings.watch_folders(settings)]
            ctx["voice_samples"] = len(voice.samples())
        return render(request, "settings.html", nav="settings", **ctx)

    # --- проверки подключений ---

    @app.post("/settings/check/{what}")
    async def settings_check(what: str):
        from opsdash import bot, brain
        from opsdash.sources import calendar, kaiten, mail, telegram, ymessenger

        def bot_check():
            me = bot.get_bot().me()
            store.set_state("bot_username", me.get("username"))
            return f"бот @{me.get('username')}"

        checks = {
            "claude": brain.check, "telegram": telegram.check, "ymessenger": ymessenger.check, "mail": mail.check,
            "gmail": mail.check_gmail, "google_calendar": calendar.google_check,
            "calendar": calendar.check, "kaiten": kaiten.check, "bot": bot_check,
        }
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

    @app.post("/settings/updates")
    def save_updates(request: Request, repo: str = Form(""), branch: str = Form(""), token: str = Form(""),
                     auto_check: str = Form(None)):
        repo = repo.strip().removeprefix("https://github.com/").strip("/").removesuffix(".git")
        if repo and not re.fullmatch(r"[\w.-]+/[\w.-]+", repo):
            return back(request, err="Репозиторий — в виде «владелец/название», например vladsaxapof1/Operation-dash",
                        to="/settings?tab=general#updates")
        config.update_settings("updates", {"repo": repo, "branch": branch.strip() or "main",
                                           "auto_check": checkbox(auto_check)})
        if token.strip():
            config.set_secret("GITHUB_TOKEN", token.strip())
        return back(request, "Сохранено", to="/settings?tab=general#updates")

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
                   backfill_days: int = Form(3)):
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

    # --- почта ---

    @app.post("/settings/mail")
    def mail_save(request: Request, email: str = Form(""), password: str = Form(""), enabled: str = Form(None),
                  folders: str = Form("INBOX"), include_sent: str = Form(None), backfill_days: int = Form(3),
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
                   backfill_days: int = Form(3)):
        if password.strip():
            config.set_secret("GMAIL_APP_PASSWORD", password.strip().replace(" ", ""))  # Google показывает его с пробелами
        config.update_settings("gmail", {
            "enabled": checkbox(enabled), "email": email.strip(), "folders": lines(folders) or ["INBOX"],
            "include_sent": checkbox(include_sent), "primary_only": checkbox(primary_only),
            "backfill_days": int(backfill_days),
        })
        collect_soon("gmail")
        return back(request, "Сохранено", to="/settings?tab=mail#gmail")

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
                        to="/settings?tab=calendar#google")
        if new or checkbox(forget):
            config.set_secret("GOOGLE_CALENDAR_ICAL", " ".join(new) or None)
        config.update_settings("google_calendar", {"enabled": checkbox(enabled)})
        collect_soon("google_calendar")
        return back(request, "Сохранено", to="/settings?tab=calendar#google")

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
        if lines(folders) and jobs.in_daemon():
            jobs.submit("docs-index")  # сразу, а не через полчаса
        daemon.sync_claude_settings()
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
                      kaiten_minutes: int = Form(30), calendar_minutes: int = Form(30), digest_morning: str = Form(""),
                      digest_evening: str = Form(""), stale_check: str = Form(""), meeting_processed: str = Form(None),
                      urgent_immediately: str = Form(None), stale_pings: str = Form(None),
                      anthology_weekly: str = Form(None), context_weekly: str = Form(None)):
        config.update_settings("schedule", {
            "collect_minutes": max(5, collect_minutes), "triage_minutes": max(15, triage_minutes),
            "kaiten_minutes": max(10, kaiten_minutes), "calendar_minutes": max(10, calendar_minutes),
            "digest_morning": digest_morning, "digest_evening": digest_evening, "stale_check": stale_check,
            **({"anthology_weekly": anthology_weekly} if anthology_weekly is not None else {}),
            **({"context_weekly": context_weekly} if context_weekly is not None else {}),
        })
        config.update_settings("notifications", {
            "meeting_processed": checkbox(meeting_processed), "urgent_immediately": checkbox(urgent_immediately),
            "stale_pings": checkbox(stale_pings),
        })
        return back(request, "Сохранено")

    # --- Claude ---

    @app.post("/settings/claude")
    def claude_save(request: Request, path: str = Form(""), git_bash_path: str = Form(""),
                    model_meetings: str = Form("opus"), model_triage: str = Form("sonnet"),
                    model_digest: str = Form("sonnet"), model_ask: str = Form("sonnet"), timeout_minutes: int = Form(30)):
        config.update_settings("claude", {
            "path": path.strip().strip('"'), "git_bash_path": git_bash_path.strip().strip('"'),
            "model_meetings": model_meetings, "model_triage": model_triage, "model_digest": model_digest,
            "model_ask": model_ask, "timeout_minutes": max(5, int(timeout_minutes)),
        })
        return back(request, "Сохранено")

    @app.post("/settings/claude/login")
    async def claude_login(request: Request):
        from opsdash import brain

        await run_in_threadpool(brain.open_login)
        return back(request, "Открыла Терминал: войдите в браузере, затем обновите эту страницу", to="/settings?tab=claude")
