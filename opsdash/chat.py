"""«Спросить Claude»: разговор с Claude о своих данных прямо в PULSE.

Каждый чат — своя сессия Claude Code: следующий вопрос продолжает её (--resume), поэтому к разговору
можно вернуться через неделю. К вопросу прикрепляют контекст — встречу (Claude читает расшифровку
целиком, а не только разбор), задачу, документ из папок или файл с компьютера. Ответ пишется по ходу
(brain.stream): видно, что Claude делает и что уже написал. Во внешние системы он, как везде, только
предлагает — «Да / Нет» под ответом.
"""

from __future__ import annotations

import re
import shutil
import threading
import time
from pathlib import Path
from urllib.parse import quote

from opsdash import brain, config, records, store

MODELS = {"opus": "Подробно · Opus", "sonnet": "Быстро · Sonnet"}
IMAGES = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
HISTORY = 8  # если сессию Claude продолжить нельзя — сколько прошлых реплик пересказать в новой

_procs: dict[int, object] = {}  # id ответа → процесс Claude: чтобы его можно было остановить
_stopped: set[int] = set()
_lock = threading.Lock()


def files_dir(chat_id: int) -> Path:
    return config.DATA_DIR / "chats" / str(int(chat_id))


def create(title: str, model: str = "opus") -> int:
    now = store.utcnow()
    return store.execute("INSERT INTO ask_chats(title, model, created, updated) VALUES(?,?,?,?)",
                         (title, model if model in MODELS else "opus", now, now))


def get(chat_id: int) -> dict | None:
    return store.row("SELECT * FROM ask_chats WHERE id=?", (chat_id,))


def list_chats(limit: int = 100) -> list[dict]:
    return store.rows("SELECT c.*, (SELECT COUNT(*) FROM ask_messages m WHERE m.chat_id=c.id AND m.role='user') AS n "
                      "FROM ask_chats c ORDER BY updated DESC LIMIT ?", (limit,))


def messages(chat_id: int) -> list[dict]:
    """Реплики чата; у ответа — шаги Claude и предложения, которые он сделал (кнопки «Да / Нет»)."""
    from opsdash import proposals

    out = store.rows("SELECT * FROM ask_messages WHERE chat_id=? ORDER BY id", (chat_id,))
    for m in out:
        m["context"] = describe(store.loads(m["context"], []))
        m["steps"] = store.loads(m["steps"], [])
        m["proposals"] = proposals.list_proposals("all", batch=f"chat:{m['id']}") if m["role"] == "assistant" else []
    return out


def delete(chat_id: int) -> None:
    from opsdash import proposals

    for m in store.rows("SELECT id FROM ask_messages WHERE chat_id=? AND role='assistant'", (chat_id,)):
        stop(m["id"])
        for p in proposals.list_proposals("pending", batch=f"chat:{m['id']}"):
            proposals.reject(p["id"])
    store.execute("DELETE FROM ask_messages WHERE chat_id=?", (chat_id,))
    store.execute("DELETE FROM ask_chats WHERE id=?", (chat_id,))
    shutil.rmtree(files_dir(chat_id), ignore_errors=True)


# --- контекст -----------------------------------------------------------------------

def parse_refs(values: list[str]) -> list[dict]:
    """Приложенное из формы: «meeting:<id>», «task:<id>», «doc:<путь>» — без дублей и несуществующего."""
    out, seen = [], set()
    for v in values:
        kind, _, ref = (v or "").partition(":")
        ref = ref.strip()
        if not ref or (kind, ref) in seen:
            continue
        if kind == "meeting" and store.row("SELECT 1 FROM meetings WHERE id=?", (ref,)):
            out.append({"type": "meeting", "id": ref})
        elif kind == "task" and ref.isdigit() and store.row("SELECT 1 FROM commitments WHERE id=?", (int(ref),)):
            out.append({"type": "task", "id": int(ref)})
        elif kind == "doc" and store.row("SELECT 1 FROM docs WHERE path=?", (ref,)):
            out.append({"type": "doc", "path": ref})
        else:
            continue
        seen.add((kind, ref))
    return out


