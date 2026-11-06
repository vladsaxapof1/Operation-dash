"""Slack (MORE-6): токен и права, список разговоров, сбор личных и отмеченных каналов, треды, упоминания."""

import json
import time

import httpx
import pytest

from opsdash import config, store
from opsdash.sources import slack

ME, PETR, OLGA, BOT = "U001", "U002", "U003", "B004"
TEAM_URL = "https://acme.slack.com/"
SCOPES = ",".join(slack.REQUIRED_SCOPES + slack.OPTIONAL_SCOPES)


def ts(minutes_ago: float) -> str:
    return f"{time.time() - minutes_ago * 60:.6f}"


class FakeSlack:
    """Slack в памяти: курсорная пагинация (response_metadata.next_cursor), oldest — «строго новее»."""

    def __init__(self, scopes=SCOPES):
        self.scopes = scopes
        self.users = {ME: ("Анна Смирнова", False), PETR: ("Пётр Иванов", False), OLGA: ("Ольга Петрова", False),
                      BOT: ("Робот", True), "USLACKBOT": ("Slackbot", True)}
        self.convs = [
            {"id": "D1", "is_im": True, "user": PETR},
            {"id": "D2", "is_im": True, "user": ME},  # заметки себе
            {"id": "D3", "is_im": True, "user": "USLACKBOT"},
            {"id": "G1", "is_mpim": True, "name": "mpdm-anna--petr--olga-1"},
            {"id": "C1", "name": "sklad", "is_channel": True},
            {"id": "C2", "name": "news", "is_channel": True},
            {"id": "C3", "name": "board", "is_group": True, "is_private": True},
        ]
        self.history: dict[str, list[dict]] = {c["id"]: [] for c in self.convs}
        self.replies: dict[tuple[str, str], list[dict]] = {}
        self.matches: list[dict] = []
        self.requests: list[httpx.Request] = []
        self.limited = 0  # сколько раз ответить 429

    def add(self, channel, user, text, minutes_ago=60, **extra) -> dict:
        m = {"type": "message", "user": user, "text": text, "ts": ts(minutes_ago), **extra}
        self.history[channel].append(m)
        return m

    @staticmethod
    def page(key, items, request, extra=None):
        limit = int(request.url.params.get("limit") or 100)
        start = int(request.url.params.get("cursor") or 0)
        chunk = items[start:start + limit]
        cursor = str(start + limit) if start + limit < len(items) else ""
        return httpx.Response(200, json={"ok": True, key: chunk, "response_metadata": {"next_cursor": cursor},
                                         **(extra or {})})

    def user(self, uid):
        name, bot = self.users[uid]
        return {"id": uid, "name": name.split()[0].lower(), "is_bot": bot, "profile": {"real_name": name}}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        method = request.url.path.removeprefix("/api/")
        p = request.url.params
        if self.limited:
            self.limited -= 1
            return httpx.Response(429, headers={"Retry-After": "2"})
        if method == "auth.test":
            return httpx.Response(200, json={"ok": True, "user_id": ME, "user": "anna", "team": "Acme",
                                             "url": TEAM_URL}, headers={"x-oauth-scopes": self.scopes})
        if method == "users.info":
            if p["user"] not in self.users:
                return httpx.Response(200, json={"ok": False, "error": "user_not_found"})
            return httpx.Response(200, json={"ok": True, "user": self.user(p["user"])})
        if method == "users.list":
            return self.page("members", [self.user(u) for u in self.users], request)
        if method == "users.conversations":
            return self.page("channels", self.convs, request)
        if method == "conversations.members":
            return self.page("members", [ME, PETR, OLGA], request)
        if method == "conversations.history":
            oldest = float(p.get("oldest") or 0)
            items = sorted((m for m in self.history[p["channel"]] if float(m["ts"]) > oldest),
                           key=lambda m: -float(m["ts"]))
            return self.page("messages", items, request)
        if method == "conversations.replies":
            oldest = float(p.get("oldest") or 0)
            parent = next(m for m in self.history[p["channel"]] if m["ts"] == p["ts"])
            items = [parent] + [r for r in self.replies.get((p["channel"], p["ts"]), []) if float(r["ts"]) > oldest]
            return self.page("messages", items, request)
        if method == "search.messages":
            if "search:read" not in self.scopes:
                return httpx.Response(200, json={"ok": False, "error": "missing_scope", "needed": "search:read"})
            return httpx.Response(200, json={"ok": True, "messages": {"matches": self.matches}})
        return httpx.Response(200, json={"ok": False, "error": "unknown_method"})

    def calls(self, method, **params) -> int:
        return sum(1 for r in self.requests if r.url.path.endswith("/" + method)
                   and all(r.url.params.get(k) == v for k, v in params.items()))


