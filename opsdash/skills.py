"""Навыки PULSE (беклог SK-1 – SK-4, MOD-3): каталог системных навыков, их состояние, «Ваши правила» и защита.

Системные навыки — .claude/skills/<id>/SKILL.md, их читает Claude в фоновых заданиях. Директор их не правит:
правка ломает продукт (промты в jobs.py и вывод ./ops на них рассчитаны). Своё поведение директор задаёт правилами
обычными словами — таблица skill_rules: они подмешиваются в запуск навыка (brain.stream → rules_prompt) и дополняют
его, не подменяя. Правило появляется вручную или уроком из её «Нет» (навык lessons → предложение skill.rule → «Да»).

Защита (SK-3): изменённый на этом компьютере системный навык видно в каталоге и в «Здоровье»; «Вернуть заводской» —
git checkout файла навыка (PULSE стоит git-клоном и обновляется из GitHub).
"""

from __future__ import annotations

import re
import subprocess
import time
from functools import lru_cache
from pathlib import Path

import yaml

from opsdash import config, store

MANIFEST = Path(__file__).with_name("skills.yaml")
SKILLS_DIR = config.APP_DIR / ".claude" / "skills"
# причины «Нет» (MOD-3): кнопки после отказа — на Сводке и в боте
REASONS = {"not_task": "не задача", "exists": "уже есть", "wrong": "не тому", "not_now": "не сейчас", "other": "другое"}


@lru_cache(maxsize=1)
def manifest() -> dict:
    return yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))


def all_skills() -> list[dict]:
    return manifest()["skills"]


def get(sid: str) -> dict | None:
    return next((s for s in all_skills() if s["id"] == sid), None)


def skill_for_job(job: str) -> str | None:
    """По какому навыку работает фоновое задание (triage → triage, groups → groups-digest)."""
    return next((s["id"] for s in all_skills() if job in s.get("jobs", [])), None)


def text(sid: str) -> str:
    path = SKILLS_DIR / sid / "SKILL.md"
    return path.read_text(encoding="utf-8") if path.exists() else ""


def split_text(sid: str) -> tuple[dict, str]:
    """Шапка навыка (name, description — по ней Claude решает, когда брать навык) и сам текст без шапки."""
    raw = text(sid)
    if raw.startswith("---\n"):
        head, sep, body = raw[4:].partition("\n---\n")
        if sep:
            try:
                meta = yaml.safe_load(head) or {}
            except yaml.YAMLError:
                meta = {}
            return (meta if isinstance(meta, dict) else {}), _for_reading(body.lstrip("\n"))
    return {}, _for_reading(raw)


_LIST_ITEM = re.compile(r"^\s{0,3}(?:[-*+]|\d+\.)\s")


def _for_reading(body: str) -> str:
    """Для показа в «Навыках»: перед списком, который идёт сразу за строкой текста, — пустая строка, иначе Markdown
    склеит пункты с абзацем (в навыках так пишут часто). Сам файл навыка не меняется."""
    out: list[str] = []
    for line in body.split("\n"):
        prev = out[-1] if out else ""
        if _LIST_ITEM.match(line) and prev.strip() and not _LIST_ITEM.match(prev) and not prev[:1].isspace():
            out.append("")
        out.append(line)
    return "\n".join(out)


# --- «Ваши правила» (SK-4) ------------------------------------------------------------------------------

def rules(sid: str, enabled_only: bool = False) -> list[dict]:
    sql = "SELECT * FROM skill_rules WHERE skill=?" + (" AND enabled=1" if enabled_only else "") + " ORDER BY id"
    return store.rows(sql, (sid,))


def rule_counts() -> dict[str, int]:
    return {r["skill"]: r["n"] for r in store.rows(
        "SELECT skill, COUNT(*) AS n FROM skill_rules WHERE enabled=1 GROUP BY skill")}


