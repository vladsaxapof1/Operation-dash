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
from datetime import datetime
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
              home / ".cargo" / "bin" / "uv.exe", Path("/opt/homebrew/bin/uv"), Path("/usr/local/bin/uv")):
        if p.exists():
            return str(p)
    return None


def _windows() -> bool:
    return os.name == "nt"


def nvidia_present() -> bool:
    """Windows: есть ли видеокарта NVIDIA — по nvidia-smi (ставится вместе с драйвером) или по списку видеоадаптеров."""
    if not _windows():
        return False
    if shutil.which("nvidia-smi") or (Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "nvidia-smi.exe").exists():
        return True
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", "(Get-CimInstance Win32_VideoController).Name"],
                             capture_output=True, text=True, timeout=30, creationflags=0x08000000).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return "nvidia" in (out or "").lower()


def wants_cuda(root: Path | None = None) -> bool:
    """Ставить ли библиотеку видеокарты NVIDIA (дополнение cuda, cuBLAS ~630 МБ): Windows, видеокарта NVIDIA есть, а в
    настройках не выбран «процессор». Уже стоит — оставляем: без дополнения uv sync её удалит, а потом скачает заново.
    Этим же решением пользуются установщик и UPDATE.bat (scripts/install.ps1, update.ps1)."""
    if not _windows():
        return False
    if ((root or config.APP_DIR) / ".venv" / "Lib" / "site-packages" / "nvidia" / "cublas").is_dir():
        return True
    # настроек ещё нет (установка, шаг 3) — будет «видеокарта, если получится» из settings.example.yaml
    try:
        if config.SETTINGS_FILE.exists() and config.load_settings()["recordings"].get("device") == "cpu":
            return False
    except Exception:  # noqa: BLE001 — настройки не читаются: решаем по видеокарте
        pass
    return nvidia_present()


CUDA_EXTRA = ["--extra", "cuda"]


def _without_cuda(cmd: list[str]) -> list[str]:
    """Та же команда uv sync, но без библиотеки видеокарты — если она не скачалась, PULSE всё равно обновляется."""
    i = next((i for i in range(len(cmd) - 1) if cmd[i:i + 2] == CUDA_EXTRA), None)
    return cmd if i is None else cmd[:i] + cmd[i + 2:]


def _sync_command(root: Path) -> list[str]:
    """uv sync — как в установщике (на Apple Silicon с macOS 14+ — с ускорением MLX, на Windows с видеокартой
    NVIDIA — с её библиотекой cuBLAS)."""
    uv = find_uv()
    if not uv:
        raise UpdateError("не найден uv — запустите установку (INSTALL) заново")
    # в папке из git (у разработчика) оставляем и инструменты разработки: тесты, проверку кода
    cmd = [uv, "sync"] if (root / ".git").exists() else [uv, "sync", "--no-dev"]
    mac = platform.mac_ver()[0]
    if sys.platform == "darwin" and platform.machine() == "arm64" and mac and int(mac.split(".")[0]) >= 14:
        cmd += ["--extra", "mlx"]
    if wants_cuda(root):
        cmd += CUDA_EXTRA
    return cmd


def sync_libraries(root: Path) -> None:
    """Библиотеки под новую версию. Библиотека видеокарты NVIDIA не скачалась — обновляемся без неё
    (расшифровка пойдёт на процессоре), попробуем при следующем обновлении."""
    cmd = _sync_command(root)
    res = subprocess.run(cmd, cwd=str(root), capture_output=True, text=True, timeout=3600 if cmd != _without_cuda(cmd) else 1800)
    if res.returncode and cmd != _without_cuda(cmd):
        store.log("update", "библиотека видеокарты NVIDIA не скачалась — расшифровка пойдёт на процессоре: "
                            + (res.stderr or res.stdout).strip()[-300:], "warning")
        res = subprocess.run(_without_cuda(cmd), cwd=str(root), capture_output=True, text=True, timeout=1800)
    if res.returncode:
        raise UpdateError("библиотеки не обновились: " + (res.stderr or res.stdout).strip()[-400:])


