"""«Спросить Claude»: разговор с Claude в PULSE — вопрос, контекст, ответ по ходу работы, продолжение разговора."""

import json
import sys
import time
import zipfile
from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient

from opsdash import brain, chat, config, records, store

# поддельный Claude Code: пишет события как настоящий (stream-json), запоминает аргументы и вопрос
FAKE = '''#!{python}
import json, sys, time
from pathlib import Path
here = Path(__file__).parent
args = sys.argv[1:]
prompt = sys.stdin.read()
with open(here / "calls.jsonl", "a", encoding="utf-8") as f:
    f.write(json.dumps({{"args": args, "prompt": prompt}}, ensure_ascii=False) + "\\n")
mode = (here / "mode.txt").read_text().strip() if (here / "mode.txt").exists() else ""
if "--resume" in args and mode == "lost-session":
    print(json.dumps({{"type": "result", "is_error": True, "subtype": "error", "result": "No conversation found"}}))
    sys.exit(1)
if "--include-partial-messages" in args and mode == "old-cli":
    sys.stderr.write("error: unknown option '--include-partial-messages'\\n")
    sys.exit(1)
if "stream-json" not in args:
    print(json.dumps({{"type": "result", "result": "ответ целиком", "session_id": "s-old"}}))
    sys.exit(0)
def out(d):
    print(json.dumps(d, ensure_ascii=False), flush=True)
def text(t):
    out({{"type": "stream_event", "event": {{"type": "content_block_delta", "delta": {{"type": "text_delta", "text": t}}}}}})
start = {{"type": "stream_event", "event": {{"type": "content_block_start", "content_block": {{"type": "text", "text": ""}}}}}}
out({{"type": "system", "subtype": "init", "session_id": "s-1"}})
out(start)
text("Сейчас посмотрю.")
out({{"type": "assistant", "message": {{"content": [{{"type": "tool_use", "name": "Read",
     "input": {{"file_path": "data/meetings/m1/transcript.md"}}}}]}}}})
out({{"type": "assistant", "message": {{"content": [{{"type": "tool_use", "name": "Bash",
     "input": {{"command": "./ops meetings search \\"склад\\""}}}}]}}}})
if mode == "slow":
    time.sleep(30)
out(start)
for part in ["| Метрика | Было |", "\\n|---|---|", "\\n| Выдачи | 120 |"]:
    text(part)
out({{"type": "result", "subtype": "success", "result": "| Метрика | Было |\\n|---|---|\\n| Выдачи | 120 |",
     "session_id": "s-1"}})
'''


@pytest.fixture
def fake_claude(tmp_path, settings_with):
    path = tmp_path / "claude"
    path.write_text(FAKE.format(python=sys.executable), encoding="utf-8")
    path.chmod(0o755)
    settings_with(claude={"path": str(path)})

    def calls():
        f = tmp_path / "calls.jsonl"
        return [json.loads(line) for line in f.read_text(encoding="utf-8").splitlines()] if f.exists() else []

    def mode(value):
        (tmp_path / "mode.txt").write_text(value, encoding="utf-8")

    return {"calls": calls, "mode": mode}


@pytest.fixture
def client():
    from opsdash.web.app import app

    return TestClient(app, base_url="http://localhost:8765")


def _meeting():
    store.execute("INSERT INTO meetings(id, title, started_at, status) VALUES('m1', 'Продажи на ноябрь', "
                  "'2026-10-28T14:00:00+00:00', 'processed')")
    folder = records.meeting_dir("m1")
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "transcript.md").write_text("[00:12:40] Спикер 1: выдачи поднимаем со 120 до 140", encoding="utf-8")


def _done(message_id, timeout=20):
    deadline = time.time() + timeout
    while store.row("SELECT status FROM ask_messages WHERE id=?", (message_id,))["status"] == "running":
        assert time.time() < deadline, "Claude не ответил"
        time.sleep(0.05)
    return store.row("SELECT * FROM ask_messages WHERE id=?", (message_id,))


def test_stream_reports_text_and_steps(fake_claude):
    texts, steps = [], []
    res = brain.stream("вопрос", on_text=texts.append, on_step=lambda tool, params: steps.append(tool))
    assert res["text"].startswith("| Метрика") and res["session_id"] == "s-1"
    assert texts[0] == "Сейчас посмотрю." and texts[-1] == res["text"]  # новый абзац — текст заново
    assert steps == ["Read", "Bash"]
    args = fake_claude["calls"]()[0]["args"]
    assert "--include-partial-messages" in args and "stream-json" in args


def test_stream_falls_back_on_old_claude(fake_claude):
    fake_claude["mode"]("old-cli")
    texts = []
    assert brain.stream("вопрос", on_text=texts.append)["text"] == "ответ целиком" and texts == ["ответ целиком"]


