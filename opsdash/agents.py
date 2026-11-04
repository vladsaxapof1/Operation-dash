"""«Поручить агенту»: подробный промт для Claude Code или Codex из задачи PULSE.

Промт собирается из двух контекстов. Задача — откуда она (встреча с цитатой и временем, сообщение), срок, проект,
связи «ждёт», решения из базы знаний, документы. И опыт агентов — как похожие задачи уже делали в папках «Офиса»:
первый промт, что человек дописал потом (этого не хватило в постановке), какие файлы читали и создали, чем кончилось.
Опыт — в индексе agent_sessions: журналы Claude Code PULSE только читает (разбор общий с «Офисом», office.sessions),
а запись остаётся и после того, как Claude Code удалит старый журнал.

Промт пишет Claude по навыку agent-brief — заданием «agent-brief» в очереди чата, шаги видны по ходу, как в «Спросить
Claude». Директор правит его, снимает лишние файлы и открывает новую сессию в приложении Claude ссылкой
claude://code/new: промт встаёт в поле ввода, отправляет его сам человек. Агента PULSE не запускает.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
import time
import unicodedata
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, urlencode

from opsdash import config, office, records, store

KEEP_DAYS = 180         # сколько хранить сессии в индексе
FILES_KEEP = 40         # файлов одной сессии в индексе
ENGINES = {"claude": "Claude", "codex": "Codex"}
# столько символов промта ссылка несёт целиком: приложение Claude обрезает поле ввода на 14 336, длинный промт
# (проверено на Mac: 12 000 символов кириллицы — ссылка в 65 КБ — доходит целиком) — файлом в data/agent-briefs
LINK_PROMPT = 12000
WINDOWS_URL = 2000      # Windows: длинную ссылку система может не передать (не проверено) — промт файлом
SERVICE = "PULSE: задача #{id}"  # служебная строка в промте: по ней PULSE найдёт запущенную сессию
# пути, которые в промт и в индекс не попадают: секреты, служебное Claude Code, временное
_PRIVATE = re.compile(r"(^|[\\/])(secrets|\.ssh|\.gnupg|\.aws|\.env[^\\/]*)([\\/]|$)|credential|password|token|парол",
                      re.IGNORECASE)
_TEMP = ("/tmp/", "/private/tmp/")  # там Claude Code держит временное сессий
# похоже на работу для агента: подсказка «Можно поручить агенту» (кнопка есть у любой задачи)
AGENTISH = re.compile(r"сводк|таблиц|расч[её]т|посчита|ресерч|исследова|черновик|регламент|постановк|анализ|"
                      r"проанализ|отч[её]т|презентац|выгруз|дашборд|скрипт|шаблон|модел", re.IGNORECASE)

_procs: dict[int, object] = {}  # сборка промта → процесс Claude: чтобы её можно было остановить
_stopped: set[int] = set()
_lock = threading.Lock()


# --- индекс сессий ---------------------------------------------------------------------------

def _useful(path: str) -> bool:
    """Файл стоит помнить и показывать: не секрет, не служебное Claude Code, не временное."""
    if not path or _PRIVATE.search(path) or path.replace("\\", "/").startswith(_TEMP):
        return False
    return not office._inside(path, str(office.claude_dir()))


def index(settings: dict | None = None) -> str:
    """Дочитать в индекс сессии из папок «Офиса»: новые и изменившиеся с прошлого раза, старше полугода — убрать."""
    settings = settings or config.load_settings()
    roots = [config.resolve_path(f) for f in settings["office"].get("folders") or []]
    prune()
    if not roots:
        return "папки «Офиса» не выбраны"
    rows = {r["id"]: r for r in store.rows("SELECT id, size, mtime, status, desktop_id FROM agent_sessions")}
    known = {sid: (r["size"], r["mtime"]) for sid, r in rows.items()}
    app = office.desktop_sessions()
    hide = office.hidden(settings)
    since = time.time() - KEEP_DAYS * 86400
    # пропущенные журналы (скрытый проект, фоновые запуски PULSE, пустые) — с размером: не перечитывать их каждый раз
    skipped = store.get_state("agents_skipped") or {}
    if skipped.get("hide") != sorted(hide):
        skipped = {"hide": sorted(hide), "files": {}}
    stats: dict[str, tuple] = {}
    tried: set[str] = set()

    def wanted(journal: Path) -> bool:
        if journal.stem in stats:  # папки офисов вложены друг в друга — сессию уже взяли
            return False
        try:
            st = journal.stat()
        except OSError:
            return False
        stats[journal.stem] = (st.st_size, st.st_mtime)
        if known.get(journal.stem) == stats[journal.stem] or skipped["files"].get(journal.stem) == list(stats[journal.stem]):
            return False
        tried.add(journal.stem)
        return True

    updated, saved = 0, set()
    for root in roots:
        for journal, s, room in office.sessions(root, since, hide, wanted):
            _save(journal, s, root, room, app.get(journal.stem), stats[journal.stem])
            saved.add(journal.stem)
            updated += 1
    skipped["files"] = {k: v for k, v in skipped["files"].items() if k in stats and k not in saved}
    skipped["files"].update({k: list(stats[k]) for k in tried - saved})
    store.set_state("agents_skipped", skipped)
    changed = [(mine["status"], mine["id"], sid) for sid, mine in app.items()  # итог по оценке приложения меняется
               if sid in rows and (rows[sid]["status"] or "", rows[sid]["desktop_id"] or "") != (mine["status"], mine["id"])]
    if changed:  # и без новых записей в журнале
        with store.db() as conn:
            conn.executemany("UPDATE agent_sessions SET status=?, desktop_id=? WHERE id=?", changed)
    total = store.scalar("SELECT COUNT(*) FROM agent_sessions") or 0
    return f"сессий в индексе: {total}, обновлено: {updated}"


def _save(journal: Path, s: dict, root: Path, room: str, mine: dict | None, stat: tuple) -> None:
    written = [p for p in s["written"] if _useful(p)][-FILES_KEEP:]
    read = [p for p, _ in s["touched"].most_common() if p not in s["written"] and _useful(p)][:FILES_KEEP]
    first = re.sub(r"^#+\s*(Задача:\s*)?", "", s["prompt"].splitlines()[0]) if s["prompt"] else ""  # «# Задача: …» — из брифа
    title = s["title"] or first[:90] or "Без названия"
    row = {"id": journal.stem, "engine": "claude", "journal": str(journal), "desktop_id": (mine or {}).get("id"),
           "folder": str(root), "room": room, "cwd": s["cwd"], "title": title, "first_prompt": s["first_prompt"],
           "prompts": store.json_col(s["asks"]), "files_read": store.json_col(read),
           "files_written": store.json_col(written), "skills": store.json_col(s["skills"]), "agents": s["agents"],
           "n_prompts": s["prompts"], "started": s["first"], "last": s["last"], "status": (mine or {}).get("status"),
           "summary": s["answer"], "size": stat[0], "mtime": stat[1], "indexed_at": store.utcnow()}
    files = " ".join(Path(p).name for p in read + written)
    with store.db() as conn:
        conn.execute(f"INSERT INTO agent_sessions({', '.join(row)}) VALUES({', '.join('?' * len(row))}) "
                     f"ON CONFLICT(id) DO UPDATE SET {', '.join(f'{k}=excluded.{k}' for k in row if k != 'id')}",
                     list(row.values()))
        conn.execute("DELETE FROM agent_sessions_fts WHERE id=?", (row["id"],))
        conn.execute("INSERT INTO agent_sessions_fts(id, title, prompts, files, room) VALUES(?,?,?,?,?)",
                     (row["id"], title, f"{s['first_prompt']}\n" + "\n".join(s["asks"]), files, room))


def prune() -> None:
    since = (datetime.now(timezone.utc) - timedelta(days=KEEP_DAYS)).replace(microsecond=0).isoformat()
    with store.db() as conn:
        old = [r[0] for r in conn.execute("SELECT id FROM agent_sessions WHERE COALESCE(last, started, '') < ?", (since,))]
        for sid in old:
            conn.execute("DELETE FROM agent_sessions_fts WHERE id=?", (sid,))
            conn.execute("DELETE FROM agent_sessions WHERE id=?", (sid,))


def _home(r: dict) -> str:
    """Папка проекта сессии: папка офиса / проект (или папка, где шла сессия)."""
    if r.get("room") and r.get("folder"):
        return str(Path(r["folder"]) / r["room"])
    return r.get("cwd") or r.get("folder") or ""


def _decorate(r: dict) -> dict:
    r = dict(r)
    for key in ("prompts", "files_read", "files_written", "skills"):
        r[key] = store.loads(r.get(key), [])
    r["when"] = store.local(r["started"], "%d.%m") if r.get("started") else ""
    started, last = store.parse_ts(r.get("started")), store.parse_ts(r.get("last"))
    r["duration"] = office._duration((last - started).total_seconds()) if started and last else ""
    r["home"] = _home(r)
    r["link"] = f"claude://code/continue?session={r['desktop_id']}" \
        if r.get("desktop_id") and office._DESKTOP_ID.match(r["desktop_id"]) else ""
    r["made"] = [{"path": p, "name": Path(p).name} for p in r["files_written"] if Path(p).exists()]
    r["outcome"] = {"completed": "готово", "review_ready": "на проверку", "blocked": "ждала действия"}.get(r.get("status") or "", "")
    return r


def similar(text: str, *, folder: str | None = None, limit: int = 5, settings: dict | None = None,
            task: int | None = None) -> list[dict]:
    """Прошлые сессии, похожие на задачу: полнотекстом по названию, промтам, файлам и проекту; общих слов с названием
    и промтами — не меньше двух (у короткого запроса — одно). folder — только сессии в этой папке; task — без сессий,
    запущенных по самой этой задаче."""
    from opsdash import docs

    query = docs.fts_query(text, "OR")
    if not query:
        return []
    settings = settings or config.load_settings()
    hide = office.hidden(settings)
    want = records._stems(text)
    rows = store.rows("SELECT s.*, bm25(agent_sessions_fts, 0, 6.0, 2.0, 1.5, 2.0) AS rank FROM agent_sessions_fts f "
                      "JOIN agent_sessions s ON s.id = f.id WHERE agent_sessions_fts MATCH ? ORDER BY rank LIMIT 80",
                      (query,))
    own = {r["session_id"] for r in store.rows("SELECT session_id FROM task_agents WHERE commitment_id=? "
                                               "AND session_id IS NOT NULL", (task,))} if task else set()
    out = []
    for r in rows:
        if (r["room"] or "").casefold() in hide or r["id"] in own:
            continue
        if folder and not (office._inside(_home(r), folder) or (r["cwd"] and office._inside(r["cwd"], folder))):
            continue
        asks = " ".join(store.loads(r["prompts"], []))
        common = want & records._stems(f"{r['title']} {r['first_prompt']} {asks}")
        if len(common) < min(2, len(want)):
            continue
        r = _decorate(r)
        r["common"] = sorted(common)
        out.append(r)
        if len(out) >= limit:
            break
    return out


def show(session_id: str) -> dict | None:
    r = store.row("SELECT * FROM agent_sessions WHERE id=?", (session_id,))
    return _decorate(r) if r else None


def stats() -> dict:
    return {"sessions": store.scalar("SELECT COUNT(*) FROM agent_sessions") or 0,
            "last": store.scalar("SELECT MAX(indexed_at) FROM agent_sessions")}


# --- где работать -------------------------------------------------------------------------------

def folders(settings: dict | None = None) -> list[dict]:
    """Куда можно поручить: папки «Офиса» и их проекты (папки первого уровня), как в «Офисе»."""
    settings = settings or config.load_settings()
    hide = office.hidden(settings)
    out = []
    for f in settings["office"].get("folders") or []:
        root = config.resolve_path(f)
        out.append({"path": str(root), "label": f"{root.name} — вся папка", "root": root.name})
        try:
            subs = sorted((p for p in root.iterdir() if p.is_dir() and not p.name.startswith((".", "_"))),
                          key=lambda p: p.name.casefold())
        except OSError:
            subs = []
        for p in subs:  # имена с диска на Mac бывают в другой записи кириллицы (NFD), скрытые — в NFC
            if unicodedata.normalize("NFC", p.name).casefold() not in hide:
                out.append({"path": str(p), "label": f"{root.name} / {p.name}", "root": root.name})
    return out


def allowed_folder(path: str, settings: dict | None = None) -> bool:
    """Поручать можно только в папки «Офиса» (или без папки)."""
    settings = settings or config.load_settings()
    return not path or (Path(path).is_dir() and any(
        office._inside(path, str(config.resolve_path(f))) for f in settings["office"].get("folders") or []))


def default_folder(c: dict, sims: list[dict], settings: dict | None = None) -> str:
    """Папка по умолчанию: где делали похожие задачи, иначе — проект с названием проекта задачи, иначе «Папка
    по умолчанию для поручений» из Настройки → Офис, иначе первая папка оттуда же."""
    settings = settings or config.load_settings()
    for s in sims:
        if s["home"] and allowed_folder(s["home"], settings):
            return s["home"]
    choices = folders(settings)
    title = ((c.get("project") or {}).get("title") or "").casefold()
    if title:
        for f in choices:
            name = unicodedata.normalize("NFC", Path(f["path"]).name).casefold()
            if not f["label"].endswith("вся папка") and (name in title or title in name):
                return f["path"]
    chosen = settings["office"].get("agent_folder") or ""
    if chosen and allowed_folder(chosen, settings):
        return chosen
    return choices[0]["path"] if choices else ""


# --- контекст задачи для навыка agent-brief ------------------------------------------------------

def task_text(c: dict) -> str:
    """По чему искать похожие сессии и документы: заголовок, суть, проект."""
    return " ".join(x for x in (records.headline(c), c.get("what") or "", (c.get("project") or {}).get("title") or "") if x)


def agentish(c: dict) -> bool:
    return bool(AGENTISH.search(f"{c.get('title') or ''} {c.get('what') or ''}"))


def _quote_time(meeting_id: str, c: dict) -> str:
    """Строка расшифровки, ближе всего к цитате задачи (или её сути): «[00:19:30] Спикер: …»."""
    path = records.meeting_dir(meeting_id) / "transcript.md"
    if not path.exists():
        return ""
    lines = [x for x in path.read_text(encoding="utf-8", errors="replace").splitlines()
             if re.match(r"\[\d{1,2}:\d{2}:\d{2}\]", x)]
    for text in (c.get("quote"), c.get("what")):  # сначала по цитате, не нашлось — по сути задачи
        want = records._stems(text)
        if not want:
            continue
        score, best = max(((len(want & records._stems(x)), x) for x in lines), default=(0, ""))
        if score >= min(3, len(want)):
            return best[:400]
    return ""


def _source(c: dict) -> list[str]:
    st, ref = c.get("source_type") or "", c.get("source_ref") or ""
    if st == "meeting" and ref:
        m = store.row("SELECT id, title, started_at FROM meetings WHERE id=?", (ref,))
        if not m:
            return [f"Откуда: встреча {ref} (уже удалена)"]
        folder = records.meeting_dir(ref)
        out = [f"Откуда: встреча «{m['title']}» {store.local(m['started_at'], '%d.%m.%Y')} (id {ref}) — "
               f"разбор {folder / 'summary.md'}, расшифровка {folder / 'transcript.md'}"]
        line = _quote_time(ref, c)
        if line:
            out.append(f"  В расшифровке похоже на это место: {line}")
        return out
    if st in ("telegram", "ymessenger", "pachca", "mail", "gmail") and ref and ref.isdigit():
        msg = store.row("SELECT * FROM messages WHERE id=?", (int(ref),))
        if msg:
            return [f"Откуда: сообщение [{msg['id']}] в чате «{msg['chat_title'] or msg['source']}» от {msg['sender_name'] or '?'} "
                    f"{store.local(msg['ts'], '%d.%m.%Y %H:%M')}: «{(msg['text'] or '').strip()[:600]}» "
                    f"(вокруг — ./ops messages show {msg['id']})"]
    if st and st != "manual":
        return [f"Откуда: {st}:{ref}"]
    return ["Откуда: добавлена вручную"]


def _task_lines(c: dict) -> list[str]:
    out = [f"Задача [{c['id']}] «{records.headline(c)}» — {records.route(c)} ({c['direction']}), статус {c['status']}"
           + (f", срок {c['due']}" + (" — ПРОСРОЧЕНО" if c.get("overdue") else "") if c.get("due") else ", без срока")]
    if c.get("what") and c["what"] != records.headline(c):
        out.append(f"Суть: {c['what']}")
    if c.get("quote"):
        out.append(f"Цитата: «{c['quote']}»")
    out += _source(c)
    if c.get("note"):
        out.append(f"Заметка: {c['note'][:500]}")
    if c.get("project"):
        from opsdash import projects

        p = projects.get(c["project"]["id"]) or {}
        out.append(f"Проект [{c['project']['id']}] «{c['project']['title']}»" + (f": {p['goal']}" if p.get("goal") else "")
                   + f" — подробно ./ops projects show {c['project']['id']}")
    for w in c.get("waits") or []:
        out.append(f"Ждёт: [{w['task']['id']}] «{w['task']['headline']}» ({w['task']['doer']})" if w["task"]
                   else f"Ждёт событие: {w['event']}" + (f" к {w['until']}" if w.get("until") else "")
                   + (" — готово" if w["done"] else ""))
    for h in c.get("holds") or []:
        out.append(f"Её ждёт: [{h['task']['id']}] «{h['task']['headline']}»")
    if c.get("parent"):
        out.append(f"Часть задачи [{c['parent']['id']}] «{c['parent']['headline']}» ({c['parent']['route']})")
    for k in c.get("children") or []:
        out.append(f"Подзадача [{k['id']}] {k['route']}: «{k['headline']}» ({k['status']})")
    if c.get("checklist"):
        out.append("Пункты: " + "; ".join(("✓ " if r["done"] else "☐ ") + r["text"] for r in c["checklist"]))
    if c.get("kaiten_card_id"):
        card = store.row("SELECT title, column_title, responsible_name FROM kaiten_cards WHERE id=?", (c["kaiten_card_id"],))
        out.append(f"Карточка Kaiten #{c['kaiten_card_id']}" + (f" «{card['title']}» ({card['column_title']}, "
                                                                f"{card['responsible_name'] or 'без исполнителя'})" if card else ""))
    return out


def _passport(folder: str) -> str:
    """Паспорт проекта (about project.md или README.md) — коротко, чтобы промт говорил на языке проекта."""
    if not folder:
        return ""
    for name in ("about project.md", "README.md"):
        p = Path(folder) / name
        if p.is_file():
            try:
                return f"{p}:\n" + p.read_text(encoding="utf-8", errors="replace")[:1500].strip()
            except OSError:
                return ""
    return ""


SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", ".claude"}


def _listing(folder: str, limit: int = 25) -> list[str]:
    """Свежие файлы папки, где будет работать агент (на два уровня вглубь): что уже лежит под рукой."""
    root = Path(folder) if folder else None
    if not root or not root.is_dir():
        return []
    found = []
    for depth, pattern in ((0, "*"), (1, "*/*")):
        for p in root.glob(pattern):
            if any(part.startswith(".") or part in SKIP_DIRS for part in p.relative_to(root).parts) or not p.is_file():
                continue
            try:
                found.append((p.stat().st_mtime, p))
            except OSError:
                continue
    found.sort(reverse=True)
    return [f"- {p} · изм. {datetime.fromtimestamp(m).strftime('%d.%m.%Y')}" for m, p in found[:limit] if _useful(str(p))]


def _candidate_files(sims: list[dict], c: dict, settings: dict) -> list[tuple[str, str]]:
    """Файлы с причиной: что использовали в похожих сессиях, что нашлось в документах по словам задачи."""
    used: Counter = Counter()
    made: dict[str, str] = {}
    for s in sims:
        for p in set(s["files_read"]) | set(s["files_written"]):
            if _useful(p) and Path(p).is_file():
                used[p] += 1
        for p in s["files_written"]:
            made.setdefault(p, s["title"])
    out = []
    for p, n in used.most_common(10):
        why = f"использовали в {n} похожих сессиях" if n > 1 else \
            (f"создали в сессии «{made[p]}»" if p in made else "использовали в похожей сессии")
        out.append((p, why))
    if settings["knowledge"].get("folders") and config.section_on("docs", settings):
        from opsdash import docs

        words = " ".join(list(dict.fromkeys(re.findall(r"\w{4,}", records.headline(c))))[:6])
        try:
            found = docs.search(words, limit=5) if words else []
        except Exception:  # noqa: BLE001 — индекс документов не обязателен
            found = []
        for d in found:
            if d["path"] not in used and _useful(d["path"]):
                out.append((d["path"], f"найден по словам «{words}»" + (f" · {d['part']}" if d.get("part") else "")))
    return out


def context(bid: int, settings: dict | None = None) -> str:
    """Всё для навыка agent-brief одним текстом: задача и её источник, где будет работать агент, похожие поручения,
    файлы с причинами, решения из базы знаний; прошлый промт и что поправить — при пересборке."""
    from opsdash import anthology

    settings = settings or config.load_settings()
    b = get(bid)
    if not b:
        raise ValueError(f"нет поручения {bid}")
    c = b["task"]
    if not c:
        raise ValueError("задачи больше нет")
    out = _task_lines(c)
    who = "Codex (приложение ChatGPT)" if b["engine"] == "codex" else "Claude Code (приложение Claude, вкладка Code)"
    out += ["", f"Кто будет делать: {who}.",
            f"Где будет работать: {b['folder'] or 'папка не выбрана — агент спросит или работает в своей'}"]
    passport = _passport(b["folder"])
    if passport:
        out += ["Паспорт проекта —", passport]
    sims = similar(task_text(c), limit=5, settings=settings, task=c["id"])
    out += ["", "Похожие прошлые сессии (подробно — ./ops agents show <id>):"]
    out += [f"- {s['id']} · {s['when']} · «{s['title']}» · {s['home']} · реплик человека: {s['n_prompts']}"
            + (f" · создали: {', '.join(f['name'] for f in s['made'][:4])}" if s["made"] else "")
            + (f" · итог: {s['outcome']}" if s["outcome"] else "") for s in sims] or ["- не нашлось"]
    listing = _listing(b["folder"])
    if listing:
        out += ["", "Свежие файлы в папке, где будет работать агент:"] + listing
    files = _candidate_files(sims, c, settings)
    out += ["", "Файлы-кандидаты (путь — почему; бери только нужные задаче):"]
    out += [f"- {p} — {why}" for p, why in files] or ["- не нашлось — поищи ./ops docs search \"<слова>\""]
    try:
        decisions = anthology.items(q=records.headline(c), limit=5) if config.section_on("kb", settings) else []
    except Exception:  # noqa: BLE001 — база знаний не обязательна
        decisions = []
    out += ["", "Решения из базы знаний по теме (./ops kb search — если нужно больше):"]
    out += [f"- {d.get('happened') or ''} {d['title']}" + (f" (источник {d['source_type']}:{d['source_ref'] or ''})"
                                                          if d.get("source_type") not in (None, "manual") else "")
            for d in decisions] or ["- не нашлось"]
    if b["prompt"] and b["status"] != "new":
        out += ["", f"Прошлый промт по задаче (с правками директора) — {brief_path(b)}"]
    if b["fix"]:
        out += [f"Директор просит поправить: «{b['fix']}»"]
    rel = _rel(brief_path(b))
    out += ["", f"Запиши промт в файл {rel} и сохрани: ./ops agents brief-save {bid} --file {rel}"]
    return "\n".join(out)


# --- поручения ---------------------------------------------------------------------------------

def _slug(text: str) -> str:
    return re.sub(r"[^a-zа-я0-9]+", "-", (text or "").lower().replace("ё", "е")).strip("-")[:40] or "zadacha"


def brief_path(b: dict) -> Path:
    """Полный промт поручения — data/agent-briefs/<задача>-<короткое имя>.md."""
    if b.get("brief_path"):
        return config.DATA_DIR / b["brief_path"]
    c = b.get("task") or store.row("SELECT * FROM commitments WHERE id=?", (b["commitment_id"],)) or {}
    return config.DATA_DIR / "agent-briefs" / f"{b['commitment_id']}-{_slug(records.headline(c) if c else '')}.md"


def _rel(path: Path) -> str:
    try:
        return str(path.relative_to(config.ROOT))
    except ValueError:
        return str(path)


def _split_file(item: str) -> tuple[str, str] | None:
    """«<путь> — зачем» → (путь, зачем). Тире бывает и в имени файла («Выгрузка CRM — октябрь.csv»): берём
    разрез, при котором файл существует, иначе — первый."""
    if item.startswith("`") and "`" in item[1:]:
        path, _, rest = item[1:].partition("`")
        return path.strip(), re.sub(r"^\s*[—–-]\s*", "", rest).strip()
    cuts = [m for m in re.finditer(r"\s+[—–-]\s+", item)]
    if not cuts:
        return None
    for m in cuts:
        if Path(os.path.expanduser(item[:m.start()].strip())).exists():
            return item[:m.start()].strip(), item[m.end():].strip()
    return item[:cuts[0].start()].strip(), item[cuts[0].end():].strip()


def parse_files(text: str) -> list[dict]:
    """Файлы из раздела «## Файлы» промта: «- <абсолютный путь> — зачем»."""
    out, inside = [], False
    for line in (text or "").splitlines():
        if line.startswith("#"):
            inside = line.lstrip("#").strip().lower().startswith("файлы")
            continue
        m = re.match(r"\s*[-*]\s+(`?(?:/|~|[A-Za-z]:[\\/]).+)$", line) if inside else None
        found = _split_file(m.group(1)) if m else None
        if found and not any(f["path"] == found[0] for f in out):
            out.append({"path": found[0], "why": found[1], "line": line})
    return out


