"""Свои подключения через MCP (беклог MORE-1): сервисы, которые помощник читает сам, без нашей разработки под каждый.

Как устроено:
- витрина — opsdash/mcp_catalog.yaml (GitHub, Linear, Notion, HubSpot, Jira и Confluence) и «Свой сервер»
  (программа на этом компьютере или адрес);
- подключённые серверы — config/settings.yaml, раздел mcp.servers; токены и пароли — в secrets/.env
  (MCP_<ID>__<ИМЯ>), в настройки и в текст для модели не попадают;
- PULSE сам спрашивает у сервера список инструментов (клиент MCP ниже) и делит их на чтение и изменения:
  по подсказке readOnlyHint, а без неё — по названию;
- **чтение** помощник вызывает сам, но только инструменты, отмеченные в окне подключения: Claude получает файл
  secrets/mcp.json (--mcp-config --strict-mcp-config) и список разрешённых инструментов (--allowedTools) — права
  задаются кодом, а не текстом навыка;
- **изменения** помощник только предлагает (`./ops propose mcp`), а выполняет PULSE после «Да» директора —
  тем же клиентом.

ChatGPT пока MCP-подключениями не пользуется — работают с Claude.
"""

from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import threading
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path

import httpx
import yaml

from opsdash import config, store

CATALOG = Path(__file__).with_name("mcp_catalog.yaml")
PROTOCOL = "2025-06-18"
TIMEOUT = 60  # первый запуск npx / uvx скачивает сервер — бывает долго
# задания, в которых помощнику доступны MCP-подключения: разговоры и то, где нужен контекст из сервисов.
# Частые фоновые разборы (переписка, встречи) обходятся без них — каждый запуск сервера стоит секунд
JOBS = {"chat", "ask", "voice", "meeting-brief", "task-log", "agent-brief"}
ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,30}$")
NEEDS = {"node": ("npx", "Node.js", "https://nodejs.org"), "uv": ("uvx", "uv", "https://docs.astral.sh/uv/")}
READ_WORDS = {"get", "list", "search", "read", "fetch", "query", "find", "retrieve", "describe", "view", "lookup",
              "show", "count", "export", "download", "check", "status", "info", "whoami", "me"}
WRITE_WORDS = {"create", "update", "delete", "remove", "add", "set", "post", "send", "write", "edit", "move",
               "archive", "assign", "comment", "merge", "close", "reopen", "upload", "patch", "put", "insert",
               "transition", "invite", "publish", "duplicate", "rename", "append"}
STATES = {"ok": "подключено", "setup": "нужна настройка", "error": "ошибка", "paused": "выключено", "off": "не подключено"}


class MCPError(Exception):
    pass


# --- витрина и подключённые серверы ----------------------------------------------------------------------------

@lru_cache(maxsize=1)
def catalog() -> list[dict]:
    return yaml.safe_load(CATALOG.read_text(encoding="utf-8"))["servers"]


def preset(pid: str) -> dict | None:
    return next((p for p in catalog() if p["id"] == pid), None)


def servers(settings: dict | None = None) -> dict[str, dict]:
    settings = settings or config.load_settings()
    return dict(((settings.get("mcp") or {}).get("servers") or {}))


def server(sid: str, settings: dict | None = None) -> dict | None:
    s = servers(settings).get(sid)
    return {**s, "id": sid} if s else None


def fields(s: dict) -> list[dict]:
    """Поля сервера: из витрины или, у своего сервера, — сохранённые при подключении (без значений секретов)."""
    p = preset(s.get("preset") or "")
    return list(p["fields"]) if p else list(s.get("fields") or [])


def secret_key(sid: str, name: str) -> str:
    return f"MCP_{sid.upper()}__{re.sub(r'[^A-Za-z0-9]', '_', name).upper()}"


def _values(s: dict) -> dict[str, str]:
    """Значения полей: обычные — из настроек, секретные — из secrets/.env."""
    plain = s.get("values") or {}
    return {f["name"]: (config.get_secret(secret_key(s["id"], f["name"])) if f.get("secret") else plain.get(f["name"], ""))
            for f in fields(s)}


