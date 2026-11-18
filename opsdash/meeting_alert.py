"""Напоминание о встрече поверх всех окон (macOS).

За N минут до встречи из календаря (Настройки → Подключения → Записи встреч) в правом верхнем углу экрана
появляется карточка PULSE: название, время, «Открыть Zoom». Когда директор открыл звонок или встреча
началась, карточка меняется на «Включите запись»: без записи PULSE не расшифрует и не разберёт встречу.

Окно рисует macOS: скрипт macos_alert.js (JavaScript for Automation, есть в каждой macOS) открывает плавающую
панель без рамки и показывает в ней страницу PULSE /alert/<key> — стиль, шрифт и тема те же, что у PULSE.
Панель не забирает фокус, видна на всех рабочих столах и поверх полноэкранных приложений.

Напоминаем только о встречах: со ссылкой на звонок или с другими участниками. Занятое время («тайм-аут»,
«обед»), встречи на весь день, «свободен» и встречи, от которых директор отказался, пропускаем.
"""

from __future__ import annotations

import hashlib
import html
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

from opsdash import config, day, overview, store

STATE_KEY = "meeting_alert"  # {key: {"status": shown|joined|recording|no-record|closed|overlap, "start", "at"}}
DEMO = "demo"
SCRIPT = Path(__file__).with_name("macos_alert.js")
LATE_MIN = 5          # PULSE запустился или проснулся позже начала: до стольких минут после начала ещё показываем
AFTER_START_MIN = 10  # сколько карточка висит после начала встречи, если на неё не ответили
OVERLAP_MIN = 5       # встречи, начинающиеся в пределах стольких минут, — «в одно время»: карточка одна
MINUTES = (1, 2, 3, 5, 10)
CARD_W, CARD_H = 408, 230  # окно с полями под тень; высоту страница уточняет сама

_URL_TAIL = r"[^\s<>\"'()\[\]]+"
PLATFORMS = [
    (re.compile(r"https?://[\w.-]*zoom\.us/(?:j|my|w|s)/" + _URL_TAIL, re.I), "Zoom"),
    (re.compile(r"https?://telemost(?:\.360)?\.yandex\.\w+/j/" + _URL_TAIL, re.I), "Телемост"),
    (re.compile(r"https?://meet\.google\.com/[a-z0-9-]+", re.I), "Google Meet"),
    (re.compile(r"https?://teams\.(?:microsoft|live)\.com/" + _URL_TAIL, re.I), "Teams"),
    (re.compile(r"https?://[\w.-]*ktalk\.ru/" + _URL_TAIL, re.I), "Толк"),
    (re.compile(r"https?://jazz\.sber\.ru/" + _URL_TAIL, re.I), "Jazz"),
    (re.compile(r"https?://(?:calls\.vk\.com|vk\.com/call)/" + _URL_TAIL, re.I), "VK Звонки"),
    (re.compile(r"https?://[\w.-]*webex\.com/" + _URL_TAIL, re.I), "Webex"),
]

# «Включите запись в …»: где идёт звонок, в предложном падеже
WHERE = {"Zoom": "в Zoom", "Телемост": "в Телемосте", "Google Meet": "в Google Meet", "Teams": "в Teams",
         "Толк": "в Толке", "Jazz": "в Jazz", "VK Звонки": "в VK Звонках", "Webex": "в Webex"}

_panel: dict = {}  # открытая карточка: {"proc": Popen, "key": key}
_last_error = [""]  # та же ошибка раз в 20 с не должна забивать «Журнал»


def is_supported() -> bool:
    return sys.platform == "darwin"


def options(settings: dict) -> dict:
    s = settings.get("meeting_alert") or {}
    minutes = int(s.get("minutes") or 2)
    return {"enabled": bool(s.get("enabled", True)), "minutes": minutes if minutes in MINUTES else 2}


# --- какие встречи ------------------------------------------------------------------------------

def call(e: dict) -> dict | None:
    """Ссылка на звонок из полей встречи: {"platform": "Zoom", "url": …} или None."""
    for field in ("url", "location", "description"):
        text = html.unescape(e.get(field) or "")
        for rx, name in PLATFORMS:
            if m := rx.search(text):
                return {"platform": name, "url": m.group(0).rstrip(".,;")}
    return None


def key(e: dict) -> str:
    """Ключ одного показа: встреча и её время (повторяющаяся встреча — каждый раз заново)."""
    return hashlib.sha1(f"{e['uid']}|{e['start']}".encode("utf-8")).hexdigest()[:12]


