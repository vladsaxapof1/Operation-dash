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
    assert cat["total"] == len(skills.all_skills()) == 18
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
    with pytest.raises(proposals.ProposalError, match="уже отклоняли"):
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


# --- качество навыков (2.0.56): доля принятых по навыкам, неделям, моделям и версиям ---------------------------

def _proposal(job, status="pending", *, created, model="sonnet", ver=None, auto=0, note=None, reason=None):
    from datetime import timezone

    return store.execute(
        "INSERT INTO proposals(kind, title, payload, status, created, decided_at, job, model, skill_ver, auto, result, "
        "reject_reason) VALUES('task.done', ?, '{}', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (f"p{created}{status}", status, created.astimezone(timezone.utc).isoformat(),
         None if status == "pending" else created.astimezone(timezone.utc).isoformat(), job, model, ver, auto,
         store.json_col({"note": note}) if note else None, reason))


def test_proposal_is_marked_with_job_model_and_version(monkeypatch):
    monkeypatch.setenv("OPSDASH_JOB", "triage")
    monkeypatch.setenv("OPSDASH_MODEL", "sonnet")
    monkeypatch.setenv("OPSDASH_SKILL_VER", "abcd1234")
    p = proposals._create("task.done", "Закрыть A", {"commitment_id": 1}, autonomy_key="task_done", reason="r",
                          source_ref=None, batch=None)
    assert (p["job"], p["model"], p["skill_ver"], p["auto"]) == ("triage", "sonnet", "abcd1234", 0)
    monkeypatch.delenv("OPSDASH_JOB")  # директор сам, в админке или в своём чате — не навык
    p = proposals._create("task.done", "Закрыть B", {"commitment_id": 2}, autonomy_key="task_done", reason="r",
                          source_ref=None, batch=None)
    assert (p["job"], p["model"], p["skill_ver"]) == (None, None, None)


def test_autonomy_marks_proposal_auto(monkeypatch, settings_with):
    settings_with(autonomy={"kaiten_comment": "auto"})
    monkeypatch.setattr(proposals, "execute", lambda p: {"ok": True})
    monkeypatch.setenv("OPSDASH_JOB", "triage")
    p = proposals._create("kaiten.comment", "Комментарий", {}, autonomy_key="kaiten_comment", reason=None,
                          source_ref=None, batch=None)
    assert p["status"] == "done" and p["auto"] == 1


def test_job_vars_for_claude_and_chatgpt():
    s = config.load_settings()
    env = brain.job_vars(s, "triage", "sonnet")
    assert env == {"OPSDASH_JOB": "triage", "OPSDASH_MODEL": "sonnet", "OPSDASH_SKILL_VER": skills.version("triage")}
    assert brain.job_vars(s, "groups", None, "chatgpt")["OPSDASH_MODEL"].startswith("chatgpt:")
    assert "OPSDASH_SKILL_VER" not in brain.job_vars(s, "summary-tests", "haiku")  # задание не по навыку


def test_version_changes_with_rules():
    before = skills.version("triage")
    assert len(before) == 8 and skills.version_for_job("first-finds") == before
    rid = skills.add_rule("triage", "Рекламные рассылки не разбирать")
    assert skills.version("triage") != before
    skills.set_rule(rid, enabled=False)
    assert skills.version("triage") == before


def test_quality_counts_outcomes_weeks_models_and_versions():
    from datetime import date, datetime, time

    tz = config.tz()
    today = date(2026, 11, 18)  # среда
    this_week = datetime.combine(date(2026, 11, 17), time(12), tz)
    last_week = datetime.combine(date(2026, 11, 10), time(12), tz)
    cur = skills.version("triage")
    for _ in range(4):
        _proposal("triage", "done", created=this_week, ver=cur)
    _proposal("triage", "failed", created=this_week, ver=cur)  # «Да» было, выполнить не вышло — тоже принято
    _proposal("triage", "rejected", created=this_week, ver=cur, reason="not_task")
    _proposal("triage", "rejected", created=last_week, model="haiku", ver="old00000")
    _proposal("first-finds", "done", created=last_week, model="haiku", ver="old00000")  # задание того же навыка
    _proposal("triage", "done", created=this_week, auto=1)                 # без директора
    _proposal("triage", "rejected", created=this_week, note="задача — дубль")  # отпало само
    _proposal("triage", "pending", created=this_week)
    _proposal(None, "rejected", created=this_week)                         # директор сам — не навык
    _proposal("digest", "rejected", created=this_week)

    q = skills.quality(weeks=2, today=today)
    t = q["triage"]
    assert t["recent"] == {"accepted": 6, "rejected": 2, "auto": 1, "dropped": 1, "pending": 1, "decided": 8,
                           "total": 11, "rate": 0.75, "line": "принято 6 из 8 · 75%"}
    assert [(w["label"], w["accepted"], w["decided"]) for w in t["weeks"]] == [("09.11", 1, 2), ("16.11", 5, 6)]
    assert {m["model"]: (m["accepted"], m["rejected"]) for m in t["models"]} == {"sonnet": (5, 1), "haiku": (1, 1)}
    assert [(v["since_label"], v["current"], v["accepted"], v["rejected"]) for v in t["versions"]] == [
        ("17.11", True, 5, 1), ("10.11", False, 1, 1)]
    assert t["reasons"] == [{"reason": "без причины", "n": 1}, {"reason": "не задача", "n": 1}]
    assert q["digest"]["recent"]["rate"] is None  # одно «Нет» — ещё не качество
    assert skills.quality_line(t["recent"]) == "принято 6 из 8 · 75%"
    assert skills.quality_line(q["digest"]["recent"]) == "принято 0 из 1"


