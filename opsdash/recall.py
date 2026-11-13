"""«Вспомнить» (фича «Память», этап 1): один поиск по всему, что знает PULSE.

Порядок выдачи — от плотного к сырому: страница и факты (opsdash.memory), задачи с ходом и решения, потом фрагменты
сырого текста: встречи (разборы и расшифровки), переписка, документы, прошлые сессии агентов.

Сырой текст — индекс фрагментов `memory_chunks` + FTS5 `memory_fts` по основам слов (как в docs: окончания
отрезаем, запрос — через OR, порядок — BM25):
- переписка — окна до 900 знаков внутри одного чата; пауза больше 45 минут — новое окно; сообщение длиннее 700
  знаков — отдельно, кусками по 1000 с нахлёстом 150;
- расшифровки — окна по 1000 знаков с меткой времени первой реплики (одна реплика внахлёст);
- разборы встреч — по разделам «## …».
Личные встречи (`meetings.private`) в индекс не попадают. Индекс дополняется сам: задание `memory-index` после
сбора сообщений и расшифровки, а `./ops recall` перед поиском дочитывает новое (это миллисекунды).

Если PULSE держит модель эмбеддингов (opsdash.embed), к словам добавляется поиск по смыслу; две выдачи сливаются
по рангам (RRF, k = 60, по 50 лучших). Без модели — только слова.

Каждый вызов пишется в журнал `recall_log`: кто спросил, запрос, сколько нашлось и каким способом. MCP-сервер
базу не пишет — его строки журнала идут через файл-приёмник data/memory/inbox.jsonl (memory.absorb_inbox).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import unicodedata
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from opsdash import config, store

KINDS = ("facts", "tasks", "decisions", "meetings", "messages", "docs", "sessions")
KIND_TITLES = {"facts": "Факты", "tasks": "Задачи", "decisions": "Решения", "meetings": "Встречи",
               "messages": "Переписка", "docs": "Документы", "sessions": "Сессии агентов"}
BUDGETS = {"low": 3_000, "mid": 6_000, "high": 18_000}  # байт UTF-8: потолок выдачи — 20 КБ (pulse-skill-output-size)
QUOTAS = {  # сколько строк каждого вида при этом бюджете (лишнее — «Ещё N: …»)
    "low": {"facts": 3, "tasks": 3, "decisions": 2, "meetings": 2, "messages": 2, "docs": 1, "sessions": 1},
    "mid": {"facts": 6, "tasks": 5, "decisions": 3, "meetings": 4, "messages": 4, "docs": 2, "sessions": 2},
    "high": {"facts": 15, "tasks": 12, "decisions": 8, "meetings": 10, "messages": 12, "docs": 5, "sessions": 4},
}

WINDOW = 900            # знаков в окне переписки
GAP_SEC = 45 * 60       # пауза в чате длиннее — новое окно
LONG = 700              # сообщение длиннее — отдельные куски
PIECE, OVERLAP = 1000, 150
MEET_WINDOW = 1000
RRF_K = 60
ARM_K = 50              # сколько берём из каждого поиска перед слиянием
LINE_MAX = 230          # знаков в строке выдачи

CHUNK_KINDS = {"msg": "переписка", "meet": "расшифровка", "sum": "разбор"}
_LINE_RE = re.compile(r"^\[(\d\d:\d\d:\d\d)\]")


class RecallError(Exception):
    pass


# --- слова --------------------------------------------------------------------------------------------------------

def norm(text: str | None) -> str:
    return unicodedata.normalize("NFC", text or "").lower().replace("ё", "е")


def words(text: str | None) -> list[str]:
    from opsdash import docs

    return [w for w in re.findall(r"\w+", norm(text)) if len(w) >= 2 and w not in docs.STOPWORDS]


def stem(word: str) -> str:
    from opsdash import docs

    return docs._stem(word)


def stems(text: str | None) -> list[str]:
    """Основы слов запроса без повторов: «переезда склада» → «переезд», «склад»."""
    return list(dict.fromkeys(stem(w) if len(w) >= 3 else w for w in words(text)))


def fts_query(text: str | None, mode: str = "OR") -> str:
    """Запрос FTS5: основы слов с «*» (любое окончание), короткие слова — целиком."""
    terms = [f'"{s}"*' if len(s) >= 3 else f'"{s}"' for s in stems(text)]
    return f" {mode} ".join(terms)


def score(text: str | None, want: list[str]) -> int:
    """Сколько основ запроса есть в тексте (без учёта формы слова)."""
    low = norm(text)
    return sum(1 for s in want if s in low)


# --- нарезка -----------------------------------------------------------------------------------------------------

def pieces(text: str, size: int = PIECE, overlap: int = OVERLAP) -> list[str]:
    """Длинный текст — кусками по size знаков с нахлёстом; режем по концу строки или предложения, если он есть."""
    text = text.strip()
    if len(text) <= size:
        return [text] if text else []
    out, start = [], 0
    while start < len(text):
        end = min(len(text), start + size)
        if end < len(text):
            cut = max(text.rfind("\n", start + size // 2, end), text.rfind(". ", start + size // 2, end))
            if cut > 0:
                end = cut + 1
        out.append(text[start:end].strip())
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return [p for p in out if p]


def _epoch(ts: str | None) -> float:
    dt = store.parse_ts(ts)
    return dt.timestamp() if dt else 0.0


def message_chunks(rows: list[dict]) -> list[dict]:
    """Окна переписки одного или нескольких чатов (rows отсортированы по чату и времени)."""
    out: list[dict] = []
    cur: dict | None = None

    def flush():
        nonlocal cur
        if cur and cur["lines"]:
            out.append({"kind": "msg", "src": cur["src"], "title": cur["title"], "ts": cur["ts"],
                        "pos": f"[{cur['ids'][0]}]", "text": "\n".join(cur["lines"]), "msg_ids": cur["ids"]})
        cur = None

    for r in rows:
        text = (r.get("text") or "").strip()
        if not text:
            continue
        src = f"{r['source']}:{r['chat_id']}"
        title = r.get("chat_title") or src
        who = r.get("sender_name") or ("Вы" if r.get("is_outgoing") else "Собеседник")
        t = _epoch(r["ts"])
        if len(text) > LONG:
            flush()
            for p in pieces(text):
                out.append({"kind": "msg", "src": src, "title": title, "ts": r["ts"], "pos": f"[{r['id']}]",
                            "text": f"{who}: {p}", "msg_ids": [r["id"]]})
            continue
        line = f"{who}: {' '.join(text.split())}"
        if cur and (cur["src"] != src or t - cur["last"] > GAP_SEC or cur["size"] + len(line) > WINDOW):
            flush()
        if not cur:
            cur = {"src": src, "title": title, "ts": r["ts"], "lines": [], "ids": [], "size": 0, "last": t}
        cur["lines"].append(line)
        cur["ids"].append(r["id"])
        cur["size"] += len(line) + 1
        cur["last"] = t
    flush()
    return out


def meeting_chunks(mid: str, title: str, ts: str | None, transcript: str, summary: str) -> list[dict]:
    """Окна расшифровки (по 1000 знаков, метка времени первой реплики) и разделы разбора встречи."""
    out: list[dict] = []
    base = {"src": mid, "title": title, "ts": ts, "msg_ids": []}
    lines = [ln.strip() for ln in transcript.splitlines() if _LINE_RE.match(ln.strip())]
    win: list[str] = []
    size = 0
    for ln in lines:
        if win and size + len(ln) > MEET_WINDOW:
            out.append({**base, "kind": "meet", "pos": "[" + _LINE_RE.match(win[0]).group(1) + "]",
                        "text": "\n".join(win)})
            win, size = [win[-1]], len(win[-1])  # одна реплика внахлёст
        win.append(ln)
        size += len(ln) + 1
    if win:
        out.append({**base, "kind": "meet", "pos": "[" + _LINE_RE.match(win[0]).group(1) + "]", "text": "\n".join(win)})
    for sec in re.split(r"\n(?=## )", summary or ""):
        sec = sec.strip()
        if len(sec) < 40:
            continue
        head = sec.splitlines()[0].lstrip("# ").strip()
        for p in pieces(sec, 1200, 100):
            out.append({**base, "kind": "sum", "pos": head[:60], "text": p})
    return out


def _hash(c: dict) -> str:
    return hashlib.sha1(f"{c['kind']}\n{c['title']}\n{c['pos']}\n{c['text']}".encode("utf-8")).hexdigest()


# --- индекс ------------------------------------------------------------------------------------------------------

def _private_backfill(conn) -> None:
    """Один раз: встречи, разбор которых озаглавлен «Личная встреча», — личные (до флага они находились поиском)."""
    if conn.execute("SELECT 1 FROM state WHERE key='memory_private_backfill'").fetchone():
        return
    for m in conn.execute("SELECT id FROM meetings WHERE COALESCE(private, 0)=0").fetchall():
        p = config.MEETINGS_DIR / m["id"] / "summary.md"
        try:
            head = p.read_text(encoding="utf-8", errors="replace")[:200] if p.exists() else ""
        except OSError:
            head = ""
        if re.match(r"\s*#\s*Личн", head):
            conn.execute("UPDATE meetings SET private=1 WHERE id=?", (m["id"],))
    conn.execute("INSERT OR REPLACE INTO state(key, value) VALUES('memory_private_backfill', 'true')")


def _put(conn, chunks: list[dict], src: str, kinds: tuple[str, ...]) -> tuple[int, int]:
    """Заменить фрагменты источника src: неизменные (по hash) остаются со своим id — и со своим вектором."""
    marks = ",".join("?" for _ in kinds)
    have = {r["hash"]: r["id"] for r in conn.execute(
        f"SELECT id, hash FROM memory_chunks WHERE src=? AND kind IN ({marks})", (src, *kinds))}
    keep, added = set(), 0
    for c in chunks:
        h = _hash(c)
        if h in have and have[h] not in keep:
            keep.add(have[h])
            continue
        cur = conn.execute("INSERT INTO memory_chunks(kind, src, title, ts, pos, text, msg_ids, hash) "
                           "VALUES(?,?,?,?,?,?,?,?)",
                           (c["kind"], c["src"], c["title"], c["ts"], c["pos"], c["text"],
                            json.dumps(c["msg_ids"]), h))
        conn.execute("INSERT INTO memory_fts(rowid, body, title) VALUES(?,?,?)",
                     (cur.lastrowid, norm(c["text"]), norm(c["title"])))
        keep.add(cur.lastrowid)
        added += 1
    gone = [i for i in have.values() if i not in keep]
    _drop(conn, gone)
    return added, len(gone)


def _drop(conn, ids: list[int]) -> None:
    for i in range(0, len(ids), 500):
        part = ids[i:i + 500]
        marks = ",".join("?" for _ in part)
        conn.execute(f"DELETE FROM memory_fts WHERE rowid IN ({marks})", part)
        conn.execute(f"DELETE FROM chunk_entities WHERE chunk_id IN ({marks})", part)
        conn.execute(f"DELETE FROM memory_chunks WHERE id IN ({marks})", part)


def _meeting_sig(m: dict) -> str:
    folder = config.MEETINGS_DIR / m["id"]
    parts = [str(m.get("private") or 0), m.get("title") or "", m.get("headline") or ""]
    for name in ("transcript.md", "summary.md"):
        try:
            st = (folder / name).stat()
            parts.append(f"{st.st_mtime_ns}:{st.st_size}")
        except OSError:
            parts.append("-")
    return "|".join(parts)


def meeting_title(m: dict) -> str:
    return (m.get("title") or m["id"]) + (f" — {m['headline']}" if m.get("headline") else "")


def refresh(full: bool = False) -> dict:
    """Дочитать в индекс новое: сообщения после прошлой отметки (чат нарезается заново, неизменные окна остаются),
    встречи с изменившимися файлами, личные — убрать. full — пересобрать всё."""
    if store._query_only:  # MCP-сервер: только читает
        return {}
    t0 = time.time()
    stats = {"added": 0, "removed": 0, "chats": 0, "meetings": 0}
    with store.db() as conn:
        conn.execute("BEGIN IMMEDIATE")  # два процесса (PULSE и ./ops recall) не режут один чат одновременно
        _private_backfill(conn)
        if full:
            conn.execute("DELETE FROM memory_chunks")
            conn.execute("DELETE FROM memory_fts")
            conn.execute("DELETE FROM chunk_entities")
            conn.execute("DELETE FROM state WHERE key IN ('memory_msg_id', 'memory_meetings')")
        last = json.loads((conn.execute("SELECT value FROM state WHERE key='memory_msg_id'").fetchone() or ["0"])[0])
        top = conn.execute("SELECT MAX(id) FROM messages").fetchone()[0] or 0
        if top > last:
            chats = conn.execute("SELECT DISTINCT source, chat_id FROM messages WHERE id > ?", (last,)).fetchall()
            for ch in chats:
                rows = [dict(r) for r in conn.execute(
                    "SELECT id, source, chat_id, chat_title, sender_name, is_outgoing, text, ts FROM messages "
                    "WHERE source=? AND chat_id=? AND text IS NOT NULL AND trim(text) <> '' ORDER BY ts, id",
                    (ch["source"], ch["chat_id"]))]
                a, r = _put(conn, message_chunks(rows), f"{ch['source']}:{ch['chat_id']}", ("msg",))
                stats["added"] += a
                stats["removed"] += r
                stats["chats"] += 1
            conn.execute("INSERT OR REPLACE INTO state(key, value) VALUES('memory_msg_id', ?)", (json.dumps(top),))
        sigs = json.loads((conn.execute("SELECT value FROM state WHERE key='memory_meetings'").fetchone()
                           or ["{}"])[0])
        meetings = [dict(m) for m in conn.execute(
            "SELECT id, title, headline, started_at, private FROM meetings")]
        alive = set()
        for m in meetings:
            alive.add(m["id"])
            sig = _meeting_sig(m)
            if sigs.get(m["id"]) == sig:
                continue
            chunks = []
            if not m.get("private"):
                folder = config.MEETINGS_DIR / m["id"]
                read = (lambda name: (folder / name).read_text(encoding="utf-8", errors="replace")
                        if (folder / name).exists() else "")
                chunks = meeting_chunks(m["id"], meeting_title(m), m["started_at"], read("transcript.md"),
                                        read("summary.md"))
            a, r = _put(conn, chunks, m["id"], ("meet", "sum"))
            stats["added"] += a
            stats["removed"] += r
            stats["meetings"] += 1
            sigs[m["id"]] = sig
        for mid in [k for k in sigs if k not in alive]:  # встречу удалили
            ids = [r[0] for r in conn.execute("SELECT id FROM memory_chunks WHERE src=? AND kind IN ('meet','sum')",
                                             (mid,))]
            _drop(conn, ids)
            stats["removed"] += len(ids)
            sigs.pop(mid)
        conn.execute("INSERT OR REPLACE INTO state(key, value) VALUES('memory_meetings', ?)",
                     (json.dumps(sigs, ensure_ascii=False),))
    if stats["added"] or stats["removed"]:
        try:  # сущности в новых фрагментах — по словарю псевдонимов (этап 2)
            from opsdash import memory

            memory.link_chunks()
        except Exception as e:  # noqa: BLE001 — поиск по словам работает и без связи со страницами
            store.log("memory", f"сущности во фрагментах: {e}", "warning")
    stats["ms"] = round((time.time() - t0) * 1000)
    return stats


def index_stats() -> dict:
    rows = store.rows("SELECT kind, COUNT(*) AS n, SUM(length(text)) AS chars FROM memory_chunks GROUP BY kind")
    return {r["kind"]: {"n": r["n"], "chars": r["chars"] or 0} for r in rows}


def forget_meeting(meeting_id: str) -> None:
    """Встречу отметили личной или удалили — её фрагменты уходят из индекса сразу."""
    with store.db() as conn:
        ids = [r[0] for r in conn.execute("SELECT id FROM memory_chunks WHERE src=? AND kind IN ('meet','sum')",
                                         (meeting_id,))]
        _drop(conn, ids)


# --- поиск фрагментов --------------------------------------------------------------------------------------------

def fts_chunks(query: str, *, kinds: tuple[str, ...] = ("msg", "meet", "sum"), k: int = ARM_K,
               since: str | None = None, about: str | None = None, src: str | None = None,
               chat: str | None = None) -> list[int]:
    """id фрагментов по убыванию BM25. about — обязательные слова (кто или что); src — префикс источника
    («telegram:» или id встречи); chat — часть названия чата или встречи."""
    q = fts_query(query)
    if not q:
        return []
    if about and (a := fts_query(about)):
        q = f"({q}) AND ({a})"
    where, params = [f"c.kind IN ({','.join('?' for _ in kinds)})"], list(kinds)
    if since:
        where.append("c.ts >= ?")
        params.append(since)
    if src:
        where.append("c.src LIKE ?")
        params.append(src + "%")
    if chat:
        where.append("c.title LIKE ?")
        params.append(f"%{chat}%")
    try:
        rows = store.rows(
            f"SELECT c.id FROM memory_fts f JOIN memory_chunks c ON c.id = f.rowid WHERE memory_fts MATCH ? "
            f"AND {' AND '.join(where)} ORDER BY bm25(memory_fts, 1.0, 0.3) LIMIT ?", (q, *params, k))
    except Exception as e:  # noqa: BLE001 — синтаксис FTS5 от необычного запроса: лучше пусто, чем падение
        store.log("recall", f"FTS5: {e}", "warning")
        return []
    return [r["id"] for r in rows]


def rrf(*arms: list, weights: tuple | None = None) -> list:
    """Слияние выдач по рангам: вес / (k + место) из каждой."""
    score_: dict = defaultdict(float)
    for n, arm in enumerate(arms):
        w = weights[n] if weights else 1.0
        for rank, key in enumerate(arm or []):
            score_[key] += w / (RRF_K + rank + 1)
    return sorted(score_, key=lambda x: -score_[x])


# вес поиска по смыслу при слиянии со словами. Встречи слова находят и так (94%): смысл с весом 0,1 только добавляет
# то, чего слова не нашли, после них; при весе 0,2–1 замер на копии давал 92%, то есть хуже одних слов
SEMANTIC_WEIGHT = {("msg",): 1.0, ("meet", "sum"): 0.1}


def hybrid(words_arm: list[int], sem_arm: list[int], kinds: dict[int, str]) -> list[int]:
    """Слова + смысл отдельно для переписки и для встреч (расшифровка и разбор — вместе, как у слов), с весами
    SEMANTIC_WEIGHT. Замер на копии (13.11.2026): переписка 32% → 40%, встречи 94% → 94%."""
    lists = []
    for group, weight in SEMANTIC_WEIGHT.items():
        w = [i for i in words_arm if kinds.get(i) in group]
        v = [i for i in sem_arm if kinds.get(i) in group]
        if w or v:
            lists.append(rrf(w[:ARM_K], v[:ARM_K], weights=(1.0, weight)) + w[ARM_K:])
    return _interleave(lists)


def _interleave(lists: list[list[int]]) -> list[int]:
    """Списки разных видов — в одну выдачу: место каждого вида сохраняется (render всё равно делит по видам)."""
    out, seen = [], set()
    for i in range(max((len(x) for x in lists), default=0)):
        for lst in lists:
            if i < len(lst) and lst[i] not in seen:
                seen.add(lst[i])
                out.append(lst[i])
    return out


def _semantic(query: str) -> dict | None:
    """Ближайшие по смыслу фрагменты и факты — у работающего PULSE (он держит модель, opsdash.embed)."""
    try:
        from opsdash import embed

        return embed.neighbours(query, ARM_K)
    except Exception:  # noqa: BLE001 — без модели и без PULSE — только слова
        return None


def chunks(ids: list[int]) -> list[dict]:
    if not ids:
        return []
    marks = ",".join("?" for _ in ids)
    by_id = {r["id"]: r for r in store.rows(f"SELECT * FROM memory_chunks WHERE id IN ({marks})", ids)}
    return [by_id[i] for i in ids if i in by_id]


def snippet(text: str, want: list[str], width: int = LINE_MAX) -> str:
    """Лучшая строка фрагмента (больше всего слов запроса), обрезанная вокруг первого совпадения."""
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()] or [""]
    best = max(lines, key=lambda ln: score(ln, want))
    one = " ".join(best.split())
    if len(one) <= width:
        return one
    low = norm(one)
    hits = [low.find(s) for s in want if low.find(s) >= 0]
    start = max(0, min(hits) - width // 4) if hits else 0
    cut = one[start:start + width]
    return ("…" if start else "") + cut + ("…" if start + width < len(one) else "")


# --- задачи, решения, документы, сессии ----------------------------------------------------------------------------

def _task_hits(want: list[str], about: list[str], since: str | None, limit: int) -> list[tuple[int, float]]:
    """Задачи (открытые выше) по словам: заголовок и суть весят больше, чем ход и цитата."""
    rows = store.rows(
        "SELECT c.id, c.status, c.title, c.what, c.who, c.whom, c.quote, c.note, c.updated, "
        "(SELECT group_concat(l.text, ' ') FROM task_log l WHERE l.task_id = c.id) AS log "
        "FROM commitments c WHERE c.archived_at IS NULL AND c.duplicate_of IS NULL")
    out = []
    for r in rows:
        if since and (r["updated"] or "") < since:
            continue
        head = f"{r['title'] or ''} {r['what'] or ''}"
        people = f"{r['who'] or ''} {r['whom'] or ''}"
        if about and not all(s in norm(f"{head} {people} {r['log'] or ''}") for s in about):
            continue
        s = 3 * score(head, want) + score(f"{r['quote'] or ''} {r['note'] or ''} {people}", want) \
            + score(r["log"], want)
        if not s:
            continue
        need = min(2, len(want))  # у длинного запроса одно общее слово — случайность
        if len({w for w in want if w in norm(f"{head} {people} {r['log'] or ''} {r['quote'] or ''}")}) < need:
            continue
        out.append((r["id"], s + (2 if r["status"] == "open" else 0)))
    out.sort(key=lambda x: -x[1])
    return out[:limit]


def _tasks(ids: list[int]) -> list[dict]:
    """Задачи для строки выдачи — только то, что нужно строке (records.list_commitments считает ещё приоритет,
    встречи, «без движения» — это втрое дольше всего поиска)."""
    from opsdash import records, worklog

    if not ids:
        return []
    marks = ",".join("?" for _ in ids)
    items = store.rows(f"SELECT * FROM commitments WHERE id IN ({marks})", ids)
    today = config.now().date().isoformat()
    parents = {r["id"]: r for r in store.rows(
        f"SELECT id, direction, who, whom, title, what FROM commitments WHERE id IN "
        f"(SELECT parent_id FROM commitments WHERE id IN ({marks}))", ids)}
    hints = {store.loads(p["payload"], {}).get("commitment_id") for p in store.rows(
        "SELECT payload FROM proposals WHERE kind='task.done' AND status='pending'")}
    for c in items:
        c["overdue"] = bool(c["due"] and c["due"] < today and c["status"] == "open")
        c["parent"] = parents.get(c.get("parent_id"))
        c["done_hint"] = c["id"] in hints and c["status"] == "open"
    records._items(items)
    worklog.decorate(items)
    return items


def _decision_hits(want: list[str], about: list[str], limit: int) -> list[dict]:
    from opsdash import decisions

    out = []
    for r in decisions.items(current=True, limit=2000):
        text = " ".join(str(r.get(k) or "") for k in ("title", "details", "topic", "subtopic", "q", "a", "metric",
                                                      "waiting_for", "quote"))
        if about and not all(s in norm(text) for s in about):
            continue
        s = 2 * score(f"{r.get('title')} {r.get('topic')} {r.get('q') or ''}", want) + score(text, want)
        if s >= min(2, len(want)) + (1 if len(want) > 1 else 0):
            out.append((s, r))
    out.sort(key=lambda x: -x[0])
    return [r for _, r in out[:limit]]


def _doc_hits(query: str, limit: int, shown: int = 5) -> list[dict]:
    """Документы: лучшие места по BM25 — по одному на файл (docs.search точнее, но в 5–10 раз медленнее)."""
    from opsdash import docs

    q = docs.fts_query(query, "OR")
    if not q or not store.scalar("SELECT 1 FROM docs LIMIT 1"):
        return []
    try:  # сначала места (быстро), отрывок — только у показанных: snippet() по всем совпадениям в 10 раз дольше
        top = store.rows("SELECT rowid AS rid, path, part FROM docs_fts WHERE docs_fts MATCH ? "
                         "ORDER BY bm25(docs_fts, 0, 0, 6.0, 1.0) LIMIT ?", (q, limit * 4))
    except Exception:  # noqa: BLE001
        return []
    titles = {r["path"]: r for r in store.rows(
        f"SELECT path, title, rel FROM docs WHERE path IN ({','.join('?' * len(top)) or 'NULL'})",
        [r["path"] for r in top])}
    out, seen = [], set()
    for r in top:
        d = titles.get(r["path"]) or {"title": "", "rel": r["path"]}
        if r["path"] in seen or (d["title"], r["part"]) in seen:
            continue
        seen.update({r["path"], (d["title"], r["part"])})
        out.append({**r, "title": d["title"], "rel": d["rel"]})
    want = stems(query)
    for r in out[:shown]:
        body = store.scalar("SELECT body FROM docs_fts WHERE rowid=?", (r["rid"],)) or ""
        r["snippet"] = snippet(body, want, 180)
    return out


def _session_hits(query: str, limit: int) -> list[dict]:
    from opsdash import agents

    if not store.scalar("SELECT 1 FROM agent_sessions LIMIT 1"):
        return []
    try:
        return agents.similar(query, limit=limit)
    except Exception:  # noqa: BLE001
        return []


# --- общий поиск -------------------------------------------------------------------------------------------------

def _private_meetings() -> set[str]:
    return {r["id"] for r in store.rows("SELECT id FROM meetings WHERE private=1")}


def _ddmm(ts: str | None) -> str:
    if not ts:
        return ""
    if "T" in ts:
        dt = store.parse_ts(ts)
        return dt.astimezone(_tz()).strftime("%d.%m") if dt else ""
    return f"{ts[8:10]}.{ts[5:7]}" if len(ts) >= 10 else ""


_TZ: list = []


def _tz():
    if not _TZ:
        _TZ.append(config.tz())
    return _TZ[0]


def _cut(text: str, n: int = LINE_MAX) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


def collect(query: str, *, about: str | None = None, since: str | None = None, kinds: list[str] | None = None,
            budget: str = "mid", limit: int | None = None, mcp: bool = False) -> dict:
    """Всё найденное по видам: {kind: [строки]}, сколько всего, каким способом. Без вывода — его собирает render."""
    kinds = [k for k in (kinds or KINDS) if k in KINDS]
    if mcp:  # агентам снаружи переписку не отдаём: в ней бывает личное
        kinds = [k for k in kinds if k != "messages"]
    want = stems(query)
    about_s = stems(about) if about else []
    if not want and not about_s:
        raise RecallError("нужны слова для поиска — например, «переезд склада»")
    if not want:
        want, query = about_s, about or ""
    quota = dict(QUOTAS.get(budget, QUOTAS["mid"]))
    if limit:
        quota = {k: limit for k in quota}
    elif len(kinds) == 1:
        quota = {k: v * 3 for k, v in quota.items()}
    found: dict[str, list[str]] = {}
    total: dict[str, int] = {}
    head: list[str] = []
    mode = "слова"

    if "facts" in kinds or not kinds:
        try:
            from opsdash import memory

            res = memory.recall_part(query, about=about, since=since, mcp=mcp, limit=quota["facts"])
        except Exception as e:  # noqa: BLE001 — память фактов не должна ломать поиск по остальному
            store.log("recall", f"факты: {e}", "warning")
            res = None
        if res:
            head += res.get("head") or []
            found["facts"], total["facts"] = res.get("lines") or [], res.get("total") or 0
            if res.get("semantic"):
                mode = "слова + смысл"

    if "tasks" in kinds:
        from opsdash import cli, records

        hits = _task_hits(want, about_s, since, 200)
        total["tasks"] = len(hits)
        ids = [i for i, _ in hits[:quota["tasks"]]]
        by_id = {c["id"]: c for c in _tasks(ids)}
        found["tasks"] = [cli._commitment_brief(by_id[i], now=160) for i in ids if i in by_id]

    if "decisions" in kinds:
        hits = _decision_hits(want, about_s, 100)
        total["decisions"] = len(hits)
        lines = []
        for r in hits[:quota["decisions"]]:
            sub = f" / {r['subtopic']}" if r.get("subtopic") else ""
            qa = f" · «{r['q']}»: {r['a']}" if r.get("q") and r.get("a") else ""
            lines.append(_cut(f"[решение {r['id']}] {r['when']} · {r['topic']}{sub} · {r['title']}{qa}", 300))
        found["decisions"] = lines

    want_chunks = [k for k, flag in (("meetings", ("meet", "sum")), ("messages", ("msg",))) if k in kinds]
    if want_chunks:
        refresh()
        ck: tuple[str, ...] = tuple(x for k in want_chunks for x in {"meetings": ("meet", "sum"),
                                                                      "messages": ("msg",)}[k])
        words_arm = fts_chunks(query, kinds=ck, k=ARM_K * 4, since=since, about=about)
        sem = _semantic(query)
        ranked = words_arm
        if sem and sem.get("chunks"):
            allowed = set(ck)
            sem_ids = [i for i in sem["chunks"] if i]
            meta = {c["id"]: c for c in chunks(list(dict.fromkeys(sem_ids + words_arm)))}
            sem_ids = [i for i in sem_ids if i in meta and meta[i]["kind"] in allowed
                       and (not since or (meta[i]["ts"] or "") >= since)
                       and (not about_s or all(s in norm(meta[i]["text"]) for s in about_s))]
            ranked = hybrid(words_arm, sem_ids, {i: c["kind"] for i, c in meta.items()})
            mode = "слова + смысл"
        private = _private_meetings()
        rows = [c for c in chunks(ranked) if not (c["kind"] != "msg" and c["src"] in private)]
        if "meetings" in kinds:
            lines, seen = [], {}
            for c in rows:
                if c["kind"] == "msg":
                    continue
                if c["src"] in seen:
                    continue
                seen[c["src"]] = c
            total["meetings"] = len(seen)
            for c in list(seen.values())[:quota["meetings"]]:
                where = f"разбор, «{c['pos']}»" if c["kind"] == "sum" else f"расшифровка {c['pos']}"
                lines.append(_cut(f"{c['src']} · {_ddmm(c['ts'])} · {c['title']}", 160)
                             + f"\n    {where}: {snippet(c['text'], want, 200)}")
            found["meetings"] = lines
        if "messages" in kinds:
            msgs = [c for c in rows if c["kind"] == "msg"]
            total["messages"] = len(msgs)
            found["messages"] = [_message_line(c, want) for c in msgs[:quota["messages"]]]

    if "docs" in kinds:
        docs_ = _doc_hits(query, quota["docs"] * 3, quota["docs"])
        total["docs"] = len(docs_)
        found["docs"] = [_cut(f"«{d['title'] or d['rel']}»" + (f" · {d['part']}" if d.get("part") else "")
                              + f": {(d.get('snippet') or '').replace('⟦', '').replace('⟧', '')}")
                         for d in docs_[:quota["docs"]]]

    if "sessions" in kinds:
        ss = _session_hits(query, quota["sessions"] * 2)
        total["sessions"] = len(ss)
        found["sessions"] = [_cut(f"сессия {s['id']} · {_ddmm(s.get('started') or s.get('updated'))} · "
                                  f"{s.get('title') or s.get('first_prompt') or ''}") for s in ss[:quota["sessions"]]]
    return {"head": head, "found": found, "total": total, "mode": mode, "kinds": kinds, "want": want}


def _message_line(c: dict, want: list[str]) -> str:
    """Окно переписки — одной строкой: лучшее по словам сообщение окна с его [id], датой и чатом."""
    ids = store.loads(c["msg_ids"], [])
    best = None
    if ids:
        marks = ",".join("?" for _ in ids)
        msgs = store.rows(f"SELECT id, ts, sender_name, is_outgoing, text FROM messages WHERE id IN ({marks})", ids)
        if msgs:
            best = max(msgs, key=lambda m: (score(m["text"], want), -m["id"]))
    if not best:
        return _cut(f"{c['pos']} {_ddmm(c['ts'])} · «{c['title']}»: {snippet(c['text'], want)}")
    who = best["sender_name"] or ("Вы" if best["is_outgoing"] else "Собеседник")
    return f"[{best['id']}] {_ddmm(best['ts'])} · «{_cut(c['title'], 50)}» · {who}: " \
           f"{snippet(best['text'], want, 200)}"


def render(res: dict, query: str, *, budget: str = "mid", about: str | None = None) -> str:
    """Выдача в пределах бюджета: от плотного к сырому; что не влезло — «Ещё N: ./ops recall … --kind …».
    Место делим в два прохода: сначала каждому найденному виду — до двух строк, потом остальное по порядку видов,
    чтобы длинные задачи не вытесняли встречи и переписку."""
    cap = BUDGETS.get(budget, BUDGETS["mid"]) - 300  # запас на строку «Ещё …»
    nbytes = lambda t: len(t.encode("utf-8")) + 1  # noqa: E731
    size = sum(nbytes(p) for p in res["head"])
    lines = {k: ["  " + ln.replace("\n", "\n  ") for ln in (res["found"].get(k) or [])] for k in KINDS}
    take = {k: 0 for k in KINDS}
    present = [k for k in KINDS if lines[k]]
    size += sum(nbytes(f"{KIND_TITLES[k]} (99 из 999):") for k in present)
    for first_pass in (True, False):
        for k in present:
            stop = min(2, len(lines[k])) if first_pass else len(lines[k])
            while take[k] < stop and size + nbytes(lines[k][take[k]]) <= cap:
                size += nbytes(lines[k][take[k]])
                take[k] += 1
    parts: list[str] = list(res["head"])
    more: list[str] = []
    for k in KINDS:
        total = res["total"].get(k, 0)
        if not total:
            continue
        n = take[k]
        if n:
            head = f"{KIND_TITLES[k]} ({n} из {total}):" if total > n else f"{KIND_TITLES[k]} ({total}):"
            parts.append("\n".join([head, *lines[k][:n]]))
        if total > n:
            more.append(f"{KIND_TITLES[k]}: ещё {total - n}" if n else f"{KIND_TITLES[k]}: {total}")
    if not parts:
        return f"По «{query}» ничего не нашлось." + (f" (о ком/чём: {about})" if about else "")
    if more:
        q = query.replace('"', "'")
        kinds = [k for k in KINDS if res["total"].get(k, 0) > take[k]]
        parts.append("Ещё: " + " · ".join(more) + f' — подробнее: ./ops recall "{q}" --kind {kinds[0]} --limit 20')
    return "\n".join(parts)


def caller() -> str:
    """Кто спрашивает: навык фонового запуска (OPSDASH_JOB), MCP или человек в терминале."""
    return os.environ.get("OPSDASH_JOB") or ("agent" if os.environ.get("OPSDASH_AGENT") else "cli")


def log(who: str, query: str, about: str | None, kinds: list[str], total: dict, mode: str, ms: float) -> None:
    rec = {"at": store.utcnow(), "who": who, "query": query[:500], "about": about or "",
           "kinds": ",".join(kinds), "found": total, "mode": mode, "ms": round(ms, 1)}
    if store._query_only:  # MCP: в базу не пишем — строка уйдёт через файл-приёмник
        try:
            from opsdash import memory

            memory.inbox_append({"type": "recall", **rec})
        except Exception:  # noqa: BLE001 — журнал не должен мешать ответу
            pass
        return
    write_log(rec)


def write_log(rec: dict) -> None:
    store.execute("INSERT INTO recall_log(at, who, query, about, kinds, found, mode, ms) VALUES(?,?,?,?,?,?,?,?)",
                  (rec.get("at") or store.utcnow(), rec.get("who") or "?", rec.get("query") or "", rec.get("about"),
                   rec.get("kinds"), json.dumps(rec.get("found") or {}, ensure_ascii=False), rec.get("mode"),
                   rec.get("ms")))


def recall(query: str, *, about: str | None = None, since: str | None = None, kinds: list[str] | None = None,
           budget: str = "mid", limit: int | None = None, who: str | None = None, mcp: bool = False) -> str:
    """Один поиск по всему — текст для Claude (CLI, MCP, навыки) в пределах бюджета."""
    if budget not in BUDGETS:
        raise RecallError("budget — low, mid или high")
    if since:
        try:
            datetime.fromisoformat(since)
        except ValueError as e:
            raise RecallError("since — дата YYYY-MM-DD") from e
    t0 = time.time()
    res = collect(query, about=about, since=since, kinds=kinds, budget=budget, limit=limit, mcp=mcp)
    text = render(res, query, budget=budget, about=about)
    log(who or ("mcp" if mcp else caller()), query, about, res["kinds"], res["total"], res["mode"],
        (time.time() - t0) * 1000)
    return text


def log_stats(days: int = 7) -> dict:
    """Журнал «Вспомнить» за дни: сколько запросов, у кого, сколько пустых — для «Памяти в цифрах»."""
    since = store.to_utc_iso(config.now() - __import__("datetime").timedelta(days=days))
    rows = store.rows("SELECT who, found, mode FROM recall_log WHERE at >= ?", (since,))
    empty = sum(1 for r in rows if not any((store.loads(r["found"], {}) or {}).values()))
    by_who: dict[str, int] = {}
    for r in rows:
        by_who[r["who"]] = by_who.get(r["who"], 0) + 1
    return {"total": len(rows), "empty": empty, "by_who": sorted(by_who.items(), key=lambda x: -x[1]),
            "semantic": sum(1 for r in rows if "смысл" in (r["mode"] or ""))}


def model_dir() -> Path:
    return config.MODELS_DIR / "e5-small"
