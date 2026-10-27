"""Очередь подтверждений.

Claude никогда не пишет во внешние системы напрямую: он создаёт «предложение»
(создать карточку, оставить комментарий, назначить встречу). Предложение
выполняется, когда директор нажимает «Да» в боте или в админке — либо сразу,
если в настройках автономии для этого действия выбрано «auto».
"""

from __future__ import annotations

import json

from opsdash import config, records, store
from opsdash.sources import kaiten

KINDS = {
    "kaiten.create_card": "Создать карточку в Kaiten",
    "kaiten.comment": "Комментарий в Kaiten",
    "kaiten.update": "Изменить карточку Kaiten",
    "calendar.create": "Создать встречу в календаре",
    "calendar.cancel": "Отменить встречу",
}


class ProposalError(Exception):
    pass


def _fmt_date(d: str | None) -> str:
    if not d:
        return ""
    try:
        y, m, dd = d[:10].split("-")
        return f"{dd}.{m}"
    except ValueError:
        return d


def _create(kind: str, title: str, payload: dict, *, autonomy_key: str, reason: str | None,
            source_ref: str | None, batch: str | None) -> dict:
    dup = store.row(
        "SELECT id FROM proposals WHERE kind=? AND title=? AND status='pending'", (kind, title)
    )
    if dup:
        return get(dup["id"])
    pid = store.execute(
        """INSERT INTO proposals(kind, title, payload, reason, source_ref, batch, status, created)
           VALUES(?,?,?,?,?,?,'pending',?)""",
        (kind, title, store.json_col(payload), reason, source_ref, batch, store.utcnow()),
    )
    settings = config.load_settings()
    if settings["autonomy"].get(autonomy_key) == "auto":
        return approve(pid)
    return get(pid)


def get(pid: int) -> dict | None:
    p = store.row("SELECT * FROM proposals WHERE id=?", (pid,))
    if p:
        p["payload"] = store.loads(p["payload"], {})
        p["result"] = store.loads(p["result"], p["result"])
    return p


def list_proposals(status: str | None = "pending", batch: str | None = None, limit: int = 200) -> list[dict]:
    sql = "SELECT * FROM proposals WHERE 1=1"
    params: list = []
    if status and status != "all":
        sql += " AND status=?"
        params.append(status)
    if batch:
        sql += " AND batch=?"
        params.append(batch)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    out = store.rows(sql, params)
    for p in out:
        p["payload"] = store.loads(p["payload"], {})
        p["result"] = store.loads(p["result"], p["result"])
    return out


# --- создание предложений ---------------------------------------------------

def propose_kaiten_card(*, title: str, description: str = "", due: str | None = None,
                        responsible: str | None = None, target: str | None = None,
                        commitment_id: int | None = None, batch: str | None = None,
                        reason: str | None = None, source_ref: str | None = None) -> dict:
    settings = config.load_settings()
    t = kaiten.find_target(target, settings)
    if not t:
        if not kaiten.targets(settings):
            raise ProposalError("В настройках Kaiten не задано, куда создавать карточки (раздел «Куда создавать»)")
        names = ", ".join(x["name"] for x in kaiten.targets(settings))
        raise ProposalError(f"Нет направления «{target}». Есть: {names}")
    user = kaiten.resolve_user(responsible) if responsible else None
    me = store.get_state("kaiten_me") or {}
    # «на себя» — только если исполнитель не указан или это точно директор;
    # не найденный в Kaiten человек считается коллегой (нужно подтверждение)
    for_me = (not responsible) or bool(user and me.get("id") and user.get("id") == me.get("id"))
    payload = {
        "title": title.strip(),
        "description": description.strip(),
        "due": due,
        "target": t["name"],
        "board_id": t.get("board_id"),
        "column_id": t.get("column_id"),
        "lane_id": t.get("lane_id"),
        "space_id": t.get("space_id"),
        "responsible_id": (user or {}).get("id"),
        "responsible_name": (user or {}).get("full_name") or (responsible if user else None),
        "responsible_unresolved": responsible if (responsible and not user) else None,
        "commitment_id": commitment_id,
    }
    who = payload["responsible_name"] or "без исполнителя"
    label = f"Карточка «{payload['title']}» → {who}"
    if due:
        label += f", до {_fmt_date(due)}"
    key = "kaiten_create_for_me" if for_me else "kaiten_create_for_others"
    return _create("kaiten.create_card", label, payload, autonomy_key=key, reason=reason,
                   source_ref=source_ref, batch=batch)


def _card_title(card_id: int) -> str:
    c = store.row("SELECT title FROM kaiten_cards WHERE id=?", (card_id,))
    return f"#{card_id} «{c['title']}»" if c else f"#{card_id}"


