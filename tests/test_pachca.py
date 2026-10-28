from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from opsdash import cli, config, links, records, store
from opsdash.sources import pachca

ME, PETR, BOT = 1, 2, 3
NOW = datetime.now(timezone.utc).replace(microsecond=0)


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


class FakePachca:
    """Пачка в памяти: курсорная пагинация, как у настоящего API (has_next, next_page)."""

    def __init__(self, scopes=pachca.SCOPES):
        self.scopes = list(scopes)
        self.users = {ME: ("Анна", "Смирнова", False), PETR: ("Пётр", "Иванов", False), BOT: ("Робот", "", True),
                      **{i: (f"Коллега{i}", "", False) for i in range(10, 16)}}
        self.chats = {
            10: {"name": "Склад", "personal": False, "channel": False, "member_ids": [ME, PETR]},
            20: {"name": "", "personal": True, "channel": False, "member_ids": [ME, PETR]},
            30: {"name": "Новости", "personal": False, "channel": True, "member_ids": [ME]},
            40: {"name": "", "personal": True, "channel": False, "member_ids": [ME]},  # заметки себе
            50: {"name": "", "personal": True, "channel": False, "member_ids": [ME, BOT]},
        }
        self.messages: dict[int, list[dict]] = {cid: [] for cid in (10, 20, 30, 40, 50, 5000, 6000)}
        self.threads: list[dict] = []
        self.requests: list[httpx.Request] = []
        self.next_id = 1

    def add(self, chat_id, user_id, text, ago=timedelta(hours=1), **extra) -> dict:
        m = {"id": self.next_id, "chat_id": chat_id, "user_id": user_id, "content": text,
             "created_at": iso(NOW - ago), "url": f"https://app.pachca.com/chats/{chat_id}?message={self.next_id}",
             "files": [], "forwarding": None, "parent_message_id": None, "thread": None, **extra}
        self.next_id += 1
        self.messages[chat_id].append(m)
        if chat_id in self.chats:
            self.chats[chat_id]["last_message_at"] = m["created_at"]
        return m

    @staticmethod
    def page(items, request):
        limit = int(request.url.params.get("limit") or 50)
        start = int(request.url.params.get("cursor") or 0)
        chunk = items[start:start + limit]
        more = start + limit < len(items)
        return httpx.Response(200, json={"data": chunk, "meta": {"paginate": {
            "next_page": str(start + limit), "prev_page": "p", "has_next": more, "has_prev": start > 0}}})

    def user(self, uid):
        first, last, bot = self.users[uid]
        return {"id": uid, "first_name": first, "last_name": last, "nickname": f"u{uid}", "bot": bot}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path.removeprefix("/api/shared/v1")
        if request.method != "GET":
            return httpx.Response(405)  # PULSE в Пачку не пишет
        if path == "/oauth/token/info":
            return httpx.Response(200, json={"data": {"id": 7, "token": "abcdefgh…wxyz", "user_id": ME,
                                                      "scopes": self.scopes}})
        if path == "/users":
            return self.page([self.user(u) for u in sorted(self.users)], request)
        if path.startswith("/users/"):
            uid = int(path.rsplit("/", 1)[1])
            if uid not in self.users:
                return httpx.Response(404, json={"errors": [{"message": "not found", "code": "not_found"}]})
            return httpx.Response(200, json={"data": self.user(uid)})
        if path == "/chats":
            return self.page([{"id": cid, "archived": False, "last_message_at": None, **c}
                              for cid, c in self.chats.items()], request)
        if path == "/messages":
            items = sorted(self.messages[int(request.url.params["chat_id"])], key=lambda m: -m["id"])
            return self.page(items, request)
        if path == "/threads":
            if "threads:read" not in self.scopes:
                return httpx.Response(403, json={"error": "insufficient_scope", "error_description": "no scope"},
                                      headers={"WWW-Authenticate": 'Bearer error="insufficient_scope", scope="threads:read"'})
            after = request.url.params["last_message_at_after"]
            return self.page([t for t in self.threads if store.parse_ts(t["updated_at"]) > store.parse_ts(after)],
                             request)
        return httpx.Response(404)

    def message_pages(self, chat_id) -> int:
        return sum(1 for r in self.requests if r.url.path.endswith("/messages")
                   and r.url.params.get("chat_id") == str(chat_id))