def libraries_busy() -> bool:
    """Windows: пока PULSE работает, файлы его библиотек открыты, и uv sync не сможет их заменить — библиотеки
    обновит restart, когда PULSE закроется."""
    from opsdash import daemon

    return os.name == "nt" and daemon.is_running()


# Windows, перезапуск после обновления: дождаться, пока старый PULSE закроется (освободит data/pult.lock), обновить
# библиотеки и запустить новый. Только стандартная библиотека Python: импорт opsdash открыл бы файлы тех самых
# библиотек (PyYAML, pydantic), которые uv sync должен заменить. Аргументы: файл блокировки, журнал, папка, uv sync…
# Библиотека видеокарты NVIDIA (--extra cuda) не скачалась — ещё раз без неё: PULSE обновится, расшифровка — на процессоре.
_WINDOWS_RESTART = """
import msvcrt, subprocess, sys, time
lock, log, root, cmd = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4:]
for _ in range(180):
    try:
        with open(lock, "a+") as f:
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        break
    except OSError:
        time.sleep(1)
with open(log, "a", encoding="utf-8") as out:
    out.write(time.strftime("%Y-%m-%d %H:%M:%S") + " uv sync\\n")
    out.flush()
    done = subprocess.run(cmd, cwd=root, stdout=out, stderr=out, creationflags=0x08000000)
    if done.returncode and "cuda" in cmd:
        i = cmd.index("cuda")
        out.write(time.strftime("%Y-%m-%d %H:%M:%S") + " uv sync без библиотеки видеокарты\\n")
        out.flush()
        subprocess.run(cmd[:i - 1] + cmd[i + 1:], cwd=root, stdout=out, stderr=out, creationflags=0x08000000)
from opsdash import cli
cli.start_background()
"""


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    # quotePath=false — русские имена файлов в выводе git как есть, а не «\320\241…». Читаем вывод как UTF-8 (так
    # пишет git): иначе Windows прочтёт его в кодировке системы (cp1251) и исказит имена. LC_ALL=C — сообщения
    # по-английски: по ним _pull понимает, что мешает, а Git для Windows на русской системе отвечает по-русски
    return subprocess.run(["git", "-c", "core.quotePath=false", "-C", str(root), *args], capture_output=True,
                          encoding="utf-8", errors="replace", env={**os.environ, "LC_ALL": "C"})


def _listed(out: str, marker: str) -> list[str]:
    """Файлы, которые git перечислил после строки с marker (с отступом, по одному в строке)."""
    lines, found, files = out.splitlines(), False, []
    for line in lines:
        if marker in line:
            found = True
        elif found and line[:1] in ("\t", " ") and line.strip():
            files.append(line.strip())
        elif found:
            break
    return files


def _set_aside(root: Path, files: list[str]) -> list[str]:
    """Файлы, которых нет в git, но с теми же именами приходят с обновлением, — в data/backup, не удаляем.
    Возвращает, что удалось отложить."""
    dest = config.DATA_DIR / "backup" / f"update-{datetime.now():%Y%m%d-%H%M%S}"
    moved = []
    for rel in files:
        src = root / rel
        if src.exists():
            (dest / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dest / rel))
            moved.append(rel)
    if moved:
        store.log("update", f"отложены файлы, которые мешали обновлению: {', '.join(moved)} → {dest}", "warning")
    return moved


def _pull(root: Path, branch: str) -> None:
    """git pull --ff-only. Что мешает — разбираем сами, где это безопасно: файлы с теми же именами, которых нет
    в git, откладываем в data/backup; uv.lock (его пересобирает uv) возвращаем как в git. О своих правках
    в коде и о разошедшейся ветке — говорим по-человечески, ничего не трогая. Файл, который не вышло отложить,
    называем в ошибке сами — а не uv.lock, который мешал рядом с ним."""
    for attempt in range(3):
        res = _git(root, "pull", "--ff-only", "origin", branch)
        if not res.returncode:
            return
        out = f"{res.stderr or ''}\n{res.stdout or ''}"
        last = attempt == 2
        blocked = _listed(out, "untracked working tree files would be overwritten")
        if blocked and not last and _set_aside(root, blocked):
            continue
        changed = _listed(out, "local changes to the following files would be overwritten")
        if changed and set(changed) <= {"uv.lock"} and not last:
            _git(root, "checkout", "--", "uv.lock")
            continue
        raise UpdateError(_explain(out, blocked, changed, branch))


