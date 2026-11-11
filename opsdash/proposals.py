"""Очередь подтверждений.

Claude никогда не пишет во внешние системы напрямую: он создаёт «предложение»
(создать карточку, оставить комментарий, назначить встречу). Так же — закрыть задачу PULSE,
если по переписке или встрече она выглядит выполненной, связать задачи («эта ждёт ту»), объединить
дубли, дописать в ход задачи сообщение, которое относится к ней только по теме, и закрыть пункт «нужен ответ»,
если директор, судя по её сообщению, уже ответила: решает директор. Предложение
выполняется, когда директор нажимает «Да» в боте или в админке — либо сразу,
если в настройках автономии для этого действия выбрано «auto».
"""

from __future__ import annotations

import json
import re

from opsdash import config, records, store
from opsdash.sources import kaiten

KINDS = {
    "kaiten.create_card": "Создать карточку в Kaiten",
    "kaiten.comment": "Комментарий в Kaiten",
    "kaiten.update": "Изменить карточку Kaiten",
    "kaiten.block": "Заблокировать карточку в Kaiten",
    "calendar.create": "Создать встречу в календаре",
    "calendar.cancel": "Отменить встречу",
    "task.done": "Закрыть выполненную задачу",
    "task.meeting": "Связать задачу со встречей",
    "task.wait": "Задача ждёт другую задачу или событие",
    "task.merge": "Объединить задачу-дубль",
    "task.log": "Дописать в ход задачи",
    "project.create": "Собрать задачи в проект",
    "attention.done": "Закрыть «нужен ответ» — директор ответила",
    "telegram.remind": "Напомнить коллеге в Telegram",
    "skill.rule": "Правило для навыка — урок из ваших «Нет»",
    "mcp.call": "Действие в подключённом сервисе (MCP)",
}

# надпись на кнопке подтверждения — что произойдёт, а не просто «Да»
ACTIONS = {
    "kaiten.create_card": "Создать карточку",
    "kaiten.comment": "Оставить комментарий",
    "kaiten.update": "Изменить карточку",
    "kaiten.block": "Заблокировать",
    "calendar.create": "Создать встречу",
    "calendar.cancel": "Отменить встречу",
    "task.done": "Закрыть задачу",
    "task.meeting": "Связать со встречей",
    "task.wait": "Отметить «ждёт»",
    "task.merge": "Объединить",
    "task.log": "Записать в ход",
    "project.create": "Создать проект",
    "attention.done": "Отметить отвеченным",
    "telegram.remind": "Напомнить",
    "skill.rule": "Включить правило",
    "mcp.call": "Выполнить",
}


def action(p: dict) -> str:
    """Надпись на кнопке «выполнить» у предложения: «Закрыть задачу», «Связать со встречей», «Напомнить»."""
    if p["kind"] == "task.log" and (p.get("payload") or {}).get("subtask"):
        return "Добавить подзадачу"
    return ACTIONS.get(p["kind"], "Да")


class ProposalError(Exception):
    pass


def _fmt_date(d: str | None) -> str:
    if not d:
        return ""
    try:
        y, m, dd = d[:10].split("-")
        return f"{dd}.{m}"
    except ValueError:
        return d


def _create(kind: str, title: str, payload: dict, *, autonomy_key: str, reason: str | None,
            source_ref: str | None, batch: str | None) -> dict:
    dup = store.row(
        "SELECT id FROM proposals WHERE kind=? AND title=? AND status='pending'", (kind, title)
    )
    if dup:
        return get(dup["id"])
    pid = store.execute(
        """INSERT INTO proposals(kind, title, payload, reason, source_ref, batch, status, created)
           VALUES(?,?,?,?,?,?,'pending',?)""",
        (kind, title, store.json_col(payload), reason, source_ref, batch, store.utcnow()),
    )
    settings = config.load_settings()
    if settings["autonomy"].get(autonomy_key) == "auto":
        return approve(pid)
    return get(pid)


def get(pid: int) -> dict | None:
    p = store.row("SELECT * FROM proposals WHERE id=?", (pid,))
    if p:
        p["payload"] = store.loads(p["payload"], {})
        p["result"] = store.loads(p["result"], p["result"])
    return p


def list_proposals(status: str | None = "pending", batch: str | None = None, limit: int = 200) -> list[dict]:
    sql = "SELECT * FROM proposals WHERE 1=1"
    params: list = []
    if status and status != "all":
        sql += " AND status=?"
        params.append(status)
    if batch:
        sql += " AND batch=?"
        params.append(batch)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    out = store.rows(sql, params)
    for p in out:
        p["payload"] = store.loads(p["payload"], {})
        p["result"] = store.loads(p["result"], p["result"])
    return out


# --- создание предложений ---------------------------------------------------

def propose_kaiten_card(*, title: str, description: str = "", due: str | None = None,
                        responsible: str | None = None, target: str | None = None,
                        commitment_id: int | None = None, batch: str | None = None,
                        reason: str | None = None, source_ref: str | None = None) -> dict:
    settings = config.load_settings()
    t = kaiten.find_target(target, settings)
    if not t:
        if not kaiten.targets(settings):
            raise ProposalError("В настройках Kaiten не задано, куда создавать карточки (раздел «Куда создавать»)")
        names = ", ".join(x["name"] for x in kaiten.targets(settings))
        raise ProposalError(f"Нет направления «{target}». Есть: {names}")
    user = kaiten.resolve_user(responsible) if responsible else None
    me = store.get_state("kaiten_me") or {}
    # «на себя» — только если исполнитель не указан или это точно директор;
    # не найденный в Kaiten человек считается коллегой (нужно подтверждение)
    for_me = (not responsible) or bool(user and me.get("id") and user.get("id") == me.get("id"))
    payload = {
        "title": title.strip(),
        "description": description.strip(),
        "due": due,
        "target": t["name"],
        "board_id": t.get("board_id"),
        "column_id": t.get("column_id"),
        "lane_id": t.get("lane_id"),
        "space_id": t.get("space_id"),
        "responsible_id": (user or {}).get("id"),
        "responsible_name": (user or {}).get("full_name") or (responsible if user else None),
        "responsible_unresolved": responsible if (responsible and not user) else None,
        "commitment_id": commitment_id,
    }
    who = payload["responsible_name"] or "без исполнителя"
    label = f"Карточка «{payload['title']}» → {who}"
    if due:
        label += f", до {_fmt_date(due)}"
    key = "kaiten_create_for_me" if for_me else "kaiten_create_for_others"
    return _create("kaiten.create_card", label, payload, autonomy_key=key, reason=reason,
                   source_ref=source_ref, batch=batch)


def _card_title(card_id: int) -> str:
    c = store.row("SELECT title FROM kaiten_cards WHERE id=?", (card_id,))
    return f"#{card_id} «{c['title']}»" if c else f"#{card_id}"


