"""Папки с записями встреч → карточки встреч → расшифровка.

PULSE следит за папками из настроек (по умолчанию data/recordings; можно
добавить папку Zoom и папку Яндекс Диска, куда Телемост сохраняет записи).
Новый файл → встреча в data/meetings/<дата>_<название>/ → расшифровка →
разбор Claude (навык process-meeting).
"""

from __future__ import annotations

import json
import re
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

from opsdash import config, records, store, transcribe, voice

AUDIO = {".mp3", ".m4a", ".wav", ".ogg", ".oga", ".opus", ".aac", ".flac", ".wma", ".amr"}
VIDEO = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v"}
TEXT = {".txt", ".vtt", ".srt", ".docx", ".md"}
ALL = AUDIO | VIDEO | TEXT
STABLE_SECONDS = 60

NAME_TIME = [
    # Zoom: «2026-10-25 14.00.12 Планёрка», общие: 2026-10-25_14-00, 20261025_140012
    re.compile(r"(?P<y>20\d{2})[-_.]?(?P<m>\d{2})[-_.]?(?P<d>\d{2})[ _T-]+(?P<H>\d{2})[.\-:_]?(?P<M>\d{2})(?:[.\-:_]?\d{2}(?!\d))?"),
]


TRANSLIT = dict(zip(
    "абвгдеёжзийклмнопрстуфхцчшщъыьэюя",
    ["a", "b", "v", "g", "d", "e", "e", "zh", "z", "i", "y", "k", "l", "m", "n", "o", "p", "r", "s", "t", "u", "f",
     "h", "ts", "ch", "sh", "sch", "", "y", "", "e", "yu", "ya"],
))


def slug(text: str, limit: int = 40) -> str:
    """Латиница для имени папки и id встречи: так безопаснее для путей, ссылок и кнопок Telegram."""
    text = "".join(TRANSLIT.get(ch, ch) for ch in text.lower())
    text = re.sub(r"[^a-z0-9\s-]", "", text).strip()
    text = re.sub(r"[\s_-]+", "-", text)
    return text[:limit].strip("-") or "vstrecha"


def time_from_name(name: str) -> datetime | None:
    for rx in NAME_TIME:
        m = rx.search(name)
        if m:
            try:
                return datetime(int(m["y"]), int(m["m"]), int(m["d"]), int(m["H"]), int(m["M"]), tzinfo=config.tz())
            except ValueError:
                continue
    return None


def clean_title(path: Path) -> str:
    stem = path.stem
    if path.parent.name and time_from_name(path.parent.name) and stem.lower().startswith(("audio", "video", "zoom")):
        stem = path.parent.name  # Zoom кладёт audio1234.m4a в папку с названием встречи
    stem = NAME_TIME[0].sub("", stem)
    stem = re.sub(r"^\d+[_\s]*", "", stem)
    stem = re.sub(r"[_]+", " ", stem).strip(" .-_")
    return stem or "Встреча"


def watch_folders(settings: dict) -> list[Path]:
    out = []
    for f in settings["recordings"].get("folders") or []:
        p = config.resolve_path(f)
        if p.exists():
            out.append(p)
    return out


def scan(settings: dict | None = None) -> list[Path]:
    """Найти новые, уже дописанные файлы записей."""
    settings = settings or config.load_settings()
    since = store.get_state("recordings_since")
    if not since:
        # при первом запуске не разбираем старые архивы целиком — только последние сутки
        since = (datetime.now(timezone.utc) - timedelta(days=1)).timestamp()
        store.set_state("recordings_since", since)
    seen = {r["path"]: r for r in store.rows("SELECT path, size FROM files_seen")}
    now = datetime.now().timestamp()
    found: list[Path] = []
    for folder in watch_folders(settings):
        for p in folder.rglob("*"):
            try:
                if not p.is_file() or p.suffix.lower() not in ALL or p.name.startswith((".", "~")):
                    continue
                if config.MEETINGS_DIR in p.parents:
                    continue
                st = p.stat()
            except OSError:
                continue
            key = str(p)
            if key in seen:
                continue
            if st.st_mtime < float(since) or now - st.st_mtime < STABLE_SECONDS or st.st_size == 0:
                continue
            found.append(p)
    # Zoom сохраняет и видео, и отдельное аудио — берём аудио
    result = []
    for p in found:
        if p.suffix.lower() in VIDEO:
            siblings = [s for s in p.parent.iterdir() if s.suffix.lower() in AUDIO]
            if any(abs(s.stat().st_mtime - p.stat().st_mtime) < 900 for s in siblings):
                mark_seen(p, None)
                continue
        result.append(p)
    return sorted(result, key=lambda x: x.stat().st_mtime)


