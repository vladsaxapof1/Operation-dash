"""Сводка контекста для Claude: что за компания, что решили, что сейчас в работе, кто есть кто.

Собирается перед каждым запуском Claude (несколько миллисекунд — всегда свежая) и добавляется
к его инструкции (`--append-system-prompt`): так ему не нужно в каждом разговоре заново искать
базовые вещи. Раздел «О компании» раз в неделю пишет сам Claude по рабочим документам
(навык company-context) — это `data/knowledge/О компании.md`; остальное — из базы PULSE.
Итог лежит в `data/knowledge/Контекст.md` — его видно и на странице «База знаний → Что знает Claude».
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from opsdash import config, store

FILE = "Контекст.md"
COMPANY = "О компании.md"
MAX_CHARS = 12_000      # ≈ 3 тыс. токенов на каждый запуск — дёшево, но хватает на суть
COMPANY_CHARS = 6_000   # раздел Claude: если разросся — обрезаем, а не раздуваем каждый запуск
DECISIONS_DAYS, DECISIONS_MAX = 60, 30


def folder() -> Path:
    return config.DATA_DIR / "knowledge"


def company_path() -> Path:
    return folder() / COMPANY


def _cut(text: str, n: int) -> str:
    text = text.strip()
    return text if len(text) <= n else text[: n].rsplit("\n", 1)[0] + "\n…"


def _date(day: str | None) -> str:
    return f"{day[8:10]}.{day[5:7]}" if day and len(day) >= 10 else ""


def _company() -> list[str]:
    p = company_path()
    if not p.exists():
        return ["## О компании и проектах",
                "_Ещё не собрано: раз в неделю этот раздел пишет Claude по рабочим документам (навык company-context)._"]
    text = p.read_text(encoding="utf-8")
    if text.lstrip().startswith("# "):  # заголовок файла — свой, в сводке он лишний
        text = text.split("\n", 1)[1] if "\n" in text else ""
    updated = date.fromtimestamp(p.stat().st_mtime).strftime("%d.%m")
    return [f"## О компании и проектах (по документам, обновлено {updated})", _cut(text, COMPANY_CHARS)]


def _decisions() -> list[str]:
    from opsdash import anthology

    rows = anthology.items(kind="decision", status="accepted", days=DECISIONS_DAYS, limit=DECISIONS_MAX)
    if not rows:
        return []
    out = [f"## Ключевые решения за {DECISIONS_DAYS} дней (база знаний — `./ops kb search`)"]
    by_project: dict[str, list] = {}
    for r in rows:
        by_project.setdefault(r["project"] or "Без темы", []).append(r)
    for project, items in sorted(by_project.items(), key=lambda kv: -len(kv[1])):
        out.append(f"**{project}:** " + "; ".join(f"{r['title']} ({_date(r['happened'])})" for r in items))
    total = store.scalar("SELECT COUNT(*) FROM anthology WHERE kind='decision' AND status='accepted'") or 0
    if total > len(rows):
        out.append(f"_Всего решений в базе: {total}, здесь — последние {len(rows)}._")
    return out


def _lessons() -> list[str]:
    from opsdash import anthology

    rows = anthology.items(kind="lesson", status="accepted", days=180, limit=8) + \
        anthology.items(kind="win", status="accepted", days=180, limit=8)
    if not rows:
        return []
    return ["## Уроки и удачи", *(f"- {r['kind_title']}: {r['title']}" for r in rows)]


def _work(settings: dict) -> list[str]:
    from opsdash import records

    out = []
    me = (store.get_state("kaiten_me") or {}).get("id")
    if me:
        cards = store.rows("SELECT title, board_title, column_title FROM kaiten_cards WHERE condition=1 AND state IN (1,2) "
                           "AND responsible_id=? ORDER BY state DESC, updated DESC LIMIT 12", (me,))
        if cards:
            out.append("**Карточки директора в Kaiten:** " + "; ".join(
                f"{c['title']} ({c['board_title']}, «{c['column_title']}»)" for c in cards))
    boards = store.rows("SELECT space_title, board_title, COUNT(*) n FROM kaiten_cards WHERE condition=1 AND state IN (1,2) "
                        "GROUP BY 1, 2 ORDER BY n DESC LIMIT 8")
    if boards:
        out.append("**Доски Kaiten:** " + "; ".join(f"{b['space_title']} / {b['board_title']} — {b['n']}" for b in boards))
    today = records.today_local().isoformat()
    for direction, title in (("mine", "Задачи директора"), ("to_me", "Должны директору")):
        items = records.list_commitments(status="open", direction=direction, limit=8)
        if items:
            out.append(f"**{title}:** " + "; ".join(
                c["what"] + (f" — {c['who']}" if direction == "to_me" and c["who"] else "")
                + (f" (до {_date(c['due'])}{', просрочено' if c['due'] < today else ''})" if c["due"] else "")
                for c in items))
    return ["## Сейчас в работе", *out] if out else []


def _people() -> list[str]:
    rows = store.rows("SELECT name, role FROM people ORDER BY name LIMIT 40")
    if not rows:
        return []
    return ["## Справочник людей PULSE", "; ".join(f"{r['name']}" + (f" — {r['role']}" if r["role"] else "") for r in rows)]


def _rules_hint() -> list[str]:
    rule = store.row("SELECT path FROM docs WHERE status='indexed' AND title LIKE 'Правила для агентов%' LIMIT 1")
    return [f"Правила работы с данными компании полностью — `./ops docs show \"{rule['path']}\"`."] if rule else []


def build(settings: dict | None = None) -> str:
    settings = settings or config.load_settings()
    owner = (settings["owner"].get("name") or "директор").strip()
    head = [f"# Контекст для Claude — {owner}",
            f"_Собрано PULSE {config.now(settings):%d.%m.%Y %H:%M}. Это короткая память о компании: детали ищи командами "
            "`./ops kb search`, `./ops docs search`, `./ops meetings search`._"]
    parts = [head, _company(), _decisions(), _lessons(), _work(settings), _people(), _rules_hint()]
    text = "\n\n".join("\n".join(p) for p in parts if p)
    return _cut(text, MAX_CHARS) + "\n"


def write(settings: dict | None = None) -> Path:
    folder().mkdir(parents=True, exist_ok=True)
    path = folder() / FILE
    path.write_text(build(settings), encoding="utf-8")
    return path