def propose_kaiten_comment(*, card_id: int, text: str, batch: str | None = None, reason: str | None = None,
                           source_ref: str | None = None) -> dict:
    payload = {"card_id": int(card_id), "text": text.strip()}
    label = f"Комментарий к {_card_title(int(card_id))}: {text.strip()[:80]}"
    return _create("kaiten.comment", label, payload, autonomy_key="kaiten_comment", reason=reason,
                   source_ref=source_ref, batch=batch)


def propose_kaiten_update(*, card_id: int, due: str | None = None, title: str | None = None,
                          responsible: str | None = None, column_id: int | None = None,
                          batch: str | None = None, reason: str | None = None, source_ref: str | None = None) -> dict:
    fields: dict = {}
    changes = []
    if due:
        fields["due_date"] = due
        changes.append(f"срок → {_fmt_date(due)}")
    if title:
        fields["title"] = title
        changes.append(f"название → «{title}»")
    if column_id:
        fields["column_id"] = int(column_id)
        changes.append("перенос в другую колонку")
    payload: dict = {"card_id": int(card_id), "fields": fields}
    if responsible:
        user = kaiten.resolve_user(responsible)
        if not user:
            raise ProposalError(f"Не нашла в Kaiten пользователя «{responsible}»")
        payload["responsible_id"] = user["id"]
        changes.append(f"исполнитель → {user.get('full_name') or responsible}")
    if not changes:
        raise ProposalError("Нечего менять: укажите срок, название, исполнителя или колонку")
    label = f"Изменить {_card_title(int(card_id))}: {', '.join(changes)}"
    return _create("kaiten.update", label, payload, autonomy_key="kaiten_update", reason=reason,
                   source_ref=source_ref, batch=batch)


def propose_kaiten_block(*, card_id: int, blocker_card: int | None = None, text: str | None = None,
                         batch: str | None = None, reason: str | None = None, source_ref: str | None = None) -> dict:
    """Заблокировать карточку в Kaiten: другой карточкой (blocker_card — задача ждёт её) и/или причиной text.
    Так «ждёт» из PULSE видно и в самой карточке Kaiten."""
    if not kaiten.blockers_supported():
        raise ProposalError("На этом тарифе Kaiten нет блокировок карточек")
    if not blocker_card and not (text or "").strip():
        raise ProposalError("Укажите карточку, которая блокирует (--blocker-card), или причину (--text)")
    if blocker_card and int(blocker_card) == int(card_id):
        raise ProposalError("Карточка не может блокировать сама себя")
    mirror = store.row("SELECT blockers FROM kaiten_cards WHERE id=?", (card_id,))
    if mirror and blocker_card and any(b.get("blocker_card_id") == int(blocker_card)
                                       for b in store.loads(mirror["blockers"], []) or []):
        raise ProposalError("В Kaiten карточку уже блокирует эта карточка")
    payload = {"card_id": int(card_id), "blocker_card_id": int(blocker_card) if blocker_card else None,
               "text": (text or "").strip() or None}
    what = f"карточкой {_card_title(int(blocker_card))}" if blocker_card else f"причиной «{payload['text']}»"
    return _create("kaiten.block", f"Kaiten: {_card_title(int(card_id))} — заблокировать {what}", payload,
                   autonomy_key="kaiten_update", reason=reason, source_ref=source_ref, batch=batch)


def propose_calendar_event(*, title: str, start: str, end: str, attendees: list[str] | None = None,
                           description: str = "", calendar: str = "", batch: str | None = None,
                           reason: str | None = None, source_ref: str | None = None,
                           commitment_id: int | None = None) -> dict:
    """commitment_id — встреча по этой задаче: создав встречу, PULSE свяжет их."""
    s = store.parse_local(start)
    e = store.parse_local(end)
    if not s or not e or e <= s:
        raise ProposalError("Неверное время встречи: нужны start и end в формате 2026-10-27T15:00")
    start, end = s.isoformat(), e.isoformat()
    payload = {"title": title, "start": start, "end": end, "attendees": attendees or [], "description": description,
               "calendar": calendar}
    if commitment_id:
        if not store.row("SELECT 1 FROM commitments WHERE id=?", (int(commitment_id),)):
            raise ProposalError(f"Нет задачи [{commitment_id}] (./ops commitments list)")
        payload["commitment_id"] = int(commitment_id)
    label = f"Встреча «{title}» {store.local(start, '%d.%m %H:%M')}–{store.local(end, '%H:%M')}"
    if attendees:
        label += f" ({len(attendees)} участн.)"
    if calendar:
        label += f" в календаре «{calendar}»"
    return _create("calendar.create", label, payload, autonomy_key="calendar", reason=reason,
                   source_ref=source_ref, batch=batch)


def propose_calendar_cancel(*, uid: str, batch: str | None = None, reason: str | None = None,
                            source_ref: str | None = None) -> dict:
    if uid.startswith("google:"):
        raise ProposalError("Встреча из Google Календаря — PULSE его только читает, отменить её можно только в Google")
    if uid.startswith(("caldav:", "ics:")):
        raise ProposalError("Встреча из календаря, который PULSE только читает, — отменить её можно только в нём")
    ev = store.row("SELECT * FROM events WHERE uid=?", (uid,))
    if not ev:
        raise ProposalError(f"Нет встречи с uid {uid} (обновите календарь: ./ops collect calendar)")
    label = f"Отменить встречу «{ev['title']}» {store.local(ev['start'])}"
    return _create("calendar.cancel", label, {"uid": uid}, autonomy_key="calendar", reason=reason,
                   source_ref=source_ref, batch=batch)


def propose_task_done(*, commitment_id: int, quote: str | None = None, batch: str | None = None,
                      reason: str | None = None, source_ref: str | None = None, label: str | None = None) -> dict:
    """Задача PULSE, судя по встрече или переписке, выполнена — закрыть её после «да» директора.
    reason — почему так кажется (обязательно), source_ref — где это видно, quote — дословно оттуда,
    label — своя подпись («— агент сделал: …», opsdash.agents)."""
    c = store.row("SELECT * FROM commitments WHERE id=?", (int(commitment_id),))
    if not c:
        raise ProposalError(f"Нет задачи [{commitment_id}] (./ops commitments list)")
    if c["status"] != "open":
        raise ProposalError(f"Задача [{commitment_id}] уже закрыта")
    if not (reason or "").strip():
        raise ProposalError("Напишите в --reason, почему задача выглядит выполненной")
    dup = store.row("SELECT id FROM proposals WHERE kind='task.done' AND status='pending' "
                    "AND json_extract(payload, '$.commitment_id')=?", (c["id"],))
    if dup:
        return get(dup["id"])
    payload = {"commitment_id": c["id"], "quote": (quote or "").strip()}
    label = label or f"Закрыть задачу «{records.headline(c)}» ({records.route(c)}) — похоже, выполнена"
    return _create("task.done", label, payload, autonomy_key="task_done", reason=reason.strip(),
                   source_ref=source_ref, batch=batch)


