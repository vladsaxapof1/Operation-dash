"""«Память» PULSE: факты, сущности, страницы знаний, «Что известно» (фича «Память», этапы 2–3).

- **Факт** — одно утверждение о рабочем мире с датой, источником и цитатой: цифра (metric), состояние (state), кто за
  что отвечает (role), как работает человек или клиент (trait), событие (event), как устроено (how). Пишется сразу,
  без «Да»; человек поправляет: «Неверно», «Устарело», «Забыть».
- **Ключ** — имя признака у цифр и состояний («конверсия в оплату»). Новый факт с тем же ключом о той же сущности
  сменяет прежний: тот получает статус replaced и ссылку на новый — «было → стало». Факт «от вас» (author=director)
  Claude не сменяет — только предлагает (предложение memory.fact).
- **Сущность** — о ком или о чём факты: человек (из справочника), компания, продукт, проект, метрика, система,
  команда. Псевдонимы — словарь, по которому фрагменты переписки и встреч связываются со страницей (chunk_entities).
- **Страница знаний** — всё о сущности сейчас: факты с историей, открытые задачи, решения, встречи, переписка.
  Собирается из базы без ИИ; выжимку сверху (pages.lead) пишет Claude (навык memory).

Факты пишутся пачкой: навык кладёт JSON-файл и зовёт `./ops facts import <файл>` один раз. Личное отсекает код:
факты из личной встречи (meetings.private) и чата «не запоминать» (chats.memory_off) не принимаются, строки, похожие
на секреты (пароли, ключи, номера карт и документов), отбрасываются по шаблонам.

Файл-приёмник data/memory/inbox.jsonl — для тех, кто не пишет в базу сам (MCP-сервер открыт только на чтение):
журнал «Вспомнить», remember и task_note агентов. Разбирает его сам PULSE (absorb_inbox).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from opsdash import config, store

FACT_KINDS = {"metric": "цифра", "state": "состояние", "role": "кто за что", "trait": "как работает",
              "event": "событие", "how": "как устроено"}
ENTITY_KINDS = {"person": "человек", "company": "компания", "product": "продукт", "project": "проект",
                "metric": "метрика", "system": "система", "team": "команда", "other": "другое"}
STATUSES = {"active": "действует", "replaced": "сменён", "outdated": "устарел", "wrong": "неверно",
            "proposed": "ждёт «Да»"}
AUTHORS = {"claude": "Claude", "director": "от вас", "agent": "от агента"}
MESSAGE_SOURCES = ("telegram", "ymessenger", "pachca", "slack", "mail")
SOURCE_TYPES = (*MESSAGE_SOURCES, "meeting", "kaiten", "agent", "manual", "document")
PAGE_MAX = 6_000          # байт: страница в выдаче CLI и MCP
SIMILAR_SKIP = 0.8        # факт без ключа почти дословно повторяет действующий о той же сущности — «уже известно»
SIMILAR_HINT = 0.5        # похож — подсказка «укажите replaces»
SAME_SOURCE_UPDATE = 0.6  # повторный разбор той же встречи: похожий факт обновляет прежний, а не дублирует


class MemoryError_(Exception):
    pass


MemoryError = MemoryError_  # noqa: A001 — имя по смыслу модуля; встроенный MemoryError здесь не нужен


def _settings() -> dict:
    return config.load_settings()["memory"]


# --- слова ----------------------------------------------------------------------------------------------------

def norm(text: str | None) -> str:
    from opsdash import recall

    return " ".join(recall.norm(text).split())


def _stemset(text: str | None) -> set[str]:
    from opsdash import recall

    return {s for s in recall.stems(text) if len(s) >= 3}


def similarity(a: str | None, b: str | None) -> float:
    x, y = _stemset(a), _stemset(b)
    return len(x & y) / len(x | y) if x and y else 0.0


def _alias_key(name: str | None) -> str:
    """Псевдоним для сравнения: без регистра, «ё», кавычек и знаков — «ООО „Ромашка“» и «ооо ромашка» — одно."""
    return " ".join(re.findall(r"\w+", norm(name)))


def _ddmm(day: str | None) -> str:
    return f"{day[8:10]}.{day[5:7]}" if day and len(day) >= 10 else ""


def _today() -> str:
    return config.now().date().isoformat()


# --- секреты и личное -----------------------------------------------------------------------------------------

_SECRET_WORDS = re.compile(r"(?i)(парол|password|passwd|\bpwd\b|токен|token|api[-_ ]?key|ключ api|secret|секретн"
                           r"|\bpin\b|пин-?код|cvv|cvc|логин и пароль)")
_SECRET_TOKEN = re.compile(r"(?<![\w-])(?=[A-Za-z0-9_\-]*\d)(?=[A-Za-z0-9_\-]*[A-Za-z])[A-Za-z0-9_\-]{24,}(?![\w-])"
                           r"|\b(?:sk|pk|ghp|gho|xox[abp]|AKIA)[-_][A-Za-z0-9_\-]{8,}")
_CARD = re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)")
_DOCUMENT = re.compile(r"(?i)(паспорт\w*\D{0,25}\d{2}\s?\d{2}\s?\d{6}|снилс\D{0,10}\d{3}-?\d{3}-?\d{3}[ -]?\d{2}"
                       r"|\b\d{3}-\d{3}-\d{3}[ -]\d{2}\b)")


def _luhn(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if alt:
            d = d * 2 - 9 if d * 2 > 9 else d * 2
        total += d
        alt = not alt
    return total % 10 == 0


def looks_secret(*texts: str | None) -> str | None:
    """Почему строка похожа на секрет (её не запоминаем) — или None."""
    for text in texts:
        if not text:
            continue
        if _SECRET_WORDS.search(text):
            return "пароль или ключ"
        if _SECRET_TOKEN.search(text):
            return "похоже на ключ или токен"
        for m in _CARD.finditer(text):
            digits = re.sub(r"\D", "", m.group())
            if 13 <= len(digits) <= 19 and _luhn(digits):
                return "номер карты"
        if _DOCUMENT.search(text):
            return "номер документа"
    return None


def _source_blocked(st: str | None, ref: str | None) -> str | None:
    """Источник, из которого факты не берутся: личная встреча, чат «не запоминать»."""
    if st == "meeting" and ref:
        mid = ref.split("#")[0].split(" ")[0]
        if store.scalar("SELECT private FROM meetings WHERE id=?", (mid,)):
            return "личная встреча"
    if st in MESSAGE_SOURCES and str(ref or "").isdigit():
        if store.scalar("SELECT c.memory_off FROM messages m JOIN chats c ON c.source=m.source AND c.chat_id=m.chat_id "
                        "WHERE m.id=?", (int(ref),)):
            return "чат «не запоминать»"
    return None


def parse_source(source: str | None) -> tuple[str | None, str | None]:
    st, _, ref = (source or "").strip().partition(":")
    st, ref = st.strip().lower() or None, ref.strip() or None
    if st and st not in SOURCE_TYPES:
        raise MemoryError(f"Источник — один из: {', '.join(SOURCE_TYPES)} (meeting:<id>, telegram:<id сообщения>, manual)")
    return st, ref


def _source_day(st: str | None, ref: str | None) -> str:
    """На какую дату факт: дата сообщения или встречи; иначе сегодня."""
    ts = None
    if st == "meeting" and ref:
        ts = store.scalar("SELECT started_at FROM meetings WHERE id=?", (ref,))
    elif st in MESSAGE_SOURCES and str(ref or "").isdigit():
        ts = store.scalar("SELECT ts FROM messages WHERE id=?", (int(ref),))
    if ts:
        return store.local(ts, "%Y-%m-%d")
    return _today()


# --- сущности ---------------------------------------------------------------------------------------------------

def _entity_rows() -> list[dict]:
    rows = store.rows("SELECT * FROM entities WHERE merged_into IS NULL")
    for r in rows:
        r["aliases"] = store.loads(r["aliases"], [])
    return rows


def _resolve(eid: int | None) -> int | None:
    seen = set()
    while eid and eid not in seen:
        seen.add(eid)
        nxt = store.scalar("SELECT merged_into FROM entities WHERE id=?", (eid,))
        if not nxt:
            return eid
        eid = nxt
    return eid


def entity(ref: str | int | None) -> dict | None:
    """Сущность по id, имени или псевдониму (объединённая — та, в которую влили)."""
    if ref is None or ref == "":
        return None
    if isinstance(ref, int) or str(ref).isdigit():
        e = store.row("SELECT * FROM entities WHERE id=?", (_resolve(int(ref)),))
    else:
        key = _alias_key(str(ref))
        e = next((r for r in _entity_rows() if key in {_alias_key(r["name"]), *map(_alias_key, r["aliases"])}), None)
    if e:
        e["aliases"] = store.loads(e["aliases"], []) if isinstance(e["aliases"], str) else e["aliases"]
    return e


def similar_entities(name: str, limit: int = 3) -> list[dict]:
    want = _stemset(name)
    key = _alias_key(name)
    out = []
    for r in _entity_rows():
        names = [r["name"], *r["aliases"]]
        best = max((similarity(name, n) for n in names), default=0)
        if best >= 0.5 or any(key and (key in _alias_key(n) or _alias_key(n) in key) for n in names if len(n) >= 3):
            out.append((best, r))
        elif want and any(_stemset(n) & want for n in names) and len(want) > 1:
            out.append((0.3, r))
    out.sort(key=lambda x: -x[0])
    return [r for _, r in out[:limit]]


def ensure_entity(name: str, kind: str | None = None, aliases: list[str] | None = None) -> tuple[int, bool]:
    """id сущности по имени или псевдониму; нет — создать (человек из справочника и проект — со ссылкой)."""
    from opsdash import records

    name = " ".join((name or "").split()).strip(" ,.;«»\"'")
    if not name:
        raise MemoryError("пустое имя сущности")
    e = entity(name)
    if e:
        if aliases:
            add_aliases(e["id"], aliases)
        return e["id"], False
    person = records.find_person(name)
    if person:
        e = store.row("SELECT id FROM entities WHERE person_id=? AND merged_into IS NULL", (person["id"],))
        if e:
            add_aliases(e["id"], [name, *(aliases or [])])
            return e["id"], False
    project = store.row("SELECT id, title FROM projects WHERE lower(title)=lower(?)", (name,))
    if kind not in ENTITY_KINDS:
        kind = "person" if person else "project" if project else "other"
    now = store.utcnow()
    eid = store.execute(
        "INSERT INTO entities(kind, name, aliases, person_id, project_id, created, updated) VALUES(?,?,?,?,?,?,?)",
        (kind, person["name"] if person else name,
         store.json_col(list(dict.fromkeys(a for a in [*(aliases or []), *(person["aliases"] if person else []),
                                                         name if person and name != person["name"] else ""]
                                           if a))),
         person["id"] if person else None, project["id"] if project else None, now, now))
    _dictionary_changed()
    return eid, True


def add_aliases(eid: int, aliases: list[str]) -> None:
    e = store.row("SELECT name, aliases FROM entities WHERE id=?", (eid,))
    if not e:
        return
    have = store.loads(e["aliases"], [])
    keys = {_alias_key(e["name"]), *map(_alias_key, have)}
    new = [a.strip() for a in aliases if a and a.strip() and _alias_key(a) not in keys]
    if new:
        store.execute("UPDATE entities SET aliases=?, updated=? WHERE id=?",
                      (store.json_col([*have, *dict.fromkeys(new)]), store.utcnow(), eid))
        _dictionary_changed()


def rename_entity(eid: int, name: str) -> None:
    e = entity(eid)
    if not e:
        raise MemoryError(f"нет сущности {eid}")
    name = " ".join(name.split())
    other = entity(name)
    if other and other["id"] != e["id"]:
        raise MemoryError(f"«{name}» — уже другая страница [{other['id']}]: объедините их")
    store.execute("UPDATE entities SET name=?, aliases=?, updated=? WHERE id=?",
                  (name, store.json_col(list(dict.fromkeys([*e["aliases"], e["name"]]))), store.utcnow(), e["id"]))
    _dictionary_changed()


def merge_entities(eid: int, into: int) -> dict:
    """Дубль сущности влить в основную: псевдонимы, факты, фрагменты; дубль остаётся ссылкой (merged_into)."""
    a, b = entity(eid), entity(into)
    if not a or not b:
        raise MemoryError("нет такой сущности")
    if a["id"] == b["id"]:
        raise MemoryError("это одна и та же страница")
    add_aliases(b["id"], [a["name"], *a["aliases"]])
    with store.db() as conn:
        for table, col in (("fact_entities", "fact_id"), ("chunk_entities", "chunk_id")):
            conn.execute(f"INSERT OR IGNORE INTO {table}({col}, entity_id) SELECT {col}, ? FROM {table} "
                         f"WHERE entity_id=?", (b["id"], a["id"]))
            conn.execute(f"DELETE FROM {table} WHERE entity_id=?", (a["id"],))
        if not b.get("person_id") and a.get("person_id"):
            conn.execute("UPDATE entities SET person_id=? WHERE id=?", (a["person_id"], b["id"]))
        if not b.get("project_id") and a.get("project_id"):
            conn.execute("UPDATE entities SET project_id=? WHERE id=?", (a["project_id"], b["id"]))
        conn.execute("UPDATE entities SET merged_into=?, updated=? WHERE id=?", (b["id"], store.utcnow(), a["id"]))
        conn.execute("DELETE FROM pages WHERE entity_id=?", (a["id"],))
    _reindex_facts_of(b["id"])
    _dictionary_changed()
    return {"from": a["name"], "into": b["name"], "id": b["id"]}


def forget_entity(eid: int) -> int:
    """«Забыть всё о …»: сущность и все её факты (только о ней) — совсем, вместе со строками индекса."""
    e = entity(eid)
    if not e:
        raise MemoryError(f"нет сущности {eid}")
    ids = [r["fact_id"] for r in store.rows(
        "SELECT fact_id FROM fact_entities WHERE entity_id=? AND fact_id NOT IN "
        "(SELECT fact_id FROM fact_entities WHERE entity_id != ?)", (e["id"], e["id"]))]
    for fid in ids:
        forget_fact(fid)
    with store.db() as conn:
        conn.execute("DELETE FROM fact_entities WHERE entity_id=?", (e["id"],))
        conn.execute("DELETE FROM chunk_entities WHERE entity_id=?", (e["id"],))
        conn.execute("DELETE FROM pages WHERE entity_id=?", (e["id"],))
        conn.execute("DELETE FROM entities WHERE id=? OR merged_into=?", (e["id"], e["id"]))
    page = pages_dir() / f"{_safe(e['name'])}.md"
    page.unlink(missing_ok=True)
    _dictionary_changed()
    return len(ids)


def _owner_entity() -> int | None:
    from opsdash import voice

    e = entity(voice.owner_name())
    return e["id"] if e else None


# --- словарь псевдонимов: фрагменты ↔ сущности --------------------------------------------------------------------

def _dictionary_changed() -> None:
    store.set_state("memory_dict_changed", store.utcnow())


def _alias_pattern(alias: str) -> str | None:
    from opsdash import recall

    words = re.findall(r"\w+", norm(alias))
    if not words or sum(len(w) for w in words) < 3:
        return None
    parts = []
    for w in words:
        if len(w) >= 4 and re.search("[а-я]", w):  # «Олег» — и «Олега», «Олегу»; «Ромашка» — и «Ромашке»
            parts.append(re.escape(recall.stem(w)) + r"\w{0,3}")
        else:
            parts.append(re.escape(w))
    return r"(?<!\w)" + r"\W{1,3}".join(parts) + r"(?!\w)"


def _matcher() -> tuple[re.Pattern | None, dict[str, int]]:
    groups, owner = {}, _owner_entity()
    parts = []
    for e in _entity_rows():
        if e["id"] == owner:  # директор — в каждом втором сообщении: связь со страницей ничего не даёт
            continue
        for n, alias in enumerate(dict.fromkeys([e["name"], *e["aliases"]])):
            pat = _alias_pattern(alias)
            if pat:
                g = f"e{e['id']}_{n}"
                groups[g] = e["id"]
                parts.append(f"(?P<{g}>{pat})")
    if not parts:
        return None, {}
    return re.compile("|".join(parts)), groups


def entities_in(*texts: str | None, matcher=None) -> set[int]:
    rx, groups = matcher or _matcher()
    if not rx:
        return set()
    found = set()
    for text in texts:
        for m in rx.finditer(norm(text)):
            found.add(groups[m.lastgroup])
    return found


def link_chunks() -> int:
    """Фрагменты «Вспомнить» ↔ сущности по словарю псевдонимов. Новые фрагменты — после прошлой отметки; словарь
    поменялся (новая сущность, псевдоним, объединение) — все заново."""
    if store._query_only:
        return 0
    changed = store.get_state("memory_dict_changed") or ""
    linked_at = store.get_state("memory_linked_at") or ""
    last = int(store.get_state("memory_linked_chunk") or 0)
    full = changed > linked_at
    rx, groups = _matcher()
    top = store.scalar("SELECT MAX(id) FROM memory_chunks") or 0
    if not rx:
        store.set_state("memory_linked_chunk", top)
        store.set_state("memory_linked_at", store.utcnow())
        return 0
    n = 0
    with store.db() as conn:
        if full:
            conn.execute("DELETE FROM chunk_entities")
            rows = conn.execute("SELECT id, title, text FROM memory_chunks").fetchall()
        else:
            rows = conn.execute("SELECT id, title, text FROM memory_chunks WHERE id > ?", (last,)).fetchall()
        for r in rows:
            for eid in entities_in(r["title"], r["text"], matcher=(rx, groups)):
                conn.execute("INSERT OR IGNORE INTO chunk_entities(chunk_id, entity_id) VALUES(?,?)", (r["id"], eid))
                n += 1
    store.set_state("memory_linked_chunk", top)
    store.set_state("memory_linked_at", store.utcnow())
    return n


# --- факты: запись ----------------------------------------------------------------------------------------------

def _fact_hash(text: str, st: str | None, ref: str | None) -> str:
    return hashlib.sha1(f"{norm(text)}|{st or ''}:{ref or ''}".encode("utf-8")).hexdigest()


def _index_fact(fid: int, conn=None) -> None:
    own = conn is None
    if own:
        ctx = store.db()
        conn = ctx.__enter__()
    try:
        f = conn.execute("SELECT id, text, key, quote FROM facts WHERE id=?", (fid,)).fetchone()
        conn.execute("DELETE FROM facts_fts WHERE rowid=?", (fid,))
        if f:
            names = " ".join(r[0] for r in conn.execute(
                "SELECT e.name || ' ' || COALESCE(e.aliases, '') FROM fact_entities fe JOIN entities e "
                "ON e.id=fe.entity_id WHERE fe.fact_id=?", (fid,)))
            conn.execute("INSERT INTO facts_fts(rowid, text, key, about) VALUES(?,?,?,?)",
                         (fid, norm(f["text"]), norm(f["key"]), norm(names)))
    finally:
        if own:
            ctx.__exit__(None, None, None)


def _reindex_facts_of(eid: int) -> None:
    for r in store.rows("SELECT fact_id FROM fact_entities WHERE entity_id=?", (eid,)):
        _index_fact(r["fact_id"])


def _about_ids(about: list | str | None, kinds: dict[str, str] | None = None,
               aliases: dict[str, list[str]] | None = None) -> tuple[list[int], list[dict]]:
    """Сущности факта по именам; новые создаются — с похожими, чтобы навык в следующий раз указал существующую."""
    if isinstance(about, str):
        about = [a for a in re.split(r"[;,]", about)]
    ids, created = [], []
    for name in about or []:
        name = " ".join(str(name).split()).strip()
        if not name:
            continue
        key = _alias_key(name)
        kind = (kinds or {}).get(key)
        eid, new = ensure_entity(name, kind, (aliases or {}).get(key))
        if eid not in ids:
            ids.append(eid)
        if new:
            created.append({"id": eid, "name": name,
                            "similar": [s["name"] for s in similar_entities(name) if s["id"] != eid]})
    return ids, created


def _active_by_key(key: str, eids: list[int]) -> dict | None:
    if not key or not eids:
        return None
    return store.row(
        f"SELECT f.* FROM facts f JOIN fact_entities fe ON fe.fact_id=f.id WHERE f.status IN ('active', 'proposed') "
        f"AND f.key=? AND fe.entity_id=? ORDER BY f.happened DESC, f.id DESC LIMIT 1", (norm(key), eids[0]))


def _active_similar(text: str, eids: list[int], exclude: int | None = None) -> tuple[float, dict | None]:
    if not eids:
        return 0.0, None
    marks = ",".join("?" for _ in eids)
    best, best_f = 0.0, None
    for f in store.rows(f"SELECT DISTINCT f.* FROM facts f JOIN fact_entities fe ON fe.fact_id=f.id "
                        f"WHERE f.status='active' AND fe.entity_id IN ({marks})", eids):
        if f["id"] == exclude:
            continue
        s = similarity(text, f["text"])
        if s > best:
            best, best_f = s, f
    return best, best_f


def add_fact(*, text: str, kind: str = "state", about=None, key: str | None = None, when: str | None = None,
             source: str | None = None, quote: str | None = None, replaces: int | None = None,
             because: int | None = None, by: str = "claude", session: str | None = None,
             kinds: dict | None = None, aliases: dict | None = None, report: dict | None = None) -> int | None:
    """Записать один факт по правилам: секреты и личное — нет; повтор из того же источника — обновить; тот же ключ
    у той же сущности — сменить прежний; почти дословный повтор действующего — «уже известно». Вернуть id или None."""
    report = report if report is not None else _new_report()
    text = " ".join((text or "").split()).strip()
    quote = " ".join((quote or "").split()).strip() or None
    if not text:
        report["errors"].append("факт без текста")
        return None
    if kind not in FACT_KINDS:
        report["errors"].append(f"«{text[:60]}»: вид — один из {', '.join(FACT_KINDS)}")
        return None
    if why := looks_secret(text, quote, key):
        report["secrets"].append(f"«{text[:40]}…» — {why}")
        return None
    st, ref = parse_source(source)
    if by == "claude" and not st:
        report["errors"].append(f"«{text[:60]}»: нет источника (source: meeting:<id>, telegram:<id>…)")
        return None
    if by == "claude" and st not in ("manual",) and not quote:
        report["errors"].append(f"«{text[:60]}»: нет цитаты (quote) — без неё факт не записываю")
        return None
    if why := _source_blocked(st, ref):
        report["private"].append(f"«{text[:40]}…» — {why}")
        return None
    when = (when or "")[:10] or _source_day(st, ref)
    try:
        date.fromisoformat(when)
    except ValueError:
        report["errors"].append(f"«{text[:60]}»: дата — YYYY-MM-DD")
        return None
    eids, created = _about_ids(about, kinds, aliases)
    report["new_entities"] += [c for c in created if c not in report["new_entities"]]
    key_n = norm(key) or None
    now = store.utcnow()
    h = _fact_hash(text, st, ref)
    # 1) повтор того же источника: тот же текст, тот же ключ у той же сущности или очень похожий — обновить
    same = store.row("SELECT * FROM facts WHERE hash=?", (h,))
    if not same and st and ref:
        for f in store.rows("SELECT * FROM facts WHERE source_type=? AND source_ref=? AND status != 'wrong'", (st, ref)):
            f_eids = [r["entity_id"] for r in store.rows("SELECT entity_id FROM fact_entities WHERE fact_id=?", (f["id"],))]
            if ((key_n and f["key"] == key_n and set(f_eids) & set(eids))
                    or similarity(text, f["text"]) >= SAME_SOURCE_UPDATE):
                same = f
                break
    if same:
        if same["status"] == "wrong":  # человек сказал «Неверно» — тот же факт из того же источника не возвращаем
            report["skipped"].append(f"«{text[:40]}…» — отмечен вами как неверный")
            return None
        store.execute("UPDATE facts SET text=?, kind=?, key=COALESCE(?, key), quote=COALESCE(?, quote), happened=?, "
                      "updated=? WHERE id=?", (text, kind, key_n, quote, when, now, same["id"]))
        _set_entities(same["id"], eids)
        report["updated"].append(same["id"])
        return same["id"]
    # 2) почти дословный повтор действующего факта о той же сущности (без ключа) — уже известно
    sim, near = _active_similar(text, eids)
    if not key_n and not replaces and near and sim >= SIMILAR_SKIP:
        report["known"].append(near["id"])
        return near["id"]
    # 3) что сменяет: явно (replaces) или тот же ключ у той же сущности
    old = store.row("SELECT * FROM facts WHERE id=?", (int(replaces),)) if replaces else _active_by_key(key_n, eids)
    status, replaces_id = "active", None
    if old and old["status"] in ("active", "proposed"):
        if (old["happened"] or "") > when:  # старый разбор, а у нас уже свежее — новое уходит в историю
            status = "replaced"
        elif old["author"] == "director" and by != "director":
            status = "proposed"  # «от вас» Claude не сменяет — предлагает
        replaces_id = old["id"]
    fid = store.execute(
        "INSERT INTO facts(kind, text, key, happened, source_type, source_ref, quote, status, replaces_id, because_id, "
        "author, agent_session, hash, created, updated) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (kind, text, key_n, when, st, ref, quote, status, replaces_id if status != "replaced" else None,
         because, by, session, h, now, now))
    _set_entities(fid, eids)
    if status == "replaced" and old:  # новое — история старого: «было» у действующего
        store.execute("UPDATE facts SET replaced_by=? WHERE id=?", (old["id"], fid))
        store.execute("UPDATE facts SET replaces_id=? WHERE id=? AND replaces_id IS NULL", (fid, old["id"]))
        report["history"].append(fid)
    elif status == "proposed" and old:
        from opsdash import proposals

        proposals.propose_memory_fact(fact_id=fid, replaces=old["id"])
        report["proposed"].append(fid)
    elif replaces_id and old:
        store.execute("UPDATE facts SET status='replaced', replaced_by=?, updated=? WHERE id=?", (fid, now, old["id"]))
        report["replaced"].append((old["id"], fid))
    else:
        report["added"].append(fid)
        if not key_n and near and sim >= SIMILAR_HINT:
            report["hints"].append(f"[ф{fid}] похож на [ф{near['id']}] «{near['text'][:70]}» — если сменяет его: "
                                   f"./ops facts set {fid} --replaces {near['id']}")
    return fid


def _set_entities(fid: int, eids: list[int]) -> None:
    with store.db() as conn:
        conn.execute("DELETE FROM fact_entities WHERE fact_id=?", (fid,))
        for eid in eids:
            conn.execute("INSERT OR IGNORE INTO fact_entities(fact_id, entity_id) VALUES(?,?)", (fid, eid))
        _index_fact(fid, conn)


def _new_report() -> dict:
    return {"added": [], "updated": [], "replaced": [], "history": [], "proposed": [], "known": [], "hints": [],
            "new_entities": [], "secrets": [], "private": [], "skipped": [], "errors": [], "over": 0}


def import_facts(data: dict | list, *, source: str | None = None, by: str = "claude", cap: int | None = None,
                 session: str | None = None) -> dict:
    """Пачка фактов из файла навыка: {"source": "meeting:<id>", "entities": [{name, kind, aliases}],
    "facts": [{kind, text, key?, about: [..], when?, quote, source?, replaces?, because?, ref?}]}."""
    if isinstance(data, list):
        data = {"facts": data}
    facts = data.get("facts") or []
    default_source = data.get("source") or source
    kinds, aliases = {}, {}
    for e in data.get("entities") or []:
        if isinstance(e, dict) and e.get("name"):
            kinds[_alias_key(e["name"])] = e.get("kind")
            aliases[_alias_key(e["name"])] = e.get("aliases") or []
    if cap is None:
        st, _ = parse_source(default_source) if default_source else (None, None)
        s = _settings()
        cap = int(s["max_facts_meeting"] if st == "meeting" else s["max_facts_day"])
    report = _new_report()
    if cap and len(facts) > cap:
        report["over"] = len(facts) - cap
        facts = facts[:cap]
    refs: dict[str, int] = {}
    pending_because: list[tuple[int, str]] = []
    touched: list[int] = []
    for f in facts:
        if not isinstance(f, dict):
            report["errors"].append("факт — объект JSON")
            continue
        because = f.get("because")
        because_id = int(because) if isinstance(because, int) or str(because or "").isdigit() else None
        fid = add_fact(text=f.get("text") or "", kind=f.get("kind") or "state", about=f.get("about"),
                       key=f.get("key"), when=f.get("when"), source=f.get("source") or default_source,
                       quote=f.get("quote"), replaces=f.get("replaces"), because=because_id, by=by,
                       session=session, kinds=kinds, aliases=aliases, report=report)
        if fid:
            touched.append(fid)
            if f.get("ref"):
                refs[str(f["ref"])] = fid
            if because and not because_id:
                pending_because.append((fid, str(because)))
    for fid, ref in pending_because:
        if ref in refs and refs[ref] != fid:
            store.execute("UPDATE facts SET because_id=? WHERE id=?", (refs[ref], fid))
    report["touched"] = touched
    if touched:
        store.set_state("memory_facts_changed", store.utcnow())
    return report


def report_text(r: dict) -> str:
    parts = [f"Записано фактов: {len(r['added'])}"]
    if r["replaced"]:
        parts.append(f"сменили прежние: {len(r['replaced'])} (" + ", ".join(f"[ф{a}] → [ф{b}]" for a, b in r["replaced"]) + ")")
    if r["updated"]:
        parts.append(f"обновлено повторов: {len(r['updated'])}")
    if r["known"]:
        parts.append(f"уже известно: {len(r['known'])}")
    if r["history"]:
        parts.append(f"в историю (есть свежее): {len(r['history'])}")
    if r["proposed"]:
        parts.append(f"сменяют факт «от вас» — предложено: {len(r['proposed'])}")
    lines = [" · ".join(parts)]
    if r["over"]:
        lines.append(f"Сверх потолка — не записано: {r['over']}. Оставляй то, что пригодится через месяц.")
    for e in r["new_entities"]:
        lines.append(f"Новая сущность [{e['id']}] «{e['name']}»" + (
            f" — похожие: {', '.join('«' + s + '»' for s in e['similar'])}; если это одно и то же: "
            f"./ops memory merge {e['id']} --into \"{e['similar'][0]}\"" if e["similar"] else ""))
    lines += [f"Подсказка: {h}" for h in r["hints"]]
    if r["secrets"]:
        lines.append("Не записано — похоже на секрет: " + "; ".join(r["secrets"]))
    if r["private"]:
        lines.append("Не записано — личное: " + "; ".join(r["private"]))
    if r["skipped"]:
        lines.append("Пропущено: " + "; ".join(r["skipped"]))
    if r["errors"]:
        lines.append("Ошибки: " + "; ".join(r["errors"]))
    return "\n".join(lines)


def set_status(fid: int, status: str, reason: str | None = None) -> dict:
    """«Неверно» (с причиной — она уйдёт в уроки навыку), «Устарело», «Вернуть»."""
    f = store.row("SELECT * FROM facts WHERE id=?", (fid,))
    if not f:
        raise MemoryError(f"нет факта [ф{fid}]")
    if status not in ("active", "outdated", "wrong"):
        raise MemoryError("статус — active, outdated или wrong")
    store.execute("UPDATE facts SET status=?, reason=?, updated=?, decided_at=? WHERE id=?",
                  (status, (reason or "").strip() or None, store.utcnow(), store.utcnow(), fid))
    if status in ("wrong", "outdated") and f["replaces_id"]:  # сменивший оказался неверным — прежний снова действует
        store.execute("UPDATE facts SET status='active', replaced_by=NULL WHERE id=? AND replaced_by=? "
                      "AND status='replaced'", (f["replaces_id"], fid))
    store.set_state("memory_facts_changed", store.utcnow())
    return store.row("SELECT * FROM facts WHERE id=?", (fid,))


def forget_fact(fid: int) -> None:
    """«Забыть»: факт удаляется совсем — с сущностями, связями, вектором и строкой индекса."""
    f = store.row("SELECT * FROM facts WHERE id=?", (fid,))
    if not f:
        return
    with store.db() as conn:
        conn.execute("DELETE FROM fact_entities WHERE fact_id=?", (fid,))
        conn.execute("DELETE FROM facts_fts WHERE rowid=?", (fid,))
        conn.execute("DELETE FROM fact_links WHERE a=? OR b=?", (fid, fid))
        conn.execute("DELETE FROM memory_vectors WHERE key=?", (f"f:{fid}",))
        conn.execute("UPDATE facts SET replaces_id=NULL WHERE replaces_id=?", (fid,))
        conn.execute("UPDATE facts SET because_id=NULL WHERE because_id=?", (fid,))
        if f["replaces_id"]:
            conn.execute("UPDATE facts SET status='active', replaced_by=NULL WHERE id=? AND replaced_by=?",
                         (f["replaces_id"], fid))
        conn.execute("UPDATE facts SET replaced_by=NULL WHERE replaced_by=?", (fid,))
        conn.execute("DELETE FROM facts WHERE id=?", (fid,))
    store.set_state("memory_facts_changed", store.utcnow())


def forget_source(st: str, ref: str) -> int:
    """Источник стал личным — факты из него забываются."""
    ids = [r["id"] for r in store.rows("SELECT id FROM facts WHERE source_type=? AND (source_ref=? OR source_ref LIKE ?)",
                                       (st, ref, f"{ref}#%"))]
    for fid in ids:
        forget_fact(fid)
    return len(ids)


def set_replaces(fid: int, old: int) -> None:
    f = store.row("SELECT * FROM facts WHERE id=?", (fid,))
    o = store.row("SELECT * FROM facts WHERE id=?", (old,))
    if not f or not o:
        raise MemoryError("нет такого факта")
    store.execute("UPDATE facts SET replaces_id=? WHERE id=?", (old, fid))
    store.execute("UPDATE facts SET status='replaced', replaced_by=?, updated=? WHERE id=?", (fid, store.utcnow(), old))


# --- факты: чтение ---------------------------------------------------------------------------------------------

def _decorate(items: list[dict]) -> list[dict]:
    if not items:
        return items
    ids = [f["id"] for f in items]
    marks = ",".join("?" for _ in ids)
    ents: dict[int, list[dict]] = {}
    for r in store.rows(f"SELECT fe.fact_id, e.id, e.name, e.kind FROM fact_entities fe JOIN entities e "
                        f"ON e.id=fe.entity_id WHERE fe.fact_id IN ({marks})", ids):
        ents.setdefault(r["fact_id"], []).append({"id": r["id"], "name": r["name"], "kind": r["kind"]})
    olds = {r["id"]: r for r in store.rows(
        f"SELECT id, text, happened FROM facts WHERE id IN (SELECT replaces_id FROM facts WHERE id IN ({marks}))", ids)}
    meetings = {r["id"]: r for r in store.rows(
        f"SELECT id, title, started_at FROM meetings WHERE id IN "
        f"(SELECT source_ref FROM facts WHERE source_type='meeting' AND id IN ({marks}))", ids)}
    for f in items:
        f["entities"] = ents.get(f["id"], [])
        f["prev"] = olds.get(f.get("replaces_id"))
        f["kind_title"] = FACT_KINDS.get(f["kind"], f["kind"])
        f["status_title"] = STATUSES.get(f["status"], f["status"])
        f["meeting"] = meetings.get(f.get("source_ref")) if f.get("source_type") == "meeting" else None
    return items


def facts(*, about: str | int | None = None, kind: str | None = None, since: str | None = None,
          status: str | None = "active", q: str | None = None, source: str | None = None, limit: int = 100,
          ids: list[int] | None = None) -> list[dict]:
    where, params = ["1=1"], []
    if status and status != "all":
        where.append("f.status=?")
        params.append(status)
    if kind:
        where.append("f.kind=?")
        params.append(kind)
    if since:
        where.append("(f.happened >= ? OR f.created >= ?)")
        params += [since, since]
    if source:
        st, _, ref = source.partition(":")
        where.append("f.source_type=?")
        params.append(st)
        if ref:
            where.append("f.source_ref=?")
            params.append(ref)
    if ids is not None:
        where.append(f"f.id IN ({','.join('?' * len(ids)) or 'NULL'})")
        params += ids
    if about not in (None, ""):
        e = entity(about)
        if not e:
            return []
        where.append("f.id IN (SELECT fact_id FROM fact_entities WHERE entity_id=?)")
        params.append(e["id"])
    order = "f.happened DESC, f.id DESC"
    if q:
        from opsdash import recall

        fq = recall.fts_query(q)
        if not fq:
            return []
        where.append("f.id IN (SELECT rowid FROM facts_fts WHERE facts_fts MATCH ?)")
        params.append(fq)
    rows = store.rows(f"SELECT f.* FROM facts f WHERE {' AND '.join(where)} ORDER BY {order} LIMIT ?", [*params, limit])
    return _decorate(rows)


def fact(fid: int) -> dict | None:
    rows = facts(ids=[fid], status="all")
    if not rows:
        return None
    f = rows[0]
    chain, cur, seen = [], f.get("replaces_id"), {fid}
    while cur and cur not in seen:  # история «было → стало»
        seen.add(cur)
        o = store.row("SELECT id, text, happened, status, source_type, source_ref FROM facts WHERE id=?", (cur,))
        if not o:
            break
        chain.append(o)
        cur = store.scalar("SELECT replaces_id FROM facts WHERE id=?", (cur,))
    f["history"] = chain
    f["next"] = store.row("SELECT id, text, happened FROM facts WHERE id=?", (f["replaced_by"],)) if f.get("replaced_by") else None
    f["because"] = store.row("SELECT id, text FROM facts WHERE id=?", (f["because_id"],)) if f.get("because_id") else None
    return f


def source_label(f: dict, *, mcp: bool = False) -> str:
    """Откуда факт — для строки выдачи. Агентам переписку не называем: «переписка, 03.11»."""
    st, ref = f.get("source_type"), f.get("source_ref") or ""
    if st == "meeting":
        m = f.get("meeting") or store.row("SELECT title FROM meetings WHERE id=?", (ref,)) or {}
        tc = re.search(r"\[(\d\d:\d\d:\d\d)\]", f.get("quote") or "")
        return f"встреча «{m.get('title') or ref}»" + (f" [{tc.group(1)}]" if tc else "") + (f" ({ref})" if mcp else "")
    if st in MESSAGE_SOURCES:
        if mcp:
            return f"переписка, {_ddmm(f.get('happened'))}"
        chat = store.scalar("SELECT chat_title FROM messages WHERE id=?", (int(ref),)) if ref.isdigit() else None
        return f"«{chat or st}» [{ref}]"
    if f.get("author") == "director" or st == "manual":
        return "от вас"
    if st == "agent" or f.get("author") == "agent":
        return "от агента"
    if st == "kaiten":
        return f"Kaiten #{ref}"
    return st or ""


def fact_line(f: dict, *, mcp: bool = False, about: bool = True, width: int = 260) -> str:
    who = ", ".join(e["name"] for e in f.get("entities") or []) if about else ""
    prev = ""
    if f.get("prev"):
        p = f["prev"]
        prev = f" (было: {_short(p['text'], 70)}, {_ddmm(p['happened'])})"
    text = _short(f["text"], width)
    mine = " · от вас" if f.get("author") == "director" else ""
    head = f"[ф{f['id']}] {_ddmm(f.get('happened'))} · {FACT_KINDS.get(f['kind'], f['kind'])}" + (f" · {who}" if who else "")
    return f"{head} · {text}{prev} · {source_label(f, mcp=mcp)}{mine if f.get('author') == 'director' and f.get('source_type') not in (None, 'manual') else ''}"


def _short(text: str | None, n: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[:n - 1].rstrip(" ,.;—") + "…"


# --- «Вспомнить»: страница и факты ------------------------------------------------------------------------------

def _entities_for_query(query: str, about: str | None) -> list[dict]:
    """Сущности, о которых спрашивают: все слова имени (или псевдонима) есть в запросе."""
    from opsdash import recall

    want = set(recall.stems(f"{query} {about or ''}"))
    out = []
    for e in _entity_rows():
        for n in [e["name"], *e["aliases"]]:
            s = set(recall.stems(n))
            if s and s <= want:
                out.append(e)
                break
    out.sort(key=lambda e: -len(recall.stems(e["name"])))
    return out


def recall_part(query: str, *, about: str | None = None, since: str | None = None, mcp: bool = False,
                limit: int = 6) -> dict | None:
    """Для «Вспомнить»: строка о странице знаний и лучшие действующие факты (слова, сущности, смысл — RRF)."""
    from opsdash import recall

    if not store.scalar("SELECT 1 FROM facts LIMIT 1") and not store.scalar("SELECT 1 FROM entities LIMIT 1"):
        return None
    head = []
    ents = _entities_for_query(query, about)
    for e in ents[:2]:
        n = store.scalar("SELECT COUNT(*) FROM fact_entities fe JOIN facts f ON f.id=fe.fact_id "
                         "WHERE fe.entity_id=? AND f.status='active'", (e["id"],)) or 0
        call = f'page("{e["name"]}")' if mcp else f'./ops page "{e["name"]}"'
        head.append(f"Страница: «{e['name']}» ({ENTITY_KINDS.get(e['kind'], e['kind'])}, фактов: {n}) — {call}")
    about_e = entity(about) if about else None
    fq = recall.fts_query(query)
    words = []
    if fq:
        sql = ("SELECT f.id FROM facts_fts x JOIN facts f ON f.id = x.rowid WHERE facts_fts MATCH ? "
               "AND f.status='active'")
        params: list = [fq]
        if since:
            sql += " AND f.happened >= ?"
            params.append(since)
        try:
            words = [r["id"] for r in store.rows(sql + " ORDER BY bm25(facts_fts, 1.0, 2.0, 1.5) LIMIT 100", params)]
        except Exception:  # noqa: BLE001
            words = []
    by_entity = []
    for e in ([about_e] if about_e else []) + ents[:2]:
        if e:
            by_entity += [r["fact_id"] for r in store.rows(
                "SELECT fe.fact_id FROM fact_entities fe JOIN facts f ON f.id=fe.fact_id WHERE fe.entity_id=? "
                "AND f.status='active'" + (" AND f.happened >= ?" if since else "") +
                " ORDER BY f.happened DESC LIMIT 30", (e["id"], *([since] if since else [])))]
    sem = recall._semantic(query) or {}
    sem_ids = [i for i in sem.get("facts") or []]
    ranked = recall.rrf(words, by_entity, sem_ids)
    if about_e:
        allowed = {r["fact_id"] for r in store.rows("SELECT fact_id FROM fact_entities WHERE entity_id=?",
                                                    (about_e["id"],))}
        ranked = [i for i in ranked if i in allowed]
    elif about:
        want = recall.stems(about)
        rows = {f["id"]: f for f in facts(ids=ranked[:200], status="active", limit=200)}
        ranked = [i for i in ranked if i in rows and all(
            s in norm(rows[i]["text"] + " " + " ".join(e["name"] for e in rows[i]["entities"])) for s in want)]
    rows = {f["id"]: f for f in facts(ids=ranked[:limit * 3], status="active", since=since, limit=limit * 3)}
    if since:
        ranked = [i for i in ranked if i in rows or i not in ranked[:limit * 3]]
    shown = [rows[i] for i in ranked if i in rows][:limit]
    return {"head": head, "lines": [fact_line(f, mcp=mcp) for f in shown], "total": len(ranked),
            "semantic": bool(sem_ids)}


# --- «Что известно»: разбор сообщений, бриф, окно задачи --------------------------------------------------------

def known_about(eids: list[int], *, limit: int = 5, exclude: set[int] | None = None, mcp: bool = False,
                since: str | None = None) -> list[dict]:
    """Свежие действующие факты о сущностях: сначала то, что помогает ответить — как работает человек, кто за что,
    состояния и цифры."""
    if not eids:
        return []
    marks = ",".join("?" for _ in eids)
    order = "CASE f.kind WHEN 'trait' THEN 0 WHEN 'role' THEN 1 WHEN 'state' THEN 2 WHEN 'metric' THEN 3 ELSE 4 END"
    rows = store.rows(
        f"SELECT DISTINCT f.* FROM facts f JOIN fact_entities fe ON fe.fact_id=f.id WHERE fe.entity_id IN ({marks}) "
        f"AND f.status='active'" + (" AND (f.happened >= ? OR f.created >= ?)" if since else "") +
        f" ORDER BY f.happened DESC, {order}, f.id DESC LIMIT ?",
        [*eids, *([since, since] if since else []), limit * 3])
    rows = [r for r in rows if r["id"] not in (exclude or set())][:limit]
    return _decorate(rows)


def entities_for(texts: list[str], names: list[str] | None = None) -> list[int]:
    """Сущности собеседников (по имени) и упомянутые в текстах (по словарю), без директора."""
    owner = _owner_entity()
    out: list[int] = []
    for n in names or []:
        e = entity(n)
        if not e:
            from opsdash import records

            p = records.find_person(n)
            e = store.row("SELECT id FROM entities WHERE person_id=? AND merged_into IS NULL", (p["id"],)) if p else None
        if e and e["id"] != owner and e["id"] not in out:
            out.append(e["id"])
    for eid in entities_in(*texts):
        if eid != owner and eid not in out:
            out.append(eid)
    return out


def known_lines(texts: list[str], names: list[str] | None = None, *, limit: int = 5,
                shown: set[int] | None = None) -> list[str]:
    eids = entities_for(texts, names)
    rows = known_about(eids, limit=limit, exclude=shown)
    if shown is not None:
        shown.update(f["id"] for f in rows)
    return [fact_line(f, width=150) for f in rows]


# --- страницы знаний --------------------------------------------------------------------------------------------

def pages_dir() -> Path:
    return config.DATA_DIR / "knowledge" / "pages"


def _safe(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|\n\r\t]+', " ", name).strip()[:80] or "без имени"


def page_list(limit: int = 50, q: str | None = None, kind: str | None = None) -> list[dict]:
    """Страницы по числу свежих фактов (30 дней), потом по числу всех."""
    since = (config.now().date() - timedelta(days=30)).isoformat()
    rows = store.rows(
        "SELECT e.*, COUNT(f.id) AS facts, SUM(CASE WHEN f.happened >= ? THEN 1 ELSE 0 END) AS fresh, "
        "MAX(f.happened) AS last, (SELECT COUNT(*) FROM chunk_entities ce WHERE ce.entity_id=e.id) AS mentions "
        "FROM entities e LEFT JOIN fact_entities fe ON fe.entity_id=e.id "
        "LEFT JOIN facts f ON f.id=fe.fact_id AND f.status='active' WHERE e.merged_into IS NULL "
        + ("AND e.kind=? " if kind else "") + "GROUP BY e.id ORDER BY fresh DESC, facts DESC, mentions DESC, e.name",
        [since, *([kind] if kind else [])])
    for r in rows:
        r["aliases"] = store.loads(r["aliases"], [])
        r["kind_title"] = ENTITY_KINDS.get(r["kind"], r["kind"])
    if q:
        key = _alias_key(q)
        rows = [r for r in rows if key in _alias_key(r["name"]) or any(key in _alias_key(a) for a in r["aliases"])]
    return rows[:limit]


def _entity_regex(e: dict) -> re.Pattern | None:
    pats = [p for p in (_alias_pattern(a) for a in dict.fromkeys([e["name"], *e["aliases"]])) if p]
    return re.compile("|".join(pats)) if pats else None


def page_data(ref: str | int, *, mcp: bool = False) -> dict | None:
    """Всё о сущности сейчас — из базы, без ИИ."""
    from opsdash import decisions, records

    e = entity(ref)
    if not e:
        return None
    rx = _entity_regex(e)
    all_facts = facts(about=e["id"], status="all", limit=500)
    active = [f for f in all_facts if f["status"] == "active"]
    by_kind: dict[str, list[dict]] = {}
    for f in active:
        by_kind.setdefault(f["kind"], []).append(f)
    history: dict[int, list[dict]] = {}  # у цифр и состояний с ключом — прежние значения
    for f in all_facts:
        if f["status"] == "replaced" and f.get("replaced_by"):
            history.setdefault(f["replaced_by"], []).append(f)
    tasks = []
    if rx:
        for c in store.rows("SELECT id, direction, who, whom, title, what, due, status FROM commitments "
                            "WHERE status='open' AND archived_at IS NULL"):
            if rx.search(norm(f"{c['title'] or ''} {c['what']} {c['who'] or ''} {c['whom'] or ''}")):
                tasks.append(c)
    decs = []
    if rx:
        for d in decisions.items(current=True, limit=2000):
            if rx.search(norm(f"{d['title']} {d.get('topic') or ''} {d.get('details') or ''}")):
                decs.append(d)
    meetings = store.rows(
        "SELECT DISTINCT m.id, m.title, m.headline, m.started_at FROM chunk_entities ce JOIN memory_chunks c "
        "ON c.id=ce.chunk_id JOIN meetings m ON m.id=c.src WHERE ce.entity_id=? AND c.kind IN ('meet','sum') "
        "AND COALESCE(m.private, 0)=0 ORDER BY m.started_at DESC LIMIT 8", (e["id"],))
    msgs = [] if mcp else store.rows(
        "SELECT c.id, c.title, c.ts, c.text, c.msg_ids FROM chunk_entities ce JOIN memory_chunks c ON c.id=ce.chunk_id "
        "WHERE ce.entity_id=? AND c.kind='msg' ORDER BY c.ts DESC LIMIT 4", (e["id"],))
    n_msgs = 0 if mcp else store.scalar(
        "SELECT COUNT(*) FROM chunk_entities ce JOIN memory_chunks c ON c.id=ce.chunk_id WHERE ce.entity_id=? "
        "AND c.kind='msg'", (e["id"],)) or 0
    person = store.row("SELECT * FROM people WHERE id=?", (e["person_id"],)) if e.get("person_id") else None
    project = store.row("SELECT * FROM projects WHERE id=?", (e["project_id"],)) if e.get("project_id") else None
    lead = store.row("SELECT * FROM pages WHERE entity_id=?", (e["id"],))
    return {"entity": e, "kind_title": ENTITY_KINDS.get(e["kind"], e["kind"]), "facts": active, "by_kind": by_kind,
            "history": history, "tasks": tasks, "decisions": decs[:8], "meetings": meetings, "messages": msgs,
            "n_messages": n_msgs, "person": person, "project": project, "lead": lead,
            "outdated": [f for f in all_facts if f["status"] in ("outdated",)][:5]}


def page_text(ref: str | int, *, mcp: bool = False, max_bytes: int = PAGE_MAX) -> str | None:
    from opsdash import recall, records

    d = page_data(ref, mcp=mcp)
    if not d:
        return None
    e = d["entity"]
    out = [f"# {e['name']} ({d['kind_title']})" + (f" · также: {', '.join(e['aliases'][:6])}" if e["aliases"] else "")]
    if d["person"]:
        p = d["person"]
        bits = [p.get("role") or ""]
        if not mcp:
            bits += [p.get("telegram") or "", p.get("email") or ""]
        if any(bits):
            out.append("Человек: " + " · ".join(b for b in bits if b))
    if d["project"]:
        out.append(f"Проект PULSE [{d['project']['id']}]: {d['project']['title']}"
                   + (f" — {d['project']['goal']}" if d["project"].get("goal") else ""))
    if d["lead"] and d["lead"].get("lead"):
        out.append(f"\n## Главное (выжимка на {_ddmm((d['lead']['lead_at'] or '')[:10])})\n{d['lead']['lead'].strip()}")
    if d["facts"]:
        out.append("\n## Факты (на дату — не «сейчас»)")
        for kind in FACT_KINDS:
            for f in d["by_kind"].get(kind, []):
                hist = sorted(d["history"].get(f["id"], []), key=lambda h: h["happened"] or "")
                line = f"- [ф{f['id']}] {FACT_KINDS[kind]}: {_short(f['text'], 220)} ({_ddmm(f['happened'])}"
                if hist:
                    line += "; было: " + " → ".join(f"{_short(h['text'], 60)} ({_ddmm(h['happened'])})" for h in hist[-2:])
                line += f") · {source_label(f, mcp=mcp)}"
                out.append(line)
    if d["tasks"]:
        out.append("\n## Открытые задачи")
        out += [f"- [{c['id']}] {records.route(c)}: «{records.headline(c)}»" + (f" · до {c['due']}" if c["due"] else "")
                for c in d["tasks"][:8]]
    if d["decisions"]:
        out.append("\n## Решения")
        out += [f"- [решение {r['id']}] {r['when']} · {r['title']}" for r in d["decisions"]]
    if d["meetings"]:
        out.append("\n## Встречи, где упоминается")
        out += [f"- {m['id']} · {recall._ddmm(m['started_at'])} · {m['title']}" for m in d["meetings"]]
    if d["messages"]:
        out.append(f"\n## Переписка (упоминаний: {d['n_messages']}, последние)")
        for c in d["messages"][:3]:
            ids = store.loads(c["msg_ids"], [])
            out.append(f"- [{ids[0] if ids else '?'}] {recall._ddmm(c['ts'])} · «{c['title']}»: "
                       f"{_short(c['text'].splitlines()[-1] if c['text'] else '', 150)}")
    text = "\n".join(out)
    while len(text.encode("utf-8")) > max_bytes and "\n" in text:
        text = text[:text.rfind("\n")]
    if len(text.encode("utf-8")) >= max_bytes - 200:
        text += f"\n… страница длиннее — факты по одному виду: ./ops facts list --about \"{e['name']}\" --kind <вид>"
    return text


def write_page(ref: str | int) -> Path | None:
    """data/knowledge/pages/<имя>.md — страницу можно открыть и без PULSE."""
    text = page_text(ref, max_bytes=60_000)
    if not text or store._query_only:
        return None
    e = entity(ref)
    p = pages_dir() / f"{_safe(e['name'])}.md"
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text + "\n", encoding="utf-8")
    except OSError:
        return None
    return p


def page_list_text(limit: int = 40, mcp: bool = False) -> str:
    rows = page_list(limit)
    if not rows:
        return "Страниц знаний пока нет: они появятся, когда PULSE запишет первые факты."
    out = [f"Страницы знаний ({len(rows)} самых живых): факты за 30 дней / всего · упоминаний"]
    for r in rows:
        out.append(f"- «{r['name']}» ({r['kind_title']}) · {r['fresh'] or 0} / {r['facts'] or 0} · {r['mentions'] or 0}"
                   + (f" · также: {', '.join(r['aliases'][:3])}" if r["aliases"] else ""))
    out.append('Страница: page("<имя>")' if mcp else 'Страница: ./ops page "<имя>"')
    return "\n".join(out)


# --- память дня и разовое наполнение ---------------------------------------------------------------------------

DAY_PART = 17_000  # байт на часть выдачи `memory day` вместе с шапкой: больше 20 КБ фоновый Claude не видит
DAY_KNOWN = 15     # фактов «Уже известно» в первой части
DAY_MSG = 280      # знаков сообщения
DAY_SKIP = re.compile(r"^(ок|окей|ok|да|нет|спасибо|спс|благодарю|понял|поняла|принято|хорошо|ага|угу|\+|👍|🙏|ясно)[.!)\s]*$",
                      re.IGNORECASE)


def day_messages(day: str) -> list[dict]:
    """Сообщения рабочих чатов за день: без чатов «не запоминать», коротких «ок» и пустых."""
    tz = config.tz()
    start = datetime.fromisoformat(day).replace(tzinfo=tz)
    a, b = store.to_utc_iso(start), store.to_utc_iso(start + timedelta(days=1))
    rows = store.rows(
        "SELECT m.* FROM messages m LEFT JOIN chats c ON c.source=m.source AND c.chat_id=m.chat_id "
        "WHERE m.ts >= ? AND m.ts < ? AND COALESCE(c.memory_off, 0)=0 AND m.text IS NOT NULL "
        "ORDER BY m.source, m.chat_id, m.ts", (a, b))
    return [r for r in rows if len((r["text"] or "").strip()) >= 12 and not DAY_SKIP.match(r["text"].strip())]


def day_text(day: str, part: int = 1) -> str:
    from opsdash import recall

    msgs = day_messages(day)
    if not msgs:
        return f"За {_ddmm(day)} в рабочих чатах сообщений нет — фактов не будет."
    chats: dict[tuple, list[dict]] = {}
    for m in msgs:
        chats.setdefault((m["source"], m["chat_id"], m["chat_title"]), []).append(m)
    known_text = ""
    eids = entities_for([m["text"] for m in msgs[:400]], list({m["sender_name"] for m in msgs if m["sender_name"]}))
    known = known_about(eids[:25], limit=DAY_KNOWN)
    if known:
        known_text = "Уже известно (сменяй по ключу, не повторяй):\n" + "\n".join(
            "  " + fact_line(f, width=110, about=False) + (f" · ключ «{f['key']}»" if f.get("key") else "") for f in known)
    room = DAY_PART - 400  # шапка и строка «Дальше»
    nb = lambda t: len(t.encode("utf-8")) + 1  # noqa: E731
    parts: list[list[str]] = []
    cur, size, started = [], nb(known_text), set()  # «Уже известно» — в первой части
    for (source, chat_id, title), items in chats.items():
        kind = store.scalar("SELECT kind FROM chats WHERE source=? AND chat_id=?", (source, chat_id)) or ""
        head, opened, prev = f"## «{title}» ({source}, {kind or 'чат'})", False, None
        for m in items:  # подряд от одного человека — имя один раз
            who = m["sender_name"] or ("Вы" if m["is_outgoing"] else "?")
            text = " ".join((m["text"] or "").split())
            text = text if len(text) <= DAY_MSG else text[:DAY_MSG - 1].rstrip(" ,.;") + "…"
            line = f"[{m['id']}] {store.local(m['ts'], '%H:%M')} " + (f"{who}: " if who != prev else "↳ ") + text
            prev = who
            if cur and size + nb(line) + nb(head) > room:  # часть полна — чат продолжится в следующей
                parts.append(cur)
                cur, size, opened = [], 0, False
            if not opened:
                h = head + " (продолжение)" if head in started else head
                started.add(head)
                cur += ["", h] if cur else [h]
                size += nb(h) + 1
                opened = True
            cur.append(line)
            size += nb(line)
    if cur:
        parts.append(cur)
    parts = [["\n".join(p)] for p in parts]
    part = max(1, min(part, len(parts)))
    head = [f"Память дня {_ddmm(day)}: чатов {len(chats)}, сообщений {len(msgs)} — часть {part} из {len(parts)}."]
    if part == 1 and known_text:
        head.append(known_text)
    text = "\n".join(head) + "\n\n" + parts[part - 1][0]
    if part < len(parts):
        text += f"\n\nДальше: ./ops memory day --date {day} --part {part + 1}"
    return text


def backfill_state() -> dict:
    done = set(store.get_state("memory_backfill_done") or [])
    todo = [m["id"] for m in store.rows(
        "SELECT id FROM meetings WHERE status='processed' AND COALESCE(private, 0)=0 ORDER BY started_at")
        if m["id"] not in done and (config.MEETINGS_DIR / m["id"] / "summary.md").exists()]
    return {"done": len(done), "todo": todo, "total": len(done) + len(todo),
            "state": store.get_state("memory_backfill") or "", "at": store.get_state("memory_backfill_at")}


def backfill_next(n: int = 10) -> list[dict]:
    todo = backfill_state()["todo"][:n]
    return [store.row("SELECT id, title, started_at FROM meetings WHERE id=?", (mid,)) for mid in todo]


def backfill_done(ids: list[str]) -> int:
    done = list(dict.fromkeys([*(store.get_state("memory_backfill_done") or []), *ids]))
    store.set_state("memory_backfill_done", done)
    return len(done)


# --- выжимки страниц ---------------------------------------------------------------------------------------------

LEAD_NEW = 10    # столько новых фактов с прошлой выжимки — пора писать заново
LEAD_TOP = 20    # и только у самых живых страниц


def leads_due() -> list[dict]:
    """Страницы, которым нужна выжимка: набралось LEAD_NEW новых фактов, выжимке больше суток, из LEAD_TOP живых."""
    since = (datetime.now().astimezone() - timedelta(days=1)).isoformat()
    out = []
    for r in page_list(LEAD_TOP):
        lead = store.row("SELECT * FROM pages WHERE entity_id=?", (r["id"],))
        n = r["facts"] or 0
        if n - ((lead or {}).get("lead_facts") or 0) >= LEAD_NEW and (not lead or (lead["lead_at"] or "") < since):
            out.append(r)
    return out


def set_leads(data: dict) -> list[str]:
    done = []
    for name, text in (data or {}).items():
        e = entity(name)
        text = (text or "").strip()
        if not e or not text:
            continue
        n = store.scalar("SELECT COUNT(*) FROM fact_entities fe JOIN facts f ON f.id=fe.fact_id WHERE fe.entity_id=? "
                         "AND f.status='active'", (e["id"],)) or 0
        store.execute("INSERT INTO pages(entity_id, lead, lead_at, lead_facts) VALUES(?,?,?,?) ON CONFLICT(entity_id) "
                      "DO UPDATE SET lead=excluded.lead, lead_at=excluded.lead_at, lead_facts=excluded.lead_facts",
                      (e["id"], text[:3000], store.utcnow(), n))
        done.append(e["name"])
    return done


# --- предложения memory.* ----------------------------------------------------------------------------------------

def execute_proposal(p: dict) -> dict:
    kind, pl = p["kind"], p["payload"]
    if kind == "memory.merge":
        return merge_entities(int(pl["entity_id"]), int(pl["into"]))
    if kind == "memory.fact":
        f = store.row("SELECT * FROM facts WHERE id=?", (pl["fact_id"],))
        if not f:
            raise MemoryError("Факта уже нет")
        now = store.utcnow()
        store.execute("UPDATE facts SET status='active', updated=? WHERE id=?", (now, f["id"]))
        if pl.get("replaces"):
            store.execute("UPDATE facts SET status='replaced', replaced_by=?, updated=? WHERE id=? AND status='active'",
                          (f["id"], now, pl["replaces"]))
        store.set_state("memory_facts_changed", now)
        return {"fact_id": f["id"]}
    raise MemoryError(f"неизвестное предложение {kind}")


def on_reject(p: dict) -> None:
    """«Нет» на факт от агента или смену факта «от вас» — факт не действует."""
    if p["kind"] == "memory.fact":
        store.execute("UPDATE facts SET status='wrong', reason=COALESCE(reason, 'отклонено'), updated=? "
                      "WHERE id=? AND status='proposed'", (store.utcnow(), p["payload"].get("fact_id")))


# --- цифры -----------------------------------------------------------------------------------------------------

def stats() -> dict:
    by_kind = {r["kind"]: r["n"] for r in store.rows(
        "SELECT kind, COUNT(*) AS n FROM facts WHERE status='active' GROUP BY kind")}
    by_status = {r["status"]: r["n"] for r in store.rows("SELECT status, COUNT(*) AS n FROM facts GROUP BY status")}
    links = {r["kind"]: r["n"] for r in store.rows("SELECT kind, COUNT(*) AS n FROM fact_links GROUP BY kind")}
    week = (config.now().date() - timedelta(days=7)).isoformat()
    return {
        "facts": by_status.get("active", 0), "by_kind": by_kind, "by_status": by_status, "links": links,
        "links_total": sum(links.values()),
        "entities": store.scalar("SELECT COUNT(*) FROM entities WHERE merged_into IS NULL") or 0,
        "sources": store.scalar("SELECT COUNT(DISTINCT source_type || ':' || COALESCE(source_ref, '')) FROM facts "
                                "WHERE status='active'") or 0,
        "week": store.scalar("SELECT COUNT(*) FROM facts WHERE created >= ?", (week,)) or 0,
        "today": store.scalar("SELECT COUNT(*) FROM facts WHERE created >= ?",
                              (store.to_utc_iso(datetime.combine(config.now().date(), datetime.min.time())),)) or 0,
        "agents": store.scalar("SELECT COUNT(*) FROM facts WHERE status='proposed' AND author='agent'") or 0,
        "chunks": store.scalar("SELECT COUNT(*) FROM memory_chunks") or 0,
        "vectors": store.scalar("SELECT COUNT(*) FROM memory_vectors") or 0,
    }


def day_summary(day: str | None = None) -> dict:
    """Для дайджеста: сколько узнал за день и что изменилось («было → стало»)."""
    day = day or _today()
    tz = config.tz()
    start = store.to_utc_iso(datetime.fromisoformat(day).replace(tzinfo=tz))
    end = store.to_utc_iso(datetime.fromisoformat(day).replace(tzinfo=tz) + timedelta(days=1))
    n = store.scalar("SELECT COUNT(*) FROM facts WHERE created >= ? AND created < ? AND status IN ('active','replaced')",
                     (start, end)) or 0
    changed = store.rows("SELECT f.id, f.text, f.key, o.text AS old FROM facts f JOIN facts o ON o.id=f.replaces_id "
                         "WHERE f.created >= ? AND f.created < ? AND f.status='active' ORDER BY f.id DESC LIMIT 3",
                         (start, end))
    return {"n": n, "changed": changed}


# --- файл-приёмник -----------------------------------------------------------------------------------------------

def inbox_path() -> Path:
    return config.DATA_DIR / "memory" / "inbox.jsonl"


def inbox_append(rec: dict) -> None:
    """Одна строка в приёмник: дописывается целиком одной записью (несколько сессий MCP пишут параллельно)."""
    p = inbox_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps({"at": store.utcnow(), **rec}, ensure_ascii=False) + "\n"
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)


HANDLERS: dict = {}


def _recall_line(rec: dict) -> str:
    from opsdash import recall

    recall.write_log(rec)
    return "recall"


HANDLERS["recall"] = _recall_line


def absorb_inbox() -> dict:
    """Разобрать приёмник: файл переименовываем и читаем — новые строки тем временем ложатся в новый файл."""
    if store._query_only:
        return {}
    p = inbox_path()
    if not p.exists() or not p.stat().st_size:
        return {}
    work = p.with_name(f"inbox.{int(time.time() * 1000)}.work")
    try:
        p.replace(work)
    except OSError:
        return {}
    done: dict[str, int] = {}
    for line in work.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
            kind = HANDLERS[rec.get("type")](rec)
        except Exception as e:  # noqa: BLE001 — одна плохая строка не должна терять остальные
            store.log("memory", f"приёмник: строка не разобрана ({e}): {line[:300]}", "warning")
            kind = "error"
        done[kind] = done.get(kind, 0) + 1
    work.unlink(missing_ok=True)
    return done


# --- связи фактов (этап 3) ---------------------------------------------------------------------------------------

LINK_KINDS = {"entity": "общая сущность", "temporal": "по времени", "semantic": "по смыслу", "causal": "причина"}
ENTITY_NEIGHBOURS = 6   # у сущности с сотней фактов каждый связан с 6 ближайшими по времени, а не со всеми
TEMPORAL_NEIGHBOURS = 6
TEMPORAL_DAYS = 1


def build_links() -> dict:
    """Пересчитать связи фактов без ИИ: общая сущность, одно время (тот же источник или ±1 день), смысл (по готовым
    векторам — embed.semantic_pairs), причина (because). Только действующие факты."""
    if store._query_only:
        return {}
    facts_ = {r["id"]: r for r in store.rows(
        "SELECT id, happened, source_type, source_ref, because_id, created FROM facts WHERE status='active'")}
    out: dict[tuple[int, int, str], float] = {}

    def add(a: int, b: int, kind: str, w: float) -> None:
        if a == b or a not in facts_ or b not in facts_:
            return
        a, b = sorted((a, b))
        out[(a, b, kind)] = max(out.get((a, b, kind), 0.0), round(w, 3))

    def day(fid: int) -> int:
        h = facts_[fid]["happened"] or (facts_[fid]["created"] or "")[:10]
        try:
            return date.fromisoformat(h).toordinal()
        except ValueError:
            return 0

    by_entity: dict[int, list[int]] = {}
    for r in store.rows("SELECT fact_id, entity_id FROM fact_entities"):
        if r["fact_id"] in facts_:
            by_entity.setdefault(r["entity_id"], []).append(r["fact_id"])
    shared: dict[tuple[int, int], int] = {}
    for ids in by_entity.values():
        ids.sort(key=day)
        for i, a in enumerate(ids):  # ближайшие по времени соседи в той же сущности
            for b in ids[max(0, i - ENTITY_NEIGHBOURS // 2): i + ENTITY_NEIGHBOURS // 2 + 1]:
                if a < b:
                    shared[(a, b)] = shared.get((a, b), 0) + 1
    for (a, b), n in shared.items():
        add(a, b, "entity", min(1.0, 0.5 + 0.25 * n))
    by_source: dict[str, list[int]] = {}
    for fid, f in facts_.items():
        if f["source_type"] and f["source_ref"]:
            key = f"{f['source_type']}:{f['source_ref']}" if f["source_type"] == "meeting" else f"day:{f['happened']}"
            by_source.setdefault(key, []).append(fid)
    for ids in by_source.values():  # один источник: встреча — все со всеми ближайшими; переписка — тот же день
        ids.sort()
        for i, a in enumerate(ids):
            for b in ids[i + 1: i + 1 + TEMPORAL_NEIGHBOURS]:
                add(a, b, "temporal", 1.0)
    ordered = sorted(facts_, key=day)
    for i, a in enumerate(ordered):  # разные источники в пределах суток — чем ближе, тем сильнее
        for b in ordered[i + 1: i + 1 + TEMPORAL_NEIGHBOURS]:
            gap = abs(day(b) - day(a))
            if gap > TEMPORAL_DAYS:
                break
            add(a, b, "temporal", 1.0 / (1 + gap))
    for fid, f in facts_.items():
        if f["because_id"]:
            add(fid, f["because_id"], "causal", 1.0)
    semantic = 0
    try:
        from opsdash import embed

        if embed.enabled():
            for a, b, s in embed.semantic_pairs():
                add(a, b, "semantic", s)
                semantic += 1
    except Exception as e:  # noqa: BLE001 — без модели и векторов связей по смыслу просто нет
        store.log("memory", f"связи по смыслу: {e}", "warning")
    with store.db() as conn:
        conn.execute("DELETE FROM fact_links")
        conn.executemany("INSERT INTO fact_links(a, b, kind, weight) VALUES(?,?,?,?)",
                         [(a, b, k, w) for (a, b, k), w in out.items()])
    store.set_state("memory_links_at", store.utcnow())
    counts: dict[str, int] = {}
    for (_, _, k) in out:
        counts[k] = counts.get(k, 0) + 1
    return counts


def links_stale() -> bool:
    return (store.get_state("memory_facts_changed") or "") > (store.get_state("memory_links_at") or "") \
        or (store.get_state("memory_vectors_at") or "") > (store.get_state("memory_links_at") or "")


# --- карта ------------------------------------------------------------------------------------------------------

def fact_graph(*, days: int = 30, kind: str | None = None, about: str | int | None = None,
               source: str | None = None, limit: int = 300) -> dict:
    """«Созвездие»: факты-точки и связи. По умолчанию — limit самых свежих и связанных; фильтры — период, вид, сущность,
    источник (meeting / messages / director / agent)."""
    where, params = ["f.status='active'"], []
    if days:
        where.append("f.happened >= ?")
        params.append((config.now().date() - timedelta(days=days)).isoformat())
    if kind:
        where.append("f.kind=?")
        params.append(kind)
    if about:
        e = entity(about)
        if not e:
            return {"nodes": [], "links": [], "total": 0}
        where.append("f.id IN (SELECT fact_id FROM fact_entities WHERE entity_id=?)")
        params.append(e["id"])
    if source == "meeting":
        where.append("f.source_type='meeting'")
    elif source == "messages":
        where.append(f"f.source_type IN ({','.join('?' * len(MESSAGE_SOURCES))})")
        params += list(MESSAGE_SOURCES)
    elif source in ("director", "agent"):
        where.append("f.author=?")
        params.append(source)
    rows = store.rows(
        "SELECT f.id, f.kind, f.text, f.happened, f.created, f.source_type, f.author, "
        "(SELECT COUNT(*) FROM fact_links l WHERE l.a=f.id OR l.b=f.id) AS deg FROM facts f WHERE "
        + " AND ".join(where), params)
    total = len(rows)
    today = config.now().date().toordinal()

    def rank(r: dict) -> float:
        try:
            age = today - date.fromisoformat(r["happened"] or r["created"][:10]).toordinal()
        except ValueError:
            age = 365
        return r["deg"] * 1.0 - age * 0.6

    rows.sort(key=rank, reverse=True)
    rows = rows[:limit]
    ids = {r["id"] for r in rows}
    names: dict[int, list[str]] = {}
    if ids:
        marks = ",".join("?" * len(ids))
        for r in store.rows(f"SELECT fe.fact_id, e.name FROM fact_entities fe JOIN entities e ON e.id=fe.entity_id "
                            f"WHERE fe.fact_id IN ({marks})", list(ids)):
            names.setdefault(r["fact_id"], []).append(r["name"])
        links = store.rows(f"SELECT a, b, kind, weight FROM fact_links WHERE a IN ({marks}) AND b IN ({marks})",
                           [*ids, *ids])
    else:
        links = []
    fresh = store.to_utc_iso(datetime.now().astimezone() - timedelta(hours=24))
    nodes = [{"id": r["id"], "kind": r["kind"], "text": _short(r["text"], 160), "label": _short(r["text"], 46),
              "deg": r["deg"], "day": _ddmm(r["happened"]), "about": names.get(r["id"], [])[:3],
              "new": (r["created"] or "") >= fresh, "src": r["source_type"] or "", "me": r["author"] == "director"}
             for r in rows]
    return {"nodes": nodes, "links": [[l["a"], l["b"], l["kind"], l["weight"]] for l in links], "total": total,
            "kinds": LINK_KINDS}


def fact_card(fid: int) -> dict | None:
    f = fact(fid)
    if not f:
        return None
    nb = store.rows("SELECT CASE WHEN l.a=? THEN l.b ELSE l.a END AS other, l.kind, l.weight FROM fact_links l "
                    "WHERE l.a=? OR l.b=? ORDER BY l.weight DESC LIMIT 40", (fid, fid, fid))
    texts = {r["id"]: r["text"] for r in store.rows(
        f"SELECT id, text FROM facts WHERE id IN ({','.join('?' * len(nb)) or 'NULL'})", [n["other"] for n in nb])}
    return {"id": f["id"], "text": f["text"], "kind": f["kind_title"], "day": _ddmm(f["happened"]),
            "status": f["status_title"], "key": f.get("key"), "quote": f.get("quote") or "",
            "source": source_label(f), "source_link": f"/meetings/{f['source_ref']}" if f["source_type"] == "meeting" else "",
            "entities": f["entities"], "prev": f["prev"]["text"] if f.get("prev") else "",
            "because": f["because"]["text"] if f.get("because") else "",
            "neighbours": [{"id": n["other"], "kind": n["kind"], "kind_title": LINK_KINDS.get(n["kind"], n["kind"]),
                            "text": _short(texts.get(n["other"], ""), 120)} for n in nb if n["other"] in texts]}


def entity_graph(*, kind: str | None = None, limit: int = 300) -> dict:
    """«Сущности»: точки — сущности, линии — совместные упоминания в фактах, задачах, встречах и решениях; толщина —
    сколько раз, свежесть — когда в последний раз."""
    from opsdash import decisions

    ents = page_list(limit * 2, kind=kind)
    ents = sorted(ents, key=lambda r: -((r["facts"] or 0) * 3 + (r["mentions"] or 0)))[:limit]
    ids = {e["id"] for e in ents}
    pair: dict[tuple[int, int], dict] = {}

    def bump(group: set[int], when: str | None) -> None:
        group = sorted(group & ids)
        for i, a in enumerate(group):
            for b in group[i + 1:]:
                p = pair.setdefault((a, b), {"n": 0, "last": ""})
                p["n"] += 1
                p["last"] = max(p["last"], when or "")

    by_fact: dict[int, set[int]] = {}
    for r in store.rows("SELECT fe.fact_id, fe.entity_id, f.happened FROM fact_entities fe JOIN facts f "
                        "ON f.id=fe.fact_id WHERE f.status='active'"):
        by_fact.setdefault(r["fact_id"], set()).add(r["entity_id"])
        by_fact.setdefault(-r["fact_id"], set()).add(r["happened"] or "")
    for fid, group in by_fact.items():
        if fid > 0:
            bump(group, next(iter(by_fact.get(-fid, {""}))))
    by_meeting: dict[str, set[int]] = {}
    when: dict[str, str] = {}
    for r in store.rows("SELECT c.src, c.ts, ce.entity_id FROM chunk_entities ce JOIN memory_chunks c "
                        "ON c.id=ce.chunk_id WHERE c.kind IN ('meet','sum')"):
        by_meeting.setdefault(r["src"], set()).add(r["entity_id"])
        when[r["src"]] = (r["ts"] or "")[:10]
    for src, group in by_meeting.items():
        bump(group, when.get(src))
    rx = _matcher()
    if rx[0]:
        for c in store.rows("SELECT title, what, updated FROM commitments WHERE archived_at IS NULL"):
            bump(entities_in(c["title"], c["what"], matcher=rx), (c["updated"] or "")[:10])
        for d in decisions.items(current=True, limit=1000):
            bump(entities_in(d["title"], d.get("details"), matcher=rx), d.get("when"))
    # у каждой сущности — её 6 самых частых соседей (и все пары, что встречались 3 раза и больше): иначе «Склад» и
    # «Олег» связаны со всеми и карта превращается в клубок
    edges_all = sorted(pair.items(), key=lambda x: -x[1]["n"])
    per: dict[int, int] = {}
    edges = []
    for (a, b), p in edges_all:
        if p["n"] >= 3 or per.get(a, 0) < 6 or per.get(b, 0) < 6:
            edges.append(((a, b), p))
            per[a] = per.get(a, 0) + 1
            per[b] = per.get(b, 0) + 1
        if len(edges) >= 2500:
            break
    nodes = [{"id": e["id"], "name": e["name"], "kind": e["kind"], "kind_title": e["kind_title"],
              "facts": e["facts"] or 0, "mentions": e["mentions"] or 0} for e in ents]
    return {"nodes": nodes, "links": [[a, b, p["n"], _ddmm(p["last"]), p["last"]] for (a, b), p in edges],
            "kinds": ENTITY_KINDS}


def numbers() -> dict:
    """«Память в цифрах»: фактов, источников, связей; состав по видам фактов и связей."""
    st = stats()
    by_kind = [(FACT_KINDS[k], st["by_kind"].get(k, 0)) for k in FACT_KINDS]
    by_link = [(LINK_KINDS[k], st["links"].get(k, 0)) for k in LINK_KINDS]
    return {**st, "kind_rows": by_kind, "link_rows": by_link,
            "kind_max": max([n for _, n in by_kind] or [1]) or 1, "link_max": max([n for _, n in by_link] or [1]) or 1}


# --- от агентов: remember и task_note через приёмник --------------------------------------------------------------

def _remember(rec: dict) -> str:
    """Агент выяснил по ходу работы — факт «от агента»: в «От агентов» на «Да» или сразу (Автономия)."""
    from opsdash import proposals

    text = " ".join(str(rec.get("text") or "").split())
    if not text:
        raise MemoryError("пустой факт")
    if why := looks_secret(text, rec.get("why")):
        store.log("memory", f"от агента не записано — {why}", "warning")
        return "secret"
    about = rec.get("about") or []
    rep = _new_report()
    kind = rec.get("kind") if rec.get("kind") in FACT_KINDS else "how"
    session = str(rec.get("session") or "")[:80] or None
    auto = config.load_settings()["autonomy"].get("memory_agents") == "auto"
    fid = add_fact(text=text, kind=kind, about=about, source="agent", quote=(rec.get("why") or "")[:500] or None,
                   by="agent", session=session, report=rep)
    if not fid:
        return "skipped"
    if fid in rep["added"] and not auto:
        store.execute("UPDATE facts SET status='proposed' WHERE id=?", (fid,))
        proposals.propose_memory_fact(fact_id=fid, reason=(rec.get("why") or "прислал агент")[:300],
                                      batch=f"agents:{store.utcnow()[:10]}")
    return "remember"


def _task_note(rec: dict) -> str:
    """Агент сообщил, что сделал по задаче — предложение «Дописать в ход задачи» (директор подтверждает)."""
    from opsdash import proposals

    tid = int(rec.get("id") or 0)
    text = " ".join(str(rec.get("text") or "").split())
    if not tid or not text:
        raise MemoryError("нужны id задачи и текст")
    c = store.row("SELECT id, status FROM commitments WHERE id=?", (tid,))
    if not c or c["status"] != "open":
        return "skipped"
    session = str(rec.get("session") or "")[:80]
    proposals.propose_task_log(commitment_id=tid, text=f"Агент: {text}", source_ref=f"agent:{session or 'mcp'}",
                               reason="прислал агент из сессии Claude Code", batch=f"agents:{store.utcnow()[:10]}")
    return "task_note"


HANDLERS["remember"] = _remember
HANDLERS["task_note"] = _task_note
