"""macOS: автозапуск (launchd), приложение «PULSE» и права доступа к папкам.

Вызывается установщиком (scripts/install-mac.sh) командой `./ops mac install`.
"""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
import time
from pathlib import Path

from opsdash import config

LABEL = "local.pult.director"
APP_NAME = "PULSE.app"
OLD_APP_NAMES = ("Пульт директора.app",)  # до переименования в PULSE — убираем при установке и удалении
PRIVACY_PANE = "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles"
# папки, доступ к которым macOS спрашивает отдельно (TCC)
PROTECTED = ("Documents", "Desktop", "Downloads", "Library/Mobile Documents")


class MacError(Exception):
    pass


def is_mac() -> bool:
    return sys.platform == "darwin"


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


SYSTEM_APPS = Path("/Applications")  # общая «Программы»: администратор Mac пишет туда без пароля
MARKER = "pulse-app"  # файл в Contents/Resources: это приложение создал PULSE, его можно заменить или удалить


def personal_apps() -> Path:
    return Path.home() / "Applications"


def is_ours(app: Path) -> bool:
    """Наше ли это приложение. В «Программах» может лежать чужое «Pulse.app» (имена в macOS без учёта регистра)."""
    if (app / "Contents" / "Resources" / MARKER).exists() or app.name in OLD_APP_NAMES:
        return True
    return app.parent == personal_apps() and app.name == APP_NAME  # создано до метки — тогда только в личной папке


def app_path() -> Path:
    """Куда ставить приложение: в общую «Программы» (там его видно в Finder и списке программ), если macOS даёт туда
    писать и место не занято чужим приложением; иначе — в личную ~/Applications."""
    system = SYSTEM_APPS / APP_NAME
    if os.access(SYSTEM_APPS, os.W_OK) and (not system.exists() or is_ours(system)):
        return system
    return personal_apps() / APP_NAME


def installed_apps() -> list[Path]:
    """Все наши копии приложения: обе папки программ, текущее и прежнее название."""
    found = [folder / name for folder in (SYSTEM_APPS, personal_apps()) for name in (APP_NAME, *OLD_APP_NAMES)]
    return [p for p in found if p.exists() and is_ours(p)]


def remove_old_apps(keep: Path | None = None) -> None:
    """Убрать прежние копии (старое название, другая папка программ), кроме keep."""
    for app in installed_apps():
        if app != keep:
            shutil.rmtree(app, ignore_errors=True)


def venv_python(root: Path | None = None) -> Path:
    root = root or config.APP_DIR
    return root / ".venv" / "bin" / "python"


def agent_dict(root: Path | None = None) -> dict:
    """Описание фоновой службы: запуск при входе, перезапуск после сбоя (но не после «Стоп»)."""
    root = root or config.APP_DIR
    home = Path.home()
    path = ":".join([str(home / ".local" / "bin"), "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin",
                     "/usr/sbin", "/sbin"])
    logs = config.LOGS_DIR
    return {
        "Label": LABEL,
        "ProgramArguments": [str(venv_python(root)), "-m", "opsdash", "daemon"],
        "WorkingDirectory": str(root),
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 30,
        "ProcessType": "Interactive",
        "EnvironmentVariables": {"PATH": path, "LANG": "ru_RU.UTF-8", "PYTHONUTF8": "1",
                                 "PYTHONIOENCODING": "utf-8"},
        "StandardOutPath": str(logs / "launchd.out.log"),
        "StandardErrorPath": str(logs / "launchd.err.log"),
    }


def _launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def domain() -> str:
    return f"gui/{os.getuid()}"


def agent_installed() -> bool:
    return plist_path().exists()


def install_agent() -> str:
    config.LOGS_DIR.mkdir(parents=True, exist_ok=True)
    path = plist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    _launchctl("bootout", f"{domain()}/{LABEL}")  # старую версию — выгрузить (ошибка, если её не было, — не страшно)
    # bootout выгружает службу не сразу: bootstrap до этого падает с «Input/output error», и после
    # обновления PULSE оставался выключенным
    for _ in range(50):
        if not loaded():
            break
        time.sleep(0.2)
    with open(path, "wb") as fh:
        plistlib.dump(agent_dict(), fh)
    for _ in range(5):
        res = _launchctl("bootstrap", domain(), str(path))
        if res.returncode == 0 or loaded():
            break
        time.sleep(1)
    else:  # старые macOS
        res = _launchctl("load", "-w", str(path))
    if not loaded():
        raise MacError(f"launchctl не принял службу: {res.stderr.strip() or res.stdout.strip()}")
    return str(path)


def loaded() -> bool:
    """Служба загружена в launchd (работает или ждёт перезапуска)."""
    return _launchctl("print", f"{domain()}/{LABEL}").returncode == 0


def uninstall_agent() -> None:
    _launchctl("bootout", f"{domain()}/{LABEL}")
    plist_path().unlink(missing_ok=True)


def kickstart() -> bool:
    """Запустить службу (если она установлена). True — если launchd принял команду."""
    if not agent_installed():
        return False
    res = _launchctl("kickstart", f"{domain()}/{LABEL}")
    if res.returncode != 0:  # служба выгружена — загрузить заново
        res = _launchctl("bootstrap", domain(), str(plist_path()))
    return res.returncode == 0


def make_app(root: Path | None = None) -> str:
    """Приложение «PULSE» в папке программ (см. app_path): открывает PULSE и запускает, если он не работает."""
    root = root or config.APP_DIR
    if not shutil.which("osacompile"):
        raise MacError("osacompile не найден")
    target = app_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    remove_old_apps(keep=target)
    if target.exists():
        shutil.rmtree(target)
    ops = root / "ops"
    script = f'do shell script quoted form of "{ops}" & " open > /dev/null 2>&1 &"'
    res = subprocess.run(["osacompile", "-o", str(target), "-e", script], capture_output=True, text=True)
    if res.returncode != 0:
        raise MacError(f"не удалось создать приложение: {res.stderr.strip()}")
    resources = target / "Contents" / "Resources"
    icon = root / "scripts" / "pult.icns"
    if icon.exists():
        shutil.copy(icon, resources / "applet.icns")
    (resources / MARKER).write_text("PULSE\n", encoding="utf-8")
    # иконку и метку добавили после сборки — переподписываем, иначе подпись приложения «сломана»
    subprocess.run(["codesign", "--force", "--sign", "-", str(target)], capture_output=True, check=False)
    subprocess.run(["touch", str(target)], check=False)
    return str(target)


def real_python() -> str:
    """Настоящий файл Python (не ссылка из .venv) — его добавляют в «Полный доступ к диску»."""
    return os.path.realpath(venv_python())


def protected_paths(settings: dict | None = None) -> list[str]:
    """Папки из настроек, к которым macOS даёт доступ только с разрешения пользователя."""
    settings = settings or config.load_settings()
    home = Path.home()
    paths = [*(settings["recordings"].get("folders") or []), *(settings["knowledge"].get("folders") or []),
             str(config.ROOT)]
    out = []
    for p in paths:
        full = config.resolve_path(p)
        for sub in PROTECTED:
            base = home / sub
            if full == base or base in full.parents:
                out.append(str(full))
                break
    return sorted(set(out))


def open_privacy_settings() -> None:
    subprocess.run(["open", PRIVACY_PANE], check=False)
