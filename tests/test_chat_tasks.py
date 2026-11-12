"""Под чатом в «новых сообщениях» — открытые задачи, к которым переписка вероятнее всего относится: по задаче уже
был ход из этого чата, это задачи собеседника, автора сообщения или упомянутого человека. Без этого фоновый разбор
искал задачу наугад в длинном списке (весь он в выдачу не помещается) и не предлагал закрыть сделанное. Пример:
Ольга обещала цифры продаж и прислала их в личку; Мария (её нет в справочнике) отвечает на вопросы; в общем чате
проверяющий пишет «все отчёты проверены, @Олег Соколов»."""

import json

import pytest

from opsdash import chat_tasks, cli, records, store, worklog

TS = "2026-11-09T07:5{}:00+00:00"


def _chat(source: str, chat_id: str, title: str, kind: str, username: str | None = None) -> None:
    store.upsert_chat(source, chat_id, title, kind, enabled=1, username=username)


def _msg(source: str, chat_id: str, title: str, msg_id: str, text: str, *, sender: str, minute: int = 0,
         processed: int = 0) -> int:
    store.add_message({"source": source, "chat_id": chat_id, "chat_title": title, "msg_id": msg_id,
                       "sender_name": sender, "is_outgoing": sender == "Я", "text": text, "ts": TS.format(minute)})
    mid = store.scalar("SELECT id FROM messages WHERE source=? AND chat_id=? AND msg_id=?", (source, chat_id, msg_id))
    store.execute("UPDATE messages SET processed=? WHERE id=?", (processed, mid))
    return mid


def _task(direction: str, title: str, *, who: str = "Анна", whom: str = "Анна", **kw) -> int:
    return records.add_commitment(direction=direction, title=title, what=title, who=who, whom=whom, force=True, **kw)


def _block(out: str, chat_title: str) -> list[str]:
    """Строки задач под чатом."""
    lines = out.split(f"«{chat_title}»", 1)[1].split("\n## ", 1)[0].splitlines()
    return [x.strip() for x in lines if x.startswith("  [") or x.startswith("  …")]


def test_tasks_under_each_chat(settings_with, capsys):
    settings_with(owner={"name": "Анна Директорова"})
    records.upsert_person("Ольга Петрова", aliases=["Ольга"])
    records.upsert_person("Ольга Смирнова")
    records.upsert_person("Олег Соколов", telegram="@oleg_s")
    figures = _task("to_me", "Цифры продаж за октябрь", who="Ольга Петрова", source_type="meeting",
                    source_ref="2026-11-09_1000")
    smirnova = _task("to_me", "Договор аренды", who="Ольга Смирнова")
    answers = _task("to_me", "Ответить на вопросы", who="мария", source_type="manual")  # её нет в справочнике
    reports = _task("to_me", "Сдать отчёты на проверку", who="Олег Соколов")
    checker = _task("mine", "Согласовать график проверок", whom="Игорь Волков")
    _task("mine", "Чужая задача", whom="Пётр")  # ни с одним чатом не связана

    _chat("telegram", "1", "Ольга LIFE", "private")
    _msg("telegram", "1", "Ольга LIFE", "1", "Продажи за октябрь: 12,4 млн", sender="Ольга LIFE")
    _chat("telegram", "2", "Мария LP", "private")
    asked = _msg("telegram", "2", "Мария LP", "1", "Вопрос 1. Кто проверяет поля?", sender="Я", processed=1)
    worklog.add(answers, "Вопросы отправлены Марии", source=f"telegram:{asked}")
    _msg("telegram", "2", "Мария LP", "2", "Ответ 1. Поля проверяет продажа", sender="Мария LP", minute=1)
    _chat("pachca", "3", "Отчёты отдела", "group")
    _msg("pachca", "3", "Отчёты отдела", "1", "все отчёты проверены @Олег Соколов @Анна Директорова FYI",
         sender="Игорь Волков", minute=2)
    _chat("telegram", "4", "Ольга Смирнова", "private")
    _msg("telegram", "4", "Ольга Смирнова", "1", "Договор подписала", sender="Ольга Смирнова", minute=3)
    _chat("telegram", "5", "Продажи", "group")
    _msg("telegram", "5", "Продажи", "1", "@oleg_s, как там отчёты?", sender="Я", minute=4)

    cli.main(["messages", "pending"])
    out = capsys.readouterr().out
    assert "→ Открытые задачи, к которым может относиться этот чат" in out
    # собеседник личного чата: «Ольга LIFE» — псевдоним «Ольга» у Ольги Петровой, а не Ольга Смирнова
    assert _block(out, "Ольга LIFE") == [
        f"[{figures}] Ольга Петрова → мне: «Цифры продаж за октябрь» — собеседник"]
    # человек не из справочника — по имени в задаче; ход из этого чата — сильнее, со «сейчас: …»
    assert _block(out, "Мария LP") == [
        f"[{answers}] мария → мне: «Ответить на вопросы» · сейчас (09.11, Telegram · Мария LP): Вопросы отправлены "
        "Марии — по ней уже был ход из этого чата"]
    # общий чат: упомянутый человек и автор сообщения; директор среди упомянутых не в счёт; два слова заголовка
    # в сообщении — «и по теме»
    assert _block(out, "Отчёты отдела") == [
        f"[{reports}] Олег Соколов → мне: «Сдать отчёты на проверку» — упомянут(а) Олег Соколов, "
        "и по теме (отчёты, проверку)",
        f"[{checker}] Я → Игорь Волков: «Согласовать график проверок» — пишет Игорь Волков"]
    # полное имя сильнее одного псевдонима: в чате Ольги Смирновой — только её задача
    assert _block(out, "Ольга Смирнова") == [f"[{smirnova}] Ольга Смирнова → мне: «Договор аренды» — собеседник"]
    # @имя Telegram из справочника; задача, показанная выше, второй раз — одной строкой
    assert _block(out, "Продажи") == [f"[{reports}] «Сдать отчёты на проверку» — выше (упомянут(а) Олег Соколов)"]
    assert "Чужая задача" not in out


