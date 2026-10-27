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
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import quote, urlparse

import markdown as md
from markdown.extensions.toc import slugify_unicode
from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from opsdash import __version__, briefs, config, digests, overview, proposals, records, store
from opsdash.web import settings_views

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
    """Размер файла: «340 КБ», «2,4 МБ»."""
    if not n:
        return ""
    if n < 1024 * 1024:
        return f"{max(1, round(n / 1024))} КБ"
    return f"{n / 1024 / 1024:.1f} МБ".replace(".", ",")


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
)


def render(request: Request, name: str, **ctx) -> HTMLResponse:
    settings = config.load_settings()
    ctx.setdefault("settings", settings)
    ctx.setdefault("nav", "")
    ctx["msg"] = request.query_params.get("msg", "")
    ctx["err"] = request.query_params.get("err", "")
    ctx["pending_count"] = store.scalar("SELECT COUNT(*) FROM proposals WHERE status='pending'") or 0
    ctx["attention_count"] = store.scalar("SELECT COUNT(*) FROM attention WHERE status='open'") or 0
    ctx["new_tasks_count"] = records.unseen_count()
    ctx["needs_setup"] = not any(settings[s].get("enabled")
                                 for s in ("telegram", "mail", "gmail", "calendar", "google_calendar", "kaiten", "ymessenger"))
    ctx["voice_tip"] = None if ctx["nav"] == "meetings" else _voice_tip(request)
    ctx["build"] = build()
    ctx["nav_collapsed"] = request.cookies.get("nav") == "collapsed"  # меню свёрнуто в иконки (app.js)
    ctx["theme"] = settings["web"].get("theme") or "system"  # system / light / dark — подключение dark.css
    ctx["update_available"] = bool((store.get_state("update_check") or {}).get("available"))
    return templates.TemplateResponse(request, name, ctx)


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
def digests_page(request: Request, tab: str = "daily"):
    tab = tab if tab in DIGEST_JOBS else "daily"
    return render(request, "digests.html", nav="digests", tab=tab, tabs=DIGEST_TABS, items=digests.latest(tab),
                  counts=digests.counts(), weekdays=overview.WEEKDAYS)


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
        return back(request, err=f"Не удалось проверить обновления: {e}", to="/settings?tab=general#updates")
    return back(request, "Доступна новая версия — можно обновить" if res["available"] else "Стоит последняя версия",
                to="/settings?tab=general#updates")


@app.post("/updates/apply")
def updates_apply(request: Request):
    from opsdash import jobs

    jobs.submit("update", "interactive")
    return back(request, to="/settings?tab=general&updating=1#updates")


@app.post("/api/shutdown")
def api_shutdown():
    from opsdash import daemon

    daemon.shutdown()
    return {"ok": True}


@app.get("/api/status")
def api_status():
    from opsdash import jobs

    meetings = [{"id": m["id"], "status": m["status"], "progress": m["progress"]}
                for m in store.rows("SELECT id, status, progress FROM meetings WHERE status IN ('new','transcribing','processing')")]
    running = [r["name"] for r in store.rows("SELECT name FROM jobs WHERE running=1")]
    return {"meetings": meetings, "running": running, "queued": jobs.queued()}


# --- главная --------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    snap = overview.snapshot()
    feed = store.rows("SELECT * FROM outbox ORDER BY id DESC LIMIT 8")
    batches: dict[str, list] = {}
    for p in snap["proposals"]:
        batches.setdefault(p["batch"] or "", []).append(p)
    overdue = len([c for c in snap["mine"] + snap["to_me"] if c["overdue"]])
    records.add_links(snap["mine"][:6] + snap["to_me"][:6])  # кнопка «↗ Telegram» у задач на сводке
    day = overview.day_grid()
    briefs.for_events(day["events"])
    return render(request, "home.html", nav="home", s=snap, feed=feed, batches=batches, overdue=overdue,
                  describe=proposals.describe, day=day, reminders=records.reminders())


@app.post("/refresh")
def refresh(request: Request):
    from opsdash import jobs

    settings = config.load_settings()
    for src in ("telegram", "ymessenger", "mail", "gmail", "calendar", "google_calendar", "kaiten"):
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

