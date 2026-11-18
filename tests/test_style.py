"""Стиль директора (opsdash.style): карточка стиля, прошлые ответы как образцы, сверка черновиков с отправленным,
инструмент style MCP-сервера, задание style."""

import io
import json
from datetime import datetime, timedelta, timezone

import pytest

from opsdash import brain, cli, config, jobs, mcp_server, records, store, style

T0 = datetime(2026, 11, 2, 7, 0, tzinfo=timezone.utc)  # понедельник, 10:00 по Москве


@pytest.fixture(autouse=True)
def writable_after(monkeypatch):
    monkeypatch.setattr(store, "_query_only", False)  # serve() включает «только чтение» — другим тестам он не нужен


@pytest.fixture(autouse=True)
def fixed_now(monkeypatch):
    # корпус — исходящие за полгода от «сейчас»: тест не должен зависеть от сегодняшней даты
    real = style._since
    monkeypatch.setattr(style, "_since", lambda days: store.to_utc_iso(T0 + timedelta(days=30) - timedelta(days=days))
                        if days else real(days))


_n = [0]


def msg(chat: str, text: str, minutes: float, *, out: bool = False, sender: str | None = None, source: str = "telegram",
        reply_to: str | None = None) -> int:
    _n[0] += 1
    store.add_message({"source": source, "chat_id": chat, "chat_title": None, "msg_id": str(_n[0]),
                       "sender_name": "Я" if out else sender, "is_outgoing": out, "text": text,
                       "ts": store.to_utc_iso(T0 + timedelta(minutes=minutes)), "reply_to": reply_to})
    return store.scalar("SELECT id FROM messages WHERE msg_id=?", (str(_n[0]),))


@pytest.fixture
def chats():
    records.upsert_person("Олег Петров", aliases=["Олег"], telegram="@oleg_p")
    records.upsert_person("Ирина Смирнова", aliases=["Ирина"])
    store.upsert_chat("telegram", "100", "Олег LIFE", "private", enabled=1, username="oleg_p")
    store.upsert_chat("telegram", "200", "Ирина Смирнова", "private", enabled=1)
    store.upsert_chat("telegram", "300", "Склад", "group", enabled=1)
    store.upsert_chat("telegram", "400", "Мама", "private", enabled=1)
    store.execute("UPDATE chats SET memory_off=1 WHERE chat_id='400'")  # «Не запоминать»
    store.upsert_chat("telegram", "500", "Новости", "channel", enabled=1)
    # Олег: на «ты», коротко, несколькими сообщениями, без точки
    msg("100", "Привет, пришли отчёт по складу", 0, sender="Олег LIFE")
    msg("100", "привет", 10, out=True)
    msg("100", "щас гляну отчёт по складу", 11, out=True)
    msg("100", "[фото] вот смотри", 12, out=True)
    msg("100", "Когда будет бюджет на декабрь?", 60 * 24, sender="Олег LIFE")
    msg("100", "бюджет на декабрь пришлю к пятнице, ты пока сверь остатки", 60 * 24 + 5, out=True)
    msg("100", "Ок, а смета по ремонту?", 60 * 48, sender="Олег LIFE")
    msg("100", "смета по ремонту бля потерялась", 60 * 48 + 2, out=True)  # мат — не образец
    msg("100", "[голосовое сообщение] ну короче слушай", 60 * 49, out=True)  # речь, а не письмо
    # Ирина: на «вы»
    msg("200", "Добрый день! Согласуете бюджет на декабрь?", 60 * 72, sender="Ирина Смирнова")
    msg("200", "Ирина, да, согласую бюджет, пришлите вашу смету", 60 * 72 + 30, out=True)
    msg("200", "Когда сможете посмотреть договор?", 60 * 96, sender="Ирина Смирнова")
    msg("200", "посмотрю договор вечером, вам напишу", 60 * 96 + 3, out=True)
    msg("200", "Пароль от кабинета: Qwerty12345", 60 * 97, sender="Ирина Смирнова")
    msg("200", "ок, пароль от кабинета вижу", 60 * 97 + 1, out=True)  # похоже на секрет — не образец
    msg("200", "Завтра везу кота к врачу, договор посмотрю позже", 60 * 98, sender="Ирина Смирнова")
    msg("200", "ок, договор посмотрим в среду, без спешки", 60 * 98 + 1, out=True)  # личное — не образец
    # группа: ответ — сообщениям последнего, кто писал
    msg("300", "Кто заберёт поставку?", 60 * 120, sender="Пётр")
    msg("300", "Олег, бюджет на декабрь уже есть?", 60 * 120 + 1, sender="Олег Петров")
    msg("300", "бюджет на декабрь будет к пятнице", 60 * 120 + 4, out=True)
    # «Не запоминать» и канал — не корпус
    msg("400", "Сынок, бюджет на декабрь какой?", 60 * 130, sender="Мама")
    msg("400", "бюджет на декабрь секретный, мам", 60 * 130 + 1, out=True)
    msg("500", "бюджет на декабрь", 60 * 131, out=True)


