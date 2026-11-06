"""Решения по темам (MEM-3; 2.0.21 — цепочки, 2.0.22 — вопросы): темы и «о чём», вопросы и что решено сейчас,
история «меняет / уточняет / продолжает / отменяет», следующий шаг, сигналы для дайджеста, решения встречи, задачи."""

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


def test_topics_and_subtopics():
    assert decisions.split_topic("Планирование 2027 / Логистика / Склад") == ("Планирование 2027", "Логистика / Склад")
    assert decisions.split_topic("") == (decisions.NO_TOPIC, "")
    dec("Склад: инвентаризация раз в месяц", project="Логистика / Склад", days_ago=3)
    dec("Доставка: перевозчик второй", project="Логистика / Доставка", days_ago=5)
    dec("Чек 15 тыс.", project="KPI", days_ago=1)
    ts = decisions.topics()
    assert [t["name"] for t in ts] == ["KPI", "Логистика"]  # свежие темы выше
    assert ts[1]["subtopics"] == ["Доставка", "Склад"] and ts[1]["current"] == 2
    assert decisions.rename_topic("Логистика", "Снабжение") == 2
    assert {r["project"] for r in decisions.items(topic="Снабжение")} == {"Снабжение / Склад", "Снабжение / Доставка"}
    with pytest.raises(decisions.DecisionError):
        decisions.rename_topic("Нет такой", "Что-то")


def test_chain_changes_refines_and_cancels():
    a = dec("Премия продаж — 80% всем", days_ago=10)
    b = dec("Премия по ступенчатой шкале", days_ago=7)
    c = dec("Премия: ступени 60 / 80 / 100%", days_ago=4)
    d = dec("Чёрную пятницу анонсируем", days_ago=9)
    e = dec("Чёрную пятницу не анонсируем", days_ago=2)
    decisions.update(b, prev=a, relation="changes")
    decisions.update(c, prev=b, relation="refines")
    decisions.update(e, prev=d, relation="cancels")
    r = {x["id"]: x for x in decisions.items(current=False)}
    assert not r[a]["current"] and r[a]["state_title"].startswith("заменено")  # прежнее больше не действует
    assert r[b]["current"] and r[c]["current"]  # уточнение не отменяет уточняемое
    assert not r[d]["current"] and r[d]["state_title"].startswith("отменено")
    assert r[c]["prev"]["id"] == b and r[c]["relation_title"] == "уточняет"
    assert {x["id"] for x in decisions.items()} == {b, c, e}  # по умолчанию — только действующие
    v = decisions.topic_view("KPI")
    assert [[x["id"] for x in ch] for ch in v["chains"]] == [[d, e], [a, b, c]]  # свежие цепочки выше, внутри — по датам
    assert [q["ids"] for q in v["decided"]] == [[d, e], [a, b, c]]  # вопрос — цепочка; свежие выше
    assert decisions.superseded() == {a: r[a]["superseded_by"], d: r[d]["superseded_by"]}
    text = decisions.topic_text("KPI")
    assert "## Что решено" in text and f"поменяли: Премия по ступенчатой шкале [decision {b}]" in text
    assert "(заменено" in text and "отменили" in text
    # без кругов и ссылок на себя
    with pytest.raises(decisions.DecisionError):
        decisions.update(a, prev=c)
    with pytest.raises(decisions.DecisionError):
        decisions.update(a, prev=a)
    decisions.update(b, prev=0)  # убрать связь
    assert decisions.get(a)["current"] and decisions.get(b)["prev"] is None


