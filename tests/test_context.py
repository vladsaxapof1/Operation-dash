from datetime import datetime

from fastapi.testclient import TestClient

from opsdash import anthology, brain, config, context, jobs, records, store


def test_context_summary_sections(settings_with):
    settings_with(owner={"name": "Анна"})
    today = records.today_local()
    anthology.add(kind="decision", title="Средний чек апсейла ОРК — 15 тыс.", project="ОРК", happened=today.isoformat())
    anthology.add(kind="decision", title="Старое решение", project="ОРК", happened="2020-01-01")
    anthology.add(kind="lesson", title="Лиды SaaS-агентов в Baseline не закладываем")
    records.add_commitment(direction="mine", what="Собрать сценарий", whom="Тимур", due=today.isoformat())
    store.set_state("kaiten_me", {"id": 7})
    store.execute("INSERT INTO kaiten_cards(id, space_title, board_title, column_title, title, state, condition, "
                  "responsible_id) VALUES(1, 'Sales', 'Аналитика', 'В работе', 'Бюджет 2027', 2, 1, 7)")
    text = context.build()
    assert text.startswith("# Контекст для Claude — Анна")
    assert "Ещё не собрано" in text  # «О компании» Claude ещё не написал
    assert "**ОРК:** Средний чек апсейла ОРК — 15 тыс." in text and "Старое решение" not in text  # только 60 дней
    assert "Урок: Лиды SaaS-агентов" in text
    assert "Бюджет 2027 (Аналитика, «В работе»)" in text and "Собрать сценарий" in text

    context.company_path().parent.mkdir(parents=True, exist_ok=True)
    context.company_path().write_text("# О компании\n\n### Словарь\nОРК — отдел развития клиентов\n" + "x" * 9000,
                                      encoding="utf-8")
    text = context.build()
    assert "### Словарь" in text and "# О компании\n" not in text and len(text) <= context.MAX_CHARS + 1


def test_summary_goes_into_every_claude_run(tmp_path, settings_with, monkeypatch):
    fake = tmp_path / "claude"
    fake.write_text("#!/bin/sh\nfor a; do echo \"$a\"; done > \"$(dirname \"$0\")/args.txt\"\ncat >/dev/null\n"
                    "echo '{\"type\":\"result\",\"result\":\"ok\"}'\n")
    fake.chmod(0o755)
    settings_with(claude={"path": str(fake)}, owner={"name": "Анна"})
    assert brain.run("привет")["text"] == "ok"
    args = (tmp_path / "args.txt").read_text(encoding="utf-8")
    assert "--append-system-prompt" in args and "# Контекст для Claude — Анна" in args
    assert (config.DATA_DIR / "knowledge" / context.FILE).exists()


def test_monday_context_job_and_page(monkeypatch, settings_with):
    from opsdash.web.app import app

    settings_with(schedule={"context_weekly": "08:00", "digest_morning": "", "digest_evening": "", "stale_check": "",
                            "anthology_weekly": ""})
    submitted = []
    monkeypatch.setattr(jobs, "submit", lambda name, kind="light", args=None: submitted.append((name, kind)) or True)
    monkeypatch.setattr(config, "now", lambda s=None: datetime(2026, 11, 2, 8, 30, tzinfo=config.tz()))  # понедельник
    jobs.tick()
    assert ("company-context", "heavy") in submitted
    monkeypatch.undo()
    page = TestClient(app, base_url="http://localhost:8765").get("/kb/context").text
    assert "Что знает Claude" in page and "Собрать «О компании»" in page and "Контекст для Claude" in page
