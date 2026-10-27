"""Обновление PULSE из GitHub: раз в день проверить, вышла ли новая версия, и поставить её по кнопке.

Откуда — Настройки → Основное → «Обновления» (репозиторий и ветка). Закрытый репозиторий читается с токеном
GITHUB_TOKEN из secrets: хватит доступа «только чтение» к этому репозиторию.

Как ставится:
- папка из git (есть .git) — git pull;
- папка из архива — скачиваем архив ветки и раскладываем поверх. Данные (data), пароли (secrets), настройки
  (config/settings.yaml) и библиотеки (.venv) не трогаем; файлы, которые из новой версии убрали, удаляем.
Потом — библиотеки (uv sync) и перезапуск PULSE.
"""

from __future__ import annotations

import io
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import httpx

from opsdash import config, store

API = "https://api.github.com"
STATE_KEY = "update_check"
INSTALLED_FILE = "update.json"  # в data: какая версия поставлена из архива и какие файлы в ней были
# что обновление не трогает никогда (пути от корня папки PULSE)
KEEP_DIRS = ("data/", "secrets/", ".venv/", ".git/", "dist/")
KEEP_FILES = ("config/settings.yaml", ".claude/settings.local.json")


class UpdateError(Exception):
    pass


def _options(settings: dict | None = None) -> dict:
    return (settings or config.load_settings())["updates"]


