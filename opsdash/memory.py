"""«Память» PULSE: файл-приёмник для тех, кто не пишет в базу сам (MCP-сервер открыт только на чтение).

Строка в data/memory/inbox.jsonl — JSON с полем type; разбирает её сам PULSE (absorb_inbox — в задании
memory-index и перед `./ops recall`).
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from opsdash import config, store


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


def link_chunks() -> int:
    """Сущности во фрагментах — появятся с фактами (этап 2)."""
    return 0


def recall_part(query: str, **_) -> dict | None:
    """Факты и страница для «Вспомнить» — появятся с фактами (этап 2)."""
    return None
