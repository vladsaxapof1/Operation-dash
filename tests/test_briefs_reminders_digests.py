"""Брифы к встречам, напоминания коллегам, загрузка людей, текст итогов встречи и раздел «Дайджесты»."""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from opsdash import brain, briefs, cli, config, digests, jobs, proposals, records, store


@pytest.fixture
def client():
    from opsdash.web.app import app

    return TestClient(app, base_url="http://localhost:8765")


def _event(uid: str, title: str, hours: float, description: str = "", minutes: int = 60, attendees=None) -> None:
    start = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(hours=hours)
    store.execute('INSERT INTO events(uid, calendar, title, start, "end", all_day, description, attendees) '
                  'VALUES(?,?,?,?,?,0,?,?)', (uid, "Работа", title, start.isoformat(),
                                             (start + timedelta(minutes=minutes)).isoformat(), description,
                                             store.json_col(attendees or [])))


def test_briefs_pending_skip_save_and_stale():
    _event("ev-1", "Обзор продаж", 3, attendees=[{"email": "petr@example.com", "name": "petr@example.com"}])
    _event("ev-2", "тайм-аут", 4)
    _event("ev-3", "(без названия)", 5)
    _event("ev-far", "Планирование", 100)
    assert [e["uid"] for e in briefs.pending()] == ["ev-1"]  # занятое время, без названия и далёкое — не берём
    assert briefs.get("ev-2")["status"] == "skipped"

    path = config.DATA_DIR / "briefs" / "ev1.md"
    path.write_text("**Тема:** обзор\n\n## Вопросы к встрече\n1. Что с планом?", encoding="utf-8")
    cli.main(["briefs", "set", "ev-1", "--file", str(path), "--topic", "Обзор продаж за неделю"])
    assert briefs.pending() == []
    events = briefs.for_events(store.rows("SELECT * FROM events WHERE uid='ev-1'"))
    assert events[0]["brief"]["status"] == "ready" and not events[0]["brief_stale"]

    store.execute("UPDATE events SET description='Новая повестка' WHERE uid='ev-1'")  # встреча изменилась
    assert briefs.for_events(store.rows("SELECT * FROM events WHERE uid='ev-1'"))[0]["brief_stale"]
    assert [e["uid"] for e in briefs.pending()] == ["ev-1"]


def test_brief_context_and_job(monkeypatch):
    records.upsert_person("Пётр Иванов", email="petr@example.com")
    _event("ev-1", "Обзор продаж", 3, attendees=[{"email": "petr@example.com", "name": "petr@example.com"}])
    store.execute("INSERT INTO meetings(id, title, status, started_at, headline) "
                  "VALUES('m1', 'Обзор продаж за неделю', 'processed', '2026-10-20T10:00:00+00:00', 'План 74%')")
    records.add_commitment(direction="to_me", who="Пётр Иванов", what="Прислать прогноз", source_type="meeting")
    text = briefs.context("ev-1")
    assert "Пётр Иванов" in text and "m1" in text and "Прислать прогноз" in text and "data/briefs/" in text

    def fake_run(prompt, **kw):  # Claude пишет бриф к первой встрече и не справляется со второй
        briefs.save("ev-1", "Бриф: спросить про прогноз", topic="Продажи")
        return {"text": "ok"}

    _event("ev-2", "Созвон", 6)
    monkeypatch.setattr(brain, "run", fake_run)
    assert jobs.job_briefs() == "брифов готово: 1 из 2"
    assert briefs.get("ev-2")["status"] == "failed", "не собранный бриф не запускается по кругу"
    assert briefs.pending() == []


def test_reminder_recipient_text_and_send(monkeypatch, settings_with):
    records.upsert_person("Пётр Иванов", telegram="@petr")
    today = records.today_local()
    cid = records.add_commitment(direction="to_me", who="Пётр Иванов", title="Прогноз по клиентам",
                                 what="Прислать прогноз", due=today.isoformat(), source_type="meeting")
    later = records.add_commitment(direction="to_me", who="Пётр Иванов", what="Отчёт за квартал",
                                   due=(today + timedelta(days=5)).isoformat())
    items = records.reminders()
    assert [c["id"] for c in items] == [cid], "напоминаем только по срокам сегодня, завтра и просроченным"
    r = items[0]["remind"]
    assert r["to"] == {"target": "@petr", "link": "https://t.me/petr", "label": "Пётр Иванов"}
    assert r["text"] == "Пётр, привет! Напоминаю про «Прогноз по клиентам» (договаривались на сегодня). Как продвигается?"

    settings_with(reminders={"template": "{имя}, добрый день! «{задача}» — {срок}"})
    assert records.reminder_text(records.get_commitment(later)).endswith(f"до {(today + timedelta(days=5)):%d.%m}")

    sent = []
    from opsdash.sources import telegram

    monkeypatch.setattr(telegram, "send", lambda target, text, settings=None: sent.append((target, text)) or 1)
    p = proposals.propose_reminder(commitment_id=cid, text="Пётр, как прогноз?")
    assert p["status"] == "pending", "от имени директора — только после «Да», автономии нет"
    done = proposals.approve(p["id"])
    assert done["status"] == "done" and sent == [("@petr", "Пётр, как прогноз?")]
    assert records.get_commitment(cid)["reminded_at"] and records.reminders() == []

    mine = records.add_commitment(direction="mine", whom="Пётр Иванов", what="Своё")
    with pytest.raises(proposals.ProposalError):
        proposals.propose_reminder(commitment_id=mine)
    stranger = records.add_commitment(direction="to_me", who="Незнакомец", what="Что-то", due=today.isoformat())
    with pytest.raises(proposals.ProposalError):
        proposals.propose_reminder(commitment_id=stranger)  # чата нет — только «Копировать»


