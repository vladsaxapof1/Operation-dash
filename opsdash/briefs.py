"""Брифы к встречам из календаря.

Появилась встреча на ближайшие сутки-полтора — Claude по навыку meeting-brief определяет тему (по названию,
описанию и участникам) и собирает бриф: что было на прошлых встречах этой серии или по этой теме, открытые
задачи с участниками, решения из базы знаний, нужные файлы, вопросы к встрече. Для встречи один на один это
и есть повестка. Тема не ясна — встреча помечается «пропущена» с причиной, бриф можно собрать кнопкой.
Брифы видны в календаре (галочка на встрече) и копируются одной кнопкой.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone

from opsdash import config, overview, records, store


def fingerprint(e: dict) -> str:
    """Название, описание и время встречи: если что-то поменялось — бриф пора пересобрать."""
    raw = f"{e.get('title') or ''}|{e.get('description') or ''}|{e.get('start') or ''}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def file_for(e: dict) -> str:
    """Куда Claude пишет текст брифа (data/briefs/…md) — путь от папки PULSE."""
    when = store.local(e["start"], "%Y-%m-%d_%H%M")
    slug = re.sub(r"[^a-zа-я0-9]+", "-", (e.get("title") or "встреча").lower().replace("ё", "е")).strip("-")[:40]
    return f"data/briefs/{when}_{slug or 'vstrecha'}.md"


def _ignored(e: dict, settings: dict) -> str | None:
    """Почему к встрече бриф не нужен вовсе (не спрашиваем Claude): весь день, без названия, занятое время."""
    title = (e.get("title") or "").strip()
    if e.get("all_day"):
        return "встреча на весь день"
    if not title or title.lower() in ("(без названия)", "без названия"):
        return "без названия"
    low = title.lower()
    if any(word.lower() in low for word in settings["briefs"].get("ignore") or []):
        return "это занятое время, а не встреча"
    return None


def upcoming(settings: dict | None = None, hours: float | None = None) -> list[dict]:
    settings = settings or config.load_settings()
    now = datetime.now(timezone.utc)
    hours = hours if hours is not None else float(settings["briefs"].get("hours_ahead") or 36)
    return [e for e in overview.events_between(now, now + timedelta(hours=hours)) if store.parse_ts(e["start"]) > now]


def pending(settings: dict | None = None, limit: int = 5) -> list[dict]:
    """Встречи, к которым пора собрать бриф: брифа нет или встреча изменилась (название, описание, время).
    Занятое время («тайм-аут»), встречи без названия и на весь день помечаем пропущенными сразу."""
    settings = settings or config.load_settings()
    (config.DATA_DIR / "briefs").mkdir(parents=True, exist_ok=True)  # сюда Claude пишет тексты брифов
    out = []
    for e in upcoming(settings):
        b = get(e["uid"])
        fp = fingerprint(e)
        if b and b["fingerprint"] == fp:
            continue
        why = _ignored(e, settings)
        if why:
            skip(e["uid"], why, event=e)
            continue
        out.append(e)
    return out[:limit]


def get(uid: str) -> dict | None:
    return store.row("SELECT * FROM briefs WHERE uid=?", (uid,))


def for_events(events: list[dict]) -> list[dict]:
    """Брифы к встречам календаря: e["brief"] — запись или None (для галочки и карточки встречи)."""
    uids = [e["uid"] for e in events]
    found = {b["uid"]: b for b in store.rows(
        f"SELECT * FROM briefs WHERE uid IN ({','.join('?' * len(uids))})", uids)} if uids else {}
    for e in events:
        b = found.get(e["uid"])
        e["brief"] = b
        e["brief_key"] = hashlib.sha1(e["uid"].encode("utf-8")).hexdigest()[:12]  # id окна с брифом на странице
        e["brief_stale"] = bool(b and b["fingerprint"] != fingerprint(e))
    return events


def _save(uid: str, event: dict | None, **fields) -> None:
    e = event or store.row("SELECT * FROM events WHERE uid=?", (uid,)) or {}
    now = store.utcnow()
    row = {"title": e.get("title"), "start": e.get("start"), "fingerprint": fingerprint(e) if e else None,
           "topic": None, "text": None, "why": None, **fields}
    store.execute(
        "INSERT INTO briefs(uid, title, start, status, topic, text, why, fingerprint, created, updated) "
        "VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(uid) DO UPDATE SET title=excluded.title, start=excluded.start, "
        "status=excluded.status, topic=excluded.topic, text=excluded.text, why=excluded.why, "
        "fingerprint=excluded.fingerprint, updated=excluded.updated",
        (uid, row["title"], row["start"], row["status"], row["topic"], row["text"], row["why"], row["fingerprint"],
         now, now))


def save(uid: str, text: str, topic: str | None = None) -> None:
    if not store.row("SELECT 1 FROM events WHERE uid=?", (uid,)):
        raise ValueError(f"Нет встречи {uid} в календаре (./ops briefs pending)")
    if not text.strip():
        raise ValueError("Пустой бриф")
    _save(uid, None, status="ready", text=text.strip(), topic=(topic or "").strip() or None)


def skip(uid: str, why: str, event: dict | None = None) -> None:
    """Бриф не собираем: тема не ясна или это не встреча. Пересоберётся, если встреча изменится."""
    _save(uid, event, status="skipped", why=why.strip() or "тема не ясна")


def fail(uid: str, why: str) -> None:
    _save(uid, None, status="failed", why=why)


def forget(uid: str) -> None:
    """Собрать бриф заново (кнопка «Обновить» / «Собрать»): следующий проход возьмёт встречу снова."""
    store.execute("DELETE FROM briefs WHERE uid=?", (uid,))


# --- контекст для Claude ---------------------------------------------------------------

def _series(uid: str) -> str:
    """Серия повторяющейся встречи: у Google — id до «_R…» / «#дата», у Яндекса — uid до «#»."""
    base = uid.split("#", 1)[0]
    return re.split(r"_R\d{8}T\d{6}", base, maxsplit=1)[0].removeprefix("google:")


