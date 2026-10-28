"""Настройки (config/settings.yaml) и секреты (secrets/.env).

Настройки — то, что можно показывать и менять в админке. Секреты (пароли,
токены, сессия Telegram) лежат отдельно и никогда не выводятся целиком.
"""

from __future__ import annotations

import copy
import functools
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml


def root_dir() -> Path:
    env = os.environ.get("OPSDASH_HOME")
    if env:
        return Path(env).resolve()
    return Path(__file__).resolve().parent.parent


ROOT = root_dir()  # данные и настройки
APP_DIR = Path(__file__).resolve().parent.parent  # программа: CLAUDE.md, навыки, документация
CONFIG_DIR = ROOT / "config"
SECRETS_DIR = ROOT / "secrets"
DATA_DIR = ROOT / "data"
SETTINGS_FILE = CONFIG_DIR / "settings.yaml"
SECRETS_FILE = SECRETS_DIR / ".env"
DB_FILE = DATA_DIR / "ops.db"
MEETINGS_DIR = DATA_DIR / "meetings"
RECORDINGS_DIR = DATA_DIR / "recordings"
MODELS_DIR = DATA_DIR / "models"
LOGS_DIR = DATA_DIR / "logs"

DEFAULTS: dict = {
    "owner": {
        "name": "",
        "timezone": "Europe/Moscow",
        "work_hours": {"start": "09:00", "end": "20:00", "days": [1, 2, 3, 4, 5]},
    },
    "web": {"port": 8765, "theme": "system"},  # theme: system / light / dark
    # откуда брать новые версии PULSE (opsdash.updates); закрытый репозиторий — с токеном GITHUB_TOKEN в secrets
    "updates": {"repo": "vladsaxapof1/Operation-dash", "branch": "claude/pult-v1", "auto_check": True},
    "claude": {
        "path": "",
        "model_meetings": "opus",
        "model_triage": "sonnet",
        "model_digest": "sonnet",
        "model_ask": "sonnet",
        "model_briefs": "sonnet",
        "timeout_minutes": 30,
    },
    "schedule": {
        "collect_minutes": 15,
        "triage_minutes": 120,
        "kaiten_minutes": 30,
        "calendar_minutes": 30,
        "docs_minutes": 30,  # индекс рабочих документов
        "anthology_weekly": "16:00",  # по пятницам Claude предлагает решения, уроки и удачи недели
        "context_weekly": "08:00",  # по понедельникам Claude обновляет раздел «О компании» сводки контекста
        "digest_morning": "09:00",
        "digest_evening": "18:30",
        "stale_check": "10:00",
        "weekly_day": 5,  # итоги недели: день (1 — понедельник … 7 — воскресенье) и время
        "weekly_time": "18:00",
        "groups_digest": "19:00",  # сводка рабочих групп Telegram за день; пусто — не делать
        "briefs_minutes": 60,  # как часто проверять, к каким встречам нужен бриф
    },
    "briefs": {
        "enabled": True,
        "hours_ahead": 36,  # готовить бриф к встречам на ближайшие N часов
        # такие встречи не разбираем: это не встречи, а занятое время
        "ignore": ["тайм-аут", "обед", "фокус", "личное", "дорога", "не беспокоить"],
    },
    "reminders": {
        # напоминание коллеге по задаче «мне должны»: {имя}, {задача}, {срок}
        "template": "{имя}, привет! Напоминаю про «{задача}» ({срок}). Как продвигается?",
    },
    "recordings": {
        "folders": ["data/recordings"],
        "whisper_model": "large-v3-turbo",
        "engine": "auto",  # auto: MLX на Apple Silicon, иначе faster-whisper
        "device": "cpu",
        "language": "ru",
        "diarization": True,
        "diarization_threshold": 0.9,
        "auto_process": True,
        "hf_endpoint": "",
    },
    "telegram": {
        "enabled": False,
        "api_id": "",
        "phone": "",
        "include_all_private": False,
        "backfill_days": 3,
    },
    "telegram_bot": {
        "enabled": False,
        "owner_chat_id": "",
        "quiet_hours": {"start": "21:00", "end": "08:30"},
    },
    "ymessenger": {"enabled": False, "my_login": ""},
    "mail": {
        "enabled": False,
        "email": "",
        "host": "imap.yandex.ru",
        "folders": ["INBOX"],
        "include_sent": True,
        "backfill_days": 3,
        "ignore_senders": ["noreply", "no-reply", "mailer-daemon", "notifications"],
    },
    "google_calendar": {"enabled": False},  # только чтение; секретные адреса iCal — в secrets (GOOGLE_CALENDAR_ICAL)
    "gmail": {  # второй ящик; «не обращать внимания» — общий список из mail
        "enabled": False,
        "email": "",
        "host": "imap.gmail.com",
        "folders": ["INBOX"],
        "include_sent": True,
        "primary_only": True,
        "backfill_days": 3,
    },
    "calendar": {
        "enabled": False,
        "login": "",
        "url": "https://caldav.yandex.ru",
        "calendars": [],
        "write_calendar": "",
        "days_back": 2,
        "days_ahead": 14,
    },
    "kaiten": {
        "enabled": False,
        "domain": "",
        "spaces": [],
        "exclude_boards": [],
        "stale_days": 3,
        "stale_states": [2],
        "stale_basis": "moved",
        "targets": [],
        "default_target": "",
        "nudge_text": "@{username}, карточка без движения {days} дн. Как продвигается? Есть ли блокеры?",
    },
    "knowledge": {
        "folders": [],
        # папки, которые не индексируем: служебные, код, черновики сборок
        "exclude": ["node_modules", "venv", "__pycache__", "_staging", "site", "dist", "build"],
    },
    "autonomy": {
        "kaiten_create_for_me": "confirm",
        "kaiten_create_for_others": "confirm",
        "kaiten_comment": "confirm",
        "kaiten_update": "confirm",
        "calendar": "confirm",
        "task_done": "confirm",  # закрывать задачи PULSE, которые по встречам и переписке выглядят выполненными
        "attention_done": "confirm",  # убирать «нужен ответ», когда по её сообщению видно, что она ответила
    },
    "notifications": {
        "meeting_processed": True,
        "urgent_immediately": True,
        "stale_pings": True,
        "max_stale_in_ping": 10,
        "groups_digest": False,  # присылать сводку групп в Telegram (в разделе «Дайджесты» она есть всегда)
    },
}