def add_rule(sid: str, text_: str, source: str = "manual", proposal_id: int | None = None) -> int:
    text_ = " ".join((text_ or "").split())
    if not get(sid):
        raise ValueError(f"Нет навыка «{sid}»")
    if not text_:
        raise ValueError("Правило пустое")
    same = store.row("SELECT id FROM skill_rules WHERE skill=? AND lower(text)=lower(?)", (sid, text_))
    if same:
        store.execute("UPDATE skill_rules SET enabled=1, updated=? WHERE id=?", (store.utcnow(), same["id"]))
        return same["id"]
    now = store.utcnow()
    return store.execute("INSERT INTO skill_rules(skill, text, enabled, source, proposal_id, created, updated) "
                         "VALUES(?,?,1,?,?,?,?)", (sid, text_[:500], source, proposal_id, now, now))


def set_rule(rule_id: int, *, enabled: bool | None = None, text_: str | None = None) -> None:
    if enabled is not None:
        store.execute("UPDATE skill_rules SET enabled=?, updated=? WHERE id=?", (int(enabled), store.utcnow(), rule_id))
    if text_ is not None and text_.strip():
        store.execute("UPDATE skill_rules SET text=?, updated=? WHERE id=?",
                      (" ".join(text_.split())[:500], store.utcnow(), rule_id))


def delete_rule(rule_id: int) -> None:
    store.execute("DELETE FROM skill_rules WHERE id=?", (rule_id,))


def rules_prompt(job: str) -> str:
    """Добавка к запуску навыка: включённые правила директора. Пусто — правил нет или задание не по навыку."""
    sid = skill_for_job(job)
    items = rules(sid, enabled_only=True) if sid else []
    if not items:
        return ""
    lines = "\n".join(f"- {r['text']}" for r in items)
    title = get(sid)["title"]
    return ("\n\nПравила директора для навыка «" + title + "» — соблюдай их; они дополняют навык и важнее его общих "
            "указаний, но не отменяют жёстких правил CLAUDE.md (внешние действия — только предложениями, никому не "
            "писать от имени директора):\n" + lines)


# --- защита системных навыков (SK-3) -----------------------------------------------------------------------

_mod_cache: tuple[float, dict | None] = (0.0, None)


def _git(*args: str, timeout: int = 10) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(config.APP_DIR), *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout)


def modified(max_age: float = 60) -> dict[str, str] | None:
    """Системные навыки, изменённые на этом компьютере: {id: «изменён» / «удалён»}. None — проверить нельзя
    (PULSE стоит не git-клоном или git не найден). Запоминается на минуту — каталог не запускает git на каждый показ."""
    global _mod_cache
    if max_age and _mod_cache[1] is not None and time.monotonic() - _mod_cache[0] < max_age:
        return _mod_cache[1]
    try:
        r = _git("status", "--porcelain", "--", ".claude/skills")
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    ids = {s["id"] for s in all_skills()}
    out: dict[str, str] = {}
    for line in r.stdout.splitlines():
        code, path = line[:2], line[3:].strip().strip('"')
        parts = path.split("/")
        if len(parts) >= 3 and parts[2] in ids and "?" not in code:
            out[parts[2]] = "удалён" if "D" in code else "изменён"
    _mod_cache = (time.monotonic(), out)
    return out


def restore(sid: str) -> None:
    """«Вернуть заводской»: файлы навыка — как в установленной версии PULSE."""
    global _mod_cache
    if not get(sid):
        raise ValueError(f"Нет навыка «{sid}»")
    r = _git("checkout", "--", f".claude/skills/{sid}")
    _mod_cache = (0.0, None)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip()[:300] or "git не смог вернуть файл")


# --- каталог (SK-1, SK-2) -----------------------------------------------------------------------------------

REQUIRES = {
    "messages": ("переписка", "/settings?tab=connections#cg-messengers",
                 lambda s: config.any_enabled(s, "messages")),
    "calendar": ("календарь", "/settings?tab=connections#cg-calendars", lambda s: config.any_enabled(s, "calendar")),
    "telegram": ("Telegram", "/settings?tab=telegram", lambda s: bool(s["telegram"].get("enabled"))),
    "bot": ("Telegram-бот", "/settings?tab=bot",
            lambda s: bool(s["telegram_bot"].get("enabled") and s["telegram_bot"].get("owner_chat_id"))),
}


