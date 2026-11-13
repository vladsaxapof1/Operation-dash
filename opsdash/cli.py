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

from opsdash import chat_tasks, config, priorities, records, store, worklog


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
    """Запустить PULSE в фоне: на Mac — через службу launchd (если установлена), на Windows — через
    scripts/start-hidden.vbs (он же перезапустит PULSE после сбоя), иначе отдельным процессом."""
    from opsdash import macos

    if macos.is_mac() and macos.kickstart():
        return
    exe = Path(sys.executable)
    pyw = exe.with_name("pythonw.exe")
    vbs = config.APP_DIR / "scripts" / "start-hidden.vbs"
    kwargs = {"cwd": str(config.APP_DIR)}
    if os.name == "nt":
        kwargs["creationflags"] = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        if vbs.exists() and pyw.exists():
            cmd = [str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "wscript.exe"), str(vbs)]
        else:
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


def cmd_doctor(a) -> None:
    """Здоровье PULSE: строка на систему и на каждый источник — что сейчас и что сделать (беклог CON-5)."""
    from opsdash import daemon, doctor

    res = doctor.run(include_off=a.all)
    if a.json:
        out(json.dumps(res, ensure_ascii=False, indent=2))
        return
    mark = {"ok": "✓", "warn": "!", "bad": "✗", "off": "–"}
    out(f"PULSE: {'работает — ' + daemon.url() if daemon.is_running() else 'не запущен'} · {res['summary']}")
    for title, rows in (("Система", res["system"]), ("Подключения", res["sources"])):
        out(f"\n{title}")
        if not rows:
            out("  ничего не подключено — Настройки → Подключения")
        for r in rows:
            out(f"  {mark[r['level']]} {r['title']}: {r['text']}")
            if r["fix"] and r["level"] in ("warn", "bad"):
                out(f"      → {r['fix']}")
    if res["worst"] == "bad":
        sys.exit(1)


def cmd_check(a) -> None:
    from opsdash import bot, brain, sources

    settings = config.load_settings()
    checks = {"claude": brain.check, **{k: sources.checker(k) for k in config.SOURCES},
              "bot": lambda: "бот @" + bot.get_bot().me().get("username", "?")}
    # «все» — помощник и то, что включено: выключенный источник проверять незачем
    names = ([n for n in checks if n == "claude" or settings["telegram_bot" if n == "bot" else n].get("enabled")]
             if a.what == "all" else [a.what])
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
    names = list(config.SOURCES) if a.what == "all" else [a.what]
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


def _chat_tasks(found: list[dict], shown: set[int]) -> None:
    """Под чатом в «новых сообщениях» — открытые задачи, к которым переписка вероятнее всего относится
    (opsdash.chat_tasks): Claude сверяет сообщения с ними, а не ищет задачу наугад в длинном списке."""
    if not found:
        return
    out("→ Открытые задачи, к которым может относиться этот чат (выполнена — propose done, продвинулась — log):")
    for f in found[:chat_tasks.PER_CHAT]:
        c = f["task"]
        if c["id"] in shown:
            out(f"  [{c['id']}] «{records.headline(c)}» — выше ({f['why']})")
            continue
        shown.add(c["id"])
        out(f"  {_commitment_brief(c, now=140)} — {f['why']}")
    if len(found) > chat_tasks.PER_CHAT:
        out(f"  …и ещё {len(found) - chat_tasks.PER_CHAT}: ./ops commitments list --brief --person \"<имя>\"")


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
        related = chat_tasks.for_pending(chats)
        shown: set[int] = set()
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
            _chat_tasks(related.get((source, chat_id, title)) or [], shown)
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
        items = []
        # чат может быть подписан латиницей: «Олег» → «Oleg …»
        for title in dict.fromkeys((a.title, _latin(a.title))):
            items = list(reversed(store.rows(sql, (f"%{title}%", f"%{title}%", since, a.limit))))
            if items:
                break
        if not items:
            return out("Сообщений нет.")
        for m in items:
            out(f"«{m['chat_title']}» " + _msg_line(m))


_LATIN = dict(zip("абвгдеёжзийклмнопрстуфхцчшщъыьэюя",
                  ["a", "b", "v", "g", "d", "e", "e", "zh", "z", "i", "y", "k", "l", "m", "n", "o", "p", "r", "s", "t",
                   "u", "f", "kh", "ts", "ch", "sh", "sch", "", "y", "", "e", "yu", "ya"]))


def _latin(text: str) -> str:
    """«Олег» → «Oleg»: имя чата латиницей, если по-русски чата не нашлось."""
    out = "".join(_LATIN.get(ch.lower(), ch) if ch.lower() in _LATIN else ch for ch in text)
    return " ".join(w[:1].upper() + w[1:] if w[:1].isalpha() else w for w in out.split(" "))


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
        if a.kind == "decision" and any(v for v in (a.stage, a.metric, a.before, a.after, a.waiting, a.tasks, a.prev,
                                                    a.question, a.answer)):
            from opsdash import decisions

            try:
                decisions.update(item_id, stage=a.stage, metric=a.metric, before=a.before, after=a.after,
                                 waiting=a.waiting, prev=a.prev, question=a.question, answer=a.answer,
                                 relation=a.relation if a.prev else None)
                for tid in _ids(a.tasks):
                    decisions.link(item_id, tid)
            except decisions.DecisionError as e:
                fail(str(e))
        out(f"Запись [{item_id}] сохранена" + (" как предложение — директор примет или отклонит" if a.proposed else ""))
    elif a.action in ("list", "search"):
        items = anthology.items(kind=a.kind, status=a.status, project=a.project, days=getattr(a, "days", None),
                                q=getattr(a, "query", None), limit=a.limit)
        if a.json:
            return as_json(items)
        from opsdash import decisions

        gone = decisions.superseded() if any(r["kind"] == "decision" for r in items) else {}
        for r in items:
            meta = " · ".join(x for x in (r["happened"], r["project"], r["status_title"] if r["status"] != "accepted" else "") if x)
            src = f" · источник {r['source_type']}:{r['source_ref'] or ''}" if r["source_type"] != "manual" else ""
            by = gone.get(r["id"])
            later = (f" · НЕ ДЕЙСТВУЕТ: {decisions.RELATIONS_PAST[by['relation']]} решением [{by['id']}] от {by['when']}"
                     if by else "")
            out(f"[{r['id']}] {r['kind_title']}: {r['title']} ({meta}){src}{later}")
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


def cmd_mcp(a) -> None:
    """Свои подключения через MCP (MORE-1): что подключено, инструменты, проверка."""
    from opsdash import mcp

    try:
        if a.action == "list":
            lines = mcp.summary_lines()
            out("\n".join(lines) if lines else "MCP-подключений нет — Настройки → Подключения → «Сервисы через MCP».")
        elif a.action == "tools":
            if not mcp.server(a.id):
                fail(f"нет подключения {a.id} — ./ops mcp list")
            ts = mcp.tools(a.id)
            if a.json:
                return as_json(ts)
            for t in ts:
                how = "читаешь сам" if t["on"] else ("чтение выключено директором" if t["read"] else "изменение — ./ops propose mcp")
                out(f"{t['name']} — {how}" + (f" · {t['description']}" if t["description"] else ""))
            if not ts:
                out("Инструменты ещё не известны — ./ops mcp check " + a.id)
        elif a.action == "check":
            out(mcp.check(a.id))
        elif a.action == "serve":
            from opsdash import mcp_server

            mcp_server.serve()
    except mcp.MCPError as e:
        fail(str(e))