def launch(s: dict) -> dict:
    """Как запустить сервер: {"type": "stdio", "command", "args", "env"} или {"type": "http", "url", "headers"}."""
    p = preset(s.get("preset") or "") or {}
    values = _values(s)
    for target, source in (p.get("derive") or {}).items():  # Confluence — тем же входом, что Jira
        if values.get(source) and any(values.get(k) for k in values if k.startswith(target.split("_")[0] + "_URL")):
            values[target] = values[source]
    env = {**(p.get("env") or {}), **{k: v for k, v in values.items()
                                      if v and (next((f for f in fields(s) if f["name"] == k), {"kind": "env"})["kind"] == "env")}}
    if (s.get("transport") or p.get("transport")) == "http":
        headers = {}
        for f in fields(s):
            if f["kind"] == "header" and values.get(f["name"]):
                headers[f.get("header") or f["name"]] = (f.get("format") or "{}").format(values[f["name"]])
        return {"type": "http", "url": s.get("url") or p.get("url"), "headers": headers}
    return {"type": "stdio", "command": s.get("command") or p.get("command"),
            "args": list(s.get("args") if s.get("args") is not None else p.get("args") or []), "env": env}


def which(prog: str) -> str | None:
    """Программа для stdio-сервера: из PATH или из обычных мест установки — у фоновой службы PULSE PATH короче, чем
    в терминале (uv установщик PULSE кладёт в ~/.local/bin, Node.js — в /opt/homebrew или Program Files)."""
    found = shutil.which(prog)
    if found:
        return found
    home = Path.home()
    names = [prog + ext for ext in (".exe", ".cmd", "")] if os.name == "nt" else [prog]
    dirs = [home / ".local" / "bin", home / ".cargo" / "bin", Path("/opt/homebrew/bin"), Path("/usr/local/bin")]
    if os.name == "nt":
        dirs += [Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "nodejs",
                 Path(os.environ.get("APPDATA", "")) / "npm"]
    for d in dirs:
        for n in names:
            if (d / n).exists():
                return str(d / n)
    expanded = Path(os.path.expanduser(prog))
    return str(expanded) if expanded.is_absolute() and expanded.exists() else None


def _env_with_path(exe: str, env: dict) -> dict:
    """Папку найденной программы — в начало PATH: npx без неё не найдёт node."""
    folder = str(Path(exe).parent)
    return {**env, "PATH": os.pathsep.join([folder, env.get("PATH") or os.environ.get("PATH", "")])}


def missing_program(s: dict) -> dict | None:
    """Чего не хватает на компьютере для stdio-сервера: {"program", "title", "url"} или None."""
    p = preset(s.get("preset") or "") or {}
    if p.get("needs") in NEEDS:
        prog, title, url = NEEDS[p["needs"]]
        return None if which(prog) else {"program": prog, "title": title, "url": url}
    if (s.get("transport") or p.get("transport")) == "stdio" and s.get("command"):
        if not which(s["command"]):
            return {"program": s["command"], "title": s["command"], "url": ""}
    return None


def save(sid: str, *, preset_id: str = "", title: str = "", transport: str = "", command: str = "",
         args: list[str] | None = None, url: str = "", values: dict[str, str] | None = None,
         custom_fields: list[dict] | None = None, enabled: bool = True) -> dict:
    """Подключить сервер или обновить его. Пустое значение секретного поля — «не менять»."""
    if not ID_RE.match(sid):
        raise MCPError("Имя сервера — латиница, цифры и «_», начинается с буквы")
    p = preset(preset_id) if preset_id else None
    settings = config.load_settings()
    mcp = settings.setdefault("mcp", {}).setdefault("servers", {})
    s = dict(mcp.get(sid) or {})
    s.update({"preset": preset_id or s.get("preset") or "", "title": (title or s.get("title") or (p or {}).get("title") or sid).strip(),
              "enabled": bool(enabled)})
    if not p:
        transport = transport or s.get("transport") or ("http" if url else "stdio")
        if transport == "http":
            if not url.startswith(("http://", "https://")) and not s.get("url"):
                raise MCPError("Адрес сервера — с http:// или https://")
            s.update({"transport": "http", "url": url.strip() or s.get("url"), "command": "", "args": []})
        else:
            if not (command or s.get("command")):
                raise MCPError("Команда запуска сервера — например, npx")
            s.update({"transport": "stdio", "command": (command or s.get("command")).strip(),
                      "args": args if args is not None else s.get("args") or [], "url": ""})
        if custom_fields is not None:
            s["fields"] = [{k: v for k, v in f.items() if k != "value"} for f in custom_fields]
    plain = dict(s.get("values") or {})
    for f in fields({**s, "id": sid}):
        v = (values or {}).get(f["name"])
        if v is None:
            continue
        v = v.strip()
        if f.get("secret"):
            if v:
                config.set_secret(secret_key(sid, f["name"]), v)
        else:
            plain[f["name"]] = v
    s["values"] = plain
    mcp[sid] = s
    config.save_settings(settings)
    return {**s, "id": sid}