@app.get("/inbox", response_class=HTMLResponse)
def inbox(request: Request, chat: str = ""):
    items = records.list_attention("open")
    chats = store.rows(
        """SELECT c.*, (SELECT COUNT(*) FROM messages m WHERE m.source=c.source AND m.chat_id=c.chat_id) AS n
           FROM chats c WHERE c.enabled=1 ORDER BY c.last_ts DESC LIMIT 60""")
    from opsdash import links

    messages, chat_link = [], None
    if chat:
        source, _, chat_id = chat.partition("|")
        messages = list(reversed(store.rows(
            "SELECT * FROM messages WHERE source=? AND chat_id=? ORDER BY ts DESC LIMIT 80", (source, chat_id))))
        chat_link = links.original(source, chat_id)
    closed = store.rows("SELECT * FROM attention WHERE status!='open' ORDER BY updated DESC LIMIT 15")
    return render(request, "inbox.html", nav="inbox", items=items, chats=chats, messages=messages, chat=chat,
                  chat_link=chat_link, closed=closed)


@app.post("/attention/{aid}/{action}")
def attention_action(request: Request, aid: int, action: str):
    records.close_attention(aid, "done" if action == "done" else ("open" if action == "reopen" else "dismissed"))
    return back(request, "Отмечено")


# --- задачи (в коде — commitments) --------------------------------------------------

BOARD_TITLES = [("mine", "Мои задачи"), ("to_me", "Мне должны"), ("other", "Между коллегами"), ("done", "Готово")]
BOARD_COLUMNS = tuple(key for key, _ in BOARD_TITLES)


@app.get("/commitments", response_class=HTMLResponse)
def commitments(request: Request, view: str = "board", status: str = "open", direction: str = "", person: str = "",
                q: str = "", add: str = "", created: str = "", created_from: str = "", created_to: str = "",
                due: str = "", due_from: str = "", due_to: str = "", scale: str = "week", sort: str = "",
                new: str = "", task: int = 0, parent: int = 0):
    from opsdash import gantt

    view = view if view in ("list", "gantt") else "board"
    person, q = person.strip(), q.strip()
    # сортировку запоминаем в cookie: по умолчанию — по сроку, «сначала новые» — по дате появления
    sort = sort if sort in records.SORTS else request.cookies.get("task_sort", "due")
    sort = sort if sort in records.SORTS else "due"
    dates = {"created": records.period(created, created_from, created_to) if created else None,
             "due": records.period(due, due_from, due_to, ahead=True) if due and due != "none" else None,
             "no_due": due == "none", "unseen": new == "1"}
    board = records.commitments_board(person=person or None, q=q or None, sort=sort, **dates) if view != "list" else None
    items = (records.list_commitments(status=status, direction=direction or None, person=person or None, q=q or None,
                                      sort=sort, **dates) if view == "list" else None)
    shown = records.add_links([c for col in board.values() for c in col] if board else items)
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
    response = render(request, "commitments.html", nav="commitments", view=view, board=board, items=items, status=status,
                      direction=direction, person=person, q=q, people=records.list_people(), meetings=meetings,
                      add=add if add in records.DIRECTIONS else "mine", today=records.today_local().isoformat(),
                      folded=folded, created=created, created_from=created_from, created_to=created_to, due=due,
                      due_from=due_from, due_to=due_to, created_periods=records.CREATED_PERIODS,
                      due_periods=records.DUE_PERIODS, gantt=timeline, scale=timeline["scale"] if timeline else scale,
                      gantt_w=min(max(gantt_w, 200), 720), sort=sort, sorts=records.SORTS, new=new == "1",
                      focus=task, extra=extra, parent_task=parent_task, workload=records.workload())
    if request.query_params.get("sort") in records.SORTS:
        response.set_cookie("task_sort", sort, max_age=31536000, samesite="lax")
    return response


@app.post("/commitments")
def commitment_add(request: Request, direction: str = Form(...), title: str = Form(""), what: str = Form(""),
                   who: str = Form(""), whom: str = Form(""), due: str = Form(""), parent: str = Form("")):
    if not (title.strip() or what.strip()):
        return back(request, err="Напишите, что сделать")
    try:
        records.add_commitment(direction=direction, title=title.strip(), what=what.strip() or title.strip(),
                               who=who.strip() or None, whom=whom.strip() or None, due=due or None, source_type="manual",
                               parent_id=records.parse_task_ref(parent))
    except ValueError as e:
        return back(request, err=str(e))
    return back(request, "Задача добавлена")


@app.post("/commitments/{cid}")
def commitment_update(request: Request, cid: int, status: str = Form(None), due: str = Form(None)):
    records.update_commitment(cid, status=status or None, due=due or None)
    return back(request, "Сохранено")