def cmd_decisions(a) -> None:
    """Решения по темам (MEM-3): что действует, цепочки «меняет / уточняет / продолжает / отменяет», следующий шаг,
    «было → стало», задачи."""
    from opsdash import decisions

    def line(r: dict) -> str:
        metric = (f" · {r['metric'] or 'метрика'}: {r['before_value'] or '—'} → {r['after_value'] or '—'}"
                  if r["metric"] or r["before_value"] or r["after_value"] else "")
        step = f" · следующий шаг: {r['waiting_for']}" if r["waiting_for"] else ""
        prev = f" · {r['relation_title']} [decision {r['prev']['id']}]" if r["prev"] else ""
        gone = (f" · НЕ ДЕЙСТВУЕТ ({r['state_title']}, [decision {r['superseded_by']['id']}])"
                if r["superseded_by"] else "")
        tasks = ", ".join(f"[commitment {t['id']}]{' ✓' if t['status'] == 'done' else ''}" for t in r["tasks"])
        sub = f" / {r['subtopic']}" if r["subtopic"] else ""
        qa = f" · вопрос «{r['q']}»: {r['a']}" if r["q"] else ""
        return (f"[decision {r['id']}] {r['when']} · {r['topic']}{sub} · {r['stage_title']} · {r['title']}{qa}"
                f"{metric}{step}{prev}{gone}" + (f" · задачи: {tasks}" if tasks else ""))

    try:
        if a.action == "list":
            rows = decisions.items(stage=a.stage, topic=a.topic, project=a.project, q=a.search, meeting=a.meeting,
                                   attention=a.attention, current=not a.all, limit=a.limit)
            if a.json:
                return as_json(rows)
            for r in rows:
                out(line(r))
            if not rows:
                out("Решений нет.")
        elif a.action == "topics":
            ts = decisions.topics()
            if a.json:
                return as_json(ts)
            for t in ts:
                extra = f" · ждут решения {t['open']}" if t["open"] else ""
                subs = f" · подтемы: {', '.join(t['subtopics'])}" if t["subtopics"] else ""
                about = f" — {t['about']}" if t["about"] else " — о чём: не написано"
                out(f"{t['name']}{about} · вопросов {t['questions']}, решений {t['total']}{extra} · "
                    f"обновлено {t['updated']}{subs}")
            if not ts:
                out("Решений нет.")
        elif a.action == "topic":
            out(decisions.topic_text(a.name))
        elif a.action == "show":
            r = decisions.get(a.id)
            if not r:
                fail(f"нет решения {a.id}")
            if a.json:
                return as_json(r)
            out(line(r))
            if r["details"]:
                out(r["details"])
            if r["source_type"] and r["source_type"] != "manual":
                out(f"Источник: {r['source_type']}:{r['source_ref'] or ''}" + (f" — «{r['quote']}»" if r["quote"] else ""))
            if r["prev"]:
                out(f"Прежнее: {r['relation_title']} [decision {r['prev']['id']}] {r['prev']['when']} · {r['prev']['title']}")
            for n in r["next"]:
                out(f"Дальше: [decision {n['id']}] {n['when']} {n['relation_title']} это решение · {n['title']}")
            for t in r["tasks"]:
                out(f"- [commitment {t['id']}] {t['status']} · {t['title']}" + (f" · {t['who']}" if t["who"] else ""))
        elif a.action == "set":
            decisions.update(a.id, stage=a.stage, metric=a.metric, before=a.before, after=a.after,
                             waiting=a.next if a.next is not None else a.waiting, project=a.topic,
                             question=a.question, answer=a.answer,
                             prev=a.prev, relation=a.relation)
            out("Сохранено")
        elif a.action == "about":
            decisions.set_about(a.name, a.text)
            out("Сохранено")
        elif a.action == "rename-topic":
            n = decisions.rename_topic(a.name, a.to)
            out(f"Тема «{a.name}» → «{a.to}»: решений перенесено {n}")
        elif a.action == "link":
            for tid in _ids(a.task):
                decisions.link(a.id, tid)
            out("Связано")
        elif a.action == "unlink":
            decisions.unlink(a.task)
            out("Связь убрана")
    except decisions.DecisionError as e:
        fail(str(e))


def _meeting_messages(a) -> None:
    """Переписка с начала встречи по её задачам (opsdash.chat_tasks.for_meeting) — шаг 7а навыка process-meeting."""
    if not records.get_meeting(a.id):
        fail(f"нет встречи {a.id}")
    res = chat_tasks.for_meeting(a.id)
    if a.json:
        return as_json(res)
    if not res["tasks"]:
        return out("У встречи нет открытых задач. Если ты ещё не записал её задачи — вызови эту команду снова, когда "
                   "запишешь (после шагов 5 и 7): переписку сверяют с уже записанными задачами.")
    if not res["chats"]:
        return out(f"С начала встречи ({store.local(res['start'])}) в чатах по её задачам ничего не писали.")
    out(f"Переписка с начала встречи ({store.local(res['start'])}) в чатах, связанных с её задачами. Triage разобрал "
        "эти сообщения, когда задач встречи ещё не было: сверь их с задачами под каждым чатом — выполнена (прислали, "
        "сделали) — propose done и log, продвинулась — log.")
    _meeting_chats(res["chats"], limit=500)


def _meeting_chats(chats: list[dict], *, limit: int, tasks: bool = True, more: str = "") -> None:
    for ch in chats:
        out(f"\n## {ch['source']} · «{ch['title']}» ({ch['kind']}, chat_id={ch['chat_id']})")
        if tasks:
            out("→ Задачи встречи, к которым может относиться этот чат:")
            for f in ch["tasks"]:
                out(f"  {_commitment_brief(f['task'], now=140)} — {f['why']}")
        for m in ch["messages"]:
            out(_msg_line(m, limit=limit))
        if ch["more"]:
            out(f"…и ещё {ch['more']}: " + (more or f"./ops messages chat \"{ch['title']}\" --days 2"))


TASK_MEETING_MESSAGES = 8  # под только что записанной задачей встречи: остальное — ./ops meetings messages


def _meeting_messages_for_task(cid: int, meeting_id: str) -> None:
    """commitments add с --source meeting:<id>: переписка с начала встречи по этой задаче — сразу, а не отдельным
    шагом в конце разбора: его Claude вызывал раньше, чем записывал задачи, и сверять было не с чем."""
    task = records.list_commitments(status="open", ids=[cid])
    res = chat_tasks.for_meeting(meeting_id, tasks=task, limit=TASK_MEETING_MESSAGES) if task else {"chats": []}
    if not res["chats"]:
        return
    out(f"\nПо этой задаче уже писали с начала встречи ({store.local(res['start'])}) — triage разобрал это, когда "
        "задачи ещё не было. Прислали или сделали то, о чём договорились, — ./ops commitments log и ./ops propose "
        "done (шаг 7а навыка process-meeting); прислали не всё или задача продвинулась — только log:")
    _meeting_chats(res["chats"], limit=400, tasks=False, more=f"./ops meetings messages {meeting_id}")


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
    elif a.action == "messages":
        _meeting_messages(a)
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
        out(f"Записано: {a.speaker} — вероятно, {a.name}. Директор подтвердит на странице встречи.")


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

def _commitment_brief(c: dict, now: int = 0) -> str:
    """Задача одной короткой строкой — чтобы весь список открытых задач помещался в выдачу целиком (полный —
    десятки КБ: фоновый Claude видит из него только начало). now — сколько знаков «сейчас: …» добавить."""
    line = f"[{c['id']}] {records.route(c)}: «{records.headline(c)}»"
    if c["due"]:
        line += f" · до {c['due']}" + (" (ПРОСРОЧЕНО)" if c.get("overdue") else "")
    if c["status"] != "open":
        line += f" · {c['status']}"
    if c.get("parent"):
        line += f" · часть [{c['parent']['id']}]"
    if c.get("checklist"):
        line += f" · пункты {c['items_done']}/{c['items_total']}"
    if c.get("done_hint"):
        line += " · предложено закрыть, ждёт директора"
    if now and c.get("now"):
        line += f" · {worklog.now_text(c, now)}"
    return line


def _commitment_line(c: dict) -> str:
    d = {"mine": "я →", "to_me": "мне ←", "other": "между"}[c["direction"]]
    title = f"«{c['title']}» " if c.get("title") else ""
    line = f"[{c['id']}] {d} {c['who'] or '?'} → {c['whom'] or '?'}: {title}{c['what']}"
    if c["due"]:
        line += f" · до {c['due']}" + (" (ПРОСРОЧЕНО)" if c.get("overdue") else "")
    if c["status"] != "open":
        line += f" · {c['status']}"
    if p := c.get("prio"):
        line += f" · приоритет {p['title']}" + (" (поставлен директором)" if p["manual"] else "") \
            + (f" — {p['why']}" if p["why"] and not p["manual"] else "")
    # важность — если её нет в «почему» приоритета: обычная (оценена) или уровень поставлен директором
    if c.get("importance") and c["status"] == "open" and (c["importance"] == "normal" or (p and p["manual"])):
        line += f" · важность {priorities.IMPORTANCE[c['importance']]}" \
            + (f": {c['importance_why']}" if c.get("importance_why") else "")
    if c.get("in_work"):
        line += " · в работе"
    if c.get("quiet"):
        from opsdash import quiet

        line += f" · {quiet.line(c)}"
    if c.get("ball_back_at") and c["status"] == "open":
        line += " · мяч снова у директора: часть коллеги готова"
    if c.get("estimate_min"):
        line += f" · ≈{c['estimate_min']} мин" + (" (оценка директора)" if c.get("estimate_by") == "user" else "")
    if c.get("agent_hint"):
        line += f" · можно поручить агенту: {c['agent_hint']}"
    if c.get("archived_at"):
        line += " · в архиве"
    if c["direction"] == "to_me" and (off := records.remind_off(c)):
        line += f" · директор: {off}"
    if c["kaiten_card_id"]:
        line += f" · kaiten #{c['kaiten_card_id']}"
    if c.get("project"):
        line += f" · проект [{c['project']['id']}] «{c['project']['title']}»"
    elif c.get("project_off"):
        line += f" · убрана директором из проекта [{c['project_off']}] — туда не возвращать"
    if c["source_type"]:
        line += f" · источник {c['source_type']}:{c['source_ref'] or ''}"
    if c.get("parent"):
        line += f" · часть задачи [{c['parent']['id']}] «{c['parent']['headline']}» ({c['parent']['route']})"
    if c.get("children"):
        line += " · подзадачи: " + ", ".join(f"[{k['id']}] {k['route']}" + (f" ({k['status']})" if k["status"] != "open" else "")
                                            for k in c["children"])
    if c.get("checklist"):
        line += f" · пункты {c['items_done']}/{c['items_total']}: " + ", ".join(
            ("✓" if r["done"] else "☐") + r["text"] for r in c["checklist"])
    if c.get("duplicate"):
        line += f" · дубль задачи [{c['duplicate']['id']}] «{c['duplicate']['headline']}»"
    for w in c.get("waits") or []:
        state = " — готово" if w["done"] else (" — ПРОСРОЧЕНО" if w["late"] else "")
        line += (f" · ждёт [{w['task']['id']}] «{w['task']['headline']}» ({w['task']['doer']}){state}" if w["task"]
                 else f" · ждёт событие «{w['event']}»" + (f" к {w['until']}" if w["until"] else "") + state)
    if c.get("holds"):
        line += " · её ждут: " + ", ".join(f"[{h['task']['id']}] «{h['task']['headline']}»" for h in c["holds"])
    if c.get("wait_risk"):
        line += f" · срок под угрозой: {c['wait_risk']}"
    if c.get("done_hint"):
        line += " · предложено закрыть, ждёт директора"
    if c.get("merge_hint"):
        line += f" · предложено объединить с [{c['merge_hint']['payload'].get('into')}], ждёт директора"
    if c.get("meeting"):
        mt = c["meeting"]
        line += f" · встреча «{mt['title']}» {store.local(mt['start'])}" + (" (прошла)" if mt["past"] else "") \
            + f" uid {mt['uid']}"
    elif c.get("meeting_hint"):
        line += " · предложено связать со встречей, ждёт директора"
    if c.get("now"):
        line += f" · {worklog.now_text(c)}"
    return line