def get(bid: int) -> dict | None:
    b = store.row("SELECT * FROM task_agents WHERE id=?", (bid,))
    return _brief(b) if b else None


def for_task(cid: int) -> dict | None:
    b = store.row("SELECT * FROM task_agents WHERE commitment_id=? ORDER BY id DESC LIMIT 1", (cid,))
    return _brief(b) if b else None


def _brief(b: dict) -> dict:
    b["task"] = records.get_commitment(b["commitment_id"])
    b["steps"] = store.loads(b["steps"], [])
    b["files"] = store.loads(b["files"], [])
    for f in b["files"]:  # галочка — строка с файлом есть в промте
        f["on"] = f["path"] in (b["prompt"] or "")
        f["exists"] = Path(os.path.expanduser(f["path"])).exists()
        f["name"] = Path(f["path"]).name
    b["similar"] = [s for s in (show(sid) for sid in store.loads(b["similar"], [])) if s]
    b["engine_title"] = ENGINES.get(b["engine"], b["engine"])
    return b


def start(cid: int, folder: str = "", engine: str = "claude", fix: str = "") -> int:
    """«Собрать промт» / «Пересобрать»: поручение задачи (одно на задачу) — в очередь чата."""
    from opsdash import jobs

    if not store.row("SELECT 1 FROM commitments WHERE id=?", (cid,)):
        raise ValueError("нет такой задачи")
    if not allowed_folder(folder):
        raise ValueError("папка — только из Настройки → Офис")
    engine = engine if engine in ENGINES else "claude"
    now = store.utcnow()
    b = store.row("SELECT * FROM task_agents WHERE commitment_id=? ORDER BY id DESC LIMIT 1", (cid,))
    if b and b["status"] == "running":
        raise ValueError("промт уже собирается")
    if b:
        store.execute("UPDATE task_agents SET folder=?, engine=?, fix=?, status='running', steps='[]', error=NULL, "
                      "updated=? WHERE id=?", (folder, engine, fix.strip() or None, now, b["id"]))
        bid = b["id"]
    else:
        bid = store.execute("INSERT INTO task_agents(commitment_id, folder, engine, status, created, updated) "
                            "VALUES(?,?,?,'running',?,?)", (cid, folder, engine, now, now))
    jobs.submit("agent-brief", "chat", {"brief_id": bid})
    return bid


