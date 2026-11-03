import io
import json
import os
import zipfile
from urllib.parse import unquote

import httpx
import pytest
from fastapi.testclient import TestClient

from opsdash import config, jobs, store, updates

OLD, NEW = "a" * 40, "b" * 40


def zipball(files: dict[str, tuple[bytes, int]], top: str = f"owner-repo-{NEW[:7]}") -> bytes:
    """Архив как у GitHub: всё в одной папке «владелец-репозиторий-коммит», с правами файлов."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, (body, mode) in files.items():
            info = zipfile.ZipInfo(f"{top}/{name}")
            info.external_attr = (0o100000 | mode) << 16
            z.writestr(info, body)
    return buf.getvalue()


def github(archive: bytes = b"", status: str = "ahead"):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/repos/owner/repo/commits/main":
            return httpx.Response(200, json={"sha": NEW, "commit": {"committer": {"date": "2026-10-27T10:00:00Z"},
                                                                     "message": "Обновления по кнопке\n\nподробности"}})
        if path == f"/repos/owner/repo/compare/{OLD}...{NEW}":
            return httpx.Response(200, json={"status": status, "commits": [
                {"commit": {"message": "Доска задач: узкий экран"}}, {"commit": {"message": "Обновления по кнопке"}}]})
        if path == f"/repos/owner/repo/zipball/{NEW}":
            return httpx.Response(200, content=archive)
        return httpx.Response(404)

    return httpx.MockTransport(handler)


@pytest.fixture
def repo(settings_with):
    return settings_with(updates={"repo": "owner/repo", "branch": "main", "auto_check": True})


def install(tmp_path, sha: str = OLD):
    """Папка PULSE, поставленная из архива: коммит подставлен в _build.py."""
    root = tmp_path / "Pult"
    (root / "opsdash").mkdir(parents=True)
    (root / "opsdash" / "_build.py").write_text(f'COMMIT = "{sha}"\nDATE = "2026-10-26T10:00:00Z"\n', encoding="utf-8")
    return root


def test_check_finds_new_version_and_changes(tmp_path, repo, monkeypatch):
    monkeypatch.setattr(config, "APP_DIR", install(tmp_path))
    res = updates.check(transport=github())
    assert res["installed"]["sha"] == OLD and res["installed"]["how"] == "archive"
    assert res["available"] and res["latest"]["message"] == "Обновления по кнопке"
    assert res["changes"] == ["Обновления по кнопке", "Доска задач: узкий экран"]  # новые сверху
    assert updates.last_check()["available"]

    # в папке из git свои, ещё не отправленные изменения: GitHub «отстаёт» — обновлять нечего
    assert not updates.check(transport=github(status="behind"))["available"]


def test_check_without_access_explains(tmp_path, repo, monkeypatch):
    monkeypatch.setattr(config, "APP_DIR", install(tmp_path))
    closed = httpx.MockTransport(lambda request: httpx.Response(404))
    with pytest.raises(updates.UpdateError, match="укажите токен"):
        updates.check(transport=closed)
    assert "укажите токен" in updates.last_check()["error"]


def test_apply_unpacks_archive_keeps_data_and_removes_dropped_files(tmp_path, repo, monkeypatch):
    root = install(tmp_path)
    for rel, body in (("data/ops.db", "база"), ("secrets/.env", "TOKEN=1"), ("config/settings.yaml", "owner: Анна"),
                      ("opsdash/old.py", "старое"), ("opsdash/kept.py", "старая версия")):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(body, encoding="utf-8")
    (config.DATA_DIR / updates.INSTALLED_FILE).write_text(json.dumps({"sha": OLD, "files": ["opsdash/old.py", "opsdash/kept.py"]}))
    synced = []
    monkeypatch.setattr(updates, "sync_libraries", lambda r: synced.append(r))
    archive = zipball({"opsdash/kept.py": (b"new", 0o644), "opsdash/new.py": (b"print(1)", 0o644), "ops": (b"#!/bin/bash", 0o755),
                       "config/settings.example.yaml": (b"example", 0o644), "config/settings.yaml": (b"PUBLIC", 0o644),
                       "data/README.md": (b"readme", 0o644)})
    assert updates.apply(root=root, transport=github(archive)) == f"версия {OLD[:7]} → {NEW[:7]}"
    assert (root / "opsdash" / "kept.py").read_text() == "new" and (root / "opsdash" / "new.py").exists()
    assert not (root / "opsdash" / "old.py").exists()  # из новой версии убрали
    assert os.access(root / "ops", os.X_OK) and (root / "config" / "settings.example.yaml").exists()
    assert (root / "config" / "settings.yaml").read_text(encoding="utf-8") == "owner: Анна"  # настройки не трогаем
    assert (root / "data" / "ops.db").read_text(encoding="utf-8") == "база" and not (root / "data" / "README.md").exists()
    assert (root / "secrets" / ".env").read_text() == "TOKEN=1" and synced == [root]
    assert updates.installed(root)["sha"] == NEW and not updates.last_check().get("available")


def test_apply_refuses_unsafe_archive_before_writing(tmp_path, repo, monkeypatch):
    root = install(tmp_path)
    monkeypatch.setattr(updates, "sync_libraries", lambda r: None)
    bad = zipball({"opsdash/a.py": (b"x", 0o644), "../../evil.sh": (b"rm -rf", 0o755)})
    with pytest.raises(updates.UpdateError, match="подозрительный путь"):
        updates.apply(root=root, transport=github(bad))
    assert not (root / "opsdash" / "a.py").exists() and not (tmp_path / "evil.sh").exists()
    with pytest.raises(updates.UpdateError, match="нет PULSE"):  # чужой архив
        updates.apply(root=root, transport=github(zipball({"README.md": (b"x", 0o644)})))


def test_scheduler_checks_daily_and_settings_page(tmp_path, repo, monkeypatch):
    from opsdash.web.app import app

    submitted = []
    monkeypatch.setattr(jobs, "submit", lambda name, kind="light", args=None: submitted.append(name) or True)
    jobs.tick()
    assert "update-check" in submitted
    store.execute("INSERT INTO jobs(name, last_run) VALUES('update-check', ?)", (store.utcnow(),))
    submitted.clear()
    jobs.tick()
    assert "update-check" not in submitted  # следующая проверка — через сутки

    monkeypatch.setattr(config, "APP_DIR", install(tmp_path))
    updates.check(transport=github())
    client = TestClient(app, base_url="http://localhost:8765")
    page = client.get("/settings?tab=updates").text
    assert "Обновления PULSE" in page and f"Доступна новая версия {NEW[:7]}" in page and "Доска задач: узкий экран" in page
    assert "● Доступно обновление" in client.get("/").text  # в меню на любой странице

    r = client.post("/settings/updates", data={"repo": "https://github.com/me/pulse.git", "branch": "dev", "token": "github_pat_x"},
                    follow_redirects=False)
    assert "msg=" in r.headers["location"] and config.get_secret("GITHUB_TOKEN") == "github_pat_x"
    assert config.load_settings()["updates"] == {"repo": "me/pulse", "branch": "dev", "auto_check": False}
    r = client.post("/updates/apply", follow_redirects=False)
    assert submitted[-1] == "update" and "updating=1" in unquote(r.headers["location"])


# --- папка из git: что мешает git pull — разбираем сами или объясняем ------------------------------------------

def _sh(cwd, *args):
    import subprocess

    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def git_pair(tmp_path, monkeypatch):
    """«GitHub» (up) и папка PULSE (pulse) — клон ветки обновлений; в up уже вышла новая версия."""
    for k, v in (("GIT_AUTHOR_NAME", "t"), ("GIT_AUTHOR_EMAIL", "t@t"), ("GIT_COMMITTER_NAME", "t"),
                 ("GIT_COMMITTER_EMAIL", "t@t")):
        monkeypatch.setenv(k, v)
    up, pulse = tmp_path / "up", tmp_path / "pulse"
    up.mkdir()
    _sh(up, "init", "-q", "-b", "claude/pult-v1")
    (up / "app.py").write_text("v1\n", encoding="utf-8")
    (up / "uv.lock").write_text("lock v1\n", encoding="utf-8")
    _sh(up, "add", ".")
    _sh(up, "commit", "-qm", "v1")
    _sh(tmp_path, "clone", "-q", str(up), str(pulse))
    (up / "app.py").write_text("v2\n", encoding="utf-8")
    (up / "uv.lock").write_text("lock v2\n", encoding="utf-8")
    (up / "Сводка — макет.html").write_text("из релиза", encoding="utf-8")
    _sh(up, "add", ".")
    _sh(up, "commit", "-qm", "v2")
    monkeypatch.setattr(updates, "sync_libraries", lambda r: None)
    return up, pulse, {"updates": {"repo": "me/pulse", "branch": "claude/pult-v1", "auto_check": True}}


def test_git_update_sets_aside_untracked_files_and_restores_uv_lock(git_pair):
    """Файл с тем же именем, которого нет в git (положили руками), — в data/backup; uv.lock — как в git."""
    up, pulse, settings = git_pair
    (pulse / "Сводка — макет.html").write_text("мой черновик", encoding="utf-8")
    (pulse / "uv.lock").write_text("пересобрал uv\n", encoding="utf-8")
    updates.apply(settings, root=pulse)
    assert (pulse / "app.py").read_text(encoding="utf-8") == "v2\n"
    assert (pulse / "uv.lock").read_text(encoding="utf-8") == "lock v2\n"
    assert (pulse / "Сводка — макет.html").read_text(encoding="utf-8") == "из релиза"
    [kept] = (config.DATA_DIR / "backup").glob("update-*/Сводка — макет.html")
    assert kept.read_text(encoding="utf-8") == "мой черновик"  # ничего не потеряно


@pytest.fixture
def windows_console(monkeypatch):
    """Как на Windows у директора: вывод программ Python читает в кодировке системы (cp1251), если не сказать иначе,
    а Git для Windows на русской системе отвечает по-русски."""
    import subprocess

    real = subprocess.run

    def run(*args, **kw):
        if kw.get("text") and not kw.get("encoding"):
            kw["encoding"] = "cp1251"
        return real(*args, **kw)

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.delenv("LC_ALL", raising=False)
    monkeypatch.setenv("LANG", "ru_RU.UTF-8")
    monkeypatch.setenv("LANGUAGE", "ru")


def test_git_update_on_windows_console(git_pair, windows_console):
    """Было: русское имя файла из вывода git читалось искажённым, файл не откладывался, и после трёх попыток
    обновление сдавалось с ошибкой про uv.lock."""
    test_git_update_sets_aside_untracked_files_and_restores_uv_lock(git_pair)


@pytest.mark.skipif(os.name == "nt", reason="в Windows имя файла не может кончаться пробелом")
def test_git_update_names_blocker_it_cannot_set_aside(git_pair):
    """Файл, который не вышло отложить, — называем его, а не uv.lock."""
    up, pulse, settings = git_pair
    (up / "черновик.html ").write_text("из релиза", encoding="utf-8")
    _sh(up, "add", ".")
    _sh(up, "commit", "-qm", "v3")
    (pulse / "черновик.html ").write_text("мой", encoding="utf-8")
    (pulse / "uv.lock").write_text("пересобрал uv\n", encoding="utf-8")
    with pytest.raises(updates.UpdateError, match="мешают файлы в папке PULSE: черновик.html"):
        updates.apply(settings, root=pulse)
    assert (pulse / "черновик.html ").read_text(encoding="utf-8") == "мой"
    assert (pulse / "uv.lock").read_text(encoding="utf-8") == "lock v1\n"  # uv.lock вернули: он не мешает


def test_git_update_explains_local_edits_and_own_commits(git_pair):
    up, pulse, settings = git_pair
    (pulse / "app.py").write_text("правлю сам\n", encoding="utf-8")
    with pytest.raises(updates.UpdateError, match=r"изменены файлы, которые меняет обновление: app\.py"):
        updates.apply(settings, root=pulse)
    assert (pulse / "app.py").read_text(encoding="utf-8") == "правлю сам\n"  # правки на месте

    _sh(pulse, "commit", "-qam", "своё")
    with pytest.raises(updates.UpdateError, match="свои коммиты, которых нет в ветке claude/pult-v1"):
        updates.apply(settings, root=pulse)
