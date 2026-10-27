"""macOS: служба автозапуска, права доступа, движок MLX (проверяется без настоящего Mac)."""

import plistlib
import shutil
import sys
import types
from pathlib import Path

import numpy as np
import pytest

from opsdash import config, macos, transcribe


def test_launch_agent_plist(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    data = macos.agent_dict(Path("/Users/anna/Pult"))
    raw = plistlib.dumps(data)
    back = plistlib.loads(raw)
    assert back["Label"] == macos.LABEL
    assert back["ProgramArguments"] == ["/Users/anna/Pult/.venv/bin/python", "-m", "opsdash", "daemon"]
    assert back["RunAtLoad"] is True and back["KeepAlive"] == {"SuccessfulExit": False}
    assert back["WorkingDirectory"] == "/Users/anna/Pult"
    assert str(tmp_path / ".local" / "bin") in back["EnvironmentVariables"]["PATH"]
    assert macos.plist_path() == tmp_path / "Library" / "LaunchAgents" / "local.pult.director.plist"


def test_protected_paths(monkeypatch, tmp_path, settings_with):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    settings_with(recordings={"folders": [str(tmp_path / "Documents" / "Zoom"), "data/recordings"]},
                  knowledge={"folders": [str(tmp_path / "Yandex.Disk.localized" / "Работа"),
                                         str(tmp_path / "Library" / "Mobile Documents" / "Отчёты")]})
    paths = macos.protected_paths()
    assert str(tmp_path / "Documents" / "Zoom") in paths
    assert str(tmp_path / "Library" / "Mobile Documents" / "Отчёты") in paths
    assert not any("Yandex.Disk" in p for p in paths)


def test_mac_cli_refuses_elsewhere(capsys):
    if sys.platform == "darwin":
        pytest.skip("на Mac команда работает по-настоящему")
    from opsdash import cli

    with pytest.raises(SystemExit):
        cli.main(["mac", "status"])
    assert "только для macOS" in capsys.readouterr().err


@pytest.fixture
def fake_mlx(monkeypatch):
    calls = {}
    mod = types.ModuleType("mlx_whisper")

    def transcribe_fn(audio, **kw):
        calls.update(kw)
        if calls.get("_fail"):
            raise RuntimeError("Metal недоступен")
        return {"segments": [{"start": 0.0, "end": 2.0, "text": " Привет всем.",
                              "words": [{"word": " Привет", "start": 0.0, "end": 0.6},
                                        {"word": " всем.", "start": 0.7, "end": 1.2}]}]}

    mod.transcribe = transcribe_fn
    monkeypatch.setitem(sys.modules, "mlx_whisper", mod)
    monkeypatch.setattr(transcribe, "is_apple_silicon", lambda: True)
    monkeypatch.setattr(transcribe, "_mlx_failed", False)
    return calls


def test_engine_choice(fake_mlx, monkeypatch):
    s = config.load_settings()
    assert transcribe.engine(s) == "mlx"
    s["recordings"]["engine"] = "faster-whisper"
    assert transcribe.engine(s) == "faster-whisper"
    monkeypatch.setattr(transcribe, "is_apple_silicon", lambda: False)
    s["recordings"]["engine"] = "auto"
    assert transcribe.engine(s) == "faster-whisper", "Windows и Intel-маки — всегда faster-whisper"


def test_mlx_words_and_model_mapping(fake_mlx):
    s = config.load_settings()
    segments, words = transcribe.whisper_words(np.zeros(16000 * 3, dtype=np.float32), s)
    assert segments == [{"start": 0.0, "end": 2.0, "text": "Привет всем."}]
    assert [w["word"] for w in words] == [" Привет", " всем."]
    assert fake_mlx["path_or_hf_repo"] == "mlx-community/whisper-large-v3-turbo"
    assert fake_mlx["word_timestamps"] is True and fake_mlx["language"] == "ru"


def test_mlx_failure_falls_back_to_faster_whisper(fake_mlx, monkeypatch):
    fake_mlx["_fail"] = True
    seg = types.SimpleNamespace(start=0.0, end=1.0, text=" ок", words=[types.SimpleNamespace(start=0.0, end=0.5, word=" ок")])

    class FakeModel:
        def transcribe(self, audio, **kw):
            return iter([seg]), None

    monkeypatch.setattr(transcribe, "get_model", lambda settings: FakeModel())
    segments, words = transcribe.whisper_words(np.zeros(16000, dtype=np.float32), config.load_settings())
    assert segments[0]["text"] == "ок" and words[0]["word"] == " ок"
    assert transcribe._mlx_failed is True
    assert transcribe.engine(config.load_settings()) == "faster-whisper", "после сбоя MLX больше не пробуем"


def test_install_agent_and_app_commands(monkeypatch, tmp_path):
    import subprocess

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(macos.os, "getuid", lambda: 501, raising=False)
    calls = []
    # служба уже работает; bootout выгружает её не сразу, а bootstrap до выгрузки отвечает ошибкой 5
    state = {"loaded": True, "unloading": 0}

    def fake_run(cmd, **kw):
        calls.append(cmd)
        if cmd[0] == "osacompile":
            (Path(cmd[2]) / "Contents" / "Resources").mkdir(parents=True)
        rc = 0
        if cmd[:2] == ["launchctl", "bootout"]:
            state["unloading"] = 3
        elif cmd[:2] == ["launchctl", "print"]:
            if state["unloading"]:
                state["unloading"] -= 1
                state["loaded"] = state["unloading"] > 0
            rc = 0 if state["loaded"] else 113
        elif cmd[:2] == ["launchctl", "bootstrap"]:
            rc = 5 if state["loaded"] else 0
            state["loaded"] = True
        return subprocess.CompletedProcess(cmd, rc, "", "")

    monkeypatch.setattr(macos.subprocess, "run", fake_run)
    monkeypatch.setattr(macos.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(macos.time, "sleep", lambda s: None)
    plist = macos.install_agent()
    assert Path(plist).exists() and plistlib.loads(Path(plist).read_bytes())["Label"] == macos.LABEL
    boots = [c for c in calls if c[:2] == ["launchctl", "bootstrap"]]
    assert boots == [["launchctl", "bootstrap", "gui/501", plist]] and state["loaded"]  # дождались выгрузки старой

    state.update(loaded=False, unloading=0)  # launchd так и не принял службу — это ошибка, а не «готово»
    monkeypatch.setattr(macos.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(
        cmd, 0 if cmd[:2] == ["launchctl", "load"] else 5, "", "Bootstrap failed: 5"))
    with pytest.raises(macos.MacError, match="не принял"):
        macos.install_agent()
    monkeypatch.setattr(macos.subprocess, "run", fake_run)

    # приложение — в общую «Программы» (здесь — временная папка), прежние копии из личной папки убираются
    system = tmp_path / "SystemApplications"
    system.mkdir()
    monkeypatch.setattr(macos, "SYSTEM_APPS", system)
    for old in ("PULSE.app", "Пульт директора.app"):  # созданы прежними версиями — без метки
        (tmp_path / "Applications" / old / "Contents").mkdir(parents=True)
    app = Path(macos.make_app(config.APP_DIR))
    script = next(c for c in calls if c[0] == "osacompile")[-1]
    assert script == f'do shell script quoted form of "{config.APP_DIR}/ops" & " open > /dev/null 2>&1 &"'
    assert app == system / "PULSE.app" and macos.is_ours(app)
    assert (app / "Contents" / "Resources" / "applet.icns").read_bytes()[:4] == b"icns"
    assert ["codesign", "--force", "--sign", "-", str(app)] in calls  # после иконки — подпись заново
    assert macos.installed_apps() == [app]

    # в «Программах» чужое «Pulse.app» — его не трогаем, ставим в личную папку
    shutil.rmtree(app)
    (system / "Pulse.app" / "Contents").mkdir(parents=True)
    monkeypatch.setattr(macos, "APP_NAME", "Pulse.app")  # та же папка, что «PULSE.app» на macOS
    app = Path(macos.make_app(config.APP_DIR))
    assert app == tmp_path / "Applications" / "Pulse.app" and (system / "Pulse.app").exists()
    monkeypatch.setattr(macos, "APP_NAME", "PULSE.app")

    # в общую папку писать нельзя — тоже в личную
    monkeypatch.setattr(macos.os, "access", lambda path, mode: False)
    assert macos.app_path() == tmp_path / "Applications" / "PULSE.app"
