"""Slack: чтение переписки директора по её пользовательскому токену (беклог MORE-6).

Директор создаёт в своём рабочем пространстве приложение по готовому манифесту (MANIFEST: api.slack.com/apps →
Create New App → From a manifest), ставит его себе и копирует User OAuth Token (`xoxp-…`) — он лежит в secrets
(SLACK_TOKEN). Токен видит то же, что сам человек: его личные сообщения, группы и каналы, где он состоит.

Такое приложение — «внутреннее» (internal customer-built app): ограничения Slack 2025 года на conversations.history
и conversations.replies (1 запрос в минуту для приложений не из Marketplace) на него не распространяются.

Что читаем: личные сообщения и личные группы — все (новые включаются сами, отключить можно в настройках), каналы —
только отмеченные, и упоминания директора в остальных каналах (если у токена есть право search:read). Ничего не
пишем: ни одного метода, который меняет что-то в Slack, здесь нет.
Документация: https://docs.slack.dev/reference/methods
"""

from __future__ import annotations

import html
import json
import re
import time
from datetime import datetime, timezone

import httpx

from opsdash import config, store

SOURCE = "slack"
API = "https://slack.com/api"
REQUIRED_SCOPES = ("channels:history", "channels:read", "groups:history", "groups:read", "im:history", "im:read",
                   "mpim:history", "mpim:read", "users:read")
OPTIONAL_SCOPES = ("search:read",)  # упоминания в каналах, которые PULSE не читает целиком
BACKFILL_DAYS = 5  # при первом чтении чата — сообщения за последние дни
THREAD_DAYS = 3  # историю перечитываем с запасом: так видно новые ответы в тредах под недавними сообщениями
THREAD_KEEP_DAYS = 14  # сколько помним прочитанные треды
PAGE = 200
MAX_PAGES = 5  # не больше 1000 сообщений чата за проход
KEEP_SUBTYPES = {None, "thread_broadcast", "file_share", "me_message", "bot_message"}  # остальное — служебное
TOKEN_ERRORS = {"invalid_auth", "not_authed", "token_revoked", "token_expired", "account_inactive"}

MANIFEST = {
    "display_information": {
        "name": "PULSE",
        "description": "PULSE — personal assistant: reads your Slack messages, never posts anything",  # видно в Slack коллегам и админу
    },
    "oauth_config": {"scopes": {"user": [*REQUIRED_SCOPES, *OPTIONAL_SCOPES]}},
    "settings": {"org_deploy_enabled": False, "socket_mode_enabled": False, "token_rotation_enabled": False},
}


def manifest_json() -> str:
    """Манифест приложения — для окна подключения: вставить в api.slack.com/apps → From a manifest."""
    return json.dumps(MANIFEST, ensure_ascii=False, indent=2)


class SlackError(Exception):
    def __init__(self, message: str, code: str = ""):
        super().__init__(message)
        self.code = code


def _http(transport: httpx.BaseTransport | None = None) -> httpx.Client:
    token = config.get_secret("SLACK_TOKEN")
    if not token:
        raise SlackError("Не задан токен Slack")
    return httpx.Client(base_url=API, headers={"Authorization": f"Bearer {token}"}, timeout=30, transport=transport)


def _error(method: str, data: dict) -> SlackError:
    code = str(data.get("error") or "unknown_error")
    if code in TOKEN_ERRORS:
        return SlackError("Slack не принял токен: он неверный или отозван. Скопируйте User OAuth Token заново: "
                          "api.slack.com/apps → ваше приложение → OAuth & Permissions", code)
    if code == "missing_scope":
        need = data.get("needed") or "нужного"
        return SlackError(f"у токена нет права {need} — добавьте его в приложении и переустановите", code)
    return SlackError(f"{method}: {code}", code)


def _call(http: httpx.Client, method: str, params: dict | None = None) -> tuple[dict, httpx.Response]:
    """Метод Web API. Slack отвечает 200 и {"ok": false, "error": …} — это тоже ошибка; 429 — ждём, сколько скажет."""
    for attempt in range(4):
        resp = http.get(f"/{method}", params=params)
        if resp.status_code != 429 or attempt == 3:
            break
        try:
            pause = float(resp.headers.get("Retry-After") or 1)
        except ValueError:
            pause = 1.0
        time.sleep(min(max(pause, 0.5), 30))
    if resp.status_code >= 400:
        raise SlackError(f"{method}: HTTP {resp.status_code}", str(resp.status_code))
    try:
        data = resp.json()
    except ValueError:
        raise SlackError(f"{method}: непонятный ответ Slack") from None
    if not data.get("ok"):
        raise _error(method, data)
    return data, resp


