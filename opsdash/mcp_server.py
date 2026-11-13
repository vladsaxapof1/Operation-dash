"""PULSE в ваших сессиях Claude Code — MCP-сервер только для чтения (stdio).

Сессия Claude Code в папке проекта («Проекты/Бюджет склада») о PULSE не знает: встречи, задачи и их ход, решения
лежат в его базе и папке data/, а не рядом с проектом. Переносить папки не нужно — PULSE подключается к Claude Code
на уровне пользователя (`claude mcp add-json --scope user pulse …`, кнопка наверху Настройки → Подключения), и в любой папке
у Claude появляются инструменты pulse: найти по словам, встреча, задача и её ход, решения по теме, проекты.

- **Только чтение.** Процесс открывает базу с PRAGMA query_only (store.read_only): запись падает на уровне SQLite,
  а не по честному слову в описании. Изменить что-то в PULSE из сессии нельзя.
- **Без переписки.** Чаты и письма (Telegram, почта, мессенджеры) сервер не отдаёт — там бывает личное. Из переписки
  видно только то, что PULSE сам записал в ход задач (task: «сейчас», записи хода).
- Ответы — тем же текстом, что `./ops` (те же функции CLI), с потолком размера: длинная выдача MCP обрезается
  самим Claude Code.
- В папки проектов ничего не кладётся: настройка — в ~/.claude.json пользователя, её пишет сам `claude mcp`.

Протокол — JSON-RPC построчно в stdin/stdout (MCP stdio), без сторонней библиотеки, как и клиент в opsdash.mcp.
"""

from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import IO, Any

from opsdash import config

NAME = "pulse"
PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")
MAX_CHARS = 30_000  # ~10 тыс. токенов: Claude Code сам режет ответы MCP больше 25 тыс. токенов
TRANSCRIPT_CHUNK = 25_000

INSTRUCTIONS = (
    "PULSE — личный рабочий пульт на этом компьютере: встречи (расшифровки и разборы), задачи и договорённости с ходом "
    "(что спросили, ответили, перенесли — по датам), решения по темам, проекты. Обращайся, когда для работы нужен "
    "контекст: что решили, кто что обещал, что обсуждали на встрече, на чём остановились. Начинай с search по "
    "2–4 словам темы, дальше — meeting, task, decisions. Ссылайся на источник: встреча (id и [чч:мм:сс]), задача [id]. "
    "Только чтение: изменить что-то в PULSE отсюда нельзя — скажи пользователю, что сделать в PULSE. "
    "Самих чатов и писем здесь нет — только то, что PULSE записал из них в ход задач."
)


class ToolError(Exception):
    pass


# --- инструменты ---------------------------------------------------------------------------------------------

def _ops(*argv: str) -> str:
    """Команда `./ops` чтения — её текст, как его видит фоновый Claude PULSE."""
    from opsdash import cli

    a = cli.build_parser().parse_args(list(argv))
    a.json = False  # cli.main ставит его сам — здесь main не участвует
    buf, err = io.StringIO(), io.StringIO()
    try:
        with redirect_stdout(buf), redirect_stderr(err):
            a.fn(a)
    except SystemExit:
        raise ToolError((err.getvalue() or buf.getvalue()).strip().removeprefix("Ошибка: ") or "не получилось")
    return buf.getvalue().strip()


def _stems(text: str) -> list[str]:
    """Основы слов запроса: «бюджета склада» найдёт и «бюджет», и «склад»; предлоги не нужны."""
    from opsdash import docs

    words = re.findall(r"\w+", (text or "").lower().replace("ё", "е"))
    return list(dict.fromkeys(docs._stem(w) for w in words if len(w) >= 2 and w not in docs.STOPWORDS))


def _has(text: str | None, stems: list[str]) -> bool:
    low = (text or "").lower().replace("ё", "е")
    return all(s in low for s in stems)


def _any(text: str, stems: list[str]) -> bool:
    low = text.lower().replace("ё", "е")
    return any(s in low for s in stems)


