"""Задачи на таймлайне (диаграмма Ганта): полоса от появления задачи до срока.

Масштаб: день — крупные дни с подписями, неделя — дни помельче с подписью недель, месяц — месяцы.
Выходные закрашены, пунктир — текущее время. Всё считается здесь, шаблон только рисует.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from opsdash import config, store

SCALES = {  # пикселей на день, сколько дней показать минимум, запас после последнего срока
    "day": (64, 21, 7),
    "week": (20, 63, 14),
    "month": (6, 180, 30),
}
MAX_DAYS = 400
WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
MONTHS = ["Январь", "Февраль", "Март", "Апрель", "Май", "Июнь", "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь",
          "Декабрь"]


def _local_day(ts: str | None) -> date | None:
    dt = store.parse_ts(ts)
    return dt.astimezone(config.tz()).date() if dt else None


def _due(value: str | None) -> date | None:
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None


def layout(groups: list[tuple[str, str, list[dict]]], scale: str = "week", now: datetime | None = None) -> dict:
    """groups: [(ключ, название, задачи)]. Возвращает сетку дней, подписи, полосы задач и линию «сейчас»."""
    scale = scale if scale in SCALES else "week"
    px, min_days, pad = SCALES[scale]
    now = now or config.now()
    today = now.date()
    spans = []
    for _, _, items in groups:
        for c in items:
            start = _local_day(c.get("created")) or today
            done = _local_day(c.get("closed_at")) if c.get("status") == "done" else None
            due = _due(c.get("due"))
            end = due or done or today
            spans.append((c, min(start, end), end, due))
    first = min([today - timedelta(days=2)] + [s for _, s, _, _ in spans])
    first -= timedelta(days=first.weekday() if scale != "day" else 1)  # неделя — с понедельника
    last = max([today] + [e for _, _, e, _ in spans]) + timedelta(days=pad)
    last = max(last, first + timedelta(days=min_days))
    if (last - first).days > MAX_DAYS:  # очень старые задачи не растягивают шкалу на годы
        first = last - timedelta(days=MAX_DAYS)
    days = (last - first).days + 1

    def x(d: date) -> int:
        return (d - first).days * px

    rows = {}
    for c, start, end, due in spans:
        left = max(0, x(start))
        rows[id(c)] = {"left": left, "width": max(px, x(end) + px - left), "due": due is not None,
                       "overdue": bool(c.get("overdue")), "start": start, "end": end}
    out_groups = [{"key": key, "title": title, "items": [{**c, "bar": rows[id(c)]} for c in items]}
                  for key, title, items in groups]
    # подписи шкалы: верхняя строка — месяцы (или недели), нижняя — дни (в масштабе «месяц» — без дней)
    top, bottom, weekends = [], [], []
    for i in range(days):
        d = first + timedelta(days=i)
        if d.weekday() >= 5:
            weekends.append(i * px)
        if scale == "week" and d.weekday() == 0:
            top.append({"left": i * px, "text": f"{d:%d.%m} – {d + timedelta(days=6):%d.%m}"})
        if scale in ("day", "month") and (d.day == 1 or i == 0):
            top.append({"left": i * px, "text": f"{MONTHS[d.month - 1]} {d.year}"})
        if scale == "day":
            bottom.append({"left": i * px, "text": f"{WEEKDAYS[d.weekday()]} {d.day}", "today": d == today})
        elif scale == "week":
            bottom.append({"left": i * px, "text": str(d.day), "today": d == today})
    now_x = round(x(today) + (now.hour * 60 + now.minute) / 1440 * px)
    return {"scale": scale, "px": px, "width": days * px, "groups": out_groups, "top": top, "bottom": bottom,
            "weekends": weekends, "now_x": now_x, "now": now, "first": first}