def test_reminder_finds_private_chat_by_name():
    store.upsert_chat("telegram", "555", "Ольга Смирнова", "private")
    cid = records.add_commitment(direction="to_me", who="Ольга", what="Прислать договор")
    assert records.recipient(records.get_commitment(cid)) == {"target": "555", "link": "tg://user?id=555",
                                                              "label": "Ольга"}


def test_workload_counts_people_and_me():
    records.upsert_person("Пётр Иванов", aliases=["Петя"])
    yesterday = (records.today_local() - timedelta(days=1)).isoformat()
    records.add_commitment(direction="to_me", who="Пётр Иванов", what="Прогноз", due=yesterday)
    records.add_commitment(direction="other", who="Петя", whom="Анна", what="Бюджет")
    records.add_commitment(direction="mine", whom="Пётр Иванов", what="Своё дело")
    store.set_state("kaiten_me", {"id": 1})
    store.execute("INSERT INTO kaiten_cards(id, title, state, condition, responsible_id, responsible_name) "
                  "VALUES(10, 'Карточка', 2, 1, 7, 'Пётр Иванов'), (11, 'Моя', 1, 1, 1, 'Директор'), "
                  "(12, 'Готово', 3, 1, 7, 'Пётр Иванов')")
    load = records.workload()
    assert load[0]["me"] and load[0]["open"] == 1 and load[0]["kaiten"] == 1
    petr = next(w for w in load if w["name"] == "Пётр Иванов")
    assert (petr["open"], petr["overdue"], petr["kaiten"]) == (2, 1, 1)
    assert petr["person_q"] == "Петя" and petr["kaiten_q"] == "Пётр Иванов"


SUMMARY = """# Обзор продаж

## Решения
1. Неделю считаем с понедельника [00:04:00], [00:05:23]
2. **Отчёт** — по пятницам [00:10:00]–[00:11:00]

## Открытые вопросы
- Кто собирает выгрузку (решит Анна)
"""


def test_followup_text_auto_and_from_claude():
    store.execute("INSERT INTO meetings(id, title, status, started_at) VALUES('m1', 'Обзор продаж', 'processed', "
                  "'2026-10-27T10:00:00+00:00')")
    folder = records.meeting_dir("m1")
    folder.mkdir(parents=True)
    (folder / "summary.md").write_text(SUMMARY, encoding="utf-8")
    records.add_commitment(direction="to_me", who="Пётр", whom="Анна", what="Прислать прогноз: по ключевым клиентам",
                           due="2026-11-06", source_type="meeting", source_ref="m1")
    text, auto = records.followup_text("m1")
    assert auto and text.startswith("Итоги встречи «Обзор продаж» 27.10")
    assert "• Неделю считаем с понедельника" in text and "[00:" not in text and "**" not in text
    assert "Следующие шаги:\n• Кто собирает выгрузку (решит Анна)" in text
    assert "• Пётр → Анна: Прислать прогноз — до 06.11" in text
    (folder / "followup.md").write_text("Итоги от Claude", encoding="utf-8")
    assert records.followup_text("m1") == ("Итоги от Claude", False)


def test_weekly_runs_once_and_catches_up(monkeypatch):
    friday = datetime(2026, 10, 30, 12, 0, tzinfo=config.tz())
    settings = config.load_settings()
    monkeypatch.setattr(config, "now", lambda s=None: friday)
    assert jobs._weekly("summary", 5, "18:00", settings) is None  # до назначенного времени
    monkeypatch.setattr(config, "now", lambda s=None: friday + timedelta(days=3))  # пятница вечером ПК был выключен
    assert jobs._weekly("summary", 5, "18:00", settings) == "2026-10-30", "итоги придут при следующем запуске"
    assert jobs._weekly("summary", 5, "18:00", settings) is None, "и только один раз"


