"""Хранилище: один файл SQLite (data/ops.db).

Здесь живут сырые сообщения, зеркало Kaiten и календаря, а также то, что
создаёт Claude: обещания, «нужен ответ», предложения на подтверждение.
Разборы встреч лежат рядом файлами Markdown (data/meetings/...).
Все отметки времени хранятся в UTC в формате ISO.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from contextlib import contextmanager
from functools import lru_cache
from datetime import datetime, timezone
from typing import Any, Iterable

from opsdash import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS docs (           -- рабочие документы из папок knowledge.folders (opsdash.docs)
    path TEXT PRIMARY KEY,
    folder TEXT,                     -- папка из настроек
    rel TEXT,                        -- путь внутри неё
    ext TEXT, title TEXT,
    size INTEGER, mtime REAL,
    status TEXT,                     -- indexed | error
    error TEXT, parts INTEGER, chars INTEGER,
    indexed_at TEXT
);

CREATE VIRTUAL TABLE IF NOT EXISTS docs_fts USING fts5(
    path UNINDEXED, part UNINDEXED, title, body,  -- part: «слайд 3», «лист «План»», «стр. 2»
    tokenize='unicode61 remove_diacritics 2', prefix='3 4'
);

CREATE TABLE IF NOT EXISTS anthology (      -- решения, уроки, удачи (opsdash.anthology)
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,              -- decision | lesson | win
    title TEXT NOT NULL,             -- суть одной строкой
    details TEXT,                    -- почему, контекст, цифры
    happened TEXT,                   -- YYYY-MM-DD: когда решили / случилось
    project TEXT,                    -- тема или направление: «ОРК», «Автоворонка»
    people TEXT DEFAULT '[]',        -- JSON: кто участвовал
    source_type TEXT,                -- meeting | document | telegram | ymessenger | mail | kaiten | manual
    source_ref TEXT,
    quote TEXT,
    status TEXT DEFAULT 'accepted',  -- accepted | proposed (предложено Claude) | outdated | rejected
    note TEXT,
    author TEXT,                     -- claude | director | notes (импорт заметок)
    created TEXT, updated TEXT
);

CREATE VIRTUAL TABLE IF NOT EXISTS anthology_fts USING fts5(
    title, details, project, tokenize='unicode61 remove_diacritics 2', prefix='3 4'
);

CREATE TABLE IF NOT EXISTS chats (
    source TEXT NOT NULL,            -- telegram | ymessenger | mail
    chat_id TEXT NOT NULL,
    title TEXT,
    kind TEXT,                       -- private | group | channel | mail
    enabled INTEGER DEFAULT 0,       -- читать этот чат
    last_ts TEXT,
    username TEXT,                   -- @имя в Telegram: различать тёзок в списке чатов
    PRIMARY KEY (source, chat_id)
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    chat_title TEXT,
    msg_id TEXT NOT NULL,
    sender_id TEXT,
    sender_name TEXT,
    is_outgoing INTEGER DEFAULT 0,
    text TEXT,
    ts TEXT NOT NULL,
    reply_to TEXT,
    link TEXT,
    processed INTEGER DEFAULT 0,
    UNIQUE (source, chat_id, msg_id)
);
CREATE INDEX IF NOT EXISTS ix_messages_processed ON messages(processed, ts);
CREATE INDEX IF NOT EXISTS ix_messages_chat ON messages(source, chat_id, ts);

CREATE TABLE IF NOT EXISTS events (
    uid TEXT PRIMARY KEY,
    calendar TEXT,
    title TEXT,
    start TEXT,
    "end" TEXT,
    all_day INTEGER DEFAULT 0,
    location TEXT,
    description TEXT,
    organizer TEXT,
    attendees TEXT,                  -- JSON [{email, name, status}]
    url TEXT,
    synced_at TEXT
);

CREATE TABLE IF NOT EXISTS kaiten_cards (
    id INTEGER PRIMARY KEY,
    space_id INTEGER, space_title TEXT,
    board_id INTEGER, board_title TEXT,
    column_id INTEGER, column_title TEXT,
    lane_id INTEGER, lane_title TEXT,
    title TEXT, description TEXT,
    state INTEGER, condition INTEGER,
    due_date TEXT,
    responsible_id INTEGER, responsible_name TEXT,
    owner_id INTEGER, owner_name TEXT,
    members TEXT,                    -- JSON [{id, name, username, type}]
    created TEXT, updated TEXT,
    last_moved_at TEXT, column_changed_at TEXT,
    url TEXT,
    synced_at TEXT
);

CREATE TABLE IF NOT EXISTS kaiten_boards (
    id INTEGER PRIMARY KEY,
    space_id INTEGER,
    title TEXT,
    columns TEXT,                    -- JSON [{id, title, type, sort_order}]
    lanes TEXT                       -- JSON [{id, title, sort_order}]
);

CREATE TABLE IF NOT EXISTS kaiten_users (
    id INTEGER PRIMARY KEY,
    full_name TEXT,
    username TEXT,
    email TEXT
);

CREATE TABLE IF NOT EXISTS kaiten_snooze (
    card_id INTEGER PRIMARY KEY,
    until TEXT,
    last_pinged TEXT
);

CREATE TABLE IF NOT EXISTS people (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    role TEXT,
    aliases TEXT DEFAULT '[]',       -- JSON: другие написания имени
    telegram TEXT,
    ymessenger TEXT,
    email TEXT,
    kaiten_user_id INTEGER,
    notes TEXT,
    updated TEXT
);

CREATE TABLE IF NOT EXISTS commitments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    direction TEXT NOT NULL,         -- mine (обещала я) | to_me (обещали мне) | other (между коллегами)
    who TEXT,                        -- кто должен сделать
    whom TEXT,                       -- кому
    what TEXT NOT NULL,              -- суть: что именно сделать, подробно
    title TEXT,                      -- короткий заголовок для карточки (2–6 слов); пусто — сокращаем what
    due TEXT,                        -- YYYY-MM-DD
    status TEXT DEFAULT 'open',      -- open | done | cancelled
    source_type TEXT,                -- meeting | telegram | ymessenger | mail | manual
    source_ref TEXT,
    quote TEXT,
    kaiten_card_id INTEGER,
    note TEXT,
    parent_id INTEGER,               -- часть какой задачи: директор передала работу дальше (Пётр → мне, для Тимура)
    seen_at TEXT,                    -- когда директор увидела задачу; пусто — «новая» (добавил Claude)
    created TEXT, updated TEXT, closed_at TEXT
);

CREATE TABLE IF NOT EXISTS attention (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id INTEGER,              -- messages.id
    source TEXT, chat_title TEXT, sender_name TEXT,
    summary TEXT NOT NULL,
    why TEXT,
    draft TEXT,
    priority TEXT DEFAULT 'normal',  -- high | normal | low
    status TEXT DEFAULT 'open',      -- open | done | dismissed
    created TEXT, updated TEXT
);

CREATE TABLE IF NOT EXISTS proposals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    payload TEXT NOT NULL,
    reason TEXT,
    source_ref TEXT,
    batch TEXT,
    status TEXT DEFAULT 'pending',   -- pending | done | rejected | failed
    result TEXT,
    created TEXT, decided_at TEXT
);

CREATE TABLE IF NOT EXISTS meetings (
    id TEXT PRIMARY KEY,
    title TEXT,
    started_at TEXT,
    duration_sec REAL,
    source_file TEXT,
    status TEXT DEFAULT 'new',       -- new | transcribing | transcribed | processing | processed | failed
    progress REAL DEFAULT 0,
    event_uid TEXT,
    participants TEXT DEFAULT '[]',
    headline TEXT,
    error TEXT,
    created TEXT, updated TEXT
);

CREATE TABLE IF NOT EXISTS files_seen (
    path TEXT PRIMARY KEY,
    size INTEGER,
    mtime REAL,
    meeting_id TEXT,
    seen_at TEXT
);

CREATE TABLE IF NOT EXISTS jobs (
    name TEXT PRIMARY KEY,
    last_run TEXT,
    last_ok TEXT,
    last_error TEXT,
    last_duration REAL,
    running INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    text TEXT NOT NULL,
    buttons TEXT,                    -- JSON inline_keyboard
    urgent INTEGER DEFAULT 0,
    created TEXT,
    sent_at TEXT,
    tg_message_id INTEGER,
    error TEXT,
    sending_at TEXT,                 -- захвачено отправителем (защита от двойной отправки)
    dead INTEGER DEFAULT 0           -- не отправится никогда (ошибка Telegram или устарело)
);

CREATE TABLE IF NOT EXISTS state (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT,
    level TEXT,
    job TEXT,
    message TEXT
);
"""

