"""Новый дизайн (v3): по умолчанию после обновления, окно «У PULSE новый дизайн», возврат к классическому."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from opsdash import config, store

PAGES = ["/", "/commitments", "/calendar", "/meetings", "/chats", "/memory", "/decisions", "/digests", "/people",
         "/ask", "/office", "/skills", "/settings", "/journal", "/help"]


@pytest.fixture
def client():
    from opsdash.web.app import app

    return TestClient(app, base_url="http://localhost:8765")


def _no_choice():
    """Установка, где дизайн ещё не выбирали (conftest для остальных тестов ставит классический)."""
    s = config.load_settings()
    s["web"].pop("design", None)
    config.save_settings(s)


def test_new_design_by_default(client):
    _no_choice()
    page = client.get("/commitments").text
    assert "/static/v3.css" in page and 'class="d3' in page


def _upgrade_from_old_version():
    """PULSE уже работал, а про окно нового дизайна ещё ничего не решено — запускаем новую версию."""
    store.log("collect", "Telegram: 3 новых сообщения")
    store.execute("DELETE FROM state WHERE key IN (?, ?)", (store.DESIGN_INTRO, store.DESIGN3_INTRO))
    store._initialized.clear()


def test_fresh_install_sees_no_intro(client):
    _no_choice()
    assert store.get_state(store.DESIGN3_INTRO) == "seen"
    assert "data-design-intro" not in client.get("/commitments").text


def test_intro_after_update_once_with_cool_button(client):
    _no_choice()
    _upgrade_from_old_version()
    page = client.get("/commitments").text
    assert "/static/v3.css" in page  # новый дизайн уже включён
    assert "data-design-intro" in page and "У PULSE новый дизайн" in page and ">Круто<" in page
    assert 'data-seen-url="/api/design-intro/seen?v=3"' in page and "Вернуть прежний" in page
    assert page.count("data-design-intro") == 1  # прежнее окно 2.0 вместе с ним не показывается
    assert client.post("/api/design-intro/seen?v=3").json() == {"ok": True}
    assert "data-design-intro" not in client.get("/commitments").text
    store._initialized.clear()
    assert "data-design-intro" not in client.get("/").text


def test_intro_return_to_classic_in_one_click(client):
    _no_choice()
    _upgrade_from_old_version()
    client.get("/commitments")
    client.post("/settings/design", data={"design": "classic"}, headers={"referer": "http://localhost:8765/commitments"})
    assert config.load_settings()["web"]["design"] == "classic"
    assert store.get_state(store.DESIGN3_INTRO) == "seen"
    assert "/static/v3.css" not in client.get("/commitments").text


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


def test_motion_setting_saves_and_marks_body(client):
    config.update_settings("web", {"design": "v3"})
    page = client.get("/settings?tab=general").text
    assert "/static/v3-motion.css" in page and 'action="/settings/motion"' in page
    # не выбирали — класса нет: при «уменьшить движение» в системе анимации выключает CSS
    assert "motion-" not in page.split("<body", 1)[1].split(">", 1)[0]
    assert 'value="normal" class="active"' in page
    for value, cls in (("fast", "motion-fast"), ("off", "motion-off"), ("normal", "motion-normal")):
        r = client.post("/settings/motion", data={"motion": value}, headers={"referer": "http://localhost:8765/settings"},
                        follow_redirects=False)
        assert r.status_code in (302, 303)
        assert config.load_settings()["web"]["motion"] == value
        body = client.get("/commitments").text.split("<body", 1)[1].split(">", 1)[0]
        assert cls in body


def test_unknown_motion_is_refused(client):
    config.update_settings("web", {"design": "v3", "motion": "fast"})
    client.post("/settings/motion", data={"motion": "warp"}, headers={"referer": "http://localhost:8765/settings"})
    assert config.load_settings()["web"]["motion"] == "fast"


def test_motion_does_not_touch_classic_design(client):
    config.update_settings("web", {"design": "classic", "motion": "off"})
    page = client.get("/commitments").text
    assert "v3-motion.css" not in page and "motion-off" not in page


@pytest.mark.parametrize("theme, cls", [("system", "auto"), ("light", "light"), ("dark", "dark"),
                                        ("graphite", "dark"), ("graphite-light", "light")])
def test_new_design_theme(client, theme, cls):
    """Новый дизайн: «Как в системе» / «Светлая» / «Тёмная»; «Графит» прежних настроек — тёмная или светлая."""
    config.update_settings("web", {"design": "v3", "theme": theme})
    page = client.get("/commitments").text
    assert f'class="d3 {cls}' in page and "/static/v3-light.css" in page
    s = client.get("/settings").text
    assert "Как в системе" in s and ">Светлая<" in s and "Графит" not in s


@pytest.mark.parametrize("design", ["v3", "classic"])
def test_nav_script_loads_once_before_app(client, design):
    """nav.js (переходы без перезагрузки) — в обоих дизайнах, первым и с data-once: после смены экрана его не
    запускают снова, а app.js и в классическом зовёт pulseReload / pulseGo, которые он даёт."""
    config.update_settings("web", {"design": design})
    page = client.get("/commitments").text
    nav = page.index('src="/static/nav.js')
    assert 0 < nav < page.index('src="/static/app.js')
    assert "data-once" in page[nav:page.index(">", nav)]


def test_nav_script_has_no_inline_eval():
    """CSP запрещает inline-скрипты: nav.js заново подключает скрипты страницы только по src."""
    src = (Path(__file__).parents[1] / "opsdash/web/static/nav.js").read_text(encoding="utf-8")
    assert "eval(" not in src and "new Function" not in src