def test_history_strength_order_cap_and_mail(settings_with, capsys, monkeypatch):
    settings_with(owner={"name": "Анна"})
    old = _task("mine", "Старая задача", whom="Борис")
    tasks = [_task("to_me", f"Задача {i}", who="Борис Ким") for i in range(3)]
    _chat("telegram", "7", "Борис Ким", "private")
    earlier = _msg("telegram", "7", "Борис Ким", "1", "Начнём", sender="Борис Ким", processed=1)
    worklog.add(old, "Борис взялся", source=f"telegram:{earlier}")
    _msg("telegram", "7", "Борис Ким", "2", "Сделал вторую", sender="Борис Ким", minute=1)
    monkeypatch.setattr(chat_tasks, "PER_CHAT", 2)
    cli.main(["messages", "pending"])
    block = _block(capsys.readouterr().out, "Борис Ким")
    assert block[0].startswith(f"[{old}] Я → Борис: «Старая задача»") and block[0].endswith("ход из этого чата")
    assert len(block) == 3 and block[1].endswith("— собеседник")  # дальше — задачи собеседника
    assert int(block[1][1:block[1].index("]")]) in tasks
    assert block[2] == '…и ещё 2: ./ops commitments list --brief --person "<имя>"'

    # почта — весь ящик одним «чатом»: ход из него ничего не говорит о связи, важен только отправитель
    store.execute("UPDATE messages SET processed=1")
    _chat("mail", "gmail:INBOX", "Gmail: входящие", "mail")
    first = _msg("mail", "gmail:INBOX", "Gmail: входящие", "m1", "Счёт", sender="Бухгалтерия", processed=1)
    unrelated = _task("mine", "Оплатить счёт", whom="Бухгалтерия")
    worklog.add(unrelated, "Пришёл счёт", source=f"mail:{first}")
    _msg("mail", "gmail:INBOX", "Gmail: входящие", "m2", "Отчёт готов", sender="Вера Лис", minute=2)
    cli.main(["messages", "pending"])
    assert "Оплатить счёт" not in capsys.readouterr().out


