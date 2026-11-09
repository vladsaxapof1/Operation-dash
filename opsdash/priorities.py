"""Приоритет задач: Срочно / Высокий / Обычный / Низкий — на карточках «Задач» и в порядке «по приоритету».

Уровень = важность × срочность (MATRIX).
- Срочность PULSE считает сам при каждом показе: горит — просрочено, срок сегодня или встреча по задаче не позже
  следующего рабочего дня; скоро — срок в ближайшие SOON_WORKDAYS рабочих дня или срок проекта в ближайшие
  PROJECT_DAYS дней; остальное — не спешно. Рабочие дни — из настроек.
- Важность оценивает Claude (навык priorities, задание importance; при записи задачи — `--importance`): высокая,
  обычная или низкая и коротко почему. Не оценена — обычная. Критерии — общие для любой роли и компании, а свои
  директор добавляет «Вашими правилами» навыка.

Поправки: задачу ждут другие задачи — важность на ступень выше; задача сама ждёт (связь «ждёт», передана часть,
спросили и ждём ответа) — не выше «Обычного», её сейчас не продвинуть; зато то, чего ждёт задача, — не ниже её уровня
(приоритет перетекает к тому, без чего её не сделать); договорённость между коллегами — не выше «Обычного», если
её не ждёт ваша задача или «мне должны».

Директор может поставить уровень сам (priority_user): он закреплён, расчёт и Claude его не меняют; «Авто» — снова
по расчёту. Ручной уровень держится до «Авто» или закрытия задачи."""

from __future__ import annotations

from datetime import date, timedelta

from opsdash import config, store, worklog

LEVELS = {"urgent": "Срочно", "high": "Высокий", "normal": "Обычный", "low": "Низкий"}
RANK = {k: i for i, k in enumerate(LEVELS)}
IMPORTANCE = {"high": "высокая", "normal": "обычная", "low": "низкая"}
_STEPS = ["low", "normal", "high"]
# важность → уровень при срочности (горит, скоро, не спешно)
MATRIX = {
    "high": ("urgent", "high", "high"),
    "normal": ("high", "normal", "normal"),
    "low": ("normal", "low", "low"),
}
SOON_WORKDAYS = 3  # «скоро» — срок не дальше третьего рабочего дня
PROJECT_DAYS = 7   # срок проекта ближе недели — его задачи «скоро»
SORT_TITLE = "по приоритету"

# правило для навыков, которые записывают задачи (разбор встречи и переписки, бот, чат) и для задания importance
IMPORTANCE_RULE = (
    "Важность (--importance high|normal|low и --importance-why «почему», 2–6 слов) — насколько задача важна сама по "
    "себе, без срока: срок PULSE учтёт сам. Высокая — обещано руководителю, собственнику, клиенту или внешнему "
    "партнёру; влияет на деньги, клиентов, запуск, юридические риски; прямо сказали «срочно», «критично», «горит», "
    "«в приоритете»; от неё зависит работа нескольких людей. Низкая — «когда будет время», «не горит», «по "
    "возможности», административные мелочи (заявки, отпуск, доступы), «посмотреть», «для информации». Сомневаешься — "
    "обычная. Уровень, который директор поставила сама, не трогай."
)


def _plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(n)
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def _ctx(settings: dict | None = None) -> dict:
    """Сегодня и границы «горит» / «скоро» — по рабочим дням из настроек."""
    from opsdash import day

    settings = settings or config.load_settings()
    today = config.now(settings).date()
    return {"today": today, "iso": today.isoformat(),
            "next": day.next_workday(today, settings, 1).isoformat(),
            "soon": day.next_workday(today, settings, SOON_WORKDAYS).isoformat(),
            "project": (today + timedelta(days=PROJECT_DAYS)).isoformat()}


def _due_urgency(due: str | None, x: dict) -> tuple[int, str | None]:
    """Срочность по сроку: 2 — горит, 1 — скоро, 0 — не спешно; и почему."""
    if not due:
        return 0, None
    if due < x["iso"]:
        try:
            n = (x["today"] - date.fromisoformat(due)).days
        except ValueError:
            return 2, "просрочено"
        return 2, f"просрочено {n} {_plural(n, 'день', 'дня', 'дней')}"
    if due == x["iso"]:
        return 2, "срок сегодня"
    if due <= x["soon"]:
        return 1, "срок завтра" if due == (x["today"] + timedelta(days=1)).isoformat() else f"срок {due[8:10]}.{due[5:7]}"
    return 0, None


def _meeting_urgency(c: dict, x: dict) -> str | None:
    """Встреча по задаче ещё впереди и не позже следующего рабочего дня — к ней нужно готовиться."""
    m = c.get("meeting")
    if not m or m.get("past"):
        return None
    start = store.parse_ts(m.get("start"))
    if not start:
        return None
    d = start.astimezone(config.tz()).date().isoformat()
    if d > x["next"]:
        return None
    return "встреча по задаче " + ("сегодня" if d == x["iso"] else f"{d[8:10]}.{d[5:7]}")