@app.post("/commitments/{cid}/edit")
def commitment_edit(request: Request, cid: int, title: str = Form(""), what: str = Form(""),
                    direction: str = Form("mine"), who: str = Form(""), whom: str = Form(""), due: str = Form(""),
                    parent: str = Form("")):
    try:
        records.edit_commitment(cid, title=title, what=what, direction=direction, who=who, whom=whom, due=due or None,
                                parent_id=records.parse_task_ref(parent))
    except ValueError as e:
        return back(request, err=str(e))
    return back(request, "Задача изменена")


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
    import subprocess

    from opsdash import docs

    d = docs.find(path)  # только файлы из индекса — не любые пути
    if not d or not Path(d["path"]).exists():
        return back(request, err="Файл не найден — возможно, его переместили")
    if sys.platform == "darwin":
        subprocess.run(["open", "-R", d["path"]] if reveal else ["open", d["path"]], check=False, timeout=15)
    elif os.name == "nt":
        if reveal:
            subprocess.run(["explorer", "/select,", d["path"]], check=False, timeout=15)
        else:
            os.startfile(d["path"])  # noqa: S606 — файл из индекса рабочих папок
    else:
        subprocess.run(["xdg-open", str(Path(d["path"]).parent if reveal else d["path"])], check=False, timeout=15)
    return back(request, "Показала в Finder" if reveal else "Открыла файл")


@app.post("/docs/index")
def docs_reindex(request: Request):
    from opsdash import jobs

    jobs.submit("docs-index")
    return back(request, "Обновляю индекс документов — обычно это до минуты, первый раз дольше")


# --- календарь ------------------------------------------------------------------

MEETING_MINUTES = [15, 30, 45, 60, 90, 120]
CALENDAR_SOURCES = {"calendar": "Яндекс", "google_calendar": "Google"}


@app.get("/calendar", response_class=HTMLResponse)
def calendar_page(request: Request, start: str = "", view: str = "week"):
    from opsdash.sources import calendar

    settings = config.load_settings()
    now = config.now(settings)
    try:
        first = date.fromisoformat(start) if start else now.date()
    except ValueError:
        first = now.date()
    view = "list" if view == "list" else "week"
    if view == "week":  # неделя — с понедельника, как в Google Календаре
        first -= timedelta(days=first.weekday())
    grid = overview.week_grid(first, settings) if view == "week" else None
    days = overview.calendar_days(first, 7, settings) if view == "list" else None
    briefs.for_events(grid["events"] if grid else [e for d in days for e in d["events"]])
    sources = [s for s in CALENDAR_SOURCES if settings[s].get("enabled")]
    synced = [(store.row("SELECT last_ok FROM jobs WHERE name=?", (f"collect:{s}",)) or {}).get("last_ok") for s in sources]
    new_at = (now + timedelta(hours=1)).replace(minute=0)  # ближайший целый час
    return render(request, "calendar.html", nav="calendar", view=view, grid=grid, days=days, first=first,
                  last=first + timedelta(days=6), today=now.date(), is_current=first <= now.date() <= first + timedelta(days=6),
                  prev=first - timedelta(days=7), next=first + timedelta(days=7), can_refresh=bool(sources),
                  updated=None if None in synced else min(synced, default=None),  # по самому давнему из календарей
                  new_day=first if first > now.date() else new_at.date(), new_time=new_at.strftime("%H:%M"),
                  minutes=MEETING_MINUTES, people=[p for p in records.list_people() if p.get("email")],
                  write_calendars=calendar.write_calendar_names(settings),
                  google_calendars=(store.get_state("google_calendar_names") or []) if settings["google_calendar"].get("enabled") else [])


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
                       calendar: str = Form("")):
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
                                             reason="Создана вручную на странице «Календарь»")
        return proposals.approve(p["id"]) if p["status"] == "pending" else p

    p = await run_in_threadpool(create)
    if p["status"] != "done":
        return back(request, err=f"Встреча не создана: {(p.get('result') or {}).get('error') or p['status']}")
    where = f" в календаре «{calendar.strip()}»" if calendar.strip() else ""
    return back(request, f"Встреча «{title}» создана{where}: {store.local(start, '%d.%m %H:%M')}")


# --- встречи ---------------------------------------------------------------------

@app.get("/meetings", response_class=HTMLResponse)
def meetings(request: Request):
    from opsdash import recordings

    items = records.list_meetings(limit=200)
    folders = [str(p) for p in recordings.watch_folders(config.load_settings())]
    return render(request, "meetings.html", nav="meetings", items=items, folders=folders)


