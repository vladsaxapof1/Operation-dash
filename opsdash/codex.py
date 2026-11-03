"""ChatGPT как «мозг» PULSE: Codex CLI в фоне (`codex exec`) — замена Claude Code на выбор (Настройки → Claude / ChatGPT).

Codex берём из приложения ChatGPT (оно носит его с собой) или установленный отдельно; вход — аккаунтом ChatGPT,
расход — в рамках подписки. Работает в папке проекта по той же инструкции CLAUDE.md и тем же навыкам: инструкцию
и сводку контекста отдаём в начале задания (Codex сам читает AGENTS.md, а не CLAUDE.md).

У Claude Code разрешённое задано списком инструментов (brain.ALLOWED_TOOLS). У Codex — песочница: писать можно
только в папку PULSE, сети нет (уведомления ./ops notify PULSE отправит сам после запуска), остальное — правила
инструкции. Свои настройки Codex (~/.codex/config.toml: плагины, управление компьютером, браузер) не подключаем.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from opsdash import config, store

SESSION_PREFIX = "chatgpt:"  # номер разговора Codex в PULSE — чтобы Claude не пытался продолжить чужой
# у Claude модель задаётся по задаче (opus — встречи, sonnet — сообщения, haiku — лёгкое); у ChatGPT модель одна,
# а глубина — сколько он рассуждает
EFFORT_BY_TIER = {"fable": "xhigh", "opus": "high", "sonnet": "medium", "haiku": "low"}
EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")

NOTE = """Ты работаешь в PULSE через Codex (ChatGPT) — по той же инструкции, что написана для Claude (ниже). У тебя так:
- Вместо инструментов Claude (Read, Grep, Glob, Skill) — команды чтения: cat, sed -n, head, rg, ls. Их можно
  в дополнение к ./ops (правило 4 для тебя — про всё остальное). Навык «X» — файл .claude/skills/X/SKILL.md:
  прочитай его и действуй по нему.
- Писать — только в data/ (apply_patch или ./ops). Код, настройки, навыки и CLAUDE.md не меняй, ничего не удаляй,
  в secrets/ не заглядывай, других программ не запускай.
