"""PULSE: один локальный процесс = веб-админка + планировщик + Telegram-бот."""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import socket
import sys
import threading
import webbrowser

import httpx

from opsdash import config, store, transcribe

server = None
stop_event = threading.Event()
_lock_handle = None
PID_FILE = config.DATA_DIR / "pult.pid"


def _setup_logging() -> None:
    config.LOGS_DIR.mkdir(parents=True, exist_ok=True)
    # pythonw (запуск без окна) не даёт stdout/stderr — отдельный файл, не тот, что ротируется
    if sys.stdout is None or sys.stderr is None:
        stream = open(config.LOGS_DIR / "pult.stdout.log", "a", encoding="utf-8", buffering=1)  # noqa: SIM115
        sys.stdout = sys.stdout or stream
        sys.stderr = sys.stderr or stream
    handler = logging.handlers.RotatingFileHandler(config.LOGS_DIR / "pult.log", maxBytes=2_000_000, backupCount=3,
                                                   encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    # httpx пишет в журнал адреса запросов, а в адресе Telegram-бота — его токен
    for noisy in ("httpx", "httpcore", "telethon", "huggingface_hub"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def running_port(settings: dict | None = None) -> int:
    """Порт запущенного PULSE (из pid-файла), иначе — из настроек."""
    try:
        return int(json.loads(PID_FILE.read_text(encoding="utf-8"))["port"])
    except (OSError, ValueError, KeyError, TypeError):
        settings = settings or config.load_settings()
        return int(settings["web"]["port"])


def url(settings: dict | None = None) -> str:
    return f"http://localhost:{running_port(settings)}"


def is_running(settings: dict | None = None) -> bool:
    try:
        return httpx.get(url(settings) + "/health", timeout=2).status_code == 200
    except httpx.ReadTimeout:
        return True  # подключился, но ответ медленный: PULSE занят (например, расшифровкой), а не выключен
    except httpx.HTTPError:
        return False


def _acquire_lock() -> bool:
    """Только один PULSE на компьютере: блокировка файла средствами ОС (снимается сама при выходе)."""
    global _lock_handle
    handle = open(config.DATA_DIR / "pult.lock", "a+")  # noqa: SIM115
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return False
    _lock_handle = handle
    return True


def _bind(port: int) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if os.name == "nt":
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    else:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", port))
    sock.listen(128)
    sock.setblocking(False)
    return sock


def sync_claude_settings(settings: dict | None = None) -> None:
    """Папки с документами директора → .claude/settings.local.json (для чата в Claude Code)."""
    settings = settings or config.load_settings()
    path = config.APP_DIR / ".claude" / "settings.local.json"
    data = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            data = {}
    folders = [str(config.resolve_path(f)) for f in settings["knowledge"].get("folders") or []]
    data.setdefault("permissions", {})["additionalDirectories"] = folders
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def run(open_browser: bool = False) -> None:
    import uvicorn

    from opsdash import bot, jobs
    from opsdash.web.app import app

    global server
    config.ensure_dirs()
    _setup_logging()
    settings = config.load_settings()
    # второй экземпляр (ярлык + автозапуск) ничего не трогает: только открывает браузер
    if not _acquire_lock():
        print(f"PULSE уже запущен: {url(settings)}")
        if open_browser:
            webbrowser.open(url(settings))
        return
    port = int(settings["web"]["port"])
    try:
        sock = _bind(port)
    except OSError as e:
        msg = f"Порт {port} занят другой программой — смените порт в config/settings.yaml (web.port): {e}"
        print(msg)
        store.log("pult", msg, "error")
        return
    if settings["recordings"].get("hf_endpoint"):
        os.environ["HF_ENDPOINT"] = settings["recordings"]["hf_endpoint"]
    sync_claude_settings(settings)
    PID_FILE.write_text(json.dumps({"pid": os.getpid(), "port": port}), encoding="utf-8")
    store.log("pult", f"PULSE запущен (pid {os.getpid()}, порт {port})")

    jobs.start_workers(stop_event)
    threading.Thread(target=bot.run_forever, args=(stop_event,), daemon=True, name="bot").start()

    cfg = uvicorn.Config(app, log_config=None, access_log=False)
    server = uvicorn.Server(cfg)
    if open_browser:
        threading.Timer(2.0, lambda: webbrowser.open(f"http://localhost:{port}")).start()
    try:
        server.run(sockets=[sock])
    finally:
        stop_event.set()
        transcribe.stop_children()  # остановка по сигналу (launchd, Ctrl+C), а не кнопкой
        store.log("pult", "PULSE остановлен")
        PID_FILE.unlink(missing_ok=True)


def shutdown() -> None:
    stop_event.set()
    transcribe.stop_children()  # расшифровка идёт в своём процессе — не оставляем его висеть
    if server:
        server.should_exit = True