def test_corpus_bursts_and_context(chats):
    ps = style.pairs()
    replies = [p["reply"] for p in ps]
    assert "привет\nщас гляну отчёт по складу\nвот смотри" in replies  # подряд до 5 минут — один ответ, без «[фото]»
    assert not any("сынок" in p["incoming"].lower() or "секретный" in p["reply"] for p in ps)  # «Не запоминать»
    assert not any("ну короче" in r for r in replies)  # голосовое — не письменный стиль
    assert all(p["chat_id"] != "500" for p in ps)  # канал
    oleg = [p for p in ps if p["chat_id"] == "100"]
    assert {p["who"] for p in oleg} == {"Олег Петров"}  # личный чат «Олег LIFE» — по @имени из справочника
    group = next(p for p in ps if p["chat_id"] == "300")
    assert group["incoming"] == "Олег, бюджет на декабрь уже есть?" and group["who"] == "Олег Петров"
    assert style.outgoing_count() == 11  # голосовое — тоже исходящее, но не корпус стиля


def test_stats_numbers(chats):
    st = style.stats()
    assert st["messages"] == 10 and st["replies"] == 8  # голосовое не в счёт, «[фото] вот смотри» — да
    assert st["punct"]["period"] == 0.0
    assert st["greet"]["forms"][0][0] == "привет"
    people = {p["who"]: p for p in st["people"]}
    assert people["Олег Петров"]["form"] == "мало данных" and people["Олег Петров"]["ty"] == 1
    assert people["Ирина Смирнова"]["vy"] == 2 and people["Олег Петров"]["rude"]
    markers = {m["what"]: m["n"] for m in st["markers"]}
    assert markers["«С уважением»"] == 0 and markers["«Добрый день»"] == 0
    assert st["by_name"] > 0  # «Ирина, да, согласую…»
    card = style.auto_card(st)
    assert card.startswith("# Как пишет") and "«С уважением»" in card and "бля" not in card


def test_examples_for_person_and_similar_situation(chats):
    items = style.examples("Олег", "бюджет на декабрь пришлёшь?", limit=3)
    assert items[0]["reply"].startswith("бюджет на декабрь пришлю к пятнице") and "похожая" in items[0]["why"]
    assert all(not style._RUDE.search(p["reply"]) for p in items)
    irina = style.examples("Ирина", "пароль от кабинета договор", limit=5)
    assert all("пароль" not in p["reply"] and "в среду" not in p["reply"] for p in irina)
    other = style.examples(None, "согласуйте смету на декабрь", limit=2)
    assert other and other[0]["who"] == "Ирина Смирнова" and other[0]["why"] == "похожая ситуация"
    text = style.examples_text("Олег Петров", "бюджет на декабрь")
    assert "Вы (3 сообщения подряд):" in text or "Вы: «бюджет на декабрь пришлю" in text
    assert "Как вы отвечали раньше — Олег Петров" in text
    assert style.examples_text("Никого такого", "") .startswith("Прошлых ответов «Никого такого» не нашлось")
    assert "Олег Петров: " in style.person_line("Олег")


def test_profanity_filter_words():
    rude = ["бля", "нихуя себе", "я сьебываю", "похуй", "пиздец"]
    fine = ["сабля и хлеб", "тебе небо себя ребята", "психую", "учеба", "требовать"]
    assert all(style._RUDE.search(t) for t in rude)
    assert not any(style._RUDE.search(t) for t in fine)


def draft(message_id: int, text: str, minutes: float) -> int:
    aid = records.add_attention(summary="ждут ответа", message_id=message_id, draft=text)
    store.execute("UPDATE attention SET created=? WHERE id=?", (store.to_utc_iso(T0 + timedelta(minutes=minutes)), aid))
    return aid