SECRET_KEYS = {
    "TELEGRAM_API_HASH": "Telegram: api_hash",
    "TELEGRAM_SESSION": "Telegram: сессия аккаунта",
    "TELEGRAM_BOT_TOKEN": "Telegram-бот: токен",
    "YM_BOT_TOKEN": "Яндекс Мессенджер: токен бота",
    "MAIL_APP_PASSWORD": "Яндекс Почта: пароль приложения",
    "GMAIL_APP_PASSWORD": "Gmail: пароль приложения",
    "CALDAV_APP_PASSWORD": "Яндекс Календарь: пароль приложения",
    "GOOGLE_CALENDAR_ICAL": "Google Календарь: секретные адреса iCal",
    "KAITEN_TOKEN": "Kaiten: API-токен",
}

_lock = threading.RLock()


def deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


_cache: dict = {"key": None, "value": None}


def load_settings() -> dict:
    """Настройки с подстановкой значений по умолчанию. Файл перечитывается, только если изменился."""
    with _lock:
        try:
            st = SETTINGS_FILE.stat()
            key = (str(SETTINGS_FILE), st.st_mtime_ns, st.st_size)
        except OSError:
            key = (str(SETTINGS_FILE), None, None)
        if _cache["key"] != key:
            user = {}
            if key[1] is not None:
                user = yaml.safe_load(SETTINGS_FILE.read_text(encoding="utf-8")) or {}
            _cache["key"], _cache["value"] = key, deep_merge(DEFAULTS, user)
        return copy.deepcopy(_cache["value"])


def _replace(tmp: Path, target: Path) -> None:
    """os.replace с повторами: на Windows файл бывает ненадолго занят (антивирус, другой процесс)."""
    for attempt in range(20):
        try:
            tmp.replace(target)
            return
        except PermissionError:
            if attempt == 19:
                raise
            time.sleep(0.1)


def save_settings(settings: dict) -> None:
    with _lock:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        header = (
            "# Настройки PULSE. Удобнее менять в админке: http://localhost:8765/settings\n"
            "# Пароли и токены здесь не хранятся — они в secrets/.env\n"
        )
        body = yaml.safe_dump(settings, allow_unicode=True, sort_keys=False, width=120)
        tmp = SETTINGS_FILE.with_suffix(".tmp")
        tmp.write_text(header + body, encoding="utf-8")
        _replace(tmp, SETTINGS_FILE)
        _cache["key"] = None


def update_settings(section: str, values: dict) -> dict:
    with _lock:
        settings = load_settings()
        settings[section] = deep_merge(settings.get(section, {}), values)
        save_settings(settings)
        return settings


# --- секреты ---------------------------------------------------------------

def load_secrets() -> dict[str, str]:
    with _lock:
        out: dict[str, str] = {}
        if not SECRETS_FILE.exists():
            return out
        for line in SECRETS_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            out[key.strip()] = value.strip()
        return out


def get_secret(key: str) -> str:
    return os.environ.get(key) or load_secrets().get(key, "")


def set_secret(key: str, value: str | None) -> None:
    with _lock:
        SECRETS_DIR.mkdir(parents=True, exist_ok=True)
        secrets = load_secrets()
        if value:
            secrets[key] = value.replace("\n", "").replace("\r", "")
        else:
            secrets.pop(key, None)
        lines = ["# Секреты PULSE. Не пересылайте этот файл и не кладите в облако."]
        lines += [f"{k}={v}" for k, v in secrets.items()]
        tmp = SECRETS_FILE.with_suffix(".tmp")
        tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
        _replace(tmp, SECRETS_FILE)


def mask(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "••••"
    return value[:3] + "••••" + value[-3:]


# --- время -----------------------------------------------------------------

def tz(settings: dict | None = None) -> ZoneInfo:
    name = (settings or load_settings())["owner"].get("timezone") or "Europe/Moscow"
    return _zone(name)


@functools.lru_cache(maxsize=16)
def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except Exception:  # noqa: BLE001
        return ZoneInfo("Europe/Moscow")


def now(settings: dict | None = None) -> datetime:
    return datetime.now(tz(settings))


def hhmm(value, default: str = "") -> str:
    """Время из настроек → 'ЧЧ:ММ'. YAML превращает 18:30 без кавычек в число минут (1110)."""
    if value is None or value == "":
        return default
    if isinstance(value, int):
        return f"{value // 60:02d}:{value % 60:02d}"
    text = str(value).strip()
    if len(text) == 4 and text[1] == ":":
        text = "0" + text
    return text


def resolve_path(p: str) -> Path:
    """Путь из настроек: относительный — от корня проекта."""
    path = Path(os.path.expandvars(os.path.expanduser(p)))
    return path if path.is_absolute() else (ROOT / path)


def ensure_dirs() -> None:
    for d in (CONFIG_DIR, SECRETS_DIR, DATA_DIR, MEETINGS_DIR, RECORDINGS_DIR, MODELS_DIR, LOGS_DIR):
        d.mkdir(parents=True, exist_ok=True)