def remove(sid: str) -> None:
    settings = config.load_settings()
    s = ((settings.get("mcp") or {}).get("servers") or {}).pop(sid, None)
    if s is None:
        return
    for f in fields({**s, "id": sid}):
        if f.get("secret"):
            config.set_secret(secret_key(sid, f["name"]), None)
    config.save_settings(settings)
    store.set_state(f"mcp_tools:{sid}", None)


def set_tools(sid: str, *, read_on: list[str], writes: bool) -> None:
    """Какие инструменты чтения помощник вызывает сам; предлагает ли он изменения (через «Да»)."""
    settings = config.load_settings()
    s = settings["mcp"]["servers"][sid]
    reads = [t["name"] for t in tools(sid) if t["read"]]
    s["read_off"] = [t for t in reads if t not in set(read_on)]
    s["writes"] = bool(writes)
    config.save_settings(settings)


def new_id(*candidates: str) -> str:
    """Имя для нового сервера: из первого варианта, в котором есть латиница (название, программа), иначе server."""
    base = "server"
    for c in candidates:
        slug = re.sub(r"_+", "_", re.sub(r"[^a-z0-9_]", "_", Path((c or "").strip().strip('"')).stem.lower())).strip("_")
        if re.search(r"[a-z]", slug):
            base = slug
            break
    if not base[0].isalpha():
        base = "s_" + base
    base = base[:24]
    taken = set(servers())
    if base not in taken:
        return base
    n = 2
    while f"{base}_{n}" in taken:
        n += 1
    return f"{base}_{n}"


# --- инструменты: чтение или изменение --------------------------------------------------------------------------

def is_read(tool: dict) -> bool:
    """Инструмент только читает? Сервер подсказывает сам (annotations.readOnlyHint); нет подсказки — по названию:
    get_issue, search_pages — чтение; create_issue, update_page — изменение. Непонятное — считаем изменением."""
    ann = tool.get("annotations") or {}
    if isinstance(ann.get("readOnlyHint"), bool):
        return ann["readOnlyHint"]
    words = [w.lower() for w in re.split(r"[_\-.\s/]+|(?<=[a-z])(?=[A-Z])", tool.get("name") or "") if w]
    if any(w in WRITE_WORDS for w in words):
        return False
    return any(w in READ_WORDS for w in words[:3])


def tools(sid: str) -> list[dict]:
    """Инструменты сервера, как PULSE их узнал при последней проверке: name, title, description, read, on."""
    s = server(sid)
    cached = store.get_state(f"mcp_tools:{sid}") or {}
    off = set((s or {}).get("read_off") or [])
    return [{**t, "on": t["read"] and t["name"] not in off} for t in cached.get("tools") or []]


def status(sid: str, settings: dict | None = None) -> dict:
    """Состояние подключения для карточки: kind (ok / setup / error / paused), label, сколько инструментов."""
    s = server(sid, settings)
    if not s:
        return {"kind": "off", "label": STATES["off"]}
    cached = store.get_state(f"mcp_tools:{sid}") or {}
    ts = tools(sid)
    reads = sum(t["on"] for t in ts)
    writes = sum(not t["read"] for t in ts)
    base = {"read_count": reads, "write_count": writes, "checked": cached.get("checked") or "",
            "error": cached.get("error") or "", "tools_total": len(ts)}
    lack = [f["label"] for f in fields(s) if f.get("secret") and not config.get_secret(secret_key(sid, f["name"]))]
    if not s.get("enabled"):
        return {**base, "kind": "paused", "label": STATES["paused"]}
    if lack:
        return {**base, "kind": "setup", "label": "нужна настройка: " + lack[0][:1].lower() + lack[0][1:]}
    need = missing_program(s)
    if need:
        return {**base, "kind": "setup", "label": f"нужна программа {need['title']}"}
    if cached.get("error"):
        return {**base, "kind": "error", "label": "ошибка: " + _short_error(cached["error"])}
    if not ts:
        return {**base, "kind": "setup", "label": "нужна проверка"}
    return {**base, "kind": "ok", "label": f"подключено · {reads} {_tools_word(reads)} чтения"}


