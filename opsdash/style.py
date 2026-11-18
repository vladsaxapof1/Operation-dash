"""Стиль директора: как он пишет — карточка стиля, примеры прошлых ответов, сверка черновиков с отправленным.

Дообучить модель на переписке нельзя, поэтому голос директора передаётся контекстом: короткая карточка стиля плюс
3–5 подобранных прошлых ответов. Их берут черновики «нужен ответ» (навык triage), ответы в боте и «Спросить» и любые
агенты Claude через MCP-сервер PULSE (инструмент style).

- **Корпус** — исходящие сообщения директора за полгода в рабочих чатах: без чатов «Не запоминать»
  (chats.memory_off), каналов и ботов, без голосовых (это речь, а не письмо) и пометок вложений «[фото]».
- **Ответ** — исходящие подряд в одном чате с паузой до 5 минут: директор часто пишет несколькими короткими
  сообщениями. **На что ответ** — входящие прямо перед ним (или то, на что он ответил цитатой), не старше 12 часов.
- **Цифры** (stats) — код, без ИИ: длина, приветствия и прощания, «ты» или «вы» с людьми, обращение по имени, точки
  и смайлы, частые обороты (слова, которые директор пишет заметно чаще собеседников), чего не пишет совсем.
- **Карточка** — раз в неделю навык style пишет её текст по цифрам, образцам и сверке черновиков
  (`./ops style save`); хранится в style_cards и копией в data/knowledge/Стиль.md. Пока карточки от Claude нет,
  её собирает код из одних цифр (auto_card).
- **Примеры** — ответы этому человеку и в похожей ситуации: по словам (основы, редкие слова весят больше) и по
  смыслу (эмбеддинги работающего PULSE, как в «Вспомнить»). Пары с матом, личным (здоровье, семья) и похожие на
  секреты не показываются.
- **Сверка черновиков** — черновик «нужен ответ» (attention.draft) и что директор отправил следом в том же чате:
  без правок, с мелкими правками, переписан. Доля первых двух — качество черновиков; пары — материал для карточки.

Модуль только читает базу, кроме save_card: MCP-сервер открыт на чтение.
"""

from __future__ import annotations

import difflib
import math
import re
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from functools import lru_cache

from opsdash import config, store

FILE = "Стиль.md"
DAYS = 180                 # корпус: исходящие за полгода
BURST_GAP = 5 * 60         # сек: исходящие подряд с паузой меньше — один ответ
CONTEXT_GAP = 12 * 3600    # сек: входящее старше — уже не «на что ответ»
MIN_MESSAGES = 50          # меньше исходящих — карточку не собираем: по десятку сообщений стиль не понять
CARD_MAX = 4_500           # знаков: карточку читают перед каждым черновиком и агенты — коротко
CARD_MIN = 200
KEEP_CARDS = 20            # прежних карточек в базе
REPLY_WAIT = 3 * 86400     # сек: ответ директора в чате после черновика — не позже
SAME, MINOR, COVER = 0.9, 0.7, 0.85  # похожесть черновика и отправленного: без правок / мелкие правки / дописал к нему
FRESH = 30                 # новых исходящих с прошлой карточки Claude, ради которых стоит её переписать
EXAMPLE_MAX = 600          # знаков ответа в примере
PLAIN = 400                # знаков: длиннее — вставленный документ или отчёт, а не обычная переписка
MAX_PARTS = 6              # сообщений в ответе-образце: длиннее — уже разговор, а не ответ
INCOMING_MAX = 300         # знаков «на что ответ» в примере (агентам через MCP — 160)
DIRECT_KINDS = ("private", "mail")

_MEDIA = re.compile(r"\[(?:фото|изображение|видео|видеосообщение|стикер|переслано|файл(?::[^\]]*)?|вложения:[^\]]*)\]",
                    re.IGNORECASE)
_VOICE = "[голосовое сообщение]"
_WORD = re.compile(r"[a-zа-яё]+(?:-[a-zа-яё]+)?", re.IGNORECASE)
_EMOJI = re.compile("[\U0001F300-\U0001FAFF☀-➿]")
_SMILE = re.compile(r"(?<![(:;])\)+\s*$|:\)|;\)")
# мат: корни внутри слова («нихуя», «сьебываю») и в начале — там, где они не прячутся в обычных словах («сабля», «тебе»)
_RUDE_IN = re.compile(r"ху[йяеёию]|пизд|(?:за|на|у|съ|сь|вы|по|отъ|оть|разъ|разь|въ|вь|до|долбо|при|про)[её]б", re.I)
_RUDE_AT = re.compile(r"^(?:бля|[её]б[аулнт]|[её]бы|сук[аи]$|сучк|мудак|мудил|гандон|долбо)", re.I)
_RUDE_OK = {"хую", "хуюшки"}


class _Rude:
    """Есть ли в тексте мат — по словам: re.search(text) как у регулярного выражения."""

    @staticmethod
    def search(text: str | None) -> bool:
        for w in _WORD.findall(text or ""):
            low = w.lower()
            if low in _RUDE_OK or "психу" in low:  # «психую», «психуешь» — не мат
                continue
            if _RUDE_IN.search(low) or _RUDE_AT.search(low):
                return True
        return False


_RUDE = _Rude()
# личное (правило 9): здоровье, семья, питомцы — такие пары в образцы не берём, даже из рабочих чатов
_PERSONAL = re.compile(r"\b(?:больни\w*|болею|болеет|заболе\w*|врач\w*|доктор\w*|температур\w*|госпитал\w*|роддом\w*|"
                       r"похорон\w*|беремен\w*|реб[её]н\w*|детей|дет(?:ям|ьми|ях)|дочк\w*|дочь|сын(?:а|у|ом|е)?|жена|жены|"
                       r"жене|женой|муж(?:а|у|ем|е)?|мама|мамы|маме|мамой|папа|папы|папе|свадьб\w*|развод\w*|кот(?:а|у|ом|ик\w*)?|"
                       r"собак\w*|ветеринар\w*|лекарств\w*|анализы)\b", re.IGNORECASE)
GREET = re.compile(r"^(?:привет\w*|прив|приветик|здаров\w*|здорово|хай|ку|салют|hello|hi|"
                   r"добр(?:ый|ое|ого)\s+(?:день|утро|вечер|дня|утра|вечера)|здравствуй\w*|доброго\s+\w+)\b", re.IGNORECASE)
BYE = re.compile(r"(?:спасибо\w*|спс|благодарю|хорошего\s+(?:дня|вечера)|до\s+(?:связи|завтра)|пока|на\s+связи|обнял)"
                 r"[\s!.)\U0001F300-\U0001FAFF]*$", re.IGNORECASE)