def base_level(t: dict, x: dict) -> str:
    """Уровень задачи без связей — для той, что ждёт эту (её уровень перетекает сюда): ручной или важность × срок."""
    if t.get("priority_user") in LEVELS:
        return t["priority_user"]
    imp = t.get("importance") if t.get("importance") in IMPORTANCE else "normal"
    u, _ = _due_urgency(t.get("due"), x)
    return MATRIX[imp][2 - u]


def _higher(a: str, b: str) -> str:
    return a if RANK[a] <= RANK[b] else b


def _cap(level: str, top: str) -> str:
    """Не выше top: «Срочно» у ждущей задачи становится «Обычным»."""
    return level if RANK[level] >= RANK[top] else top


def compute(c: dict, x: dict) -> dict | None:
    """Приоритет открытой задачи (у закрытых и архивных — None). Нужны поля после records._relations: holds,
    blocked, waits, children, parent, project, meeting, now."""
    if c.get("status") != "open" or c.get("archived_at"):
        return None
    reasons: list[str] = []
    # срочность
    u, why = _due_urgency(c.get("due"), x)
    if u < 2 and (meet := _meeting_urgency(c, x)):
        u, why = 2, meet
    p = c.get("project")
    if u < 1 and p and p.get("status") == "active" and p.get("due") and x["iso"] <= p["due"] <= x["project"]:
        u, why = 1, f"проект «{p['title']}» до {p['due'][8:10]}.{p['due'][5:7]}"
    if why:
        reasons.append(why)
    # важность: оценка Claude, задачу ждут — на ступень выше
    imp = c.get("importance") if c.get("importance") in IMPORTANCE else "normal"
    if imp != "normal":
        reasons.append(f"{IMPORTANCE[imp]} важность" + (f": {c['importance_why']}" if c.get("importance_why") else ""))
    holds = [h for h in c.get("holds") or [] if h["task"]["status"] == "open" and not h["task"].get("archived_at")]
    if holds:
        imp = _STEPS[min(_STEPS.index(imp) + 1, 2)]
        names = list(dict.fromkeys("ваша задача" if h["task"]["direction"] == "mine" else (h["task"].get("doer") or "?")
                                   for h in holds))
        reasons.append(("её ждёт " if len(holds) == 1 else "её ждут ") + ", ".join(names[:3]))
    level = MATRIX[imp][2 - u]
    # сама ждёт — не выше «Обычного»
    waiting = None
    if c.get("blocked"):
        w = next((w for w in c.get("waits") or [] if not w["done"]), None)
        waiting = (f"ждёт «{w['task']['headline']}»" if w and w["task"] else f"ждёт: {w['event']}" if w else "ждёт")
    elif open_kids := [k for k in c.get("children") or [] if k["status"] == "open" and not k.get("archived_at")]:
        waiting = "передано: " + ", ".join(dict.fromkeys("ваша часть" if k["direction"] == "mine" else (k.get("who") or "?")
                                                        for k in open_kids))
    elif c.get("now") and c["now"].get("waiting"):
        waiting = c["now"]["waiting"]
    if waiting:
        level = _cap(level, "normal")
        reasons.append(waiting)
    # то, чего ждут: не ниже уровня ждущей задачи (и основной — у её части)
    waiters = [(h["task"], "без неё не сделать") for h in holds]
    parent = c.get("parent")
    if parent and parent.get("status") == "open" and not parent.get("archived_at"):
        waiters.append((parent, "часть задачи"))
    mine_waits = any(t.get("direction") in ("mine", "to_me") for t, _ in waiters)
    if c.get("direction") == "other" and not mine_waits:
        level = _cap(level, "normal")
    for t, how in waiters:
        inherited = base_level(t, x)
        if RANK[inherited] < RANK[level]:
            level = inherited
            reasons.append(f"{how} «{t.get('headline') or t.get('title') or ''}» ({LEVELS[inherited].lower()})")
    auto = level
    manual = c.get("priority_user") if c.get("priority_user") in LEVELS else None
    key = manual or auto
    return {"key": key, "title": LEVELS[key], "rank": RANK[key], "manual": bool(manual), "auto": auto,
            "auto_title": LEVELS[auto], "why": " · ".join(reasons), "set_at": c.get("priority_user_at")}


def decorate(items: list[dict], settings: dict | None = None) -> None:
    """c["prio"] — приоритет для карточки, окна задачи и порядка «по приоритету» (records._relations)."""
    if not items:
        return
    x = _ctx(settings)
    for c in items:
        c["prio"] = compute(c, x)