def save_file(chat_id: int, name: str, data: bytes) -> dict:
    """Файл с компьютера — в data/chats/<id>/. Презентации, таблицы, Word и PDF Claude напрямую не читает:
    рядом кладём их текст по частям («## слайд 3»). Картинки он смотрит сам."""
    from opsdash import docs

    folder = files_dir(chat_id)
    folder.mkdir(parents=True, exist_ok=True)
    name = re.sub(r'[\\/:*?"<>|]+', "_", Path(name or "файл").name).strip(". ") or "файл"
    path = folder / name
    n = 2
    while path.exists():
        path = folder / f"{Path(name).stem} ({n}){Path(name).suffix}"
        n += 1
    path.write_bytes(data)
    item = {"type": "file", "name": path.name, "path": _rel(path)}
    ext = path.suffix.lower()
    if ext in docs.EXTS - docs.TEXT:
        try:
            parts = docs.extract(path)
            text = "\n\n".join(f"## {part}\n{body}" if part else body for part, body in parts)
            txt = path.with_name(path.name + ".md")
            txt.write_text(f"# {path.name}\n\n{text}", encoding="utf-8")
            item["text"] = _rel(txt)
        except Exception as e:  # noqa: BLE001 — файл всё равно приложен, Claude скажет, что не прочитал
            item["error"] = str(e)[:200]
    elif ext not in docs.TEXT and ext not in IMAGES:
        item["error"] = "такой формат не читается — подойдут документы, таблицы, презентации, PDF, текст и картинки"
    return item


def _rel(path: Path) -> str:
    """Путь для Claude: он работает в папке программы, данные обычно лежат в ней же (data/…)."""
    try:
        return str(path.relative_to(config.APP_DIR))
    except ValueError:
        return str(path)


def describe(items: list[dict]) -> list[dict]:
    """Подписи приложенного для показа: label — коротко, href — куда ведёт."""
    out = []
    for c in items:
        c = dict(c)
        if c["type"] == "meeting":
            m = store.row("SELECT title, started_at FROM meetings WHERE id=?", (c["id"],))
            c["label"] = f"встреча «{m['title'] or c['id']}» {store.local(m['started_at'], '%d.%m')}" if m \
                else f"встреча {c['id']} (удалена)"
            c["href"] = f"/meetings/{c['id']}" if m else None
        elif c["type"] == "task":
            t = store.row("SELECT * FROM commitments WHERE id=?", (c["id"],))
            c["label"] = f"задача «{records.headline(t)}»" if t else "задача (удалена)"
            c["href"] = f"/commitments?task={c['id']}" if t else None
        elif c["type"] == "doc":
            d = store.row("SELECT rel, title FROM docs WHERE path=?", (c["path"],))
            c["label"] = f"документ «{(d or {}).get('rel') or Path(c['path']).name}»"
            c["href"] = f"/docs/view?path={quote(c['path'])}" if d else None
        else:
            c["label"] = f"файл «{c['name']}»"
            c["href"] = None
        out.append(c)
    return out


def _context_prompt(items: list[dict]) -> str:
    lines = []
    for c in items:
        if c["type"] == "meeting":
            m = store.row("SELECT title, started_at, status FROM meetings WHERE id=?", (c["id"],))
            if not m:
                continue
            folder = _rel(records.meeting_dir(c["id"]))
            lines.append(f"- Встреча «{m['title']}» {store.local(m['started_at'], '%d.%m.%Y %H:%M')} — папка {folder}/: "
                         f"расшифровка {folder}/transcript.md (читай её целиком, а не только разбор), "
                         f"разбор {folder}/summary.md, данные {folder}/meta.json.")
        elif c["type"] == "task":
            t = records.get_commitment(c["id"])
            if not t:
                continue
            line = f"- Задача [{t['id']}] «{records.headline(t)}» ({records.route(t)}" \
                   + (f", до {t['due']}" if t["due"] else "") + f", {t['status']})"
            if t["what"] != records.headline(t):
                line += f": {t['what']}"
            if t["source_type"]:
                line += f" · источник {t['source_type']}:{t['source_ref'] or ''}"
            if t.get("meeting"):
                line += f" · встреча по задаче «{t['meeting']['title']}» {store.local(t['meeting']['start'])}"
            lines.append(line)
        elif c["type"] == "doc":
            d = store.row("SELECT rel FROM docs WHERE path=?", (c["path"],))
            lines.append(f"- Документ «{(d or {}).get('rel') or c['path']}»: ./ops docs show \"{c['path']}\" "
                         "(нужное место — --part).")
        elif c["type"] == "file":
            if c.get("error"):
                lines.append(f"- Файл «{c['name']}» ({c['path']}) — прочитать не получилось: {c['error']}.")
            elif c.get("text"):
                lines.append(f"- Файл «{c['name']}» — его текст по частям: {c['text']} (Read).")
            else:
                lines.append(f"- Файл «{c['name']}»: {c['path']} (Read).")
    return ("Директор приложила к вопросу:\n" + "\n".join(lines) + "\n\n") if lines else ""


# --- вопрос и ответ -----------------------------------------------------------------

