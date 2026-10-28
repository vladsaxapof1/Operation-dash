"""Расшифровка записей: Whisper + разметка спикеров (sherpa-onnx).

Движки Whisper: faster-whisper (Windows, Intel-маки, процессор) и MLX — видеоядра
Apple Silicon, в разы быстрее; при любой ошибке MLX откатываемся на faster-whisper.
Всё работает локально на компьютере, звук никуда не отправляется.
Модели скачиваются один раз в data/models. Записи встреч расшифровываются
в отдельном процессе (run_in_child), чтобы PULSE отвечал и во время расшифровки.
"""

from __future__ import annotations

import multiprocessing
import os
import platform
import re
import signal
import sys
import tarfile
import threading
import time
import traceback
import zipfile
from pathlib import Path
from xml.etree import ElementTree

import httpx

from opsdash import config, store

SR = 16000
DIAR_SEGMENTATION_URL = ("https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-segmentation-models/"
                         "sherpa-onnx-pyannote-segmentation-3-0.tar.bz2")
DIAR_EMBEDDING_URL = ("https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/"
                      "3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx")

_model = None
_model_key = None
_model_device = "cpu"
_model_used = 0.0
_model_lock = threading.Lock()


class TranscribeError(Exception):
    pass


def fmt_ts(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


# --- Whisper ------------------------------------------------------------------

def _prepare_env(settings: dict) -> None:
    """Зеркало HuggingFace — до первой загрузки модели. Если библиотека уже загружена,
    подменяем адрес и в ней (она читает его один раз при импорте)."""
    endpoint = (settings["recordings"].get("hf_endpoint") or "").rstrip("/")
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
    if not endpoint:
        return
    os.environ["HF_ENDPOINT"] = endpoint
    constants = sys.modules.get("huggingface_hub.constants")
    if constants is not None:
        constants.ENDPOINT = endpoint
        constants.HUGGINGFACE_CO_URL_TEMPLATE = endpoint + "/{repo_id}/resolve/{revision}/{filename}"


def models_dir() -> Path:
    """Папка моделей. На Windows при кириллице в пути — C:\\ProgramData\\Pult\\models:
    некоторые библиотеки распознавания плохо открывают файлы по не-ASCII путям."""
    path = config.MODELS_DIR
    if os.name == "nt" and not str(path).isascii():
        alt = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "Pult" / "models"
        try:
            alt.mkdir(parents=True, exist_ok=True)
            return alt
        except OSError:
            pass
    return path


def get_model(settings: dict):
    global _model, _model_key, _model_used, _model_device
    rs = settings["recordings"]
    name = rs.get("whisper_model") or "large-v3-turbo"
    device = rs.get("device") or "auto"
    key = (name, device)
    with _model_lock:
        if _model is not None and _model_key == key:
            _model_used = time.time()
            return _model
        _prepare_env(settings)
        from faster_whisper import WhisperModel

        root = str(models_dir() / "whisper")
        threads = max(2, (os.cpu_count() or 4) - 2)
        model = None
        if device in ("auto", "cuda") and not _cuda_failed:
            try:
                import ctranslate2

                if ctranslate2.get_cuda_device_count() > 0:
                    model = WhisperModel(name, device="cuda", compute_type="float16", download_root=root)
            except Exception as e:  # noqa: BLE001 — нет CUDA-библиотек: работаем на процессоре
                if device == "cuda":
                    store.log("transcribe", f"видеокарта недоступна, перехожу на процессор: {e}", "warning")
        _model_device = "cuda" if model is not None else "cpu"
        if model is None:
            model = WhisperModel(name, device="cpu", compute_type="int8", download_root=root, cpu_threads=threads)
        _model, _model_key, _model_used = model, key, time.time()
        return model


_cuda_failed = False


def _cpu_fallback(settings: dict, error: Exception):
    """Видеокарта есть, но библиотек CUDA нет — дальше считаем на процессоре."""
    global _model, _model_key, _cuda_failed
    store.log("transcribe", f"видеокарта не сработала, считаю на процессоре: {error}", "warning")
    with _model_lock:
        _cuda_failed = True
        _model, _model_key = None, None
    return get_model(settings)


def release_if_idle(idle_seconds: int = 600) -> None:
    """Освободить память, если модель давно не использовалась."""
    global _model, _model_key
    with _model_lock:
        if _model is not None and time.time() - _model_used > idle_seconds:
            _model, _model_key = None, None


def decode(path: Path, settings: dict | None = None):
    _prepare_env(settings or config.load_settings())
    from faster_whisper import decode_audio

    try:
        return decode_audio(str(path), sampling_rate=SR)
    except Exception as e:  # noqa: BLE001
        raise TranscribeError(f"Не удалось прочитать звук из файла {path.name}: {e}") from e


# имена моделей faster-whisper → готовые MLX-версии на HuggingFace
MLX_MODELS = {
    "large-v3-turbo": "mlx-community/whisper-large-v3-turbo",
    "large-v3": "mlx-community/whisper-large-v3-mlx",
    "medium": "mlx-community/whisper-medium-mlx",
    "small": "mlx-community/whisper-small-mlx",
    "base": "mlx-community/whisper-base-mlx",
    "tiny": "mlx-community/whisper-tiny-mlx",
}
_mlx_failed = False


def is_apple_silicon() -> bool:
    return sys.platform == "darwin" and platform.machine() == "arm64"


def engine(settings: dict) -> str:
    """Каким движком расшифровывать: 'mlx' или 'faster-whisper'."""
    choice = settings["recordings"].get("engine") or "auto"
    if choice == "faster-whisper" or _mlx_failed or not is_apple_silicon():
        return "faster-whisper"
    try:
        import mlx_whisper  # noqa: F401
    except Exception:  # noqa: BLE001 — пакет не установлен (macOS 13 или Intel)
        return "faster-whisper"
    return "mlx"


def mlx_words(audio, settings: dict, progress=None) -> tuple[list[dict], list[dict]]:
    import mlx_whisper

    _prepare_env(settings)
    name = settings["recordings"].get("whisper_model") or "large-v3-turbo"
    result = mlx_whisper.transcribe(
        audio, path_or_hf_repo=MLX_MODELS.get(name, name), language=settings["recordings"].get("language") or None,
        word_timestamps=True, condition_on_previous_text=False, verbose=None,
    )
    segments, words = [], []
    for seg in result.get("segments") or []:
        segments.append({"start": float(seg["start"]), "end": float(seg["end"]), "text": seg["text"].strip()})
        for w in seg.get("words") or []:
            words.append({"start": float(w["start"]), "end": float(w["end"]), "word": w["word"]})
    if progress:
        progress(1.0)
    return segments, words


def whisper_words(audio, settings: dict, progress=None) -> tuple[list[dict], list[dict]]:
    """Вернуть (сегменты, слова) с отметками времени."""
    global _mlx_failed
    if engine(settings) == "mlx":
        try:
            return mlx_words(audio, settings, progress)
        except Exception as e:  # noqa: BLE001 — откат на проверенный движок
            _mlx_failed = True
            store.log("transcribe", f"MLX не сработал, считаю через faster-whisper: {e}", "warning")
    model = get_model(settings)
    lang = settings["recordings"].get("language") or None
    duration = max(len(audio) / SR, 1)

    def run(m):
        segments_iter, _info = m.transcribe(
            audio, language=lang, vad_filter=True, word_timestamps=True, beam_size=5,
            condition_on_previous_text=False,
        )
        segments, words = [], []
        for seg in segments_iter:
            segments.append({"start": seg.start, "end": seg.end, "text": seg.text.strip()})
            for w in seg.words or []:
                words.append({"start": w.start, "end": w.end, "word": w.word})
            if progress:
                progress(min(seg.end / duration, 1.0))
        return segments, words

    try:
        return run(model)
    except Exception as e:  # noqa: BLE001
        if _model_device != "cuda":
            raise
        return run(_cpu_fallback(settings, e))


# --- спикеры ------------------------------------------------------------------

def _download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    with httpx.stream("GET", url, follow_redirects=True, timeout=120) as resp:
        resp.raise_for_status()
        with open(tmp, "wb") as f:
            for chunk in resp.iter_bytes(1 << 20):
                f.write(chunk)
    tmp.replace(dest)


def diarization_models() -> tuple[Path, Path]:
    base = models_dir() / "diarization"
    seg = base / "segmentation.onnx"
    emb = base / "embedding.onnx"
    if not seg.exists():
        archive = base / "segmentation.tar.bz2"
        _download(DIAR_SEGMENTATION_URL, archive)
        with tarfile.open(archive, "r:bz2") as tar:
            member = next(m for m in tar.getmembers() if m.name.endswith("/model.onnx"))
            with tar.extractfile(member) as src, open(seg, "wb") as out:
                out.write(src.read())
        archive.unlink(missing_ok=True)
    if not emb.exists():
        _download(DIAR_EMBEDDING_URL, emb)
    return seg, emb


def diarize(audio, settings: dict, num_speakers: int | None = None) -> list[dict]:
    import sherpa_onnx

    seg_model, emb_model = diarization_models()
    threads = max(2, (os.cpu_count() or 4) - 2)
    threshold = float(settings["recordings"].get("diarization_threshold") or 0.9)
    cfg = sherpa_onnx.OfflineSpeakerDiarizationConfig(
        segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
            pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(model=str(seg_model)),
            num_threads=threads,
        ),
        embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(emb_model), num_threads=threads),
        clustering=sherpa_onnx.FastClusteringConfig(num_clusters=num_speakers or -1, threshold=threshold),
        min_duration_on=0.3,
        min_duration_off=0.5,
    )
    if not cfg.validate():
        raise TranscribeError("Неверная конфигурация разметки спикеров")
    sd = sherpa_onnx.OfflineSpeakerDiarization(cfg)
    result = sd.process(audio).sort_by_start_time()
    return [{"start": r.start, "end": r.end, "speaker": int(r.speaker)} for r in result]