def propose_task_meeting(*, commitment_id: int, event_uid: str, batch: str | None = None,
                         reason: str | None = None, source_ref: str | None = None) -> dict:
    """Встреча из календаря посвящена задаче PULSE — связать их после «да» директора (или сразу — автономия).
    Задача остаётся открытой до встречи; по её итогам Claude предложит закрыть."""
    c = store.row("SELECT * FROM commitments WHERE id=?", (int(commitment_id),))
    if not c:
        raise ProposalError(f"Нет задачи [{commitment_id}] (./ops commitments list)")
    if c["status"] != "open":
        raise ProposalError(f"Задача [{commitment_id}] уже закрыта")
    e = store.row("SELECT * FROM events WHERE uid=?", (event_uid,))
    if not e:
        raise ProposalError(f"Нет встречи с uid {event_uid} (./ops calendar)")
    if c["event_uid"] == e["uid"]:
        raise ProposalError("Задача уже связана с этой встречей")
    if store.row("SELECT id FROM proposals WHERE kind='task.meeting' AND status='rejected' "
                 "AND json_extract(payload, '$.commitment_id')=? AND json_extract(payload, '$.event_uid')=?",
                 (c["id"], e["uid"])):
        raise ProposalError("Директор уже ответила «Нет» на эту пару — не предлагай её снова")
    dup = store.row("SELECT id FROM proposals WHERE kind='task.meeting' AND status='pending' "
                    "AND json_extract(payload, '$.commitment_id')=?", (c["id"],))
    if dup:
        return get(dup["id"])
    label = f"Встреча «{e['title'] or 'Без названия'}» {store.local(e['start'])} — по задаче «{records.headline(c)}»"
    return _create("task.meeting", label, {"commitment_id": c["id"], "event_uid": e["uid"]},
                   autonomy_key="task_meeting", reason=reason, source_ref=source_ref, batch=batch)


def propose_task_wait(*, commitment_id: int, waits_for: int | None = None, event: str | None = None,
                      until: str | None = None, batch: str | None = None, reason: str | None = None,
                      source_ref: str | None = None) -> dict:
    """Задача, судя по встрече или переписке, ждёт другую задачу или событие — связать после «да» директора
    (или сразу — автономия). Прямо прозвучавшую связь Claude ставит сам: ./ops commitments wait."""
    c = store.row("SELECT * FROM commitments WHERE id=?", (int(commitment_id),))
    if not c:
        raise ProposalError(f"Нет задачи [{commitment_id}] (./ops commitments list)")
    if c["status"] != "open":
        raise ProposalError(f"Задача [{commitment_id}] уже закрыта")
    try:
        records.check_wait(c["id"], waits_for, event, until)
    except ValueError as e:
        raise ProposalError(str(e)) from e
    event = " ".join((event or "").split())
    if waits_for and records.find_waits(c["id"], waits_for=int(waits_for)):
        raise ProposalError("Задача уже ждёт эту задачу")
    payload = {"commitment_id": c["id"], "waits_for": int(waits_for) if waits_for else None,
               "event": None if waits_for else event, "until": None if waits_for else (until or None)}
    for p in store.rows("SELECT * FROM proposals WHERE kind='task.wait' AND status IN ('pending', 'rejected') "
                        "AND json_extract(payload, '$.commitment_id')=?", (c["id"],)):
        pl = store.loads(p["payload"], {})
        same = (pl.get("waits_for") == payload["waits_for"] if waits_for
                else (pl.get("event") or "").lower() == event.lower())
        if same and p["status"] == "rejected":
            raise ProposalError("Директор уже ответила «Нет» на эту связь — не предлагай её снова")
        if same:
            return get(p["id"])
    if waits_for:
        other = store.row("SELECT * FROM commitments WHERE id=?", (int(waits_for),))
        what = f"«{records.headline(other)}» ({records.route(other)})"
    else:
        what = f"событие: {event}" + (f" ({_fmt_date(until)})" if until else "")
    label = f"«{records.headline(c)}» ждёт {what}"
    return _create("task.wait", label, payload, autonomy_key="task_wait", reason=reason, source_ref=source_ref,
                   batch=batch)


def propose_task_merge(*, commitment_id: int, into: int, quote: str | None = None, batch: str | None = None,
                       reason: str | None = None, source_ref: str | None = None) -> dict:
    """Задача commitment_id — дубль задачи into (одно и то же записано дважды) — объединить после «да» директора:
    дубль закроется, его источник, связи и подзадачи перейдут в into."""
    try:
        c, keep = records.check_merge(int(commitment_id), int(into))
    except ValueError as e:
        raise ProposalError(str(e)) from e
    if not (reason or "").strip():
        raise ProposalError("Напишите в --reason, почему это одна и та же задача")
    for p in store.rows("SELECT * FROM proposals WHERE kind='task.merge' AND status IN ('pending', 'rejected') "
                        "AND json_extract(payload, '$.commitment_id') IN (?, ?)", (c["id"], keep["id"])):
        pl = store.loads(p["payload"], {})
        if {pl.get("commitment_id"), pl.get("into")} == {c["id"], keep["id"]}:
            if p["status"] == "rejected":
                raise ProposalError("Директор уже ответила «Нет»: это разные задачи — не предлагай снова")
            return get(p["id"])
    payload = {"commitment_id": c["id"], "into": keep["id"], "quote": (quote or "").strip()}
    label = (f"Объединить дубль: «{records.headline(c)}» ({records.route(c)}) → "
             f"«{records.headline(keep)}» ({records.route(keep)})")
    return _create("task.merge", label, payload, autonomy_key="task_merge", reason=reason.strip(),
                   source_ref=source_ref, batch=batch)


