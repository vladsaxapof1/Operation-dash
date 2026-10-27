"""Пачка (pachca.com): чтение переписки директора по её персональному токену.

Персональный токен видит то же, что сам человек в интерфейсе Пачки: его чаты, треды и
сообщения. PULSE берёт только отмеченные в админке чаты (и треды в них) и ничего не пишет.
Токен выпускают в Пачке: Интеграции → API; по умолчанию это могут только администраторы.
Документация: https://dev.pachca.com
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timedelta, timezone

import httpx

from opsdash import config, store

SOURCE = "pachca"
API = "https://api.pachca.com/api/shared/v1"
APP = "https://app.pachca.com"
SCOPES = ("chats:read", "messages:read", "threads:read", "users:read")  # всё, что нужно PULSE, — только чтение
BACKFILL_DAYS = 3  # при первом чтении чата — сообщения за последние дни
PAGE = 50  # больше Пачка за раз не отдаёт
MAX_PAGES = 10  # не больше 500 новых сообщений чата за проход (накопилось больше — старшие пропускаем)
THREADS_OVERLAP = timedelta(minutes=10)  # треды берём с запасом: часы компьютера и сервера могут расходиться


class PachcaError(Exception):
    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


def _http(transport: httpx.BaseTransport | None = None) -> httpx.Client:
    token = config.get_secret("PACHCA_TOKEN")
    if not token:
        raise PachcaError("Не задан токен Пачки")
    return httpx.Client(base_url=API, headers={"Authorization": f"Bearer {token}"}, timeout=30, transport=transport)


def _error(resp: httpx.Response, path: str) -> PachcaError:
    try:
        data = resp.json()
    except ValueError:
        data = {}
    errors = data.get("errors") if isinstance(data, dict) else None
    detail = (errors[0].get("message") if errors else None) or data.get("error_description") or resp.text[:200]
    if resp.status_code == 401:
        return PachcaError("Пачка не приняла токен: он неверный, отозван или истёк. "
                           "Создайте новый в Пачке: Интеграции → API", 401)
    if resp.status_code == 403 and data.get("error") == "insufficient_scope":
        scope = re.search(r'scope="([^"]+)"', resp.headers.get("WWW-Authenticate", ""))
        need = scope.group(1) if scope else "нужного"
        return PachcaError(f"у токена нет права {need} — добавьте его в Пачке: Интеграции → API", 403)
    if resp.status_code == 402:
        return PachcaError("пространство Пачки приостановлено: израсходован лимит тарифа", 402)
    return PachcaError(f"{path}: {resp.status_code} {detail}", resp.status_code)


def _get(http: httpx.Client, path: str, params: dict | None = None) -> dict:
    for attempt in range(4):
        resp = http.get(path, params=params)
        if resp.status_code != 429 or attempt == 3:
            break
        # лимит запросов: Пачка говорит, сколько подождать
        try:
            pause = float(resp.headers.get("Retry-After") or 1)
        except ValueError:
            pause = 1.0
        time.sleep(min(max(pause, 0.5), 30))
    if resp.status_code >= 400:
        raise _error(resp, path)
    return resp.json() if resp.content else {}


def _pages(http: httpx.Client, path: str, params: dict, max_pages: int):
    """Элементы списка по страницам. Конец — has_next: false (пустая страница концом не считается)."""
    cursor = None
    for _ in range(max_pages):
        data = _get(http, path, {**params, "limit": PAGE, **({"cursor": cursor} if cursor else {})})
        yield from data.get("data") or []
        paginate = (data.get("meta") or {}).get("paginate") or {}
        cursor = paginate.get("next_page")
        if not paginate.get("has_next") or not cursor:
            return


def _ts(value: str | None) -> datetime | None:
    return store.parse_ts(value)


def _user_name(u: dict) -> str:
    return " ".join(x for x in (u.get("first_name"), u.get("last_name")) if x).strip() or u.get("nickname") or ""


def _users(http: httpx.Client, ids) -> dict[int, dict]:
    """Имена сотрудников {id: {name, bot}}. Знакомые — из памяти; незнакомых много — весь список разом."""
    cache = {int(k): v for k, v in (store.get_state("pachca_users") or {}).items()}
    unknown = {int(i) for i in ids if i} - set(cache)
    if not unknown:
        return cache
    try:
        if len(unknown) > 3:
            for u in _pages(http, "/users", {}, 200):
                cache[int(u["id"])] = {"name": _user_name(u), "bot": bool(u.get("bot"))}
        for uid in unknown - set(cache):
            try:
                u = _get(http, f"/users/{uid}").get("data") or {}
                cache[uid] = {"name": _user_name(u), "bot": bool(u.get("bot"))}
            except PachcaError as e:
                if e.status != 404:
                    raise
                cache[uid] = {"name": "", "bot": False}  # удалённый сотрудник — больше не спрашиваем
    except PachcaError as e:
        if e.status != 403:  # без права users:read работаем без имён
            raise
    store.set_state("pachca_users", {str(k): v for k, v in cache.items()})
    return cache


def _me(http: httpx.Client) -> dict:
    """Владелец токена и его права (метод не требует прав)."""
    info = _get(http, "/oauth/token/info").get("data") or {}
    uid = int(info.get("user_id") or 0)
    me = {"id": uid, "name": (_users(http, [uid]).get(uid) or {}).get("name", ""), "scopes": info.get("scopes") or []}
    store.set_state("pachca_me", {"id": me["id"], "name": me["name"]})
    return me


def _file_label(f: dict, m: dict) -> str:
    kind, name = f.get("file_type"), f.get("name") or ""
    if kind == "voice":
        transcript = ((m.get("voice_content") or {}).get("transcript") or "").strip()
        return f"[голосовое сообщение] {transcript}".strip()
    if kind == "image":
        return "[изображение]"
    if kind == "video":
        return "[видео]"
    return f"[файл: {name}]" if name else "[файл]"


def parse_message(m: dict, chat_id: str, chat_title: str, me_id: int, users: dict[int, dict],
                  thread_of: int | None = None) -> dict | None:
    """Сообщение Пачки → строка messages. thread_of — сообщение, под которым тред (для ответов в треде)."""
    text = (m.get("content") or "").strip()
    labels = [_file_label(f, m) for f in m.get("files") or []]
    if m.get("forwarding"):
        labels.insert(0, "[переслано]")
    text = " ".join([*labels, text]).strip()
    if not text:
        return None
    uid = int(m.get("user_id") or 0)
    outgoing = bool(me_id) and uid == me_id
    reply = m.get("parent_message_id") or thread_of
    return {
        "source": SOURCE,
        "chat_id": chat_id,
        "chat_title": chat_title,
        "msg_id": str(m["id"]),
        "sender_id": str(uid or ""),
        "sender_name": "Я" if outgoing else (m.get("display_name") or (users.get(uid) or {}).get("name") or ""),
        "is_outgoing": outgoing,
        "text": text[:8000],
        "ts": store.to_utc_iso(_ts(m.get("created_at")) or datetime.now(timezone.utc)),
        "reply_to": str(reply) if reply else None,
        "link": m.get("url") or f"{APP}/chats/{m.get('chat_id') or chat_id}?message={m['id']}",
    }


def _refresh_chats(http: httpx.Client, me_id: int) -> list[dict]:
    chats = list(_pages(http, "/chats", {}, 100))
    others = {u for c in chats if c.get("personal") for u in c.get("member_ids") or [] if u != me_id}
    users = _users(http, others)
    out = []
    for c in chats:
        kind = "channel" if c.get("channel") else "private" if c.get("personal") else "group"
        title = (c.get("name") or "").strip()
        if c.get("personal"):
            other = [u for u in c.get("member_ids") or [] if u != me_id]
            if not other:
                continue  # чат с самим собой — заметки
            person = users.get(other[0]) or {}
            title = person.get("name") or title
            if person.get("bot"):
                kind = "bot"
        last = _ts(c.get("last_message_at"))
        store.upsert_chat(SOURCE, str(c["id"]), title or f"Чат {c['id']}", kind,
                          last_ts=store.to_utc_iso(last) if last else None)
        out.append(c)
    return out


def _read_chat(http: httpx.Client, api_chat_id: int, chat_id: str, title: str, me_id: int, since: datetime,
               thread_of: int | None = None, last_at: str | None = None) -> int:
    """Новые сообщения чата или треда. Пачка отдаёт их от новых к старым: листаем, пока не дойдём
    до прочитанного (чекпоинт — id последнего сообщения) или, при первом чтении, до since."""
    key = f"pachca_last:{api_chat_id}"
    seen = store.get_state(key) or {}
    last_id = int(seen.get("id") or 0)
    if last_at and seen.get("at") == last_at:
        return 0  # с прошлого раза в чате ничего не писали
    batch = []
    for m in _pages(http, "/messages", {"chat_id": api_chat_id, "sort": "id", "order": "desc"}, MAX_PAGES):
        if int(m["id"]) <= last_id:
            break
        if not last_id and (_ts(m.get("created_at")) or since) < since:
            break
        batch.append(m)
    users = _users(http, {m.get("user_id") for m in batch if not m.get("display_name")})
    new = 0
    for m in reversed(batch):
        msg = parse_message(m, chat_id, title, me_id, users, thread_of)
        if msg:
            new += int(store.add_message(msg))
    store.set_state(key, {"id": max([last_id, *(int(m["id"]) for m in batch)]), "at": last_at or seen.get("at")})
    return new


def _read_threads(http: httpx.Client, enabled: dict[str, dict], me_id: int, since: datetime) -> int:
    """Треды под сообщениями отмеченных чатов. Их сообщения кладём в сам чат — ответом на сообщение,
    под которым тред."""
    started = datetime.now(timezone.utc)
    after = store.get_state("pachca_threads_since") or since.replace(microsecond=0).isoformat()
    new = 0
    for t in _pages(http, "/threads", {"last_message_at_after": after}, 20):
        parent = str(t.get("message_chat_id") or "")
        if parent not in enabled:
            continue
        new += _read_chat(http, t["chat_id"], parent, enabled[parent]["title"], me_id, since,
                          thread_of=t.get("message_id"), last_at=t.get("updated_at"))
    store.set_state("pachca_threads_since", (started - THREADS_OVERLAP).replace(microsecond=0).isoformat())
    return new


def collect(settings: dict | None = None, transport: httpx.BaseTransport | None = None) -> str:
    since = datetime.now(timezone.utc) - timedelta(days=BACKFILL_DAYS)
    with _http(transport) as http:
        me = _me(http)
        chats = _refresh_chats(http, me["id"])
        enabled = {r["chat_id"]: r for r in store.rows("SELECT chat_id, title FROM chats WHERE source=? AND enabled=1",
                                                         (SOURCE,))}
        new_total, read = 0, 0
        for c in chats:
            chat_id = str(c["id"])
            if chat_id not in enabled:
                continue
            read += 1
            new_total += _read_chat(http, c["id"], chat_id, enabled[chat_id]["title"], me["id"], since,
                                    last_at=c.get("last_message_at"))
        note = ""
        try:
            new_total += _read_threads(http, enabled, me["id"], since)
        except PachcaError as e:
            if e.status != 403:
                raise
            note = "; треды не читаются — у токена нет права threads:read"
    return f"чатов прочитано: {read}, новых сообщений: {new_total}{note}"


def refresh_chats(settings: dict | None = None, transport: httpx.BaseTransport | None = None) -> int:
    """Обновить список чатов для выбора в админке."""
    with _http(transport) as http:
        return len(_refresh_chats(http, _me(http)["id"]))


def _writes(scopes: list[str]) -> list[str]:
    return [s for s in scopes if not s.endswith(":read") and not s.startswith("search:")]


def check(settings: dict | None = None, transport: httpx.BaseTransport | None = None) -> str:
    with _http(transport) as http:
        me = _me(http)
    who = f"вход как {me['name']}" if me["name"] else f"токен принят (сотрудник #{me['id']})"
    missing = [s for s in SCOPES if s not in me["scopes"]]
    if missing:
        raise PachcaError(f"{who}, но не хватает прав: {', '.join(missing)}. Добавьте их токену в Пачке: "
                          "Интеграции → API → меню токена → «Изменить»")
    extra = _writes(me["scopes"])
    if extra:
        who += f"; лишние права: {', '.join(extra)} — PULSE только читает, их можно снять"
    return who