def eligible(e: dict, mine: set[str], settings: dict) -> bool:
    """Встреча, о которой стоит напомнить: занятое время со звонком или с другими участниками."""
    if day.classify(e, mine, settings["day"].get("focus_titles") or []) != "busy":
        return False
    others = day.others(e.get("attendees") or [], mine)
    title = (e.get("title") or "").casefold()
    if not others and any(w.casefold() in title for w in settings["briefs"].get("ignore") or []):
        return False
    return bool(call(e) or others)


def _mine_status(e: dict, mine: set[str]) -> str:
    return next(((a.get("status") or "").upper() for a in e.get("attendees") or []
                 if (a.get("email") or "").lower() in mine), "")


def _rank(e: dict, mine: set[str]) -> tuple:
    """Из встреч в одно время — та, что важнее: принятая вами, со звонком, с бо́льшим числом участников."""
    return (_mine_status(e, mine) != "ACCEPTED", call(e) is None, -len(e.get("attendees") or []), e["start"])


def candidates(settings: dict, now: datetime | None = None) -> list[dict]:
    """Встречи, которые начинаются в ближайшие N минут (или начались не больше LATE_MIN назад), — важная первой."""
    now = now or config.now(settings)
    minutes = options(settings)["minutes"]
    events = overview.events_between(now - timedelta(minutes=LATE_MIN), now + timedelta(minutes=minutes))
    mine = day.my_emails(settings, events)
    out = [e for e in events
           if now - timedelta(minutes=LATE_MIN) <= store.parse_ts(e["start"]) <= now + timedelta(minutes=minutes)
           and eligible(e, mine, settings)]
    return sorted(out, key=lambda e: _rank(e, mine))


def state() -> dict:
    return store.get_state(STATE_KEY) or {}


def mark(k: str, status: str, start: str | None = None) -> None:
    """Запомнить, что с карточкой сделали; записи старше двух дней убираем."""
    st = state()
    now = store.utcnow()
    cutoff = store.to_utc_iso(datetime.now().astimezone() - timedelta(days=2))
    st = {x: v for x, v in st.items() if (v.get("start") or v.get("at") or "") >= cutoff}
    prev = st.get(k) or {}
    st[k] = {**prev, "status": status, "at": now, **({"start": start} if start else {})}
    store.set_state(STATE_KEY, st)


def tick(settings: dict, now: datetime | None = None) -> str | None:
    """Проход планировщика (раз в 20 с): пора показать карточку — показать. Возвращает ключ показанной."""
    opts = options(settings)
    if not opts["enabled"] or not is_supported():
        return None
    found = candidates(settings, now)
    seen = state()
    fresh = [e for e in found if key(e) not in seen]
    if not fresh:
        return None
    top = fresh[0]
    start = store.parse_ts(top["start"])
    for e in found:  # встречи в то же время — в той же карточке строкой «Ещё в это время», отдельной не будет
        if e is not top and key(e) not in seen and abs(store.parse_ts(e["start"]) - start) <= timedelta(minutes=OVERLAP_MIN):
            mark(key(e), "overlap", e["start"])
    mark(key(top), "shown", top["start"])
    show(key(top), settings)
    return key(top)


def run(settings: dict) -> None:
    """tick для планировщика: ошибка не роняет остальные задания и пишется в «Журнал» один раз подряд."""
    try:
        tick(settings)
        _last_error[0] = ""
    except Exception as e:  # noqa: BLE001
        if str(e) != _last_error[0]:
            _last_error[0] = str(e)
            store.log("meeting-alert", f"карточка встречи: {e}", "error")


# --- карточка ---------------------------------------------------------------------------------

def find(k: str, settings: dict | None = None) -> dict | None:
    """Встреча по ключу карточки — из встреч за сутки вокруг сейчас."""
    settings = settings or config.load_settings()
    now = config.now(settings)
    return next((e for e in overview.events_between(now - timedelta(days=1), now + timedelta(days=1)) if key(e) == k),
                None)


def demo_event(settings: dict) -> dict:
    """Пример для кнопки «Показать пример»: звонок в Zoom через N минут, ссылки нет — ничего не откроется."""
    start = config.now(settings).replace(microsecond=0) + timedelta(minutes=options(settings)["minutes"])
    return {"uid": DEMO, "title": "Пример: встреча с командой", "start": store.to_utc_iso(start),
            "end": store.to_utc_iso(start + timedelta(minutes=30)), "attendees": [{}, {}, {}], "url": "",
            "location": "", "description": ""}


