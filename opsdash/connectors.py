"""Подключения (беклог CON-1, CON-2): описание источников из connectors.yaml и их состояние для каталога.

Описание — данные: название, группа, «что ловит», что станет возможно, что PULSE читает, шаги подключения.
Здесь — то, что требует кода: сделан ли шаг (CHECKS), состояние карточки из шагов и сбора, понятный текст ошибки.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml

from opsdash import config, store

MANIFEST = Path(__file__).with_name("connectors.yaml")
LOGOS = Path(__file__).parent / "web" / "static" / "logos"


@lru_cache(maxsize=1)
def manifest() -> dict:
    return yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))


def all_connectors() -> list[dict]:
    return manifest()["connectors"]


def get(cid: str) -> dict | None:
    return next((c for c in all_connectors() if c["id"] == cid), None)


def panel_ids() -> set[str]:
    """Источники с окном подключения поверх каталога — у остальных своя страница настроек (page)."""
    return {c["id"] for c in all_connectors() if not c.get("page")}


def logo_file(cid: str) -> str:
    """Имя файла логотипа в static/logos, если он есть; иначе — пусто, и каталог рисует плашку с буквами."""
    for ext in ("svg", "png"):
        if (LOGOS / f"{cid}.{ext}").exists():
            return f"{cid}.{ext}"
    return ""


# --- шаги: сделан ли шаг подключения (по настройкам, ключам и тому, что PULSE уже знает) ---------------

def _chats_on(source: str) -> bool:
    return bool(store.scalar("SELECT 1 FROM chats WHERE source=? AND enabled=1 LIMIT 1", (source,)))


CHECKS = {
    "telegram_keys": lambda s, sec: bool(s["telegram"].get("api_id") and sec.get("TELEGRAM_API_HASH")),
    "telegram_login": lambda s, sec: bool(store.get_state("telegram_me")),
    "telegram_chats": lambda s, sec: bool(s["telegram"].get("include_all_private")) or _chats_on("telegram"),
    "pachca_token": lambda s, sec: bool(sec.get("PACHCA_TOKEN")),
    "pachca_chats": lambda s, sec: _chats_on("pachca"),
    "slack_token": lambda s, sec: bool(sec.get("SLACK_TOKEN")),
    "slack_chats": lambda s, sec: _chats_on("slack"),
    "ymessenger_token": lambda s, sec: bool(sec.get("YM_BOT_TOKEN")),
    "ymessenger_chats": lambda s, sec: _chats_on("ymessenger"),
    "mail_password": lambda s, sec: bool(s["mail"].get("email") and sec.get("MAIL_APP_PASSWORD")),
    "gmail_password": lambda s, sec: bool(s["gmail"].get("email") and sec.get("GMAIL_APP_PASSWORD")),
    "calendar_password": lambda s, sec: bool(s["calendar"].get("login") and sec.get("CALDAV_APP_PASSWORD")),
    "google_ical": lambda s, sec: bool(sec.get("GOOGLE_CALENDAR_ICAL")),
    "imap_password": lambda s, sec: bool(s["imap"].get("email") and s["imap"].get("host") and sec.get("IMAP_PASSWORD")),
    "caldav_password": lambda s, sec: bool(s["caldav"].get("url") and s["caldav"].get("login")
                                           and sec.get("CALDAV_OTHER_PASSWORD")),
    "ics_urls": lambda s, sec: bool(sec.get("ICS_CALENDAR_URLS")),
    "kaiten_token": lambda s, sec: bool(s["kaiten"].get("domain") and sec.get("KAITEN_TOKEN")),
    "kaiten_spaces": lambda s, sec: bool(s["kaiten"].get("spaces")),
    "kaiten_targets": lambda s, sec: bool(s["kaiten"].get("targets")),
    "bot_token": lambda s, sec: bool(sec.get("TELEGRAM_BOT_TOKEN")),
    "bot_paired": lambda s, sec: bool(s["telegram_bot"].get("owner_chat_id")),
}

# понятный текст ошибки сбора: первое совпадение по куску сообщения (без учёта регистра); полный текст — в окне
ERRORS = [
    (("authenticationfailed", "invalid credentials", "login failed", "[auth]", "application-specific password",
      "username and password not accepted", "неверный пароль"), "пароль не подходит"),
    (("authkey", "session revoked", "session expired", "сессия"), "сессия устарела — войдите заново"),
    (("401", "unauthorized", "invalid token", "token is invalid", "токен"), "токен не принят"),
    (("403", "forbidden", "доступ"), "нет доступа"),
    (("404", "not found"), "адрес не найден"),
    (("timed out", "timeout", "connection", "network", "nodename", "name or service", "ssl", "eof occurred"),
     "нет связи с сервисом"),
]


def humanize(error: str | None) -> str:
    text = (error or "").lower()
    for needles, human in ERRORS:
        if any(n in text for n in needles):
            return human
    return "сбор не удался"


def assistant(which: str, settings: dict) -> dict:
    """Помощник на этом компьютере: найден ли, выполнен ли вход (None — узнать не удалось), кем вошли.
    Вход проверяется не чаще раза в минуту — страница настроек не запускает claude / codex на каждый показ."""
    if which == "claude":
        from opsdash import brain

        path = brain.find_claude(settings)
        login = brain.login_status(path, settings, max_age=60) if path else None
        who = (login or {}).get("email")
    else:
        from opsdash import codex

        path = codex.find(settings)
        login = codex.login_status(path, settings, max_age=60) if path else None
        who = (login or {}).get("how")
    return {"found": bool(path), "logged": None if login is None else bool(login.get("loggedIn")), "who": who or ""}


def _assistant_state(c: dict, settings: dict, out: dict) -> dict:
    """Claude и ChatGPT: работает один из двух (Настройки → Подключения → Помощник)."""
    from opsdash import brain

    engine = brain.engine(settings)
    a = assistant(c["assistant"], settings)
    ready = a["found"] and a["logged"] is not False
    problem = "не установлен" if not a["found"] else ("нужен вход" if not ready else "")
    out = {**out, "active": engine == c["assistant"], "who": a["who"]}
    if engine == c["assistant"]:
        if problem:
            return {**out, "kind": "error", "label": "выбран, но " + problem}
        return {**out, "kind": "ok", "label": "работает в PULSE"}
    if problem:
        return {**out, "kind": "off" if not a["found"] else "paused", "label": problem}
    return {**out, "kind": "standby", "label": "готов · сейчас работает " + brain.ENGINES[engine]}


def state(c: dict, settings: dict, secrets: dict, health: dict) -> dict:
    """Состояние карточки источника.

    kind: ok — работает; pending — ждёт первого сбора; setup — включён, но не хватает шага; error — сбор падает;
    paused — настроен, но выключен; off — не подключён; info — источник без шагов (своя страница настроек).
    """
    steps = [{"title": st["title"], "done": CHECKS[st["check"]](settings, secrets)} for st in c.get("steps", [])]
    todo = next((st for st in steps if not st["done"]), None)
    h = health.get(c.get("health") or "") or {}
    out = {"steps": steps, "todo": todo, "last_ok": h.get("last_ok"), "error": h.get("error") or "", "label": "",
           "title_now": c["title"]}
    if c.get("assistant"):
        return _assistant_state(c, settings, out)
    if c["id"] == "recordings":
        n = len(settings["recordings"].get("folders") or [])
        return {**out, "kind": "info", "label": f"папок с записями: {n}" if n else "папки не указаны"}
    if c["id"] == "knowledge":
        n = len(settings["knowledge"].get("folders") or [])
        return {**out, "kind": "info", "label": f"папок: {n}" if n else "папки не указаны"}
    enabled = bool(settings.get(c.get("section") or "", {}).get("enabled"))
    if not enabled:
        if any(st["done"] for st in steps):
            return {**out, "kind": "paused", "label": "выключено"}
        return {**out, "kind": "off", "label": "не подключено"}
    if todo:
        return {**out, "kind": "setup", "label": "нужна настройка: " + todo["title"][:1].lower() + todo["title"][1:]}
    if h.get("status") == "error":
        return {**out, "kind": "error", "label": "ошибка: " + humanize(h.get("error"))}
    if h.get("status") == "pending":
        return {**out, "kind": "pending", "label": "ждёт первого сбора"}
    return {**out, "kind": "ok", "label": "подключено"}


def catalog(settings: dict | None = None) -> dict:
    """Каталог подключений: группы с карточками и сводка «подключено N из M»."""
    from opsdash import overview

    from opsdash import brain

    settings = settings or config.load_settings()
    secrets = config.load_secrets()
    health = {h["key"]: h for h in overview.sources_health(settings)}
    groups = []
    for g in manifest()["groups"]:
        cards = []
        for c in all_connectors():
            if c["group"] != g["id"]:
                continue
            if c.get("archived_section") and not config.section_on(c["archived_section"], settings):
                continue  # «Документы» в архиве — карточки нет, раздел включается в «Архивных разделах»
            cards.append({**c, **state(c, settings, secrets, health), "logo": logo_file(c["id"]),
                          "panel": not c.get("page")})
        if cards:
            groups.append({**g, "cards": cards})
    from opsdash import mcp

    # сервисы через MCP (MORE-1): подключённые, витрина и «Свой сервер» — после источников, перед помощником;
    # в «подключено N из M» не входят (это не источники сбора)
    mcp_group = {"id": "mcp", "title": "Сервисы через MCP", "cards": mcp.cards(settings), "mcp": True}
    ai_at = next((i for i, g in enumerate(groups) if g["id"] == "ai"), len(groups))
    groups.insert(ai_at, mcp_group)
    cards = [card for g in groups for card in g["cards"] if not g.get("mcp")]
    sources = [card for card in cards if card.get("section")]
    on = sum(card["kind"] not in ("off", "paused") for card in sources)
    # выбранный помощник не работает — PULSE стоит целиком: это тоже «требует внимания»
    broken_ai = sum(bool(card.get("active")) and card["kind"] == "error" for card in cards)
    return {"groups": groups, "on": on, "total": len(sources), "engine": brain.name(settings), "soon": soon(settings),
            "engine_key": brain.engine(settings), "engines": brain.ENGINES,
            "problems": sum(card["kind"] in ("error", "setup") for card in sources) + broken_ai}


def soon(settings: dict) -> list[dict]:
    """«Скоро» (CON-8): серые карточки; «Нужно мне» — новая задача на GitHub в репозитории обновлений PULSE."""
    from urllib.parse import urlencode

    repo = (settings.get("updates") or {}).get("repo") or ""
    out = []
    for s in manifest().get("soon") or []:
        query = urlencode({"title": f"Нужно подключение: {s['title']}",
                           "body": f"Хочу, чтобы PULSE подключал {s['title']}.\n\nЧто важно: "})
        out.append({**s, "request": f"https://github.com/{repo}/issues/new?{query}" if repo else ""})
    return out


def card(cid: str, settings: dict | None = None) -> dict | None:
    """Одна карточка — для окна подключения."""
    for g in catalog(settings)["groups"]:
        for c in g["cards"]:
            if c["id"] == cid:
                return c
    return None


# --- «Что PULSE делает с данными» (беклог CON-7) ---------------------------------------------------------

def _size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    total = 0
    for p in path.rglob("*") if path.exists() else []:
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            pass
    return total


def human_size(n: int) -> str:
    for unit, div in (("ГБ", 1024 ** 3), ("МБ", 1024 ** 2), ("КБ", 1024)):
        if n >= div:
            v = n / div
            return (f"{v:.1f}" if v < 10 else f"{v:.0f}").replace(".", ",") + " " + unit
    return f"{n} Б"


def data_overview(settings: dict | None = None) -> dict:
    """Где лежат данные PULSE и сколько занимают; что помощник получал за неделю — по заданиям."""
    from datetime import datetime, timedelta, timezone

    from opsdash import brain, jobs

    settings = settings or config.load_settings()
    db = sum(_size(config.DB_FILE.with_name(config.DB_FILE.name + s)) for s in ("", "-wal", "-shm"))
    meetings = sum(1 for p in config.MEETINGS_DIR.iterdir() if p.is_dir()) if config.MEETINGS_DIR.exists() else 0
    places = [
        {"what": "Сообщения, письма, задачи, ход задач, встречи календаря, люди", "where": "data/ops.db",
         "size": human_size(db)},
        {"what": f"Записи и расшифровки встреч ({meetings})", "where": "data/meetings, data/recordings",
         "size": human_size(_size(config.MEETINGS_DIR) + _size(config.RECORDINGS_DIR))},
        {"what": "Модели распознавания речи", "where": "data/models", "size": human_size(_size(config.MODELS_DIR))},
        {"what": "Журналы работы", "where": "data/logs", "size": human_size(_size(config.LOGS_DIR))},
        {"what": "Пароли, токены и ключи подключений", "where": "secrets/.env",
         "size": f"ключей: {len(config.load_secrets())}"},
        {"what": "Настройки", "where": "config/settings.yaml", "size": human_size(_size(config.SETTINGS_FILE))},
    ]
    week = (datetime.now(timezone.utc) - timedelta(days=7)).replace(microsecond=0).isoformat()
    runs = store.rows("SELECT job, COUNT(*) AS n FROM claude_runs WHERE created >= ? GROUP BY job ORDER BY n DESC",
                      (week,))
    for r in runs:
        r["title"] = jobs.TITLES.get(r["job"], r["job"])
    return {"places": places, "runs": runs, "runs_total": sum(r["n"] for r in runs), "engine": brain.name(settings),
            "root": str(config.ROOT)}
