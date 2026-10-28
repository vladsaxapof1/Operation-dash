import asyncio
import os
import time
import zipfile
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from opsdash import config, recordings, store, transcribe
from opsdash.sources import calendar, mail, telegram, ymessenger


def test_mail_parse_html_quotes_and_outgoing():
    m = EmailMessage()
    m["Subject"] = "Вакансии Казань"
    m["From"] = "Мария Кузнецова <maria@example.ru>"
    m["To"] = "anna@example.ru"
    m["Date"] = "Sun, 25 Oct 2026 10:00:00 +0300"
    m["Message-ID"] = "<abc@example.ru>"
    m.set_content("plain fallback")
    m.add_alternative("<html><style>p{}</style><p>Анна, посмотрите тексты.</p><p>До среды.</p>"
                      "<p>25.10.2026 9:00, Анна написал:</p><p>старое письмо</p></html>", subtype="html")
    msg = mail.parse_message(m.as_bytes(), "INBOX", "anna@example.ru")
    assert msg["sender_name"] == "Мария Кузнецова" and not msg["is_outgoing"]
    assert "Тема: Вакансии Казань" in msg["text"]
    assert msg["ts"] == "2026-10-25T07:00:00+00:00"

    html_only = EmailMessage()
    html_only["From"] = "anna@example.ru"
    html_only.set_content("<p>Привет</p><blockquote>&gt; цитата</blockquote>", subtype="html")
    out = mail.parse_message(html_only.as_bytes(), "INBOX", "anna@example.ru")
    assert out["is_outgoing"] and "Привет" in out["text"]
    assert mail.strip_quotes("Ответ\n> старое\nOn Mon, Petr wrote:\nхвост") == "Ответ"



def _letter(sender, subject, msg_id):
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = sender, "anna@gmail.com", subject
    m["Date"], m["Message-ID"] = "Mon, 26 Oct 2026 10:00:00 +0300", msg_id
    m.set_content("текст")
    return m.as_bytes()


class _FakeImap:
    LETTERS = {1: _letter("Мария <maria@x.ru>", "Склад", "<1@x>"), 2: _letter("noreply@shop.ru", "Скидки", "<2@x>"),
               3: _letter("anna@gmail.com", "Ответ Марии", "<3@x>")}
    last = None

    def __init__(self, host, port, timeout=None):
        self.host, self.searches, self.folder = host, [], None
        _FakeImap.last = self

    def login(self, user, pwd):
        self.user, self.pwd = user, pwd

    def list(self):
        return "OK", [b'(\\HasNoChildren) "/" "INBOX"', b'(\\HasNoChildren \\Sent) "/" "[Gmail]/Sent Mail"']

    def select(self, name, readonly=False):
        self.folder = name.strip('"')
        return "OK", [b"3"]

    def response(self, code):
        return code, [b"7"]

    def uid(self, cmd, *args):
        if cmd == "search":
            self.searches.append((self.folder, args[1:]))
            return "OK", [b"1 2" if self.folder == "INBOX" else b"3"]
        return "OK", [(b"1 (BODY[] {9})", self.LETTERS[int(args[0])]), b")"]

    def logout(self):
        pass


def test_gmail_second_mailbox(monkeypatch, settings_with):
    monkeypatch.setattr(mail.imaplib, "IMAP4_SSL", _FakeImap)
    settings_with(gmail={"enabled": True, "email": "anna@gmail.com"}, mail={"email": "anna@ya.ru"})
    config.set_secret("GMAIL_APP_PASSWORD", "abcdefghijklmnop")
    assert mail.collect_gmail() == "новых писем: 2"  # рассылка noreply отсеяна общим списком
    imap = _FakeImap.last
    assert (imap.host, imap.user, imap.pwd) == ("imap.gmail.com", "anna@gmail.com", "abcdefghijklmnop")
    assert imap.searches[0] == ("INBOX", ("SINCE " + imap.searches[0][1][0][6:], "X-GM-RAW", mail.GMAIL_PRIMARY))
    assert "category:primary" not in mail.GMAIL_PRIMARY  # Gmail по IMAP по нему ничего не находит
    assert imap.searches[1][0] == "[Gmail]/Sent Mail" and "X-GM-RAW" not in imap.searches[1][1]
    rows = store.rows("SELECT source, chat_id, chat_title, sender_name, is_outgoing FROM messages ORDER BY msg_id")
    assert [tuple(r.values()) for r in rows] == [
        ("mail", "gmail:INBOX", "Gmail: входящие", "Мария", 0),
        ("mail", "gmail:SENT", "Gmail: отправленные", "Я", 1),
    ]
    assert store.get_state("mail:gmail:INBOX") == {"validity": "7", "uid": 2}

    config.set_secret("MAIL_APP_PASSWORD", "yandexpass")  # Яндекс-ящик — чаты прежние, без приставки
    mail.collect()
    assert _FakeImap.last.host == "imap.yandex.ru" and "X-GM-RAW" not in _FakeImap.last.searches[0][1]
    assert store.row("SELECT title FROM chats WHERE chat_id='INBOX'")["title"] == "Почта: входящие"