def card(k: str, settings: dict | None = None, now: datetime | None = None) -> dict | None:
    """Всё для страницы карточки: встреча, звонок, бриф, встречи в то же время, шаг (скоро / запись)."""
    settings = settings or config.load_settings()
    now = now or config.now(settings)
    e = demo_event(settings) if k == DEMO else find(k, settings)
    if not e:
        return None
    tz = config.tz(settings)
    start, end = store.parse_ts(e["start"]).astimezone(tz), store.parse_ts(e["end"]).astimezone(tz)
    mine = day.my_emails(settings, [e])
    status = (state().get(k) or {}).get("status", "")
    c = call(e) or ({"platform": "Zoom", "url": ""} if k == DEMO else None)
    brief = None
    if k != DEMO:
        from opsdash import briefs

        b = briefs.for_events([dict(e)])[0]
        if b["brief"] and b["brief"]["status"] == "ready":
            brief = {"href": f"/calendar?brief={b['brief_key']}", "topic": b["brief"].get("topic") or ""}
    same_time = []
    if k != DEMO:
        for o in candidates(settings, now):
            if key(o) != k and abs(store.parse_ts(o["start"]) - store.parse_ts(e["start"])) <= timedelta(minutes=OVERLAP_MIN):
                same_time.append({"key": key(o), "title": o.get("title") or "Встреча"})
    people = len(day.others(e.get("attendees") or [], mine)) if k != DEMO else 3
    return {
        "key": k, "demo": k == DEMO, "title": (e.get("title") or "").strip() or "Встреча",
        "time": f"{start:%H:%M}–{end:%H:%M}", "start_ms": int(start.timestamp() * 1000),
        "until_ms": int((start + timedelta(minutes=AFTER_START_MIN)).timestamp() * 1000),
        "platform": c["platform"] if c else "", "where": WHERE.get(c["platform"], "") if c else "",
        "has_link": bool(c and c["url"]), "people": people, "brief": brief, "same_time": same_time[:2],
        # встреча без звонка (в переговорке): открывать нечего — сразу «Включите запись», с обратным отсчётом
        "step": "record" if status == "joined" or now >= start or not c else "soon",
    }


def act(k: str, action: str, settings: dict | None = None) -> dict:
    """Кнопка на карточке: join — открыть звонок, recording — запись идёт, no-record — без записи, closed — ✕."""
    if action not in ("join", "recording", "no-record", "closed"):
        raise ValueError("Неизвестное действие")
    settings = settings or config.load_settings()
    if k == DEMO:
        return {"step": "record" if action == "join" else "done"}
    e = find(k, settings)
    if not e:
        raise LookupError("Встреча не найдена")
    if action == "join" and (c := call(e)):
        open_call(c["url"])
    mark(k, "joined" if action == "join" else action, e["start"])
    return {"step": "record" if action == "join" else "done"}


def zoom_app_url(url: str) -> str | None:
    """https://…zoom.us/j/123?pwd=… → zoommtg://zoom.us/join?confno=123&pwd=…: сразу в приложение, без вкладки."""
    m = re.match(r"https?://([\w.-]*zoom\.us)/j/(\d+)(?:\?(?:.*&)?pwd=([\w.-]+))?", url)
    if not m:
        return None
    return f"zoommtg://{m.group(1)}/join?action=join&confno={m.group(2)}" + (f"&pwd={m.group(3)}" if m.group(3) else "")


def open_call(url: str) -> None:
    """Открыть звонок: Zoom — в приложении, если оно стоит, остальное — как ссылку в системе."""
    if sys.platform == "darwin":
        app = zoom_app_url(url)
        if app and subprocess.run(["open", app], capture_output=True).returncode == 0:
            return
        subprocess.run(["open", url], capture_output=True)
    else:
        import webbrowser

        webbrowser.open(url)


def show(k: str, settings: dict | None = None) -> bool:
    """Открыть карточку поверх всех окон. Прежняя карточка закрывается: на экране всегда одна."""
    if not is_supported() or not SCRIPT.exists():
        return False
    from opsdash import daemon

    settings = settings or config.load_settings()
    close()
    url = f"http://127.0.0.1:{daemon.running_port(settings)}/alert/{k}"
    ttl = (options(settings)["minutes"] + AFTER_START_MIN + LATE_MIN) * 60
    try:
        proc = subprocess.Popen(["osascript", "-l", "JavaScript", str(SCRIPT), url, str(ttl), str(CARD_W), str(CARD_H)],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                start_new_session=True)
    except OSError as e:
        store.log("meeting-alert", f"не открылась карточка встречи: {e}", "error")
        return False
    _panel.update(proc=proc, key=k)
    return True


def close() -> None:
    proc = _panel.pop("proc", None)
    _panel.pop("key", None)
    if proc and proc.poll() is None:
        proc.terminate()