@pytest.fixture
def fake():
    config.set_secret("SLACK_TOKEN", "xoxp-secret-token")
    return FakeSlack()


def run(fake, fn=None):
    return (fn or slack.collect)(transport=httpx.MockTransport(fake))


def test_check_names_user_team_and_scopes(fake):
    assert run(fake, slack.check) == "вход как Анна Смирнова · рабочее пространство Acme"
    assert store.get_state("slack_me") == {"id": ME, "name": "Анна Смирнова", "team": "Acme", "url": TEAM_URL}
    fake.scopes = ",".join(slack.REQUIRED_SCOPES)
    assert run(fake, slack.check).endswith("упоминания в других каналах не видны — нет права search:read")
    fake.scopes = "channels:read,users:read"
    with pytest.raises(slack.SlackError, match="не хватает прав: channels:history, groups:history"):
        run(fake, slack.check)


def test_invalid_token_and_missing_token(fake):
    real = fake.__call__

    def bad(request):
        if request.url.path.endswith("auth.test"):
            return httpx.Response(200, json={"ok": False, "error": "invalid_auth"})
        return real(request)

    with pytest.raises(slack.SlackError, match="Slack не принял токен.*OAuth & Permissions"):
        slack.check(transport=httpx.MockTransport(bad))
    config.set_secret("SLACK_TOKEN", None)
    with pytest.raises(slack.SlackError, match="Не задан токен Slack"):
        run(fake, slack.check)


def test_refresh_chats_titles_kinds_and_dms(fake):
    assert run(fake, slack.refresh_chats) == 6  # без заметок себе
    chats = {c["chat_id"]: c for c in store.rows("SELECT * FROM chats WHERE source='slack'")}
    assert set(chats) == {"D1", "D3", "G1", "C1", "C2", "C3"}
    assert (chats["D1"]["title"], chats["D1"]["kind"], chats["D1"]["enabled"]) == ("Пётр Иванов", "private", 1)
    assert (chats["D3"]["kind"], chats["D3"]["enabled"]) == ("bot", 0)  # Slackbot сам не включается
    assert (chats["G1"]["title"], chats["G1"]["kind"], chats["G1"]["enabled"]) == (
        "Группа: Пётр Иванов, Ольга Петрова", "group", 1)
    assert (chats["C1"]["title"], chats["C1"]["kind"], chats["C1"]["enabled"]) == ("#sklad", "channel", 0)
    assert (chats["C3"]["title"], chats["C3"]["kind"]) == ("#board", "group")
    assert set(store.get_state("slack_dms")) == {"D1", "D3", "G1"}
    store.execute("UPDATE chats SET enabled=0 WHERE source='slack' AND chat_id='D1'")  # директор выключила
    run(fake, slack.refresh_chats)
    assert store.scalar("SELECT enabled FROM chats WHERE source='slack' AND chat_id='D1'") == 0  # не включаем снова


