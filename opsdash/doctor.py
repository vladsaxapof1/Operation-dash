"""Здоровье PULSE (беклог CON-5): одна проверка на всё — источники и система.

Показывают её `./ops doctor`, блок «Здоровье» в Настройки → Подключения и мастер первого запуска. Раз в пять минут
(обслуживание, jobs.job_housekeeping) watch() смотрит, не замолчал ли источник и не падает ли он дольше часа, —
и один раз в день по каждому источнику пишет директору: в углу экрана и в Telegram.

Строка: group (sources / system), key, title, level (ok / warn / bad / off), text — что сейчас, fix — что сделать,
link — где это сделать.
"""

from __future__ import annotations

import shutil
import sys
from datetime import datetime, timedelta, timezone

from opsdash import config, store

ORDER = {"bad": 0, "warn": 1, "ok": 2, "off": 3}
JOBS_STALE_MIN = 15  # обслуживание идёт каждые 5 минут; дольше 15 — фоновые задания стоят
FAILING_MIN = 60  # источник падает дольше часа — это уже не случайный сбой, а повод написать директору
DISK_BAD_GB, DISK_WARN_GB = 2, 10


def _row(group: str, key: str, title: str, level: str, text: str, fix: str = "", link: str = "") -> dict:
    return {"group": group, "key": key, "title": title, "level": level, "text": text, "fix": fix, "link": link}


def _ago(ts: str | None) -> timedelta | None:
    dt = store.parse_ts(ts)
    return datetime.now(timezone.utc) - dt if dt else None


