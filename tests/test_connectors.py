"""Подключения: описание источников, состояние карточек, каталог и окно подключения (CON-1 – CON-3)."""

import pytest
from starlette.testclient import TestClient

from opsdash import brain, codex, config, connectors, store


@pytest.fixture(autouse=True)
def no_real_assistants(monkeypatch):
    # без настоящих Claude Code и Codex на машине: что найдено и вошли ли — задаёт сам тест
    monkeypatch.setattr(brain, "find_claude", lambda *a, **k: None)
    monkeypatch.setattr(codex, "find", lambda *a, **k: None)


@pytest.fixture
def client():
    from opsdash.web.app import app

    return TestClient(app, base_url="http://localhost:8765")


def card(cid):
    return connectors.card(cid)


def test_manifest_complete():
    ids = [c["id"] for c in connectors.all_connectors()]
    assert len(ids) == len(set(ids))
    groups = {g["id"] for g in connectors.manifest()["groups"]}
    for c in connectors.all_connectors():
        assert c["group"] in groups and c["catches"] and c["enables"] and c["reads"] and c["never"], c["id"]
        for st in c.get("steps", []):
            assert st["check"] in connectors.CHECKS and set(st) == {"title", "check"}, st  # запятая не делит шаг
    # у каждого окна — свой шаблон с формой
    from pathlib import Path

    tpl = Path(connectors.__file__).parent / "web" / "templates" / "connectors"
    # mcp.html — общее окно MCP-подключений (opsdash.mcp), у него нет записи в connectors.yaml
    assert {p.stem for p in tpl.glob("*.html") if not p.stem.startswith("_")} - {"mcp"} == connectors.panel_ids()


def test_card_states(settings_with):
    assert card("mail")["kind"] == "off" and card("mail")["label"] == "не подключено"
    settings_with(mail={"email": "a@x.ru"})
    config.set_secret("MAIL_APP_PASSWORD", "secret")
    assert card("mail")["kind"] == "paused"  # всё введено, но галочка «Читать почту» снята
    settings_with(mail={"enabled": True})
    assert card("mail")["kind"] == "pending"  # включено, первого сбора ещё не было
    store.execute("INSERT INTO jobs (name, last_run, last_ok) VALUES ('collect:mail', '2026-11-06T10:00:00+00:00', "
                  "'2026-11-06T10:00:00+00:00')")
    assert card("mail")["kind"] == "ok" and card("mail")["last_ok"]
    store.execute("UPDATE jobs SET last_run='2026-11-06T11:00:00+00:00', "
                  "last_error='[AUTHENTICATIONFAILED] Invalid credentials' WHERE name='collect:mail'")
    c = card("mail")
    assert c["kind"] == "error" and c["label"] == "ошибка: пароль не подходит" and "Invalid credentials" in c["error"]


def test_setup_names_the_missing_step(settings_with):
    settings_with(telegram={"enabled": True, "api_id": "1"})
    config.set_secret("TELEGRAM_API_HASH", "h" * 32)
    c = card("telegram")
    assert c["kind"] == "setup" and c["label"] == "нужна настройка: вход в аккаунт"
    assert [s["done"] for s in c["steps"]] == [True, False, False]
    store.set_state("telegram_me", {"name": "Анна"})
    assert card("telegram")["label"] == "нужна настройка: какие чаты читать"
    store.upsert_chat("telegram", "7", "Склад", "group", enabled=1)
    assert card("telegram")["kind"] == "pending"


def test_humanize_errors():
    assert connectors.humanize("Connection timed out") == "нет связи с сервисом"
    assert connectors.humanize("HTTP 401 Unauthorized") == "токен не принят"
    assert connectors.humanize("что-то странное") == "сбор не удался"
    assert connectors.humanize(None) == "сбор не удался"


