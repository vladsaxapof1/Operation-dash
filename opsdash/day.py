"""«Реальность дня»: сколько времени на задачи есть между встречами и сколько их нужно.

Ёмкость — свободные окна рабочего дня без встреч, буферов и обеда; спрос — оценки того, что нужно сделать сегодня;
вердикт — одно с другим одной строкой; план — какие задачи в какие окна. Всё считается без ИИ, за миллисекунды.
План живёт только в PULSE (таблица day_plan): в календарь ничего не пишем. Решения директора (source=user) —
«в план на 14:30», «завтра», «снять срок» — закреплены, алгоритм их не двигает.
Спецификация — docs/design/FEATURE-реальность-дня.md.
"""

from __future__ import annotations

import math
import os
from datetime import date, datetime, time, timedelta

from opsdash import config, records, store

CHIPS = [(15, "15м"), (30, "30м"), (60, "1ч"), (120, "2ч"), (240, "полдня")]  # оценка в один клик
DEFAULT_ESTIMATE = 30  # пока задачу не оценили: считаем 30 мин с пометкой «оценка по умолчанию»
BIG = 240  # больше полдня — задачу лучше разбить
SPLIT_HINT = "разбить на части, передать или поручить агенту"
NARROW = 0.34  # фокус-блок или пустое событие под плановой задачей — узкой полосой слева, задача — справа
BACKFILL = "day_estimates_backfill"  # разовая доразметка оценок у старых задач прошла (дата)


# --- время ----------------------------------------------------------------------------------------

def minutes(value, default: int | None = None) -> int | None:
    """'09:30' (или 570 — так YAML читает 09:30 без кавычек) → минуты от полуночи."""
    try:
        h, m = config.hhmm(value).split(":")
        return int(h) * 60 + int(m)
    except (ValueError, AttributeError):
        return default


def hm(m: int) -> str:
    return f"{m // 60:02d}:{m % 60:02d}"


def dur(m: int | float | None) -> str:
    """Минуты по-человечески: «40 мин», «1 ч», «2 ч 10 мин»."""
    m = int(round(m or 0))
    if m < 60:
        return f"{m} мин"
    return f"{m // 60} ч" + (f" {m % 60} мин" if m % 60 else "")


def est_text(m: int | None) -> str:
    """Оценка на чипе и в строке: «30 мин», «1 ч», «полдня»."""
    return "полдня" if m == 240 else dur(m)


def opts(settings: dict | None = None) -> dict:
    """Настройки раздела «День» с умолчаниями (Настройки → День)."""
    settings = settings or config.load_settings()
    d = {**config.DEFAULTS["day"], **(settings.get("day") or {})}

    def num(key: str) -> int:
        try:
            return max(0, int(d.get(key)))
        except (TypeError, ValueError):
            return config.DEFAULTS["day"][key]

    titles = d.get("focus_titles") or []
    if isinstance(titles, str):
        titles = titles.split(",")
    lunch = None
    if "-" in str(d.get("lunch") or ""):  # «13:00-14:00»; пусто — без обеда
        a, _, b = str(d["lunch"]).partition("-")
        lo, hi = minutes(a.strip()), minutes(b.strip())
        lunch = (lo, hi) if lo is not None and hi is not None and hi > lo else None
    return {"focus_titles": [t.strip().casefold() for t in titles if str(t).strip()],
            "after": num("buffer_after_min"), "before": num("buffer_before_min"), "min_window": num("min_window_min"),
            "reply": num("reply_min"), "tight": num("tight_pct"), "over": num("overload_pct"), "lunch": lunch}


def work_window(day: date, settings: dict) -> tuple[int, int] | None:
    """Рабочие часы дня в минутах; None — выходной по настройкам."""
    wh = settings["owner"].get("work_hours") or {}
    if day.isoweekday() not in (wh.get("days") or [1, 2, 3, 4, 5]):
        return None
    lo, hi = minutes(wh.get("start"), 9 * 60), minutes(wh.get("end"), 20 * 60)
    return (lo, hi) if hi > lo else None


WEEKDAYS_TO = ["В понедельник", "Во вторник", "В среду", "В четверг", "В пятницу", "В субботу", "В воскресенье"]
WEEKDAYS_ON = ["понедельник", "вторник", "среду", "четверг", "пятницу", "субботу", "воскресенье"]


def next_label(day: date, settings: dict) -> str:
    """Надпись на кнопке «на следующий рабочий день»: «Завтра» или «В понедельник»."""
    d = next_workday(day, settings)
    return "Завтра" if d == day + timedelta(days=1) else WEEKDAYS_TO[d.weekday()]


def next_workday(day: date, settings: dict, n: int = 1) -> date:
    """n-й рабочий день после day (по рабочим дням из настроек)."""
    days = (settings["owner"].get("work_hours") or {}).get("days") or [1, 2, 3, 4, 5]
    d = day
    for _ in range(n):
        d += timedelta(days=1)
        for _ in range(7):
            if d.isoweekday() in days:
                break
            d += timedelta(days=1)
    return d


# --- ёмкость --------------------------------------------------------------------------------------

def my_emails(settings: dict, events: list[dict] = ()) -> set[str]:
    """Адреса директора: логин календаря, почта, Gmail и календари Google, названные адресом (основной так и
    называется). По ним видно, что встреча «с участниками» и что директор от неё отказалась."""
    out = {settings["calendar"].get("login"), settings["mail"].get("email"), settings["gmail"].get("email")}
    out |= {(e.get("calendar") or "").removeprefix("Google: ") for e in events}
    return {a.strip().lower() for a in out if a and "@" in a}


def classify(e: dict, mine: set[str], focus_titles: list[str]) -> str:
    """busy — встреча (занято); focus — фокус-блок: сам себе поставил время на работу, задачи — туда в первую
    очередь; free — отказались или «свободен» (TRANSP:TRANSPARENT), не занято; skip — на весь день."""
    s, f = store.parse_ts(e["start"]), store.parse_ts(e["end"])
    if e.get("all_day") or not s or not f or f - s >= timedelta(hours=24):
        return "skip"
    if (e.get("transp") or "").upper() == "TRANSPARENT":
        return "free"
    attendees = e["attendees"] if isinstance(e.get("attendees"), list) else store.loads(e.get("attendees"), [])
    if any((a.get("email") or "").lower() in mine and (a.get("status") or "").upper() == "DECLINED" for a in attendees):
        return "free"
    title = (e.get("title") or "").casefold()
    if any(t in title for t in focus_titles) and not others(attendees, mine):
        return "focus"
    return "busy"


def others(attendees: list[dict], mine: set[str]) -> list[dict]:
    """Участники, кроме самого директора."""
    return [a for a in attendees or [] if (a.get("email") or "").lower() not in mine]