def _reply(source: str, chat_id: str, title: str, msg_id: str, text: str, *, sender: str, to: str,
           minute: int = 0) -> int:
    store.add_message({"source": source, "chat_id": chat_id, "chat_title": title, "msg_id": msg_id, "reply_to": to,
                       "sender_name": sender, "is_outgoing": sender == "Я", "text": text, "ts": TS.format(minute)})
    return store.scalar("SELECT id FROM messages WHERE source=? AND chat_id=? AND msg_id=?", (source, chat_id, msg_id))


def test_topic_tasks_and_propose_log(settings_with, capsys, monkeypatch):
    """Директор обещала Олегу «Демо и макеты CRM», а в треде Петра про новый интерфейс CRM ответила «хочу отдать
    макет, как вижу CRM с очередью» (11.11.2026). По людям задача с тредом не связана — разбор её не видел и мог
    завести новую задачу. Теперь она под чатом «по теме», а ход и подзадачу Claude предлагает, не пишет сам."""
    from fastapi.testclient import TestClient

    from opsdash import proposals, welcome
    from opsdash.web.app import app

    settings_with(owner={"name": "Анна Директорова"})
    records.upsert_person("Олег Соколов")
    records.upsert_person("Пётр Иванов")
    crm = _task("mine", "Демо и макеты CRM", whom="Олег Соколов")
    _task("to_me", "Отчёт по CRM", who="Вера Лис")  # одно общее слово — не тема
    _task("mine", "Договор аренды", whom="Олег Соколов")

    _chat("pachca", "9", "Проект CRM", "group")
    _msg("pachca", "9", "Проект CRM", "100", "Мы обновили интерфейс CRM", sender="Пётр Иванов")
    reply = _reply("pachca", "9", "Проект CRM", "101", "Норм. Хочу макет отдать, как вижу «тунельную» CRMку с очередью", sender="Я",
                   to="100", minute=1)
    cli.main(["messages", "pending"])
    assert _block(capsys.readouterr().out, "Проект CRM") == [
        f"[{crm}] Я → Олег Соколов: «Демо и макеты CRM» — по теме (макеты, CRM) — {chat_tasks.TOPIC_HINT}"]

    args = ["propose", "log", "--commitment", str(crm), "--source", f"pachca:{reply}",
            "--text", "Пётр показал новый интерфейс CRM; вы ответили, что отдадите макет CRM с очередью",
            "--quote", "хочу отдать макет, как вижу CRM с очередью", "--subtask", "Макет CRM с очередью",
            "--whom", "Пётр Иванов", "--batch", "triage:2026-11-11"]
    with pytest.raises(SystemExit):  # без «почему» не предлагаем
        cli.main(args)
    assert "почему это про эту задачу" in capsys.readouterr().err
    args += ["--reason", "Тот же макет CRM, что вы обещали Олегу"]
    cli.main(args)
    cli.main(args)  # повтор — то же предложение, не второе
    assert "ждёт подтверждения директора" in capsys.readouterr().out
    [p] = proposals.list_proposals()
    assert p["kind"] == "task.log" and proposals.action(p) == "Добавить подзадачу"
    assert p["title"] == "«Демо и макеты CRM» (Я → Олег Соколов): подзадача «Макет CRM с очередью» и запись в ход"
    assert "подзадача «Макет CRM с очередью» (ваша, для Пётр Иванов)" in proposals.describe(p)
    assert [label for label, _ in proposals.links(p)] == ["Задача", "Сообщение в Пачке"]

    monkeypatch.setattr(welcome, "should_open", lambda settings: False)
    home = TestClient(app, base_url="http://localhost:8765").get("/").text
    assert "похоже, по задаче" in home and "Не про эту задачу" in home and "Добавить подзадачу" in home

    done = proposals.approve(p["id"])
    assert done["status"] == "done" and proposals.result_text(done) == "✅ Подзадача добавлена, запись — в ходе задачи"
    sub = store.row("SELECT * FROM commitments WHERE id=?", (done["result"]["subtask_id"],))
    assert (sub["parent_id"], sub["direction"], sub["whom"], sub["title"]) == (crm, "mine", "Пётр Иванов",
                                                                              "Макет CRM с очередью")
    assert (sub["source_type"], sub["source_ref"]) == ("pachca", str(reply))
    entry = store.row("SELECT * FROM task_log WHERE task_id=? AND kind!='event'", (crm,))
    assert entry["text"].startswith("Пётр показал") and (entry["source_type"], entry["source_ref"]) == ("pachca",
                                                                                                         str(reply))
    with pytest.raises(proposals.ProposalError, match="уже есть в ходе"):
        proposals.propose_task_log(commitment_id=crm, text="ещё раз", reason="то же", source_ref=f"pachca:{reply}")

    # «Не про эту задачу» — по этому сообщению больше не предлагаем; без подзадачи — просто запись в ход
    other = _msg("pachca", "9", "Проект CRM", "102", "Макеты CRM для склада посмотрим позже", sender="Пётр Иванов",
                 minute=2)
    p2 = proposals.propose_task_log(commitment_id=crm, text="Пётр: макеты для склада — позже", reason="макеты CRM",
                                    source_ref=f"pachca:{other}")
    assert proposals.action(p2) == "Записать в ход" and p2["title"].endswith("в ход — Пётр: макеты для склада — позже")
    proposals.reject(p2["id"])
    with pytest.raises(proposals.ProposalError, match="уже ответила «Нет»"):
        proposals.propose_task_log(commitment_id=crm, text="снова", reason="макеты", source_ref=f"pachca:{other}")


