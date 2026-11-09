"""Под чатом в «новых сообщениях» — открытые задачи, к которым переписка вероятнее всего относится: по задаче уже
был ход из этого чата, это задачи собеседника, автора сообщения или упомянутого человека. Без этого фоновый разбор
искал задачу наугад в длинном списке (весь он в выдачу не помещается) и не предлагал закрыть сделанное. Пример:
Ольга обещала цифры продаж и прислала их в личку; Мария (её нет в справочнике) отвечает на вопросы; в общем чате
проверяющий пишет «все отчёты проверены, @Олег Соколов»."""

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
    # общий чат: упомянутый человек и автор сообщения; директор среди упомянутых не в счёт
    assert _block(out, "Отчёты отдела") == [
        f"[{reports}] Олег Соколов → мне: «Сдать отчёты на проверку» — упомянут(а) Олег Соколов",
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