def propose_task_log(*, commitment_id: int, text: str, quote: str | None = None, subtask: str | None = None,
                     who: str | None = None, whom: str | None = None, due: str | None = None,
                     batch: str | None = None, reason: str | None = None, source_ref: str | None = None) -> dict:
    """Сообщение (или место встречи), похоже, продолжает открытую задачу, но связь не наверняка — по теме, а не по
    людям: директор обещала «Демо и макеты CRM» одному коллеге, а в треде с другим пишет «хочу отдать макет, как
    вижу CRM с очередью». После её «Да» text станет записью в ходе задачи; subtask — ещё и подзадача с этим заголовком
    (её новое «сделаю» по этой задаче; who — если подзадачу обещал коллега, whom — кому обещала она)."""
    from opsdash import worklog

    c = store.row("SELECT * FROM commitments WHERE id=?", (int(commitment_id),))
    if not c:
        raise ProposalError(f"Нет задачи [{commitment_id}] (./ops commitments list)")
    if c["status"] != "open":
        raise ProposalError(f"Задача [{commitment_id}] уже закрыта")
    text = re.sub(r"\s+", " ", text or "").strip()
    if not text:
        raise ProposalError("Напишите в --text, что произошло — это станет записью в ходе задачи")
    if not (reason or "").strip():
        raise ProposalError("Напишите в --reason, почему это про эту задачу")
    try:
        st, ref = worklog.parse_source(source_ref)
    except ValueError as e:
        raise ProposalError(str(e)) from e
    if not st or not ref:
        raise ProposalError("Укажите --source: сообщение (pachca:<id>, telegram:<id>…) или встречу (meeting:<id>)")
    if store.row("SELECT 1 FROM task_log WHERE task_id=? AND source_type=? AND source_ref=? AND kind!='event'",
                 (c["id"], st, ref)):
        raise ProposalError("Это уже есть в ходе задачи — ./ops commitments show " + str(c["id"]))
    if due and not records._as_date(due):
        raise ProposalError("Срок — дата в виде ГГГГ-ММ-ДД")
    subtask = " ".join((subtask or "").split()) or None
    payload = {"commitment_id": c["id"], "text": text, "quote": " ".join((quote or "").split()),
               "subtask": {"title": subtask, "who": (who or "").strip() or None, "whom": (whom or "").strip() or None,
                           "due": due or None} if subtask else None}
    source_ref = f"{st}:{ref}"
    head = f"«{records.headline(c)}» ({records.route(c)})"
    # заголовок на Сводке — короткий; вся запись — в «почему так думаю»
    label = (f"{head}: подзадача «{subtask}» и запись в ход" if subtask
             else f"{head}: в ход — {text if len(text) <= 80 else text[:text.rfind(' ', 0, 80)].rstrip(' ,.;—') + '…'}")
    for p in store.rows("SELECT * FROM proposals WHERE kind='task.log' AND status IN ('pending', 'rejected') "
                        "AND source_ref=? AND json_extract(payload, '$.commitment_id')=?", (source_ref, c["id"])):
        if p["status"] == "rejected":
            raise ProposalError("Директор уже ответила «Нет»: это не про эту задачу — по этому сообщению не предлагай")
        store.execute("UPDATE proposals SET payload=?, reason=?, title=? WHERE id=?",
                      (store.json_col(payload), reason.strip(), label, p["id"]))
        return get(p["id"])
    return _create("task.log", label, payload, autonomy_key="task_log", reason=reason.strip(), source_ref=source_ref,
                   batch=batch)


def propose_project(*, title: str, tasks: list[int], goal: str | None = None, owner: str | None = None,
                    due: str | None = None, batch: str | None = None, reason: str | None = None,
                    source_ref: str | None = None) -> dict:
    """Собрать задачи в новый проект после «да» директора: название — результат («Запуск склада
    в Казани»), goal — что считаем готовым, tasks — задачи проекта (подзадачи пойдут следом)."""
    title = " ".join((title or "").split())
    if not title:
        raise ProposalError("Назовите проект (--title)")
    if store.row("SELECT 1 FROM projects WHERE lower(title)=lower(?) AND status!='done'", (title,)):
        raise ProposalError(f"Проект «{title}» уже есть — добавь задачи в него: ./ops commitments set <id> --project <id>")
    if due and not records._as_date(due):
        raise ProposalError("Срок — дата в виде ГГГГ-ММ-ДД")
    ids = list(dict.fromkeys(int(t) for t in tasks))
    rows = store.rows(f"SELECT * FROM commitments WHERE id IN ({','.join('?' * len(ids)) or 'NULL'})", ids)
    missing = set(ids) - {r["id"] for r in rows}
    if missing:
        raise ProposalError(f"Нет задач: {', '.join(map(str, sorted(missing)))} (./ops commitments list)")
    if len(rows) < 2:
        raise ProposalError("Проект — это хотя бы две задачи с общим результатом")
    if not (reason or "").strip():
        raise ProposalError("Напишите в --reason, что объединяет эти задачи")
    rejected = store.row("SELECT 1 FROM proposals WHERE kind='project.create' AND status='rejected' "
                         "AND lower(json_extract(payload, '$.title'))=lower(?)", (title,))
    if rejected:
        raise ProposalError("Директор уже ответила «Нет» на такой проект — не предлагай его снова")
    payload = {"title": title, "goal": (goal or "").strip() or None, "owner": (owner or "").strip() or None,
               "due": due or None, "tasks": ids}
    return _create("project.create", f"Проект «{title}»: {len(ids)} {_tasks_word(len(ids))}", payload,
                   autonomy_key="project_create", reason=reason.strip(), source_ref=source_ref, batch=batch)


def _tasks_word(n: int) -> str:
    return "задача" if n % 10 == 1 and n % 100 != 11 else (
        "задачи" if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14 else "задач")


def propose_answered(*, attention_id: int, quote: str | None = None, batch: str | None = None,
                     reason: str | None = None, source_ref: str | None = None) -> dict:
    """«Нужен ответ»: судя по её сообщению, директор уже ответила — закрыть пункт после её «да».
    source_ref — её сообщение с ответом (telegram:<id>), quote — дословно оттуда, reason — почему это ответ."""
    a = store.row("SELECT * FROM attention WHERE id=?", (int(attention_id),))
    if not a:
        raise ProposalError(f"Нет пункта [{attention_id}] (./ops attention list)")
    if a["status"] != "open":
        raise ProposalError(f"Пункт [{attention_id}] уже закрыт")
    if not (reason or "").strip():
        raise ProposalError("Напишите в --reason, почему это ответ на вопрос")
    ref = (source_ref or "").partition(":")[2]
    msg = store.row("SELECT ts FROM messages WHERE id=?", (int(ref),)) if ref.isdigit() else None
    if a["reopened_at"] and msg and msg["ts"] <= a["reopened_at"]:
        raise ProposalError("Директор вернула этот пункт уже после этого сообщения — значит, это не ответ")
    if source_ref and store.row(
            "SELECT id FROM proposals WHERE kind='attention.done' AND status='rejected' AND source_ref=? "
            "AND json_extract(payload, '$.attention_id')=?", (source_ref, a["id"])):
        raise ProposalError("Директор уже ответила «Нет» на это предложение — по этому сообщению не предлагай")
    dup = store.row("SELECT id FROM proposals WHERE kind='attention.done' AND status='pending' "
                    "AND json_extract(payload, '$.attention_id')=?", (a["id"],))
    if dup:
        return get(dup["id"])
    who = a["sender_name"] or a["chat_title"] or "?"
    label = f"Закрыть «нужен ответ»: {who} — {a['summary'][:120]} — похоже, вы ответили"
    return _create("attention.done", label, {"attention_id": a["id"], "quote": (quote or "").strip()},
                   autonomy_key="attention_done", reason=reason.strip(), source_ref=source_ref, batch=batch)


