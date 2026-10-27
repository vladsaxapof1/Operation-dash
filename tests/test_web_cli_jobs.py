import asyncio
import json
import os
import threading
import time
from datetime import datetime, timedelta
from types import SimpleNamespace
from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient

from opsdash import cli, config, demo, jobs, proposals, records, store


@pytest.fixture
def client():
    from opsdash.web.app import app

    return TestClient(app, base_url="http://localhost:8765")


def test_all_pages_render_with_demo(client):
    demo.seed()
    mid = store.row("SELECT id FROM meetings WHERE status='processed'")["id"]
    pages = ["/", "/inbox", "/commitments?direction=to_me", "/meetings", f"/meetings/{mid}",
             f"/meetings/{mid}?tab=transcript", "/tasks?who=Пётр Иванов", "/people", "/people?edit=1", "/journal",
             "/help", "/inbox?chat=telegram|-1001"] + [f"/settings?tab={t}" for t, _ in __import__(
                 "opsdash.web.settings_views", fromlist=["TABS"]).TABS]
    for url in pages:
        r = client.get(url)
        assert r.status_code == 200, url
    home = client.get("/").text
    assert "Ждут вашего «да»" in home and "Ирина" in home and "Запуск склада Казань" in home
    demo.clear()
    assert store.scalar("SELECT COUNT(*) FROM meetings") == 0
    assert store.scalar("SELECT COUNT(*) FROM proposals") == 0


