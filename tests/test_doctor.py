"""Здоровье PULSE (CON-5) и «другой сервер» (MORE-2): почта по IMAP, календарь по CalDAV и по ссылке."""

from collections import namedtuple
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from starlette.testclient import TestClient

from opsdash import brain, codex, config, doctor, jobs, store
from opsdash.sources import calendar, mail


@pytest.fixture(autouse=True)
def assistant_ok(monkeypatch):
    monkeypatch.setattr(brain, "find_claude", lambda *a, **k: "/x/claude")
    monkeypatch.setattr(brain, "login_status", lambda *a, **k: {"loggedIn": True, "email": "a@x.ru"})
    monkeypatch.setattr(codex, "find", lambda *a, **k: None)


def ago(**kw) -> str:
    return (datetime.now(timezone.utc) - timedelta(**kw)).replace(microsecond=0).isoformat()


def job(name, last_ok=None, last_run=None, last_error=None):
    store.execute("INSERT OR REPLACE INTO jobs(name, last_run, last_ok, last_error) VALUES(?,?,?,?)",
                  (name, last_run or last_ok, last_ok, last_error))


def rows(res):
    return {r["key"]: r for r in res["system"] + res["sources"]}


def test_system_rows(monkeypatch, settings_with):
    res = doctor.run()
    r = rows(res)
    assert r["assistant"]["level"] == "ok" and "Claude" in r["assistant"]["text"]
    assert r["jobs"]["level"] == "warn" and r["jobs"]["text"] == "ещё не запускались"
    assert r["bot"]["level"] == "warn" and "не подключён" in r["bot"]["text"]
    job("housekeeping", last_ok=ago(minutes=2))
    assert rows(doctor.run())["jobs"]["level"] == "ok"
    job("housekeeping", last_ok=ago(hours=3))
    assert rows(doctor.run())["jobs"]["text"] == "не идут 3 ч"
    Usage = namedtuple("Usage", "total used free")
    monkeypatch.setattr(doctor.shutil, "disk_usage", lambda p: Usage(1, 1, int(1.5 * 1024 ** 3)))
    res = doctor.run()
    assert rows(res)["disk"]["level"] == "bad" and res["worst"] == "bad"
    monkeypatch.setattr(brain, "find_claude", lambda *a, **k: None)
    assert rows(doctor.run())["assistant"]["text"] == "Claude не установлен"


def test_source_rows_and_silence(settings_with):
    settings_with(mail={"enabled": True, "email": "a@x.ru"})
    config.set_secret("MAIL_APP_PASSWORD", "p")
    assert rows(doctor.run())["mail"]["text"] == "ждёт первого сбора"
    job("collect:mail", last_ok=ago(minutes=5))
    assert rows(doctor.run())["mail"]["level"] == "ok"
    job("collect:mail", last_ok=ago(hours=5))
    r = rows(doctor.run())["mail"]
    assert r["level"] == "warn" and r["text"] == "не собирается 5 ч" and r["link"] == "/settings?tab=mail"
    assert "gmail" not in rows(doctor.run())  # выключенное не показываем
    assert rows(doctor.run(include_off=True))["gmail"]["level"] == "off"


def test_watch_alerts_once_a_day(monkeypatch, settings_with):
    from opsdash import bot

    sent = []
    monkeypatch.setattr(bot, "notify", lambda text, **kw: sent.append(text))
    settings_with(mail={"enabled": True}, gmail={"enabled": True}, telegram_bot={"enabled": True, "owner_chat_id": "1"})
    job("collect:mail", last_ok=ago(hours=2), last_run=ago(minutes=3), last_error="[AUTHENTICATIONFAILED] bad")
    job("collect:gmail", last_ok=ago(minutes=40), last_run=ago(minutes=3), last_error="timeout")  # падает недолго
    assert doctor.watch() == ["mail"]
    assert len(sent) == 1 and "пароль не подходит" in sent[0]
    assert store.scalar("SELECT COUNT(*) FROM notices WHERE kind='doctor'") == 1
    assert doctor.watch() == []  # сегодня уже писали
    # молчит без ошибок: пишем, только если PULSE работает дольше срока (после сна компьютера сбор догонит сам)
    job("collect:mail", last_ok=ago(hours=5))
    store.set_state("doctor:alert:mail:" + config.now().date().isoformat(), None)
    store.set_state("daemon_started", ago(minutes=10))
    assert doctor.watch() == []
    store.set_state("daemon_started", ago(hours=6))
    assert doctor.watch() == ["mail"]


