"""Расшифровка встречи идёт в отдельном процессе, чтобы PULSE отвечал во время неё.

Функции child_* выполняются в дочернем процессе: туда они попадают по имени модуля
(через pickle), поэтому лежат на уровне модуля, а подмены делают уже внутри процесса.
"""

import os
import threading
import time
from pathlib import Path
from urllib.parse import unquote

import pytest

from opsdash import config, daemon, jobs, records, recordings, store, transcribe

UTTERANCES = [{"start": 0.0, "end": 4.0, "speaker": "Спикер 1", "text": "склад переносим на пятницу"}]


def _fake_audio(path, settings, *, num_speakers=None, diarization=True, progress=None):
    if "счётчик" in path.name:  # прогресс растёт — видно, замерла ли расшифровка
        for i in range(1, 60):
            progress(i / 100)
            time.sleep(0.05)
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


def child_writes_slowly(marker):
    """Держит запись в базе открытой секунду — пауза не должна заморозить процесс посреди неё."""
    with store.db() as conn:
        conn.execute("INSERT INTO state(key, value) VALUES('пишу', '1')")
        Path(marker).touch()
        time.sleep(1)
    time.sleep(60)


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


def _start_long(tmp_path, monkeypatch, name="2026-10-27 11-00-00 долгая.m4a", progress=0.5):
    """Долгая расшифровка в рабочем потоке; вернуть (запись, встреча, поток, результат) после её начала."""
    rec, mid = _meeting(tmp_path, name)
    monkeypatch.setattr(recordings, "transcribe_meeting", child_transcribe)
    result = []
    worker = threading.Thread(target=lambda: result.append(jobs.run_job("transcribe", {"meeting_id": mid})))
    worker.start()
    deadline = time.time() + 30
    while (records.get_meeting(mid)["progress"] or 0) < progress:  # прогресс из дочернего процесса виден PULSE
        assert time.time() < deadline, "дочерний процесс не начал расшифровку"
        time.sleep(0.05)
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


def test_pause_freezes_transcription_and_resume_continues(tmp_path, settings_with, monkeypatch):
    settings_with(recordings={"auto_process": False})
    _, mid, worker, result = _start_long(tmp_path, monkeypatch, "2026-10-27 11-00-00 счётчик.m4a", progress=0.1)
    assert recordings.pause(mid) and records.get_meeting(mid)["status"] == "paused"
    frozen = records.get_meeting(mid)["progress"]
    time.sleep(0.6)
    assert records.get_meeting(mid)["progress"] == frozen < 0.5  # процесс замер и не считает
    store.set_state("база не заперта", True)  # замер не посреди записи в базу
    assert worker.is_alive() and recordings.resume(mid) and records.get_meeting(mid)["status"] == "transcribing"
    worker.join(30)
    assert result == ["счётчик: 1 мин"]
    m = records.get_meeting(mid)
    assert m["status"] == "transcribed" and m["progress"] == 1.0
    assert not transcribe._children and not transcribe._tags and not transcribe._guards and not transcribe._paused


def test_pause_waits_until_child_leaves_database(tmp_path, settings_with):
    store.set_state("база создана", True)
    marker = tmp_path / "inside"
    errors = []

    def run():
        try:
            transcribe.run_in_child(child_writes_slowly, str(marker), tag="t")
        except transcribe.Cancelled:
            pass
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    worker = threading.Thread(target=run)
    worker.start()
    deadline = time.time() + 30
    while not marker.exists():
        assert time.time() < deadline, "дочерний процесс не начал запись"
        time.sleep(0.02)
    try:
        assert transcribe.pause("t")
        started = time.time()
        store.set_state("PULSE пишет", True)  # без замка — ждал бы 30 с и падал «database is locked»
        assert time.time() - started < 5 and store.get_state("пишу") == 1
    finally:
        assert transcribe.stop("t", "тест")  # иначе замороженный процесс не даст тестам закончиться
    worker.join(15)
    assert not worker.is_alive() and not errors and not transcribe._children


