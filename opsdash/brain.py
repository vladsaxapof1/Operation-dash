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
    # npm-обёртка claude.cmd (Windows) — только если родного claude.exe нет: она идёт через cmd.exe (см. _fit_command)
    if found and not _is_cmd(found):
        return found
    home = Path.home()
    candidates = [
        home / ".local" / "bin" / "claude.exe",
        home / ".local" / "bin" / "claude",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "claude" / "claude.exe",
        home / ".claude" / "local" / "claude",
        Path("/opt/homebrew/bin/claude"),
        Path("/usr/local/bin/claude"),
    ]
    for c in candidates:
        if str(c) not in ("", ".") and c.exists():
            return str(c)
    desktop = _desktop_app_claude()
    if desktop:
        return str(desktop)
    npm = Path(os.environ.get("APPDATA", "")) / "npm" / "claude.cmd"
    return found or (str(npm) if os.environ.get("APPDATA") and npm.exists() else None)


def _is_cmd(path: str) -> bool:
    return path.lower().endswith((".cmd", ".bat"))


def app_dirs() -> list[Path]:
    """Папки приложения Claude: в них его Claude Code (claude-code) и сессии вкладки Code. На Windows приложение
    в пакете MSIX пишет не в %APPDATA%, а в свою копию в %LOCALAPPDATA%\\Packages."""
    if sys.platform == "darwin":
        return [Path.home() / "Library" / "Application Support" / "Claude"]
    if os.name == "nt":
        dirs = [Path(os.environ.get("APPDATA") or Path.home()) / "Claude"]
        if os.environ.get("LOCALAPPDATA"):
            dirs += sorted(Path(os.environ["LOCALAPPDATA"], "Packages").glob("Claude_*/LocalCache/Roaming/Claude"))
        return dirs
    return [Path.home() / ".config" / "Claude"]


def _desktop_app_claude() -> Path | None:
    """Claude Code, который скачало себе приложение Claude (вкладка Code). Папка меняется с каждой
    версией (…/claude-code/2.1.308/<хеш>/claude.app), поэтому ищем при каждом запуске и берём новейшую."""
    if sys.platform == "darwin":
        patterns = ["*/*/claude.app/Contents/MacOS/claude"]
    elif os.name == "nt":
        patterns = ["*/*/claude.exe", "*/claude.exe"]
    else:
        return None
    found = []  # (номер версии — первая папка внутри claude-code, путь)
    for root in (d / "claude-code" for d in app_dirs()):
        for pattern in patterns:
            found += [(p.relative_to(root).parts[0], p) for p in root.glob(pattern) if os.access(p, os.X_OK)]

    def version(item: tuple) -> tuple:
        return tuple(int(x) if x.isdigit() else 0 for x in item[0].split("."))

    best = max(found, key=version, default=None)
    return best[1] if best else None


def _git_bash() -> str | None:
    """Windows: bash.exe из Git for Windows — без него Claude Code не запускается. Сам Claude ищет git в PATH, а PATH
    у PULSE — с момента входа в Windows: Git, поставленный позже, он без подсказки не увидит."""
    if os.name != "nt" or os.environ.get("CLAUDE_CODE_GIT_BASH_PATH"):
        return None
    git = shutil.which("git")
    candidates = [Path(git).parent.parent / "bin" / "bash.exe"] if git else []  # …\Git\cmd\git.exe → …\Git\bin
    for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramW6432"),
                 os.environ.get("LOCALAPPDATA") and os.path.join(os.environ["LOCALAPPDATA"], "Programs")):
        if base:
            candidates.append(Path(base) / "Git" / "bin" / "bash.exe")
    return next((str(c) for c in candidates if c.exists()), None)


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
    """Открыть окно со входом в Claude Code (macOS — Терминал, Windows — PowerShell) — дальше браузер,
    как при обычном входе."""
    settings = settings or config.load_settings()
    claude = find_claude(settings)
    if not claude:
        raise BrainError("Claude Code не найден")
    if os.name == "nt":
        script = config.DATA_DIR / "claude-login.ps1"
        quoted = "'" + claude.replace("'", "''") + "'"
        script.write_text(
            f"& {quoted} auth login --claudeai\n"
            'Write-Host ""; Write-Host "Готово. Вернитесь в PULSE и обновите страницу настроек."\n'
            'Read-Host "Нажмите Enter, чтобы закрыть окно"\n',
            encoding="utf-8-sig")  # Windows PowerShell читает файл как UTF-8 только с BOM
        subprocess.Popen(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)],
                         env=_env(settings), creationflags=0x00000010)  # CREATE_NEW_CONSOLE: у PULSE своего окна нет
        return
    if sys.platform != "darwin":
        raise BrainError(f"Выполните в терминале: {shlex.quote(claude)} auth login")
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
    git_bash = settings["claude"].get("git_bash_path") or _git_bash()
    if git_bash:
        env["CLAUDE_CODE_GIT_BASH_PATH"] = git_bash
    return env