def sort_key(c: dict) -> tuple:
    """Порядок «по приоритету» — по умолчанию на доске и в списке:
    1. закреплённые «в работе» — всегда сверху, между собой по уровню (Срочно → Низкий), затем по сроку;
    2. остальные открытые — по сроку (просроченные, сегодня, завтра…, без срока — в конце), в один день — по уровню:
       сегодня Срочно → сегодня Высокий → … → завтра Срочно → завтра Высокий;
    3. закрытые, затем архивные.
    Дальше — сколько задач её ждут; равные — в том порядке, что пришли (новые сверху)."""
    p = c.get("prio")
    rank, due, holds = p["rank"] if p else len(LEVELS), c.get("due") or "9999-99-99", -len(c.get("holds") or [])
    if c.get("archived"):
        return (3, due, rank, holds)
    if c.get("status", "open") != "open":
        return (2, due, rank, holds)
    if c.get("in_work"):
        return (0, rank, due, holds)
    return (1, due, rank, holds)


# --- правка ----------------------------------------------------------------------------------------

def set_level(cid: int, level: str | None) -> bool:
    """Уровень, который директор поставила сама; None — снова по расчёту («Авто»). False — уже так."""
    if level is not None and level not in LEVELS:
        raise ValueError(f"Приоритет — один из: {', '.join(LEVELS)} или auto")
    c = store.row("SELECT priority_user FROM commitments WHERE id=?", (cid,))
    if not c:
        raise ValueError(f"Нет задачи [{cid}]")
    if c["priority_user"] == level:
        return False
    now = store.utcnow()
    store.execute("UPDATE commitments SET priority_user=?, priority_user_at=?, seen_at=COALESCE(seen_at, ?), updated=? "
                  "WHERE id=?", (level, now if level else None, now, now, cid))
    worklog.event(cid, f"Приоритет: {LEVELS[level]} — поставили сами" if level else "Приоритет снова считается сам")
    return True


def set_importance(cid: int, value: str, why: str | None = None) -> None:
    """Оценка важности (Claude): high | normal | low и коротко почему. Уровень, поставленный директором, не меняет."""
    if value not in IMPORTANCE:
        raise ValueError(f"Важность — одна из: {', '.join(IMPORTANCE)}")
    if not store.row("SELECT 1 FROM commitments WHERE id=?", (cid,)):
        raise ValueError(f"Нет задачи [{cid}]")
    why = " ".join((why or "").split())[:120] or None
    store.execute("UPDATE commitments SET importance=?, importance_why=? WHERE id=?", (value, why, cid))


def parse_level(value: str) -> str | None:
    """--priority: urgent | high | normal | low | auto (или по-русски: срочно, высокий, обычный, низкий)."""
    v = (value or "").strip().lower()
    if v in ("auto", "авто"):
        return None
    for key, title in LEVELS.items():
        if v in (key, title.lower()):
            return key
    raise ValueError("Приоритет — urgent | high | normal | low | auto")


# --- оценка важности Claude (задание importance) ---------------------------------------------------

def unassessed(limit: int = 200) -> list[dict]:
    """Открытые задачи директора и «мне должны» без оценки важности."""
    return store.rows("SELECT id, direction, who, whom, title, what, quote, due, source_type, project_id "
                      "FROM commitments WHERE direction IN ('mine', 'to_me') AND status='open' AND archived_at IS NULL "
                      "AND importance IS NULL ORDER BY due IS NULL, due, id LIMIT ?", (limit,))


def job_importance(**_) -> str:
    """Оценка важности у задач, где её ещё нет: лёгкая модель, одним списком, по навыку priorities (в запуск
    добавляются «Ваши правила» директора к нему). Уровни, которые директор поставила сама, не трогаются."""
    from opsdash import brain

    todo = unassessed()
    if not todo:
        return "оценивать нечего"
    prompt = (
        "Оцени важность задач строго по навыку priorities (.claude/skills/priorities/SKILL.md). Список: "
        "`./ops commitments importance`. Сохрани всё одной командой: "
        "`./ops commitments importance --set \"<id>=<high|normal|low>:<почему, 2–6 слов>; …\"`. "
        "Больше ничего не меняй и директору не пиши. Это фоновый запуск."
    )
    brain.run(prompt, model="haiku", job="importance", timeout_min=10)
    left = len(unassessed())
    return f"оценено задач: {len(todo) - left} из {len(todo)}"


def parse_batch(text: str) -> list[tuple[int, str, str | None]]:
    """«12=high:обещано собственнику; 15=low» → [(12, 'high', 'обещано собственнику'), (15, 'low', None)]."""
    out = []
    for part in (text or "").replace("\n", ";").split(";"):
        part = part.strip()
        if not part:
            continue
        cid, sep, rest = part.partition("=")
        value, _, why = rest.partition(":")
        try:
            num = int(cid.strip().lstrip("#[").rstrip("]"))
        except ValueError:
            raise ValueError(f"«{part}»: номер задачи — число") from None
        value = value.strip().lower()
        if not sep or value not in IMPORTANCE:
            raise ValueError(f"«{part}»: нужно <id>=<high|normal|low>[:почему]")
        out.append((num, value, why.strip() or None))
    return out