def propose_kaiten_comment(*, card_id: int, text: str, batch: str | None = None, reason: str | None = None,
                           source_ref: str | None = None) -> dict:
    payload = {"card_id": int(card_id), "text": text.strip()}
    label = f"Комментарий к {_card_title(int(card_id))}: {text.strip()[:80]}"
    return _create("kaiten.comment", label, payload, autonomy_key="kaiten_comment", reason=reason,
                   source_ref=source_ref, batch=batch)


def propose_kaiten_update(*, card_id: int, due: str | None = None, title: str | None = None,
                          responsible: str | None = None, column_id: int | None = None,
                          batch: str | None = None, reason: str | None = None, source_ref: str | None = None) -> dict:
    fields: dict = {}
    changes = []
    if due:
        fields["due_date"] = due
        changes.append(f"срок → {_fmt_date(due)}")
    if title:
        fields["title"] = title
        changes.append(f"название → «{title}»")
    if column_id:
        fields["column_id"] = int(column_id)
        changes.append("перенос в другую колонку")
    payload: dict = {"card_id": int(card_id), "fields": fields}
    if responsible:
        user = kaiten.resolve_user(responsible)
        if not user:
            raise ProposalError(f"Не нашла в Kaiten пользователя «{responsible}»")
        payload["responsible_id"] = user["id"]
        changes.append(f"исполнитель → {user.get('full_name') or responsible}")
    if not changes:
        raise ProposalError("Нечего менять: укажите срок, название, исполнителя или колонку")
    label = f"Изменить {_card_title(int(card_id))}: {', '.join(changes)}"
    return _create("kaiten.update", label, payload, autonomy_key="kaiten_update", reason=reason,
                   source_ref=source_ref, batch=batch)


def propose_calendar_event(*, title: str, start: str, end: str, attendees: list[str] | None = None,
                           description: str = "", calendar: str = "", batch: str | None = None,
                           reason: str | None = None, source_ref: str | None = None) -> dict:
    s = store.parse_local(start)
    e = store.parse_local(end)
    if not s or not e or e <= s:
        raise ProposalError("Неверное время встречи: нужны start и end в формате 2026-10-27T15:00")
    start, end = s.isoformat(), e.isoformat()
    payload = {"title": title, "start": start, "end": end, "attendees": attendees or [], "description": description,
               "calendar": calendar}
    label = f"Встреча «{title}» {store.local(start, '%d.%m %H:%M')}–{store.local(end, '%H:%M')}"
    if attendees:
        label += f" ({len(attendees)} участн.)"
    if calendar:
        label += f" в календаре «{calendar}»"
    return _create("calendar.create", label, payload, autonomy_key="calendar", reason=reason,
                   source_ref=source_ref, batch=batch)


def propose_calendar_cancel(*, uid: str, batch: str | None = None, reason: str | None = None,
                            source_ref: str | None = None) -> dict:
    if uid.startswith("google:"):
        raise ProposalError("Встреча из Google Календаря — PULSE его только читает, отменить её можно только в Google")
    ev = store.row("SELECT * FROM events WHERE uid=?", (uid,))
    if not ev:
        raise ProposalError(f"Нет встречи с uid {uid} (обновите календарь: ./ops collect calendar)")
    label = f"Отменить встречу «{ev['title']}» {store.local(ev['start'])}"
    return _create("calendar.cancel", label, {"uid": uid}, autonomy_key="calendar", reason=reason,
                   source_ref=source_ref, batch=batch)


# --- решение ----------------------------------------------------------------

def reject(pid: int) -> dict:
    store.execute(
        "UPDATE proposals SET status='rejected', decided_at=? WHERE id=? AND status='pending'", (store.utcnow(), pid)
    )
    return get(pid)


def approve(pid: int) -> dict:
    p = get(pid)
    if not p:
        raise ProposalError(f"Нет предложения #{pid}")
    if p["status"] != "pending":
        return p
    # помечаем заранее, чтобы двойное нажатие не создало две карточки
    claimed = store.execute(
        "UPDATE proposals SET status='running', decided_at=? WHERE id=? AND status='pending'", (store.utcnow(), pid)
    )
    if not claimed:
        return get(pid)
    try:
        result = execute(p)
        store.execute("UPDATE proposals SET status='done', result=? WHERE id=?", (store.json_col(result), pid))
        store.log("proposals", f"#{pid} выполнено: {p['title']}")
    except Exception as e:  # noqa: BLE001 — ошибку показываем директору как есть
        store.execute("UPDATE proposals SET status='failed', result=? WHERE id=?", (store.json_col({"error": str(e)}), pid))
        store.log("proposals", f"#{pid} ошибка: {e}", "error")
    return get(pid)


