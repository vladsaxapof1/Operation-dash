"""Уведомления в правом нижнем углу: что PULSE только что сделал — разобрал сообщения, расшифровал
и разобрал встречу, отправил дайджест, ответил в чате — и что делает сейчас.

Появляются после фоновых заданий (jobs.run_job), висят, пока их не скроют — по одному (✕ или переходом по нему) или «Скрыть все»;
показываем за последние сутки. Ошибка одного и того же задания не плодит новые уведомления, а обновляет
прежнее: сбор сообщений каждые 15 минут иначе засыпал бы угол одинаковыми ошибками.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from opsdash import store

KEEP_HOURS = 24
LIMIT = 20
# успешное выполнение этих заданий — не новость: они идут постоянно или директор и так видит результат
QUIET = {"housekeeping", "docs-index", "ask", "voice", "recordings", "update-check"}
# «Сейчас» в углу: что делает Claude или расшифровка; сбор и обслуживание — секунды, о них не пишем
BUSY = {"triage", "process-meeting", "transcribe", "digest", "weekly", "groups", "briefs", "followup", "anthology",
        "company-context", "chat", "update", "projects"}


def add(kind: str, title: str, text: str = "", link: str | None = None, level: str = "info",
        merge_hours: float = 0) -> int:
    """Новое уведомление. merge_hours — если такое же (задание и заголовок) не скрыто и появилось недавно,
    обновить его и поднять наверх, а не добавлять ещё одно."""
    now = store.utcnow()
    if merge_hours:
        since = (datetime.now(timezone.utc) - timedelta(hours=merge_hours)).replace(microsecond=0).isoformat()
        old = store.row("SELECT id FROM notices WHERE kind=? AND title=? AND dismissed_at IS NULL AND created >= ? "
                        "ORDER BY id DESC LIMIT 1", (kind, title, since))
        if old:
            store.execute("DELETE FROM notices WHERE id=?", (old["id"],))  # новым id — чтобы встало наверх
    return store.execute("INSERT INTO notices(kind, title, text, link, level, created) VALUES(?,?,?,?,?,?)",
                         (kind, title, (text or "")[:400], link, level, now))


def recent(after: int = 0, limit: int = LIMIT) -> list[dict]:
    """Не скрытые за последние сутки, свежие сверху; after — только новее этого id (для обновления страницы)."""
    since = (datetime.now(timezone.utc) - timedelta(hours=KEEP_HOURS)).replace(microsecond=0).isoformat()
    return store.rows("SELECT * FROM notices WHERE dismissed_at IS NULL AND created >= ? AND id > ? "
                      "ORDER BY id DESC LIMIT ?", (since, after, limit))


def dismiss(notice_id: int) -> None:
    store.execute("UPDATE notices SET dismissed_at=? WHERE id=?", (store.utcnow(), notice_id))


def dismiss_all() -> None:
    store.execute("UPDATE notices SET dismissed_at=? WHERE dismissed_at IS NULL", (store.utcnow(),))


def running() -> list[dict]:
    """Что PULSE делает прямо сейчас — для строки «Сейчас» над уведомлениями."""
    from opsdash import jobs

    out = []
    for j in store.rows("SELECT name FROM jobs WHERE running=1 ORDER BY last_run"):
        if j["name"] not in BUSY:
            continue
        item = {"name": j["name"], "title": jobs.TITLES.get(j["name"], j["name"]), "detail": ""}
        if j["name"] == "transcribe":
            m = store.row("SELECT title, progress FROM meetings WHERE status='transcribing' ORDER BY updated DESC LIMIT 1")
            if m:
                item["detail"] = f"«{m['title']}» · {round((m['progress'] or 0) * 100)}%"
        elif j["name"] == "triage":
            n = store.scalar("SELECT COUNT(*) FROM messages WHERE processed=0") or 0
            item["detail"] = f"{n} новых" if n else ""
        out.append(item)
    return out


def _triage_link(result: str) -> str:
    """Куда ведёт «Разобрал новые сообщения» — туда, где прибавилось больше: новые задачи (поровну — тоже они)
    или «Нужен ответ». Ни того ни другого, а есть предложения — на сводку, где их подтверждают."""
    found = {k: int(n) for k, n in re.findall(r"(нужен ответ|задач|на подтверждение): \+(\d+)", result)}
    replies, tasks = found.get("нужен ответ", 0), found.get("задач", 0)
    if replies > tasks:
        return "/inbox"
    if tasks:
        return "/commitments?new=1"
    return "/" if found.get("на подтверждение") else "/inbox"


def _meeting(meeting_id: str | None) -> dict:
    if not meeting_id:
        return {}
    return store.row("SELECT id, title, headline, status FROM meetings WHERE id=?", (meeting_id,)) or {}


def from_job(name: str, args: dict | None, result: str | None, error: str | None) -> None:
    """Задание закончилось — уведомить, если есть о чём. Ошибки внутри не мешают самому заданию."""
    try:
        _from_job(name, args or {}, result or "", error)
    except Exception as e:  # noqa: BLE001
        store.log("notices", f"уведомление о «{name}» не создано: {e}", "warning")


def _from_job(name: str, args: dict, result: str, error: str | None) -> None:
    if not error and (name in QUIET or name.startswith("collect:")):
        return
    s = summary(name, args, result, error)
    if s:
        add(name, s["title"], s["text"], s["link"], s["level"], merge_hours=6 if error else 0)


def summary(name: str, args: dict, result: str, error: str | None) -> dict | None:
    """Итог задания по-человечески: {title, text, link, level}. None — сказать нечего («новых сообщений нет»).
    Нужен уведомлению в углу и разделу «Офис» (opsdash.office)."""
    from opsdash import jobs

    def done(title: str, text: str = "", link: str | None = None, level: str = "ok") -> dict:
        return {"title": title, "text": text, "link": link, "level": level}

    if error:
        return done(f"Не получилось: {jobs.TITLES.get(name, name)}", error.splitlines()[0][:300], "/journal", "bad")
    mid = args.get("meeting_id")
    if name == "triage":
        if result.startswith("разобрано"):
            return done("Разобрал новые сообщения", result, _triage_link(result))
    elif name == "process-meeting":
        m = _meeting(mid)
        if m:
            return done(f"Разобрал встречу «{m['title'] or mid}»", m.get("headline") or "", f"/meetings/{mid}")
    elif name == "transcribe":
        m = _meeting(mid)
        if result.startswith("остановлено"):
            if not result.endswith(("отменили", "встречу удалили")):  # остановили кнопкой — и так видно
                return done("Остановил расшифровку", result.removeprefix("остановлено: "), "/meetings", "info")
        elif m.get("status") in ("transcribed", "processing", "processed"):
            minutes = result.rpartition(": ")[2] if result.endswith(" мин") else ""
            return done(f"Расшифровал встречу «{m['title'] or mid}»", minutes, f"/meetings/{mid}")
    elif name == "digest":
        return done(result[:1].upper() + result[1:], "", "/digests")
    elif name == "weekly":
        return done("Итоги недели готовы", "", "/digests")
    elif name == "groups":
        if not result.startswith("в рабочих группах"):
            return done("Сводка рабочих групп готова", result, "/digests")
    elif name == "briefs":
        if result.startswith("брифов готово"):
            return done("Брифы к встречам", result, "/calendar")
    elif name == "followup":
        return done("Текст итогов встречи готов", "", f"/meetings/{mid}?tab=followup" if mid else "/meetings")
    elif name == "anthology":
        return done("Обзор недели для базы знаний готов", "Решения, уроки и удачи — подтвердите или отклоните", "/kb")
    elif name == "projects":
        return done("Собрал проекты", result, "/" if result.startswith("на подтверждение") else "/commitments?view=projects")
    elif name == "company-context":
        return done("Обновил раздел «О компании»", "", "/kb/context")
    elif name == "stale":
        if result.startswith("напоминаний") and not result.endswith(": 0"):
            return done("Напомнил о задачах Kaiten без движения", result, "/tasks", "info")
    elif name == "chat":
        if result == "ответ готов":
            m = store.row("SELECT c.id, c.title FROM ask_messages m JOIN ask_chats c ON c.id=m.chat_id WHERE m.id=?",
                          (args.get("message_id"),))
            if m:
                return done("Claude ответил", f"«{m['title']}»", f"/ask/{m['id']}")
    elif name == "update":
        return done("PULSE обновлён", result, "/settings?tab=general#updates")
    return None
