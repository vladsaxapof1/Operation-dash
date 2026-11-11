"""Задачи, к которым может относиться переписка в чате, — для разбора новых сообщений (./ops messages pending).

Весь список открытых задач давно не помещается в одну выдачу: Claude Code показывает фоновому Claude начало
(около 2 КБ), а остальное тот ищет наугад — и задачу, которую переписка показывает выполненной, не находит.
Поэтому под каждым чатом PULSE сам выкладывает задачи, к которым его сообщения вероятнее всего относятся:
- по задаче уже был ход из этого чата (или задача из него появилась);
- задачи собеседника личного чата («Ольга LIFE» — Ольга Петрова из справочника, у которой псевдоним «Ольга»);
- задачи автора нового сообщения и упомянутых в нём людей («@Ольга Петрова», «@olga_p»).
Задачи директора сами по себе в счёт не идут — у неё все задачи: важен второй участник (кому она обещала,
кто обещал ей). Последними — до трёх задач по теме: два слова заголовка задачи в одном сообщении. Такая связь
не наверняка, поэтому ход по ней фоновый Claude не пишет сам, а предлагает директору (propose log).

То же в обратную сторону — для разбора встречи (./ops meetings messages <id>): переписка с начала встречи в чатах,
связанных с её задачами, — то, что triage разобрал, пока этих задач ещё не было (for_meeting)."""

from __future__ import annotations

import json
import re

from opsdash import config, records, store

PER_CHAT = 6  # задач под одним чатом: дальше — подсказка, как найти остальные
_MENTION = re.compile(r"(?<![\w@])@([A-Za-zА-Яа-яЁё][\w.]*(?:[  ][A-ZА-ЯЁ][a-zа-яё]+)?)")
_ORDER = {"chat": 0, "peer": 1, "mention": 2, "sender": 3, "topic": 4}
TOPIC_PER_CHAT = 3  # задач «по теме» под одним чатом: связь слабая, больше — шум
TOPIC_HINT = "связь не наверняка: относится — propose log"


def _topic(c: dict, msgs: list[dict]) -> tuple[int, str] | None:
    """Задача по теме сообщения, а не по людям: два слова её заголовка — в одном сообщении («Демо и макеты CRM» и
    «хочу отдать макет, как вижу CRM с очередью» — в треде с другим коллегой, 11.11.2026). (сколько слов совпало,
    какие — словами заголовка) или None. Задачи без заголовка не сравниваем: суть длинная, совпадёт что угодно."""
    title = c.get("title") or ""
    stems = _topic_stems(title)
    best: set[str] = set()
    for m in msgs:
        common = stems & _topic_stems(m.get("text"))
        if len(common) > len(best):
            best = common
    if len(best) < 2:
        return None
    words = [w for w in _WORD.findall(title) if w.lower().replace("ё", "е")[:5] in best]
    return len(best), ", ".join(dict.fromkeys(words))


_WORD = re.compile(r"[A-Za-z0-9]+|[А-Яа-яЁё0-9]+")  # «CRMку», «API-шка» — латиница и кириллица отдельно
# слова, которые есть в любой переписке: по ним тему не узнать (основы — первые 5 букв)
_COMMON = {"еще", "один", "одна", "одно", "сегод", "завтр", "вчера", "сейча", "потом", "тоже", "тольк", "очень", "можно", "нужен", "нужна",
           "давай", "прост", "когда", "котор", "следу", "недел", "месяц", "январ", "февра", "марта", "апрел", "мая",
           "июня", "июля", "авгус", "сентя", "октяб", "ноябр", "декаб"}


def _topic_stems(text: str | None) -> set[str]:
    """Как records._stems, но слово из латиницы с русским окончанием («CRMку») — это «crm»; без общих слов."""
    words = _WORD.findall((text or "").lower().replace("ё", "е"))
    return {w[:5] for w in words if len(w) >= 3 and w not in records._STOP} - _COMMON


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
            tasks: list[dict], people: list[dict], owner: str = "", username: str | None = None,
            topics: bool = False) -> list[dict]:
    """Открытые задачи для чата — [{"task", "why"}], сильные связи первыми (ход из чата, собеседник, упомянутый,
    автор сообщения), внутри — в порядке tasks (обычно по приоритету). topics — ещё и задачи по теме сообщений
    (слова заголовка), не больше TOPIC_PER_CHAT, последними: задача директора «Демо и макеты CRM» обещана одному
    коллеге, а продолжение пишут в треде с другим — по людям такую связь не найти."""
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
    topic: dict[int, tuple[int, str]] = {}  # id → (сколько слов заголовка в сообщении, какие)
    if topics:
        topic = {c["id"]: t for c in tasks if (t := _topic(c, msgs))}
        for cid, (n, words) in topic.items():
            if cid in by_task:  # связь по людям есть, тема её подтверждает — такая задача выше остальных той же силы
                by_task[cid] = (by_task[cid][0], f"{by_task[cid][1]}, и по теме ({words})")
        found = sorted((cid for cid in topic if cid not in by_task), key=lambda i: -topic[i][0])
        for cid in found[:TOPIC_PER_CHAT]:  # больше общих слов — выше; при равенстве — порядок tasks
            by_task[cid] = (_ORDER["topic"], f"по теме ({topic[cid][1]}) — {TOPIC_HINT}")
    by_id = {c["id"]: c for c in tasks}
    picked = [c["id"] for c in tasks if c["id"] in by_task]  # порядок tasks — по приоритету
    picked.sort(key=lambda i: history.get(i) or "", reverse=True)  # свежий ход из этого чата — выше
    picked.sort(key=lambda i: -topic.get(i, (0, ""))[0])  # больше слов заголовка в сообщениях — выше
    picked.sort(key=lambda i: by_task[i][0])  # сильная связь — выше (сортировка устойчивая)
    return [{"task": by_id[i], "why": by_task[i][1], "history": by_task[i][0] == _ORDER["chat"]} for i in picked]