def _clip(e: dict, day_start: datetime, tz) -> tuple[int, int]:
    """Событие в минутах от начала дня, обрезанное границами суток (событие через полночь — своей частью)."""
    s = store.parse_ts(e["start"]).astimezone(tz)
    f = store.parse_ts(e["end"]).astimezone(tz)
    end = day_start + timedelta(days=1)
    s, f = max(s, day_start), min(f, end)
    # время на часах: разница дат с одним поясом — без учёта перевода часов (как и сетка дня)
    return (int((s.replace(tzinfo=None) - day_start.replace(tzinfo=None)).total_seconds() // 60),
            int(math.ceil((f.replace(tzinfo=None) - day_start.replace(tzinfo=None)).total_seconds() / 60)))


def union(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Склейка: пересекающиеся и стыкующиеся отрезки — в один."""
    out: list[list[int]] = []
    for s, e in sorted(x for x in spans if x[1] > x[0]):
        if out and s <= out[-1][1]:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return [(s, e) for s, e in out]


def subtract(spans: list[tuple[int, int]], cut: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Отрезки spans без отрезков cut."""
    out = []
    cut = union(cut)
    for s, e in union(spans):
        for cs, ce in cut:
            if ce <= s or cs >= e:
                continue
            if cs > s:
                out.append((s, cs))
            s = max(s, ce)
            if s >= e:
                break
        if s < e:
            out.append((s, e))
    return out


def layout(day: date, events: list[dict], settings: dict, *, now: datetime | None = None,
           briefs: set[str] | frozenset = frozenset()) -> dict:
    """Окна дня без базы (чистая функция): встречи склеиваются; после встречи с участниками — буфер «на хвосты»,
    перед встречей с готовым брифом — буфер на его чтение (буферы не заходят на соседние события и рабочее время
    не покидают); фокус-блоки — не занятость, а лучшие окна; окно короче минимального показывается, но в ёмкость
    не входит; сегодня — только от текущего момента. briefs — uid встреч с готовым брифом."""
    o = opts(settings)
    tz = config.tz(settings)
    day_start = datetime.combine(day, time.min, tz)
    mine = my_emails(settings, events)
    meetings, focus, free = [], [], []
    for e in events:
        kind = classify(e, mine, o["focus_titles"])
        if kind == "skip":
            continue
        s, f = _clip(e, day_start, tz)
        if f <= s:
            continue
        attendees = e["attendees"] if isinstance(e.get("attendees"), list) else store.loads(e.get("attendees"), [])
        item = {"s": s, "e": f, "uid": e["uid"], "title": e.get("title") or "", "kind": kind,
                "with_people": bool(others(attendees, mine)), "brief": e["uid"] in briefs}
        {"busy": meetings, "focus": focus, "free": free}[kind].append(item)
    busy = [(m["s"], m["e"]) for m in meetings]
    if o["lunch"]:
        busy.append(o["lunch"])
    busy = union(busy)
    raw = [(m["e"], m["e"] + o["after"]) for m in meetings if m["with_people"] and o["after"]]
    raw += [(m["s"] - o["before"], m["s"]) for m in meetings if m["brief"] and o["before"]]
    ww = work_window(day, settings)
    out = {"date": day, "work": ww, "meetings": meetings, "focus": focus, "free_events": free, "busy": busy,
           "buffers": [], "windows": [], "from": None, "off": None, "lunch": o["lunch"]}
    if ww is None:
        out["off"] = "weekend"
        return out
    lo, hi = ww
    if now is not None:
        now = now.astimezone(tz)
        if now.date() > day:
            lo = hi
        elif now.date() == day:
            lo = max(lo, now.hour * 60 + now.minute)
    if lo >= hi:
        out["off"] = "over"
        out["from"] = hi
        return out
    out["from"] = lo
    buffers = subtract([(max(s, lo), min(e, hi)) for s, e in raw], busy)
    out["buffers"] = [(s, e) for s, e in buffers if e > s]
    for s, e in subtract([(lo, hi)], busy + out["buffers"]):
        hits = [f for f in focus if f["s"] < e and f["e"] > s]
        out["windows"].append({"s": s, "e": e, "len": e - s, "short": e - s < o["min_window"],
                               "focus": hits[0] if hits else None})
    return out


def capacity(lay: dict) -> int:
    return sum(w["len"] for w in lay["windows"] if not w["short"])


def windows(day: date | None = None, settings: dict | None = None, now: datetime | None = None) -> dict:
    """Окна дня по календарю из базы: встречи всех календарей и готовые брифы."""
    settings = settings or config.load_settings()
    tz = config.tz(settings)
    day = day or config.now(settings).date()
    begin = datetime.combine(day, time.min, tz)
    events = store.rows('SELECT * FROM events WHERE start < ? AND "end" > ? ORDER BY start',
                        (store.to_utc_iso(begin + timedelta(days=1)), store.to_utc_iso(begin)))
    uids = [e["uid"] for e in events]
    ready = {r["uid"] for r in store.rows(
        f"SELECT uid FROM briefs WHERE status='ready' AND uid IN ({','.join('?' * len(uids)) or 'NULL'})", uids)}
    return layout(day, events, settings, now=now if now is not None else config.now(settings), briefs=ready)


# --- спрос ----------------------------------------------------------------------------------------

def _plural(n: int, one: str, few: str, many: str) -> str:
    k = abs(n) % 100
    if 11 <= k <= 19:
        return many
    return one if k % 10 == 1 else few if 2 <= k % 10 <= 4 else many


def _blocked() -> dict[int, dict]:
    """Задачи, которые сами чего-то ждут — мяч не у директора: связь «ждёт» (не готово), открытая подзадача (часть
    работы передана: «моя часть готова — дальше Пётр») или последняя запись хода — «спросили, ждём ответа».
    id → {why: что ждёт, словами («ждёт «…»», «передано: Пётр», «ответа нет 2 дня»), who: у кого мяч (или None),
    what: подробность для подсказки, theirs: мяч у других (не ждёт другую задачу или часть самого директора)}."""
    blocked: dict[int, dict] = {}
    for r in store.rows(
            "SELECT l.task_id, l.event, w.direction, w.who, w.title, w.what FROM task_links l "
            "LEFT JOIN commitments w ON w.id=l.waits_for "
            "WHERE (l.waits_for IS NOT NULL AND w.status='open') OR (l.waits_for IS NULL AND l.done_at IS NULL) ORDER BY l.id"):
        task = bool(r["what"] or r["title"])
        blocked.setdefault(r["task_id"], {
            "why": f"ждёт «{records.headline(r)}»" if task else f"ждёт: {r['event']}",
            "who": ("вы" if r["direction"] == "mine" else r["who"]) if task else None,
            "what": records.headline(r) if task else r["event"], "theirs": not task or r["direction"] != "mine"})
    for r in store.rows("SELECT parent_id, direction, who, title, what FROM commitments WHERE parent_id IS NOT NULL "
                        "AND status='open' AND archived_at IS NULL ORDER BY id"):
        who = "ваша часть" if r["direction"] == "mine" else r["who"] or "?"
        blocked.setdefault(r["parent_id"], {"why": "передано: " + who, "who": None if r["direction"] == "mine" else who,
                                            "what": records.headline(r), "theirs": r["direction"] != "mine"})
    # последняя запись хода (не событие PULSE) — вопрос без ответа: ждём ответа, задачу пока не сделать
    today = config.now().date()
    for r in store.rows(
            "SELECT task_id, at, text FROM (SELECT task_id, kind, at, text, ROW_NUMBER() OVER "
            "(PARTITION BY task_id ORDER BY at DESC, id DESC) AS n FROM task_log WHERE kind != 'event') "
            "WHERE n=1 AND kind='ask'"):
        dt = store.parse_ts(r["at"])
        days = (today - dt.astimezone(config.tz()).date()).days if dt else 0
        why = "ждём ответа" if days <= 0 else f"ответа нет {days} {_plural(days, 'день', 'дня', 'дней')}"
        blocked.setdefault(r["task_id"], {"why": why, "who": None, "what": r["text"], "theirs": True})
    return blocked


def demand(day: date | None = None, settings: dict | None = None, *, rows: list[dict] | None = None) -> dict:
    """Что нужно сделать в этот день, по приоритету. Только задачи директора: срок сегодня или просрочено, всё
    «в работе», срок в ближайшие 2 рабочих дня, если их ждут другие, и то, что директор сама поставила в план;
    плюс «нужен ответ» × минуты на ответ. Задачи, которые сами чего-то ждут (связь «ждёт» или открытая подзадача —
    часть работы передана), в план не идут (waiting). rows — решения директора из day_plan на этот день."""
    settings = settings or config.load_settings()
    o = opts(settings)
    day = day or config.now(settings).date()
    iso = day.isoformat()
    soon = next_workday(day, settings, 2).isoformat()
    rows = decisions(day) if rows is None else rows
    forced = {r["commitment_id"]: r for r in rows if r["source"] == "user" and r["status"] == "planned"}
    skipped = {r["commitment_id"] for r in rows if r["status"] in ("moved", "dropped")}
    # директор поставила задачу на день позже — до него задача в план не идёт (перенесла с сетки Календаря)
    skipped |= {r["commitment_id"] for r in store.rows(
        "SELECT commitment_id FROM day_plan WHERE date > ? AND source='user' AND status='planned'", (iso,))
        if r["commitment_id"] not in forced}
    tasks = store.rows("SELECT * FROM commitments WHERE direction='mine' AND status='open' AND archived_at IS NULL")
    holders: dict[int, list[dict]] = {}
    for r in store.rows(
            "SELECT l.waits_for AS for_id, t.direction, t.who, t.whom, t.title, t.what FROM task_links l "
            "JOIN commitments t ON t.id=l.task_id WHERE l.waits_for IS NOT NULL AND t.status='open' "
            "AND t.archived_at IS NULL"):
        holders.setdefault(r["for_id"], []).append(r)
    blocked = _blocked()
    pids = {c["project_id"] for c in tasks if c.get("project_id")}
    projects = {p["id"]: p for p in store.rows(
        f"SELECT id, title, due, status FROM projects WHERE id IN ({','.join('?' * len(pids)) or 'NULL'})", list(pids))}
    risk_until = (day + timedelta(days=7)).isoformat()
    items, waiting = [], []
    for c in tasks:
        due, held = c["due"], holders.get(c["id"], [])
        want = (c["id"] in forced or (due and due <= iso) or c["in_work_at"]
                or (held and due and iso < due <= soon))
        if not want or c["id"] in skipped:
            continue
        if c["id"] in blocked and c["id"] not in forced:
            waiting.append({**c, "why": blocked[c["id"]]["why"]})
            continue
        p = projects.get(c.get("project_id"))
        at_risk = bool(p and p["status"] != "done" and p["due"] and p["due"] <= risk_until)
        reasons = []
        if c["id"] in forced:
            reasons.append("вы поставили в план")
        if due and due < iso:
            n = (day - date.fromisoformat(due)).days
            reasons.append(f"просрочено {n} {_plural(n, 'день', 'дня', 'дней')}")
        elif due == iso:
            reasons.append("срок сегодня")
        if held:
            names = list(dict.fromkeys("ваша задача" if h["direction"] == "mine" else (h["who"] or "?") for h in held))
            reasons.append(("ждёт " if len(held) == 1 else "ждут ") + ", ".join(names[:3])
                           + (f" и ещё {len(names) - 3}" if len(names) > 3 else ""))
        if c["in_work_at"]:
            reasons.append("в работе")
        if c.get("ball_back_at"):
            reasons.append("мяч снова у вас")
        if at_risk:
            reasons.append(f"проект «{p['title']}» до {date.fromisoformat(p['due']):%d.%m}")
        if due and due > iso:
            reasons.append(f"срок {date.fromisoformat(due):%d.%m}")
        tier = (-1 if c["id"] in forced else 0 if due and due < iso else 1 if due == iso else 2 if held
                else 3 if c["in_work_at"] else 4 if at_risk else 5)
        est = c.get("estimate_min")
        items.append({
            "kind": "task", "id": c["id"], "title": records.headline(c), "whom": c.get("whom"), "due": due,
            "est": est or DEFAULT_ESTIMATE, "est_by": c.get("estimate_by") if est else None, "est_default": not est,
            "reasons": reasons, "reason": " · ".join(reasons), "tier": tier, "holders": len(held),
            "pin": forced.get(c["id"]), "in_work": bool(c["in_work_at"]),
            "key": (tier, -len(held) if tier == 2 else 0, due or "9999", c["id"]),
        })
    items.sort(key=lambda i: i["key"])
    replies = store.scalar("SELECT COUNT(*) FROM attention WHERE status='open'") or 0
    if replies and o["reply"]:
        items.append({"kind": "reply", "id": None, "title": f"Ответить: {replies} "
                      f"{_plural(replies, 'сообщение', 'сообщения', 'сообщений')}", "est": replies * o["reply"],
                      "est_default": False, "est_by": None, "reasons": [f"{replies} × {o['reply']} мин"],
                      "reason": f"{replies} × {o['reply']} мин", "tier": 6, "holders": 0, "pin": None, "in_work": False,
                      "count": replies})
    return {"items": items, "waiting": [{"id": c["id"], "title": records.headline(c), "why": c["why"]} for c in waiting]}


# --- вердикт и план -------------------------------------------------------------------------------

def verdict(cap: int, need: int, settings: dict | None = None, *, off: str | None = None) -> dict:
    """≤ 85% ёмкости — «Реально», до 110% — «Плотно», больше — «Перегруз N ч M мин» (пороги — Настройки → День)."""
    o = opts(settings)
    if off == "weekend":
        note = f"задач на {dur(need)}" if need else "задач нет"
        return {"level": "off", "title": "Выходной", "note": note, "line": f"Выходной по рабочим дням из настроек · {note}"}
    if off == "over":
        note = f"не сделано задач на {dur(need)}" if need else "всё сделано"
        return {"level": "off", "title": "День закончился", "note": note, "line": f"Рабочий день закончился · {note}"}
    if not need:
        return {"level": "ok", "title": "Реально", "line": f"Свободно {dur(cap)}, задач на сегодня нет"}
    pct = need * 100 / cap if cap else math.inf
    if pct <= o["tight"]:
        level, title = "ok", "Реально"
    elif pct <= o["over"]:
        level, title = "tight", "Плотно"
    else:
        level, title = "over", f"Перегруз {dur(need - cap)}"
    free = f"Свободно {dur(cap)}" if cap else "Свободных окон нет"
    return {"level": level, "title": title, "pct": None if pct == math.inf else round(pct),
            "line": f"{free}, задач на {dur(need)} — {title[0].lower() + title[1:]}"}


def place(wins: list[dict], items: list[dict], pins: list[dict] = ()) -> dict:
    """Жадная расстановка без ИИ: закреплённое директором — на своё время; остальное по приоритету — в первое окно,
    где хватает места; фокус-блоки — первыми, дальше окна по времени. Мелочи (до SHORT минут) — пачкой подряд
    (_groups). Не поместилось — «не влезает сегодня»; больше самого длинного окна — отдельно («разбить или передать»)."""
    usable = [w for w in wins if not w["short"]]
    free = {id(w): [(w["s"], w["e"])] for w in usable}
    blocks = []
    for p in sorted(pins, key=lambda p: p["s"]):
        blocks.append({**p, "source": "user"})
        for w in usable:
            free[id(w)] = subtract(free[id(w)], [(p["s"], p["e"])])
    longest = max((w["len"] for w in usable), default=0)
    order = sorted(usable, key=lambda w: (w["focus"] is None, w["s"]))
    overflow, too_big = [], []
    pinned = {p["item"]["id"] for p in pins if p["item"].get("id")}
    placed, left = _greedy(order, free, [it for it in items if it.get("id") not in pinned])
    blocks += placed
    for it in left:
        (too_big if it["est"] > longest else overflow).append(it)
    blocks.sort(key=lambda b: b["s"])
    for b in blocks:
        b["item"]["slot"] = f"{hm(b['s'])}–{hm(b['e'])}"
    return {"blocks": blocks, "overflow": overflow, "too_big": too_big, "longest": longest}


SHORT = 15  # задача до 15 минут — «мелочь»: такие ставятся подряд пачкой, а на сетке рисуются одним блоком
BATCH = 60  # пачка мелочей — не длиннее часа


def _groups(items: list[dict]) -> list[list[dict]]:
    """Порядок расстановки группами: мелочи собираются в пачки до BATCH минут, пачка встаёт на место первой из них
    (мелочи удобнее сделать разом, чем рассыпать по дню); остальное — по одной."""
    out: list[list[dict]] = []
    batch: list[dict] | None = None
    for it in items:
        if it.get("id") and it["est"] <= SHORT:
            if batch is not None and sum(x["est"] for x in batch) + it["est"] <= BATCH:
                batch.append(it)
                continue
            batch = [it]
            out.append(batch)
        else:
            out.append([it])
    return out


def _greedy(order: list[dict], free: dict, items: list[dict]) -> tuple[list[dict], list[dict]]:
    """Каждую группу (_groups) — в первое окно из order, где хватает места на неё целиком; пачке мелочей места нет —
    по одной. free — свободные отрезки окон (меняется). Вернуть (блоки, что не поместилось)."""
    blocks, left = [], []
    queue = _groups(items)
    i = 0
    while i < len(queue):
        group = queue[i]
        i += 1
        est = sum(x["est"] for x in group)
        spot = next(((w, s) for w in order if (s := _fit(w, free[id(w)], est)) is not None), None)
        if not spot:
            if len(group) > 1:
                queue[i:i] = [[x] for x in group]
            else:
                left.append(group[0])
            continue
        w, s = spot
        free[id(w)] = subtract(free[id(w)], [(s, s + est)])
        for x in group:
            blocks.append({"item": x, "s": s, "e": s + x["est"], "source": "auto", "focus": w["focus"]})
            s += x["est"]
    return blocks, left


def _fit(w: dict, free: list[tuple[int, int]], est: int) -> int | None:
    """Где в окне начать задачу длиной est: в окне с фокус-блоком — сначала в самом фокус-блоке, потом в остальном."""
    f = w["focus"]
    hits = lambda s, e: bool(f and s < f["e"] and e > f["s"])  # noqa: E731
    for s, e in sorted(free, key=lambda x: (not hits(*x), x[0])):
        for start in ([max(s, f["s"]), s] if hits(s, e) else [s]):
            if e - start >= est:
                return start
    return None


def decisions(day: date) -> list[dict]:
    return store.rows("SELECT * FROM day_plan WHERE date=? ORDER BY id", (day.isoformat(),))


def plan(day: date | None = None, settings: dict | None = None, now: datetime | None = None, *,
         save: bool = True) -> dict:
    """День целиком: окна, спрос, вердикт, план, «сейчас». Пересчитывается при каждом открытии Сводки и вызове
    ./ops day; расстановка PULSE (source=auto) на сегодня и вперёд сохраняется в day_plan, решения директора
    не трогаются. Закрытые задачи из плана помечаются «сделано»; уже начавшиеся блоки остаются историей дня —
    из неё «План дня: сделано N из M»."""
    settings = settings or config.load_settings()
    now = now if now is not None else config.now(settings)
    day = day or now.date()
    lay = windows(day, settings, now)
    if save:
        _mark_done(day, settings)
    rows = decisions(day)
    dem = demand(day, settings, rows=rows)
    items = dem["items"]
    by_id = {i["id"]: i for i in items if i["id"]}
    pins = []
    for r in rows:
        it = by_id.get(r["commitment_id"])
        s = minutes(r["start"]) if r["source"] == "user" and r["status"] == "planned" else None
        if it and s is not None:
            pins.append({"item": it, "s": s, "e": s + it["est"], "focus": None})
    cap, need = capacity(lay), sum(i["est"] for i in items)
    placed = place(lay["windows"], items, pins) if not lay["off"] else {"blocks": [], "overflow": [], "too_big": [],
                                                                         "longest": 0}
    if save and day >= now.date():
        _save(day, placed["blocks"], hm(lay["from"]) if day == now.date() and lay["from"] is not None else "00:00")
    today = day == now.date()
    out = {**lay, **placed, "items": items, "waiting": dem["waiting"], "capacity": cap, "need": need,
           "verdict": verdict(cap, need, settings, off=lay["off"]), "now": now, "today": today}
    out["current"], out["next"] = _now_window(out, now)
    out["summary"] = _summary(out)
    out["digest"] = digest_line(out, settings)
    saved = decisions(day) if save else rows
    out["rows"] = saved
    out["progress"] = progress(day, rows=saved)
    # сделанное из плана — на сетке галочкой на своём (уже прошедшем) месте
    m = now.hour * 60 + now.minute if today else 24 * 60
    titles = {r["id"]: records.headline(r) for r in store.rows(
        f"SELECT id, title, what FROM commitments WHERE id IN ({','.join('?' * len(saved)) or 'NULL'})",
        [r["commitment_id"] for r in saved])}
    out["history"] = [{**r, "title": titles.get(r["commitment_id"], "")} for r in saved
                      if r["status"] == "done" and r["start"] and r["end"] and minutes(r["start"]) < m]
    # вечером: что из сегодняшнего не закрыто и требует решения — срок сегодня или раньше, или вы ставили в план
    out["carry"] = [i for i in items if i["kind"] == "task" and ((i["due"] and i["due"] <= day.isoformat()) or i["pin"])]
    out["carry_label"] = "на завтра" if next_workday(day, settings) == day + timedelta(days=1) else \
        "на " + WEEKDAYS_ON[next_workday(day, settings).weekday()]
    return out


def _mark_done(day: date, settings: dict) -> None:
    """Закрытые задачи из плана дня — «сделано», если закрыты до конца этого дня."""
    end = store.to_utc_iso(datetime.combine(day + timedelta(days=1), time.min, config.tz(settings)))
    store.execute("UPDATE day_plan SET status='done', updated=? WHERE date=? AND status='planned' AND commitment_id IN "
                  "(SELECT id FROM commitments WHERE status='done' AND COALESCE(closed_at, '') < ?)",
                  (store.utcnow(), day.isoformat(), end))


def progress(day: date, *, rows: list[dict] | None = None) -> dict:
    """«План дня: сделано N из M»: задачи, которые были в плане на этот день (расставил PULSE или поставила
    директор), и сколько из них закрыто. Перенесённые и снятые решениями директора не считаются."""
    rows = decisions(day) if rows is None else rows
    ids = {r["commitment_id"] for r in rows if r["commitment_id"] and r["status"] in ("planned", "done")}
    done = {r["commitment_id"] for r in rows if r["commitment_id"] and r["status"] == "done"}
    return {"done": len(done), "total": len(ids),
            "line": f"План дня: сделано {len(done)} из {len(ids)}" if ids else ""}


def _save(day: date, blocks: list[dict], since: str) -> None:
    """Расстановка PULSE на день — в day_plan: прежняя расстановка с момента since (ЧЧ:ММ) заменяется новой; что уже
    началось, остаётся историей дня, если задачу не переставили заново. Решения директора не трогаются."""
    now = store.utcnow()
    auto = [b for b in blocks if b["source"] == "auto" and b["item"].get("id")]
    ids = [b["item"]["id"] for b in auto]
    with store.db() as conn:
        conn.execute("DELETE FROM day_plan WHERE date=? AND source='auto' AND status='planned' AND (start >= ? "
                     f"OR commitment_id IN ({','.join('?' * len(ids)) or 'NULL'}))", (day.isoformat(), since, *ids))
        conn.executemany(
            'INSERT INTO day_plan(date, commitment_id, start, "end", status, source, updated) '
            "VALUES(?,?,?,?,'planned','auto',?)",
            [(day.isoformat(), b["item"]["id"], hm(b["s"]), hm(b["e"]), now) for b in auto])


def _now_window(p: dict, now: datetime) -> tuple[dict | None, dict | None]:
    """«Сейчас»: окно, в котором мы сейчас (и что в него подходит), или следующее окно."""
    if not p["today"] or p["off"]:
        return None, None
    m = now.hour * 60 + now.minute
    usable = [w for w in p["windows"] if not w["short"]]
    cur = next((w for w in usable if w["s"] <= m < w["e"]), None)
    nxt = next((w for w in usable if w["s"] > m), None)
    pick = cur or nxt
    if pick:
        left = pick["e"] - max(pick["s"], m)
        fit = next((b["item"] for b in p["blocks"] if pick["s"] <= b["s"] < pick["e"]), None)
        # в окне задач по плану нет — подойдёт то, что сегодня не встало (поставленное на другое время не предлагаем)
        placed = {b["item"].get("id") for b in p["blocks"]}
        fit = fit or next((i for i in p["items"] if i["est"] <= left and (not i["id"] or i["id"] not in placed)), None)
        pick = {**pick, "left": left, "fit": fit}
    return (pick, None) if cur else (None, pick)


def _summary(p: dict) -> str:
    """«Свободно 2 ч 10 мин (тайм-аут 11:00 + 2 окна)» — откуда ёмкость."""
    usable = [w for w in p["windows"] if not w["short"]]
    focus = [w for w in usable if w["focus"]]
    parts = [f"{w['focus']['title'].lower()} {hm(max(w['focus']['s'], w['s']))}" for w in focus]
    rest = len(usable) - len(focus)
    if rest:
        parts.append(f"{rest} {_plural(rest, 'окно', 'окна', 'окон')}")
    return " + ".join(parts)


def digest_line(p: dict, settings: dict | None = None) -> str:
    """Строка для утреннего дайджеста (Telegram HTML): вердикт и три главных дела по плану."""
    settings = settings or config.load_settings()
    v = p["verdict"]
    if v["level"] == "off":
        return v["line"]
    head = f"Свободно {dur(p['capacity'])}" + (f" ({p['summary']})" if p["summary"] else "")
    if not p["capacity"]:
        head = "Свободных окон нет"
    word = {"ok": "реально", "tight": "плотно", "over": "перегруз"}[v["level"]]
    line = f"{head}, задач на {dur(p['need'])} — {word}." if p["need"] else f"{head}, задач на сегодня нет."
    top = [i for i in p["items"] if i["kind"] == "task"][:3]
    if top:
        line += " Главное: " + ", ".join(f"«{_esc(i['title'])}»" for i in top)
    port = settings["web"].get("port") or 8765
    return line + f' — <a href="http://localhost:{port}/">разобрать день на Сводке</a>'


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# --- сетка дня на Сводке и в Календаре ----------------------------------------------------------------
# Одна легенда везде: задача «в работе» — светло-зелёная с зелёным пунктиром, остальные запланированные — серые
# с пунктиром; закреплённая директором — сплошной рамкой; сделанная из плана — галочкой на прошедшем месте.

def _box(s: int, e: int, top: int, px: float, bottom: int, **kw) -> dict | None:
    """Блок на часовой сетке: минуты от полуночи → top/height в пикселях, обрезанный видимыми часами."""
    s, e = max(s, top), min(e, bottom)
    if e <= s:
        return None
    return {"top": round((s - top) * px), "height": max(round((e - s) * px) - 2, 4), "len": e - s, **kw}


LINE_PX = 19  # блок задачи не ниже одной строки (шрифт 12px и рамки): короче названия не видно вовсе
BATCH_PX = 34  # пачка мелочей — не ниже двух строк: «Мелочи ×2: …» и продолжение названий


def _batches(blocks: list[dict]) -> list[dict]:
    """Мелочи подряд (каждая до SHORT минут, вплотную одна к другой) — одним блоком «Мелочи ×N» со списком внутри:
    по отдельности 15-минутные задачи на сетке — полоски без названия."""
    out: list[dict] = []
    run: list[dict] = []

    def flush() -> None:
        if len(run) > 1:
            items = [b["item"] for b in run]
            out.append({"item": {"id": None, "members": items, "ids": ",".join(str(i["id"]) for i in items),
                                 "title": f"Мелочи ×{len(items)}", "est": run[-1]["e"] - run[0]["s"],
                                 "in_work": any(i.get("in_work") for i in items)},
                        "s": run[0]["s"], "e": run[-1]["e"], "late": any(b.get("late") for b in run),
                        "source": "user" if all(b.get("source") == "user" for b in run) else "auto"})
        else:
            out.extend(run)
        run.clear()

    for b in sorted(blocks, key=lambda b: b["s"]):
        short = bool(b["item"].get("id")) and b.get("source") != "done" and b["e"] - b["s"] <= SHORT
        if not (short and run and b["s"] == run[-1]["e"]):
            flush()
        (run if short else out).append(b)
    flush()
    return out


def _lift(layer: list[dict], events: list[dict]) -> None:
    """Короткие блоки задач — не ниже строки (пачка мелочей — двух): тянем вниз, а если снизу встреча или другая
    задача — вверх, в свободное место; совсем некуда — сколько поместится. Время блока при этом то же (в подписи)."""
    for b in sorted(layer, key=lambda b: b["top"]):
        need = BATCH_PX if b["item"].get("members") else LINE_PX
        if b["height"] >= need:
            continue
        s, e = b["start"], b["start"] + b["len"]
        obst = [(g["top"], g["top"] + g["height"]) for g in events if not (g["s"] < e and g["e"] > s)]
        obst += [(o["top"], o["top"] + o["height"]) for o in layer if o is not b]
        above = max([bt for t, bt in obst if t < b["top"] and bt <= b["top"] + 2] + [0])
        below = min([t for t, bt in obst if t >= b["top"]] + [10 ** 6])
        top = b["top"]
        if top + need > below - 2:
            top = max(above + 2, below - 2 - need, 0)
        b["top"], b["height"] = top, max(b["height"], min(need, below - 2 - top))
        b["lines"] = max(1, (b["height"] - 3) // 15)


def _task_blocks(blocks: list[dict], col_events: list[dict], top: int, px: float, bottom: int,
                 on: date | None = None) -> list[dict]:
    """Блоки задач одного дня on на сетке. Событие, которое задача перекрывает (фокус-блок, встреча, от которой
    отказались), — узкой полосой слева, задача — справа. Мелочи подряд — одним блоком (_batches), короткие блоки —
    не ниже строки (_lift). key — для меню блока по щелчку."""
    layer, narrow = [], []
    for blk in _batches(blocks):
        it = blk["item"]
        b = _box(blk["s"], blk["e"], top, px, bottom, kind="plan", item=it, time=f"{hm(blk['s'])}–{hm(blk['e'])}",
                 pinned=blk.get("source") == "user", done=blk.get("source") == "done", start=blk["s"],
                 work=bool(it.get("in_work")) and blk.get("source") != "done", late=bool(blk.get("late")),
                 left=0.0, width=1.0, day=on, key=f"{on or ''}-{blk['s']}-{it.get('id') or it.get('ids', '')}")
        if not b:
            continue
        b["lines"] = max(1, (b["height"] - 3) // 15)
        layer.append(b)
        for g in col_events:
            if g["s"] < blk["e"] and g["e"] > blk["s"]:
                narrow.append(g)
                b["left"], b["width"] = NARROW, 1 - NARROW
    for g in narrow:
        g["left"], g["width"] = 0.0, NARROW
    _lift(layer, col_events)
    return layer


def _done_blocks(p: dict) -> list[dict]:
    return [{"s": minutes(r["start"]), "e": minutes(r["end"]), "source": "done",
             "item": {"id": r["commitment_id"], "title": r["title"], "est": minutes(r["end"]) - minutes(r["start"])}}
            for r in p.get("history") or []]


def decorate(grid: dict, p: dict) -> None:
    """Слой плана на сетке дня (overview.day_grid): плановые блоки задач (их можно перетащить — day.js), сделанное
    из плана — галочкой на прошедшем месте, буферы штриховкой, короткие окна серым; фокус-блоки и встречи,
    от которых отказались, — отметкой на самих событиях."""
    top = grid["hour_from"] * 60
    px = grid["hour_px"] / 60
    bottom = top + len(grid["hours"]) * 60
    kinds = {f["uid"]: "focus" for f in p["focus"]} | {f["uid"]: "free" for f in p["free_events"]}
    for e in grid["events"]:
        e["day_kind"] = kinds.get(e["uid"])
    layer = []
    for s, e in p["buffers"]:
        if b := _box(s, e, top, px, bottom, kind="buffer", title="буфер"):
            layer.append(b)
    for w in p["windows"]:
        if w["short"] and (b := _box(w["s"], w["e"], top, px, bottom, kind="short",
                                     title=f"окно {w['len']} мин — короткое")):
            layer.append(b)
    layer += _task_blocks(p["blocks"] + _done_blocks(p), grid["days"][0]["events"], top, px, bottom,
                          grid["days"][0].get("date"))
    grid["plan_layer"] = layer
    grid["plan_from"] = top  # для перетаскивания: минута верхнего края сетки


# --- расписание задач на несколько дней (Календарь) ---------------------------------------------------

def _future_decisions(today: date) -> tuple[dict, dict, dict]:
    """Решения директора на дни после today: pins {день: {задача: минута начала или None — «в план» без времени}},
    not_before {задача: первый день, куда она её поставила — раньше не ставим}, skip {день: задачи «не в этот день»}."""
    pins: dict[date, dict[int, int | None]] = {}
    not_before: dict[int, date] = {}
    skip: dict[date, set[int]] = {}
    for r in store.rows("SELECT * FROM day_plan WHERE date > ? AND source='user' AND status != 'done'",
                        (today.isoformat(),)):
        d, cid = date.fromisoformat(r["date"]), r["commitment_id"]
        if r["status"] == "planned":
            pins.setdefault(d, {})[cid] = minutes(r["start"])
            not_before[cid] = min(not_before.get(cid, d), d)
        else:  # moved, dropped
            skip.setdefault(d, set()).add(cid)
    return pins, not_before, skip


def plan_range(start: date, days: int, settings: dict | None = None, now: datetime | None = None) -> dict:
    """Задачи директора по свободным окнам дней start … start+days-1 — тот же детерминированный алгоритм, без ИИ.
    Сегодня — ровно план Сводки (те же day_plan и решения директора). Дальше по рабочим дням: что не поместилось
    сегодня, затем «в работе» и задачи со сроком — «в работе» первыми, остальные по сроку, каждая не позже своего
    срока; задачи без срока и не «в работе» по будущим дням не раскладываются. Не успела к сроку — плашка «не влезает
    до срока» в день срока, а сама задача встаёт в первое окно после него. Задачи, у которых мяч не у директора
    (ждут коллегу, подзадачу или ответ — _blocked), времени не занимают: плашка «ждёт» в день срока, просроченные и
    «в работе» без срока — сегодня. Прошедшие дни — пустые.
    Вернуть {день: {"blocks", "late", "waiting", "count", "minutes", "work"}}; хранится только план на сегодня."""
    settings = settings or config.load_settings()
    now = now if now is not None else config.now(settings)
    today = now.date()
    end = start + timedelta(days=days - 1)
    out = {start + timedelta(days=i): {"blocks": [], "late": [], "waiting": []} for i in range(days)}
    if end >= today:
        p = plan(today, settings, now)
        if today in out:
            out[today]["blocks"] = p["blocks"] + _done_blocks(p)
        taken = {b["item"]["id"] for b in p["blocks"] if b["item"].get("id")} | {r["commitment_id"] for r in p["history"]}
        late: dict[int, dict] = {}  # задачи, не успевшие к сроку: id → плашка (в день срока)
        queue = []
        blocked = _blocked()
        pins, not_before, skip = _future_decisions(today)
        dropped = {r["commitment_id"] for r in p["rows"] if r["status"] == "dropped"}  # «не сегодня» — решение директора
        for c in store.rows("SELECT * FROM commitments WHERE direction='mine' AND status='open' AND archived_at IS NULL"):
            if c["id"] in taken or not (c["due"] or c["in_work_at"] or c["id"] in not_before):
                continue
            due = records._as_date(c["due"])
            item = {"id": c["id"], "title": records.headline(c), "est": c.get("estimate_min") or DEFAULT_ESTIMATE,
                    "est_default": not c.get("estimate_min"), "in_work": bool(c["in_work_at"]), "due": due}
            if c["id"] in blocked and c["id"] not in not_before:
                # мяч у коллеги: времени не занимает — плашка «ждёт» в день срока (просрочено — сегодня);
                # ждёт другую задачу самого директора — плашка не нужна, та задача в плане
                d = max(due or today, today)
                if d in out and blocked[c["id"]]["theirs"]:
                    out[d]["waiting"].append({"item": item, "day": d, "late": bool(due and due < today),
                                              **blocked[c["id"]]})
                continue
            if due and due <= today and c["id"] not in not_before:  # срок сегодня или прошёл, а в план не попала
                late[c["id"]] = {"item": item, "day": today, "why": f"вы убрали из плана — срок {due:%d.%m}"
                                 if c["id"] in dropped else "не влезает до срока"}
            queue.append(item)
        queue.sort(key=lambda i: (not i["in_work"], i["due"] or date.max, i["id"]))
        if end > today:
            events, ready = _calendar(today + timedelta(days=1), end, settings)
            longest = 0
            d = today + timedelta(days=1)
            while d <= end and queue:
                day_pins = pins.get(d, {})
                work = work_window(d, settings)
                if work or any(it["id"] in day_pins for it in queue):
                    if work:
                        for it in queue:  # срок вчера и раньше, а задача так и не встала — плашка в день срока
                            if it["due"] and it["due"] < d and it["id"] not in late and it["id"] not in not_before:
                                late[it["id"]] = {"item": it, "day": it["due"], "why": "не влезает до срока"}
                    lay = layout(d, events, settings, briefs=ready)
                    usable = [w for w in lay["windows"] if not w["short"]] if work else []
                    longest = max([longest] + [w["len"] for w in usable])
                    free = {id(w): [(w["s"], w["e"])] for w in usable}
                    order = sorted(usable, key=lambda w: (w["focus"] is None, w["s"]))
                    blocks = []
                    for it in queue:  # директор поставила на время (перетащила) — ровно туда, в любой день
                        if day_pins.get(it["id"]) is not None:
                            s = day_pins[it["id"]]
                            blocks.append({"item": it, "s": s, "e": s + it["est"], "source": "user"})
                            for w in usable:
                                free[id(w)] = subtract(free[id(w)], [(s, s + it["est"])])
                    pinned = {b["item"]["id"] for b in blocks}
                    rest = [it for it in queue if it["id"] not in pinned and it["id"] not in skip.get(d, ())
                            and not_before.get(it["id"], d) <= d]
                    rest.sort(key=lambda i: i["id"] not in day_pins)  # «в план на этот день» без времени — первыми
                    placed, _ = _greedy(order, free, rest) if work else ([], [])
                    for b in blocks + placed:
                        b["late"] = bool(b["item"]["due"] and b["item"]["due"] < d)
                        if b["item"]["id"] in day_pins:
                            b["source"] = "user"
                    if d in out:
                        out[d]["blocks"] += blocks + placed
                    done = {b["item"]["id"] for b in blocks + placed}
                    queue = [it for it in queue if it["id"] not in done]
                d += timedelta(days=1)
            for it in queue:  # срок в этом диапазоне, а окон до него так и не нашлось
                if it["due"] and it["due"] <= end and it["id"] not in late and it["id"] not in not_before:
                    late[it["id"]] = {"item": it, "day": it["due"],
                                      "why": "длиннее любого окна" if it["est"] > longest else "не влезает до срока"}
        for x in late.values():
            if x["day"] in out and x["day"] >= today:
                out[x["day"]]["late"].append(x)
    for d, v in out.items():
        if d < today:
            v["blocks"], v["late"], v["waiting"] = [], [], []
        v["blocks"].sort(key=lambda b: b["s"])
        tasks = [b for b in v["blocks"] if b["item"].get("id") and b.get("source") != "done"]
        v["count"], v["minutes"] = len(tasks), sum(b["e"] - b["s"] for b in tasks)
        v["work"] = any(b["item"].get("in_work") for b in tasks)
    return out


WAITS_SHOWN = 2  # плашек «ждёт» в день Календаря по одной; больше — одной общей «Ждут других: N»


def calendar_layer(grid: dict, pr: dict) -> None:
    """Задачи на недельной сетке Календаря (overview.week_grid): блоки в колонках дней, а в строке «весь день» под
    встречами на весь день — плашки «не влезает до срока» (kind=late) и «ждёт: Пётр» (kind=wait — мяч у коллеги)."""
    top = grid["hour_from"] * 60
    px = grid["hour_px"] / 60
    bottom = top + len(grid["hours"]) * 60
    late = []
    today = config.now().date()
    grid["plan_from"] = top  # для перетаскивания и оценки краем: минута верхнего края сетки
    for col, d in enumerate(grid["days"]):
        v = pr.get(d["date"]) or {"blocks": [], "late": [], "waiting": []}
        d["past"] = d["date"] < today  # в прошедший день задачи не переносим
        d["tasks"] = _task_blocks(v["blocks"], d["events"], top, px, bottom, d["date"])
        late += [{**x, "col": col, "kind": "late"} for x in v["late"]]
        waits = v.get("waiting") or []
        if len(waits) > WAITS_SHOWN:  # много — одной плашкой «Ждут других: N», список по нажатию
            late.append({"col": col, "kind": "waits", "items": waits, "late": any(x["late"] for x in waits)})
        else:
            late += [{**x, "col": col, "kind": "wait"} for x in waits]
    rows: dict[int, int] = {}
    for x in late:
        x["row"] = grid["allday_rows"] + rows.get(x["col"], 0)
        rows[x["col"]] = rows.get(x["col"], 0) + 1
    grid["late"], grid["late_rows"] = late, max(rows.values(), default=0)


# --- оценки ---------------------------------------------------------------------------------------

def set_estimate(cid: int, value: int | None, *, by: str = "user") -> bool:
    """Оценка задачи в минутах (None — убрать). Claude не перезаписывает оценку директора — тогда False."""
    if value is not None and not 0 < int(value) <= 24 * 60:
        raise ValueError("Оценка — минуты, от 1 до 1440 (шкала: 15, 30, 60, 120, 240)")
    c = store.row("SELECT estimate_by FROM commitments WHERE id=?", (cid,))
    if not c:
        raise ValueError(f"Нет задачи [{cid}]")
    if by == "claude" and c["estimate_by"] == "user":
        return False
    store.execute("UPDATE commitments SET estimate_min=?, estimate_by=? WHERE id=?",
                  (int(value) if value else None, by if value else None, cid))
    if by == "user":  # «мяч снова у вас — что осталось?»: директор ответила оценкой
        store.execute("UPDATE commitments SET ball_back_at=NULL WHERE id=?", (cid,))
    return True


def agent_or_user() -> str:
    """Кто ставит оценку из ./ops: фоновый запуск Claude — claude, человек в терминале — user."""
    return "claude" if os.environ.get("OPSDASH_AGENT") else "user"


def unestimated(limit: int = 200) -> list[dict]:
    """Открытые задачи директора без оценки — для разовой доразметки."""
    return store.rows("SELECT id, title, what, due, whom, in_work_at FROM commitments WHERE direction='mine' "
                      "AND status='open' AND archived_at IS NULL AND estimate_min IS NULL "
                      "ORDER BY due IS NULL, due, id LIMIT ?", (limit,))


ESTIMATE_RULE = (
    "Оценка (--estimate, минуты) — сколько времени займёт у самого директора, а не у всего проекта: «согласовать "
    "договор» — 30, «собрать сводку решений» — 60, «написать стратегию направления» — 240. Шкала: 15, 30, 60, 120, "
    "240 (полдня); больше полдня — лучше разбить на части. Ставь у её задач (mine); оценку директора не меняешь."
)


def job_estimates(**_) -> str:
    """Разовая доразметка: оценки у открытых задач директора, где их ещё нет (после появления «Реальности дня»).
    Лёгкая модель, одним списком; оценки директора не трогаются."""
    from opsdash import brain

    todo = unestimated()
    if not todo:
        store.set_state(BACKFILL, config.now().date().isoformat())
        return "оценивать нечего"
    prompt = (
        "Оцени время по задачам директора, у которых ещё нет оценки. Список: `./ops day estimates`. "
        f"{ESTIMATE_RULE} Сохрани всё одной командой: `./ops day estimates --set \"<id>=<минуты>,<id>=<минуты>,…\"`. "
        "Больше ничего не меняй и директору не пиши. Это фоновый запуск."
    )
    brain.run(prompt, model="haiku", job="estimates", timeout_min=10)
    left = len(unestimated())
    store.set_state(BACKFILL, config.now().date().isoformat())
    return f"оценено задач: {len(todo) - left} из {len(todo)}"


# --- решения директора («Разобрать день») ---------------------------------------------------------

def decide(cid: int, act: str, *, day: date | None = None, start: str = "", due: str = "", who: str = "",
           what: str = "", settings: dict | None = None) -> str:
    """Решение по задаче из «Разобрать день» и с сетки дня: plan — в план (start «ЧЧ:ММ» или пусто — «авто»; в
    занятое директором окно — после её задач), move — блок перетащили на сетке (ровно на start дня day — и в
    Календаре на другой день: прежнее закрепление снимается), skip — «не в этот день», unplan — отменить
    своё решение, tomorrow — на следующий рабочий день, due — свой срок, nodue — снять срок, delegate — передать
    часть коллеге (подзадача: «Я + Пётр → Тимур», who — кому, what — что). Вернуть текст для уведомления."""
    settings = settings or config.load_settings()
    day = day or config.now(settings).date()
    c = records.get_commitment(cid)
    if not c:
        raise ValueError("Такой задачи нет")
    iso, now = day.isoformat(), store.utcnow()

    def mark(status: str, s: str | None = None, e: str | None = None, on: str = iso) -> None:
        with store.db() as conn:
            conn.execute("DELETE FROM day_plan WHERE date=? AND commitment_id=? AND status!='done'", (on, cid))
            if status:
                conn.execute('INSERT INTO day_plan(date, commitment_id, start, "end", status, source, updated) '
                             "VALUES(?,?,?,?,?,'user',?)", (on, cid, s, e, status, now))

    if act in ("plan", "move"):
        if (start or act == "move") and minutes(start) is None:
            raise ValueError("Время — в виде ЧЧ:ММ")
        est = c.get("estimate_min") or DEFAULT_ESTIMATE
        s = minutes(start) if start else None
        if act == "move":  # перетащили на сетке — ровно на это время этого дня; прежнее место (другой день) снимается
            s = max(0, min(s, 24 * 60 - est))
            store.execute("DELETE FROM day_plan WHERE commitment_id=? AND source='user' AND status='planned' "
                          "AND date >= ? AND date != ?", (cid, config.now(settings).date().isoformat(), iso))
            mark("planned", hm(s), hm(s + est))
            return f"В плане на {hm(s)}" + ("" if day == config.now(settings).date() else f", {day:%d.%m}")
        if s is not None:  # в выбранном окне — после задач, которые директор уже поставила туда сама
            taken = sorted((minutes(r["start"]), minutes(r["end"])) for r in decisions(day)
                           if r["source"] == "user" and r["status"] == "planned" and r["start"] and r["end"]
                           and r["commitment_id"] != cid)
            for a, b in taken:
                if a < s + est and b > s:
                    s = b
        mark("planned", hm(s) if s is not None else None, hm(s + est) if s is not None else None)
        return f"В плане на {hm(s)}" if s is not None else "В плане — PULSE поставит в свободное окно"
    if act == "unplan":  # отменить своё решение: PULSE расставит задачу сам, если она нужна сегодня
        mark("")
        return "Решение отменено — PULSE расставит сам"
    if act == "skip":  # «Не в этот день»: в этот день PULSE задачу не ставит — встанет в следующее окно
        mark("dropped")
        return "Не " + ("сегодня" if day == config.now(settings).date() else f"{day:%d.%m}") + " — PULSE поставит дальше"
    if act == "tomorrow" and c["due"] and c["due"] > next_workday(day, settings).isoformat():
        # срок и так позже — переносим только решение «в план»: завтра задача будет в плане сама
        nxt = next_workday(day, settings).isoformat()
        mark("moved")
        mark("planned", on=nxt)
        return f"В плане на {date.fromisoformat(nxt):%d.%m}"
    if act in ("tomorrow", "due"):
        new = next_workday(day, settings).isoformat() if act == "tomorrow" else due.strip()
        records.set_due(cid, new)
        if new > iso:
            mark("moved")
        return f"Срок — {date.fromisoformat(new):%d.%m}"
    if act == "nodue":
        store.execute("UPDATE commitments SET due=NULL, no_due_at=?, seen_at=COALESCE(seen_at, ?), updated=? "
                      "WHERE id=?", (now, now, now, cid))
        from opsdash import worklog

        worklog.event(cid, "Срок снят")
        mark("dropped")
        return "Срок снят"
    if act == "delegate":  # задача остаётся на директоре и ждёт свою часть — из плана уходит, пока часть не готова
        if not who.strip():
            raise ValueError("Кому передать?")
        if due.strip() and not records._as_date(due.strip()):
            raise ValueError("Срок — дата в виде ГГГГ-ММ-ДД")
        title = what.strip() or records.headline(c)
        records.add_commitment(direction="to_me", who=who.strip(), title=title, what=title, due=due.strip() or None,
                               source_type="manual", parent_id=cid)
        return f"Передано: {who.strip()} — задача ждёт эту часть"
    raise ValueError("Неизвестное действие")


# --- неделя и срок в момент обещания ----------------------------------------------------------------

WEEKDAYS_FROM = ["понедельника", "вторника", "среды", "четверга", "пятницы", "субботы", "воскресенья"]


def _calendar(first: date, last: date, settings: dict) -> tuple[list[dict], set[str]]:
    """События с first по last (включительно) одним запросом и uid встреч с готовым брифом."""
    tz = config.tz(settings)
    begin = datetime.combine(first, time.min, tz)
    events = store.rows('SELECT * FROM events WHERE start < ? AND "end" > ? ORDER BY start',
                        (store.to_utc_iso(datetime.combine(last + timedelta(days=1), time.min, tz)), store.to_utc_iso(begin)))
    uids = [e["uid"] for e in events]
    ready = {r["uid"] for r in store.rows(
        f"SELECT uid FROM briefs WHERE status='ready' AND uid IN ({','.join('?' * len(uids)) or 'NULL'})", uids)}
    return events, ready


def _by_due(today: date, *, exclude: int | None = None) -> dict[date, list[dict]]:
    """Задачи директора по дню, к которому их надо сделать: срок (просроченные — сегодня), «в работе» без срока —
    сегодня. Задачи, которые сами чего-то ждут, не считаются: их всё равно не закончить."""
    blocked = _blocked()
    out: dict[date, list[dict]] = {}
    for c in store.rows("SELECT * FROM commitments WHERE direction='mine' AND status='open' AND archived_at IS NULL"):
        if c["id"] in blocked or c["id"] == exclude:
            continue
        d = records._as_date(c["due"]) if c["due"] else (today if c["in_work_at"] else None)
        if d:
            out.setdefault(max(d, today), []).append(
                {"id": c["id"], "title": records.headline(c), "est": c.get("estimate_min") or DEFAULT_ESTIMATE})
    return out


WEEKDAYS_SHORT = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]


def day_text(value: str | date | None) -> str:
    """«чт, 09.11» — как у быстрых сроков в «Уточнить срок»."""
    d = records._as_date(value) if isinstance(value, str) else value
    return f"{WEEKDAYS_SHORT[d.weekday()]}, {d:%d.%m}" if d else ""


def _hours(m: int) -> str:
    """Для недели — с точностью до получаса: «9 ч», «13 ч 30 мин»."""
    return dur(round(m / 30) * 30)


def week(day: date | None = None, settings: dict | None = None, now: datetime | None = None) -> dict:
    """Неделя (рабочие дни недели, в которую входит day; текущая — от сегодня): окна и задачи по дням, нарастающим
    итогом — задачу со сроком в четверг можно делать с понедельника. «На неделю: окон 9 ч, задач 14 ч — перегруз
    с четверга»: с какого дня задач больше, чем окон до него."""
    settings = settings or config.load_settings()
    o = opts(settings)
    now = now if now is not None else config.now(settings)
    today = now.date()
    day = day or today
    monday = day - timedelta(days=day.weekday())
    days = [d for d in (monday + timedelta(days=i) for i in range(7)) if d >= today and work_window(d, settings)]
    out = {"monday": monday, "days": [], "capacity": 0, "need": 0, "level": "ok", "from": None, "line": ""}
    if not days:
        return out
    events, ready = _calendar(days[0], days[-1], settings)
    first = demand(days[0], settings)["items"]  # первый день — как в плане дня: просроченное, «в работе», ответы
    counted = {i["id"] for i in first if i["id"]}
    later = _by_due(days[0])
    cap_sum = need_sum = 0
    for d in days:
        cap = capacity(layout(d, events, settings, now=now, briefs=ready))
        items = first if d == days[0] else [t for t in later.get(d, []) if t["id"] not in counted]
        counted |= {i["id"] for i in items if i.get("id")}
        need = sum(i["est"] for i in items)
        cap_sum, need_sum = cap_sum + cap, need_sum + need
        pct = need_sum * 100 / cap_sum if cap_sum else (math.inf if need_sum else 0)
        level = "over" if pct > o["over"] else "tight" if pct > o["tight"] else "ok"
        out["days"].append({"date": d, "capacity": cap, "need": need, "items": items, "level": level,
                            "cum_capacity": cap_sum, "cum_need": need_sum})
    out["capacity"], out["need"] = cap_sum, need_sum
    # перегружен только первый день (в нём всё просроченное и «в работе»), а дальше по неделе всё помещается —
    # это забота «Разобрать день» того дня, а не недели
    head, rest = out["days"][0], out["days"][1:]
    only_first = bool(rest) and head["level"] != "ok" and all(x["level"] == "ok" for x in rest)
    for level in ("over", "tight"):
        hit = next((x for x in (rest if only_first else out["days"]) if x["level"] == level), None)
        if hit:
            out["level"], out["from"] = level, hit["date"]
            break
    word = {"over": "перегруз", "tight": "плотно"}.get(out["level"])
    when = ("уже сегодня" if out["from"] == today else f"с {WEEKDAYS_FROM[out['from'].weekday()]}") if word else ""
    first = "сегодня" if head["date"] == today else WEEKDAYS_TO[head["date"].weekday()].lower()
    out["line"] = (f"На неделю: окон {_hours(cap_sum)}, задач {_hours(need_sum)} — "
                   + (f"{word} {when}" if word else "реально")
                   + (f" ({first} — {'перегруз' if head['level'] == 'over' else 'плотно'})" if only_first else ""))
    return out


def capacity_until(until: date, est: int = DEFAULT_ESTIMATE, *, exclude: int | None = None,
                   settings: dict | None = None, now: datetime | None = None) -> dict:
    """Хватит ли окон до конца дня until ещё на задачу длиной est: окна с сегодняшнего момента против задач директора
    с этим сроком и раньше (exclude — саму задачу, если она уже записана). Не хватает — первый день, к которому
    успеть реально (realistic). Ищем не дальше 60 дней."""
    settings = settings or config.load_settings()
    now = now if now is not None else config.now(settings)
    today = now.date()
    until = max(until, today)
    horizon = until + timedelta(days=60)
    events, ready = _calendar(today, horizon, settings)
    need_by = _by_due(today, exclude=exclude)
    replies = (store.scalar("SELECT COUNT(*) FROM attention WHERE status='open'") or 0) * opts(settings)["reply"]
    cap_sum, need_sum, res = 0, replies, {}
    d = today
    while d <= horizon:
        cap_sum += capacity(layout(d, events, settings, now=now, briefs=ready))
        need_sum += sum(t["est"] for t in need_by.get(d, []))
        fits = cap_sum >= need_sum + est
        if d == until:
            res = {"until": until, "capacity": cap_sum, "need": need_sum, "est": est, "ok": fits, "realistic": None}
        if d >= until and fits and work_window(d, settings):
            res["realistic"] = d
            break
        d += timedelta(days=1)
    res["why"] = (f"до {until:%d.%m} окон для задач {dur(res['capacity'])}, а ваших задач с этим сроком и раньше "
                  f"уже {dur(res['need'])}" + (f" — ещё {dur(est)} не помещается" if not res["ok"] else ""))
    return res


def check_due(cid: int, settings: dict | None = None, now: datetime | None = None) -> dict | None:
    """Проверка срока в момент обещания: новая задача директора со сроком — хватит ли окон до него. Не хватает —
    у задачи запоминается реальный срок (due_hint): «Уточнить срок» предложит его директору. None — проверять нечего."""
    c = store.row("SELECT * FROM commitments WHERE id=?", (cid,))
    if not c or c["direction"] != "mine" or c["status"] != "open" or not records._as_date(c["due"]):
        return None
    res = capacity_until(date.fromisoformat(c["due"]), c.get("estimate_min") or DEFAULT_ESTIMATE, exclude=cid,
                         settings=settings, now=now)
    hint = None if res["ok"] or not res["realistic"] else res["realistic"].isoformat()
    store.execute("UPDATE commitments SET due_hint=?, due_hint_why=? WHERE id=?", (hint, res["why"] if hint else None, cid))
    return res


def flow(first: date, last: date, settings: dict | None = None) -> dict:
    """Приток и отток задач директора за дни first..last: сколько добавилось и закрылось, сколько дала одна разобранная
    встреча. Дубли, закрытые объединением, не считаются."""
    settings = settings or config.load_settings()
    tz = config.tz(settings)
    s = store.to_utc_iso(datetime.combine(first, time.min, tz))
    e = store.to_utc_iso(datetime.combine(last + timedelta(days=1), time.min, tz))
    mine = "direction='mine' AND duplicate_of IS NULL"
    added = store.scalar(f"SELECT COUNT(*) FROM commitments WHERE {mine} AND created >= ? AND created < ?", (s, e)) or 0
    closed = store.scalar(f"SELECT COUNT(*) FROM commitments WHERE {mine} AND status='done' AND closed_at >= ? "
                          "AND closed_at < ?", (s, e)) or 0
    from_meetings = store.scalar(f"SELECT COUNT(*) FROM commitments WHERE {mine} AND source_type='meeting' "
                                 "AND created >= ? AND created < ?", (s, e)) or 0
    meetings = store.scalar("SELECT COUNT(*) FROM meetings WHERE status='processed' AND started_at >= ? "
                            "AND started_at < ?", (s, e)) or 0
    per = from_meetings / meetings if meetings else None
    line = f"Ваши задачи: добавилось {added}, закрыто {closed}"
    if added != closed:
        line += f" ({'долг вырос' if added > closed else 'долг уменьшился'} на {abs(added - closed)})"
    if per is not None:
        whole = abs(per - round(per)) < 0.05
        num = str(round(per)) if whole else f"{per:.1f}".replace(".", ",")
        tasks = _plural(round(per), "задача", "задачи", "задач") if whole else "задачи"
        line += (f"; {meetings} {_plural(meetings, 'разобранная встреча', 'разобранные встречи', 'разобранных встреч')} "
                 f"— каждая в среднем даёт вам {num} {tasks}")
    return {"added": added, "closed": closed, "from_meetings": from_meetings, "meetings": meetings, "per_meeting": per,
            "line": line}


# --- живые сигналы ---------------------------------------------------------------------------------

WATCH = "day_watch"  # что видели в прошлый раз: дата, встречи сегодня, вердикт, задачи на сегодня


def watch(settings: dict | None = None, now: datetime | None = None) -> list[str]:
    """Уведомления в углу экрана (обслуживание раз в 5 минут и после сбора календаря): новая задача от Claude со
    сроком сегодня, когда день и так перегружен («места нет — что двигаем?»), и изменение календаря — только если
    поменялся вердикт. Первый проход дня ничего не сообщает — только запоминает. Вернуть заголовки уведомлений."""
    from opsdash import notices

    settings = settings or config.load_settings()
    if not config.any_enabled(settings, "calendar"):
        return []
    now = now if now is not None else config.now(settings)
    p = plan(now.date(), settings, now)
    events = sorted(f"{m['uid']}@{m['s']}-{m['e']}" for m in p["meetings"] + p["focus"])
    level, iso = p["verdict"]["level"], now.date().isoformat()
    today_ids = [i["id"] for i in p["items"] if i["kind"] == "task" and i["due"] == iso]
    old = store.get_state(WATCH) or {}
    store.set_state(WATCH, {"date": iso, "events": events, "level": level, "tasks": today_ids,
                            "at": store.utcnow()})
    if old.get("date") != iso or level == "off":
        return []
    out = []
    if old.get("events") != events and old.get("level") != level:
        title = f"Календарь изменился: {p['verdict']['title'].lower()}"
        notices.add("day", title, p["verdict"]["line"], "/#day", "bad" if level == "over" else "info", merge_hours=1)
        out.append(title)
    fresh = set(today_ids) - set(old.get("tasks") or [])
    if fresh and level == "over":
        new = store.rows(f"SELECT id, title, what FROM commitments WHERE id IN ({','.join('?' * len(fresh))}) "
                         "AND source_type != 'manual' AND created >= ?", [*fresh, old.get("at") or ""])
        for c in new:
            title = f"Задача «{records.headline(c)}» на сегодня: места нет — что двигаем?"
            notices.add("day", title, p["verdict"]["line"], "/#day", "bad")
            out.append(title)
    return out


def carry_over(day: date | None = None, settings: dict | None = None) -> int:
    """Вечером — «Перенести незакрытое»: всё, что было на сегодня и не закрыто (срок сегодня или раньше, или
    директор ставила в план), — на следующий рабочий день. Вернуть, сколько задач перенесено."""
    settings = settings or config.load_settings()
    day = day or config.now(settings).date()
    p = plan(day, settings)
    for it in p["carry"]:
        decide(it["id"], "tomorrow", day=day, settings=settings)
    return len(p["carry"])