def test_ymessenger_parse_and_collect():
    updates = [
        {"update_id": 5, "message_id": 1, "timestamp": 1761400000, "chat": {"type": "group", "id": "0/0/abc"},
         "from": {"login": "d.orlov@corp.ru", "display_name": "Дмитрий"}, "text": "График согласован"},
        {"update_id": 6, "message_id": 2, "timestamp": 1761400100, "chat": {"type": "private"},
         "from": {"login": "anna@corp.ru", "display_name": "Анна"}, "text": "Ок"},
        {"update_id": 7, "message_id": 3, "timestamp": 1761400200, "chat": {"type": "group", "id": "0/0/abc"},
         "from": {"login": "x"}, "sticker": {"id": "s"}},
    ]
    seen_offsets = []

    def handler(request):
        seen_offsets.append(request.url.params.get("offset"))
        off = int(request.url.params.get("offset") or 0)
        return httpx.Response(200, json={"ok": True, "updates": [u for u in updates if u["update_id"] >= off]})

    config.set_secret("YM_BOT_TOKEN", "t")
    config.update_settings("ymessenger", {"my_login": "anna@corp.ru"})
    res = ymessenger.collect(transport=httpx.MockTransport(handler))
    assert "3" in res
    rows = store.rows("SELECT * FROM messages ORDER BY ts")
    assert rows[0]["chat_title"].startswith("Чат") and rows[1]["is_outgoing"] == 1
    assert rows[2]["text"] == "[стикер]"
    assert store.get_state("ym_offset") == 8
    ymessenger.collect(transport=httpx.MockTransport(handler))
    assert store.scalar("SELECT COUNT(*) FROM messages") == 3



def test_telegram_dialogs_skip_migrated_groups_and_deleted_accounts():
    def dialog(chat_id, name, user=False, date=None, **ent):
        return SimpleNamespace(id=chat_id, name=name, entity=SimpleNamespace(**ent), date=date,
                               is_user=user, is_channel=not user and chat_id < -10**12, is_group=not user)

    last = datetime(2026, 10, 26, 9, 30, tzinfo=timezone.utc)
    dialogs = [
        dialog(-1001234567890, "Отдел продаж"),
        dialog(-5000000001, "Отдел продаж", migrated_to=SimpleNamespace(channel_id=1234567890)),
        dialog(777, "", user=True, deleted=True),
        dialog(42, "Олег", user=True, date=last, username="oleg"),
        dialog(43, "Олег", user=True, username=None, usernames=[SimpleNamespace(username="oleg_k", active=True)]),
    ]

    class Client:
        async def iter_dialogs(self):
            for d in dialogs:
                yield d

    store.upsert_chat("telegram", "-5000000001", "Отдел продаж", "group")  # остался от прошлого списка
    asyncio.run(telegram._refresh_dialogs(Client()))
    rows = {r["chat_id"]: r["username"] for r in store.rows("SELECT chat_id, username FROM chats")}
    assert rows == {"-1001234567890": "", "42": "oleg", "43": "oleg_k"}  # тёзки различаются по @username
    # время последнего сообщения — для сортировки «свежие сверху», даже у нечитаемых чатов
    assert store.scalar("SELECT last_ts FROM chats WHERE chat_id='42'") == "2026-10-26T09:30:00+00:00"