def build(bid: int) -> str:
    """Задание «agent-brief»: Claude (или ChatGPT) собирает промт по навыку, шаги — в поручение по ходу."""
    from opsdash import brain, chat

    b = get(bid)
    if not b:
        return "поручения уже нет"
    if not b["task"]:
        store.execute("UPDATE task_agents SET status='failed', error='Задачи больше нет' WHERE id=?", (bid,))
        return "задачи больше нет"
    try:  # свежие сессии — в индекс до того, как Claude станет искать похожие
        index()
    except Exception as e:  # noqa: BLE001 — без свежего индекса промт всё равно соберётся
        store.log("agent-brief", f"индекс сессий не обновлён: {e}", "warning")
    sims = similar(task_text(b["task"]), limit=3, task=b["commitment_id"])
    store.execute("UPDATE task_agents SET similar=? WHERE id=?", (store.json_col([s["id"] for s in sims]), bid))
    prompt = (f"Собери промт для агента по задаче [{b['commitment_id']}] строго по навыку agent-brief "
              f"(.claude/skills/agent-brief/SKILL.md). Всё о задаче и прошлых поручениях: ./ops agents context {bid}. "
              "Это фоновый запуск: директор видит только шаги и готовый промт в окне задачи.")
    steps: list[str] = []

    def on_step(tool: str, params: dict) -> None:
        label = chat.step_label(tool, params)
        if label and (not steps or steps[-1] != label):
            steps.append(label)
            store.execute("UPDATE task_agents SET steps=?, updated=? WHERE id=?", (store.json_col(steps), store.utcnow(), bid))

    def on_start(proc) -> None:
        with _lock:
            _procs[bid] = proc

    try:
        brain.stream(prompt, model="opus", job="agent-brief", timeout_min=15, on_step=on_step, on_start=on_start,
                     partial=False)
    except brain.BrainError as e:
        stopped = bid in _stopped
        store.execute("UPDATE task_agents SET status='failed', error=?, updated=? WHERE id=?",
                      ("Остановлено" if stopped else str(e)[:1500], store.utcnow(), bid))
        if stopped:
            return "остановлено"
        raise
    finally:
        with _lock:
            _procs.pop(bid, None)
            _stopped.discard(bid)
    if (store.row("SELECT status FROM task_agents WHERE id=?", (bid,)) or {}).get("status") == "ready":
        return "промт готов"
    written = next((p for p in (config.APP_DIR / _rel(brief_path(b)), brief_path(b)) if p.exists()), None)
    if written and written.stat().st_mtime > time.time() - 30 * 60:  # записал файл, но не сохранил командой
        save(bid, written.read_text(encoding="utf-8"))
        return "промт готов"
    store.execute("UPDATE task_agents SET status='failed', error=?, updated=? WHERE id=?",
                  ("Промт не собрался — нажмите «Собрать промт» ещё раз", store.utcnow(), bid))
    raise RuntimeError("Claude не сохранил промт")