_LAT = str.maketrans({"а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh", "з": "z",
                      "и": "i", "й": "i", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r",
                      "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "sh",
                      "ъ": "", "ы": "i", "ь": "", "э": "e", "ю": "yu", "я": "ya"})


def _lat(word: str) -> str:
    """Фамилия латиницей для сравнения с почтой: «Иванов» и p.ivanov — одно и то же."""
    return word.lower().translate(_LAT).replace("kh", "h").replace("ts", "c").replace("y", "i")


def _known_names() -> list[str]:
    """Полные имена, которые PULSE знает: справочник, задачи, личные чаты Telegram, Kaiten."""
    names = [n for p in records.list_people() for n in [p["name"], *p["aliases"]]]
    names += [r["n"] for r in store.rows("SELECT DISTINCT who AS n FROM commitments WHERE who IS NOT NULL "
                                         "UNION SELECT DISTINCT whom FROM commitments WHERE whom IS NOT NULL "
                                         "UNION SELECT title FROM chats WHERE kind='private' AND title IS NOT NULL "
                                         "UNION SELECT full_name FROM kaiten_users WHERE full_name IS NOT NULL")]
    return list(dict.fromkeys(n.strip() for n in names if n and n.strip()))


def _name_for_email(email: str, names: list[str]) -> str | None:
    """Имя человека по рабочей почте вида p.ivanov@… — по фамилии латиницей (и первой букве имени)."""
    parts = [x for x in re.split(r"[._-]", email.split("@")[0].lower()) if x]
    if not parts or len(parts[-1]) < 3:
        return None
    surname = parts[-1].replace("kh", "h").replace("ts", "c").replace("y", "i")
    initial = parts[0][0] if len(parts) > 1 else ""
    for n in names:
        words = re.findall(r"[А-Яа-яЁё]+", n)
        if len(words) < 2:
            continue
        last = _lat(words[-1])
        if last[:4] == surname[:4] or (last[:3] == surname[:3] and initial and _lat(words[0])[:1] == initial):
            return " ".join(words[:2])
    return None


def _tasks_of(name: str, known: list[str]) -> list[dict]:
    """Открытые задачи человека: по полному имени, иначе по фамилии, а по одному имени — если так он записан
    в справочнике или тёзок нет (две Юлии — не угадываем)."""
    words = re.findall(r"[А-Яа-яЁё]{3,}", name)
    if not words:
        return []
    found = records.list_commitments(status="open", person=name, limit=30)
    if not found and len(words) > 1:
        found = records.list_commitments(status="open", person=words[-1], limit=30)
    if not found:
        first = words[0].lower()
        # «Пётр» в справочнике — это и есть «тот самый» Пётр директора, даже если в чатах есть тёзки
        in_people = any(first == n.lower() for p in records.list_people() for n in [p["name"], *p["aliases"]])
        namesakes = {n for n in known if len(n.split()) > 1 and n.split()[0].lower() == first
                     and not (len(words) > 1 and words[-1].lower() in n.lower())}
        if in_people or not namesakes:
            found = records.list_commitments(status="open", person=words[0], limit=30)
    return found


