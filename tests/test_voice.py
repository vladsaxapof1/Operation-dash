import json
from urllib.parse import unquote

import numpy as np
import pytest
from fastapi.testclient import TestClient

from opsdash import config, jobs, proposals, recordings, records, store, transcribe, voice

UTTERANCES = [
    {"start": 0.0, "end": 4.0, "speaker": "Спикер 1", "text": "сколько тебе на это время примерно нужно"},
    {"start": 4.0, "end": 9.0, "speaker": "Спикер 2", "text": "15 минут, по 15 сделаю в каждом канале"},
    {"start": 9.0, "end": 12.0, "speaker": "Спикер 1", "text": "тогда созвонимся"},
]
VECTORS = {"Спикер 1": [0.0, 1.0, 0.0], "Спикер 2": [0.9, 0.436, 0.0]}  # «Спикер 2» похож на директора


def _unit(v):
    v = np.asarray(v, dtype="float32")
    return v / np.linalg.norm(v)


@pytest.fixture
def fake_audio(monkeypatch):
    """Звук и отпечатки голоса без модели: отпечаток зависит от того, чьи реплики переданы."""
    def embed(audio, parts):
        label = next(u["speaker"] for u in UTTERANCES if (u["start"], u["end"]) == parts[0])
        return _unit(VECTORS[label])

    monkeypatch.setattr(voice, "embed", embed)
    monkeypatch.setattr(transcribe, "decode", lambda path, settings=None: np.zeros(16000 * 12, dtype="float32"))


def _meeting(tmp_path, settings_with):
    settings_with(owner={"name": "Анна"})
    rec = tmp_path / "2026-10-26 12-30-28.mov"
    rec.write_bytes(b"x")
    mid = recordings.create_meeting(rec)
    folder = records.meeting_dir(mid)
    header = ["Дата: 26.10.2026 12:30", "Спикеры определены автоматически (2): имена сопоставляются при разборе"]
    (folder / "transcript.md").write_text(transcribe.render_markdown("Анна / Тимур", header, UTTERANCES), encoding="utf-8")
    (folder / "transcript.json").write_text(json.dumps(UTTERANCES, ensure_ascii=False), encoding="utf-8")
    (folder / "meta.json").write_text(json.dumps({"id": mid, "source_file": str(rec)}), encoding="utf-8")
    return mid


def test_recognize_owner_by_voice(fake_audio):
    assert voice.recognize(None, UTTERANCES) == {}  # голос ещё не запомнен — никого не подписываем
    voice.remember("m0", "Спикер 3", _unit([1.0, 0.0, 0.0]), 60)
    assert voice.recognize(None, UTTERANCES) == {"Спикер 2": 0.9}
    voice.remember("m1", "Спикер 1", _unit([0.0, 1.0, 0.0]), 1)  # короткая ошибочная отметка почти не влияет
    assert voice.owner_print()[0] > 0.99
    assert list(voice.recognize(None, UTTERANCES)) == ["Спикер 2"]


def test_this_is_me_relabels_cleans_up_and_reprocesses(tmp_path, settings_with, fake_audio, monkeypatch):
    from opsdash.web.app import app

    mid = _meeting(tmp_path, settings_with)
    wrong = records.add_commitment(direction="to_me", what="Собрать сценарий", who="Тимур", whom="Анна",
                                   source_type="meeting", source_ref=mid)
    kept = records.add_commitment(direction="other", what="Уже в Kaiten", who="Тимур", source_type="meeting",
                                  source_ref=mid, kaiten_card_id=7)
    p = proposals.propose_calendar_event(title="Созвон", start="2026-10-27T15:00", end="2026-10-27T15:30",
                                         batch=f"meeting:{mid}")
    submitted = []
    monkeypatch.setattr(jobs, "submit", lambda name, kind="light", args=None: submitted.append((name, args)) or True)
    client = TestClient(app, base_url="http://localhost:8765")

    page = client.get(f"/meetings/{mid}").text
    assert "Кто в записи" in page and "«15 минут, по 15 сделаю в каждом канале»" in page

    r = client.post(f"/meetings/{mid}/me", data={"speaker": "Спикер 2"}, follow_redirects=False)
    assert "Голос запомнила" in unquote(r.headers["location"])
    md = (records.meeting_dir(mid) / "transcript.md").read_text(encoding="utf-8")
    assert "] Анна: 15 минут" in md and "] Спикер 1: сколько тебе" in md
    assert "Вы в записи: Анна = Спикер 2 (отмечено вами)" in md
    assert json.loads((records.meeting_dir(mid) / "transcript.json").read_text(encoding="utf-8"))[1]["speaker"] == "Спикер 2"
    assert store.row("SELECT 1 FROM commitments WHERE id=?", (wrong,)) is None
    assert store.row("SELECT 1 FROM commitments WHERE id=?", (kept,)) is not None  # карточка уже создана — не трогаем
    assert proposals.get(p["id"])["status"] == "rejected"
    assert voice.samples()[mid]["speaker"] == "Спикер 2"
    assert submitted == [("process-meeting", {"meeting_id": mid})]
    assert "<span class=\"chip acc\">вы</span>" in client.get(f"/meetings/{mid}").text

    client.post(f"/meetings/{mid}/me", data={"speaker": "Спикер 1"})  # передумал — переотметка заменяет прежнюю
    md = (records.meeting_dir(mid) / "transcript.md").read_text(encoding="utf-8")
    assert "] Анна: сколько тебе" in md and "] Спикер 2: 15 минут" in md and md.count("Вы в записи:") == 1
    assert voice.samples()[mid]["speaker"] == "Спикер 1"