_init_lock = threading.Lock()
_initialized: set[str] = set()

# колонки, добавленные после первой версии: (таблица, колонка, определение[, что заполнить у старых строк])
MIGRATIONS = [
    ("outbox", "sending_at", "TEXT"),
    ("outbox", "dead", "INTEGER DEFAULT 0"),
    ("chats", "username", "TEXT"),
    ("commitments", "title", "TEXT"),
    ("commitments", "parent_id", "INTEGER"),
    # задачи, которые были до появления отметки «новая», — уже просмотрены
    ("commitments", "seen_at", "TEXT", "UPDATE commitments SET seen_at=COALESCE(created, updated, '')"),
]


def _migrate(conn: sqlite3.Connection) -> None:
    for table, column, ddl, *backfill in MIGRATIONS:
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
            for sql in backfill:
                conn.execute(sql)
    # до 26.10.2026 для личных чатов Telegram сохранялись ссылки t.me/<человек>/<id> — такие не открываются
    conn.execute("UPDATE messages SET link=NULL WHERE source='telegram' AND chat_id NOT LIKE '-%' AND link IS NOT NULL")


def utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def to_utc_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=config.tz())
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def parse_local(value: str | None) -> datetime | None:
    """Как parse_ts, но время без пояса считается местным (так пишут люди и Claude)."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00").replace(" ", "T"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=config.tz())
    return dt


def local(value: str | datetime | None, fmt: str = "%d.%m %H:%M") -> str:
    dt = parse_ts(value) if isinstance(value, str) else value
    if not dt:
        return ""
    return dt.astimezone(config.tz()).strftime(fmt)


@lru_cache(maxsize=512)
def _like_regex(pattern: str, escape: str | None) -> re.Pattern:
    out, i = [], 0
    while i < len(pattern):
        ch = pattern[i]
        if escape and ch == escape and i + 1 < len(pattern):
            out.append(re.escape(pattern[i + 1]))
            i += 2
            continue
        out.append(".*" if ch == "%" else "." if ch == "_" else re.escape(ch))
        i += 1
    return re.compile("".join(out), re.IGNORECASE | re.DOTALL)


def _like(pattern, value, escape=None):
    """LIKE без учёта регистра для кириллицы (встроенный в SQLite понимает только латиницу)."""
    if pattern is None or value is None:
        return None
    return _like_regex(str(pattern), escape).fullmatch(str(value)) is not None


def connect() -> sqlite3.Connection:
    path = str(config.DB_FILE)
    config.DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.create_function("like", 2, _like, deterministic=True)
    conn.create_function("like", 3, _like, deterministic=True)
    conn.create_function("lower", 1, lambda s: s.lower() if isinstance(s, str) else s, deterministic=True)
    conn.execute("PRAGMA foreign_keys=ON")
    if path not in _initialized:
        with _init_lock:
            if path not in _initialized:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.executescript(SCHEMA)
                _migrate(conn)
                conn.commit()
                _initialized.add(path)
    return conn


@contextmanager
def db():
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def rows(sql: str, params: Iterable[Any] = ()) -> list[dict]:
    with db() as conn:
        return [dict(r) for r in conn.execute(sql, tuple(params)).fetchall()]


def row(sql: str, params: Iterable[Any] = ()) -> dict | None:
    with db() as conn:
        r = conn.execute(sql, tuple(params)).fetchone()
        return dict(r) if r else None


def execute(sql: str, params: Iterable[Any] = ()) -> int:
    """Выполнить запрос; вернуть lastrowid (для INSERT) или число изменённых строк."""
    with db() as conn:
        cur = conn.execute(sql, tuple(params))
        return cur.lastrowid if sql.lstrip().upper().startswith("INSERT") else cur.rowcount


def scalar(sql: str, params: Iterable[Any] = ()) -> Any:
    with db() as conn:
        r = conn.execute(sql, tuple(params)).fetchone()
        return r[0] if r else None


# --- state (чекпоинты сборщиков) -------------------------------------------

def get_state(key: str, default: Any = None) -> Any:
    r = row("SELECT value FROM state WHERE key=?", (key,))
    if not r:
        return default
    try:
        return json.loads(r["value"])
    except (TypeError, ValueError):
        return r["value"]


def set_state(key: str, value: Any) -> None:
    execute(
        "INSERT INTO state(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, json.dumps(value, ensure_ascii=False)),
    )


# --- журнал ----------------------------------------------------------------

def log(job: str, message: str, level: str = "info") -> None:
    execute("INSERT INTO log(ts, level, job, message) VALUES(?,?,?,?)", (utcnow(), level, job, message[:4000]))
    execute("DELETE FROM log WHERE id < (SELECT MAX(id) - 5000 FROM log)")


# --- сообщения -------------------------------------------------------------

def upsert_chat(source: str, chat_id: str, title: str | None, kind: str | None, enabled: int | None = None,
                username: str | None = None, last_ts: str | None = None) -> None:
    """last_ts — время последнего сообщения в чате (из списка диалогов): по нему чаты в настройках
    сортируются «свежие сверху», даже те, что PULSE не читает."""
    existing = row("SELECT enabled FROM chats WHERE source=? AND chat_id=?", (source, chat_id))
    if existing:
        execute(
            "UPDATE chats SET title=COALESCE(?, title), kind=COALESCE(?, kind), username=COALESCE(?, username), "
            "last_ts=CASE WHEN ? > COALESCE(last_ts, '') THEN ? ELSE last_ts END WHERE source=? AND chat_id=?",
            (title, kind, username, last_ts, last_ts, source, chat_id),
        )
        if enabled is not None:
            execute("UPDATE chats SET enabled=? WHERE source=? AND chat_id=?", (enabled, source, chat_id))
    else:
        execute(
            "INSERT INTO chats(source, chat_id, title, kind, enabled, username, last_ts) VALUES(?,?,?,?,?,?,?)",
            (source, chat_id, title, kind, enabled or 0, username, last_ts),
        )


def add_message(msg: dict) -> bool:
    """Сохранить сообщение. True — если оно новое."""
    with db() as conn:
        cur = conn.execute(
            """INSERT OR IGNORE INTO messages
               (source, chat_id, chat_title, msg_id, sender_id, sender_name, is_outgoing, text, ts, reply_to, link)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                msg["source"], str(msg["chat_id"]), msg.get("chat_title"), str(msg["msg_id"]),
                msg.get("sender_id"), msg.get("sender_name"), int(bool(msg.get("is_outgoing"))),
                msg.get("text") or "", msg["ts"], msg.get("reply_to"), msg.get("link"),
            ),
        )
        new = cur.rowcount > 0
        if new:
            conn.execute(
                "UPDATE chats SET last_ts=? WHERE source=? AND chat_id=? AND (last_ts IS NULL OR last_ts < ?)",
                (msg["ts"], msg["source"], str(msg["chat_id"]), msg["ts"]),
            )
        return new


def json_col(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def loads(value: Any, default: Any = None) -> Any:
    if not value:
        return default
    if not isinstance(value, (str, bytes)):
        return value  # уже разобрано
    try:
        return json.loads(value)
    except ValueError:
        return default
