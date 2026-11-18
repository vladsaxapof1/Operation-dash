"""Карточка «скоро встреча» поверх всех окон: какие встречи, когда показать, шаги, кнопки, настройки."""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from opsdash import config, meeting_alert, store

ME = "me@example.com"
ZOOM = "https://us06web.zoom.us/j/1234567890?pwd=abcDEF.1"


@pytest.fixture
def client():
    from opsdash.web.app import app

    return TestClient(app, base_url="http://localhost:8765")


@pytest.fixture
def shown(monkeypatch):
    """Вместо окна macOS — список показанных карточек; звонки не открываются по-настоящему."""
    calls = {"show": [], "open": []}
    monkeypatch.setattr(meeting_alert, "is_supported", lambda: True)
    monkeypatch.setattr(meeting_alert, "show", lambda k, settings=None: calls["show"].append(k) or True)
    monkeypatch.setattr(meeting_alert, "open_call", lambda url: calls["open"].append(url))
    config.update_settings("calendar", {"login": ME})
    return calls


def _event(uid, title, minutes_from_now, *, people=(), me="ACCEPTED", length=30, **kw):
    start = datetime.now(timezone.utc).replace(second=0, microsecond=0) + timedelta(minutes=minutes_from_now)
    attendees = ([{"email": ME, "name": "Я", "status": me}] if people else []) + [
        {"email": p, "name": p, "status": "ACCEPTED"} for p in people]
    store.execute('INSERT INTO events(uid, calendar, title, start, "end", all_day, location, description, url, '
                  'attendees, transp) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                  (uid, "Работа", title, start.isoformat(), (start + timedelta(minutes=length)).isoformat(),
                   kw.get("all_day", 0), kw.get("location", ""), kw.get("description", ""), kw.get("url", ""),
                   store.json_col(attendees), kw.get("transp", "OPAQUE")))
    return store.row("SELECT * FROM events WHERE uid=?", (uid,))


def _key(uid):
    return meeting_alert.key(store.row("SELECT * FROM events WHERE uid=?", (uid,)))


def test_call_link_from_any_field():
    assert meeting_alert.call({"description": f'Подключиться: <a href="{ZOOM.replace("&", "&amp;")}">Zoom</a>'}) == \
        {"platform": "Zoom", "url": ZOOM}
    assert meeting_alert.call({"location": "https://telemost.yandex.ru/j/11112222333344"})["platform"] == "Телемост"
    assert meeting_alert.call({"url": "https://meet.google.com/abc-defg-hij"})["platform"] == "Google Meet"
    assert meeting_alert.call({"location": "Переговорка 3", "description": "без ссылки"}) is None


def test_zoom_opens_in_app():
    assert meeting_alert.zoom_app_url(ZOOM) == \
        "zoommtg://us06web.zoom.us/join?action=join&confno=1234567890&pwd=abcDEF.1"
    assert meeting_alert.zoom_app_url("https://us06web.zoom.us/j/9876543210") == \
        "zoommtg://us06web.zoom.us/join?action=join&confno=9876543210"
    assert meeting_alert.zoom_app_url("https://telemost.yandex.ru/j/1") is None


def test_which_meetings_get_a_card(shown):
    settings = config.load_settings()
    _event("call", "Обзор недели", 2, description=ZOOM)                       # звонок без участников в календаре
    _event("people", "Разбор с Петром", 2, people=["petr@example.com"])        # участники без ссылки
    _event("busy", "тайм-аут", 2)                                               # занятое время
    _event("declined", "Планёрка", 2, people=["petr@example.com"], me="DECLINED", description=ZOOM)
    _event("free", "Вебинар", 2, transp="TRANSPARENT", description=ZOOM)
    _event("allday", "Командировка", 2, all_day=1, people=["petr@example.com"])
    _event("lunch", "обед", 2, description=ZOOM)                                 # «обед» со ссылкой: всё равно не встреча
    found = {e["uid"] for e in meeting_alert.candidates(settings)}
    assert found == {"call", "people"}


def test_card_shows_once_in_the_window(shown):
    settings = config.load_settings()
    _event("m1", "Обзор недели", 5, description=ZOOM)
    assert meeting_alert.tick(settings) is None  # за 5 минут рано: по умолчанию за 2
    config.update_settings("meeting_alert", {"minutes": 5})
    settings = config.load_settings()
    k = meeting_alert.tick(settings)
    assert k == _key("m1") and shown["show"] == [k]
    assert meeting_alert.tick(settings) is None  # второй раз не показываем
    config.update_settings("meeting_alert", {"enabled": False})
    _event("m2", "Ещё встреча", 4, description=ZOOM)
    assert meeting_alert.tick(config.load_settings()) is None


