"""Сводка «что сейчас важно» — общая для дашборда, бота и дайджеста."""

from __future__ import annotations

import html
from datetime import date, datetime, time, timedelta, timezone

from opsdash import config, proposals, records, store
from opsdash.sources import kaiten

SOURCE_TITLES = {
    "telegram": "Telegram",
    "ymessenger": "Я.Мессенджер",
    "pachca": "Пачка",
    "mail": "Почта",
    "gmail": "Gmail",
    "calendar": "Календарь",
    "google_calendar": "Google Календарь",
    "kaiten": "Kaiten",
    "recordings": "Записи встреч",
    "bot": "Telegram-бот",
}


def events_between(start: datetime, end: datetime) -> list[dict]:
    items = store.rows(
        'SELECT * FROM events WHERE start < ? AND "end" > ? ORDER BY start',
        (store.to_utc_iso(end), store.to_utc_iso(start)),
    )
    for e in items:
        e["attendees"] = store.loads(e["attendees"], [])
    return items


WEEKDAYS = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]


def calendar_days(first: date, days: int = 7, settings: dict | None = None) -> list[dict]:
    """Встречи по дням начиная с first; встреча на несколько дней попадает в каждый из них."""
    tz = config.tz(settings)
    today = config.now(settings).date()
    begin = datetime.combine(first, time.min, tz)
    events = events_between(begin, begin + timedelta(days=days))
    out = []
    for i in range(days):
        d = first + timedelta(days=i)
        start = datetime.combine(d, time.min, tz)
        end = start + timedelta(days=1)
        out.append({
            "date": d, "weekday": WEEKDAYS[d.weekday()], "today": d == today, "tomorrow": d == today + timedelta(days=1),
            "events": [e for e in events if store.parse_ts(e["start"]) < end and store.parse_ts(e["end"]) > start],
        })
    return out


WEEKDAYS_SHORT = ["ПН", "ВТ", "СР", "ЧТ", "ПТ", "СБ", "ВС"]
MONTHS_SHORT = ["янв", "февр", "март", "апр", "май", "июнь", "июль", "авг", "сент", "окт", "нояб", "дек"]
MONTHS = ["Январь", "Февраль", "Март", "Апрель", "Май", "Июнь", "Июль", "Август", "Сентябрь", "Октябрь",
          "Ноябрь", "Декабрь"]
HOUR_PX = 48  # высота часа в сетке недели


def week_title(monday: date) -> str:
    """«Октябрь 2026» или, если неделя на стыке, «Окт – нояб 2026» — как в Google Календаре."""
    sunday = monday + timedelta(days=6)
    if monday.month == sunday.month:
        return f"{MONTHS[monday.month - 1]} {monday.year}"
    left = MONTHS_SHORT[monday.month - 1].capitalize() + ("" if monday.year == sunday.year else f" {monday.year}")
    return f"{left} – {MONTHS_SHORT[sunday.month - 1]} {sunday.year}"


def _layout_day(segments: list[dict]) -> None:
    """Встречи одного дня, идущие одновременно, — рядом в колонках (left/width в долях ширины дня)."""
    segments.sort(key=lambda g: (g["s"], -g["e"]))
    cluster: list[dict] = []
    cols: list[float] = []  # где кончается последняя встреча в каждой колонке
    cluster_end = -1.0

    def close():
        for g in cluster:
            g["width"] = 1 / len(cols)
            g["left"] = g["col"] / len(cols)

    for g in segments:
        if g["s"] >= cluster_end and cluster:
            close()
            cluster, cols = [], []
        col = next((i for i, end in enumerate(cols) if end <= g["s"]), None)
        if col is None:
            cols.append(g["e"])
            col = len(cols) - 1
        else:
            cols[col] = g["e"]
        g["col"] = col
        cluster.append(g)
        cluster_end = max(cluster_end if len(cluster) > 1 else -1.0, g["e"])
    if cluster:
        close()


