"""Веб-интерфейс PULSE: дашборд и админка на http://localhost:8765.

Сервер слушает только этот компьютер (127.0.0.1). Запросы с чужих сайтов
отсекаются проверкой заголовков Host/Origin.
"""

from __future__ import annotations

import functools
import html
import json
import os
import platform
import re
import shutil
import sys
import threading
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote, urlparse

import markdown as md
from markupsafe import Markup
from markdown.extensions.toc import slugify_unicode
from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from opsdash import __version__, briefs, config, digests, overview, proposals, records, store
from opsdash import day as day_mod
from opsdash.web import agent_views, settings_views

HERE = Path(__file__).parent
app = FastAPI(title="PULSE", docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")

DOCS_DIR = config.APP_DIR / "docs"
if (DOCS_DIR / "img").is_dir():
    app.mount("/help/img", StaticFiles(directory=DOCS_DIR / "img"), name="docs-img")


# --- фильтры шаблонов ---------------------------------------------------------

def f_local(value, fmt: str = "%d.%m %H:%M") -> str:
    return store.local(value, fmt)


def f_date(value) -> str:
    if not value:
        return ""
    try:
        y, m, d = str(value)[:10].split("-")
        return f"{d}.{m}"
    except ValueError:
        return str(value)


UNSAFE_URL = re.compile(r'(href|src)\s*=\s*"\s*(javascript|data|vbscript):[^"]*"', re.IGNORECASE)


def f_md(text: str) -> str:
    """Markdown → HTML. Сырой HTML в тексте экранируется: расшифровки и разборы приходят
    из файлов и переписки, им нельзя доверять."""
    text = (text or "").replace("<", "&lt;")
    out = md.markdown(
        text, extensions=["tables", "sane_lists", "nl2br", "toc"],
        extension_configs={"toc": {"slugify": slugify_unicode}},
    )
    # внутри `кода` Markdown экранирует & ещё раз: «&lt;» стало бы видно как есть — `C:\Users\&lt;вы>`
    out = re.sub(r"<code[^>]*>.*?</code>", lambda m: m.group(0).replace("&amp;lt;", "&lt;"), out, flags=re.S)
    # ссылки на задачи PULSE из разбора встречи (records.link_summary) — значком, а не обычной ссылкой
    out = out.replace('<a href="/commitments?task=', '<a class="task-ref" href="/commitments?task=')
    return UNSAFE_URL.sub(r'\1="#"', out)


def f_plain(text: str) -> str:
    """Markdown → простой текст для кнопки «Копировать» (в мессенджер): без решёток, звёздочек и ссылок-скобок."""
    out = []
    for line in (text or "").splitlines():
        line = re.sub(r"^#{1,6}\s+", "", line)
        line = re.sub(r"^(\s*)[-*]\s+", r"\1• ", line)
        line = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", line)
        out.append(line.replace("**", "").replace("__", ""))
    return "\n".join(out).strip()


def f_ago(value) -> str:
    dt = store.parse_ts(value) if isinstance(value, str) else value
    if not dt:
        return "—"
    delta = config.now() - dt.astimezone(config.tz())
    s = int(delta.total_seconds())
    if s < 60:
        return "только что"
    if s < 3600:
        return f"{s // 60} мин назад"
    if s < 86400:
        return f"{s // 3600} ч назад"
    return f"{s // 86400} дн назад"


def f_plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def f_tg_html(text: str) -> str:
    """Текст уведомления Telegram (HTML-подмножество) → безопасный HTML."""
    from markupsafe import Markup, escape

    safe = str(escape(text or ""))
    for tag in ("b", "i", "code"):
        safe = safe.replace(f"&lt;{tag}&gt;", f"<{tag}>").replace(f"&lt;/{tag}&gt;", f"</{tag}>")
    safe = re.sub(r"&lt;a href=&#34;(https?://[^&]+)&#34;&gt;(.*?)&lt;/a&gt;", r'<a href="\1" target="_blank">\2</a>', safe)
    # текст уже экранирован для Telegram (&quot; и т. п.) — не показываем эти сущности буквально
    safe = re.sub(r"&amp;(lt|gt|amp|quot|#\d+|#x[0-9a-fA-F]+);", r"&\1;", safe)
    return Markup(safe.replace("\n", "<br>"))


def f_people(attendees: list[dict] | None, limit: int = 3) -> str:
    """Участники встречи коротко: «p.orlov, ivanova, s.petrov и ещё 12». Переговорки Google пропускаем."""
    names = []
    for a in attendees or []:
        email, name = a.get("email") or "", a.get("name") or ""
        if email.endswith("resource.calendar.google.com") or "http" in name:
            continue
        names.append(name if name and "@" not in name else email.split("@")[0])
    rest = len(names) - limit
    return ", ".join(names[:limit]) + (f" и ещё {rest}" if rest > 0 else "")


URL_RE = re.compile(r"https?://[^\s<>\"]+")


def _link(url: str) -> str:
    """Короткая ссылка: вместо адреса целиком — имя сайта (us06web.zoom.us)."""
    from markupsafe import escape

    host = (urlparse(url).hostname or "ссылка").removeprefix("www.")
    return f'<a href="{escape(url)}" target="_blank">{escape(host)}</a>'


def _linkify(text: str) -> str:
    from markupsafe import Markup, escape

    out, pos = [], 0
    for m in URL_RE.finditer(text):
        out += [str(escape(text[pos:m.start()])), _link(m.group(0))]
        pos = m.end()
    out.append(str(escape(text[pos:])))
    return Markup("".join(out))


def f_place(location: str | None) -> str:
    """Место встречи со ссылками на звонок. В длинном месте (в Google туда вставляют приглашение
    Zoom целиком) оставляем только ссылки — по одной на сайт."""
    from markupsafe import Markup, escape

    location = location or ""
    if len(location) <= 100:
        return _linkify(location)
    first: dict = {}
    for m in URL_RE.finditer(location):
        first.setdefault(urlparse(m.group(0)).hostname, m.group(0))
    return Markup(" · ".join(_link(u) for u in first.values())) if first else Markup(escape(location[:80].rstrip() + "…"))


def f_hl(text: str | None) -> str:
    """Отрывок из поиска по документам: совпадения ⟦…⟧ — подсветкой."""
    from markupsafe import Markup, escape

    return Markup(str(escape(text or "")).replace("⟦", "<mark>").replace("⟧", "</mark>"))


def f_note(text: str | None, limit: int = 400) -> str:
    """Описание встречи: Google присылает его в HTML (<br>, ссылки) — показываем текстом, ссылки короткими."""
    from opsdash.sources import mail

    text = text or ""
    if re.search(r"<(br|p|div|a|b|i|span|ul|li)\b", text, re.I):
        text = mail.html_to_text(text)
    text = text.strip()
    return _linkify(text[:limit] + ("…" if len(text) > limit else ""))


def f_size(n: int | None) -> str:
    """Размер файла: «340 КБ», «2,4 МБ», «1,8 ГБ»."""
    if not n:
        return ""
    if n < 1024 * 1024:
        return f"{max(1, round(n / 1024))} КБ"
    if n < 1024 ** 3:
        return f"{n / 1024 / 1024:.1f} МБ".replace(".", ",")
    return f"{n / 1024 ** 3:.1f} ГБ".replace(".", ",")


def f_timecodes(html_text: str) -> Markup:
    """Тайм-коды «[00:12:40]» в разборе встречи — серым и мельче, как в макете 2.0."""
    return Markup(re.sub(r"\[(\d{1,2}:\d{2}:\d{2})\]", r'<span class="tc">\1</span>', str(html_text)))


templates.env.filters.update(timecodes=f_timecodes)
templates.env.filters.update(local=f_local, date=f_date, md=f_md, ago=f_ago, tg=f_tg_html, hhmm=config.hhmm,
                             people=f_people, place=f_place, note=f_note, hl=f_hl, size=f_size,
                             headline=records.headline, route=records.route, plain=f_plain)
templates.env.globals.update(
    plural=f_plural, version=__version__, SOURCE_TITLES=overview.SOURCE_TITLES,
    # метка для ?v= у app.js/style.css: меняется с файлами, и браузер не берёт старые из кэша
    static_v=f"{__version__}.{int(max(p.stat().st_mtime for p in (HERE / 'static').iterdir()))}",
    is_mac=sys.platform == "darwin", is_windows=os.name == "nt",
    is_apple_silicon=sys.platform == "darwin" and platform.machine() == "arm64",
    proposal_links=proposals.links,  # ссылки у предложений — для макросов (они не видят переменных страницы)
    proposal_action=proposals.action,  # надпись на кнопке подтверждения: «Закрыть задачу», «Напомнить»…
)
templates.env.filters.update(dur=day_mod.dur, hm=day_mod.hm, est=day_mod.est_text,  # «Реальность дня»
                             day_text=day_mod.day_text)
templates.env.globals.update(DAY_CHIPS=day_mod.CHIPS, SPLIT_HINT=day_mod.SPLIT_HINT,
                             day_people=lambda: [p["name"] for p in records.list_people()])  # «Передать» — кому


def render(request: Request, name: str, **ctx) -> HTMLResponse:
    settings = config.load_settings()
    ctx.setdefault("settings", settings)
    ctx.setdefault("nav", "")
    ctx["msg"] = request.query_params.get("msg", "")
    ctx["err"] = request.query_params.get("err", "")
    ctx["pending_count"] = store.scalar("SELECT COUNT(*) FROM proposals WHERE status='pending'") or 0
    ctx["attention_count"] = store.scalar("SELECT COUNT(*) FROM attention WHERE status='open'") or 0
    ctx["new_tasks_count"] = records.unseen_count()
    # меню: сколько ждёт в «Нужно от вас» (подтвердить, ответить, напомнить) и есть ли срочное; сколько задач в работе
    feed = ctx.get("feed") or needs_from_you()
    ctx["feed_count"], ctx["feed_urgent"] = feed["total"], feed["urgent"]
    ctx["tasks_count"] = records.active_count()
    ctx["sections_on"] = {name: config.section_on(name, settings) for name in config.SECTIONS}  # архивные — не в меню
    ctx["needs_setup"] = not any(settings[s].get("enabled")
                                 for s in ("telegram", "mail", "gmail", "calendar", "google_calendar", "kaiten",
                                           "ymessenger", "pachca"))
    ctx["voice_tip"] = None if ctx["nav"] == "meetings" else _voice_tip(request)
    ctx["build"] = build()
    ctx["nav_collapsed"] = request.cookies.get("nav") == "collapsed"  # меню свёрнуто в иконки (app.js)
    ctx["theme"] = settings["web"].get("theme") or "system"  # system / light / dark — подключение dark.css
    ctx["update_available"] = bool((store.get_state("update_check") or {}).get("available"))
    ctx["design_intro"] = store.get_state(store.DESIGN_INTRO) == "show"  # окно «У PULSE новый дизайн» — один раз
    from opsdash import notices

    ctx["notices"] = notices.recent()  # угол экрана: что PULSE сделал и что делает сейчас
    ctx["running_now"] = notices.running()
    from opsdash import brain

    ctx["ai_name"] = brain.name(settings)  # кто работает: «Claude» или «ChatGPT» — в меню и в «Спросить»
    return templates.TemplateResponse(request, name, ctx)


@app.get("/api/notices")
def notices_api(after: int = 0):
    """Для угла экрана на любой странице: новые уведомления (id больше after) и что PULSE делает сейчас."""
    from opsdash import notices

    return {"items": [{**n, "ago": f_ago(n["created"])} for n in notices.recent(after)], "running": notices.running()}


@app.post("/api/notices/dismiss-all")
def notices_dismiss_all():
    from opsdash import notices

    notices.dismiss_all()
    return {"ok": True}


@app.post("/api/notices/{notice_id}/dismiss")
def notices_dismiss(notice_id: int):
    from opsdash import notices

    notices.dismiss(notice_id)
    return {"ok": True}


@app.post("/api/design-intro/seen")
def design_intro_seen():
    """Окно «У PULSE новый дизайн» закрыли («Круто», Esc или клик мимо) — больше не показываем."""
    store.set_state(store.DESIGN_INTRO, "seen")
    return {"ok": True}


def _voice_tip(request: Request) -> dict | None:
    """Подсказка справа внизу: научить PULSE голосам. Пока ваш голос не известен — отметить себя в свежей
    записи; потом — подтвердить догадки Claude, кто говорит. Только по самой свежей записи со спикерами;
    «Позже» прячет её (cookie на неделю)."""
    from opsdash import voice

    owner_known = bool(voice.samples())
    for m in store.rows("SELECT id, title FROM meetings WHERE status IN ('transcribed', 'processed') "
                        "ORDER BY started_at DESC LIMIT 5"):
        try:
            meta = json.loads((records.meeting_dir(m["id"]) / "meta.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if len(meta.get("speakers") or []) < 2:
            continue
        title = m["title"] or m["id"]
        if not owner_known:
            tip = {"id": "me", "meeting": m["id"], "title": "Научите PULSE узнавать ваш голос", "button": "Открыть встречу",
                   "text": f"Откройте запись «{title}» и нажмите «Это я» у своего спикера. Дальше PULSE будет узнавать "
                           "вас в записях сам — и разбор не перепутает, кто ставит задачи, а кто их получает."}
        else:
            who, _ = voice.speaker_map(meta)
            open_guesses = [k for k in voice.guesses(m["id"], meta) if k not in who]
            if not open_guesses:
                return None
            n = len(open_guesses)
            tip = {"id": m["id"], "meeting": m["id"], "title": "Кто говорит во встрече?", "button": "Подтвердить",
                   "text": f"Claude предположил имена {n} {f_plural(n, 'спикера', 'спикеров', 'спикеров')} в «{title}». "
                           "Подтвердите — PULSE запомнит голоса и в следующих записях подпишет людей сам."}
        return None if request.cookies.get("voice_tip") == tip["id"] else tip
    return None


def back(request: Request, msg: str = "", err: str = "", to: str | None = None) -> RedirectResponse:
    target = to or request.headers.get("referer") or "/"
    parsed = urlparse(target)
    path = parsed.path or "/"
    query = "&".join(p for p in (parsed.query or "").split("&") if p and not p.startswith(("msg=", "err=")))
    if msg:
        query = (query + "&" if query else "") + "msg=" + quote(msg)
    if err:
        query = (query + "&" if query else "") + "err=" + quote(err)
    return RedirectResponse(path + ("?" + query if query else "") + (f"#{parsed.fragment}" if parsed.fragment else ""),
                            status_code=303)


# --- защита: только этот компьютер ------------------------------------------------

ALLOWED_HOSTS = {"localhost", "127.0.0.1"}
SECURITY_HEADERS = {
    # скрипты — только наши файлы: внедрённый в расшифровку код не выполнится
    "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                               "img-src 'self' data:; object-src 'none'; frame-ancestors 'none'; base-uri 'none'",
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    # не no-referrer: при нём браузер шлёт у наших же форм «Origin: null», и проверка ниже их отклоняет
    "Referrer-Policy": "same-origin",
}


def _archived_section(path: str) -> str:
    """Страница архивного раздела (Документы, База знаний) — или пусто. Сводка контекста (/kb/context) — не раздел."""
    for name, prefix in (("docs", "/docs"), ("kb", "/kb")):
        if (path == prefix or path.startswith(prefix + "/")) and path != "/kb/context" and not config.section_on(name):
            return name
    return ""


@app.middleware("http")
async def archived_sections(request: Request, call_next):
    """Архивный раздел не открывается: ведём в Настройки → Архивные разделы, где его можно включить."""
    if _archived_section(request.url.path):
        return RedirectResponse("/settings?tab=archive", status_code=303)
    return await call_next(request)


@app.middleware("http")
async def local_only(request: Request, call_next):
    host = (request.headers.get("host") or "").split(":")[0]
    if host not in ALLOWED_HOSTS:
        return PlainTextResponse("Доступ только с этого компьютера", status_code=403)
    if request.method == "POST":
        origin = request.headers.get("origin") or request.headers.get("referer") or ""
        if origin and urlparse(origin).hostname not in ALLOWED_HOSTS:
            return PlainTextResponse("Запрос с чужого сайта отклонён", status_code=403)
    response = await call_next(request)
    for key, value in SECURITY_HEADERS.items():
        response.headers.setdefault(key, value)
    if request.url.path.startswith("/static/"):  # после обновления браузер не должен держать старый app.js
        response.headers.setdefault("Cache-Control", "no-cache")
    return response


# --- дайджесты -------------------------------------------------------------------

DIGEST_TABS = [("daily", "Ежедневные"), ("weekly", "Итоги недели"), ("groups", "Группы Telegram")]
DIGEST_JOBS = {"daily": "digest", "weekly": "weekly", "groups": "groups"}


@app.get("/digests", response_class=HTMLResponse)
def digests_page(request: Request, tab: str = "daily", d: int = 0):
    """Дайджесты (5.12): слева — список по дням, справа — выбранный, читаемым текстом, с «Копировать»."""
    tab = tab if tab in DIGEST_JOBS else "daily"
    items = digests.latest(tab)
    tz = config.tz()
    for it in items:
        it["local"] = store.parse_ts(it["created"]).astimezone(tz)
        it["day_label"] = _day_label(it["local"].date()).capitalize()
        it["preview"] = re.sub(r"<[^>]+>", " ", it["text"] or "")
        it["preview"] = re.sub(r"\s+", " ", it["preview"]).strip()
    current = next((it for it in items if it["id"] == d), items[0] if items else None)
    sch = config.load_settings()["schedule"]
    nxt = None
    if tab == "daily":
        now = config.now()
        for t in (sch.get("digest_morning"), sch.get("digest_evening")):
            if t:
                h, m = (int(x) for x in config.hhmm(t).split(":"))
                at = now.replace(hour=h, minute=m, second=0, microsecond=0)
                at = at if at > now else at + timedelta(days=1)
                nxt = at if nxt is None or at < nxt else nxt
    return render(request, "digests.html", nav="digests", tab=tab, tabs=DIGEST_TABS, items=items, current=current,
                  counts=digests.counts(), weekdays=overview.WEEKDAYS, next_at=nxt,
                  next_label=_day_label(nxt.date()).split(",")[0] if nxt else "")


@app.post("/digests/{did}/resend")
def digest_resend(request: Request, did: int):
    """«Отправить ещё раз»: тот же текст — снова в ваш Telegram-бот."""
    from opsdash import bot

    d = store.row("SELECT * FROM digests WHERE id=?", (did,))
    if not d:
        return back(request, err="Такого дайджеста нет")
    if not config.load_settings()["telegram_bot"].get("enabled"):
        return back(request, err="Telegram-бот не подключён — Настройки → Подключения")
    bot.notify(d["text"])
    return back(request, "Отправлено в Telegram-бот")


@app.post("/digests/run")
def digests_run(request: Request, tab: str = Form("daily")):
    from opsdash import jobs

    if tab not in DIGEST_JOBS:
        return back(request, err="Нет такого дайджеста")
    jobs.submit(DIGEST_JOBS[tab], "heavy")
    return back(request, "Claude собирает дайджест — обычно 1–3 минуты, обновите страницу позже")


# --- служебное ------------------------------------------------------------------

@app.get("/health")
def health():
    return {"ok": True, "version": __version__, "build": build()}


@functools.lru_cache(maxsize=1)
def build() -> str:
    """Коммит запущенной версии (не меняется до перезапуска) — для меню и «обновление закончилось»."""
    from opsdash import updates

    return updates.installed()["sha"][:7]


@app.post("/updates/check")
async def updates_check(request: Request):
    from opsdash import updates

    try:
        res = await run_in_threadpool(updates.check)
    except updates.UpdateError as e:
        return back(request, err=f"Не удалось проверить обновления: {e}", to="/settings?tab=updates")
    return back(request, "Доступна новая версия — можно обновить" if res["available"] else "Стоит последняя версия",
                to="/settings?tab=updates")


@app.post("/updates/apply")
def updates_apply(request: Request):
    from opsdash import jobs

    jobs.submit("update", "interactive")
    return back(request, to="/settings?tab=updates&updating=1")


@app.post("/api/shutdown")
def api_shutdown():
    from opsdash import daemon

    daemon.shutdown()
    return {"ok": True}


@app.get("/api/status")
def api_status(waiting: int = 0):
    from opsdash import jobs, recordings

    meetings = [{"id": m["id"], "status": m["status"], "progress": m["progress"]}
                for m in store.rows("SELECT id, status, progress FROM meetings WHERE status IN ('new','transcribing','paused','processing')")]
    running = [r["name"] for r in store.rows("SELECT name FROM jobs WHERE running=1")]
    out = {"meetings": meetings, "running": running, "queued": jobs.queued()}
    if waiting:  # список встреч: найденные, но ещё не взятые записи — когда встреча появится, страница обновится
        out["waiting"] = [[w["path"], w["growing"]] for w in recordings.waiting()]
    return out


# --- главная --------------------------------------------------------------------

WEEKDAYS_FULL = ("Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье")
MONTHS_GEN = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября",
              "ноября", "декабря")


def needs_from_you(snap: dict | None = None, *, due: bool = False) -> dict:
    """«Нужно от вас» (5.1): всё, что ждёт директора, одной лентой — подтвердить, ответить, напомнить. Срочное и
    просроченное сверху, потом подтверждения, ответы, напоминания, карточки Kaiten без движения. due — ещё строка
    «Уточнить срок» (задачи без срока) в самом низу: только в ленте на Сводке, в счётчик меню она не входит."""
    from opsdash.sources import kaiten

    settings = config.load_settings()
    props = snap["proposals"] if snap else proposals.list_proposals("pending")
    attention = snap["attention"] if snap else records.list_attention("open")
    stale = snap["stale"] if snap else kaiten.stale_cards(settings)
    reminders = records.reminders()
    items: list[dict] = []
    for a in attention:
        items.append({"kind": "answer", "a": a, "urgent": a["priority"] == "high",
                      "order": 0 if a["priority"] == "high" else (3 if a["priority"] == "low" else 2)})
    for c in reminders:
        items.append({"kind": "remind", "c": c, "urgent": c["overdue"], "order": 0 if c["overdue"] else 4})
    batches: dict[str, list] = {}
    for p in props:
        batches.setdefault(p["batch"] or f"single:{p['id']}", []).append(p)
    for batch, group in batches.items():
        if len(group) > 1:
            items.append({"kind": "confirm", "batch": batch, "group": group, "urgent": False, "order": 1})
        else:
            items.append({"kind": "confirm", "p": group[0], "urgent": False, "order": 1})
    for k in stale:
        items.append({"kind": "remind", "kaiten": k, "urgent": False, "order": 5})
    no_due = records.due_queue() if due else []
    if no_due:
        items.append({"kind": "due", "tasks": no_due, "urgent": False, "order": 6})
    items.sort(key=lambda i: i["order"])
    counts = {"confirm": len(props), "answer": len(attention), "remind": len(reminders) + len(stale), "due": len(no_due)}
    return {"items": items, "counts": counts, "total": sum(counts.values()) - counts["due"],
            "urgent": any(i["urgent"] for i in items)}


def due_choices() -> list[dict]:
    """Быстрые сроки окна «Уточнить срок»: «Завтра · ср, 04.11» — по дате видно, что выберет кнопка."""
    from opsdash import gantt

    return [{"label": label, "iso": d.isoformat(), "text": f"{gantt.WEEKDAYS[d.weekday()].lower()}, {d:%d.%m}"}
            for label, d in records.due_choices()]


def _next_collect(settings: dict) -> datetime | None:
    """Когда PULSE в следующий раз соберёт сообщения и встречи — для строки «Следующий сбор в …»."""
    sch, best = settings["schedule"], None
    for src, minutes in (("telegram", sch["collect_minutes"]), ("ymessenger", sch["collect_minutes"]),
                         ("pachca", sch["collect_minutes"]), ("mail", sch["collect_minutes"]),
                         ("gmail", sch["collect_minutes"]), ("calendar", sch["calendar_minutes"]),
                         ("google_calendar", sch["calendar_minutes"]), ("kaiten", sch["kaiten_minutes"])):
        if not settings[src].get("enabled"):
            continue
        last = store.parse_ts((store.row("SELECT last_run FROM jobs WHERE name=?", (f"collect:{src}",)) or {}).get("last_run"))
        due = last + timedelta(minutes=minutes) if last else datetime.now(config.tz(settings))
        best = due if best is None or due < best else best
    return best.astimezone(config.tz(settings)) if best else None


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    from opsdash import notices, task_meetings

    settings = config.load_settings()
    snap = overview.snapshot()
    feed = needs_from_you(snap, due=True)
    no_due = next((i["tasks"] for i in feed["items"] if i["kind"] == "due"), [])
    meeting_ids = {i["batch"][8:] for i in feed["items"] if i.get("batch", "").startswith("meeting:")}
    meeting_ids |= {c["source_ref"] for c in no_due if c["source_type"] == "meeting" and c["source_ref"]}
    meetings = {m["id"]: m for m in store.rows(
        f"SELECT id, title, started_at FROM meetings WHERE id IN ({','.join('?' * len(meeting_ids))})",
        list(meeting_ids))} if meeting_ids else {}
    day = overview.day_grid()
    briefs.for_events(day["events"])
    task_meetings.for_events(day["events"])
    now = snap["now"]
    dp = day_mod.plan(settings=settings, now=now)  # «Реальность дня»: окна, спрос, вердикт и план — без ИИ
    day_mod.decorate(day, dp)
    dp["week"] = day_mod.week(settings=settings, now=now)  # «На неделю: …» — в понедельник и когда впереди перегруз
    timed = [e for e in day["events"] if not e["all_day"] and not e.get("day_kind")]  # фокус-блок — не встреча
    busy_min = sum(e - s for s, e in day_mod.union([(m["s"], m["e"]) for m in dp["meetings"]]))
    left = [e for e in timed if (e.get("e_local") or e["s_local"]) > now]
    end_h, end_m = (int(x) for x in (settings["owner"]["work_hours"].get("end") or "20:00").split(":"))
    start_h, start_m = (int(x) for x in (settings["owner"]["work_hours"].get("start") or "09:00").split(":"))
    work_left = int((now.replace(hour=end_h, minute=end_m, second=0, microsecond=0) - now).total_seconds() // 60)
    before_start = now.hour * 60 + now.minute < start_h * 60 + start_m
    return render(request, "home.html", nav="home", s=snap, feed=feed, meetings=meetings, no_due=no_due,
                  meeting_titles={k: m["title"] for k, m in meetings.items()}, due_choices=due_choices(),
                  dp=dp, next_label=day_mod.next_label(now.date(), settings), meet_n=len(timed),
                  describe=proposals.describe, day=day, busy_min=busy_min, left=left, work_left=work_left,
                  before_start=before_start, work_start=f"{start_h:02d}:{start_m:02d}",
                  weekday=WEEKDAYS_FULL[now.weekday()], date_text=f"{now.day} {MONTHS_GEN[now.month - 1]}",
                  log=[n for n in notices.recent(limit=8) if n["level"] != "bad"][:4],
                  next_collect=_next_collect(settings), hide_setup=request.cookies.get("hide_setup") == "1")


@app.post("/refresh")
def refresh(request: Request):
    from opsdash import jobs

    settings = config.load_settings()
    for src in ("telegram", "ymessenger", "pachca", "mail", "gmail", "calendar", "google_calendar", "kaiten"):
        if settings[src].get("enabled"):
            jobs.submit(f"collect:{src}")
    jobs.submit("recordings")
    return back(request, "Запустила сбор данных — обновите страницу через минуту")


# --- предложения -----------------------------------------------------------------

@app.post("/proposals/{pid}/{action}")
def proposal_action(request: Request, pid: int, action: str):
    if action == "approve":
        p = proposals.approve(pid)
        if p["status"] == "failed":
            return back(request, err=proposals.result_text(p))
        return back(request, proposals.result_text(p))
    proposals.reject(pid)
    return back(request, "Отклонено")


@app.post("/proposals/batch")
def proposal_batch(request: Request, batch: str = Form(...), top: int = Form(...), action: str = Form("approve")):
    # только показанные на странице: с номерами не больше top
    items = [p for p in proposals.list_proposals("pending", batch=batch) if p["id"] <= top]
    results = [proposals.approve(p["id"]) if action == "approve" else proposals.reject(p["id"]) for p in items]
    failed = [p for p in results if p["status"] == "failed"]
    if failed:
        return back(request, err=f"Не получилось: {len(failed)} из {len(results)}. " + proposals.result_text(failed[0]))
    return back(request, f"Готово: {len(results)}")


# --- нужен ответ ------------------------------------------------------------------

@app.get("/inbox")
def inbox_redirect(request: Request):
    """«Нужен ответ» растворился в Сводке, переписка — в разделе «Переписка» (2.0); старые ссылки из бота ведут туда."""
    query = str(request.url.query)
    return RedirectResponse("/chats" + ("?" + query if query else ""), status_code=303)


CHATS_SHOWN = 8      # в списке слева — столько последних чатов каждого источника, остальные — под «Ещё N чатов»
CHAT_PAGE = 80       # сообщений чата за раз; более ранние подгружаются при прокрутке вверх (app.js)


@app.get("/chats", response_class=HTMLResponse)
def chats_page(request: Request, chat: str = "", q: str = "", n: int = CHAT_PAGE):
    """Переписка (5.11): окно в чаты, которые PULSE читает, — как мессенджер. Слева — последние чаты по источникам,
    справа — выбранный чат: новые сообщения внизу, при прокрутке вверх подгружаются более ранние. Сообщения,
    вынесенные в Сводку («ждёт вашего ответа»), подсвечены. Писать в чаты PULSE не умеет."""
    from opsdash import jobs, links

    settings = config.load_settings()
    attention = records.list_attention("open")
    by_chat: dict[str, list] = {}
    for a in attention:
        by_chat.setdefault(f"{a.get('source')}|{a.get('chat_id')}", []).append(a)
    chats = store.rows(
        """SELECT c.*, (SELECT text FROM messages m WHERE m.source=c.source AND m.chat_id=c.chat_id ORDER BY ts DESC LIMIT 1) AS last_text,
                  (SELECT sender_name FROM messages m WHERE m.source=c.source AND m.chat_id=c.chat_id ORDER BY ts DESC LIMIT 1) AS last_sender,
                  (SELECT is_outgoing FROM messages m WHERE m.source=c.source AND m.chat_id=c.chat_id ORDER BY ts DESC LIMIT 1) AS last_out,
                  (SELECT MIN(ts) FROM messages m WHERE m.source=c.source AND m.chat_id=c.chat_id) AS first_ts
           FROM chats c WHERE c.enabled=1 ORDER BY c.last_ts DESC LIMIT 300""")
    for c in chats:
        c["key"] = f"{c['source']}|{c['chat_id']}"
        c["att"] = by_chat.get(c["key"], [])
        c["urgent"] = any(a["priority"] == "high" for a in c["att"])
    groups: dict[str, list] = {}
    order = {"telegram": 0, "mail": 1, "gmail": 2, "pachca": 3, "ymessenger": 4}
    for c in sorted(chats, key=lambda c: order.get(c["source"], 9)):
        groups.setdefault(c["source"], []).append(c)
    if not chat and chats:  # по умолчанию — чат, где ждут ответа, иначе самый свежий
        chat = next((c["key"] for c in chats if c["att"]), chats[0]["key"])
    current = next((c for c in chats if c["key"] == chat), None)
    for items in groups.values():  # под «Ещё» не прячем чаты, где ждут ответа, и открытый чат
        for i, c in enumerate(items):
            c["more"] = i >= CHATS_SHOWN and not c["att"] and c["key"] != chat
    messages, chat_link, more = [], None, 0
    n = max(CHAT_PAGE, min(n, 2000))
    if chat:
        source, _, chat_id = chat.partition("|")
        messages = list(reversed(store.rows(
            "SELECT * FROM messages WHERE source=? AND chat_id=? ORDER BY ts DESC LIMIT ?", (source, chat_id, n + 1))))
        if len(messages) > n:  # есть более ранние — ссылка «показать раньше» сверху ленты
            messages, more = messages[1:], min(n + CHAT_PAGE * 2, 2000)
        chat_link = links.original(source, chat_id)
        flagged = {a["message_id"]: a for a in attention if a.get("message_id")}
        tz = config.tz(settings)
        prev_day = None
        for m in messages:
            m["att"] = flagged.get(m["id"])
            ts = store.parse_ts(m["ts"])
            day = ts.astimezone(tz).date() if ts else None
            m["day_label"] = _day_label(day) if day and day != prev_day else ""
            prev_day = day
    results = []
    if q.strip():
        results = store.rows("SELECT * FROM messages WHERE text LIKE ? ORDER BY ts DESC LIMIT 60", (f"%{q.strip()}%",))
    closed = store.rows("SELECT * FROM attention WHERE status!='open' ORDER BY updated DESC LIMIT 15")
    off = [overview.SOURCE_TITLES.get(k, k) for k in ("telegram", "pachca", "ymessenger") if not settings[k].get("enabled")]
    off += ["почта"] if not (settings["mail"].get("enabled") or settings["gmail"].get("enabled")) else []
    last = store.row("SELECT ts, message FROM log WHERE job='triage' AND (message LIKE 'ок:%' OR level='error') "
                     "ORDER BY id DESC LIMIT 1")
    triage = {"busy": any(k.partition("|")[0] == "triage" for k in jobs.queued()),
              "pending": store.scalar("SELECT COUNT(*) FROM messages WHERE processed=0") or 0,
              "last_ts": last["ts"] if last else None,
              "last": last["message"].split("\n")[0].removeprefix("ок: ") if last else ""}
    return render(request, "inbox.html", nav="chats", groups=groups, chats=chats, current=current, chat=chat,
                  messages=messages, more=more, chat_link=chat_link, closed=closed, triage=triage, attention=attention,
                  chats_total=store.scalar("SELECT COUNT(*) FROM chats WHERE enabled=1") or 0,
                  off_sources=off, q=q.strip(), results=results,
                  synced=store.scalar("SELECT MAX(last_run) FROM jobs WHERE name LIKE 'collect:%'"))


def _day_label(d: date) -> str:
    today = config.now().date()
    if d == today:
        return "сегодня"
    if d == today - timedelta(days=1):
        return f"вчера, {d.day} {MONTHS_GEN[d.month - 1]}"
    return f"{d.day} {MONTHS_GEN[d.month - 1]}"


@app.post("/attention/{aid}/{action}")
def attention_action(request: Request, aid: int, action: str):
    records.close_attention(aid, "done" if action == "done" else ("open" if action == "reopen" else "dismissed"))
    return back(request, "Отмечено")


# --- задачи (в коде — commitments) --------------------------------------------------

BOARD_TITLES = [("mine", "Мои задачи"), ("to_me", "Мне должны"), ("other", "Между коллегами"), ("done", "Готово")]
BOARD_COLUMNS = tuple(key for key, _ in BOARD_TITLES)


LIST_GROUPS = {"dir": "кто кому", "project": "по проекту", "person": "по человеку", "none": "без групп"}  # вид «Список»
FOCUS_DAYS = 14  # задачи со сроком дальше — в конце колонки под пунктирной плашкой «Ещё N со сроком после …»
KAITEN_PARAMS = ("who", "space", "q", "mine", "only", "created", "created_from", "created_to", "due", "due_from", "due_to",
                 "kview")


@app.get("/commitments", response_class=HTMLResponse)
def commitments(request: Request, view: str = "board", status: str = "open", direction: str = "", person: str = "",
                q: str = "", add: str = "", created: str = "", created_from: str = "", created_to: str = "",
                due: str = "", due_from: str = "", due_to: str = "", scale: str = "week", sort: str = "",
                new: str = "", task: int = 0, parent: int = 0, archived: str = "", project: str = "",
                done_projects: str = "", done: str = "", group: str = ""):
    from opsdash import gantt, projects

    if view == "kaiten":  # Kaiten — вкладка «Задач» (5.7)
        return tasks(request, **{k: v for k, v in request.query_params.items() if k in KAITEN_PARAMS})
    view = view if view in ("list", "gantt", "projects") else "board"
    project = project if project == "none" or project.isdigit() else ""
    # «Показать архивные» — архивные задачи добавляются серыми в конец колонок и списка
    show_archived = archived == "1" or status == "archived"  # status=archived — ссылки из прошлой версии
    status = "all" if status == "archived" else status
    person, q = person.strip(), q.strip()
    # сортировку запоминаем в cookie: по умолчанию — по сроку, «сначала новые» — по дате появления
    sort = sort if sort in records.SORTS else request.cookies.get("task_sort", "due")
    sort = sort if sort in records.SORTS else "due"
    dates = {"created": records.period(created, created_from, created_to) if created else None,
             "due": records.period(due, due_from, due_to, ahead=True) if due and due != "none" else None,
             "no_due": due == "none", "unseen": new == "1"}
    board = (records.commitments_board(person=person or None, q=q or None, sort=sort, archived=show_archived,
                                       project=project or None, **dates) if view in ("board", "gantt") else None)
    items = (records.list_commitments(status=status, direction=direction or None, person=person or None, q=q or None,
                                      sort=sort, archived=show_archived, project=project or None, **dates)
             if view == "list" else None)
    shown = records.add_links([c for col in board.values() for c in col] if board else (items or []))
    if items is not None:  # «Список»: группы по проекту и по человеку (5.3)
        records.with_reminders(items)
        for c in items:
            c["project_title"] = (c.get("project") or {}).get("title") or ""
            c["person_key"] = c["whom"] if c["direction"] == "mine" else c["who"]
            c["route"] = records.route(c)
        if group in ("project", "person"):
            key = "project_title" if group == "project" else "person_key"
            items.sort(key=lambda c: (not c[key], c[key] or ""))  # groupby — подряд; «вне проектов» в конце
    # дальние сроки: открытые задачи со сроком позже чем через FOCUS_DAYS дней сейчас не в фокусе — в конце колонки
    # (группы списка, Ганта) под пунктирной плашкой, по щелчку раскрываются. «В работе» не прячем; при фильтре
    # по сроку и поиске показываем всё — их просили явно
    horizon = records.today_local() + timedelta(days=FOCUS_DAYS)
    fold = not q and not (due and due != "none")
    for c in shown:
        c["later"] = bool(fold and c["status"] == "open" and not c["archived"] and not c["in_work"]
                          and c["due"] and c["due"] > horizon.isoformat())
    # ?task=<id> — открыть окно задачи (ссылка со встречи или со сводки); если фильтры её прячут — окно всё равно есть
    extra = None
    if task and not any(c["id"] == task for c in shown):
        extra = records.get_commitment(task)
        shown = shown + records.add_links([extra]) if extra else shown
    meeting_ids = {c["source_ref"] for c in shown if c["source_type"] == "meeting" and c["source_ref"]}
    meetings = {m["id"]: m["title"] for m in store.rows(
        f"SELECT id, title FROM meetings WHERE id IN ({','.join('?' * len(meeting_ids))})", list(meeting_ids))} if meeting_ids else {}
    # свёрнутые колонки доски (и группы на таймлайне) запоминает браузер — cookie из app.js, чтобы не мелькали
    folded = {k for k in request.cookies.get("kb_folded", "").split(".") if k in BOARD_COLUMNS}
    # ширина колонки с названиями на таймлайне — её тянут мышкой, запоминает cookie (app.js)
    gantt_w = int(request.cookies["gantt_w"]) if request.cookies.get("gantt_w", "").isdigit() else 300
    timeline = gantt.layout([(key, title, board[key]) for key, title in BOARD_TITLES], scale) if view == "gantt" else None
    parent_task = next((o for o in records.task_options() if o["id"] == parent), None) if parent else None
    # активные фильтры — строкой под заголовком, у каждого «× снять» (5.2); их число — на кнопке «Фильтр»
    base = {k: v for k, v in request.query_params.items() if k not in ("msg", "err", "task", "add", "parent") and v}

    def without(*keys: str) -> str:
        return "?" + "&".join(f"{k}={quote(v)}" for k, v in base.items() if k not in keys)

    period_titles = dict(records.CREATED_PERIODS) | dict(records.DUE_PERIODS) | {"custom": "свой период", "none": "без срока"}
    proj_title = {str(o["id"]): o["title"] for o in projects.options()}.get(project, "вне проектов" if project == "none" else "")
    chips = [(label, without(*keys)) for label, keys, on in (
        (f"проект: {proj_title}", ("project",), bool(project)),
        (f"человек: {person}", ("person",), bool(person)),
        (f"создана: {period_titles.get(created, created)}", ("created", "created_from", "created_to"), bool(created)),
        (f"срок: {period_titles.get(due, due)}", ("due", "due_from", "due_to"), bool(due) and due != "none"),
        (records.DIRECTIONS.get(direction, direction), ("direction",), view == "list" and bool(direction)),
        ({"done": "выполненные", "all": "все статусы"}.get(status, status), ("status",), view == "list" and status != "open"),
        ("с архивными", ("archived",), show_archived),
        (f"поиск «{q}»", ("q",), bool(q)),
    ) if on]
    # «Без срока»: кнопка «Разобрать по одной» — окно «Уточнить срок» с показанными открытыми задачами без срока,
    # ваши и что вам должны: сроки договорённостей между коллегами директор не разбирает
    no_due = [c for c in ((board["mine"] + board["to_me"]) if board else items or [])
              if c["status"] == "open" and not c["due"] and not c["archived"] and c["direction"] != "other"
              ] if due == "none" else []
    # переключатели «Новые» и «Без срока» под заголовком (вместо «× снять» у этих двух фильтров): второй щелчок снимает
    toggles = {key: (without(*keys) + ("" if on else add)).replace("?&", "?", 1) for key, keys, on, add in (
        ("new", ("new",), new == "1", "&new=1"), ("no_due", ("due", "due_from", "due_to"), due == "none", "&due=none"))}
    response = render(request, "commitments.html", nav="commitments", view=view, board=board, items=items, status=status,
                      direction=direction, person=person, q=q, people=records.list_people(), meetings=meetings,
                      add=add if add in records.DIRECTIONS else "mine", today=records.today_local().isoformat(),
                      folded=folded, created=created, created_from=created_from, created_to=created_to, due=due,
                      due_from=due_from, due_to=due_to, created_periods=records.CREATED_PERIODS,
                      due_periods=records.DUE_PERIODS, gantt=timeline, scale=timeline["scale"] if timeline else scale,
                      gantt_w=min(max(gantt_w, 200), 720), sort=sort, sorts=records.SORTS, new=new == "1",
                      focus=task, extra=extra, parent_task=parent_task, show_archived=show_archived,
                      archived_count=store.scalar("SELECT COUNT(*) FROM commitments WHERE archived_at IS NOT NULL") or 0,
                      project=project, project_options=projects.options(),
                      projects=projects.list_projects("all" if done_projects else "active") if view == "projects" else [],
                      done_projects=bool(done_projects), show_done=done == "1",
                      group=group if group in LIST_GROUPS else "dir", list_groups=LIST_GROUPS,
                      filter_chips=chips, filter_count=sum(1 for c in chips if not c[0].startswith("поиск")),
                      no_due_count=records.no_due_count(), no_due=no_due, due_choices=due_choices() if no_due else [],
                      toggles=toggles, later_after=horizon,
                      in_projects=store.scalar("SELECT COUNT(*) FROM commitments WHERE status='open' AND archived_at IS NULL "
                                               "AND project_id IS NOT NULL") or 0,
                      done_projects_count=store.scalar("SELECT COUNT(*) FROM projects WHERE status='done'") or 0)
    if request.query_params.get("sort") in records.SORTS:
        response.set_cookie("task_sort", sort, max_age=31536000, samesite="lax")
    return response


@app.post("/commitments")
def commitment_add(request: Request, direction: str = Form(...), title: str = Form(""), what: str = Form(""),
                   who: str = Form(""), whom: str = Form(""), due: str = Form(""), parent: str = Form(""),
                   project: str = Form("")):
    if not (title.strip() or what.strip()):
        return back(request, err="Напишите, что сделать")
    try:
        cid = records.add_commitment(direction=direction, title=title.strip(), what=what.strip() or title.strip(),
                                     who=who.strip() or None, whom=whom.strip() or None, due=due or None,
                                     source_type="manual", parent_id=records.parse_task_ref(parent),
                                     project_id=int(project) if project.isdigit() else None)
    except ValueError as e:
        return back(request, err=str(e))
    similar = [c for c in records.similar_open(direction=direction, who=who.strip(), whom=whom.strip(), title=title,
                                               what=what, exclude=cid) if c["similarity"] >= 0.5]
    if similar:
        return back(request, f"Задача добавлена. Похожая уже есть: «{similar[0]['headline']}» — если это одно и то же, "
                             "откройте новую и выберите «Это дубль задачи…»")
    return back(request, "Задача добавлена")


@app.post("/commitments/{cid}")
def commitment_update(request: Request, cid: int, status: str = Form(None), due: str = Form(None)):
    records.update_commitment(cid, status=status or None, due=due or None)
    return back(request, "Сохранено")


@app.post("/commitments/{cid}/due")
def commitment_due(request: Request, cid: int, due: str = Form(""), act: str = Form("save")):
    """«Уточнить срок»: срок с быстрой кнопки или из календаря; act=skip — «Оставить без срока», больше не спрашивать.
    Окно разбора отправляет форму из app.js и ждёт JSON, без скриптов — обычный возврат на страницу."""
    as_json = "application/json" in request.headers.get("accept", "")
    err = "" if records.get_commitment(cid) else "Такой задачи нет"
    if not err and act != "skip" and not due.strip():
        err = "Выберите срок или «Оставить без срока»"
    if not err:
        try:
            records.set_due(cid, None if act == "skip" else due.strip())
        except ValueError as e:
            err = str(e)
    if as_json:
        return JSONResponse({"ok": not err, "err": err}, status_code=400 if err else 200)
    if err:
        return back(request, err=err)
    return back(request, "Оставлена без срока" if act == "skip" else f"Срок — {f_date(due.strip())}")


# --- реальность дня (opsdash.day) ---------------------------------------------------------------

@app.post("/commitments/{cid}/estimate")
def commitment_estimate(request: Request, cid: int, minutes: int = Form(0)):
    """Оценка в один клик из окна задачи: 15м · 30м · 1ч · 2ч · полдня. Ручную оценку Claude больше не меняет."""
    try:
        day_mod.set_estimate(cid, minutes or None, by="user")
    except ValueError as e:
        if "application/json" in request.headers.get("accept", ""):
            return JSONResponse({"ok": False, "err": str(e)}, status_code=400)
        return back(request, err=str(e))
    if "application/json" in request.headers.get("accept", ""):
        return JSONResponse({"ok": True, "note": "ваша оценка" if minutes else "не оценена — считаю 30 мин"})
    return back(request, f"Оценка — {day_mod.est_text(minutes)}" if minutes else "Оценка убрана")


def _day_review(after: bool = False) -> str:
    """Тело окна «Разобрать день» — заново после каждого решения: итог, окна и предложенное время пересчитаны."""
    settings = config.load_settings()
    p = day_mod.plan(settings=settings)
    return str(templates.env.get_template("_day.html").module.review_body(
        p, due_choices(), day_mod.next_label(p["date"], settings), after))


@app.get("/day/review", response_class=HTMLResponse)
def day_review(after: int = 0):
    return HTMLResponse(_day_review(bool(after)))


@app.post("/day/act")
def day_act(request: Request, cid: int = Form(...), act: str = Form(...), start: str = Form(""), due: str = Form(""),
            minutes: int = Form(0), who: str = Form(""), what: str = Form("")):
    """Решение из «Разобрать день» и с сетки дня: в план (окно или «авто»), перетащили блок, на следующий рабочий
    день, свой срок, снять срок, передать коллеге, оценка. Окно ждёт JSON с новым телом; без скриптов — возврат."""
    as_json = "application/json" in request.headers.get("accept", "")
    try:
        if act == "estimate":
            day_mod.set_estimate(cid, minutes or None, by="user")
            msg = f"Оценка — {day_mod.est_text(minutes)}" if minutes else "Оценка убрана"
        else:
            if act == "due" and not due.strip():
                raise ValueError("Выберите срок")
            msg = day_mod.decide(cid, act, start=start.strip(), due=due.strip(), who=who, what=what)
    except ValueError as e:
        if as_json:
            return JSONResponse({"ok": False, "err": str(e)}, status_code=400)
        return back(request, err=str(e))
    if as_json:
        return JSONResponse({"ok": True, "msg": msg, "html": _day_review(after=True)})
    return back(request, msg)


@app.post("/day/carry")
def day_carry(request: Request):
    """Вечером на Сводке — «Перенести незакрытое на завтра» одной кнопкой."""
    n = day_mod.carry_over()
    return back(request, f"Перенесено задач: {n}" if n else "Переносить нечего", to="/")


@app.post("/commitments/{cid}/state")
def commitment_state(request: Request, cid: int, state: str = Form("")):
    """Статус из окна задачи: Новая / В работе / Ждёт / Готово. «Ждёт» без связи — просим указать, чего ждёт."""
    c = records.get_commitment(cid)
    if not c or state not in ("new", "work", "wait", "done"):
        return back(request, err="Такой задачи нет")
    if state == "done":
        records.update_commitment(cid, status="done")
        return back(request, "Готово")
    if c["status"] != "open":
        records.update_commitment(cid, status="open")
    records.set_in_work(cid, state == "work")
    if state == "wait" and not records.get_commitment(cid)["blocked"]:
        return _back_task(request, cid, err="Укажите, чего ждёт задача: «+ связь» в окне задачи")
    return _back_task(request, cid, {"new": "Задача — новая", "work": "Задача в работе", "wait": "Задача ждёт"}[state])


@app.post("/commitments/{cid}/work")
def commitment_work(request: Request, cid: int, on: int = Form(1)):
    """«В работе»: карточка закрепляется сверху своей колонки и отмечается зелёным; повторное нажатие — снять."""
    records.set_in_work(cid, bool(on))
    return back(request, "Задача в работе" if on else "Отметка «в работе» снята")


@app.post("/commitments/{cid}/archive")
def commitment_archive(request: Request, cid: int, on: int = Form(1)):
    records.set_archived(cid, bool(on))
    return back(request, "Задача в архиве — увидеть её: «Показать архивные» в фильтрах" if on
                else "Задача возвращена из архива")


@app.post("/commitments/{cid}/edit")
def commitment_edit(request: Request, cid: int, title: str = Form(""), what: str = Form(""),
                    direction: str = Form("mine"), who: str = Form(""), whom: str = Form(""), due: str = Form(""),
                    parent: str = Form(""), project: str = Form(""), has_project: str = Form("")):
    """project: «#3 · Запуск склада» из подсказок, пусто — ни в каком проекте; без has_project (поля нет в форме) —
    не менять: пустое поле FastAPI не отличает от отсутствующего."""
    if not has_project:
        project_id = -1
    elif project.strip():
        project_id = records.parse_task_ref(project) if project.strip().startswith("#") else None
        if not project_id:
            return back(request, err="Выберите проект из подсказок — начните вводить его название")
    else:
        project_id = None
    try:
        records.edit_commitment(cid, title=title, what=what, direction=direction, who=who, whom=whom, due=due or None,
                                parent_id=records.parse_task_ref(parent), project_id=project_id)
    except ValueError as e:
        return back(request, err=str(e))
    return back(request, "Задача изменена")


def _back_task(request: Request, cid: int, msg: str = "", err: str = "") -> RedirectResponse:
    """Назад туда же (вид, фильтры), но с открытым окном задачи: правишь пункты или связи — окно не закрывается."""
    ref = urlparse(request.headers.get("referer") or "/commitments")
    query = "&".join(p for p in ref.query.split("&") if p and not p.startswith(("task=", "msg=", "err=")))
    return back(request, msg, err, to=f"{ref.path or '/commitments'}?{query + '&' if query else ''}task={cid}")


@app.post("/commitments/{cid}/items")
def commitment_items_add(request: Request, cid: int, text: str = Form("")):
    """Пункты внутри задачи: один или несколько через «;»."""
    try:
        added = records.add_items(cid, text)
    except ValueError as e:
        return _back_task(request, cid, err=str(e))
    return _back_task(request, cid, "Пункт добавлен" if len(added) == 1 else f"Пунктов добавлено: {len(added)}")


@app.post("/commitments/{cid}/items/{item_id}/done")
def commitment_item_done(request: Request, cid: int, item_id: int, on: int = Form(1)):
    if store.row("SELECT 1 FROM task_items WHERE id=? AND task_id=?", (item_id, cid)):
        records.set_item_done(item_id, bool(on))
    return _back_task(request, cid)


@app.post("/commitments/{cid}/items/{item_id}/remove")
def commitment_item_remove(request: Request, cid: int, item_id: int):
    if store.row("SELECT 1 FROM task_items WHERE id=? AND task_id=?", (item_id, cid)):
        records.remove_item(item_id)
    return _back_task(request, cid, "Пункт убран")


@app.post("/commitments/{cid}/wait")
def commitment_wait(request: Request, cid: int, target: str = Form(""), until: str = Form("")):
    """«Ждёт»: задачу из подсказок («#69 · …») или событие своими словами («встреча Олега с собственником»)."""
    m = re.match(r"\s*#(\d+)\b", target)
    try:
        if m:
            records.add_wait(cid, waits_for=int(m.group(1)))
        else:
            records.add_wait(cid, event=target, until=until or None)
    except ValueError as e:
        return _back_task(request, cid, err=str(e))
    return _back_task(request, cid, "Связь добавлена: задача ждёт" + (" другую" if m else " событие"))


@app.post("/commitments/{cid}/wait/{link_id}/remove")
def commitment_unwait(request: Request, cid: int, link_id: int):
    if store.row("SELECT 1 FROM task_links WHERE id=? AND task_id=?", (link_id, cid)):
        records.remove_wait(link_id)
    return _back_task(request, cid, "Связь убрана")


@app.post("/commitments/{cid}/wait/{link_id}/done")
def commitment_event_done(request: Request, cid: int, link_id: int, on: int = Form(1)):
    """Событие, которого ждала задача, случилось (on=0 — ещё нет)."""
    if not store.row("SELECT 1 FROM task_links WHERE id=? AND task_id=?", (link_id, cid)):
        return back(request, err="Такой связи у задачи нет")
    records.set_event_done(link_id, bool(on))
    return _back_task(request, cid, "Отмечено: случилось" if on else "Снова ждём")


@app.post("/commitments/{cid}/wait/{link_id}/kaiten")
def commitment_wait_kaiten(request: Request, cid: int, link_id: int):
    """«Заблокировать в Kaiten»: директор сама нажала — блокировка ставится сразу, карточкой той задачи."""
    link = store.row("SELECT * FROM task_links WHERE id=? AND task_id=? AND waits_for IS NOT NULL", (link_id, cid))
    c, other = records.get_commitment(cid), records.get_commitment(link["waits_for"]) if link else None
    if not link or not c or not other or not c["kaiten_card_id"] or not other["kaiten_card_id"]:
        return _back_task(request, cid, err="У обеих задач должны быть карточки Kaiten")
    try:
        p = proposals.propose_kaiten_block(card_id=c["kaiten_card_id"], blocker_card=other["kaiten_card_id"],
                                           text=f"Ждёт «{records.headline(other)}»", reason="вы нажали «Заблокировать в Kaiten»")
    except proposals.ProposalError as e:
        return _back_task(request, cid, err=str(e))
    p = proposals.approve(p["id"])
    if p["status"] == "failed":
        return _back_task(request, cid, err=proposals.result_text(p))
    return _back_task(request, cid, proposals.result_text(p))


@app.post("/commitments/{cid}/merge")
def commitment_merge(request: Request, cid: int, into: str = Form("")):
    """«Это дубль задачи…»: эта задача закрывается, её источник, связи и подзадачи переходят в выбранную."""
    keep = records.parse_task_ref(into) if into.strip().startswith("#") else None
    if not keep:
        return back(request, err="Выберите задачу из подсказок — начните вводить её название")
    try:
        records.merge_commitments(cid, keep)
    except ValueError as e:
        return back(request, err=str(e))
    # вернуться туда же (вид, фильтры), но с открытым окном задачи, которая осталась
    ref = urlparse(request.headers.get("referer") or "/commitments")
    query = "&".join(p for p in ref.query.split("&") if p and not p.startswith("task="))
    return back(request, "Объединено: задача закрыта как дубль, всё перенесено в основную",
                to=f"{ref.path or '/commitments'}?{query + '&' if query else ''}task={keep}")


@app.get("/api/event-options")
def event_options():
    """Подсказки «Выбрать из календаря» в окне задачи: встречи на две недели вперёд «30.10 15:00 · Название»."""
    from opsdash import task_meetings

    return [o["label"] for o in task_meetings.options()]


@app.post("/commitments/{cid}/meeting")
def commitment_meeting(request: Request, cid: int, event: str = Form(""), action: str = Form("link")):
    """Встреча по задаче: связать с выбранной из календаря или отвязать. Задача остаётся открытой до встречи."""
    from opsdash import task_meetings

    if not records.get_commitment(cid):
        return back(request, err="Такой задачи нет")
    if action == "unlink":
        task_meetings.unlink(cid)
        return back(request, "Встреча отвязана от задачи")
    uid = task_meetings.find(event)
    if not uid:
        return back(request, err="Такой встречи нет в календаре на две недели вперёд — выберите её из списка")
    task_meetings.link(cid, uid)
    for p in store.rows("SELECT id FROM proposals WHERE kind='task.meeting' AND status='pending' "
                        "AND json_extract(payload, '$.commitment_id')=?", (cid,)):
        proposals.reject(p["id"])  # подсказка связать с другой встречей больше не нужна
    return back(request, "Встреча связана с задачей — она на карточке")


# --- проекты: задачи с общим результатом ----------------------------------------------

@app.get("/projects/{pid}", response_class=HTMLResponse)
def project_page(request: Request, pid: int, task: int = 0):
    from opsdash import projects

    p = projects.get(pid)
    if not p:
        return back(request, err="Такого проекта нет", to="/commitments?view=projects")
    shown = [c for step in p["steps"] for c in step] + p["finished"] + p["parked"]
    records.add_links(shown)
    meeting_ids = {c["source_ref"] for c in shown if c["source_type"] == "meeting" and c["source_ref"]}
    meetings = {m["id"]: m["title"] for m in store.rows(
        f"SELECT id, title FROM meetings WHERE id IN ({','.join('?' * len(meeting_ids))})", list(meeting_ids))} if meeting_ids else {}
    return render(request, "project.html", nav="commitments", p=p, meetings=meetings, people=records.list_people(),
                  statuses=projects.STATUSES, focus=task)


@app.post("/projects")
def project_add(request: Request, title: str = Form(""), goal: str = Form(""), owner: str = Form(""),
                due: str = Form("")):
    from opsdash import projects

    try:
        pid = projects.add(title=title, goal=goal, owner=owner, due=due or None)
    except ValueError as e:
        return back(request, err=str(e))
    return back(request, "Проект создан — добавьте в него задачи", to=f"/projects/{pid}")


@app.post("/projects/{pid}/edit")
def project_edit(request: Request, pid: int, title: str = Form(""), goal: str = Form(""), owner: str = Form(""),
                 due: str = Form(""), card: str = Form("")):
    from opsdash import projects

    card = card.strip().lstrip("#")
    try:
        projects.update(pid, title=title, goal=goal, owner=owner, due=due, kaiten_card_id=int(card) if card.isdigit() else "")
    except ValueError as e:
        return back(request, err=str(e))
    return back(request, "Проект изменён")


@app.post("/projects/{pid}/status")
def project_status(request: Request, pid: int, status: str = Form(...)):
    from opsdash import projects

    try:
        projects.update(pid, status=status)
    except ValueError as e:
        return back(request, err=str(e))
    return back(request, {"done": "Проект завершён", "paused": "Проект на паузе"}.get(status, "Проект снова в работе"))


@app.post("/projects/{pid}/delete")
def project_delete(request: Request, pid: int):
    from opsdash import projects

    projects.delete(pid)
    return back(request, "Проект удалён, его задачи остались", to="/commitments?view=projects")


@app.post("/projects/{pid}/tasks")
def project_tasks(request: Request, pid: int, task: str = Form("")):
    """«Добавить задачу» на странице проекта: из подсказок «#69 · …» — вместе с её подзадачами."""
    from opsdash import projects

    tid = records.parse_task_ref(task) if task.strip().startswith("#") else None
    if not tid:
        return back(request, err="Выберите задачу из подсказок — начните вводить её название")
    if not projects.assign(pid, [tid]):
        return back(request, err="Такой задачи нет")
    return back(request, "Задача добавлена в проект")


@app.get("/api/project-options")
def project_options():
    """Подсказки поля «Проект» в окне задачи: «#3 · Запуск склада»."""
    from opsdash import projects

    return [f"#{p['id']} · {p['title']}" for p in projects.options() if p["status"] != "done"]


@app.get("/api/task-options")
def task_options():
    """Подсказки поля «Часть задачи»: открытые задачи «#id · заголовок · кто → кому»."""
    return [o["label"] for o in records.task_options()]


@app.post("/commitments/{cid}/remind")
def commitment_remind(request: Request, cid: int, text: str = Form("")):
    """«Отправить» у напоминания: директор сама нажала и подтвердила — сообщение уходит из её Telegram сразу."""
    try:
        p = proposals.propose_reminder(commitment_id=cid, text=text, reason="вы нажали «Отправить»")
    except proposals.ProposalError as e:
        return back(request, err=str(e))
    p = proposals.approve(p["id"])
    if p["status"] == "failed":
        return back(request, err=proposals.result_text(p))
    return back(request, proposals.result_text(p))


@app.post("/commitments/{cid}/seen")
def commitment_seen(cid: int):
    """Директор увидела новую задачу (навела на карточку или открыла её) — снять отметку «новая» (app.js)."""
    records.mark_seen(cid)
    return {"ok": True, "unseen": records.unseen_count()}


# --- база знаний: антология решений, уроков и удач ----------------------------------

@app.get("/kb", response_class=HTMLResponse)
def kb_page(request: Request, kind: str = "", status: str = "current", project: str = "", q: str = ""):
    from opsdash import anthology

    kind = kind if kind in anthology.KINDS else ""
    status = status if status in ("current", "all", "outdated") else "current"
    shown = [r for r in anthology.items(kind=kind or None, status=status, project=project or None, q=q or None)
             if r["status"] != "proposed"]
    months: list[tuple[str, list]] = [(f"Найдено: {len(shown)}", shown)] if q and shown else []
    for r in [] if q else shown:
        day = r["happened"] or r["created"][:10]
        title = f"{overview.MONTHS[int(day[5:7]) - 1]} {day[:4]}"
        if not months or months[-1][0] != title:
            months.append((title, []))
        months[-1][1].append(r)
    meeting_ids = [r["source_ref"] for r in shown if r["source_type"] == "meeting" and r["source_ref"]]
    meetings = {m["id"]: m["title"] for m in store.rows(
        f"SELECT id, title FROM meetings WHERE id IN ({','.join('?' * len(meeting_ids))})", meeting_ids)} if meeting_ids else {}
    return render(request, "kb.html", nav="kb", months=months, total=len(shown), kind=kind, status=status,
                  project=project, q=q, kinds=anthology.KINDS, projects=anthology.projects(), counts=anthology.counts(),
                  proposed=[] if q or status != "current" else anthology.items(status="proposed", kind=kind or None),
                  meetings=meetings, today=records.today_local().isoformat())


@app.get("/kb/context", response_class=HTMLResponse)
def kb_context(request: Request):
    from opsdash import context

    text = context.write().read_text(encoding="utf-8")
    job = store.row("SELECT last_ok, last_error, running FROM jobs WHERE name='company-context'") or {}
    return render(request, "kb_context.html", nav="kb", text=text, chars=len(text), job=job,
                  has_company=context.company_path().exists())


@app.post("/kb")
def kb_add(request: Request, kind: str = Form(...), title: str = Form(...), details: str = Form(""),
           happened: str = Form(""), project: str = Form("")):
    from opsdash import anthology

    try:
        anthology.add(kind=kind, title=title, details=details, happened=happened or None, project=project or None)
    except anthology.AnthologyError as e:
        return back(request, err=str(e))
    return back(request, f"{anthology.KINDS[kind]} добавлено в базу знаний")


@app.post("/kb/{item_id}")
def kb_set(request: Request, item_id: int, status: str = Form(...)):
    from opsdash import anthology

    if status not in anthology.STATUSES or not anthology.get(item_id):
        return back(request, err="Запись не найдена")
    anthology.update(item_id, status=status)
    return back(request, {"accepted": "Принято в базу знаний", "rejected": "Отклонено", "outdated": "Отмечено как устаревшее"}
                .get(status, "Сохранено"))


# --- документы -------------------------------------------------------------------

@app.get("/docs", response_class=HTMLResponse)
def docs_page(request: Request, q: str = "", type: str = ""):
    from opsdash import docs

    q = q.strip()
    ext = type if type in docs.TYPE_TITLES and type != ".htm" else ""
    if q:
        results = docs.search(q, limit=30, ext=ext or None)
    else:  # без запроса фильтр по типу показывает все документы этого типа, свежие сверху
        results = docs.listing(ext, limit=120) if ext else []
    counts = docs.type_counts()
    return render(request, "docs.html", nav="docs", q=q, type=ext, results=results, stats=docs.stats(),
                  recent=[] if q or ext else docs.recent(days=7, limit=30), folders=docs.folders(),
                  types=[(k, v, counts.get(k, 0)) for k, v in docs.TYPE_TITLES.items()
                         if k != ".htm" and (counts.get(k) or k == ext)], total=sum(counts.values()),
                  type_title=docs.TYPE_TITLES.get(ext, ""))


@app.get("/docs/view", response_class=HTMLResponse)
def doc_view(request: Request, path: str, q: str = ""):
    from opsdash import docs

    d = docs.find(path)
    if not d:
        return back(request, err="Документа нет в индексе — возможно, файл удалён или переименован", to="/docs")
    return render(request, "doc.html", nav="docs", d=d, parts=docs.parts(d["path"]), q=q)


@app.post("/docs/open")
def doc_open(request: Request, path: str = Form(...), reveal: str = Form("")):
    """Открыть файл в его программе или показать в Finder — на этом же компьютере."""
    from opsdash import docs

    d = docs.find(path)  # только файлы из индекса — не любые пути
    if not d or not Path(d["path"]).exists():
        return back(request, err="Файл не найден — возможно, его переместили")
    _open_local(d["path"], bool(reveal))
    return back(request, "Показала в Finder" if reveal else "Открыла файл")


def _open_local(path: str, reveal: bool) -> None:
    """Открыть файл в его программе или показать в Finder / Проводнике — на этом компьютере. Путь проверяет
    вызывающий: только документы из индекса или файлы из папок «Офиса»."""
    import subprocess

    if sys.platform == "darwin":
        subprocess.run(["open", "-R", path] if reveal else ["open", path], check=False, timeout=15)
    elif os.name == "nt":
        if reveal:
            subprocess.run(["explorer", "/select,", path], check=False, timeout=15)
        else:
            os.startfile(path)  # noqa: S606 — проверенный путь из рабочих папок
    else:
        subprocess.run(["xdg-open", str(Path(path).parent if reveal else path)], check=False, timeout=15)


@app.post("/docs/index")
def docs_reindex(request: Request):
    from opsdash import jobs

    jobs.submit("docs-index")
    return back(request, "Обновляю индекс документов — обычно это до минуты, первый раз дольше")


# --- календарь ------------------------------------------------------------------

MEETING_MINUTES = [15, 30, 45, 60, 90, 120]
CALENDAR_SOURCES = {"calendar": "Яндекс", "google_calendar": "Google"}


MONTHS_NOM = ("Январь", "Февраль", "Март", "Апрель", "Май", "Июнь", "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь",
              "Декабрь")
MONTHS_SHORT = ("янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен", "окт", "ноя", "дек")


@app.get("/calendar", response_class=HTMLResponse)
def calendar_page(request: Request, start: str = "", view: str = "week", task: int = 0):
    """Календарь (5.8): неделя сеткой 09–20, месяц и список. Встреча — поповер: время, участники, запись, задачи."""
    from opsdash import task_meetings
    from opsdash.sources import calendar

    settings = config.load_settings()
    now = config.now(settings)
    try:
        first = date.fromisoformat(start) if start else now.date()
    except ValueError:
        first = now.date()
    view = view if view in ("list", "month") else "week"
    month_first = first.replace(day=1)
    if view == "month":  # месяц — сеткой 6×7 с понедельника перед первым числом
        first = month_first - timedelta(days=month_first.weekday())
    elif view == "week":  # неделя — с понедельника, как в Google Календаре
        first -= timedelta(days=first.weekday())
    span = 42 if view == "month" else 7
    wh = settings["owner"].get("work_hours") or {}  # весь рабочий день: на нём задачи
    hours = (min(9, day_mod.minutes(wh.get("start"), 540) // 60), max(20, -(-day_mod.minutes(wh.get("end"), 1200) // 60)))
    grid = overview.week_grid(first, settings, hours=hours) if view == "week" else None
    days = overview.calendar_days(first, span, settings) if view != "week" else None
    # задачи по свободным окнам дней («Реальность дня»); «Задачи» у видов — показать или скрыть, выбор запоминается
    show_tasks = (request.query_params.get("tasks") or request.cookies.get("cal_tasks") or "1") == "1"
    if show_tasks:
        pr = day_mod.plan_range(first, span, settings, now)
        if grid:
            day_mod.calendar_layer(grid, pr)
        else:
            for d in days:
                d["plan"] = pr.get(d["date"])
    shown = grid["events"] if grid else [e for d in days for e in d["events"]]
    briefs.for_events(shown)
    task_meetings.for_events(shown)
    recorded = {m["event_uid"]: m for m in store.rows(
        "SELECT id, status, event_uid FROM meetings WHERE event_uid IS NOT NULL")}
    for e in shown:
        rec = recorded.get(e["uid"])
        e["recorded"], e["rec_status"] = (rec["id"], rec["status"]) if rec else (None, None)
        s_, f_ = store.parse_ts(e["start"]), store.parse_ts(e["end"])
        e["minutes"] = int((f_ - s_).total_seconds() // 60) if s_ and f_ and not e.get("all_day") else 0
    # ?task=<id> — «Назначить встречу» из окна задачи: форма новой встречи заполнена по задаче
    for_task = records.get_commitment(task) if task else None
    if for_task:
        people = records.list_people()
        emails = [p["email"] for n in (for_task["who"], for_task["whom"]) if n
                  for p in [records._person_by_name(n, people)] if p and p.get("email")]
        for_task = {"id": for_task["id"], "title": records.headline(for_task), "attendees": ", ".join(dict.fromkeys(emails)),
                    "description": for_task["what"] if for_task["what"] != records.headline(for_task) else ""}
    sources = [s for s in CALENDAR_SOURCES if settings[s].get("enabled")]
    synced = [(store.row("SELECT last_ok FROM jobs WHERE name=?", (f"collect:{s}",)) or {}).get("last_ok") for s in sources]
    new_at = (now + timedelta(hours=1)).replace(minute=0)  # ближайший целый час
    last = first + timedelta(days=span - 1)
    if view == "month":
        title, prev, nxt = (f"{MONTHS_NOM[month_first.month - 1]} {month_first.year}",
                            (month_first - timedelta(days=1)).replace(day=1), (month_first + timedelta(days=32)).replace(day=1))
    else:
        title = (f"{first.day}{' ' + MONTHS_SHORT[first.month - 1] if first.month != last.month else ''} – "
                 f"{last.day} {MONTHS_SHORT[last.month - 1]}")
        prev, nxt = first - timedelta(days=7), first + timedelta(days=7)
    if not grid:  # месяц и список: номер для поповера встречи и «прошла»
        tz = config.tz(settings)
        for i, e in enumerate({e["uid"]: e for e in shown}.values()):
            e.setdefault("n", f"d{i}")
            e["s_local"] = store.parse_ts(e["start"]).astimezone(tz)
            e["past"] = store.parse_ts(e["end"]) <= now
    count = len({e["uid"] for e in shown if view != "month" or e["s_local"].month == month_first.month})
    page = render(request, "calendar.html", nav="calendar", view=view, grid=grid, days=days, first=first,
                  show_tasks=show_tasks,
                  last=last, today=now.date(), is_current=first <= now.date() <= last, title=title, count=count,
                  week_no=first.isocalendar()[1], month=month_first.month,
                  prev=prev, next=nxt, can_refresh=bool(sources), connected=bool(sources),
                  updated=None if None in synced else min(synced, default=None),  # по самому давнему из календарей
                  new_day=first if first > now.date() else new_at.date(), new_time=new_at.strftime("%H:%M"),
                  minutes=MEETING_MINUTES, people=[p for p in records.list_people() if p.get("email")],
                  write_calendars=calendar.write_calendar_names(settings), for_task=for_task,
                  hide_cal_banner=request.cookies.get("hide_cal_banner") == "1",
                  google_calendars=(store.get_state("google_calendar_names") or []) if settings["google_calendar"].get("enabled") else [])
    if request.query_params.get("tasks") in ("0", "1"):  # «Задачи» нажали — запомнить выбор
        page.set_cookie("cal_tasks", request.query_params["tasks"], max_age=365 * 86400, samesite="lax")
    return page


@app.post("/briefs/build")
def brief_build(request: Request, uid: str = Form(...)):
    """«Собрать бриф» / «Обновить» у встречи — Claude соберёт его в фоне за пару минут."""
    from opsdash import jobs

    if not store.row("SELECT 1 FROM events WHERE uid=?", (uid,)):
        return back(request, err="Встречи уже нет в календаре")
    jobs.submit("briefs", "heavy", {"uid": uid})
    return back(request, "Собираю бриф — обычно 1–3 минуты, обновите страницу позже")


@app.post("/calendar/refresh")
async def calendar_refresh(request: Request):
    """Обновить Яндекс и Google Календарь сейчас, не дожидаясь расписания."""
    from opsdash import jobs

    settings = config.load_settings()
    done, errors = [], []
    for src, title in CALENDAR_SOURCES.items():
        if not settings[src].get("enabled"):
            continue
        try:
            done.append(f"{title}: {await run_in_threadpool(jobs.run_job, f'collect:{src}')}")
        except Exception as e:  # noqa: BLE001 — показываем, что не обновилось, остальное обновлено
            errors.append(f"{title}: {e}")
    if not done and not errors:
        return back(request, err="Календари не подключены — Настройки → Календарь")
    return back(request, ("Календарь обновлён — " + " · ".join(done)) if done else "", err=" · ".join(errors))


@app.post("/calendar/new")
async def calendar_new(request: Request, title: str = Form(...), day: str = Form(...), at: str = Form(...),
                       minutes: int = Form(60), attendees: str = Form(""), description: str = Form(""),
                       calendar: str = Form(""), task: int = Form(0)):
    title = title.strip()
    emails = [a for a in re.split(r"[\s,;]+", attendees) if a]
    bad = [a for a in emails if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", a)]
    start = store.parse_local(f"{day}T{at}")
    if not title:
        return back(request, err="Укажите название встречи")
    if bad:
        return back(request, err="Не похоже на адрес почты: " + ", ".join(bad))
    if not start:
        return back(request, err="Укажите дату и время начала")
    end = start + timedelta(minutes=max(5, min(int(minutes), 24 * 60)))

    def create() -> dict:
        # та же дорожка, что у встреч от Claude (журнал, история), но «да» — сама отправка формы
        p = proposals.propose_calendar_event(title=title, start=start.isoformat(), end=end.isoformat(), attendees=emails,
                                             description=description.strip(), calendar=calendar.strip(), source_ref="manual",
                                             reason="Создана вручную на странице «Календарь»", commitment_id=task or None)
        return proposals.approve(p["id"]) if p["status"] == "pending" else p

    p = await run_in_threadpool(create)
    if p["status"] != "done":
        return back(request, err=f"Встреча не создана: {(p.get('result') or {}).get('error') or p['status']}")
    where = f" в календаре «{calendar.strip()}»" if calendar.strip() else ""
    if task:  # «Назначить встречу» из окна задачи — назад к задаче, встреча уже на её карточке
        return back(request, f"Встреча «{title}» создана{where}: {store.local(start, '%d.%m %H:%M')} — она на карточке задачи",
                    to=f"/commitments?task={task}")
    return back(request, f"Встреча «{title}» создана{where}: {store.local(start, '%d.%m %H:%M')}")


# --- спросить Claude ---------------------------------------------------------------

def _ask_page(request: Request, chat_id: int | None, prefill: list[dict]):
    from opsdash import chat

    current = chat.get(chat_id) if chat_id else None
    if chat_id and not current:
        return back(request, err="Такого чата нет", to="/ask")
    meetings = store.rows("SELECT id, title, started_at FROM meetings WHERE status NOT IN ('new','cancelled') "
                          "ORDER BY started_at DESC LIMIT 100")
    chats = chat.list_chats()
    last = current or (chats[0] if chats else {})  # новый чат — с моделью и рассуждением прошлого разговора
    return render(request, "ask.html", nav="ask", chats=chats, chat=current,
                  items=chat.messages(chat_id) if current else [], prefill=chat.describe(prefill), meetings=meetings,
                  models=chat.model_labels(), model=last.get("model") or "opus", efforts=chat.EFFORTS,
                  effort=config.load_settings()["claude"].get("chat_effort") or "", describe=proposals.describe,
                  chat_days=_chat_days(chats), q=request.query_params.get("q", "").strip())


def _chat_days(chats: list[dict]) -> list[tuple[str, list[dict]]]:
    """Разговоры «Спросить Claude» по дням: «Сегодня», «Вчера», дата."""
    tz, out = config.tz(), []
    for c in chats:
        ts = store.parse_ts(c["updated"])
        label = _day_label(ts.astimezone(tz).date()).split(",")[0].capitalize() if ts else "Раньше"
        c["time"] = ts.astimezone(tz).strftime("%H:%M") if ts else ""
        if not out or out[-1][0] != label:
            out.append((label, []))
        out[-1][1].append(c)
    return out


@app.get("/ask", response_class=HTMLResponse)
def ask_page(request: Request, meeting: str = "", task: int = 0, doc: str = "", chat_ref: str = "", person: int = 0):
    """«Спросить Claude»: новый чат. ?meeting= / ?task= / ?doc= / ?chat_ref= — с этим контекстом (кнопки на страницах)."""
    from opsdash import chat

    refs = ([f"meeting:{meeting}"] * bool(meeting) + [f"task:{task}"] * bool(task) + [f"doc:{doc}"] * bool(doc)
            + [f"chat:{chat_ref}"] * bool(chat_ref) + [f"person:{person}"] * bool(person))
    return _ask_page(request, None, chat.parse_refs(refs))


@app.get("/ask/{chat_id}", response_class=HTMLResponse)
def ask_chat(request: Request, chat_id: int):
    return _ask_page(request, chat_id, [])


@app.post("/ask/send")
async def ask_send(request: Request, text: str = Form(""), chat_id: int = Form(0), model: str = Form("opus"),
                   effort: str = Form(""), files: list[UploadFile] = File(default=[])):
    from opsdash import chat

    form = await request.form()
    context = chat.parse_refs([str(v) for v in form.getlist("ctx")])
    uploads = [(f.filename, await f.read()) for f in files if f.filename]
    if not text.strip() and not context and not uploads:
        return back(request, err="Напишите вопрос или приложите встречу, задачу, файл")
    if chat_id and not chat.get(chat_id):
        return back(request, err="Такого чата нет", to="/ask")

    def go() -> tuple[int, int]:
        named = context or [{"type": "file", "name": name} for name, _ in uploads[:1]]  # чат без текста — по файлу
        cid = chat_id or chat.create(chat.title_for(text, named), model, effort)
        attached = context + [chat.save_file(cid, name, data) for name, data in uploads]
        return chat.ask(cid, text, attached, model, effort)

    cid, _ = await run_in_threadpool(go)
    return RedirectResponse(f"/ask/{cid}#end", status_code=303)


@app.get("/api/ask/{chat_id}/live")
def ask_live(chat_id: int):
    from opsdash import chat

    return chat.live(chat_id)


@app.post("/ask/{chat_id}/stop")
def ask_stop(request: Request, chat_id: int):
    from opsdash import chat

    m = chat.live(chat_id)
    if m.get("running"):
        chat.stop(m["id"])
    return back(request, "Остановлено", to=f"/ask/{chat_id}")


@app.post("/ask/{chat_id}/retry")
def ask_retry(request: Request, chat_id: int, message_id: int = Form(...)):
    from opsdash import chat

    if not chat.retry(message_id):
        return back(request, err="Этот ответ ещё пишется")
    return RedirectResponse(f"/ask/{chat_id}#end", status_code=303)


@app.post("/ask/{chat_id}/delete")
def ask_delete(request: Request, chat_id: int):
    from opsdash import chat

    chat.delete(chat_id)
    return back(request, "Чат удалён", to="/ask")


@app.get("/api/ask/docs")
def ask_docs(q: str = ""):
    """Подсказки «Документ» в контексте вопроса: лучшие совпадения поиска по папкам документов."""
    from opsdash import docs

    if len(q.strip()) < 2:
        return []
    return [{"label": d.get("rel") or d["title"], "path": d["path"]} for d in docs.search(q, limit=12)]


@app.post("/api/ask/voice/warm")
def ask_voice_warm():
    """Нажали микрофон — загружаем модель, пока человек говорит первую фразу."""
    from opsdash import transcribe

    threading.Thread(target=transcribe.warm, daemon=True, name="whisper-warm").start()
    return {"ok": True}


@app.post("/api/ask/voice")
async def ask_voice(audio: UploadFile = File(...), prompt: str = Form("")):
    """Диктовка в «Спросить Claude»: фраза из браузера (кусок до паузы) → текст. Расшифровывает тот же Whisper,
    что и встречи, — на этом компьютере, звук никуда не уходит. prompt — что уже надиктовано (для связности)."""
    import tempfile

    from opsdash import transcribe

    data = await audio.read()
    if not data:
        return JSONResponse({"error": "Пустая запись"}, status_code=400)
    suffix = Path(audio.filename or "voice.webm").suffix or ".webm"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as fh:
        fh.write(data)
        tmp = Path(fh.name)
    try:
        text = await run_in_threadpool(transcribe.transcribe_short, tmp, None, prompt[-300:] or None)
    except Exception as e:  # noqa: BLE001 — покажем у кнопки микрофона
        return JSONResponse({"error": f"Не получилось расшифровать: {e}"}, status_code=500)
    finally:
        tmp.unlink(missing_ok=True)
    return {"text": text}


# --- встречи ---------------------------------------------------------------------

@app.get("/meetings", response_class=HTMLResponse)
def meetings(request: Request, q: str = ""):
    """Встречи (5.9): одна лента по дням — записи и встречи календаря без записи; поиск по названию и расшифровке."""
    from opsdash import recordings

    settings = config.load_settings()
    tz, now = config.tz(settings), config.now(settings)
    items = records.list_meetings(limit=200)
    q = q.strip()
    if q:
        low = q.lower()
        items = [m for m in items if low in (m["title"] or "").lower() or low in (m["headline"] or "").lower()
                 or any(low in records.read_meeting_file(m["id"], f).lower() for f in ("summary.md", "transcript.md"))]
    pending = {r["batch"][8:]: r["n"] for r in store.rows(
        "SELECT batch, COUNT(*) AS n FROM proposals WHERE status='pending' AND batch LIKE 'meeting:%' GROUP BY batch")}
    rows = []
    queue = recordings.queue_notes()  # «в очереди» — только если впереди кто-то есть
    for m in items:
        start = store.parse_ts(m["started_at"])
        rows.append({"kind": "rec", "m": m, "start": start.astimezone(tz) if start else None, "queue": queue.get(m["id"]),
                     "people": ", ".join(store.loads(m["participants"], [])[:4]), "pending": pending.get(m["id"], 0)})
    if not q:  # встречи календаря за неделю без записи и сегодняшние впереди (с брифами) — тоже в ленте
        recorded = {m["event_uid"] for m in items if m["event_uid"]}
        begin = datetime.combine(now.date() - timedelta(days=7), datetime.min.time(), tz)
        until = max(now + timedelta(hours=1), datetime.combine(now.date() + timedelta(days=1), datetime.min.time(), tz))
        events = [e for e in overview.events_between(begin, until) if e["uid"] not in recorded and not e.get("all_day")]
        for e in briefs.for_events(events):
            s, f = store.parse_ts(e["start"]), store.parse_ts(e["end"])
            rows.append({"kind": "event", "e": e, "start": s.astimezone(tz) if s else None,
                         "minutes": int((f - s).total_seconds() // 60) if s and f else 0,
                         "people": f_people(e["attendees"], 3),
                         # до начала — «запланирована», с начала до конца — «идёт сейчас», после — «записи нет»
                         "state": "past" if f and f <= now else "now" if s and s <= now else "planned"})
    rows.sort(key=lambda r: r["start"] or now, reverse=True)
    days: list[dict] = []
    for r in rows:
        d = (r["start"] or now).date()
        if not days or days[-1]["date"] != d:
            label = ("Сегодня" if d == now.date() else "Вчера" if d == now.date() - timedelta(days=1)
                     else WEEKDAYS_FULL[d.weekday()])
            days.append({"date": d, "label": f"{label}, {d.day} {MONTHS_GEN[d.month - 1]}", "rows": []})
        days[-1]["rows"].append(r)
    stats = {"total": len(items), "processed": sum(m["status"] == "processed" for m in items),
             "waiting": sum(m["status"] == "transcribed" for m in items),
             "work": sum(m["status"] in ("new", "transcribing", "paused", "processing") for m in items)}
    folders = [str(p) for p in recordings.watch_folders(settings)]
    return render(request, "meetings.html", nav="meetings", days=days, folders=folders, q=q, stats=stats,
                  waiting=recordings.waiting(settings), formats=", ".join(sorted(e.lstrip(".") for e in recordings.ALL)))


@app.get("/meetings/{mid}", response_class=HTMLResponse)
def meeting(request: Request, mid: str, tab: str = "summary"):
    from opsdash import recordings, voice

    m = records.get_meeting(mid)
    if not m:
        return back(request, err="Встреча не найдена", to="/meetings")
    src = Path(m["source_file"] or "")
    recording = {"name": src.name, "size": src.stat().st_size if src.is_file() else None}
    summary = records.read_meeting_file(mid, "summary.md")
    transcript = records.read_meeting_file(mid, "transcript.md")
    related = proposals.list_proposals("all", batch=f"meeting:{mid}")
    commitments_ = records.meeting_commitments(mid)
    summary = records.link_summary(summary, commitments_)  # строки задач в разборе — со ссылками на задачи PULSE
    followup, followup_auto = records.followup_text(mid)
    meta_path = records.meeting_dir(mid) / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    who, how = voice.speaker_map(meta)
    speakers = voice.speakers_overview(mid)
    for s in speakers:
        s["who"], s["how"] = who.get(s["label"]), how.get(s["label"], "")
        s["name"] = (meta.get("speaker_names") or {}).get(s["label"]) or (voice.display_name(s["who"]) if s["who"] else "")
    guesses = {k: g for k, g in voice.guesses(mid, meta).items() if k not in who} if speakers else {}
    return render(request, "meeting.html", nav="meetings", m=m, summary=summary, transcript=transcript, tab=tab,
                  queue_note=recordings.queue_notes().get(mid) if m["status"] == "new" else None, related=related, commitments=commitments_, folder=str(records.meeting_dir(mid)),
                  folder_rel=os.path.relpath(records.meeting_dir(mid), config.DATA_DIR.parent).replace(os.sep, "/"),
                  followup=followup, followup_auto=followup_auto,
                  # обрывки (меньше 10 с речи) сворачиваем, только когда спикеров много
                  describe=proposals.describe, speakers=[s for s in speakers if len(speakers) <= 4 or not s["short"] or s["who"]],
                  short_speakers=[s for s in speakers if len(speakers) > 4 and s["short"] and not s["who"]], guesses=guesses,
                  people=records.list_people(), can_tidy=len(speakers) > 1 and not (records.meeting_dir(mid) / voice.PRINTS_FILE).exists(),
                  recording=recording, memory=recordings.memory(mid))


@app.post("/meetings/{mid}/speaker")
async def meeting_speaker(request: Request, mid: str, speaker: str = Form(...), name: str = Form(...)):
    """«Это Никита»: запомнить голос человека из справочника (нет такого — добавить) и подписать его речь."""
    from opsdash import voice

    name = name.strip()
    if not name:
        return back(request, err="Напишите, кто это")
    if name.casefold() == voice.owner_name().casefold():
        return await meeting_me(request, mid, speaker)
    person = records.find_person(name)
    created = not person
    pid = person["id"] if person else records.upsert_person(name)
    res = await run_in_threadpool(voice.mark_person, mid, speaker, pid)
    who = person["name"] if person else name
    parts = [f"{speaker} — {who}." + (" Добавила в справочник людей." if created else "")]
    parts.append(f"Голос запомнила — в следующих записях подпишу {who} сама." if res["voice_saved"]
                 else "Голос запомнить не получилось (нет файла записи) — имя в расшифровке исправлено.")
    if res["also"]:
        parts.append(f"Тот же голос у {', '.join(res['also'])} — подписала тоже.")
    parts.append("Чтобы разбор учёл имена — «Разобрать заново».")
    return back(request, " ".join(parts), to=f"/meetings/{mid}")


@app.post("/meetings/{mid}/speakers/tidy")
async def meeting_speakers_tidy(request: Request, mid: str):
    """Старая запись: склеить части одного голоса и узнать знакомых — без новой расшифровки."""
    from opsdash import voice

    try:
        res = await run_in_threadpool(voice.retidy, mid)
    except ValueError as e:
        return back(request, err=str(e))
    return back(request, f"Склеила части одних и тех же голосов: заметно говорят {res['main']} "
                         f"(было {res['before']} «спикеров», стало {res['after']}, остальное — обрывки реплик)"
                         + (f", узнано по голосу: {res['known']}" if res["known"] else "")
                         + ". Чтобы разбор учёл — «Разобрать заново».", to=f"/meetings/{mid}")


@app.post("/meetings/{mid}/me")
async def meeting_me(request: Request, mid: str, speaker: str = Form(...)):
    """«Это я»: подписать речь директора, запомнить голос и разобрать встречу заново."""
    from opsdash import jobs, voice

    res = await run_in_threadpool(voice.mark_me, mid, speaker)
    jobs.submit("process-meeting", "heavy", {"meeting_id": mid})
    parts = [f"Готово: {speaker} — это вы."]
    parts.append("Голос запомнила — в следующих записях подпишу вас сама." if res["voice_saved"]
                 else "Голос запомнить не получилось (нет файла записи) — имя в расшифровке исправлено.")
    if res["commitments_removed"] or res["proposals_rejected"]:
        parts.append(f"Итоги прошлого разбора убраны (задач: {res['commitments_removed']}, "
                     f"предложений: {res['proposals_rejected']}).")
    if res["already_in_kaiten"]:
        parts.append(f"Карточек уже создано в Kaiten: {res['already_in_kaiten']} — проверьте их вручную.")
    parts.append("Разбираю встречу заново — это 2–5 минут.")
    return back(request, " ".join(parts), to=f"/meetings/{mid}")


@app.post("/meetings/upload")
async def meeting_upload(request: Request, file: UploadFile = File(...), title: str = Form(""), event_uid: str = Form("")):
    """event_uid — запись загрузили со строки встречи календаря: связываем с ней, а не угадываем по времени."""
    from opsdash import jobs, recordings

    name = Path(file.filename or "zapis").name
    if Path(name).suffix.lower() not in recordings.ALL:
        return back(request, err="Такой формат не подходит: нужно аудио, видео или .txt/.vtt/.srt/.docx")
    dest = config.RECORDINGS_DIR / "upload" / name
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest = dest.with_name(f"{config.now():%H%M%S}_{name}")
    with open(dest, "wb") as fh:
        await run_in_threadpool(shutil.copyfileobj, file.file, fh)
    mid = recordings.create_meeting(dest, title.strip() or None, event_uid=event_uid.strip() or None)
    jobs.submit("transcribe", "transcribe", {"meeting_id": mid})
    return back(request, "Запись загружена — расшифровка началась", to=f"/meetings/{mid}")


@app.post("/meetings/{mid}/rerun")
def meeting_rerun(request: Request, mid: str, step: str = Form("process")):
    from opsdash import jobs

    from opsdash import recordings

    if step == "transcribe":
        m = records.get_meeting(mid)
        if not m:
            return back(request, err="Такой встречи нет")
        if m["status"] in recordings.IN_QUEUE:  # иначе идущая расшифровка выглядела бы «в очереди»
            return back(request, "Расшифровка уже идёт, ждёт очереди или стоит на паузе")
        if not Path(m["source_file"]).exists():
            return back(request, err="Файла записи больше нет в папке — расшифровать заново не из чего")
        records.update_meeting(mid, status="new", error="")
        jobs.submit("transcribe", "transcribe", {"meeting_id": mid})
        return back(request, "Расшифровка поставлена в очередь")
    jobs.submit("process-meeting", "heavy", {"meeting_id": mid})
    return back(request, "Разбор поставлен в очередь")


@app.post("/meetings/{mid}/transcription")
def meeting_transcription(request: Request, mid: str, action: str = Form(...)):
    """Пауза, «Продолжить» и отмена расшифровки — со страницы встречи и из списка встреч."""
    from opsdash import recordings

    done = {"pause": (recordings.pause, "Расшифровка на паузе — компьютер не нагружается"),
            "resume": (recordings.resume, "Расшифровка продолжается"),
            "cancel": (recordings.cancel, "Расшифровка отменена")}.get(action)
    if not done:
        return back(request, err="Не понял, что сделать с расшифровкой")
    fn, msg = done
    if not fn(mid):
        return back(request, err="Сейчас это сделать нельзя — обновите страницу")
    return back(request, msg)


@app.post("/meetings/{mid}/delete")
def meeting_delete(request: Request, mid: str, what: str = Form(...)):
    """Удалить файл записи (встреча остаётся) или встречу целиком — со всем, что PULSE по ней запомнил."""
    from opsdash import recordings

    try:
        if what == "recording":
            name = recordings.delete_recording(mid)
            return back(request, f"Файл записи {name} удалён. Расшифровка, разбор и задачи остались")
        if what != "all":
            return back(request, err="Не понял, что удалить")
        m = records.get_meeting(mid)
        done = recordings.forget_meeting(mid)
    except recordings.MeetingError as e:
        return back(request, err=str(e))
    parts = [f"Встреча «{m['title'] or mid}» удалена"]
    gone = [f"{label}: {done[key]}" for key, label in (("commitments", "задач"), ("proposals", "предложений"),
                                                         ("kb", "записей базы знаний")) if done[key]]
    if gone:
        parts.append("вместе с ней — " + ", ".join(gone))
    if done["in_kaiten"]:
        parts.append(f"карточки в Kaiten ({done['in_kaiten']}) остались — удалите их там, если не нужны")
    if done["file_error"]:
        return back(request, msg="; ".join(parts), err=f"Файл записи удалить не получилось: {done['file_error']}",
                    to="/meetings")
    return back(request, "; ".join(parts), to="/meetings")


@app.post("/meetings/{mid}/followup")
def meeting_followup(request: Request, mid: str):
    from opsdash import jobs

    jobs.submit("followup", "heavy", {"meeting_id": mid})
    return back(request, "Claude готовит текст итогов — обновите страницу через минуту", to=f"/meetings/{mid}?tab=followup")


@app.post("/meetings/{mid}/title")
def meeting_title(request: Request, mid: str, title: str = Form(...)):
    records.update_meeting(mid, title=title.strip())
    return back(request, "Название сохранено")


# --- задачи Kaiten ------------------------------------------------------------------

@app.get("/tasks")
def tasks_redirect(request: Request):
    """«Задачи Kaiten» стали вкладкой «Задач» (2.0): старые ссылки (бот, уведомления) ведут туда же, с фильтрами."""
    query = "&".join(f"{k}={quote(v)}" for k, v in request.query_params.items() if k in KAITEN_PARAMS + ("view",))
    return RedirectResponse("/commitments?view=kaiten" + ("&" + query.replace("view=", "kview=") if query else ""),
                            status_code=303)


def tasks(request: Request, who: str = "", space: str = "", q: str = "", kview: str = "boards", mine: str = "",
          only: str = "", created: str = "", created_from: str = "", created_to: str = "", due: str = "",
          due_from: str = "", due_to: str = ""):
    """Вкладка «Kaiten» в «Задачах» (5.7): зеркало досок Kaiten, только чтение."""
    from opsdash.sources import kaiten

    settings = config.load_settings()
    view = "list" if kview == "list" else "boards"
    sql = "SELECT * FROM kaiten_cards WHERE condition=1 AND state IN (1,2)"
    params: list = []
    if who:
        sql += " AND responsible_name=?"
        params.append(who)
    if space:
        sql += " AND space_title=?"
        params.append(space)
    if q:
        sql += " AND title LIKE ?"
        params.append(f"%{q}%")
    # даты Kaiten приходят в разных видах («…Z», «…+00:00»), поэтому сравниваем не строками в SQL, а здесь
    c_from, c_to = records.period(created, created_from, created_to) if created else (None, None)
    d_from, d_to = records.period(due, due_from, due_to, ahead=True) if due and due != "none" else (None, None)

    def within(day: date | None, frm: date | None, to: date | None) -> bool:
        if not (frm or to):
            return True
        return day is not None and (not frm or day >= frm) and (not to or day <= to)

    def in_dates(c: dict) -> bool:
        made = store.parse_ts(c["created"])
        if not within(made.astimezone(config.tz(settings)).date() if made else None, c_from, c_to):
            return False
        day = _as_day(c["due_date"])
        return day is None if due == "none" else within(day, d_from, d_to)

    every = kaiten.decorate([c for c in store.rows(
        sql + " ORDER BY state DESC, due_date IS NULL, due_date, updated DESC LIMIT 400", params) if in_dates(c)], settings)
    counts = {"work": sum(c["state"] == 2 for c in every), "queue": sum(c["state"] == 1 for c in every),
              "mine": sum(c["mine"] for c in every), "stale": sum(c["idle_days"] is not None for c in every)}
    cards = [c for c in every if (not mine or c["mine"]) and (
        not only or (only == "stale" and c["idle_days"] is not None) or (only == "work" and c["state"] == 2)
        or (only == "queue" and c["state"] == 1))]
    people = [r["responsible_name"] for r in store.rows(
        "SELECT DISTINCT responsible_name FROM kaiten_cards WHERE responsible_name IS NOT NULL ORDER BY 1")]
    spaces = [r["space_title"] for r in store.rows(
        "SELECT DISTINCT space_title FROM kaiten_cards WHERE space_title IS NOT NULL ORDER BY 1")]
    synced = store.scalar("SELECT MAX(synced_at) FROM kaiten_cards")
    domain = (settings["kaiten"].get("domain") or "").strip().rstrip("/")
    return render(request, "tasks.html", nav="tasks", view=view, cards=cards, boards=kaiten.boards_view(cards),
                  spaces_n=len({c["space_title"] for c in every}), stale_days=settings["kaiten"].get("stale_days"),
                  counts=counts, people=people, spaces=spaces, who=who, space=space, q=q, mine=mine, only=only,
                  synced=synced, kaiten_base=("https://" + domain.removeprefix("https://")) if domain else "",
                  created=created, created_from=created_from, created_to=created_to, due=due, due_from=due_from,
                  due_to=due_to, created_periods=records.CREATED_PERIODS, due_periods=records.DUE_PERIODS)


def _as_day(value: str | None) -> date | None:
    try:
        return date.fromisoformat(value[:10]) if value else None
    except ValueError:
        return None


@app.post("/tasks/{card_id}/{action}")
def task_action(request: Request, card_id: int, action: str, days: int = Form(3)):
    from opsdash import bot
    from opsdash.sources import kaiten

    if action == "snooze":
        kaiten.snooze(card_id, days)
        return back(request, f"Отложено на {days} дн.")
    if action == "nudge":
        p = bot.nudge_card(card_id)
        return back(request, proposals.result_text(p)) if p["status"] != "failed" else back(request, err=proposals.result_text(p))
    return back(request)


# --- люди ----------------------------------------------------------------------------

PEOPLE_SORTS = {"load": "По загрузке", "name": "По имени", "contact": "По последнему контакту"}


@app.get("/people", response_class=HTMLResponse)
def people(request: Request, edit: int = 0, person: int = 0, q: str = "", sort: str = "load"):
    """Люди (5.13): кто, где найти, кто кому должен, последний контакт. Карточка человека — окном поверх списка."""
    from opsdash import voice
    from opsdash.sources import kaiten

    settings = config.load_settings()
    items = records.list_people()
    kusers = store.rows("SELECT * FROM kaiten_users ORDER BY full_name")
    load = {w["key"]: w for w in records.workload()}
    open_tasks = records.list_commitments(status="open")
    by_id = {p["id"]: p for p in items}
    for p in items:
        p.update(owes_me=[], i_owe=[], owed_to=[], kaiten=[], meetings=[], last=None, waiting=0)
    for c in open_tasks:
        who = records._person_by_name(c["who"], items) if c["who"] else None
        whom = records._person_by_name(c["whom"], items) if c["whom"] else None
        if who and c["direction"] == "to_me":
            by_id[who["id"]]["owes_me"].append(c)
        if whom and c["direction"] == "mine":
            by_id[whom["id"]]["i_owe"].append(c)
        if whom and c["direction"] in ("mine", "other"):
            by_id[whom["id"]]["owed_to"].append(c)
    cards = kaiten.decorate(store.rows("SELECT * FROM kaiten_cards WHERE condition=1 AND state IN (1,2) "
                                       "AND responsible_id IS NOT NULL ORDER BY state DESC, due_date"), settings)
    for p in items:
        names = [p["name"], *(p["aliases"] or [])]
        marks = ",".join("?" * len(names))
        p["kaiten"] = [c for c in cards if p.get("kaiten_user_id") and c["responsible_id"] == p["kaiten_user_id"]]
        p["last"] = store.row(f"SELECT ts, source, chat_id, chat_title FROM messages WHERE is_outgoing=0 AND sender_name IN ({marks}) "
                              "ORDER BY ts DESC LIMIT 1", names)
        p["waiting"] = store.scalar(f"SELECT COUNT(*) FROM attention WHERE status='open' AND sender_name IN ({marks})", names) or 0
        p["meetings"] = store.rows("SELECT id, title, started_at FROM meetings WHERE participants LIKE ? "
                                   "ORDER BY started_at DESC LIMIT 3", (f"%{p['name']}%",))
        p["load"] = len(p["owes_me"]) + len(p["i_owe"]) + len(p["kaiten"])
    query = q.strip().lower()
    shown = [p for p in items if not query or query in " ".join(
        [p["name"], p.get("role") or "", p.get("telegram") or "", p.get("email") or "", *(p["aliases"] or [])]).lower()]
    sort = sort if sort in PEOPLE_SORTS else "load"
    if sort == "name":
        shown.sort(key=lambda p: p["name"])
    elif sort == "contact":
        shown.sort(key=lambda p: (p["last"] or {}).get("ts") or "", reverse=True)
    else:
        shown.sort(key=lambda p: (-p["load"], p["name"]))
    # Claude заметил новых людей: пишут в читаемые чаты, а в справочнике их нет
    known = {n.lower() for p in items for n in [p["name"], *(p["aliases"] or [])]}
    fresh = [r for r in store.rows(
        "SELECT sender_name, chat_title, COUNT(*) AS n FROM messages WHERE is_outgoing=0 AND sender_name IS NOT NULL "
        "AND sender_name != '' GROUP BY sender_name ORDER BY n DESC LIMIT 30") if r["sender_name"].lower() not in known
        and not any(r["sender_name"].lower() in k or k in r["sender_name"].lower() for k in known)][:3]
    current = by_id.get(edit) or by_id.get(person)
    return render(request, "people.html", nav="people", items=shown, everyone=items, kusers=kusers, current=current,
                  editing=bool(edit and current), voices=voice.voice_counts(), load=load, q=q.strip(), sort=sort,
                  sorts=PEOPLE_SORTS, fresh=fresh)


@app.post("/people/{pid}/voice/forget")
def people_voice_forget(request: Request, pid: int):
    from opsdash import voice

    voice.forget_person(pid)
    return back(request, "Голос забыт — в записях подпишу человека, только когда вы снова подтвердите")


@app.post("/people")
def people_save(request: Request, name: str = Form(...), pid: int = Form(0), role: str = Form(""),
                aliases: str = Form(""), telegram: str = Form(""), ymessenger: str = Form(""), email: str = Form(""),
                kaiten_user_id: str = Form(""), notes: str = Form("")):
    alias_list = [a.strip() for a in aliases.split(",") if a.strip()]
    if pid:
        store.execute(
            "UPDATE people SET name=?, role=?, aliases=?, telegram=?, ymessenger=?, email=?, kaiten_user_id=?, notes=?, updated=? WHERE id=?",
            (name.strip(), role or None, store.json_col(alias_list), telegram or None, ymessenger or None, email or None,
             int(kaiten_user_id) if kaiten_user_id.strip().isdigit() else None, notes or None, store.utcnow(), pid))
    else:
        records.upsert_person(name.strip(), role=role or None, aliases=alias_list, telegram=telegram or None,
                              ymessenger=ymessenger or None, email=email or None,
                              kaiten_user_id=int(kaiten_user_id) if kaiten_user_id.strip().isdigit() else None,
                              notes=notes or None)
    return back(request, "Сохранено", to="/people")


@app.post("/people/{pid}/delete")
def people_delete(request: Request, pid: int):
    from opsdash import voice

    records.delete_person(pid)
    voice.forget_person(pid)
    return back(request, "Удалено", to="/people")


# --- журнал ------------------------------------------------------------------------

CLAUDE_JOBS = {"triage", "process-meeting", "followup", "digest", "weekly", "groups", "briefs", "anthology",
               "company-context", "projects", "ask", "voice", "chat"}


@app.get("/journal", response_class=HTMLResponse)
def journal(request: Request, kind: str = "", limit: int = 120, level: str = ""):
    """Журнал (5.16): события по дням — что сделал PULSE (сюда же «Лента бота» со Сводки) и задания с «Запустить»."""
    from opsdash import jobs

    settings = config.load_settings()
    kind = "errors" if level == "error" else kind  # ?level=error — ссылки из прошлой версии
    limit = max(60, min(int(limit), 2000))
    events = [{"ts": l["ts"], "kind": "error" if l["level"] == "error" else ("claude" if l["job"] in CLAUDE_JOBS else "job"),
               "level": l["level"], "title": l["message"].split("\n")[0][:300], "rest": "\n".join(l["message"].split("\n")[1:])[:600],
               "who": jobs.TITLES.get(l["job"], l["job"]).replace(" (Claude)", ""), "claude": l["job"] in CLAUDE_JOBS}
              for l in store.rows("SELECT * FROM log ORDER BY id DESC LIMIT ?", (limit,))]
    events += [{"ts": o["created"], "kind": "notify", "level": "info", "title": "Отправлено в бот: «" +
                re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", o["text"] or "")).strip()[:160] + "»", "rest": "",
                "who": "уведомление" if o["sent_at"] else "не отправлено", "claude": False}
               for o in store.rows("SELECT * FROM outbox ORDER BY id DESC LIMIT ?", (limit // 2,))]
    events.sort(key=lambda e: e["ts"] or "", reverse=True)
    counts = {"errors": sum(e["kind"] == "error" for e in events)}
    shown = [e for e in events if not kind or (kind == "errors" and e["kind"] == "error") or (kind == "notify" and e["kind"] == "notify")
             or (kind == "claude" and e["claude"])][:limit]
    tz = config.tz(settings)
    prev = None
    for e in shown:
        ts = store.parse_ts(e["ts"])
        e["time"] = ts.astimezone(tz).strftime("%H:%M") if ts else ""
        day = ts.astimezone(tz).date() if ts else None
        e["day"] = _day_label(day).split(",")[0].capitalize() if day and day != prev else ""
        prev = day
    week_ago = store.to_utc_iso(config.now(settings) - timedelta(days=7))
    errors_week = store.scalar("SELECT COUNT(*) FROM log WHERE level='error' AND ts >= ?", (week_ago,)) or 0
    job_rows = {j["name"]: j for j in store.rows("SELECT * FROM jobs")}
    off = config.archived_jobs(settings)  # задания архивных разделов не показываем — о них строка под списком
    sch = settings["schedule"]
    sources = ("telegram", "ymessenger", "pachca", "mail", "gmail", "calendar", "google_calendar", "kaiten")
    every = lambda m: f"каждые {m} мин"  # noqa: E731
    collect = [(f"collect:{src}", {"calendar": sch["calendar_minutes"], "google_calendar": sch["calendar_minutes"],
                                   "kaiten": sch["kaiten_minutes"]}.get(src, sch["collect_minutes"]))
               for src in sources if settings[src].get("enabled")]
    groups = [
        ("Сбор", [(name, every(m)) for name, m in collect] + [("recordings", "следит за папкой записей")]
                 + ([("docs-index", every(sch.get("docs_minutes") or 30))] if settings["knowledge"].get("folders") else [])),
        ("Claude", [("triage", every(sch["triage_minutes"]) + " в рабочее время"),
                    ("digest", " и ".join(config.hhmm(t) for t in (sch.get("digest_morning"), sch.get("digest_evening")) if t) or "выключен"),
                    ("projects", "по понедельникам"), ("anthology", "по пятницам"), ("company-context", "по понедельникам")]),
        ("Напоминания", [("stale", f"каждый день в {config.hhmm(sch.get('stale_check'))}" if sch.get("stale_check") else "выключено")]),
    ]
    groups = [(g, [(n, w) for n, w in items if n not in off]) for g, items in groups]
    not_connected = [overview.SOURCE_TITLES.get(s_, s_) for s_ in sources if not settings[s_].get("enabled")]
    nxt = _next_collect(settings)
    return render(request, "journal.html", nav="journal", events=shown, counts=counts, kind=kind, limit=limit,
                  more=len(events) > len(shown) or len(shown) >= limit, jobs=job_rows, groups=groups, titles=jobs.TITLES,
                  queued=jobs.queued(), errors_week=errors_week, next_collect=nxt, not_connected=not_connected,
                  archived_sections=[config.SECTIONS[n]["title"] for n in config.SECTIONS if not config.section_on(n, settings)])


# --- офис ----------------------------------------------------------------------------

@app.get("/office", response_class=HTMLResponse)
def office_page(request: Request, o: str = ""):
    """Офис целиком: PULSE и папки с Claude Code — одной картинкой, списками под ней. o — какой список раскрыть."""
    from opsdash import office

    settings = config.load_settings()
    return render(request, "office.html", nav="office", snaps=office.everything(settings), open_key=o, settings=settings)


@app.get("/office/live", response_class=HTMLResponse)
def office_live(request: Request, o: str = ""):
    """Списки и данные картинки — страница подменяет их сама, пока открыта (app.js); o — какой офис выбран."""
    from opsdash import office

    return templates.TemplateResponse(request, "_office_body.html", {"snaps": office.everything(), "open_key": o})


@app.get("/api/office")
def office_api(o: str = ""):
    from opsdash import office

    return office.snapshot(o) if o else {"offices": office.everything()}


@app.post("/office/open")
def office_open(request: Request, path: str = Form(...), reveal: str = Form("")):
    from opsdash import office

    if not office.can_open(path):  # только файлы из папок офиса
        return back(request, err="Файл не найден — возможно, его переместили")
    _open_local(path, bool(reveal))
    return back(request, "Показала в Finder" if reveal else "Открыла файл")


@app.post("/jobs/run")
def job_run(request: Request, name: str = Form(...)):
    from opsdash import jobs

    if name in config.archived_jobs():
        return back(request, err=f"«{jobs.TITLES.get(name, name)}» — задание архивного раздела. Включите раздел в "
                                 "Настройки → Архивные разделы")
    args = {"force": True} if name == "triage" else None
    heavy = name in ("triage", "digest", "anthology", "company-context", "projects")  # запуски Claude — не мешать сбору
    jobs.submit(name, "heavy" if heavy else "light", args)
    return back(request, f"Запущено: {jobs.TITLES.get(name, name)}")


# --- справка ---------------------------------------------------------------------

def _docs() -> list[dict]:
    out = []
    for p in sorted(DOCS_DIR.glob("*.md")):
        first = p.read_text(encoding="utf-8").splitlines()[0] if p.stat().st_size else p.stem
        out.append({"slug": p.stem, "title": first.lstrip("# ").strip()})
    return out


@app.get("/help", response_class=HTMLResponse)
@app.get("/help/{slug}", response_class=HTMLResponse)
@app.get("/help/examples/{example}", response_class=HTMLResponse)
def help_page(request: Request, slug: str = "", example: str = ""):
    docs = _docs()
    if not docs:
        return render(request, "help.html", nav="help", docs=[], body="Документация не найдена", slug="")
    slug = slug or docs[0]["slug"]
    if example:
        path = DOCS_DIR / "examples" / f"{Path(example).name}.md"
        slug = ""
    else:
        path = DOCS_DIR / f"{Path(slug).name}.md"
    if not path.exists():
        return back(request, err="Нет такой страницы", to="/help")
    text = path.read_text(encoding="utf-8")
    text = re.sub(r"\]\((\d\d-[^)#]+)\.md(#[^)]*)?\)", r"](/help/\1\2)", text)
    text = re.sub(r"\]\(examples/([^)]+)\.md\)", r"](/help/examples/\1)", text)
    body = f_md(text)
    # оглавление текущего раздела — вложенными пунктами подменю (5.18): заголовки второго уровня
    toc = [{"id": m.group(1), "title": re.sub(r"<[^>]+>", "", m.group(2))}
           for m in re.finditer(r'<h2 id="([^"]+)">(.*?)</h2>', body)]
    q = request.query_params.get("q", "").strip()
    hits = []
    if q:  # поиск по справке: строки разделов, где есть слова запроса
        for d in docs:
            for line in (DOCS_DIR / f"{d['slug']}.md").read_text(encoding="utf-8").splitlines():
                if q.lower() in line.lower() and line.strip():
                    hits.append({"slug": d["slug"], "doc": d["title"], "line": re.sub(r"[#*`>|]", "", line).strip()[:200]})
                    if len(hits) >= 40:
                        break
    return render(request, "help.html", nav="help", docs=docs, body=body, slug=slug, toc=toc, q=q, hits=hits)


# --- настройки -------------------------------------------------------------------

settings_views.register(app, render, back)
agent_views.register(app, render, templates, back)


@app.exception_handler(Exception)
async def on_error(request: Request, exc: Exception):
    store.log("web", f"{request.method} {request.url.path}: {exc}", "error")
    if request.method == "POST":
        return back(request, err=str(exc)[:400])
    return HTMLResponse(f"<h1>Ошибка</h1><pre>{html.escape(str(exc))}</pre><p><a href='/'>На главную</a></p>",
                        status_code=500)