def test_draft_checks_verdicts():
    store.upsert_chat("telegram", "700", "Пётр", "private", enabled=1)
    store.upsert_chat("telegram", "800", "Анна", "private", enabled=1)
    store.upsert_chat("telegram", "900", "Вера", "private", enabled=1)
    q1 = msg("700", "Пришлёшь отчёт?", 0, sender="Пётр")
    draft(q1, "Пришлю отчёт сегодня до вечера", 1)
    msg("700", "Пришлю отчёт сегодня до вечера", 3, out=True)  # скопировал как есть
    q2 = msg("800", "Встреча в силе?", 0, sender="Анна")
    msg("800", "да", 0.5, out=True)  # ответил до черновика — не в счёт
    draft(q2, "Анна, привет. Да, встреча в силе, жду в 15:00.", 1)
    msg("800", "Да, встреча в силе, жду в 15:00", 4, out=True)
    msg("800", "захвати ноут", 5, out=True)  # тот же ответ — вторым сообщением
    msg("800", "ок", 7, sender="Анна")
    msg("800", "и зарядку", 8, out=True)  # после её ответа — уже другой разговор
    q3 = msg("900", "Подпишешь договор?", 0, sender="Вера")
    draft(q3, "Вера, добрый день. Договор подпишу завтра после согласования с юристами.", 1)
    msg("900", "щас гляну", 2, out=True)
    q4 = msg("700", "А презентация?", 60, sender="Пётр")
    draft(q4, "Презентацию пришлю завтра", 61)  # ответа нет
    checks = {c["chat_id"] + c["draft"][:5]: c for c in style.draft_checks(days=None)}
    assert checks["700Пришл"]["verdict"] == "same"
    anna = checks["800Анна,"]
    assert anna["sent"] == "Да, встреча в силе, жду в 15:00\nзахвати ноут" and anna["verdict"] == "minor"
    assert checks["900Вера,"]["verdict"] == "rewritten"
    assert checks["700Презе"]["verdict"] == "none"
    ds = style.draft_stats(checks=list(checks.values()))
    assert (ds["answered"], ds["same"], ds["minor"], ds["rewritten"], ds["none"]) == (3, 1, 1, 1, 1)
    assert ds["share"] == round(2 / 3, 3)
    assert "без правок 1, с мелкими правками 1 — 67%" in style.drafts_line(ds)


def test_save_card_export_and_card_text(chats):
    with pytest.raises(style.StyleError, match="короткая"):
        style.save_card("# Как пишет\n\nкоротко")
    with pytest.raises(style.StyleError, match="длиннее"):
        style.save_card("# Как пишет\n\n" + "очень длинно " * 500)
    with pytest.raises(style.StyleError, match="пароль"):
        style.save_card("# Как пишет\n\n" + "пиши коротко, без точки в конце. " * 10 + "пароль: hunter2")
    text = "# Как пишет директор\n\n## Главное\n" + "- Без точки в конце, без имени в начале.\n" * 8
    cid = style.save_card(text, stats_={"messages": 10})
    assert style.card()["id"] == cid and style.card()["author"] == "claude" and style.card()["messages"] == 10
    exported = style.path().read_text(encoding="utf-8")
    assert exported.startswith("# Как пишет директор") and "Карточка стиля PULSE" in exported
    out = style.card_text("Олег")
    assert out.startswith("# Как пишет директор") and "(Claude)" in out and "## С этим человеком" in out


def test_card_without_claude_and_too_few_messages(chats):
    assert style.card_text().startswith("Карточки стиля пока нет: исходящих сообщений в рабочих чатах за полгода 10")
    style.MIN_MESSAGES, old = 5, style.MIN_MESSAGES
    try:
        assert style.card_text().startswith("# Как пишет")  # из одних цифр
    finally:
        style.MIN_MESSAGES = old


def test_cli_style(chats, capsys, tmp_path):
    cli.main(["style", "examples", "--person", "Олег", "--text", "бюджет на декабрь"])
    assert "бюджет на декабрь пришлю к пятнице" in capsys.readouterr().out
    cli.main(["style", "facts"])
    out = capsys.readouterr().out
    assert "Исходящих за 180 дней: 10 сообщений" in out and "С кем как" in out
    cli.main(["style", "drafts"])
    assert "черновиков" in capsys.readouterr().out.lower()
    card = config.DATA_DIR / "style" / "card.md"
    card.parent.mkdir(parents=True, exist_ok=True)
    card.write_text("# Как пишет директор\n\n" + "- Коротко, без точки в конце.\n" * 10, encoding="utf-8")
    cli.main(["style", "save", "--file", "data/style/card.md"])
    assert "Карточка стиля сохранена" in capsys.readouterr().out
    cli.main(["style", "card", "--person", "Ирина"])
    out = capsys.readouterr().out
    assert out.startswith("# Как пишет директор") and "Ирина Смирнова: " in out


