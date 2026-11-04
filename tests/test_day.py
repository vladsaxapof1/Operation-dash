"""«Реальность дня»: окна между встречами (склейка, буферы, фокус-блоки, короткие окна), «сейчас», полночь и весь
день, часовой пояс, спрос по приоритету, жадная расстановка, решения директора, оценки, ./ops day и Сводка."""

import re
import time as clock
from datetime import date, datetime, time, timedelta

import pytest
from fastapi.testclient import TestClient

from opsdash import config, day, records, store

DAY = date(2026, 11, 3)  # вторник


def ev(uid, title, start, end, people=(), *, on=DAY, end_on=None, **kw):
    """Событие календаря как в базе: время местное (часы:минуты), участники — адреса."""
    tz = config.tz()
    s = datetime.combine(on, time.fromisoformat(start), tz)
    f = datetime.combine(end_on or on, time.fromisoformat(end), tz)
    return {"uid": uid, "title": title, "start": store.to_utc_iso(s), "end": store.to_utc_iso(f), "all_day": 0,
            "calendar": "Рабочий", "attendees": [{"email": p, "name": p, "status": ""} for p in people], **kw}


def spans(items):
    return [(day.hm(s), day.hm(e)) for s, e in items]


def wins(lay, short=None):
    return [(day.hm(w["s"]), day.hm(w["e"])) for w in lay["windows"] if short is None or w["short"] == short]


def test_merge_buffers_and_short_windows():
    settings = config.load_settings()
    events = [
        ev("a", "Планёрка", "10:00", "11:00", ["petr@example.ru"]),
        ev("b", "Созвон с маркетингом", "10:30", "11:30", ["ira@example.ru"]),  # пересекается — склеиваются
        ev("c", "1:1 с Олегом", "11:35", "12:00", ["oleg@example.ru"]),  # буфер после «b» — только 5 минут до неё
        ev("d", "Итоги дня", "15:00", "15:30"),  # без участников — без буфера «на хвосты»
        ev("e", "Созвон с подрядчиком", "15:50", "16:30", ["podr@example.ru"]),  # окно 15:30–15:50 короткое
        ev("f", "Вечерний созвон", "19:30", "20:00", ["petr@example.ru"]),  # буфер после — за концом рабочего дня
    ]
    lay = day.layout(DAY, events, settings)
    assert spans(lay["busy"]) == [("10:00", "11:30"), ("11:35", "12:00"), ("15:00", "15:30"), ("15:50", "16:30"),
                                  ("19:30", "20:00")]
    assert spans(lay["buffers"]) == [("11:30", "11:35"), ("12:00", "12:10"), ("16:30", "16:40")]
    assert wins(lay) == [("09:00", "10:00"), ("12:10", "15:00"), ("15:30", "15:50"), ("16:40", "19:30")]
    assert wins(lay, short=True) == [("15:30", "15:50")]  # 20 минут меньше 25 — видно, но не считается
    assert day.capacity(lay) == 60 + 170 + 170


def test_brief_buffer_stays_inside_work_hours_and_gaps():
    settings = config.load_settings()
    events = [ev("a", "Статус", "09:00", "09:30", ["d@example.ru"]),  # бриф готов, но 08:50 — до рабочего дня
              ev("b", "Звонок", "11:00", "11:55", ["p@example.ru"]),
              ev("c", "Бюджет Q4", "12:00", "13:00", ["o@example.ru"])]  # бриф готов: буфер только 11:55–12:00
    lay = day.layout(DAY, events, settings, briefs={"a", "c"})
    assert spans(lay["buffers"]) == [("09:30", "09:40"), ("11:55", "12:00"), ("13:00", "13:10")]
    assert wins(lay) == [("09:40", "11:00"), ("13:10", "20:00")]


def test_focus_block_is_a_window_and_gets_tasks_first(settings_with):
    settings = settings_with(calendar={"login": "anna@example.ru"})
    events = [ev("t", "Тайм-аут", "11:00", "12:00", ["anna@example.ru"]),  # только сама — фокус-блок
              ev("f", "Фокус: стратегия", "16:00", "17:00", ["petr@example.ru"]),  # с участником — обычная встреча
              ev("m", "Планёрка", "12:00", "13:00", ["petr@example.ru"])]
    lay = day.layout(DAY, events, settings)
    assert [f["uid"] for f in lay["focus"]] == ["t"] and [m["uid"] for m in lay["meetings"]] == ["f", "m"]
    assert wins(lay) == [("09:00", "12:00"), ("13:10", "16:00"), ("17:10", "20:00")]
    assert lay["windows"][0]["focus"]["title"] == "Тайм-аут"
    items = [{"id": i, "title": f"Задача {i}", "est": est} for i, est in enumerate([30, 30, 30], 1)]
    placed = day.place(lay["windows"], items)
    # сначала сам «Тайм-аут», когда он заполнен — остальное в том же окне
    assert [(b["item"]["id"], day.hm(b["s"])) for b in placed["blocks"]] == [(3, "09:00"), (1, "11:00"), (2, "11:30")]