def _found_meetings(stems: list[str], limit: int) -> list[str]:
    from opsdash import records, store

    out = []
    for m in records.list_meetings(limit=1000):
        summary = records.read_meeting_file(m["id"], "summary.md")
        head = f"{m['title']} {m.get('headline') or ''}"
        where = "разбор"
        if not _has(f"{head}\n{summary}", stems):
            transcript = records.read_meeting_file(m["id"], "transcript.md")
            if not _has(f"{head}\n{summary}\n{transcript}", stems):
                continue
            where, summary = "расшифровка", transcript
        lines = [ln.strip() for ln in summary.splitlines() if ln.strip() and _any(ln, stems)][:3]
        out.append(f"встреча {m['id']} · {store.local(m['started_at'])} · {m['title']}"
                   + (f" — {m['headline']}" if m.get("headline") else "") + f" · нашлось в: {where}"
                   + "".join(f"\n    {ln[:300]}" for ln in lines))
        if len(out) >= limit:
            break
    return out


def _task_text(c: dict) -> str:
    return " ".join(str(c.get(k) or "") for k in ("title", "what", "quote", "who", "whom", "note"))


def _found_tasks(stems: list[str], limit: int, **filters) -> list[str]:
    from opsdash import cli, records

    items = [c for c in records.list_commitments(**filters, sort="priority", limit=5000)
             if not stems or _has(_task_text(c), stems)]
    items.sort(key=lambda c: c["status"] != "open")  # открытые сверху, внутри — как на доске
    return [cli._commitment_brief(c, now=200) for c in items[:limit]]


def _found_decisions(stems: list[str], limit: int) -> list[str]:
    from opsdash import decisions

    out = []
    for r in decisions.items(current=True, limit=2000):
        text = " ".join(str(r.get(k) or "") for k in ("title", "details", "topic", "subtopic", "q", "a", "metric",
                                                      "waiting_for", "quote"))
        if not _has(text, stems):
            continue
        sub = f" / {r['subtopic']}" if r.get("subtopic") else ""
        qa = f" · вопрос «{r['q']}»: {r['a']}" if r.get("q") else ""
        step = f" · следующий шаг: {r['waiting_for']}" if r.get("waiting_for") else ""
        out.append(f"[решение {r['id']}] {r['when']} · {r['topic']}{sub} · {r['stage_title']} · {r['title']}{qa}{step}")
        if len(out) >= limit:
            break
    return out


def search(query: str, limit: int = 10) -> str:
    stems = _stems(query)
    if not stems:
        raise ToolError("нужны слова для поиска — например, «бюджет склада»")
    limit = max(1, min(int(limit or 10), 30))
    parts = []
    for title, found in (("Встречи", _found_meetings(stems, limit)),
                         ("Задачи и договорённости", _found_tasks(stems, limit, status="all")),
                         ("Решения", _found_decisions(stems, limit))):
        if found:
            parts.append(f"## {title}\n" + "\n".join(found))
    if not parts:
        return f"По словам «{query}» в PULSE ничего нет: ни во встречах, ни в задачах, ни в решениях."
    return ("\n\n".join(parts) + "\n\nПодробнее: встреча — meeting(id), задача — task(id), "
            "тема решений — decisions(topic).")


def meetings(days: int | None = None, limit: int = 30) -> str:
    return _ops("meetings", "list", "--limit", str(max(1, min(int(limit or 30), 200))),
                *(["--days", str(int(days))] if days else []))


def meeting(id: str, transcript: bool = False, offset: int = 0) -> str:
    from opsdash import records

    if not transcript:
        return _ops("meetings", "show", str(id))
    if not records.get_meeting(str(id)):
        raise ToolError(f"нет встречи {id}")
    text = records.read_meeting_file(str(id), "transcript.md")
    if not text:
        return "Расшифровки этой встречи нет."
    offset = max(0, int(offset or 0))
    chunk = text[offset:offset + TRANSCRIPT_CHUNK]
    rest = len(text) - offset - len(chunk)
    return chunk + (f"\n\n… дальше ещё {rest} знаков: meeting(id, transcript=true, offset={offset + len(chunk)})"
                    if rest > 0 else "")


def tasks(search: str = "", person: str = "", direction: str = "", status: str = "open", limit: int = 50) -> str:
    if direction and direction not in ("mine", "to_me", "other"):
        raise ToolError("direction — mine, to_me или other")
    if status not in ("open", "done", "cancelled", "all"):
        raise ToolError("status — open, done, cancelled или all")
    found = _found_tasks(_stems(search), max(1, min(int(limit or 50), 200)), status=status,
                         direction=direction or None, person=person or None)
    return "\n".join(found) if found else "Задач нет."