def propose_reminder(*, commitment_id: int, text: str | None = None, batch: str | None = None,
                     reason: str | None = None, source_ref: str | None = None, manual: bool = False) -> dict:
    """Напомнить исполнителю задачи «мне должны» в Telegram — сообщение уйдёт от имени директора, поэтому
    только после её «Да» (автономии для этого нет). text — свой текст, иначе по шаблону из настроек.
    Директор попросила не напоминать («Не напоминать 3 дня», «Больше не напоминать») — Claude не предлагает;
    manual — директор сама нажала «Отправить»: её решение важнее."""
    c = store.row("SELECT * FROM commitments WHERE id=?", (int(commitment_id),))
    if not c:
        raise ProposalError(f"Нет задачи [{commitment_id}] (./ops commitments list)")
    if c["direction"] != "to_me" or c["status"] != "open":
        raise ProposalError("Напоминать можно только по открытым задачам «мне должны»")
    if not manual and (off := records.remind_off(c)):
        raise ProposalError(f"Директор попросила по этой задаче: {off} — напоминание не предлагай")
    to = records.recipient(c)
    if not to:
        raise ProposalError(f"Не нашла чат Telegram с «{c['who']}» — укажите его @username в «Людях»")
    text = (text or "").strip() or records.reminder_text(c, name=to["label"])
    payload = {"commitment_id": c["id"], "target": to["target"], "to": to["label"], "text": text}
    dup = store.row("SELECT id FROM proposals WHERE kind='telegram.remind' AND status='pending' "
                    "AND json_extract(payload, '$.commitment_id')=?", (c["id"],))
    if dup:
        store.execute("UPDATE proposals SET payload=? WHERE id=?", (store.json_col(payload), dup["id"]))
        return get(dup["id"])
    label = f"Напомнить в Telegram → {to['label']}: «{records.headline(c)}»"
    return _create("telegram.remind", label, payload, autonomy_key="telegram_send", reason=reason,
                   source_ref=source_ref, batch=batch)


def propose_rule(*, skill: str, text: str, quote: str | None = None, batch: str | None = None,
                 reason: str | None = None, source_ref: str | None = None) -> dict:
    """Правило для навыка из уроков «Нет» (MOD-3, навык lessons): включится в «Ваши правила» после «Да».
    Такое же правило уже есть — не предлагаем; такое же уже отклоняли — тоже (решение директора главнее модели)."""
    from opsdash import skills

    sk = skills.get(skill)
    text = " ".join((text or "").split())
    if not sk:
        raise ProposalError(f"Нет навыка «{skill}» — список: ./ops skills list")
    if not text:
        raise ProposalError("Пустое правило")
    if store.row("SELECT 1 FROM skill_rules WHERE skill=? AND lower(text)=lower(?) AND enabled=1", (skill, text)):
        raise ProposalError("Такое правило уже действует — ./ops skills rules " + skill)
    for p in store.rows("SELECT payload FROM proposals WHERE kind='skill.rule' AND status='rejected'"):
        pl = store.loads(p["payload"], {})
        if pl.get("skill") == skill and (pl.get("text") or "").lower() == text.lower():
            raise ProposalError("Такое правило директор уже отклоняла — снова не предлагаем")
    return _create("skill.rule", f"Правило для «{sk['title']}»: {text}", {"skill": skill, "text": text, "quote": quote},
                   autonomy_key="skill_rule", reason=reason, source_ref=source_ref, batch=batch)


def propose_mcp_call(*, server_id: str, tool: str, args: dict | None = None, title: str = "", batch: str | None = None,
                     reason: str | None = None, source_ref: str | None = None) -> dict:
    """Изменение в подключённом через MCP сервисе (MORE-1): создать задачу в Linear, страницу в Notion… Помощник
    сам такие инструменты не вызывает — PULSE выполнит после «Да». Инструменты чтения так не предлагаются."""
    from opsdash import mcp

    s = mcp.server(server_id)
    if not s:
        raise ProposalError(f"Нет подключения «{server_id}» — ./ops mcp list")
    if not s.get("enabled") or mcp.status(server_id)["kind"] != "ok":
        raise ProposalError(f"Подключение «{s.get('title') or server_id}» выключено или не настроено")
    if not s.get("writes", True):
        raise ProposalError(f"Изменения в «{s.get('title') or server_id}» директор не разрешила — только чтение")
    known = {t["name"]: t for t in mcp.tools(server_id)}
    if tool not in known:
        raise ProposalError(f"У «{s.get('title') or server_id}» нет инструмента {tool} — ./ops mcp tools {server_id}")
    if known[tool]["read"]:
        raise ProposalError(f"{tool} только читает — вызывай его сам, предлагать не нужно")
    if not isinstance(args, dict):
        raise ProposalError("Параметры инструмента — JSON-объект: --args '{\"title\": \"…\"}'")
    title = " ".join((title or "").split()) or f"{known[tool].get('title') or tool} в {s.get('title') or server_id}"
    return _create("mcp.call", title, {"server": server_id, "server_title": s.get("title") or server_id, "tool": tool,
                                       "args": args}, autonomy_key="mcp_call", reason=reason, source_ref=source_ref,
                   batch=batch)


# --- решение ----------------------------------------------------------------

def reject(pid: int) -> dict:
    store.execute(
        "UPDATE proposals SET status='rejected', decided_at=? WHERE id=? AND status='pending'", (store.utcnow(), pid)
    )
    return get(pid)


def approve(pid: int) -> dict:
    p = get(pid)
    if not p:
        raise ProposalError(f"Нет предложения #{pid}")
    if p["status"] != "pending":
        return p
    # помечаем заранее, чтобы двойное нажатие не создало две карточки
    claimed = store.execute(
        "UPDATE proposals SET status='running', decided_at=? WHERE id=? AND status='pending'", (store.utcnow(), pid)
    )
    if not claimed:
        return get(pid)
    try:
        result = execute(p)
        store.execute("UPDATE proposals SET status='done', result=? WHERE id=?", (store.json_col(result), pid))
        store.log("proposals", f"#{pid} выполнено: {p['title']}")
    except Exception as e:  # noqa: BLE001 — ошибку показываем директору как есть
        store.execute("UPDATE proposals SET status='failed', result=? WHERE id=?", (store.json_col({"error": str(e)}), pid))
        store.log("proposals", f"#{pid} ошибка: {e}", "error")
    return get(pid)


def approve_batch(batch: str) -> list[dict]:
    return [approve(p["id"]) for p in reversed(list_proposals("pending", batch=batch))]