def test_telegram_forum_group_reads_all_topics_and_keeps_topic_names(monkeypatch, settings_with, capsys):
    from telethon.tl.functions.messages import GetForumTopicsByIDRequest
    from telethon.tl.types import ForumTopicDeleted, MessageReplyHeader, User

    from opsdash import cli, records

    class Channel(SimpleNamespace):  # по имени класса сборщик понимает, что у сообщений бывают ссылки
        pass

    olga, me = User(id=5, first_name="Ольга"), User(id=1, first_name="Анна")
    now = datetime.now(timezone.utc)

    def msg(mid, text, reply=None, action=None):
        return SimpleNamespace(
            id=mid, message=text, action=action, out=False, sender=olga, sender_id=olga.id,
            date=now - timedelta(minutes=60 - mid), reply_to=reply,
            reply_to_msg_id=reply.reply_to_msg_id if reply else None,
            voice=None, video_note=None, photo=None, video=None, document=None, sticker=None, file=None)

    forum, broken, plain = (Channel(id=100, username=None, forum=True), Channel(id=200, username=None, forum=True),
                            Channel(id=300, username=None, forum=False))
    history = {
        100: [msg(10, "Всем привет"),  # тема «General» — без пометки темой
              msg(20, "", action=SimpleNamespace()),  # «тема создана» — служебное
              msg(21, "Вчера 12 продаж", MessageReplyHeader(forum_topic=True, reply_to_msg_id=20)),
              msg(22, "А сколько возвратов?", MessageReplyHeader(forum_topic=True, reply_to_msg_id=21,
                                                                  reply_to_top_id=20)),
              msg(23, "Привет!", MessageReplyHeader(reply_to_msg_id=10)),
              msg(31, "Из удалённой темы", MessageReplyHeader(forum_topic=True, reply_to_msg_id=30))],
        200: [msg(5, "Названий тем не узнать", MessageReplyHeader(forum_topic=True, reply_to_msg_id=4))],
        300: [msg(7, "Ответ в обычной группе", MessageReplyHeader(reply_to_msg_id=6))],
    }
    dialogs = [SimpleNamespace(id=-1000000000000 - e.id, name=name, entity=e, date=now, is_user=False,
                               is_channel=True, is_group=True, message=SimpleNamespace(id=history[e.id][-1].id))
               for e, name in ((forum, "Отдел продаж"), (broken, "Сломанный форум"), (plain, "Склад"))]
    requests = []

    class Client:
        async def get_me(self):
            return me

        async def iter_dialogs(self):
            for d in dialogs:
                yield d

        async def iter_messages(self, entity, **kw):
            for m in history[entity.id]:
                yield m

        async def __call__(self, request):
            requests.append(request)
            if request.peer is broken:
                raise RuntimeError("TOPIC_ID_INVALID")
            return SimpleNamespace(topics=[SimpleNamespace(id=1, title="General"),
                                           SimpleNamespace(id=20, title="Итоги дня"), ForumTopicDeleted(id=30)])

        async def disconnect(self):
            pass

    async def authorized(settings):
        return Client()

    monkeypatch.setattr(telegram, "_authorized_client", authorized)
    for d in dialogs:
        store.upsert_chat("telegram", str(d.id), d.name, "group", enabled=1)
    assert telegram.collect(settings_with(telegram={"backfill_days": 3})) == "чатов прочитано: 3, новых сообщений: 7"

    # названия тем — одним запросом на группу с темами, только по темам из пачки; у обычной группы не спрашиваем
    assert [(type(r), r.peer, r.topics) for r in requests] == [
        (GetForumTopicsByIDRequest, forum, [1, 20, 30]), (GetForumTopicsByIDRequest, broken, [4])]
    rows = store.rows("SELECT msg_id, chat_title, topic, reply_to FROM messages ORDER BY id")
    assert [(r["msg_id"], r["chat_title"], r["topic"], r["reply_to"]) for r in rows] == [
        ("10", "Отдел продаж", "General", None),
        ("21", "Отдел продаж", "Итоги дня", None),  # пометка «ответ на начало темы» — не ответ
        ("22", "Отдел продаж", "Итоги дня", "21"),
        ("23", "Отдел продаж", "General", "10"),
        ("31", "Отдел продаж", None, None),  # тема удалена — без названия
        ("5", "Сломанный форум", None, None),  # названия не получили — сообщение всё равно сохранено
        ("7", "Склад", None, "6"),
    ]
    assert store.scalar("SELECT COUNT(*) FROM log WHERE job='telegram' AND level='warning'") == 1

    cli.main(["messages", "pending", "--source", "telegram"])
    out = capsys.readouterr().out
    assert "Ольга в теме «Итоги дня» (ответ на 21): А сколько возвратов?" in out
    assert "Ольга в теме «General»: Всем привет" in out and "Ольга (ответ на 6): Ответ в обычной группе" in out
    cli.main(["messages", "chat", "Итоги дня"])
    assert [line.split(": ", 1)[1] for line in capsys.readouterr().out.splitlines()] == [
        "Вчера 12 продаж", "А сколько возвратов?"]

    mid = store.scalar("SELECT id FROM messages WHERE msg_id='22'")
    aid = records.add_attention(summary="Спрашивают про возвраты", message_id=mid)
    assert store.scalar("SELECT chat_title FROM attention WHERE id=?", (aid,)) == "Отдел продаж · Итоги дня"