def task(id: int) -> str:
    return _ops("commitments", "show", str(int(id)))


def decisions(topic: str = "") -> str:
    return _ops("decisions", "topic", topic) if topic.strip() else _ops("decisions", "topics")


def projects(id: int | None = None) -> str:
    return _ops("projects", "show", str(int(id))) if id else _ops("projects", "list")


def _schema(props: dict, required: tuple = ()) -> dict:
    return {"type": "object", "properties": props, "required": list(required), "additionalProperties": False}


TOOLS: dict[str, tuple[Any, str, dict]] = {
    "search": (search, "Найти в PULSE по словам: встречи (разборы и расшифровки), задачи и договорённости, решения. "
               "С этого начинай — 2–4 слова темы: «бюджет склада», «KPI ноябрь».",
               _schema({"query": {"type": "string", "description": "слова темы"},
                        "limit": {"type": "integer", "description": "сколько показать в каждом разделе, до 30"}},
                       ("query",))),
    "meeting": (meeting, "Встреча: разбор — главное, решения, задачи. transcript=true — расшифровка с метками "
                "[чч:мм:сс] (длинная — частями, offset из конца ответа).",
                _schema({"id": {"type": "string", "description": "id встречи из search или meetings"},
                         "transcript": {"type": "boolean"}, "offset": {"type": "integer"}}, ("id",))),
    "meetings": (meetings, "Последние встречи: дата, название, главное одной строкой.",
                 _schema({"days": {"type": "integer", "description": "за сколько дней"},
                          "limit": {"type": "integer"}})),
    "task": (task, "Задача целиком: суть, кто кому, срок, источник с цитатой, подзадачи, чего ждёт, и ход — что по ней "
             "спрашивали, отвечали, о чём договорились, по датам. Последняя запись хода — текущий статус.",
             _schema({"id": {"type": "integer", "description": "id задачи из search или tasks"}}, ("id",))),
    "tasks": (tasks, "Задачи и договорённости директора: mine — задачи директора, to_me — что должны директору, other — между "
              "коллегами. Фильтры по словам и человеку.",
              _schema({"search": {"type": "string"}, "person": {"type": "string", "description": "имя"},
                       "direction": {"type": "string", "enum": ["mine", "to_me", "other"]},
                       "status": {"type": "string", "enum": ["open", "done", "cancelled", "all"]},
                       "limit": {"type": "integer"}})),
    "decisions": (decisions, "Решения по темам. Без темы — список тем; с темой — вопросы, что по ним решено сейчас, "
                  "история (меняет / уточняет / отменяет) и следующий шаг. «НЕ ДЕЙСТВУЕТ» — решение заменили.",
                  _schema({"topic": {"type": "string", "description": "тема, как в списке тем"}})),
    "projects": (projects, "Проекты: задачи с общим результатом. Без id — список с прогрессом и тем, на ком стоят; "
                 "с id — задачи по шагам: что можно делать сейчас, что ждёт.",
                 _schema({"id": {"type": "integer"}})),
}


def tool_list() -> list[dict]:
    return [{"name": name, "description": desc, "inputSchema": schema,
             "annotations": {"readOnlyHint": True, "openWorldHint": False}}
            for name, (_, desc, schema) in TOOLS.items()]


def call(name: str, args: dict | None) -> tuple[str, bool]:
    """Текст ответа и признак ошибки — ошибка тоже текстом: модель прочтёт и поправит запрос."""
    if name not in TOOLS:
        return f"Нет инструмента {name}", True
    fn = TOOLS[name][0]
    try:
        text = fn(**(args or {}))
    except ToolError as e:
        return str(e), True
    except TypeError as e:
        return f"Неверные параметры: {e}", True
    except Exception as e:  # noqa: BLE001 — сервер не должен падать от одного запроса
        return f"Не получилось: {e}", True
    text = text or "Пусто."
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS] + "\n\n… ответ обрезан — уточните запрос."
    return text, False


# --- протокол --------------------------------------------------------------------------------------------------