def cmd_commitments(a) -> None:
    if a.action == "list":
        items = records.list_commitments(status=a.status, direction=a.direction, person=a.person, overdue=a.overdue,
                                         sort=a.sort, quiet=a.quiet, q=a.search)
        if a.json:
            return as_json(items)
        for c in items:
            out(_commitment_brief(c) if a.brief else _commitment_line(c))
        if not items:
            out("Обещаний нет.")
    elif a.action == "add":
        st, ref = parse_source(a.source)
        try:
            cid = records.add_commitment(
                direction=a.direction, what=a.what, who=a.who, whom=a.whom, due=a.due, source_type=st, source_ref=ref,
                quote=a.quote, kaiten_card_id=a.card, note=a.note, title=a.title, parent_id=a.parent, force=a.force,
                project_id=a.project,
            )
        except ValueError as e:
            fail(str(e))
        out(f"Обещание [{cid}] сохранено" + (f" как часть задачи [{a.parent}]" if a.parent else ""))
        if a.importance:
            _importance(cid, a.importance, a.importance_why)
        if a.estimate:
            _estimate(cid, a.estimate)
        if a.direction == "mine" and a.due:
            _due_check(cid)
        _no_due_hint(cid)
        if a.agent:
            _agent_hint(cid, a.agent)
        similar = records.similar_open(direction=a.direction, who=a.who, whom=a.whom, title=a.title, what=a.what,
                                       kaiten_card_id=a.card, exclude=cid)
        if similar:
            out("Похожие открытые задачи того же человека — проверь, не дубль ли это:")
            for c in similar:
                out(f"  [{c['id']}] «{c['headline']}» ({c['route']}): {short(c['what'], 160)}")
            out(f"Если это та же работа — ./ops commitments merge {cid} --into <id> (новую ты только что добавил, "
                "её можно объединить сразу); если часть — --parent; если другая задача — ничего делать не нужно.")
        if st == "meeting" and ref:
            _meeting_messages_for_task(cid, ref)
    elif a.action == "wait":
        if not a.waits_for and not a.event:
            fail("укажите --for <id задачи> или --event \"<событие>\"")
        try:
            if a.remove or a.happened:
                found = records.find_waits(a.id, waits_for=a.waits_for, event=a.event)
                if not found:
                    fail("такой связи у задачи нет — ./ops commitments list покажет, чего она ждёт")
                for link in found:
                    if a.remove:
                        records.remove_wait(link["id"])
                    else:
                        records.set_event_done(link["id"])
                out("Связь убрана" if a.remove else "Отмечено: событие случилось")
                return
            st, ref = parse_source(a.source)
            records.add_wait(a.id, waits_for=a.waits_for, event=a.event, until=a.until, note=a.note,
                             source_ref=f"{st}:{ref}" if ref else None,
                             by="claude" if os.environ.get("OPSDASH_AGENT") else "director")
        except ValueError as e:
            fail(str(e))
        c = records.get_commitment(a.id)
        out(f"Задача [{a.id}] ждёт: " + "; ".join(
            (f"[{w['task']['id']}] «{w['task']['headline']}»" if w["task"] else f"событие «{w['event']}»")
            + (" — готово" if w["done"] else "") for w in c["waits"]))
    elif a.action == "handoff":
        try:
            kid = records.hand_off(a.id, who=a.who or "", what=a.what or "", due=a.due, part=a.part, text=a.text,
                                   source=a.source, quote=a.quote)
        except ValueError as e:
            fail(str(e))
        k = records.get_commitment(kid)
        out(f"Мяч у коллеги: [{kid}] «{records.headline(k)}» ({records.route(k)}) — "
            f"часть задачи [{a.id}]. Задача [{a.id}] не в плане, пока эта часть не готова; готово — "
            f"./ops propose done --commitment {kid} …")
    elif a.action == "items":
        if not records.get_commitment(a.id):
            fail(f"нет задачи [{a.id}]")
        try:
            if a.add:
                records.add_items(a.id, a.add, by="claude" if os.environ.get("OPSDASH_AGENT") else "director")
            for texts, op in ((a.done, "done"), (a.undo, "undo"), (a.remove, "remove")):
                for text in texts or []:
                    found = records.find_items(a.id, text)
                    if not found:
                        fail(f"у задачи нет пункта «{text}» — ./ops commitments items {a.id} покажет список")
                    for r in found:
                        if op == "remove":
                            records.remove_item(r["id"])
                        else:
                            records.set_item_done(r["id"], op == "done", a.note)
        except ValueError as e:
            fail(str(e))
        c = records.get_commitment(a.id)
        out(f"Пункты задачи [{a.id}] «{records.headline(c)}» — {c['items_done']} из {c['items_total']}:")
        for r in c["checklist"]:
            out(f"  {'✓' if r['done'] else '☐'} {r['text']}" + (f" — {r['note']}" if r.get("note") else ""))
        if not c["checklist"]:
            out("  пунктов нет — добавить: --add \"Приёмка\" --add \"Хранение\"")
    elif a.action == "show":
        c = records.get_commitment(a.id)
        if not c:
            fail(f"нет задачи [{a.id}]")
        if a.json:
            return as_json(c)
        out(_commitment_line(c))
        if c.get("quote"):
            out(f"Цитата: «{c['quote']}»")
        if c.get("note"):
            out(f"Заметка: {c['note']}")
        out("")
        for line in worklog.lines(c):
            out(line)
    elif a.action == "log":
        c = records.get_commitment(a.id)
        if not c:
            fail(f"нет задачи [{a.id}]")
        if a.remove:
            e = worklog.get(a.remove)
            if not e or e["task_id"] != a.id:
                fail(f"у задачи нет записи {a.remove} — ./ops commitments show {a.id} покажет ход")
            worklog.remove(a.remove)
            out(f"Запись {a.remove} убрана из хода задачи")
            return
        if not a.text:
            for line in worklog.lines(c):
                out(line)
            return
        kind = "ask" if a.ask else a.kind
        try:
            lid, new = worklog.add(a.id, a.text, kind=kind, source=a.source, quote=a.quote, at=a.at, by=a.by)
        except ValueError as e:
            fail(str(e))
        out(f"{'Записано' if new else 'Уже было — запись обновлена'}: ход задачи [{a.id}] «{records.headline(c)}», "
            f"запись {lid}")
        if c["status"] != "open":
            out("Задача уже закрыта — если работа продолжается, скажи директору (вернуть — решение директора)")
        _no_due_hint(a.id)
    elif a.action == "merge":
        drop = records.get_commitment(a.id)
        if not drop:
            fail(f"нет задачи [{a.id}]")
        if os.environ.get("OPSDASH_AGENT") and drop["seen_at"]:
            fail("задача уже показана директору — объединять только предложением: "
                 f"./ops propose merge --commitment {a.id} --into {a.into} --reason \"<почему это одно и то же>\"")
        try:
            records.merge_commitments(a.id, a.into, note=a.note)
        except ValueError as e:
            fail(str(e))
        out(f"Задача [{a.id}] закрыта как дубль [{a.into}]: источник, связи и подзадачи — в [{a.into}]")
    elif a.action == "set":
        try:
            records.update_commitment(a.id, status=a.status, due=a.due, kaiten_card_id=a.card, note=a.note, what=a.what,
                                      title=a.title, who=a.who, whom=a.whom, parent_id=a.parent, project_id=a.project,
                                      by="claude" if os.environ.get("OPSDASH_AGENT") else "director")
        except ValueError as e:
            fail(str(e))
        if a.in_work:
            records.set_in_work(a.id, a.in_work == "on")
        if a.archived:
            records.set_archived(a.id, a.archived == "on")
        if a.remind:
            act = a.remind if a.remind in records.REMIND_ACTS - {"pause"} else "pause"
            try:
                records.set_reminders(a.id, act, a.remind if act == "pause" else None)
            except ValueError as e:
                fail(str(e))
        out("Сохранено")
        if a.importance:
            _importance(a.id, a.importance, a.importance_why)
        if a.priority:
            try:
                level = priorities.parse_level(a.priority)
                priorities.set_level(a.id, level)
            except ValueError as e:
                fail(str(e))
            out(f"Приоритет: {priorities.LEVELS[level]} — закреплён, расчёт его не меняет" if level
                else "Приоритет снова считается сам")
        if a.estimate:
            _estimate(a.id, a.estimate)
        if a.agent is not None:
            _agent_hint(a.id, a.agent)
    elif a.action == "importance":
        if a.set:
            try:
                batch = priorities.parse_batch(a.set)
                for cid, value, why in batch:
                    priorities.set_importance(cid, value, why)
            except ValueError as e:
                fail(str(e))
            out(f"Важность сохранена: {len(batch)}")
            return
        items = priorities.unassessed()
        if a.json:
            return as_json(items)
        for c in items:
            d = {"mine": "задача директора", "to_me": f"должен директору: {c['who'] or '?'}"}[c["direction"]]
            out(f"[{c['id']}] «{records.headline(c)}» ({d} → {c['whom'] or '?'}): {short(c['what'], 300)}"
                + (f" · до {c['due']}" if c["due"] else "") + (f" · цитата: «{short(c['quote'], 200)}»" if c["quote"] else "")
                + (f" · источник {c['source_type']}" if c["source_type"] else ""))
        if not items:
            out("У всех открытых задач директора и «мне должны» важность оценена.")


