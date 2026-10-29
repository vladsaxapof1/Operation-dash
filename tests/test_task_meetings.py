"""Задачи и встречи календаря: встреча, посвящённая задаче, видна на её карточке, в брифе и в разборе записи."""

import json
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient

from opsdash import briefs, cli, config, jobs, proposals, records, recordings, store, task_meetings


def _event(uid, title, hours, minutes=30, attendees=()):
    start = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(hours=hours)
    store.execute('INSERT INTO events(uid, calendar, title, start, "end", all_day, attendees) VALUES(?,?,?,?,?,0,?)',
                  (uid, "Работа", title, start.isoformat(), (start + timedelta(minutes=minutes)).isoformat(),
                   json.dumps([{"name": a, "email": f"{i}@x.ru"} for i, a in enumerate(attendees)], ensure_ascii=False)))
    return start


def _task(title, what=None, **kw):
    return records.add_commitment(direction=kw.pop("direction", "mine"), what=what or title, title=title, **kw)


@pytest.fixture
def client():
    from opsdash.web.app import app

    return TestClient(app, base_url="http://localhost:8765")


def test_suggest_finds_meeting_for_task_and_respects_no(settings_with):
    lead = _task("Собраться ещё раз по лид-продаже", whom="Пётр Иванов")
    other = _task("Прислать прогноз по ключевым клиентам", whom="Анна")
    _task("Купить подарок")
    _event("lead", "Лид-продажа: пересаживаемся", 26, attendees=["Пётр Иванов"])
    _event("forecast", "Прогноз Q4", 50, attendees=["Анна Смирнова"])  # одно общее слово, но тот же человек
    _event("general", "Встреча", 3)
    assert task_meetings.suggest() == 2
    hints = {p["payload"]["commitment_id"]: p for p in proposals.list_proposals("pending")}
    assert hints[lead]["payload"]["event_uid"] == "lead" and hints[other]["payload"]["event_uid"] == "forecast"
    assert "Лид-продажа" in hints[lead]["title"] and "собраться" in hints[lead]["title"].lower()
    assert task_meetings.suggest() == 0  # уже предложено — не дублируем

    proposals.reject(hints[other]["id"])  # «Не то»
    assert task_meetings.suggest() == 0 and records.get_commitment(other)["meeting"] is None
    with pytest.raises(proposals.ProposalError, match="«Нет»"):
        proposals.propose_task_meeting(commitment_id=other, event_uid="forecast")

    proposals.approve(hints[lead]["id"])  # «Связать»
    c = records.get_commitment(lead)
    assert c["status"] == "open" and c["meeting"]["uid"] == "lead"  # задача ждёт встречи открытой
    assert c["meeting"]["when"].startswith(("завтра", "сегодня")) and c["meeting"]["people"] == ["Пётр Иванов"]


def test_autonomy_links_right_away(settings_with):
    settings_with(autonomy={"task_meeting": "auto"})
    cid = _task("Обсудить перенос склада")
    _event("sklad", "Перенос склада", 4)
    task_meetings.suggest()
    assert records.get_commitment(cid)["meeting"]["uid"] == "sklad"
    assert proposals.list_proposals("done")[0]["kind"] == "task.meeting"


def test_meeting_label_and_record(settings_with):
    now = datetime.now(timezone.utc)
    local = config.now()
    assert task_meetings.when(now + timedelta(days=2, hours=1)).split()[0] in task_meetings.WEEKDAYS + ("завтра",)
    assert task_meetings.when(now + timedelta(days=9)) == (local + timedelta(days=9)).strftime("%d.%m %H:%M")
    assert task_meetings.when(now - timedelta(days=3)) == f"была {(local - timedelta(days=3)):%d.%m}"
    cid = _task("Согласовать перенос склада")
    _event("past", "Склад", -30)
    task_meetings.link(cid, "past")
    store.execute("INSERT INTO meetings(id, title, event_uid, status) VALUES('m1', 'Склад', 'past', 'processed')")
    mt = records.get_commitment(cid)["meeting"]
    assert mt["past"] and mt["recorded"] == "m1" and mt["when"].startswith("была")
    store.execute("DELETE FROM events")  # прошедшая ушла из календаря — встреча на задаче остаётся
    assert records.get_commitment(cid)["meeting"]["title"] == "Склад"