def test_collect_dms_enabled_channel_mrkdwn_threads_and_checkpoint(fake):
    fake.add("D1", PETR, "Старое, до подключения", minutes_ago=60 * 24 * 10)
    fake.add("D1", PETR, "<@U001>, посмотрите <https://acme.com/doc|договор> &amp; счёт?", minutes_ago=30,
             files=[{"name": "Счёт.pdf", "mimetype": "application/pdf"}, {"name": "x.png", "mimetype": "image/png"}])
    fake.add("D1", ME, "Посмотрю до пятницы", minutes_ago=20)
    fake.add("D1", PETR, "joined", minutes_ago=19, subtype="channel_join")
    fake.add("G1", OLGA, "В <#C1|sklad> обсудим, <!here>", minutes_ago=15)
    parent = fake.add("C1", PETR, "Отгрузка в среду", minutes_ago=50, reply_count=1, latest_reply=ts(40),
                      thread_ts=None)
    parent["thread_ts"] = parent["ts"]
    fake.replies[("C1", parent["ts"])] = [{"type": "message", "user": OLGA, "text": "Накладные готовы",
                                           "ts": parent["latest_reply"], "thread_ts": parent["ts"]}]
    fake.add("C2", PETR, "Новость в неотмеченном канале")
    run(fake, slack.refresh_chats)
    store.execute("UPDATE chats SET enabled=1 WHERE source='slack' AND chat_id='C1'")

    assert run(fake) == "каналов прочитано: 1, личных: 2, новых сообщений: 5, упоминаний: 0"
    assert fake.calls("conversations.history", channel="C2") == 0
    d1 = store.rows("SELECT * FROM messages WHERE source='slack' AND chat_id='D1' ORDER BY ts")
    assert [m["text"] for m in d1] == [
        "[файл: Счёт.pdf] [изображение] @Анна Смирнова, посмотрите договор (https://acme.com/doc) & счёт?",
        "Посмотрю до пятницы"]
    assert d1[0]["sender_name"] == "Пётр Иванов" and not d1[0]["is_outgoing"]
    assert d1[1]["sender_name"] == "Я" and d1[1]["is_outgoing"] == 1
    assert d1[0]["link"] == f"{TEAM_URL}archives/D1/p{d1[0]['msg_id'].replace('.', '')}"
    group = store.row("SELECT * FROM messages WHERE chat_id='G1'")
    assert group["text"] == "В #sklad обсудим, @here" and group["chat_title"] == "Группа: Пётр Иванов, Ольга Петрова"
    reply = store.row("SELECT * FROM messages WHERE text='Накладные готовы'")
    assert reply["reply_to"] == parent["ts"] and reply["chat_id"] == "C1"
    assert reply["link"].endswith(f"?thread_ts={parent['ts']}&cid=C1")

    # повторный сбор: ничего нового; тред не перечитываем, пока в нём нет новых ответов
    replies_before = fake.calls("conversations.replies")
    assert run(fake) == "каналов прочитано: 1, личных: 2, новых сообщений: 0, упоминаний: 0"
    assert fake.calls("conversations.replies") == replies_before
    # новый ответ в треде и новое сообщение
    parent["latest_reply"] = ts(1)
    parent["reply_count"] = 2
    fake.replies[("C1", parent["ts"])].append({"type": "message", "user": ME, "text": "Спасибо!",
                                               "ts": parent["latest_reply"], "thread_ts": parent["ts"]})
    fake.add("D1", PETR, "Жду", minutes_ago=0.5)
    assert run(fake) == "каналов прочитано: 1, личных: 2, новых сообщений: 2, упоминаний: 0"
    assert store.get_state("slack_last:D1")
    assert parent["ts"] in store.get_state("slack_threads:C1")
    assert all(r.method == "GET" for r in fake.requests)  # PULSE в Slack не пишет


