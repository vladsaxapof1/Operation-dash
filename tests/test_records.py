from datetime import timedelta

import pytest

from opsdash import config, records, store


def test_commitments_dedupe_overdue_and_close():
    yesterday = (config.now().date() - timedelta(days=1)).isoformat()
    a = records.add_commitment(direction="to_me", who="Пётр", what="Прислать прогноз", due=yesterday)
    b = records.add_commitment(direction="to_me", who="Пётр", what="прислать прогноз")
    assert a == b, "то же открытое обещание не дублируется"
    items = records.list_commitments(direction="to_me")
    assert len(items) == 1 and items[0]["overdue"]
    assert records.list_commitments(overdue=True)[0]["id"] == a

    records.update_commitment(a, status="done")
    assert records.list_commitments() == []
    done = records.list_commitments(status="done")[0]
    assert done["closed_at"] and not done["overdue"]


def test_commitment_rejects_bad_direction_and_date():
    import pytest

    with pytest.raises(ValueError):
        records.add_commitment(direction="maybe", what="x")
    with pytest.raises(ValueError):
        records.add_commitment(direction="mine", what="x", due="30.10.2026")


def test_headline_short_title_or_trimmed_what():
    assert records.headline({"title": " Прогноз по клиентам ", "what": "длинно"}) == "Прогноз по клиентам"
    assert records.headline({"what": "Обновить записку к WBR"}) == "Обновить записку к WBR"
    # без заголовка — до первой скобки, двоеточия, тире или запятой
    assert records.headline({"what": "Исправить своды по продажам (и по допродажам): тянуть только квалифицированные лиды, "
                                     "а не все"}) == "Исправить своды по продажам"
    assert records.headline({"what": "Собрать по банку раскладку в том формате, который запросил коллега "
                                     "(ответ на его сообщение с образцом)"}) == "Собрать по банку раскладку в том формате"
    long = records.headline({"what": "Протестировать проставление оценок звонков через браузерное расширение и отписаться "
                                     "коллеге получилось или нет"})
    assert long.endswith("…") and len(long) <= records.HEADLINE_MAX and " " not in long[-2:]
    cid = records.add_commitment(direction="mine", what="Подробно: что и как", title="Коротко")
    assert store.row("SELECT title FROM commitments WHERE id=?", (cid,))["title"] == "Коротко"


def test_people_upsert_merges_aliases_and_find():
    pid = records.upsert_person("Пётр Иванов", aliases=["Петя"], telegram="@p_ivanov")
    same = records.upsert_person("Пётр Иванов", aliases=["Иванов"], role="продажи")
    assert pid == same
    p = records.find_person("петя")
    assert p["id"] == pid and set(p["aliases"]) == {"Петя", "Иванов"} and p["role"] == "продажи"
    assert records.find_person("@p_ivanov")["id"] == pid
    assert records.find_person("Иван")["id"] == pid  # частичное совпадение, единственный кандидат
    assert records.find_person("Сидоров") is None


def _msg(chat: str, msg_id: str, sender: str, text: str, ts: str, **extra) -> int:
    store.add_message({"source": "telegram", "chat_id": chat, "chat_title": "Пётр", "msg_id": msg_id,
                       "sender_name": sender, "text": text, "ts": ts, "is_outgoing": sender == "Я", **extra})
    return store.row("SELECT id FROM messages WHERE chat_id=? AND msg_id=?", (chat, msg_id))["id"]


def test_attention_closes_only_after_approval(capsys):
    """Любое её сообщение в чате — ещё не ответ: Claude по смыслу предлагает закрыть, директор подтверждает."""
    from opsdash import cli, jobs, proposals

    store.upsert_chat("telegram", "1", "Пётр", "private", enabled=1)
    mid = _msg("1", "10", "Пётр", "Посмотришь задачи по счетам? Нужны комментарии", "2026-10-27T14:23:00+00:00")
    aid = records.add_attention(message_id=mid, summary="Дать комментарии по задачам")
    assert records.add_attention(message_id=mid, summary="обновлено") == aid  # без дублей
    other = _msg("1", "11", "Я", "Не знаю, спроси у Олега", "2026-10-27T15:27:00+00:00")
    jobs.job_housekeeping()
    assert [a["id"] for a in records.list_attention("open")] == [aid], "сообщение о другом не закрывает пункт"

    cli.main(["attention", "list"])
    assert f"после вопроса вы писали [{other}]" in capsys.readouterr().out, "Claude видит, что она писала"
    with pytest.raises(proposals.ProposalError):
        proposals.propose_answered(attention_id=aid, source_ref=f"telegram:{other}")  # без довода
    p = proposals.propose_answered(attention_id=aid, reason="ответила", quote="Не знаю", source_ref=f"telegram:{other}")
    assert p["status"] == "pending" and records.list_attention("open")[0]["done_hint"]["id"] == p["id"]
    proposals.reject(p["id"])
    assert records.list_attention("open")[0]["done_hint"] is None
    with pytest.raises(proposals.ProposalError, match="Нет"):
        proposals.propose_answered(attention_id=aid, reason="ответила", source_ref=f"telegram:{other}")

    answer = _msg("1", "12", "Я", "Посмотрел, по второй задаче ок, по остальным правки в документе",
                  "2026-10-28T08:00:00+00:00")
    p = proposals.propose_answered(attention_id=aid, reason="дал комментарии", quote="по второй задаче ок",
                                   source_ref=f"telegram:{answer}")
    assert "похоже, вы ответили" in p["title"] and "Ваш ответ" in proposals.describe(p)
    assert proposals.approve(p["id"])["status"] == "done"
    assert records.list_attention("open") == []