def test_colleague_task_names_the_sender(settings_with, capsys):
    """HR пишет директору, что с сотрудником поговорили и дату согласовали, а задача «Минус ставка в отделе ОП» — между
    двумя руководителями: HR только назван в её тексте (12.11.2026). По людям связи не было, по теме — одно общее
    слово («АБ» из двух букв не считалось), и разбор записал новость в соседнюю задачу HR. Теперь задача под чатом."""
    settings_with(owner={"name": "Анна Директорова"})
    records.upsert_person("Ольга Петрова", aliases=["Ольга"])
    records.upsert_person("Ольга Смирнова")
    records.upsert_person("Пётр Иванов")
    records.upsert_person("Олег Соколов")
    cut = records.add_commitment(direction="other", who="Пётр Иванов", whom="Олег Соколов",
                                 title="Минус ставка в отделе ОП", force=True,
                                 what="Сократить одну ставку в отделе ОП до конца месяца; разговор — с Ольгой Петровой")
    # «с Ольгой» без фамилии — Ольг в справочнике две: не угадываем
    records.add_commitment(direction="other", who="Пётр Иванов", whom="Олег Соколов", title="График отпусков",
                           what="Свести график отпусков, согласовать с Ольгой", force=True)
    store_mo = _task("other", "Склад в МО", who="Пётр Иванов", whom="Олег Соколов")
    _task("other", "Склад в области", who="Пётр Иванов", whom="Олег Соколов")  # «МО» нет — одно слово, не тема

    _chat("telegram", "11", "Ольга HR Петрова", "private")
    _msg("telegram", "11", "Ольга HR Петрова", "1", "в ОП поговорили, договорились на дату, как подпишет — дам знать",
         sender="Ольга HR Петрова")
    _chat("telegram", "12", "Логистика", "group")
    _msg("telegram", "12", "Логистика", "1", "по МО склад нашли", sender="Вера Лис", minute=1)
    cli.main(["messages", "pending"])
    out = capsys.readouterr().out
    [line] = _block(out, "Ольга HR Петрова")
    assert line.startswith(f"[{cut}] ") and line.endswith("— Ольга Петрова упомянут(а) в тексте задачи")
    assert "График отпусков" not in out
    [line] = _block(out, "Логистика")
    assert line.startswith(f"[{store_mo}] ") and line.endswith(f"— по теме (Склад, МО) — {chat_tasks.TOPIC_HINT}")
    assert "Склад в области" not in out

    # слова с маленькой буквы — не имя; падежи имени — имя
    people = records.list_people()
    olga = next(p for p in people if p["name"] == "Ольга Петрова")
    assert chat_tasks._named(olga, chat_tasks._names_in("Передать Ольге Петровой отчёт"), people)
    assert not chat_tasks._named(olga, chat_tasks._names_in("ольга петрова"), people)
    assert not chat_tasks._named(olga, chat_tasks._names_in("Передать Ольге отчёт"), people)