- Интернета нет. ./ops notify ставит сообщение в очередь — PULSE отправит его сам, как только ты закончишь.
- Твой последний ответ — итог работы (в фоновом запуске его никто не читает, кроме журнала)."""


class Unsupported(Exception):
    """Старый Codex не знает флаг — повторить без него."""


def find(settings: dict | None = None) -> str | None:
    settings = settings or config.load_settings()
    explicit = os.path.expanduser((settings["chatgpt"].get("path") or "").strip().strip('"'))
    if explicit and Path(explicit).exists():
        return explicit
    found = shutil.which("codex")
    if found and not found.lower().endswith((".cmd", ".bat")):
        return found
    home = Path.home()
    candidates = [
        # приложение ChatGPT для Mac носит Codex с собой (вкладка Codex)
        Path("/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex"),
        home / "Applications" / "ChatGPT.app" / "Contents" / "Resources" / "codex-cli" / "bin" / "codex",
        Path("/Applications/Codex.app/Contents/Resources/codex"),
        home / ".local" / "bin" / "codex",
        home / ".npm-global" / "bin" / "codex",
        Path("/opt/homebrew/bin/codex"),
        Path("/usr/local/bin/codex"),
    ]
    for c in candidates:
        if c.exists() and os.access(c, os.X_OK):
            return str(c)
    npm = Path(os.environ.get("APPDATA", "")) / "npm" / "codex.cmd"
    return found or (str(npm) if os.environ.get("APPDATA") and npm.exists() else None)


def _codex(settings: dict) -> str:
    from opsdash import brain

    codex = find(settings)
    if not codex:
        raise brain.BrainError("Не найден Codex (ChatGPT). Установите приложение ChatGPT или Codex CLI и войдите — "
                               "Настройки → Claude / ChatGPT")
    return codex


def _env(settings: dict) -> dict:
    from opsdash import brain

    env = brain._env(settings)
    env["OPSDASH_SANDBOX"] = "1"  # сети в песочнице нет: ./ops notify не пытается отправить сам (cli.cmd_notify)
    return env


_login_cache: dict[str, tuple[float, dict | None]] = {}


def login_status(codex: str, settings: dict | None = None, max_age: float = 0) -> dict | None:
    """Вход в Codex: {"loggedIn": bool, "how": «ChatGPT» / «API-ключ»}. None — узнать не удалось."""
    cached = _login_cache.get(codex)
    if max_age and cached and time.monotonic() - cached[0] < max_age:
        return cached[1]
    try:
        proc = subprocess.run([codex, "login", "status"], capture_output=True, text=True, timeout=30,
                              encoding="utf-8", errors="replace", env=_env(settings or config.load_settings()))
        said = f"{proc.stdout}\n{proc.stderr}".lower()
    except (OSError, subprocess.SubprocessError):
        said, proc = "", None
    if proc is None or ("logged in" not in said and "not logged" not in said):
        result = None
    elif proc.returncode == 0 and "not logged" not in said:
        result = {"loggedIn": True, "how": "API-ключ" if "api key" in said else "ChatGPT"}
    else:
        result = {"loggedIn": False, "how": None}
    _login_cache[codex] = (time.monotonic(), result)
    return result


def open_login(settings: dict | None = None) -> None:
    """Открыть окно со входом в Codex (macOS — Терминал, Windows — PowerShell) — дальше браузер с ChatGPT."""
    from opsdash import brain

    settings = settings or config.load_settings()
    codex = _codex(settings)
    _login_cache.pop(codex, None)
    if os.name == "nt":
        script = config.DATA_DIR / "chatgpt-login.ps1"
        quoted = "'" + codex.replace("'", "''") + "'"
        script.write_text(
            f"& {quoted} login\n"
            'Write-Host ""; Write-Host "Готово. Вернитесь в PULSE и обновите страницу настроек."\n'
            'Read-Host "Нажмите Enter, чтобы закрыть окно"\n',
            encoding="utf-8-sig")  # Windows PowerShell читает файл как UTF-8 только с BOM
        subprocess.Popen(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)],
                         env=_env(settings), creationflags=0x00000010)  # CREATE_NEW_CONSOLE
        return
    if sys.platform != "darwin":
        raise brain.BrainError(f"Выполните в терминале: {shlex.quote(codex)} login")
    script = config.DATA_DIR / "chatgpt-login.command"  # .command открывается в Терминале без лишних разрешений
    script.write_text(
        "#!/bin/bash\n"
        f"{shlex.quote(codex)} login\n"
        'echo; echo "Готово. Вернитесь в PULSE и обновите страницу настроек. Это окно можно закрыть."\n',
        encoding="utf-8")
    script.chmod(0o700)
    subprocess.run(["open", str(script)], check=True, timeout=30)


def check(settings: dict | None = None) -> str:
    from opsdash import brain

    settings = settings or config.load_settings()
    codex = find(settings)
    if not codex:
        raise brain.BrainError("Codex (ChatGPT) не найден")
    proc = subprocess.run([codex, "--version"], capture_output=True, text=True, timeout=60, env=_env(settings))
    status = login_status(codex, settings)
    if status and not status["loggedIn"]:
        raise brain.BrainError("Codex найден, но вход не выполнен — нажмите «Войти в ChatGPT»")
    how = f", вход: {status['how']}" if status and status.get("how") else ""
    return f"{(proc.stdout or proc.stderr).strip()}{how}"


def models() -> list[dict]:
    """Модели, которые Codex показывает в своём списке (из его кэша): [{slug, title, about}]. Нет кэша — пусто."""
    home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    try:
        data = json.loads((home / "models_cache.json").read_text(encoding="utf-8"))
        items = [m for m in data.get("models") or [] if m.get("slug") and m.get("visibility", "list") == "list"]
    except (OSError, ValueError, AttributeError):
        return []
    items.sort(key=lambda m: m.get("priority") if isinstance(m.get("priority"), (int, float)) else 99)
    return [{"slug": m["slug"], "title": m.get("display_name") or m["slug"], "about": m.get("description") or ""}
            for m in items]


def effort_for(model: str | None, effort: str | None) -> str:
    """Глубина рассуждения: явная (из «Спросить») или по «весу» задачи — модели Claude, которую выбрали бы для неё."""
    if effort in EFFORTS:
        return effort
    return EFFORT_BY_TIER.get((model or "").lower(), "medium")


def instructions(settings: dict, job: str) -> str:
    """Начало нового разговора: как работать через Codex, инструкция PULSE (CLAUDE.md) и сводка контекста."""
    parts = [NOTE]
    try:
        parts.append("# Инструкция PULSE (CLAUDE.md)\n\n" + (config.APP_DIR / "CLAUDE.md").read_text(encoding="utf-8"))
    except OSError as e:
        store.log(job, f"CLAUDE.md не прочитан: {e}", "warning")
    try:
        from opsdash import context

        parts.append(context.write(settings).read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001 — без сводки ChatGPT всё равно работает, просто ищет больше
        store.log(job, f"сводка контекста не собрана: {e}", "warning")
    return "\n\n---\n\n".join(parts) + "\n\n---\n\n# Задание\n\n"


def _command(settings: dict, codex: str, *, model: str | None, resume: str | None, effort: str,
             own_config: bool = False) -> list[str]:
    cmd = [codex, "exec", "--json", "--skip-git-repo-check", "--sandbox", "workspace-write",
           "-c", f'model_reasoning_effort="{effort}"']
    if not own_config:
        cmd.append("--ignore-user-config")
    if settings["chatgpt"].get("model"):
        cmd += ["-m", settings["chatgpt"]["model"]]
    data = config.DATA_DIR.resolve()
    if not data.is_relative_to(config.APP_DIR.resolve()):  # данные живут отдельно от программы (OPSDASH_HOME)
        cmd += ["--add-dir", str(data)]
    return cmd + (["resume", resume, "-"] if resume else ["-"])


SHELLS = {"bash", "zsh", "sh", "pwsh", "powershell", "cmd"}
READERS = {"cat", "head", "tail", "sed", "nl", "less", "more", "type", "get-content", "gc"}
SEARCHERS = {"rg", "grep", "egrep", "findstr", "select-string"}
LISTERS = {"ls", "find", "fd", "tree", "dir", "get-childitem"}


def _unwrap(command) -> str:
    """Команда без оболочки: Codex пишет «/bin/zsh -lc 'cat a.txt'», нам нужно «cat a.txt»."""
    if isinstance(command, list):
        parts = [str(p) for p in command]
    else:
        try:
            parts = shlex.split(str(command))
        except ValueError:
            return str(command)
    if (len(parts) >= 3 and Path(parts[0]).name.lower().removesuffix(".exe") in SHELLS
            and parts[1].lower() in ("-lc", "-c", "-command", "/c")):
        return parts[2]
    return " ".join(parts) if isinstance(command, list) else str(command)


def step(item: dict) -> tuple[str, dict] | None:
    """Шаг Codex — так, как его назвал бы Claude Code: тогда подписи «Читает…», «Ищет…» в «Спросить» и «Офисе» общие."""
    kind = item.get("type")
    if kind == "command_execution":
        cmd = _unwrap(item.get("command") or "")
        try:
            words = shlex.split(cmd)
        except ValueError:
            words = cmd.split()
        for i, w in enumerate(words):  # первая команда цепочки: «sed -n 1,80p файл | head» → sed
            if w in ("|", "||", "&&", ";", ">", ">>"):
                words = words[:i]
                break
        prog = Path(words[0]).name.lower().removesuffix(".exe") if words else ""
        args = [w for w in words[1:] if not w.startswith("-")]
        if prog in READERS and args:
            return "Read", {"file_path": args[-1]}
        if prog in SEARCHERS and args:
            return "Grep", {"pattern": args[0]}
        if prog in LISTERS:
            return "Glob", {"pattern": args[-1] if args else "."}
        return "Bash", {"command": cmd}
    if kind == "file_change":
        changes = item.get("changes") or [{}]
        return "Edit", {"file_path": (changes[0] or {}).get("path") or ""}
    if kind == "web_search":
        return "WebSearch", {"query": item.get("query") or ""}
    if kind == "mcp_tool_call":
        return f"mcp__{item.get('server') or 'mcp'}__{item.get('tool') or ''}", item.get("arguments") or {}
    return None


def stream(prompt: str, *, settings: dict, model: str | None = None, resume: str | None = None, job: str = "brain",
           timeout_min: int | None = None, on_text=None, on_step=None, on_start=None, effort: str | None = None,
           own_config: bool = False) -> dict:
    """Запуск ChatGPT (Codex) в фоне — как brain.stream: on_text(текст) — сообщение, которое он сейчас написал,
    on_step(инструмент, параметры) — что делает, on_start(процесс) — чтобы можно было остановить.
    Вернуть {"text", "session_id", "raw"}; session_id — с приставкой «chatgpt:»."""
    from opsdash import brain

    codex = _codex(settings)
    status = login_status(codex, settings, max_age=300)
    if status and not status["loggedIn"]:
        raise brain.BrainError("ChatGPT: не выполнен вход — Настройки → Claude / ChatGPT → «Войти в ChatGPT»")
    thread = resume[len(SESSION_PREFIX):] if resume else None
    level = effort_for(model, effort)
    cmd = _command(settings, codex, model=model, resume=thread, effort=level, own_config=own_config)
    task = prompt if resume else instructions(settings, job) + prompt
    timeout = 60 * int(timeout_min or settings["claude"].get("timeout_minutes") or 30)
    kwargs = {"creationflags": 0x08000000} if os.name == "nt" else {}
    store.log(job, f"запуск ChatGPT ({settings['chatgpt'].get('model') or 'модель по умолчанию'}, рассуждение: {level})")
    started = time.monotonic()
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            cwd=str(config.APP_DIR), env=_env(settings), text=True, encoding="utf-8",
                            errors="replace", **kwargs)
    if on_start:
        on_start(proc)
    errors: list[str] = []
    reader = threading.Thread(target=lambda: errors.append(proc.stderr.read()), daemon=True)  # не даём трубе забиться
    reader.start()
    timed_out = threading.Event()

    def kill() -> None:
        timed_out.set()
        proc.kill()

    timer = threading.Timer(timeout, kill)
    timer.start()
    session, text, failure, usage, done, seen = thread, "", "", None, False, set()
    try:
        proc.stdin.write(task)
        proc.stdin.close()
        for line in proc.stdout:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            kind, item = d.get("type"), d.get("item") or {}
            if kind == "thread.started":
                session = d.get("thread_id") or session
            elif kind in ("item.started", "item.completed"):
                if item.get("type") == "agent_message" and kind == "item.completed":
                    text = (item.get("text") or "").strip()
                    if on_text and text:
                        on_text(text)
                elif on_step and item.get("id") not in seen and (s := step(item)):
                    seen.add(item.get("id"))
                    on_step(*s)
            elif kind == "turn.completed":
                usage, done = d.get("usage") or {}, True
            elif kind == "turn.failed":
                failure = str((d.get("error") or {}).get("message") or "ход не завершён")
            elif kind == "error":
                failure = str(d.get("message") or failure)
        proc.wait()
        reader.join(5)
    finally:
        timer.cancel()
        _flush_outbox()
    if timed_out.is_set():
        raise brain.BrainError(f"ChatGPT не уложился в {timeout // 60} мин")
    stderr = "".join(errors)
    if not done and not own_config and "--ignore-user-config" in stderr:
        # старый Codex не знает этот флаг — тогда со своими настройками (их и так берёт)
        return stream(prompt, settings=settings, model=model, resume=resume, job=job, timeout_min=timeout_min,
                      on_text=on_text, on_step=on_step, on_start=on_start, effort=effort, own_config=True)
    if not done or proc.returncode != 0:
        reason = failure or stderr.strip()[-1500:] or f"код {proc.returncode}"
        if "not logged in" in reason.lower() or "401 unauthorized" in reason.lower():
            reason = "не выполнен вход — Настройки → Claude / ChatGPT → «Войти в ChatGPT»"
        raise brain.BrainError(f"ChatGPT: {reason[:1500]}")
    tokens = sum(int((usage or {}).get(k) or 0) for k in ("input_tokens", "output_tokens"))
    store.log(job, f"ChatGPT закончил за {round(time.monotonic() - started)} с"
                   + (f", {round(tokens / 1000)} тыс. токенов (в рамках подписки)" if tokens else ""))
    return {"text": text, "session_id": SESSION_PREFIX + session if session else None,
            "raw": {"engine": "chatgpt", "usage": usage}}


def _flush_outbox() -> None:
    """Уведомления, которые ChatGPT поставил в очередь из песочницы, — сразу в Telegram (не ждать обслуживания)."""
    try:
        from opsdash import bot

        bot.flush_outbox()
    except Exception as e:  # noqa: BLE001 — уйдут при следующем обслуживании
        store.log("bot", f"не удалось отправить уведомление: {e}", "warning")