def test_cli_doctor_exit_code(capsys, monkeypatch):
    from opsdash import cli

    monkeypatch.setattr(brain, "find_claude", lambda *a, **k: None)
    with pytest.raises(SystemExit) as e:
        cli.main(["doctor"])
    assert e.value.code == 1
    assert "✗ Помощник: Claude не установлен" in capsys.readouterr().out


def test_catalog_shows_health():
    from opsdash.web.app import app

    page = TestClient(app, base_url="http://localhost:8765").get("/settings?tab=connections").text
    assert 'id="conn-health-h">Здоровье<' in page and "Фоновые задания" in page and "Запустите PULSE" in page


# --- «другой сервер» --------------------------------------------------------------------------------------

def test_registry_schedules_new_sources(settings_with, monkeypatch):
    assert {"collect:imap", "collect:caldav", "collect:ics_calendar"} <= set(jobs.REGISTRY)
    assert jobs.TITLES["collect:imap"] == "Сбор: Почта по IMAP"
    started = []
    monkeypatch.setattr(jobs, "submit", lambda name, kind="light", args=None: started.append(name) or True)
    settings_with(imap={"enabled": True}, ics_calendar={"enabled": True})
    jobs.tick()
    assert "collect:imap" in started and "collect:ics_calendar" in started and "collect:caldav" not in started


def test_imap_login_and_port(monkeypatch, settings_with):
    seen = {}

    class FakeImap:
        def __init__(self, host, port, timeout=None):
            seen["addr"] = (host, port)

        def login(self, user, pwd):
            seen["login"] = (user, pwd)

        def list(self):
            return "OK", [b'(\\HasNoChildren) "/" "INBOX"']

        def logout(self):
            pass

    monkeypatch.setattr(mail.imaplib, "IMAP4_SSL", FakeImap)
    from opsdash.web.app import app

    client = TestClient(app, base_url="http://localhost:8765")
    client.post("/settings/imap", data={"email": "anna@company.com", "login": "anna", "host": "imaps://mail.company.com/",
                                        "port": "1993", "password": "secret", "enabled": "on", "folders": "INBOX"})
    assert config.load_settings()["imap"]["host"] == "mail.company.com"
    assert mail.check_imap() == "вход выполнен, папок: 1"
    assert seen == {"addr": ("mail.company.com", 1993), "login": ("anna", "secret")}
    page = client.get("/settings?tab=imap").text
    assert 'data-conn="imap"' in page and "IMAP-сервер" in page


def _feed(day):
    d = day.strftime("%Y%m%d")
    return ("BEGIN:VCALENDAR\r\nX-WR-CALNAME:Работа\r\n"
            f"BEGIN:VEVENT\r\nUID:o1\r\nSUMMARY:Созвон с партнёром\r\nDTSTART:{d}T070000Z\r\nDTEND:{d}T080000Z\r\nEND:VEVENT\r\n"
            "END:VCALENDAR\r\n").encode()


def test_ics_calendar_read_only_and_kept_by_other_collectors(monkeypatch, settings_with):
    from opsdash import proposals

    settings_with(ics_calendar={"enabled": True})
    config.set_secret("ICS_CALENDAR_URLS", "webcal://outlook.office365.com/owa/calendar/x/calendar.ics")
    day = (datetime.now(config.tz()) + timedelta(days=1)).date()
    asked = []

    def fake_get(url, **kw):
        asked.append(url)
        return httpx.Response(200, content=_feed(day), request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", fake_get)
    assert calendar.ics_collect() == "встреч в окне: 1"
    assert asked[0].startswith("https://outlook.office365.com/")  # webcal:// → https://
    ev = store.row("SELECT * FROM events")
    assert ev["uid"].startswith("ics:o1") and ev["calendar"] == "Работа"
    monkeypatch.setattr(calendar, "_calendars", lambda settings, section="calendar": [])
    calendar.collect()  # Яндекс чистит только своё
    config.set_secret("GOOGLE_CALENDAR_ICAL", "https://calendar.google.com/x.ics")
    calendar.google_collect()  # и Google — только своё
    assert store.scalar("SELECT COUNT(*) FROM events WHERE uid LIKE 'ics:%'") == 1
    with pytest.raises(proposals.ProposalError, match="только читает"):
        proposals.propose_calendar_cancel(uid=ev["uid"])


def test_caldav_needs_server(settings_with):
    settings_with(caldav={"login": "a@icloud.com"})
    config.set_secret("CALDAV_OTHER_PASSWORD", "p")
    with pytest.raises(calendar.CalendarError, match="адрес CalDAV-сервера"):
        calendar.caldav_check()