def test_questions_now_and_about():
    assert decisions.guess_qa("Розница в ноябре: веса 20/20") == ("Розница в ноябре", "Веса 20/20")
    assert decisions.guess_qa("Премия продаж — 80% всем") == ("Премия продаж", "80% всем")
    assert decisions.guess_qa("Петров переходит в отдел продлений") == ("", "Петров переходит в отдел продлений")
    a = dec("Розница в ноябре: веса 20/20", days_ago=8)
    b = dec("Тимлид розницы: веса 35 / 12,5 / 12,5 / 40", days_ago=4)
    alone = dec("Петров переходит в отдел продлений", days_ago=2)
    decisions.update(b, prev=a, relation="changes")
    qs = {q["latest"]["id"]: q for q in decisions.topic_view("KPI")["decided"]}
    assert qs[b]["label"] == "Тимлид розницы" and qs[b]["now"] == "Веса 35 / 12,5 / 12,5 / 40" and qs[b]["changes"] == 1
    assert qs[alone]["label"] == "" and qs[alone]["now"] == "Петров переходит в отдел продлений"
    decisions.update(a, question="Розница: веса тимлида")  # подпись вопроса — от любого решения цепочки
    decisions.update(b, answer="35 / 12,5 / 12,5 / 40; маржа 40%")
    q = decisions.topic_view("KPI")["decided"][1]
    assert (q["label"], q["now"], q["since"]) == ("Розница: веса тимлида", "35 / 12,5 / 12,5 / 40; маржа 40%",
                                                  decisions.get(b)["when"])
    ch = decisions.changed()[0]
    assert (ch["question"], ch["was"], ch["now"]) == ("Розница: веса тимлида", "Веса 20/20", "35 / 12,5 / 12,5 / 40; маржа 40%")
    t = decisions.topics()[0]
    assert t["questions"] == 2 and t["about"] == "" and t["labels"] == ["Розница: веса тимлида"]
    decisions.set_about("KPI", "KPI на ноябрь для команд продаж")
    assert decisions.topics()[0]["about"] == "KPI на ноябрь для команд продаж"
    decisions.rename_topic("KPI", "Премии")  # «о чём» переезжает вместе с темой
    assert decisions.topics()[0]["about"] == "KPI на ноябрь для команд продаж" and decisions.abouts() == {
        "Премии": "KPI на ноябрь для команд продаж"}


def test_signals_open_without_step_and_changed():
    from opsdash import overview

    question = dec("Цель для нового тимлида — после 02.11", days_ago=5)
    old, new = dec("Премия продаж — 80% всем", days_ago=6), dec("Премия по ступенчатой шкале", days_ago=1)
    assert decisions.summary()["attention"] == 0
    decisions.update(question, stage="discussing")
    r = decisions.get(question)
    assert r["no_step"] and r["stage_title"] == "ждёт решения" and decisions.summary()["attention"] == 1
    decisions.update(question, waiting="Олег обсудит с собственником до 15.11")
    assert decisions.summary()["attention"] == 0
    decisions.update(new, prev=old, relation="changes")
    lines = decisions.changed_lines()
    assert len(lines) == 1 and "было «80% всем» → стало «Премия по ступенчатой шкале»" in lines[0]
    decisions.update(new, question="Премия продаж", answer="По ступенчатой шкале")
    assert "«Премия продаж»: было «80% всем» → стало «По ступенчатой шкале»" in decisions.changed_lines()[0]
    assert f"[decision {new}]" in overview.overview_markdown()  # что поменяли — в сводке для дайджеста
    decisions.update(new, relation="refines")
    assert decisions.changed_lines() == []  # уточнение — не смена
    with pytest.raises(decisions.DecisionError):
        decisions.update(old, stage="bogus")
    decisions.update(old, stage="implemented")  # стадии 2.0.20 «внедрено» и «ждёт» — это «принято»
    assert decisions.get(old)["stage"] == "decided"


def test_legacy_stages_migrated():
    """В базе 2.0.20 стадии «ждёт» и «внедрено» → «принято»."""
    did, wid = dec("Старое внедрённое"), dec("Старое ждущее")
    store.execute("UPDATE anthology SET stage='implemented' WHERE id=?", (did,))
    store.execute("UPDATE anthology SET stage='waiting' WHERE id=?", (wid,))
    for sql in [m for m in store.MIGRATIONS if m[:2] == ("anthology", "prev_id")][0][3:]:
        store.execute(sql)
    assert store.scalar("SELECT COUNT(*) FROM anthology WHERE stage IN ('implemented', 'waiting')") == 0
    assert decisions.get(did)["stage"] == decisions.get(wid)["stage"] == "decided"