def test_cancel_running_transcription_keeps_meeting(tmp_path, settings_with, monkeypatch):
    rec, mid, worker, result = _start_long(tmp_path, monkeypatch)
    [proc] = list(transcribe._children)
    assert recordings.pause(mid)  # отменить можно и с паузы
    assert recordings.cancel(mid)
    worker.join(15)
    assert not worker.is_alive() and not proc.is_alive() and not transcribe._children
    assert result and result[0] == "остановлено: расшифровку отменили"
    m = records.get_meeting(mid)
    assert m["status"] == "cancelled" and m["progress"] == 0 and rec.exists()  # встреча и запись остались
    jobs.job_recordings()  # отменённую сама не ставит в очередь снова
    assert records.get_meeting(mid)["status"] == "cancelled"
    assert not _outbox()


def test_whole_queue_pause_resume_and_stop(tmp_path, settings_with, monkeypatch):
    """Кнопки угла «Сейчас» и шапки «Встреч»: пауза всей расшифровки (идущая замирает, ждущие не начинаются),
    «Продолжить» и «Остановить всё» — встречи остаются, PULSE их сам в очередь не возвращает."""
    from fastapi.testclient import TestClient

    from opsdash import notices
    from opsdash.web.app import app

    settings_with(recordings={"auto_process": False})
    submitted = []
    rec, mid, worker, result = _start_long(tmp_path, monkeypatch, "2026-10-27 11-00-00 счётчик долгая.m4a", progress=0.1)
    monkeypatch.setattr(jobs, "submit", lambda *a, **k: submitted.append(k.get("args") or a[2]) or True)
    _, waiting = _meeting(tmp_path, "2026-10-27 12-00-00 следом.m4a")
    q = recordings.queue_state()
    assert q["current"]["id"] == mid and q["waiting"] == 1 and q["count"] == 2 and not q["paused"]

    assert recordings.pause_all() == 2
    assert {records.get_meeting(m)["status"] for m in (mid, waiting)} == {"paused"}
    frozen = records.get_meeting(mid)["progress"]
    time.sleep(0.6)
    assert records.get_meeting(mid)["progress"] == frozen  # замерла
    q = recordings.queue_state()
    assert q["paused"] and q["current"]["id"] == mid  # замороженная — по-прежнему «текущая»

    store.execute("INSERT OR REPLACE INTO jobs(name, running) VALUES('transcribe', 1)")
    [now] = [r for r in notices.running() if r["name"] == "transcribe"]
    assert "на паузе" in now["detail"] and "ещё 1 в очереди" in now["detail"] and now["tr"]["paused"]
    client = TestClient(app, base_url="http://localhost:8765")
    api = client.get("/api/notices").json()
    assert api["tr_only"] and api["tr"] == {"paused": True} and 'value="resume"' in api["tr_html"]
    assert "data-tr-confirm" in api["tr_html"] and "tr" not in api["running"][0]  # в угол — только разметка кнопок
    page = client.get("/meetings").text
    assert "tr-bar paused" in page and "Остановить всё</button>" in page and 'data-notice-now-act' in page

    assert recordings.resume_all() == 2
    assert records.get_meeting(mid)["status"] == "transcribing" and records.get_meeting(waiting)["status"] == "new"
    assert submitted == [{"meeting_id": waiting}]  # замершая — с того же места, ждущая — снова в очередь

    location = unquote(client.post("/transcription", data={"action": "cancel"}, follow_redirects=False,
                                   headers={"referer": "http://localhost:8765/meetings"}).headers["location"])
    assert location.startswith("/meetings?msg=Расшифровка остановлена: 2 записи")
    worker.join(15)
    assert not worker.is_alive() and result == ["остановлено: расшифровку отменили"] and not transcribe._children
    assert {records.get_meeting(m)["status"] for m in (mid, waiting)} == {"cancelled"} and rec.exists()
    assert recordings.queue_state() is None and recordings.cancel_all() == 0
    assert "err=" in client.post("/transcription", data={"action": "pause"}, follow_redirects=False).headers["location"]