def test_now_in_the_middle_of_the_day():
    settings = config.load_settings()
    tz = config.tz()
    events = [ev("m", "Планёрка", "12:00", "13:00", ["p@example.ru"]), ev("n", "1:1", "15:00", "15:30", ["o@example.ru"])]
    lay = day.layout(DAY, events, settings, now=datetime.combine(DAY, time(12, 30), tz))
    assert lay["from"] == 12 * 60 + 30 and wins(lay) == [("13:10", "15:00"), ("15:40", "20:00")]
    p = {**lay, "today": True, "off": None, "blocks": [], "items": [{"id": 1, "title": "Сводка", "est": 30}]}
    cur, nxt = day._now_window(p, datetime.combine(DAY, time(12, 30), tz))
    assert cur is None and day.hm(nxt["s"]) == "13:10" and nxt["fit"]["title"] == "Сводка"
    lay = day.layout(DAY, events, settings, now=datetime.combine(DAY, time(14, 20), tz))
    p = {**lay, "today": True, "off": None, "blocks": [], "items": []}
    cur, nxt = day._now_window(p, datetime.combine(DAY, time(14, 20), tz))
    assert day.hm(cur["e"]) == "15:00" and cur["left"] == 40 and nxt is None
    late = day.layout(DAY, events, settings, now=datetime.combine(DAY, time(20, 30), tz))
    assert late["off"] == "over" and late["windows"] == []
    assert day.verdict(0, 90, settings, off="over")["line"] == "Рабочий день закончился · не сделано задач на 1 ч 30 мин"


def test_overnight_and_all_day_events():
    settings = config.load_settings()
    events = [
        ev("night", "Перелёт", "23:00", "09:30", ["p@example.ru"], on=DAY - timedelta(days=1), end_on=DAY),
        ev("late", "Созвон с Владивостоком", "19:30", "00:30", ["v@example.ru"], end_on=DAY + timedelta(days=1)),
        ev("all", "Конференция", "00:00", "00:00", end_on=DAY + timedelta(days=1), all_day=1),
        ev("long", "Выездная сессия", "09:00", "10:00", ["p@example.ru"], on=DAY - timedelta(days=1)),
    ]
    events[-1]["end"] = store.to_utc_iso(datetime.combine(DAY + timedelta(days=1), time(10), config.tz()))  # двое суток
    lay = day.layout(DAY, events, settings)
    assert spans(lay["busy"]) == [("00:00", "09:30"), ("19:30", "24:00")]
    assert wins(lay) == [("09:40", "19:30")]


def test_owner_timezone(settings_with):
    settings = settings_with(owner={"timezone": "Asia/Yekaterinburg"})
    e = {"uid": "x", "title": "Планёрка", "start": "2026-11-03T05:00:00+00:00", "end": "2026-11-03T06:00:00+00:00",
         "all_day": 0, "attendees": [{"email": "p@example.ru"}]}  # 10:00–11:00 по Екатеринбургу
    lay = day.layout(DAY, [e], settings)
    assert wins(lay) == [("09:00", "10:00"), ("11:10", "20:00")]
    store.execute('INSERT INTO events(uid, title, start, "end", all_day, attendees) VALUES(?,?,?,?,0,?)',
                  ("x", e["title"], e["start"], e["end"], store.json_col(e["attendees"])))
    assert wins(day.windows(DAY, settings, now=datetime(2026, 11, 3, 8, 0, tzinfo=config.tz(settings)))) == \
        [("09:00", "10:00"), ("11:10", "20:00")]


def test_declined_transparent_and_lunch(settings_with):
    settings = settings_with(calendar={"login": "anna@example.ru"}, day={"lunch": "13:00-14:00"})
    events = [
        ev("d", "Ревью", "10:00", "11:00", ["petr@example.ru"]),
        ev("g", "Синк", "15:00", "16:00", ["petr@example.ru", "anna@gmail.example"], calendar="Google: anna@gmail.example"),
        ev("t", "Вебинар", "17:00", "18:00", ["x@example.ru"], transp="TRANSPARENT"),
    ]
    events[0]["attendees"].append({"email": "Anna@Example.ru", "status": "DECLINED"})
    events[1]["attendees"][1]["status"] = "DECLINED"  # отказ в Google: адрес — название календаря
    lay = day.layout(DAY, events, settings)
    assert [e["uid"] for e in lay["free_events"]] == ["d", "g", "t"] and lay["meetings"] == []
    assert wins(lay) == [("09:00", "13:00"), ("14:00", "20:00")]


def test_verdict_levels():
    assert day.verdict(130, 100)["level"] == "ok"
    v = day.verdict(130, 270)
    assert v["level"] == "over" and v["title"] == "Перегруз 2 ч 20 мин"
    assert v["line"] == "Свободно 2 ч 10 мин, задач на 4 ч 30 мин — перегруз 2 ч 20 мин"
    assert day.verdict(100, 105)["title"] == "Плотно"
    assert day.verdict(0, 30)["line"] == "Свободных окон нет, задач на 30 мин — перегруз 30 мин"
    assert day.verdict(0, 0, off="weekend")["title"] == "Выходной"