def _tools_word(n: int) -> str:
    n100 = n % 100
    if 11 <= n100 <= 19:
        return "инструментов"
    return {1: "инструмент"}.get(n % 10, "инструмента" if 2 <= n % 10 <= 4 else "инструментов")


def _short_error(err: str) -> str:
    low = err.lower()
    if "401" in low or "403" in low or "unauthor" in low or "не принял" in low:
        return "ключ не принят"
    if "не найден" in low or "no such file" in low or "not found" in low and "command" in low:
        return "программа не найдена"
    if "timed out" in low or "timeout" in low or "не ответил" in low:
        return "сервер не ответил"
    return "не удалось подключиться"


# --- клиент MCP: JSON-RPC по stdio или HTTP ---------------------------------------------------------------------

class _Stdio:
    """Сервер-программа: строки JSON в stdin / stdout."""

    def __init__(self, spec: dict, timeout: float):
        exe = which(spec["command"]) or os.path.expanduser(spec["command"])
        env = _env_with_path(exe, {**os.environ, **{k: str(v) for k, v in (spec.get("env") or {}).items()}})
        kwargs = {"creationflags": 0x08000000} if os.name == "nt" else {}
        try:
            self.proc = subprocess.Popen([exe, *spec.get("args", [])], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
                                         env=env, cwd=str(config.DATA_DIR), **kwargs)
        except OSError as e:
            raise MCPError(f"не удалось запустить {spec['command']}: программа не найдена или не запускается ({e})")
        self.timeout = timeout
        self.lines: queue.Queue = queue.Queue()
        self.err: list[str] = []
        threading.Thread(target=self._pump, daemon=True).start()
        threading.Thread(target=lambda: self.err.append(self.proc.stderr.read()[-2000:]), daemon=True).start()

    def _pump(self) -> None:
        for line in self.proc.stdout:
            self.lines.put(line)
        self.lines.put(None)

    def send(self, msg: dict) -> None:
        try:
            self.proc.stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError):
            raise MCPError("сервер закрылся: " + self._stderr())

    def receive(self, rid: int) -> dict:
        while True:
            try:
                line = self.lines.get(timeout=self.timeout)
            except queue.Empty:
                raise MCPError(f"сервер не ответил за {int(self.timeout)} с")
            if line is None:
                raise MCPError("сервер закрылся: " + self._stderr())
            try:
                msg = json.loads(line)
            except ValueError:
                continue  # служебный вывод сервера
            if msg.get("id") == rid and ("result" in msg or "error" in msg):
                return msg
            if "method" in msg and "id" in msg:  # запрос от сервера (ping, roots/list) — вежливо отвечаем
                self.send({"jsonrpc": "2.0", "id": msg["id"], **({"result": {}} if msg["method"] == "ping"
                                                                 else {"error": {"code": -32601, "message": "not supported"}})})

    def _stderr(self) -> str:
        try:
            self.proc.wait(2)
        except subprocess.TimeoutExpired:
            pass
        return (" ".join(self.err).strip() or f"код {self.proc.returncode}")[-500:]

    def close(self) -> None:
        try:
            self.proc.stdin.close()
            self.proc.terminate()
            self.proc.wait(3)
        except Exception:  # noqa: BLE001
            self.proc.kill()


