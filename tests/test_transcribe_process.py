"""Расшифровка встречи идёт в отдельном процессе, чтобы PULSE отвечал во время неё.

Функции child_* выполняются в дочернем процессе: туда они попадают по имени модуля
(через pickle), поэтому лежат на уровне модуля, а подмены делают уже внутри процесса.
"""

import os
import threading
import time
from urllib.parse import unquote

import pytest

from opsdash import daemon, jobs, records, recordings, store, transcribe

UTTERANCES = [{"start": 0.0, "end": 4.0, "speaker": "Спикер 1", "text": "склад переносим на пятницу"}]


def _fake_audio(path, settings, *, num_speakers=None, diarization=True, progress=None):
    progress(0.5)
    if "долгая" in path.name:
        time.sleep(60)  # ждём, пока PULSE нас остановит
    return {"duration": 12.0, "utterances": UTTERANCES, "diarization_error": None,
            "speakers": ["Спикер 1"], "audio": None}


def child_transcribe(meeting_id, settings=None):
    """Настоящая transcribe_meeting, только вместо Whisper — подделка."""
    transcribe.transcribe_audio = _fake_audio
    store.set_state("test_child_pid", os.getpid())
    return recordings.transcribe_meeting(meeting_id, settings)


def child_crashes(meeting_id, settings=None):
    os._exit(3)


def _meeting(tmp_path, name="2026-10-27 10-00-00 Планёрка.m4a"):
    rec = tmp_path / name
    rec.write_bytes(b"x")
    return rec, recordings.create_meeting(rec)


def _outbox() -> list[str]:
    return [r["text"] for r in store.rows("SELECT text FROM outbox")]


def test_transcribe_job_runs_in_separate_process(tmp_path, settings_with, monkeypatch):
    settings_with(recordings={"auto_process": False})
    _, mid = _meeting(tmp_path)
    monkeypatch.setattr(recordings, "transcribe_meeting", child_transcribe)
    assert jobs.run_job("transcribe", {"meeting_id": mid}) == "Планёрка: 1 мин"  # результат — из дочернего процесса
    assert store.get_state("test_child_pid") not in (None, os.getpid())
    m = records.get_meeting(mid)
    assert m["status"] == "transcribed" and m["progress"] == 1.0  # пишет в базу сам дочерний процесс
    assert "склад переносим на пятницу" in (records.meeting_dir(mid) / "transcript.md").read_text(encoding="utf-8")
    assert not transcribe._children


def test_error_in_child_fails_meeting_and_notifies(tmp_path, settings_with):
    rec, mid = _meeting(tmp_path)
    (records.meeting_dir(mid) / "transcript.md").write_text("старая расшифровка", encoding="utf-8")  # расшифровка заново
    rec.unlink()  # настоящая transcribe_meeting в дочернем процессе: файла нет
    with pytest.raises(transcribe.TranscribeError, match="Файл записи не найден") as err:
        jobs.run_job("transcribe", {"meeting_id": mid})
    assert "transcribe_meeting" in str(err.value.__cause__)  # трассировка из дочернего процесса — в журнал
    m = records.get_meeting(mid)
    assert m["status"] == "failed" and "Файл записи не найден" in m["error"]
    assert any("Не получилось расшифровать" in t for t in _outbox())
    assert store.row("SELECT 1 FROM log WHERE job='transcribe' AND level='error'")


def test_crashed_child_is_an_error(tmp_path, settings_with, monkeypatch):
    _, mid = _meeting(tmp_path)
    monkeypatch.setattr(recordings, "transcribe_meeting", child_crashes)
    with pytest.raises(transcribe.TranscribeError, match="код 3"):
        jobs.job_transcribe(mid)
    assert records.get_meeting(mid)["status"] == "failed"
    assert any("Не получилось расшифровать" in t for t in _outbox())


def _start_long(tmp_path, monkeypatch, name="2026-10-27 11-00-00 долгая.m4a"):
    """Долгая расшифровка в рабочем потоке; вернуть (запись, встреча, поток, результат) после её начала."""
    rec, mid = _meeting(tmp_path, name)
    monkeypatch.setattr(recordings, "transcribe_meeting", child_transcribe)
    result = []
    worker = threading.Thread(target=lambda: result.append(jobs.run_job("transcribe", {"meeting_id": mid})))
    worker.start()
    deadline = time.time() + 30
    while (records.get_meeting(mid)["progress"] or 0) < 0.5:  # прогресс из дочернего процесса виден PULSE
        assert time.time() < deadline, "дочерний процесс не начал расшифровку"
        time.sleep(0.1)
    return rec, mid, worker, result