def test_mentions_in_unread_channels(fake):
    fake.add("C1", PETR, "Своё", minutes_ago=5)
    run(fake, slack.refresh_chats)
    store.execute("UPDATE chats SET enabled=1 WHERE source='slack' AND chat_id='C1'")
    store.upsert_chat("slack", "C9", "#никогда", "channel", enabled=-1)
    fake.matches = [
        {"ts": ts(10), "user": OLGA, "text": "<@U001>, нужен ваш ответ по бюджету",
         "channel": {"id": "C7", "name": "finance", "is_private": True}, "permalink": f"{TEAM_URL}archives/C7/p1"},
        {"ts": ts(9), "user": OLGA, "text": "<@U001> тут", "channel": {"id": "C1", "name": "sklad"}},  # уже читаем
        {"ts": ts(8), "user": OLGA, "text": "<@U001> лично", "channel": {"id": "D1", "is_im": True}},
        {"ts": ts(7), "user": OLGA, "text": "<@U001> не читать", "channel": {"id": "C9", "name": "x"}},
        {"ts": ts(60 * 24 * 10), "user": OLGA, "text": "<@U001> давно", "channel": {"id": "C8", "name": "old"}},
    ]
    assert run(fake).endswith("упоминаний: 1")
    m = store.row("SELECT * FROM messages WHERE chat_id='C7'")
    assert m["text"] == "@Анна Смирнова, нужен ваш ответ по бюджету" and m["sender_name"] == "Ольга Петрова"
    assert m["link"] == f"{TEAM_URL}archives/C7/p1" and m["processed"] == 0  # дойдёт до разбора
    chat = store.row("SELECT * FROM chats WHERE source='slack' AND chat_id='C7'")
    assert (chat["title"], chat["kind"], chat["enabled"]) == ("#finance", "group", 0)  # целиком не читаем
    assert not store.row("SELECT 1 FROM messages WHERE chat_id IN ('C8', 'C9')")
    assert run(fake).endswith("упоминаний: 0")  # уже взятое повторно не берём
    fake.scopes = ",".join(slack.REQUIRED_SCOPES)
    assert "упоминаний" not in run(fake)


def test_rate_limit_is_retried(fake, monkeypatch):
    pauses = []
    monkeypatch.setattr(slack.time, "sleep", pauses.append)
    fake.limited = 2
    assert run(fake, slack.check).startswith("вход как Анна Смирнова")
    assert pauses == [2.0, 2.0]


def test_manifest_has_only_read_scopes():
    data = json.loads(slack.manifest_json())
    scopes = data["oauth_config"]["scopes"]["user"]
    assert set(scopes) == set(slack.REQUIRED_SCOPES) | {"search:read"}
    assert all(s.endswith((":read", ":history")) for s in scopes) and "bot" not in data["oauth_config"]["scopes"]
    assert data["settings"]["socket_mode_enabled"] is False


def test_window_and_token_save(fake, monkeypatch):
    """Окно Slack в «Подключениях»: шаги приложения с манифестом; Bot Token не принимаем; после токена — список."""
    from starlette.testclient import TestClient

    from opsdash.web.app import app

    client = TestClient(app, base_url="http://localhost:8765")
    config.set_secret("SLACK_TOKEN", None)  # директор ещё не подключала Slack
    page = client.get("/settings?tab=connections").text
    assert 'href="/settings?tab=slack"' in page and "Личные сообщения, упоминания и рабочие каналы Slack" in page
    page = client.get("/settings?tab=slack").text
    assert "From a manifest" in page and "Скопировать манифест" in page and "search:read" in page
    h = {"referer": "http://localhost:8765/settings?tab=slack"}
    r = client.post("/settings/slack", data={"token": "xoxb-bot"}, headers=h, follow_redirects=False)
    assert "err=" in r.headers["location"] and not config.get_secret("SLACK_TOKEN")
    monkeypatch.setattr(slack, "refresh_chats", lambda *a, **k: 7)
    r = client.post("/settings/slack", data={"token": "xoxp-user"}, headers=h, follow_redirects=False)
    assert "msg=" in r.headers["location"] and config.get_secret("SLACK_TOKEN") == "xoxp-user"
    assert config.load_settings()["slack"]["enabled"]  # первый токен — читать сразу