TY = re.compile(r"\b(?:ты|тебе|тебя|тобой|твой|твоя|твоё|твое|твои|твоих|твоим|сможешь|можешь|скинь|пришли|глянь|"
                r"посмотри|напиши|сделай|подскажи|скажи|набери)\b", re.IGNORECASE)
VY = re.compile(r"\b(?:вы|вам|вас|вами|ваш|ваша|ваше|ваши|ваших|вашим|сможете|можете|скиньте|пришлите|гляньте|"
                r"посмотрите|напишите|сделайте|подскажите|скажите|наберите)\b", re.IGNORECASE)
_NAME_LEAD = re.compile(r"^[А-ЯЁA-Z][а-яёa-z]{1,12},\s+")
_COMMON_OPENERS = {"да", "нет", "ну", "так", "слушай", "смотри", "давай", "окей", "ок", "спасибо", "коллеги", "кстати",
                   "короче", "привет", "блин", "вот", "это", "там", "тут", "можно", "нужно", "надо", "думаю", "понял",
                   "хорошо", "неа", "ага", "ладно", "согласен", "а", "и", "но", "если", "когда", "уже", "пока"}
# то, что встречается в черновиках и деловой переписке, — сколько раз это пишет сам директор
MARKERS = [
    ("«Добрый день»", re.compile(r"\bдобрый день\b", re.I)),
    ("«Здравствуйте»", re.compile(r"\bздравствуйте\b", re.I)),
    ("«С уважением»", re.compile(r"\bс уважением\b", re.I)),
    ("«Коллеги»", re.compile(r"\bколлеги\b", re.I)),
    ("«Пожалуйста»", re.compile(r"\bпожалуйста\b", re.I)),
    ("«Подскажите»", re.compile(r"\bподскажите\b", re.I)),
    ("«Благодарю»", re.compile(r"\bблагодарю\b", re.I)),
    ("«Буду признателен»", re.compile(r"признател", re.I)),
    ("«Прошу»", re.compile(r"\bпрошу\b", re.I)),
    ("«Необходимо»", re.compile(r"\bнеобходим", re.I)),
    ("«Данный»", re.compile(r"\bданн(?:ый|ая|ое|ые|ого|ой|ом|ых|ую)\b", re.I)),
    ("«Также»", re.compile(r"\bтакже\b", re.I)),
    ("«Хорошего дня»", re.compile(r"хорошего дня", re.I)),
    ("тире «—»", re.compile("—")),
    ("буква «ё»", re.compile("[ёЁ]")),
    ("точка с запятой", re.compile(";")),
    ("восклицательный знак", re.compile("!")),
]


class StyleError(Exception):
    pass


# --- корпус -------------------------------------------------------------------------------------------------------

def clean(text: str | None) -> str:
    """Текст сообщения без пометок вложений и шапки письма; голосовое — пусто (это речь, а не письмо)."""
    t = (text or "").replace("\r", "")
    if t.startswith("Тема:"):  # письмо: «Тема: …\nКому: …\n\n<текст>»
        t = t.split("\n\n", 1)[1] if "\n\n" in t else ""
    if t.lstrip().startswith(_VOICE):
        return ""
    t = _MEDIA.sub(" ", t)
    t = "\n".join(re.sub(r"[ \t\xa0]+", " ", ln).strip() for ln in t.split("\n"))
    return re.sub(r"\n{3,}", "\n\n", t).strip()


def _since(days: int) -> str:
    return store.to_utc_iso(datetime.now(timezone.utc) - timedelta(days=days))


def _epoch(ts: str | None) -> float:
    dt = store.parse_ts(ts)
    return dt.timestamp() if dt else 0.0


_ALLOWED = ("COALESCE(c.memory_off, 0)=0 AND COALESCE(c.kind, '') NOT IN ('channel', 'bot')")


def timeline(days: int = DAYS) -> list[dict]:
    """Все сообщения рабочих чатов за days дней, по чатам и времени: из них — ответы и на что они."""
    return store.rows(
        "SELECT m.id, m.source, m.chat_id, m.chat_title, m.topic, m.msg_id, m.sender_name, m.is_outgoing, m.text, m.ts, "
        "m.reply_to, COALESCE(c.kind, '') AS kind, c.title AS title, c.username AS username "
        "FROM messages m LEFT JOIN chats c ON c.source=m.source AND c.chat_id=m.chat_id "
        f"WHERE m.ts >= ? AND {_ALLOWED} ORDER BY m.source, m.chat_id, m.ts, m.id", (_since(days),))


def outgoing_count(days: int = DAYS) -> int:
    return store.scalar("SELECT COUNT(*) FROM messages m LEFT JOIN chats c ON c.source=m.source AND c.chat_id=m.chat_id "
                        f"WHERE m.is_outgoing=1 AND m.ts >= ? AND {_ALLOWED} AND COALESCE(m.text, '') != ''",
                        (_since(days),)) or 0


def enough() -> bool:
    return outgoing_count() >= MIN_MESSAGES


def _person_index():
    """Кто из справочника за названием чата или именем отправителя (как под чатами в разборе сообщений)."""
    from opsdash import chat_tasks, records

    people = records.list_people()
    cache: dict = {}

    def find(label: str | None, username: str | None = None) -> dict | None:
        key = (label or "", username or "")
        if key not in cache:
            hits = chat_tasks._people_for(label or "", people, username) if (label or username) else []
            cache[key] = hits[0] if len(hits) == 1 else None
        return cache[key]

    return people, find


def pairs(rows: list[dict] | None = None, days: int = DAYS) -> list[dict]:
    """Ответы директора и на что они: исходящие подряд (пауза до BURST_GAP) — один ответ; входящие прямо перед ним —
    ситуация. В группе ситуация — сообщения последнего, кто писал перед ответом (или то, на что ответ цитатой)."""
    rows = timeline(days) if rows is None else rows
    _, find = _person_index()
    by_chat: dict[tuple, list[dict]] = defaultdict(list)
    for r in rows:
        by_chat[(r["source"], r["chat_id"], r.get("topic") or "")].append(r)
    # письмо и ответ на него лежат в разных папках («Входящие» и «Отправленные»): цитату ищем по всей почте
    mail = {m["msg_id"]: m for m in rows if m["source"] == "mail"}
    out = []
    for (source, chat_id, _topic), msgs in by_chat.items():
        ext = mail if source == "mail" else {m["msg_id"]: m for m in msgs}
        i, n = 0, len(msgs)
        while i < n:
            if not msgs[i]["is_outgoing"]:
                i += 1
                continue
            burst, j = [msgs[i]], i + 1
            while j < n and msgs[j]["is_outgoing"] and _epoch(msgs[j]["ts"]) - _epoch(burst[-1]["ts"]) <= BURST_GAP:
                burst.append(msgs[j])
                j += 1
            parts = [t for t in (clean(b["text"]) for b in burst) if t]
            if parts:
                out.append(_pair(source, chat_id, msgs, i, burst, parts, ext, find))
            i = j
    out.sort(key=lambda p: p["ts"])
    return out