def assign_speakers(words: list[dict], turns: list[dict], max_len: float = 60.0) -> list[dict]:
    """Разложить слова по спикерам и склеить в реплики."""
    if not turns:
        return []
    order: dict[int, int] = {}

    def label(spk: int) -> str:
        if spk not in order:
            order[spk] = len(order) + 1
        return f"Спикер {order[spk]}"

    def speaker_at(t: float) -> int:
        best, best_d = turns[0]["speaker"], float("inf")
        for tr in turns:
            if tr["start"] <= t <= tr["end"]:
                return tr["speaker"]
            d = min(abs(t - tr["start"]), abs(t - tr["end"]))
            if d < best_d:
                best, best_d = tr["speaker"], d
        return best

    utterances: list[dict] = []
    for w in words:
        spk = label(speaker_at((w["start"] + w["end"]) / 2))
        cur = utterances[-1] if utterances else None
        if (cur and cur["speaker"] == spk and w["start"] - cur["end"] < 2.0
                and w["end"] - cur["start"] < max_len):
            cur["text"] += w["word"]
            cur["end"] = w["end"]
        else:
            utterances.append({"start": w["start"], "end": w["end"], "speaker": spk, "text": w["word"]})
    for u in utterances:
        u["text"] = u["text"].strip()
    return [u for u in utterances if u["text"]]