def test_reject_with_note_is_not_a_no():
    p = proposals._create("task.done", "Закрыть C", {"commitment_id": 3}, autonomy_key="task_done", reason="r",
                          source_ref=None, batch=None)
    store.execute("UPDATE proposals SET job='triage' WHERE id=?", (p["id"],))
    proposals.reject(p["id"], note="разговор удалён")
    assert skills.quality()["triage"]["recent"]["dropped"] == 1


def test_migration_backfills_job_and_auto_from_old_rows():
    import sqlite3

    store._initialized.clear()
    config.DB_FILE.unlink(missing_ok=True)
    conn = sqlite3.connect(str(config.DB_FILE))
    conn.executescript(store.SCHEMA)  # база до 2.0.56: у предложений нет job и auto
    cols = {r[1] for r in conn.execute("PRAGMA table_info(proposals)")}
    assert "job" not in cols
    rows = [("meeting:2026-11-01-x", "done", "2026-11-01T10:00:00+00:00", "2026-11-01T12:00:00+00:00"),
            ("triage:abc", "rejected", "2026-11-01T10:00:00+00:00", "2026-11-01T12:00:00+00:00"),
            ("agents:1", "done", "2026-11-01T10:00:00+00:00", "2026-11-01T12:00:00+00:00"),
            (None, "done", "2026-11-01T10:00:00+00:00", "2026-11-01T10:00:01+00:00")]
    for i, (batch, status, created, decided) in enumerate(rows):
        conn.execute("INSERT INTO proposals(kind, title, payload, batch, status, created, decided_at) "
                     "VALUES('task.done', ?, '{}', ?, ?, ?, ?)", (f"t{i}", batch, status, created, decided))
    conn.commit()
    conn.close()
    got = store.rows("SELECT batch, job, auto FROM proposals ORDER BY id")
    assert [(r["job"], r["auto"]) for r in got] == [("process-meeting", 0), ("triage", 0), (None, 0), (None, 1)]


def test_cli_skills_stats(capsys):
    from datetime import datetime, timedelta

    now = datetime.now(config.tz()) - timedelta(hours=1)
    for status in ("done", "done", "done", "done", "rejected"):
        _proposal("process-meeting", status, created=now, model="opus", reason="exists" if status == "rejected" else None)
    from opsdash import cli

    cli.main(["skills", "stats"])
    text = capsys.readouterr().out
    assert "process-meeting — Разбор встречи: принято 4 из 5 · 80%" in text and "почему «Нет»: уже есть 1" in text
    cli.main(["skills", "stats", "--skill", "digest"])
    assert "ничего не предлагали" in capsys.readouterr().out


def test_skill_pages_show_quality_in_both_designs(client):
    from datetime import datetime, timedelta

    now = datetime.now(config.tz()) - timedelta(hours=1)
    for status in ("done", "done", "done", "done", "rejected"):
        _proposal("triage", status, created=now)
    _proposal("triage", "rejected", created=now, note="разговор удалён")
    for design in ("classic", None):  # None — новый дизайн, он по умолчанию
        s = config.load_settings()
        if design:
            s["web"]["design"] = design
        else:
            s["web"].pop("design", None)
        config.save_settings(s)
        page = client.get("/skills/triage").text
        assert "Сколько предложений вы принимаете" in page, design
        assert "вы приняли 4 из 5 предложений, это <b>80%</b>" in page
        assert "отпало само без вашего ответа (1)" in page and "без причины: 1" in page
        assert 'class="sk-q-chart"' in page and "Неделя с " in page
        assert "За 4 недели принято 4 из 5 · 80%" in client.get("/skills").text
        # навык без предложений за 8 недель: блок есть, но честно пустой
        assert "За 8 недель навык ничего не предлагал." in client.get("/skills/projects").text


def test_skill_that_proposes_nothing_has_no_quality_block(client):
    """«Что предлагает: Ничего — …» — без «Только после вашего „Да“» и без пустого блока «Сколько предложений»."""
    for design in ("classic", None):  # None — новый дизайн, он по умолчанию
        s = config.load_settings()
        if design:
            s["web"]["design"] = design
        else:
            s["web"].pop("design", None)
        config.save_settings(s)
        for sid in ("weekly", "style"):
            page = client.get(f"/skills/{sid}").text
            assert "Сколько предложений вы принимаете" not in page, (design, sid)
            assert "после вашего «Да»" not in page.split("Что предлагает", 1)[1].split("</dd>", 1)[0], (design, sid)
        page = client.get("/skills/projects").text
        assert "Сколько предложений вы принимаете" in page and "после вашего «Да»" in page, design
