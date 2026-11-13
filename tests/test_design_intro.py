"""Окно «У PULSE новый дизайн»: один раз — тем, кто пользовался старым PULSE; новой установке — никогда."""

import pytest
from fastapi.testclient import TestClient

from opsdash import store


@pytest.fixture
def client():
    from opsdash.web.app import app

    return TestClient(app, base_url="http://localhost:8765")


def _upgrade_from_old_version():
    """База старой версии: PULSE уже работал, а про окно ещё ничего не решено. Запускаем новую версию."""
    store.log("collect", "Telegram: 3 новых сообщения")
    store.execute("DELETE FROM state WHERE key=?", (store.DESIGN_INTRO,))
    store._initialized.clear()


def test_fresh_install_never_sees_the_window(client):
    assert store.get_state(store.DESIGN_INTRO) == "seen"
    assert "data-design-intro" not in client.get("/").text


def test_shown_once_after_update_until_closed(client):
    _upgrade_from_old_version()
    assert store.get_state(store.DESIGN_INTRO) == "show"
    page = client.get("/").text
    assert "data-design-intro" in page and "У PULSE новый дизайн" in page and ">Круто<" in page
    assert "data-design-intro" in client.get("/commitments").text  # не закрыли — покажем и на другой странице

    assert client.post("/api/design-intro/seen").json() == {"ok": True}
    assert "data-design-intro" not in client.get("/").text
    store._initialized.clear()  # перезапуск PULSE решения не меняет
    assert "data-design-intro" not in client.get("/").text


def test_archived_sections_hidden_and_switchable(client):
    """Документы — в архиве: нет в меню, страницы ведут в Настройки, задания не запускаются. База знаний с 2.0.46 —
    вкладка «Уроки и удачи» в «Памяти»: открывается всегда, а архив выключает только обзор недели (anthology)."""
    from opsdash import config, jobs

    page = client.get("/").text
    assert 'href="/docs"' not in page and 'href="/kb"' not in page and 'href="/memory"' in page
    for path in ("/docs", "/docs/view?path=x"):
        r = client.get(path, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/settings?tab=archive"
    assert client.get("/kb", follow_redirects=False).status_code == 200  # «Уроки и удачи» — в «Памяти»
    assert client.get("/kb/context").status_code == 200  # сводка контекста Claude — не раздел, открывается
    assert config.archived_jobs() == {"docs-index", "company-context", "anthology"}
    assert "Индекс документов" not in client.get("/journal").text

    archive = client.get("/settings?tab=archive").text
    assert "Архивные разделы" in archive and "Включить" in archive
    client.post("/settings/sections/kb", data={"on": "1"})
    assert config.section_on("kb") and config.archived_jobs() == {"docs-index", "company-context"}
    assert client.get("/kb").status_code == 200
    client.post("/settings/sections/kb", data={})
    assert not config.section_on("kb") and config.load_settings()["sections"]["archived_at"]["kb"]