# --- импорт готовых текстовых расшифровок ---------------------------------------

CUE_TIME = re.compile(r"(\d{1,2}:)?\d{1,2}:\d{2}[.,]\d{1,3}\s*-->\s*")


def _cue_seconds(stamp: str) -> float:
    parts = stamp.replace(",", ".").split(":")
    parts = [float(p) for p in parts]
    while len(parts) < 3:
        parts.insert(0, 0.0)
    return parts[0] * 3600 + parts[1] * 60 + parts[2]


def parse_subtitles(text: str) -> list[dict]:
    out = []
    blocks = re.split(r"\n\s*\n", text.replace("\r", ""))
    for b in blocks:
        lines = [ln for ln in b.strip().split("\n") if ln.strip()]
        idx = next((i for i, ln in enumerate(lines) if "-->" in ln), None)
        if idx is None:
            continue
        start = _cue_seconds(lines[idx].split("-->")[0].strip())
        end = _cue_seconds(lines[idx].split("-->")[1].strip().split(" ")[0])
        body = " ".join(lines[idx + 1:])
        speaker = None
        m = re.match(r"<v\s+([^>]+)>(.*)", body)
        if m:
            speaker, body = m.group(1).strip(), m.group(2)
        else:
            m = re.match(r"^([^:]{2,40}):\s+(.+)$", body)
            if m:
                speaker, body = m.group(1).strip(), m.group(2)
        body = re.sub(r"</?[^>]+>", "", body).strip()
        if body:
            out.append({"start": start, "end": end, "speaker": speaker, "text": body})
    return out


