"""Демо-стенд для видео-онбординга: отдельная папка данных (OPSDASH_HOME), ваши данные не затрагиваются.

Запуск из папки PULSE:  .venv/bin/python onboarding/build/seed_demo.py
Потом веб-интерфейс стенда:  OPSDASH_HOME=onboarding/build/.demo-home .venv/bin/python -m uvicorn opsdash.web.app:app --port 8790
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import zipfile
from datetime import datetime, time, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOME = HERE / ".demo-home"
if HOME.exists():
    shutil.rmtree(HOME)  # стенд всегда собирается заново
os.environ["OPSDASH_HOME"] = str(HOME)
sys.path.insert(0, str(HERE.parent.parent))

from opsdash import anthology, config, demo, docs, records, store, voice  # noqa: E402

config.ensure_dirs()

DOCS = HOME / "Документы компании"
config.update_settings("owner", {"name": "Анна Смирнова", "timezone": "Europe/Moscow"})
for section in ("telegram", "telegram_bot", "mail", "calendar", "kaiten"):
    config.update_settings(section, {"enabled": True})
config.update_settings("kaiten", {"domain": "demo.kaiten.ru"})
config.update_settings("knowledge", {"folders": [str(DOCS)]})

demo.seed()
tz = config.tz()
now = config.now()
today = now.date()


def at(day_offset: int, hh: int, mm: int = 0) -> datetime:
    return datetime.combine(today + timedelta(days=day_offset), time(hh, mm), tz)


# --- календарь: день с прошедшими и будущими встречами, неделя вперёд ---------------------
store.execute("DELETE FROM events WHERE uid LIKE 'demo-ev-%'")
events = [
    (0, 9, 30, 30, "Утренний статус операционки", ["d_orlov", "m_kuz"]),
    (0, 11, 0, 60, "Планёрка операционного блока", ["p_ivanov", "m_kuz", "sokolov_o", "d_orlov"]),
    (0, 11, 30, 30, "Созвон с маркетингом", ["irina_l"]),
    (0, 13, 0, 30, "1:1 с Петром Ивановым", ["p_ivanov"]),
    (0, 15, 0, 60, "Бюджет Q4: сценарии", ["sokolov_o"]),
    (0, 17, 0, 45, "Созвон с подрядчиком по складу", ["d_orlov", "podryad"]),
    (0, 18, 30, 30, "Итоги дня", []),
    (1, 10, 0, 60, "Сверка плана Q4: продажи и финансы", ["p_ivanov", "sokolov_o"]),
    (1, 14, 0, 45, "Собеседование: менеджер в Казань", ["m_kuz"]),
    (2, 11, 0, 90, "Стратсессия: склад в Казани", ["d_orlov", "p_ivanov"]),
    (2, 16, 0, 30, "1:1 с Марией Кузнецовой", ["m_kuz"]),
    (3, 12, 0, 60, "Встреча с перевозчиком", ["d_orlov"]),
    (4, 10, 0, 60, "Планёрка операционного блока", ["p_ivanov", "m_kuz", "sokolov_o", "d_orlov"]),
    (-1, 10, 0, 60, "Планёрка операционного блока", ["p_ivanov", "m_kuz", "sokolov_o", "d_orlov"]),
    (-1, 15, 0, 45, "Ревью воронки продаж", ["p_ivanov"]),
]
for i, (day, hh, mm, dur, title, who) in enumerate(events):
    st = at(day, hh, mm)
    store.execute(
        'INSERT OR REPLACE INTO events(uid, calendar, title, start, "end", all_day, location, description, organizer, '
        "attendees, url, synced_at) VALUES(?,?,?,?,?,0,?,?,?,?,?,?)",
        (f"demo-ev-{i}", "Рабочий", title, store.to_utc_iso(st), store.to_utc_iso(st + timedelta(minutes=dur)),
         "Телемост" if who else "", "", "a_smirnova@example.ru",
         json.dumps([{"email": f"{w}@example.ru", "name": w, "status": ""} for w in who]), "", store.utcnow()))

# --- задачи и договорённости: побольше и разные сроки, пара закрытых --------------------------
extra = [
    ("mine", "Анна Смирнова", "Олег Соколов", "Утвердить сценарий бюджета Q4", 4, "meeting"),
    ("to_me", "Ирина Лебедева", "Анна Смирнова", "Прислать итоговую смету кампании «Осень»", 0, "telegram"),
    ("other", "Пётр Иванов", "Олег Соколов", "Передать данные по воронке B2B для сверки", 1, "meeting"),
]
planerka = next(m for m in store.get_state("demo_meetings") if "planerka" in m)
for direction, who, whom, what, due, src in extra:
    records.add_commitment(direction=direction, who=who, whom=whom, what=what, due=(today + timedelta(days=due)).isoformat(),
                           source_type=src, source_ref=planerka if src == "meeting" else None)
for what in ("Согласовать график отпусков склада", "Подписать договор с новым перевозчиком"):
    cid = records.add_commitment(direction="mine", who="Анна Смирнова", whom="Дмитрий Орлов", what=what, source_type="manual")
    records.update_commitment(cid, status="done")

# --- база знаний ---------------------------------------------------------------------------------
for kind, title, details, days_ago, project in [
    ("decision", "Запуск склада в Казани переносим на 15.11", "Оборудование задерживается; новый график согласует Дмитрий Орлов.", 1, "Склад Казань"),
    ("decision", "План продаж Q4 пересматриваем только после сверки с финансами", "Без сверки с бюджетом план не меняем.", 1, "План Q4"),
    ("lesson", "Оборудование для склада заказываем с запасом в 3 недели", "Поставщик дважды сдвигал отгрузку — запуск съехал.", 8, "Склад Казань"),
    ("win", "Кампания «Весна» окупилась за 6 недель", "Повторяем механику с промокодами партнёров в «Осени».", 40, "Маркетинг"),
    ("decision", "Найм в регионы — только через внутренние рекомендации и hh", "Агентства дали низкую конверсию в выход.", 20, "HR"),
]:
    anthology.add(kind=kind, title=title, details=details, happened=(today - timedelta(days=days_ago)).isoformat(),
                         project=project)

# --- документы: образцы разных типов (только для поиска — файлы минимальные) -----------------------
DOCS.mkdir(parents=True, exist_ok=True)


def zip_doc(path: Path, files: dict[str, str]) -> None:
    with zipfile.ZipFile(path, "w") as z:
        for name, body in files.items():
            z.writestr(name, body)


A = 'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'
slides = ["Бюджет Q4: три сценария", "Базовый: выручка 182 млн, маржа 21%", "Сокращение на 10%: урезаем маркетинг и найм",
          "Рост: запуск склада в Казани в ноябре"]
zip_doc(DOCS / "Бюджет Q4 — сценарии.pptx", {
    f"ppt/slides/slide{i}.xml": f'<p:sld xmlns:p="x" {A}><a:p><a:r><a:t>{t}</a:t></a:r></a:p></p:sld>'
    for i, t in enumerate(slides, 1)})
zip_doc(DOCS / "QBR Q3 — коммерция.pptx", {
    "ppt/slides/slide1.xml": f'<p:sld xmlns:p="x" {A}><a:p><a:r><a:t>QBR Q3: конверсия пролонгации выросла до 41%</a:t></a:r></a:p></p:sld>'})
W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
zip_doc(DOCS / "Регламент приёмки возвратов.docx", {"word/document.xml": (
    f'<w:document {W}><w:body>' + "".join(f"<w:p><w:r><w:t>{t}</w:t></w:r></w:p>" for t in (
        "Регламент приёмки возвратов", "Возврат принимается в течение 14 дней при наличии акта.",
        "Ответственный — руководитель склада.")) + "</w:body></w:document>")})
S = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
R = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
rows = [("Клиент", "Q3, млн", "Прогноз Q4, млн"), ("Альфа-Ритейл", "12,4", "14,0"), ("ТехноСнаб", "8,1", "7,5"),
        ("Северный путь", "5,6", "6,2")]
zip_doc(DOCS / "Прогноз по ключевым клиентам.xlsx", {
    "xl/workbook.xml": f'<workbook {S} {R}><sheets><sheet name="Прогноз" sheetId="1" r:id="rId1"/></sheets></workbook>',
    "xl/_rels/workbook.xml.rels": '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                                  '<Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>',
    "xl/worksheets/sheet1.xml": f'<worksheet {S}><sheetData>' + "".join(
        "<row>" + "".join(f'<c t="inlineStr"><is><t>{v}</t></is></c>' for v in r) + "</row>" for r in rows)
        + "</sheetData></worksheet>"})
(DOCS / "Склад Казань — график запуска.md").write_text(
    "# Склад Казань — график запуска\n\nЗапуск переносится на 15.11 из-за задержки оборудования.\n"
    "Подрядчик подтвердил новый график монтажа стеллажей.\n", encoding="utf-8")
(DOCS / "Вакансии Казань.md").write_text(
    "# Вакансии Казань\n\nДва менеджера по продажам, бюджет согласован на планёрке.\n", encoding="utf-8")
(DOCS / "План продаж Q4.csv").write_text("Месяц;План, млн;Факт, млн\nОктябрь;60;58\nНоябрь;62;\nДекабрь;70;\n",
                                          encoding="utf-8")
(DOCS / "Заметки со стратсессии.txt").write_text("Стратсессия: приоритеты — склад в Казани, B2B-воронка, найм.\n",
                                                  encoding="utf-8")
ages = {  # (создан, изменён) — дней назад; на macOS дата изменения раньше даты создания сдвигает и её
    "Бюджет Q4 — сценарии.pptx": (21, 1), "QBR Q3 — коммерция.pptx": (35, 12), "Регламент приёмки возвратов.docx": (120, 30),
    "Прогноз по ключевым клиентам.xlsx": (14, 2), "Склад Казань — график запуска.md": (10, 1), "Вакансии Казань.md": (6, 3),
    "План продаж Q4.csv": (40, 5), "Заметки со стратсессии.txt": (60, 45)}
for name, (born, changed) in ages.items():
    for days in (born, changed):
        ts = (now - timedelta(days=days, hours=3)).timestamp()
        os.utime(DOCS / name, (ts, ts))
print(docs.index())

# --- встреча: спикеры и догадки Claude, кто есть кто -------------------------------------------
mid = planerka
folder = records.meeting_dir(mid)
lines = [
    (5, 40, "Спикер 1", "Коллеги, доброе утро. Начнём со склада. Дима, что у нас по Казани, успеваем к ноябрю?"),
    (41, 95, "Спикер 2", "Оборудование задерживается, поставщик сдвинул отгрузку. Реально запускаемся не раньше середины ноября, график пришлю."),
    (96, 130, "Спикер 1", "Хорошо, тогда фиксируем пятнадцатое ноября. Дима, согласуй, пожалуйста, новый график с подрядчиком до конца недели."),
    (131, 180, "Спикер 3", "По продажам: план четвёртого квартала нужно пересмотреть, без Казани мы его не делаем. Я пришлю прогноз по ключевым клиентам к среде."),
    (181, 230, "Спикер 4", "Тогда я подготовлю вариант бюджета с урезанием на десять процентов, чтобы было с чем сравнивать."),
    (231, 280, "Спикер 5", "По найму — две вакансии в Казань, бюджет есть, размещаю на этой неделе. Нужен только ваш ок по текстам."),
    (281, 300, "Спикер 1", "Отлично, Маша, пришли тексты, посмотрю до среды."),
]
utterances = [{"start": float(s), "end": float(e), "speaker": spk, "text": t} for s, e, spk, t in lines]
(folder / "transcript.json").write_text(json.dumps(utterances, ensure_ascii=False, indent=1), encoding="utf-8")
meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
meta.update(speakers=[f"Спикер {i}" for i in range(1, 6)], speaker_guesses={
    "Спикер 2": {"name": "Дмитрий Орлов", "why": "к нему обращаются «Дима» и он отвечает про склад [00:00:41]"},
    "Спикер 3": {"name": "Пётр Иванов", "why": "говорит о плане продаж и прогнозе по клиентам [00:02:11]"},
    "Спикер 4": {"name": "Олег Соколов", "why": "готовит вариант бюджета [00:03:01]"},
    "Спикер 5": {"name": "Мария Кузнецова", "why": "ведёт найм, к ней обращаются «Маша» [00:04:41]"},
})
(folder / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

# голос одного сотрудника уже запомнен — чтобы в «Людях» было видно отметку
petr = records.find_person("Пётр Иванов")
voice.remember_person(petr["id"], "demo-0", "Спикер 3", [1.0] + [0.0] * 15, 60)

# --- чаты Telegram в настройках: разные типы и время последнего сообщения ------------------------
chats = [
    ("301", "Руководители направлений", "group", 1, 0.2), ("302", "Ольга, ассистент", "private", 1, 0.5), ("303", "Склад и логистика", "group", 1, 1),
    ("304", "Ирина Лебедева", "private", 1, 3), ("305", "Маркетинг — запуск «Осени»", "group", 1, 5),
    ("306", "Олег Соколов", "private", 0, 7), ("307", "Новости отрасли", "channel", 0, 9), ("308", "Мама", "private", -1, 10),
    ("309", "Доставка еды", "bot", 0, 26), ("310", "Совет директоров", "group", 1, 30), ("311", "Курсы английского", "channel", -1, 50),
    ("312", "Мария Кузнецова", "private", 1, 52), ("313", "Такси", "bot", 0, 80), ("314", "Дмитрий Орлов", "private", 1, 90),
]
for cid, title, kind, enabled, hours in chats:
    store.upsert_chat("telegram", cid, title, kind, enabled=enabled,
                      last_ts=store.to_utc_iso(now - timedelta(hours=hours)))

# --- источники «на связи» ---------------------------------------------------------------------------
for job in ("collect:telegram", "collect:mail", "collect:calendar", "collect:kaiten", "docs-index"):
    store.execute("INSERT OR REPLACE INTO jobs(name, last_run, last_ok, running) VALUES(?,?,?,0)",
                  (job, store.to_utc_iso(now - timedelta(minutes=7)), store.to_utc_iso(now - timedelta(minutes=7))))
store.set_state("telegram_me", {"id": 1, "name": "Анна Смирнова", "username": "a_smirnova"})
print("Демо-стенд готов:", HOME)