def test_late_start_still_shows_but_not_long_after(shown):
    settings = config.load_settings()
    _event("old", "Давно идёт", -20, description=ZOOM, length=60)
    assert meeting_alert.tick(settings) is None
    _event("late", "Только началась", -3, description=ZOOM)
    assert meeting_alert.tick(settings) == _key("late")


def test_same_time_meetings_one_card(shown):
    settings = config.load_settings()
    _event("maybe", "Daily", 1, people=["a@example.com", "b@example.com", "c@example.com"], me="NEEDS-ACTION",
           description=ZOOM)
    _event("yes", "Планёрка", 1, people=["a@example.com"], description=ZOOM)
    k = meeting_alert.tick(settings)
    assert k == _key("yes")  # принятая вами важнее той, на которую не ответили
    assert meeting_alert.state()[_key("maybe")]["status"] == "overlap"
    assert meeting_alert.tick(settings) is None and shown["show"] == [k]
    c = meeting_alert.card(k, settings)
    assert c["same_time"] == [{"key": _key("maybe"), "title": "Daily"}]


def test_card_steps(shown):
    settings = config.load_settings()
    _event("soon", "Обзор недели", 2, description=ZOOM, people=["petr@example.com"])
    c = meeting_alert.card(_key("soon"), settings)
    assert c["step"] == "soon" and c["platform"] == "Zoom" and c["has_link"] and c["people"] == 1
    meeting_alert.act(_key("soon"), "join", settings)
    assert shown["open"] == [ZOOM]
    assert meeting_alert.card(_key("soon"), settings)["step"] == "record"
    _event("started", "Уже идёт", -1, description=ZOOM)
    assert meeting_alert.card(_key("started"), settings)["step"] == "record"
    assert meeting_alert.card("nope", settings) is None
    _event("room", "Встреча в переговорке", 2, people=["petr@example.com"])  # без звонка: открывать нечего
    room = meeting_alert.card(_key("room"), settings)
    assert room["step"] == "record" and room["where"] == "" and not room["has_link"]
    _event("tm", "Созвон с юристами", 2, location="https://telemost.yandex.ru/j/11112222333344")
    assert meeting_alert.card(_key("tm"), settings)["where"] == "в Телемосте"


def test_alert_page_and_buttons(client, shown):
    _event("m", "Обзор недели", 2, description=ZOOM, people=["petr@example.com"])
    k = _key("m")
    page = client.get(f"/alert/{k}")
    assert page.status_code == 200
    assert "Обзор недели" in page.text and "Открыть Zoom" in page.text and "Включите запись в Zoom" in page.text
    assert "1 участник" in page.text and "/static/alert.js" in page.text
    assert "pulse:close" in client.get("/alert/unknown").text  # нечего показать — окно закроется
    r = client.post(f"/alert/{k}/join", headers={"referer": f"http://localhost:8765/alert/{k}"})
    assert r.json() == {"step": "record"} and shown["open"] == [ZOOM]
    r = client.post(f"/alert/{k}/recording", headers={"referer": f"http://localhost:8765/alert/{k}"})
    assert r.json() == {"step": "done"} and meeting_alert.state()[k]["status"] == "recording"
    assert client.post(f"/alert/{k}/bogus").status_code == 404


def test_demo_card_opens_nothing(client, shown):
    page = client.get("/alert/demo")
    assert "Пример: встреча с командой" in page.text and "Открыть Zoom" in page.text
    assert client.post("/alert/demo/join").json() == {"step": "record"}
    assert shown["open"] == [] and meeting_alert.state() == {}


def test_settings_save_and_test_button(client, shown):
    r = client.post("/settings/meeting-alert", data={"minutes": "5"}, follow_redirects=False,
                    headers={"referer": "http://localhost:8765/settings?tab=recordings"})
    assert r.status_code == 303
    assert config.load_settings()["meeting_alert"] == {"enabled": False, "minutes": 5}
    client.post("/settings/meeting-alert", data={"enabled": "on", "minutes": "7"})
    assert config.load_settings()["meeting_alert"] == {"enabled": True, "minutes": 2}  # 7 нет в списке
    r = client.post("/settings/meeting-alert/test", follow_redirects=False,
                    headers={"referer": "http://localhost:8765/settings?tab=recordings"})
    assert shown["show"] == ["demo"] and "msg=" in r.headers["location"]
