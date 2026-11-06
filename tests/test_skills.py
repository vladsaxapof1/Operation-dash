"""Навыки (SK-1 – SK-4) и уроки из «Нет» (MOD-3)."""

import subprocess

import pytest
from starlette.testclient import TestClient

from opsdash import brain, config, jobs, proposals, skills, store

REF = "http://localhost:8765"


@pytest.fixture
def client():
    from opsdash.web.app import app

    return TestClient(app, base_url=REF)


@pytest.fixture(autouse=True)
def clean_git(monkeypatch):
    # по умолчанию — «ничего не изменено»; тесты защиты подменяют ответ git сами
    monkeypatch.setattr(skills, "_git", lambda *a, **k: subprocess.CompletedProcess(a, 0, "", ""))
    skills._mod_cache = (0.0, None)


def find(cat, sid):
    return next(s for a in cat["areas"] for s in a["skills"] if s["id"] == sid)


def test_manifest_covers_every_skill_folder():
    on_disk = {p.parent.name for p in skills.SKILLS_DIR.glob("*/SKILL.md")}
    assert {s["id"] for s in skills.all_skills()} == on_disk
    for s in skills.all_skills():
        assert s["area"] in {a["id"] for a in skills.manifest()["areas"]} and s["what"] and s["jobs"], s["id"]
        for j in s["jobs"]:
            assert j in jobs.REGISTRY, (s["id"], j)


def test_catalog_statuses(settings_with):
    cat = skills.catalog()
    assert cat["total"] == 14
    assert find(cat, "triage")["kind"] == "need" and "переписка" in find(cat, "triage")["label"]
    assert find(cat, "anthology")["kind"] == "off"  # «База знаний» в архиве
    assert find(cat, "digest")["kind"] == "idle"
    settings_with(gmail={"enabled": True})
    store.execute("INSERT INTO jobs(name, last_run, last_ok) VALUES('triage', '2026-11-06T10:00:00+00:00', "
                  "'2026-11-06T10:00:00+00:00')")
    assert find(skills.catalog(), "triage")["kind"] == "ok"


def test_rules_are_added_to_the_run(monkeypatch, client):
    r = client.post("/skills/process-meeting/rules", data={"text": "  В итогах   пиши короче  "}, follow_redirects=False)
    assert r.headers["location"].startswith("/skills/process-meeting?msg=")
    rid = skills.rules("process-meeting")[0]["id"]
    assert skills.rules("process-meeting")[0]["text"] == "В итогах пиши короче"
    seen = {}

    def fake_stream(prompt, **kw):
        seen["prompt"] = prompt
        return {"text": "", "session_id": "s", "raw": {}}

    monkeypatch.setattr(brain, "find_claude", lambda *a, **k: "/x/claude")
    monkeypatch.setattr(brain, "_stream", fake_stream)
    brain.run("Разбери встречу", job="process-meeting")
    assert "Правила директора для навыка «Разбор встречи»" in seen["prompt"] and "- В итогах пиши короче" in seen["prompt"]
    brain.run("Разбери сообщения", job="triage")
    assert "Правила директора" not in seen["prompt"]  # у другого навыка правил нет
    client.post(f"/skills/rules/{rid}", data={"action": "off"})
    brain.run("Разбери встречу", job="process-meeting")
    assert "Правила директора" not in seen["prompt"]  # выключенное не действует
    page = client.get("/skills/process-meeting").text
    assert "Ваши правила" in page and "В итогах пиши короче" in page and "Текст навыка" in page and "Включить" in page
    client.post(f"/skills/rules/{rid}", data={"action": "delete"})
    assert skills.rules("process-meeting") == []


def test_catalog_page_and_nav(client):
    page = client.get("/skills").text
    assert ">Навыки<" in page and "Разбор сообщений" in page and "Уроки из ваших «Нет»" in page
    assert 'href="/skills"' in page and "data-sk-search" in page
    assert "не хватает подключения: переписка" in page