def test_gpu_reason_is_explained(settings_with, monkeypatch):
    """Почему расшифровка не на видеокарте — простыми словами, в журнал (на Windows) и в «Записи встреч»."""
    import sys
    import types

    calls = []

    class FakeWhisper:
        def __init__(self, name, device, compute_type, **_):
            calls.append((device, compute_type))

    fake_ct2 = types.SimpleNamespace(get_cuda_device_count=lambda: 1, get_supported_compute_types=lambda d: {"int8", "float32"})
    monkeypatch.setitem(sys.modules, "ctranslate2", fake_ct2)
    monkeypatch.setitem(sys.modules, "faster_whisper", types.SimpleNamespace(WhisperModel=FakeWhisper))
    monkeypatch.setattr(transcribe, "_model", None)
    monkeypatch.setattr(transcribe, "_cuda_failed", "")
    monkeypatch.setattr(transcribe, "_cuda_libs_ready", True)  # папок NVIDIA на Mac нет — и путей Windows не строим
    real_os = os.name
    monkeypatch.setattr(transcribe.os, "name", "nt")  # журнал пишем только на Windows
    settings = config.load_settings()
    settings["recordings"]["device"] = "auto"

    monkeypatch.setattr(transcribe, "_cuda_libs_missing", lambda: True)
    transcribe.get_model(settings)
    assert calls == [("cpu", "int8")] and store.get_state("transcribe_device")["why"] == transcribe.NO_CUDA_LIBS
    assert store.row("SELECT 1 FROM log WHERE job='transcribe' AND message LIKE '%библиотека cuBLAS (CUDA 12) не установлена%'")

    # библиотеки есть: GeForce 10xx не умеет быстрый float16 — пусть ctranslate2 выберет сам
    monkeypatch.setattr(transcribe, "_model", None)
    monkeypatch.setattr(transcribe, "_cuda_libs_missing", lambda: False)
    transcribe.get_model(settings)
    assert calls[-1] == ("cuda", "auto") and store.get_state("transcribe_device")["device"] == "cuda"
    # видеокарта упала на первой фразе — dll не нашлась: дальше процессор, причина та же
    transcribe._cpu_fallback(settings, RuntimeError("Library cublas64_12.dll is not found or cannot be loaded"))
    assert calls[-1] == ("cpu", "int8") and store.get_state("transcribe_device")["why"] == transcribe.NO_CUDA_LIBS

    monkeypatch.setattr(transcribe, "_cuda_failed", "")
    monkeypatch.setattr(transcribe, "_model", None)
    monkeypatch.setattr(fake_ct2, "get_cuda_device_count", lambda: 0)
    transcribe.get_model(settings)
    assert store.get_state("transcribe_device")["why"] == transcribe.NO_NVIDIA
    monkeypatch.setattr(transcribe.os, "name", real_os)
    from fastapi.testclient import TestClient

    from opsdash.web.app import app, templates

    client = TestClient(app, base_url="http://localhost:8765")
    monkeypatch.setitem(templates.env.globals, "is_mac", True)
    monkeypatch.setitem(templates.env.globals, "is_apple_silicon", False)
    assert "шла на процессоре" not in client.get("/settings?tab=recordings").text  # на Mac выбора «Где считать» нет
    monkeypatch.setitem(templates.env.globals, "is_mac", False)  # Windows
    page = client.get("/settings?tab=recordings").text
    assert "Где считать" in page and "шла на процессоре: видеокарты NVIDIA не нашлось" in page
    assert "Скачать и перезапустить" not in page  # видеокарты нет — и качать нечего
    # видеокарта есть, библиотеки нет (PULSE ставили до 2.0.44) — скачать перезапуском, как после обновления
    store.set_state("transcribe_device", {"device": "cpu", "why": transcribe.NO_CUDA_LIBS, "at": store.utcnow()})
    settings_with(recordings={"device": "cpu"})
    assert "Скачать и перезапустить" not in client.get("/settings?tab=recordings").text  # выбран процессор
    settings_with(recordings={"device": "auto"})
    page = client.get("/settings?tab=recordings").text
    assert "Скачать и перезапустить" in page and 'action="/updates/restart"' in page
    monkeypatch.setattr(transcribe, "cuda_lib_dirs", lambda: ["C:/nvidia/cublas/bin"])
    assert "Скачать и перезапустить" not in client.get("/settings?tab=recordings").text  # скачана, ждёт перезапуска


