"""Команда ./ops — единый вход в PULSE для Claude и для человека.

Примеры:
  ./ops overview                         сводка «что сейчас важно»
  ./ops messages pending                 новые сообщения для разбора
  ./ops commitments list --direction to_me
  ./ops propose card --title "..." --responsible "Иванова" --due 2026-10-30
Полный список: ./ops --help и ./ops <команда> --help
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

from opsdash import config, records, store


def out(text: str = "") -> None:
    print(text)


def as_json(data) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2, default=str))


def fail(msg: str, code: int = 1) -> None:
    print(f"Ошибка: {msg}", file=sys.stderr)
    sys.exit(code)


def short(text: str | None, n: int = 600) -> str:
    text = (text or "").replace("\r", "")
    return text if len(text) <= n else text[: n - 1] + "…"


def parse_source(value: str | None) -> tuple[str, str | None]:
    """'meeting:2026-10-25_1400_x' → ('meeting', '2026-...')."""
    if not value:
        return "manual", None
    kind, _, ref = value.partition(":")
    return kind, (ref or None)


# --- сервис -------------------------------------------------------------------

def cmd_daemon(a) -> None:
    from opsdash import daemon

    daemon.run(open_browser=a.open)


def start_background() -> None:
    """Запустить PULSE в фоне: на Mac — через службу launchd (если установлена), иначе отдельным процессом."""
    from opsdash import macos

    if macos.is_mac() and macos.kickstart():
        return
    exe = Path(sys.executable)
    pyw = exe.with_name("pythonw.exe")
    kwargs = {"cwd": str(config.APP_DIR)}
    if os.name == "nt":
        kwargs["creationflags"] = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        cmd = [str(pyw if pyw.exists() else exe), "-m", "opsdash", "daemon"]
    else:
        kwargs["start_new_session"] = True
        cmd = [str(exe), "-m", "opsdash", "daemon"]
    subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kwargs)


def cmd_start(a) -> None:
    from opsdash import daemon

    if daemon.is_running():
        out(f"PULSE уже работает: {daemon.url()}")
        return
    start_background()
    out(f"PULSE запускается: {daemon.url()}")


def cmd_stop(a) -> None:
    import httpx

    from opsdash import daemon

    try:
        httpx.post(daemon.url() + "/api/shutdown", timeout=10, headers={"Origin": daemon.url()})
        out("PULSE остановлен")
    except httpx.ReadTimeout:  # запрос дошёл, но PULSE занят — остановится, как только ответит
        out("PULSE занят (например, расшифровкой) и не ответил вовремя — остановится, как освободится")
    except httpx.HTTPError:
        out("PULSE не запущен")


def cmd_open(a) -> None:
    """Открыть дашборд; если PULSE не запущен — сначала запустить и дождаться."""
    import time
    import webbrowser

    from opsdash import daemon

    if not daemon.is_running():
        start_background()
        for _ in range(40):
            time.sleep(0.5)
            if daemon.is_running():
                break
    webbrowser.open(daemon.url())


def cmd_mac(a) -> None:
    from opsdash import macos

    if not macos.is_mac():
        fail("эта команда — только для macOS")
    if a.action == "install":
        out(f"Служба автозапуска: {macos.install_agent()}")
        try:
            out(f"Приложение: {macos.make_app()}")
        except macos.MacError as e:
            out(f"Приложение не создано ({e}) — PULSE можно открывать файлом START.command")
    elif a.action == "uninstall":
        macos.uninstall_agent()
        macos.remove_old_apps()  # все наши копии: обе папки программ, прежнее название
        out("Автозапуск и приложение удалены (данные в папке PULSE остались)")
    elif a.action == "permissions":
        paths = macos.protected_paths()
        out("Python PULSE (его добавляют в «Полный доступ к диску», если macOS не спросила сама):")
        out(f"  {macos.real_python()}")
        if paths:
            out("Папки, к которым macOS спросит доступ:")
            for p in paths:
                out(f"  {p}")
        else:
            out("Защищённых папок (Документы, Рабочий стол, Загрузки, iCloud) в настройках нет — разрешения не нужны.")
        if a.open:
            macos.open_privacy_settings()
    elif a.action == "status":
        out(f"Служба: {'установлена' if macos.agent_installed() else 'нет'} ({macos.plist_path()})")
        apps = macos.installed_apps()
        out(f"Приложение: {', '.join(map(str, apps)) if apps else 'нет'}")


def cmd_status(a) -> None:
    from opsdash import daemon, jobs, overview

    out(f"PULSE: {'работает — ' + daemon.url() if daemon.is_running() else 'не запущен'}")
    for h in overview.sources_health():
        mark = {"ok": "✓", "error": "✗", "off": "–", "pending": "…"}[h["status"]]
        line = f"  {mark} {h['title']}"
        if h["last_ok"]:
            line += f" (последний сбор {store.local(h['last_ok'])})"
        if h["status"] == "error":
            line += f" — {short(h['error'], 200)}"
        out(line)
    for j in store.rows("SELECT * FROM jobs ORDER BY last_run DESC LIMIT 15"):
        state = "идёт" if j["running"] else ("ошибка" if j["last_error"] else "ок")
        out(f"  {jobs.TITLES.get(j['name'], j['name'])}: {store.local(j['last_run'])} · {state}")


def cmd_check(a) -> None:
    from opsdash import bot, brain
    from opsdash.sources import calendar, kaiten, mail, pachca, telegram, ymessenger

    checks = {
        "claude": brain.check, "telegram": telegram.check, "ymessenger": ymessenger.check, "pachca": pachca.check,
        "mail": mail.check, "gmail": mail.check_gmail, "calendar": calendar.check,
        "google_calendar": calendar.google_check, "kaiten": kaiten.check,
        "bot": lambda: "бот @" + bot.get_bot().me().get("username", "?"),
    }
    names = list(checks) if a.what == "all" else [a.what]
    ok = True
    for n in names:
        try:
            out(f"✓ {n}: {checks[n]()}")
        except Exception as e:  # noqa: BLE001
            ok = False
            out(f"✗ {n}: {e}")
    if not ok:
        sys.exit(1)


def cmd_collect(a) -> None:
    from opsdash import jobs

    settings = config.load_settings()
    names = (["telegram", "ymessenger", "pachca", "mail", "gmail", "calendar", "google_calendar", "kaiten"]
             if a.what == "all" else [a.what])
    for n in names:
        if a.what == "all" and not settings[n].get("enabled"):
            continue
        try:
            out(f"{n}: {jobs.run_job('collect:' + n)}")
        except Exception as e:  # noqa: BLE001
            out(f"{n}: ошибка — {e}")


def cmd_run(a) -> None:
    from opsdash import jobs

    args = {}
    if a.meeting:
        args["meeting_id"] = a.meeting
    if a.job == "triage":
        args["force"] = True
    out(jobs.run_job(a.job, args))


def cmd_overview(a) -> None:
    from opsdash import overview

    out(overview.overview_markdown())


# --- сообщения ------------------------------------------------------------------

def _msg_line(m: dict, mark: str = "", limit: int = 1500) -> str:
    who = m["sender_name"] or "?"
    topic = f" в теме «{m['topic']}»" if m.get("topic") else ""
    reply = f" (ответ на {m['reply_to']})" if m.get("reply_to") and m["source"] != "mail" else ""
    return f"[{m['id']}]{mark} {store.local(m['ts'])} {who}{topic}{reply}: {short(m['text'], limit)}"


def cmd_messages(a) -> None:
    if a.action == "pending":
        sql = "SELECT * FROM messages WHERE processed=0"
        params: list = []
        if a.source:
            sql += " AND source=?"
            params.append(a.source)
        sql += " ORDER BY ts LIMIT ?"
        params.append(a.limit)
        items = store.rows(sql, params)
        total = store.scalar("SELECT COUNT(*) FROM messages WHERE processed=0") or 0
        store.set_state("last_pending_ids", [m["id"] for m in items])
        if a.json:
            return as_json(items)
        if not items:
            return out("Новых сообщений нет.")
        out(f"Новых сообщений: {total}, показано: {len(items)} (после разбора: ./ops messages done --shown)")
        chats: dict[tuple, list] = {}
        for m in items:
            chats.setdefault((m["source"], m["chat_id"], m["chat_title"]), []).append(m)
        for (source, chat_id, title), msgs in chats.items():
            kind = store.scalar("SELECT kind FROM chats WHERE source=? AND chat_id=?", (source, chat_id)) or ""
            out(f"\n## {source} · «{title}» ({kind}, chat_id={chat_id}) — новых: {len(msgs)}")
            if a.context:
                ctx = store.rows(
                    "SELECT * FROM messages WHERE source=? AND chat_id=? AND processed=1 AND ts < ? ORDER BY ts DESC LIMIT ?",
                    (source, chat_id, msgs[0]["ts"], a.context),
                )
                for m in reversed(ctx):
                    out(_msg_line(m, " (контекст)"))
            for m in msgs:
                out(_msg_line(m))
    elif a.action == "done":
        ids = []
        if a.shown:
            ids = store.get_state("last_pending_ids") or []
        elif a.ids:
            ids = [int(x) for x in a.ids.split(",") if x.strip()]
        elif a.upto:
            n = store.execute("UPDATE messages SET processed=1 WHERE processed=0 AND id<=?", (a.upto,))
            return out(f"Отмечено разобранными: {n}")
        if not ids:
            fail("укажите --shown, --ids или --upto")
        marks = ",".join("?" for _ in ids)
        n = store.execute(f"UPDATE messages SET processed=1 WHERE id IN ({marks})", ids)
        out(f"Отмечено разобранными: {n}")
    elif a.action == "search":
        sql = "SELECT * FROM messages WHERE text LIKE ? AND ts >= ?"
        params = [f"%{a.query}%", store.to_utc_iso(config.now() - timedelta(days=a.days))]
        if a.source:
            sql += " AND source=?"
            params.append(a.source)
        if a.chat:
            sql += " AND (chat_title LIKE ? OR topic LIKE ?)"
            params += [f"%{a.chat}%"] * 2
        sql += " ORDER BY ts DESC LIMIT ?"
        params.append(a.limit)
        items = store.rows(sql, params)
        if a.json:
            return as_json(items)
        if not items:
            return out("Ничего не нашлось.")
        for m in items:
            out(f"{m['source']} · «{m['chat_title']}» " + _msg_line(m))
    elif a.action == "show":
        m = store.row("SELECT * FROM messages WHERE id=?", (a.id,))
        if not m:
            fail(f"нет сообщения {a.id}")
        around = store.rows(
            "SELECT * FROM (SELECT * FROM messages WHERE source=? AND chat_id=? AND ts<=? ORDER BY ts DESC LIMIT ?) "
            "UNION SELECT * FROM (SELECT * FROM messages WHERE source=? AND chat_id=? AND ts>? ORDER BY ts LIMIT ?) ORDER BY ts",
            (m["source"], m["chat_id"], m["ts"], a.context + 1, m["source"], m["chat_id"], m["ts"], a.context),
        )
        out(f"{m['source']} · «{m['chat_title']}»" + (f" · {m['link']}" if m["link"] else ""))
        for x in around:
            out(_msg_line(x, " ◀" if x["id"] == m["id"] else "", limit=6000))
    elif a.action == "groups":
        from opsdash import digests

        chats = digests.group_messages(a.hours)
        if not chats:
            return out(f"В рабочих группах за {a.hours} ч сообщений нет.")
        for c in chats:
            out(f"\n## «{c['title']}» ({c['source']}) — сообщений: {len(c['messages'])}")
            for m in c["messages"][-a.limit:]:
                out(_msg_line(m))
    elif a.action == "chat":
        sql = "SELECT * FROM messages WHERE (chat_title LIKE ? OR topic LIKE ?) AND ts >= ? ORDER BY ts DESC LIMIT ?"
        since = store.to_utc_iso(config.now() - timedelta(days=a.days))
        items = list(reversed(store.rows(sql, (f"%{a.title}%", f"%{a.title}%", since, a.limit))))
        if not items:
            return out("Сообщений нет.")
        for m in items:
            out(f"«{m['chat_title']}» " + _msg_line(m))


def cmd_briefs(a) -> None:
    from opsdash import briefs

    try:
        if a.action == "pending":
            items = briefs.pending(limit=a.limit)
            if not items:
                return out("Новых встреч для брифа нет.")
            for e in items:
                out(f"- {e['uid']} · {store.local(e['start'], '%d.%m %H:%M')} · {e['title']} · файл: {briefs.file_for(e)}")
        elif a.action == "context":
            out(briefs.context(a.uid))
        elif a.action == "set":
            path = Path(a.file)
            if not path.is_absolute():  # Claude пишет от папки программы; данные обычно там же
                path = next((base / path for base in (config.ROOT, config.APP_DIR) if (base / path).exists()),
                            config.ROOT / path)
            briefs.save(a.uid, path.read_text(encoding="utf-8"), topic=a.topic)
            out(f"Бриф сохранён: {a.uid}")
        elif a.action == "skip":
            briefs.skip(a.uid, a.why)
            out(f"Без брифа: {a.uid} — {a.why}")
        elif a.action == "show":
            b = briefs.get(a.uid)
            if not b:
                return out("Брифа нет.")
            out(f"{b['title']} · {b['status']}" + (f" · тема: {b['topic']}" if b["topic"] else ""))
            out(b["text"] or b["why"] or "")
    except (ValueError, OSError) as e:
        fail(str(e))


def cmd_week(a) -> None:
    from datetime import date

    from opsdash import digests

    try:
        day = date.fromisoformat(a.date) if a.date else None
    except ValueError:
        fail("дата — в виде ГГГГ-ММ-ДД")
    out(digests.week_stats(day))


def cmd_chats(a) -> None:
    sql = "SELECT * FROM chats WHERE 1=1"
    params = []
    if a.source:
        sql += " AND source=?"
        params.append(a.source)
    if a.enabled:
        sql += " AND enabled=1"
    for c in store.rows(sql + " ORDER BY source, last_ts DESC", params):
        flag = {1: "читаю", 0: "—", -1: "исключён"}.get(c["enabled"], "?")
        out(f"{c['source']} · {c['chat_id']} · «{c['title']}» ({c['kind']}) · {flag}")


# --- встречи -------------------------------------------------------------------

def cmd_docs(a) -> None:
    from opsdash import docs, jobs

    if a.action == "search":
        items = docs.search(a.query, limit=a.limit, ext=("." + a.type.lstrip(".")) if a.type else None)
        if a.json:
            return as_json([{k: v for k, v in d.items() if k != "modified"} for d in items])
        for d in items:
            where = f" · {d['part']}" if d["part"] else ""
            more = f" (ещё: {', '.join(d['more'])})" if d["more"] else ""
            out(f"{d['rel']}{where} · {d['type']} · изм. {store.local(d['modified'], '%d.%m.%Y')}{more}\n"
                f"   {short(d['snippet'], 400)}")
        if not items:
            out("Ничего не найдено. Попробуйте другие слова или ./ops docs status.")
    elif a.action == "show":
        d = docs.find(a.file)
        if not d:
            fail(f"Нет такого документа в индексе: {a.file} (путь, путь внутри папки или имя файла — ./ops docs search)")
        out(f"# {d['rel']}\nПуть: {d['path']} · {docs.TYPE_TITLES.get(d['ext'], d['ext'])} · частей: {d['parts']}")
        if d["status"] == "error":
            out(f"Не прочитан: {d['error']}")
        left = a.max_chars
        for p in docs.parts(d["path"]):
            if a.part and a.part.lower() not in (p["part"] or "").lower():
                continue
            text = p["body"][:left]
            out(f"\n## {p['part'] or 'текст'}\n{text}")
            left -= len(text)
            if left <= 0:
                out(f"\n… обрезано на {a.max_chars} символах — уточните часть: --part \"слайд 5\"")
                break
    elif a.action == "recent":
        items = docs.recent(days=a.days, limit=a.limit)
        for d in items:
            out(f"{store.local(d['modified'], '%d.%m %H:%M')} · {d['rel']} · {d['type']}")
        if not items:
            out(f"За {a.days} дн. документы не менялись.")
    elif a.action == "status":
        s = docs.stats()
        out(f"Папки: {', '.join(str(f) for f in docs.folders()) or 'не выбраны'}")
        out(f"В индексе: {s['indexed']} ({', '.join(f'{t}: {n}' for t, n in s['types'])}); не прочитано: {s['errors']}")
        if s.get("last_ok"):
            out(f"Обновлён: {store.local(s['last_ok'])}")
        if s.get("last_error"):
            out(f"Ошибка последнего обновления: {s['last_error']}")
    elif a.action == "index":
        out(jobs.run_job("docs-index"))


def cmd_kb(a) -> None:
    from opsdash import anthology

    if a.action == "add":
        source_type, _, ref = (a.source or "manual").partition(":")
        try:
            item_id = anthology.add(
                kind=a.kind, title=a.title, details=a.details or "", happened=a.date, project=a.project,
                people=[p for p in (a.people or "").split(",") if p.strip()], source_type=source_type,
                source_ref=ref or None, quote=a.quote, status="proposed" if a.proposed else "accepted",
                author="claude" if os.environ.get("OPSDASH_AGENT") else "director")
        except anthology.AnthologyError as e:
            fail(str(e))
        out(f"Запись [{item_id}] сохранена" + (" как предложение — директор примет или отклонит" if a.proposed else ""))
    elif a.action in ("list", "search"):
        items = anthology.items(kind=a.kind, status=a.status, project=a.project, days=getattr(a, "days", None),
                                q=getattr(a, "query", None), limit=a.limit)
        if a.json:
            return as_json(items)
        for r in items:
            meta = " · ".join(x for x in (r["happened"], r["project"], r["status_title"] if r["status"] != "accepted" else "") if x)
            src = f" · источник {r['source_type']}:{r['source_ref'] or ''}" if r["source_type"] != "manual" else ""
            out(f"[{r['id']}] {r['kind_title']}: {r['title']} ({meta}){src}")
            if r["details"] and a.details:
                out("    " + short(r["details"].replace("\n", " "), 500))
        if not items:
            out("Записей нет.")
    elif a.action == "set":
        if not anthology.get(a.id):
            fail(f"нет записи {a.id}")
        anthology.update(a.id, status=a.status, note=a.note, title=a.title, details=a.details)
        out("Сохранено")
    elif a.action == "import":
        out(anthology.import_notes())


def cmd_meetings(a) -> None:
    if a.action == "list":
        items = records.list_meetings(limit=a.limit, days=a.days)
        if a.json:
            return as_json(items)
        for m in items:
            out(f"{m['id']} · {store.local(m['started_at'])} · {m['title']} · {m['status']}"
                + (f" — {m['headline']}" if m.get("headline") else ""))
        if not items:
            out("Встреч нет.")
    elif a.action == "show":
        m = records.get_meeting(a.id)
        if not m:
            fail(f"нет встречи {a.id}")
        folder = records.meeting_dir(a.id)
        out(f"# {m['title']} ({m['id']})\nСтатус: {m['status']} · начало {store.local(m['started_at'])} · папка {folder}")
        summary = records.read_meeting_file(a.id, "summary.md")
        out("\n" + (summary or "(разбора ещё нет)"))
        if a.transcript:
            out("\n" + records.read_meeting_file(a.id, "transcript.md"))
    elif a.action == "set":
        if not records.get_meeting(a.id):
            fail(f"нет встречи {a.id}")
        participants = [p.strip() for p in a.participants.split(",")] if a.participants else None
        records.update_meeting(a.id, title=a.title, status=a.status, headline=a.headline, participants=participants)
        out("Сохранено")
    elif a.action == "add":
        from opsdash import recordings

        p = Path(a.file).expanduser().resolve()
        if not p.exists():
            fail(f"нет файла {p}")
        mid = recordings.add_file(p, a.title)
        out(f"Встреча создана: {mid}. Расшифровка: ./ops run transcribe --meeting {mid}")
    elif a.action == "search":
        q = a.query.lower()
        hits = 0
        for m in records.list_meetings(limit=1000):
            for name in ("summary.md", "transcript.md"):
                text = records.read_meeting_file(m["id"], name)
                for line in text.splitlines():
                    if q in line.lower():
                        out(f"{m['id']}/{name}: {short(line.strip(), 300)}")
                        hits += 1
                        if hits >= a.limit:
                            return
        if not hits:
            out("Ничего не нашлось.")
    elif a.action == "guess":
        from opsdash import voice

        folder = records.meeting_dir(a.id)
        if not (folder / "meta.json").exists():
            fail(f"нет расшифровки встречи {a.id}")
        meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
        if a.speaker not in (meta.get("speakers") or []):
            fail(f"в записи нет «{a.speaker}»; спикеры: {', '.join(meta.get('speakers') or []) or 'не размечены'}")
        if a.speaker in voice.speaker_map(meta)[0]:
            return out(f"«{a.speaker}» уже подписан (по голосу или директором) — догадка не нужна")
        meta.setdefault("speaker_guesses", {})[a.speaker] = {"name": a.name.strip(), "why": a.why.strip()}
        (folder / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        out(f"Записала: {a.speaker} — вероятно, {a.name}. Директор подтвердит на странице встречи.")


# --- люди ----------------------------------------------------------------------

def _person_line(p: dict) -> str:
    bits = [f"[{p['id']}] {p['name']}"]
    if p.get("role"):
        bits.append(p["role"])
    if p.get("aliases"):
        bits.append("также: " + ", ".join(p["aliases"]))
    for k, label in (("telegram", "tg"), ("ymessenger", "ям"), ("email", "почта"), ("kaiten_user_id", "kaiten")):
        if p.get(k):
            bits.append(f"{label}: {p[k]}")
    return " · ".join(bits)


def cmd_people(a) -> None:
    if a.action == "list":
        items = records.list_people()
        if a.json:
            return as_json(items)
        for p in items:
            out(_person_line(p))
        if not items:
            out("Справочник пуст.")
    elif a.action == "find":
        p = records.find_person(a.query)
        if p:
            out(_person_line(p))
        else:
            hits = [p for p in records.list_people() if a.query.lower() in json.dumps(p, ensure_ascii=False).lower()]
            for p in hits:
                out(_person_line(p))
            if not hits:
                out("Не найден.")
    elif a.action == "set":
        pid = records.upsert_person(
            a.name, person_id=a.id, role=a.role, aliases=a.alias or [], telegram=a.telegram, ymessenger=a.ymessenger,
            email=a.email, kaiten_user_id=a.kaiten_user, notes=a.notes,
        )
        out(f"Сохранено: [{pid}] {a.name}")


# --- обещания ------------------------------------------------------------------

def _commitment_line(c: dict) -> str:
    d = {"mine": "я →", "to_me": "мне ←", "other": "между"}[c["direction"]]
    title = f"«{c['title']}» " if c.get("title") else ""
    line = f"[{c['id']}] {d} {c['who'] or '?'} → {c['whom'] or '?'}: {title}{c['what']}"
    if c["due"]:
        line += f" · до {c['due']}" + (" (ПРОСРОЧЕНО)" if c.get("overdue") else "")
    if c["status"] != "open":
        line += f" · {c['status']}"
    if c.get("in_work"):
        line += " · в работе"
    if c.get("archived_at"):
        line += " · в архиве"
    if c["kaiten_card_id"]:
        line += f" · kaiten #{c['kaiten_card_id']}"
    if c["source_type"]:
        line += f" · источник {c['source_type']}:{c['source_ref'] or ''}"
    if c.get("parent"):
        line += f" · часть задачи [{c['parent']['id']}] «{c['parent']['headline']}» ({c['parent']['route']})"
    if c.get("children"):
        line += " · подзадачи: " + ", ".join(f"[{k['id']}] {k['route']}" + (f" ({k['status']})" if k["status"] != "open" else "")
                                            for k in c["children"])
    if c.get("done_hint"):
        line += " · предложено закрыть, ждёт директора"
    return line


def cmd_commitments(a) -> None:
    if a.action == "list":
        items = records.list_commitments(status=a.status, direction=a.direction, person=a.person, overdue=a.overdue)
        if a.json:
            return as_json(items)
        for c in items:
            out(_commitment_line(c))
        if not items:
            out("Обещаний нет.")
    elif a.action == "add":
        st, ref = parse_source(a.source)
        try:
            cid = records.add_commitment(
                direction=a.direction, what=a.what, who=a.who, whom=a.whom, due=a.due, source_type=st, source_ref=ref,
                quote=a.quote, kaiten_card_id=a.card, note=a.note, title=a.title, parent_id=a.parent,
            )
        except ValueError as e:
            fail(str(e))
        out(f"Обещание [{cid}] сохранено" + (f" как часть задачи [{a.parent}]" if a.parent else ""))
    elif a.action == "set":
        try:
            records.update_commitment(a.id, status=a.status, due=a.due, kaiten_card_id=a.card, note=a.note, what=a.what,
                                      title=a.title, who=a.who, whom=a.whom, parent_id=a.parent)
        except ValueError as e:
            fail(str(e))
        if a.in_work:
            records.set_in_work(a.id, a.in_work == "on")
        if a.archived:
            records.set_archived(a.id, a.archived == "on")
        out("Сохранено")


# --- нужен ответ ----------------------------------------------------------------

def cmd_attention(a) -> None:
    if a.action == "list":
        items = records.list_attention("all" if a.all else "open")
        if a.json:
            return as_json(items)
        for x in items:
            marks = ""
            if x["status"] == "open" and x["reopened_at"]:
                marks += f" · вернула вручную {store.local(x['reopened_at'])}"
            if x["done_hint"]:
                marks += " · предложено закрыть, ждёт директора"
            out(f"[{x['id']}] {x['priority']} · {x['status']}{marks} · {x['source']} · «{x['chat_title']}» · "
                f"{x['sender_name']}: {x['summary']} (сообщение {x['message_id']})"
                + (f"\n    черновик: {short(x['draft'], 300)}" if x["draft"] else ""))
            if x["status"] == "open":
                for r in records.replies_after(x):
                    out(f"    после вопроса вы писали [{r['id']}] {store.local(r['ts'])}"
                        f"{' (ответ на это сообщение)' if r['is_reply'] else ''}: {short(r['text'], 200)}")
        if not items:
            out("Никто не ждёт ответа.")
    elif a.action == "add":
        aid = records.add_attention(summary=a.summary, message_id=a.message, why=a.why, draft=a.draft, priority=a.priority)
        out(f"Добавлено [{aid}]")
    elif a.action == "close":
        records.close_attention(a.id, "dismissed" if a.dismiss else "done")
        out("Закрыто")


# --- Kaiten --------------------------------------------------------------------

def cmd_kaiten(a) -> None:
    from opsdash.sources import kaiten

    if a.action == "sync":
        out(kaiten.collect())
    elif a.action == "cards":
        if a.stale:
            items = kaiten.stale_cards()
        else:
            sql = "SELECT * FROM kaiten_cards WHERE condition=1"
            params: list = []
            if not a.all_states:
                sql += " AND state IN (1,2)"
            if a.space:
                sql += " AND space_id=?"
                params.append(a.space)
            if a.who:
                sql += " AND (responsible_name LIKE ? OR members LIKE ?)"
                params += [f"%{a.who}%", f"%{a.who}%"]
            if a.search:
                sql += " AND (title LIKE ? OR description LIKE ?)"
                params += [f"%{a.search}%", f"%{a.search}%"]
            if a.mine:
                me = store.get_state("kaiten_me") or {}
                sql += " AND (responsible_id=? OR owner_id=? OR members LIKE ?)"
                params += [me.get("id"), me.get("id"), f"%\"id\": {me.get('id')}%"]
            sql += " ORDER BY updated DESC LIMIT ?"
            params.append(a.limit)
            items = store.rows(sql, params)
        if a.json:
            return as_json(items)
        state = {1: "очередь", 2: "в работе", 3: "готово"}
        for c in items[: a.limit]:
            line = (f"#{c['id']} {c['title']} · {c['space_title'] or ''}/{c['board_title'] or ''} · «{c['column_title']}» "
                    f"({state.get(c['state'], c['state'])}) · {c['responsible_name'] or 'без исполнителя'}")
            if c.get("due_date"):
                line += f" · срок {c['due_date']}"
            if c.get("idle_days") is not None:
                line += f" · без движения {c['idle_days']} дн."
            out(line + f" · {c['url']}")
        if not items:
            out("Карточек нет (обновить: ./ops kaiten sync).")
    elif a.action == "card":
        c = store.row("SELECT * FROM kaiten_cards WHERE id=?", (a.id,))
        if a.fresh or not c:
            kc = kaiten.client()
            raw = kc.card(a.id)
            c = kaiten.normalize_card(raw, kc, c["space_id"] if c else None, c["space_title"] if c else None)
        out(f"#{c['id']} {c['title']}\n{c['url']}\n{c['space_title']}/{c['board_title']} · «{c['column_title']}» · "
            f"исполнитель: {c['responsible_name'] or '—'} · срок: {c['due_date'] or '—'}\n"
            f"обновлена {store.local(c['updated'])}, перемещена {store.local(c['last_moved_at'])}\n\n{short(c['description'], 3000)}")
    elif a.action == "users":
        sql = "SELECT * FROM kaiten_users"
        params = []
        if a.query:
            sql += " WHERE full_name LIKE ? OR username LIKE ? OR email LIKE ?"
            params = [f"%{a.query}%"] * 3
        for u in store.rows(sql + " ORDER BY full_name", params):
            out(f"{u['id']} · {u['full_name']} · @{u['username']} · {u['email'] or ''}")
    elif a.action == "boards":
        sql = "SELECT * FROM kaiten_boards" + (" WHERE space_id=?" if a.space else "")
        for b in store.rows(sql, [a.space] if a.space else []):
            cols = ", ".join(f"{c['title']}={c['id']}" for c in store.loads(b["columns"], []))
            out(f"доска {b['id']} «{b['title']}» (пространство {b['space_id']}): колонки {cols}")
    elif a.action == "targets":
        settings = config.load_settings()
        for t in kaiten.targets(settings):
            default = " (по умолчанию)" if t["name"] == settings["kaiten"].get("default_target") else ""
            out(f"«{t['name']}»{default}: пространство {t.get('space_id')}, доска {t.get('board_id')}, колонка {t.get('column_id')}")
        if not kaiten.targets(settings):
            out("Направления не настроены (админка → Kaiten → Куда создавать карточки).")


def cmd_calendar(a) -> None:
    from datetime import datetime, time, timedelta

    from opsdash import overview

    tzinfo = config.tz()
    start = datetime.combine(
        datetime.fromisoformat(a.date).date() if a.date else config.now().date(), time.min, tzinfo)
    items = overview.events_between(start, start + timedelta(days=a.days))
    if a.json:
        return as_json(items)
    for e in items:
        who = ", ".join(x.get("name") or x.get("email") for x in e["attendees"][:10])
        out(f"{store.local(e['start'], '%d.%m %H:%M')}–{store.local(e['end'], '%H:%M')} {e['title']}"
            + (f" · {who}" if who else "") + f" · uid {e['uid']}")
    if not items:
        out("Встреч нет.")


# --- предложения ------------------------------------------------------------------

def cmd_propose(a) -> None:
    from opsdash import proposals

    st, ref = parse_source(a.source) if a.source else (None, None)
    source_ref = f"{st}:{ref}" if ref else None
    try:
        if a.kind == "card":
            p = proposals.propose_kaiten_card(
                title=a.title, description=a.description or "", due=a.due, responsible=a.responsible, target=a.target,
                commitment_id=a.commitment, batch=a.batch, reason=a.reason, source_ref=source_ref)
        elif a.kind == "comment":
            p = proposals.propose_kaiten_comment(card_id=a.card, text=a.text, batch=a.batch, reason=a.reason,
                                                 source_ref=source_ref)
        elif a.kind == "update":
            p = proposals.propose_kaiten_update(card_id=a.card, due=a.due, title=a.title, responsible=a.responsible,
                                                column_id=a.column, batch=a.batch, reason=a.reason, source_ref=source_ref)
        elif a.kind == "meeting":
            p = proposals.propose_calendar_event(title=a.title, start=a.start, end=a.end, attendees=a.attendee or [],
                                                 description=a.description or "", calendar=a.calendar or "",
                                                 batch=a.batch, reason=a.reason, source_ref=source_ref)
        elif a.kind == "cancel-meeting":
            p = proposals.propose_calendar_cancel(uid=a.uid, batch=a.batch, reason=a.reason, source_ref=source_ref)
        elif a.kind == "done":
            p = proposals.propose_task_done(commitment_id=a.commitment, quote=a.quote, batch=a.batch, reason=a.reason,
                                            source_ref=source_ref)
        elif a.kind == "answered":
            p = proposals.propose_answered(attention_id=a.attention, quote=a.quote, batch=a.batch, reason=a.reason,
                                           source_ref=source_ref)
        elif a.kind == "remind":
            p = proposals.propose_reminder(commitment_id=a.commitment, text=a.text, batch=a.batch, reason=a.reason,
                                           source_ref=source_ref)
        else:
            fail("неизвестный тип")
    except proposals.ProposalError as e:
        fail(str(e))
    status = {"pending": "ждёт подтверждения директора", "done": "выполнено сразу (автономия)", "failed": "ошибка"}
    out(f"Предложение [{p['id']}] {p['title']} — {status.get(p['status'], p['status'])}")
    if p["status"] in ("done", "failed"):
        out(proposals.result_text(p))


def cmd_proposals(a) -> None:
    from opsdash import proposals

    if a.action == "list":
        items = proposals.list_proposals("all" if a.all else "pending", batch=a.batch)
        if a.json:
            return as_json(items)
        for p in items:
            out(f"[{p['id']}] {p['status']} · {p['title']}" + (f" · пачка {p['batch']}" if p["batch"] else ""))
        if not items:
            out("Нет предложений.")
    elif a.action == "approve":
        if os.environ.get("OPSDASH_AGENT"):
            fail("в фоновом режиме подтверждать нельзя — решение принимает директор (кнопка в боте или на сводке)")
        if not a.batch and not a.id:
            fail("укажите номер предложения или --batch")
        results = proposals.approve_batch(a.batch) if a.batch else [proposals.approve(a.id)]
        for p in results:
            out(f"[{p['id']}] {p['title']}: {proposals.result_text(p)}")
    elif a.action == "reject":
        p = proposals.reject(a.id)
        out(f"[{p['id']}] отклонено")


def cmd_notify(a) -> None:
    from opsdash import bot

    prefs = config.load_settings()["notifications"]
    if a.kind == "meeting" and not prefs.get("meeting_processed", True):
        return out("Не отправлено: итоги встреч выключены в настройках (видно на дашборде)")
    if a.kind == "urgent" and not prefs.get("urgent_immediately", True):
        return out("Не отправлено: срочные уведомления выключены — попадёт в дайджест")
    text = a.text.replace("\\n", "\n")  # «\n» из командной строки — перенос строки
    if a.kind in ("digest", "weekly", "groups"):  # дайджесты — ещё и в раздел «Дайджесты»
        from opsdash import digests

        digests.save(digests.daily_kind() if a.kind == "digest" else a.kind, text)
        if a.kind == "groups" and not prefs.get("groups_digest"):
            return out("Сохранено в разделе «Дайджесты» (в Telegram сводка групп не отправляется — так в настройках)")
    oid = bot.notify(text, batch=a.batch, urgent=a.urgent or a.kind == "urgent")
    row = store.row("SELECT sent_at, error FROM outbox WHERE id=?", (oid,))
    if row and row["sent_at"]:
        out("Отправлено в Telegram")
    else:
        out("Поставлено в очередь (бот не настроен, тихие часы или нет связи) — видно на дашборде")


def cmd_demo(a) -> None:
    from opsdash import demo

    if a.clear:
        demo.clear()
        out("Демо-данные удалены")
    else:
        demo.seed()
        out("Демо-данные добавлены — откройте дашборд")


def cmd_version(a) -> None:
    from opsdash import __version__, updates

    sha = updates.installed()["sha"]
    out(f"PULSE {__version__}{' · ' + sha[:7] if sha else ''} · {config.ROOT}")


def cmd_update(a) -> None:
    """Проверить новую версию в GitHub и поставить её (данные, настройки и пароли не трогаются)."""
    from opsdash import daemon, updates

    try:
        res = updates.check()
    except updates.UpdateError as e:
        fail(f"не удалось проверить обновления: {e}")
    cur, latest = res["installed"], res["latest"]
    out(f'Стоит: {cur["sha"][:7] or "неизвестно"} · последняя в GitHub: {latest["sha"][:7]} '
        f'от {store.local(latest["date"], "%d.%m %H:%M")} — {latest["message"]}')
    if not res["available"]:
        return out("Обновлять нечего.")
    for line in res["changes"]:
        out(f"  • {line}")
    if a.check:
        return out("Поставить: ./ops update")
    try:
        out(f"Обновлено: {updates.apply()}")
    except updates.UpdateError as e:
        fail(f"обновление не удалось: {e}")
    if not a.no_restart and daemon.is_running():
        updates.restart(in_daemon=False)
        out("PULSE перезапускается — через полминуты откройте его снова.")


# --- разбор аргументов -------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ops", description="PULSE — пульт операционного директора. Флаг --json — вывод в JSON.")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("daemon", help="запустить PULSE в этом окне")
    s.add_argument("--open", action="store_true", help="открыть браузер")
    s.set_defaults(fn=cmd_daemon)
    sub.add_parser("start", help="запустить PULSE в фоне").set_defaults(fn=cmd_start)
    sub.add_parser("stop", help="остановить PULSE").set_defaults(fn=cmd_stop)
    sub.add_parser("open", help="открыть дашборд в браузере (запустит PULSE, если нужно)").set_defaults(fn=cmd_open)
    s = sub.add_parser("mac", help="macOS: автозапуск, приложение, права доступа")
    s.add_argument("action", choices=["install", "uninstall", "permissions", "status"])
    s.add_argument("--open", action="store_true", help="открыть настройки «Полный доступ к диску»")
    s.set_defaults(fn=cmd_mac)
    sub.add_parser("status", help="состояние источников и заданий").set_defaults(fn=cmd_status)
    s = sub.add_parser("check", help="проверить подключения")
    s.add_argument("what", nargs="?", default="all",
                   choices=["all", "claude", "telegram", "ymessenger", "pachca", "mail", "gmail", "calendar",
                            "google_calendar", "kaiten", "bot"])
    s.set_defaults(fn=cmd_check)
    s = sub.add_parser("collect", help="собрать данные сейчас")
    s.add_argument("what", nargs="?", default="all",
                   choices=["all", "telegram", "ymessenger", "pachca", "mail", "gmail", "calendar", "google_calendar",
                            "kaiten"])
    s.set_defaults(fn=cmd_collect)
    s = sub.add_parser("run", help="выполнить задание сейчас")
    s.add_argument("job", choices=["triage", "digest", "stale", "recordings", "transcribe", "process-meeting", "housekeeping",
                            "docs-index", "anthology", "company-context"])
    s.add_argument("--meeting", help="id встречи (для transcribe и process-meeting)")
    s.set_defaults(fn=cmd_run)
    sub.add_parser("overview", help="сводка: что сейчас важно").set_defaults(fn=cmd_overview)

    s = sub.add_parser("messages", help="сообщения из Telegram, Я.Мессенджера, Пачки и почты")
    ms = s.add_subparsers(dest="action", required=True)
    x = ms.add_parser("pending", help="новые (неразобранные) сообщения")
    x.add_argument("--limit", type=int, default=200)
    x.add_argument("--source", choices=["telegram", "ymessenger", "pachca", "mail"])
    x.add_argument("--context", type=int, default=3, help="сколько прежних сообщений чата показать для контекста")
    x = ms.add_parser("done", help="отметить разобранными")
    x.add_argument("--shown", action="store_true", help="все, что показала последняя команда pending")
    x.add_argument("--ids", help="через запятую")
    x.add_argument("--upto", type=int)
    x = ms.add_parser("search", help="поиск по тексту")
    x.add_argument("query")
    x.add_argument("--days", type=int, default=60)
    x.add_argument("--source")
    x.add_argument("--chat", help="часть названия чата или темы")
    x.add_argument("--limit", type=int, default=50)
    x = ms.add_parser("show", help="сообщение с контекстом")
    x.add_argument("id", type=int)
    x.add_argument("--context", type=int, default=8)
    x = ms.add_parser("groups", help="сообщения рабочих групп за последние часы — для сводки групп")
    x.add_argument("--hours", type=int, default=24)
    x.add_argument("--limit", type=int, default=150, help="не больше стольких последних сообщений на группу")
    x = ms.add_parser("chat", help="последние сообщения чата (или темы группы) по названию")
    x.add_argument("title")
    x.add_argument("--days", type=int, default=7)
    x.add_argument("--limit", type=int, default=100)
    s.set_defaults(fn=cmd_messages)

    s = sub.add_parser("briefs", help="брифы к встречам из календаря")
    ms = s.add_subparsers(dest="action", required=True)
    x = ms.add_parser("pending", help="встречи, к которым нужен бриф")
    x.add_argument("--limit", type=int, default=5)
    x = ms.add_parser("context", help="что известно к встрече: участники, прошлые встречи, задачи, решения, файлы")
    x.add_argument("uid")
    x = ms.add_parser("set", help="сохранить бриф из файла (Markdown)")
    x.add_argument("uid")
    x.add_argument("--file", required=True, help="файл с текстом брифа (путь из ./ops briefs pending)")
    x.add_argument("--topic", help="тема встречи одной строкой")
    x = ms.add_parser("skip", help="бриф не нужен: тема не ясна или это не встреча")
    x.add_argument("uid")
    x.add_argument("--why", required=True)
    x = ms.add_parser("show")
    x.add_argument("uid")
    s.set_defaults(fn=cmd_briefs)

    s = sub.add_parser("week", help="данные для итогов недели: встречи, задачи, решения, загрузка")
    s.add_argument("--date", help="любой день нужной недели, ГГГГ-ММ-ДД (по умолчанию — эта неделя)")
    s.set_defaults(fn=cmd_week)

    s = sub.add_parser("chats", help="список чатов")
    s.add_argument("--source")
    s.add_argument("--enabled", action="store_true")
    s.set_defaults(fn=cmd_chats)

    s = sub.add_parser("docs", help="рабочие документы: поиск по папкам из настроек (Word, Excel, презентации, PDF…)")
    ms = s.add_subparsers(dest="action", required=True)
    x = ms.add_parser("search", help="найти документы по словам")
    x.add_argument("query")
    x.add_argument("--limit", type=int, default=10)
    x.add_argument("--type", help="pptx | xlsx | docx | pdf | md | html | txt | csv")
    x = ms.add_parser("show", help="текст документа (по слайдам, листам, страницам)")
    x.add_argument("file", help="полный путь, путь внутри папки или имя файла")
    x.add_argument("--part", help="только эта часть: «слайд 5», «лист «План»», «стр. 2»")
    x.add_argument("--max-chars", type=int, default=20000)
    x = ms.add_parser("recent", help="что менялось за последние дни")
    x.add_argument("--days", type=int, default=7)
    x.add_argument("--limit", type=int, default=40)
    ms.add_parser("status", help="что в индексе")
    ms.add_parser("index", help="обновить индекс сейчас")
    s.set_defaults(fn=cmd_docs)

    s = sub.add_parser("kb", help="база знаний: решения, уроки, удачи (антология)")
    ms = s.add_subparsers(dest="action", required=True)
    x = ms.add_parser("add", help="добавить решение, урок или удачу")
    x.add_argument("--kind", required=True, choices=["decision", "lesson", "win"], help="решение | урок | удача")
    x.add_argument("--title", required=True, help="суть одной строкой: «Средний чек апсейла ОРК ставим 15 тыс.»")
    x.add_argument("--details", help="почему, контекст, цифры")
    x.add_argument("--date", help="YYYY-MM-DD — когда решили или случилось")
    x.add_argument("--project", help="тема или направление: «ОРК», «Автоворонка»")
    x.add_argument("--people", help="участники через запятую")
    x.add_argument("--source", help="meeting:<id> | document:<путь> | telegram:<id сообщения> | kaiten:<id карточки>")
    x.add_argument("--quote", help="«[чч:мм:сс] дословно» — откуда это известно")
    x.add_argument("--proposed", action="store_true", help="только предложить: директор примет или отклонит")
    for name in ("list", "search"):
        x = ms.add_parser(name, help="записи" if name == "list" else "найти по словам")
        if name == "search":
            x.add_argument("query")
        else:
            x.add_argument("--days", type=int)
        x.add_argument("--kind", choices=["decision", "lesson", "win"])
        x.add_argument("--project")
        x.add_argument("--status", default="current", help="current (по умолчанию) | all | accepted | proposed | outdated")
        x.add_argument("--details", action="store_true", help="показать и подробности")
        x.add_argument("--limit", type=int, default=50)
    x = ms.add_parser("set", help="изменить запись")
    x.add_argument("id", type=int)
    x.add_argument("--status", choices=["accepted", "proposed", "outdated", "rejected"])
    x.add_argument("--title")
    x.add_argument("--details")
    x.add_argument("--note", help="почему изменено: «заменено решением встречи 26.10»")
    ms.add_parser("import", help="забрать заметки-решения из папок с документами")
    s.set_defaults(fn=cmd_kb)

    s = sub.add_parser("meetings", help="встречи")
    ms = s.add_subparsers(dest="action", required=True)
    x = ms.add_parser("list")
    x.add_argument("--days", type=int)
    x.add_argument("--limit", type=int, default=30)
    x = ms.add_parser("show")
    x.add_argument("id")
    x.add_argument("--transcript", action="store_true")
    x = ms.add_parser("set")
    x.add_argument("id")
    x.add_argument("--title")
    x.add_argument("--status", choices=["new", "transcribed", "processed", "failed"])
    x.add_argument("--headline", help="одна строка: главное по встрече")
    x.add_argument("--participants", help="через запятую")
    x = ms.add_parser("add", help="добавить файл записи вручную")
    x.add_argument("file")
    x.add_argument("--title")
    x = ms.add_parser("search", help="поиск по разборам и расшифровкам")
    x.add_argument("query")
    x.add_argument("--limit", type=int, default=40)
    x = ms.add_parser("guess", help="догадка, кто из спикеров кто: директор подтвердит на странице встречи")
    x.add_argument("id")
    x.add_argument("--speaker", required=True, help="«Спикер 2»")
    x.add_argument("--name", required=True, help="имя, как в справочнике людей")
    x.add_argument("--why", default="", help="почему так думаешь: «обращаются по имени [00:12:03]»")
    s.set_defaults(fn=cmd_meetings)

    s = sub.add_parser("people", help="справочник людей")
    ms = s.add_subparsers(dest="action", required=True)
    ms.add_parser("list")
    x = ms.add_parser("find")
    x.add_argument("query")
    x = ms.add_parser("set", help="добавить или обновить человека")
    x.add_argument("--name", required=True)
    x.add_argument("--id", type=int)
    x.add_argument("--role")
    x.add_argument("--alias", action="append", help="другое написание имени (можно несколько раз)")
    x.add_argument("--telegram")
    x.add_argument("--ymessenger")
    x.add_argument("--email")
    x.add_argument("--kaiten-user", type=int)
    x.add_argument("--notes")
    s.set_defaults(fn=cmd_people)

    s = sub.add_parser("commitments", help="обещания и договорённости")
    ms = s.add_subparsers(dest="action", required=True)
    x = ms.add_parser("list")
    x.add_argument("--status", default="open", choices=["open", "done", "cancelled", "all", "archived"],
                   help="archived — архив (без него архивные не показываются)")
    x.add_argument("--direction", choices=["mine", "to_me", "other"])
    x.add_argument("--person")
    x.add_argument("--overdue", action="store_true")
    x = ms.add_parser("add")
    x.add_argument("--direction", required=True, choices=["mine", "to_me", "other"],
                   help="mine — обещала директор; to_me — обещали директору; other — между коллегами")
    x.add_argument("--title", help="короткий заголовок для карточки, 2–6 слов: «Прогноз по ключевым клиентам»")
    x.add_argument("--what", required=True, help="суть: что именно сделать, с подробностями")
    x.add_argument("--who", help="кто должен сделать")
    x.add_argument("--whom", help="кому обещано")
    x.add_argument("--due", help="YYYY-MM-DD")
    x.add_argument("--source",
                   help="meeting:<id> | telegram:<message id> | ymessenger:<id> | pachca:<id> | mail:<id> | manual")
    x.add_argument("--quote", help="дословная цитата")
    x.add_argument("--card", type=int, help="карточка Kaiten")
    x.add_argument("--note")
    x.add_argument("--parent", type=int, help="часть задачи с этим id: директор передала её работу дальше")
    x = ms.add_parser("set")
    x.add_argument("id", type=int)
    x.add_argument("--status", choices=["open", "done", "cancelled"])
    x.add_argument("--due")
    x.add_argument("--card", type=int)
    x.add_argument("--note")
    x.add_argument("--title")
    x.add_argument("--what")
    x.add_argument("--who")
    x.add_argument("--whom")
    x.add_argument("--parent", type=int, help="сделать частью задачи с этим id (0 — отвязать)")
    x.add_argument("--in-work", choices=["on", "off"], help="отметка «В работе» (по просьбе директора)")
    x.add_argument("--archived", choices=["on", "off"], help="убрать в архив или вернуть (по просьбе директора)")
    s.set_defaults(fn=cmd_commitments)

    s = sub.add_parser("attention", help="где ждут ответа директора")
    ms = s.add_subparsers(dest="action", required=True)
    x = ms.add_parser("list")
    x.add_argument("--all", action="store_true")
    x = ms.add_parser("add")
    x.add_argument("--message", type=int, help="id сообщения из ./ops messages")
    x.add_argument("--summary", required=True, help="суть: что от неё хотят")
    x.add_argument("--why", help="почему важно")
    x.add_argument("--draft", help="черновик ответа")
    x.add_argument("--priority", default="normal", choices=["high", "normal", "low"])
    x = ms.add_parser("close")
    x.add_argument("id", type=int)
    x.add_argument("--dismiss", action="store_true")
    s.set_defaults(fn=cmd_attention)

    s = sub.add_parser("kaiten", help="задачи Kaiten (зеркало)")
    ms = s.add_subparsers(dest="action", required=True)
    ms.add_parser("sync")
    x = ms.add_parser("cards")
    x.add_argument("--space", type=int)
    x.add_argument("--who", help="исполнитель или участник")
    x.add_argument("--search")
    x.add_argument("--stale", action="store_true", help="без движения")
    x.add_argument("--mine", action="store_true")
    x.add_argument("--all-states", action="store_true", help="включая готовые")
    x.add_argument("--limit", type=int, default=60)
    x = ms.add_parser("card")
    x.add_argument("id", type=int)
    x.add_argument("--fresh", action="store_true", help="перечитать из Kaiten")
    x = ms.add_parser("users")
    x.add_argument("query", nargs="?")
    x = ms.add_parser("boards")
    x.add_argument("--space", type=int)
    ms.add_parser("targets", help="куда можно создавать карточки")
    s.set_defaults(fn=cmd_kaiten)

    s = sub.add_parser("calendar", help="встречи из календаря")
    s.add_argument("--date", help="YYYY-MM-DD, по умолчанию сегодня")
    s.add_argument("--days", type=int, default=1)
    s.set_defaults(fn=cmd_calendar)

    s = sub.add_parser("propose", help="предложить действие (выполнится после «да» директора)")
    ps = s.add_subparsers(dest="kind", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--batch", help="пачка (например meeting:<id>) — уйдёт одним сообщением")
    common.add_argument("--reason", help="почему предлагаешь (покажется директору)")
    common.add_argument("--source", help="meeting:<id> | telegram:<message id> | ...")
    x = ps.add_parser("card", parents=[common], help="создать карточку в Kaiten")
    x.add_argument("--title", required=True)
    x.add_argument("--description")
    x.add_argument("--due", help="YYYY-MM-DD")
    x.add_argument("--responsible", help="имя, @username или id в Kaiten")
    x.add_argument("--target", help="направление из ./ops kaiten targets")
    x.add_argument("--commitment", type=int, help="связать с обещанием")
    x = ps.add_parser("comment", parents=[common], help="комментарий в карточку")
    x.add_argument("--card", type=int, required=True)
    x.add_argument("--text", required=True)
    x = ps.add_parser("update", parents=[common], help="изменить карточку")
    x.add_argument("--card", type=int, required=True)
    x.add_argument("--due")
    x.add_argument("--title")
    x.add_argument("--responsible")
    x.add_argument("--column", type=int, help="id колонки (./ops kaiten boards)")
    x = ps.add_parser("meeting", parents=[common], help="создать встречу в календаре")
    x.add_argument("--title", required=True)
    x.add_argument("--start", required=True, help="2026-10-27T15:00")
    x.add_argument("--end", required=True)
    x.add_argument("--attendee", action="append", help="email участника (можно несколько раз)")
    x.add_argument("--description")
    x.add_argument("--calendar", help="название календаря Яндекса (по умолчанию — из настроек)")
    x = ps.add_parser("cancel-meeting", parents=[common], help="отменить встречу")
    x.add_argument("--uid", required=True)
    x = ps.add_parser("remind", parents=[common], help="напомнить исполнителю задачи «мне должны» в Telegram (после «Да»)")
    x.add_argument("--commitment", type=int, required=True, help="id задачи «мне должны»")
    x.add_argument("--text", help="свой текст; без него — по шаблону из настроек")
    x = ps.add_parser("done", parents=[common], help="закрыть задачу PULSE, которая выглядит выполненной")
    x.add_argument("--commitment", type=int, required=True, help="id задачи (./ops commitments list)")
    x.add_argument("--quote", help="дословно, откуда видно, что сделано ([чч:мм:сс] для встречи)")
    x = ps.add_parser("answered", parents=[common],
                      help="убрать «нужен ответ»: по её сообщению видно, что директор ответила")
    x.add_argument("--attention", type=int, required=True, help="id пункта (./ops attention list)")
    x.add_argument("--quote", help="её ответ дословно; --source — это сообщение (telegram:<id>)")
    s.set_defaults(fn=cmd_propose)

    s = sub.add_parser("proposals", help="очередь подтверждений")
    ms = s.add_subparsers(dest="action", required=True)
    x = ms.add_parser("list")
    x.add_argument("--all", action="store_true")
    x.add_argument("--batch")
    x = ms.add_parser("approve", help="выполнить (только по прямой просьбе директора!)")
    x.add_argument("id", type=int, nargs="?")
    x.add_argument("--batch")
    x = ms.add_parser("reject")
    x.add_argument("id", type=int)
    s.set_defaults(fn=cmd_proposals)

    s = sub.add_parser("notify", help="написать директору в Telegram")
    s.add_argument("text", help="HTML-разметка Telegram: <b>, <i>, <a href>")
    s.add_argument("--batch", help="приложить предложения этой пачки с кнопками")
    s.add_argument("--urgent", action="store_true", help="отправить даже в тихие часы")
    s.add_argument("--kind", choices=["meeting", "urgent", "digest", "weekly", "groups", "info"], default="info",
                   help="тип: учитываются настройки уведомлений; digest, weekly и groups сохраняются в «Дайджестах»")
    s.set_defaults(fn=cmd_notify)

    s = sub.add_parser("demo", help="демо-данные для знакомства с дашбордом")
    s.add_argument("--clear", action="store_true")
    s.set_defaults(fn=cmd_demo)
    sub.add_parser("version").set_defaults(fn=cmd_version)
    s = sub.add_parser("update", help="обновить PULSE из GitHub (данные, настройки и пароли не трогаются)")
    s.add_argument("--check", action="store_true", help="только проверить, есть ли новая версия")
    s.add_argument("--no-restart", action="store_true", help="не перезапускать PULSE (это сделает скрипт обновления)")
    s.set_defaults(fn=cmd_update)
    return p


def main(argv: list[str] | None = None) -> None:
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8")
            except (ValueError, OSError):
                pass
    config.ensure_dirs()
    argv = list(sys.argv[1:] if argv is None else argv)
    want_json = "--json" in argv
    argv = [x for x in argv if x != "--json"]  # --json можно писать в любом месте команды
    args = build_parser().parse_args(argv)
    args.json = want_json
    try:
        args.fn(args)
    except KeyboardInterrupt:
        sys.exit(130)
    except BrokenPipeError:  # вывод обрезали (| head) — это не ошибка
        try:
            sys.stdout = open(os.devnull, "w")  # noqa: SIM115
        except OSError:
            pass
        sys.exit(0)
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001 — Claude и человеку нужен понятный текст, не трейсбек
        fail(str(e))


if __name__ == "__main__":
    main()