def mark_seen(path: Path, meeting_id: str | None) -> None:
    try:
        st = path.stat()
        size, mtime = st.st_size, st.st_mtime
    except OSError:
        size, mtime = 0, 0
    store.execute(
        "INSERT INTO files_seen(path, size, mtime, meeting_id, seen_at) VALUES(?,?,?,?,?) "
        "ON CONFLICT(path) DO UPDATE SET meeting_id=excluded.meeting_id",
        (str(path), size, mtime, meeting_id, store.utcnow()),
    )


def create_meeting(path: Path, title: str | None = None) -> str:
    started = time_from_name(path.name) or time_from_name(path.parent.name)
    started_from_name = started is not None
    if not started:
        started = datetime.fromtimestamp(path.stat().st_mtime, tz=config.tz())
    title = title or clean_title(path)
    base = f"{started.astimezone(config.tz()):%Y-%m-%d_%H%M}_{slug(title)}"
    mid, n = base, 2
    while store.row("SELECT 1 FROM meetings WHERE id=?", (mid,)):
        mid, n = f"{base}-{n}", n + 1
    records.meeting_dir(mid).mkdir(parents=True, exist_ok=True)
    now = store.utcnow()
    store.execute(
        """INSERT INTO meetings(id, title, started_at, source_file, status, progress, participants, created, updated)
           VALUES(?,?,?,?, 'new', 0, '[]', ?, ?)""",
        (mid, title, store.to_utc_iso(started), str(path), now, now),
    )
    store.set_state(f"meeting_time_from_name:{mid}", started_from_name)
    mark_seen(path, mid)
    store.log("recordings", f"новая запись: {path.name} → встреча {mid}")
    return mid


def source_gone(meeting_id: str) -> str | None:
    """Почему расшифровывать встречу больше незачем: файл записи удалили из папки или положили на его место
    другой (сменились размер или время). Только пока расшифровки нет: у готовой встречи файл можно удалять."""
    m = store.row("SELECT source_file FROM meetings WHERE id=?", (meeting_id,))
    if not m:
        return "встречи больше нет"
    if (records.meeting_dir(meeting_id) / "transcript.md").exists():
        return None
    path = Path(m["source_file"])
    try:
        st = path.stat()
    except OSError:
        return f"запись {path.name} удалили из папки"
    seen = store.row("SELECT size, mtime FROM files_seen WHERE path=?", (str(path),))
    if seen and seen["mtime"] and (st.st_size != seen["size"] or abs(st.st_mtime - seen["mtime"]) > 1):
        return f"запись {path.name} заменили другим файлом"
    return None


class MeetingError(Exception):
    """Действие со встречей сейчас невозможно — текст для директора."""


IN_QUEUE = ("new", "transcribing", "paused")  # расшифровка ждёт очереди, идёт или стоит на паузе


def _remove(meeting_id: str, source_file: str, *, forget_file: bool) -> None:
    store.execute("DELETE FROM meetings WHERE id=?", (meeting_id,))
    if forget_file:  # если запись вернут (или на том же месте окажется новая), PULSE найдёт её заново
        store.execute("DELETE FROM files_seen WHERE path=?", (source_file,))
    store.execute("DELETE FROM state WHERE key=?", (f"meeting_time_from_name:{meeting_id}",))
    folder = records.meeting_dir(meeting_id)
    if meeting_id and folder.parent == config.MEETINGS_DIR:
        shutil.rmtree(folder, ignore_errors=True)


def drop_meeting(meeting_id: str, reason: str) -> None:
    """Убрать встречу, у которой нет ни расшифровки, ни записи: показывать и расшифровывать нечего."""
    m = store.row("SELECT source_file FROM meetings WHERE id=?", (meeting_id,))
    if not m:
        return
    _remove(meeting_id, m["source_file"], forget_file=True)
    store.log("recordings", f"{reason} — встреча {meeting_id} убрана из очереди")


