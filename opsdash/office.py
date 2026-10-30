"""Раздел «Офис»: кто из агентов Claude сейчас работает, что делает и что уже сделал.

Офисы двух видов:
- PULSE — фоновые задания по рабочим местам: секретарь разбирает сообщения, стенографист — встречи и т. д.
  Каждый запуск задания пишется в agent_runs: шаги Claude по ходу («Читает расшифровку…») и итог со ссылкой —
  тот же, что в уведомлении в углу экрана (notices.summary);
- папки, где вы сами работаете с Claude Code (Настройки → Офис). Их сессии PULSE читает из журналов
  Claude Code: ~/.claude/projects/<папка>/<сессия>.jsonl, субагенты — <сессия>/subagents/agent-*.jsonl
  и .meta.json рядом. Только читает: в сами папки ничего не ставит. Формат журналов Claude Code официально
  не описан — что не разобрали, пропускаем молча; каждый раз дочитываем только дописанное. Сессии, начатые
  в приложении Claude, открываются там же кнопкой «Открыть в Claude» (desktop_sessions); фоновые запуски
  самого PULSE в офисы-папки не попадают — они уже в офисе PULSE.
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import unicodedata
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

from opsdash import config, store

# --- офис PULSE: рабочие места --------------------------------------------------------------

DESKS = [
    {"key": "triage", "title": "Секретарь", "what": "Разбирает сообщения: кто ждёт ответа, какие прозвучали задачи",
     "jobs": ["triage"], "run": [("triage", "Разобрать сейчас")], "link": ("/inbox", "Нужен ответ")},
    {"key": "meetings", "title": "Стенографист", "what": "Разбирает встречи: решения, задачи, текст итогов",
     "jobs": ["process-meeting", "followup"], "run": [], "link": ("/meetings", "Встречи")},
    {"key": "transcribe", "title": "Машинистка", "what": "Расшифровывает записи встреч на этом компьютере, без Claude",
     "jobs": ["transcribe"], "run": [], "link": ("/meetings", "Встречи")},
    {"key": "briefs", "title": "Референт", "what": "Готовит брифы к встречам из календаря",
     "jobs": ["briefs"], "run": [], "link": ("/calendar", "Календарь")},
    {"key": "digests", "title": "Редактор", "what": "Дайджесты утром и вечером, итоги недели, сводка рабочих групп",
     "jobs": ["digest", "weekly", "groups"], "run": [("digest", "Собрать дайджест")], "link": ("/digests", "Дайджесты")},
    {"key": "kb", "title": "Архивариус", "what": "База знаний: решения и уроки недели, сводка «О компании» для Claude",
     "jobs": ["anthology", "company-context"], "run": [("anthology", "Обзор недели"), ("company-context", "«О компании»")],
     "link": ("/kb", "База знаний")},
    {"key": "bot", "title": "Дежурный", "what": "Отвечает на вопросы и голосовые в Telegram-боте",
     "jobs": ["ask", "voice"], "run": [], "link": None},
    {"key": "chat", "title": "Собеседник", "what": "Отвечает в разделе «Спросить Claude»",
     "jobs": ["chat"], "run": [], "link": ("/ask", "Спросить Claude")},
]
TRACKED = {job for desk in DESKS for job in desk["jobs"]}  # сбор не пишем: он раз в 15 минут, хватит таблицы jobs
COURIER = [  # рабочее место «Курьер»: сборщики без Claude — (задание, раздел настроек, название)
    ("collect:telegram", "telegram", "Telegram"), ("collect:ymessenger", "ymessenger", "Я.Мессенджер"),
    ("collect:pachca", "pachca", "Пачка"), ("collect:mail", "mail", "Почта"), ("collect:gmail", "gmail", "Gmail"),
    ("collect:calendar", "calendar", "Календарь"), ("collect:google_calendar", "google_calendar", "Google Календарь"),
    ("collect:kaiten", "kaiten", "Kaiten"),
]
STATES = {
    "working": "работает", "queued": "в очереди", "error": "ошибка", "idle": "свободен", "off": "не подключён",
    "waiting": "ждёт ответа", "blocked": "ждёт разрешения?", "rest": "не активна", "done": "готово",
}
STEPS_KEEP = 200       # шагов одного запуска в базе
RUNS_KEEP_DAYS = 30    # сколько хранить запуски
WEEKDAYS = ["в понедельник", "во вторник", "в среду", "в четверг", "в пятницу", "в субботу", "в воскресенье"]

_local = threading.local()  # запуск задания, который идёт в этом потоке очереди (jobs._worker)


def start_run(name: str, args: dict | None) -> int | None:
    """Задание началось (jobs.run_job): завести запуск, если это рабочее место офиса."""
    if name not in TRACKED:
        return None
    rid = store.execute("INSERT INTO agent_runs(job, args, started) VALUES(?,?,?)",
                        (name, store.json_col(args or {}), store.utcnow()))
    _local.run, _local.steps = rid, []
    return rid


def add_step(label: str) -> None:
    """Шаг Claude в запуске этого потока; подряд одинаковые не повторяем."""
    rid, steps = getattr(_local, "run", None), getattr(_local, "steps", None)
    if not rid or not label or (steps and steps[-1]["t"] == label):
        return
    steps.append({"t": label, "at": store.utcnow()})
    store.execute("UPDATE agent_runs SET steps=? WHERE id=?", (store.json_col(steps[-STEPS_KEEP:]), rid))


def finish_run(rid: int | None, name: str, args: dict | None, result: str | None, error: str | None) -> None:
    """Задание закончилось: итог по-человечески и куда ведёт. Ошибки учёта не мешают самому заданию."""
    if not rid:
        return
    _local.run = None
    try:
        from opsdash import notices

        s = notices.summary(name, args or {}, result or "", error) or {}
        store.execute("UPDATE agent_runs SET finished=?, status=?, result=?, title=?, text=?, link=? WHERE id=?",
                      (store.utcnow(), "error" if error else "ok", (error or result or "")[:1000],
                       s.get("title"), s.get("text"), s.get("link"), rid))
    except Exception as e:  # noqa: BLE001
        store.log("office", f"итог запуска «{name}» не записан: {e}", "warning")


def step_recorder(on_step=None):
    """Для brain.stream: шаги Claude — в запуск задания, если оно идёт в этом потоке, и дальше — on_step."""
    if not getattr(_local, "run", None):
        return on_step

    def step(tool: str, params: dict) -> None:
        try:
            from opsdash import chat

            add_step(chat.step_label(tool, params))
        except Exception:  # noqa: BLE001 — учёт шагов не должен мешать Claude
            pass
        if on_step:
            on_step(tool, params)

    return step


def interrupted() -> None:
    """PULSE перезапустился: запуски, оставшиеся «в работе», прерваны."""
    store.execute("UPDATE agent_runs SET status='error', finished=?, result='Прервано перезапуском PULSE', "
                  "title='Прервано перезапуском PULSE' WHERE status='running'", (store.utcnow(),))


def prune() -> None:
    since = (datetime.now(timezone.utc) - timedelta(days=RUNS_KEEP_DAYS)).replace(microsecond=0).isoformat()
    store.execute("DELETE FROM agent_runs WHERE started < ?", (since,))


def pulse(settings: dict | None = None) -> list[dict]:
    """Рабочие места офиса PULSE: состояние, что делает сейчас, последний итог, история, когда следующий запуск."""
    from opsdash import jobs, notices

    settings = settings or config.load_settings()
    state = {j["name"]: j for j in store.rows("SELECT * FROM jobs")}
    queued = {key.split("|")[0] for key in jobs.queued()}
    details = {r["name"]: r["detail"] for r in notices.running()}
    out = []
    for desk in DESKS:
        names = desk["jobs"]
        runs = [_run(r) for r in store.rows(f"SELECT * FROM agent_runs WHERE job IN ({','.join('?' * len(names))}) "
                                            "ORDER BY id DESC LIMIT 12", names)]
        busy = [n for n in names if (state.get(n) or {}).get("running")]
        current = next((r for r in runs if r["status"] == "running" and r["job"] in busy), None)
        if busy and not current:  # запущено до появления офиса — шагов нет, но видно, что работает
            current = {"job": busy[0], "what": _job_title(busy[0]), "steps": [], "started": state[busy[0]]["last_run"]}
        done = [r for r in runs if r is not current and r["status"] != "running"]
        off = _off(desk["key"], settings)
        if current:
            st = "working"
        elif any(n in queued for n in names):
            st = "queued"
        elif done and done[0]["status"] == "error":
            st = "error"
        else:
            st = "off" if off and not done else "idle"
        out.append({**desk, "state": st, "state_title": STATES[st], "current": current,
                    "detail": details.get(current["job"], "") if current else "", "last": done[0] if done else None,
                    "history": done[:10], "off": off, "next": "" if off else _next(desk["key"], settings)})
    out.append(_courier(settings, state, queued))
    return out


def _job_title(name: str) -> str:
    from opsdash import jobs

    return jobs.TITLES.get(name, name).replace(" (Claude)", "")


def _run(r: dict) -> dict:
    args = store.loads(r["args"], {})
    what = _job_title(r["job"])
    if args.get("meeting_id"):
        m = store.row("SELECT title FROM meetings WHERE id=?", (args["meeting_id"],))
        what += f" «{m['title']}»" if m and m["title"] else ""
    elif args.get("when"):
        what = f"{args['when'].capitalize()} дайджест"
    started, finished = store.parse_ts(r["started"]), store.parse_ts(r["finished"])
    return {**r, "args": args, "what": what, "steps": store.loads(r["steps"], []),
            "duration": _duration((finished - started).total_seconds()) if started and finished else ""}


def _duration(seconds: float) -> str:
    s = int(seconds)
    if s < 60:
        return f"{max(s, 1)} с"
    if s < 3600:
        return f"{s // 60} мин"
    return f"{s // 3600} ч {s % 3600 // 60} мин"


def _off(key: str, settings: dict) -> str:
    """Почему рабочее место не работает (ничего не подключено) — или пусто."""
    if key == "bot" and not settings["telegram_bot"].get("enabled"):
        return "Telegram-бот не подключён"
    if key == "briefs" and not settings["briefs"].get("enabled"):
        return "Брифы выключены"
    if key == "triage" and not any(settings[s].get("enabled") for s in ("telegram", "ymessenger", "pachca", "mail", "gmail")):
        return "Не подключены ни мессенджеры, ни почта"
    return ""


def _next(key: str, settings: dict) -> str:
    """Когда рабочее место возьмётся за работу в следующий раз — по расписанию из настроек."""
    from opsdash import jobs

    sch = settings["schedule"]
    if key == "triage":
        unread = store.scalar("SELECT COUNT(*) FROM messages WHERE processed=0") or 0
        if not unread:
            return "когда придут новые сообщения"
        if not jobs.in_work_hours(settings):
            return f"в рабочее время — ждут {unread} новых"
        last = store.parse_ts((store.row("SELECT last_run FROM jobs WHERE name='triage'") or {}).get("last_run"))
        at = last + timedelta(minutes=int(sch["triage_minutes"])) if last else None
        if not at or at <= datetime.now(timezone.utc):
            return f"вот-вот — ждут {unread} новых"
        return f"{_when(at, settings)} — ждут {unread} новых"
    if key == "meetings":
        return "после расшифровки каждой записи"
    if key == "transcribe":
        n = store.scalar("SELECT COUNT(*) FROM meetings WHERE status='new'") or 0
        return f"в очереди записей: {n}" if n else "когда появится новая запись"
    if key == "briefs":
        return f"смотрит календарь каждые {sch.get('briefs_minutes') or 60} мин в рабочее время"
    if key == "digests":
        times = [(at, title) for k, title in (("digest_morning", "утренний"), ("digest_evening", "вечерний"))
                 if sch.get(k) and (at := _next_at(sch[k], settings))]
        if times:
            at, title = min(times)
            return f"{title} дайджест — {_when(at, settings)}"
        return "дайджесты выключены в расписании"
    if key == "kb":
        at = sch.get("anthology_weekly") and _next_at(sch["anthology_weekly"], settings, weekday=5)
        return f"обзор недели — {_when(at, settings)}" if at else "по кнопке"
    if key == "bot":
        return "когда напишете боту"
    if key == "chat":
        return "когда спросите"
    return ""


def _next_at(at, settings: dict, weekday: int | None = None) -> datetime | None:
    """Ближайший момент «в ЧЧ:ММ» — в рабочий день или в день недели weekday (1 — понедельник)."""
    try:
        hh, mm = (int(x) for x in config.hhmm(at).split(":"))
    except (ValueError, AttributeError):
        return None
    now = config.now(settings)
    days = (settings["owner"].get("work_hours") or {}).get("days") or [1, 2, 3, 4, 5]
    for i in range(8):
        d = (now + timedelta(days=i)).replace(hour=hh, minute=mm, second=0, microsecond=0)
        if d > now and (d.isoweekday() == weekday if weekday else d.isoweekday() in days):
            return d
    return None


def _when(at: datetime, settings: dict) -> str:
    local = at.astimezone(config.tz(settings))
    days = (local.date() - config.now(settings).date()).days
    day = {0: "сегодня", 1: "завтра"}.get(days) or WEEKDAYS[local.weekday()]
    return f"{day} в {local:%H:%M}"


def _courier(settings: dict, state: dict, queued: set) -> dict:
    """«Курьер»: сборщики сообщений, задач и календаря — когда забирали и нет ли ошибок."""
    sources = []
    for name, section, title in COURIER:
        if not settings[section].get("enabled"):
            continue
        j = state.get(name) or {}
        failed = j.get("last_error") and (not j.get("last_ok") or j["last_ok"] < (j.get("last_run") or ""))
        sources.append({"job": name, "title": title, "last": j.get("last_run"), "running": bool(j.get("running")),
                        "error": (j.get("last_error") or "").splitlines()[0][:200] if failed else ""})
    st = ("working" if any(s["running"] for s in sources) else "queued" if any(s["job"] in queued for s in sources)
          else "error" if any(s["error"] for s in sources) else "idle" if sources else "off")
    return {"key": "courier", "title": "Курьер", "what": "Забирает новые сообщения, задачи Kaiten и календарь, без Claude",
            "jobs": [s["job"] for s in sources], "run": [], "link": ("/journal", "Журнал"), "state": st,
            "state_title": STATES[st], "current": None, "detail": "", "last": None, "history": [], "sources": sources,
            "off": "" if sources else "Ничего не подключено",
            "next": f"каждые {settings['schedule']['collect_minutes']} мин" if sources else ""}


# --- офисы в папках: сессии Claude Code ---------------------------------------------------------

ACTIVE = timedelta(minutes=3)      # без новых записей дольше — Claude не работает (закрыли приложение, сбой)
STALE_TOOL = timedelta(minutes=5)  # инструмент без ответа дольше — скорее всего, ждёт разрешения
WAITING = timedelta(hours=2)       # ответил недавно — «ждёт ответа»; раньше — сессия просто не активна
HEAD = 256 * 1024                  # где искать папку сессии, прежде чем читать журнал целиком
SHOWN_STEPS = 40
WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
SILENT_TOOLS = ("ToolSearch", "TodoWrite", "update_plan", "wrap_up", "mark_chapter")  # служебные — не шаги
# нужные записи журнала — прежде чем разбирать JSON строки целиком (Claude Code пишет без пробелов, но мало ли)
_WANTED = re.compile(rb'"type": ?"(?:assistant|user|custom-title)"')
_CWD = re.compile(rb'"cwd": ?"')

_files: dict[str, dict] = {}  # путь журнала → {pos, size, mtime, s}: разобранное и докуда дочитано
_cwds: dict[str, str] = {}    # путь журнала → папка сессии
_files_lock = threading.Lock()


def claude_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def _norm(path: str) -> str:
    """Путь для сравнения: в журналах и в настройках кириллица может быть записана по-разному (NFC / NFD)."""
    return os.path.normcase(unicodedata.normalize("NFC", os.path.abspath(os.path.expanduser(path)))).rstrip(os.sep)


def _inside(path: str, root: str) -> bool:
    p, r = _norm(path), _norm(root)
    return p == r or p.startswith(r + os.sep)


def step_label(tool: str, params: dict) -> str:
    """Что делает Claude Code — коротко, для карточки сессии. Пусто — служебный шаг, не показываем."""
    p = params or {}
    if tool.endswith(SILENT_TOOLS):
        return ""
    if tool == "Bash":
        desc = (p.get("description") or "").strip()
        cmd = (p.get("command") or "").strip().split()
        return desc[:140] if desc else f"Выполняет команду {cmd[0]}" if cmd else "Выполняет команду"
    name = Path(p.get("file_path") or p.get("notebook_path") or "").name
    if tool == "Read":
        return f"Читает «{name}»" if name else "Читает файл"
    if tool == "Write":
        return f"Пишет «{name}»" if name else "Пишет файл"
    if tool in WRITE_TOOLS:
        return f"Правит «{name}»" if name else "Правит файл"
    if tool == "Grep":
        return f"Ищет «{str(p.get('pattern') or '')[:60]}»"
    if tool == "Glob":
        return f"Ищет файлы {str(p.get('pattern') or '')[:60]}"
    if tool in ("Agent", "Task"):
        return f"Поручает субагенту: {p.get('description') or 'задачу'}"
    if tool == "Skill":
        return f"Открывает навык «{p.get('skill') or p.get('name') or ''}»"
    if tool == "WebSearch":
        return f"Ищет в интернете: {str(p.get('query') or '')[:80]}"
    if tool == "WebFetch":
        return f"Открывает страницу {urlparse(str(p.get('url') or '')).netloc}"
    if tool in ("SendUserFile", "Artifact"):
        return "Показывает результат" if tool == "SendUserFile" else "Публикует страницу"
    if tool.startswith("mcp__ccd_"):  # служебное приложения Claude: главы, план, сессии
        return ""
    if "browser" in tool.lower() or "chrome" in tool.lower():
        return "Работает в браузере"
    if tool.startswith("mcp__"):
        return "Инструмент: " + tool.rsplit("__", 1)[-1].replace("_", " ").replace("-", " ")
    return tool


def desktop_dir() -> Path:
    """Где приложение Claude хранит свои сессии Claude Code (вкладка Code)."""
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support" / "Claude"
    elif os.name == "nt":
        base = Path(os.environ.get("APPDATA") or Path.home()) / "Claude"
    else:
        base = Path.home() / ".config" / "Claude"
    return base / "claude-code-sessions"


_desktop: dict[str, tuple] = {}  # файл сессии приложения → (mtime, номер журнала Claude Code, {id, archived})
_DESKTOP_ID = re.compile(r"^local_[A-Za-z0-9-]{1,64}$")  # такие номера приложение принимает в ссылке


def desktop_sessions() -> dict[str, dict]:
    """Сессии приложения Claude: номер журнала Claude Code → {id: local_…, archived}. Открыть сессию в приложении —
    ссылка claude://code/continue?session=<id>. Только чтение; что не разобрали — пропускаем."""
    out = {}
    for f in desktop_dir().glob("*/*/local_*.json"):
        key = str(f)
        try:
            mtime = f.stat().st_mtime
        except OSError:
            continue
        c = _desktop.get(key)
        if not c or c[0] != mtime:
            try:
                d = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            c = _desktop[key] = (mtime, str(d.get("cliSessionId") or ""),
                                 {"id": str(d.get("sessionId") or ""), "archived": bool(d.get("isArchived"))})
        if c[1] and _DESKTOP_ID.match(c[2]["id"]):
            out[c[1]] = c[2]
    return out