def test_subtitles_docx_and_speaker_assignment(tmp_path):
    vtt = "WEBVTT\n\n1\n00:00:01.000 --> 00:00:04.000\n<v Пётр Иванов>Начнём со склада\n\n2\n00:00:05,000 --> 00:00:07,000\nАнна: Хорошо\n"
    cues = transcribe.parse_subtitles(vtt)
    assert cues == [{"start": 1.0, "end": 4.0, "speaker": "Пётр Иванов", "text": "Начнём со склада"},
                    {"start": 5.0, "end": 7.0, "speaker": "Анна", "text": "Хорошо"}]
    docx = tmp_path / "t.docx"
    with zipfile.ZipFile(docx, "w") as z:
        z.writestr("word/document.xml", '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                   "<w:body><w:p><w:r><w:t>Строка один</w:t></w:r></w:p><w:p><w:r><w:t>Строка два</w:t></w:r></w:p></w:body></w:document>")
    assert transcribe.docx_text(docx) == "Строка один\nСтрока два"

    words = [{"start": 0.0, "end": 0.5, "word": " Привет"}, {"start": 0.6, "end": 1.0, "word": " всем."},
             {"start": 1.2, "end": 1.6, "word": " Добрый"}, {"start": 1.7, "end": 2.0, "word": " день."}]
    turns = [{"start": 0.0, "end": 1.1, "speaker": 7}, {"start": 1.1, "end": 2.5, "speaker": 3}]
    utt = transcribe.assign_speakers(words, turns)
    assert [(u["speaker"], u["text"]) for u in utt] == [("Спикер 1", "Привет всем."), ("Спикер 2", "Добрый день.")]
    md = transcribe.render_markdown("Встреча", ["Дата: 25.10"], utt)
    assert "[00:00:01] Спикер 2: Добрый день." in md


def test_recording_names_and_scan(tmp_path, settings_with):
    assert recordings.time_from_name("2026-10-25 14.00.12 Планёрка").hour == 14
    assert recordings.time_from_name("20261025_093000.m4a").minute == 30
    zoom_dir = tmp_path / "2026-10-25 14.00.12 Планёрка отдела"
    zoom_dir.mkdir()
    assert recordings.clean_title(zoom_dir / "audio1234.m4a") == "Планёрка отдела"

    settings_with(recordings={"folders": [str(tmp_path)]})
    audio = zoom_dir / "audio1234.m4a"
    video = zoom_dir / "video1234.mp4"
    other = tmp_path / "заметки.pdf"
    for f in (audio, video, other):
        f.write_bytes(b"x")
    old = time.time() - 120
    for f in (audio, video):
        os.utime(f, (old, old))
    store.set_state("recordings_since", old - 10)
    found = recordings.scan()
    assert found == [audio], "видео рядом с аудио Zoom пропускается, pdf не берётся"
    mid = recordings.create_meeting(audio)
    assert mid.startswith("2026-10-25_1400_") and recordings.scan() == []


def test_text_transcript_meeting_end_to_end(tmp_path, settings_with):
    settings_with(recordings={"folders": [str(tmp_path)]})
    f = tmp_path / "2026-10-24 10.00 Планёрка.txt"
    f.write_text("Пётр: склад переносим\nАнна: согласую с собственником", encoding="utf-8")
    store.execute('INSERT INTO events(uid, title, start, "end", all_day, attendees) VALUES(?,?,?,?,0,?)',
                  ("e1", "Планёрка операционного блока", "2026-10-24T06:55:00+00:00", "2026-10-24T08:00:00+00:00",
                   '[{"email": "petr@x.ru", "name": "Пётр Иванов"}]'))
    mid = recordings.create_meeting(f)
    meta = recordings.transcribe_meeting(mid)
    assert meta["title"] == "Планёрка операционного блока"
    m = store.row("SELECT * FROM meetings WHERE id=?", (mid,))
    assert m["status"] == "transcribed" and m["event_uid"] == "e1"
    text = (config.MEETINGS_DIR / mid / "transcript.md").read_text(encoding="utf-8")
    assert "согласую с собственником" in text and "Пётр Иванов" in text



def _google_feed(day):
    d = day.strftime("%Y%m%d")
    nd = (day + timedelta(days=7)).strftime("%Y%m%d")
    return ("BEGIN:VCALENDAR\r\nX-WR-CALNAME:anna@gmail.com\r\n"
            f"BEGIN:VEVENT\r\nUID:once\r\nSUMMARY:Созвон с банком\r\nDTSTART:{d}T070000Z\r\nDTEND:{d}T080000Z\r\nEND:VEVENT\r\n"
            f"BEGIN:VEVENT\r\nUID:weekly\r\nSUMMARY:Планёрка\r\nDTSTART:{d}T090000Z\r\nDTEND:{d}T093000Z\r\n"
            "RRULE:FREQ=WEEKLY;COUNT=2\r\nEND:VEVENT\r\n"
            f"BEGIN:VEVENT\r\nUID:weekly\r\nRECURRENCE-ID:{nd}T090000Z\r\nSUMMARY:Планёрка (перенос)\r\n"
            f"DTSTART:{nd}T110000Z\r\nDTEND:{nd}T113000Z\r\nEND:VEVENT\r\n"
            "END:VCALENDAR\r\n").encode()


def test_google_calendar_read_only_via_ical(monkeypatch, settings_with):
    import httpx

    from opsdash import proposals

    settings_with(google_calendar={"enabled": True}, calendar={"days_ahead": 14})
    config.set_secret("GOOGLE_CALENDAR_ICAL", "https://calendar.google.com/calendar/ical/a/private-b/basic.ics")
    day = (datetime.now(config.tz()) + timedelta(days=1)).date()
    feed = {"body": _google_feed(day), "status": 200}

    def fake_get(url, **kw):
        return httpx.Response(feed["status"], content=feed["body"], request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", fake_get)
    yandex = {"uid": "y1", "calendar": "Мои события", "title": "Яндекс-встреча",
              "start": store.to_utc_iso(datetime.combine(day, datetime.min.time(), config.tz()) + timedelta(hours=15)),
              "end": store.to_utc_iso(datetime.combine(day, datetime.min.time(), config.tz()) + timedelta(hours=16)),
              "attendees": "[]"}
    calendar.save_event(yandex)

    assert calendar.google_collect() == "встреч в окне: 3"
    rows = store.rows("SELECT uid, calendar, title FROM events ORDER BY start")
    google = [r for r in rows if r["uid"].startswith("google:")]
    assert [r["title"] for r in google] == ["Созвон с банком", "Планёрка", "Планёрка (перенос)"]
    assert {r["calendar"] for r in google} == {"Google: anna@gmail.com"}
    assert any(r["uid"] == "y1" for r in rows)  # Яндекс не тронут

    monkeypatch.setattr(calendar, "_calendars", lambda settings: [])  # сбор Яндекса чистит только своё
    calendar.collect()
    assert [r["uid"] for r in store.rows("SELECT uid FROM events") if not r["uid"].startswith("google:")] == []
    assert store.scalar("SELECT COUNT(*) FROM events WHERE uid LIKE 'google:%'") == 3

    feed["status"] = 404  # адрес сломался — ошибка, а встречи не исчезают как «отменённые»
    with pytest.raises(calendar.CalendarError, match="секретный адрес"):
        calendar.google_collect()
    assert store.scalar("SELECT COUNT(*) FROM events WHERE uid LIKE 'google:%'") == 3

    with pytest.raises(proposals.ProposalError, match="только читает"):
        proposals.propose_calendar_cancel(uid=google[0]["uid"])

def test_write_calendar_by_name(monkeypatch, settings_with):
    cals = [SimpleNamespace(name="Мои события"), SimpleNamespace(name="Не забыть")]
    monkeypatch.setattr(calendar, "_calendars", lambda settings: cals)
    settings = settings_with(calendar={"write_calendar": ""})
    assert calendar._write_calendar(settings).name == "Мои события"  # по умолчанию — первый
    assert calendar._write_calendar(settings, "не забыть").name == "Не забыть"
    with pytest.raises(calendar.CalendarError, match="Нет календаря «Работа»"):
        calendar._write_calendar(settings, "Работа")  # не молча в первый, а с ошибкой
    store.set_state("calendar_names", ["Мои события", "Не забыть"])
    assert calendar.write_calendar_names(settings_with(calendar={"write_calendar": "Не забыть"})) == (
        ["Мои события", "Не забыть"], "Не забыть")



def test_week_grid_layout(settings_with):
    from opsdash import overview

    settings_with(owner={"timezone": "Europe/Moscow"})
    tz = config.tz()
    monday = date(2026, 10, 26)

    def ev(uid, title, d, h1, m1, h2, m2, all_day=0, days=0):
        s = datetime(2026, 10, d, h1, m1, tzinfo=tz)
        e = datetime(2026, 10, d, h2, m2, tzinfo=tz) + timedelta(days=days)
        calendar.save_event({"uid": uid, "calendar": "Мои события", "title": title, "start": store.to_utc_iso(s),
                             "end": store.to_utc_iso(e), "all_day": all_day, "attendees": "[]"})

    ev("a", "План", 26, 11, 0, 12, 0)
    ev("b", "Созвон", 26, 11, 30, 12, 30)  # пересекается с «План» — рядом, в двух колонках
    ev("c", "Обед", 26, 13, 0, 13, 30)     # отдельно — во всю ширину
    ev("d", "Ранний", 27, 6, 30, 7, 0)     # раньше 8:00 — сетка начинается с 6:00
    ev("e", "Дом", 26, 0, 0, 0, 0, all_day=1, days=5)  # пять дней — одна полоса
    ev("f", "Конференция", 28, 0, 0, 0, 0, all_day=1, days=2)  # пересекается с «Дом» — вторая строка
    g = overview.week_grid(monday)
    day0 = {x["ev"]["title"]: x for x in g["days"][0]["events"]}
    assert (day0["План"]["left"], day0["План"]["width"]) == (0, 0.5)
    assert (day0["Созвон"]["left"], day0["Созвон"]["width"]) == (0.5, 0.5)
    assert (day0["Обед"]["left"], day0["Обед"]["width"]) == (0, 1)
    assert g["hour_from"] == 6 and g["hours"][0] == 6 and g["hours"][-1] == 19
    assert day0["План"]["top"] == 5 * 48 and day0["Обед"]["short"]
    bars = {a["ev"]["title"]: a for a in g["allday"]}
    assert (bars["Дом"]["col"], bars["Дом"]["span"], bars["Дом"]["row"]) == (0, 5, 0)
    assert (bars["Конференция"]["col"], bars["Конференция"]["span"], bars["Конференция"]["row"]) == (2, 2, 1)
    assert g["allday_rows"] == 2 and g["week"] == 44
    assert overview.week_title(monday) == "Окт – нояб 2026" and overview.week_title(date(2026, 10, 5)) == "Октябрь 2026"
    assert overview.week_title(date(2026, 12, 28)) == "Дек 2026 – янв 2027"

def test_calendar_component_and_ical():
    from icalendar import Calendar

    ics = ("BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:u1\r\nSUMMARY:Планёрка\r\nDTSTART:20261026T070000Z\r\n"
           "DTEND:20261026T080000Z\r\nATTENDEE;CN=Пётр:mailto:petr@x.ru\r\nORGANIZER:mailto:anna@x.ru\r\n"
           "END:VEVENT\r\nBEGIN:VEVENT\r\nUID:u2\r\nSUMMARY:Отпуск\r\nDTSTART;VALUE=DATE:20261027\r\nEND:VEVENT\r\n"
           "BEGIN:VEVENT\r\nUID:u3\r\nSUMMARY:Отменена\r\nSTATUS:CANCELLED\r\nDTSTART:20261026T090000Z\r\nEND:VEVENT\r\n"
           "END:VCALENDAR\r\n")
    comps = list(Calendar.from_ical(ics).walk("VEVENT"))
    tz = config.tz()
    a = calendar.parse_component(comps[0], "Рабочий", tz)
    assert a["start"] == "2026-10-26T07:00:00+00:00" and '"name": "Пётр"' in a["attendees"] and a["organizer"] == "anna@x.ru"
    b = calendar.parse_component(comps[1], "Рабочий", tz)
    assert b["all_day"] == 1 and b["end"] > b["start"]
    assert calendar.parse_component(comps[2], "Рабочий", tz) is None
    ical = calendar.build_ical("x@pult", "Созвон, итоги", "2026-10-27T15:00", "2026-10-27T15:30", ["petr@x.ru"], "", "anna@x.ru")
    assert "DTSTART:20261027T120000Z" in ical and "SUMMARY:Созвон\\, итоги" in ical and "mailto:petr@x.ru" in ical
    _ = (date, datetime, Path)
