"""Демо-данные: чтобы посмотреть дашборд и бота до подключения реальных источников.

./ops demo — добавить, ./ops demo --clear — убрать (реальные данные не трогаются).
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, time, timedelta

from opsdash import config, records, store

PEOPLE = [
    ("Пётр Иванов", "руководитель продаж", ["Петя", "Иванов"], "@p_ivanov", 101),
    ("Мария Кузнецова", "HR-директор", ["Маша", "Кузнецова"], "@m_kuz", 102),
    ("Олег Соколов", "финансовый директор", ["Олег", "Соколов"], "@sokolov_o", 103),
    ("Ирина Лебедева", "маркетинг", ["Ира", "Лебедева"], "@irina_l", 104),
    ("Дмитрий Орлов", "логистика и склад", ["Дима", "Орлов"], "@d_orlov", 105),
]

SUMMARY = """# Планёрка операционного блока

**Дата:** {date} · **Длительность:** 48 мин · **Участники:** Анна (директор), Пётр Иванов, Мария Кузнецова, Олег Соколов, Дмитрий Орлов

## Коротко
Сдвигаем запуск склада в Казани на 2 недели из-за задержки оборудования. Продажи просят пересмотреть план Q4 — решение после сверки с финансами. Найм двух менеджеров в регион согласован.

## Решения
1. Запуск склада в Казани переносится на 15.11 — Дмитрий согласует новый график с подрядчиком. [00:12:40]
2. План продаж Q4 пересматриваем после сверки с финансами в среду. [00:21:05]
3. Открываем 2 вакансии менеджеров в Казань, бюджет согласован. [00:33:18]

## Задачи
| Что | Кто | Срок | Kaiten |
|---|---|---|---|
| Новый график запуска склада с подрядчиком | Дмитрий Орлов | {d3} | предложено |
| Сверка плана Q4 продажи ↔ финансы | Олег Соколов | {d2} | предложено |
| Разместить 2 вакансии менеджеров (Казань) | Мария Кузнецова | {d5} | предложено |

## Договорённости
- **Анна обещала:** согласовать с собственником перенос запуска — до {d1} [00:14:02]
- **Пётр обещал Анне:** прислать прогноз по ключевым клиентам — до {d2} [00:19:30]
- **Олег обещал Анне:** подготовить вариант бюджета Q4 с урезанием на 10% — до {d3} [00:24:11]

## Открытые вопросы
- Кто замещает Дмитрия в отпуске с 20.11? Не решили.

## Связь с текущими задачами
- Карточка #4012 «Запуск склада Казань» — дата устарела, предложено обновить срок.
"""

TRANSCRIPT = """# Расшифровка: Планёрка операционного блока

Дата: {date}
Длительность: 48 мин
Источник: 2026-10-24 10.00.05 Планёрка/audio1234.m4a (аудио)
Спикеры определены автоматически (5): имена сопоставляются при разборе

[00:00:05] Спикер 1: Коллеги, доброе утро. Начнём со склада, Дима, что у нас по Казани?

[00:00:14] Спикер 2: Оборудование задерживается, поставщик сдвинул отгрузку. Реально запускаемся не раньше середины ноября.

[00:12:40] Спикер 1: Хорошо, тогда фиксируем пятнадцатое ноября. Дима, согласуй, пожалуйста, новый график с подрядчиком до конца недели.

[00:14:02] Спикер 1: Я сама поговорю с собственником про перенос, до завтра.

[00:19:30] Спикер 3: Я пришлю прогноз по ключевым клиентам к среде, чтобы было с чем идти к Олегу.

[00:24:11] Спикер 4: Сделаю вариант бюджета с урезанием на десять процентов.