def _blank() -> dict:
    return {"cwd": "", "entry": "", "title": "", "prompt": "", "first": None, "last": None, "turn": "", "pending": {},
            "steps": [], "tools": 0, "prompts": 0, "written": [], "touched": Counter(), "model": "", "agents": 0}


def _feed(s: dict, raw: bytes, sub: bool) -> None:
    """Одна строка журнала Claude Code → состояние сессии."""
    if not _WANTED.search(raw):
        return  # служебные записи (снимки запросов, файлов) — большие и нам не нужны
    try:
        d = json.loads(raw)
    except ValueError:
        return
    kind = d.get("type")
    if kind == "custom-title":
        s["title"] = (d.get("customTitle") or "").strip() or s["title"]
        return
    if kind not in ("assistant", "user") or (d.get("isSidechain") and not sub):
        return
    at = d.get("timestamp") or s["last"]
    s["first"], s["last"] = s["first"] or at, at
    s["cwd"] = d.get("cwd") or s["cwd"]
    s["entry"] = d.get("entrypoint") or s["entry"]  # claude-desktop, cli; sdk-… — запуск без человека (claude -p)
    message = d.get("message") or {}
    content = message.get("content")
    blocks = [{"type": "text", "text": content}] if isinstance(content, str) else \
        [b for b in content or [] if isinstance(b, dict)]
    if kind == "user":
        for b in blocks:
            if b.get("type") == "tool_result":
                s["pending"].pop(b.get("tool_use_id"), None)
                s["turn"] = "open"
            elif b.get("type") == "text" and not d.get("isMeta"):
                text = (b.get("text") or "").strip()
                if text.startswith("[Request interrupted"):
                    s["turn"] = "stopped"
                    s["pending"].clear()
                elif text and not text.startswith("<"):  # <command-name>, <system-reminder> — не вопрос человека
                    s["prompts"] += 1
                    s["prompt"] = s["prompt"] or text[:300]
                    s["turn"] = "open"
        return
    s["model"] = message.get("model") or s["model"]
    for b in blocks:
        if b.get("type") == "tool_use":
            name, params = b.get("name") or "", b.get("input") or {}
            s["tools"] += 1
            s["pending"][b.get("id")] = at
            s["turn"] = "open"
            s["agents"] += name in ("Agent", "Task")
            path = params.get("file_path") or params.get("notebook_path") or ""
            if path:
                s["touched"][path] += 1
                if name in WRITE_TOOLS and path not in s["written"]:
                    s["written"].append(path)
            label = step_label(name, params)
            if label and (not s["steps"] or s["steps"][-1]["t"] != label):
                s["steps"].append({"t": label, "at": at})
                del s["steps"][:-SHOWN_STEPS]
        elif b.get("type") == "text" and (b.get("text") or "").strip():
            s["turn"] = "answered"
        elif b.get("type") == "thinking":
            s["turn"] = "open"