def stop(bid: int) -> bool:
    with _lock:
        proc = _procs.get(bid)
        if proc is None:
            return False
        _stopped.add(bid)
    proc.terminate()
    return True


def interrupted() -> None:
    """PULSE перезапустился: сборки, оставшиеся «в работе», прерваны."""
    store.execute("UPDATE task_agents SET status='failed', error='Прервано перезапуском PULSE — соберите ещё раз' "
                  "WHERE status='running'")


def save(bid: int, text: str) -> dict:
    """Промт поручения (от навыка — ./ops agents brief-save, от директора — правка в окне) — в базу и в файл.
    Служебная строка «PULSE: задача #…» — всегда в начале: по ней PULSE найдёт запущенную сессию."""
    b = store.row("SELECT * FROM task_agents WHERE id=?", (bid,))
    if not b:
        raise ValueError(f"нет поручения {bid}")
    text = (text or "").replace("\r\n", "\n").strip()
    if not text:
        raise ValueError("пустой промт")
    service = SERVICE.format(id=b["commitment_id"])
    if service not in text:
        lines = text.splitlines()
        at = 1 if lines and lines[0].startswith("# ") else 0
        text = "\n".join(lines[:at] + [service] + lines[at:])
    path = brief_path(b)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text + "\n", encoding="utf-8")
    files = store.loads(b["files"], [])
    if b["status"] == "running" or not files:  # новый промт от навыка — список файлов из него; правка — список прежний
        files = [{"path": f["path"], "why": f["why"]} for f in parse_files(text)]
    store.execute("UPDATE task_agents SET prompt=?, brief_path=?, files=?, status='ready', error=NULL, updated=? WHERE id=?",
                  (text, path.relative_to(config.DATA_DIR).as_posix(), store.json_col(files), store.utcnow(), bid))
    return get(bid)