def _pair(source, chat_id, msgs, i, burst, parts, ext, find) -> dict:
    first = burst[0]
    kind = first["kind"] or ("mail" if source == "mail" else "")
    t0 = _epoch(first["ts"])
    prev: list[dict] = []
    quoted = next((ext[b["reply_to"]] for b in burst if b.get("reply_to") in ext
                   and not ext[b["reply_to"]]["is_outgoing"]), None)
    if quoted and t0 - _epoch(quoted["ts"]) <= CONTEXT_GAP * 2:
        prev = [quoted]
    else:
        k = i - 1
        while k >= 0 and not msgs[k]["is_outgoing"] and len(prev) < 3 and t0 - _epoch(msgs[k]["ts"]) <= CONTEXT_GAP:
            prev.insert(0, msgs[k])
            k -= 1
        if prev and kind not in DIRECT_KINDS:  # в группе — только последний, кто писал
            last = prev[-1]["sender_name"]
            prev = [p for p in prev if p["sender_name"] == last]
    incoming = "\n".join(t for t in (clean(p["text"]) for p in prev) if t)
    title = first.get("title") or first.get("chat_title") or ""
    if kind == "private":
        label = title
        person = find(label, first.get("username"))
    else:  # группа и письмо: тот, кому отвечаем; папка почты — не человек
        label = prev[-1]["sender_name"] if prev else ""
        person = find(label) if label else None
    return {"source": source, "chat_id": chat_id, "chat": title, "kind": kind, "ts": first["ts"],
            "ids": [b["id"] for b in burst], "parts": parts, "reply": "\n".join(parts),
            "in_ids": [p["id"] for p in prev], "incoming": incoming,
            "who": person["name"] if person else label, "person_id": person["id"] if person else None}


# --- цифры --------------------------------------------------------------------------------------------------------

def _share(n: int, total: int) -> float:
    return round(n / total, 3) if total else 0.0


def _words(text: str) -> list[str]:
    return [w.lower().replace("ё", "е") for w in _WORD.findall(text or "")]


def _form(ty: int, vy: int) -> str:
    if ty >= 3 and ty >= 2 * vy:
        return "ты"
    if vy >= 3 and vy >= 2 * ty:
        return "вы"
    if ty + vy < 3:
        return "мало данных"
    return "по-разному"


def _names_of(person: dict | None, label: str) -> list[str]:
    names = [label] + ([person["name"], *(person.get("aliases") or [])] if person else [])
    return [w.lower().replace("ё", "е") for n in names for w in _WORD.findall(n or "") if len(w) >= 2]


def _by_name(text: str, names: list[str]) -> bool:
    """Начинается ли сообщение с обращения по имени собеседника: «Оль, …», «Пётр, …»."""
    m = re.match(r"^([А-ЯЁA-Z][а-яёa-z]{1,12}),\s", text)
    if not m:
        return False
    w = m.group(1).lower().replace("ё", "е").rstrip("ь")
    return w not in _COMMON_OPENERS and len(w) >= 2 and any(n.startswith(w[:3]) for n in names)


def stats(days: int = DAYS, rows: list[dict] | None = None) -> dict:
    """Цифры стиля по исходящим за days дней — без ИИ. Из них собирается карточка (навык style или auto_card)."""
    rows = timeline(days) if rows is None else rows
    ps = pairs(rows)
    msgs = [(r, t) for r in rows if r["is_outgoing"] and (t := clean(r["text"]))]
    st: dict = {"days": days, "messages": len(msgs), "replies": len(ps), "updated": store.utcnow()}
    if not msgs:
        return st
    texts = [t for _, t in msgs]
    lens = sorted(len(t) for t in texts)
    burst_lens = sorted(len(p["reply"]) for p in ps)
    st["chats"] = len({(r["source"], r["chat_id"]) for r, _ in msgs})
    st["length"] = {
        "median": int(statistics.median(lens)), "p90": lens[int(len(lens) * 0.9)],
        "short": _share(sum(1 for x in lens if x <= 30), len(lens)),
        "long": _share(sum(1 for x in lens if x > 300), len(lens)),
        "reply_median": int(statistics.median(burst_lens)) if burst_lens else 0,
        "multi": _share(sum(1 for p in ps if len(p["parts"]) > 1), len(ps)),
        "parts_avg": round(sum(len(p["parts"]) for p in ps) / len(ps), 1) if ps else 0,
    }
    st["punct"] = {
        "capital": _share(sum(1 for t in texts if t[:1].isupper()), len(texts)),
        "period": _share(sum(1 for t in texts if t.rstrip().endswith(".") and not t.rstrip().endswith("..")), len(texts)),
        "question": _share(sum(1 for t in texts if "?" in t), len(texts)),
        "emoji": _share(sum(1 for t in texts if _EMOJI.search(t)), len(texts)),
        "smile": _share(sum(1 for t in texts if _SMILE.search(t)), len(texts)),
        "lines": _share(sum(1 for t in texts if "\n" in t), len(texts)),
    }
    # приветствие — в первом ответе дня в чате; прощание — в конце ответа
    tz = config.tz()
    day_first: dict[tuple, dict] = {}
    for p in ps:
        key = (p["source"], p["chat_id"], store.parse_ts(p["ts"]).astimezone(tz).date())
        day_first.setdefault(key, p)
    greets = Counter()
    for p in day_first.values():
        m = GREET.match(p["parts"][0])
        if m:
            greets[m.group(0).lower()] += 1
    byes = Counter(m.group(0).strip(" !.)").lower() for p in ps if (m := BYE.search(p["parts"][-1])))
    st["greet"] = {"share": _share(sum(greets.values()), len(day_first)), "day_starts": len(day_first),
                   "forms": greets.most_common(6)}
    st["bye"] = {"share": _share(sum(byes.values()), len(ps)), "forms": byes.most_common(6)}
    st["short_replies"] = Counter(t.lower().strip(" .!") for t in texts if len(t) <= 20).most_common(15)
    plain = [t for t in texts if len(t) <= PLAIN]  # обороты и знаки — в обычных сообщениях, без вставленных текстов
    st["plain"] = len(plain)
    st["markers"] = [{"what": name, "n": (n := sum(1 for t in plain if rx.search(t))), "share": _share(n, len(plain))}
                     for name, rx in MARKERS]
    rude = [p for p in ps if _RUDE.search(p["reply"])]
    st["rude"] = {"share": _share(len(rude), len(ps)), "people": len({p["who"] for p in rude})}
    st["words"], st["phrases"] = _characteristic(rows)
    st["people"] = _people_stats(ps)
    named = [p for p in ps if p["kind"] in DIRECT_KINDS]
    people, _ = _person_index()
    by_id = {x["id"]: x for x in people}
    st["by_name"] = _share(sum(1 for p in named if _by_name(p["parts"][0], _names_of(
        by_id.get(p["person_id"]), f"{p['who']} {p['chat']}"))), len(named))
    st["draft_words"] = _draft_words(texts)
    st["drafts"] = draft_stats(30)
    st["drafts_diff"] = drafts_diff(60)
    return st