def test_reopened_attention_needs_a_newer_answer():
    from opsdash import proposals

    store.upsert_chat("telegram", "1", "Пётр", "private", enabled=1)
    mid = _msg("1", "10", "Пётр", "Согласуете?", "2026-10-25T09:00:00+00:00")
    old = _msg("1", "11", "Я", "Да", "2026-10-25T09:05:00+00:00")
    aid = records.add_attention(message_id=mid, summary="Пётр ждёт согласования")
    records.close_attention(aid, "done")
    records.close_attention(aid, "open")  # «Вернуть»: это был не ответ
    with pytest.raises(proposals.ProposalError, match="вернула"):
        proposals.propose_answered(attention_id=aid, reason="ответила «Да»", source_ref=f"telegram:{old}")
    p = proposals.propose_answered(attention_id=aid, reason="ответила", source_ref="manual:")
    records.close_attention(aid, "done")  # закрыла сама — предложение больше не ждёт ответа
    assert proposals.get(p["id"])["status"] == "done"


def test_replies_after_in_group_marks_direct_reply():
    store.upsert_chat("telegram", "-5", "Операционка", "group", enabled=1)
    mid = _msg("-5", "1", "Олег", "Анна, что ответить клиенту?", "2026-10-25T08:00:00+00:00")
    _msg("-5", "2", "Я", "Гляну после обеда", "2026-10-25T08:05:00+00:00")
    _msg("-5", "3", "Я", "14 дней", "2026-10-25T09:00:00+00:00", reply_to="1")
    records.add_attention(message_id=mid, summary="Олег ждёт ответа клиенту")
    replies = records.replies_after(records.list_attention("open")[0])
    assert [(r["text"], r["is_reply"]) for r in replies] == [("Гляну после обеда", False), ("14 дней", True)]


def test_in_work_goes_first_and_archive_hides():
    today = config.now().date()
    soon = records.add_commitment(direction="mine", what="Срочное", due=today.isoformat())
    later = records.add_commitment(direction="mine", what="Не к спеху", due=(today + timedelta(days=9)).isoformat())
    owed = records.add_commitment(direction="to_me", who="Пётр", what="Прислать отчёт", due=today.isoformat())
    assert [c["id"] for c in records.list_commitments(direction="mine")] == [soon, later]

    records.set_in_work(later, True)
    mine = records.commitments_board()["mine"]
    assert [c["id"] for c in mine] == [later, soon] and mine[0]["in_work"] and not mine[1]["in_work"]
    assert [c["id"] for c in records.list_commitments(direction="mine", sort="created")][0] == later
    records.set_in_work(later, False)
    assert [c["id"] for c in records.list_commitments(direction="mine")] == [soon, later]

    records.set_in_work(owed, True)
    records.set_archived(owed, True)
    assert not records.get_commitment(owed)["in_work"], "в архиве — уже не в работе"
    assert owed not in [c["id"] for c in records.list_commitments()]
    assert owed not in [c["id"] for c in records.commitments_board()["to_me"]]
    assert records.reminders() == [] and owed not in [o["id"] for o in records.task_options()]
    assert [c["id"] for c in records.list_commitments(status="archived")] == [owed]
    assert records.add_commitment(direction="to_me", who="Пётр", what="прислать отчёт") == owed, \
        "убранную в архив задачу Claude не заводит заново"
    records.set_archived(owed, False)
    assert owed in [c["id"] for c in records.list_commitments()]

    records.update_commitment(soon, status="done")
    assert [c["id"] for c in records.commitments_board()["done"]] == [soon]
    records.set_archived(soon, True)
    assert records.commitments_board()["done"] == []
    assert records.list_commitments(status="archived")[0]["status"] == "done", "архив не меняет статус"


def test_settings_roundtrip_and_hhmm(settings_with):
    s = settings_with(owner={"name": "Анна"})
    assert s["owner"]["name"] == "Анна" and s["owner"]["timezone"] == "Europe/Moscow"
    assert config.hhmm(1110) == "18:30"
    assert config.hhmm("9:05") == "09:05"
    config.set_secret("KAITEN_TOKEN", "abc\n")
    assert config.get_secret("KAITEN_TOKEN") == "abc"
    assert config.mask("1234567890abc") == "123••••abc"