def _settled_status(meeting_id: str) -> str:
    """Статус встречи, когда расшифровка не идёт, — по тому, что уже лежит в её папке."""
    folder = records.meeting_dir(meeting_id)
    if (folder / "summary.md").exists():
        return "processed"
    if (folder / "transcript.md").exists():
        return "transcribed"
    return "cancelled"


def pause(meeting_id: str) -> bool:
    """Пауза: идущая расшифровка замирает на том же месте и не грузит компьютер, ждущая не начинается."""
    from opsdash import jobs

    m = records.get_meeting(meeting_id)
    if not m or m["status"] not in ("new", "transcribing"):
        return False
    if not transcribe.pause(meeting_id):
        if m["status"] != "new":  # идёт не в PULSE (./ops run) — отсюда её не остановить
            return False
        jobs.unqueue("transcribe", {"meeting_id": meeting_id})
    records.update_meeting(meeting_id, status="paused")
    store.log("recordings", f"{meeting_id}: расшифровка на паузе")
    return True


def resume(meeting_id: str) -> bool:
    """Продолжить с того же места. Если PULSE за это время перезапускали — расшифровка начнётся заново."""
    from opsdash import jobs

    m = records.get_meeting(meeting_id)
    if not m or m["status"] != "paused":
        return False
    records.update_meeting(meeting_id, status="transcribing")  # до разморозки: закончив, процесс сам поставит «готово»
    if not transcribe.resume(meeting_id):
        records.update_meeting(meeting_id, status="new")
        jobs.submit("transcribe", "transcribe", {"meeting_id": meeting_id})
    store.log("recordings", f"{meeting_id}: расшифровка продолжена")
    return True


def cancel(meeting_id: str) -> bool:
    """Отменить расшифровку. Встреча остаётся: новую — можно расшифровать позже, у расшифрованной раньше
    остаются прежние расшифровка и разбор."""
    from opsdash import jobs

    m = records.get_meeting(meeting_id)
    if not m or m["status"] not in IN_QUEUE:
        return False
    jobs.unqueue("transcribe", {"meeting_id": meeting_id})
    transcribe.stop(meeting_id, "расшифровку отменили")
    status = _settled_status(meeting_id)
    records.update_meeting(meeting_id, status=status, progress=0 if status == "cancelled" else 1.0)
    store.log("recordings", f"{meeting_id}: расшифровка отменена")
    return True


def memory(meeting_id: str) -> dict:
    """Что PULSE помнит по встрече, кроме её папки: для предупреждения перед удалением."""
    batch = f"meeting:{meeting_id}"
    return {
        "commitments": store.scalar("SELECT COUNT(*) FROM commitments WHERE source_type='meeting' AND source_ref=?",
                                    (meeting_id,)) or 0,
        "in_kaiten": store.scalar("SELECT COUNT(*) FROM commitments WHERE source_type='meeting' AND source_ref=? "
                                  "AND kaiten_card_id IS NOT NULL", (meeting_id,)) or 0,
        "proposals": store.scalar("SELECT COUNT(*) FROM proposals WHERE batch=? OR source_ref=?", (batch, batch)) or 0,
        "kb": store.scalar("SELECT COUNT(*) FROM anthology WHERE source_type='meeting' AND source_ref=?",
                           (meeting_id,)) or 0,
    }


def delete_recording(meeting_id: str) -> str:
    """Удалить с диска файл записи — расшифровка, разбор и задачи остаются. Вернуть имя файла."""
    m = records.get_meeting(meeting_id)
    if not m:
        raise MeetingError("Такой встречи нет")
    if m["status"] in IN_QUEUE:
        raise MeetingError("Запись ещё не расшифрована — отмените расшифровку или удалите встречу целиком")
    if not (records.meeting_dir(meeting_id) / "transcript.md").exists():
        raise MeetingError("Расшифровки нет — без записи от встречи ничего не останется. Удалите встречу целиком")
    path = Path(m["source_file"])
    try:
        path.unlink(missing_ok=True)
    except OSError as e:
        raise MeetingError(f"Не получилось удалить {path.name}: {e}") from e
    store.log("recordings", f"{meeting_id}: файл записи {path.name} удалён")
    return path.name