def test_pause_and_cancel_meeting_waiting_in_queue(tmp_path, settings_with, monkeypatch):
    submitted = []
    monkeypatch.setattr(jobs, "submit", lambda *a, **k: submitted.append(a) or True)
    _, mid = _meeting(tmp_path)
    assert recordings.pause(mid) and records.get_meeting(mid)["status"] == "paused"
    assert jobs.job_transcribe(mid) == "на паузе"  # задание из очереди её не трогает
    jobs.job_recordings()
    assert not submitted
    assert recordings.resume(mid) and records.get_meeting(mid)["status"] == "new"  # процесса нет — снова в очередь
    assert submitted == [("transcribe", "transcribe", {"meeting_id": mid})]
    assert recordings.cancel(mid) and records.get_meeting(mid)["status"] == "cancelled"
    assert jobs.job_transcribe(mid) == "отменена"
    assert not recordings.pause(mid) and not recordings.resume(mid) and not recordings.cancel(mid)
    # расшифровывали заново и передумали — встреча возвращается к прежней расшифровке и разбору
    folder = records.meeting_dir(mid)
    (folder / "transcript.md").write_text("старая", encoding="utf-8")
    (folder / "summary.md").write_text("разбор", encoding="utf-8")
    records.update_meeting(mid, status="new")
    assert recordings.cancel(mid) and records.get_meeting(mid)["status"] == "processed"


def test_unqueued_job_is_skipped_until_asked_again(monkeypatch):
    import queue

    ran = []
    monkeypatch.setattr(jobs, "_queues", {"transcribe": queue.Queue()})
    monkeypatch.setattr(jobs, "_safe_run", lambda name, args: ran.append(args["meeting_id"]))
    for mid in ("a", "b", "c"):
        assert jobs.submit("transcribe", "transcribe", {"meeting_id": mid})
    assert jobs.unqueue("transcribe", {"meeting_id": "a"}) and jobs.unqueue("transcribe", {"meeting_id": "c"})
    assert jobs.submit("transcribe", "transcribe", {"meeting_id": "c"})  # снова нужна — встаёт на своё место
    assert jobs.queued() == [jobs._key("transcribe", {"meeting_id": m}) for m in ("b", "c")]
    stop = threading.Event()
    worker = threading.Thread(target=jobs._worker, args=("transcribe", stop))
    worker.start()
    deadline = time.time() + 10
    while jobs._pending and time.time() < deadline:
        time.sleep(0.05)
    stop.set()
    worker.join(5)
    assert ran == ["b", "c"] and not jobs._pending and not jobs._dropped and not jobs._running