def _importance(cid: int, value: str, why: str | None) -> None:
    """--importance: оценка важности (Claude). Срок PULSE учитывает сам; уровень директора она не меняет."""
    try:
        priorities.set_importance(cid, value, why)
    except ValueError as e:
        fail(str(e))
    c = records.get_commitment(cid)
    p = c.get("prio") if c else None
    out(f"Важность: {priorities.IMPORTANCE[value]}" + (f" · приоритет сейчас: {p['title']}" if p else "")
        + (" (поставлен директором — не меняется)" if p and p["manual"] else ""))


def _agent_hint(cid: int, why: str) -> None:
    """--agent: работу можно поручить агенту (Claude Code, Codex) — пометка на задаче, без запусков."""
    from opsdash import agents

    agents.mark_hint(cid, why)
    out("Пометка «Можно поручить агенту»: " + why.strip() if why.strip() else "Пометка «Можно поручить агенту» снята")


def _due_check(cid: int) -> None:
    """Срок в момент обещания: хватит ли директору окон до срока новой задачи (opsdash.day.check_due)."""
    from opsdash import day

    res = day.check_due(cid)
    if res and not res["ok"]:
        out(f"Срок под угрозой с самого начала: {res['why']}."
            + (f" Реальнее {res['realistic']:%d.%m} — директору это предложит «Уточнить срок»; в разборе встречи "
               f"пометь у задачи: «срок под угрозой с самого начала — реальнее {res['realistic']:%d.%m}»"
               if res["realistic"] else ""))


def _no_due_hint(cid: int) -> None:
    """Задача директора без срока не попадает в план дня и легко теряется. Если в переписке или на встрече директор
    говорит, когда займётся («вечером посмотрю», «завтра гляну»), — это и есть срок: напомнить поставить его."""
    c = store.row("SELECT direction, status, due, no_due_at, in_work_at FROM commitments WHERE id=?", (cid,))
    if not c or c["direction"] != "mine" or c["status"] != "open" or c["due"] or c["no_due_at"] or c["in_work_at"]:
        return
    out("У задачи директора нет срока — в план дня она не попадёт. Директор говорит, когда займётся («вечером "
        "посмотрю», «завтра гляну», «обсудим на встрече в среду»), или срок назвал тот, кто просит, — поставь: "
        f"./ops commitments set {cid} --due YYYY-MM-DD (день считай от даты сообщения или встречи). Не прозвучал — "
        "оставь без срока.")


def _estimate(cid: int, value: int) -> None:
    """--estimate: сколько займёт у директора. Из фонового запуска — оценка Claude, оценку директора она не меняет."""
    from opsdash import day

    try:
        ok = day.set_estimate(cid, value, by=day.agent_or_user())
    except ValueError as e:
        fail(str(e))
    out(f"Оценка: {day.est_text(value)}" if ok else "Оценка поставлена директором — не меняю")
    if value > day.BIG:
        out(f"Больше полдня — предложи {day.SPLIT_HINT} (./ops commitments items {cid} --add \"…\")")


# --- реальность дня ---------------------------------------------------------------------------------

def cmd_day(a) -> None:
    from datetime import date

    from opsdash import day

    if a.action == "estimates":
        if a.set:
            by, done, kept = day.agent_or_user(), 0, 0
            for part in a.set.replace(";", ",").split(","):
                cid, _, value = part.strip().partition("=")
                if not cid:
                    continue
                try:
                    ok = day.set_estimate(int(cid.strip().lstrip("#[").rstrip("]")), int(value), by=by)
                except ValueError as e:
                    fail(f"«{part.strip()}»: {e}")
                done, kept = done + ok, kept + (not ok)
            out(f"Оценки сохранены: {done}" + (f"; оценки директора не менял: {kept}" if kept else ""))
            return
        items = day.unestimated()
        if a.json:
            return as_json(items)
        for c in items:
            out(f"[{c['id']}] «{records.headline(c)}» → {c['whom'] or '?'}: {short(c['what'], 300)}"
                + (f" · до {c['due']}" if c["due"] else "") + (" · в работе" if c["in_work_at"] else ""))
        if not items:
            out("Все открытые задачи директора оценены.")
        return
    try:
        when = date.fromisoformat(getattr(a, "week_date", None) or a.date) if (getattr(a, "week_date", None) or a.date) \
            else None
        until = date.fromisoformat(a.until) if a.action == "capacity" else None
    except ValueError:
        fail("дата — в виде ГГГГ-ММ-ДД")
    if a.action == "week":
        w = day.week(when)
        if a.json:
            return as_json(w)
        out(w["line"] or "На этой неделе рабочих дней больше нет")
        for x in w["days"]:
            out(f"- {day.day_text(x['date'])}: окон {day.dur(x['capacity'])}, задач {day.dur(x['need'])}"
                f" (с начала недели: окон {day.dur(x['cum_capacity'])}, задач {day.dur(x['cum_need'])})"
                + {"over": " — перегруз", "tight": " — плотно"}.get(x["level"], "")
                + (": " + "; ".join(f"«{i['title']}»" for i in x["items"] if i.get("id"))[:400] if x["items"] else ""))
        return
    if a.action == "capacity":
        task = store.row("SELECT due, estimate_min FROM commitments WHERE id=?", (a.task,)) if a.task else None
        if a.task and not task:
            fail(f"нет задачи [{a.task}]")
        if task and task["due"] == until.isoformat() and not a.estimate:
            res = day.check_due(a.task)  # срок самой задачи — «реальнее …» запомнится для «Уточнить срок»
        else:
            res = None
        res = res or day.capacity_until(until, a.estimate or (task or {}).get("estimate_min") or day.DEFAULT_ESTIMATE,
                                        exclude=a.task)
        if a.json:
            return as_json(res)
        out(("Успеваете: " if res["ok"] else "Не успеваете: ") + res["why"] + "."
            + ("" if res["ok"] else f" Реальнее {res['realistic']:%d.%m}." if res["realistic"]
               else " В ближайшие два месяца окон не хватает."))
        return
    p = day.plan(when)
    if a.json:
        return as_json({k: v for k, v in p.items() if k not in ("meetings", "free_events")})
    v = p["verdict"]
    ww = f"рабочее время {day.hm(p['work'][0])}–{day.hm(p['work'][1])}" if p["work"] else "выходной по настройкам"
    out(f"# День {p['date']:%d.%m} · {ww}" + (f", считаю с {day.hm(p['from'])}" if p["today"] and p["from"] else ""))
    out(f"Вердикт: {v['line']}")
    out(f"Строка для дайджеста: {p['digest']}")
    if p["today"] and (w := day.week())["line"]:
        out(f"{w['line']} (неделя по дням — ./ops day week)")
    if p["progress"]["total"]:
        out(f"{p['progress']['line']} (для вечернего дайджеста)")
    out(f"\n## Окна ({day.dur(p['capacity'])} для задач)")
    for w in p["windows"]:
        out(f"- {day.hm(w['s'])}–{day.hm(w['e'])} ({day.dur(w['len'])})"
            + (f" · {w['focus']['title']} (фокус-блок)" if w["focus"] else "")
            + (" · короткое, не считается" if w["short"] else ""))
    if not p["windows"]:
        out("- нет")
    busy = sum(e - s for s, e in day.union([(m["s"], m["e"]) for m in p["meetings"]]))
    out(f"Занято: встречи {day.dur(busy)} ({len(p['meetings'])}), буферы {day.dur(sum(e - s for s, e in p['buffers']))}"
        + (f", обед {day.hm(p['lunch'][0])}–{day.hm(p['lunch'][1])}" if p["lunch"] else ""))
    if p["current"]:
        w = p["current"]
        out(f"Сейчас: окно до {day.hm(w['e'])} ({day.dur(w['left'])})"
            + (f" — подходит «{w['fit']['title']}» (≈{day.dur(w['fit']['est'])})" if w["fit"] else ""))
    elif p["next"]:
        out(f"Следующее окно: {day.hm(p['next']['s'])}–{day.hm(p['next']['e'])}")
    out(f"\n## Нужно сделать ({day.dur(p['need'])}), по приоритету")
    for i in p["items"]:
        ref = f"[commitment {i['id']}] «{i['title']}»" if i["id"] else i["title"]
        est = day.dur(i["est"]) + (" (оценка по умолчанию)" if i["est_default"] else
                                   " (оценка директора)" if i["est_by"] == "user" else "")
        out(f"- {ref} — {est} — {i['reason']}" + (f" → {i['slot']}" if i.get("slot") else ""))
    if not p["items"]:
        out("- ничего")
    if p["overflow"]:
        out("Не влезает сегодня: " + "; ".join(f"«{i['title']}» ({day.dur(i['est'])})" for i in p["overflow"]))
    for i in p["too_big"]:
        out(f"Не помещается ни в одно окно: «{i['title']}» ({day.dur(i['est'])}) — самое длинное окно "
            f"{day.dur(p['longest'])} — {day.SPLIT_HINT}")
    if p["waiting"]:
        out("Ждут других, в план не ставлю: " + "; ".join(f"[commitment {c['id']}] «{c['title']}» ({c['why']})"
                                                          for c in p["waiting"]))
    if p["off"] == "over" and p["today"] and p["carry"]:
        out("Не закрыто за день (директор переносит кнопкой на Сводке): "
            + "; ".join(f"[commitment {i['id']}] «{i['title']}»" for i in p["carry"]))