def execute(p: dict) -> dict:
    kind, pl = p["kind"], p["payload"]
    if kind == "mcp.call":
        from opsdash import mcp

        return {"text": mcp.call(pl["server"], pl["tool"], pl.get("args") or {})}
    if kind == "kaiten.create_card":
        kc = kaiten.client()
        desc = pl.get("description") or ""
        if pl.get("responsible_unresolved"):
            desc += f"\n\nИсполнитель по разбору: {pl['responsible_unresolved']} (не найден в Kaiten)"
        card = kc.create_card(
            board_id=pl["board_id"], column_id=pl.get("column_id"), lane_id=pl.get("lane_id"), title=pl["title"],
            description=desc.strip(), due_date=pl.get("due"),
        )
        url = kc.card_url(card, pl.get("space_id"))
        warning = None
        if pl.get("responsible_id"):
            # карточка уже создана: ошибка назначения не должна выглядеть как «не создано» (иначе — дубль)
            try:
                kc.set_responsible(card["id"], pl["responsible_id"])
            except Exception as e:  # noqa: BLE001
                warning = f"карточка создана, но исполнителя назначить не удалось: {e}"
                store.log("proposals", warning, "warning")
        try:
            fresh = kc.card(card["id"])
            space_title = next(
                (s.get("title") for s in config.load_settings()["kaiten"].get("spaces") or []
                 if int(s["id"]) == int(pl.get("space_id") or 0)), None,
            )
            kaiten.save_card(kaiten.normalize_card(fresh, kc, pl.get("space_id"), space_title))
        except Exception:  # noqa: BLE001 — зеркало обновится при следующей синхронизации
            pass
        if pl.get("commitment_id"):
            records.update_commitment(int(pl["commitment_id"]), kaiten_card_id=card["id"])
        return {"card_id": card["id"], "url": url, **({"warning": warning} if warning else {})}
    if kind == "kaiten.comment":
        kaiten.client().comment(pl["card_id"], pl["text"])
        return {"card_id": pl["card_id"]}
    if kind == "kaiten.block":
        kc = kaiten.client()
        kc.block(pl["card_id"], blocker_card_id=pl.get("blocker_card_id"), reason=pl.get("text"))
        try:
            kaiten.save_blockers(pl["card_id"], kc.blockers(pl["card_id"]))
        except Exception:  # noqa: BLE001 — зеркало обновится при следующей синхронизации
            pass
        return {"card_id": pl["card_id"]}
    if kind == "kaiten.update":
        kc = kaiten.client()
        if pl.get("fields"):
            kc.update_card(pl["card_id"], pl["fields"])
        if pl.get("responsible_id"):
            kc.set_responsible(pl["card_id"], pl["responsible_id"])
        return {"card_id": pl["card_id"]}
    if kind == "calendar.create":
        from opsdash.sources import calendar

        uid = calendar.create_event(pl["title"], pl["start"], pl["end"], pl.get("attendees") or [], pl.get("description") or "",
                                    calendar_name=pl.get("calendar") or "")
        if pl.get("commitment_id"):
            from opsdash import task_meetings

            task_meetings.link(pl["commitment_id"], uid, title=pl["title"], start=pl["start"], end=pl["end"])
        return {"uid": uid}
    if kind == "calendar.cancel":
        from opsdash.sources import calendar

        calendar.cancel_event(pl["uid"])
        return {"uid": pl["uid"]}
    if kind == "telegram.remind":
        from opsdash.sources import telegram

        telegram.send(pl["target"], pl["text"])
        records.mark_reminded(pl["commitment_id"])
        from opsdash import worklog

        worklog.event(pl["commitment_id"], f"Напомнили{' ' + pl['to'] if pl.get('to') else ''} в Telegram: «{pl['text']}»")
        return {"commitment_id": pl["commitment_id"], "to": pl.get("to")}
    if kind == "attention.done":
        a = store.row("SELECT status FROM attention WHERE id=?", (pl["attention_id"],))
        if not a:
            raise ProposalError("Пункта уже нет")
        if a["status"] == "open":
            records.close_attention(pl["attention_id"], "done")
        return {"attention_id": pl["attention_id"]}
    if kind == "task.meeting":
        from opsdash import task_meetings

        if not store.row("SELECT 1 FROM commitments WHERE id=?", (pl["commitment_id"],)):
            raise ProposalError("Задачи уже нет")
        if not store.row("SELECT 1 FROM events WHERE uid=?", (pl["event_uid"],)):
            raise ProposalError("Встречи уже нет в календаре")
        task_meetings.link(pl["commitment_id"], pl["event_uid"])
        return {"commitment_id": pl["commitment_id"], "event_uid": pl["event_uid"]}
    if kind == "project.create":
        from opsdash import projects

        pid = projects.add(title=pl["title"], goal=pl.get("goal"), owner=pl.get("owner"), due=pl.get("due"), by="claude")
        found = projects.assign(pid, pl.get("tasks") or [], by="director")  # директор сказала «Да»
        return {"project_id": pid, "tasks": found}
    if kind == "task.wait":
        if not store.row("SELECT 1 FROM commitments WHERE id=?", (pl["commitment_id"],)):
            raise ProposalError("Задачи уже нет")
        link = records.add_wait(pl["commitment_id"], waits_for=pl.get("waits_for"), event=pl.get("event"),
                                until=pl.get("until"), note=p.get("reason"), source_ref=p.get("source_ref"), by="claude")
        return {"commitment_id": pl["commitment_id"], "link_id": link}
    if kind == "task.merge":
        records.merge_commitments(pl["commitment_id"], pl["into"], note=p.get("reason"))
        return {"commitment_id": pl["commitment_id"], "into": pl["into"]}
    if kind == "task.log":
        from opsdash import worklog

        if not store.row("SELECT 1 FROM commitments WHERE id=?", (pl["commitment_id"],)):
            raise ProposalError("Задачи уже нет")
        st, ref = worklog.parse_source(p.get("source_ref"))
        res = {"commitment_id": pl["commitment_id"]}
        sub = pl.get("subtask")
        if sub:  # сначала подзадача: запись в ходе основной задачи — о том, что появилось
            res["subtask_id"] = records.add_commitment(
                direction="to_me" if sub.get("who") else "mine", title=sub["title"], what=pl["text"],
                who=sub.get("who"), whom=sub.get("whom"), due=sub.get("due"), source_type=st or "manual",
                source_ref=ref, quote=pl.get("quote") or None, parent_id=pl["commitment_id"])
        res["entry_id"], _ = worklog.add(pl["commitment_id"], pl["text"], source=p.get("source_ref"),
                                         quote=pl.get("quote") or None, by="claude")
        return res
    if kind == "skill.rule":
        from opsdash import skills

        return {"rule_id": skills.add_rule(pl["skill"], pl["text"], source="lesson", proposal_id=p["id"])}
    if kind == "task.done":
        c = store.row("SELECT status FROM commitments WHERE id=?", (pl["commitment_id"],))
        if not c:
            raise ProposalError("Задачи уже нет")
        if c["status"] == "open":
            note = f"Закрыта по подтверждению директора: {p.get('reason') or ''}".strip()
            if p.get("source_ref"):
                note += f" ({_source_text(p['source_ref'])})"
            records.update_commitment(pl["commitment_id"], status="done", note=note)
        return {"commitment_id": pl["commitment_id"]}
    raise ProposalError(f"Неизвестный тип предложения: {kind}")