def _client(transport: httpx.BaseTransport | None = None) -> httpx.Client:
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "PULSE"}
    token = config.get_secret("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return httpx.Client(base_url=API, headers=headers, timeout=60, follow_redirects=True, transport=transport)


def _get(client: httpx.Client, path: str) -> httpx.Response:
    try:
        r = client.get(path)
    except httpx.HTTPError as e:
        raise UpdateError(f"нет связи с GitHub ({e})") from e
    if r.status_code == 401:
        raise UpdateError("GitHub не принял токен — проверьте его в настройках обновлений")
    if r.status_code in (403, 404):
        # закрытый репозиторий без токена GitHub показывает как «не найден»
        hint = "" if config.get_secret("GITHUB_TOKEN") else " Если репозиторий закрытый — укажите токен."
        raise UpdateError(f"репозиторий или ветка не найдены (GitHub ответил {r.status_code}).{hint}")
    if r.status_code >= 400:
        raise UpdateError(f"GitHub ответил {r.status_code}: {r.text[:200]}")
    return r


# --- какая версия стоит ------------------------------------------------------------------

def _record() -> dict:
    path = config.DATA_DIR / INSTALLED_FILE
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def installed(root: Path | None = None) -> dict:
    """Какая версия стоит: {"sha", "date", "how": "git" | "archive" | ""} ("" — неизвестно)."""
    root = root or config.APP_DIR
    if (root / ".git").exists():
        res = subprocess.run(["git", "-C", str(root), "log", "-1", "--format=%H %cI"], capture_output=True, text=True)
        if res.returncode == 0 and res.stdout.strip():
            sha, date = res.stdout.split()
            return {"sha": sha, "date": date, "how": "git"}
    rec = _record()
    if rec.get("sha"):
        return {"sha": rec["sha"], "date": rec.get("date", ""), "how": "archive"}
    try:  # архив, скачанный вручную: номер коммита подставлен в _build.py
        text = (root / "opsdash" / "_build.py").read_text(encoding="utf-8")
    except OSError:
        text = ""
    sha = re.search(r'COMMIT = "([0-9a-f]{40})"', text)
    date = re.search(r'DATE = "([^"$]+)"', text)
    if sha:
        return {"sha": sha.group(1), "date": date.group(1) if date else "", "how": "archive"}
    return {"sha": "", "date": "", "how": ""}


# --- проверка ------------------------------------------------------------------------------

def _head(client: httpx.Client, opts: dict) -> dict:
    c = _get(client, f'/repos/{opts["repo"]}/commits/{opts["branch"]}').json()
    return {"sha": c["sha"], "date": c["commit"]["committer"]["date"], "message": c["commit"]["message"].split("\n")[0]}


def check(settings: dict | None = None, transport: httpx.BaseTransport | None = None) -> dict:
    """Есть ли новая версия. Результат запоминается — его показывают настройки и боковое меню."""
    opts = _options(settings)
    cur = installed()
    try:
        with _client(transport) as client:
            latest = _head(client, opts)
            available, changes = cur["sha"] != latest["sha"], []
            if cur["sha"] and available:
                try:
                    cmp = _get(client, f'/repos/{opts["repo"]}/compare/{cur["sha"]}...{latest["sha"]}').json()
                    # behind — у нас новее (не отправленные изменения в папке из git): обновлять нечего
                    available = cmp.get("status") in ("ahead", "diverged")
                    changes = [x["commit"]["message"].split("\n")[0] for x in cmp.get("commits", [])][-15:][::-1]
                except UpdateError:
                    pass  # версия не из этого репозитория — без списка изменений
    except UpdateError as e:
        result = {**(store.get_state(STATE_KEY) or {}), "checked_at": store.utcnow(), "error": str(e)}
        store.set_state(STATE_KEY, result)
        raise
    result = {"checked_at": store.utcnow(), "installed": cur, "latest": latest, "available": available,
              "changes": changes, "error": ""}
    store.set_state(STATE_KEY, result)
    return result


def last_check() -> dict:
    return store.get_state(STATE_KEY) or {}


# --- установка -----------------------------------------------------------------------------

def _protected(rel: str) -> bool:
    return rel.startswith(KEEP_DIRS) or rel in KEEP_FILES


def unpack(data: bytes, root: Path) -> list[str]:
    """Разложить архив GitHub поверх папки PULSE. Сначала проверяем весь архив, потом пишем — чтобы плохой архив
    не оставил папку наполовину обновлённой."""
    root = root.resolve()
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as e:
        raise UpdateError("GitHub прислал не архив") from e
    with z:
        members = [m for m in z.infolist() if not m.is_dir()]
        if not members:
            raise UpdateError("архив пустой")
        prefix = members[0].filename.split("/")[0] + "/"  # GitHub кладёт всё в папку «владелец-репозиторий-коммит»
        plan = []
        for m in members:
            if not m.filename.startswith(prefix):
                raise UpdateError("в архиве не одна папка верхнего уровня")
            rel = m.filename[len(prefix):]
            if not rel or _protected(rel):
                continue
            target = (root / rel).resolve()
            if root not in target.parents:
                raise UpdateError(f"подозрительный путь в архиве: {rel}")
            plan.append((m, rel, target))
        if not any(rel.startswith("opsdash/") for _, rel, _ in plan):
            raise UpdateError("в архиве нет PULSE")
        for m, rel, target in plan:
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(target.name + ".new")
            tmp.write_bytes(z.read(m))
            mode = (m.external_attr >> 16) & 0o777
            if mode:
                os.chmod(tmp, mode)  # ops, *.command, скрипты — исполняемые
            os.replace(tmp, target)
    files = [rel for _, rel, _ in plan]
    for rel in set(_record().get("files") or []) - set(files):  # убрали из новой версии — убираем и здесь
        p = root / rel
        if not _protected(rel) and p.is_file():
            p.unlink()
    return files


def find_uv() -> str | None:
    found = shutil.which("uv")
    if found:
        return found
    home = Path.home()
    for p in (home / ".local" / "bin" / "uv", home / ".local" / "bin" / "uv.exe", home / ".cargo" / "bin" / "uv",
              Path("/opt/homebrew/bin/uv"), Path("/usr/local/bin/uv")):
        if p.exists():
            return str(p)
    return None


def sync_libraries(root: Path) -> None:
    """Библиотеки под новую версию — как в установщике (на Apple Silicon с macOS 14+ — с ускорением MLX)."""
    uv = find_uv()
    if not uv:
        raise UpdateError("не найден uv — запустите установку (INSTALL) заново")
    # в папке из git (у разработчика) оставляем и инструменты разработки: тесты, проверку кода
    cmd = [uv, "sync"] if (root / ".git").exists() else [uv, "sync", "--no-dev"]
    mac = platform.mac_ver()[0]
    if sys.platform == "darwin" and platform.machine() == "arm64" and mac and int(mac.split(".")[0]) >= 14:
        cmd += ["--extra", "mlx"]
    res = subprocess.run(cmd, cwd=str(root), capture_output=True, text=True, timeout=1800)
    if res.returncode:
        raise UpdateError("библиотеки не обновились: " + (res.stderr or res.stdout).strip()[-400:])


def apply(settings: dict | None = None, root: Path | None = None, transport: httpx.BaseTransport | None = None) -> str:
    """Поставить последнюю версию из ветки. Перезапуск — отдельно (restart)."""
    real = root is None
    root = root or config.APP_DIR
    opts = _options(settings)
    before = installed(root)
    if (root / ".git").exists():
        res = subprocess.run(["git", "-C", str(root), "pull", "--ff-only", "origin", opts["branch"]],
                             capture_output=True, text=True)
        if res.returncode:
            raise UpdateError("git pull не удался: " + (res.stderr or res.stdout).strip()[-300:])
    else:
        with _client(transport) as client:
            head = _head(client, opts)
            data = _get(client, f'/repos/{opts["repo"]}/zipball/{head["sha"]}').content
        files = unpack(data, root)
        (config.DATA_DIR / INSTALLED_FILE).write_text(json.dumps(
            {"sha": head["sha"], "date": head["date"], "installed_at": store.utcnow(), "files": files},
            ensure_ascii=False, indent=1), encoding="utf-8")
    sync_libraries(root)
    if real and sys.platform == "darwin":
        from opsdash import macos

        if macos.installed_apps():
            try:
                macos.make_app(root)  # приложение могло поменяться вместе с кодом
            except macos.MacError as e:
                store.log("update", f"приложение не обновлено: {e}", "warning")
    after = installed(root)
    store.set_state(STATE_KEY, {**last_check(), "installed": after, "available": False, "changes": []})
    return f'версия {before["sha"][:7] or "?"} → {after["sha"][:7]}'


def restart(in_daemon: bool) -> None:
    """Перезапустить PULSE с новым кодом. Изнутри PULSE — через пару секунд, чтобы страница успела получить ответ."""
    from opsdash import daemon, macos

    if macos.is_mac() and macos.agent_installed():
        subprocess.Popen(["/bin/sh", "-c", f"sleep 2; launchctl kickstart -k {macos.domain()}/{macos.LABEL}"],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
        return
    # Windows и запуск без службы: новый PULSE стартует, когда старый закроется
    kwargs: dict = {"cwd": str(config.APP_DIR), "stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
                    "stderr": subprocess.DEVNULL}
    if os.name == "nt":
        kwargs["creationflags"] = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5); from opsdash import cli; cli.start_background()"],
                     **kwargs)
    if in_daemon:
        import threading

        threading.Timer(1.0, daemon.shutdown).start()
    else:
        try:
            httpx.post(daemon.url() + "/api/shutdown", timeout=10, headers={"Origin": daemon.url()})
        except httpx.HTTPError:
            pass
