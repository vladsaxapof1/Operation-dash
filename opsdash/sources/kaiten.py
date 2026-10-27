"""Kaiten: зеркало карточек отслеживаемых пространств, «без движения», запись.

Документация API: https://developers.kaiten.ru
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timedelta, timezone

import httpx

from opsdash import config, store

SOURCE = "kaiten"


class KaitenError(Exception):
    pass


def normalize_domain(domain: str) -> str:
    domain = (domain or "").strip()
    domain = re.sub(r"^https?://", "", domain).split("/")[0]
    return domain


class Kaiten:
    def __init__(self, domain: str, token: str, transport: httpx.BaseTransport | None = None):
        domain = normalize_domain(domain)
        if not domain or not token:
            raise KaitenError("Kaiten не настроен: укажите домен и токен в настройках")
        self.domain = domain
        self.http = httpx.Client(
            base_url=f"https://{domain}/api/latest",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout=30,
            transport=transport,
        )

    def _request(self, method: str, path: str, **kw):
        for attempt in range(4):
            resp = self.http.request(method, path, **kw)
            if resp.status_code == 429 and attempt < 3:
                time.sleep(2 ** attempt)
                continue
            if resp.status_code == 401:
                raise KaitenError("Kaiten: токен не подходит (401)")
            if resp.status_code >= 400:
                raise KaitenError(f"Kaiten {method} {path}: {resp.status_code} {resp.text[:300]}")
            if not resp.content:
                return None
            return resp.json()
        raise KaitenError("Kaiten: слишком много запросов, попробуйте позже")

    def get(self, path: str, **params):
        return self._request("GET", path, params=params or None)

    def post(self, path: str, payload: dict):
        return self._request("POST", path, json=payload)

    def patch(self, path: str, payload: dict):
        return self._request("PATCH", path, json=payload)

    # --- чтение ---
    def current_user(self) -> dict:
        return self.get("/users/current")

    def spaces(self) -> list[dict]:
        return self.get("/spaces") or []

    def boards(self, space_id: int) -> list[dict]:
        return self.get(f"/spaces/{space_id}/boards") or []

    def users(self) -> list[dict]:
        out, offset = [], 0
        while True:
            page = self.get("/users", limit=100, offset=offset) or []
            out.extend(page)
            if len(page) < 100:
                return out
            offset += 100

    def cards(self, space_id: int) -> list[dict]:
        out, offset = [], 0
        while True:
            page = self.get(
                "/cards", space_id=space_id, condition=1, limit=100, offset=offset,
                additional_card_fields="description",
            ) or []
            out.extend(page)
            if len(page) < 100:
                return out
            offset += 100

    def card(self, card_id: int) -> dict:
        return self.get(f"/cards/{card_id}")

    # --- запись ---
    def create_card(self, *, board_id: int, title: str, column_id: int | None = None, lane_id: int | None = None,
                    description: str = "", due_date: str | None = None, responsible_id: int | None = None) -> dict:
        payload: dict = {"title": title, "board_id": board_id, "text_format_type_id": 1}
        if column_id:
            payload["column_id"] = column_id
        if lane_id:
            payload["lane_id"] = lane_id
        if description:
            payload["description"] = description
        if due_date:
            payload["due_date"] = kaiten_due(due_date)
        card = self.post("/cards", payload)
        if responsible_id:
            self.set_responsible(card["id"], responsible_id)
        return card

    def set_responsible(self, card_id: int, user_id: int) -> None:
        try:
            self.post(f"/cards/{card_id}/members", {"user_id": user_id})
        except KaitenError as e:
            if "400" not in str(e) and "409" not in str(e):  # уже участник — не страшно
                raise
        self.patch(f"/cards/{card_id}/members/{user_id}", {"type": 2})

    def comment(self, card_id: int, text: str) -> dict:
        return self.post(f"/cards/{card_id}/comments", {"text": text})

    def update_card(self, card_id: int, fields: dict) -> dict:
        payload = dict(fields)
        if payload.get("due_date"):
            payload["due_date"] = kaiten_due(payload["due_date"])
        return self.patch(f"/cards/{card_id}", payload)

    def card_url(self, card: dict, space_id: int | None = None) -> str:
        space_id = space_id or _space_of(card)
        if space_id:
            return f"https://{self.domain}/space/{space_id}/card/{card['id']}"
        return f"https://{self.domain}/{card['id']}"


def kaiten_due(value: str) -> str:
    """'2026-10-30' → ISO с полуднем UTC, чтобы дата не «съехала» из-за часового пояса."""
    if len(value) == 10:
        return f"{value}T12:00:00.000Z"
    return value


def _space_of(card: dict) -> int | None:
    for sp in (card.get("board") or {}).get("spaces") or []:
        if sp.get("primary_path") or len((card.get("board") or {}).get("spaces")) == 1:
            return sp.get("space_id") or sp.get("id")
    return card.get("space_id")


def client(settings: dict | None = None, transport: httpx.BaseTransport | None = None) -> Kaiten:
    settings = settings or config.load_settings()
    return Kaiten(settings["kaiten"]["domain"], config.get_secret("KAITEN_TOKEN"), transport=transport)


# --- синхронизация ---------------------------------------------------------

def _member_list(card: dict) -> list[dict]:
    out = []
    for m in card.get("members") or []:
        out.append({
            "id": m.get("id") or m.get("user_id"),
            "name": m.get("full_name") or m.get("username"),
            "username": m.get("username"),
            "type": m.get("type"),
        })
    return out


def normalize_card(card: dict, kc: Kaiten, space_id: int | None, space_title: str | None) -> dict:
    members = _member_list(card)
    responsible = next((m for m in members if m.get("type") == 2), None)
    owner = card.get("owner") or {}
    return {
        "id": card["id"],
        "space_id": space_id,
        "space_title": space_title,
        "board_id": card.get("board_id"),
        "board_title": (card.get("board") or {}).get("title"),
        "column_id": card.get("column_id"),
        "column_title": (card.get("column") or {}).get("title"),
        "lane_id": card.get("lane_id"),
        "lane_title": (card.get("lane") or {}).get("title"),
        "title": card.get("title"),
        "description": (card.get("description") or "")[:3000],
        "state": card.get("state"),
        "condition": card.get("condition"),
        "due_date": (card.get("due_date") or "")[:10] or None,
        "responsible_id": (responsible or {}).get("id") or card.get("responsible_id"),
        "responsible_name": (responsible or {}).get("name"),
        "owner_id": card.get("owner_id"),
        "owner_name": owner.get("full_name"),
        "members": store.json_col(members),
        "created": card.get("created"),
        "updated": card.get("updated"),
        "last_moved_at": card.get("last_moved_at"),
        "column_changed_at": card.get("column_changed_at"),
        "url": kc.card_url(card, space_id),
        "synced_at": store.utcnow(),
    }


CARD_COLUMNS = [
    "id", "space_id", "space_title", "board_id", "board_title", "column_id", "column_title", "lane_id",
    "lane_title", "title", "description", "state", "condition", "due_date", "responsible_id", "responsible_name",
    "owner_id", "owner_name", "members", "created", "updated", "last_moved_at", "column_changed_at", "url",
    "synced_at",
]


def save_card(card: dict) -> None:
    cols = ",".join(CARD_COLUMNS)
    marks = ",".join("?" for _ in CARD_COLUMNS)
    updates = ",".join(f"{c}=excluded.{c}" for c in CARD_COLUMNS if c != "id")
    store.execute(
        f"INSERT INTO kaiten_cards({cols}) VALUES({marks}) ON CONFLICT(id) DO UPDATE SET {updates}",
        [card.get(c) for c in CARD_COLUMNS],
    )


def collect(settings: dict | None = None, transport: httpx.BaseTransport | None = None) -> str:
    settings = settings or config.load_settings()
    kc = client(settings, transport)
    me = kc.current_user()
    store.set_state("kaiten_me", {"id": me.get("id"), "name": me.get("full_name"), "username": me.get("username")})

    users = kc.users()
    with store.db() as conn:
        for u in users:
            conn.execute(
                "INSERT INTO kaiten_users(id, full_name, username, email) VALUES(?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET full_name=excluded.full_name, username=excluded.username, email=excluded.email",
                (u.get("id"), u.get("full_name"), u.get("username"), u.get("email")),
            )

    tracked = [s for s in settings["kaiten"].get("spaces") or [] if s.get("track", True)]
    total = 0
    for sp in tracked:
        space_id = int(sp["id"])
        for b in kc.boards(space_id):
            store.execute(
                "INSERT INTO kaiten_boards(id, space_id, title, columns, lanes) VALUES(?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET space_id=excluded.space_id, title=excluded.title, "
                "columns=excluded.columns, lanes=excluded.lanes",
                (
                    b["id"], space_id, b.get("title"),
                    store.json_col([
                        {"id": c["id"], "title": c.get("title"), "type": c.get("type"), "sort_order": c.get("sort_order")}
                        for c in b.get("columns") or []
                    ]),
                    store.json_col([
                        {"id": ln["id"], "title": ln.get("title"), "sort_order": ln.get("sort_order")}
                        for ln in b.get("lanes") or [] if ln.get("condition", 1) == 1
                    ]),
                ),
            )
        seen = []
        for card in kc.cards(space_id):
            save_card(normalize_card(card, kc, space_id, sp.get("title")))
            seen.append(card["id"])
        total += len(seen)
        # карточки, которых больше нет на досках пространства (архив/перенос), убираем из зеркала
        if seen:
            marks = ",".join("?" for _ in seen)
            store.execute(f"DELETE FROM kaiten_cards WHERE space_id=? AND id NOT IN ({marks})", [space_id, *seen])
        else:
            store.execute("DELETE FROM kaiten_cards WHERE space_id=?", (space_id,))
    tracked_ids = [int(s["id"]) for s in tracked]
    if tracked_ids:
        marks = ",".join("?" for _ in tracked_ids)
        store.execute(f"DELETE FROM kaiten_cards WHERE space_id NOT IN ({marks})", tracked_ids)
    else:
        store.execute("DELETE FROM kaiten_cards")
    return f"карточек: {total} в {len(tracked)} пространствах"


def check(settings: dict | None = None, transport: httpx.BaseTransport | None = None) -> str:
    kc = client(settings, transport)
    me = kc.current_user()
    spaces = kc.spaces()
    return f"вход как {me.get('full_name')}; доступно пространств: {len(spaces)}"


# --- «без движения» --------------------------------------------------------

def idle_since(card: dict, basis: str) -> datetime | None:
    if basis == "updated":
        keys = ("updated", "created")
    else:
        keys = ("last_moved_at", "column_changed_at", "created")
    for k in keys:
        dt = store.parse_ts(card.get(k))
        if dt:
            return dt
    return None


def stale_cards(settings: dict | None = None, include_snoozed: bool = False) -> list[dict]:
    settings = settings or config.load_settings()
    ks = settings["kaiten"]
    days = int(ks.get("stale_days") or 3)
    states = [int(s) for s in ks.get("stale_states") or [2]]
    excluded = {int(b) for b in ks.get("exclude_boards") or []}
    now = datetime.now(timezone.utc)
    snoozed = {
        r["card_id"]
        for r in store.rows("SELECT card_id FROM kaiten_snooze WHERE until > ?", (store.utcnow(),))
    }
    out = []
    marks = ",".join("?" for _ in states)
    for card in store.rows(f"SELECT * FROM kaiten_cards WHERE condition=1 AND state IN ({marks})", states):
        if card["board_id"] in excluded:
            continue
        if not include_snoozed and card["id"] in snoozed:
            continue
        since = idle_since(card, ks.get("stale_basis", "moved"))
        if not since:
            continue
        idle = (now - since).days
        if idle >= days:
            card["idle_days"] = idle
            card["snoozed"] = card["id"] in snoozed
            out.append(card)
    out.sort(key=lambda c: -c["idle_days"])
    return out


# --- страница «Задачи Kaiten» ----------------------------------------------------

STATE_TITLES = {1: "в очереди", 2: "в работе", 3: "готово"}


def _initials(name: str | None) -> str:
    parts = [p for p in re.split(r"\s+", (name or "").strip()) if p]
    return "".join(p[0] for p in parts[:2]).upper() or "?"


def decorate(cards: list[dict], settings: dict | None = None) -> list[dict]:
    """Признаки для отображения карточки: простой, срок, «моя», инициалы исполнителя."""
    settings = settings or config.load_settings()
    stale = {c["id"]: c for c in stale_cards(settings, include_snoozed=True)}
    me = (store.get_state("kaiten_me") or {}).get("id")
    today = datetime.now(config.tz(settings)).date().isoformat()
    now = datetime.now(timezone.utc)
    for c in cards:
        s = stale.get(c["id"])
        c["idle_days"] = s["idle_days"] if s else None
        c["snoozed"] = bool(s and s.get("snoozed"))
        c["overdue"] = bool(c["due_date"] and c["due_date"][:10] < today)
        moved = store.parse_ts(c.get("column_changed_at") or c.get("last_moved_at") or c.get("created"))
        c["in_column_days"] = (now - moved).days if moved else None
        c["mine"] = bool(me and me in (c["responsible_id"], c["owner_id"] if not c["responsible_id"] else None))
        c["person"] = c["responsible_name"] or ""
        c["initials"] = _initials(c["responsible_name"] or c["owner_name"])
        c["state_title"] = STATE_TITLES.get(c["state"], "")
    return cards


def boards_view(cards: list[dict]) -> list[dict]:
    """Карточки по доскам, а внутри — по колонкам в том порядке, что в Kaiten."""
    boards: dict[int, dict] = {}
    known = {b["id"]: b for b in store.rows("SELECT * FROM kaiten_boards")}
    for c in cards:
        b = boards.setdefault(c["board_id"], {"id": c["board_id"], "title": c["board_title"] or "Без доски",
                                              "space_id": c["space_id"], "space_title": c["space_title"] or "",
                                              "columns": {}, "count": 0})
        col = b["columns"].setdefault(c["column_id"], {"id": c["column_id"], "title": c["column_title"] or "—", "cards": []})
        col["cards"].append(c)
        b["count"] += 1
    out = []
    for b in boards.values():
        order = {col["id"]: (col.get("sort_order") or 0, i)
                 for i, col in enumerate(store.loads((known.get(b["id"]) or {}).get("columns"), []))}
        b["columns"] = sorted(b["columns"].values(), key=lambda col: order.get(col["id"], (10**9, col["title"])))
        b["stale"] = sum(1 for col in b["columns"] for c in col["cards"] if c["idle_days"] is not None)
        out.append(b)
    return sorted(out, key=lambda b: (b["space_title"], -b["count"], b["title"]))


def snooze(card_id: int, days: int) -> None:
    until = (datetime.now(timezone.utc) + timedelta(days=days)).replace(microsecond=0).isoformat()
    store.execute(
        "INSERT INTO kaiten_snooze(card_id, until) VALUES(?,?) ON CONFLICT(card_id) DO UPDATE SET until=excluded.until",
        (card_id, until),
    )


def mark_pinged(card_ids: list[int]) -> None:
    for cid in card_ids:
        store.execute(
            "INSERT INTO kaiten_snooze(card_id, last_pinged) VALUES(?,?) "
            "ON CONFLICT(card_id) DO UPDATE SET last_pinged=excluded.last_pinged",
            (cid, store.utcnow()),
        )


def resolve_user(ref: str | int | None) -> dict | None:
    """Найти пользователя Kaiten по id, @username, имени или записи справочника людей."""
    if ref in (None, ""):
        return None
    ref_s = str(ref).strip().lstrip("@")
    if ref_s.isdigit():
        return store.row("SELECT * FROM kaiten_users WHERE id=?", (int(ref_s),)) or {"id": int(ref_s), "full_name": None}
    u = store.row("SELECT * FROM kaiten_users WHERE lower(username)=lower(?)", (ref_s,))
    if u:
        return u
    from opsdash import records  # локальный импорт: records зависит от store

    person = records.find_person(ref_s)
    if person and person.get("kaiten_user_id"):
        return store.row("SELECT * FROM kaiten_users WHERE id=?", (person["kaiten_user_id"],)) or {
            "id": person["kaiten_user_id"], "full_name": person["name"],
        }
    candidates = store.rows("SELECT * FROM kaiten_users WHERE full_name LIKE ?", (f"%{ref_s}%",))
    if len(candidates) == 1:
        return candidates[0]
    # «Пётр Иванов» против «Иванов Пётр»
    parts = [p for p in re.split(r"\s+", ref_s) if len(p) > 1]
    if parts:
        users = store.rows("SELECT * FROM kaiten_users")
        hits = [u for u in users if all(p.lower() in (u.get("full_name") or "").lower() for p in parts)]
        if len(hits) == 1:
            return hits[0]
    return None


def targets(settings: dict | None = None) -> list[dict]:
    settings = settings or config.load_settings()
    return settings["kaiten"].get("targets") or []


def find_target(name: str | None, settings: dict | None = None) -> dict | None:
    settings = settings or config.load_settings()
    ts = targets(settings)
    if not ts:
        return None
    name = name or settings["kaiten"].get("default_target") or ""
    for t in ts:
        if t.get("name", "").lower() == name.lower():
            return t
    if not name:
        return ts[0]
    return None