def _command(settings: dict, claude: str, job: str, *, model: str | None, resume: str | None,
             partial: bool = False, effort: str | None = None) -> list[str]:
    # всегда поток событий: в нём и ответ, и заполненность лимитов подписки (opsdash.usage)
    cmd = [claude, "-p", "--output-format", "stream-json", "--verbose"] + (["--include-partial-messages"] if partial else [])
    if model:
        cmd += ["--model", model]
    if effort:
        cmd += ["--effort", effort]
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


def _fit_command(cmd: list[str], prompt: str) -> tuple[list[str], str]:
    """npm-обёртка claude.cmd запускается через cmd.exe, а он не примет команду длиннее 8191 символа — сводка контекста
    туда не влезает. Тогда сводку отдаём в начале самого задания (оно идёт через stdin, там длина не ограничена)."""
    if not _is_cmd(cmd[0]) or "--append-system-prompt" not in cmd:
        return cmd, prompt
    i = cmd.index("--append-system-prompt")
    return cmd[:i] + cmd[i + 2:], f"{cmd[i + 1]}\n\n---\n\n{prompt}"


def _claude(settings: dict) -> str:
    claude = find_claude(settings)
    if not claude:
        raise BrainError("Не найден Claude Code (команда claude). Установите его и войдите — см. docs/01-установка.md")
    return claude


def _result(data, returncode: int, stderr: str, out: str, job: str, model: str | None = None,
            limits: dict | None = None) -> dict:
    from opsdash import usage

    usage.record(job, model, data if isinstance(data, dict) else None, limits)  # расход и лимиты — в «Настройки → Claude»
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
    """Запуск Claude в фоне; вернуть {"text", "session_id", "raw"}."""
    return stream(prompt, model=model, resume=resume, job=job, timeout_min=timeout_min, partial=False)


def stream(prompt: str, *, model: str | None = None, resume: str | None = None, job: str = "brain",
           timeout_min: int | None = None, on_text=None, on_step=None, on_start=None, effort: str | None = None,
           partial: bool = True) -> dict:
    """Как run, но по ходу работы: on_text(текст) — ответ, который Claude сейчас пишет (каждый новый абзац
    рассуждений начинается заново), on_step(инструмент, параметры) — что он делает (читает файл, ищет),
    on_start(процесс) — чтобы его можно было остановить. Результат тот же, что у run."""
    from opsdash import office

    on_step = office.step_recorder(on_step)  # шаги — и в раздел «Офис», если это фоновое задание
    settings = config.load_settings()
    cmd = _command(settings, _claude(settings), job, model=model, resume=resume, partial=partial, effort=effort)
    cmd, task = _fit_command(cmd, prompt)  # prompt не трогаем: с ним stream повторяют для старого Claude Code
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
    data, text, last, limits = None, "", "", None
    try:
        proc.stdin.write(task)
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
            elif kind == "rate_limit_event":  # заполненность лимитов подписки — на весь аккаунт, раз за запуск
                limits = (d.get("rate_limit_info") or {}).get("unifiedWindows") or limits
            elif kind == "result":
                data = d
        proc.wait()
        reader.join(5)
    finally:
        timer.cancel()
    if timed_out.is_set():
        raise BrainError(f"Claude не уложился в {timeout // 60} мин")
    stderr = "".join(errors)
    if data is None and effort and "--effort" in stderr:  # старый Claude Code не знает --effort — думает как умеет
        return stream(prompt, model=model, resume=resume, job=job, timeout_min=timeout_min, on_text=on_text,
                      on_step=on_step, on_start=on_start, partial=partial)
    if data is None and partial and "include-partial-messages" in stderr:  # старый Claude Code: без ответа по ходу
        result = stream(prompt, model=model, resume=resume, job=job, timeout_min=timeout_min, on_step=on_step,
                        on_start=on_start, effort=effort, partial=False)
        if on_text:
            on_text(result["text"])
        return result
    return _result(data, proc.returncode, stderr, last, job, model, limits)


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