def docx_text(path: Path) -> str:
    ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    with zipfile.ZipFile(path) as z:
        root = ElementTree.fromstring(z.read("word/document.xml"))
    paras = []
    for p in root.iter(f"{ns}p"):
        t = "".join(x.text or "" for x in p.iter(f"{ns}t"))
        if t.strip():
            paras.append(t)
    return "\n".join(paras)


def read_text_any(data: bytes) -> str:
    """UTF-8 (с BOM или без), UTF-16, иначе Windows-1251 — так сохраняют файлы на русской Windows."""
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1251", errors="replace")


def import_text(path: Path) -> tuple[list[dict], str]:
    """Вернуть (реплики, исходный текст) для текстовой расшифровки."""
    ext = path.suffix.lower()
    if ext == ".docx":
        raw = docx_text(path)
    else:
        raw = read_text_any(path.read_bytes())
    if ext in (".vtt", ".srt") or "-->" in raw[:2000]:
        cues = parse_subtitles(raw)
        if cues:
            return cues, raw
    return [], raw


# --- запись результата ----------------------------------------------------------

def render_markdown(title: str, header: list[str], utterances: list[dict], raw_text: str | None = None) -> str:
    lines = [f"# Расшифровка: {title}", ""]
    lines += [f"{h}  " for h in header]
    lines.append("")
    if utterances:
        for u in utterances:
            who = f"{u['speaker']}: " if u.get("speaker") else ""
            stamp = f"[{fmt_ts(u['start'])}] " if u.get("start") is not None else ""
            lines.append(f"{stamp}{who}{u['text']}")
            lines.append("")
    elif raw_text:
        lines.append(raw_text.strip())
    return "\n".join(lines).rstrip() + "\n"


def transcribe_audio(path: Path, settings: dict, *, num_speakers: int | None = None, diarization: bool = True,
                     progress=None) -> dict:
    audio = decode(path, settings)
    duration = len(audio) / SR
    if duration < 1:
        raise TranscribeError("В файле нет звука")

    def p_whisper(x):
        if progress:
            progress(x * (0.8 if diarization else 1.0))

    segments, words = whisper_words(audio, settings, p_whisper)
    utterances, diar_error = [], None
    if diarization and words:
        try:
            turns = diarize(audio, settings, num_speakers)
            utterances = assign_speakers(words, turns)
        except Exception as e:  # noqa: BLE001 — без спикеров расшифровка всё равно полезна
            diar_error = str(e)
            store.log("transcribe", f"разметка спикеров не удалась: {e}", "warning")
    if not utterances:
        utterances = [{"start": s["start"], "end": s["end"], "speaker": None, "text": s["text"]} for s in segments]
    if progress:
        progress(1.0)
    return {"duration": duration, "utterances": utterances, "diarization_error": diar_error,
            "speakers": sorted({u["speaker"] for u in utterances if u.get("speaker")}),
            "audio": audio}  # для узнавания голоса директора (voice.recognize)