@pytest.fixture
def fake():
    config.set_secret("PACHCA_TOKEN", "secret-token-value")
    return FakePachca()


def test_collect_reads_enabled_chats_with_pagination_checkpoint_and_threads(fake):
    for i in range(120):  # три страницы по 50
        fake.add(10, ME if i % 10 == 0 else PETR, f"Склад {i}", ago=timedelta(minutes=200 - i))
    fake.add(20, PETR, "Старое, до подключения", ago=timedelta(days=10))
    fake.add(20, PETR, "Анна, посмотрите договор?", ago=timedelta(minutes=30),
             files=[{"id": 1, "name": "Договор.pdf", "file_type": "file"}])
    fake.add(30, PETR, "Новость канала")

    res = pachca.collect(transport=httpx.MockTransport(fake))
    assert res == "чатов прочитано: 0, новых сообщений: 0"  # пока ничего не отмечено — только список чатов
    chats = {c["chat_id"]: c for c in store.rows("SELECT * FROM chats WHERE source='pachca'")}
    assert set(chats) == {"10", "20", "30", "50"}  # заметки себе в список не попадают
    assert chats["20"]["title"] == "Пётр Иванов" and chats["20"]["kind"] == "private"
    assert chats["30"]["kind"] == "channel" and chats["50"]["kind"] == "bot" and chats["10"]["enabled"] == 0
    assert store.get_state("pachca_me") == {"id": ME, "name": "Анна Смирнова"}

    store.execute("UPDATE chats SET enabled=1 WHERE source='pachca' AND chat_id IN ('10', '20')")
    fake.requests.clear()
    assert pachca.collect(transport=httpx.MockTransport(fake)) == "чатов прочитано: 2, новых сообщений: 121"
    assert fake.message_pages(10) == 3 and fake.message_pages(30) == 0
    rows = store.rows("SELECT * FROM messages WHERE chat_id='10' ORDER BY ts")
    assert len(rows) == 120 and rows[0]["text"] == "Склад 0" and rows[-1]["text"] == "Склад 119"
    assert rows[0]["is_outgoing"] == 1 and rows[0]["sender_name"] == "Я"
    assert rows[1]["is_outgoing"] == 0 and rows[1]["sender_name"] == "Пётр Иванов"
    assert rows[1]["link"] == f"https://app.pachca.com/chats/10?message={rows[1]['msg_id']}"
    private = store.rows("SELECT * FROM messages WHERE chat_id='20'")
    assert [m["text"] for m in private] == ["[файл: Договор.pdf] Анна, посмотрите договор?"]  # старое не тянем
    assert store.get_state("pachca_last:10")["id"] == 120

    # новое в «Складе» и тред под его сообщением; в личке ничего не писали — её не запрашиваем
    fake.add(10, PETR, "Отгрузка завтра", ago=timedelta(minutes=1))
    fake.add(5000, PETR, "В треде: накладные готовы", ago=timedelta(minutes=2))
    fake.add(5000, ME, "Спасибо!", ago=timedelta(minutes=1))
    fake.add(6000, PETR, "Тред в чужом канале", ago=timedelta(minutes=1))
    fake.threads = [{"id": 900, "chat_id": 5000, "message_id": 5, "message_chat_id": 10, "updated_at": iso(NOW)},
                    {"id": 901, "chat_id": 6000, "message_id": 121, "message_chat_id": 30, "updated_at": iso(NOW)}]
    fake.requests.clear()
    assert pachca.collect(transport=httpx.MockTransport(fake)) == "чатов прочитано: 2, новых сообщений: 3"
    assert fake.message_pages(10) == 1 and fake.message_pages(20) == 0 and fake.message_pages(6000) == 0
    thread = store.rows("SELECT * FROM messages WHERE text LIKE '%накладные%' OR text='Спасибо!' ORDER BY ts")
    assert [(m["chat_id"], m["chat_title"], m["reply_to"]) for m in thread] == [("10", "Склад", "5")] * 2
    assert thread[1]["is_outgoing"] == 1

    # повторный сбор ничего не дублирует
    assert pachca.collect(transport=httpx.MockTransport(fake)).endswith("новых сообщений: 0")
    assert store.scalar("SELECT COUNT(*) FROM messages WHERE source='pachca'") == 124
    assert not [r for r in fake.requests if r.method != "GET"]  # только чтение


