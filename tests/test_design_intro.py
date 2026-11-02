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