def test_foreign_origin_and_host_rejected(client):
    assert client.post("/commitments", data={"direction": "mine", "what": "x"},
                       headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.post("/commitments", data={"direction": "mine", "what": "x"},
                       headers={"Origin": "null"}).status_code == 403
    assert client.get("/", headers={"Host": "evil.example"}).status_code == 403
    # с no-referrer браузер шлёт у своих же форм «Origin: null» — и все POST отклоняются
    assert client.get("/").headers["Referrer-Policy"] == "same-origin"
    assert client.get("/static/app.js").headers["Cache-Control"] == "no-cache"
    r = client.post("/commitments", data={"direction": "mine", "what": "Позвонить Олегу", "due": ""},
                    headers={"Origin": "http://localhost:8765"}, follow_redirects=False)
    assert r.status_code == 303
    assert records.list_commitments()[0]["what"] == "Позвонить Олегу"


def test_settings_forms_save(client):
    client.post("/settings/general", data={"name": "Анна", "timezone": "Asia/Yekaterinburg", "work_start": "10:00",
                                           "work_end": "19:00", "days": ["1", "2", "3"], "port": "8765"})
    s = config.load_settings()
    assert s["owner"]["name"] == "Анна" and s["owner"]["work_hours"]["days"] == [1, 2, 3]
    r = client.post("/settings/general", data={"timezone": "Mars/Base"}, follow_redirects=False)
    assert "err=" in r.headers["location"]
    client.post("/settings/mail", data={"email": "a@x.ru", "password": "abcd efgh", "enabled": "on",
                                        "folders": "INBOX\r\nРабота", "ignore_senders": "noreply"})
    assert config.get_secret("MAIL_APP_PASSWORD") == "abcdefgh"
    assert config.load_settings()["mail"]["folders"] == ["INBOX", "Работа"]
    client.post("/settings/mail", data={"email": "a@x.ru", "password": ""})  # пустой пароль — не трогаем
    assert config.get_secret("MAIL_APP_PASSWORD") == "abcdefgh"
    client.post("/settings/autonomy", data={"kaiten_create_for_me": "auto", "calendar": "bogus"})
    assert config.load_settings()["autonomy"]["kaiten_create_for_me"] == "auto"
    assert config.load_settings()["autonomy"]["calendar"] == "confirm"
    client.post("/settings/kaiten/targets/add", data={"name": "Продажи", "space_id": "20", "board_id": "200"})
    s = config.load_settings()["kaiten"]
    assert s["default_target"] == "Продажи" and s["targets"][0]["board_id"] == 200
    client.post("/settings/chats", data={"source": "telegram", "chat__42": "1"})  # несуществующий — без ошибки



class _FakeQR:
    """QR-вход Telethon: первый токен истекает, второй ждёт сканирования (gate) или требует пароль."""

    def __init__(self, result, recreate_error=None):
        self.result, self.n, self.gate, self.recreate_error = result, 1, threading.Event(), recreate_error

    @property
    def url(self):
        return f"tg://login?token=t{self.n}"

    async def wait(self):
        if self.n == 1:
            raise asyncio.TimeoutError
        while not self.gate.is_set():
            await asyncio.sleep(0.01)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    async def recreate(self):
        if self.recreate_error:
            raise self.recreate_error
        self.n += 1


class _FakeTelegramClient:
    def __init__(self, qr):
        self.qr, self.password = qr, None
        self.session = SimpleNamespace(save=lambda: "SESSION")

    async def connect(self):
        pass

    async def disconnect(self):
        pass

    async def qr_login(self):
        return self.qr

    async def sign_in(self, password=None):
        self.password = password


def _qr_setup(monkeypatch, result, recreate_error=None):
    from opsdash.sources import telegram

    fake = _FakeTelegramClient(_FakeQR(result, recreate_error))
    monkeypatch.setattr(telegram, "_client", lambda **k: fake)
    monkeypatch.setattr(telegram, "check", lambda *a, **k: None)
    monkeypatch.setattr(telegram, "refresh_dialogs", lambda *a, **k: 0)
    return fake


def _qr_wait(client, state, version=None):
    for _ in range(200):
        res = client.get("/settings/telegram/qr/status").json()
        if res["state"] == state and (version is None or res["version"] == version):
            return res
        time.sleep(0.01)
    raise AssertionError(f"QR-вход не дошёл до {state}: {res}")


def test_telegram_qr_login(client, monkeypatch):
    fake = _qr_setup(monkeypatch, SimpleNamespace(phone="79991234567"))
    client.post("/settings/telegram/qr")
    _qr_wait(client, "waiting", version=2)  # истёкший токен заменён
    assert 'qr.svg?v=2' in client.get("/settings?tab=telegram").text
    svg = client.get("/settings/telegram/qr.svg?v=2")
    assert svg.status_code == 200 and svg.headers["content-type"].startswith("image/svg+xml")

    fake.qr.gate.set()  # отсканировали
    _qr_wait(client, "ok")
    r = client.post("/settings/telegram/qr/done", follow_redirects=False)
    assert "msg=" in r.headers["location"]
    assert config.get_secret("TELEGRAM_SESSION") == "SESSION"
    tg = config.load_settings()["telegram"]
    assert tg["enabled"] and tg["phone"] == "+79991234567"
    assert client.get("/settings/telegram/qr/status").json()["state"] == ""
    assert client.get("/settings/telegram/qr.svg").status_code == 404


def test_telegram_qr_login_with_cloud_password(client, monkeypatch):
    from telethon.errors import SessionPasswordNeededError

    fake = _qr_setup(monkeypatch, SessionPasswordNeededError(None))
    client.post("/settings/telegram/qr")
    fake.qr.gate.set()
    _qr_wait(client, "password")
    assert "Облачный пароль Telegram" in client.get("/settings?tab=telegram").text
    client.post("/settings/telegram/password", data={"password": "секрет"})
    assert fake.password == "секрет"
    assert config.get_secret("TELEGRAM_SESSION") == "SESSION"


def test_telegram_qr_password_noticed_on_token_renewal(client, monkeypatch):
    # уведомление о скане потерялось — о пароле Telegram сообщает при выпуске нового токена
    from telethon.errors import SessionPasswordNeededError

    _qr_setup(monkeypatch, None, recreate_error=SessionPasswordNeededError(None))
    client.post("/settings/telegram/qr")
    _qr_wait(client, "password")


def test_chats_save_more_than_thousand_fields(client):
    store.execute("INSERT INTO chats (source, chat_id, title, kind, enabled) VALUES ('telegram', '7', 'Склад', 'group', 0)")
    data = {"source": "telegram", "chat__7": "1", **{f"chat__{i}": "0" for i in range(1000, 2500)}}
    r = client.post("/settings/chats", data=data, follow_redirects=False)
    assert r.status_code == 303 and "msg=" in r.headers["location"]
    assert store.scalar("SELECT enabled FROM chats WHERE chat_id='7'") == 1


def test_chat_list_shows_username(client):
    store.upsert_chat("telegram", "42", "Олег", "private", username="oleg")
    assert "@oleg" in client.get("/settings?tab=telegram").text


def test_chat_list_sorted_by_last_message_and_saved_instantly(client):
    store.upsert_chat("telegram", "1", "«Альфа» чат", "group", last_ts="2026-10-20T10:00:00+00:00")
    store.upsert_chat("telegram", "2", "Борис", "private", last_ts="2026-10-26T10:00:00+00:00")
    store.upsert_chat("telegram", "3", "Вера", "private")
    store.upsert_chat("telegram", "2", None, None, last_ts="2026-10-01T00:00:00+00:00")  # старее — не затирает
    page = client.get("/settings?tab=telegram").text
    assert "Telegram <span class=\"tab-note\">(личный аккаунт)</span>" in page and "(уведомления)" in page
    assert page.index("Борис") < page.index("«Альфа» чат") < page.index("Вера")  # свежие сверху, без сообщений — внизу
    assert 'data-kind="private">Личные <span class="muted" data-kind-count="private">2</span>' in page  # фильтр по типу
    assert 'data-kind="bot"' not in page.split("<table")[0]  # ботов нет — и кнопки «Боты» нет
    by_title = client.get("/settings?tab=telegram&sort=title").text
    assert by_title.index("«Альфа» чат") < by_title.index("Борис")  # кавычки в начале не мешают алфавиту
    r = client.post("/settings/chats", data={"source": "telegram", "chat__3": "-1", "ajax": "1"})
    assert r.json() == {"ok": True, "saved": 1} and store.scalar("SELECT enabled FROM chats WHERE chat_id='3'") == -1


@pytest.mark.skipif(os.name == "nt", reason="Claude Code внутри приложения Claude — только macOS")
def test_claude_from_desktop_app_and_login(client, monkeypatch, tmp_path):
    from opsdash import brain

    monkeypatch.setattr(brain.sys, "platform", "darwin")
    monkeypatch.setattr(brain.shutil, "which", lambda name: None)
    monkeypatch.setenv("HOME", str(tmp_path))
    root = tmp_path / "Library" / "Application Support" / "Claude" / "claude-code"

    def install(version, status):
        exe = root / version / "a93ded4b1b47" / "claude.app" / "Contents" / "MacOS" / "claude"
        exe.parent.mkdir(parents=True, exist_ok=True)
        exe.write_text(f"#!/bin/sh\n[ \"$1\" = auth ] && echo '{status}' && exit 0\necho '{version} (Claude Code)'\n")
        exe.chmod(0o755)

    install("2.1.99", '{"loggedIn": true}')
    install("2.1.308", '{"loggedIn": false, "authMethod": "none"}')
    assert "/2.1.308/" in brain.find_claude()  # новейшая версия, а не «99» > «308» по алфавиту
    with pytest.raises(brain.BrainError, match="вход не выполнен"):
        brain.check()
    assert "Войти в Claude" in client.get("/settings?tab=claude").text

    opened = []
    monkeypatch.setattr(brain.subprocess, "run", lambda cmd, **k: opened.append(cmd))
    client.post("/settings/claude/login", follow_redirects=False)
    script = config.DATA_DIR / "claude-login.command"
    assert opened == [["open", str(script)]]
    assert "/2.1.308/" in script.read_text() and "auth login" in script.read_text()
    monkeypatch.undo()

    monkeypatch.setattr(brain.shutil, "which", lambda name: None)
    monkeypatch.setenv("HOME", str(tmp_path))
    install("2.1.308", '{"loggedIn": true, "email": "anna@example.com"}')
    assert "anna@example.com" in brain.check()
    assert "вход: <b>anna@example.com</b>" in client.get("/settings?tab=claude").text


def test_sources_card_bot_and_claude(client, monkeypatch, settings_with):
    from opsdash import brain, overview

    settings_with(telegram_bot={"enabled": True, "owner_chat_id": "42"})
    monkeypatch.setattr(brain, "find_claude", lambda *a, **k: None)
    health = {h["key"]: h for h in overview.sources_health()}
    assert health["bot"]["status"] == "ok" and health["bot"]["note"] == "подключён"
    assert health["claude"]["status"] == "error" and health["claude"]["note"] == "не найден"

    monkeypatch.setattr(brain, "find_claude", lambda *a, **k: "/x/claude")
    monkeypatch.setattr(brain, "login_status", lambda *a, **k: {"loggedIn": False})
    home = client.get("/").text
    assert "нужен вход" in home and "подключён" in home and "ожидает" not in home
    monkeypatch.setattr(brain, "login_status", lambda *a, **k: {"loggedIn": True})
    assert {h["key"]: h for h in overview.sources_health()}["claude"]["status"] == "ok"


def test_saving_source_settings_collects_right_away(client, monkeypatch):
    submitted = []
    monkeypatch.setattr(jobs, "in_daemon", lambda: True)
    monkeypatch.setattr(jobs, "submit", lambda name, *a, **k: submitted.append(name) or True)
    client.post("/settings/calendar", data={"login": "a@x.ru", "password": "", "enabled": "on"})
    client.post("/settings/mail", data={"email": "a@x.ru", "password": ""})  # выключена — не собираем
    assert submitted == ["collect:calendar"]


def test_bot_feed_text_not_double_escaped():
    from opsdash.web.app import f_tg_html

    html = str(f_tg_html('❌ <b>Встреча</b>: {&quot;result&quot;: &quot;x&quot;} &lt;script&gt; <script>'))
    assert "<b>Встреча</b>" in html and '{&quot;result&quot;: &quot;x&quot;}' in html
    assert "&amp;quot;" not in html and "<script>" not in html


@pytest.mark.skipif(os.name == "nt", reason="поддельный claude — shell-скрипт")
def test_claude_error_shows_reason_not_raw_json(tmp_path, settings_with):
    from opsdash import brain

    fake = tmp_path / "claude"
    fake.write_text("#!/bin/sh\ncat >/dev/null\n"
                    "echo '{\"type\":\"result\",\"is_error\":true,\"result\":\"Not logged in · Please run /login\"}'\n"
                    "exit 1\n")
    fake.chmod(0o755)
    settings_with(claude={"path": str(fake)})
    with pytest.raises(brain.BrainError) as err:
        brain.run("привет")
    assert str(err.value) == "Claude: не выполнен вход — Настройки → Claude → «Войти в Claude»"


def test_calendar_week_and_new_meeting(client, monkeypatch, settings_with):
    from opsdash.sources import calendar

    settings_with(calendar={"enabled": True}, autonomy={"calendar": "confirm"})
    now = config.now()
    day = (now + timedelta(days=2)).date()
    tz = config.tz()
    for uid, title, start, end, all_day in [
        ("a", "Планёрка", datetime(day.year, day.month, day.day, 10, 0, tzinfo=tz), None, 0),
        ("b", "Отпуск", datetime(day.year, day.month, day.day, tzinfo=tz), None, 1),
    ]:
        end = end or start + timedelta(hours=1 if not all_day else 48)
        store.execute('INSERT INTO events(uid, calendar, title, start, "end", all_day, attendees) VALUES(?,?,?,?,?,?,?)',
                      (uid, "Мои события", title, store.to_utc_iso(start), store.to_utc_iso(end), all_day, "[]"))
    page = client.get("/calendar?view=list").text
    assert "Планёрка" in page and "10:00–11:00" in page and page.count("Отпуск") == 2  # два дня отпуска — в обоих днях
    week = client.get(f"/calendar?start={day.isoformat()}").text  # неделя сеткой — по умолчанию
    assert 'class="wk-col' in week and "Неделя вперёд" in week and 'id="ev-' in week
    assert week.count('class="ev ev-allday') == 1 or week.count('class="ev ev-allday') == 2  # на стыке недель — две полосы
    monday = day - timedelta(days=day.weekday())
    assert f'data-day="{monday.isoformat()}"' in week

    store.set_state("calendar_names", ["Мои события", "Не забыть"])
    store.set_state("google_calendar_names", ["Google: anna@gmail.com"])
    settings_with(calendar={"write_calendar": "Не забыть"}, google_calendar={"enabled": True})
    form = client.get("/calendar").text
    assert '<option value="Не забыть" selected>' in form and "<option disabled>Google: anna@gmail.com — только чтение" in form

    created = []
    monkeypatch.setattr(calendar, "create_event", lambda *a, **k: created.append(a) or created.append(k) or "uid-1@pult")
    r = client.post("/calendar/new", data={"title": "Склад", "day": day.isoformat(), "at": "15:30", "minutes": "45",
                                          "attendees": "ivanov@x.ru,\npetrova@x.ru", "description": "повестка",
                                          "calendar": "Мои события"},
                    follow_redirects=False)
    assert "msg=" in r.headers["location"], r.headers["location"]
    title, start, end, attendees, description = created[0]
    assert created[1] == {"calendar_name": "Мои события"}
    assert "Мои события" in unquote(r.headers["location"])
    assert title == "Склад" and attendees == ["ivanov@x.ru", "petrova@x.ru"] and description == "повестка"
    assert store.local(start, "%H:%M") == "15:30" and store.local(end, "%H:%M") == "16:15"
    assert proposals.list_proposals("done")[0]["source_ref"] == "manual"  # след в истории, без отдельного «да»

    r = client.post("/calendar/new", data={"title": "x", "day": day.isoformat(), "at": "10:00", "attendees": "не почта"},
                    follow_redirects=False)
    assert "err=" in r.headers["location"] and len(created) == 2


def test_google_calendar_settings_and_marker(client):
    from opsdash.sources import calendar

    r = client.post("/settings/google_calendar", data={"urls": "webcal://x", "enabled": "on"}, follow_redirects=False)
    assert "err=" in r.headers["location"] and not calendar.google_urls()
    client.post("/settings/google_calendar", data={"urls": "https://a.ics\nhttps://b.ics", "enabled": "on"})
    assert calendar.google_urls() == ["https://a.ics", "https://b.ics"] and config.load_settings()["google_calendar"]["enabled"]
    client.post("/settings/google_calendar", data={"urls": "", "enabled": "on"})  # пусто — адреса не трогаем
    assert len(calendar.google_urls()) == 2
    assert "сохранено адресов: 2" in client.get("/settings?tab=calendar").text
    client.post("/settings/google_calendar", data={"urls": "", "forget": "on"})
    assert calendar.google_urls() == []

    start = config.now().replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    calendar.save_event({"uid": "google:x", "calendar": "Google: anna@gmail.com", "title": "Созвон с банком",
                         "start": store.to_utc_iso(start), "end": store.to_utc_iso(start + timedelta(hours=1)),
                         "attendees": "[]"})
    page = client.get("/calendar?view=list").text
    assert "Созвон с банком" in page and 'title="Google: anna@gmail.com">Google</span>' in page
    week = client.get(f"/calendar?start={start.date().isoformat()}").text  # в неделе Google — своим цветом
    assert 'class="ev google' in week and "Google: anna@gmail.com" in week


def test_meeting_people_and_place_are_short():
    from opsdash.web.app import f_note, f_people, f_place

    att = [{"email": "c_1@resource.calendar.google.com", "name": "COMPANY-ZOOM-5-https://zoom.us/j/1 (100)"},
           {"email": "p.orlov@company.ru", "name": "p.orlov@company.ru"}, {"email": "p@x.ru", "name": "Пётр"},
           *({"email": f"u{i}@x.ru", "name": ""} for i in range(4))]
    assert f_people(att) == "p.orlov, Пётр, u0 и ещё 3"
    assert f_people([]) == ""
    place = str(f_place('ZOOM-5-https://us06web.zoom.us/j/37?pwd=a"b (100) <script>'))
    assert '<a href="https://us06web.zoom.us/j/37?pwd=a" target="_blank">us06web.zoom.us</a>' in place
    assert "&lt;script&gt;" in place and "<script>" not in place
    invite = "Pavel Orlov приглашает вас на конференцию Zoom. " * 3 + "https://us06web.zoom.us/j/5 https://us06web.zoom.us/j/6"
    assert str(f_place(invite)) == '<a href="https://us06web.zoom.us/j/5" target="_blank">us06web.zoom.us</a>'
    assert str(f_place("Переговорная " * 20)).endswith("…") and len(str(f_place("Переговорная " * 20))) <= 81
    note = str(f_note('Привет!<br>Zoom: <a href="https://zoom.us/j/1">https://zoom.us/j/1</a><br><b>Код</b>: 1 &lt;x&gt;'))
    assert "<br>" not in note and "Привет!\nZoom: " in note and '>zoom.us</a>' in note and "&lt;x&gt;" in note


def test_calendar_refresh_button(client, monkeypatch, settings_with):
    from opsdash.sources import calendar

    page = client.get("/calendar").text
    assert "/calendar/refresh" not in page  # календари не подключены — кнопки нет
    r = client.post("/calendar/refresh", follow_redirects=False)
    assert "err=" in r.headers["location"]

    settings_with(calendar={"enabled": True}, google_calendar={"enabled": True})
    monkeypatch.setattr(calendar, "collect", lambda *a, **k: "встреч в окне: 3")

    def broken(*a, **k):
        raise calendar.CalendarError("адрес не работает")

    monkeypatch.setattr(calendar, "google_collect", broken)
    assert "↻ Обновить" in client.get("/calendar").text
    r = client.post("/calendar/refresh", headers={"Referer": "http://localhost:8765/calendar?view=list"},
                    follow_redirects=False)
    where = unquote(r.headers["location"])
    assert where.startswith("/calendar?view=list") and "Яндекс: встреч в окне: 3" in where and "Google: адрес не работает" in where
    jobs_rows = {j["name"]: j for j in store.rows("SELECT * FROM jobs")}
    assert jobs_rows["collect:calendar"]["last_ok"] and jobs_rows["collect:google_calendar"]["last_error"]
    assert "обновлён" not in client.get("/calendar").text  # Google ещё ни разу не обновился — не делаем вид, что всё свежее


def test_tasks_board_and_list(client):
    today = records.today_local()
    late = records.add_commitment(direction="mine", what="Собрать сценарий", whom="Тимур",
                                  due=(today - timedelta(days=2)).isoformat(), source_type="meeting", source_ref="m1",
                                  quote="[00:02:43] 15 минут")
    records.add_commitment(direction="to_me", what="Прислать прогноз", who="Пётр", due=today.isoformat())
    records.add_commitment(direction="other", what="Согласовать бюджет", who="Олег", whom="Ирина")
    done = records.add_commitment(direction="mine", what="Отправить отчёт", whom="Анна")
    records.update_commitment(done, status="done")
    old = records.add_commitment(direction="mine", what="Старое дело", whom="Анна")
    records.update_commitment(old, status="done")
    store.execute("UPDATE commitments SET closed_at='2026-01-01T00:00:00+00:00' WHERE id=?", (old,))
    gone = records.add_commitment(direction="mine", what="Отменённое", whom="Анна")
    records.update_commitment(gone, status="cancelled")
    store.execute("INSERT INTO meetings(id, title, status) VALUES('m1', 'Анна / Тимур', 'processed')")

    board = records.commitments_board()
    assert [c["what"] for c in board["mine"]] == ["Собрать сценарий"] and board["mine"][0]["overdue"]
    assert board["to_me"][0]["due_today"] and [c["what"] for c in board["done"]] == ["Отправить отчёт"]

    page = client.get("/commitments").text  # доска — вид по умолчанию
    assert "Задачи и договорённости" in page and 'class="kb"' in page
    assert "просрочено на 2 дня" in page and "осталось " in page and "без срока" in page  # срок и сколько осталось
    assert "<b>Я</b> → Тимур" in page and "<b>Пётр</b> → мне" in page and "<b>Олег</b> → Ирина" in page
    assert ">Анна / Тимур</a>" in page and "[00:02:43] 15 минут" in page
    assert "Старое дело" not in page and "Отменённое" not in page
    assert "Прислать прогноз" not in client.get("/commitments?q=сценарий").text
    assert "Согласовать бюджет" in client.get("/commitments?person=Ирина").text
    listing = client.get("/commitments?view=list&direction=mine").text
    assert "Собрать сценарий" in listing and "Прислать прогноз" not in listing and "моя задача" in listing
    assert '<option value="to_me" selected>Мне должны</option>' in client.get("/commitments?add=to_me").text

    # фильтры по сроку и дате создания
    due_today = client.get("/commitments?due=today").text
    assert "Прислать прогноз" in due_today and "Собрать сценарий" not in due_today
    assert "Собрать сценарий" in client.get("/commitments?due=overdue").text
    no_due = client.get("/commitments?view=list&due=none").text
    assert "Согласовать бюджет" in no_due and "Прислать прогноз" not in no_due
    tomorrow = (today + timedelta(days=1)).isoformat()
    assert "Собрать сценарий" not in client.get(f"/commitments?created=custom&created_from={tomorrow}").text
    assert "Собрать сценарий" in client.get("/commitments?created=week").text

    client.cookies.set("kb_folded", "done.other")  # свёрнутые колонки помнит браузер
    folded = client.get("/commitments").text
    assert 'class="kb-col kb-done folded"' in folded and 'class="kb-col kb-mine"' in folded
    client.cookies.clear()

    client.post(f"/commitments/{late}", data={"due": (today + timedelta(days=3)).isoformat()})
    assert not records.commitments_board()["mine"][0]["overdue"]


def test_kaiten_tasks_boards_view(client, settings_with):
    settings_with(kaiten={"enabled": True, "domain": "company.kaiten.ru", "stale_days": 3})
    store.set_state("kaiten_me", {"id": 100500, "name": "Anna Smirnova"})
    old, fresh = "2026-06-26T14:29:39.888Z", store.utcnow()
    store.execute("INSERT INTO kaiten_boards(id, space_id, title, columns) VALUES(?,?,?,?)",
                  (11, 1, "Аналитика", json.dumps([{"id": 102, "title": "Ключевое", "sort_order": 1},
                                                   {"id": 101, "title": "В работе", "sort_order": 2}])))
    for cid, board, col, col_title, state, resp_id, resp, moved, title in [
        (1, 11, 101, "В работе", 2, 100500, "Anna Smirnova", old, "Распределение работ"),
        (2, 11, 102, "Ключевое", 2, 100500, "Anna Smirnova", fresh, "Бюджет 2027"),
        (3, 12, 201, "Беклог", 1, None, None, fresh, "Спешиал ЧЛ"),
    ]:
        store.execute(
            "INSERT INTO kaiten_cards(id, space_id, space_title, board_id, board_title, column_id, column_title, title, state,"
            " condition, responsible_id, responsible_name, owner_name, last_moved_at, column_changed_at, url) "
            "VALUES(?,1,'Продажи',?,?,?,?,?,?,1,?,?,'Ольга Белова',?,?,?)",
            (cid, board, "Аналитика" if board == 11 else "Качество", col, col_title, title, state, resp_id, resp,
             moved, moved, f"https://company.kaiten.ru/space/1/card/{cid}"))

    page = client.get("/tasks").text  # доски — вид по умолчанию
    assert "<h2>Аналитика</h2>" in page and "<h2>Качество</h2>" in page
    assert page.index("Ключевое") < page.index(">В работе<")  # колонки — в порядке Kaiten
    assert page.count('<span class="chip ok">вы</span>') == 2 and "дн. без движения" in page
    assert '<span class="avatar" title="Anna Smirnova">AS</span>' in page
    assert "без исполнителя · автор Ольга Белова" in page
    assert 'href="https://company.kaiten.ru/space/1"' in page
    stale = client.get("/tasks?only=stale").text
    assert "Распределение работ" in stale and "Бюджет 2027" not in stale
    mine = client.get("/tasks?mine=1").text
    assert "Спешиал ЧЛ" not in mine and "Бюджет 2027" in mine
    listing = client.get("/tasks?view=list").text
    assert "<table" in listing and "в очереди" in listing and "Спешиал ЧЛ" in listing


def test_busy_dashboard_is_not_reported_as_stopped(monkeypatch, capsys):
    import httpx

    from opsdash import daemon

    def slow(*a, **k):
        raise httpx.ReadTimeout("занят")

    def down(*a, **k):
        raise httpx.ConnectError("нет")

    monkeypatch.setattr(httpx, "get", slow)
    assert daemon.is_running()  # отвечает медленно (идёт расшифровка), но работает
    monkeypatch.setattr(httpx, "get", down)
    assert not daemon.is_running()
    monkeypatch.setattr(httpx, "post", slow)
    cli.main(["stop"])
    assert "занят" in capsys.readouterr().out
    monkeypatch.setattr(httpx, "post", down)
    cli.main(["stop"])
    assert "не запущен" in capsys.readouterr().out

def test_upload_text_transcript_creates_meeting(client, monkeypatch):
    submitted = []
    monkeypatch.setattr(jobs, "submit", lambda *a, **k: submitted.append(a) or True)
    r = client.post("/meetings/upload", files={"file": ("итоги.txt", "Пётр: сделаю к пятнице".encode(), "text/plain")},
                    data={"title": "Созвон"}, follow_redirects=False)
    assert r.status_code == 303 and "/meetings/" in r.headers["location"]
    assert submitted and submitted[0][0] == "transcribe"
    bad = client.post("/meetings/upload", files={"file": ("x.exe", b"MZ", "application/octet-stream")}, follow_redirects=False)
    assert "err=" in bad.headers["location"]


def test_cli_commitments_messages_and_propose(capsys):
    cli.main(["commitments", "add", "--direction", "to_me", "--who", "Пётр", "--what", "Прогноз", "--due", "2026-10-30",
              "--source", "meeting:m1"])
    cli.main(["commitments", "list", "--person", "пётр"])
    out = capsys.readouterr().out
    assert "Прогноз" in out and "meeting:m1" in out
    cli.main(["commitments", "list", "--json"])
    assert '"what": "Прогноз"' in capsys.readouterr().out

    for i in range(3):
        store.add_message({"source": "telegram", "chat_id": "1", "chat_title": "Склад", "msg_id": str(i),
                           "sender_name": "Дима", "text": f"Сообщение {i}", "ts": f"2026-10-25T0{i}:00:00+00:00"})
    cli.main(["messages", "pending", "--limit", "2"])
    out = capsys.readouterr().out
    assert "Новых сообщений: 3, показано: 2" in out and "Сообщение 2" not in out
    cli.main(["messages", "done", "--shown"])
    assert store.scalar("SELECT COUNT(*) FROM messages WHERE processed=0") == 1
    cli.main(["messages", "search", "сообщение"])
    assert "Сообщение 1" in capsys.readouterr().out

    with pytest.raises(SystemExit):
        cli.main(["propose", "card", "--title", "x"])
    assert "Куда создавать" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        cli.main(["propose", "meeting", "--title", "x", "--start", "2026-10-27T15:00", "--end", "2026-10-27T14:00"])


def test_cli_propose_meeting_local_time():
    p = proposals.propose_calendar_event(title="Созвон", start="2026-10-27T15:00", end="2026-10-27T15:30",
                                         attendees=["petr@x.ru"])
    assert p["payload"]["start"].startswith("2026-10-27T15:00") and "+03:00" in p["payload"]["start"]
    assert "27.10 15:00–15:30" in p["title"]


def test_scheduler_daily_and_due(monkeypatch, settings_with):
    settings = settings_with(schedule={"digest_morning": "09:00"})
    monday_930 = datetime(2026, 10, 26, 9, 30, tzinfo=config.tz())
    monkeypatch.setattr(config, "now", lambda s=None: monday_930)
    assert jobs._daily("digest_morning", "09:00", settings)
    assert not jobs._daily("digest_morning", "09:00", settings), "второй раз в тот же день — нет"
    assert not jobs._daily("late", "04:00", settings), "больше 4 часов назад — пропускаем"
    assert jobs.in_work_hours(settings, monday_930)
    sunday = datetime(2026, 10, 25, 12, 0, tzinfo=config.tz())
    assert not jobs.in_work_hours(settings, sunday)
    monkeypatch.setattr(config, "now", lambda s=None: sunday)
    assert not jobs._daily("digest_evening", "11:00", settings)
    assert jobs._due("collect:mail", 15)
    store.execute("INSERT INTO jobs(name, last_run) VALUES('collect:mail', ?)", (store.utcnow(),))
    assert not jobs._due("collect:mail", 15)


def test_tick_submits_enabled_sources(monkeypatch, settings_with):
    settings_with(mail={"enabled": True}, schedule={"digest_morning": "", "digest_evening": "", "stale_check": ""})
    submitted = []
    monkeypatch.setattr(jobs, "submit", lambda name, kind="light", args=None: submitted.append(name) or True)
    started = jobs.tick()
    assert "collect:mail" in started and "collect:telegram" not in started and "recordings" in started
    assert "triage" not in started  # нет новых сообщений


def test_run_job_records_errors():
    with pytest.raises(Exception):
        jobs.run_job("collect:kaiten")  # Kaiten не настроен
    j = store.row("SELECT * FROM jobs WHERE name='collect:kaiten'")
    assert j["last_error"] and not j["running"]
    assert store.row("SELECT * FROM log WHERE job='collect:kaiten' AND level='error'")