def test_weekly_skips_weeks_before_it_existed(monkeypatch):
    monkeypatch.setattr(config, "now", lambda s=None: datetime(2026, 11, 3, 9, 0, tzinfo=config.tz()))  # вторник
    assert jobs._weekly("summary", 5, "18:00", config.load_settings()) is None


def test_digests_saved_from_notify_and_page(client, settings_with):
    cli.main(["notify", "--kind", "weekly", "<b>Итоги недели</b>\nвстреч 14"])
    cli.main(["notify", "--kind", "groups", "<b>Группы</b>"])
    assert [d["kind"] for d in digests.latest("weekly")] == ["weekly"]
    assert store.scalar("SELECT COUNT(*) FROM outbox") == 1, "сводка групп в Telegram — только если включено"
    settings_with(notifications={"groups_digest": True})
    cli.main(["notify", "--kind", "groups", "<b>Группы 2</b>"])
    assert store.scalar("SELECT COUNT(*) FROM outbox") == 2
    page = client.get("/digests?tab=weekly").text
    assert "<b>Итоги недели</b><br>встреч 14" in page and "Дайджесты" in page
    assert "Неделя" in digests.week_stats()


def test_pages_show_briefs_reminders_workload_followup(client, monkeypatch):
    records.upsert_person("Пётр Иванов", telegram="@petr")
    cid = records.add_commitment(direction="to_me", who="Пётр Иванов", title="Прогноз", what="Прислать прогноз",
                                 due=records.today_local().isoformat(), source_type="meeting")
    _event("ev-1", "Обзор продаж", 0.5)
    briefs.save("ev-1", "## Вопросы к встрече\n1. **Что** с планом?", topic="Продажи")

    home = client.get("/").text
    assert "Напомните коллегам" in home and "https://t.me/petr" in home and "договаривались на сегодня" in home
    assert 'class="ev-ok"' in home and "Открыть бриф" in home and "• **" not in home and "1. Что с планом?" in home
    week = client.get("/calendar").text
    assert 'class="ev-ok"' in week and 'class="dlg dlg-brief"' in week
    assert "Загрузка" in client.get("/commitments").text and "задач 1" in client.get("/people").text

    from opsdash.sources import telegram

    monkeypatch.setattr(telegram, "send", lambda target, text, settings=None: 1)
    r = client.post(f"/commitments/{cid}/remind", data={"text": "Пётр, как прогноз?"}, follow_redirects=False)
    assert "%D0%9D%D0%B0%D0%BF%D0%BE%D0%BC%D0%B8%D0%BD%D0%B0%D0%BD%D0%B8%D0%B5" in r.headers["location"]  # «Напоминание…»
    assert records.get_commitment(cid)["reminded_at"]

    store.execute("INSERT INTO meetings(id, title, status) VALUES('m1', 'Обзор', 'processed')")
    records.meeting_dir("m1").mkdir(parents=True)
    (records.meeting_dir("m1") / "summary.md").write_text(SUMMARY, encoding="utf-8")
    page = client.get("/meetings/m1?tab=followup").text
    assert "Текст для отправки" in page and "Копировать текст" in page and "Подготовить с Claude" in page

    r = client.post("/settings/digests", data={"weekly_day": 5, "weekly_time": "18:00", "groups_digest": "",
                                               "briefs_hours": 24, "briefs_ignore": "обед, спорт",
                                               "reminder_template": "{имя}, как {задача}?"}, follow_redirects=False)
    s = config.load_settings()
    assert r.status_code == 303 and s["briefs"]["hours_ahead"] == 24 and s["briefs"]["ignore"] == ["обед", "спорт"]
    assert not s["briefs"]["enabled"] and s["reminders"]["template"] == "{имя}, как {задача}?"
    bad = client.post("/settings/digests", data={"reminder_template": "{кому}"}, follow_redirects=False)
    assert "err=" in bad.headers["location"]


def test_past_digests_moved_from_bot_feed():
    for text in ("<b>Доброе утро! Главное на сегодня</b>\n…", "<b>Итоги дня</b>\n…", "<b>Разобрала встречу</b>"):
        store.execute("INSERT INTO outbox(text, created) VALUES(?, ?)", (text, store.utcnow()))
    store.execute("DELETE FROM state WHERE key='digests_backfilled'")
    store._initialized.clear()  # как при запуске новой версии
    assert [d["kind"] for d in digests.latest("daily")] == ["evening", "morning"]
    store._initialized.clear()
    assert len(digests.latest("daily")) == 2, "переносим один раз"
