"""Замер «Вспомнить» на копии базы: какую долю эталона находит каждый способ поиска в первых 10.

Эталон — сообщения и встречи, которые разборы (triage, process-meeting) сами привязали к ходу задач, читая всё подряд,
а не поиском. Источник, из которого задача родилась, исключён: её название списано с него. Запрос по переписке —
название задачи, по встречам — её полная формулировка. Место считается среди источников того же вида — как отдельный
поиск по переписке (`messages search`) или по встречам (`meetings search`).

Только на копии — живую базу скрипт не трогает, но индекс «Вспомнить» в копии дочитывает:

    OPSDASH_HOME=<папка копии> .venv/bin/python scripts/recall_bench.py [--no-vec] [--json итог.json]

Векторы (поиск по смыслу) берутся из memory_vectors копии; нет их — строки «по смыслу» пропускаются. --embed —
досчитать недостающие векторы фрагментов моделью из data/models/e5-small (несколько минут).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from opsdash import config, recall, store  # noqa: E402

MESSAGE_SOURCES = ("telegram", "pachca", "mail", "ymessenger", "slack", "gmail", "imap")


def load_index() -> dict:
    rows = store.rows("SELECT id, kind, src, ts, text, msg_ids FROM memory_chunks")
    by_id = {r["id"]: r for r in rows}
    msg_chunk = {}
    for r in rows:
        for mid in store.loads(r["msg_ids"], []) or []:
            msg_chunk.setdefault(mid, r["id"])
    return {"rows": by_id, "msg_chunk": msg_chunk, "low": None}


def unit(ix: dict, cid: int) -> tuple:
    r = ix["rows"][cid]
    return ("msg", cid) if r["kind"] == "msg" else ("meet", r["src"])


def units(ix: dict, cids: list[int]) -> list[tuple]:
    return list(dict.fromkeys(unit(ix, c) for c in cids if c in ix["rows"]))


def gold_set(ix: dict) -> list[dict]:
    meetings = {r["id"] for r in store.rows("SELECT id FROM meetings WHERE COALESCE(private, 0)=0")}
    tasks: dict[int, dict] = {}
    marks = ",".join("?" for _ in MESSAGE_SOURCES)
    for r in store.rows(
            f"SELECT l.task_id, l.source_type st, l.source_ref sr, c.title, c.what, c.source_type cst, "
            f"c.source_ref csr FROM task_log l JOIN commitments c ON c.id = l.task_id "
            f"WHERE l.author='claude' AND l.source_ref IS NOT NULL AND l.source_type IN ({marks}, 'meeting')",
            MESSAGE_SOURCES):
        if (r["st"], str(r["sr"])) == (r["cst"], str(r["csr"])):
            continue
        t = tasks.setdefault(r["task_id"], {"id": r["task_id"], "title": r["title"] or "", "what": r["what"] or "",
                                            "gold": set()})
        if r["st"] == "meeting":
            mid = next((m for m in meetings if str(r["sr"]).startswith(m)), None)
            if mid:
                t["gold"].add(("meet", mid))
        else:
            for num in re.findall(r"\d+", str(r["sr"])):
                cid = ix["msg_chunk"].get(int(num))
                if cid:
                    t["gold"].add(("msg", cid))
    return [t for t in tasks.values() if t["gold"]]


def like(ix: dict, needle: str, k: int = 200) -> list[int]:
    """Как было до «Вспомнить»: точная подстрока без учёта регистра, свежие сверху."""
    if ix["low"] is None:
        ix["low"] = {cid: recall.norm(r["text"]) for cid, r in ix["rows"].items()}
    needle = recall.norm(needle).strip()
    if not needle:
        return []
    hits = [c for c, low in ix["low"].items() if needle in low]
    return sorted(hits, key=lambda c: ix["rows"][c]["ts"] or "", reverse=True)[:k * 4]


def rare_stem(ix: dict, q: str) -> str:
    """Самое редкое слово запроса — как если бы модель удачно выбрала одно ключевое слово."""
    if ix["low"] is None:
        like(ix, "x")
    best, best_df = "", None
    for w in dict.fromkeys(recall.words(q)):
        if len(w) < 4:
            continue
        s = recall.stem(w)
        df = sum(1 for low in ix["low"].values() if s in low)
        if df and (best_df is None or df < best_df):
            best, best_df = s, df
    return best


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--no-vec", action="store_true", help="без поиска по смыслу")
    p.add_argument("--embed", action="store_true", help="досчитать векторы фрагментов моделью (долго)")
    p.add_argument("--json", help="записать итог в файл")
    a = p.parse_args()
    if "OPSDASH_HOME" not in __import__("os").environ:
        sys.exit("Запускайте на копии: OPSDASH_HOME=<папка копии> … (живую базу замер не трогает)")
    t0 = time.time()
    res = recall.refresh()
    print(f"индекс: +{res.get('added', 0)} фрагментов за {time.time() - t0:.1f} с; "
          + ", ".join(f"{recall.CHUNK_KINDS[k]} — {v['n']}" for k, v in recall.index_stats().items()))
    ix = load_index()
    vec = None
    if not a.no_vec:
        try:
            from opsdash import embed

            if a.embed:
                t0 = time.time()
                n = embed.fill(chunks=True, facts=False, progress=print)
                print(f"векторы: +{n} за {time.time() - t0:.0f} с")
            vec = embed.LocalSearch()
            if not vec.ready():
                print("векторов фрагментов нет — строки «по смыслу» пропущены (--embed, чтобы досчитать)")
                vec = None
        except Exception as e:  # noqa: BLE001
            print(f"поиск по смыслу недоступен: {e}")
            vec = None
    tasks = gold_set(ix)
    print(f"задач в замере: {len(tasks)}, эталонных источников: {sum(len(t['gold']) for t in tasks)} "
          f"(переписка: {sum(1 for t in tasks for g in t['gold'] if g[0] == 'msg')}, "
          f"встречи: {sum(1 for t in tasks for g in t['gold'] if g[0] == 'meet')})")
    arms = {
        "Сейчас: фраза целиком": lambda q: like(ix, q),
        "Сейчас: одно удачное слово": lambda q: like(ix, rare_stem(ix, q)),
        "FTS5 по основам": lambda q: recall.fts_chunks(q, k=400),
    }
    if vec:
        arms["Только эмбеддинги"] = lambda q: vec.chunks(q, 400)
        arms["FTS5 + эмбеддинги"] = lambda q: recall.rrf(recall.fts_chunks(q, k=recall.ARM_K),
                                                       vec.chunks(q, recall.ARM_K)) + recall.fts_chunks(q, k=400)
    table: dict[str, dict] = {}
    for kind, field, label in (("msg", "title", "Переписка, запрос-название"),
                               ("meet", "what", "Встречи, запрос-формулировка")):
        qs = [t for t in tasks if t[field].strip() and any(g[0] == kind for g in t["gold"])]
        for name, fn in arms.items():
            rec, ms = [], []
            for t in qs:
                gold = {g for g in t["gold"] if g[0] == kind}
                s = time.time()
                found = [u for u in units(ix, fn(t[field])) if u[0] == kind][:10]
                ms.append((time.time() - s) * 1000)
                rec.append(len(gold & set(found)) / len(gold))
            table.setdefault(name, {})[label] = {"recall10": round(100 * sum(rec) / max(1, len(rec))),
                                                "queries": len(qs), "ms": round(sorted(ms)[len(ms) // 2] if ms else 0, 1)}
    cols = ["Переписка, запрос-название", "Встречи, запрос-формулировка"]
    print("\n| Способ | " + " | ".join(cols) + " |\n|---|---|---|")
    for name, row in table.items():
        print(f"| {name} | " + " | ".join(f"{row[c]['recall10']}% ({row[c]['ms']} мс)" for c in cols) + " |")
    print(f"\nзапросов: переписка — {table['FTS5 по основам'][cols[0]]['queries']}, "
          f"встречи — {table['FTS5 по основам'][cols[1]]['queries']}; в скобках — медиана времени запроса")
    if a.json:
        Path(a.json).write_text(json.dumps(table, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    config.ensure_dirs()
    main()