class _Http:
    """Сервер по адресу (Streamable HTTP): POST с JSON-RPC, ответ — JSON или поток событий."""

    def __init__(self, spec: dict, timeout: float, transport: httpx.BaseTransport | None = None):
        self.url = spec["url"]
        self.http = httpx.Client(timeout=timeout, headers=spec.get("headers") or {}, transport=transport,
                                 follow_redirects=True)
        self.session: str | None = None
        self.pending: dict[int, dict] = {}

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
             "MCP-Protocol-Version": PROTOCOL}
        if self.session:
            h["Mcp-Session-Id"] = self.session
        return h

    def send(self, msg: dict) -> None:
        try:
            with self.http.stream("POST", self.url, json=msg, headers=self._headers()) as resp:
                if resp.status_code in (401, 403):
                    raise MCPError(f"сервер не принял ключ ({resp.status_code}) — проверьте токен")
                if resp.status_code == 404 and self.session:
                    raise MCPError("сессия сервера закончилась — проверьте подключение ещё раз")
                if resp.status_code >= 400:
                    resp.read()
                    raise MCPError(f"{resp.status_code} {resp.text[:200]}")
                self.session = resp.headers.get("mcp-session-id") or self.session
                if resp.status_code == 202 or "id" not in msg:
                    return
                if "text/event-stream" in resp.headers.get("content-type", ""):
                    data: list[str] = []
                    for line in resp.iter_lines():
                        if line.startswith("data:"):
                            data.append(line[5:].strip())
                        elif not line.strip() and data:
                            self._keep("\n".join(data))
                            data = []
                            if msg["id"] in self.pending:
                                return
                    if data:
                        self._keep("\n".join(data))
                else:
                    resp.read()
                    self._keep(resp.text)
        except httpx.HTTPError as e:
            raise MCPError(f"нет связи с сервером: {e}")

    def _keep(self, text: str) -> None:
        try:
            data = json.loads(text)
        except ValueError:
            return
        for msg in data if isinstance(data, list) else [data]:
            if isinstance(msg, dict) and "id" in msg and ("result" in msg or "error" in msg):
                self.pending[msg["id"]] = msg

    def receive(self, rid: int) -> dict:
        if rid not in self.pending:
            raise MCPError("сервер не прислал ответ")
        return self.pending.pop(rid)

    def close(self) -> None:
        self.http.close()


class Session:
    def __init__(self, conn):
        self.conn = conn
        self.rid = 0

    def request(self, method: str, params: dict | None = None) -> dict:
        self.rid += 1
        self.conn.send({"jsonrpc": "2.0", "id": self.rid, "method": method, **({"params": params} if params else {})})
        msg = self.conn.receive(self.rid)
        if "error" in msg:
            err = msg["error"] or {}
            raise MCPError(f"{method}: {err.get('message') or err}")
        return msg.get("result") or {}

    def notify(self, method: str) -> None:
        self.conn.send({"jsonrpc": "2.0", "method": method})


@contextmanager
def connect(spec: dict, timeout: float = TIMEOUT, transport: httpx.BaseTransport | None = None):
    """Сессия с сервером: initialize → initialized → запросы."""
    conn = _Http(spec, timeout, transport) if spec["type"] == "http" else _Stdio(spec, timeout)
    try:
        session = Session(conn)
        from opsdash import __version__

        session.request("initialize", {"protocolVersion": PROTOCOL, "capabilities": {},
                                       "clientInfo": {"name": "PULSE", "version": __version__}})
        session.notify("notifications/initialized")
        yield session
    finally:
        conn.close()


def check(sid: str, transport: httpx.BaseTransport | None = None) -> str:
    """Подключиться к серверу и запомнить его инструменты. Строка — для «Проверить подключение»."""
    s = server(sid)
    if not s:
        raise MCPError(f"Нет подключения {sid}")
    need = missing_program(s)
    if need:
        err = f"не найдена программа {need['program']} — установите {need['title']}" + (f": {need['url']}" if need["url"] else "")
        store.set_state(f"mcp_tools:{sid}", {"tools": [], "checked": store.utcnow(), "error": err})
        raise MCPError(err)
    found: list[dict] = []
    try:
        with connect(launch(s), transport=transport) as session:
            cursor = None
            for _ in range(20):
                res = session.request("tools/list", {"cursor": cursor} if cursor else None)
                found += res.get("tools") or []
                cursor = res.get("nextCursor")
                if not cursor:
                    break
    except MCPError as e:
        prev = store.get_state(f"mcp_tools:{sid}") or {}
        store.set_state(f"mcp_tools:{sid}", {**prev, "checked": store.utcnow(), "error": str(e)[:500]})
        raise
    items = [{"name": t["name"], "title": (t.get("title") or (t.get("annotations") or {}).get("title") or "").strip(),
              "description": " ".join((t.get("description") or "").split())[:300], "read": is_read(t)}
             for t in found if t.get("name")]
    store.set_state(f"mcp_tools:{sid}", {"tools": items, "checked": store.utcnow(), "error": ""})
    reads = sum(t["read"] for t in items)
    return f"инструментов: {len(items)} — чтение {reads}, изменения {len(items) - reads}"


