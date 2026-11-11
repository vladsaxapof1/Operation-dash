"""Пути к файлам в текстах PULSE — кнопкой «Показать в Finder» и «Скопировать путь» (opsdash/filepaths.py)."""

import re

import pytest
from fastapi.testclient import TestClient

from opsdash import config, filepaths

PATH_BTN = re.compile(r'data-fpath="([^"]*)".*?<span class="fpath-t">(.*?)</span>', re.S)


def found(html: str) -> list[tuple[str, str]]:
    """[(что показано, полный путь)] — кнопки путей в HTML."""
    return [(shown, full) for full, shown in PATH_BTN.findall(html)]


@pytest.fixture
def office(clean_home, settings_with, tmp_path):
    """Рабочая папка «Работа» с проектом, где в именах пробелы, и бриф в папке PULSE."""
    root = tmp_path / "Работа"
    deck = root / "Презентация по итогам октября" / "Итоги октября — Коммерция.pptx"
    deck.parent.mkdir(parents=True)
    deck.write_text("x")
    (root / "Презентация по итогам октября" / "_скрипты 06.11").mkdir()
    (root / "Презентация по итогам октября" / "_скрипты 06.11" / "build.sh").write_text("x")
    brief = config.DATA_DIR / "briefs" / "agent-qbr-2026-11-11.md"
    brief.parent.mkdir(parents=True, exist_ok=True)
    brief.write_text("x")
    (config.SECRETS_DIR / "kaiten.token").write_text("x")
    settings_with(office={"folders": [str(root)]})
    filepaths._WS.update(key=None)
    return {"root": root.resolve(), "deck": deck.resolve(), "brief": brief.resolve()}


def test_resolves_relative_paths_from_pulse_and_work_folders(office):
    r = filepaths.Resolver()
    assert r.resolve("data/briefs/agent-qbr-2026-11-11.md") == office["brief"]
    assert r.resolve("data/briefs/") == office["brief"].parent
    # от родителя рабочей папки («Работа/…»), от неё самой и от её подпапки
    assert r.resolve("Работа/Презентация по итогам октября") == office["deck"].parent
    assert r.resolve("Презентация по итогам октября/Итоги октября — Коммерция.pptx") == office["deck"]
    assert r.resolve(str(office["deck"])) == office["deck"]
    assert r.resolve("opsdash/web/app.py:120") is None  # такого файла в папке PULSE теста нет


def test_forgives_a_typo_but_not_a_different_name(office):
    r = filepaths.Resolver()
    assert r.resolve("Работа/Презентаця по итогам октября") == office["deck"].parent  # Claude ошибся в букве
    assert r.resolve("Работа/Отчёт по итогам ноября") is None


def test_never_secrets_or_outside_work_folders(office):
    r = filepaths.Resolver()
    assert r.resolve("secrets/kaiten.token") is None
    assert r.resolve(str(config.SECRETS_DIR / "kaiten.token")) is None
    assert r.resolve("/etc/hosts") is None
    assert r.resolve("data/briefs/agent-qbr-2026-11-11.md/../../../secrets/kaiten.token") is None
    assert r.resolve("https://example.com/data/briefs") is None


def test_path_with_spaces_in_plain_text_ends_where_the_file_ends(office):
    deck = office["deck"]
    text = (f"Файлы:\n- {deck} — текущая версия деки, нужный слайд 06\n"
            f"- {deck.parent}/_скрипты 06.11/build.sh — сборка и рендер.\n"
            "Бриф — data/briefs/agent-qbr-2026-11-11.md, папка — Работа/Презентация по итогам октября. "
            "А это не пути: и/или, 50/50, км/ч, /start, https://example.com/data/briefs")
    out = found(str(filepaths.text(text)))
    assert [shown for shown, _ in out] == [
        str(deck), f"{deck.parent}/_скрипты 06.11/build.sh", "data/briefs/agent-qbr-2026-11-11.md",
        "Работа/Презентация по итогам октября"]
    assert out[2][1] == str(office["brief"])


def test_markdown_code_links_and_quotes_become_buttons(office):
    from opsdash.web.app import f_md

    html = f_md("Промт лежит в `data/briefs/agent-qbr-2026-11-11.md`, агент работает в папке "
                "«Работа/Презентаця по итогам октября». Ссылкой: [бриф](data/briefs/agent-qbr-2026-11-11.md), "
                "на сайт — [пример](https://example.com/data/briefs).\n\n    data/briefs\n\n`secrets/kaiten.token`")
    assert found(html) == [("data/briefs/agent-qbr-2026-11-11.md", str(office["brief"])),
                           ("Работа/Презентаця по итогам октября", str(office["deck"].parent)),
                           ("бриф", str(office["brief"]))]
    assert 'class="fpath mono"' in html and "«<span" in html  # `код` — моноширинно, кавычки «» остаются
    assert '<a href="https://example.com/data/briefs">' in html and "<pre><code>data/briefs" in html
    assert "<code>secrets/kaiten.token</code>" in html


def test_plain_text_is_escaped(office):
    out = str(filepaths.text("<script>alert(1)</script> и data/briefs/"))
    assert "<script>" not in out and "&lt;script&gt;" in out and 'class="fpath"' in out
    assert str(filepaths.text("<b>без путей</b>")) == "&lt;b&gt;без путей&lt;/b&gt;"


def test_task_window_shows_paths_as_buttons(office):
    from opsdash import records, worklog
    from opsdash.web.app import app

    cid = records.add_commitment(direction="mine", what="Обновить слайд в «Работа/Презентация по итогам октября»",
                                 title="Слайд итогов")
    worklog.add(cid, "Промт лежит в data/briefs/agent-qbr-2026-11-11.md", by="director")
    page = TestClient(app, base_url="http://localhost:8765").get(f"/commitments?task={cid}").text
    shown = [s for s, _ in found(page)]
    assert "Работа/Презентация по итогам октября" in shown and "data/briefs/agent-qbr-2026-11-11.md" in shown
    assert 'data-text="Промт лежит в data/briefs/agent-qbr-2026-11-11.md"' in page  # «изменить» — исходный текст


def test_reveal_endpoint_shows_only_allowed_files(office, monkeypatch):
    from opsdash.web.app import app

    client = TestClient(app, base_url="http://localhost:8765")
    shown = []
    monkeypatch.setattr(filepaths, "reveal", lambda path: shown.append(path))
    r = client.post("/files/reveal", data={"path": str(office["brief"])})
    assert r.status_code == 200 and r.json()["ok"] and shown == [office["brief"]]
    assert client.post("/files/reveal", data={"path": "Работа/Презентация по итогам октября"}).json()["ok"]
    for bad in (str(config.SECRETS_DIR / "kaiten.token"), "/etc/hosts", "data/нет такого.md"):
        r = client.post("/files/reveal", data={"path": bad})
        assert r.status_code == 404 and not r.json()["ok"]
    assert len(shown) == 2
    r = client.post("/files/reveal", data={"path": str(office["brief"])}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403 and len(shown) == 2  # с чужого сайта — нет


def test_reveal_opens_folder_and_selects_file(office, monkeypatch):
    calls = []
    monkeypatch.setattr(filepaths.subprocess, "run", lambda cmd, **k: calls.append(cmd))
    monkeypatch.setattr(filepaths.sys, "platform", "darwin")
    filepaths.reveal(office["deck"])
    filepaths.reveal(office["deck"].parent)
    assert calls == [["open", "-R", str(office["deck"])], ["open", str(office["deck"].parent)]]