def _ids(value: str | None) -> list[int]:
    try:
        return [int(x) for x in (value or "").replace(" ", ",").split(",") if x.strip()]
    except ValueError:
        fail("номера задач — числа через запятую: 57,69,80")


def _project_line(p: dict) -> str:
    line = f"[{p['id']}] «{p['title']}» — {p['done']} из {p['total']} ({p['percent']}%" + (
        ", с учётом пунктов)" if p["partial"] else ")")
    if p["status"] != "active":
        line += f" · {p['status_title']}"
    if p["due"]:
        line += f" · срок {p['due']}" + (" (ПРОСРОЧЕН)" if p["late"] else "")
    if p["owner"]:
        line += f" · ведёт {p['owner']}"
    if p["stuck_on"]:
        line += " · стоит на: " + "; ".join(f"{s['who']} — [{s['task']['id']}] «{records.headline(s['task'])}»"
                                            for s in p["stuck_on"])
    if p["blocked"]:
        line += f" · ждут: {p['blocked']}"
    if p["overdue"]:
        line += f" · просрочено: {p['overdue']}"
    if p.get("goal"):
        line += f"\n    результат: {p['goal']}"
    return line


def cmd_projects(a) -> None:
    from opsdash import projects

    if a.action == "list":
        items = projects.list_projects("all" if a.all else "active")
        if a.json:
            return as_json(items)
        for p in items:
            out(_project_line(p))
        if not items:
            out("Проектов нет.")
    elif a.action == "show":
        p = projects.get(a.id)
        if not p:
            fail(f"нет проекта [{a.id}]")
        if a.json:
            return as_json(p)
        out(_project_line(p))
        for n, step in enumerate(p["steps"], 1):
            out(f"\nШаг {n}" + (" — можно делать сейчас" if n == 1 else " — ждёт предыдущих"))
            for c in step:
                out("  " + _commitment_line(c))
        if p["parked"]:
            out("\nВ архиве (в прогресс не идут):")
            for c in p["parked"]:
                out("  " + _commitment_line(c))
        out("\nВыполнено:" if p["finished"] else "\nВыполненных пока нет.")
        for c in p["finished"]:
            out("  " + _commitment_line(c))
    elif a.action == "add":
        if os.environ.get("OPSDASH_AGENT"):
            fail("новый проект — только предложением: ./ops propose project --title … --tasks … --reason …")
        try:
            pid = projects.add(title=a.title, goal=a.goal, owner=a.owner, due=a.due, kaiten_card_id=a.card)
            found = projects.assign(pid, _ids(a.tasks), by="director")
        except ValueError as e:
            fail(str(e))
        out(f"Проект [{pid}] «{a.title}» создан" + (f", задач: {len(found)}" if found else ""))
    elif a.action == "set":
        try:
            projects.update(a.id, title=a.title, goal=a.goal, owner=a.owner, due=a.due, kaiten_card_id=a.card,
                            status=a.status)
            if a.tasks:
                projects.assign(a.id, _ids(a.tasks), by="claude" if os.environ.get("OPSDASH_AGENT") else "director")
        except ValueError as e:
            fail(str(e))
        out("Сохранено")


# --- поручения агенту -------------------------------------------------------------

def _session_line(s: dict) -> str:
    line = f"{s['id']} · {s['when']} · «{s['title']}» · {s['home'] or '?'} · реплик человека: {s['n_prompts']}"
    if s["duration"]:
        line += f" · шла {s['duration']}"
    if s["made"]:
        line += " · создала: " + ", ".join(f["name"] for f in s["made"][:5])
    if s["outcome"]:
        line += f" · итог: {s['outcome']}"
    return line


def cmd_agents(a) -> None:
    from opsdash import agents

    if a.action == "index":
        out(agents.index())
    elif a.action == "watch":
        out(agents.check())
    elif a.action == "examples":
        items = agents.examples(limit=a.limit, text=a.query or "")
        for g in items:
            out(f"{g['brief']} — «{g['headline']}» · {agents.ENGINES.get(g['engine'], 'Claude')} · уточнений: {g['replies']}"
                + (f" · сделал: {', '.join(g['made'][:3])}" if g["made"] else ""))
        if not items:
            out("Удачных поручений пока нет: образцом становится промт, после которого агенту хватило 1–2 реплик.")
    elif a.action == "similar":
        items = agents.similar(a.query, folder=a.folder, limit=a.limit)
        if a.json:
            return as_json(items)
        for s in items:
            out(_session_line(s))
            out(f"   первый промт: {short(' '.join(s['first_prompt'].split()), 300)}")
        if not items:
            st = agents.stats()
            out("Похожих сессий нет." + ("" if st["sessions"] else " Индекс пуст — ./ops agents index "
                                         "(папки — Настройки → Офис)."))
    elif a.action == "show":
        s = agents.show(a.id)
        if not s:
            fail(f"нет сессии {a.id} в индексе (./ops agents similar \"<слова>\")")
        if a.json:
            return as_json(s)
        out(_session_line(s))
        out(f"Папка: {s['cwd'] or s['folder']}" + (f" · навыки: {', '.join(s['skills'])}" if s["skills"] else "")
            + (f" · субагентов: {s['agents']}" if s["agents"] else "") + (f" · открыть: {s['link']}" if s["link"] else ""))
        out(f"\n## Первый промт\n{short(s['first_prompt'], a.max_chars)}")
        if s["prompts"]:
            out("\n## Что человек дописал потом (этого не хватило в первом промте)")
            for i, text in enumerate(s["prompts"], 2):
                out(f"{i}. {' '.join(text.split())}")
        for title, files in (("Прочитала", s["files_read"]), ("Создала и изменила", s["files_written"])):
            if files:
                out(f"\n## {title}")
                out("\n".join(f"- {p}" + ("" if Path(p).exists() else " (файла уже нет)") for p in files[:20]))
        if s["summary"]:
            out(f"\n## Последний ответ агента\n{short(s['summary'], 1500)}")
    elif a.action == "context":
        try:
            out(agents.context(a.id))
        except ValueError as e:
            fail(str(e))
    elif a.action == "brief-save":
        path = Path(a.file)
        if not path.is_absolute():  # Claude пишет от папки программы; данные обычно там же
            path = next((base / path for base in (config.ROOT, config.APP_DIR) if (base / path).exists()), config.ROOT / path)
        try:
            b = agents.save(a.id, path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as e:
            fail(str(e))
        out(f"Промт сохранён: {agents.brief_path(b)} ({len(b['prompt'])} символов, файлов: {len(b['files'])})")
        missing = [f["path"] for f in b["files"] if not f["exists"]]
        if missing:
            out("Этих файлов нет — исправь путь или убери строку и сохрани ещё раз: " + "; ".join(missing))
        if not b["files"]:
            out("В разделе «## Файлы» не нашлось строк «- <абсолютный путь> — зачем».")


# --- нужен ответ ----------------------------------------------------------------

def cmd_attention(a) -> None:
    if a.action == "list":
        items = records.list_attention("all" if a.all else "open")
        if a.json:
            return as_json(items)
        for x in items:
            marks = ""
            if x["status"] == "open" and x["reopened_at"]:
                marks += f" · возвращён вручную {store.local(x['reopened_at'])}"
            if x["done_hint"]:
                marks += " · предложено закрыть, ждёт директора"
            for c in x["tasks"]:  # по сообщению уже заведена задача — вторую по нему не заводить
                marks += f" · по сообщению уже есть задача [{c['id']}] «{c['headline']}» ({c['route']})"
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
    from opsdash import task_meetings

    items = task_meetings.for_events(overview.events_between(start, start + timedelta(days=a.days)))
    if a.json:
        return as_json(items)
    for e in items:
        who = ", ".join(x.get("name") or x.get("email") for x in e["attendees"][:10])
        tasks = "; ".join(f"[{t['id']}] {t['headline']}" for t in e["tasks"])
        out(f"{store.local(e['start'], '%d.%m %H:%M')}–{store.local(e['end'], '%H:%M')} {e['title']}"
            + (f" · {who}" if who else "") + (f" · по задаче {tasks}" if tasks else "") + f" · uid {e['uid']}")
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
                                                 batch=a.batch, reason=a.reason, source_ref=source_ref,
                                                 commitment_id=a.commitment)
        elif a.kind == "cancel-meeting":
            p = proposals.propose_calendar_cancel(uid=a.uid, batch=a.batch, reason=a.reason, source_ref=source_ref)
        elif a.kind == "link-meeting":
            p = proposals.propose_task_meeting(commitment_id=a.commitment, event_uid=a.uid, batch=a.batch,
                                               reason=a.reason, source_ref=source_ref)
        elif a.kind == "done":
            p = proposals.propose_task_done(commitment_id=a.commitment, quote=a.quote, batch=a.batch, reason=a.reason,
                                            source_ref=source_ref)
        elif a.kind == "wait":
            p = proposals.propose_task_wait(commitment_id=a.commitment, waits_for=a.waits_for, event=a.event,
                                            until=a.until, batch=a.batch, reason=a.reason, source_ref=source_ref)
        elif a.kind == "merge":
            p = proposals.propose_task_merge(commitment_id=a.commitment, into=a.into, quote=a.quote, batch=a.batch,
                                             reason=a.reason, source_ref=source_ref)
        elif a.kind == "log":
            p = proposals.propose_task_log(commitment_id=a.commitment, text=a.text, quote=a.quote, subtask=a.subtask,
                                           who=a.who, whom=a.whom, due=a.due, batch=a.batch, reason=a.reason,
                                           source_ref=source_ref)
        elif a.kind == "kaiten-block":
            p = proposals.propose_kaiten_block(card_id=a.card, blocker_card=a.blocker_card, text=a.text, batch=a.batch,
                                               reason=a.reason, source_ref=source_ref)
        elif a.kind == "project":
            p = proposals.propose_project(title=a.title, goal=a.goal, owner=a.owner, due=a.due,
                                          tasks=_ids(a.tasks), batch=a.batch, reason=a.reason, source_ref=source_ref)
        elif a.kind == "answered":
            p = proposals.propose_answered(attention_id=a.attention, quote=a.quote, batch=a.batch, reason=a.reason,
                                           source_ref=source_ref)
        elif a.kind == "remind":
            p = proposals.propose_reminder(commitment_id=a.commitment, text=a.text, batch=a.batch, reason=a.reason,
                                           source_ref=source_ref)
        elif a.kind == "rule":
            p = proposals.propose_rule(skill=a.skill, text=a.text, quote=a.quote, batch=a.batch, reason=a.reason,
                                       source_ref=source_ref)
        elif a.kind == "mcp":
            try:
                args = json.loads(a.args or "{}")
            except ValueError:
                fail("--args — JSON-объект: '{\"title\": \"…\"}'")
            p = proposals.propose_mcp_call(server_id=a.server, tool=a.tool, args=args, title=a.title or "",
                                           batch=a.batch, reason=a.reason, source_ref=source_ref)
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
    elif a.action == "rejected":  # материал для уроков «Нет» (навык lessons)
        from opsdash import skills

        items = skills.rejected(a.days)
        if a.json:
            return as_json(items)
        for p in items:
            why = p["reject_reason_text"] + (f" — {p['reject_note']}" if p["reject_note"] else "")
            out(f"[{p['id']}] {store.local(p['decided_at'])} · {p['kind']} · {p['title']}"
                + (f" · почему «Нет»: {why}" if why else " · причина не указана")
                + (f" · предлагали потому что: {p['reason']}" if p["reason"] else ""))
        if not items:
            out(f"За {a.days} дн. отклонённых нет.")


