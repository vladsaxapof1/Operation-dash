"""Запуск Claude Code в фоне (`claude -p`) — «мозг» PULSE.

Claude работает в папке проекта: читает CLAUDE.md и навыки из .claude/skills,
пользуется командой ./ops и пишет только в data/. Писать во внешние системы
он может только через очередь подтверждений (./ops propose ...).
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

ALLOWED_TOOLS = [
    "Read", "Glob", "Grep", "Skill",
    "Edit(data/**)", "Write(data/**)",
    "Bash(./ops *)", "Bash(ops *)",
]
DISALLOWED_TOOLS = ["Read(secrets/**)", "Edit(secrets/**)", "Edit(config/**)", "WebFetch", "WebSearch"]


class BrainError(Exception):
    pass


def find_claude(settings: dict | None = None) -> str | None:
    settings = settings or config.load_settings()
    explicit = os.path.expanduser((settings["claude"].get("path") or "").strip().strip('"'))
    if explicit and Path(explicit).exists():
        return explicit
    found = shutil.which("claude")
    if found:
        return found
    home = Path.home()
    candidates = [
        home / ".local" / "bin" / "claude.exe",
        home / ".local" / "bin" / "claude",
        Path(os.environ.get("APPDATA", "")) / "npm" / "claude.cmd",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "claude" / "claude.exe",
        home / ".claude" / "local" / "claude",
        Path("/opt/homebrew/bin/claude"),
        Path("/usr/local/bin/claude"),
    ]
    for c in candidates:
        if str(c) not in ("", ".") and c.exists():
            return str(c)
    desktop = _desktop_app_claude()
    return str(desktop) if desktop else None


def _desktop_app_claude() -> Path | None:
    """Claude Code, который скачало себе приложение Claude (вкладка Code). Папка меняется с каждой
    версией (…/claude-code/2.1.308/<хеш>/claude.app), поэтому ищем при каждом запуске и берём новейшую."""
    if sys.platform != "darwin":
        return None
    root = Path.home() / "Library" / "Application Support" / "Claude" / "claude-code"
    found = [p for p in root.glob("*/*/claude.app/Contents/MacOS/claude") if os.access(p, os.X_OK)]

    def version(p: Path) -> tuple:
        return tuple(int(x) if x.isdigit() else 0 for x in p.parts[-6].split("."))

    return max(found, key=version, default=None)


_login_cache: dict[str, tuple[float, dict | None]] = {}


def login_status(claude: str, settings: dict | None = None, max_age: float = 0) -> dict | None:
    """Вход в Claude Code: {"loggedIn": bool, "email": …}. None — узнать не удалось (старая версия).
    max_age — можно взять ответ не старше стольких секунд (главная страница не запускает claude каждый раз)."""
    cached = _login_cache.get(claude)
    if max_age and cached and time.monotonic() - cached[0] < max_age:
        return cached[1]
    try:
        proc = subprocess.run([claude, "auth", "status", "--json"], capture_output=True, text=True, timeout=30,
                              env=_env(settings or config.load_settings()))
        data = json.loads(proc.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        data = None
    result = data if isinstance(data, dict) and "loggedIn" in data else None
    _login_cache[claude] = (time.monotonic(), result)
    return result


def open_login(settings: dict | None = None) -> None:
    """macOS: открыть Терминал со входом в Claude Code — дальше браузер, как при обычном входе."""
    claude = find_claude(settings)
    if not claude:
        raise BrainError("Claude Code не найден")
    if sys.platform != "darwin":
        raise BrainError(f'Выполните в PowerShell: & "{claude}" auth login')
    script = config.DATA_DIR / "claude-login.command"  # .command открывается в Терминале без лишних разрешений
    script.write_text(
        "#!/bin/bash\n"
        f"{shlex.quote(claude)} auth login --claudeai\n"
        'echo; echo "Готово. Вернитесь в PULSE и обновите страницу настроек. Это окно можно закрыть."\n',
        encoding="utf-8")
    script.chmod(0o700)
    subprocess.run(["open", str(script)], check=True, timeout=30)


def _env(settings: dict) -> dict:
    env = dict(os.environ)
    scripts = Path(sys.executable).parent
    env["PATH"] = os.pathsep.join([str(config.APP_DIR), str(scripts), env.get("PATH", "")])
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["OPSDASH_HOME"] = str(config.ROOT)
    env["OPSDASH_AGENT"] = "1"  # фоновый Claude: подтверждать предложения ему запрещено (см. cli.cmd_proposals)
    git_bash = settings["claude"].get("git_bash_path")
    if git_bash:
        env["CLAUDE_CODE_GIT_BASH_PATH"] = git_bash
    return env


def _command(settings: dict, claude: str, job: str, *, model: str | None, resume: str | None,
             stream: bool = False) -> list[str]:
    cmd = [claude, "-p", "--output-format", "stream-json", "--verbose", "--include-partial-messages"] if stream \
        else [claude, "-p", "--output-format", "json"]
    if model:
        cmd += ["--model", model]
    if resume:
        cmd += ["--resume", resume]
    for folder in settings["knowledge"].get("folders") or []:
        p = config.resolve_path(folder)
        if p.exists():
            cmd += ["--add-dir", str(p)]
    cmd += ["--allowedTools", *ALLOWED_TOOLS, "--disallowedTools", *DISALLOWED_TOOLS]
    try:  # короткая память о компании — к инструкции каждого запуска (opsdash.context)
        from opsdash import context

        cmd += ["--append-system-prompt", context.write(settings).read_text(encoding="utf-8")]
    except Exception as e:  # noqa: BLE001 — без сводки Claude всё равно работает, просто ищет больше
        store.log(job, f"сводка контекста не собрана: {e}", "warning")
    return cmd


def _claude(settings: dict) -> str:
    claude = find_claude(settings)
    if not claude:
        raise BrainError("Не найден Claude Code (команда claude). Установите его и войдите — см. docs/01-установка.md")
    return claude


def _result(data, returncode: int, stderr: str, out: str, job: str) -> dict:
    if isinstance(data, dict) and (returncode != 0 or data.get("is_error")) and (data.get("result") or data.get("subtype")):
        # в JSON-ответе есть причина — показываем её, а не весь ответ целиком
        reason = str(data.get("result") or data.get("subtype"))
        if "not logged in" in reason.lower():
            reason = "не выполнен вход — Настройки → Claude → «Войти в Claude»"
        raise BrainError(f"Claude: {reason[:1500]}")
    if returncode != 0 or not isinstance(data, dict):
        err = (stderr or out or "").strip()[-1500:]
        raise BrainError(f"Claude завершился с ошибкой (код {returncode}): {err}")
    cost = data.get("total_cost_usd")
    store.log(job, f"Claude закончил за {round((data.get('duration_ms') or 0) / 1000)} с"
                   + (f", ≈${cost:.2f} (в рамках подписки)" if cost else ""))
    return {"text": (data.get("result") or "").strip(), "session_id": data.get("session_id"), "raw": data}


def run(prompt: str, *, model: str | None = None, resume: str | None = None, job: str = "brain",
        timeout_min: int | None = None) -> dict:
    settings = config.load_settings()
    cmd = _command(settings, _claude(settings), job, model=model, resume=resume)
    timeout = 60 * int(timeout_min or settings["claude"].get("timeout_minutes") or 30)
    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW: без мигающих окон консоли
    store.log(job, f"запуск Claude ({model or 'модель по умолчанию'})")
    try:
        proc = subprocess.run(
            cmd, input=prompt, cwd=str(config.APP_DIR), env=_env(settings), capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=timeout, **kwargs,
        )
    except subprocess.TimeoutExpired as e:
        raise BrainError(f"Claude не уложился в {timeout // 60} мин") from e
    out = (proc.stdout or "").strip()
    data = None
    for line in reversed(out.splitlines()):
        try:
            data = json.loads(line)
            break
        except ValueError:
            continue
    if data is None:
        try:
            data = json.loads(out)
        except ValueError:
            data = None
    return _result(data, proc.returncode, proc.stderr, out, job)


def stream(prompt: str, *, model: str | None = None, resume: str | None = None, job: str = "brain",
           timeout_min: int | None = None, on_text=None, on_step=None, on_start=None) -> dict:
    """Как run, но по ходу работы: on_text(текст) — ответ, который Claude сейчас пишет (каждый новый абзац
    рассуждений начинается заново), on_step(инструмент, параметры) — что он делает (читает файл, ищет),
    on_start(процесс) — чтобы его можно было остановить. Результат тот же, что у run."""
    settings = config.load_settings()
    cmd = _command(settings, _claude(settings), job, model=model, resume=resume, stream=True)
    timeout = 60 * int(timeout_min or settings["claude"].get("timeout_minutes") or 30)
    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = 0x08000000
    store.log(job, f"запуск Claude ({model or 'модель по умолчанию'})")
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
    data, text, last = None, "", ""
    try:
        proc.stdin.write(prompt)
        proc.stdin.close()
        for line in proc.stdout:
            last = line
            try:
                d = json.loads(line)
            except ValueError:
                continue
            kind = d.get("type")
            if kind == "stream_event":
                e = d.get("event") or {}
                if e.get("type") == "content_block_start" and (e.get("content_block") or {}).get("type") == "text":
                    text = ""
                elif e.get("type") == "content_block_delta" and (e.get("delta") or {}).get("type") == "text_delta":
                    text += e["delta"].get("text") or ""
                    if on_text:
                        on_text(text)
            elif kind == "assistant" and on_step:
                for block in (d.get("message") or {}).get("content") or []:
                    if block.get("type") == "tool_use":
                        on_step(block.get("name") or "", block.get("input") or {})
            elif kind == "result":
                data = d
        proc.wait()
        reader.join(5)
    finally:
        timer.cancel()
    if timed_out.is_set():
        raise BrainError(f"Claude не уложился в {timeout // 60} мин")
    stderr = "".join(errors)
    if data is None and "include-partial-messages" in stderr:  # старый Claude Code: без ответа по ходу работы
        result = run(prompt, model=model, resume=resume, job=job, timeout_min=timeout_min)
        if on_text:
            on_text(result["text"])
        return result
    return _result(data, proc.returncode, stderr, last, job)


def check(settings: dict | None = None) -> str:
    claude = find_claude(settings)
    if not claude:
        raise BrainError("Claude Code не найден")
    proc = subprocess.run([claude, "--version"], capture_output=True, text=True, timeout=60)
    status = login_status(claude, settings)
    if status and not status["loggedIn"]:
        raise BrainError("Claude Code найден, но вход не выполнен — нажмите «Войти в Claude»")
    who = f", вход: {status['email']}" if status and status.get("email") else ""
    return f"{(proc.stdout or proc.stderr).strip()}{who}"