def transcribe_short(path: Path, settings: dict | None = None) -> str:
    """Голосовое сообщение → текст (без разметки спикеров)."""
    settings = settings or config.load_settings()
    audio = decode(path, settings)
    segments, _ = whisper_words(audio, settings)
    return " ".join(s["text"] for s in segments).strip()


# --- отдельный процесс ------------------------------------------------------------
# Whisper и разметка спикеров подолгу не отпускают GIL: в процессе PULSE на время расшифровки
# часовой записи замирали админка и бот. Поэтому расшифровка встречи идёт в своём процессе.

_children: dict = {}  # процесс → почему остановлен: None — работает, SHUTDOWN — вместе с PULSE, иначе причина отмены
_tags: dict = {}      # метка (id встречи) → процесс: чтобы поставить на паузу или отменить именно его
_paused: set = set()  # замороженные процессы
_guards: dict = {}    # процесс → замок, без которого он не трогает базу (store.set_guard)
_children_lock = threading.Lock()
WATCH_SECONDS = 15  # как часто спрашивать cancel, пока идёт расшифровка
SHUTDOWN = "остановлено вместе с PULSE"


class Interrupted(Exception):
    """Процесс остановили вместе с PULSE — это не ошибка расшифровки."""


class Cancelled(Exception):
    """Считать дальше незачем (запись убрали из папки, расшифровку отменили) — это не ошибка расшифровки."""


class RemoteTraceback(Exception):
    """Трассировка из дочернего процесса — чтобы в журнале было видно, где именно упало."""

    def __str__(self) -> str:
        return self.args[0]


def _child_main(fn, args: tuple, conn, guard=None) -> None:
    signal.signal(signal.SIGINT, signal.SIG_IGN)  # Ctrl+C в окне PULSE: нас остановит сам PULSE
    if guard is not None:
        store.set_guard(guard)
    parent = multiprocessing.parent_process()
    if parent is not None:
        def watch() -> None:  # PULSE убит без остановки — не считаем впустую
            parent.join()
            os._exit(1)

        threading.Thread(target=watch, daemon=True).start()
    try:
        conn.send((True, fn(*args), None))
    except BaseException as e:  # noqa: BLE001 — любую ошибку передаём PULSE
        conn.send((False, str(e) or type(e).__name__, traceback.format_exc()))
    finally:
        conn.close()


def run_in_child(fn, *args, cancel=None, tag=None):
    """Выполнить fn(*args) в отдельном процессе и дождаться результата. Ошибка внутри —
    TranscribeError с тем же текстом; остановка PULSE (stop_children) — Interrupted.
    fn — функция уровня модуля, аргументы и результат — простые данные (передаются через pickle).
    cancel() спрашиваем раз в WATCH_SECONDS: вернула причину два раза подряд — процесс останавливаем,
    Cancelled с этой причиной. Два раза — чтобы не отменить из-за мгновенной подмены файла облачной папкой.
    tag — по нему процесс ставят на паузу (pause / resume) и отменяют (stop — тоже Cancelled)."""
    ctx = multiprocessing.get_context("spawn")
    recv, send = ctx.Pipe(duplex=False)
    guard = ctx.RLock() if tag is not None else None
    proc = ctx.Process(target=_child_main, args=(fn, args, send, guard), daemon=True,
                       name=f"pult-{getattr(fn, '__name__', 'child')}")
    with _children_lock:
        proc.start()
        _children[proc] = None
        if tag is not None:
            _tags[tag] = proc
            _guards[proc] = guard
    send.close()  # иначе не узнаем, что процесс завершился, ничего не прислав
    ok = value = tb = cancelled = None
    try:
        strikes = 0
        while cancel is not None and not recv.poll(WATCH_SECONDS):
            try:
                reason = cancel()
            except Exception:  # noqa: BLE001 — база занята и т. п.: проверим в следующий раз
                reason = None
            strikes = strikes + 1 if reason else 0
            if strikes >= 2:
                cancelled = reason
                break
        if cancelled is None:
            ok, value, tb = recv.recv()
    except EOFError:  # упал или его остановили
        pass
    finally:
        recv.close()
        with _children_lock:  # дальше процессом не управляют: он закончил или его сейчас остановим
            if _tags.get(tag) is proc:
                del _tags[tag]
            paused = proc in _paused
            _paused.discard(proc)
        if paused:  # замороженный не завершится, пока его не разморозить
            _unfreeze(proc)
        if ok is None and proc.is_alive():  # ждать перестали (отмена, Ctrl+C в ./ops run) — процесс больше не нужен
            proc.terminate()
        proc.join(30)
        if proc.is_alive():  # результат прислал, но завис при выходе
            proc.kill()
            proc.join()
        with _children_lock:
            stopped = _children.pop(proc, None)
            _guards.pop(proc, None)
    if ok:
        return value
    if stopped == SHUTDOWN:
        raise Interrupted(SHUTDOWN)
    if stopped or cancelled:
        raise Cancelled(stopped or cancelled)
    if ok is None:
        raise TranscribeError(f"Процесс расшифровки неожиданно завершился (код {proc.exitcode})")
    raise TranscribeError(value) from RemoteTraceback(tb)