def test_brief_and_search(capsys):
    long_what = "Собрать сводку по продажам за октябрь по всем направлениям, с разбивкой по менеджерам " * 5
    a = records.add_commitment(direction="to_me", who="Ольга", title="Сводка продаж", what=long_what, due="2026-11-12")
    b = records.add_commitment(direction="mine", whom="Пётр", title="Договор аренды", what="Подписать договор")
    cli.main(["commitments", "list", "--brief"])
    brief = capsys.readouterr().out
    assert brief.splitlines() == [f"[{a}] Ольга → мне: «Сводка продаж» · до 2026-11-12",
                                  f"[{b}] Я → Пётр: «Договор аренды»"]
    cli.main(["commitments", "list"])
    assert len(capsys.readouterr().out) > 3 * len(brief)  # полный — с сутью и всем остальным
    cli.main(["commitments", "list", "--brief", "--search", "аренд"])
    assert capsys.readouterr().out.strip() == f"[{b}] Я → Пётр: «Договор аренды»"
    cli.main(["commitments", "list", "--search", "менеджерам"])
    assert capsys.readouterr().out.startswith(f"[{a}] мне ← Ольга → ?: «Сводка продаж» Собрать сводку")


def _meeting(mid: str, start: str, linked: list[int] | None = None) -> None:
    store.execute("INSERT INTO meetings(id, title, status, started_at) VALUES(?, 'Продажи софта', 'processing', ?)",
                  (mid, start))
    folder = records.meeting_dir(mid)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "meta.json").write_text(json.dumps({"id": mid, "tasks": [{"id": i} for i in linked or []]}),
                                      encoding="utf-8")


def test_meeting_messages_since_start(settings_with, capsys, monkeypatch):
    """«Скинь цены в телегу» — и Пётр прислал их, пока встреча ещё шла. Triage разобрал сообщение раньше, чем разбор
    встречи записал задачу, и сверить было не с чем. Разбор встречи видит переписку с её начала по своим задачам."""
    settings_with(owner={"name": "Анна Директорова"})
    records.upsert_person("Пётр Смирнов", aliases=["Пётр"])
    records.upsert_person("Ольга Петрова", aliases=["Ольга"])
    linked = _task("to_me", "Отчёт по оттоку", who="Ольга Петрова")  # директор заранее связала со встречей
    _meeting("m1", TS.format(1), linked=[linked])
    prices = _task("to_me", "Цены тарифов", who="Пётр", source_type="meeting", source_ref="m1")
    done = _task("to_me", "Список клиентов", who="Пётр", source_type="meeting", source_ref="m1")
    store.execute("UPDATE commitments SET status='done' WHERE id=?", (done,))
    elsewhere = _task("to_me", "Договор с банком", who="Пётр", source_type="meeting", source_ref="m0")

    _chat("telegram", "1", "Пётр LP", "private")
    _msg("telegram", "1", "Пётр LP", "1", "Старый прайс до встречи", sender="Пётр LP", minute=0, processed=1)
    _msg("telegram", "1", "Пётр LP", "2", "Оптимальный 990 ₽/мес, Базовый 490 ₽/мес", sender="Пётр LP", minute=3,
         processed=1)
    _msg("telegram", "1", "Пётр LP", "3", "Спасибо!", sender="Я", minute=4, processed=1)
    _chat("pachca", "2", "Продажи", "group")
    _msg("pachca", "2", "Продажи", "1", "Кто сегодня дежурит?", sender="Игорь Волков", minute=2, processed=1)
    _msg("pachca", "2", "Продажи", "2", "Отток выложила, @Анна Директорова", sender="Ольга Петрова", minute=5,
         processed=1)
    _chat("telegram", "3", "Вера", "private")
    _msg("telegram", "3", "Вера", "1", "Привет!", sender="Вера", minute=6, processed=1)

    cli.main(["meetings", "messages", "m1"])
    out = capsys.readouterr().out
    assert "Triage разобрал эти сообщения, когда задач встречи ещё не было" in out
    peer = out.split("«Пётр LP»", 1)[1].split("\n## ", 1)[0]
    assert f"[{prices}] Пётр → мне: «Цены тарифов» — собеседник" in peer
    assert "Оптимальный 990 ₽/мес" in peer and "Я: Спасибо!" in peer  # личный чат — целиком, с ответами директора
    assert "Старый прайс" not in out  # до начала встречи — не её переписка
    assert "Список клиентов" not in out and "Договор с банком" not in out  # закрытая и задача другой встречи
    group = out.split("«Продажи»", 1)[1].split("\n## ", 1)[0]
    assert f"[{linked}] Ольга Петрова → мне: «Отчёт по оттоку» — пишет Ольга Петрова" in group
    assert "Отток выложила" in group and "дежурит" not in group  # в группе — только сообщения по задачам
    assert "«Вера»" not in out
    assert elsewhere

    monkeypatch.setattr(chat_tasks, "MEETING_MESSAGES", 1)
    cli.main(["meetings", "messages", "m1"])
    out = capsys.readouterr().out
    # раньше по делу написал Пётр (дежурство в группе в счёт не идёт); чат с задачей виден и без сообщений
    assert out.index("«Пётр LP»") < out.index("«Продажи»")
    assert "Спасибо!" not in out and '…и ещё 1: ./ops messages chat "Пётр LP" --days 2' in out
    assert "Отток выложила" not in out and '…и ещё 1: ./ops messages chat "Продажи" --days 2' in out

    _meeting("m2", TS.format(7))
    cli.main(["meetings", "messages", "m2"])
    assert "нет открытых задач" in capsys.readouterr().out
    _task("to_me", "Презентация", who="Пётр", source_type="meeting", source_ref="m2")
    cli.main(["meetings", "messages", "m2"])
    assert "в чатах по её задачам ничего не писали" in capsys.readouterr().out


