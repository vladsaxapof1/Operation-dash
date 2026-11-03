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
SPLIT_HINT = "разбить на части или передать"
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


def demand(day: date | None = None, settings: dict | None = None, *, rows: list[dict] | None = None) -> dict:
    """Что нужно сделать в этот день, по приоритету. Только задачи директора: срок сегодня или просрочено, всё
    «в работе», срок в ближайшие 2 рабочих дня, если их ждут другие, и то, что директор сама поставила в план;
    плюс «нужен ответ» × минуты на ответ. Задачи, которые сами чего-то ждут, в план не идут (waiting).
    rows — решения директора из day_plan на этот день (если уже прочитаны)."""
    settings = settings or config.load_settings()
    o = opts(settings)
    day = day or config.now(settings).date()
    iso = day.isoformat()
    soon = next_workday(day, settings, 2).isoformat()
    rows = decisions(day) if rows is None else rows
    forced = {r["commitment_id"]: r for r in rows if r["source"] == "user" and r["status"] == "planned"}
    skipped = {r["commitment_id"] for r in rows if r["status"] in ("moved", "dropped")}
    tasks = store.rows("SELECT * FROM commitments WHERE direction='mine' AND status='open' AND archived_at IS NULL")
    holders: dict[int, list[dict]] = {}
    for r in store.rows(
            "SELECT l.waits_for AS for_id, t.direction, t.who, t.whom, t.title, t.what FROM task_links l "
            "JOIN commitments t ON t.id=l.task_id WHERE l.waits_for IS NOT NULL AND t.status='open' "
            "AND t.archived_at IS NULL"):
        holders.setdefault(r["for_id"], []).append(r)
    blocked = {r["task_id"] for r in store.rows(
        "SELECT DISTINCT l.task_id FROM task_links l LEFT JOIN commitments w ON w.id=l.waits_for "
        "WHERE (l.waits_for IS NOT NULL AND w.status='open') OR (l.waits_for IS NULL AND l.done_at IS NULL)")}
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
            waiting.append(c)
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
    return {"items": items, "waiting": [{"id": c["id"], "title": records.headline(c)} for c in waiting]}


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
    где хватает места; фокус-блоки — первыми, дальше окна по времени. Не поместилось — «не влезает сегодня»;
    больше самого длинного окна — отдельно («разбить или передать»)."""
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
    for it in items:
        if it.get("id") in pinned:
            continue
        spot = None
        for w in order:
            if (s := _fit(w, free[id(w)], it["est"])) is not None:
                spot = (w, s)
                break
        if not spot:
            (too_big if it["est"] > longest else overflow).append(it)
            continue
        w, s = spot
        free[id(w)] = subtract(free[id(w)], [(s, s + it["est"])])
        blocks.append({"item": it, "s": s, "e": s + it["est"], "source": "auto", "focus": w["focus"]})
    blocks.sort(key=lambda b: b["s"])
    for b in blocks:
        b["item"]["slot"] = f"{hm(b['s'])}–{hm(b['e'])}"
    return {"blocks": blocks, "overflow": overflow, "too_big": too_big, "longest": longest}


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
    не трогаются. Закрытые задачи из плана помечаются «сделано»."""
    settings = settings or config.load_settings()
    now = now if now is not None else config.now(settings)
    day = day or now.date()
    lay = windows(day, settings, now)
    if save:
        store.execute("UPDATE day_plan SET status='done', updated=? WHERE date=? AND status='planned' "
                      "AND commitment_id IN (SELECT id FROM commitments WHERE status='done')",
                      (store.utcnow(), day.isoformat()))
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
        _save(day, placed["blocks"])
    out = {**lay, **placed, "items": items, "waiting": dem["waiting"], "capacity": cap, "need": need,
           "verdict": verdict(cap, need, settings, off=lay["off"]), "now": now, "today": day == now.date()}
    out["current"], out["next"] = _now_window(out, now)
    out["summary"] = _summary(out)
    out["digest"] = digest_line(out, settings)
    return out


