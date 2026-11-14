"""Новый дизайн (бета): переключатель в настройках, каркас v3 поверх тех же данных и маршрутов."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from opsdash import config

PAGES = ["/", "/commitments", "/calendar", "/meetings", "/chats", "/memory", "/decisions", "/digests", "/people",
         "/ask", "/office", "/skills", "/settings", "/journal", "/help"]


@pytest.fixture
def client():
    from opsdash.web.app import app

    return TestClient(app, base_url="http://localhost:8765")


def test_classic_by_default(client):
    page = client.get("/").text
    assert "/static/v3.css" not in page and 'class="d3' not in page


def test_switch_to_new_design_and_back(client):
    r = client.post("/settings/design", data={"design": "v3"}, headers={"referer": "http://localhost:8765/settings"},
                    follow_redirects=False)
    assert r.status_code in (302, 303)
    assert config.load_settings()["web"]["design"] == "v3"
    page = client.get("/commitments").text  # «/» на чистой установке ведёт в мастер — он без меню
    assert "/static/v3.css" in page and 'class="d3' in page
    # меню нового дизайна: группы «Работа · Память · Система», Журнал и Справка — внизу
    assert ">Работа<" in page and ">Система<" in page and 'href="/journal"' in page

    client.post("/settings/design", data={"design": "classic"}, headers={"referer": "http://localhost:8765/settings"})
    assert "/static/v3.css" not in client.get("/").text


def test_unknown_design_is_refused(client):
    client.post("/settings/design", data={"design": "neon"}, headers={"referer": "http://localhost:8765/settings"})
    assert config.load_settings()["web"].get("design", "classic") == "classic"


@pytest.mark.parametrize("path", PAGES)
def test_every_section_opens_in_new_design(client, path):
    config.update_settings("web", {"design": "v3"})
    r = client.get(path)
    assert r.status_code == 200, path
    assert "/static/v3.css" in r.text


def test_v3_welcome_without_menu_and_calendar_day_review(client):
    client.post("/settings/design", data={"design": "v3"}, headers={"referer": "http://localhost:8765/settings"})
    wz = client.get("/welcome").text
    assert "wz-shell" in wz and 'class="sidebar' not in wz and 'class="tabbar' not in wz
    cal = client.get("/calendar").text  # «Разобрать день» и его окно — и в Календаре
    assert 'data-dlg-open="day-dlg"' in cal and 'id="day-dlg"' in cal


def test_calendar_free_time_opens_new_meeting_dialog():
    js = (Path(__file__).parent.parent / "opsdash/web/static/app.js").read_text(encoding="utf-8")
    i = js.index('querySelectorAll(".wk-col")')
    assert 'getElementById("new-dlg")' in js[i:i + 1500] and "openDlg(newDlg)" in js[i:i + 1500]