def forget_meeting(meeting_id: str) -> dict:
    """Удалить встречу целиком: файл записи, расшифровку и разбор, задачи и договорённости с неё, предложения,
    записи базы знаний, образцы голосов. Карточки, уже созданные в Kaiten, остаются там: внешние системы
    PULSE меняет только по предложениям. Вернуть, что удалено."""
    from opsdash import anthology, jobs

    m = records.get_meeting(meeting_id)
    if not m:
        raise MeetingError("Такой встречи нет")
    args = {"meeting_id": meeting_id}
    if jobs.running("process-meeting", args) or jobs.running("followup", args):  # Claude допишет задачи после нас
        raise MeetingError("Claude сейчас разбирает эту встречу — удалите её через пару минут, когда закончит")
    for name in ("transcribe", "process-meeting", "followup"):
        jobs.unqueue(name, args)
    transcribe.stop(meeting_id, "встречу удалили")
    done = memory(meeting_id)
    path = Path(m["source_file"])
    try:
        path.unlink(missing_ok=True)
        done["file_error"] = None
    except OSError as e:  # остальное всё равно удаляем, а файл помним — чтобы не завести встречу заново
        done["file_error"] = str(e)
    ids = [r["id"] for r in store.rows("SELECT id FROM commitments WHERE source_type='meeting' AND source_ref=?",
                                       (meeting_id,))]
    if ids:
        marks = ",".join("?" * len(ids))
        store.execute(f"UPDATE commitments SET parent_id=NULL WHERE parent_id IN ({marks})", ids)
        store.execute(f"DELETE FROM commitments WHERE id IN ({marks})", ids)
    store.execute("DELETE FROM proposals WHERE batch=? OR source_ref=?", (f"meeting:{meeting_id}",) * 2)
    kb = [r["id"] for r in store.rows("SELECT id FROM anthology WHERE source_type='meeting' AND source_ref=?",
                                      (meeting_id,))]
    if kb:
        anthology.delete(kb)
    done["voices"] = voice.forget_meeting(meeting_id)
    _remove(meeting_id, m["source_file"], forget_file=done["file_error"] is None)
    store.log("recordings", f"встреча {meeting_id} ({m['title']}) удалена целиком: задач {done['commitments']}, "
                            f"предложений {done['proposals']}, записей базы знаний {done['kb']}"
                            + (f"; файл записи не удалён: {done['file_error']}" if done["file_error"] else ""))
    return done


def match_event(start: datetime, end: datetime) -> dict | None:
    """Встреча календаря, больше всего пересекающаяся с записью."""
    from opsdash import overview

    best, best_overlap = None, 0.0
    for ev in overview.events_between(start - timedelta(minutes=15), end + timedelta(minutes=15)):
        if ev["all_day"]:
            continue
        es, ee = store.parse_ts(ev["start"]), store.parse_ts(ev["end"])
        overlap = (min(ee, end) - max(es, start)).total_seconds()
        if overlap > best_overlap:
            best, best_overlap = ev, overlap
    return best


def _tidy_speakers(meeting_id: str, res: dict) -> dict:
    """Склеить части одного голоса и подписать знакомые голоса (директор, люди из справочника) до разбора,
    чтобы Claude не гадал. Не вышло — расшифровка годится и так."""
    plain = {"utterances": res["utterances"], "who": {}, "how": {}, "prints": None}
    if not res["speakers"] or res.get("audio") is None:
        return plain
    try:
        t = voice.tidy(res["audio"], res["utterances"])
    except Exception as e:  # noqa: BLE001
        store.log("voice", f"{meeting_id}: спикеры не уточнены — {e}", "warning")
        return plain
    store.log("voice", f"{meeting_id}: спикеров {t['before']} → {t['after']}"
                       + (f"; узнаны по голосу: {', '.join(t['who'])}" if t["who"] else ""))
    return t