def live(bid: int) -> dict:
    b = store.row("SELECT status, steps, error FROM task_agents WHERE id=?", (bid,))
    if not b:
        return {"status": "gone", "steps": []}
    return {"status": b["status"], "steps": store.loads(b["steps"], []), "error": b["error"] or ""}


# --- запуск ------------------------------------------------------------------------------------

def short_prompt(b: dict) -> str:
    """Короткое поручение для ссылки, когда промт длиннее, чем она несёт: остальное — в файле брифа."""
    c = b.get("task") or {}
    return (f"Задача из PULSE #{b['commitment_id']}" + (f" «{records.headline(c)}»" if c else "") + ".\n"
            f"{SERVICE.format(id=b['commitment_id'])}\n\n"
            f"Полный бриф — в файле {brief_path(b)}. Прочитай его целиком и действуй по нему; "
            "если что-то неясно — сначала спроси.")


def claude_link(b: dict, windows: bool | None = None) -> tuple[str, bool]:
    """Ссылка «новая сессия в приложении Claude»: промт — в поле ввода (сам не отправляется), папка — сессии.
    Длинный промт — коротким поручением с путём к файлу брифа. Второе значение — ушёл ли промт целиком.
    file= приложение Claude (4.4212) для новой сессии Code пока не прикладывает — путь к брифу есть в тексте."""
    def url(q: str, attach: bool) -> str:
        params = [("q", q)] + [("folder", b["folder"])] * bool(b.get("folder")) \
            + [("file", str(brief_path(b)))] * attach
        return "claude://code/new?" + urlencode(params, quote_via=quote)

    prompt = b.get("prompt") or ""
    if prompt and len(prompt) <= LINK_PROMPT:
        full = url(prompt, False)
        if not (os.name == "nt" if windows is None else windows) or len(full) <= WINDOWS_URL:
            return full, True
    return url(short_prompt(b), True), False