def call(sid: str, tool: str, args: dict | None = None, transport: httpx.BaseTransport | None = None) -> str:
    """Вызвать инструмент — для изменений после «Да» директора. Вернуть текст ответа сервера."""
    s = server(sid)
    if not s or not s.get("enabled"):
        raise MCPError(f"Подключение {sid} выключено или удалено")
    with connect(launch(s), transport=transport) as session:
        res = session.request("tools/call", {"name": tool, "arguments": args or {}})
    text = " ".join(c.get("text") or "" for c in res.get("content") or [] if c.get("type") == "text").strip()
    if res.get("isError"):
        raise MCPError(text[:500] or "сервис вернул ошибку")
    return text[:2000]


# --- для помощника: файл подключений и разрешённые инструменты --------------------------------------------------

def active(settings: dict | None = None) -> list[dict]:
    """Включённые и настроенные серверы, у которых PULSE знает инструменты."""
    out = []
    for sid in servers(settings):
        st = status(sid, settings)
        if st["kind"] == "ok":
            out.append(server(sid, settings))
    return out


def allowed_tools(settings: dict | None = None) -> list[str]:
    """Инструменты чтения, которые помощник вызывает сам: mcp__<id>__<инструмент>."""
    return [f"mcp__{s['id']}__{t['name']}" for s in active(settings) for t in tools(s["id"]) if t["on"]]