def test_greedy_placement_overflow_and_too_big():
    wins_ = [{"s": 540, "e": 600, "len": 60, "short": False, "focus": None},
             {"s": 700, "e": 730, "len": 30, "short": False, "focus": None},
             {"s": 760, "e": 780, "len": 20, "short": True, "focus": None},  # короткое — не используется
             {"s": 800, "e": 1000, "len": 200, "short": False, "focus": None}]
    items = [{"id": i, "title": str(i), "est": est} for i, est in enumerate([120, 60, 30, 240, 90, 15], 1)]
    res = day.place(wins_, items)
    assert [(b["item"]["id"], b["item"]["slot"]) for b in res["blocks"]] == [
        (2, "09:00–10:00"), (3, "11:40–12:10"), (1, "13:20–15:20"), (6, "15:20–15:35")]
    assert [i["id"] for i in res["overflow"]] == [5] and [i["id"] for i in res["too_big"]] == [4]
    assert res["longest"] == 200


def _task(title, due=None, **kw):
    return records.add_commitment(direction=kw.pop("direction", "mine"), whom="Тимур", title=title, what=title,
                                  due=due, **kw)


def test_demand_priorities_and_reasons():
    iso = lambda n: (DAY + timedelta(days=n)).isoformat()  # noqa: E731
    later = _task("Позже", iso(5))
    work = _task("В работе", iso(10))
    records.set_in_work(work, True)
    held = _task("Её ждут", iso(2))  # среда и четверг — ближайшие 2 рабочих дня
    for who in ("Пётр", "Мария"):
        waiter = _task(f"Ждёт {who}", direction="to_me", who=who)
        records.add_wait(waiter, waits_for=held)
    today = _task("Сегодня", iso(0))
    overdue = _task("Просрочено", iso(-2))
    blocked = _task("Ждёт других", iso(0))
    records.add_wait(blocked, waits_for=_task("Чужая", direction="to_me", who="Олег"))
    _task("Не моя", iso(0), direction="to_me", who="Олег")
    for _ in range(2):
        records.add_attention(summary="Ответить")
    day.set_estimate(today, 60)
    dem = day.demand(DAY)
    items = dem["items"]
    assert [i["id"] for i in items] == [overdue, today, held, work, None]
    assert [i["reason"] for i in items] == [
        "просрочено 2 дня", "срок сегодня", "ждут Пётр, Мария · срок 05.11", "в работе · срок 13.11", "2 × 5 мин"]
    assert items[0]["est"] == 30 and items[0]["est_default"] and items[1]["est"] == 60 and items[-1]["est"] == 10
    assert [c["id"] for c in dem["waiting"]] == [blocked] and later not in [i["id"] for i in items]


def test_decisions_pin_move_and_drop(settings_with):
    settings = settings_with(calendar={"enabled": True})
    tz = config.tz()
    now = datetime.combine(DAY, time(9, 0), tz)
    a, b, c, d = (_task(t, DAY.isoformat()) for t in ("А", "Б", "В", "Г"))
    later = _task("На следующей неделе", (DAY + timedelta(days=7)).isoformat())
    day.decide(a, "plan", day=DAY, start="14:00")
    day.decide(b, "plan", day=DAY, start="14:00")  # то же окно — встаёт после первой
    day.decide(later, "plan", day=DAY)  # «авто»: в план сегодня, хотя срок не сегодня
    assert "срок — " in day.decide(c, "tomorrow", day=DAY).lower()
    day.decide(d, "nodue", day=DAY)
    p = day.plan(DAY, settings, now)
    slots = {b_["item"]["id"]: b_["item"]["slot"] for b_ in p["blocks"]}
    assert slots[a] == "14:00–14:30" and slots[b] == "14:30–15:00" and slots[later] == "09:00–09:30"
    assert c not in slots and d not in slots
    assert records.get_commitment(c)["due"] == "2026-11-04"
    assert records.get_commitment(d)["due"] is None and records.get_commitment(d)["no_due_at"]
    assert [i["id"] for i in p["items"][:3]] == [a, b, later]  # решения директора — первыми
    assert p["items"][2]["reason"] == "вы поставили в план · срок 10.11"
    # расстановка PULSE сохранена, решения директора — отдельно; закрытая задача из плана — «сделано»
    rows = {(r["commitment_id"], r["source"], r["status"]) for r in day.decisions(DAY)}
    assert (a, "user", "planned") in rows and (c, "user", "moved") in rows and (later, "user", "planned") in rows
    records.update_commitment(a, status="done")
    store.execute("UPDATE commitments SET closed_at=? WHERE id=?",  # закрыта в тот же день
                  (store.to_utc_iso(datetime.combine(DAY, time(15, 0), tz)), a))
    day.plan(DAY, settings, now)
    assert store.scalar("SELECT status FROM day_plan WHERE commitment_id=?", (a,)) == "done"
    day.decide(later, "unplan", day=DAY)  # решение отменено — PULSE снова расставляет сам, задача не на сегодня
    assert later not in [i["id"] for i in day.plan(DAY, settings, now)["items"]]