def test_catalog_page_and_counts(client, settings_with):
    settings_with(kaiten={"enabled": True, "domain": "x.kaiten.ru"})
    page = client.get("/settings?tab=connections").text
    assert "Подключено 1 из 13" in page and "2 требуют внимания" in page  # Kaiten без токена и выбранный Claude не найден
    assert "нужна настройка: адрес и токен" in page
    assert 'href="/settings?tab=gmail"' in page and 'href="/settings?tab=claude"' in page  # окно или своя страница
    assert 'id="conn-dlg"' not in page  # на каталоге окно не открыто
    assert '<span class="subnav-n warn-text">1 из 13</span>' in page  # подменю: оранжевым — есть что починить
    assert "Документы" not in page.split("conn-head")[1]  # раздел в архиве — карточки нет


def test_source_tab_opens_panel_over_catalog(client):
    page = client.get("/settings?tab=pachca").text
    assert 'data-conn="pachca"' in page and "data-open-on-load" in page
    assert "Персональный токен" in page  # форма источника — в окне
    assert "Что станет возможно" in page and "Только отмеченные чаты и треды в них" in page
    assert 'aria-current="page"><span>Подключения</span>' in page  # в подменю выделены «Подключения»
    gmail = client.get("/settings?tab=gmail").text
    assert "Адрес Gmail" in gmail and "Яндекс ID → Безопасность" not in gmail  # почта Яндекса — отдельным окном


def test_saving_returns_to_open_panel(client):
    r = client.post("/settings/gmail", data={"email": "a@gmail.com", "password": ""}, follow_redirects=False)
    assert r.headers["location"].startswith("/settings?tab=gmail")


def test_logos():
    assert card("telegram")["logo"] == "telegram.svg" and card("kaiten")["logo"] == "kaiten.png"  # svg или png
    assert card("claude")["logo"] == "claude.svg" and card("chatgpt")["logo"] == "chatgpt.svg"
    assert all(card(c["id"])["logo"] for c in connectors.all_connectors() if c["id"] not in ("recordings", "knowledge"))


def test_assistants_one_works(monkeypatch, settings_with, client):
    assert card("claude")["kind"] == "error" and card("claude")["label"] == "выбран, но не установлен"
    assert card("chatgpt")["kind"] == "off" and card("chatgpt")["label"] == "не установлен"
    assert connectors.catalog()["problems"] == 1  # выбранный помощник не работает — «требует внимания»
    monkeypatch.setattr(brain, "find_claude", lambda *a, **k: "/x/claude")
    monkeypatch.setattr(brain, "login_status", lambda *a, **k: {"loggedIn": True, "email": "a@x.ru"})
    monkeypatch.setattr(codex, "find", lambda *a, **k: "/x/codex")
    monkeypatch.setattr(codex, "login_status", lambda *a, **k: {"loggedIn": False})
    assert card("claude")["kind"] == "ok" and card("claude")["active"] and card("claude")["who"] == "a@x.ru"
    assert card("chatgpt")["label"] == "нужен вход" and not card("chatgpt")["active"]
    page = client.get("/settings?tab=connections").text
    assert 'aria-label="Кто работает в PULSE"' in page and "Работает только один помощник — сейчас Claude" in page
    assert "conn-card is-active" in page or "is-active" in page
    # окно ChatGPT: работает Claude, кнопка переключить; переключили — вернулись туда же
    gpt = client.get("/settings?tab=chatgpt").text
    assert "Переключить на ChatGPT" in gpt and "Путь к codex" in gpt and "Путь к claude" not in gpt
    r = client.post("/settings/ai", data={"engine": "chatgpt"}, follow_redirects=False,
                    headers={"referer": "http://localhost:8765/settings?tab=chatgpt"})
    assert r.headers["location"].startswith("/settings?tab=chatgpt&msg=")
    assert brain.engine() == "chatgpt" and card("chatgpt")["label"] == "выбран, но нужен вход"
    assert card("claude")["kind"] == "standby" and card("claude")["label"] == "готов · сейчас работает ChatGPT"