def open_url(url: str) -> None:
    """Открыть ссылку системно — как будто по ней щёлкнули: на Mac open, на Windows — оболочка."""
    if sys.platform == "darwin":
        subprocess.run(["open", url], check=True, timeout=30)
    elif os.name == "nt":
        os.startfile(url)  # noqa: S606 — ссылка claude://, собранная здесь же
    else:
        subprocess.run(["xdg-open", url], check=False, timeout=30)


def launch(bid: int, text: str | None = None) -> dict:
    """«Открыть в Claude»: сохранить правку, открыть новую сессию с промтом в поле ввода. Отправляет — человек."""
    if text is not None:
        save(bid, text)
    b = get(bid)
    if not b or not b["prompt"]:
        raise ValueError("промта ещё нет — соберите его")
    url, whole = claude_link(b)
    open_url(url)
    store.execute("UPDATE task_agents SET launched_at=? WHERE id=?", (store.utcnow(), bid))
    _marks.clear()
    return {"whole": whole, "url": url}


# --- запущенные поручения: сессия агента на задаче -----------------------------------------------

WATCH_DAYS = 14         # столько дней после «Открыть в Claude» ищем сессию и следим, не готова ли она
LATE_START = 120        # сессию могли начать чуть раньше отметки «открыли» (часы, задержка записи) — секунд
STATES = {"working": ("работает", "работает"), "waiting": ("ждёт вашего ответа", "ждёт"),
          "blocked": ("ждёт разрешения?", "ждёт"), "done": ("готово", "готово"), "rest": ("не активна", "агент")}
