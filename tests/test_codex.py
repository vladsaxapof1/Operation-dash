"""ChatGPT вместо Claude: Codex в фоне, переключатель в настройках, и никогда — оба сразу."""

import json
import os
import sys
import threading
import time

import pytest
from fastapi.testclient import TestClient

from opsdash import brain, chat, cli, codex, config, store

# поддельный Codex CLI: события --json как у настоящего (codex exec), запоминает аргументы и задание
FAKE = '''#!{python}
import json, sys, time
from pathlib import Path
here = Path(__file__).parent
args = sys.argv[1:]
mode = (here / "mode.txt").read_text().strip() if (here / "mode.txt").exists() else ""
if args[:2] == ["login", "status"]:
    print("Not logged in" if mode == "logged-out" else "Logged in using ChatGPT")
    sys.exit(1 if mode == "logged-out" else 0)
if args == ["--version"]:
    print("codex-cli 0.166.0")
    sys.exit(0)
prompt = sys.stdin.read()
with open(here / "calls.jsonl", "a", encoding="utf-8") as f:
    f.write(json.dumps({{"args": args, "prompt": prompt}}, ensure_ascii=False) + "\\n")
if "--ignore-user-config" in args and mode == "old-cli":
    sys.stderr.write("error: unexpected argument '--ignore-user-config' found\\n")
    sys.exit(2)
def out(d):
    print(json.dumps(d, ensure_ascii=False), flush=True)
thread = args[args.index("resume") + 1] if "resume" in args else "t-1"
out({{"type": "thread.started", "thread_id": thread}})
out({{"type": "turn.started"}})
if mode == "fail":
    out({{"type": "error", "message": "The 'x' model is not supported when using Codex with a ChatGPT account."}})
    out({{"type": "turn.failed", "error": {{"message": "The 'x' model is not supported when using Codex with a ChatGPT account."}}}})
    sys.exit(1)
out({{"type": "item.completed", "item": {{"id": "item_0", "type": "agent_message", "text": "Сейчас посмотрю."}}}})
out({{"type": "item.started", "item": {{"id": "item_1", "type": "command_execution",
     "command": "/bin/zsh -lc 'sed -n 1,200p data/meetings/m1/transcript.md'", "status": "in_progress"}}}})
out({{"type": "item.completed", "item": {{"id": "item_1", "type": "command_execution",
     "command": "/bin/zsh -lc 'sed -n 1,200p data/meetings/m1/transcript.md'", "exit_code": 0, "status": "completed"}}}})
out({{"type": "item.started", "item": {{"id": "item_2", "type": "command_execution",
     "command": "/bin/zsh -lc './ops meetings search \\"склад\\"'", "status": "in_progress"}}}})
out({{"type": "item.completed", "item": {{"id": "item_3", "type": "file_change",
     "changes": [{{"path": "data/meetings/m1/summary.md", "kind": "add"}}], "status": "completed"}}}})
if mode == "slow":
    time.sleep(30)
out({{"type": "item.completed", "item": {{"id": "item_4", "type": "agent_message", "text": "| Выдачи | 120 |"}}}})
out({{"type": "turn.completed", "usage": {{"input_tokens": 42000, "cached_input_tokens": 30000, "output_tokens": 900}}}})
'''


@pytest.fixture
def fake_codex(tmp_path, settings_with):
    if os.name == "nt":
        pytest.skip("поддельный codex — скрипт с #!, Windows его не запускает")
    path = tmp_path / "codex"
    path.write_text(FAKE.format(python=sys.executable), encoding="utf-8")
    path.chmod(0o755)
    settings_with(ai={"engine": "chatgpt"}, chatgpt={"path": str(path)})
    codex._login_cache.clear()

    def calls():
        f = tmp_path / "calls.jsonl"
        return [json.loads(line) for line in f.read_text(encoding="utf-8").splitlines()] if f.exists() else []

    def mode(value):
        (tmp_path / "mode.txt").write_text(value, encoding="utf-8")
        codex._login_cache.clear()

    return {"calls": calls, "mode": mode, "path": str(path)}


@pytest.fixture
def client():
    from opsdash.web.app import app

    return TestClient(app, base_url="http://localhost:8765")