def _human(td: timedelta) -> str:
    minutes = int(td.total_seconds() // 60)
    if minutes < 60:
        return f"{max(minutes, 1)} мин"
    hours = minutes // 60
    return f"{hours} ч" if hours < 48 else f"{hours // 24} дн."


def silent_after(key: str, settings: dict) -> timedelta:
    """Сколько источник может не собираться, пока это не тревожно: четыре интервала сбора, но не меньше полутора часов."""
    return timedelta(minutes=max(4 * config.source_minutes(key, settings), 90))


# --- источники ---------------------------------------------------------------------------------------------

def source_rows(settings: dict, catalog: dict | None = None, include_off: bool = False) -> list[dict]:
    """Источники из каталога подключений (connectors): что с каждым и что сделать."""
    from opsdash import connectors

    catalog = catalog or connectors.catalog(settings)
    jobs = {j["name"]: j for j in store.rows("SELECT * FROM jobs")}
    out = []
    for g in catalog["groups"]:
        for c in g["cards"]:
            if not c.get("section"):
                continue
            link = f"/settings?tab={c['id']}"
            title, kind = c["title"], c["kind"]
            if kind in ("off", "paused"):
                if include_off:
                    out.append(_row("sources", c["id"], title, "off", c["label"], "", link))
                continue
            if kind == "setup":
                out.append(_row("sources", c["id"], title, "warn", c["label"],
                                f"Откройте окно {title} и пройдите шаг «{c['todo']['title']}».", link))
            elif kind == "error":
                out.append(_row("sources", c["id"], title, "bad", c["label"],
                                f"Откройте окно {title} и нажмите «Проверить подключение». Подробно: {c['error'][:200]}",
                                link))
            elif kind == "pending":
                out.append(_row("sources", c["id"], title, "warn", "ждёт первого сбора",
                                "Первый сбор — в течение нескольких минут после запуска PULSE.", link))
            else:
                text, level, fix = "работает", "ok", ""
                if c["id"] in config.SOURCES:
                    ago = _ago(c.get("last_ok") or (jobs.get(f"collect:{c['id']}") or {}).get("last_ok"))
                    if ago is not None:
                        text = f"собрано {_human(ago)} назад"
                        if ago > silent_after(c["id"], settings):
                            level = "warn"
                            text = f"не собирается {_human(ago)}"
                            fix = "Проверьте, что PULSE запущен, и нажмите «Проверить подключение» в окне источника."
                elif c["id"] == "bot":
                    text = "привязан"
                out.append(_row("sources", c["id"], title, level, text, fix, link))
    return out


# --- система -----------------------------------------------------------------------------------------------

def _assistant(settings: dict) -> dict:
    from opsdash import brain, connectors

    engine = brain.engine(settings)
    name = brain.ENGINES[engine]
    a = connectors.assistant(engine, settings)
    link = f"/settings?tab={engine}"
    if not a["found"]:
        return _row("system", "assistant", "Помощник", "bad", f"{name} не установлен",
                    f"Без помощника PULSE не разбирает встречи и сообщения. Установите {name} — инструкция в окне.", link)
    if a["logged"] is False:
        return _row("system", "assistant", "Помощник", "bad", f"{name}: нужен вход",
                    f"Откройте окно {name} и нажмите «Войти».", link)
    return _row("system", "assistant", "Помощник", "ok", name + (f" · {a['who']}" if a["who"] else ""), "", link)


def _jobs() -> dict:
    """Фоновые задания идут: обслуживание (каждые 5 минут) проходило недавно."""
    last = (store.row("SELECT last_run FROM jobs WHERE name='housekeeping'") or {}).get("last_run")
    ago = _ago(last)
    if ago is None:
        return _row("system", "jobs", "Фоновые задания", "warn", "ещё не запускались",
                    "Запустите PULSE (START): без него нет сбора, разборов и дайджестов.", "/journal")
    if ago > timedelta(minutes=JOBS_STALE_MIN):
        return _row("system", "jobs", "Фоновые задания", "bad", f"не идут {_human(ago)}",
                    "PULSE не запущен или компьютер спал. Запустите PULSE (START) — сбор догонит сам.", "/journal")
    return _row("system", "jobs", "Фоновые задания", "ok", "идут", "", "/journal")


def _transcribe(settings: dict) -> dict:
    from opsdash import recordings, transcribe

    link = "/settings?tab=recordings"
    folders = settings["recordings"].get("folders") or []
    found = recordings.watch_folders(settings)
    if folders and len(found) < len(folders):
        missing = [f for f in folders if config.resolve_path(f) not in found]
        return _row("system", "transcribe", "Записи встреч", "warn", f"папка не найдена: {missing[0]}",
                    "Проверьте путь в окне «Записи встреч» — новые записи из неё не расшифруются.", link)
    week = store.to_utc_iso(datetime.now(timezone.utc) - timedelta(days=7))
    failed = store.scalar("SELECT COUNT(*) FROM meetings WHERE status='failed' AND created >= ?", (week,)) or 0
    engine = "видеоядра Apple Silicon (MLX)" if transcribe.engine(settings) == "mlx" else "процессор (faster-whisper)"
    if failed:
        return _row("system", "transcribe", "Записи встреч", "warn",
                    f"не расшифровано за неделю: {failed}", "Откройте «Встречи» — у таких записей есть причина и «Повторить».",
                    "/meetings")
    return _row("system", "transcribe", "Записи встреч", "ok", f"расшифровка на этом компьютере: {engine}", "", link)


def _bot(settings: dict) -> dict:
    link = "/settings?tab=bot"
    tb = settings["telegram_bot"]
    if not tb.get("enabled"):
        return _row("system", "bot", "Уведомления", "warn", "Telegram-бот не подключён",
                    "Без бота дайджесты, итоги встреч и кнопки «Да / Нет» видны только здесь.", link)
    if not tb.get("owner_chat_id"):
        return _row("system", "bot", "Уведомления", "warn", "бот ждёт привязки",
                    "Откройте окно Telegram-бота и отправьте боту команду /start с кодом.", link)
    stuck_since = store.to_utc_iso(datetime.now(timezone.utc) - timedelta(minutes=30))
    stuck = store.scalar("SELECT COUNT(*) FROM outbox WHERE sent_at IS NULL AND dead=0 AND created <= ?",
                         (stuck_since,)) or 0
    if stuck:
        return _row("system", "bot", "Уведомления", "warn", f"не доставлено в Telegram: {stuck}",
                    "Telegram недоступен или бот заблокирован — PULSE повторит сам. Проверка — в окне бота.", link)
    return _row("system", "bot", "Уведомления", "ok", "Telegram-бот доставляет", "", link)


def _disk() -> dict:
    free = shutil.disk_usage(config.DATA_DIR).free / 1024 ** 3
    text = f"свободно {free:.0f} ГБ" if free >= 10 else f"свободно {free:.1f} ГБ".replace(".", ",")
    if free < DISK_BAD_GB:
        return _row("system", "disk", "Место на диске", "bad", text,
                    "Расшифровка и база остановятся, когда место кончится. Освободите место на диске.")
    if free < DISK_WARN_GB:
        return _row("system", "disk", "Место на диске", "warn", text,
                    "Модели расшифровки и записи встреч занимают гигабайты — стоит освободить место.")
    return _row("system", "disk", "Место на диске", "ok", text)


def _errors() -> dict:
    day = store.to_utc_iso(datetime.now(timezone.utc) - timedelta(days=1))
    n = store.scalar("SELECT COUNT(*) FROM log WHERE level='error' AND ts >= ?", (day,)) or 0
    if n:
        return _row("system", "errors", "Ошибки за сутки", "warn", f"{n}",
                    "Что именно — в «Журнале», фильтр «ошибки».", "/journal?level=error")
    return _row("system", "errors", "Ошибки за сутки", "ok", "нет", "", "/journal")


def _autostart() -> dict | None:
    if sys.platform != "darwin":
        return None
    from opsdash import macos

    if macos.agent_installed():
        return _row("system", "autostart", "Автозапуск", "ok", "PULSE запускается сам при входе в систему")
    return _row("system", "autostart", "Автозапуск", "warn", "выключен",
                "После перезагрузки PULSE не запустится сам. Включите: ./ops mac install (или INSTALL ещё раз).")


def system_rows(settings: dict) -> list[dict]:
    rows = [_assistant(settings), _jobs(), _bot(settings), _transcribe(settings), _disk(), _errors(), _autostart()]
    return [r for r in rows if r]


def run(settings: dict | None = None, include_off: bool = False, catalog: dict | None = None) -> dict:
    """Вся проверка: система, источники и итог — худший уровень и сколько чего."""
    from opsdash import connectors

    settings = settings or config.load_settings()
    catalog = catalog or connectors.catalog(settings)
    system = system_rows(settings)
    sources = source_rows(settings, catalog, include_off)
    rows = system + sources
    worst = min((r["level"] for r in rows if r["level"] != "off"), key=ORDER.get, default="ok")
    counts = {lv: sum(r["level"] == lv for r in rows) for lv in ORDER}
    problems = [r for r in rows if r["level"] in ("bad", "warn")]

    def say(items: list[dict]) -> str:
        n = sum(r["level"] in ("bad", "warn") for r in items)
        return "всё в порядке" if not n else f"{n} {'требует' if n == 1 else 'требуют'} внимания"

    # system_* — для блока «Здоровье» над каталогом: источники там видны на своих карточках
    return {"system": system, "sources": sources, "worst": worst, "counts": counts, "summary": say(rows),
            "system_summary": say(system),
            "system_worst": min((r["level"] for r in system if r["level"] != "off"), key=ORDER.get, default="ok"),
            "problems": sorted(problems, key=lambda r: ORDER[r["level"]])}


# --- сигнал директору: источник замолчал или падает дольше часа ------------------------------------------

def _started_ago() -> timedelta | None:
    return _ago(store.get_state("daemon_started"))


def alerts(settings: dict | None = None) -> list[dict]:
    """Источники, о которых стоит написать: включённый источник падает дольше часа (сбои по одному — не повод)
    или давно не собирается, хотя PULSE работает дольше этого срока (после сна компьютера сбор догонит сам)."""
    settings = settings or config.load_settings()
    started = _started_ago()
    out = []
    for src in config.SOURCES:
        if not settings[src].get("enabled"):
            continue
        job = store.row("SELECT * FROM jobs WHERE name=?", (f"collect:{src}",)) or {}
        if not job.get("last_ok"):
            continue  # ни разу не собирался — это настройка, а не сбой: видно в каталоге
        since_ok = _ago(job["last_ok"])
        title = config.SOURCES[src]["title"]
        failing = job.get("last_error") and job["last_ok"] < (job.get("last_run") or "")
        if failing and since_ok > timedelta(minutes=FAILING_MIN):
            from opsdash import connectors

            out.append({"key": src, "title": title, "level": "bad",
                        "text": f"не собирается {_human(since_ok)}: {connectors.humanize(job['last_error'])}"})
        elif since_ok > silent_after(src, settings) and started and started > silent_after(src, settings):
            out.append({"key": src, "title": title, "level": "warn", "text": f"не собирается {_human(since_ok)}"})
    return out


def watch(settings: dict | None = None) -> list[str]:
    """Раз в пять минут из обслуживания: по каждому тревожному источнику — одно сообщение в день (угол экрана
    и Telegram, если бот привязан). Возвращает ключи источников, о которых написал сейчас."""
    from opsdash import bot, notices

    settings = settings or config.load_settings()
    today = config.now(settings).date().isoformat()
    sent = []
    for a in alerts(settings):
        stamp = f"doctor:alert:{a['key']}:{today}"
        if store.get_state(stamp):
            continue
        store.set_state(stamp, True)
        link = f"/settings?tab={a['key']}"
        notices.add("doctor", f"{a['title']}: {a['text']}", "Откройте окно источника и нажмите «Проверить подключение».",
                    link=link, level="error" if a["level"] == "bad" else "warning", merge_hours=24)
        tb = settings["telegram_bot"]
        if tb.get("enabled") and tb.get("owner_chat_id"):
            try:
                bot.notify(f"⚠️ <b>{a['title']}</b> {a['text']}.\nНастройки → Подключения → {a['title']} → "
                           "«Проверить подключение».")
            except Exception as e:  # noqa: BLE001 — сигнал не должен ронять обслуживание
                store.log("doctor", f"не отправил сигнал о {a['key']}: {e}", "warning")
        sent.append(a["key"])
    return sent