@pytest.mark.parametrize("change", ["удалили", "заменили"])
def test_recording_removed_mid_transcription_stops_it(tmp_path, settings_with, monkeypatch, change):
    """Запись удалили из папки или положили на её место другую, пока шла расшифровка: процесс останавливается,
    пустая встреча уходит из очереди без сообщения об ошибке, новая запись на том же месте найдётся заново."""
    monkeypatch.setattr(transcribe, "WATCH_SECONDS", 0.2)
    rec, mid, worker, result = _start_long(tmp_path, monkeypatch)
    [proc] = list(transcribe._children)
    if change == "удалили":
        rec.unlink()
    else:
        rec.write_bytes(b"another recording")
    worker.join(15)
    assert not worker.is_alive() and not proc.is_alive() and not transcribe._children
    assert result and result[0].startswith("остановлено") and change in result[0]
    assert records.get_meeting(mid) is None and not records.meeting_dir(mid).exists()
    assert not store.row("SELECT 1 FROM files_seen WHERE path=?", (str(rec),))
    assert not store.row("SELECT 1 FROM jobs WHERE name='process-meeting'")  # разбирать нечего
    assert not _outbox()


def test_short_glitch_does_not_stop_transcription(tmp_path, settings_with, monkeypatch):
    """Облачная папка подменяет файл за мгновение — одна неудачная проверка расшифровку не останавливает."""
    monkeypatch.setattr(transcribe, "WATCH_SECONDS", 0.05)
    checks = iter(["нет файла", None, "нет файла", None])
    _, mid = _meeting(tmp_path)
    assert transcribe.run_in_child(child_transcribe, mid, None, cancel=lambda: next(checks, None))["id"] == mid


def test_queued_meeting_without_recording_leaves_queue(tmp_path, settings_with, monkeypatch):
    """Запись удалили, пока встреча ждала очереди: встреча убирается при поиске записей и при старте расшифровки,
    а идущую расшифровку поиск не трогает — её остановит она сама."""
    submitted = []
    monkeypatch.setattr(jobs, "submit", lambda *a, **k: submitted.append(k["args"]["meeting_id"]) or True)
    rec_a, mid_a = _meeting(tmp_path, "2026-10-27 10-00-00 a.m4a")
    rec_b, mid_b = _meeting(tmp_path, "2026-10-27 11-00-00 b.m4a")
    _, mid_c = _meeting(tmp_path, "2026-10-27 12-00-00 c.m4a")
    rec_a.unlink()
    rec_b.unlink()
    jobs._running.add(jobs._key("transcribe", {"meeting_id": mid_b}))
    try:
        jobs.job_recordings()
    finally:
        jobs._running.clear()
    assert records.get_meeting(mid_a) is None and records.get_meeting(mid_b) and submitted == [mid_b, mid_c]
    assert "удалили из папки" in jobs.job_transcribe(mid_b) and records.get_meeting(mid_b) is None
    assert jobs.job_transcribe(mid_a) == "встречи больше нет"
    assert not _outbox()


def test_rerun_transcription_button(tmp_path, settings_with, monkeypatch):
    from fastapi.testclient import TestClient

    from opsdash.web.app import app

    submitted = []
    monkeypatch.setattr(jobs, "submit", lambda *a, **k: submitted.append(a) or True)
    client = TestClient(app, base_url="http://localhost:8765")
    rec, mid = _meeting(tmp_path)

    def rerun():
        return unquote(client.post(f"/meetings/{mid}/rerun", data={"step": "transcribe"},
                                   follow_redirects=False).headers["location"])

    assert "уже идёт" in rerun() and not submitted  # встреча в очереди — «Расшифровать заново» её не трогает
    records.update_meeting(mid, status="processed")
    rec.unlink()
    assert "Файла записи больше нет" in rerun() and records.get_meeting(mid)["status"] == "processed"
    rec.write_bytes(b"x")
    rerun()
    assert records.get_meeting(mid)["status"] == "new" and submitted


def test_shutdown_stops_transcription_process(tmp_path, settings_with, monkeypatch):
    monkeypatch.setattr(daemon, "stop_event", threading.Event())
    _, mid, worker, result = _start_long(tmp_path, monkeypatch)
    [proc] = list(transcribe._children)
    daemon.shutdown()
    worker.join(15)
    assert not worker.is_alive() and not proc.is_alive() and not transcribe._children
    assert result and "прервано" in result[0]
    assert records.get_meeting(mid)["status"] == "transcribing"  # при запуске PULSE вернёт её в очередь
    assert not _outbox()