def _characteristic(rows: list[dict], top: int = 25) -> tuple[list, list]:
    """Слова и пары слов, которые директор пишет заметно чаще собеседников в тех же чатах (лог-отношение частот).
    Только обычные сообщения (до PLAIN знаков, без латиницы-кода и ссылок) и только то, что встречается хотя бы в трёх
    чатах: длинные вставленные тексты и слова одной темы — не стиль."""
    from opsdash import docs

    mine, theirs, mine2, theirs2 = Counter(), Counter(), Counter(), Counter()
    chats: dict[str, set] = defaultdict(set)
    for r in rows:
        text = clean(r["text"])
        if not text or len(text) > PLAIN or "http" in text:
            continue
        ws = _words(text)
        big = {f"{a} {b}" for a, b in zip(ws, ws[1:]) if not (a in docs.STOPWORDS and b in docs.STOPWORDS)}
        (mine if r["is_outgoing"] else theirs).update(set(ws))
        (mine2 if r["is_outgoing"] else theirs2).update(big)
        if r["is_outgoing"]:
            for w in set(ws) | big:
                chats[w].add(r["chat_id"])

    def rank(a: Counter, b: Counter, min_n: int) -> list:
        na, nb, v = sum(a.values()) or 1, sum(b.values()) or 1, len(a | b) or 1
        scored = [(w, n, math.log((n + 1) / (na + v)) - math.log((b[w] + 1) / (nb + v)))
                  for w, n in a.items() if n >= min_n and len(w) >= 2 and not _RUDE.search(w)
                  and len(chats[w]) >= 3 and not re.search("[a-z]", w)]
        scored.sort(key=lambda x: -x[2])
        return [(w, n) for w, n, s in scored[:top] if s > 0.5]

    return rank(mine, theirs, 5), rank(mine2, theirs2, 4)


def _people_stats(ps: list[dict], top: int = 15) -> list[dict]:
    """С кем как: «ты» или «вы», длина ответа, приветствие, смайлы — по личным чатам и письмам."""
    by: dict[str, list[dict]] = defaultdict(list)
    for p in ps:
        if p["kind"] in DIRECT_KINDS and p["who"]:
            by[p["who"]].append(p)
    out = []
    for who, items in sorted(by.items(), key=lambda kv: -len(kv[1]))[:top]:
        out.append(_person_line(who, items))
    return out


def _person_line(who: str, items: list[dict]) -> dict:
    texts = [t for p in items for t in p["parts"]]
    ty = sum(1 for t in texts if TY.search(t))
    vy = sum(1 for t in texts if VY.search(t))
    tz = config.tz()
    firsts: dict = {}
    for p in items:
        firsts.setdefault((p["chat_id"], store.parse_ts(p["ts"]).astimezone(tz).date()), p)
    return {"who": who, "replies": len(items), "ty": ty, "vy": vy, "form": _form(ty, vy),
            "median": int(statistics.median(len(p["reply"]) for p in items)),
            "multi": _share(sum(1 for p in items if len(p["parts"]) > 1), len(items)),
            "greet": _share(sum(1 for p in firsts.values() if GREET.match(p["parts"][0])), len(firsts)),
            "emoji": _share(sum(1 for t in texts if _EMOJI.search(t) or _SMILE.search(t)), len(texts)),
            "rude": any(_RUDE.search(t) for t in texts)}


def _draft_words(texts: list[str], top: int = 15) -> list[tuple[str, int]]:
    """Слова из черновиков PULSE, которых у самого директора нет ни разу: чего он не пишет."""
    mine = Counter(w for t in texts for w in _words(t))
    drafts = Counter(w for r in store.rows("SELECT draft FROM attention WHERE COALESCE(draft, '') != ''")
                     for w in set(_words(r["draft"])))
    return [(w, n) for w, n in drafts.most_common() if n >= 3 and len(w) >= 3 and not mine.get(w)][:top]


# --- сверка черновиков --------------------------------------------------------------------------------------------

def _norm(text: str) -> str:
    t = (text or "").lower().replace("ё", "е")
    return " ".join(re.findall(r"[a-zа-я0-9]+", t))


def _body(draft: str) -> str:
    """Черновик без обращения и приветствия в начале: «Анна, привет. Да, в силе» → «Да, в силе». Убрать их —
    мелкая правка, а не другой ответ."""
    t = _NAME_LEAD.sub("", (draft or "").strip(), count=1)
    m = GREET.match(t)
    if m:
        t = t[m.end():].lstrip(" ,.!)")
    return t or draft


def similarity(draft: str, sent: str) -> tuple[float, float]:
    """Похожесть черновика и отправленного (0–1) и какая доля черновика (без обращения и приветствия) вошла
    в отправленное."""
    a, b, body = _norm(draft), _norm(sent), _norm(_body(draft))
    if not a or not b:
        return 0.0, 0.0
    ratio = difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()
    sm = difflib.SequenceMatcher(None, body, b, autojunk=False)
    covered = sum(blk.size for blk in sm.get_matching_blocks())
    return round(ratio, 3), round(covered / (len(body) or 1), 3)


def verdict(ratio: float, cover: float, sent_len: int = 0, draft_len: int = 0) -> str:
    if ratio >= SAME:
        return "same"
    if ratio >= MINOR or (cover >= COVER and sent_len <= draft_len * 1.6 + 40):
        return "minor"
    return "rewritten"


VERDICTS = {"same": "без правок", "minor": "мелкие правки", "rewritten": "переписали", "none": "в чате не ответили"}


def _sent_after(a: dict) -> list[dict]:
    """Что директор отправил в том же чате после появления черновика: первый ответ (исходящие подряд)."""
    start, end = a["created"], store.to_utc_iso(store.parse_ts(a["created"]) + timedelta(seconds=REPLY_WAIT))
    if a["kind"] == "mail" or a["source"] == "mail":
        return store.rows("SELECT id, ts, text, is_outgoing FROM messages WHERE source=? AND is_outgoing=1 AND reply_to=? "
                          "AND ts > ? AND ts <= ? ORDER BY ts LIMIT 1", (a["source"], a["msg_ext"], start, end))
    rows = store.rows("SELECT id, ts, text, is_outgoing FROM messages WHERE source=? AND chat_id=? "
                      "AND COALESCE(topic, '')=? AND ts > ? AND ts <= ? ORDER BY ts, id LIMIT 40",
                      (a["source"], a["chat_id"], a.get("topic") or "", start, end))
    burst: list[dict] = []
    for r in rows:
        if r["is_outgoing"]:
            if burst and _epoch(r["ts"]) - _epoch(burst[-1]["ts"]) > BURST_GAP:
                break
            if clean(r["text"]):
                burst.append(r)
        elif burst:
            break
    return burst