def test_new_recording_signed_by_voice(tmp_path, settings_with, monkeypatch, fake_audio):
    settings_with(owner={"name": "Анна"}, recordings={"folders": [str(tmp_path)]})
    rec = tmp_path / "2026-10-27 10-00-00.mov"
    rec.write_bytes(b"x")
    mid = recordings.create_meeting(rec)
    monkeypatch.setattr(transcribe, "transcribe_audio", lambda *a, **k: {
        "duration": 12.0, "utterances": UTTERANCES, "diarization_error": None,
        "speakers": ["Спикер 1", "Спикер 2"], "audio": np.zeros(10, dtype="float32")})
    voice.remember("m0", "Спикер 3", _unit([1.0, 0.0, 0.0]), 60)
    recordings.transcribe_meeting(mid)
    folder = records.meeting_dir(mid)
    md = (folder / "transcript.md").read_text(encoding="utf-8")
    assert "Вы в записи: Анна = Спикер 2 (узнан по голосу, сходство 0.90)" in md and "] Анна: 15 минут" in md
    meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
    assert meta["speaker_names"] == {"Спикер 2": "Анна"} and meta["speaker_people"] == {"Спикер 2": "me"}
    assert "audio" not in meta and set(voice.load_prints(mid)) == {"Спикер 1", "Спикер 2"}


SPLIT = [  # разметка разделила Никиту на «Спикер 2» и «Спикер 4», а Пётр — «Спикер 3»
    {"start": 0.0, "end": 5.0, "speaker": "Спикер 1", "text": "начнём"},
    {"start": 5.0, "end": 10.0, "speaker": "Спикер 2", "text": "по складу всё готово"},
    {"start": 10.0, "end": 15.0, "speaker": "Спикер 3", "text": "а по закупкам нет"},
    {"start": 15.0, "end": 20.0, "speaker": "Спикер 4", "text": "закупки я возьму"},
    {"start": 20.5, "end": 25.0, "speaker": "Спикер 2", "text": "до пятницы"},
]
SPLIT_VECTORS = {"Спикер 1": [1, 0, 0], "Спикер 2": [0, 1, 0], "Спикер 3": [0, 0, 1], "Спикер 4": [0.1, 0.95, 0.1]}


@pytest.fixture
def split_audio(monkeypatch):
    def embed(audio, parts):
        label = next(u["speaker"] for u in SPLIT if (u["start"], u["end"]) == parts[0])
        return _unit(SPLIT_VECTORS[label])

    monkeypatch.setattr(voice, "embed", embed)
    monkeypatch.setattr(transcribe, "decode", lambda path, settings=None: np.zeros(16000 * 25, dtype="float32"))


def test_tidy_merges_split_voice_and_names_known_people(split_audio, settings_with):
    settings_with(owner={"name": "Анна"})
    res = voice.tidy(None, SPLIT)  # голосов ещё не знаем — только склейка частей одного человека
    assert res["before"] == 4 and res["after"] == 3 and res["who"] == {}
    assert [u["speaker"] for u in res["utterances"]] == ["Спикер 1", "Спикер 2", "Спикер 3", "Спикер 2"]
    assert res["utterances"][-1]["text"] == "закупки я возьму до пятницы"  # соседние реплики — одной

    nikita = records.upsert_person("Игорь Смирнов")
    voice.remember_person(nikita, "m0", "Спикер 5", _unit([0, 1, 0.05]), 40)
    voice.remember("m0", "Спикер 1", _unit([1, 0, 0]), 40)
    res = voice.tidy(None, SPLIT, renumber=False)
    assert res["who"] == {"Спикер 1": "me", "Спикер 2": str(nikita)} and res["mapping"]["Спикер 4"] == "Спикер 2"
    assert voice.notes(res["who"], res["how"])[1].startswith("Узнаны: Игорь Смирнов = Спикер 2 (узнан по голосу")
    records.delete_person(nikita)
    assert str(nikita) not in voice.voices()  # удалённого из справочника не подписываем