def test_estimates_user_wins_over_claude(monkeypatch, capsys):
    from opsdash import cli

    cid = _task("Сводка решений")
    assert day.set_estimate(cid, 60, by="claude")
    assert day.set_estimate(cid, 30, by="user")
    assert not day.set_estimate(cid, 120, by="claude")  # ручную оценку Claude не перезаписывает
    assert store.row("SELECT estimate_min, estimate_by FROM commitments WHERE id=?", (cid,)) == \
        {"estimate_min": 30, "estimate_by": "user"}
    with pytest.raises(ValueError):
        day.set_estimate(cid, -5)
    monkeypatch.setenv("OPSDASH_AGENT", "1")
    cli.main(["commitments", "add", "--direction", "mine", "--title", "Стратегия склада", "--what", "Стратегия склада",
              "--estimate", "300"])
    out = capsys.readouterr().out
    assert "Оценка: 5 ч" in out and "разбить" in out
    new = store.row("SELECT id, estimate_min, estimate_by FROM commitments WHERE title='Стратегия склада'")
    assert new["estimate_min"] == 300 and new["estimate_by"] == "claude"
    plain = _task("Без оценки")
    cli.main(["day", "estimates"])
    assert f"[{plain}]" in capsys.readouterr().out
    cli.main(["day", "estimates", "--set", f"{plain}=15, {cid}=240"])
    assert "Оценки сохранены: 1; оценки директора не менял: 1" in capsys.readouterr().out
    cli.main(["commitments", "set", str(cid), "--estimate", "60"])
    assert "Оценку директор поставила сама" in capsys.readouterr().out
    assert day.unestimated() == []


def test_backfill_job_runs_once(monkeypatch, settings_with):
    from opsdash import brain, jobs

    settings_with(schedule={"digest_morning": "", "digest_evening": "", "stale_check": ""})
    cid = _task("Старая задача")
    submitted = []
    monkeypatch.setattr(jobs, "submit", lambda name, kind="light", args=None: submitted.append(name) or True)
    monkeypatch.setattr(jobs, "in_work_hours", lambda settings, at=None: True)
    jobs.tick()
    assert "estimates" in submitted
    monkeypatch.setattr(brain, "run", lambda prompt, **kw: day.set_estimate(cid, 30, by="claude"))
    assert jobs.REGISTRY["estimates"]() == "оценено задач: 1 из 1"
    submitted.clear()
    _task("Ещё одна")
    store.execute("DELETE FROM jobs")
    jobs.tick()
    assert "estimates" not in submitted  # разовая: дальше оценки ставят навыки при создании задачи


def _busy_day():
    """8 встреч и «Тайм-аут», как в спецификации."""
    plan = [("09:30", "10:00"), ("10:30", "11:00"), ("12:00", "13:00"), ("12:30", "13:00"), ("13:30", "14:00"),
            ("15:00", "16:00"), ("16:30", "17:15"), ("18:30", "19:00")]
    events = [ev(f"m{i}", f"Встреча {i}", s, e, ["p@example.ru"]) for i, (s, e) in enumerate(plan)]
    events.append(ev("focus", "Тайм-аут", "11:00", "12:00"))
    for e in events:
        store.execute('INSERT INTO events(uid, title, start, "end", all_day, attendees) VALUES(?,?,?,?,0,?)',
                      (e["uid"], e["title"], e["start"], e["end"], store.json_col(e["attendees"])))


def test_cli_day_and_speed(settings_with, capsys, monkeypatch):
    from opsdash import cli

    settings = settings_with(calendar={"enabled": True})
    _busy_day()
    for i in range(20):
        _task(f"Задача {i}", (DAY - timedelta(days=i % 3)).isoformat())
    now = datetime.combine(DAY, time(8, 0), config.tz())
    monkeypatch.setattr(config, "now", lambda s=None: now)
    day.plan(DAY, settings, now)
    started = clock.perf_counter()
    p = day.plan(DAY, settings, now)
    assert clock.perf_counter() - started < 0.2  # без ИИ и быстро
    assert p["capacity"] == 30 + 50 + 40 + 65 + 60 and p["summary"] == "тайм-аут 11:10 + 4 окна"
    assert p["verdict"]["level"] == "over" and "Главное: «" in p["digest"]
    cli.main(["day", "--date", DAY.isoformat()])
    out = capsys.readouterr().out
    assert "Вердикт: " in out and "Тайм-аут (фокус-блок)" in out and "Не влезает сегодня" in out


def test_overview_page_review_and_estimate_chips(settings_with, monkeypatch):
    from opsdash.web.app import app

    settings_with(calendar={"enabled": True})
    monkeypatch.setattr(config, "now", lambda s=None: datetime.combine(DAY, time(10, 0), config.tz(s)))
    _busy_day()
    cid = _task("Сводка решений по KPI", records.today_local().isoformat())
    client = TestClient(app, base_url="http://localhost:8765")
    page = client.get("/").text
    assert "Разобрать день" in page and 'id="day-dlg"' in page and "Сводка решений по KPI" in page
    res = client.post("/day/act", data={"cid": cid, "act": "estimate", "minutes": 120},
                      headers={"Accept": "application/json"}).json()
    assert res["ok"] and "После ваших решений" in res["html"]
    assert store.scalar("SELECT estimate_by FROM commitments WHERE id=?", (cid,)) == "user"
    res = client.post("/day/act", data={"cid": cid, "act": "tomorrow"}, headers={"Accept": "application/json"}).json()
    assert res["ok"] and records.get_commitment(cid)["due"] > records.today_local().isoformat()
    bad = client.post("/day/act", data={"cid": cid, "act": "due"}, headers={"Accept": "application/json"})
    assert bad.status_code == 400 and bad.json()["err"] == "Выберите срок"
    res = client.post(f"/commitments/{cid}/estimate", data={"minutes": 15}, headers={"Accept": "application/json"}).json()
    assert res == {"ok": True, "note": "ваша оценка"}
    board = client.get(f"/commitments?task={cid}").text
    assert "Займёт у вас" in board and 'value="15" aria-pressed="true"' in board
    assert "Фокус-блоки" in client.get("/settings?tab=day").text
    client.post("/settings/day", data={"focus_titles": "тайм-аут, глубокая работа", "buffer_after_min": 5,
                                       "buffer_before_min": 0, "min_window_min": 30, "reply_min": 3,
                                       "lunch_on": "on", "lunch_start": "13:00", "lunch_end": "13:45"})
    d = config.load_settings()["day"]
    assert d["focus_titles"] == ["тайм-аут", "глубокая работа"] and d["lunch"] == "13:00-13:45"
    assert day.opts()["lunch"] == (780, 825) and day.opts()["after"] == 5