def test_ask_answers_with_context_and_continues(fake_claude):
    _meeting()
    task = records.add_commitment(direction="mine", title="План продаж на ноябрь", what="Согласовать план продаж на ноябрь")
    context = chat.parse_refs(["meeting:m1", f"task:{task}", "task:999", "meeting:нет", "meeting:m1"])
    assert [c["type"] for c in context] == ["meeting", "task"]
    chat_id, answer = chat.ask(None, "Распиши решения по каждой метрике", context, "opus")
    m = _done(answer)
    assert m["status"] == "done" and "| Выдачи | 120 |" in m["text"]
    assert json.loads(m["steps"]) == ["Читает расшифровку «Продажи на ноябрь»", "Ищет во встречах: «склад»"]
    c = chat.get(chat_id)
    assert c["session_id"] == "s-1" and c["title"] == "Распиши решения по каждой метрике"
    first = fake_claude["calls"]()[0]
    assert "навыку chat" in first["prompt"] and "data/meetings/m1/transcript.md (читай её целиком" in first["prompt"]
    assert f"Задача [{task}] «План продаж на ноябрь»" in first["prompt"] and f"--batch chat:{answer}" in first["prompt"]
    assert ["--model", "opus"] == first["args"][first["args"].index("--model"):][:2]

    _, second = chat.ask(chat_id, "А что не договорили?", [], "sonnet")
    _done(second)
    call = fake_claude["calls"]()[1]
    assert call["args"][call["args"].index("--resume") + 1] == "s-1" and "навыку chat" not in call["prompt"]
    assert "sonnet" in call["args"] and chat.get(chat_id)["model"] == "sonnet"
    items = chat.messages(chat_id)
    assert [m["role"] for m in items] == ["user", "assistant", "user", "assistant"]
    assert items[0]["context"][0]["label"] == "встреча «Продажи на ноябрь» 28.10" and items[0]["context"][0]["href"] == "/meetings/m1"


def test_lost_session_starts_over_with_history(fake_claude):
    chat_id, first = chat.ask(None, "Первый вопрос", [])
    _done(first)
    fake_claude["mode"]("lost-session")
    _, second = chat.ask(chat_id, "Второй вопрос", [])
    assert _done(second)["status"] == "done"
    last = fake_claude["calls"]()[-1]
    assert "--resume" not in last["args"] and "Директор: Первый вопрос" in last["prompt"] and "Второй вопрос" in last["prompt"]


def test_stop_and_retry(fake_claude):
    fake_claude["mode"]("slow")
    chat_id, answer = chat.ask(None, "Долгий вопрос", [])
    deadline = time.time() + 20
    while not json.loads(store.row("SELECT steps FROM ask_messages WHERE id=?", (answer,))["steps"]):
        assert time.time() < deadline
        time.sleep(0.05)
    assert chat.live(chat_id)["running"] and chat.stop(answer)
    m = _done(answer)
    assert m["status"] == "failed" and m["error"] == "Остановлено" and m["text"] == "Сейчас посмотрю."
    fake_claude["mode"]("")
    assert chat.retry(answer) and _done(answer)["status"] == "done"


def test_files_and_labels(settings_with):
    chat_id = chat.create("Файлы")
    docx = config.DATA_DIR / "tmp.docx"
    with zipfile.ZipFile(docx, "w") as z:
        z.writestr("word/document.xml", '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                                        "<w:body><w:p><w:r><w:t>План продаж: выдачи 140</w:t></w:r></w:p></w:body></w:document>")
    item = chat.save_file(chat_id, "../План продаж.docx", docx.read_bytes())
    assert item["name"] == "План продаж.docx" and (config.APP_DIR / item["text"]).read_text(encoding="utf-8").count("выдачи 140")
    again = chat.save_file(chat_id, "План продаж.docx", b"x")
    assert again["name"] == "План продаж (2).docx"
    assert "error" in chat.save_file(chat_id, "архив.zip", b"PK")
    assert chat.save_file(chat_id, "скрин.png", b"\x89PNG")["path"].endswith("скрин.png")
    assert chat.step_label("Bash", {"command": './ops docs search "прогноз Q4"'}) == "Ищет в документах: «прогноз Q4»"
    assert chat.step_label("Bash", {"command": "./ops commitments list --person Пётр"}) == "Смотрит задачи"
    assert chat.step_label("Read", {"file_path": "/x/data/chats/1/План.docx.md"}) == "Читает «План.docx.md»"
    chat.delete(chat_id)
    assert chat.get(chat_id) is None and not chat.files_dir(chat_id).exists()


def test_ask_pages(fake_claude, client):
    _meeting()
    page = client.get("/ask?meeting=m1").text
    assert "Спросить Claude" in page and 'value="meeting:m1"' in page and "встреча «Продажи на ноябрь» 28.10" in page
    assert 'href="/ask"' in client.get("/").text  # пункт меню
    assert "/ask?meeting=m1" in client.get("/meetings/m1").text
    r = client.post("/ask/send", data={"text": "Что решили?", "ctx": ["meeting:m1"], "model": "opus"},
                    files=[("files", ("заметки.txt", "выдачи 140".encode(), "text/plain"))], follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/ask/")
    chat_id = int(r.headers["location"].split("/")[2].split("#")[0])
    answer = store.row("SELECT id FROM ask_messages WHERE chat_id=? AND role='assistant'", (chat_id,))["id"]
    _done(answer)
    page = client.get(f"/ask/{chat_id}").text
    assert "Что решили?" in page and "файл «заметки.txt»" in page and "<table>" in page and "Что делал Claude" in page
    assert client.get(f"/api/ask/{chat_id}/live").json()["running"] is False
    assert "err=" in client.post("/ask/send", data={"text": " "}, follow_redirects=False).headers["location"]
    location = unquote(client.post(f"/ask/{chat_id}/delete", follow_redirects=False).headers["location"])
    assert location == "/ask?msg=Чат удалён" and chat.get(chat_id) is None


def test_voice(client, monkeypatch):
    from opsdash import transcribe

    heard = []
    monkeypatch.setattr(transcribe, "transcribe_short", lambda path: heard.append(path.read_bytes()) or "Что решили по складу?")
    r = client.post("/api/ask/voice", files={"audio": ("voice.webm", b"webm-bytes", "audio/webm")})
    assert r.json() == {"text": "Что решили по складу?"} and heard == [b"webm-bytes"]
    assert client.post("/api/ask/voice", files={"audio": ("voice.webm", b"", "audio/webm")}).status_code == 400