[00:33:18] Спикер 5: По найму — две вакансии в Казань, бюджет есть, размещаю на этой неделе.
"""


def _ts(dt: datetime) -> str:
    return store.to_utc_iso(dt)


def seed() -> None:
    clear()
    tz = config.tz()
    now = config.now()
    today = now.date()
    ids: dict[str, list] = {k: [] for k in ("people", "messages", "commitments", "attention", "proposals", "outbox")}

    def d(n: int) -> str:
        return (today + timedelta(days=n)).isoformat()

    for name, role, aliases, tg, kid in PEOPLE:
        ids["people"].append(records.upsert_person(name, role=role, aliases=aliases, telegram=tg, kaiten_user_id=kid))

    # Kaiten
    store.set_state("kaiten_me", {"id": 100, "name": "Анна Смирнова", "username": "a_smirnova"})
    users = [(100, "Анна Смирнова", "a_smirnova")] + [(p[4], p[0], p[3].lstrip("@")) for p in PEOPLE]
    for uid, name, username in users:
        store.execute("INSERT OR REPLACE INTO kaiten_users(id, full_name, username, email) VALUES(?,?,?,?)",
                      (uid, name, username, f"{username}@example.ru"))
    cards = [
        (4012, "Запуск склада Казань", "В работе", 2, 105, "Дмитрий Орлов", 9, d(-3)),
        (4020, "Договор с новым перевозчиком", "Согласование", 2, 105, "Дмитрий Орлов", 5, d(4)),
        (4031, "Регламент приёмки возвратов", "В работе", 2, 102, "Мария Кузнецова", 4, None),
        (4044, "Бюджет Q4: сценарии", "В работе", 2, 103, "Олег Соколов", 1, d(3)),
        (4051, "CRM: воронка для B2B", "В работе", 2, 101, "Пётр Иванов", 6, d(10)),
        (4060, "Обновить KPI отдела продаж", "Очередь", 1, 101, "Пётр Иванов", 12, None),
        (4071, "Запуск рекламной кампании «Осень»", "В работе", 2, 104, "Ирина Лебедева", 2, d(1)),
        (4080, "Инвентаризация склада Москва", "Готово", 3, 105, "Дмитрий Орлов", 1, None),
    ]
    for cid, title, column, state, rid, rname, idle, due in cards:
        moved = _ts(now - timedelta(days=idle, hours=2))
        store.execute(
            """INSERT OR REPLACE INTO kaiten_cards(id, space_id, space_title, board_id, board_title, column_id, column_title,
               lane_id, lane_title, title, description, state, condition, due_date, responsible_id, responsible_name,
               owner_id, owner_name, members, created, updated, last_moved_at, column_changed_at, url, synced_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,1,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (cid, 1, "Операционка", 10, "Проекты", 100 + state, column, None, None, title, "", state, due, rid, rname,
             100, "Анна Смирнова", json.dumps([{"id": rid, "name": rname, "type": 2}], ensure_ascii=False),
             _ts(now - timedelta(days=30)), moved, moved, moved, f"https://demo.kaiten.ru/space/1/card/{cid}", store.utcnow()),
        )
    store.set_state("demo_cards", [c[0] for c in cards])

    # календарь
    day0 = datetime.combine(today, time.min, tz)
    events = [
        ("demo-ev-1", "Планёрка операционного блока", 10, 0, 60, ["p_ivanov@example.ru", "m_kuz@example.ru", "sokolov_o@example.ru"]),
        ("demo-ev-2", "1:1 с Петром Ивановым", 13, 0, 30, ["p_ivanov@example.ru"]),
        ("demo-ev-3", "Созвон с подрядчиком по складу", 16, 30, 45, ["d_orlov@example.ru", "podryad@example.com"]),
    ]
    for uid, title, hh, mm, dur, att in events:
        st = day0 + timedelta(hours=hh, minutes=mm)
        store.execute(
            'INSERT OR REPLACE INTO events(uid, calendar, title, start, "end", all_day, location, description, organizer, '
            "attendees, url, synced_at) VALUES(?,?,?,?,?,0,?,?,?,?,?,?)",
            (uid, "Рабочий", title, _ts(st), _ts(st + timedelta(minutes=dur)), "Телемост", "", "a_smirnova@example.ru",
             json.dumps([{"email": a, "name": a.split("@")[0], "status": ""} for a in att]), "", store.utcnow()),
        )

    # встречи
    mdate = datetime.combine(today - timedelta(days=1), time(10, 0), tz)
    mid = f"{mdate:%Y-%m-%d_%H%M}_planerka-operacionnogo-bloka"
    folder = records.meeting_dir(mid)
    folder.mkdir(parents=True, exist_ok=True)
    def dm(n: int) -> str:
        return (today + timedelta(days=n)).strftime("%d.%m")

    fmt = {"date": mdate.strftime("%d.%m.%Y %H:%M"), "d1": dm(1), "d2": dm(2), "d3": dm(3), "d5": dm(5)}
    (folder / "summary.md").write_text(SUMMARY.format(**fmt), encoding="utf-8")
    (folder / "transcript.md").write_text(TRANSCRIPT.format(**fmt), encoding="utf-8")
    (folder / "meta.json").write_text(json.dumps({"id": mid, "title": "Планёрка операционного блока", "demo": True},
                                                 ensure_ascii=False), encoding="utf-8")
    store.execute(
        """INSERT OR REPLACE INTO meetings(id, title, started_at, duration_sec, source_file, status, progress, participants,
           headline, created, updated) VALUES(?,?,?,?,?, 'processed', 1, ?, ?, ?, ?)""",
        (mid, "Планёрка операционного блока", _ts(mdate), 48 * 60, "demo/audio1234.m4a",
         json.dumps(["Пётр Иванов", "Мария Кузнецова", "Олег Соколов", "Дмитрий Орлов"], ensure_ascii=False),
         "Склад в Казани переносим на 15.11; план Q4 — после сверки с финансами", store.utcnow(), store.utcnow()),
    )
    mid2 = f"{now:%Y-%m-%d}_1100_sozvon-s-marketingom"
    records.meeting_dir(mid2).mkdir(parents=True, exist_ok=True)
    (records.meeting_dir(mid2) / "transcript.md").write_text(
        "# Расшифровка: Созвон с маркетингом\n\n[00:00:04] Спикер 1: Ира, по «Осени» — что с бюджетом?\n\n"
        "[00:00:11] Спикер 2: Смета готова, нужен только ваш ок до завтра.\n", encoding="utf-8")
    store.execute(
        """INSERT OR REPLACE INTO meetings(id, title, started_at, duration_sec, source_file, status, progress, participants,
           created, updated) VALUES(?,?,?,?,?, 'transcribed', 1, '[]', ?, ?)""",
        (mid2, "Созвон с маркетингом", _ts(datetime.combine(today, time(11, 0), tz)), 22 * 60, "demo/zoom.m4a",
         store.utcnow(), store.utcnow()),
    )
    store.set_state("demo_meetings", [mid, mid2])

    # чаты и сообщения
    chats = [
        ("telegram", "-1001", "Операционка", "group"), ("telegram", "201", "Пётр Иванов", "private"),
        ("ymessenger", "0/0/demo-sklad", "Склад и логистика", "group"), ("mail", "INBOX", "Почта: входящие", "mail"),
    ]
    for src, cid, title, kind in chats:
        store.upsert_chat(src, cid, title, kind, enabled=1)
    msgs = [
        ("telegram", "-1001", "Операционка", "Олег Соколов", False, "Коллеги, сверку по Q4 переносим на четверг, в среду аудит.", 26),
        ("telegram", "-1001", "Операционка", "Ирина Лебедева", False, "Анна, нужно ваше ок по бюджету кампании «Осень» до завтра 12:00, иначе не успеем запустить.", 3),
        ("telegram", "201", "Пётр Иванов", "Пётр Иванов", False, "Анна, прогноз по клиентам будет в пятницу, в среду не успеваю — ок?", 2),
        ("ymessenger", "0/0/demo-sklad", "Склад и логистика", "Дмитрий Орлов", False, "Подрядчик подтвердил новый график, отправил его в карточку.", 1),
        ("mail", "INBOX", "Почта: входящие", "Мария Кузнецова", False, "Тема: Вакансии Казань\nКому: Анна\n\nАнна, добрый день! Подготовила тексты вакансий, посмотрите, пожалуйста, до среды.", 5),
    ]
    for i, (src, cid, title, sender, out, text, hours_ago) in enumerate(msgs):
        store.add_message({"source": src, "chat_id": cid, "chat_title": title, "msg_id": f"demo-{i}",
                           "sender_name": sender, "is_outgoing": out, "text": text,
                           "ts": _ts(now - timedelta(hours=hours_ago))})
        mrow = store.row("SELECT id FROM messages WHERE source=? AND chat_id=? AND msg_id=?", (src, cid, f"demo-{i}"))
        ids["messages"].append(mrow["id"])
    store.execute(f"UPDATE messages SET processed=1 WHERE id IN ({','.join('?' for _ in ids['messages'][:3])})",
                  ids["messages"][:3])

    ids["attention"] += [
        records.add_attention(message_id=ids["messages"][1], priority="high",
                              summary="Ирина ждёт ок по бюджету кампании «Осень» до завтра 12:00",
                              why="без согласования кампания не стартует в срок",
                              draft="Ира, бюджет согласую — пришлите, пожалуйста, итоговую смету одним файлом."),
        records.add_attention(message_id=ids["messages"][2], summary="Пётр просит сдвинуть прогноз по клиентам на пятницу",
                              why="влияет на сверку плана Q4",
                              draft="Петя, ок на пятницу, но тогда до 12:00 — сверка с Олегом в четверг."),
        records.add_attention(message_id=ids["messages"][4], priority="low",
                              summary="Мария просит посмотреть тексты вакансий до среды"),
    ]

    src_m = f"meeting:{mid}"
    commitments = [
        ("mine", "Анна", "Собственник", "Согласовать с собственником перенос запуска склада", d(1), src_m),
        ("mine", "Анна", "Ирина Лебедева", "Дать ок по бюджету кампании «Осень»", d(1), "telegram:demo"),
        ("mine", "Анна", "Мария Кузнецова", "Посмотреть тексты вакансий", d(-1), "mail:demo"),
        ("to_me", "Пётр Иванов", "Анна", "Прислать прогноз по ключевым клиентам", d(2), src_m),
        ("to_me", "Олег Соколов", "Анна", "Вариант бюджета Q4 с урезанием на 10%", d(3), src_m),
        ("to_me", "Дмитрий Орлов", "Анна", "Новый график запуска склада с подрядчиком", d(-2), src_m),
        ("other", "Мария Кузнецова", "Дмитрий Орлов", "Найти замещающего на время отпуска Дмитрия", None, src_m),
    ]
    for direction, who, whom, what, due, source in commitments:
        st, _, ref = source.partition(":")
        ids["commitments"].append(records.add_commitment(direction=direction, who=who, whom=whom, what=what, due=due,
                                                         source_type=st, source_ref=ref))

    batch = f"meeting:{mid}"
    proposals_data = [
        ("Карточка «Новый график запуска склада с подрядчиком» → Дмитрий Орлов, до " + d(3)[8:10] + "." + d(3)[5:7],
         {"title": "Новый график запуска склада с подрядчиком", "responsible_id": 105, "responsible_name": "Дмитрий Орлов",
          "due": d(3), "target": "Операционка", "board_id": 10, "space_id": 1}),
        ("Карточка «Сверка плана Q4: продажи ↔ финансы» → Олег Соколов, до " + d(2)[8:10] + "." + d(2)[5:7],
         {"title": "Сверка плана Q4: продажи ↔ финансы", "responsible_id": 103, "responsible_name": "Олег Соколов",
          "due": d(2), "target": "Операционка", "board_id": 10, "space_id": 1}),
        ("Изменить #4012 «Запуск склада Казань»: срок → 15.11", {"card_id": 4012, "fields": {"due_date": f"{today.year}-11-15"}}),
    ]
    for i, (title, payload) in enumerate(proposals_data):
        kind = "kaiten.update" if "card_id" in payload else "kaiten.create_card"
        ids["proposals"].append(store.execute(
            "INSERT INTO proposals(kind, title, payload, reason, source_ref, batch, status, created) VALUES(?,?,?,?,?,?,'pending',?)",
            (kind, title, json.dumps(payload, ensure_ascii=False), "договорились на планёрке", f"meeting:{mid}", batch,
             store.utcnow()),
        ))

    feed = [
        ("✅ <b>Разобрала планёрку операционного блока</b>\nСклад в Казани переносим на 15.11; 3 решения, 3 задачи.", 20),
        ("🔴 Ирина ждёт ок по бюджету кампании «Осень» до завтра 12:00", 3),
    ]
    for text, hours in feed:
        ids["outbox"].append(store.execute(
            "INSERT INTO outbox(text, buttons, urgent, created, sent_at) VALUES(?, '[]', 0, ?, ?)",
            (text, _ts(now - timedelta(hours=hours)), _ts(now - timedelta(hours=hours)))))
    store.set_state("demo_ids", ids)
    store.execute(
        "INSERT OR REPLACE INTO jobs(name, last_run, last_ok, running) VALUES('collect:kaiten', ?, ?, 0)",
        (store.utcnow(), store.utcnow()),
    )


def clear() -> None:
    ids = store.get_state("demo_ids") or {}
    for table, values in ids.items():
        if values:
            store.execute(f"DELETE FROM {table} WHERE id IN ({','.join('?' for _ in values)})", values)
    for mid in store.get_state("demo_meetings") or []:
        store.execute("DELETE FROM meetings WHERE id=?", (mid,))
        shutil.rmtree(records.meeting_dir(mid), ignore_errors=True)
    cards = store.get_state("demo_cards") or []
    if cards:
        store.execute(f"DELETE FROM kaiten_cards WHERE id IN ({','.join('?' for _ in cards)})", cards)
    store.execute("DELETE FROM events WHERE uid LIKE 'demo-ev-%'")
    store.execute("DELETE FROM chats WHERE chat_id IN ('-1001', '201', '0/0/demo-sklad') ")
    store.execute("DELETE FROM attention WHERE message_id NOT IN (SELECT id FROM messages)")
    if ids:
        store.execute("DELETE FROM kaiten_users WHERE id BETWEEN 100 AND 105")
        if (store.get_state("kaiten_me") or {}).get("id") == 100:
            store.set_state("kaiten_me", None)
    for key in ("demo_ids", "demo_meetings", "demo_cards"):
        store.set_state(key, None)