# --- этап 2: передать, вечер, сигналы, перетаскивание ------------------------------------------------

def _at(hh, mm=0):
    return datetime.combine(DAY, time(hh, mm), config.tz())


def test_delegate_part_to_colleague():
    cid = _task("Отчёт по возвратам", DAY.isoformat())
    with pytest.raises(ValueError, match="Кому"):
        day.decide(cid, "delegate", day=DAY, what="Выгрузка")
    assert day.decide(cid, "delegate", day=DAY, who="Пётр Иванов", what="Выгрузка по возвратам",
                      due="2026-11-05").startswith("Передано: Пётр Иванов")
    kid = store.row("SELECT * FROM commitments WHERE parent_id=?", (cid,))
    assert kid["direction"] == "to_me" and kid["who"] == "Пётр Иванов" and kid["due"] == "2026-11-05"
    assert records.get_commitment(cid)["helpers"] == ["Пётр Иванов"]  # на доске — «Я + Пётр → Тимур»
    dem = day.demand(DAY)
    assert cid not in [i["id"] for i in dem["items"]]  # ждёт свою часть — в план не идёт
    assert dem["waiting"] == [{"id": cid, "title": "Отчёт по возвратам", "why": "передано: Пётр Иванов"}]
    records.update_commitment(kid["id"], status="done")
    assert cid in [i["id"] for i in day.demand(DAY)["items"]]  # часть готова — задача снова в плане


def test_history_progress_and_no_duplicate_rows(settings_with):
    settings = settings_with(calendar={"enabled": True})
    x, y = _task("Икс", DAY.isoformat()), _task("Игрек", DAY.isoformat())
    p = day.plan(DAY, settings, _at(9))
    assert [b["item"]["slot"] for b in p["blocks"]] == ["09:00–09:30", "09:30–10:00"]
    assert p["progress"] == {"done": 0, "total": 2, "line": "План дня: сделано 0 из 2"}
    records.update_commitment(x, status="done")
    store.execute("UPDATE commitments SET closed_at=? WHERE id=?", (store.to_utc_iso(_at(9, 20)), x))
    for hh, mm in ((12, 0), (12, 0), (12, 5)):  # пересчёты не плодят строк: «Игрек» переставлен, «Икс» — история
        p = day.plan(DAY, settings, _at(hh, mm))
    assert store.scalar("SELECT COUNT(*) FROM day_plan WHERE commitment_id=?", (y,)) == 1
    assert p["progress"]["line"] == "План дня: сделано 1 из 2"
    assert [(h["title"], h["start"]) for h in p["history"]] == [("Икс", "09:00")]
    grid = {"hour_from": 9, "hour_px": 36, "hours": list(range(9, 20)), "events": [], "days": [{"events": []}]}
    day.decorate(grid, p)
    blocks = [b for b in grid["plan_layer"] if b["kind"] == "plan"]
    assert [(b["item"]["title"], b["done"]) for b in blocks] == [("Игрек", False), ("Икс", True)]


def test_evening_carry_over(settings_with, monkeypatch):
    settings = settings_with(calendar={"enabled": True})
    monkeypatch.setattr(config, "now", lambda s=None: _at(20, 30))
    today_, old = _task("Сегодня", DAY.isoformat()), _task("Просрочено", "2026-11-01")
    later = _task("В план сама", "2026-11-10")
    day.decide(later, "plan", day=DAY)
    work = _task("В работе", "2026-11-12")
    records.set_in_work(work, True)
    p = day.plan(DAY, settings)
    assert p["off"] == "over" and [i["id"] for i in p["carry"]] == [later, old, today_]
    assert p["carry_label"] == "на завтра"
    assert day.carry_over(DAY, settings) == 3
    assert records.get_commitment(today_)["due"] == records.get_commitment(old)["due"] == "2026-11-04"
    assert records.get_commitment(later)["due"] == "2026-11-10"  # срок и так позже — переносится только план
    tomorrow = {(r["commitment_id"], r["status"]) for r in day.decisions(DAY + timedelta(days=1))}
    assert tomorrow == {(later, "planned")} and records.get_commitment(work)["due"] == "2026-11-12"
    assert day.plan(DAY, settings)["carry"] == []
    friday = date(2026, 11, 6)
    assert day.plan(friday, settings, datetime.combine(friday, time(21), config.tz()))["carry_label"] == "на понедельник"


