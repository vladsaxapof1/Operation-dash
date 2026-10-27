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
    page = client.get("/settings?tab=general").text
    assert "Обновления PULSE" in page and f"Доступна новая версия {NEW[:7]}" in page and "Доска задач: узкий экран" in page
    assert "● Доступно обновление" in client.get("/").text  # в меню на любой странице

    r = client.post("/settings/updates", data={"repo": "https://github.com/me/pulse.git", "branch": "dev", "token": "github_pat_x"},
                    follow_redirects=False)
    assert "msg=" in r.headers["location"] and config.get_secret("GITHUB_TOKEN") == "github_pat_x"
    assert config.load_settings()["updates"] == {"repo": "me/pulse", "branch": "dev", "auto_check": False}
    r = client.post("/updates/apply", follow_redirects=False)
    assert submitted[-1] == "update" and "updating=1" in unquote(r.headers["location"])
