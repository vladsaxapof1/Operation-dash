"""Первый день: мастер первого запуска (ONB-2) и «Первые находки» (ONB-3)."""

import pytest
from starlette.testclient import TestClient

from opsdash import brain, codex, config, jobs, store, welcome

REF = "http://localhost:8765"


@pytest.fixture(autouse=True)
def assistants(monkeypatch):
    monkeypatch.setattr(brain, "find_claude", lambda *a, **k: "/x/claude")
    monkeypatch.setattr(brain, "login_status", lambda *a, **k: {"loggedIn": True, "email": "anna@x.ru"})
    monkeypatch.setattr(codex, "find", lambda *a, **k: None)


@pytest.fixture
def client():
    from opsdash.web.app import app

    return TestClient(app, base_url=REF)


def status(cid):
    return {c["id"]: c["status"] for c in welcome.chapters()}[cid]


def test_fresh_install_opens_wizard_once(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/welcome"
    page = client.get("/welcome").text
    assert "Первый запуск" in page and "Как вас зовут" in page and "Глава 1 из 8" in page
    client.post("/welcome/dismiss")
    assert client.get("/", follow_redirects=False).status_code == 200  # закрыли — Сводка открывается


def test_about_then_assistant(client):
    assert status("about") == "todo"
    r = client.post("/welcome/about", data={"name": "Анна", "timezone": "Europe/Berlin", "work_start": "09:00",
                                            "work_end": "18:00", "days": ["1", "2"]}, follow_redirects=False)
    assert r.headers["location"] == "/welcome?step=assistant"
    s = config.load_settings()["owner"]
    assert s["name"] == "Анна" and s["timezone"] == "Europe/Berlin" and s["work_hours"]["days"] == [1, 2]
    assert status("about") == "done" and status("assistant") == "done" and welcome.current() == "notifications"
    assert client.get("/", follow_redirects=False).status_code == 200  # имя есть — мастер сам не открывается


def test_assistant_needs_login(client, monkeypatch):
    monkeypatch.setattr(brain, "login_status", lambda *a, **k: {"loggedIn": False})
    page = client.get("/welcome?step=assistant").text
    assert 'action="/settings/claude/login"' in page and "Войти в Claude" in page
    assert status("assistant") == "todo"


def test_source_form_returns_to_wizard(client):
    page = client.get("/welcome?step=messages&open=gmail").text
    assert "Адрес Gmail" in page and "is-picked" in page
    r = client.post("/settings/gmail", data={"email": "anna@gmail.com", "password": "abcd efgh", "enabled": "on"},
                    headers={"referer": REF + "/welcome?step=messages&open=gmail"}, follow_redirects=False)
    assert r.headers["location"].startswith("/welcome?step=messages&open=gmail&msg=")
    assert status("messages") == "done"  # Gmail включён и настроен — глава сделана
    # вход в Telegram с облачным паролем: нужный параметр переносится, вкладка — нет
    from opsdash.web.app import back

    class Req:
        headers = {"referer": REF + "/welcome?step=messages&open=telegram"}

    loc = back(Req(), "x", to="/settings?tab=telegram&need=password").headers["location"]
    assert loc.startswith("/welcome?step=messages&open=telegram&need=password&msg=")


def test_skip_and_back(client):
    r = client.post("/welcome/go", data={"step": "tasks", "action": "skip"}, follow_redirects=False)
    assert r.headers["location"] == "/welcome?step=recordings" and status("tasks") == "skipped"
    assert "пропущено" in client.get("/welcome?step=recordings").text
    client.post("/welcome/go", data={"step": "recordings", "action": "next"})
    assert status("recordings") == "done"
    r = client.post("/welcome/go", data={"step": "recordings", "action": "back"}, follow_redirects=False)
    assert r.headers["location"] == "/welcome?step=tasks"


def test_finish_starts_first_finds(client, monkeypatch):
    queued = []
    monkeypatch.setattr(jobs, "submit", lambda name, kind="light", args=None: queued.append(name) or True)
    page = client.get("/welcome?step=done").text
    assert "Первый разбор" in page and "за 5 дней" in page
    r = client.post("/welcome/finish", follow_redirects=False)
    assert r.headers["location"].startswith("/?msg=") and queued == ["first-finds"]
    assert store.get_state("first_finds")["status"] == "queued"
    home = client.get("/").text
    assert "PULSE читает вашу переписку за 5 дней" in home and "data-first-run" in home
    assert client.get("/welcome/status").json() == {"status": "queued"}


def test_finish_without_assistant(client, monkeypatch):
    monkeypatch.setattr(brain, "find_claude", lambda *a, **k: None)
    r = client.post("/welcome/finish", follow_redirects=False)
    assert "err=" in r.headers["location"] and store.get_state("first_finds") is None


def test_first_finds_job_and_dialog(client, monkeypatch, settings_with):
    from opsdash import bot

    sent = []
    monkeypatch.setattr(bot, "notify", lambda text, **kw: sent.append(text))
    settings_with(owner={"name": "Анна"}, telegram_bot={"enabled": True, "owner_chat_id": "1"})

    def fake_triage(force=False, **kw):
        assert force  # первый разбор — сразу, не дожидаясь рабочего времени
        now = store.utcnow()
        store.execute("INSERT INTO messages(source, chat_id, msg_id, text, ts) VALUES('telegram','1','m1','x',?)", (now,))
        store.execute("INSERT INTO attention(source, chat_title, sender_name, summary, status, created) "
                      "VALUES('telegram','Склад','Пётр','Ждёт согласования счёта','open',?)", (now,))
        store.execute("INSERT INTO commitments(direction, who, whom, what, title, status, created) "
                      "VALUES('mine','Анна','Пётр','Прислать прогноз','Прогноз по складу','open',?)", (now,))
        return "разобрано: 1"

    monkeypatch.setattr(jobs, "job_triage", fake_triage)
    assert jobs.run_job("first-finds") == "ждут ответа: 1, ваших задач: 1, вам должны: 0"
    st = store.get_state("first_finds")
    assert st["status"] == "done" and st["found"]["mine_top"][0]["title"] == "Прогноз по складу"
    assert sent and "за 5 дней" in sent[0] and "Ждут вашего ответа: 1" in sent[0]
    home = client.get("/").text
    assert "Что PULSE уже нашёл" in home and "Ждёт согласования счёта" in home and "data-first-finds" in home
    client.post("/welcome/seen")
    assert "Что PULSE уже нашёл" not in client.get("/").text


def test_first_collect_is_five_days():
    s = config.DEFAULTS
    assert s["telegram"]["backfill_days"] == s["mail"]["backfill_days"] == s["gmail"]["backfill_days"] == 5
    from opsdash.sources import pachca

    assert pachca.BACKFILL_DAYS == 5