def test_drag_pins_block_exactly(settings_with):
    settings = settings_with(calendar={"enabled": True})
    a, b = _task("А", DAY.isoformat()), _task("Б", DAY.isoformat())
    day.decide(a, "plan", day=DAY, start="16:00")
    day.decide(b, "move", day=DAY, start="16:10")  # перетащили — ровно сюда, даже поверх другой задачи
    p = day.plan(DAY, settings, _at(9))
    slots = {x["item"]["id"]: (x["item"]["slot"], x["source"]) for x in p["blocks"]}
    assert slots[a] == ("16:00–16:30", "user") and slots[b] == ("16:10–16:40", "user")
    with pytest.raises(ValueError):
        day.decide(b, "move", day=DAY, start="")


def test_live_signals_overload_and_calendar(settings_with):
    from opsdash import notices

    settings = settings_with(calendar={"enabled": True})
    _busy_day()
    for i in range(3):
        _task(f"Задача {i}", DAY.isoformat())
    assert day.watch(settings, _at(9)) == []  # первый проход дня только запоминает
    assert day.watch(settings, _at(9, 5)) == []
    store.execute('INSERT INTO events(uid, title, start, "end", all_day, attendees) VALUES(?,?,?,?,0,?)',
                  ("big", "Выездная сессия", store.to_utc_iso(_at(9)), store.to_utc_iso(_at(20)),
                   store.json_col([{"email": "p@example.ru"}])))
    got = day.watch(settings, _at(9, 10))
    assert got and got[0].startswith("Календарь изменился: перегруз")
    records.add_commitment(direction="mine", whom="Тимур", title="Срочная справка", what="Срочная справка",
                           due=DAY.isoformat(), source_type="meeting")
    records.add_commitment(direction="mine", whom="Тимур", title="Своя", what="Своя", due=DAY.isoformat())
    assert day.watch(settings, _at(9, 15)) == ["Задача «Срочная справка» на сегодня: места нет — что двигаем?"]
    store.execute('INSERT INTO events(uid, title, start, "end", all_day, attendees) VALUES(?,?,?,?,0,?)',
                  ("more", "Ещё встреча", store.to_utc_iso(_at(10)), store.to_utc_iso(_at(11)), "[]"))
    assert day.watch(settings, _at(9, 20)) == []  # календарь изменился, вердикт тот же — молчим
    assert [n["link"] for n in notices.recent()] == ["/#day", "/#day"]


def test_overview_evening_carry_and_review_delegate(settings_with, monkeypatch):
    from opsdash.web.app import app

    settings_with(calendar={"enabled": True})
    records.upsert_person("Пётр Иванов")
    cid = _task("Сводка решений по KPI", DAY.isoformat())
    client = TestClient(app, base_url="http://localhost:8765")
    monkeypatch.setattr(config, "now", lambda s=None: _at(11))
    page = client.get("/").text
    assert 'data-drag="%d"' % cid in page and 'id="day-pass-%d"' % cid in page and '<option value="Пётр Иванов">' in page
    assert f'href="/agents?task={cid}" data-agent-open="{cid}"' in page  # «Агенту» — панель «Поручить агенту»
    assert client.get(f"/agents?task={cid}").status_code == 200  # без скриптов — страница поручения
    res = client.post("/day/act", data={"cid": cid, "act": "delegate", "who": "Пётр Иванов", "what": "Цифры по KPI"},
                      headers={"Accept": "application/json"}).json()
    assert res["ok"] and "передано: Пётр Иванов" in res["html"]
    other = _task("Ответ юристам", DAY.isoformat())
    monkeypatch.setattr(config, "now", lambda s=None: _at(20, 30))
    page = client.get("/").text
    assert "Перенести незакрытое на завтра" in page and "Ответ юристам" in page
    client.post("/day/carry")
    assert records.get_commitment(other)["due"] == "2026-11-04"


# --- этап 3: неделя, срок в момент обещания, приток и отток --------------------------------------------

def _meeting(uid, on, start="09:00", end="19:30"):
    """Встреча с участником на весь рабочий день — окон в этот день не остаётся."""
    tz = config.tz()
    store.execute('INSERT INTO events(uid, title, start, "end", all_day, attendees) VALUES(?,?,?,?,0,?)',
                  (uid, "Выездная сессия", store.to_utc_iso(datetime.combine(on, time.fromisoformat(start), tz)),
                   store.to_utc_iso(datetime.combine(on, time.fromisoformat(end), tz)),
                   store.json_col([{"email": "p@example.ru"}])))


def test_week_overload_from_thursday():
    settings = config.load_settings()
    _busy_day()  # вторник: окон 4 ч 05 мин
    wed, thu, fri = (DAY + timedelta(days=n) for n in (1, 2, 3))
    for i, d in enumerate((wed, thu, fri)):
        _meeting(f"full{i}", d)
    for n in range(3):
        day.set_estimate(_task(f"Вторник {n}", DAY.isoformat()), 30)
    day.set_estimate(_task("Среда", wed.isoformat()), 60)
    day.set_estimate(_task("Четверг", thu.isoformat()), 120)
    w = day.week(DAY, settings, _at(8))
    assert [(x["date"], x["capacity"], x["need"], x["level"]) for x in w["days"]] == [
        (DAY, 245, 90, "ok"), (wed, 0, 60, "ok"), (thu, 0, 120, "over"), (fri, 0, 0, "over")]
    assert w["line"] == "На неделю: окон 4 ч, задач 4 ч 30 мин — перегруз с четверга"
    assert day.week(DAY, settings, _at(8, 0) + timedelta(days=1))["days"][0]["date"] == wed  # с сегодняшнего дня