def test_run_goes_to_chatgpt_with_instructions_and_sandbox(fake_codex):
    res = brain.run("Разбери встречу m1", model="opus", job="process-meeting")
    assert res["text"] == "| Выдачи | 120 |" and res["session_id"] == "chatgpt:t-1"
    [call] = fake_codex["calls"]()
    args = call["args"]
    assert args[:2] == ["exec", "--json"] and args[-1] == "-"
    assert args[args.index("--sandbox") + 1] == "workspace-write" and "--ignore-user-config" in args
    assert 'model_reasoning_effort="high"' in args and "-m" not in args  # opus → думать глубоко; модель — как в Codex
    # инструкция для Codex, CLAUDE.md и сводка контекста — в начале задания, само задание — в конце
    assert call["prompt"].startswith("Ты работаешь в PULSE через Codex")
    assert "# Инструкция PULSE (CLAUDE.md)" in call["prompt"] and "Жёсткие правила" in call["prompt"]
    assert call["prompt"].rstrip().endswith("Разбери встречу m1")
    assert store.row("SELECT 1 FROM log WHERE message LIKE 'ChatGPT закончил%' AND job='process-meeting'")


def test_model_setting_and_effort_by_task(fake_codex, settings_with):
    settings_with(chatgpt={"model": "gpt-test"})
    brain.run("Разбери сообщения", model="sonnet", job="triage")
    brain.stream("вопрос", model="opus", effort="max", job="chat")  # «Спросить»: явное рассуждение важнее
    first, second = (c["args"] for c in fake_codex["calls"]())
    assert first[first.index("-m") + 1] == "gpt-test" and 'model_reasoning_effort="medium"' in first
    assert 'model_reasoning_effort="max"' in second


def test_stream_reports_text_and_steps_like_claude(fake_codex):
    texts, steps = [], []
    brain.stream("вопрос", on_text=texts.append, on_step=lambda tool, params: steps.append((tool, params)))
    assert texts == ["Сейчас посмотрю.", "| Выдачи | 120 |"]
    assert [t for t, _ in steps] == ["Read", "Bash", "Edit"]  # шаги Codex — под именами шагов Claude
    assert steps[0][1]["file_path"] == "data/meetings/m1/transcript.md"
    assert chat.step_label(*steps[1]) == "Ищет во встречах: «склад»"


def test_resume_continues_codex_session_without_repeating_instructions(fake_codex):
    res = brain.stream("а что по складу?", resume="chatgpt:t-7", job="chat")
    [call] = fake_codex["calls"]()
    assert call["args"][-3:] == ["resume", "t-7", "-"] and call["prompt"] == "а что по складу?"
    assert res["session_id"] == "chatgpt:t-7"


def test_claude_session_is_not_resumed_by_chatgpt(fake_codex):
    with pytest.raises(brain.BrainError, match="начат с Claude"):
        brain.stream("вопрос", resume="s-1")
    assert fake_codex["calls"]() == []


def test_chat_started_with_claude_continues_with_chatgpt(fake_codex):
    chat_id = chat.create("План продаж", "opus")
    store.execute("UPDATE ask_chats SET session_id='s-claude' WHERE id=?", (chat_id,))
    now = store.utcnow()
    store.execute("INSERT INTO ask_messages(chat_id, role, text, status, created, updated) VALUES(?,?,?,?,?,?)",
                  (chat_id, "user", "Сколько выдач в октябре?", "done", now, now))
    store.execute("INSERT INTO ask_messages(chat_id, role, text, status, created, updated) VALUES(?,?,?,?,?,?)",
                  (chat_id, "assistant", "120 выдач.", "done", now, now))
    store.execute("INSERT INTO ask_messages(chat_id, role, text, status, created, updated) VALUES(?,?,?,?,?,?)",
                  (chat_id, "user", "А в ноябре?", "done", now, now))
    answer = store.execute("INSERT INTO ask_messages(chat_id, role, text, status, created, updated) VALUES(?,?,?,?,?,?)",
                           (chat_id, "assistant", "", "running", now, now))
    assert chat.run(answer) == "ответ готов"
    [call] = fake_codex["calls"]()
    assert "resume" not in call["args"]  # сессию Claude продолжить нельзя — новая, с пересказом начала
    assert "Начало этого разговора" in call["prompt"] and "120 выдач." in call["prompt"]
    assert chat.get(chat_id)["session_id"] == "chatgpt:t-1"
    m = store.row("SELECT * FROM ask_messages WHERE id=?", (answer,))
    assert m["status"] == "done" and m["text"] == "| Выдачи | 120 |"


def test_failure_and_login_are_explained(fake_codex):
    fake_codex["mode"]("fail")
    with pytest.raises(brain.BrainError, match="not supported when using Codex"):
        brain.run("привет")
    fake_codex["mode"]("logged-out")
    with pytest.raises(brain.BrainError, match="не выполнен вход"):
        brain.run("привет")
    assert len(fake_codex["calls"]()) == 1  # без входа Codex даже не запускаем
    with pytest.raises(brain.BrainError, match="вход не выполнен"):
        codex.check()


