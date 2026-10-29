"""Расход Claude: сколько PULSE запускал Claude, сколько токенов ушло и сколько это стоило бы по ценам API,
и насколько заполнены лимиты подписки — для «Настройки → Claude».

Каждый запуск Claude Code сообщает свою стоимость по ценам API (на подписке Pro / Max денег не берут —
расходуется лимит) и токены по моделям. Лимиты подписки (5 часов и неделя) он сообщает в начале запуска —
одним числом на весь аккаунт, вместе с вашей работой в Claude; отделить из них долю PULSE нельзя.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from opsdash import config, store

WINDOWS = {"five_hour": "5 часов", "seven_day": "неделя"}


def record(job: str, model: str | None, data: dict | None, limits: dict | None) -> None:
    """Запуск закончился (или упал, но успел потратить токены) — запомнить расход; лимиты — если пришли."""
    try:
        if limits:
            store.set_state("claude_limits", {**limits, "at": store.utcnow()})
        if not data:
            return
        models = data.get("modelUsage") or {}
        main = max(models, key=lambda m: (models[m] or {}).get("costUSD") or 0) if models else (model or "")
        fresh = cached = out = 0
        for u in models.values():
            fresh += int(u.get("inputTokens") or 0) + int(u.get("cacheCreationInputTokens") or 0)
            cached += int(u.get("cacheReadInputTokens") or 0)
            out += int(u.get("outputTokens") or 0)
        store.execute("INSERT INTO claude_runs(job, model, created, duration_s, cost_usd, fresh_tokens, cached_tokens, "
                      "output_tokens, ok) VALUES(?,?,?,?,?,?,?,?,?)",
                      (job, main, store.utcnow(), round((data.get("duration_ms") or 0) / 1000, 1),
                       float(data.get("total_cost_usd") or 0), fresh, cached, out, 0 if data.get("is_error") else 1))
    except Exception as e:  # noqa: BLE001 — учёт не должен мешать самому запуску
        store.log("usage", f"расход Claude не записан: {e}", "warning")


def limits() -> dict | None:
    """Лимиты подписки по последнему запуску: [{title, percent, resets}], when — когда узнали."""
    data = store.get_state("claude_limits") or {}
    windows = []
    for key, title in WINDOWS.items():
        w = data.get(key) or {}
        if w.get("utilization") is None:
            continue
        resets = datetime.fromtimestamp(w["resetsAt"], tz=timezone.utc) if w.get("resetsAt") else None
        windows.append({"key": key, "title": title, "percent": round(float(w["utilization"]) * 100),
                        "resets": resets.astimezone(config.tz()) if resets else None})
    return {"windows": windows, "at": data.get("at")} if windows else None


def _backfill() -> None:
    """Запуски до появления учёта — из журнала («Claude закончил за 87 с, ≈$0.85»): стоимость без токенов."""
    if store.get_state("claude_runs_backfilled"):
        return
    store.set_state("claude_runs_backfilled", True)
    if store.scalar("SELECT COUNT(*) FROM claude_runs"):
        first = store.scalar("SELECT MIN(created) FROM claude_runs")
    else:
        first = "9999"
    for r in store.rows("SELECT ts, job, message FROM log WHERE message LIKE 'Claude закончил%' AND ts < ? ORDER BY id",
                        (first,)):
        m = re.search(r"за (\d+) с(?:, ≈\$([\d.]+))?", r["message"])
        if m:
            store.execute("INSERT INTO claude_runs(job, created, duration_s, cost_usd) VALUES(?,?,?,?)",
                          (r["job"], r["ts"], float(m.group(1)), float(m.group(2) or 0)))


def summary(days: int = 30) -> dict:
    """Расход PULSE: за 7 и за 30 дней, по задачам, по дням (14 последних) и прикидка на месяц."""
    from opsdash import jobs

    _backfill()
    now = datetime.now(timezone.utc)

    def since(d: int) -> str:
        return (now - timedelta(days=d)).replace(microsecond=0).isoformat()

    def totals(d: int) -> dict:
        r = store.row("SELECT COUNT(*) AS runs, COALESCE(SUM(cost_usd), 0) AS cost, COALESCE(SUM(fresh_tokens), 0) AS fresh, "
                      "COALESCE(SUM(cached_tokens), 0) AS cached, COALESCE(SUM(output_tokens), 0) AS out, "
                      "SUM(fresh_tokens IS NULL) AS unknown FROM claude_runs WHERE created >= ?", (since(d),))
        # у запусков, перенесённых из журнала, токенов нет — тогда сумма токенов неполная, её не показываем
        r["tokens"] = r["fresh"] + r["cached"] + r["out"] if not r["unknown"] else None
        return r

    week, month = totals(7), totals(days)
    by_job = store.rows("SELECT job, COUNT(*) AS runs, SUM(cost_usd) AS cost, AVG(duration_s) AS seconds "
                        "FROM claude_runs WHERE created >= ? GROUP BY job ORDER BY cost DESC", (since(days),))
    for j in by_job:
        j["title"] = jobs.TITLES.get(j["job"], j["job"])
        j["share"] = round(100 * (j["cost"] or 0) / month["cost"]) if month["cost"] else 0
        j["per_run"] = (j["cost"] or 0) / j["runs"] if j["runs"] else 0
    tz = config.tz()
    days_list = []
    for i in range(13, -1, -1):
        day = (datetime.now(tz) - timedelta(days=i)).date()
        start = datetime.combine(day, datetime.min.time(), tz).astimezone(timezone.utc).isoformat()
        end = datetime.combine(day + timedelta(days=1), datetime.min.time(), tz).astimezone(timezone.utc).isoformat()
        r = store.row("SELECT COUNT(*) AS runs, COALESCE(SUM(cost_usd), 0) AS cost FROM claude_runs "
                      "WHERE created >= ? AND created < ?", (start, end))
        days_list.append({"day": day, "runs": r["runs"], "cost": r["cost"]})
    top = max((d["cost"] for d in days_list), default=0) or 1
    for d in days_list:
        d["bar"] = round(100 * d["cost"] / top)
    first = store.parse_ts(store.scalar("SELECT MIN(created) FROM claude_runs WHERE created >= ?", (since(7),)))
    span = max(1.0, min(7.0, (now - first).total_seconds() / 86400)) if first else 7.0
    return {"week": week, "month": month, "by_job": by_job, "days": days_list,
            "per_month": week["cost"] / span * 30 if week["runs"] else 0, "span_days": round(span, 1)}