def test_meeting_decisions_block_and_summary_links(client):
    mid = "m-premii"
    store.execute("INSERT INTO meetings(id, title, status) VALUES(?, 'Правки премий', 'processed')", (mid,))
    old = dec("Премия продаж — 80% всем", days_ago=6)
    new = anthology.add(kind="decision", title="Премия по ступенчатой шкале вместо план-факта", project="KPI",
                        happened=date.today().isoformat(), source_type="meeting", source_ref=mid, export_now=False)
    other = anthology.add(kind="decision", title="Отчёт за месяц сначала смотрит финансист", project="Отчёты",
                          happened=date.today().isoformat(), source_type="meeting", source_ref=mid, export_now=False)
    decisions.update(new, prev=old, relation="changes")
    rows = decisions.items(meeting=mid, current=False)
    assert {r["id"] for r in rows} == {new, other}
    text = ("# Правки премий\n\n## Решения\n1. Премия — по ступенчатой шкале вместо план-факта [00:02:10]\n"
            "2. Отчёт за месяц сначала смотрит финансист [00:10:00]\n3. Цены не меняем\n\n## Задачи\n"
            "- Премия по шкале — Пётр\n")
    out = decisions.link_summary(text, rows)
    assert "[меняет решение от" in out and f"#d{new})" in out and "[решение →](/decisions?topic=" in out
    assert "3. Цены не меняем\n" in out and "- Премия по шкале — Пётр\n" in out  # без решения и вне раздела — без ссылок
    folder = records.meeting_dir(mid)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "summary.md").write_text(text, encoding="utf-8")
    page = client.get(f"/meetings/{mid}").text
    assert "Решения встречи" in page and "Отчёт за месяц сначала смотрит финансист" in page
    assert "<b>Премия продаж:</b>" not in page and "<b>Отчёт за месяц сначала смотрит финансист" not in page
    assert "меняет решение от" in page and f"#d{old}" in page  # ссылка и на прежнее решение


def test_tasks_still_link():
    did = dec("Средний чек апсейла — 15 тыс.")
    t = task()
    decisions.link(did, t)
    assert decisions.get(did)["tasks"][0]["id"] == t
    decisions.unlink(t)
    assert decisions.get(did)["tasks"] == []


def test_pages_and_actions(client):
    a = dec("Премия продаж — 80% всем", days_ago=5)
    b = dec("Премия по ступенчатой шкале", days_ago=3)
    single = dec("Средний чек апсейла — 15 тыс.", days_ago=4)
    h = {"referer": REF + "/decisions?topic=KPI"}
    page = client.get("/decisions").text
    assert 'href="/decisions?topic=KPI"' in page and "Вопросов" in page and "Изменилось за неделю" not in page
    client.post(f"/decisions/{b}", data={"prev": str(a), "relation": "changes", "metric": "премия", "before": "80% всем",
                                         "after": "грейд 80–85%", "waiting": "", "project": "KPI",
                                         "question": "Премия продаж", "answer": "По ступенчатой шкале"}, headers=h)
    assert decisions.get(b)["prev"]["id"] == a and not decisions.get(a)["current"]
    page = client.get("/decisions").text
    assert "Изменилось за неделю" in page and "80% всем</span>" in page and "По ступенчатой шкале" in page
    topic = client.get("/decisions?topic=KPI").text
    assert "Что решено" in topic and "Премия продаж" in topic and "1 изменение" in topic and "поменяли" in topic
    assert "Средний чек апсейла" in topic and "подробнее" in topic and "действует" not in topic
    assert "премия: 80% всем → грейд 80–85%" in topic
    assert "Тема\tВопрос\tСейчас\tСтатус\tЧего ждём" in topic  # «Скопировать таблицей»
    assert "регламент" not in topic
    client.post(f"/decisions/{single}", data={"stage": "discussing"}, headers=h)
    assert decisions.get(single)["stage"] == "discussing"
    topic = client.get("/decisions?topic=KPI").text
    assert "Ждут решения" in topic and "шага нет" in topic
    page = client.get("/decisions").text
    assert 'title="Решения — ждут решения без следующего шага: 1"' in page  # счётчик в меню
    att = client.get("/decisions?f=attention").text
    assert "Средний чек апсейла" in att and "По ступенчатой шкале" not in att
    assert "80% всем" in client.get("/decisions?q=Премия").text  # поиск — и по истории
    r = client.post(f"/decisions/{single}/task", data={"what": "Внести в калькулятор", "who": "", "due": ""},
                    headers=h, follow_redirects=False)
    assert "msg=" in r.headers["location"] and decisions.get(single)["tasks"][0]["title"] == "Внести в калькулятор"
    client.post("/decisions/topic-rename", data={"old": "KPI", "new": "KPI", "about": "Премии и чек"})
    assert decisions.abouts() == {"KPI": "Премии и чек"}
    assert "Премии и чек" in client.get("/decisions").text
    r = client.post("/decisions/topic-rename", data={"old": "KPI", "new": "KPI-калькуляторы", "about": "Премии и чек"},
                    follow_redirects=False)
    assert "topic=KPI-" in r.headers["location"] and decisions.topics()[0]["name"] == "KPI-калькуляторы"
    assert decisions.abouts() == {"KPI-калькуляторы": "Премии и чек"}
    client.post(f"/decisions/{single}/outdated", headers=h)
    assert decisions.get(single) is None
    r = client.get("/decisions?topic=KPI", headers=h, follow_redirects=False)  # тему переименовали, а ссылка старая
    assert r.status_code == 303 and r.headers["location"].startswith("/decisions?err=")
    assert "Нет темы" in client.get("/decisions?topic=KPI", headers=h).text  # без петли редиректов


