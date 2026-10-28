"""Антология: что решили, на чём обожглись и что сработало.

Откуда записи:
- заметки-решения в рабочих папках (md с `type: решение` во frontmatter) — забираются сами при
  обновлении индекса документов, это решения, уже записанные людьми;
- разборы встреч — Claude добавляет прозвучавшие решения (`./ops kb add`);
- раз в неделю Claude предлагает уроки и удачи — со статусом «предложено», директор принимает или отклоняет;
- вручную на странице «База знаний».

Для Claude антология — память о принятом: `./ops kb search` перед ответом «почему так решили»
и при разборе встречи — чтобы заметить, что новое решение меняет прежнее.
"""

from __future__ import annotations

import re
from pathlib import Path

from opsdash import config, store

KINDS = {"decision": "Решение", "lesson": "Урок", "win": "Удача"}
STATUSES = {"accepted": "актуально", "proposed": "предложено Claude", "outdated": "устарело", "rejected": "отклонено"}
EXPORT = "Антология.md"  # копия для чтения глазами и Claude: data/knowledge/Антология.md

# заметки: type во frontmatter → вид записи; статус заметки → статус записи
NOTE_KINDS = {"решение": "decision", "урок": "lesson", "ошибка": "lesson", "удача": "win", "победа": "win"}
NOTE_OUTDATED = {"отменено", "устарело", "заменено", "отменён", "отменена"}
FIELDS_FROM_NOTE = ("title", "details", "happened", "project", "people", "quote")


class AnthologyError(Exception):
    pass


def _fts(conn, item_id: int) -> None:
    conn.execute("DELETE FROM anthology_fts WHERE rowid=?", (item_id,))
    r = conn.execute("SELECT title, details, project FROM anthology WHERE id=?", (item_id,)).fetchone()
    if r:
        conn.execute("INSERT INTO anthology_fts(rowid, title, details, project) VALUES(?,?,?,?)",
                     (item_id, r["title"], r["details"] or "", r["project"] or ""))