def _matcher(tasks: list[dict], topics: bool = False):
    """related() для чата по ключу (source, chat_id, title) — со справочником и именем директора, прочитанными раз."""
    people = records.list_people()
    owner = (config.load_settings().get("owner") or {}).get("name") or ""

    def match(key: tuple, msgs: list[dict], ch: dict) -> list[dict]:
        return related(*key, ch.get("kind"), msgs, tasks=tasks, people=people, owner=owner,
                       username=ch.get("username"), topics=topics)
    return match


def _chat_row(source: str, chat_id: str) -> dict:
    return store.row("SELECT kind, username FROM chats WHERE source=? AND chat_id=?", (source, chat_id)) or {}


def for_pending(chats: dict[tuple, list[dict]]) -> dict[tuple, list[dict]]:
    """Задачи под каждым чатом выдачи «новые сообщения»: ключ — (source, chat_id, title)."""
    tasks = records.list_commitments(status="open", sort="priority")
    if not tasks:
        return {}
    match = _matcher(tasks, topics=True)
    return {key: match(key, msgs, _chat_row(*key[:2])) for key, msgs in chats.items()}


MEETING_MESSAGES = 40  # сообщений в выдаче «переписка с начала встречи»: больше фоновый Claude не прочитает целиком


def meeting_tasks(meeting_id: str) -> list[dict]:
    """Открытые задачи встречи: появились на ней или директор заранее связала их со встречей (meta.json → tasks)."""
    try:
        meta = json.loads((records.meeting_dir(meeting_id) / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        meta = {}
    linked = [t["id"] for t in meta.get("tasks") or [] if isinstance(t, dict) and t.get("id")]
    ids = [r["id"] for r in store.rows("SELECT id FROM commitments WHERE source_type='meeting' AND source_ref=?",
                                       (meeting_id,))] + linked
    return records.list_commitments(status="open", sort="priority", ids=ids) if ids else []


def for_meeting(meeting_id: str, *, tasks: list[dict] | None = None, limit: int | None = None) -> dict:
    """Переписка с начала встречи в чатах, связанных с её задачами, — для разбора встречи.

    Встреча расшифровывается и разбирается через 10–60 минут после конца, а triage тем временем уже разобрал
    сообщения, которые пришли по ходу и сразу после, — без задач встречи, их ещё не было. «Скинь цены в
    телегу» — и цены приходят, пока встреча идёт: задача появляется уже выполненной, но никто этого не видит.
    Разбор встречи сверяет эти сообщения с только что записанными задачами (навык process-meeting, шаг 7а).

    Личный чат — целиком: собеседник и есть второй участник задачи. В группах и почте — только сообщения тех, с кем
    задачи, и те, где их упоминают: иначе одно упоминание в большом рабочем чате тащит за собой весь его поток.

    tasks — только эти задачи (только что записанная: ./ops commitments add показывает её переписку сразу).

    {"start", "tasks", "chats": [{"source", "chat_id", "title", "kind", "tasks": [{"task", "why", "history"}],
    "messages", "more"}]} — чаты в порядке первого сообщения, в каждом — сообщения по порядку; всего не больше
    limit (по умолчанию MEETING_MESSAGES), остальное — в "more" (сколько не показано)."""
    m = records.get_meeting(meeting_id) or {}
    start = m.get("started_at")
    if not start:
        tasks = []
    elif tasks is None:
        tasks = meeting_tasks(meeting_id)
    if not tasks:
        return {"start": start, "tasks": tasks, "chats": []}
    chats: dict[tuple, list[dict]] = {}
    for msg in store.rows("SELECT * FROM messages WHERE ts >= ? ORDER BY ts, id", (start,)):
        chats.setdefault((msg["source"], msg["chat_id"], msg["chat_title"]), []).append(msg)
    match = _matcher(tasks)
    picked = []
    for key, msgs in chats.items():
        ch = _chat_row(*key[:2])
        if ch.get("kind") != "private":
            msgs = [x for x in msgs if any(not f["history"] for f in match(key, [x], ch))]
        if msgs and (found := match(key, msgs, ch)):
            picked.append((key, ch, msgs, found))
    picked.sort(key=lambda p: (p[2][0]["ts"], p[2][0]["id"]))  # раньше написали по делу — выше
    out, left = [], MEETING_MESSAGES if limit is None else limit
    for key, ch, msgs, found in picked:
        shown = msgs[:max(left, 0)]
        left -= len(shown)
        out.append({"source": key[0], "chat_id": key[1], "title": key[2], "kind": ch.get("kind") or "",
                    "tasks": found, "messages": shown, "more": len(msgs) - len(shown)})
    return {"start": start, "tasks": tasks, "chats": out}