def draft_checks(days: int | None = 60, limit: int | None = None) -> list[dict]:
    """Черновики «нужен ответ» и что ушло на самом деле: verdict — same | minor | rewritten | none."""
    where, params = "COALESCE(a.draft, '') != ''", []
    if days:
        where += " AND a.created >= ?"
        params.append(_since(days))
    rows = store.rows(
        "SELECT a.id, a.draft, a.created, a.status, a.sender_name, a.chat_title, m.source, m.chat_id, m.topic, "
        "m.msg_id AS msg_ext, m.text AS msg_text, COALESCE(c.kind, '') AS kind, COALESCE(c.memory_off, 0) AS off "
        "FROM attention a JOIN messages m ON m.id=a.message_id "
        "LEFT JOIN chats c ON c.source=m.source AND c.chat_id=m.chat_id "
        f"WHERE {where} ORDER BY a.created DESC" + (" LIMIT ?" if limit else ""), (*params, *([limit] if limit else [])))
    out = []
    for a in rows:
        sent = _sent_after(a)
        text = "\n".join(clean(s["text"]) for s in sent)
        if not text:
            out.append({**a, "sent": "", "sent_at": None, "ratio": 0.0, "cover": 0.0, "verdict": "none"})
            continue
        ratio, cover = similarity(a["draft"], text)
        out.append({**a, "sent": text, "sent_at": sent[0]["ts"], "sent_ids": [s["id"] for s in sent], "ratio": ratio,
                    "cover": cover, "verdict": verdict(ratio, cover, len(_norm(text)), len(_norm(a["draft"])))})
    return out


def draft_stats(days: int = 30, checks: list[dict] | None = None) -> dict:
    """Доля черновиков, ушедших без правок или с мелкими правками, — среди тех, на которые директор ответил в чате."""
    checks = draft_checks(days) if checks is None else checks
    n = Counter(c["verdict"] for c in checks)
    answered = n["same"] + n["minor"] + n["rewritten"]
    return {"days": days, "drafts": len(checks), "answered": answered, "same": n["same"], "minor": n["minor"],
            "rewritten": n["rewritten"], "none": n["none"],
            "share": round((n["same"] + n["minor"]) / answered, 3) if answered else None}


def drafts_diff(days: int = 60) -> dict:
    """Чем черновики отличаются от того, что директор отправил вместо них: длина, имя и приветствие, точка в конце."""
    done = [c for c in draft_checks(days) if c["verdict"] != "none"]
    if not done:
        return {}
    named = lambda t: bool(_NAME_LEAD.match(t))  # noqa: E731
    greets = lambda t: bool(GREET.match(_NAME_LEAD.sub("", t, count=1)))  # noqa: E731 — «Оль, привет» — тоже приветствие
    return {"pairs": len(done),
            "draft_len": int(statistics.median(len(c["draft"]) for c in done)),
            "sent_len": int(statistics.median(len(c["sent"]) for c in done)),
            "draft_named": _share(sum(1 for c in done if named(c["draft"])), len(done)),
            "sent_named": _share(sum(1 for c in done if named(c["sent"])), len(done)),
            "draft_greet": _share(sum(1 for c in done if greets(c["draft"])), len(done)),
            "sent_greet": _share(sum(1 for c in done if greets(c["sent"])), len(done)),
            "draft_period": _share(sum(1 for c in done if c["draft"].rstrip().endswith(".")), len(done)),
            "sent_period": _share(sum(1 for c in done if c["sent"].rstrip().endswith(".")), len(done)),
            "sent_multi": _share(sum(1 for c in done if len(c.get("sent_ids") or []) > 1), len(done))}


# --- примеры ------------------------------------------------------------------------------------------------------

@lru_cache(maxsize=100_000)
def _stem(word: str) -> str:
    from opsdash import recall

    return recall.stem(word) if len(word) >= 3 else word


def _stems(text: str) -> set[str]:
    """Основы слов для похожих ситуаций: основа «Вспомнить», обрезанная до 6 букв, — «согласуйте» и «согласуете»
    сходятся в «соглас», которые стеммер разводит."""
    from opsdash import recall

    return {_stem(w)[:6] for w in recall.words(text)}


def _good(p: dict) -> bool:
    """Ответ годится в образец: в нём есть слова, нет мата и секретов, он не вставленный документ."""
    from opsdash import memory

    r = p["reply"]
    return (12 <= len(r) <= 1500 and len(r.split()) >= 2 and len(p["parts"]) <= MAX_PARTS
            and not _RUDE.search(r) and not _RUDE.search(p["incoming"])
            and not _PERSONAL.search(r) and not _PERSONAL.search(p["incoming"])
            and not memory.looks_secret(r, p["incoming"]))


def _resolve(person: str):
    from opsdash import chat_tasks, records

    people = records.list_people()
    p = records.find_person(person)
    if not p:
        hits = chat_tasks._people_for(person, people)
        p = hits[0] if len(hits) == 1 else None
    return p


def _for_person(ps: list[dict], person: str) -> list[dict]:
    from opsdash import records

    p = _resolve(person)
    if p:
        return [x for x in ps if x["person_id"] == p["id"]]
    want = records._words(person)
    return [x for x in ps if want and want <= records._words(f"{x['who']} {x['chat']}")]


def _semantic_ranks(text: str) -> dict[int, int]:
    """Сообщения из фрагментов переписки, ближайших по смыслу (у работающего PULSE с моделью), — их место."""
    try:
        from opsdash import embed

        near = embed.neighbours(text, 50)
    except Exception:  # noqa: BLE001 — без модели и без PULSE — только слова
        near = None
    ids = (near or {}).get("chunks") or []
    if not ids:
        return {}
    import json

    rows = {r["id"]: r for r in store.rows(f"SELECT id, kind, msg_ids FROM memory_chunks WHERE id IN "
                                           f"({','.join('?' * len(ids))})", ids)}
    ranks: dict[int, int] = {}
    for rank, cid in enumerate(ids):
        r = rows.get(cid)
        if not r or r["kind"] != "msg":
            continue
        for mid in json.loads(r["msg_ids"] or "[]"):
            ranks.setdefault(int(mid), rank)
    return ranks