def test_link_from_task_window_and_calendar(client, settings_with, monkeypatch):
    cid = _task("Собраться ещё раз по лид-продаже")
    _event("lead", "Лид-продажа", 26)
    label = task_meetings.label(store.row("SELECT * FROM events WHERE uid='lead'"))
    assert client.get("/api/event-options").json() == [label]

    def post(**data):
        return unquote(client.post(f"/commitments/{cid}/meeting", data=data, follow_redirects=False).headers["location"])

    assert "err=" in post(event="нет такой")
    assert "связана" in post(event=label) and records.get_commitment(cid)["meeting"]["uid"] == "lead"
    board = client.get("/commitments").text
    assert "chip meet" in board and "Отвязать" in board and "Лид-продажа" in board
    assert "отвязана" in post(action="unlink") and records.get_commitment(cid)["meeting"] is None

    # «+ Назначить встречу»: форма календаря заполнена по задаче, созданная встреча — на карточке
    store.execute("INSERT INTO people(name, email) VALUES('Пётр Иванов', 'petr@x.ru')")
    records.update_commitment(cid, whom="Пётр Иванов")
    page = client.get(f"/calendar?task={cid}").text
    assert "Встреча по задаче «Собраться ещё раз по лид-продаже»" in page and "petr@x.ru" in page
    from opsdash.sources import calendar

    monkeypatch.setattr(calendar, "create_event", lambda *a, **k: "new@pult")
    settings_with(calendar={"enabled": True})
    r = client.post("/calendar/new", data={"title": "Лид-продажа", "day": (config.now() + timedelta(days=1)).date().isoformat(),
                                           "at": "15:00", "minutes": "30", "task": str(cid)}, follow_redirects=False)
    assert unquote(r.headers["location"]).startswith(f"/commitments?task={cid}&msg=Встреча «Лид-продажа» создана")
    mt = records.get_commitment(cid)["meeting"]
    assert mt["uid"] == "new@pult" and mt["title"] == "Лид-продажа"  # в календаре PULSE её ещё нет — помним сами


def test_hint_in_task_window_and_calendar_card(client, settings_with):
    cid = _task("Собраться ещё раз по лид-продаже")
    _event("lead", "Лид-продажа: пересаживаемся", 2)
    task_meetings.suggest()
    board = client.get("/commitments").text
    assert "встреча?" in board and "Похоже, встреча по этой задаче" in board
    [p] = proposals.list_proposals("pending")
    client.post(f"/proposals/{p['id']}/approve", follow_redirects=False)
    assert records.get_commitment(cid)["meeting"]["uid"] == "lead"
    week = client.get("/calendar").text
    assert "По задаче:" in week and f"/commitments?task={cid}" in week


def test_claude_sees_meeting_tasks(settings_with, capsys, tmp_path):
    cid = _task("Собраться ещё раз по лид-продаже", whom="Пётр")
    other = _task("Прислать прогноз", direction="to_me", who="Пётр")
    _event("lead", "Лид-продажа", 2, attendees=["Пётр"])
    store.execute("INSERT INTO people(name) VALUES('Пётр')")
    cli.main(["propose", "link-meeting", "--commitment", str(cid), "--uid", "lead", "--reason", "так в названии"])
    assert "ждёт подтверждения" in capsys.readouterr().out
    proposals.approve(proposals.list_proposals("pending")[0]["id"])
    cli.main(["calendar", "--days", "2"])
    assert f"по задаче [{cid}] Собраться ещё раз по лид-продаже" in capsys.readouterr().out
    cli.main(["commitments", "list"])
    assert "встреча «Лид-продажа»" in capsys.readouterr().out
    ctx = briefs.context("lead")
    head, rest = ctx.split("Открытые задачи с участниками:")
    assert "Встреча посвящена задачам:" in head and f"[{cid}]" in head and f"[{cid}]" not in rest and f"[{other}]" in rest

    # запись этой встречи: в meta.json — задачи, по которым Claude предложит итог
    rec = tmp_path / "лид.txt"
    rec.write_text("Пётр: пересаживаемся с понедельника", encoding="utf-8")
    start = store.parse_ts(store.scalar("SELECT start FROM events WHERE uid='lead'"))
    mid = recordings.create_meeting(rec)
    records.update_meeting(mid, started_at=store.to_utc_iso(start))
    meta = recordings.transcribe_meeting(mid)
    assert meta["event"]["uid"] == "lead" and [t["id"] for t in meta["tasks"]] == [cid]


def test_calendar_sync_suggests(settings_with, monkeypatch):
    from opsdash.sources import calendar

    cid = _task("Собраться ещё раз по лид-продаже")
    monkeypatch.setattr(calendar, "collect", lambda: _event("lead", "Лид-продажа", 5) and "встреч в окне: 1")
    assert jobs.collect_source("calendar") == "встреч в окне: 1"
    assert proposals.list_proposals("pending")[0]["payload"] == {"commitment_id": cid, "event_uid": "lead"}