def test_week_only_today_overloaded_is_not_week_overload():
    settings = config.load_settings()
    _busy_day()
    day.set_estimate(_task("Большая просроченная", "2026-11-01"), 240)
    w = day.week(DAY, settings, _at(8))
    assert w["days"][0]["level"] == "tight" and all(x["level"] == "ok" for x in w["days"][1:])
    assert w["line"].endswith("— реально (сегодня — плотно)")
    nxt = day.week(DAY + timedelta(days=7), settings, _at(8))  # следующая неделя: первый день — понедельник
    assert nxt["days"][0]["date"] == date(2026, 11, 9) and nxt["line"].startswith("На неделю: окон 55 ч")


def test_capacity_until_and_due_hint(capsys, monkeypatch):
    from opsdash import cli

    settings = config.load_settings()
    _busy_day()  # сегодня окон 4 ч 05 мин; завтра календарь пуст
    day.set_estimate(_task("Уже на сегодня", DAY.isoformat()), 240)
    res = day.capacity_until(DAY, 60, settings=settings, now=_at(8))
    assert not res["ok"] and res["capacity"] == 245 and res["need"] == 240 and res["realistic"] == DAY + timedelta(days=1)
    assert day.capacity_until(DAY + timedelta(days=1), 60, settings=settings, now=_at(8))["ok"]
    cid = _task("Справка для собственника", DAY.isoformat(), source_type="meeting")
    day.set_estimate(cid, 60, by="claude")
    assert day.check_due(cid, settings, _at(8))["realistic"] == date(2026, 11, 4)
    c = store.row("SELECT due_hint, due_hint_why FROM commitments WHERE id=?", (cid,))
    assert c["due_hint"] == "2026-11-04" and c["due_hint_why"].startswith("до 03.11 окон для задач 4 ч 5 мин")
    assert [x["id"] for x in records.due_queue() if x["due_hint"]] == [cid]  # «Уточнить срок» предложит дату
    records.set_due(cid, None)  # «Оставить 03.11» — срок прежний, больше не спрашиваем
    c = records.get_commitment(cid)
    assert c["due"] == DAY.isoformat() and not c["due_hint"] and not c["no_due_at"] and cid not in [
        x["id"] for x in records.due_queue()]
    # Claude записывает задачу со сроком — проверка сразу, текст для пометки в разборе встречи
    monkeypatch.setattr(config, "now", lambda s=None: _at(8))
    cli.main(["commitments", "add", "--direction", "mine", "--title", "Ещё справка", "--what", "Ещё справка",
              "--due", DAY.isoformat(), "--estimate", "60"])
    out = capsys.readouterr().out
    assert "Срок под угрозой с самого начала" in out and "срок под угрозой с самого начала — реальнее" in out
    cli.main(["day", "capacity", "--until", DAY.isoformat(), "--estimate", "15"])
    assert capsys.readouterr().out.startswith("Не успеваете: до 03.11")
    cli.main(["day", "week"])
    assert capsys.readouterr().out.startswith("На неделю:")


def test_due_dialog_offers_realistic_date(settings_with, monkeypatch):
    from opsdash.web.app import app

    settings_with(calendar={"enabled": True})
    monkeypatch.setattr(config, "now", lambda s=None: _at(10))
    _busy_day()
    day.set_estimate(_task("Уже на сегодня", DAY.isoformat()), 240)
    cid = _task("Справка для собственника", DAY.isoformat(), source_type="meeting")
    day.check_due(cid)
    page = TestClient(app, base_url="http://localhost:8765").get("/").text
    assert "срок под угрозой — 1" in page and 'data-due-pick="2026-11-04"' in page and "Реальнее<span>ср, 04.11</span>" in page
    assert "Оставить 03.11" in page


def test_week_line_on_monday_overview(settings_with, monkeypatch):
    from opsdash.web.app import app

    settings_with(calendar={"enabled": True})
    monday = date(2026, 11, 2)
    monkeypatch.setattr(config, "now", lambda s=None: datetime.combine(monday, time(9), config.tz(s)))
    _task("Отчёт", monday.isoformat())
    page = TestClient(app, base_url="http://localhost:8765").get("/").text
    assert "На неделю: окон 55 ч, задач 30 мин — реально" in page
    monkeypatch.setattr(config, "now", lambda s=None: _at(9))  # вторник и неделя реальна — строки нет
    assert "На неделю:" not in TestClient(app, base_url="http://localhost:8765").get("/").text


def test_flow_in_weekly_stats():
    from opsdash import digests

    tz = config.tz()
    a, b = _task("Из встречи", source_type="meeting"), _task("Из встречи 2", source_type="meeting")
    c = _task("Вручную")
    records.update_commitment(a, status="done")
    for cid in (a, b, c):
        store.execute("UPDATE commitments SET created=? WHERE id=?", (store.to_utc_iso(_at(10)), cid))
    store.execute("UPDATE commitments SET closed_at=? WHERE id=?", (store.to_utc_iso(_at(15)), a))
    store.execute("INSERT INTO meetings(id, title, started_at, status) VALUES('m1', 'Планёрка', ?, 'processed')",
                  (store.to_utc_iso(datetime.combine(DAY, time(11), tz)),))
    f = day.flow(date(2026, 11, 2), date(2026, 11, 8))
    assert (f["added"], f["closed"], f["from_meetings"], f["meetings"]) == (3, 1, 2, 1)
    assert f["line"] == "Ваши задачи: добавилось 3, закрыто 1 (долг вырос на 2); 1 разобранная встреча — каждая в среднем даёт вам 2 задачи"
    assert "Приток и отток: Ваши задачи:" in digests.week_stats(DAY)