def approve_batch(batch: str) -> list[dict]:
    return [approve(p["id"]) for p in reversed(list_proposals("pending", batch=batch))]


def execute(p: dict) -> dict:
    kind, pl = p["kind"], p["payload"]
    if kind == "kaiten.create_card":
        kc = kaiten.client()
        desc = pl.get("description") or ""
        if pl.get("responsible_unresolved"):
            desc += f"\n\nИсполнитель по разбору: {pl['responsible_unresolved']} (не найден в Kaiten)"
        card = kc.create_card(
            board_id=pl["board_id"], column_id=pl.get("column_id"), lane_id=pl.get("lane_id"), title=pl["title"],
            description=desc.strip(), due_date=pl.get("due"),
        )
        url = kc.card_url(card, pl.get("space_id"))
        warning = None
        if pl.get("responsible_id"):
            # карточка уже создана: ошибка назначения не должна выглядеть как «не создано» (иначе — дубль)
            try:
                kc.set_responsible(card["id"], pl["responsible_id"])
            except Exception as e:  # noqa: BLE001
                warning = f"карточка создана, но исполнителя назначить не удалось: {e}"
                store.log("proposals", warning, "warning")
        try:
            fresh = kc.card(card["id"])
            space_title = next(
                (s.get("title") for s in config.load_settings()["kaiten"].get("spaces") or []
                 if int(s["id"]) == int(pl.get("space_id") or 0)), None,
            )
            kaiten.save_card(kaiten.normalize_card(fresh, kc, pl.get("space_id"), space_title))
        except Exception:  # noqa: BLE001 — зеркало обновится при следующей синхронизации
            pass
        if pl.get("commitment_id"):
            records.update_commitment(int(pl["commitment_id"]), kaiten_card_id=card["id"])
        return {"card_id": card["id"], "url": url, **({"warning": warning} if warning else {})}
    if kind == "kaiten.comment":
        kaiten.client().comment(pl["card_id"], pl["text"])
        return {"card_id": pl["card_id"]}
    if kind == "kaiten.update":
        kc = kaiten.client()
        if pl.get("fields"):
            kc.update_card(pl["card_id"], pl["fields"])
        if pl.get("responsible_id"):
            kc.set_responsible(pl["card_id"], pl["responsible_id"])
        return {"card_id": pl["card_id"]}
    if kind == "calendar.create":
        from opsdash.sources import calendar

        uid = calendar.create_event(pl["title"], pl["start"], pl["end"], pl.get("attendees") or [], pl.get("description") or "",
                                    calendar_name=pl.get("calendar") or "")
        return {"uid": uid}
    if kind == "calendar.cancel":
        from opsdash.sources import calendar

        calendar.cancel_event(pl["uid"])
        return {"uid": pl["uid"]}
    raise ProposalError(f"Неизвестный тип предложения: {kind}")


def describe(p: dict) -> str:
    """Подробное описание для админки и бота."""
    pl = p["payload"]
    lines = [p["title"]]
    if p["kind"] == "kaiten.create_card":
        lines.append(f"Куда: {pl.get('target')}")
        if pl.get("responsible_unresolved"):
            lines.append(f"⚠ Исполнитель «{pl['responsible_unresolved']}» не найден в Kaiten — карточка будет без исполнителя")
        if pl.get("description"):
            lines.append(pl["description"][:500])
    elif p["kind"] == "kaiten.comment":
        lines.append(pl.get("text", "")[:800])
    elif p["kind"] == "calendar.create" and pl.get("attendees"):
        lines.append("Участники: " + ", ".join(pl["attendees"]))
    if p.get("reason"):
        lines.append(f"Почему: {p['reason']}")
    return "\n".join(lines)


def result_text(p: dict) -> str:
    if p["status"] == "done":
        res = p.get("result") or {}
        if isinstance(res, dict) and res.get("warning"):
            return f"✅ Готово с оговоркой: {res['warning']} — {res.get('url', '')}"
        if isinstance(res, dict) and res.get("url"):
            return f"✅ Готово: {res['url']}"
        return "✅ Готово"
    if p["status"] == "failed":
        res = p.get("result") or {}
        err = res.get("error") if isinstance(res, dict) else res
        return f"❌ Не получилось: {err}"
    if p["status"] == "rejected":
        return "✖ Отклонено"
    return "⏳ Ждёт подтверждения"


def dumps(p: dict) -> str:
    return json.dumps(p, ensure_ascii=False, indent=2)