def claude_config(settings: dict | None = None) -> Path | None:
    """secrets/mcp.json для Claude Code (--mcp-config). Ключи — в файле в папке secrets, которую помощнику читать
    запрещено; в текст для модели они не попадают. Нет подключений — файла нет."""
    path = config.SECRETS_DIR / "mcp.json"
    on = active(settings)
    if not on:
        path.unlink(missing_ok=True)
        return None
    servers_ = {}
    for s in on:
        spec = launch(s)
        if spec["type"] == "http":
            servers_[s["id"]] = {"type": "http", "url": spec["url"], "headers": spec["headers"]}
        else:
            exe = which(spec["command"]) or spec["command"]
            servers_[s["id"]] = {"type": "stdio", "command": exe, "args": spec["args"],
                                 "env": _env_with_path(exe, spec["env"]) if Path(exe).is_absolute() else spec["env"]}
    config.SECRETS_DIR.mkdir(parents=True, exist_ok=True)
    text = json.dumps({"mcpServers": servers_}, ensure_ascii=False, indent=2)
    if not path.exists() or path.read_text(encoding="utf-8") != text:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(text, encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        tmp.replace(path)
    return path


def summary_lines(settings: dict | None = None) -> list[str]:
    """Для ./ops mcp list и подсказки помощнику: что подключено, какие инструменты он вызывает сам, какие — через «Да»."""
    out = []
    for sid, raw in servers(settings).items():
        s = {**raw, "id": sid}
        st = status(sid, settings)
        out.append(f"{sid} — {s.get('title') or sid}: {st['label']}")
        ts = tools(sid)
        reads = [t["name"] for t in ts if t["on"]]
        writes = [t["name"] for t in ts if not t["read"]] if s.get("writes", True) else []
        if reads:
            out.append(f"  читает сам (mcp__{sid}__…): {', '.join(reads)}")
        if writes and st["kind"] == "ok":
            out.append(f"  изменения — только предложением ./ops propose mcp --server {sid} --tool …: {', '.join(writes)}")
    return out


def _command_line(s: dict) -> str:
    """Команда запуска своего сервера одной строкой — для поля в окне (пути с пробелами — в кавычках)."""
    import shlex

    parts = [s.get("command") or "", *(s.get("args") or [])]
    if os.name == "nt":
        return subprocess.list2cmdline(parts)
    return shlex.join(parts)


# --- карточки каталога «Подключения» ----------------------------------------------------------------------------

def cards(settings: dict | None = None) -> list[dict]:
    """Группа «Сервисы через MCP»: подключённые, потом витрина, потом «Свой сервер»."""
    settings = settings or config.load_settings()
    have = servers(settings)
    out = []
    for sid, raw in have.items():
        p = preset(raw.get("preset") or "") or {}
        st = status(sid, settings)
        out.append({"id": f"mcp:{sid}", "href": f"/settings?tab=mcp&id={sid}", "title": raw.get("title") or sid,
                    "title_now": raw.get("title") or sid, "color": p.get("color") or "#6B7280",
                    "mono": p.get("mono") or (raw.get("title") or sid)[:2].upper(), "logo": "",
                    "catches": p.get("catches") or "Свой MCP-сервер",
                    "reads": "Только отмеченные инструменты чтения" + (f": {st['read_count']}" if st.get("read_count") else ""),
                    "skips": "Инструменты изменения — их помощник сам не вызывает",
                    "never": "Ничего не меняет без вашего «Да»", **st})
    used = {raw.get("preset") for raw in have.values()}
    for p in catalog():
        if p["id"] in used:
            continue
        out.append({"id": f"mcp-new:{p['id']}", "href": f"/settings?tab=mcp&preset={p['id']}", "title": p["title"],
                    "title_now": p["title"], "color": p["color"], "mono": p["mono"], "logo": "", "catches": p["catches"],
                    "kind": "off", "label": "сообщество" if p.get("vendor") == "community" else "официальный сервер",
                    "minutes": p.get("minutes")})
    out.append({"id": "mcp-new:custom", "href": "/settings?tab=mcp&preset=custom", "title": "Свой сервер",
                "title_now": "Свой сервер", "color": "#6B7280", "mono": "+", "logo": "",
                "catches": "Любой MCP-сервер — программа на этом компьютере или адрес", "kind": "off",
                "label": "команда или адрес", "minutes": 5})
    return out


def conn(sid: str = "", preset_id: str = "", settings: dict | None = None) -> dict | None:
    """Окно подключения: тексты слева (как у источников из connectors.yaml) и что нужно форме справа."""
    settings = settings or config.load_settings()
    s = server(sid, settings) if sid else None
    if sid and not s:
        return None
    pid = (s or {}).get("preset") if s else preset_id
    p = preset(pid or "")
    if not s and not p and preset_id != "custom":
        return None
    title = (s or {}).get("title") or (p or {}).get("title") or "Свой сервер"
    st = status(sid, settings) if s else {"kind": "off", "label": STATES["off"]}
    return {
        "id": "mcp", "server_id": sid, "preset_id": pid or "custom", "preset": p, "server": s, "title": title,
        "title_now": title, "color": (p or {}).get("color") or "#6B7280", "mono": (p or {}).get("mono") or "+", "logo": "",
        "catches": (p or {}).get("catches") or "Любой MCP-сервер: программа на этом компьютере или адрес в интернете",
        "enables": (p or {}).get("enables") or ["Помощник ищет и читает данные сервиса, когда это нужно для ответа",
                                                 "Изменения в сервисе — только предложением, после вашего «Да»"],
        "reads": "Только то, что разрешите: инструменты чтения, отмеченные в этом окне",
        "skips": "Инструменты, которые что-то меняют, помощник сам не вызывает",
        "never": "Ничего не меняет без вашего «Да»: изменения — предложением в очереди",
        "how": "Ключ доступа сервиса" if p else "Команда запуска или адрес сервера",
        "minutes": (p or {}).get("minutes") or 5, "help": "сервисы-через-mcp", "steps": [], "todo": None,
        "vendor": (p or {}).get("vendor") or "", "note": (p or {}).get("note") or "",
        "fields": fields(s) if s else list((p or {}).get("fields") or []),
        "command_line": _command_line(s) if s and s.get("transport") == "stdio" else "",
        "field_values": (s or {}).get("values") or {},  # не «values»: в шаблоне conn.values — метод словаря
        "need": missing_program(s or {"preset": pid or ""}),
        "tools": tools(sid) if s else [], **st,
    }