def test_meeting_task_shows_messages_on_add(settings_with, capsys, monkeypatch):
    """Разбор встречи вызывал общую проверку раньше, чем записывал задачи, — и сверять было не с чем. Поэтому
    commitments add со встречи сразу показывает переписку с её начала по новой задаче."""
    settings_with(owner={"name": "Анна Директорова"})
    records.upsert_person("Пётр Смирнов", aliases=["Пётр"])
    _meeting("m1", TS.format(1))
    cli.main(["meetings", "messages", "m1"])
    assert "вызови эту команду снова, когда запишешь" in capsys.readouterr().out  # рано: задач ещё нет

    _chat("telegram", "1", "Пётр LP", "private")
    for i in range(3):
        _msg("telegram", "1", "Пётр LP", str(i), f"Цены, часть {i}", sender="Пётр LP", minute=2 + i, processed=1)
    _chat("telegram", "2", "Вера", "private")
    _msg("telegram", "2", "Вера", "1", "Привет!", sender="Вера", minute=3, processed=1)
    monkeypatch.setattr(cli, "TASK_MEETING_MESSAGES", 2)
    cli.main(["commitments", "add", "--direction", "to_me", "--who", "Пётр", "--title", "Цены тарифов",
              "--what", "Прислать цены тарифов", "--source", "meeting:m1"])
    out = capsys.readouterr().out
    assert "По этой задаче уже писали с начала встречи" in out and "propose done" in out
    assert "Цены, часть 0" in out and "Цены, часть 1" in out and "Цены, часть 2" not in out
    assert "…и ещё 1: ./ops meetings messages m1" in out
    assert "Вера" not in out and "→ Задачи встречи" not in out  # задача одна — список задач под чатом не нужен

    cli.main(["commitments", "add", "--direction", "to_me", "--who", "Ольга", "--title", "Отчёт",
              "--what", "Прислать отчёт", "--source", "meeting:m1"])
    assert "уже писали" not in capsys.readouterr().out  # по задаче никто не писал — и строки нет
    cli.main(["commitments", "add", "--direction", "to_me", "--who", "Пётр", "--title", "Договор",
              "--what", "Прислать договор", "--source", "telegram:1"])
    assert "уже писали" not in capsys.readouterr().out  # не со встречи — переписку разбирает triage