def test_cli(capsys):
    from opsdash import cli

    t = task()
    old = dec("Премия продаж — 80% всем", days_ago=12)
    cli.main(["kb", "add", "--kind", "decision", "--title", "Премия по ступенчатой шкале", "--project", "KPI",
              "--question", "Премия продаж", "--answer", "По ступенчатой шкале",
              "--metric", "премия", "--before", "80%", "--after", "грейд 80–85%", "--tasks", str(t),
              "--prev", str(old), "--relation", "changes",
              "--date", (date.today() - timedelta(days=9)).isoformat()])
    new = decisions.items()[0]
    assert new["prev"]["id"] == old and new["tasks"][0]["id"] == t
    assert (new["question"], new["answer"]) == ("Премия продаж", "По ступенчатой шкале")
    capsys.readouterr()
    cli.main(["decisions", "list"])
    out = capsys.readouterr().out
    assert "премия: 80% → грейд 80–85%" in out and f"меняет [decision {old}]" in out
    assert "Премия продаж — 80% всем" not in out  # заменённое — только с --all
    cli.main(["decisions", "list", "--all"])
    assert "НЕ ДЕЙСТВУЕТ" in capsys.readouterr().out
    cli.main(["kb", "list", "--kind", "decision"])
    assert f"НЕ ДЕЙСТВУЕТ: заменено решением [{new['id']}]" in capsys.readouterr().out  # помощник видит, что сменилось
    cli.main(["decisions", "topics"])
    assert "KPI — о чём: не написано · вопросов 1, решений 2" in capsys.readouterr().out
    cli.main(["decisions", "about", "KPI", "--text", "Премии продаж"])
    cli.main(["decisions", "topics"])
    assert "KPI — Премии продаж · вопросов 1" in capsys.readouterr().out
    cli.main(["decisions", "topic", "KPI"])
    out = capsys.readouterr().out
    assert "Премии продаж" in out and "- «Премия продаж»: По ступенчатой шкале" in out and "поменяли:" in out
    cli.main(["decisions", "list", "--meeting", "m1", "--all"])
    assert "Премия продаж — 80% всем" in capsys.readouterr().out  # решения встречи
    cli.main(["decisions", "set", str(new["id"]), "--stage", "discussing", "--next", "посчитаю эффект"])
    assert decisions.get(new["id"])["waiting_for"] == "посчитаю эффект"
    cli.main(["decisions", "rename-topic", "KPI", "--to", "KPI-калькуляторы"])
    assert decisions.get(new["id"])["topic"] == "KPI-калькуляторы"


def test_decisions_job(monkeypatch):
    from opsdash import brain

    a, b = dec("Премия продаж — 80% всем"), dec("Премия по ступенчатой шкале", days_ago=5)

    def fake_run(prompt, **kw):
        assert kw["job"] == "decisions" and "навыку decisions" in prompt
        decisions.update(b, prev=a, relation="changes", question="Премия продаж")
        return {"text": ""}

    monkeypatch.setattr(brain, "run", fake_run)
    assert jobs.job_decisions() == "связей в цепочках: было 0, стало 1; решений с вопросом: было 0, стало 1"


def test_one_time_chains_backfill_after_update(monkeypatch):
    dec("Старое решение", days_ago=40)
    started = []
    monkeypatch.setattr(jobs, "submit", lambda name, kind="light", args=None: started.append(name) or True)
    monkeypatch.setattr(jobs, "in_work_hours", lambda *a, **k: True)
    jobs.tick()
    jobs.tick()
    assert started.count("decisions") == 1  # один раз — дальше по понедельникам и по кнопке
