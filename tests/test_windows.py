"""Windows: Claude Code и Git Bash, вход в Claude, обновление библиотек после перезапуска.
Тесты с пометкой «только Windows» идут в GitHub Actions (.github/workflows/windows.yml)."""

import os
import subprocess
import sys

import pytest

from opsdash import brain, config, updates
from tests.test_updates import github, install, zipball

windows_only = pytest.mark.skipif(os.name != "nt", reason="только Windows")


def test_npm_claude_cmd_gets_context_through_the_task():
    cmd = ["C:/npm/claude.cmd", "-p", "--append-system-prompt", "СВОДКА", "--allowedTools", "Read"]
    fitted, task = brain._fit_command(cmd, "Разбери встречу")
    assert fitted == ["C:/npm/claude.cmd", "-p", "--allowedTools", "Read"]  # cmd.exe: не больше 8191 символа
    assert task.startswith("СВОДКА") and task.endswith("Разбери встречу")
    native = ["C:/bin/claude.exe", "-p", "--append-system-prompt", "СВОДКА"]
    assert brain._fit_command(native, "x") == (native, "x")


def test_native_claude_preferred_over_npm_wrapper(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setattr(brain.shutil, "which", lambda name: str(tmp_path / "npm" / "claude.cmd"))
    fallback = brain.find_claude()  # другого нет — годится и обёртка (на Mac разработчика может стоять свой claude)
    assert fallback.endswith("claude.cmd") or fallback.startswith(("/opt/homebrew/", "/usr/local/"))
    exe = tmp_path / ".local" / "bin" / "claude.exe"
    exe.parent.mkdir(parents=True)
    exe.write_text("")
    assert brain.find_claude() == str(exe)


def test_windows_settings_offer_login_button_and_help_has_one_line_install(monkeypatch):
    from fastapi.testclient import TestClient

    from opsdash.web.app import app, templates

    monkeypatch.setitem(templates.env.globals, "is_mac", False)
    monkeypatch.setitem(templates.env.globals, "is_windows", True)
    monkeypatch.setattr(brain, "find_claude", lambda *a, **k: r"C:\Users\Анна\.local\bin\claude.exe")
    monkeypatch.setattr(brain, "login_status", lambda *a, **k: {"loggedIn": False})
    opened = []
    monkeypatch.setattr(brain, "open_login", lambda *a, **k: opened.append(1))
    client = TestClient(app, base_url="http://localhost:8765")
    page = client.get("/settings?tab=claude").text
    assert "Войти в Claude" in page and "окно PowerShell" in page and "bash.exe" in page
    client.post("/settings/claude/login", follow_redirects=False)
    assert opened == [1]
    help_page = client.get("/help/01-установка").text
    assert "scripts/get-windows.ps1 | iex" in help_page and "Войти в Claude" in help_page
    assert r"<code>C:\Users\&lt;вы&gt;\Pult</code>" in help_page and "&amp;lt;" not in help_page  # без двойного экранирования


def test_apply_on_windows_leaves_libraries_for_restart(tmp_path, settings_with, monkeypatch):
    """Работающий PULSE держит файлы библиотек открытыми — uv sync, когда он закроется (restart)."""
    from opsdash import macos

    settings_with(updates={"repo": "owner/repo", "branch": "main", "auto_check": True})
    monkeypatch.setattr(config, "APP_DIR", install(tmp_path))
    monkeypatch.setattr(macos, "installed_apps", list)  # не трогать настоящее приложение PULSE на этом Mac
    monkeypatch.setattr(updates, "libraries_busy", lambda: True)
    synced = []
    monkeypatch.setattr(updates, "sync_libraries", lambda root: synced.append(root))
    result = updates.apply(transport=github(zipball({"opsdash/new.py": (b"x", 0o644)})))
    assert result.endswith("(библиотеки — при перезапуске)") and synced == []
    assert (tmp_path / "Pult" / "opsdash" / "new.py").exists()
    compile(updates._WINDOWS_RESTART, "restart", "exec")


@windows_only
def test_windows_restart_waits_for_pulse_then_syncs_and_starts(tmp_path):
    """Помощник перезапуска: ждёт, пока освободится блокировка PULSE, обновляет библиотеки и запускает PULSE.
    Вместо opsdash — подставной пакет в рабочей папке: настоящий PULSE в тесте не запускаем."""
    (tmp_path / "opsdash").mkdir()
    (tmp_path / "opsdash" / "__init__.py").write_text("")
    (tmp_path / "opsdash" / "cli.py").write_text("def start_background():\n    open('started', 'w').close()\n")
    lock, log = tmp_path / "pult.lock", tmp_path / "update-libs.log"
    lock.write_text("")
    sync = [sys.executable, "-c", "open('synced', 'w').close()"]
    subprocess.run([sys.executable, "-c", updates._WINDOWS_RESTART, str(lock), str(log), str(tmp_path), *sync],
                   cwd=str(tmp_path), timeout=60, check=True)
    assert (tmp_path / "synced").exists() and (tmp_path / "started").exists() and "uv sync" in log.read_text()


@windows_only
def test_windows_restart_runs_helper_with_uv_sync(monkeypatch):
    started = []
    monkeypatch.setattr(updates.subprocess, "Popen", lambda cmd, **kw: started.append(cmd))
    monkeypatch.setattr(updates, "find_uv", lambda: r"C:\uv\uv.exe")
    updates.restart(in_daemon=False)
    assert started[0][2] == updates._WINDOWS_RESTART and started[0][3].endswith("pult.lock")
    assert started[0][6:8] == [r"C:\uv\uv.exe", "sync"]


@windows_only
def test_windows_claude_from_desktop_app(tmp_path, monkeypatch):
    monkeypatch.setattr(brain.shutil, "which", lambda name: None)
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    for version in ("2.1.99", "2.1.308"):
        exe = tmp_path / "Roaming" / "Claude" / "claude-code" / version / "a93ded4b1b47" / "claude.exe"
        exe.parent.mkdir(parents=True)
        exe.write_text("")
    assert "\\2.1.308\\" in brain.find_claude()  # новейшая версия, а не «99» > «308» по алфавиту


@windows_only
def test_windows_git_bash_found_for_claude(tmp_path, monkeypatch, settings_with):
    monkeypatch.delenv("CLAUDE_CODE_GIT_BASH_PATH", raising=False)
    monkeypatch.setattr(brain.shutil, "which", lambda name: None)
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "Program Files"))
    monkeypatch.delenv("ProgramW6432", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    settings = settings_with(claude={"git_bash_path": ""})
    assert "CLAUDE_CODE_GIT_BASH_PATH" not in brain._env(settings)
    bash = tmp_path / "Program Files" / "Git" / "bin" / "bash.exe"
    bash.parent.mkdir(parents=True)
    bash.write_text("")
    assert brain._env(settings)["CLAUDE_CODE_GIT_BASH_PATH"] == str(bash)


@windows_only
def test_windows_login_opens_powershell(tmp_path, monkeypatch, settings_with):
    exe = tmp_path / "Claude Code" / "claude.exe"
    exe.parent.mkdir()
    exe.write_text("")
    settings = settings_with(claude={"path": str(exe)})
    opened = []
    monkeypatch.setattr(brain.subprocess, "Popen", lambda cmd, **kw: opened.append((cmd, kw)))
    brain.open_login(settings)
    script = config.DATA_DIR / "claude-login.ps1"
    assert opened[0][0][0] == "powershell" and opened[0][0][-1] == str(script)
    assert opened[0][1]["creationflags"] == 0x00000010  # своё окно: у PULSE консоли нет
    assert script.read_bytes().startswith(b"\xef\xbb\xbf")  # Windows PowerShell читает UTF-8 только с BOM
    assert f"& '{exe}' auth login --claudeai" in script.read_text(encoding="utf-8-sig")