def test_recordings_and_claude_are_panels_not_menu_items(client):
    page = client.get("/settings?tab=recordings").text
    assert 'data-conn="recordings"' in page and "Папки с записями" in page
    menu = page.split('class="subnav"')[1].split("</nav>")[0]
    assert "Записи встреч" not in menu and "Claude / ChatGPT" not in menu and "Расписание" in menu
    claude = client.get("/settings?tab=claude").text
    assert 'data-conn="claude"' in claude and "Расход Claude" in claude and "Путь к codex" not in claude


def test_docs_folders_have_own_menu_item(client):
    """Папки с документами — пунктом «Документы» в подменю: в архиве раздела карточки в каталоге нет, а папки
    задаются и тогда (их читает Claude); раздел можно включить прямо там."""
    assert not config.section_on("docs")  # по умолчанию раздел в архиве
    page = client.get("/settings?tab=docs").text
    menu = page.split('class="subnav"')[1].split("</nav>")[0]
    assert 'aria-current="page"><span>Документы</span><span class="subnav-n">в архиве</span>' in menu
    assert "Папки с рабочими документами" in page and "Включить раздел" in page
    assert 'data-conn="knowledge"' not in client.get("/settings?tab=connections").text  # карточки в каталоге нет
    old = client.get("/settings?tab=knowledge").text  # старая ссылка — на пункт меню, а не в пустой каталог
    assert 'aria-current="page"><span>Документы</span>' in old and "Папки с рабочими документами" in old
    r = client.post("/settings/sections/docs", data={"on": "1"}, follow_redirects=False,
                    headers={"referer": "http://localhost:8765/settings?tab=docs"})
    assert config.section_on("docs") and r.headers["location"].startswith("/settings?tab=docs&msg=")
    page = client.get("/settings?tab=docs").text
    assert "Включить раздел" not in page and 'aria-current="page"><span>Документы</span></a>' in page
    assert 'data-conn="knowledge"' in client.get("/settings?tab=knowledge").text  # раздел включён — снова окно


def test_data_page(client, settings_with):
    settings_with(mail={"enabled": True})
    page = client.get("/settings?tab=data").text
    assert "Что PULSE делает с данными" in page and "data/ops.db" in page and "secrets/.env" in page
    assert "Не отправляет письма и не помечает их прочитанными" in page  # что читает каждое подключение — из описания
    assert 'aria-current="page"><span>Ваши данные</span>' in page
    assert 'href="/settings?tab=data"' in client.get("/settings?tab=connections").text  # ссылка из каталога


def test_empty_blocks_suggest_connections(client, settings_with):
    store.set_state("welcome", {"dismissed": True})  # чистая установка открыла бы мастер — его закрыли
    home = client.get("/").text
    assert "PULSE пока ничего не видит" in home and 'href="/settings?tab=connections">Открыть подключения' in home
    assert "Здесь появятся письма и сообщения" in home and "Переписка не подключена" in home
    assert 'href="/settings?tab=ics_calendar">Outlook и iCloud по ссылке' in home  # блок «Сегодня» без календаря
    settings_with(gmail={"enabled": True})
    home = client.get("/").text
    assert "Здесь появятся письма и сообщения" not in home and "От вас сейчас ничего не ждут" in home


def test_soon_cards_open_request(client, settings_with):
    settings_with(updates={"repo": "owner/pulse"})
    soon = {s["id"]: s for s in connectors.catalog()["soon"]}
    assert soon["outlook"]["request"].startswith("https://github.com/owner/pulse/issues/new?title=")
    page = client.get("/settings?tab=connections").text
    assert 'id="cg-soon">Скоро<' in page and "Нужно мне ↗" in page and "Outlook и Microsoft 365" in page
    settings_with(updates={"repo": ""})
    assert "Нужно мне ↗" not in client.get("/settings?tab=connections").text  # некуда — кнопки нет


def test_soon_texts_not_split_by_commas():
    for s in connectors.manifest()["soon"]:
        assert set(s) == {"id", "title", "color", "mono", "catches"}, s  # запятая в тексте не делит запись на поля