def _cwd(path: Path) -> str:
    """Папка, в которой шла сессия, — из первых записей журнала (запоминаем)."""
    key = str(path)
    if key in _cwds:
        return _cwds[key]
    try:
        with path.open("rb") as f:
            head = f.read(HEAD)
    except OSError:
        return ""
    for raw in head.splitlines():
        if _CWD.search(raw):
            try:
                cwd = json.loads(raw).get("cwd") or ""
            except ValueError:
                continue
            if cwd:
                _cwds[key] = cwd
                return cwd
    if len(head) == HEAD:  # в начале большого журнала папки нет — больше не ищем; маленький перечитаем, когда допишется
        _cwds[key] = ""
    return ""


def _session(path: Path, sub: bool = False) -> dict | None:
    """Разобранный журнал; читаем только то, что дописано с прошлого раза."""
    try:
        st = path.stat()
    except OSError:
        return None
    key = str(path)
    with _files_lock:
        c = _files.get(key)
        if c and c["size"] == st.st_size and c["mtime"] == st.st_mtime:
            return c["s"]
        if not c or st.st_size < c["pos"]:  # новый журнал или его переписали
            c = {"pos": 0, "s": _blank()}
        try:
            with path.open("rb") as f:
                f.seek(c["pos"])
                data = f.read()
        except OSError:
            return None
        end = data.rfind(b"\n") + 1  # недописанную последнюю строку дочитаем в следующий раз
        for raw in data[:end].splitlines():
            _feed(c["s"], raw, sub)
        c.update(pos=c["pos"] + end, size=st.st_size, mtime=st.st_mtime)
        _files[key] = c
        return c["s"]