def test_triage_sees_pachca_messages(fake, capsys):
    fake.add(20, PETR, "Анна, согласуете счёт до пятницы?")
    store.upsert_chat("pachca", "20", "Пётр Иванов", "private", enabled=1)
    pachca.collect(transport=httpx.MockTransport(fake))
    cli.main(["messages", "pending", "--source", "pachca"])
    out = capsys.readouterr().out
    assert "## pachca · «Пётр Иванов» (private, chat_id=20)" in out and "согласуете счёт" in out
    mid = store.scalar("SELECT id FROM messages WHERE source='pachca'")
    cli.main(["commitments", "add", "--direction", "mine", "--whom", "Пётр Иванов", "--what", "Согласовать счёт",
              "--source", f"pachca:{mid}"])
    task = records.add_links(store.rows("SELECT * FROM commitments"))[0]
    assert task["link"].startswith("https://app.pachca.com/chats/20?message=") and task["link_label"] == "Пачка"


def test_message_text_voice_forward_and_bot_sender():
    m = {"id": 9, "chat_id": 5000, "user_id": BOT, "content": "", "created_at": "2026-10-27T09:00:00.000Z",
         "url": "https://app.pachca.com/chats/5000?message=9", "parent_message_id": 7, "display_name": "Сборка",
         "files": [{"name": "voice.ogg", "file_type": "voice"}], "voice_content": {"transcript": "Отгрузку переносим"},
         "forwarding": {"original_message_id": 1}}
    msg = pachca.parse_message(m, "10", "Склад", ME, {}, thread_of=5)
    assert msg["text"] == "[переслано] [голосовое сообщение] Отгрузку переносим"
    assert msg["sender_name"] == "Сборка" and msg["reply_to"] == "7" and msg["ts"] == "2026-10-27T09:00:00+00:00"
    assert pachca.parse_message({**m, "files": [], "forwarding": None}, "10", "Склад", ME, {}) is None


def test_unknown_senders_are_loaded_once(fake):
    store.upsert_chat("pachca", "10", "Склад", "group", enabled=1)
    for uid in (10, 11, 12, 13, 99):  # 99 — удалённый сотрудник
        fake.add(10, uid, f"Сообщение от {uid}")
    pachca.collect(transport=httpx.MockTransport(fake))
    names = [m["sender_name"] for m in store.rows("SELECT sender_name FROM messages ORDER BY id")]
    assert names == ["Коллега10", "Коллега11", "Коллега12", "Коллега13", ""]
    users = [r.url.path.rsplit("/", 1)[1] for r in fake.requests if "/users" in r.url.path]
    # владелец токена и собеседники в личках, затем незнакомые отправители — весь список разом и недостающий
    assert users == ["1", "2", "3", "users", "99"]
    fake.requests.clear()
    fake.add(10, 99, "Ещё раз")
    pachca.collect(transport=httpx.MockTransport(fake))
    assert not [r for r in fake.requests if "/users" in r.url.path]  # имена запомнены, и пустое тоже


def test_bad_token_is_reported_without_leaking_it(fake):
    bad = httpx.MockTransport(lambda r: httpx.Response(401, json={"error": "invalid_token",
                                                                   "error_description": "Access token is invalid"}))
    for fn in (pachca.check, pachca.collect):
        with pytest.raises(pachca.PachcaError) as e:
            fn(transport=bad)
        assert "не приняла токен" in str(e.value) and "secret-token-value" not in str(e.value)
    config.set_secret("PACHCA_TOKEN", None)
    with pytest.raises(pachca.PachcaError, match="Не задан токен"):
        pachca.check(transport=httpx.MockTransport(fake))


