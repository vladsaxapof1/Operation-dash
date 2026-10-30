"""Расход Claude в «Настройки → Claude»: каждый запуск — токены и стоимость по ценам API, лимиты подписки."""

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient

from opsdash import brain, store, usage

RESET = int((datetime.now(timezone.utc) + timedelta(hours=3)).timestamp())
FAKE = f'''#!{sys.executable}
import json, sys
sys.stdin.read()
print(json.dumps({{"type": "system", "subtype": "init", "session_id": "s"}}))
print(json.dumps({{"type": "rate_limit_event", "rate_limit_info": {{"status": "allowed", "unifiedWindows": {{
    "five_hour": {{"utilization": 0.17, "resetsAt": {RESET}}}, "seven_day": {{"utilization": 0.34, "resetsAt": {RESET}}}}}}}}}))
print(json.dumps({{"type": "result", "subtype": "success", "result": "ок", "session_id": "s", "duration_ms": 14000,
    "total_cost_usd": 0.21, "modelUsage": {{
        "claude-sonnet-5-5": {{"inputTokens": 3, "outputTokens": 1300, "cacheReadInputTokens": 215000,
                               "cacheCreationInputTokens": 42900, "costUSD": 0.2}},
        "claude-haiku-5-5": {{"inputTokens": 1, "outputTokens": 10, "cacheReadInputTokens": 1000,
                              "cacheCreationInputTokens": 0, "costUSD": 0.01}}}}}}))
'''


@pytest.fixture
def client():
    from opsdash.web.app import app

    return TestClient(app, base_url="http://localhost:8765")


@pytest.fixture
def fake_claude(tmp_path, settings_with):
    if os.name == "nt":
        pytest.skip("поддельный claude — скрипт с #!, Windows его не запускает")
    path = tmp_path / "claude"
    path.write_text(FAKE, encoding="utf-8")
    path.chmod(0o755)
    settings_with(claude={"path": str(path)})


def test_every_run_records_usage_and_limits(fake_claude):
    assert brain.run("Разбери новые сообщения", model="sonnet", job="triage")["text"] == "ок"
    [r] = store.rows("SELECT * FROM claude_runs")
    assert (r["job"], r["model"], r["cost_usd"], r["duration_s"]) == ("triage", "claude-sonnet-5-5", 0.21, 14.0)
    assert (r["fresh_tokens"], r["cached_tokens"], r["output_tokens"]) == (42904, 216000, 1310)
    lim = usage.limits()
    assert [(w["title"], w["percent"]) for w in lim["windows"]] == [("5 часов", 17), ("неделя", 34)]
    assert lim["windows"][0]["resets"] is not None


def test_summary_backfills_history_from_journal(settings_with):
    store.log("process-meeting", "Claude закончил за 87 с, ≈$0.85 (в рамках подписки)")
    store.log("triage", "Claude закончил за 14 с, ≈$0.21 (в рамках подписки)")
    store.log("triage", "Claude закончил за 12 с")
    s = usage.summary()
    assert s["week"]["runs"] == 3 and round(s["week"]["cost"], 2) == 1.06
    assert [(j["job"], j["runs"], j["share"]) for j in s["by_job"]] == [("process-meeting", 1, 80), ("triage", 2, 20)]
    assert s["by_job"][0]["title"] == "Разбор встречи (Claude)" and s["days"][-1]["runs"] == 3 and s["days"][-1]["bar"] == 100
    assert round(s["per_month"]) == round(1.06 * 30)  # история — меньше суток: месяц ≈ 30 таких дней
    assert usage.summary()["week"]["runs"] == 3  # из журнала — один раз


def test_settings_page_and_refresh(client, fake_claude):
    page = client.get("/settings?tab=claude").text
    assert "Расход Claude" in page and "Узнать сейчас" in page
    r = client.post("/settings/claude/limits", follow_redirects=False)
    assert unquote(r.headers["location"]).endswith("msg=Лимиты обновлены#usage")
    page = client.get("/settings?tab=claude").text
    assert "Лимит подписки · неделя" in page and "<b>34%</b>" in page and "≈$0" in page and "Обновить" in page
    assert store.scalar("SELECT job FROM claude_runs") == "limits"


def test_record_never_breaks_a_run(settings_with):
    usage.record("triage", "sonnet", {"modelUsage": {"x": None}}, None)  # кривой ответ — учёт молча пропускает
    assert store.row("SELECT 1 FROM log WHERE job='usage' AND level='warning'")