FINISHED = ("completed", "review_ready")  # итог хода по оценке приложения Claude: готово или на проверку
_marks: dict = {}       # задача → поручение и его сессия: на странице много карточек — считаем раз в несколько секунд
MARKS_TTL = 5


def _service(cid: int) -> re.Pattern:
    return re.compile(rf"PULSE: задача #{int(cid)}(?!\d)")


def _started(b: dict, s: dict) -> bool:
    """Сессия — по этому поручению: в первой реплике служебная строка или путь к файлу брифа, начата после
    «Открыть в Claude»."""
    first, launched = store.parse_ts(s["first"]), store.parse_ts(b["launched_at"])
    if not first or not launched or first < launched - timedelta(seconds=LATE_START):
        return False
    text = s["first_prompt"] or ""
    return bool(_service(b["commitment_id"]).search(text)) or bool(b["brief_path"] and Path(b["brief_path"]).name in text)


def watching() -> list[dict]:
    """Поручения, которые открыли в Claude за последние дни и которые ещё не закончены."""
    since = (datetime.now(timezone.utc) - timedelta(days=WATCH_DAYS)).replace(microsecond=0).isoformat()
    return store.rows("SELECT a.* FROM task_agents a JOIN commitments c ON c.id = a.commitment_id WHERE "
                      "a.launched_at >= ? AND c.status = 'open' AND COALESCE(a.state, '') != 'proposed'", (since,))