# --- задачи по дням в Календаре: plan_range ------------------------------------------------------------

def _slots(v):
    return [(b["item"]["title"], day.hm(b["s"]), bool(b.get("late"))) for b in v["blocks"]]


def test_plan_range_deadlines_work_first_and_late(settings_with):
    settings = settings_with(calendar={"enabled": True})
    wed, thu, fri = (DAY + timedelta(days=n) for n in (1, 2, 3))
    _meeting("tue", DAY, "09:00", "19:30")  # сегодня окон нет
    _meeting("wed", wed, "10:00", "20:00")  # в среду — только 09:00–10:00
    day.set_estimate(_task("К среде, большая", wed.isoformat()), 120)  # к сроку не влезает
    day.set_estimate(_task("К четвергу", thu.isoformat()), 60)
    work = _task("В работе без срока")
    records.set_in_work(work, True)
    day.set_estimate(work, 60)
    _task("Без срока — по дням не раскладываем")
    day.set_estimate(_task("Сегодняшняя", DAY.isoformat()), 30)
    pr = day.plan_range(DAY - timedelta(days=1), 5, settings, _at(8))
    assert pr[DAY - timedelta(days=1)] == {"blocks": [], "late": [], "count": 0, "minutes": 0, "work": False}  # прошлое
    assert [x["item"]["title"] for x in pr[DAY]["late"]] == ["Сегодняшняя"]  # сегодня окон нет — к сроку не успела
    assert _slots(pr[wed]) == [("В работе без срока", "09:00", False)]  # «в работе» — первой
    assert [x["item"]["title"] for x in pr[wed]["late"]] == ["К среде, большая"]  # плашка в день срока
    assert _slots(pr[thu]) == [("Сегодняшняя", "09:00", True), ("К среде, большая", "09:30", True),
                               ("К четвергу", "11:30", False)]  # опоздавшие — с первого окна после срока
    assert pr[thu]["count"] == 3 and pr[thu]["minutes"] == 210 and not pr[thu]["work"] and pr[wed]["work"]
    assert pr[fri]["blocks"] == []


def test_plan_range_today_matches_overview(settings_with):
    settings = settings_with(calendar={"enabled": True})
    _busy_day()
    a = _task("Срочное", DAY.isoformat())
    day.set_estimate(a, 60)
    records.set_in_work(_task("Долгая", "2026-11-20"), True)
    day.decide(a, "plan", day=DAY, start="17:30")
    p = day.plan(DAY, settings, _at(9))
    pr = day.plan_range(DAY, 7, settings, _at(9))
    assert [(b["item"]["id"], b["s"], b["source"]) for b in pr[DAY]["blocks"]] == \
        [(b["item"]["id"], b["s"], b["source"]) for b in p["blocks"]]
    assert all(b["item"]["id"] not in {x["item"]["id"] for x in pr[DAY + timedelta(days=1)]["blocks"]} for b in p["blocks"])


def test_calendar_views_show_tasks_and_toggle(settings_with, monkeypatch):
    from opsdash.web.app import app

    settings_with(calendar={"enabled": True})
    monkeypatch.setattr(config, "now", lambda s=None: _at(8))
    work = _task("Регламент склада", "2026-11-05")
    records.set_in_work(work, True)
    plain = _task("Сводка по KPI", "2026-11-04")
    client = TestClient(app, base_url="http://localhost:8765")
    page = client.get("/calendar?view=week").text
    blocks = {int(i): c for c, i in re.findall(r'class="(pb[^"]*)" href="/commitments\?task=(\d+)"', page)}
    assert "work" in blocks[work].split() and "work" not in blocks[plain].split()
    assert "в работе</span><span><i></i>запланировано" in page
    month = client.get("/calendar?view=month").text
    cells = re.findall(r'<a class="mtasks[^"]*" href="\?start=([\d-]+)&view=week"[^>]*>(.*?)</a>', month, re.S)
    assert cells == [("2026-11-03", '<span class="tdot work" aria-hidden="true"></span>задачи: 1 · 30 мин'),
                     ("2026-11-04", '<span class="tdot" aria-hidden="true"></span>задачи: 1 · 30 мин')]
    assert f'href="/commitments?task={plain}">Сводка по KPI</a>' in client.get("/calendar?view=list").text
    off = client.get("/calendar?view=week&tasks=0")
    assert 'class="pb' not in off.text and off.cookies.get("cal_tasks") == "0"
    assert 'class="pb' not in client.get("/calendar?view=week").text  # выбор запомнен
    assert 'class="pb work' in client.get("/calendar?view=week&tasks=1").text
    home = client.get("/").text  # на Сводке — та же легенда
    assert re.search(rf'class="pb work[^"]*" href="/commitments\?task={work}"', home)
