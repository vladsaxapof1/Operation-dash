"""Задачи, к которым может относиться переписка в чате, — для разбора новых сообщений (./ops messages pending).

Весь список открытых задач давно не помещается в одну выдачу: Claude Code показывает фоновому Claude начало
(около 2 КБ), а остальное тот ищет наугад — и задачу, которую переписка показывает выполненной, не находит.
Поэтому под каждым чатом PULSE сам выкладывает задачи, к которым его сообщения вероятнее всего относятся:
- по задаче уже был ход из этого чата (или задача из него появилась);
- задачи собеседника личного чата («Ольга LIFE» — Ольга Петрова из справочника, у которой псевдоним «Ольга»);
- задачи автора нового сообщения и упомянутых в нём людей («@Ольга Петрова», «@olga_p»).
Задачи директора сами по себе в счёт не идут — у неё все задачи: важен второй участник (кому она обещала,
кто обещал ей)."""

from __future__ import annotations

import re

from opsdash import config, records, store

PER_CHAT = 6  # задач под одним чатом: дальше — подсказка, как найти остальные
_MENTION = re.compile(r"(?<![\w@])@([A-Za-zА-Яа-яЁё][\w.]*(?:[  ][A-ZА-ЯЁ][a-zа-яё]+)?)")
_ORDER = {"chat": 0, "peer": 1, "mention": 2, "sender": 3}


def _counterparts(c: dict) -> list[str]:
    """Второй участник задачи: кому обещала директор, кто обещал ей; у договорённости коллег — оба."""
    if c["direction"] == "mine":
        return [c.get("whom") or ""]
    if c["direction"] == "to_me":
        return [c.get("who") or ""]
    return [c.get("who") or "", c.get("whom") or ""]


def _people_for(name: str, people: list[dict], username: str | None = None) -> list[dict]:
    """Кто из справочника скрывается за именем в чате: @имя Telegram или все слова имени/псевдонима есть в названии
    («Ольга LIFE» → псевдоним «Ольга»). Совпало больше слов — тот и есть; одинаково у троих и больше — не угадываем."""
    user = (username or "").lower().lstrip("@")
    if user:
        for p in people:
            if (p.get("telegram") or "").lower().lstrip("@") == user:
                return [p]
    words = records._words(name)
    best, score = [], 0
    for p in people:
        s = max((len(w) for n in [p["name"], *(p["aliases"] or [])] if (w := records._words(n)) and w <= words),
                default=0)
        if s > score:
            best, score = [p], s
        elif s and s == score:
            best.append(p)
    return best if len(best) <= 2 else []


def _is_owner(name: str, owner: set[str]) -> bool:
    return bool(owner) and owner <= records._words(name)


def _history(source: str, chat_id: str) -> dict[int, str]:
    """Задачи, по которым уже был ход из этого чата или которые из него появились: id → время последнего такого
    сообщения."""
    found: dict[int, str] = {}
    rows = store.rows(
        "SELECT t.task_id AS id, MAX(m.ts) AS ts FROM task_log t JOIN messages m ON m.id = CAST(t.source_ref AS INTEGER) "
        "AND m.source = t.source_type WHERE t.source_type = ? AND m.chat_id = ? GROUP BY t.task_id", (source, chat_id))
    rows += store.rows(
        "SELECT c.id, m.ts FROM commitments c JOIN messages m ON m.id = CAST(c.source_ref AS INTEGER) "
        "AND m.source = c.source_type WHERE c.source_type = ? AND m.chat_id = ?", (source, chat_id))
    for r in rows:
        found[r["id"]] = max(found.get(r["id"], ""), r["ts"] or "")
    return found


def related(source: str, chat_id: str, title: str | None, kind: str | None, msgs: list[dict], *,
            tasks: list[dict], people: list[dict], owner: str = "", username: str | None = None) -> list[dict]:
    """Открытые задачи для чата — [{"task", "why"}], сильные связи первыми (ход из чата, собеседник, упомянутый,
    автор сообщения), внутри — в порядке tasks (обычно по приоритету)."""
    me = records._words(owner)
    names: list[tuple[str, str, str | None]] = []  # (имя в чате, откуда, @имя Telegram)
    if kind == "private" and title:
        names.append((title, "peer", username))
    for m in msgs:
        sender = (m.get("sender_name") or "").strip()
        if sender and not m.get("is_outgoing") and sender != "Я":
            names.append((sender, "peer" if kind == "private" else "sender", None))
        for mention in _MENTION.findall(m.get("text") or ""):
            mention = mention.replace(" ", " ").rstrip(".")  # «… @oleg_s.» — точка конца фразы
            names.append((mention, "mention", mention if " " not in mention else None))
    names = [(n, how, u) for n, how, u in dict.fromkeys(names) if not _is_owner(n, me)]
    persons = [(n, how, _people_for(n, people, u)) for n, how, u in names]

    history = {} if kind == "mail" else _history(source, chat_id)  # почта — весь ящик одним «чатом»
    by_task: dict[int, tuple[int, str]] = {}  # id → (сила связи, почему)
    for c in tasks:
        if c["id"] in history:
            by_task[c["id"]] = (_ORDER["chat"], "по ней уже был ход из этого чата")
            continue
        for t in filter(str.strip, _counterparts(c)):
            if _is_owner(t, me):
                continue
            tp = records._person_by_name(t, people)
            tw = records._words(t)
            for n, how, ps in persons:
                if (tp and any(p["id"] == tp["id"] for p in ps)) or (tw and tw <= records._words(n)):
                    label = ps[0]["name"] if len(ps) == 1 else n
                    why = {"peer": "собеседник", "mention": f"упомянут(а) {label}", "sender": f"пишет {label}"}[how]
                    if c["id"] not in by_task or _ORDER[how] < by_task[c["id"]][0]:
                        by_task[c["id"]] = (_ORDER[how], why)
    by_id = {c["id"]: c for c in tasks}
    picked = [c["id"] for c in tasks if c["id"] in by_task]  # порядок tasks — по приоритету
    picked.sort(key=lambda i: history.get(i) or "", reverse=True)  # свежий ход из этого чата — выше
    picked.sort(key=lambda i: by_task[i][0])  # сильная связь — выше (сортировка устойчивая)
    return [{"task": by_id[i], "why": by_task[i][1]} for i in picked]


def for_pending(chats: dict[tuple, list[dict]]) -> dict[tuple, list[dict]]:
    """Задачи под каждым чатом выдачи «новые сообщения»: ключ — (source, chat_id, title)."""
    tasks = records.list_commitments(status="open", sort="priority")
    if not tasks:
        return {}
    people = records.list_people()
    owner = (config.load_settings().get("owner") or {}).get("name") or ""
    out = {}
    for (source, chat_id, title), msgs in chats.items():
        ch = store.row("SELECT kind, username FROM chats WHERE source=? AND chat_id=?", (source, chat_id)) or {}
        out[(source, chat_id, title)] = related(source, chat_id, title, ch.get("kind"), msgs, tasks=tasks,
                                                people=people, owner=owner, username=ch.get("username"))
    return out