def rpc(*msgs: dict) -> list[dict]:
    out = io.StringIO()
    mcp_server.serve(io.StringIO("".join(json.dumps(m, ensure_ascii=False) + "\n" for m in msgs)), out)
    return [json.loads(line) for line in out.getvalue().splitlines()]


def test_mcp_style_tool_read_only(chats, monkeypatch):
    monkeypatch.setattr(style, "MIN_MESSAGES", 5)
    replies = rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                  {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                   "params": {"name": "style", "arguments": {"person": "Олег", "text": "бюджет на декабрь"}}})
    tool = next(t for t in replies[0]["result"]["tools"] if t["name"] == "style")
    assert tool["annotations"]["readOnlyHint"] and "person" in tool["inputSchema"]["properties"]
    res = replies[1]["result"]
    text = res["content"][0]["text"]
    assert not res["isError"] and text.startswith("# Как пишет") and "Олег Петров: " in text
    assert "бюджет на декабрь пришлю к пятнице" in text and "секретный" not in text and "бля" not in text
    assert not store.scalar("SELECT COUNT(*) FROM style_cards")  # сервер ничего не записал


def test_style_job(chats, monkeypatch):
    assert jobs.job_style().startswith("исходящих за полгода 10 — мало")
    monkeypatch.setattr(style, "MIN_MESSAGES", 5)
    seen = {}

    def fake_run(prompt, **kw):
        seen.update(kw, prompt=prompt)
        assert style.card()["author"] == "auto"  # карточка из цифр уже есть, пока Claude пишет свою
        style.save_card("# Как пишет директор\n\n" + "- Коротко, без точки в конце.\n" * 10)
        return {"text": ""}

    monkeypatch.setattr(brain, "run", fake_run)
    assert jobs.job_style() == "карточка стиля обновлена по 10 сообщениям"
    assert seen["job"] == "style" and "навыку style" in seen["prompt"]
    assert jobs.job_style() == "нового в переписке мало — карточка прежняя"  # с прошлой карточки ничего не писал
    later = store.to_utc_iso(datetime.now(timezone.utc) + timedelta(minutes=1))
    for i in range(style.FRESH):
        store.add_message({"source": "telegram", "chat_id": "100", "msg_id": f"new{i}", "is_outgoing": 1,
                           "text": f"новое {i}", "ts": later})
    monkeypatch.setattr(brain, "run", lambda prompt, **kw: {"text": ""})  # Claude карточку не сохранил
    with pytest.raises(RuntimeError, match="не сохранил"):
        jobs.job_style()
    assert style.card()["author"] == "claude"  # прежняя карточка Claude осталась
    assert "style" in jobs.REGISTRY and jobs.TITLES["style"].startswith("Карточка")


def test_first_card_without_waiting_for_friday(chats, monkeypatch):
    started = []
    monkeypatch.setattr(jobs, "submit", lambda name, kind="light", args=None, **k: started.append(name) or True)
    monkeypatch.setattr(jobs, "in_work_hours", lambda *a, **k: True)
    monkeypatch.setattr(config, "now", lambda s=None: datetime(2026, 11, 10, 12, 0, tzinfo=config.tz(s)))  # вторник
    monkeypatch.setattr(style, "MIN_MESSAGES", 5)
    jobs._style_checked[0] = 0.0
    jobs.tick()
    jobs._style_checked[0] = 0.0
    jobs.tick()
    assert started.count("style") == 1  # один раз — дальше по пятницам и по кнопке


def test_skill_page_shows_card_and_drafts(chats):
    from starlette.testclient import TestClient

    from opsdash.web.app import app

    client = TestClient(app, base_url="http://localhost:8765")
    style.save_card("# Как пишет директор\n\n" + "- Коротко, без точки в конце.\n" * 10)
    page = client.get("/skills/style").text
    assert "Ваш стиль ответов" in page and "Без точки в конце" not in page and "Коротко, без точки в конце" in page
    assert "Черновики ответов" in page
    assert "Черновики ответов" in client.get("/skills/triage").text
