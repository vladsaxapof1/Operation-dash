from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient

from opsdash import anthology, cli, config, docs, store

NOTE = """---
type: решение
date: 2026-09-29
тема: "[[ОРК]]"
встреча: "[[2026-09-29 17-55 Встреча]]"
статус: принято
tags: [решение]
---

# 2026-09-29 Средний чек апсейла ОРК в плане ставим 15 тысяч

## Что решили
В плане Q4 средний чек апсейла ОРК ставим **15 тыс.** вместо 12 тыс.

## Почему
Чек в апсейле не управляется: менеджер продаёт то, что [[Клиент|клиент]] просит.

## Кто решил
- [[Олег]], с [[Игорь Смирнов|Игорем Смирновым]]
- Павел — базы и сегмент Восток

## Последствия
- Выручка апсейла ОРК в плане немного вырастет.

## Источник
- Встреча: [[2026-09-29 17-55 Встреча]]
"""


def test_add_dedupe_search_and_export():
    first = anthology.add(kind="decision", title="Чек в автоворонке ЛК не снижаем", project="Автоворонка",
                          happened="2026-10-01", details="Запуск — тестом гипотез на части лидов")
    assert anthology.add(kind="decision", title="чек в автоворонке ЛК не снижаем") == first  # без дублей
    lesson = anthology.add(kind="lesson", title="Лиды SaaS-агентов в Baseline не закладываем", status="proposed",
                           author="claude")
    with pytest.raises(anthology.AnthologyError):
        anthology.add(kind="idea", title="x")
    assert [r["title"] for r in anthology.items(q="автоворонки")] == ["Чек в автоворонке ЛК не снижаем"]  # окончания
    anthology.add(kind="decision", title="Тесты гипотез запускаем на части лидов", details="Как с чеком в автоворонке ЛК")
    assert anthology.items(q="автоворонка")[0]["title"] == "Чек в автоворонке ЛК не снижаем"  # совпадение в сути — выше
    assert anthology.counts() == {"decision": 2, "lesson": 0, "win": 0, "proposed": 1}
    anthology.update(lesson, status="accepted")
    anthology.update(first, status="outdated")
    assert first not in [r["id"] for r in anthology.items()]  # устаревшее не мешает актуальным
    assert {first, lesson} <= {r["id"] for r in anthology.items(status="all")}
    text = (config.DATA_DIR / "knowledge" / anthology.EXPORT).read_text(encoding="utf-8")
    assert "## Уроки" in text and "Лиды SaaS-агентов" in text and "Чек в автоворонке" not in text


def test_decision_notes_come_from_document_folders(tmp_path, settings_with):
    root = tmp_path / "Компания"
    (root / "Решения").mkdir(parents=True)
    note = root / "Решения" / "2026-09-29 Средний чек апсейла ОРК в плане ставим 15 тысяч.md"
    note.write_text(NOTE, encoding="utf-8")
    (root / "просто заметка.md").write_text("---\ntype: встреча\n---\n# Встреча", encoding="utf-8")
    settings_with(knowledge={"folders": [str(root)]})
    assert docs.index().endswith("заметок-решений: новых 1, обновлено 0")
    r = anthology.items()[0]
    assert (r["kind"], r["title"], r["happened"], r["project"]) == (
        "decision", "Средний чек апсейла ОРК в плане ставим 15 тысяч", "2026-09-29", "ОРК")
    assert "ставим 15 тыс. вместо" in r["details"] and "Почему: Чек в апсейле не управляется" in r["details"]
    assert "то, что клиент просит" in r["details"] and "Последствия: - Выручка апсейла" in r["details"]
    assert r["people"] == ["Олег", "Игорь Смирнов", "Павел"] and r["quote"] == "Встреча: 2026-09-29 17-55 Встреча"
    assert (r["source_type"], r["source_ref"], r["author"]) == ("document", str(note), "notes")

    note.write_text(NOTE.replace("статус: принято", "статус: отменено").replace("15 тыс.", "12 тыс."), encoding="utf-8")
    assert docs.index().endswith("заметок-решений: новых 0, обновлено 1")
    r = anthology.get(r["id"])
    assert r["status"] == "outdated" and "12 тыс." in r["details"]