def transcribe_meeting(meeting_id: str, settings: dict | None = None) -> dict:
    settings = settings or config.load_settings()
    m = records.get_meeting(meeting_id)
    if not m:
        raise transcribe.TranscribeError(f"Нет встречи {meeting_id}")
    src = Path(m["source_file"])
    if not src.exists():
        raise transcribe.TranscribeError(f"Файл записи не найден: {src}")
    folder = records.meeting_dir(meeting_id)
    folder.mkdir(parents=True, exist_ok=True)
    records.update_meeting(meeting_id, status="transcribing", progress=0, error="")

    started = store.parse_ts(m["started_at"])
    ext = src.suffix.lower()
    raw_text = None
    diar_error = None
    tidy = {"who": {}, "how": {}, "prints": None}
    if ext in TEXT:
        utterances, raw_text = transcribe.import_text(src)
        duration = max((u["end"] for u in utterances), default=0)
        kind = "текстовая расшифровка"
    else:
        # примерное окно встречи — для подбора числа спикеров по календарю
        pre_event = match_event(started, started + timedelta(hours=1)) if started else None
        n_att = len(store.loads(pre_event["attendees"], [])) if pre_event else 0
        num_speakers = n_att if 2 <= n_att <= 12 else None

        def progress(x: float) -> None:
            records.update_meeting(meeting_id, progress=round(x, 3))

        res = transcribe.transcribe_audio(
            src, settings, num_speakers=num_speakers,
            diarization=bool(settings["recordings"].get("diarization", True)), progress=progress,
        )
        duration, diar_error = res["duration"], res["diarization_error"]
        tidy = _tidy_speakers(meeting_id, res)
        utterances = tidy["utterances"]
        del res  # звук больше не нужен — не держим его в памяти
        kind = "аудио"
        # время по дате изменения файла — это конец записи
        if not store.get_state(f"meeting_time_from_name:{meeting_id}") and duration:
            started = started - timedelta(seconds=duration)

    end = started + timedelta(seconds=duration or 3600)
    event = match_event(started, end)
    title = m["title"]
    participants: list[str] = []
    header = [f"Дата: {started.astimezone(config.tz()):%d.%m.%Y %H:%M}"]
    if duration:
        header.append(f"Длительность: {max(1, round(duration / 60))} мин")
    header.append(f"Источник: {src.name} ({kind})")
    if event:
        title = event["title"] or title
        atts = store.loads(event["attendees"], [])
        participants = [a.get("name") or a.get("email") for a in atts]
        header.append(f"Встреча в календаре: {event['title']}")
        if participants:
            header.append("Участники по календарю: " + ", ".join(participants))
    speakers = sorted({u["speaker"] for u in utterances if u.get("speaker")})
    if speakers and kind == "аудио":
        header.append(f"Спикеры определены автоматически ({len(speakers)}): имена сопоставляются при разборе")
    if diar_error:
        header.append("Спикеры не размечены (ошибка разметки) — см. журнал")
    header += voice.notes(tidy["who"], tidy["how"], settings)
    names = {label: voice.display_name(key, settings) for label, key in tidy["who"].items()}

    (folder / "transcript.md").write_text(
        transcribe.render_markdown(title, header, voice.named(utterances, names), raw_text), encoding="utf-8")
    (folder / "transcript.json").write_text(json.dumps(utterances, ensure_ascii=False, indent=1), encoding="utf-8")
    if tidy["prints"]:
        voice.save_prints(meeting_id, tidy["prints"])
    meta = {
        "id": meeting_id,
        "title": title,
        "started_at": store.to_utc_iso(started),
        "started_local": started.astimezone(config.tz()).strftime("%Y-%m-%d %H:%M"),
        "duration_min": max(1, round(duration / 60)) if duration else None,
        "source_file": str(src),
        "speakers": speakers,
        **({"speaker_names": names, "speaker_people": tidy["who"], "speaker_how": tidy["how"]} if names else {}),
        "event": {
            "uid": event["uid"], "title": event["title"], "organizer": event["organizer"],
            "attendees": store.loads(event["attendees"], []), "description": event["description"],
        } if event else None,
    }
    (folder / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    records.update_meeting(
        meeting_id, status="transcribed", progress=1.0, title=title, duration_sec=duration,
        started_at=store.to_utc_iso(started), event_uid=event["uid"] if event else "", participants=participants,
    )
    store.log("recordings", f"расшифровано: {meeting_id}" + (f" ({max(1, round(duration / 60))} мин)" if duration else ""))
    return meta


def add_file(path: Path, title: str | None = None, copy: bool = False) -> str:
    """Добавить запись вручную (из админки или CLI)."""
    if copy:
        dest = config.RECORDINGS_DIR / path.name
        if dest.exists():
            dest = config.RECORDINGS_DIR / f"{datetime.now():%H%M%S}_{path.name}"
        shutil.copy2(path, dest)
        path = dest
    return create_meeting(path, title)