def test_confirm_person_remembers_voice_and_names_same_voice(tmp_path, settings_with, split_audio):
    from opsdash.web.app import app

    settings_with(owner={"name": "Анна"})
    rec = tmp_path / "2026-10-26 13-03-00.mov"
    rec.write_bytes(b"x")
    mid = recordings.create_meeting(rec)
    folder = records.meeting_dir(mid)
    (folder / "transcript.md").write_text(transcribe.render_markdown("Склад", ["Дата: 26.10.2026 13:03"], SPLIT), encoding="utf-8")
    (folder / "transcript.json").write_text(json.dumps(SPLIT, ensure_ascii=False), encoding="utf-8")
    (folder / "meta.json").write_text(json.dumps({"id": mid, "source_file": str(rec), "speakers": ["Спикер 1", "Спикер 2", "Спикер 3", "Спикер 4"],
                                                  "speaker_guesses": {"Спикер 3": {"name": "Пётр Иванов", "why": "про закупки"}}}),
                                      encoding="utf-8")
    store.execute("UPDATE meetings SET status='processed' WHERE id=?", (mid,))
    client = TestClient(app, base_url="http://localhost:8765")

    assert "Научите PULSE узнавать ваш голос" in client.get("/").text  # подсказка справа внизу
    client.cookies.set("voice_tip", "me")
    assert "Научите PULSE узнавать ваш голос" not in client.get("/").text  # «Позже»
    client.cookies.clear()

    page = client.get(f"/meetings/{mid}").text
    assert "Claude думает, это <b>Пётр Иванов</b> — про закупки" in page and "Да, это Пётр" in page

    r = client.post(f"/meetings/{mid}/speaker", data={"speaker": "Спикер 2", "name": "Никита"}, follow_redirects=False)
    msg = unquote(r.headers["location"])
    assert "Добавила в справочник" in msg and "Тот же голос у Спикер 4" in msg
    nikita = records.find_person("Никита")["id"]
    assert voice.person_samples(nikita)[mid]["speaker"] == "Спикер 2" and voice.voice_counts() == {nikita: 1}
    md = (folder / "transcript.md").read_text(encoding="utf-8")
    assert "] Никита: по складу" in md and "] Никита: закупки я возьму" in md and "Узнаны: Никита = Спикер 2 (отмечено вами)" in md

    voice.remember("m9", "Спикер 1", _unit([1, 0, 0]), 30)  # голос директора известен — дальше подсказка про догадки
    assert "Кто говорит во встрече?" in client.get("/").text
    assert "🎙 голос запомнен" in client.get("/people").text
    client.post(f"/people/{nikita}/voice/forget")
    assert voice.voice_counts() == {}


def test_guesses_from_summary_and_cli(tmp_path, settings_with, capsys):
    from opsdash import cli

    rec = tmp_path / "2026-10-26 14-00-00.mov"
    rec.write_bytes(b"x")
    mid = recordings.create_meeting(rec)
    folder = records.meeting_dir(mid)
    (folder / "summary.md").write_text("# Встреча\n\n## Кто есть кто в записи\n- Спикер 1 — Анна (вы, по голосу)\n"
                                       "- Спикер 2 — Пётр Иванов (вероятно: обращаются по имени)\n- Спикер 5 — не определён\n",
                                       encoding="utf-8")
    meta = {"id": mid, "speakers": ["Спикер 1", "Спикер 2", "Спикер 5"]}
    assert voice.guesses(mid, meta) == {"Спикер 2": {"name": "Пётр Иванов", "why": "вероятно: обращаются по имени"}}
    (folder / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    cli.main(["meetings", "guess", mid, "--speaker", "Спикер 5", "--name", "Олег", "--why", "ведёт встречу"])
    assert "Записала" in capsys.readouterr().out
    assert voice.guesses(mid) == {"Спикер 5": {"name": "Олег", "why": "ведёт встречу"}}


def test_voice_settings_block(settings_with):
    from opsdash.web.app import app

    client = TestClient(app, base_url="http://localhost:8765")
    assert "PULSE ещё не знает ваш голос" in client.get("/settings?tab=recordings").text
    voice.remember("m0", "Спикер 1", _unit([1, 0, 0]), 30)
    assert "PULSE знает ваш голос (по 1 записи)" in client.get("/settings?tab=recordings").text
    client.post("/settings/voice/forget")
    assert voice.samples() == {}
