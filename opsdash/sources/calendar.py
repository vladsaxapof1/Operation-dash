"""Яндекс Календарь по CalDAV (пароль приложения «Календарь») и Google Календарь — только чтение,
по секретным адресам iCal (без OAuth и паролей).

Чтение: встречи на ближайшие дни — для сводок и чтобы привязать запись к встрече
(тема, участники). Запись (создать/отменить) — только в Яндекс и только через подтверждение.
Встречи Google хранятся в той же таблице с приставкой «google:» в uid.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, time, timedelta, timezone

from opsdash import config, store

SOURCE = "calendar"
GOOGLE = "google:"  # приставка uid у встреч из Google Календаря


class CalendarError(Exception):
    pass


def _client(settings: dict):
    import caldav

    cs = settings["calendar"]
    pwd = config.get_secret("CALDAV_APP_PASSWORD")
    if not cs.get("login") or not pwd:
        raise CalendarError("Укажите логин (адрес почты) и пароль приложения для календаря")
    return caldav.DAVClient(url=cs.get("url") or "https://caldav.yandex.ru", username=cs["login"], password=pwd)


def _calendars(settings: dict):
    client = _client(settings)
    try:
        cals = client.principal().calendars()
    except Exception as e:  # noqa: BLE001
        raise CalendarError(f"Календарь не пустил: {e}") from e
    wanted = [c.strip().lower() for c in settings["calendar"].get("calendars") or [] if c.strip()]
    if wanted:
        cals = [c for c in cals if (c.name or "").strip().lower() in wanted]
    return cals


def _to_dt(value, tzinfo) -> tuple[datetime, bool]:
    if isinstance(value, datetime):
        return (value if value.tzinfo else value.replace(tzinfo=tzinfo)), False
    if isinstance(value, date):
        return datetime.combine(value, time.min, tzinfo), True
    raise ValueError(value)


def _addr(v) -> str:
    return str(v).replace("mailto:", "").replace("MAILTO:", "").strip()


def parse_component(comp, cal_name: str, tzinfo) -> dict | None:
    if comp.get("dtstart") is None:
        return None
    start, all_day = _to_dt(comp.get("dtstart").dt, tzinfo)
    if comp.get("dtend") is not None:
        end, _ = _to_dt(comp.get("dtend").dt, tzinfo)
    elif comp.get("duration") is not None:
        end = start + comp.get("duration").dt
    else:
        end = start + (timedelta(days=1) if all_day else timedelta(hours=1))
    raw_att = comp.get("attendee") or []
    if not isinstance(raw_att, list):
        raw_att = [raw_att]
    attendees = []
    for a in raw_att:
        params = getattr(a, "params", {}) or {}
        attendees.append({"email": _addr(a), "name": params.get("CN", ""), "status": params.get("PARTSTAT", "")})
    uid = str(comp.get("uid") or uuid.uuid4())
    rid = comp.get("recurrence-id")
    key = f"{uid}#{_to_dt(rid.dt, tzinfo)[0].isoformat()}" if rid is not None else uid
    status = str(comp.get("status") or "").upper()
    if status == "CANCELLED":
        return None
    organizer = comp.get("organizer")
    return {
        "uid": key,
        "calendar": cal_name,
        "title": str(comp.get("summary") or "(без названия)"),
        "start": store.to_utc_iso(start),
        "end": store.to_utc_iso(end),
        "all_day": int(all_day),
        "location": str(comp.get("location") or ""),
        "description": str(comp.get("description") or "")[:3000],
        "organizer": _addr(organizer) if organizer else "",
        "attendees": store.json_col(attendees),
        "url": str(comp.get("url") or ""),
        "synced_at": store.utcnow(),
    }


def save_event(item: dict) -> None:
    cols = list(item)
    quoted = [f'"{c}"' for c in cols]
    updates = ",".join(f"{q}=excluded.{q}" for c, q in zip(cols, quoted) if c != "uid")
    store.execute(
        f"INSERT INTO events({','.join(quoted)}) VALUES({','.join('?' for _ in cols)}) "
        f"ON CONFLICT(uid) DO UPDATE SET {updates}",
        [item[c] for c in cols],
    )


def _window(settings: dict) -> tuple[datetime, datetime]:
    tzinfo = config.tz(settings)
    cs = settings["calendar"]
    now = datetime.now(tzinfo)
    start = datetime.combine(now.date() - timedelta(days=int(cs.get("days_back") or 2)), time.min, tzinfo)
    end = datetime.combine(now.date() + timedelta(days=int(cs.get("days_ahead") or 14)), time.max, tzinfo)
    return start, end


def _forget_missing(start: datetime, end: datetime, seen: list[str], google: bool) -> None:
    """Встречи в окне, которых больше нет в календаре, — отменены. Яндекс и Google чистятся каждый своё."""
    own = "uid LIKE 'google:%'" if google else "uid NOT LIKE 'google:%'"
    params = [store.to_utc_iso(start), store.to_utc_iso(end)]
    if seen:
        marks = ",".join("?" for _ in seen)
        store.execute(f"DELETE FROM events WHERE start >= ? AND start <= ? AND {own} AND uid NOT IN ({marks})",
                      [*params, *seen])
    else:
        store.execute(f"DELETE FROM events WHERE start >= ? AND start <= ? AND {own}", params)


def collect(settings: dict | None = None) -> str:
    settings = settings or config.load_settings()
    tzinfo = config.tz(settings)
    start, end = _window(settings)
    seen = []
    failed = False
    cals = _calendars(settings)
    store.set_state("calendar_names", [c.name or "" for c in cals])  # для выбора календаря в форме «Новая встреча»
    for cal in cals:
        try:
            found = cal.search(start=start, end=end, event=True, expand=True)
        except Exception as e:  # noqa: BLE001
            store.log(SOURCE, f"календарь «{cal.name}»: {e}", "warning")
            failed = True
            continue
        for ev in found:
            for comp in ev.icalendar_instance.walk("VEVENT"):
                item = parse_component(comp, cal.name or "", tzinfo)
                if not item:
                    continue
                save_event(item)
                seen.append(item["uid"])
    if failed:  # не удаляем то, что просто не удалось прочитать
        return f"встреч в окне: {len(seen)} (часть календарей не прочиталась)"
    _forget_missing(start, end, seen, google=False)
    return f"встреч в окне: {len(seen)}"


# --- Google Календарь: секретные адреса iCal ---------------------------------------

def google_urls() -> list[str]:
    return config.get_secret("GOOGLE_CALENDAR_ICAL").split()


def _google_feed(url: str):
    import httpx
    from icalendar import Calendar

    try:
        r = httpx.get(url, timeout=60, follow_redirects=True)
        r.raise_for_status()
        return Calendar.from_ical(r.content)
    except httpx.HTTPStatusError as e:
        raise CalendarError(f"Google Календарь не отдал встречи (код {e.response.status_code}) — "
                            "проверьте секретный адрес iCal") from e
    except (httpx.HTTPError, ValueError) as e:
        raise CalendarError(f"Google Календарь: не удалось прочитать адрес ({e})") from e


def google_collect(settings: dict | None = None) -> str:
    import recurring_ical_events

    settings = settings or config.load_settings()
    tzinfo = config.tz(settings)
    urls = google_urls()
    if not urls:
        raise CalendarError("Добавьте секретный адрес iCal Google Календаря в настройках")
    start, end = _window(settings)
    seen, names = [], []
    for url in urls:  # ошибка любого адреса — ошибка сбора: иначе его встречи удалились бы как отменённые
        feed = _google_feed(url)
        name = "Google: " + str(feed.get("x-wr-calname") or "календарь")
        names.append(name)
        for comp in recurring_ical_events.of(feed).between(start, end):  # повторы разворачиваются, переносы учтены
            item = parse_component(comp, name, tzinfo)
            if item:
                item["uid"] = GOOGLE + item["uid"]
                save_event(item)
                seen.append(item["uid"])
    _forget_missing(start, end, seen, google=True)
    store.set_state("google_calendar_names", names)
    return f"встреч в окне: {len(seen)}"


def google_check(settings: dict | None = None) -> str:
    urls = google_urls()
    if not urls:
        raise CalendarError("Добавьте секретный адрес iCal Google Календаря")
    names = [str(_google_feed(u).get("x-wr-calname") or "календарь") for u in urls]
    return "календари: " + ", ".join(names)


def check(settings: dict | None = None) -> str:
    settings = settings or config.load_settings()
    cals = _calendars(settings)
    return "календари: " + (", ".join(c.name or "?" for c in cals) or "не найдены")


def _write_calendar(settings: dict, name: str = ""):
    """Календарь для новой встречи: названный явно, иначе из настроек, иначе первый."""
    cals = _calendars(settings)
    if not cals:
        raise CalendarError("Не найден календарь для записи")
    if name and not any((c.name or "").strip().lower() == name.strip().lower() for c in cals):
        raise CalendarError(f"Нет календаря «{name}» в Яндекс Календаре")
    name = (name or settings["calendar"].get("write_calendar") or "").strip().lower()
    for c in cals:
        if name and (c.name or "").strip().lower() == name:
            return c
    return cals[0]


def write_calendar_names(settings: dict | None = None) -> tuple[list[str], str]:
    """Календари Яндекса, куда можно поставить встречу (по последнему сбору), и какой выбран по умолчанию."""
    settings = settings or config.load_settings()
    names = [n for n in store.get_state("calendar_names") or [] if n]
    wanted = (settings["calendar"].get("write_calendar") or "").strip().lower()
    default = next((n for n in names if n.strip().lower() == wanted), names[0] if names else "")
    return names, default


def _ical_dt(value: str) -> str:
    dt = store.parse_local(value)
    if dt is None:
        raise CalendarError(f"Неверное время: {value}")
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def build_ical(uid: str, title: str, start: str, end: str, attendees: list[str], description: str, organizer: str) -> str:
    def esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")

    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Pult//RU", "BEGIN:VEVENT",
        f"UID:{uid}", f"DTSTAMP:{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}",
        f"DTSTART:{_ical_dt(start)}", f"DTEND:{_ical_dt(end)}", f"SUMMARY:{esc(title)}",
    ]
    if description:
        lines.append(f"DESCRIPTION:{esc(description)}")
    if organizer:
        lines.append(f"ORGANIZER:mailto:{organizer}")
    for a in attendees:
        lines.append(f"ATTENDEE;ROLE=REQ-PARTICIPANT;PARTSTAT=NEEDS-ACTION;RSVP=TRUE:mailto:{a.strip()}")
    lines += ["END:VEVENT", "END:VCALENDAR"]
    return "\r\n".join(lines) + "\r\n"


def create_event(title: str, start: str, end: str, attendees: list[str], description: str = "",
                 calendar_name: str = "") -> str:
    settings = config.load_settings()
    cal = _write_calendar(settings, calendar_name)
    uid = f"{uuid.uuid4()}@pult"
    cal.save_event(build_ical(uid, title, start, end, attendees, description, settings["calendar"].get("login", "")))
    try:
        collect(settings)
    except Exception:  # noqa: BLE001
        pass
    return uid


def cancel_event(uid_key: str) -> None:
    if uid_key.startswith(GOOGLE):
        raise CalendarError("Встреча из Google Календаря — PULSE его только читает, отмените её в Google Календаре")
    settings = config.load_settings()
    uid = uid_key.split("#", 1)[0]
    if "#" in uid_key:
        raise CalendarError("Это повторяющаяся встреча — отмените её вручную в календаре")
    for cal in _calendars(settings):
        try:
            ev = cal.event_by_uid(uid)
        except Exception:  # noqa: BLE001
            continue
        ev.delete()
        store.execute("DELETE FROM events WHERE uid=?", (uid_key,))
        return
    raise CalendarError("Встреча не найдена в календаре")
