"""Почта по IMAP (пароль приложения): Яндекс Почта и, вторым ящиком, Gmail. Письма только
читаются и не помечаются прочитанными (BODY.PEEK). Письма обоих ящиков хранятся как source="mail";
чаты Gmail отличаются приставкой «gmail:» (gmail:INBOX)."""

from __future__ import annotations

import email
import imaplib
import re
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header
from email.utils import getaddresses, parseaddr, parsedate_to_datetime
from html.parser import HTMLParser

from opsdash import config, store

SOURCE = "mail"
FOLDER_TITLES = {"INBOX": "входящие", "SENT": "отправленные"}
# раздел настроек → как подключаться и как подписывать чаты
ACCOUNTS = {
    "mail": {"secret": "MAIL_APP_PASSWORD", "host": "imap.yandex.ru", "prefix": "", "title": "Почта",
             "help": "Проверьте пароль приложения и что IMAP включён в настройках почты"},
    "gmail": {"secret": "GMAIL_APP_PASSWORD", "host": "imap.gmail.com", "prefix": "gmail:", "title": "Gmail",
              "help": "Нужен пароль приложения Google (myaccount.google.com/apppasswords), а не обычный пароль"},
}


class MailError(Exception):
    pass


class _Text(HTMLParser):
    BLOCK = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "table", "blockquote"}

    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("style", "script", "head"):
            self.skip += 1
        if tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("style", "script", "head") and self.skip:
            self.skip -= 1

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    p = _Text()
    p.feed(html)
    text = "".join(p.parts)
    text = re.sub(r"[ \t\xa0]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


QUOTE_MARKERS = [
    re.compile(r"^-{2,}\s*(Original Message|Исходное сообщение|Пересылаемое сообщение)", re.I),
    re.compile(r"^\d{1,2}[./]\d{1,2}[./]\d{2,4}.*(написал|wrote)", re.I),
    re.compile(r"^(On|В) .{5,80}(wrote|написал[аи]?)\s*:?\s*$", re.I),
    re.compile(r"^(From|От):\s.+", re.I),
]


def strip_quotes(text: str) -> str:
    out = []
    for line in text.splitlines():
        s = line.strip()
        if any(p.match(s) for p in QUOTE_MARKERS):
            break
        if s.startswith(">"):
            continue
        out.append(line)
    return "\n".join(out).strip()


def _decode(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:  # noqa: BLE001
        return value


def body_text(msg: email.message.Message) -> str:
    plain, html = None, None
    for part in msg.walk() if msg.is_multipart() else [msg]:
        if part.get_content_maintype() == "multipart" or part.get("Content-Disposition", "").startswith("attachment"):
            continue
        ctype = part.get_content_type()
        try:
            payload = part.get_payload(decode=True)
            if payload is None:
                continue
            text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        except (LookupError, AttributeError):
            continue
        if ctype == "text/plain" and plain is None:
            plain = text
        elif ctype == "text/html" and html is None:
            html = text
    text = plain if plain and plain.strip() else (html_to_text(html) if html else "")
    return strip_quotes(text)


def attachments(msg: email.message.Message) -> list[str]:
    names = []
    for part in msg.walk():
        fn = part.get_filename()
        if fn:
            names.append(_decode(fn))
    return names


def parse_message(raw: bytes, folder_key: str, my_email: str, account: str = "mail") -> dict:
    msg = email.message_from_bytes(raw)
    subject = _decode(msg.get("Subject"))
    from_name, from_addr = parseaddr(_decode(msg.get("From")))
    to = ", ".join(n or a for n, a in getaddresses([_decode(msg.get("To", "")), _decode(msg.get("Cc", ""))]) if a)
    try:
        ts = parsedate_to_datetime(msg.get("Date"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        ts = datetime.now(timezone.utc)
    body = body_text(msg)
    files = attachments(msg)
    text = f"Тема: {subject}\nКому: {to}\n\n{body}"
    if files:
        text += "\n\n[вложения: " + ", ".join(files) + "]"
    outgoing = folder_key == "SENT" or (my_email and from_addr.lower() == my_email.lower())
    acc = ACCOUNTS[account]
    return {
        "source": SOURCE,
        "chat_id": acc["prefix"] + folder_key,
        "chat_title": f"{acc['title']}: {FOLDER_TITLES.get(folder_key, folder_key)}",
        "msg_id": (msg.get("Message-ID") or "").strip() or f"{folder_key}:{ts.isoformat()}:{subject}",
        "sender_id": from_addr.lower(),
        "sender_name": "Я" if outgoing else (from_name or from_addr),
        "is_outgoing": outgoing,
        "text": text[:8000],
        "ts": store.to_utc_iso(ts),
        "reply_to": (msg.get("In-Reply-To") or "").strip() or None,
        "link": None,
        "_from": from_addr.lower(),
    }


def imap_utf7(name: str) -> str:
    """Имя папки в кодировке IMAP (modified UTF-7, RFC 3501): «Работа» → «&BCAEMAQxBD4EQgQw-»."""
    import base64

    out, buf = [], []

    def flush():
        if buf:
            data = "".join(buf).encode("utf-16-be")
            out.append("&" + base64.b64encode(data).decode().rstrip("=").replace("/", ",") + "-")
            buf.clear()

    for ch in name:
        if 0x20 <= ord(ch) <= 0x7E:
            flush()
            out.append("&-" if ch == "&" else ch)
        else:
            buf.append(ch)
    flush()
    return "".join(out)


LIST_RE = re.compile(r'\((?P<flags>[^)]*)\) "(?P<delim>[^"]*)" (?P<name>.+)')


def _folders(imap: imaplib.IMAP4_SSL) -> list[tuple[str, str]]:
    typ, data = imap.list()
    out = []
    for raw in data or []:
        line = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
        m = LIST_RE.match(line)
        if m:
            out.append((m.group("flags"), m.group("name").strip().strip('"')))
    return out


def _connect(settings: dict, account: str = "mail") -> imaplib.IMAP4_SSL:
    ms, acc = settings[account], ACCOUNTS[account]
    pwd = config.get_secret(acc["secret"])
    if not ms.get("email") or not pwd:
        raise MailError("Укажите адрес почты и пароль приложения")
    imap = imaplib.IMAP4_SSL(ms.get("host") or acc["host"], 993, timeout=60)
    try:
        imap.login(ms["email"], pwd)
    except imaplib.IMAP4.error as e:
        raise MailError(f"{acc['title']} не пустила: {e}. {acc['help']}") from e
    return imap


# Gmail: вкладка «Основные» — это всё, что не попало в другие вкладки. Прямой фильтр
# category:primary по IMAP не находит ничего (проверено на живом ящике), поэтому исключаем остальные
GMAIL_PRIMARY = '"-category:promotions -category:social -category:updates -category:forums"'


def _search(imap: imaplib.IMAP4_SSL, criteria: str, primary_only: bool) -> tuple:
    if primary_only:  # Gmail: только вкладка «Основные», без «Промоакций», «Соцсетей» и «Оповещений»
        try:
            return imap.uid("search", None, criteria, "X-GM-RAW", GMAIL_PRIMARY)
        except imaplib.IMAP4.error as e:
            store.log(SOURCE, f"Gmail: фильтр «Основные» не сработал ({e}) — читаю все входящие", "warning")
    return imap.uid("search", None, criteria)


def collect(settings: dict | None = None, account: str = "mail") -> str:
    settings = settings or config.load_settings()
    ms, prefix = settings[account], ACCOUNTS[account]["prefix"]
    ignore = [s.lower() for s in settings["mail"].get("ignore_senders") or []]  # список общий для обоих ящиков
    imap = _connect(settings, account)
    total = 0
    try:
        targets = [(f, f.upper() if f.upper() == "INBOX" else f) for f in ms.get("folders") or ["INBOX"]]
        if ms.get("include_sent"):
            sent = next((name for flags, name in _folders(imap) if "\\Sent" in flags), None)
            if sent:
                targets.append((sent, "SENT"))
        since = datetime.now() - timedelta(days=int(ms.get("backfill_days") or 3))
        months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
        backfill = f"{since.day:02d}-{months[since.month - 1]}-{since.year}"  # IMAP не понимает локализованные месяцы
        for folder, key in targets:
            # имена из LIST уже закодированы; написанные человеком («Работа») — кодируем
            encoded = folder if folder.isascii() else imap_utf7(folder)
            try:
                typ, _ = imap.select(f'"{encoded}"', readonly=True)
            except imaplib.IMAP4.error:
                typ = "NO"
            if typ != "OK":
                store.log(SOURCE, f"папка «{folder}» не найдена в почте", "warning")
                continue
            validity = (imap.response("UIDVALIDITY")[1] or [b"0"])[0]
            validity = validity.decode() if isinstance(validity, bytes) else str(validity)
            st = store.get_state(f"mail:{prefix}{key}") or {}
            primary = account == "gmail" and key == "INBOX" and ms.get("primary_only", True)
            if st.get("validity") == validity and st.get("uid"):
                typ, data = _search(imap, f"UID {int(st['uid']) + 1}:*", primary)
            else:
                typ, data = _search(imap, f"SINCE {backfill}", primary)
            uids = [int(u) for u in (data[0] or b"").split()] if data else []
            uids = [u for u in uids if u > int(st.get("uid") or 0) or st.get("validity") != validity]
            max_uid = int(st.get("uid") or 0) if st.get("validity") == validity else 0
            for uid in sorted(uids)[:300]:  # сначала старые: остальное заберём в следующий раз
                typ, parts = imap.uid("fetch", str(uid), "(BODY.PEEK[])")
                raw = next((p[1] for p in parts or [] if isinstance(p, tuple)), None)
                max_uid = max(max_uid, uid)
                if not raw:
                    continue
                msg = parse_message(raw, key, ms.get("email", ""), account)
                if key != "SENT" and any(s in msg["_from"] for s in ignore):
                    continue
                msg.pop("_from", None)
                store.upsert_chat(SOURCE, msg["chat_id"], msg["chat_title"], "mail", enabled=1)
                total += int(store.add_message(msg))
            store.set_state(f"mail:{prefix}{key}", {"validity": validity, "uid": max_uid})
    finally:
        try:
            imap.logout()
        except Exception:  # noqa: BLE001
            pass
    return f"новых писем: {total}"


def check(settings: dict | None = None, account: str = "mail") -> str:
    settings = settings or config.load_settings()
    imap = _connect(settings, account)
    try:
        names = [n for _, n in _folders(imap)]
        return f"вход выполнен, папок: {len(names)}"
    finally:
        imap.logout()


def collect_gmail(settings: dict | None = None) -> str:
    return collect(settings, "gmail")


def check_gmail(settings: dict | None = None) -> str:
    return check(settings, "gmail")