@app.get("/meetings/{mid}", response_class=HTMLResponse)
def meeting(request: Request, mid: str, tab: str = "summary"):
    from opsdash import voice

    m = records.get_meeting(mid)
    if not m:
        return back(request, err="Встреча не найдена", to="/meetings")
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
                  related=related, commitments=commitments_, folder=str(records.meeting_dir(mid)),
                  followup=followup, followup_auto=followup_auto,
                  # обрывки (меньше 10 с речи) сворачиваем, только когда спикеров много
                  describe=proposals.describe, speakers=[s for s in speakers if len(speakers) <= 4 or not s["short"] or s["who"]],
                  short_speakers=[s for s in speakers if len(speakers) > 4 and s["short"] and not s["who"]], guesses=guesses,
                  people=records.list_people(), can_tidy=len(speakers) > 1 and not (records.meeting_dir(mid) / voice.PRINTS_FILE).exists())


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
async def meeting_upload(request: Request, file: UploadFile = File(...), title: str = Form("")):
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
    mid = recordings.create_meeting(dest, title.strip() or None)
    jobs.submit("transcribe", "heavy", {"meeting_id": mid})
    return back(request, "Запись загружена — расшифровка началась", to=f"/meetings/{mid}")


@app.post("/meetings/{mid}/rerun")
def meeting_rerun(request: Request, mid: str, step: str = Form("process")):
    from opsdash import jobs

    if step == "transcribe":
        records.update_meeting(mid, status="new", error="")
        jobs.submit("transcribe", "heavy", {"meeting_id": mid})
        return back(request, "Расшифровка поставлена в очередь")
    jobs.submit("process-meeting", "heavy", {"meeting_id": mid})
    return back(request, "Разбор поставлен в очередь")


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

@app.get("/tasks", response_class=HTMLResponse)
def tasks(request: Request, who: str = "", space: str = "", q: str = "", view: str = "boards", mine: str = "",
          only: str = "", created: str = "", created_from: str = "", created_to: str = "", due: str = "",
          due_from: str = "", due_to: str = ""):
    from opsdash.sources import kaiten

    settings = config.load_settings()
    view = "list" if view == "list" else "boards"
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

@app.get("/people", response_class=HTMLResponse)
def people(request: Request, edit: int = 0):
    from opsdash import voice

    items = records.list_people()
    kusers = store.rows("SELECT * FROM kaiten_users ORDER BY full_name")
    current = next((p for p in items if p["id"] == edit), None)
    load = records.workload()
    by_person = {w["key"]: w for w in load}
    return render(request, "people.html", nav="people", items=items, kusers=kusers, current=current,
                  voices=voice.voice_counts(), load=by_person, workload=load)


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

@app.get("/journal", response_class=HTMLResponse)
def journal(request: Request, level: str = ""):
    from opsdash import jobs

    sql = "SELECT * FROM log" + (" WHERE level=?" if level else "") + " ORDER BY id DESC LIMIT 300"
    logs = store.rows(sql, [level] if level else [])
    job_rows = {j["name"]: j for j in store.rows("SELECT * FROM jobs")}
    runnable = ["collect:telegram", "collect:ymessenger", "collect:mail", "collect:gmail", "collect:calendar",
                "collect:google_calendar", "collect:kaiten", "docs-index", "anthology", "company-context",
                "recordings", "triage", "digest", "stale"]
    return render(request, "journal.html", nav="journal", logs=logs, jobs=job_rows, runnable=runnable,
                  titles=jobs.TITLES, queued=jobs.queued(), level=level)


@app.post("/jobs/run")
def job_run(request: Request, name: str = Form(...)):
    from opsdash import jobs

    args = {"force": True} if name == "triage" else None
    heavy = name in ("triage", "digest", "anthology", "company-context")  # запуски Claude — не мешать сбору сообщений
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
    return render(request, "help.html", nav="help", docs=docs, body=f_md(text), slug=slug)


# --- настройки -------------------------------------------------------------------

settings_views.register(app, render, back)


@app.exception_handler(Exception)
async def on_error(request: Request, exc: Exception):
    store.log("web", f"{request.method} {request.url.path}: {exc}", "error")
    if request.method == "POST":
        return back(request, err=str(exc)[:400])
    return HTMLResponse(f"<h1>Ошибка</h1><pre>{html.escape(str(exc))}</pre><p><a href='/'>На главную</a></p>",
                        status_code=500)