def test_modified_system_skill_and_restore(monkeypatch, client):
    calls = []

    def fake_git(*args, **kw):
        calls.append(args)
        if args[0] == "status":
            return subprocess.CompletedProcess(args, 0, " M .claude/skills/triage/SKILL.md\n?? .claude/skills/mine/\n", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(skills, "_git", fake_git)
    assert skills.modified(max_age=0) == {"triage": "изменён"}
    cat = skills.catalog()
    assert find(cat, "triage")["kind"] == "warn" and find(cat, "triage")["label"] == "изменён на этом компьютере"
    assert "Изменены на этом компьютере" in client.get("/skills").text
    from opsdash import doctor

    assert {r["key"]: r for r in doctor.run()["system"]}["skills"]["level"] == "warn"
    client.post("/skills/triage/restore")
    assert ("checkout", "--", ".claude/skills/triage") in calls


def test_reject_reason_web(client):
    p = proposals._create("task.done", "Закрыть «Прогноз»", {"commitment_id": 1}, autonomy_key="task_done",
                          reason="похоже, сделано", source_ref=None, batch=None)
    r = client.post(f"/proposals/{p['id']}/reject", headers={"referer": REF + "/"}, follow_redirects=False)
    assert f"why={p['id']}" in r.headers["location"]
    page = client.get(r.headers["location"]).text
    assert 'data-why="%d"' % p["id"] in page and "не задача" in page
    assert page.index("data-why=") < page.index("/static/app.js")  # полоса — до скриптов, иначе app.js её не найдёт
    assert client.post(f"/why/{p['id']}", data={"reason": "exists"}).json()["ok"]
    assert store.row("SELECT reject_reason FROM proposals WHERE id=?", (p["id"],))["reject_reason"] == "exists"
    assert "data-why=" not in client.get(r.headers["location"]).text  # ответили — больше не спрашиваем
    assert not skills.set_reason(p["id"], "bogus")


def test_reject_reason_bot(monkeypatch):
    from opsdash import bot as botmod

    p = proposals._create("telegram.remind", "Напомнить Петру", {}, autonomy_key="telegram_send", reason=None,
                          source_ref=None, batch=None)

    class FakeBot:
        def __init__(self):
            self.sent, self.edits = [], []

        def answer_callback(self, *a, **k):
            pass

        def send(self, chat_id, text, buttons=None, reply_to=None):
            self.sent.append((text, buttons))
            return 1

        def edit(self, chat_id, mid, text, buttons=None):
            self.edits.append(text)

        def call(self, *a, **k):
            return {}

    b = FakeBot()
    botmod.handle_callback(b, {"id": "1", "data": f"p:r:{p['id']}"}, "42")
    text, buttons = b.sent[-1]
    assert "почему" in text and buttons[0][0]["callback_data"] == f"w:{p['id']}:not_task"
    botmod.handle_callback(b, {"id": "2", "data": f"w:{p['id']}:not_now", "message": {"message_id": 7, "text": ""}}, "42")
    assert store.row("SELECT reject_reason FROM proposals WHERE id=?", (p["id"],))["reject_reason"] == "not_now"
    assert "не сейчас" in b.edits[-1]


def test_rule_proposal_lifecycle():
    p = proposals.propose_rule(skill="triage", text="Рассылки не разбирай как поручения", reason="3 отказа «не задача»")
    assert p["status"] == "pending" and proposals.action(p) == "Включить правило"
    done = proposals.approve(p["id"])
    assert done["status"] == "done" and "Правило включено" in proposals.result_text(done)
    r = skills.rules("triage")[0]
    assert r["source"] == "lesson" and r["proposal_id"] == p["id"]
    with pytest.raises(proposals.ProposalError, match="уже действует"):
        proposals.propose_rule(skill="triage", text="рассылки не разбирай как поручения")
    q = proposals.propose_rule(skill="digest", text="Не больше трёх «ждут ответа»")
    proposals.reject(q["id"])
    with pytest.raises(proposals.ProposalError, match="уже отклоняла"):
        proposals.propose_rule(skill="digest", text="Не больше трёх «ждут ответа»")
    with pytest.raises(proposals.ProposalError, match="Нет навыка"):
        proposals.propose_rule(skill="nope", text="x")


def test_lessons_job(monkeypatch):
    assert jobs.job_lessons() == "отклонённых за две недели нет — уроков нет"
    p = proposals._create("task.meeting", "Связать с «Daily»", {}, autonomy_key="task_meeting", reason=None,
                          source_ref="telegram:1", batch=None)
    proposals.reject(p["id"])
    skills.set_reason(p["id"], "not_task")
    assert skills.rejected(14)[0]["reject_reason_text"] == "не задача"
    asked = {}

    def fake_run(prompt, **kw):
        asked.update(kw, prompt=prompt)
        proposals.propose_rule(skill="triage", text="Не связывай задачи с «Daily»")
        return {"text": ""}

    monkeypatch.setattr(brain, "run", fake_run)
    assert jobs.job_lessons() == "предложено правил: 1"
    assert asked["job"] == "lessons" and "навыку lessons" in asked["prompt"]


def test_cli_rejected_and_skills(capsys):
    from opsdash import cli

    p = proposals._create("task.done", "Закрыть X", {"commitment_id": 1}, autonomy_key="task_done", reason="r",
                          source_ref=None, batch=None)
    proposals.reject(p["id"])
    skills.set_reason(p["id"], "wrong", "это Олег")
    cli.main(["proposals", "rejected"])
    assert "почему «Нет»: не тому — это Олег" in capsys.readouterr().out
    cli.main(["skills", "list"])
    assert "triage — Разбор сообщений" in capsys.readouterr().out


def test_skill_text_is_readable_markdown(client):
    meta, body = skills.split_text("triage")
    assert meta["name"] == "triage" and not body.startswith("---")
    assert skills._for_reading("Текст\n- пункт\n- ещё") == "Текст\n\n- пункт\n- ещё"  # список не слипается с абзацем
    page = client.get("/skills/triage").text
    assert "<h2" in page and "&lt;h2" not in page and "&lt;code&gt;" not in page  # Markdown отрисован, а не экранирован
    assert "name: triage" not in page and "Когда Claude берёт этот навык" in page