def handle(msg: dict) -> dict | None:
    """Ответ на одно сообщение JSON-RPC; уведомления (без id) — без ответа."""
    from opsdash import __version__

    method, mid = msg.get("method"), msg.get("id")
    if mid is None:
        return None
    params = msg.get("params") or {}
    if method == "initialize":
        asked = params.get("protocolVersion")
        result = {"protocolVersion": asked if asked in PROTOCOLS else PROTOCOLS[0],
                  "capabilities": {"tools": {}}, "serverInfo": {"name": NAME, "title": "PULSE", "version": __version__},
                  "instructions": INSTRUCTIONS}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": tool_list()}
    elif method == "tools/call":
        text, err = call(params.get("name", ""), params.get("arguments"))
        result = {"content": [{"type": "text", "text": text}], "isError": err}
    else:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"нет метода {method}"}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def serve(inp: IO[str] | None = None, outp: IO[str] | None = None) -> None:
    """Цикл сервера: строка JSON на входе — строка JSON на выходе. В stdout — только протокол."""
    from opsdash import store

    if inp is None:
        sys.stdin.reconfigure(encoding="utf-8")
        inp = sys.stdin
    if outp is None:
        sys.stdout.reconfigure(encoding="utf-8")
        outp = sys.stdout
    store.read_only()
    for line in inp:
        if not line.strip():
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            reply = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "не JSON"}}
        else:
            reply = handle(msg) if isinstance(msg, dict) else None
        if reply:
            outp.write(json.dumps(reply, ensure_ascii=False) + "\n")
            outp.flush()


# --- подключение к Claude Code ---------------------------------------------------------------------------------

def spec() -> dict:
    """Как Claude Code запускает сервер: python из .venv PULSE (PULSE стоит в нём пакетом — папка сессии не важна)."""
    win = config.APP_DIR / ".venv" / "Scripts" / "python.exe"
    nix = config.APP_DIR / ".venv" / "bin" / "python"
    py = win if win.exists() else nix if nix.exists() else Path(sys.executable)
    env = {"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    if os.environ.get("OPSDASH_HOME"):
        env["OPSDASH_HOME"] = os.environ["OPSDASH_HOME"]
    return {"type": "stdio", "command": str(py), "args": ["-m", "opsdash", "mcp", "serve"], "env": env}


def _claude_json() -> Path:
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(base) / ".claude.json" if base else Path.home() / ".claude.json"


def status() -> dict:
    """Подключён ли PULSE к Claude Code: on — есть в настройках пользователя; stale — там путь к другой папке PULSE
    (её перенесли или переустановили) — надо переподключить."""
    try:
        data = json.loads(_claude_json().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    s = (data.get("mcpServers") or {}).get(NAME) if isinstance(data, dict) else None
    if not isinstance(s, dict):
        return {"on": False, "stale": False}
    want = spec()
    return {"on": True, "stale": s.get("command") != want["command"] or s.get("args") != want["args"]}


def manual_command() -> str:
    return f"claude mcp add-json --scope user {NAME} '{json.dumps(spec(), ensure_ascii=False)}'"


def _claude(*args: str) -> subprocess.CompletedProcess:
    from opsdash import brain

    settings = config.load_settings()
    claude = brain.find_claude(settings)
    if not claude:
        raise ToolError("не найден Claude Code (команда claude) — Настройки → Подключения → Claude")
    return subprocess.run([claude, "mcp", *args], capture_output=True, text=True, timeout=60,
                          env=brain._env(settings))


def connect() -> None:
    """Подключить PULSE ко всем сессиям Claude Code этого пользователя (переподключить — с новым путём)."""
    if status()["on"]:
        _claude("remove", "--scope", "user", NAME)
    proc = _claude("add-json", "--scope", "user", NAME, json.dumps(spec(), ensure_ascii=False))
    if proc.returncode != 0:
        raise ToolError((proc.stderr or proc.stdout or "claude mcp add-json не сработал").strip()[:500])


def disconnect() -> None:
    proc = _claude("remove", "--scope", "user", NAME)
    if proc.returncode != 0 and status()["on"]:
        raise ToolError((proc.stderr or proc.stdout or "claude mcp remove не сработал").strip()[:500])
