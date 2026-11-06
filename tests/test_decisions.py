"""Реестр решений (MEM-3): стадии, «было → стало», связь с задачами, «без исполнения»."""

from datetime import date, timedelta

import pytest
from starlette.testclient import TestClient

from opsdash import anthology, decisions, jobs, records, store

REF = "http://localhost:8765"


@pytest.fixture
def client():
    from opsdash.web.app import app

    return TestClient(app, base_url=REF)


def dec(title, days_ago=10, project="KPI"):
    return anthology.add(kind="decision", title=title, details="почему", project=project,
                         happened=(date.today() - timedelta(days=days_ago)).isoformat(), source_type="meeting",
                         source_ref="m1", export_now=False)


def task(what="Внести 15 тыс. в калькулятор"):
    return records.add_commitment(direction="mine", what=what, who="Анна", whom="Анна")


def test_orphan_and_links():
    old, fresh = dec("Средний чек апсейла — 15 тыс."), dec("Новый регламент", days_ago=2)
    rows = {r["id"]: r for r in decisions.items()}
    assert rows[old]["orphan"] and not rows[fresh]["orphan"]  # свежее — ещё не повод
    assert rows[old]["stage"] == "decided" and rows[old]["stage_title"] == "принято"
    assert decisions.summary()["orphans"] == 1
    t = task()
    decisions.link(old, t)
    r = decisions.get(old)
    assert not r["orphan"] and r["tasks"][0]["id"] == t and r["open_tasks"] == 1
    records.update_commitment(t, status="done")
    assert decisions.get(old)["all_done"]  # все задачи закрыты — подсказка «внедрено?»
    decisions.update(old, stage="implemented", metric="средний чек", before="12 тыс.", after="15 тыс.")
    r = decisions.get(old)
    assert r["stage_title"] == "внедрено" and (r["before_value"], r["after_value"]) == ("12 тыс.", "15 тыс.")
    decisions.update(fresh, stage="waiting", waiting="согласования собственника")
    assert decisions.items(stage="waiting")[0]["waiting_for"] == "согласования собственника"
    with pytest.raises(decisions.DecisionError):
        decisions.update(old, stage="bogus")
    decisions.unlink(t)
    assert decisions.get(old)["tasks"] == []


def test_register_page_and_actions(client):
    did = dec("Средний чек апсейла — 15 тыс.")
    page = client.get("/decisions").text
    assert "Средний чек апсейла" in page and "Нет задач — решение не исполняется" in page
    assert ">1</b> без исполнения" in page and 'href="/decisions"' in page
    assert 'title="Решения — без исполнения: 1"' in page  # счётчик в меню
    r = client.post(f"/decisions/{did}/task", data={"what": "Внести в калькулятор", "who": "", "due": ""},
                    headers={"referer": REF + "/decisions"}, follow_redirects=False)
    assert "msg=" in r.headers["location"]
    t = decisions.get(did)["tasks"][0]
    assert t["title"] == "Внести в калькулятор" and t["direction"] == "mine"
    other = task("Пересчитать план")
    client.post(f"/decisions/{did}/link", data={"task": f"#{other} · Пересчитать план"}, headers={"referer": REF + "/decisions"})
    assert {x["id"] for x in decisions.get(did)["tasks"]} == {t["id"], other}
    client.post(f"/decisions/{did}", data={"stage": "waiting"}, headers={"referer": REF + "/decisions"})
    assert decisions.get(did)["stage"] == "waiting"
    client.post(f"/decisions/{did}", data={"metric": "чек", "before": "12", "after": "15", "waiting": "MR 517"},
                headers={"referer": REF + "/decisions"})
    assert decisions.get(did)["waiting_for"] == "MR 517"
    assert "12</b> → <b>15" in client.get("/decisions").text
    client.post(f"/decisions/{did}/outdated", headers={"referer": REF + "/decisions"})
    assert decisions.get(did) is None or decisions.items() == []


def test_cli_and_overview(capsys):
    from opsdash import cli, overview

    t = task()
    cli.main(["kb", "add", "--kind", "decision", "--title", "Чек 15 тыс.", "--metric", "средний чек", "--before", "12",
              "--after", "15", "--tasks", str(t), "--date", (date.today() - timedelta(days=9)).isoformat()])
    d = decisions.items()[0]
    assert d["metric"] == "средний чек" and d["tasks"][0]["id"] == t
    cli.main(["decisions", "list"])
    out = capsys.readouterr().out
    assert "средний чек: 12 → 15" in out and f"[commitment {t}]" in out
    orphan = dec("Сменить поставщика")
    assert f"[decision {orphan}]" in overview.overview_markdown()  # без исполнения — в сводке для дайджеста
    cli.main(["decisions", "link", str(orphan), "--task", str(task("Подписать договор"))])
    assert f"[decision {orphan}]" not in overview.overview_markdown()


def test_decisions_job(monkeypatch):
    from opsdash import brain

    did = dec("Сменить поставщика")
    t = task("Подписать договор с новым поставщиком")

    def fake_run(prompt, **kw):
        assert kw["job"] == "decisions" and "навыку decisions" in prompt
        decisions.link(did, t)
        return {"text": ""}

    monkeypatch.setattr(brain, "run", fake_run)
    assert jobs.job_decisions() == "без исполнения: было 1, стало 0"


def test_one_time_backfill_after_update(monkeypatch):
    dec("Старое решение", days_ago=40)
    started = []
    monkeypatch.setattr(jobs, "submit", lambda name, kind="light", args=None: started.append(name) or True)
    monkeypatch.setattr(jobs, "in_work_hours", lambda *a, **k: True)
    jobs.tick()
    jobs.tick()
    assert started.count("decisions") == 1  # один раз — дальше по понедельникам и по кнопке