def test_check_lists_missing_and_extra_scopes(fake, monkeypatch, capsys):
    real_http = pachca._http
    monkeypatch.setattr(pachca, "_http", lambda transport=None: real_http(httpx.MockTransport(fake)))
    cli.main(["check", "pachca"])
    assert capsys.readouterr().out.strip() == "✓ pachca: вход как Анна Смирнова"

    fake.scopes = ["chats:read", "messages:read", "users:read", "messages:create"]
    with pytest.raises(SystemExit):
        cli.main(["check", "pachca"])
    out = capsys.readouterr().out
    assert out.startswith("✗ pachca: вход как Анна Смирнова, но не хватает прав: threads:read")

    fake.scopes = [*pachca.SCOPES, "messages:create", "search:messages"]
    assert pachca.check() == "вход как Анна Смирнова; лишние права: messages:create — PULSE только читает, их можно снять"

    # без threads:read сбор всё равно идёт — только без тредов
    fake.scopes = ["chats:read", "messages:read", "users:read"]
    store.upsert_chat("pachca", "10", "Склад", "group", enabled=1)
    fake.add(10, PETR, "Привет")
    assert pachca.collect().endswith("новых сообщений: 1; треды не читаются — у токена нет права threads:read")


def test_rate_limit_is_retried(fake, monkeypatch):
    pauses, calls = [], []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "2"}, json={"errors": [{"code": "rate_limit"}]})
        return fake(request)

    monkeypatch.setattr(pachca.time, "sleep", pauses.append)
    assert pachca.check(transport=httpx.MockTransport(handler)) == "вход как Анна Смирнова"
    assert pauses == [2.0]


def test_original_link_and_settings_tab(fake, monkeypatch):
    store.upsert_chat("pachca", "10", "Склад", "group", enabled=1)
    store.add_message({"source": "pachca", "chat_id": "10", "chat_title": "Склад", "msg_id": "77", "sender_id": "2",
                       "sender_name": "Пётр Иванов", "text": "в треде", "ts": "2026-10-27T09:00:00+00:00",
                       "link": "https://app.pachca.com/chats/5000?message=77"})
    assert links.original("pachca", "10", "77") == ("https://app.pachca.com/chats/5000?message=77", "К сообщению")
    assert links.original("pachca", "10", "78") == ("https://app.pachca.com/chats/10?message=78", "К сообщению")
    assert links.original("pachca", "10") == ("https://app.pachca.com/chats/10", "Открыть в Пачке")

    from opsdash.web.app import app

    client = TestClient(app, base_url="http://localhost:8765")
    real_http = pachca._http
    monkeypatch.setattr(pachca, "_http", lambda transport=None: real_http(httpx.MockTransport(fake)))
    r = client.post("/settings/pachca", data={"token": "new-token", "enabled": "on"}, follow_redirects=False)
    assert "Чатов в Пачке: 4" in __import__("urllib.parse").parse.unquote(r.headers["location"])
    assert config.get_secret("PACHCA_TOKEN") == "new-token" and config.load_settings()["pachca"]["enabled"]
    page = client.get("/settings?tab=pachca").text
    assert "Токен принят: <b>Анна Смирнова</b>" in page and "Пётр Иванов" in page and "new-token" not in page
    assert 'id="pachca-chats"' in page and 'data-show="-1"' not in page  # «никогда» — только у Telegram
    client.post("/settings/chats", data={"source": "pachca", "chat__20": "1", "ajax": "1"})
    assert store.scalar("SELECT enabled FROM chats WHERE source='pachca' AND chat_id='20'") == 1


def test_first_token_turns_reading_on(fake, monkeypatch):
    """Галочку «Читать Пачку» при подключении легко не заметить — первый токен включает чтение сам."""
    from urllib.parse import unquote

    from opsdash.web.app import app

    config.set_secret("PACHCA_TOKEN", None)
    client = TestClient(app, base_url="http://localhost:8765")
    real_http = pachca._http
    monkeypatch.setattr(pachca, "_http", lambda transport=None: real_http(httpx.MockTransport(fake)))
    r = client.post("/settings/pachca", data={"token": "first-token"}, follow_redirects=False)
    assert "чтение Пачки включено" in unquote(r.headers["location"]) and config.load_settings()["pachca"]["enabled"]
    client.post("/settings/pachca", data={})  # сняла галочку сама — так и остаётся
    assert not config.load_settings()["pachca"]["enabled"]
    client.post("/settings/pachca", data={"token": "other-token"})  # сменила токен — галочку не трогаем
    assert not config.load_settings()["pachca"]["enabled"]