def link(settings: dict | None = None) -> int:
    """Найти сессии, запущенные из поручений: по служебной строке «PULSE: задача #…» в первой реплике (или пути
    к брифу). Ищем в папке поручения, без папки — во всех папках «Офиса»; берём самую свежую. Сколько нашли."""
    settings = settings or config.load_settings()
    roots = [config.resolve_path(f) for f in settings["office"].get("folders") or []]
    app = office.desktop_sessions()
    found = 0
    for b in watching():
        launched = store.parse_ts(b["launched_at"])
        since = launched.timestamp() - LATE_START
        best = None
        for root in [Path(b["folder"])] if b["folder"] else roots:
            for journal, s, room in office.sessions(root, since):  # скрытый проект — не помеха: папку выбрала директор
                if _started(b, s) and (not best or (s["first"] or "") > (best[1]["first"] or "")):
                    best = (journal, s, room, root)
        if not best or best[0].stem == b["session_id"]:
            continue
        journal, s, room, root = best
        try:
            st = journal.stat()
            _save(journal, s, root, room, app.get(journal.stem), (st.st_size, st.st_mtime))  # опыт — сразу в индекс
        except OSError:
            continue
        mine = app.get(journal.stem) or {}
        store.execute("UPDATE task_agents SET session_id=?, desktop_id=?, state='linked', result_files='[]' WHERE id=?",
                      (journal.stem, mine.get("id"), b["id"]))
        found += 1
    _marks.clear()
    return found


def session_state(b: dict, app: dict | None = None) -> dict | None:
    """Сессия поручения сейчас: работает, ждёт вашего ответа, готово; что нужно от вас, ссылка, созданные файлы.
    Состояние — как в «Офисе» (по журналу), итог — по оценке приложения Claude."""
    if not b.get("session_id"):
        return None
    r = store.row("SELECT * FROM agent_sessions WHERE id=?", (b["session_id"],)) or {}
    journal = Path(r["journal"]) if r.get("journal") else None
    s = office._session(journal) if journal and journal.exists() else None
    app = office.desktop_sessions() if app is None else app
    mine = app.get(b["session_id"]) or {}
    status = mine.get("status") or (r.get("status") or "")  # приложение забыло сессию — его последняя оценка из индекса
    now = datetime.now(timezone.utc)
    st = office._state(s, now) if s else "rest"
    if st == "waiting" and (mine.get("seen") or status in FINISHED):
        st = "rest"
    if status in FINISHED and st != "working":
        st = "done"
    written = s["written"] if s else store.loads(r.get("files_written"), [])
    files = [{"path": p, "name": Path(p).name} for p in reversed(written) if _useful(p) and Path(p).is_file()][:10]
    desktop = mine.get("id") or b.get("desktop_id") or r.get("desktop_id") or ""
    title, short = STATES[st]
    return {"state": st, "title": title, "short": short, "status": status,
            "needs": (mine.get("needs") or "") if st != "done" else "", "files": files,
            "session_title": r.get("title") or (s["title"] if s else "") or "Сессия агента",
            "last": (s or {}).get("last") or r.get("last"),
            "link": f"claude://code/continue?session={desktop}" if office._DESKTOP_ID.match(desktop or "") and
            not mine.get("archived") else ""}


def check(settings: dict | None = None) -> str:
    """Задание «agents-watch»: найти запущенные сессии, запомнить их файлы; готова — предложить закрыть задачу."""
    from opsdash import proposals

    found = link(settings)
    proposed = 0
    app = office.desktop_sessions()
    for b in watching():
        st = session_state(b, app)
        if not st:
            continue
        store.execute("UPDATE task_agents SET result_files=? WHERE id=?",
                      (store.json_col([f["path"] for f in st["files"]]), b["id"]))
        if st["state"] != "done":
            continue
        c = store.row("SELECT * FROM commitments WHERE id=?", (b["commitment_id"],))
        names = ", ".join(f["name"] for f in st["files"][:4])
        made = f"агент сделал: {names}" if names else "сессия агента закончила работу"
        try:
            proposals.propose_task_done(
                commitment_id=c["id"], source_ref=f"agent:{b['session_id']}",
                label=f"Закрыть задачу «{records.headline(c)}» — {made}",
                reason=f"Сессия «{st['session_title']}» по этой задаче готова"
                       + (" (приложение Claude: на проверку)" if st["status"] == "review_ready" else "")
                       + (f". Создала: {', '.join(f['name'] for f in st['files'])}" if st["files"] else ""))
        except proposals.ProposalError as e:
            store.log("agents-watch", f"не предложила закрыть задачу [{c['id']}]: {e}", "warning")
            continue
        store.execute("UPDATE task_agents SET state='proposed' WHERE id=?", (b["id"],))
        proposed += 1
    _marks.clear()
    return f"нашла сессий: {found}, предложила закрыть: {proposed}"


def marks() -> dict[int, dict]:
    """Задача → поручение (промт готов или открыт в Claude) и состояние его сессии — для окна задачи и метки
    на карточке. Считаем раз в несколько секунд: карточек на странице много, а приложение Claude читаем один раз."""
    if _marks.get("at", 0) > time.time() - MARKS_TTL:
        return _marks["data"]
    rows = store.rows("SELECT * FROM task_agents WHERE id IN (SELECT MAX(id) FROM task_agents GROUP BY commitment_id) "
                      "AND (prompt IS NOT NULL OR launched_at IS NOT NULL)")
    app = office.desktop_sessions() if any(r["session_id"] for r in rows) else {}
    data = {r["commitment_id"]: {"brief": r, "st": session_state(r, app)} for r in rows}
    _marks.update(at=time.time(), data=data)
    return data


def of(cid: int) -> dict | None:
    return marks().get(cid)


def task_of_sessions() -> dict[str, dict]:
    """Сессия агента → её задача: пометка «по задаче «…»» в «Офисе»."""
    rows = store.rows("SELECT a.session_id, c.* FROM task_agents a JOIN commitments c ON c.id = a.commitment_id "
                      "WHERE a.session_id IS NOT NULL")
    return {r["session_id"]: {"id": r["id"], "headline": records.headline(r)} for r in rows}