def week_grid(monday: date, settings: dict | None = None, *, ndays: int = 7, hour_px: int = HOUR_PX,
              hours: tuple[int, int] = (8, 20)) -> dict:
    """Неделя в виде сетки, как в Google Календаре: столбцы — дни, по вертикали — часы.
    С ndays=1 — один день (расписание на сводке). hours — часы, видные всегда; встречи раньше или позже расширяют сетку."""
    tz = config.tz(settings)
    now = config.now(settings)
    begin = datetime.combine(monday, time.min, tz)
    events = events_between(begin, begin + timedelta(days=ndays))
    days = [{"date": monday + timedelta(days=i), "weekday": WEEKDAYS_SHORT[(monday + timedelta(days=i)).weekday()],
             "today": monday + timedelta(days=i) == now.date(), "events": []} for i in range(ndays)]
    allday, timed = [], []
    for n, e in enumerate(events):
        e["n"] = n
        e["google"] = e["uid"].startswith("google:")
        e["past"] = store.parse_ts(e["end"]) <= now
        s, f = store.parse_ts(e["start"]).astimezone(tz), store.parse_ts(e["end"]).astimezone(tz)
        e["s_local"], e["e_local"] = s, f
        e["now"] = s <= now < f  # идёт сейчас
        if e["all_day"] or f - s >= timedelta(hours=24):
            first = max((s.date() - monday).days, 0)
            last = min(((f - timedelta(seconds=1)).date() - monday).days, ndays - 1)
            allday.append({"ev": e, "col": first, "span": max(last - first + 1, 1)})
        else:
            timed.append(e)
    # полоса «весь день»: многодневные встречи — одной полосой, раскладываем по строкам без наложений
    rows: list[list[tuple[int, int]]] = []
    for a in sorted(allday, key=lambda a: (a["col"], -a["span"])):
        cells = (a["col"], a["col"] + a["span"])
        row = next((i for i, r in enumerate(rows) if all(cells[1] <= x or cells[0] >= y for x, y in r)), None)
        if row is None:
            rows.append([])
            row = len(rows) - 1
        rows[row].append(cells)
        a["row"] = row
    hour_from, hour_to = hours
    for e in timed:
        for i, d in enumerate(days):
            day_start = datetime.combine(d["date"], time.min, tz)
            s = max(e["s_local"], day_start)
            f = min(e["e_local"], day_start + timedelta(days=1))
            if f <= s:
                continue
            sm, fm = (s - day_start).total_seconds() / 60, (f - day_start).total_seconds() / 60
            d["events"].append({"ev": e, "s": sm, "e": fm})
            hour_from = min(hour_from, int(sm // 60))
            hour_to = max(hour_to, min(24, int(-(-fm // 60))))
    for d in days:
        _layout_day(d["events"])
        for g in d["events"]:
            g["top"] = round((g["s"] - hour_from * 60) * hour_px / 60)
            g["height"] = max(round((g["e"] - g["s"]) * hour_px / 60) - 2, 18)
            g["short"] = g["e"] - g["s"] < 45  # одна строка: «Название, 12:30»
            g["tall"] = g["e"] - g["s"] >= 75  # хватает места на третью строку: где и с кем
    now_top = None
    if monday <= now.date() < monday + timedelta(days=ndays):
        minutes = now.hour * 60 + now.minute
        if hour_from * 60 <= minutes <= hour_to * 60:
            now_top = round((minutes - hour_from * 60) * hour_px / 60)
    return {
        "days": days, "allday": allday, "allday_rows": len(rows), "hours": list(range(hour_from, hour_to)),
        "hour_from": hour_from, "hour_px": hour_px, "height": (hour_to - hour_from) * hour_px,
        "now_top": now_top, "now": now, "events": events, "title": week_title(monday), "week": monday.isocalendar().week,
    }


DAY_HOUR_PX = 44  # высота часа в расписании на сводке


def day_grid(settings: dict | None = None) -> dict:
    """Сегодня — расписанием, как день в календаре. Всегда видно 9:00–18:00 и текущий час (днём и вечером),
    чтобы линия «сейчас» показывала, что уже прошло, а что впереди."""
    now = config.now(settings)
    lo, hi = 9, 18
    if 7 <= now.hour < 23:
        lo, hi = min(lo, now.hour), max(hi, now.hour + 1)
    grid = week_grid(now.date(), settings, ndays=1, hour_px=DAY_HOUR_PX, hours=(lo, hi))
    upcoming = [e for e in grid["events"] if not e["all_day"] and e["s_local"] > now]
    grid["next"] = upcoming[0] if upcoming else None
    grid["next_in"] = int((upcoming[0]["s_local"] - now).total_seconds() // 60) + 1 if upcoming else None
    return grid


def events_today(settings: dict | None = None) -> list[dict]:
    tz = config.tz(settings)
    day = config.now(settings).date()
    start = datetime.combine(day, time.min, tz)
    return events_between(start, start + timedelta(days=1))


def sources_health(settings: dict | None = None) -> list[dict]:
    settings = settings or config.load_settings()
    jobs = {j["name"]: j for j in store.rows("SELECT * FROM jobs")}
    out = []
    for key in ("telegram", "ymessenger", "pachca", "mail", "gmail", "calendar", "google_calendar", "kaiten"):
        enabled = settings.get(key, {}).get("enabled")
        job = jobs.get(f"collect:{key}") or {}
        if not enabled:
            status = "off"
        elif job.get("last_error") and (not job.get("last_ok") or job["last_ok"] < (job.get("last_run") or "")):
            status = "error"
        elif job.get("last_ok"):
            status = "ok"
        else:
            status = "pending"
        out.append({
            "key": key, "title": SOURCE_TITLES[key], "status": status,
            "last_ok": job.get("last_ok"), "error": job.get("last_error"),
        })
    bot = settings["telegram_bot"]
    paired = bot.get("enabled") and bot.get("owner_chat_id")
    out.append({
        "key": "bot", "title": SOURCE_TITLES["bot"],
        "status": "ok" if paired else ("pending" if bot.get("enabled") else "off"),
        "last_ok": None, "error": None, "note": "подключён" if paired else "ждёт привязки",
    })
    out.append(_claude_health(settings))
    return out


def _claude_health(settings: dict) -> dict:
    from opsdash import brain

    item = {"key": "claude", "title": "Claude", "status": "ok", "last_ok": None, "error": None, "note": "подключён"}
    path = brain.find_claude(settings)
    login = brain.login_status(path, settings, max_age=60) if path else None
    if not path:
        item.update(status="error", note="не найден", error="Claude Code не найден — Настройки → Claude")
    elif login and not login["loggedIn"]:
        item.update(status="error", note="нужен вход", error="Вход в Claude не выполнен — Настройки → Claude → «Войти в Claude»")
    return item


def snapshot(settings: dict | None = None) -> dict:
    settings = settings or config.load_settings()
    commitments = records.list_commitments(status="open")
    return {
        "now": config.now(settings),
        "owner": settings["owner"].get("name") or "",
        "proposals": proposals.list_proposals("pending"),
        "attention": records.list_attention("open"),
        "mine": [c for c in commitments if c["direction"] == "mine"],
        "to_me": [c for c in commitments if c["direction"] == "to_me"],
        "other": [c for c in commitments if c["direction"] == "other"],
        "stale": kaiten.stale_cards(settings),
        "events": events_today(settings),
        "meetings": records.list_meetings(limit=8),
        "health": sources_health(settings),
        "unprocessed": store.scalar("SELECT COUNT(*) FROM messages WHERE processed=0") or 0,
    }


def _e(s) -> str:
    return html.escape(str(s or ""))


def _due(c: dict) -> str:
    if not c.get("due"):
        return ""
    mark = " ❗" if c.get("overdue") else ""
    return f" — до {proposals._fmt_date(c['due'])}{mark}"


def today_text(settings: dict | None = None, snap: dict | None = None) -> str:
    """Короткая сводка для бота (HTML-разметка Telegram). Без ИИ — работает всегда."""
    s = snap or snapshot(settings)
    lines = [f"<b>Сводка на {s['now'].strftime('%d.%m %H:%M')}</b>"]
    if s["events"]:
        lines.append("\n<b>📅 Сегодня</b>")
        for e in s["events"][:8]:
            when = "весь день" if e["all_day"] else store.local(e["start"], "%H:%M")
            lines.append(f"• {when} {_e(e['title'])}")
    if s["attention"]:
        lines.append(f"\n<b>💬 Ждут вашего ответа: {len(s['attention'])}</b>")
        for a in s["attention"][:6]:
            pr = "🔴 " if a["priority"] == "high" else ""
            lines.append(f"• {pr}{_e(a['sender_name'] or a['chat_title'])}: {_e(a['summary'])}")
    overdue_mine = [c for c in s["mine"] if c["overdue"]]
    if s["mine"]:
        lines.append(f"\n<b>🫵 Ваши задачи: {len(s['mine'])}</b>" + (f" (просрочено {len(overdue_mine)})" if overdue_mine else ""))
        for c in (overdue_mine + [c for c in s["mine"] if not c["overdue"]])[:5]:
            lines.append(f"• {_e(c['what'])}{_e(' → ' + c['whom'] if c['whom'] else '')}{_due(c)}")
    if s["to_me"]:
        overdue = [c for c in s["to_me"] if c["overdue"]]
        lines.append(f"\n<b>🤝 Вам должны: {len(s['to_me'])}</b>" + (f" (просрочено {len(overdue)})" if overdue else ""))
        for c in (overdue + [c for c in s["to_me"] if not c["overdue"]])[:5]:
            lines.append(f"• {_e(c['who'])}: {_e(c['what'])}{_due(c)}")
    if s["stale"]:
        lines.append(f"\n<b>🐌 Без движения в Kaiten: {len(s['stale'])}</b>")
        for c in s["stale"][:5]:
            lines.append(f"• {_e(c['title'])} — {_e(c['responsible_name'] or 'без исполнителя')}, {c['idle_days']} дн.")
    if s["proposals"]:
        lines.append(f"\n<b>⏳ Ждут вашего «да»: {len(s['proposals'])}</b> — /approvals")
    if len(lines) == 1:
        lines.append("Всё спокойно: срочного нет.")
    return "\n".join(lines)


def overview_markdown(settings: dict | None = None) -> str:
    """Та же сводка в Markdown — для Claude (./ops overview)."""
    s = snapshot(settings)
    settings = settings or config.load_settings()
    out = [f"# Сводка на {s['now'].strftime('%Y-%m-%d %H:%M, %A')} ({settings['owner']['timezone']})",
           f"Директор: {settings['owner'].get('name') or '(имя не указано в настройках)'}"]
    out.append("\n## Сегодня в календаре")
    out += [f"- {store.local(e['start'], '%H:%M')}–{store.local(e['end'], '%H:%M')} {e['title']} (uid {e['uid']})"
            for e in s["events"]] or ["- встреч нет"]
    out.append("\n## Нужен ответ директора")
    out += [f"- [attention {a['id']}] {a['priority']} · {a['source']} · {a['chat_title']} · {a['sender_name']}: "
            f"{a['summary']}" for a in s["attention"]] or ["- нет"]
    for key, title in (("mine", "Задачи директора"), ("to_me", "Должны директору"), ("other", "Между коллегами")):
        out.append(f"\n## {title}")
        out += [f"- [commitment {c['id']}] {c['who'] or ''} → {c['whom'] or ''}: {c['what']}"
                f"{' — до ' + c['due'] if c['due'] else ''}{' (ПРОСРОЧЕНО)' if c['overdue'] else ''}"
                f"{' · kaiten #' + str(c['kaiten_card_id']) if c['kaiten_card_id'] else ''}"
                f"{' · часть [commitment ' + str(c['parent_id']) + ']' if c.get('parent_id') else ''}"
                f"{' · предложено закрыть' if c.get('done_hint') else ''}" for c in s[key]] or ["- нет"]
    out.append("\n## Kaiten: без движения")
    out += [f"- #{c['id']} {c['title']} · {c['responsible_name'] or 'без исполнителя'} · {c['column_title']} · "
            f"{c['idle_days']} дн. · {c['url']}" for c in s["stale"]] or ["- нет"]
    out.append("\n## Ждут подтверждения")
    out += [f"- [proposal {p['id']}] {p['title']}" for p in s["proposals"]] or ["- нет"]
    out.append("\n## Последние встречи")
    out += [f"- {m['id']}: {m['title'] or ''} · {m['status']}" for m in s["meetings"]] or ["- нет"]
    out.append(f"\nНеразобранных сообщений: {s['unprocessed']}")
    return "\n".join(out)


def since(hours: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).replace(microsecond=0).isoformat()