def _source_text(source_ref: str | None) -> str:
    """Откуда известно, по-человечески: «встреча «Обзор продаж» 27.10», «Telegram, Операционка, 27.10»."""
    kind, _, ref = (source_ref or "").partition(":")
    if kind == "agent":  # сессия Claude Code, которой поручили задачу (opsdash.agents)
        s = store.row("SELECT title FROM agent_sessions WHERE id=?", (ref,))
        return f"сессия агента «{s['title']}»" if s else "сессия агента"
    if kind == "meeting":
        m = store.row("SELECT title, started_at FROM meetings WHERE id=?", (ref,))
        return f"встреча «{m['title'] or ref}» {store.local(m['started_at'], '%d.%m')}" if m else f"встреча {ref}"
    if kind in ("telegram", "ymessenger", "pachca", "slack", "mail") and ref.isdigit():
        msg = store.row("SELECT chat_title, topic, sender_name, ts FROM messages WHERE id=?", (int(ref),))
        from opsdash import overview

        name = overview.SOURCE_TITLES.get(kind, kind)
        if msg:
            chat = " · ".join(filter(None, (msg["chat_title"], msg["topic"]))) or msg["sender_name"] or ""
            return f"{name}, {chat}, {store.local(msg['ts'], '%d.%m')}"
        return name
    return source_ref or ""


def links(p: dict) -> list[tuple[str, str]]:
    """Ссылки у предложения на сводке: задача PULSE, откуда известно (встреча, сообщение), карточка Kaiten."""
    from opsdash import links as orig

    out, pl = [], p.get("payload") or {}
    cid = pl.get("commitment_id")
    if cid:
        out.append(("Задача", f"/commitments?task={cid}"))
    if p["kind"] == "project.create" and isinstance(p.get("result"), dict) and p["result"].get("project_id"):
        out.append(("Проект", f"/projects/{p['result']['project_id']}"))
    other = pl.get("into") or pl.get("waits_for")
    if other:
        out.append(("Дубль чего" if pl.get("into") else "Чего ждёт", f"/commitments?task={other}"))
    if p["kind"] == "task.log" and isinstance(p.get("result"), dict) and p["result"].get("subtask_id"):
        out.append(("Подзадача", f"/commitments?task={p['result']['subtask_id']}"))
    if pl.get("attention_id"):
        out.append(("Нужен ответ", "/"))
    if p["kind"] == "task.meeting":
        start = store.scalar("SELECT start FROM events WHERE uid=?", (pl.get("event_uid"),))
        if start:
            out.append(("Встреча в календаре", f"/calendar?start={store.local(start, '%Y-%m-%d')}"))
    kind, _, ref = (p.get("source_ref") or "").partition(":")
    if kind == "agent" and ref:
        desktop = store.scalar("SELECT desktop_id FROM agent_sessions WHERE id=?", (ref,))
        if desktop:
            out.append(("Сессия агента", f"claude://code/continue?session={desktop}"))
    if kind == "meeting" and ref:
        out.append(("Встреча", f"/meetings/{ref}?tab=transcript" if p["kind"] == "task.done" else f"/meetings/{ref}"))
    elif kind in ("telegram", "ymessenger", "pachca", "slack", "mail") and ref.isdigit():
        msg = store.row("SELECT source, chat_id, msg_id, sender_id FROM messages WHERE id=?", (int(ref),))
        found = orig.original(msg["source"], msg["chat_id"], msg["msg_id"], msg["sender_id"]) if msg else None
        if found:
            out.append(("Сообщение в Telegram" if kind == "telegram" else "Сообщение в Пачке" if kind == "pachca"
                        else "Сообщение в Slack" if kind == "slack"
                        else "Письмо" if kind == "mail" else "Сообщение",
                        found[0]))
    card_id = pl.get("card_id")
    if not card_id and cid:
        card_id = store.scalar("SELECT kaiten_card_id FROM commitments WHERE id=?", (cid,))
    if card_id:
        url = store.scalar("SELECT url FROM kaiten_cards WHERE id=?", (card_id,))
        if url:
            out.append((f"Kaiten #{card_id}", url))
    return out


