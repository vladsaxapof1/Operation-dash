"""Раздел «Дайджесты»: утренний и вечерний дайджест (то, что ушло в Telegram), итоги недели и сводка
рабочих групп Telegram. Тексты пишет Claude по навыкам digest, weekly и groups-digest и сохраняет командой
./ops notify; здесь — хранение и данные, из которых Claude собирает итоги недели и сводку групп."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

from opsdash import config, overview, records, store

KINDS = {"daily": ("morning", "evening"), "weekly": ("weekly",), "groups": ("groups",)}
TITLES = {"morning": "Утренний дайджест", "evening": "Вечерний дайджест", "weekly": "Итоги недели",
          "groups": "Рабочие группы Telegram"}


def daily_kind(settings: dict | None = None) -> str:
    return "morning" if config.now(settings).hour < 14 else "evening"


def save(kind: str, text: str) -> int:
    return store.execute("INSERT INTO digests(kind, text, created) VALUES(?,?,?)", (kind, text, store.utcnow()))


def latest(tab: str = "daily", limit: int = 30) -> list[dict]:
    kinds = KINDS.get(tab, KINDS["daily"])
    items = store.rows(f"SELECT * FROM digests WHERE kind IN ({','.join('?' * len(kinds))}) ORDER BY id DESC LIMIT ?",
                       [*kinds, limit])
    for d in items:
        d["title"] = TITLES.get(d["kind"], d["kind"])
    return items


def counts() -> dict[str, int]:
    return {tab: store.scalar(f"SELECT COUNT(*) FROM digests WHERE kind IN ({','.join('?' * len(k))})", k) or 0
            for tab, k in KINDS.items()}


# --- данные для итогов недели и сводки групп ---------------------------------------------

def _busy(e: dict, settings: dict) -> bool:
    """Встреча, а не занятое время: с названием, не на весь день, не «тайм-аут» и т. п."""
    title = (e.get("title") or "").strip().lower()
    ignore = [w.lower() for w in settings["briefs"].get("ignore") or []]
    return bool(title) and title not in ("(без названия)", "без названия") and not e.get("all_day") \
        and not any(w in title for w in ignore)


def _minutes(e: dict) -> int:
    return int((store.parse_ts(e["end"]) - store.parse_ts(e["start"])).total_seconds() // 60)


def week_stats(day: date | None = None, settings: dict | None = None) -> str:
    """Неделя, в которую входит day (пн–вс, до текущего момента), — одним текстом для Claude: встречи и часы,
    разобранные записи, задачи (новые, закрытые, просроченные), решения, что было на подтверждении; и что впереди."""
    settings = settings or config.load_settings()
    tz = config.tz(settings)
    day = day or config.now(settings).date()
    monday = day - timedelta(days=day.weekday())
    start = datetime.combine(monday, time.min, tz)
    end = min(start + timedelta(days=7), config.now(settings))
    s_utc, e_utc = store.to_utc_iso(start), store.to_utc_iso(end)
    out = [f"Неделя {monday:%d.%m}–{(monday + timedelta(days=6)):%d.%m.%Y} (данные до {end:%d.%m %H:%M})"]

    events = [e for e in overview.events_between(start, end) if _busy(e, settings)]
    total = sum(_minutes(e) for e in events)
    out.append(f"\n## Встречи по календарю: {len(events)}, {total / 60:.1f} ч".replace(".", ","))
    out += [f"- {overview.WEEKDAYS_SHORT[store.parse_ts(e['start']).astimezone(tz).weekday()]} "
            f"{store.local(e['start'], '%d.%m %H:%M')} · {_minutes(e)} мин · {e['title']}" for e in events]

    met = store.rows("SELECT id, title, started_at, duration_sec, headline FROM meetings WHERE status='processed' "
                     "AND started_at >= ? AND started_at < ? ORDER BY started_at", (s_utc, e_utc))
    out.append(f"\n## Разобранные записи встреч: {len(met)}")
    out += [f"- {m['id']} · {store.local(m['started_at'], '%d.%m')} · {m['title']}"
            f"{' — ' + m['headline'] if m['headline'] else ''}" for m in met]

    created = store.rows("SELECT * FROM commitments WHERE created >= ? AND created < ?", (s_utc, e_utc))
    closed = store.rows("SELECT * FROM commitments WHERE status='done' AND closed_at >= ? AND closed_at < ?", (s_utc, e_utc))
    out.append("\n## Задачи и договорённости")
    for key, title in records.DIRECTIONS.items():
        out.append(f"- {title}: новых {sum(c['direction'] == key for c in created)}, "
                   f"закрыто {sum(c['direction'] == key for c in closed)}")
    out.append("Закрыто за неделю:")
    out += [f"  - {records.route(c)}: {records.headline(c)}" for c in closed] or ["  - ничего"]
    overdue = records.list_commitments(status="open", overdue=True)
    out.append(f"Просрочено сейчас: {len(overdue)}")
    out += [f"  - [{c['id']}] {records.route(c)}: {records.headline(c)} · срок {c['due']}" for c in overdue]

    load = records.workload()
    out.append("\n## Загрузка (открытые задачи · карточки Kaiten)")
    out += [f"- {w['name']}: {w['open']} (просрочено {w['overdue']}) · Kaiten {w['kaiten']}" for w in load[:12]]

    decisions = store.rows("SELECT kind, title FROM anthology WHERE status IN ('accepted','proposed') "
                           "AND COALESCE(happened, substr(created, 1, 10)) >= ? ORDER BY id", (monday.isoformat(),))
    out.append(f"\n## База знаний за неделю: {len(decisions)}")
    out += [f"- {d['kind']}: {d['title']}" for d in decisions]

    props = store.rows("SELECT status, COUNT(*) AS n FROM proposals WHERE created >= ? AND created < ? GROUP BY status",
                       (s_utc, e_utc))
    answered = store.scalar("SELECT COUNT(*) FROM attention WHERE status='done' AND updated >= ?", (s_utc,)) or 0
    out.append("\n## Подтверждения и ответы")
    out.append("- предложения: " + (", ".join(f"{p['status']} {p['n']}" for p in props) or "не было"))
    out.append(f"- закрыто «нужен ответ»: {answered}, ждут сейчас: "
               f"{store.scalar('SELECT COUNT(*) FROM attention WHERE status=?', ('open',)) or 0}")

    nxt = datetime.combine(monday + timedelta(days=7), time.min, tz)
    upcoming = [e for e in overview.events_between(nxt, nxt + timedelta(days=7)) if _busy(e, settings)]
    due_next = [c for c in records.list_commitments(status="open")
                if c["due"] and (monday + timedelta(days=7)).isoformat() <= c["due"] <= (monday + timedelta(days=13)).isoformat()]
    out.append(f"\n## Следующая неделя: встреч {len(upcoming)}, {sum(_minutes(e) for e in upcoming) / 60:.1f} ч"
               .replace(".", ","))
    out += [f"- срок на неделе: {records.route(c)}: {records.headline(c)} · {c['due']}" for c in due_next]
    return "\n".join(out)


def group_messages(hours: int = 24) -> list[dict]:
    """Сообщения рабочих групп (чаты-группы, которые PULSE читает) за последние hours часов — по чатам."""
    since = store.to_utc_iso(datetime.now().astimezone() - timedelta(hours=hours))
    rows = store.rows(
        "SELECT m.* FROM messages m JOIN chats c ON c.source=m.source AND c.chat_id=m.chat_id "
        "WHERE c.enabled=1 AND c.kind='group' AND m.ts >= ? ORDER BY m.chat_title, m.ts", (since,))
    chats: dict[tuple, dict] = {}
    for m in rows:
        chats.setdefault((m["source"], m["chat_id"]), {"title": m["chat_title"], "source": m["source"],
                                                       "messages": []})["messages"].append(m)
    return list(chats.values())
