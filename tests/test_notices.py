"""Разбор сообщений по накоплению и уведомления в углу экрана: что PULSE сделал и что делает сейчас."""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from opsdash import jobs, notices, records, store


@pytest.fixture
def client():
    from opsdash.web.app import app

    return TestClient(app, base_url="http://localhost:8765")


def _messages(n):
    for i in range(n):
        store.add_message({"source": "telegram", "chat_id": "7", "chat_title": "Продажи", "msg_id": str(i),
                           "sender_name": "Пётр", "text": f"Сообщение {i}", "ts": store.utcnow()})


def _last_triage(minutes_ago):
    at = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).replace(microsecond=0).isoformat()
    store.execute("INSERT INTO jobs(name, last_run) VALUES('triage', ?) ON CONFLICT(name) DO UPDATE SET last_run=?",
                  (at, at))


@pytest.mark.parametrize("unread, minutes_ago, batch, work, runs", [
    (60, 20, 50, True, True),     # накопилось 60 ≥ 50 — не ждём таймера (45 мин)
    (30, 20, 50, True, False),    # 30 — меньше порога, таймер ещё не прошёл
    (60, 5, 50, True, False),     # только что разбирали — пауза 10 минут, чтобы не по кругу
    (60, 20, 0, True, False),     # 0 — только по таймеру
    (5, 50, 50, True, True),      # таймер прошёл — разбираем сколько есть
    (60, 20, 50, False, False),   # не рабочее время
])
def test_triage_runs_by_timer_or_when_messages_pile_up(monkeypatch, settings_with, unread, minutes_ago, batch, work, runs):
    settings_with(schedule={"triage_minutes": 45, "triage_batch": batch, "digest_morning": "", "digest_evening": "",
                            "stale_check": ""})
    monkeypatch.setattr(jobs, "in_work_hours", lambda settings, at=None: work)
    submitted = []
    monkeypatch.setattr(jobs, "submit", lambda name, kind="light", args=None: submitted.append(name) or True)
    _messages(unread)
    _last_triage(minutes_ago)
    jobs.tick()
    assert ("triage" in submitted) is runs


def test_triage_says_what_it_found_and_notices(monkeypatch, settings_with):
    _messages(3)
    monkeypatch.setattr(jobs, "in_work_hours", lambda settings, at=None: True)

    def claude(prompt, **_):
        store.execute("INSERT INTO attention(summary, status) VALUES('Пётр ждёт ответа', 'open')")
        records.add_commitment(direction="to_me", what="Прислать прогноз", who="Пётр")
        records.add_commitment(direction="mine", what="Ответить Петру")
        store.execute("UPDATE messages SET processed=1")

    monkeypatch.setattr(jobs.brain, "run", claude)
    assert jobs.run_job("triage") == "разобрано 3 из 3 · нужен ответ: +1 · задач: +2"
    [n] = notices.recent()
    assert n["title"] == "Разобрал новые сообщения" and n["text"].startswith("разобрано 3 из 3")
    assert n["link"] == "/commitments?new=1"  # задач прибавилось больше, чем «Нужен ответ» — ведём в задачи


@pytest.mark.parametrize("result, link", [
    ("разобрано 5 из 5 · задач: +2", "/commitments?new=1"),                          # ответов не нужно — в задачи
    ("разобрано 5 из 5 · нужен ответ: +1 · задач: +3", "/commitments?new=1"),
    ("разобрано 5 из 5 · нужен ответ: +3 · задач: +1", "/inbox"),                    # где больше плюсов
    ("разобрано 5 из 5 · нужен ответ: +2 · задач: +2", "/commitments?new=1"),        # поровну — в задачи
    ("разобрано 5 из 5 · нужен ответ: +1 · на подтверждение: +4", "/inbox"),
    ("разобрано 5 из 5 · на подтверждение: +2", "/"),                                # только предложения — на сводку
    ("разобрано 5 из 5", "/inbox"),
])
def test_triage_notice_leads_where_most_was_added(settings_with, result, link):
    notices.from_job("triage", {}, result, None)
    assert notices.recent()[0]["link"] == link


def test_notices_from_jobs(settings_with):
    store.execute("INSERT INTO meetings(id, title, headline, status) VALUES('m1', 'Планёрка', 'Склад — в пятницу', 'transcribed')")
    notices.from_job("transcribe", {"meeting_id": "m1"}, "Планёрка: 42 мин", None)
    notices.from_job("process-meeting", {"meeting_id": "m1"}, "готово", None)
    notices.from_job("collect:telegram", {}, "новых сообщений: 4", None)  # сбор идёт постоянно — не новость
    notices.from_job("triage", {}, "новых сообщений нет", None)
    notices.from_job("transcribe", {"meeting_id": "m1"}, "остановлено: расшифровку отменили", None)  # нажали сами
    notices.from_job("transcribe", {"meeting_id": "m1"}, "остановлено: запись a.mov удалили из папки", None)
    titles = [(n["title"], n["text"], n["link"]) for n in notices.recent()]
    assert titles == [("Остановил расшифровку", "запись a.mov удалили из папки", "/meetings"),
                      ("Разобрал встречу «Планёрка»", "Склад — в пятницу", "/meetings/m1"),
                      ("Расшифровал встречу «Планёрка»", "42 мин", "/meetings/m1")]


def test_repeated_error_updates_one_notice(settings_with):
    notices.from_job("collect:telegram", {}, None, "сессия устарела")
    notices.from_job("collect:telegram", {}, None, "сессия устарела\nподробности")
    [n] = notices.recent()
    assert n["title"] == "Не получилось: Сбор: Telegram" and n["level"] == "bad" and n["link"] == "/journal"


def test_notices_api_and_corner(client, settings_with):
    first = notices.add("triage", "Разобрал новые сообщения", "разобрано 5 из 5", "/inbox", "ok")
    second = notices.add("digest", "Утренний дайджест отправлен", link="/digests")
    page = client.get("/").text
    assert 'data-notices data-last="%d"' % second in page and "Разобрал новые сообщения" in page and "Скрыть все" in page
    assert [n["id"] for n in client.get(f"/api/notices?after={first}").json()["items"]] == [second]
    client.post(f"/api/notices/{second}/dismiss")
    assert [n["id"] for n in notices.recent()] == [first]
    client.post("/api/notices/dismiss-all")
    assert notices.recent() == [] and 'data-last="0"' in client.get("/").text

    _messages(12)
    store.execute("INSERT INTO jobs(name, last_run, running) VALUES('triage', ?, 1)", (store.utcnow(),))
    running = client.get("/api/notices").json()["running"]
    assert running == [{"name": "triage", "title": "Разбор сообщений (Claude)", "detail": "12 новых"}]
    assert "Разбор сообщений (Claude) — 12 новых" in client.get("/").text


def test_schedule_setting_saved(client, settings_with):
    from opsdash import config

    client.post("/settings/schedule", data={"collect_minutes": "15", "triage_minutes": "45", "triage_batch": "30"})
    assert config.load_settings()["schedule"]["triage_batch"] == 30
    assert 'name="triage_batch" value="30"' in client.get("/settings?tab=schedule").text