def _state(s: dict, now: datetime, sub: bool = False, finished: bool | None = None) -> str:
    last = store.parse_ts(s["last"])
    if not last:
        return "rest"
    age = now - last
    if finished:
        return "done"
    if s["turn"] == "answered":  # ответил; запуск без человека (claude -p) ответа не ждёт — отвечать некому
        return "done" if sub else "waiting" if age < WAITING and not s["entry"].startswith("sdk") else "rest"
    if s["turn"] == "stopped":
        return "rest"
    if s["pending"]:
        oldest = min((t for t in map(store.parse_ts, s["pending"].values()) if t), default=last)
        if now - oldest < STALE_TOOL:
            return "working"
        return "blocked" if age < timedelta(hours=1) else "rest"
    return "working" if age < ACTIVE else "rest"


def _files_view(paths: list[str], root: Path, limit: int = 8) -> list[dict]:
    out = []
    for p in reversed(paths):
        if not _inside(p, str(root)) or not Path(p).exists():
            continue
        out.append({"path": p, "name": Path(p).name, "rel": os.path.relpath(p, root)})
        if len(out) >= limit:
            break
    return out


def _subagents(journal: Path, root: Path, now: datetime) -> list[dict]:
    out = []
    for f in sorted((journal.parent / journal.stem / "subagents").glob("agent-*.jsonl")):
        s = _session(f, sub=True)
        if not s or not s["last"]:
            continue
        try:
            meta = json.loads(f.with_name(f.stem + ".meta.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            meta = {}
        st = _state(s, now, sub=True, finished=meta.get("finished") is True)
        out.append({"id": f.stem.removeprefix("agent-"), "title": meta.get("description") or s["prompt"][:80] or "Субагент",
                    "kind": meta.get("agentType") or "", "state": st, "state_title": STATES[st], "first": s["first"],
                    "last": s["last"], "steps": s["steps"][-5:], "tools": s["tools"], "files": _files_view(s["written"], root, 5)})
    return sorted(out, key=lambda a: a["last"] or "", reverse=True)


def _room(s: dict, root: Path) -> str:
    """Проект (папка первого уровня), в котором работала сессия: по её папке, иначе — по файлам, которые она
    трогала. Пусто — сама папка офиса."""
    rel = os.path.relpath(_norm(s["cwd"]), _norm(str(root))) if s["cwd"] else "."
    if rel != "." and not rel.startswith(".."):
        return rel.split(os.sep)[0]
    rooms = Counter()
    for p, n in s["touched"].items():
        if _inside(p, str(root)):
            parts = os.path.relpath(_norm(p), _norm(str(root))).split(os.sep)
            if len(parts) > 1:
                rooms[parts[0]] += n
    return rooms.most_common(1)[0][0] if rooms else ""


def folder(path: str, settings: dict | None = None) -> dict:
    """Офис в папке: сессии Claude Code за последние дни по проектам (папкам первого уровня)."""
    settings = settings or config.load_settings()
    root = config.resolve_path(path)
    days = int(settings["office"].get("days") or 7)
    hide = {unicodedata.normalize("NFC", h.strip()).casefold() for h in settings["office"].get("hide") or [] if h.strip()}
    now = datetime.now(timezone.utc)
    since = now.timestamp() - days * 86400
    app = desktop_sessions()
    sessions = []
    for journal in (claude_dir() / "projects").glob("*/*.jsonl"):
        try:
            if journal.stat().st_mtime < since:
                continue
        except OSError:
            continue
        cwd = _cwd(journal)
        if not cwd or not _inside(cwd, str(root)):
            continue
        s = _session(journal)
        if not s or not (s["prompts"] or s["tools"]) or (store.parse_ts(s["last"]) or now).timestamp() < since:
            continue  # пустая или давняя: приложение Claude иногда дописывает в старые журналы служебное
        if s["entry"].startswith("sdk") and _inside(s["cwd"] or cwd, str(config.APP_DIR)):
            continue  # фоновые запуски самого PULSE (разбор сообщений, дайджесты, «Спросить Claude») — они в офисе PULSE
        room = _room(s, root)
        if room.casefold() in hide:
            continue
        agents = _subagents(journal, root, now)
        st = _state(s, now)
        if st != "working" and any(a["state"] == "working" for a in agents):
            st = "working"
        title = s["title"] or (s["prompt"].splitlines()[0][:90] if s["prompt"] else "Без названия")
        mine = app.get(journal.stem)  # сессия из приложения Claude — её можно открыть там же ссылкой
        link = f"claude://code/continue?session={mine['id']}" if mine and not mine["archived"] else ""
        sessions.append({"id": journal.stem, "title": title, "room": room, "state": st, "state_title": STATES[st], "link": link,
                         "first": s["first"], "last": s["last"], "steps": s["steps"][-6:], "tools": s["tools"],
                         "prompts": s["prompts"], "model": s["model"], "cwd": s["cwd"],
                         "files": _files_view(s["written"], root), "agents": agents})
    rooms: dict[str, list] = {}
    for s in sorted(sessions, key=lambda x: x["last"] or "", reverse=True):
        rooms.setdefault(s["room"], []).append(s)
    out = []
    for name, items in rooms.items():
        home = root / name if name else root
        passport = next((home / n for n in ("about project.md", "README.md") if (home / n).exists()), None)
        out.append({"name": name or "Общее", "path": str(home), "passport": str(passport) if passport else "",
                    "sessions": items, "working": sum(s["state"] == "working" for s in items)})
    agents = [a for s in sessions for a in s["agents"]]
    return {"name": root.name, "path": str(root), "exists": root.exists(), "days": days, "rooms": out,
            "counts": {"sessions": len(sessions), "working": sum(s["state"] == "working" for s in sessions),
                       "waiting": sum(s["state"] in ("waiting", "blocked") for s in sessions),
                       "agents": len(agents), "agents_working": sum(a["state"] == "working" for a in agents),
                       "tools": sum(s["tools"] for s in sessions)}}


def offices(settings: dict | None = None) -> list[dict]:
    """Вкладки раздела: PULSE и папки из настроек."""
    settings = settings or config.load_settings()
    tabs = [{"key": "pulse", "title": "PULSE"}]
    for i, p in enumerate(settings["office"].get("folders") or []):
        tabs.append({"key": f"f{i}", "title": config.resolve_path(p).name or p, "path": p})
    return tabs


def snapshot(key: str, settings: dict | None = None) -> dict:
    """Всё, что показывает офис в одной вкладке настроек (и /api/office); scene — для картинки (static/office.js)."""
    settings = settings or config.load_settings()
    tab = next((t for t in offices(settings) if t["key"] == key), None)
    if not tab or key == "pulse":
        snap = {"key": "pulse", "kind": "pulse", "name": "PULSE", "desks": pulse(settings)}
    else:
        snap = {"key": key, "kind": "folder", **folder(tab["path"], settings)}
    snap["scene"] = scene(snap)
    return snap


def everything(settings: dict | None = None) -> list[dict]:
    """Офис целиком: PULSE и папки из настроек — на одной картинке, списками под ней."""
    settings = settings or config.load_settings()
    return [snapshot(t["key"], settings) for t in offices(settings)]


# --- картинка офиса -------------------------------------------------------------------------------

SCENE_DESKS = 16           # столов офиса-папки на картинке; остальные сессии — в списке под ней
SCENE_PER_ZONE = 4         # столов одного проекта
SCENE_RECENT = timedelta(hours=24)  # на картинке — сессии за сутки; кто работает или ждёт ответа — всегда
SAY = {"queued": "В очереди", "error": "Не получилось — нажмите", "waiting": "Жду ответа", "blocked": "Нужно разрешение?"}


def scene(snap: dict) -> dict:
    """Что рисует картинка офиса: столы, кто за ними и что говорит. Подробности — в карточках под ней
    (id стола = data-obj карточки)."""
    if snap["kind"] == "pulse":
        desks = []
        for d in snap["desks"]:
            say = SAY.get(d["state"], "")
            if d["state"] == "working":
                steps = (d["current"] or {}).get("steps") or []
                say = steps[-1]["t"] if steps else " — ".join(x for x in ((d["current"] or {}).get("what"), d["detail"]) if x)
            desks.append({"id": f"desk:{d['key']}", "role": d["key"], "label": d["title"], "state": d["state"],
                          "say": say, "hint": f"{d['title']}: {d['state_title']}"})
        n = Counter(d["state"] for d in snap["desks"])
        return {"kind": "pulse", "key": snap["key"], "name": "PULSE", "desks": desks,
                "board": ["PULSE", f"работают: {n['working']}", f"в очереди: {n['queued']}", f"ошибок: {n['error']}"]}
    rank = {"working": 0, "blocked": 1, "waiting": 2}
    cutoff = datetime.now(timezone.utc) - SCENE_RECENT
    sessions = [s for r in snap["rooms"] for s in r["sessions"]  # уже свежие сверху
                if s["state"] in rank or (store.parse_ts(s["last"]) or cutoff) > cutoff]
    shown = sorted(sessions, key=lambda s: rank.get(s["state"], 3))[:SCENE_DESKS]
    ids = {s["id"] for s in shown}
    zones = []
    for i, r in enumerate(snap["rooms"]):
        items = sorted((s for s in r["sessions"] if s["id"] in ids), key=lambda s: rank.get(s["state"], 3))
        if items:
            zones.append({"name": r["name"], "color": i, "desks": [_scene_session(s) for s in items[:SCENE_PER_ZONE]]})
    c = snap["counts"]
    more = c["sessions"] - sum(len(z["desks"]) for z in zones)
    return {"kind": "folder", "key": snap["key"], "name": snap["name"], "zones": zones,
            "board": [snap["name"][:18], f"работают: {c['working']}", f"ждут ответа: {c['waiting']}",
                      f"субагенты: {c['agents_working']}", f"ещё в списке: {more}" if more else f"сессий: {c['sessions']}"]}


def _scene_session(s: dict) -> dict:
    agents = [a for a in s["agents"] if a["state"] == "working"]
    say = SAY.get(s["state"], "")
    if s["state"] == "working":
        say = s["steps"][-1]["t"] if s["steps"] else "Работают субагенты" if agents else ""
    return {"id": f"sess:{s['id']}", "label": s["title"][:32], "state": s["state"], "say": say,
            "hint": f"{s['title']}: {s['state_title']}",
            "agents": [{"label": a["title"][:32], "say": a["steps"][-1]["t"] if a["steps"] else ""} for a in agents[:2]],
            "agents_more": max(0, len(agents) - 2)}


def can_open(path: str, settings: dict | None = None) -> bool:
    """Открывать из офиса можно только то, что лежит в папках офисов."""
    settings = settings or config.load_settings()
    return bool(path) and Path(path).exists() and any(
        _inside(path, str(config.resolve_path(f))) for f in settings["office"].get("folders") or [])