def status(sk: dict, settings: dict, jobs: dict, mods: dict | None) -> dict:
    """Состояние навыка: ok — работает; off — в архиве; need — не хватает подключения; warn — изменён или
    последний запуск с ошибкой; idle — ещё не запускался. Изменённый системный навык — важнее всего остального."""
    if mods and sk["id"] in mods:
        return {"kind": "warn", "label": f"{mods[sk['id']]} на этом компьютере", "link": f"/skills/{sk['id']}"}
    for req in sk.get("requires", []):
        if req.startswith("section:"):
            name = req.split(":", 1)[1]
            if not config.section_on(name, settings):
                return {"kind": "off", "label": f"в архиве — раздел «{config.SECTIONS[name]['title']}»",
                        "link": "/settings?tab=archive"}
        elif req in REQUIRES and not REQUIRES[req][2](settings):
            title, link, _ = REQUIRES[req]
            return {"kind": "need", "label": f"не хватает подключения: {title}", "link": link}
    rows = [jobs[j] for j in sk.get("jobs", []) if j in jobs]
    last = max(rows, key=lambda j: j.get("last_run") or "", default=None)
    if not last or not last.get("last_run"):
        return {"kind": "idle", "label": "ещё не запускался", "link": ""}
    if last.get("last_error") and (not last.get("last_ok") or last["last_ok"] < last["last_run"]):
        return {"kind": "warn", "label": "последний запуск — с ошибкой", "link": "/journal?level=error",
                "last_run": last["last_run"]}
    return {"kind": "ok", "label": "работает", "link": "", "last_run": last["last_run"]}


def catalog(settings: dict | None = None) -> dict:
    """Каталог навыков по разделам: состояние, сколько ваших правил, системный ли изменён."""
    settings = settings or config.load_settings()
    jobs = {j["name"]: j for j in store.rows("SELECT * FROM jobs")}
    mods = modified()
    counts = rule_counts()
    areas = []
    for a in manifest()["areas"]:
        items = [{**sk, **status(sk, settings, jobs, mods), "rules": counts.get(sk["id"], 0)}
                 for sk in all_skills() if sk["area"] == a["id"]]
        if items:
            areas.append({**a, "skills": items})
    return {"areas": areas, "git": mods is not None, "modified": mods or {},
            "titles": {s["id"]: s["title"] for s in all_skills()},
            "total": len(all_skills()), "rules": sum(counts.values())}


def runs(sid: str, limit: int = 6) -> list[dict]:
    """Последние запуски навыка — из учёта запусков помощника (claude_runs)."""
    from opsdash import jobs

    sk = get(sid) or {}
    names = sk.get("jobs") or []
    if not names:
        return []
    rows = store.rows(f"SELECT * FROM claude_runs WHERE job IN ({','.join('?' * len(names))}) "
                      "ORDER BY created DESC LIMIT ?", (*names, limit))
    for r in rows:
        r["title"] = jobs.TITLES.get(r["job"], r["job"])
    return rows


# --- «Нет» с причиной (MOD-3) ------------------------------------------------------------------------------

def set_reason(pid: int, reason: str, note: str = "") -> bool:
    """Причина отказа: только у отклонённого предложения и только из списка REASONS."""
    if reason not in REASONS:
        return False
    return bool(store.execute("UPDATE proposals SET reject_reason=?, reject_note=? WHERE id=? AND status='rejected'",
                              (reason, (note or "").strip()[:300] or None, pid)))


def rejected(days: int = 14) -> list[dict]:
    """Отклонённые за последние дни — с причинами: материал для уроков (навык lessons)."""
    from datetime import datetime, timedelta, timezone

    since = (datetime.now(timezone.utc) - timedelta(days=days)).replace(microsecond=0).isoformat()
    rows = store.rows("SELECT id, kind, title, reason, source_ref, reject_reason, reject_note, decided_at FROM proposals "
                      "WHERE status='rejected' AND decided_at >= ? ORDER BY decided_at DESC", (since,))
    for r in rows:
        r["reject_reason_text"] = REASONS.get(r["reject_reason"] or "", "")
    return rows