def _ranked(cands: list[dict], text: str) -> list[dict]:
    """Похожие ситуации: по словам (редкие весят больше) и по смыслу — слияние по местам, как в «Вспомнить»."""
    from opsdash import recall

    want = _stems(text)
    if not want:
        return []
    bags = [_stems(p["incoming"]) for p in cands]
    df = Counter(s for b in bags for s in b)
    n = len(cands) or 1
    idf = {s: math.log(1 + n / (1 + df[s])) for s in want}
    q_norm = math.sqrt(sum(idf.values())) or 1.0
    scored = []
    need = min(2, len(want))  # одно общее слово («будет», «готово») — ещё не похожая ситуация
    for i, (p, bag) in enumerate(zip(cands, bags)):
        common = want & bag
        if len(common) >= need:
            score = sum(idf[s] for s in common) / (q_norm * math.sqrt(len(bag) or 1))
            scored.append((score, i))
    scored.sort(key=lambda x: -x[0])
    words_arm = [i for _, i in scored[:60]]
    sem = _semantic_ranks(text)
    sem_scored = sorted(((min((sem[m] for m in p["in_ids"] + p["ids"] if m in sem), default=None), i)
                         for i, p in enumerate(cands)), key=lambda x: (x[0] is None, x[0] or 0))
    sem_arm = [i for r, i in sem_scored if r is not None][:60]
    order = recall.rrf(words_arm, sem_arm) if sem_arm else words_arm
    return [cands[i] for i in order]


def examples(person: str | None = None, text: str | None = None, limit: int = 4, days: int = DAYS) -> list[dict]:
    """3–5 прошлых ответов директора: этому человеку (похожие на text — первыми, иначе свежие) и в похожей ситуации
    с другими людьми. why — почему выбран."""
    limit = max(1, min(int(limit or 4), 8))
    ps = [p for p in pairs(days=days) if _good(p)]
    text = (text or "").strip()
    out: list[dict] = []
    seen: set[str] = set()

    def take(items: list[dict], why: str, k: int) -> None:
        for p in items:
            if len(out) >= limit or k <= 0:
                return
            key = _norm(p["reply"])
            if key in seen:
                continue
            seen.add(key)
            out.append({**p, "why": why})
            k -= 1

    mine = _for_person(ps, person) if person else []
    if mine:
        with_ctx = [p for p in mine if p["incoming"]]
        similar = _ranked(with_ctx, text) if text else []
        take(similar, "этому человеку, похожая ситуация", limit - 1 if text else limit)
        take(list(reversed(mine)), "этому человеку, недавно", limit - 1 if text else limit)
    if text:
        own = {p["ids"][0] for p in mine}
        others = [p for p in ps if p["incoming"] and p["ids"][0] not in own]
        take(_ranked(others, text), "похожая ситуация", limit)
    if not out and not person and not text:
        take(list(reversed(ps)), "недавно", limit)
    if len(out) < limit and mine:
        take(list(reversed(mine)), "этому человеку, недавно", limit)
    return out