def test_forget_meeting_removes_everything_it_left(tmp_path, settings_with):
    import numpy as np

    from opsdash import anthology, voice

    rec, mid = _meeting(tmp_path)
    _, other = _meeting(tmp_path, "2026-10-27 12-00-00 Другая.m4a")
    folder = records.meeting_dir(mid)
    for name in ("transcript.md", "summary.md", "meta.json", voice.PRINTS_FILE):
        (folder / name).write_text("{}", encoding="utf-8")
    records.update_meeting(mid, status="processed")
    mine = records.add_commitment(direction="mine", what="Прислать прогноз", source_type="meeting", source_ref=mid)
    records.add_commitment(direction="to_me", what="Сверить склад", source_type="meeting", source_ref=mid,
                           kaiten_card_id=77)
    sub = records.add_commitment(direction="other", what="Часть прогноза", source_type="telegram", source_ref="5",
                                 parent_id=mine)
    keep_task = records.add_commitment(direction="mine", what="Чужая", source_type="meeting", source_ref=other)
    for batch, src in ((f"meeting:{mid}", None), ("ask:3", f"meeting:{mid}"), (f"meeting:{other}", None)):
        store.execute("INSERT INTO proposals(kind, title, payload, source_ref, batch, status) VALUES(?,?,?,?,?,?)",
                      ("kaiten.card", f"Карточка {batch}", "{}", src, batch, "pending"))
    kb = anthology.add(kind="decision", title="Склад переносим на пятницу", source_type="meeting", source_ref=mid)
    kb_other = anthology.add(kind="lesson", title="Сверять остатки заранее", source_type="meeting", source_ref=other)
    vec = np.ones(4, dtype="float32")
    voice.remember(mid, "Спикер 1", vec, 30)
    voice.remember(other, "Спикер 2", vec, 30)
    voice.remember_person(5, mid, "Спикер 3", vec, 20)

    assert recordings.memory(mid) == {"commitments": 2, "in_kaiten": 1, "proposals": 2, "kb": 1}
    done = recordings.forget_meeting(mid)
    assert done["commitments"] == 2 and done["proposals"] == 2 and done["kb"] == 1 and done["voices"] == 2
    assert done["file_error"] is None and not rec.exists() and not folder.exists()
    assert records.get_meeting(mid) is None and records.get_meeting(other)
    assert not store.row("SELECT 1 FROM files_seen WHERE path=?", (str(rec),))
    assert {r["id"] for r in store.rows("SELECT id FROM commitments")} == {sub, keep_task}
    assert store.row("SELECT parent_id FROM commitments WHERE id=?", (sub,))["parent_id"] is None
    assert [r["batch"] for r in store.rows("SELECT batch FROM proposals")] == [f"meeting:{other}"]
    assert anthology.get(kb) is None and anthology.get(kb_other)
    assert not anthology.items(q="пятницу") and anthology.items(q="остатки")  # и из поиска
    assert "пятницу" not in (config.DATA_DIR / "knowledge" / anthology.EXPORT).read_text(encoding="utf-8")
    assert set(voice.samples()) == {other} and not voice.person_samples(5)
    with pytest.raises(recordings.MeetingError):
        recordings.forget_meeting(mid)


def test_forget_meeting_stops_its_transcription(tmp_path, settings_with, monkeypatch):
    rec, mid, worker, result = _start_long(tmp_path, monkeypatch)
    recordings.forget_meeting(mid)
    worker.join(15)
    assert not worker.is_alive() and not transcribe._children and result[0].startswith("остановлено")
    assert records.get_meeting(mid) is None and not rec.exists() and not records.meeting_dir(mid).exists()
    assert not _outbox()


def test_meeting_page_controls_and_delete(tmp_path, settings_with, monkeypatch):
    from fastapi.testclient import TestClient

    from opsdash.web.app import app

    monkeypatch.setattr(jobs, "submit", lambda *a, **k: True)
    client = TestClient(app, base_url="http://localhost:8765")
    rec, mid = _meeting(tmp_path)

    def post(path, **data):
        return unquote(client.post(f"/meetings/{mid}/{path}", data=data, follow_redirects=False).headers["location"])

    page = client.get(f"/meetings/{mid}").text
    assert "Ждёт очереди" in page and 'value="pause"' in page and ">Удалить встречу</button>" in page
    assert "Удалить только запись" not in page  # расшифровки нет — только целиком
    assert 'value="pause"' in client.get("/meetings").text
    assert "на паузе" in post("transcription", action="pause")
    assert 'value="resume"' in client.get(f"/meetings/{mid}").text
    assert "err=" in post("transcription", action="взорвать")
    assert "err=" in post("delete", what="recording") and rec.exists()
    assert "err=" in post("delete", what="что-то") and records.get_meeting(mid)  # целиком — только по явному «all»
    post("transcription", action="cancel")
    page = client.get(f"/meetings/{mid}").text
    assert "Расшифровка остановлена" in page and 'value="transcribe"' in page  # «Расшифровать» — снова с начала

    (records.meeting_dir(mid) / "transcript.md").write_text("# Расшифровка", encoding="utf-8")
    records.update_meeting(mid, status="transcribed")
    assert "Удалить только запись" in client.get(f"/meetings/{mid}").text
    assert "Файл записи" in post("delete", what="recording") and not rec.exists()
    page = client.get(f"/meetings/{mid}?tab=record").text
    assert "Файла записи уже нет" in page and records.get_meeting(mid)["status"] == "transcribed"
    assert post("delete", what="all").startswith("/meetings?msg=Встреча") and records.get_meeting(mid) is None


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