def ask(chat_id: int | None, text: str, context: list[dict], model: str | None = None) -> tuple[int, int]:
    """Новый вопрос (в новом или прежнем чате). Ответ Claude пишет в фоне; вернуть (чат, ответ)."""
    from opsdash import jobs

    text = (text or "").strip()
    if chat_id is None:
        chat_id = create(title_for(text, context), model or "opus")
    elif model in MODELS:
        store.execute("UPDATE ask_chats SET model=? WHERE id=?", (model, chat_id))
    now = store.utcnow()
    store.execute("INSERT INTO ask_messages(chat_id, role, text, context, created, updated) VALUES(?,?,?,?,?,?)",
                  (chat_id, "user", text, store.json_col(context), now, now))
    answer = store.execute("INSERT INTO ask_messages(chat_id, role, status, created, updated) "
                           "VALUES(?, 'assistant', 'running', ?, ?)", (chat_id, now, now))
    store.execute("UPDATE ask_chats SET updated=? WHERE id=?", (now, chat_id))
    jobs.submit("chat", "chat", {"message_id": answer})
    return chat_id, answer


def retry(message_id: int) -> bool:
    """«Спросить ещё раз» у ответа, который не получился."""
    from opsdash import jobs

    m = store.row("SELECT * FROM ask_messages WHERE id=? AND role='assistant'", (message_id,))
    if not m or m["status"] == "running":
        return False
    store.execute("UPDATE ask_messages SET status='running', text='', steps='[]', error=NULL, updated=? WHERE id=?",
                  (store.utcnow(), message_id))
    jobs.submit("chat", "chat", {"message_id": message_id})
    return True


def stop(message_id: int) -> bool:
    with _lock:
        proc = _procs.get(message_id)
        if proc is None:
            return False
        _stopped.add(message_id)
    proc.terminate()
    return True


def title_for(text: str, context: list[dict]) -> str:
    """Название чата — начало первого вопроса (или что приложено, если вопроса нет)."""
    first = re.sub(r"\s+", " ", text).strip()
    if not first and context:
        first = describe(context[:1])[0]["label"]
    return (first[:70].rsplit(" ", 1)[0] + "…") if len(first) > 70 else (first or "Новый чат")


def step_label(tool: str, params: dict) -> str:
    """Что Claude делает — по-человечески: «Читает расшифровку «КПЭ…»», «Ищет в переписке: склад»."""
    if tool == "Bash":
        cmd = (params.get("command") or "").strip()
        m = re.match(r"(?:\./)?ops\s+(\S+)(?:\s+(\S+))?(.*)", cmd)
        if not m:
            return "Выполняет команду"
        group, action, rest = m.group(1), m.group(2) or "", m.group(3) or ""
        query = re.search(r'"([^"]+)"', rest) or re.search(r'"([^"]+)"', action)
        q = f": «{query.group(1)}»" if query else ""
        return {
            ("meetings", "search"): f"Ищет во встречах{q}", ("messages", "search"): f"Ищет в переписке{q}",
            ("docs", "search"): f"Ищет в документах{q}", ("docs", "show"): f"Открывает документ{q}",
            ("kb", "search"): f"Ищет в базе знаний{q}",
        }.get((group, action)) or {
            "meetings": "Смотрит встречи", "messages": "Смотрит переписку", "commitments": "Смотрит задачи",
            "kaiten": "Смотрит Kaiten", "docs": "Смотрит документы", "kb": "Смотрит базу знаний",
            "calendar": "Смотрит календарь", "people": "Смотрит справочник людей", "propose": "Готовит предложение",
            "attention": "Смотрит «Нужен ответ»", "overview": "Смотрит сводку", "briefs": "Смотрит брифы",
        }.get(group, f"Выполняет ./ops {group}")
    path = params.get("file_path") or params.get("path") or ""
    name = Path(path).name
    if tool == "Read":
        m = re.search(r"data/meetings/([^/]+)/(transcript|summary)\.md", path)
        if m:
            row = store.row("SELECT title FROM meetings WHERE id=?", (m.group(1),))
            what = "расшифровку" if m.group(2) == "transcript" else "разбор"
            return f"Читает {what} «{row['title']}»" if row else f"Читает {what} встречи"
        if name == "SKILL.md":
            return "Открывает инструкцию"
        return f"Читает «{name}»" if name else "Читает файл"
    if tool in ("Write", "Edit"):
        return f"Записывает «{name}»" if name else "Записывает файл"
    if tool == "Grep":
        return f"Ищет «{params.get('pattern', '')}» в файлах"
    if tool == "Glob":
        return "Ищет файлы"
    if tool == "Skill":
        return f"Открывает навык «{params.get('skill') or params.get('name') or ''}»"
    return tool