def add(*, kind: str, title: str, details: str = "", happened: str | None = None, project: str | None = None,
        people: list[str] | None = None, source_type: str = "manual", source_ref: str | None = None,
        quote: str | None = None, status: str = "accepted", author: str = "director", export_now: bool = True) -> int:
    """Новая запись. Та же суть того же вида (кроме отклонённых) — не дублируем, возвращаем прежнюю."""
    if kind not in KINDS:
        raise AnthologyError(f"Вид записи — один из: {', '.join(KINDS)}")
    if status not in STATUSES:
        raise AnthologyError(f"Статус — один из: {', '.join(STATUSES)}")
    title = re.sub(r"\s+", " ", title or "").strip()
    if not title:
        raise AnthologyError("Нужна суть записи одной строкой")
    if happened:
        happened = happened[:10]
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", happened):
            raise AnthologyError("Дата — в формате YYYY-MM-DD")
    dup = store.row("SELECT id FROM anthology WHERE kind=? AND lower(title)=lower(?) AND status!='rejected'",
                    (kind, title))
    if dup:
        return dup["id"]
    now = store.utcnow()
    with store.db() as conn:
        cur = conn.execute(
            """INSERT INTO anthology(kind, title, details, happened, project, people, source_type, source_ref, quote,
                                     status, author, created, updated) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (kind, title, (details or "").strip(), happened, (project or "").strip() or None,
             store.json_col([p.strip() for p in people or [] if p.strip()]), source_type, source_ref, quote,
             status, author, now, now))
        _fts(conn, cur.lastrowid)
    if export_now:
        export()
    return cur.lastrowid


def update(item_id: int, export_now: bool = True, **fields) -> None:
    allowed = {"kind", "title", "details", "happened", "project", "status", "note", "people", "quote"}
    sets = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if "people" in sets:
        sets["people"] = store.json_col(sets["people"])
    if not sets:
        return
    if "status" in sets and sets["status"] not in STATUSES:
        raise AnthologyError(f"Статус — один из: {', '.join(STATUSES)}")
    with store.db() as conn:
        conn.execute(f"UPDATE anthology SET {', '.join(f'{k}=?' for k in sets)}, updated=? WHERE id=?",
                     [*sets.values(), store.utcnow(), item_id])
        _fts(conn, item_id)
    if export_now:
        export()


def delete(item_ids: list[int]) -> None:
    """Удалить записи совсем (источник удалили — например, встречу целиком)."""
    with store.db() as conn:
        for item_id in item_ids:
            conn.execute("DELETE FROM anthology WHERE id=?", (item_id,))
            _fts(conn, item_id)
    export()


def get(item_id: int) -> dict | None:
    r = store.row("SELECT * FROM anthology WHERE id=?", (item_id,))
    return _decorate([r])[0] if r else None


def _decorate(items: list[dict]) -> list[dict]:
    for r in items:
        r["people"] = store.loads(r["people"], [])
        r["kind_title"] = KINDS.get(r["kind"], r["kind"])
        r["status_title"] = STATUSES.get(r["status"], r["status"])
    return items


def items(*, kind: str | None = None, status: str | None = "current", project: str | None = None,
          q: str | None = None, days: int | None = None, limit: int = 500) -> list[dict]:
    """status: current — актуальные и предложенные; all — все, кроме отклонённых; или конкретный статус."""
    from opsdash import docs

    sql, params = "SELECT a.* FROM anthology a WHERE 1=1", []
    if q:  # при поиске — сначала самые подходящие; совпадение в сути весит больше, чем в подробностях
        query = docs.fts_query(q)
        if not query:
            return []
        sql = ("SELECT a.* FROM anthology a JOIN (SELECT rowid, bm25(anthology_fts, 5.0, 1.0, 2.0) AS rank "
               "FROM anthology_fts WHERE anthology_fts MATCH ?) f ON f.rowid = a.id WHERE 1=1")
        params.append(query)
    if kind:
        sql += " AND a.kind=?"
        params.append(kind)
    if status == "current":
        sql += " AND a.status IN ('accepted', 'proposed')"
    elif status == "all":
        sql += " AND a.status!='rejected'"
    elif status:
        sql += " AND a.status=?"
        params.append(status)
    if project:
        sql += " AND a.project=?"
        params.append(project)
    if days:
        sql += " AND COALESCE(a.happened, substr(a.created, 1, 10)) >= date('now', ?)"
        params.append(f"-{int(days)} days")
    sql += (" ORDER BY f.rank" if q else
            " ORDER BY COALESCE(a.happened, substr(a.created, 1, 10)) DESC, a.id DESC") + " LIMIT ?"
    params.append(limit)
    return _decorate(store.rows(sql, params))


def projects() -> list[str]:
    return [r["project"] for r in store.rows(
        "SELECT project, COUNT(*) n FROM anthology WHERE project IS NOT NULL AND status!='rejected' "
        "GROUP BY project ORDER BY n DESC, project")]


def counts() -> dict:
    out = {k: 0 for k in KINDS}
    for r in store.rows("SELECT kind, COUNT(*) n FROM anthology WHERE status='accepted' GROUP BY kind"):
        out[r["kind"]] = r["n"]
    out["proposed"] = store.scalar("SELECT COUNT(*) FROM anthology WHERE status='proposed'") or 0
    return out


# --- заметки-решения из рабочих папок ------------------------------------------------

WIKI_RE = re.compile(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]")


def plain(text: str) -> str:
    """Obsidian-разметка → обычный текст: [[Цель|подпись]] → подпись, **жирный** → жирный."""
    text = WIKI_RE.sub(lambda m: (m.group(2) or m.group(1)).strip(), text or "")
    return re.sub(r"\*\*(.+?)\*\*|__(.+?)__", lambda m: m.group(1) or m.group(2), text).strip()


def _names(section: str) -> list[str]:
    """«Кто решил»: [[Игорь Смирнов|Игорем]] → «Игорь Смирнов»; «Павел — базы» → «Павел»."""
    names = []
    for line in section.splitlines():
        line = line.strip().lstrip("-*• ").strip()
        if not line:
            continue
        linked = [m.group(1).strip() for m in WIKI_RE.finditer(line)]
        names += linked or [re.split(r"\s+[—–-]\s+|,", line)[0].strip()]
    return [n for n in dict.fromkeys(names) if n and len(n) <= 40]


def parse_note(text: str, path: Path) -> dict | None:
    """md-заметка с frontmatter `type: решение` (или урок/удача) → поля записи. Иначе None."""
    m = re.match(r"\ufeff?---\s*\n(.*?)\n---\s*\n(.*)", text, re.S)
    if not m:
        return None
    meta = {}
    for line in m.group(1).splitlines():
        k, sep, v = line.partition(":")
        if sep:
            meta[k.strip().lower()] = v.strip().strip('"').strip("'")
    kind = NOTE_KINDS.get(meta.get("type", "").lower())
    if not kind:
        return None
    body = m.group(2)
    h1 = re.search(r"^#\s+(.+)$", body, re.M)
    title = plain(h1.group(1) if h1 else path.stem)
    date = meta.get("date") or (re.match(r"\d{4}-\d{2}-\d{2}", path.stem) or [None])[0]
    title = re.sub(r"^\d{4}-\d{2}-\d{2}\s+", "", title)  # дата в заголовке уже есть в поле «когда»

    def section(name: str) -> str:
        s = re.search(rf"^##\s+(?:{name})[^\n]*\n(.*?)(?=^##\s|\Z)", body, re.M | re.S | re.I)
        return s.group(1).strip() if s else ""

    what = section("Что решили|Решение|Что случилось|Что сработало|Что произошло")
    why, after = section("Почему"), section("Последствия")
    parts = [what, f"Почему: {why}" if why else "", f"Последствия: {after}" if after else ""]
    details = plain("\n\n".join(x for x in parts if x) or body.strip()[:1500])
    source = plain(section("Источник|Откуда взялось").split("\n")[0].lstrip("-*• "))
    project = plain(meta.get("тема", "") or meta.get("проект", "")).split(",")[0].strip()
    status = "outdated" if meta.get("статус", "").lower() in NOTE_OUTDATED else "accepted"
    return {"kind": kind, "title": title, "details": details[:3000], "project": project or None, "status": status,
            "happened": date if date and re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(date)) else None,
            "people": _names(section("Кто решил|Кто")), "quote": source[:300] or None}


def import_notes() -> str:
    """Забрать заметки-решения из индекса документов: новые — добавить, изменённые — обновить."""
    rows = store.rows("SELECT path FROM docs WHERE ext='.md' AND status='indexed'")
    known = {r["source_ref"]: r for r in store.rows("SELECT * FROM anthology WHERE source_type='document'")}
    added = updated = 0
    for r in rows:
        path = Path(r["path"])
        try:
            head = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if not head.lstrip("﻿").startswith("---"):
            continue
        note = parse_note(head, path)
        if not note:
            continue
        old = known.get(r["path"])
        if old is None:
            add(**note, source_type="document", source_ref=r["path"], author="notes", export_now=False)
            added += 1
        elif old["status"] != "rejected" and any(
                (store.loads(old[k], []) if k == "people" else old[k]) != note[k] for k in FIELDS_FROM_NOTE):
            update(old["id"], export_now=False, **{k: note[k] for k in FIELDS_FROM_NOTE},
                   **({"status": note["status"]} if note["status"] == "outdated" else {}))
            updated += 1
    if added or updated:
        export()
    return f"заметок-решений: новых {added}, обновлено {updated}"


# --- копия в markdown ----------------------------------------------------------------

def export() -> Path:
    """data/knowledge/Антология.md — актуальные записи по видам и месяцам (её же удобно отдать Claude)."""
    folder = config.DATA_DIR / "knowledge"
    folder.mkdir(parents=True, exist_ok=True)
    lines = ["# Антология: решения, уроки, удачи", "",
             "_Собирает PULSE. Изменения — на странице «База знаний»: этот файл перезаписывается._", ""]
    for kind, title in (("decision", "Решения"), ("lesson", "Уроки"), ("win", "Удачи")):
        rows = items(kind=kind, status="accepted", limit=2000)
        if not rows:
            continue
        lines += [f"## {title}", ""]
        month = None
        for r in rows:
            m = (r["happened"] or r["created"][:10])[:7]
            if m != month:
                lines += [f"### {m}", ""]
                month = m
            meta = " · ".join(x for x in (r["happened"], r["project"]) if x)
            lines.append(f"- **{r['title']}**" + (f" ({meta})" if meta else ""))
            if r["details"]:
                lines.append("  " + re.sub(r"\s*\n\s*", " ", r["details"])[:400])
        lines.append("")
    path = folder / EXPORT
    path.write_text("\n".join(lines), encoding="utf-8")
    return path