def _freeze(proc, on: bool) -> None:
    """Заморозить процесс (считать перестаёт, память и прогресс остаются) или разморозить.
    На Windows нет SIGSTOP — там NtSuspendProcess."""
    if os.name == "nt":
        import ctypes

        handle = ctypes.windll.kernel32.OpenProcess(0x0800, False, proc.pid)  # PROCESS_SUSPEND_RESUME
        if not handle:
            raise OSError("нет доступа к процессу расшифровки")
        try:
            (ctypes.windll.ntdll.NtSuspendProcess if on else ctypes.windll.ntdll.NtResumeProcess)(handle)
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    else:
        os.kill(proc.pid, signal.SIGSTOP if on else signal.SIGCONT)


def _unfreeze(proc) -> None:
    try:
        _freeze(proc, False)
    except OSError:  # уже завершился
        pass


def _set_paused(tag, on: bool) -> bool:
    with _children_lock:
        proc = _tags.get(tag)
        if proc is None:
            return False
        if (proc in _paused) != on:
            guard = _guards.get(proc) if on else None
            if guard is not None and not guard.acquire(timeout=10):  # пишет в базу дольше обычного — не сейчас
                return False
            try:
                _freeze(proc, on)
            except OSError:
                return False
            finally:
                if guard is not None:
                    guard.release()
            (_paused.add if on else _paused.discard)(proc)
    return True


def pause(tag) -> bool:
    """Поставить на паузу процесс с этой меткой. False — такого процесса нет."""
    return _set_paused(tag, True)


def resume(tag) -> bool:
    """Продолжить процесс с этой меткой с того же места. False — такого процесса нет."""
    return _set_paused(tag, False)


def stop(tag, reason: str, timeout: float = 10) -> bool:
    """Остановить процесс с этой меткой и дождаться, пока он завершится; run_in_child поднимет Cancelled(reason).
    False — такого процесса нет."""
    from multiprocessing import connection

    with _children_lock:
        proc = _tags.pop(tag, None)
        if proc is None:
            return False
        _children[proc] = reason
        paused = proc in _paused
        _paused.discard(proc)
    if paused:
        _unfreeze(proc)
    proc.terminate()
    connection.wait([proc.sentinel], timeout)
    return True


def stop_children(timeout: float = 5) -> None:
    """Остановить процессы расшифровки (при остановке PULSE), чтобы они не остались висеть."""
    with _children_lock:
        procs = list(_children)
        for p in procs:
            _children[p] = SHUTDOWN
        paused = list(_paused)
        _paused.clear()
    for p in paused:
        _unfreeze(p)
    for p in procs:
        p.terminate()
    for p in procs:
        p.join(timeout)
        if p.is_alive():
            p.kill()