def cmd_skills(a) -> None:
    from opsdash import skills

    if a.action == "list":
        cat = skills.catalog()
        if a.json:
            return as_json(cat)
        for area in cat["areas"]:
            out(area["title"])
            for s in area["skills"]:
                out(f"  {s['id']} — {s['title']}: {s['label']}" + (f" · правил: {s['rules']}" if s["rules"] else ""))
    elif a.action == "rules":
        if not skills.get(a.skill):
            fail(f"нет навыка «{a.skill}» — ./ops skills list")
        items = skills.rules(a.skill)
        if a.json:
            return as_json(items)
        for r in items:
            out(f"[{r['id']}] {'вкл' if r['enabled'] else 'выкл'} · {r['text']}" + (" · урок из «Нет»" if r["source"] == "lesson" else ""))
        if not items:
            out("Правил нет.")


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
    sandbox = bool(os.environ.get("OPSDASH_SANDBOX"))  # ChatGPT в песочнице без сети (opsdash.codex)
    oid = bot.notify(text, batch=a.batch, urgent=a.urgent or a.kind == "urgent", send_now=not sandbox)
    row = store.row("SELECT sent_at, error FROM outbox WHERE id=?", (oid,))
    if row and row["sent_at"]:
        out("Отправлено в Telegram")
    elif sandbox:
        out("Поставлено в очередь — PULSE отправит в Telegram сразу, как ты закончишь. Повторно не отправляй")
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
    elif updates.libraries_busy():  # Windows, PULSE работает, а перезапуск не просили
        out("Библиотеки обновятся после перезапуска: запустите UPDATE.bat.")