def test_cli_kb(monkeypatch, capsys):
    monkeypatch.setenv("OPSDASH_AGENT", "1")
    cli.main(["kb", "add", "--kind", "win", "--title", "Рассылка по триалам дала +12% продлений",
              "--project", "Розница", "--source", "meeting:m1", "--quote", "[00:12:01] сработало", "--proposed"])
    assert "как предложение" in capsys.readouterr().out
    r = anthology.items()[0]
    assert (r["status"], r["author"], r["source_type"], r["source_ref"]) == ("proposed", "claude", "meeting", "m1")
    cli.main(["kb", "search", "рассылки триалов", "--details"])
    assert "Удача: Рассылка по триалам" in capsys.readouterr().out
    cli.main(["kb", "set", str(r["id"]), "--status", "accepted", "--note", "подтверждено"])
    assert anthology.get(r["id"])["status"] == "accepted"
    with pytest.raises(SystemExit):
        cli.main(["kb", "add", "--kind", "win", "--title", "x", "--date", "26.10.2026"])


def test_kb_page():
    from opsdash.web.app import app

    store.execute("INSERT INTO meetings(id, title, status) VALUES('m1', 'План МСБ', 'processed')")
    anthology.add(kind="decision", title="Цели конверсии розницы ставим по новому маппингу", project="Розница",
                  happened="2026-10-01", source_type="meeting", source_ref="m1", details="Маркетинг 8,75%")
    anthology.add(kind="decision", title="Средний чек апсейла ОРК — 15 тыс.", project="ОРК", happened="2026-09-29")
    proposed = anthology.add(kind="lesson", title="Лиды SaaS-агентов в Baseline не закладываем", status="proposed")
    client = TestClient(app, base_url="http://localhost:8765")
    page = client.get("/kb").text
    assert "База знаний" in page and "Октябрь 2026" in page and "Сентябрь 2026" in page
    assert "Предложено Claude" in page and "Лиды SaaS-агентов" in page and ">План МСБ</a>" in page
    assert "Средний чек" not in client.get("/kb?project=Розница").text
    assert "Цели конверсии" in client.get("/kb?q=маппинга").text
    found = client.get("/kb", params={"q": "чек апсейла"}).text
    assert "Найдено: 1" in found and "Средний чек апсейла" in found and "Сентябрь 2026" not in found
    r = client.post(f"/kb/{proposed}", data={"status": "accepted"}, follow_redirects=False)
    assert "Принято" in unquote(r.headers["location"]) and anthology.get(proposed)["status"] == "accepted"
    client.post("/kb", data={"kind": "win", "title": "Автоворонка ЛК окупилась за месяц", "project": "Автоворонка"})
    assert anthology.counts()["win"] == 1


def test_weekly_review_runs_on_friday(monkeypatch, settings_with):
    from datetime import datetime

    from opsdash import jobs

    settings_with(schedule={"anthology_weekly": "16:00", "digest_morning": "", "digest_evening": "", "stale_check": ""})
    submitted = []
    monkeypatch.setattr(jobs, "submit", lambda name, kind="light", args=None: submitted.append((name, kind)) or True)
    for day, expected in ((29, False), (30, True)):  # четверг — нет, пятница после 16:00 — да
        monkeypatch.setattr(config, "now", lambda s=None, d=day: datetime(2026, 10, d, 16, 30, tzinfo=config.tz()))
        submitted.clear()
        jobs.tick()
        assert (("anthology", "heavy") in submitted) is expected
    assert "anthology" in jobs.REGISTRY and jobs.TITLES["anthology"].startswith("База знаний")