def _history(chat_id: int, before: int) -> str:
    rows = store.rows("SELECT role, text FROM ask_messages WHERE chat_id=? AND id < ? AND status='done' AND text != '' "
                      "ORDER BY id DESC LIMIT ?", (chat_id, before, HISTORY))
    if not rows:
        return ""
    lines = [f"{'Директор' if r['role'] == 'user' else 'Ты'}: {r['text'][:1500]}" for r in reversed(rows)]
    return "Начало этого разговора (прежняя сессия недоступна):\n" + "\n\n".join(lines) + "\n\n"


def run(message_id: int) -> str:
    """Задание «chat»: ответить на вопрос перед ответом message_id, показывая ход работы."""
    m = store.row("SELECT * FROM ask_messages WHERE id=?", (message_id,))
    if not m:
        return "ответа уже нет"
    chat = get(m["chat_id"])
    if not chat:
        return "чат удалён"
    q = store.row("SELECT * FROM ask_messages WHERE chat_id=? AND role='user' AND id < ? ORDER BY id DESC LIMIT 1",
                  (chat["id"], message_id))
    context = store.loads(q["context"], []) if q else []
    question = q["text"] if q else ""
    prompt_tail = (
        _context_prompt(context)
        + f"Если создаёшь предложения (./ops propose ...), указывай --batch chat:{message_id} — кнопки «Да / Нет» "
        "появятся под твоим ответом.\n\n"
        + f"Вопрос: {question or '(без текста — разберись с приложенным)'}"
    )
    head = ("Директор пишет тебе в разделе «Спросить Claude» на дашборде PULSE (веб, не Telegram). "
            "Действуй по навыку chat (.claude/skills/chat/SKILL.md).\n\n")
    steps: list[str] = []
    state = {"saved": 0.0, "text": ""}

    def save(force: bool = False) -> None:
        if force or time.time() - state["saved"] > 0.7:
            state["saved"] = time.time()
            store.execute("UPDATE ask_messages SET text=?, steps=?, updated=? WHERE id=?",
                          (state["text"], store.json_col(steps), store.utcnow(), message_id))

    def on_text(text: str) -> None:
        state["text"] = text
        save()

    def on_step(tool: str, params: dict) -> None:
        label = step_label(tool, params)
        if not steps or steps[-1] != label:
            steps.append(label)
        save(force=True)

    def on_start(proc) -> None:
        with _lock:
            _procs[message_id] = proc

    model = chat["model"] if chat["model"] in MODELS else "opus"
    try:
        try:
            res = brain.stream(head + prompt_tail if not chat["session_id"] else prompt_tail, model=model,
                               resume=chat["session_id"], job="chat", timeout_min=30,
                               on_text=on_text, on_step=on_step, on_start=on_start)
        except brain.BrainError:
            if not chat["session_id"] or message_id in _stopped:
                raise
            # прежнюю сессию продолжить нельзя (её удалили, сменился компьютер) — начинаем новую с пересказом
            res = brain.stream(head + _history(chat["id"], message_id) + prompt_tail, model=model, job="chat",
                               timeout_min=30, on_text=on_text, on_step=on_step, on_start=on_start)
    except brain.BrainError as e:
        stopped = message_id in _stopped
        store.execute("UPDATE ask_messages SET status='failed', text=?, steps=?, error=?, updated=? WHERE id=?",
                      (state["text"], store.json_col(steps), "Остановлено" if stopped else str(e)[:1500],
                       store.utcnow(), message_id))
        if stopped:
            return "остановлено"
        raise
    finally:
        with _lock:
            _procs.pop(message_id, None)
            _stopped.discard(message_id)
    store.execute("UPDATE ask_messages SET status='done', text=?, steps=?, error=NULL, updated=? WHERE id=?",
                  (res["text"] or state["text"] or "Готово.", store.json_col(steps), store.utcnow(), message_id))
    store.execute("UPDATE ask_chats SET session_id=?, updated=? WHERE id=?",
                  (res.get("session_id") or chat["session_id"], store.utcnow(), chat["id"]))
    return "ответ готов"


def live(chat_id: int) -> dict:
    """Для страницы чата, пока Claude пишет: последний ответ — текст, шаги, состояние."""
    m = store.row("SELECT id, text, steps, status FROM ask_messages WHERE chat_id=? AND role='assistant' "
                  "ORDER BY id DESC LIMIT 1", (chat_id,))
    if not m:
        return {"running": False}
    return {"running": m["status"] == "running", "id": m["id"], "status": m["status"], "text": m["text"] or "",
            "steps": store.loads(m["steps"], [])}