def test_old_codex_without_ignore_user_config(fake_codex):
    fake_codex["mode"]("old-cli")
    assert brain.run("привет")["text"] == "| Выдачи | 120 |"
    first, second = (c["args"] for c in fake_codex["calls"]())
    assert "--ignore-user-config" in first and "--ignore-user-config" not in second


def test_only_one_engine_at_a_time(settings_with):
    order = []
    claude_in, release = threading.Event(), threading.Event()

    def claude_job():
        with brain.turn("claude", "triage"):
            order.append("claude начал")
            claude_in.set()
            release.wait(5)
            order.append("claude закончил")

    def chatgpt_job():
        with brain.turn("chatgpt", "chat"):
            order.append("chatgpt начал")

    a = threading.Thread(target=claude_job)
    a.start()
    claude_in.wait(5)
    with brain.turn("claude", "ask"):  # второй запуск того же — не ждёт (очереди PULSE работают параллельно)
        order.append("claude второй")
    b = threading.Thread(target=chatgpt_job)
    b.start()
    time.sleep(0.3)
    assert "chatgpt начал" not in order and brain.busy() == "claude"
    release.set()
    a.join(5)
    b.join(5)
    assert order == ["claude начал", "claude второй", "claude закончил", "chatgpt начал"]
    assert brain.busy() is None
    assert store.row("SELECT 1 FROM log WHERE job='chat' AND message LIKE 'ChatGPT ждёт, пока Claude%'")


def test_step_names_from_shell_commands():
    def cmd(c):
        return codex.step({"type": "command_execution", "command": c})

    assert cmd("/bin/zsh -lc 'sed -n 1,80p .claude/skills/triage/SKILL.md | head'") == (
        "Read", {"file_path": ".claude/skills/triage/SKILL.md"})
    assert chat.step_label(*cmd("/bin/zsh -lc 'cat .claude/skills/triage/SKILL.md'")) == "Открывает инструкцию"
    assert cmd("bash -lc 'rg -n \"склад\" data/meetings'") == ("Grep", {"pattern": "склад"})
    assert cmd("/bin/zsh -lc 'ls data'") == ("Glob", {"pattern": "data"})
    assert cmd(["/bin/bash", "-lc", "./ops commitments list"]) == ("Bash", {"command": "./ops commitments list"})
    assert codex.step({"type": "reasoning", "text": "…"}) is None


def test_notify_from_sandbox_waits_for_pulse(settings_with, monkeypatch, capsys):
    monkeypatch.setenv("OPSDASH_SANDBOX", "1")
    sent = []
    monkeypatch.setattr("opsdash.bot.flush_outbox", lambda *a, **k: sent.append(1))
    cli.main(["notify", "Дайджест готов"])
    assert "PULSE отправит в Telegram сразу" in capsys.readouterr().out and not sent
    assert store.scalar("SELECT COUNT(*) FROM outbox WHERE sent_at IS NULL") == 1


def test_settings_switch_engine(client, fake_codex, settings_with):
    settings_with(ai={"engine": "claude"})
    page = client.get("/settings?tab=chatgpt").text  # окно ChatGPT поверх каталога «Подключений»
    assert 'aria-label="Кто работает в PULSE"' in page and "Codex найден" in page and "вход: <b>ChatGPT</b>" in page
    assert "Спросить Claude" in page and "Переключить на ChatGPT" in page
    r = client.post("/settings/ai", data={"engine": "chatgpt"}, follow_redirects=False)
    assert r.status_code == 303 and config.load_settings()["ai"]["engine"] == "chatgpt"
    page = client.get("/settings?tab=chatgpt").text
    assert "Спросить ChatGPT" in page and "Сейчас в PULSE работает ChatGPT." in page
    assert "ChatGPT — подробно" in client.get("/ask").text  # выбор в чате — глубина рассуждения ChatGPT
    client.post("/settings/chatgpt", data={"path": fake_codex["path"], "model": "gpt-test"})
    assert config.load_settings()["chatgpt"]["model"] == "gpt-test"
    assert client.post("/settings/check/chatgpt").json() == {"ok": True, "message": "codex-cli 0.166.0, вход: ChatGPT"}
    assert client.post("/settings/ai", data={"engine": "gemini"}, follow_redirects=False).status_code == 303
    assert config.load_settings()["ai"]["engine"] == "chatgpt"


def test_health_shows_active_engine(fake_codex):
    from opsdash import overview

    item = overview._claude_health(config.load_settings())
    assert (item["title"], item["status"]) == ("ChatGPT", "ok")
    fake_codex["mode"]("logged-out")
    item = overview._claude_health(config.load_settings())
    assert (item["status"], item["note"]) == ("error", "нужен вход")