def describe(p: dict) -> str:
    """Подробное описание для админки и бота."""
    pl = p["payload"]
    lines = [p["title"]]
    if p["kind"] == "telegram.remind":
        lines.append(f"Текст: «{pl.get('text', '')[:600]}»")
        if p.get("reason"):
            lines.append(f"Почему: {p['reason']}")
        lines.append(f"«{action(p)}» — сообщение уйдёт от вашего имени из вашего Telegram.")
        return "\n".join(lines)
    if p["kind"] == "mcp.call":
        lines.append(f"Сервис: {pl.get('server_title') or pl.get('server')} · инструмент {pl.get('tool')}")
        for k, v in list((pl.get("args") or {}).items())[:12]:
            text = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
            lines.append(f"• {k}: {text[:300]}")
        if p.get("reason"):
            lines.append(f"Почему: {p['reason']}")
        if p.get("source_ref"):
            lines.append(f"Источник: {_source_text(p['source_ref'])}")
        lines.append(f"«{action(p)}» — PULSE выполнит это в {pl.get('server_title') or pl.get('server')} от вашего имени.")
        return "\n".join(lines)
    if p["kind"] == "attention.done":
        if p.get("reason"):
            lines.append(f"Почему так думаю: {p['reason']}")
        if pl.get("quote"):
            lines.append(f"Ваш ответ: «{pl['quote'][:400]}»")
        if p.get("source_ref"):
            lines.append(f"Источник: {_source_text(p['source_ref'])}")
        lines.append(f"«{action(p)}» — пункт уйдёт из «Нужен ответ»; «Нет» — останется, пока не закроете сами.")
        return "\n".join(lines)
    if p["kind"] == "task.done":
        if p.get("reason"):
            lines.append(f"Почему так думаю: {p['reason']}")
        if pl.get("quote"):
            lines.append(f"«{pl['quote'][:400]}»")
        if p.get("source_ref"):
            lines.append(f"Источник: {_source_text(p['source_ref'])}")
        lines.append(f"«{action(p)}» — задача закроется в PULSE; карточку в Kaiten, если она есть, это не трогает.")
        return "\n".join(lines)
    if p["kind"] == "project.create":
        if pl.get("goal"):
            lines.append(f"Результат: {pl['goal']}")
        if p.get("reason"):
            lines.append(f"Почему вместе: {p['reason']}")
        rows = store.rows(f"SELECT * FROM commitments WHERE id IN ({','.join('?' * len(pl.get('tasks') or [])) or 'NULL'})",
                          pl.get("tasks") or [])
        for c in rows[:12]:
            lines.append(f"• {records.headline(c)} — {records.route(c)}" + (" (готово)" if c["status"] == "done" else ""))
        if len(rows) > 12:
            lines.append(f"• и ещё {len(rows) - 12}")
        lines.append(f"«{action(p)}» — проект появится в «Задачи → Проекты» с прогрессом и цепочкой задач.")
        return "\n".join(lines)
    if p["kind"] == "task.wait":
        if p.get("reason"):
            lines.append(f"Почему так думаю: {p['reason']}")
        if p.get("source_ref"):
            lines.append(f"Источник: {_source_text(p['source_ref'])}")
        lines.append(f"«{action(p)}» — на карточке появится «ждёт», а когда то, чего ждём, будет готово, PULSE скажет «Можно делать».")
        return "\n".join(lines)
    if p["kind"] == "task.merge":
        if p.get("reason"):
            lines.append(f"Почему так думаю: {p['reason']}")
        if pl.get("quote"):
            lines.append(f"«{pl['quote'][:400]}»")
        if p.get("source_ref"):
            lines.append(f"Источник: {_source_text(p['source_ref'])}")
        lines.append(f"«{action(p)}» — первая задача закроется как дубль; её источник, цитата, связи и подзадачи перейдут во вторую. "
                     "Вернуть дубль можно кнопкой «Вернуть» в его окне.")
        return "\n".join(lines)
    if p["kind"] == "task.log":
        if p.get("reason"):
            lines.append(f"Почему так думаю: {p['reason']}")
        lines.append(f"Запись в ход: {pl.get('text', '')[:400]}")
        if pl.get("quote"):
            lines.append(f"«{pl['quote'][:400]}»")
        if p.get("source_ref"):
            lines.append(f"Источник: {_source_text(p['source_ref'])}")
        sub = pl.get("subtask")
        if sub:
            whose = f"обещал(а) {sub['who']}" if sub.get("who") else f"ваша{', для ' + sub['whom'] if sub.get('whom') else ''}"
            lines.append(f"«{action(p)}» — у задачи появится подзадача «{sub['title']}» ({whose}"
                         f"{', до ' + _fmt_date(sub['due']) if sub.get('due') else ''}), а в её ходе — эта запись.")
        else:
            lines.append(f"«{action(p)}» — запись появится в ходе задачи и станет строкой «Сейчас» на карточке; "
                         "«Не про эту задачу» — по этому сообщению Claude больше не предложит.")
        return "\n".join(lines)
    if p["kind"] == "task.meeting":
        if p.get("reason"):
            lines.append(f"Почему так думаю: {p['reason']}")
        lines.append(f"«{action(p)}» — встреча появится на карточке задачи. Задача останется открытой до встречи, "
                     "а по её итогам Claude предложит закрыть.")
        return "\n".join(lines)
    if p["kind"] == "kaiten.create_card":
        lines.append(f"Куда: {pl.get('target')}")
        if pl.get("responsible_unresolved"):
            lines.append(f"⚠ Исполнитель «{pl['responsible_unresolved']}» не найден в Kaiten — карточка будет без исполнителя")
        if pl.get("description"):
            lines.append(pl["description"][:500])
    elif p["kind"] == "kaiten.comment":
        lines.append(pl.get("text", "")[:800])
    elif p["kind"] == "kaiten.block":
        if pl.get("blocker_card_id") and pl.get("text"):
            lines.append(f"Причина: {pl['text']}")
        lines.append(f"«{action(p)}» — в карточке Kaiten появится «Заблокировано» со ссылкой; снимают блокировку в самом Kaiten.")
    elif p["kind"] == "calendar.create":
        if pl.get("attendees"):
            lines.append("Участники: " + ", ".join(pl["attendees"]))
        c = store.row("SELECT * FROM commitments WHERE id=?", (pl["commitment_id"],)) if pl.get("commitment_id") else None
        if c:
            lines.append(f"По задаче «{records.headline(c)}» — встреча появится на её карточке")
    if p.get("reason"):
        lines.append(f"Почему: {p['reason']}")
    return "\n".join(lines)


def result_text(p: dict) -> str:
    if p["status"] == "done" and p["kind"] == "task.done":
        return "✅ Задача закрыта"
    if p["status"] == "done" and p["kind"] == "attention.done":
        return "✅ Убрано из «Нужен ответ»"
    if p["status"] == "done" and p["kind"] == "project.create":
        return "✅ Проект создан — «Задачи → Проекты»"
    if p["status"] == "done" and p["kind"] == "kaiten.block":
        return "✅ Карточка заблокирована в Kaiten"
    if p["status"] == "done" and p["kind"] == "task.wait":
        return "✅ Связь «ждёт» — на карточке задачи"
    if p["status"] == "done" and p["kind"] == "task.merge":
        return "✅ Дубль объединён с задачей"
    if p["status"] == "done" and p["kind"] == "task.meeting":
        return "✅ Встреча — на карточке задачи"
    if p["status"] == "done" and p["kind"] == "task.log":
        sub = isinstance(p.get("result"), dict) and p["result"].get("subtask_id")
        return "✅ Подзадача добавлена, запись — в ходе задачи" if sub else "✅ Записано в ход задачи"
    if p["status"] == "done" and p["kind"] == "skill.rule":
        return "✅ Правило включено — Навыки → " + ((p.get("payload") or {}).get("skill") or "")
    if p["status"] == "done" and p["kind"] == "mcp.call":
        text = ((p.get("result") or {}) if isinstance(p.get("result"), dict) else {}).get("text") or ""
        return "✅ Выполнено" + (f": {text[:300]}" if text else "")
    if p["status"] == "done" and p["kind"] == "telegram.remind":
        return f"✅ Напоминание отправлено: {(p.get('payload') or {}).get('to') or ''}".rstrip(": ")
    if p["status"] == "done":
        res = p.get("result") or {}
        if isinstance(res, dict) and res.get("warning"):
            return f"✅ Готово с оговоркой: {res['warning']} — {res.get('url', '')}"
        if isinstance(res, dict) and res.get("url"):
            return f"✅ Готово: {res['url']}"
        return "✅ Готово"
    if p["status"] == "failed":
        res = p.get("result") or {}
        err = res.get("error") if isinstance(res, dict) else res
        return f"❌ Не получилось: {err}"
    if p["status"] == "rejected":
        return "✖ Отклонено"
    return "⏳ Ждёт подтверждения"


def dumps(p: dict) -> str:
    return json.dumps(p, ensure_ascii=False, indent=2)