# --- разбор аргументов -------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ops", description="PULSE — личный рабочий пульт. Флаг --json — вывод в JSON.")
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
    s = sub.add_parser("doctor", help="здоровье PULSE: система и подключения — что не так и что сделать")
    s.add_argument("--all", action="store_true", help="показать и выключенные источники")
    s.set_defaults(fn=cmd_doctor)
    s = sub.add_parser("check", help="проверить подключения")
    s.add_argument("what", nargs="?", default="all",
                   choices=["all", "claude", *config.SOURCES, "bot"])
    s.set_defaults(fn=cmd_check)
    s = sub.add_parser("collect", help="собрать данные сейчас")
    s.add_argument("what", nargs="?", default="all",
                   choices=["all", *config.SOURCES])
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
    x.add_argument("--source", choices=["telegram", "ymessenger", "pachca", "slack", "mail"])
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
    # решения по темам (только для --kind decision)
    x.add_argument("--stage", choices=["discussing", "decided", "waiting"],
                   help="решение: discussing — ждёт решения (открытый вопрос) | decided — принято (по умолчанию)")
    x.add_argument("--prev", type=int, help="решение: id прежнего решения по тому же вопросу (./ops decisions topic)")
    x.add_argument("--relation", choices=["changes", "refines", "follows", "cancels"], default="refines",
                   help="как новое связано с прежним: меняет | уточняет (по умолчанию) | продолжает | отменяет")
    x.add_argument("--question", help="решение: вопрос, 2–4 слова — «Розница: веса тимлида» (как у прежнего решения цепочки)")
    x.add_argument("--answer", help="решение: что по вопросу решено после него, коротко — «35 / 12,5 / 12,5 / 40»")
    x.add_argument("--metric", help="решение: что меняем — «средний чек апсейла ОРК»")
    x.add_argument("--before", help="решение: было — «12 тыс.»")
    x.add_argument("--after", help="решение: стало или цель — «15 тыс.»")
    x.add_argument("--waiting", help="решение: следующий шаг или чего ждём — «Олег обсудит с собственником до 15.11»")
    x.add_argument("--tasks", help="решение: id задач, которые его исполняют, через запятую")
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
    x = ms.add_parser("messages", help="переписка с начала встречи в чатах по её задачам — не выполнены ли они уже")
    x.add_argument("id")
    x.add_argument("--json", action="store_true")
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
    x.add_argument("--sort", default="priority", choices=[*records.SORTS, "due"],
                   help="priority — «в работе» сверху, дальше по сроку, в один день — Срочно → Низкий (как на доске); "
                        "due — по сроку; created — новые сверху")
    x.add_argument("--quiet", action="store_true",
                   help="только без движения: по задаче давно ничего не происходит (порог — Настройки → День)")
    x.add_argument("--search", help="найти по словам в заголовке, сути, цитате и именах")
    x.add_argument("--brief", action="store_true",
                   help="коротко, одной строкой: кто → кому, заголовок, срок; подробности — ./ops commitments show <id>")
    x = ms.add_parser("add")
    x.add_argument("--direction", required=True, choices=["mine", "to_me", "other"],
                   help="mine — задача директора; to_me — обещали директору; other — между коллегами")
    x.add_argument("--title", help="короткий заголовок для карточки, 2–6 слов: «Прогноз по ключевым клиентам»")
    x.add_argument("--what", required=True, help="суть: что именно сделать, с подробностями")
    x.add_argument("--who", help="кто должен сделать")
    x.add_argument("--whom", help="кому обещано")
    x.add_argument("--due", help="YYYY-MM-DD")
    x.add_argument("--source",
                   help="meeting:<id> | telegram:<message id> | ymessenger:<id> | pachca:<id> | slack:<id> | mail:<id> | manual")
    x.add_argument("--quote", help="дословная цитата")
    x.add_argument("--card", type=int, help="карточка Kaiten")
    x.add_argument("--note")
    x.add_argument("--parent", type=int, help="часть задачи с этим id: её работа передана директором дальше")
    x.add_argument("--force", action="store_true",
                   help="добавить, даже если у этой карточки Kaiten уже есть открытая задача")
    x.add_argument("--project", type=int, help="в проект с этим id (./ops projects list)")
    x.add_argument("--estimate", type=int, help="сколько займёт у директора, мин: 15, 30, 60, 120, 240 (полдня)")
    x.add_argument("--agent", help="можно поручить агенту: что он сделает («собрать сводку из выгрузки CRM»)")
    x.add_argument("--importance", choices=list(priorities.IMPORTANCE),
                   help="важность без учёта срока: high — руководителю, клиенту, деньги, «срочно»; low — «когда будет время»")
    x.add_argument("--importance-why", help="почему, 2–6 слов: «обещано собственнику»")
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
    x.add_argument("--project", type=int, help="в проект с этим id (0 — убрать из проекта); подзадачи — следом")
    x.add_argument("--in-work", choices=["on", "off"], help="отметка «В работе» (по просьбе директора)")
    x.add_argument("--archived", choices=["on", "off"], help="убрать в архив или вернуть (по просьбе директора)")
    x.add_argument("--remind", metavar="reminded|off|on|YYYY-MM-DD",
                   help="напоминания по задаче «мне должны» (по просьбе директора): reminded — напоминание уже отправлено лично директором; "
                        "YYYY-MM-DD — не напоминать до этой даты; off — больше не напоминать (до нового срока); on — снова")
    x.add_argument("--estimate", type=int, help="сколько займёт у директора, мин (оценку директора Claude не меняет)")
    x.add_argument("--agent", help="можно поручить агенту: что он сделает; \"\" — снять пометку")
    x.add_argument("--priority", metavar="urgent|high|normal|low|auto",
                   help="приоритет от директора (только по прямой просьбе): закреплён, расчёт его не меняет; "
                        "auto — снова по расчёту")
    x.add_argument("--importance", choices=list(priorities.IMPORTANCE),
                   help="оценка важности Claude: high | normal | low (срок PULSE учтёт сам)")
    x.add_argument("--importance-why", help="почему, 2–6 слов")
    x = ms.add_parser("importance", help="задачи без оценки важности; --set — сохранить оценки пачкой")
    x.add_argument("--set", metavar="\"<id>=<high|normal|low>:<почему>; …\"",
                   help="«12=high:обещано собственнику; 15=low:заявка в HR»")
    x = ms.add_parser("wait", help="задача ждёт другую задачу или событие: без него её не закончить")
    x.add_argument("id", type=int, help="задача, которая ждёт")
    x.add_argument("--for", dest="waits_for", type=int, help="id задачи, которую она ждёт")
    x.add_argument("--event", help="событие, которое она ждёт: «встреча Олега с собственником»; с --remove/--happened — "
                                   "часть его текста")
    x.add_argument("--until", help="когда событие ожидается, YYYY-MM-DD")
    x.add_argument("--note", help="почему ждёт (покажется на карточке)")
    x.add_argument("--source", help="откуда известно: meeting:<id> | telegram:<id> | pachca:<id> | …")
    x.add_argument("--remove", action="store_true", help="убрать связь")
    x.add_argument("--happened", action="store_true", help="событие случилось — задача его больше не ждёт")
    x = ms.add_parser("handoff", help="своя часть задачи готова, дальше — коллега (MR на ревью, документ на "
                                      "согласование): его часть — задача «мне должны», эта уходит из плана до её готовности")
    x.add_argument("id", type=int, help="задача директора")
    x.add_argument("--who", help="кто дальше (у кого теперь мяч)")
    x.add_argument("--what", help="что он должен сделать, результатом: «Ревью и мерж MR по признаку клиента»")
    x.add_argument("--due", help="к какому сроку, YYYY-MM-DD — если прозвучал")
    x.add_argument("--part", type=int, help="его часть уже записана задачей с этим id (вместо --who/--what)")
    x.add_argument("--text", help="что произошло, для хода задачи: «05.11 отправил MR на ревью Петру»")
    x.add_argument("--source", help="откуда: telegram:<id> | pachca:<id> | slack:<id> | ymessenger:<id> | mail:<id> | meeting:<id>")
    x.add_argument("--quote", help="дословная цитата")
    x = ms.add_parser("items", help="пункты внутри задачи (разделы, этапы, части): прогресс по частям")
    x.add_argument("id", type=int)
    x.add_argument("--add", action="append", help="добавить пункт (можно несколько раз или через «;»)")
    x.add_argument("--done", action="append", help="пункт готов (текст или его часть)")
    x.add_argument("--undo", action="append", help="пункт снова не готов")
    x.add_argument("--remove", action="append", help="убрать пункт")
    x.add_argument("--note", help="откуда известно, что готово (с --done)")
    x = ms.add_parser("show", help="задача целиком: суть, связи и ход задачи — что по ней происходило")
    x.add_argument("id", type=int)
    x = ms.add_parser("log", help="ход задачи: записать, что произошло (из переписки, встречи), или показать ход")
    x.add_argument("id", type=int)
    x.add_argument("--text", help="что произошло, одной-двумя фразами: кто, что сказал или сделал, даты; "
                                  "без --text — показать ход")
    x.add_argument("--kind", default="update", choices=["update", "note"],
                   help="update — что произошло (по умолчанию); note — комментарий директора (по просьбе директора)")
    x.add_argument("--ask", action="store_true",
                   help="спросили или попросили и ждём ответа: пока нет новой записи — «ответа нет N дней»")
    x.add_argument("--source", help="откуда: telegram:<id> | pachca:<id> | ymessenger:<id> | mail:<id> | "
                                    "meeting:<id> | kaiten:<card> | manual")
    x.add_argument("--quote", help="дословная цитата (у встречи — с [чч:мм:сс])")
    x.add_argument("--at", help="когда случилось, YYYY-MM-DD[ HH:MM]; по умолчанию — время сообщения или встречи")
    x.add_argument("--by", choices=["director", "claude"], help="кто пишет: director — запись по просьбе директора")
    x.add_argument("--remove", type=int, metavar="ЗАПИСЬ", help="убрать запись (номер — из ./ops commitments show)")
    x = ms.add_parser("merge", help="задача — дубль другой: закрыть её, источник, связи и подзадачи — в ту")
    x.add_argument("id", type=int, help="дубль: эта задача закроется")
    x.add_argument("--into", type=int, required=True, help="задача, которая остаётся")
    x.add_argument("--note", help="почему это одно и то же")
    s.set_defaults(fn=cmd_commitments)

    s = sub.add_parser("projects", help="проекты: задачи с общим результатом, прогресс и цепочка")
    ms = s.add_subparsers(dest="action", required=True)
    x = ms.add_parser("list", help="проекты с прогрессом и тем, на ком они стоят")
    x.add_argument("--all", action="store_true", help="и завершённые")
    x = ms.add_parser("show", help="проект: задачи по шагам — что можно делать сейчас, что ждёт")
    x.add_argument("id", type=int)
    for name in ("add", "set"):
        x = ms.add_parser(name, help="новый проект (по просьбе директора)" if name == "add" else "изменить проект")
        if name == "set":
            x.add_argument("id", type=int)
            x.add_argument("--status", choices=["active", "paused", "done"])
        x.add_argument("--title", required=name == "add", help="название — результат, а не тема")
        x.add_argument("--goal", help="что считаем готовым")
        x.add_argument("--owner", help="кто ведёт (пусто — директор)")
        x.add_argument("--due", help="YYYY-MM-DD")
        x.add_argument("--card", type=int, help="карточка проекта в Kaiten")
        x.add_argument("--tasks", help="добавить задачи: id через запятую")
    s.set_defaults(fn=cmd_projects)

    s = sub.add_parser("agents", help="поручения агенту: прошлые сессии Claude Code в папках «Офиса» и промты")
    ms = s.add_subparsers(dest="action", required=True)
    ms.add_parser("index", help="дочитать новые сессии в индекс")
    ms.add_parser("watch", help="найти сессии, запущенные из поручений; готовые — предложить закрыть задачу")
    x = ms.add_parser("examples", help="удачные промты прошлых поручений — образцы (агенту хватило 1–2 реплик)")
    x.add_argument("query", nargs="?", help="о чём задача — похожие первыми")
    x.add_argument("--limit", type=int, default=3)
    x = ms.add_parser("similar", help="прошлые сессии, похожие на задачу")
    x.add_argument("query")
    x.add_argument("--folder", help="только сессии в этой папке")
    x.add_argument("--limit", type=int, default=5)
    x.add_argument("--json", action="store_true")
    x = ms.add_parser("show", help="сессия: первый промт, что дописывали потом, файлы, итог")
    x.add_argument("id")
    x.add_argument("--max-chars", type=int, default=6000, help="первый промт — не длиннее")
    x.add_argument("--json", action="store_true")
    x = ms.add_parser("context", help="всё для промта по поручению: задача, источник, похожие сессии, файлы")
    x.add_argument("id", type=int, help="номер поручения")
    x = ms.add_parser("brief-save", help="сохранить готовый промт из файла (Markdown)")
    x.add_argument("id", type=int, help="номер поручения")
    x.add_argument("--file", required=True)
    s.set_defaults(fn=cmd_agents)

    s = sub.add_parser("attention", help="где ждут ответа директора")
    ms = s.add_subparsers(dest="action", required=True)
    x = ms.add_parser("list")
    x.add_argument("--all", action="store_true")
    x = ms.add_parser("add")
    x.add_argument("--message", type=int, help="id сообщения из ./ops messages")
    x.add_argument("--summary", required=True, help="суть: что от директора хотят")
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

    s = sub.add_parser("day", help="реальность дня: окна между встречами, сколько задач, вердикт и план (без ИИ)")
    s.add_argument("--date", help="YYYY-MM-DD, по умолчанию сегодня")
    ms = s.add_subparsers(dest="action")
    x = ms.add_parser("estimates", help="задачи директора без оценки времени; --set — сохранить оценки пачкой")
    x.add_argument("--set", help="«<id>=<минуты>,<id>=<минуты>»: 12=30,15=60")
    x = ms.add_parser("week", help="неделя: окна и задачи по дням — «На неделю: окон 9 ч, задач 14 ч — перегруз с четверга»")
    x.add_argument("--date", dest="week_date", help="любой день нужной недели, YYYY-MM-DD; по умолчанию — эта")
    x = ms.add_parser("capacity", help="успевает ли директор к сроку: окна до него против задач с этим сроком и раньше")
    x.add_argument("--until", required=True, help="срок, YYYY-MM-DD")
    x.add_argument("--estimate", type=int, help="сколько займёт новая задача, мин (по умолчанию 30)")
    x.add_argument("--task", type=int, help="задача, которую проверяем (уже записана): её оценка, саму её не считаем")
    s.set_defaults(fn=cmd_day, action=None)

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
    x.add_argument("--commitment", type=int, help="встреча по этой задаче — после создания она будет на её карточке")
    x = ps.add_parser("link-meeting", parents=[common],
                      help="связать задачу со встречей календаря, которая ей посвящена (задача ждёт встречи открытой)")
    x.add_argument("--commitment", type=int, required=True, help="id задачи (./ops commitments list)")
    x.add_argument("--uid", required=True, help="uid встречи (./ops calendar)")
    x = ps.add_parser("cancel-meeting", parents=[common], help="отменить встречу")
    x.add_argument("--uid", required=True)
    x = ps.add_parser("rule", parents=[common],
                      help="правило для навыка из уроков «Нет» (навык lessons) — включится после «Да» директора")
    x.add_argument("--skill", required=True, help="навык: ./ops skills list")
    x.add_argument("--text", required=True, help="правило обычными словами — как его прочтёт навык")
    x.add_argument("--quote", help="на чём основано: какие предложения и с какой причиной отклонены")
    x = ps.add_parser("mcp", parents=[common],
                      help="изменение в сервисе, подключённом через MCP (Linear, Notion…) — PULSE выполнит после «Да»")
    x.add_argument("--server", required=True, help="подключение: ./ops mcp list")
    x.add_argument("--tool", required=True, help="инструмент изменения: ./ops mcp tools <подключение>")
    x.add_argument("--args", help="параметры инструмента — JSON-объект")
    x.add_argument("--title", help="что сделать — по-человечески: «Создать задачу в Linear: Прогноз по клиентам»")
    x = ps.add_parser("remind", parents=[common], help="напомнить исполнителю задачи «мне должны» в Telegram (после «Да»)")
    x.add_argument("--commitment", type=int, required=True, help="id задачи «мне должны»")
    x.add_argument("--text", help="свой текст; без него — по шаблону из настроек")
    x = ps.add_parser("done", parents=[common], help="закрыть задачу PULSE, которая выглядит выполненной")
    x.add_argument("--commitment", type=int, required=True, help="id задачи (./ops commitments list)")
    x.add_argument("--quote", help="дословно, откуда видно, что сделано ([чч:мм:сс] для встречи)")
    x = ps.add_parser("wait", parents=[common],
                      help="задача ждёт другую задачу или событие — когда это понятно по смыслу, но не сказано прямо")
    x.add_argument("--commitment", type=int, required=True, help="id задачи, которая ждёт")
    x.add_argument("--for", dest="waits_for", type=int, help="id задачи, которую она ждёт")
    x.add_argument("--event", help="событие, которое она ждёт")
    x.add_argument("--until", help="когда событие ожидается, YYYY-MM-DD")
    x = ps.add_parser("merge", parents=[common], help="объединить дубль: одна и та же задача записана дважды")
    x.add_argument("--commitment", type=int, required=True, help="id дубля — эта задача закроется")
    x.add_argument("--into", type=int, required=True, help="id задачи, которая остаётся")
    x.add_argument("--quote", help="дословно, откуда видно, что это одно и то же")
    x = ps.add_parser("log", parents=[common],
                      help="дописать в ход задачи сообщение, которое относится к ней по теме, а не по людям (после «Да»); "
                           "с --subtask — ещё и подзадача")
    x.add_argument("--commitment", type=int, required=True, help="id задачи (./ops commitments list --brief)")
    x.add_argument("--text", required=True, help="запись в ход: что произошло, одной-двумя фразами, с датами ДД.ММ")
    x.add_argument("--quote", help="дословно из сообщения")
    x.add_argument("--subtask", help="заголовок подзадачи, 2–6 слов: в сообщении — новое «сделаю» по этой задаче")
    x.add_argument("--whom", help="подзадача директора: кому это сказано")
    x.add_argument("--who", help="подзадачу обещал коллега — кто (тогда она «мне должны»)")
    x.add_argument("--due", help="срок подзадачи, YYYY-MM-DD — если прозвучал")
    x = ps.add_parser("kaiten-block", parents=[common],
                      help="заблокировать карточку Kaiten: её задача ждёт другую (--blocker-card) или причину (--text)")
    x.add_argument("--card", type=int, required=True, help="карточка, которая ждёт")
    x.add_argument("--blocker-card", type=int, help="карточка, которую она ждёт")
    x.add_argument("--text", help="причина блокировки текстом")
    x = ps.add_parser("project", parents=[common], help="собрать задачи в новый проект (после «Да» директора)")
    x.add_argument("--title", required=True, help="название — результат: «Запуск склада в Казани»")
    x.add_argument("--goal", help="что считаем готовым")
    x.add_argument("--owner", help="кто ведёт (пусто — директор)")
    x.add_argument("--due", help="срок проекта, YYYY-MM-DD")
    x.add_argument("--tasks", required=True, help="id задач через запятую: 57,69,80")
    x = ps.add_parser("answered", parents=[common],
                      help="убрать «нужен ответ»: по сообщению директора видно, что ответ уже дан")
    x.add_argument("--attention", type=int, required=True, help="id пункта (./ops attention list)")
    x.add_argument("--quote", help="ответ директора дословно; --source — это сообщение (telegram:<id>)")
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
    x = ms.add_parser("rejected", help="отклонённые за последние дни — с причинами «Нет» (для уроков)")
    x.add_argument("--days", type=int, default=14)
    s.set_defaults(fn=cmd_proposals)

    s = sub.add_parser("mcp", help="сервисы, подключённые через MCP: что подключено, инструменты чтения и изменения")
    ms = s.add_subparsers(dest="action", required=True)
    ms.add_parser("list", help="подключения и инструменты: что читаешь сам, что — через «Да»")
    x = ms.add_parser("tools", help="инструменты подключения с описанием")
    x.add_argument("id")
    x = ms.add_parser("check", help="подключиться и обновить список инструментов")
    x.add_argument("id")
    ms.add_parser("serve", help="сам PULSE как MCP-сервер только для чтения — для сессий Claude Code в любой папке "
                                "(подключается наверху Настройки → Подключения)")
    s.set_defaults(fn=cmd_mcp)

    s = sub.add_parser("decisions", help="решения по темам: что действует, цепочки, следующий шаг")
    ms = s.add_subparsers(dest="action", required=True)
    x = ms.add_parser("list", help="действующие решения, новые сверху")
    x.add_argument("--topic", help="тема — как в ./ops decisions topics")
    x.add_argument("--project", help="точное направление с подтемой: «Логистика / Склад»")
    x.add_argument("--stage", choices=["discussing", "decided"], help="ждёт решения | принято")
    x.add_argument("--meeting", help="решения одной встречи — id встречи")
    x.add_argument("--attention", action="store_true", help="только ждут решения, а следующего шага нет")
    x.add_argument("--all", action="store_true", help="и заменённые, отменённые — вся история")
    x.add_argument("--search", help="найти по словам")
    x.add_argument("--limit", type=int, default=200)
    ms.add_parser("topics", help="темы: о чём, сколько вопросов, ждут решения")
    x = ms.add_parser("topic", help="тема: о чём, ждут решения, что решено по вопросам и история")
    x.add_argument("name")
    x = ms.add_parser("show")
    x.add_argument("id", type=int)
    x = ms.add_parser("set", help="поля решения; пустая строка — очистить поле, --prev 0 — убрать связь с прежним")
    x.add_argument("id", type=int)
    x.add_argument("--stage", choices=["discussing", "decided", "waiting", "implemented"],
                   help="ждёт решения | принято")
    x.add_argument("--prev", type=int, help="id прежнего решения по тому же вопросу; 0 — убрать связь")
    x.add_argument("--relation", choices=["changes", "refines", "follows", "cancels"],
                   help="меняет | уточняет | продолжает | отменяет прежнее")
    x.add_argument("--topic", help="тема (с подтемой через « / »): перенести решение")
    x.add_argument("--question", help="вопрос, 2–4 слова: «Розница: веса тимлида»; пустая строка — убрать")
    x.add_argument("--answer", help="что по вопросу решено после этого решения, коротко; пустая строка — убрать")
    x.add_argument("--metric")
    x.add_argument("--before")
    x.add_argument("--after")
    x.add_argument("--next", help="следующий шаг или чего ждём")
    x.add_argument("--waiting", help=argparse.SUPPRESS)
    x = ms.add_parser("about", help="о чём тема — одной строкой")
    x.add_argument("name")
    x.add_argument("--text", required=True)
    x = ms.add_parser("rename-topic", help="переименовать тему или влить в другую — подтемы сохранятся")
    x.add_argument("name")
    x.add_argument("--to", required=True)
    x = ms.add_parser("link", help="задача исполняет решение")
    x.add_argument("id", type=int, help="id решения")
    x.add_argument("--task", required=True, help="id задач через запятую")
    x = ms.add_parser("unlink", help="убрать связь задачи с решением")
    x.add_argument("--task", type=int, required=True)
    s.set_defaults(fn=cmd_decisions)

    s = sub.add_parser("skills", help="навыки PULSE и «Ваши правила» к ним")
    ms = s.add_subparsers(dest="action", required=True)
    ms.add_parser("list", help="навыки: что делают, сколько правил")
    x = ms.add_parser("rules", help="правила директора к навыку")
    x.add_argument("skill")
    s.set_defaults(fn=cmd_skills)

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