def _get(http: httpx.Client, method: str, params: dict | None = None) -> dict:
    return _call(http, method, params)[0]


def _pages(http: httpx.Client, method: str, params: dict, key: str, max_pages: int):
    """Элементы списка по страницам: курсор — response_metadata.next_cursor, пустой — конец."""
    cursor = None
    for _ in range(max_pages):
        data = _get(http, method, {**params, **({"cursor": cursor} if cursor else {})})
        yield from data.get(key) or []
        cursor = (data.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            return


# --- люди ---------------------------------------------------------------------

def _user_name(u: dict) -> str:
    profile = u.get("profile") or {}
    return (profile.get("real_name") or u.get("real_name") or profile.get("display_name") or u.get("name") or "").strip()


def _user_entry(u: dict) -> dict:
    return {"name": _user_name(u), "bot": bool(u.get("is_bot")) or u.get("id") == "USLACKBOT"}


def _users(http: httpx.Client, ids) -> dict[str, dict]:
    """Имена людей {id: {name, bot}}. Знакомые — из памяти; незнакомых много — весь список разом."""
    cache = dict(store.get_state("slack_users") or {})
    unknown = {str(i) for i in ids if i} - set(cache)
    if not unknown:
        return cache
    if len(unknown) > 3:
        for u in _pages(http, "users.list", {"limit": PAGE}, "members", 50):
            cache[u["id"]] = _user_entry(u)
    for uid in unknown - set(cache):
        try:
            cache[uid] = _user_entry(_get(http, "users.info", {"user": uid}).get("user") or {"id": uid})
        except SlackError as e:
            if e.code not in ("user_not_found", "bot_not_found"):
                raise
            cache[uid] = {"name": "", "bot": False}  # удалённый человек — больше не спрашиваем
    store.set_state("slack_users", cache)
    return cache


def _me(http: httpx.Client) -> dict:
    """Владелец токена, рабочее пространство и права токена (заголовок x-oauth-scopes)."""
    data, resp = _call(http, "auth.test")
    uid = data.get("user_id") or ""
    scopes = [s.strip() for s in (resp.headers.get("x-oauth-scopes") or "").split(",") if s.strip()]
    name = (_users(http, [uid]).get(uid) or {}).get("name") or data.get("user") or ""
    me = {"id": uid, "name": name, "team": data.get("team") or "", "url": data.get("url") or "", "scopes": scopes}
    store.set_state("slack_me", {k: me[k] for k in ("id", "name", "team", "url")})
    return me


# --- текст сообщений ------------------------------------------------------------

_TOKEN = re.compile(r"<([^<>]+)>")
_MENTION_IDS = re.compile(r"<@([UW][A-Z0-9]+)")


def _text(raw: str, users: dict[str, dict]) -> str:
    """Разметка Slack (mrkdwn) → обычный текст: <@U1> → @Имя, <#C1|склад> → #склад, <url|текст> → текст (url)."""
    def token(m: re.Match) -> str:
        body = m.group(1)
        target, _, label = body.partition("|")
        if target.startswith("@"):
            uid = target[1:]
            return "@" + ((users.get(uid) or {}).get("name") or label or uid)
        if target.startswith("#"):
            return "#" + (label or target[1:])
        if target.startswith("!"):
            special = target[1:]
            if special in ("here", "channel", "everyone"):
                return "@" + special
            return label or "@" + special.split("^")[0]
        if target.startswith("mailto:"):
            return label or target[len("mailto:"):]
        return f"{label} ({target})" if label and label != target else target

    return html.unescape(_TOKEN.sub(token, raw or "")).strip()


def _file_label(f: dict) -> str:
    if (f.get("mimetype") or "").startswith("image/"):
        return "[изображение]"
    name = f.get("name") or f.get("title") or ""
    return f"[файл: {name}]" if name else "[файл]"


def _iso(ts: str) -> str:
    return store.to_utc_iso(datetime.fromtimestamp(float(ts), timezone.utc))


def message_link(team_url: str, channel_id: str, ts: str, thread_ts: str | None = None) -> str:
    base = (team_url or "https://slack.com/").rstrip("/") + "/"
    link = f"{base}archives/{channel_id}/p{ts.replace('.', '')}"
    return link + (f"?thread_ts={thread_ts}&cid={channel_id}" if thread_ts and thread_ts != ts else "")


def parse_message(m: dict, channel_id: str, title: str, me_id: str, users: dict[str, dict],
                  team_url: str) -> dict | None:
    """Сообщение Slack → строка messages. Ответ в треде — reply_to на сообщение, под которым тред."""
    if m.get("subtype") not in KEEP_SUBTYPES or not m.get("ts"):
        return None
    text = " ".join([*(_file_label(f) for f in m.get("files") or []), _text(m.get("text") or "", users)]).strip()
    if not text:
        return None
    uid = m.get("user") or ""
    outgoing = bool(me_id) and uid == me_id
    ts, thread_ts = str(m["ts"]), m.get("thread_ts")
    sender = (users.get(uid) or {}).get("name") or m.get("username") or (m.get("bot_profile") or {}).get("name") or ""
    return {
        "source": SOURCE,
        "chat_id": channel_id,
        "chat_title": title,
        "msg_id": ts,
        "sender_id": uid or (m.get("bot_id") or ""),
        "sender_name": "Я" if outgoing else sender,
        "is_outgoing": outgoing,
        "text": text[:8000],
        "ts": _iso(ts),
        "reply_to": str(thread_ts) if thread_ts and str(thread_ts) != ts else None,
        "link": m.get("permalink") or message_link(team_url, channel_id, ts, thread_ts),
    }


def _mentioned(messages: list[dict]) -> set[str]:
    ids = set()
    for m in messages:
        if m.get("user"):
            ids.add(m["user"])
        ids.update(_MENTION_IDS.findall(m.get("text") or ""))
    return ids


# --- чаты -----------------------------------------------------------------------

def _refresh_chats(http: httpx.Client, me: dict) -> list[dict]:
    """Список разговоров директора → таблица chats. Личные сообщения и личные группы при первом появлении
    включаются сами (их PULSE читает всегда, пока директор не выключит), каналы — по выбору."""
    convs = list(_pages(http, "users.conversations", {"types": "public_channel,private_channel,mpim,im",
                                                      "exclude_archived": "true", "limit": PAGE}, "channels", 50))
    members: dict[str, list[str]] = {}
    for c in convs:
        if c.get("is_mpim"):
            members[c["id"]] = [u for u in _pages(http, "conversations.members", {"channel": c["id"], "limit": PAGE},
                                                  "members", 1) if u != me["id"]]
    users = _users(http, {c.get("user") for c in convs if c.get("is_im")} | {u for ms in members.values() for u in ms})
    out, dms = [], []
    for c in convs:
        cid = c["id"]
        if c.get("is_im"):
            other = c.get("user") or ""
            if other == me["id"]:
                continue  # разговор с самим собой — заметки
            person = users.get(other) or {}
            title, kind = person.get("name") or other, "bot" if person.get("bot") or other == "USLACKBOT" else "private"
        elif c.get("is_mpim"):
            names = [(users.get(u) or {}).get("name") or u for u in members.get(cid, [])]
            title, kind = "Группа: " + ", ".join(names), "group"
        else:
            title, kind = "#" + (c.get("name") or cid), "group" if c.get("is_private") else "channel"
        dm = bool(c.get("is_im") or c.get("is_mpim"))
        new = not store.row("SELECT 1 FROM chats WHERE source=? AND chat_id=?", (SOURCE, cid))
        store.upsert_chat(SOURCE, cid, title, kind, enabled=1 if new and dm and kind != "bot" else None)
        if dm:
            dms.append(cid)
        out.append({"id": cid, "title": title, "kind": kind, "dm": dm})
    store.set_state("slack_dms", dms)
    return out


def _read_chat(http: httpx.Client, channel_id: str, title: str, me: dict) -> int:
    """Новые сообщения разговора и ответы в его тредах. Историю берём с запасом в THREAD_DAYS дней:
    повторы база отбрасывает (source, chat_id, msg_id), зато видны новые ответы под недавними сообщениями."""
    now = time.time()
    key = f"slack_last:{channel_id}"
    last = store.get_state(key)
    oldest = min(float(last), now - THREAD_DAYS * 86400) if last else now - BACKFILL_DAYS * 86400
    batch = list(_pages(http, "conversations.history", {"channel": channel_id, "oldest": f"{oldest:.6f}",
                                                        "limit": PAGE}, "messages", MAX_PAGES))
    threads_key = f"slack_threads:{channel_id}"
    threads = {ts: seen for ts, seen in (store.get_state(threads_key) or {}).items()
               if float(ts) >= now - THREAD_KEEP_DAYS * 86400}
    replies: list[dict] = []
    for m in batch:
        ts, latest = str(m.get("ts") or ""), m.get("latest_reply")
        if not m.get("reply_count") or not latest or str(m.get("thread_ts") or ts) != ts:
            continue
        seen = threads.get(ts)
        if seen and float(latest) <= float(seen):
            continue
        for r in _pages(http, "conversations.replies", {"channel": channel_id, "ts": ts, "oldest": seen or ts,
                                                        "limit": PAGE}, "messages", MAX_PAGES):
            if str(r.get("ts")) != ts:
                replies.append(r)
        threads[ts] = str(latest)
    users = _users(http, _mentioned(batch + replies))
    new = 0
    for m in sorted(batch + replies, key=lambda x: float(x.get("ts") or 0)):
        msg = parse_message(m, channel_id, title, me["id"], users, me["url"])
        if msg:
            new += int(store.add_message(msg))
    newest = max([float(last or 0), *(float(m["ts"]) for m in batch if m.get("ts"))])
    if newest:
        store.set_state(key, f"{newest:.6f}")
    store.set_state(threads_key, threads)
    return new


def _read_mentions(http: httpx.Client, me: dict, read_ids: set[str]) -> int:
    """Упоминания директора в каналах, которые PULSE целиком не читает (право search:read). Сообщение кладём
    в его канал — канал появляется в списке, но читать его целиком PULSE не начинает. Каналы «никогда» — пропускаем."""
    now = time.time()
    since = float(store.get_state("slack_mentions_since") or now - BACKFILL_DAYS * 86400)
    data = _get(http, "search.messages", {"query": f"<@{me['id']}>", "sort": "timestamp", "sort_dir": "desc",
                                          "count": 50})
    matches = [m for m in (data.get("messages") or {}).get("matches") or []
               if m.get("ts") and float(m["ts"]) > since]
    never = {r["chat_id"] for r in store.rows("SELECT chat_id FROM chats WHERE source=? AND enabled=-1", (SOURCE,))}
    users = _users(http, _mentioned(matches))
    new = 0
    for m in sorted(matches, key=lambda x: float(x["ts"])):
        ch = m.get("channel") or {}
        cid = ch.get("id") or ""
        if not cid or cid in read_ids or cid in never or ch.get("is_im") or ch.get("is_mpim"):
            continue
        title = "#" + (ch.get("name") or cid)
        store.upsert_chat(SOURCE, cid, title, "group" if ch.get("is_private") else "channel")
        msg = parse_message(m, cid, title, me["id"], users, me["url"])
        if msg:
            new += int(store.add_message(msg))
    newest = max([since, *(float(m["ts"]) for m in matches)])
    store.set_state("slack_mentions_since", f"{newest:.6f}")
    return new


def collect(settings: dict | None = None, transport: httpx.BaseTransport | None = None) -> str:
    with _http(transport) as http:
        me = _me(http)
        convs = _refresh_chats(http, me)
        enabled = {r["chat_id"] for r in store.rows("SELECT chat_id FROM chats WHERE source=? AND enabled=1",
                                                    (SOURCE,))}
        channels = personal = new = 0
        read_ids = set()
        for c in convs:
            if c["id"] not in enabled:
                continue
            read_ids.add(c["id"])
            new += _read_chat(http, c["id"], c["title"], me)
            if c["dm"]:
                personal += 1
            else:
                channels += 1
        line = f"каналов прочитано: {channels}, личных: {personal}, новых сообщений: {new}"
        if "search:read" in me["scopes"]:
            line += f", упоминаний: {_read_mentions(http, me, read_ids)}"
    return line


def refresh_chats(settings: dict | None = None, transport: httpx.BaseTransport | None = None) -> int:
    """Обновить список разговоров для выбора в настройках."""
    with _http(transport) as http:
        return len(_refresh_chats(http, _me(http)))


def check(settings: dict | None = None, transport: httpx.BaseTransport | None = None) -> str:
    with _http(transport) as http:
        me = _me(http)
    who = f"вход как {me['name'] or me['id']}" + (f" · рабочее пространство {me['team']}" if me["team"] else "")
    if me["scopes"]:
        missing = [s for s in REQUIRED_SCOPES if s not in me["scopes"]]
        if missing:
            raise SlackError(f"{who}, но не хватает прав: {', '.join(missing)}. Добавьте их приложению "
                             "(OAuth & Permissions → User Token Scopes) и переустановите его", "missing_scope")
        if "search:read" not in me["scopes"]:
            who += "; упоминания в других каналах не видны — нет права search:read"
    return who