def _explain(out: str, blocked: list[str], changed: list[str], branch: str) -> str:
    if changed:
        return ("в папке PULSE изменены файлы, которые меняет обновление: " + ", ".join(changed) + ". Сохраните правки "
                "(git stash) или верните как было (git checkout -- <файл>) и нажмите «Обновить» ещё раз")
    if blocked:
        return "мешают файлы в папке PULSE: " + ", ".join(blocked) + ". Переименуйте их и нажмите «Обновить» ещё раз"
    if "fast-forward" in out or "diverg" in out:
        return (f"в папке PULSE есть свои коммиты, которых нет в ветке {branch}, — новую версию нельзя просто "
                "подтянуть. Отправьте их на GitHub или переключите папку на эту ветку")
    useful = [ln.strip() for ln in out.splitlines() if ln.strip().startswith(("error:", "fatal:"))]
    return "git pull не удался: " + (" ".join(useful) or out.strip()[-300:])


def apply(settings: dict | None = None, root: Path | None = None, transport: httpx.BaseTransport | None = None) -> str:
    """Поставить последнюю версию из ветки. Перезапуск — отдельно (restart)."""
    real = root is None
    root = root or config.APP_DIR
    opts = _options(settings)
    before = installed(root)
    if (root / ".git").exists():
        _pull(root, opts["branch"])
    else:
        with _client(transport) as client:
            head = _head(client, opts)
            data = _get(client, f'/repos/{opts["repo"]}/zipball/{head["sha"]}').content
        files = unpack(data, root)
        (config.DATA_DIR / INSTALLED_FILE).write_text(json.dumps(
            {"sha": head["sha"], "date": head["date"], "installed_at": store.utcnow(), "files": files},
            ensure_ascii=False, indent=1), encoding="utf-8")
    later = real and libraries_busy()
    if not later:
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
    return f'версия {before["sha"][:7] or "?"} → {after["sha"][:7]}' + (" (библиотеки — при перезапуске)" if later else "")


_stale: dict = {"at": 0.0, "value": None}


def files_version(root: Path | None = None) -> str:
    """Номер версии в файлах PULSE (opsdash/__init__.py) — может быть новее запущенного кода."""
    try:
        text = ((root or config.APP_DIR) / "opsdash" / "__init__.py").read_text(encoding="utf-8")
    except OSError:
        return ""
    found = re.search(r'__version__ = "([^"]+)"', text)
    return found.group(1) if found else ""


def stale(running_sha: str, max_age: float = 60) -> dict | None:
    """Файлы PULSE новее запущенного кода: их обновили мимо кнопки «Обновить» (git pull, слияние в приложении Claude),
    а PULSE не перезапускался. Тогда {"sha", "version"} новой версии, иначе None. Git спрашиваем не чаще раза в минуту."""
    import time

    now = time.monotonic()
    if now - _stale["at"] > max_age:
        cur = installed()
        newer = bool(cur["sha"]) and bool(running_sha) and not cur["sha"].startswith(running_sha)
        _stale.update(at=now, value={"sha": cur["sha"], "version": files_version()} if newer else None)
    return _stale["value"]


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
    cmd = [sys.executable, "-c", "import time; time.sleep(5); from opsdash import cli; cli.start_background()"]
    if os.name == "nt":
        kwargs["creationflags"] = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        try:  # библиотеки — когда старый PULSE закроется (libraries_busy); без новых версий uv sync проходит за секунду
            cmd = [sys.executable, "-c", _WINDOWS_RESTART, str(config.DATA_DIR / "pult.lock"),
                   str(config.DATA_DIR / "logs" / "update-libs.log"), str(config.APP_DIR), *_sync_command(config.APP_DIR)]
        except UpdateError:
            pass
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(cmd, **kwargs)
    if in_daemon:
        import threading

        threading.Timer(1.0, daemon.shutdown).start()
    else:
        try:
            httpx.post(daemon.url() + "/api/shutdown", timeout=10, headers={"Origin": daemon.url()})
        except httpx.HTTPError:
            pass