def context(uid: str, settings: dict | None = None) -> str:
    """Что известно к встрече — одним текстом для Claude: сама встреча и участники, прошлые встречи этой серии,
    с похожим названием или (для встречи один на один) с этим человеком, открытые задачи с участниками,
    решения из базы знаний и документы по теме."""
    from opsdash import anthology

    settings = settings or config.load_settings()
    e = store.row("SELECT * FROM events WHERE uid=?", (uid,))
    if not e:
        raise ValueError(f"Нет встречи {uid} в календаре")
    owner = (settings["owner"].get("name") or "").split(" ")[0].lower()
    known = _known_names()
    by_email = {(p.get("email") or "").lower(): p["name"] for p in records.list_people() if p.get("email")}
    own_emails = {x.lower() for x in re.findall(r"[\w.+-]+@[\w.-]+", f"{e['calendar'] or ''} "
                  f"{settings['calendar'].get('login') or ''} {settings.get('mail', {}).get('login') or ''}")}
    names = []  # участники, кроме самого директора
    for a in store.loads(e["attendees"], []):
        email = (a.get("email") or "").lower()
        if email.endswith("resource.calendar.google.com") or email in own_emails:
            continue
        label = a.get("name") if a.get("name") and "@" not in a.get("name") else None
        name = by_email.get(email) or label or _name_for_email(email, known) or email
        if name.split(" ")[0].lower() != owner:
            names.append(name)
    # имена в названии («Пётр / Анна») — тоже участники: так устроены встречи один на один
    firsts = {re.findall(r"[А-Яа-яЁё]+", n)[0].lower(): n for n in known if re.findall(r"[А-Яа-яЁё]+", n)}
    for w in re.findall(r"[А-Яа-яЁё]{3,}", e["title"] or ""):
        if w.lower() in firsts and w.lower() != owner and not any(w.lower() in n.lower() for n in names):
            names.append(w)
    out = [f"Встреча: {e['title']}", f"Когда: {store.local(e['start'], '%d.%m.%Y %H:%M')}–{store.local(e['end'], '%H:%M')}"
           f" ({e['calendar'] or ''})", f"uid: {uid}"]
    if e["organizer"]:
        out.append(f"Организатор: {e['organizer']}")
    if names:
        out.append(f"Участники, кроме вас ({len(names)}): " + ", ".join(names))
    if (e["description"] or "").strip():
        out += ["Описание:", e["description"].strip()[:2000]]

    keys = list(dict.fromkeys(w for n in names if "@" not in n for w in re.findall(r"[А-Яа-яЁё]{3,}", n)))
    name_stems = records._stems(" ".join(keys) + " " + owner)
    topic = records._stems(e["title"]) - name_stems  # о чём встреча, без имён
    series = _series(uid)
    past = []
    for m in store.rows("SELECT id, title, started_at, headline, event_uid, participants FROM meetings "
                        "WHERE status='processed' AND started_at < ? ORDER BY started_at DESC LIMIT 300", (e["start"],)):
        same = bool(m["event_uid"]) and _series(m["event_uid"]) == series
        common = len(topic & records._stems(m["title"]))
        people_m = " ".join(store.loads(m["participants"], [])) + " " + (m["title"] or "")
        one_on_one = not topic and keys and any(k.lower() in people_m.lower() for k in keys) \
            and len(store.loads(m["participants"], [])) <= 3
        if same or (topic and common >= (1 if len(topic) <= 2 else 2)) or one_on_one:
            past.append((m, same))
        if len(past) >= 3:
            break
    out.append("\nПрошлые встречи (серия, похожее название или с этим человеком) — разбор в data/meetings/<id>/summary.md:")
    out += [f"- {m['id']} · {store.local(m['started_at'], '%d.%m')} · {m['title']}{' (та же серия)' if same else ''}"
            f"{' — ' + m['headline'] if m['headline'] else ''}" for m, same in past] or ["- не нашлось"]

    tasks = {}
    for n in names:
        if "@" in n:
            continue
        for c in _tasks_of(n, known):
            tasks[c["id"]] = c
    out.append("\nОткрытые задачи с участниками:")
    out += [f"- [{c['id']}] {records.route(c)}: {records.headline(c)}{' · до ' + c['due'] if c['due'] else ''}"
            f"{' (ПРОСРОЧЕНО)' if c['overdue'] else ''}" for c in sorted(tasks.values(), key=lambda c: c["due"] or "9")]
    if not tasks:
        out.append("- не нашлось (проверь ./ops commitments list --person \"<имя>\")")

    query = " ".join(w for w in re.findall(r"\w+", e["title"] or "") if records._stems(w) & topic)
    try:
        decisions = anthology.items(q=query, limit=5) if query else []
    except Exception:  # noqa: BLE001 — поиск по базе знаний не обязателен
        decisions = []
    out.append("\nРешения из базы знаний по теме:")
    out += [f"- {d.get('happened') or ''} {d['title']}" for d in decisions] or ["- не нашлось — поищи ./ops kb search"]
    try:
        from opsdash import docs

        files = docs.search(query, limit=5) if query and settings["knowledge"].get("folders") else []
    except Exception:  # noqa: BLE001 — индекс документов не обязателен
        files = []
    out.append("\nДокументы по теме (./ops docs show \"<файл>\" --part \"…\"):")
    out += [f"- {f.get('rel') or f['path']}{' · ' + f['part'] if f.get('part') else ''}" for f in files] or ["- не нашлось"]
    out.append(f"\nФайл для брифа: {file_for(dict(e))}")
    return "\n".join(out)