def _save(day: date, blocks: list[dict]) -> None:
    """Расстановка PULSE на день — в day_plan (старая расстановка PULSE заменяется, решения директора остаются)."""
    now = store.utcnow()
    with store.db() as conn:
        conn.execute("DELETE FROM day_plan WHERE date=? AND source='auto' AND status='planned'", (day.isoformat(),))
        conn.executemany(
            'INSERT INTO day_plan(date, commitment_id, start, "end", status, source, updated) '
            "VALUES(?,?,?,?,'planned','auto',?)",
            [(day.isoformat(), b["item"]["id"], hm(b["s"]), hm(b["e"]), now)
             for b in blocks if b["source"] == "auto" and b["item"].get("id")])


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
        fit = fit or next((i for i in p["items"] if i["est"] <= left), None)
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


# --- сетка дня на Сводке ----------------------------------------------------------------------------

def decorate(grid: dict, p: dict) -> None:
    """Слой плана на сетке дня (overview.day_grid): плановые блоки задач, буферы штриховкой, короткие окна серым;
    фокус-блоки и встречи, от которых отказались, — отметкой на самих событиях."""
    top = grid["hour_from"] * 60
    px = grid["hour_px"] / 60
    bottom = top + len(grid["hours"]) * 60
    kinds = {f["uid"]: "focus" for f in p["focus"]} | {f["uid"]: "free" for f in p["free_events"]}
    for e in grid["events"]:
        e["day_kind"] = kinds.get(e["uid"])
    def box(s: int, e: int, **kw) -> dict | None:
        s, e = max(s, top), min(e, bottom)
        if e <= s:
            return None
        return {"top": round((s - top) * px), "height": max(round((e - s) * px) - 2, 4), "len": e - s, **kw}
    layer = []
    for s, e in p["buffers"]:
        if b := box(s, e, kind="buffer", title="буфер"):
            layer.append(b)
    for w in p["windows"]:
        if w["short"] and (b := box(w["s"], w["e"], kind="short", title=f"окно {w['len']} мин — короткое")):
            layer.append(b)
    narrow = []
    for blk in p["blocks"]:
        it = blk["item"]
        b = box(blk["s"], blk["e"], kind="plan", item=it, time=f"{hm(blk['s'])}–{hm(blk['e'])}", pinned=blk["source"] == "user",
                left=0.0, width=1.0)
        if not b:
            continue
        layer.append(b)
        for g in grid["days"][0]["events"]:
            if g["s"] < blk["e"] and g["e"] > blk["s"]:
                narrow.append(g)
                b["left"], b["width"] = NARROW, 1 - NARROW
    for g in narrow:
        g["left"], g["width"] = 0.0, NARROW
    grid["plan_layer"] = layer


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

def decide(cid: int, act: str, *, day: date | None = None, start: str = "", due: str = "",
           settings: dict | None = None) -> str:
    """Решение по задаче из «Разобрать день»: plan — в план (start «ЧЧ:ММ» или пусто — «авто»), unplan — отменить
    своё решение, tomorrow — на следующий рабочий день, due — свой срок, nodue — снять срок. Вернуть текст для
    уведомления."""
    settings = settings or config.load_settings()
    day = day or config.now(settings).date()
    if not records.get_commitment(cid):
        raise ValueError("Такой задачи нет")
    iso, now = day.isoformat(), store.utcnow()

    def mark(status: str, s: str | None = None, e: str | None = None) -> None:
        with store.db() as conn:
            conn.execute("DELETE FROM day_plan WHERE date=? AND commitment_id=? AND status!='done'", (iso, cid))
            if status:
                conn.execute('INSERT INTO day_plan(date, commitment_id, start, "end", status, source, updated) '
                             "VALUES(?,?,?,?,?,'user',?)", (iso, cid, s, e, status, now))

    if act == "plan":
        if start and minutes(start) is None:
            raise ValueError("Время — в виде ЧЧ:ММ")
        est = store.scalar("SELECT estimate_min FROM commitments WHERE id=?", (cid,)) or DEFAULT_ESTIMATE
        s = minutes(start) if start else None
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
    if act in ("tomorrow", "due"):
        new = next_workday(day, settings).isoformat() if act == "tomorrow" else due.strip()
        records.set_due(cid, new)
        if new > iso:
            mark("moved")
        return f"Срок — {date.fromisoformat(new):%d.%m}"
    if act == "nodue":
        store.execute("UPDATE commitments SET due=NULL, no_due_at=?, seen_at=COALESCE(seen_at, ?), updated=? "
                      "WHERE id=?", (now, now, now, cid))
        mark("dropped")
        return "Срок снят"
    raise ValueError("Неизвестное действие")