def sample(people: int = 12, per: int = 3) -> list[dict]:
    """Образцы для навыка style: по нескольку свежих ответов людям, с которыми директор переписывается больше всего,
    — короткие и развёрнутые вперемешку."""
    ps = [p for p in pairs() if _good(p)]
    by: dict[str, list[dict]] = defaultdict(list)
    for p in ps:
        by[p["who"] or p["chat"]].append(p)
    out = []
    for who, items in sorted(by.items(), key=lambda kv: -len(kv[1]))[:people]:
        items = sorted(items, key=lambda p: p["ts"], reverse=True)[:40]
        picked = sorted(items[:per * 3], key=lambda p: len(p["reply"]))
        step = max(1, len(picked) // per)
        out += [{**p, "why": "образец"} for p in picked[::step][:per]]
    return out


# --- карточка -----------------------------------------------------------------------------------------------------

def card() -> dict | None:
    return store.row("SELECT * FROM style_cards ORDER BY id DESC LIMIT 1")


def path():
    return config.DATA_DIR / "knowledge" / FILE


def _pct(x: float | None) -> str:
    x = (x or 0) * 100
    if 0 < x < 1:
        return f"{x:.1f}%".replace(".", ",")
    return f"{round(x)}%"


def auto_card(st: dict) -> str:
    """Карточка из одних цифр — пока Claude не написал свою (навык style) или если его запуск не удался."""
    name = config.load_settings()["owner"].get("name") or "директор"
    if not st.get("messages"):
        return f"# Как пишет {name}\n\nИсходящих сообщений в рабочих чатах пока нет — стиль не известен."
    ln, pu, gr = st["length"], st["punct"], st["greet"]
    lines = [f"# Как пишет {name}", "",
             f"_Собрано кодом по {st['messages']} сообщениям за {st['days']} дней; текст карточки напишет Claude "
             "(навык style)._", "", "## Длина и ритм",
             f"- Сообщение — обычно {ln['median']} знаков, 9 из 10 короче {ln['p90']}; "
             f"{_pct(ln['short'])} — до 30 знаков.",
             f"- Ответ целиком — около {ln['reply_median']} знаков; несколькими сообщениями подряд — {_pct(ln['multi'])} "
             "ответов. Черновик — так же: коротко, при необходимости 2–3 короткие строки."]
    lines += ["", "## Приветствия, прощания, обращение",
              f"- Приветствие в первом ответе дня — {_pct(gr['share'])}"
              + (f": {', '.join(f'«{f}»' for f, _ in gr['forms'][:3])}" if gr["forms"] else "") + ".",
              f"- В конце ответа благодарит или прощается — {_pct(st['bye']['share'])}"
              + (f": {', '.join(f'«{f}»' for f, _ in st['bye']['forms'][:3])}" if st["bye"]["forms"] else "") + ".",
              f"- По имени в начале сообщения обращается в {_pct(st['by_name'])} личных ответов."]
    lines += ["", "## Знаки",
              f"- С заглавной буквы — {_pct(pu['capital'])} сообщений, точка в конце — {_pct(pu['period'])}, "
              f"смайлы и эмодзи — {_pct(max(pu['emoji'], pu['smile']))}."]
    forms = [p for p in st.get("people") or [] if p["form"] in ("ты", "вы")]
    if forms:
        ty = [p["who"] for p in forms if p["form"] == "ты"]
        vy = [p["who"] for p in forms if p["form"] == "вы"]
        lines += ["", "## «Ты» и «вы»"]
        if ty:
            lines.append(f"- На «ты»: {', '.join(ty[:12])}.")
        if vy:
            lines.append(f"- На «вы»: {', '.join(vy[:12])}.")
    if st.get("words") or st.get("phrases"):
        lines += ["", "## Частые слова и обороты",
                  "- " + ", ".join(f"«{w}»" for w, _ in (st["phrases"][:8] + st["words"][:12]))]
    never = [m["what"] for m in st.get("markers") or [] if m["share"] < 0.005]
    if never or st.get("draft_words"):
        lines += ["", "## Чего не пишет"]
        if never:
            lines.append(f"- Почти никогда: {', '.join(never)}.")
        if st.get("draft_words"):
            lines.append("- Было в черновиках PULSE, у самого — ни разу: "
                         + ", ".join(f"«{w}»" for w, _ in st["draft_words"][:10]) + ".")
    if st.get("rude", {}).get("share"):
        lines.append("- Мат в переписке бывает с близкими коллегами; в черновики его не переносить.")
    text = "\n".join(lines)
    return text if len(text) <= CARD_MAX else text[:CARD_MAX].rsplit("\n", 1)[0]


def person_line(person: str, ps: list[dict] | None = None) -> str:
    """Одна строка «как с этим человеком» — к карточке перед черновиком."""
    ps = pairs() if ps is None else ps
    items = [p for p in _for_person(ps, person) if p["kind"] in DIRECT_KINDS] or _for_person(ps, person)
    if not items:
        return f"{person}: переписки за полгода нет — опирайтесь на карточку и похожие ситуации."
    s = _person_line(items[-1]["who"], items)
    form = {"ты": "на «ты»", "вы": "на «вы»"}.get(s["form"], "«ты» или «вы» — по ситуации" if s["form"] == "по-разному"
                                                    else "«ты» или «вы» — не понять, мало сообщений")
    return (f"{s['who']}: {form}; ответов за полгода {s['replies']}, обычно {s['median']} знаков"
            + (f", несколькими сообщениями — {_pct(s['multi'])}" if s["multi"] >= 0.2 else "")
            + f"; приветствие в первом ответе дня — {_pct(s['greet'])}"
            + ("; смайлы бывают" if s["emoji"] >= 0.05 else "") + ".")


def card_text(person: str | None = None) -> str:
    """Карточка стиля (от Claude или из цифр) и, если назван человек, — как с ним."""
    c = card()
    if c:
        head = f"_Карточка стиля от {store.local(c['created'], '%d.%m')}"
        head += " (Claude)._" if c["author"] == "claude" else " (по цифрам)._"
        text = f"{c['text'].strip()}\n\n{head}"
    else:
        st = stats()
        if st["messages"] < MIN_MESSAGES:
            return (f"Карточки стиля пока нет: исходящих сообщений в рабочих чатах за полгода {st['messages']}, "
                    f"нужно хотя бы {MIN_MESSAGES}.")
        text = auto_card(st)
    if person:
        text += "\n\n## С этим человеком\n- " + person_line(person)
    return text


def save_card(text: str, *, author: str = "claude", stats_: dict | None = None) -> int:
    """Сохранить карточку в базу и копией в data/knowledge/Стиль.md. Прежние — последние KEEP_CARDS."""
    from opsdash import memory

    text = (text or "").strip()
    if len(text) < CARD_MIN:
        raise StyleError("Карточка слишком короткая — в ней должно быть, как директор пишет: длина, приветствия, "
                         "«ты» и «вы», обороты, чего не пишет")
    if len(text) > CARD_MAX:
        raise StyleError(f"Карточка длиннее {CARD_MAX} знаков ({len(text)}) — сократите: её читают перед каждым "
                         "черновиком")
    if why := memory.looks_secret(text):
        raise StyleError(f"не сохраняю: {why}")
    if author not in ("claude", "auto"):
        raise StyleError("author — claude или auto")
    st = stats_ if stats_ is not None else {}
    cid = store.execute("INSERT INTO style_cards(text, stats, author, messages, created) VALUES(?,?,?,?,?)",
                        (text, store.json_col(st), author, st.get("messages") or outgoing_count(), store.utcnow()))
    store.execute("DELETE FROM style_cards WHERE id <= ?", (cid - KEEP_CARDS,))
    export(text, author)
    return cid


def export(text: str, author: str) -> None:
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    by = "Claude по вашей переписке" if author == "claude" else "по цифрам из вашей переписки"
    p.write_text(f"{text.strip()}\n\n---\n_Карточка стиля PULSE, {config.now().strftime('%d.%m.%Y')}, {by}. Её читают "
                 "черновики ответов и агенты (MCP-инструмент style). Прежние версии — в базе PULSE._\n",
                 encoding="utf-8")


def stale() -> bool:
    """Стоит ли переписывать карточку Claude: её нет, или с неё набралось FRESH новых исходящих."""
    c = store.row("SELECT created FROM style_cards WHERE author='claude' ORDER BY id DESC LIMIT 1")
    if not c:
        return True
    n = store.scalar("SELECT COUNT(*) FROM messages m LEFT JOIN chats c ON c.source=m.source AND c.chat_id=m.chat_id "
                     f"WHERE m.is_outgoing=1 AND m.ts > ? AND {_ALLOWED}", (c["created"],)) or 0
    return n >= FRESH


# --- тексты для ./ops и MCP --------------------------------------------------------------------------------------

def _cut(text: str, n: int, *, lines: bool = False) -> str:
    """Текст в n знаков. lines — переносы строк сохранить (у директора они — часть стиля), иначе — « / »."""
    rows = [" ".join(ln.split()) for ln in (text or "").split("\n") if ln.strip()]
    text = ("\n" if lines else " / ").join(rows)
    return text if len(text) <= n else text[:n - 1].rstrip(" ,.;/") + "…"


def _block(text: str, indent: str) -> str:
    return text.replace("\n", "\n" + indent)


def _src(p: dict) -> str:
    from opsdash import overview

    where = overview.SOURCE_TITLES.get(p["source"], p["source"])
    kind = {"private": "личный чат", "mail": "письмо"}.get(p["kind"], f"«{p['chat']}»" if p["chat"] else "группа")
    return f"{where}, {kind}"


def _plural(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return one
    return few if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14 else many


def example_lines(items: list[dict], *, incoming_max: int = INCOMING_MAX) -> list[str]:
    out = []
    for n, p in enumerate(items, 1):
        out.append(f"{n}. {store.local(p['ts'], '%d.%m.%y')} · {p['who'] or p['chat']} ({_src(p)}) · {p['why']}")
        if p["incoming"]:
            out.append(f"   Вам: «{_cut(p['incoming'], incoming_max)}»")
        if len(p["parts"]) == 1:
            out.append(f"   Вы: «{_block(_cut(p['reply'], EXAMPLE_MAX, lines=True), '        ')}»")
        else:
            out.append(f"   Вы ({len(p['parts'])} {_plural(len(p['parts']), 'сообщение', 'сообщения', 'сообщений')} "
                       "подряд):")
            left = EXAMPLE_MAX
            for part in p["parts"]:
                if left <= 0:
                    out.append("     › …")
                    break
                out.append(f"     › {_block(_cut(part, left, lines=True), '       ')}")
                left -= len(part)
    return out


def examples_text(person: str | None = None, text: str | None = None, limit: int = 4, *, mcp: bool = False) -> str:
    items = examples(person, text, limit)
    if not items:
        who = f" «{person}»" if person else ""
        return (f"Прошлых ответов{who} не нашлось" + (" в похожей ситуации" if text else "")
                + " — пишите по карточке стиля.")
    head = "Как вы отвечали раньше" + (f" — {person}" if person else "") + (" и в похожих ситуациях" if text else "") + ":"
    tail = ("Берите из примеров форму — длину, обращение, приветствие, обороты; факты — из этой ситуации."
            if not mcp else "Из примеров бери форму — длину, обращение, приветствие, обороты; факты — из своей задачи.")
    return "\n".join([head, *example_lines(items, incoming_max=160 if mcp else INCOMING_MAX), "", tail])


def facts_text(st: dict | None = None) -> str:
    """Цифры стиля для навыка style — коротко, до ~12 КБ."""
    st = st or stats()
    if not st.get("messages"):
        return "Исходящих сообщений в рабочих чатах нет."
    ln, pu = st["length"], st["punct"]
    out = [f"Исходящих за {st['days']} дней: {st['messages']} сообщений в {st['chats']} чатах, ответов {st['replies']} "
           "(исходящие подряд с паузой до 5 минут — один ответ).",
           "", "## Длина",
           f"- сообщение: медиана {ln['median']} знаков, 90% короче {ln['p90']}; до 30 знаков — {_pct(ln['short'])}; "
           f"длиннее 300 — {_pct(ln['long'])}",
           f"- ответ: медиана {ln['reply_median']} знаков; несколькими сообщениями — {_pct(ln['multi'])}, "
           f"в среднем {ln['parts_avg']} сообщения",
           "", "## Знаки",
           f"- с заглавной {_pct(pu['capital'])} · точка в конце {_pct(pu['period'])} · вопрос {_pct(pu['question'])} · "
           f"эмодзи {_pct(pu['emoji'])} · скобка-смайл {_pct(pu['smile'])} · несколько строк {_pct(pu['lines'])}",
           "", "## Приветствия и прощания",
           f"- приветствие в первом ответе дня в чате: {_pct(st['greet']['share'])} из {st['greet']['day_starts']}; "
           + (", ".join(f"«{f}» {n}" for f, n in st["greet"]["forms"]) or "форм нет"),
           f"- благодарность или прощание в конце ответа: {_pct(st['bye']['share'])}; "
           + (", ".join(f"«{f}» {n}" for f, n in st["bye"]["forms"]) or "форм нет"),
           f"- обращение по имени в начале личного ответа: {_pct(st['by_name'])}",
           "", "## Короткие ответы целиком (до 20 знаков)",
           "- " + ", ".join(f"«{t}» {n}" for t, n in st["short_replies"]),
           "", "## Слова и обороты, которые директор пишет заметно чаще собеседников",
           "- пары слов: " + (", ".join(f"«{w}» {n}" for w, n in st["phrases"]) or "нет"),
           "- слова: " + (", ".join(f"«{w}» {n}" for w, n in st["words"]) or "нет"),
           "", f"## Деловые обороты и знаки — сколько раз у директора (в {st['plain']} обычных сообщениях до {PLAIN} знаков)",
           "- " + ", ".join(f"{m['what']} {m['n']} ({_pct(m['share'])})" for m in st["markers"])]
    if st.get("draft_words"):
        out += ["- было в черновиках PULSE, у директора — ни разу: "
                + ", ".join(f"«{w}» ({n} черн.)" for w, n in st["draft_words"])]
    out += [f"- мат: {_pct(st['rude']['share'])} ответов, с {st['rude']['people']} людьми (в карточку слова не "
            "выписывай)", "", "## С кем как (личные чаты и письма; ты/вы — сколько сообщений с такими формами)"]
    for p in st["people"]:
        out.append(f"- {p['who']}: ответов {p['replies']} · ты {p['ty']} / вы {p['vy']} → {p['form']} · медиана "
                   f"{p['median']} знаков · несколькими сообщениями {_pct(p['multi'])} · приветствие "
                   f"{_pct(p['greet'])} · смайлы {_pct(p['emoji'])}" + (" · бывает мат" if p["rude"] else ""))
    ds, dd = st.get("drafts") or {}, st.get("drafts_diff") or {}
    out += ["", "## Черновики PULSE → что ушло на самом деле", drafts_line(ds)]
    if dd:
        out.append(f"- за 60 дней пар {dd['pairs']}: длина черновика {dd['draft_len']} знаков → отправлено "
                   f"{dd['sent_len']}; имя в начале {_pct(dd['draft_named'])} → {_pct(dd['sent_named'])}; приветствие "
                   f"{_pct(dd['draft_greet'])} → {_pct(dd['sent_greet'])}; точка в конце {_pct(dd['draft_period'])} → "
                   f"{_pct(dd['sent_period'])}; несколькими сообщениями ушло {_pct(dd['sent_multi'])}")
    return "\n".join(out)


def drafts_line(ds: dict) -> str:
    if not ds or not ds.get("drafts"):
        return "- черновиков за 30 дней нет"
    if not ds["answered"]:
        return f"- черновиков за {ds['days']} дней {ds['drafts']}, ответов следом в том же чате нет — сравнивать не с чем"
    return (f"- за {ds['days']} дней черновиков {ds['drafts']}, ответили следом в том же чате на {ds['answered']}: "
            f"без правок {ds['same']}, с мелкими правками {ds['minor']} — {_pct(ds['share'])}; переписали "
            f"{ds['rewritten']}")


def drafts_text(days: int = 60, limit: int = 25) -> str:
    """Пары «черновик → отправлено» для навыка style и для директора (./ops style drafts)."""
    checks = draft_checks(days)
    ds = draft_stats(days, checks)
    out = [drafts_line(ds).lstrip("- ").capitalize()]
    shown = [c for c in checks if c["verdict"] != "none"][:limit]
    for c in shown:
        who = c["sender_name"] or c["chat_title"] or "?"
        out += ["", f"{store.local(c['created'], '%d.%m')} · {who} · {VERDICTS[c['verdict']]} "
                    f"(похожесть {str(c['ratio']).replace('.', ',')})",
                f"  вам: «{_cut(clean(c['msg_text']), 200)}»",
                f"  черновик: «{_cut(c['draft'], 400)}»",
                f"  отправили: «{_cut(c['sent'], 400)}»"]
    if not shown:
        out.append("Пар «черновик → отправлено» нет.")
    return "\n".join(out)


def sample_text(people: int = 12, per: int = 3) -> str:
    items = sample(people, per)
    if not items:
        return "Ответов для образцов нет."
    return "\n".join(["Образцы ответов директора людям, с которыми он переписывается больше всего:",
                      *example_lines(items, incoming_max=200)])
