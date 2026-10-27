"""Голоса в записях встреч: директор и люди из справочника.

Разметка делит запись на «Спикер 1…N», а кто из них кто, раньше угадывал Claude по смыслу —
и ошибался. Теперь директор отмечает «это я» или подтверждает «это Никита» на странице встречи:
PULSE запоминает отпечаток голоса и в новых записях сам подписывает знакомых ещё до разбора.

Ещё разметка любит делить одного человека на несколько «спикеров» (говорил то громче, то тише,
отошёл от микрофона) — в записи на 5 человек выходило 40. После разметки части с похожим голосом
склеиваются (tidy).
"""

from __future__ import annotations

import json
import re
from functools import lru_cache

from opsdash import config, records, store, transcribe

# сходство голосов (косинус отпечатков той же модели, что размечает спикеров). Замерено на записях
# директора 26.10.2026: его же голос в других записях — 0.68–0.85, другие люди — не выше 0.47
THRESHOLD = 0.6
# склейка частей одной записи (сравниваем группы по среднему сходству всех пар). «Тестовая встреча»
# 26.10.2026: 40 «спикеров» при 0.65 сложились в 5 человек и обрывки; при 0.68 короткие (8 с) части
# одного человека уже остаются отдельно, при 0.62 — то же, что при 0.65
MERGE_THRESHOLD = 0.65
MIN_SECONDS = 3.0        # меньше — отпечаток слишком шумный, такого спикера не подписываем
SAMPLE_SECONDS = 120.0   # больше для отпечатка не нужно
SHORT_SECONDS = 10.0     # спикеры, сказавшие меньше, на странице встречи свёрнуты
STATE_KEY = "owner_voice"
PEOPLE_KEY = "people_voices"  # {id человека: {"samples": {id встречи: {speaker, seconds, vector}}}}
PRINTS_FILE = "speakers.json"  # отпечатки спикеров записи — чтобы подтверждение не перечитывало звук
LABEL_RE = re.compile(r"Спикер \d+")
NOTE_PREFIX = "Вы в записи:"
PEOPLE_PREFIX = "Узнаны:"
CONFIRMED = "отмечено вами"


def owner_name(settings: dict | None = None) -> str:
    return ((settings or config.load_settings())["owner"].get("name") or "").strip() or "Директор"


def spans(utterances: list[dict]) -> dict[str, list[tuple[float, float]]]:
    out: dict[str, list[tuple[float, float]]] = {}
    for u in utterances:
        if u.get("speaker") and u.get("start") is not None:
            out.setdefault(u["speaker"], []).append((float(u["start"]), float(u["end"])))
    return out


@lru_cache(maxsize=1)
def _extractor():
    import sherpa_onnx

    _, model = transcribe.diarization_models()
    return sherpa_onnx.SpeakerEmbeddingExtractor(
        sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(model), num_threads=2))


def embed(audio, parts: list[tuple[float, float]]):
    """Отпечаток голоса по репликам спикера (до SAMPLE_SECONDS речи). None — если речи слишком мало."""
    import numpy as np

    chunks, total = [], 0.0
    for s, e in parts:
        if e - s < 0.8:  # «да», «угу» — почти один шум
            continue
        chunks.append(audio[int(s * transcribe.SR):int(e * transcribe.SR)])
        total += e - s
        if total >= SAMPLE_SECONDS:
            break
    if total < MIN_SECONDS:
        return None
    extractor = _extractor()
    stream = extractor.create_stream()
    stream.accept_waveform(transcribe.SR, np.concatenate(chunks))
    stream.input_finished()
    v = np.asarray(extractor.compute(stream), dtype="float32")
    return v / (float(np.linalg.norm(v)) or 1.0)


def _mean(items) -> object:
    """Средний отпечаток, взвешенный по длительности речи: короткая ошибочная отметка почти не влияет."""
    import numpy as np

    items = list(items)
    if not items:
        return None
    v = sum(np.asarray(s["vector"], dtype="float32") * max(s["seconds"], 0.1) for s in items)
    return v / (float(np.linalg.norm(v)) or 1.0)


def _sample(label: str, vector, seconds: float) -> dict:
    return {"speaker": label, "seconds": round(seconds, 1), "vector": [round(float(x), 5) for x in vector]}


# --- отпечаток директора --------------------------------------------------------

def samples() -> dict:
    return (store.get_state(STATE_KEY) or {}).get("samples", {})


def owner_print():
    """Средний отпечаток по всем отметкам «это я» (взвешенный по длительности речи)."""
    return _mean(samples().values())


def remember(meeting_id: str, label: str, vector, seconds: float) -> None:
    data = store.get_state(STATE_KEY) or {}
    data.setdefault("samples", {})[meeting_id] = _sample(label, vector, seconds)
    store.set_state(STATE_KEY, data)


def forget() -> None:
    store.set_state(STATE_KEY, None)


# --- голоса людей из справочника ----------------------------------------------------

def _people_data() -> dict:
    return store.get_state(PEOPLE_KEY) or {}


def person_samples(person_id: int) -> dict:
    return _people_data().get(str(person_id), {}).get("samples", {})


def remember_person(person_id: int, meeting_id: str, label: str, vector, seconds: float) -> None:
    data = _people_data()
    data.setdefault(str(person_id), {}).setdefault("samples", {})[meeting_id] = _sample(label, vector, seconds)
    store.set_state(PEOPLE_KEY, data)


def forget_person(person_id: int) -> None:
    data = _people_data()
    if data.pop(str(person_id), None) is not None:
        store.set_state(PEOPLE_KEY, data)


def voice_counts() -> dict[int, int]:
    """По скольким записям запомнен голос каждого человека: {id: 2}."""
    return {int(pid): len(d.get("samples", {})) for pid, d in _people_data().items() if d.get("samples")}


def _drop_sample(meeting_id: str, label: str) -> None:
    """Отметка спикера оказалась ошибочной — его голос из этой записи больше ничей."""
    data = store.get_state(STATE_KEY) or {}
    if (data.get("samples", {}).get(meeting_id) or {}).get("speaker") == label:
        del data["samples"][meeting_id]
        store.set_state(STATE_KEY, data)
    people = _people_data()
    changed = False
    for d in people.values():
        if (d.get("samples", {}).get(meeting_id) or {}).get("speaker") == label:
            del d["samples"][meeting_id]
            changed = True
    if changed:
        store.set_state(PEOPLE_KEY, people)


def voices() -> dict[str, object]:
    """Знакомые голоса: {"me": отпечаток директора, "12": отпечаток человека с id 12}."""
    out = {}
    me = owner_print()
    if me is not None:
        out["me"] = me
    alive = {str(p["id"]) for p in records.list_people()}
    for pid, d in _people_data().items():
        v = _mean(d.get("samples", {}).values())
        if v is not None and pid in alive:  # человека удалили из справочника — его голос не подписываем
            out[pid] = v
    return out


def display_name(key: str, settings: dict | None = None) -> str:
    if key == "me":
        return owner_name(settings)
    p = store.row("SELECT name FROM people WHERE id=?", (int(key),))
    return p["name"] if p else "?"


def identify(vector, known: dict[str, object], threshold: float = THRESHOLD) -> tuple[str, float] | None:
    """Чей это голос: лучший знакомый со сходством не ниже threshold."""
    import numpy as np

    scores = [(key, float(np.dot(v, vector))) for key, v in known.items()]
    best = max(scores, key=lambda s: s[1], default=None)
    return (best[0], round(best[1], 2)) if best and best[1] >= threshold else None


def recognize(audio, utterances: list[dict]) -> dict[str, float]:
    """Какие спикеры записи — директор: {«Спикер 2»: 0.85}. Пусто — голос не узнан или не запомнен."""
    import numpy as np

    me = owner_print()
    if me is None:
        return {}
    found = {}
    for label, parts in spans(utterances).items():
        if not LABEL_RE.fullmatch(label):
            continue
        v = embed(audio, parts)
        if v is not None:
            score = float(np.dot(me, v))
            if score >= THRESHOLD:  # бывает, что разметка делит голос директора на двух «спикеров»
                found[label] = round(score, 2)
    return found


# --- спикеры записи: склеить части одного голоса и узнать знакомых -------------------

def speaker_prints(audio, utterances: list[dict]) -> dict[str, dict]:
    """Отпечаток каждого «Спикера N» записи: {label: {"vector", "seconds"}}; без тех, кто сказал слишком мало."""
    out = {}
    for label, parts in spans(utterances).items():
        if not LABEL_RE.fullmatch(label):
            continue
        v = embed(audio, parts)
        if v is not None:
            out[label] = {"vector": v, "seconds": sum(e - s for s, e in parts)}
    return out


def groups(prints: dict[str, dict], threshold: float = MERGE_THRESHOLD) -> list[list[str]]:
    """Части одного голоса: объединяем группы, пока среднее сходство всех пар между ними ≥ threshold.
    Среднее по парам, а не по «центру» группы: иначе большая группа стягивает к себе похожих соседей."""
    import numpy as np

    labels = list(prints)
    sim = {(a, b): float(np.dot(prints[a]["vector"], prints[b]["vector"])) for a in labels for b in labels}
    out = [[label] for label in labels]
    while len(out) > 1:
        best = None
        for i in range(len(out)):
            for j in range(i + 1, len(out)):
                s = sum(sim[a, b] for a in out[i] for b in out[j]) / (len(out[i]) * len(out[j]))
                if s >= threshold and (best is None or s > best[0]):
                    best = (s, i, j)
        if best is None:
            break
        _, i, j = best
        out[i] += out.pop(j)
    return out


def _join(utterances: list[dict], max_len: float = 60.0) -> list[dict]:
    """Соседние реплики одного спикера после склейки — одной репликой, как при разметке."""
    out: list[dict] = []
    for u in utterances:
        cur = out[-1] if out else None
        if (cur and cur.get("speaker") and cur["speaker"] == u.get("speaker") and u["start"] - cur["end"] < 2.0
                and u["end"] - cur["start"] < max_len):
            cur["end"], cur["text"] = u["end"], f"{cur['text']} {u['text']}".strip()
        else:
            out.append(dict(u))
    return out


def tidy(audio, utterances: list[dict], *, renumber: bool = True) -> dict:
    """Привести спикеров записи в порядок: склеить части одного голоса и подписать знакомых.
    renumber — заново «Спикер 1…N» по порядку появления (для новой записи); иначе у группы остаётся
    номер той части, что говорила дольше, — так ссылки в уже готовом разборе остаются верными.
    Возвращает реплики, кто есть кто {label: "me" | id человека}, как узнан, отпечатки и замену номеров."""
    prints = speaker_prints(audio, utterances)
    known = voices()
    mapping: dict[str, str] = {}
    who: dict[str, str] = {}
    how: dict[str, str] = {}
    merged: dict[str, dict] = {}
    by_key: dict[str, str] = {}
    for g in sorted(groups(prints), key=lambda g: -sum(prints[x]["seconds"] for x in g)):
        rep = max(g, key=lambda x: prints[x]["seconds"])
        vector = _mean({"vector": prints[x]["vector"], "seconds": prints[x]["seconds"]} for x in g)
        seconds = sum(prints[x]["seconds"] for x in g)
        hit = identify(vector, known)
        if hit and hit[0] in by_key:  # тот же знакомый голос, что уже нашли, — тоже один человек
            rep = by_key[hit[0]]
            old = merged[rep]
            vector = _mean([{"vector": old["vector"], "seconds": old["seconds"]}, {"vector": vector, "seconds": seconds}])
            seconds += old["seconds"]
        elif hit:
            by_key[hit[0]], who[rep], how[rep] = rep, hit[0], f"узнан по голосу, сходство {hit[1]:.2f}"
        merged[rep] = {"vector": vector, "seconds": seconds}
        for x in g:
            mapping[x] = rep
    out = _join([{**u, "speaker": mapping.get(u.get("speaker"), u.get("speaker"))} for u in utterances])
    if renumber:
        order: dict[str, str] = {}
        for u in out:
            if u.get("speaker") and LABEL_RE.fullmatch(u["speaker"]) and u["speaker"] not in order:
                order[u["speaker"]] = f"Спикер {len(order) + 1}"
        out = [{**u, "speaker": order.get(u.get("speaker"), u.get("speaker"))} for u in out]
        mapping = {old: order.get(new, new) for old, new in mapping.items()}
        who = {order.get(k, k): v for k, v in who.items()}
        how = {order.get(k, k): v for k, v in how.items()}
        merged = {order.get(k, k): v for k, v in merged.items()}
    talk = spans(out)
    return {"utterances": out, "who": who, "how": how, "prints": merged, "mapping": mapping,
            "before": len(spans(utterances)), "after": len(talk),
            "main": sum(sum(e - s for s, e in parts) >= SHORT_SECONDS for parts in talk.values())}


def save_prints(meeting_id: str, prints: dict[str, dict]) -> None:
    (records.meeting_dir(meeting_id) / PRINTS_FILE).write_text(json.dumps(
        {label: {"seconds": round(p["seconds"], 1), "vector": [round(float(x), 5) for x in p["vector"]]}
         for label, p in prints.items()}), encoding="utf-8")


def load_prints(meeting_id: str) -> dict[str, dict] | None:
    import numpy as np

    path = records.meeting_dir(meeting_id) / PRINTS_FILE
    if not path.exists():
        return None
    return {label: {"seconds": p["seconds"], "vector": np.asarray(p["vector"], dtype="float32")}
            for label, p in json.loads(path.read_text(encoding="utf-8")).items()}


def _audio(meeting_id: str, settings: dict):
    """Звук записи — None, если файла нет или это готовая текстовая расшифровка."""
    from pathlib import Path

    from opsdash import recordings

    meta = json.loads((records.meeting_dir(meeting_id) / "meta.json").read_text(encoding="utf-8"))
    src = Path(meta.get("source_file") or "")
    if src.exists() and src.suffix.lower() in recordings.AUDIO | recordings.VIDEO:
        return transcribe.decode(src, settings)
    return None


def meeting_prints(meeting_id: str, settings: dict | None = None) -> dict[str, dict]:
    """Отпечатки спикеров записи: из файла, а для старых записей — снять по звуку и сохранить."""
    prints = load_prints(meeting_id)
    if prints is None:
        audio = _audio(meeting_id, settings or config.load_settings())
        if audio is None:
            return {}
        utterances = json.loads((records.meeting_dir(meeting_id) / "transcript.json").read_text(encoding="utf-8"))
        prints = speaker_prints(audio, utterances)
        save_prints(meeting_id, prints)
    return prints


# --- имена в расшифровке ----------------------------------------------------------

def named(utterances: list[dict], names: dict[str, str]) -> list[dict]:
    return [{**u, "speaker": names.get(u.get("speaker"), u.get("speaker"))} for u in utterances]


def note(names: dict[str, str], how: str) -> str | None:
    if not names:
        return None
    who = next(iter(names.values()))
    return f"{NOTE_PREFIX} {who} = {', '.join(names)} ({how})"


def notes(who: dict[str, str], how: dict[str, str], settings: dict | None = None) -> list[str]:
    """Строки шапки расшифровки: «Вы в записи: …» и «Узнаны: Никита = Спикер 2 (…)» — Claude им доверяет."""
    out = []
    mine = [label for label, key in who.items() if key == "me"]
    if mine:
        out.append(note({label: owner_name(settings) for label in mine}, how.get(mine[0], CONFIRMED)))
    others = [f"{display_name(key, settings)} = {label} ({how.get(label, CONFIRMED)})"
              for label, key in who.items() if key != "me"]
    if others:
        out.append(f"{PEOPLE_PREFIX} " + "; ".join(others))
    return out


def speaker_map(meta: dict) -> tuple[dict[str, str], dict[str, str]]:
    """Кто есть кто из meta.json: {label: "me" | id человека} и как узнан. Записи до голосов людей
    хранили только директора: speaker_names + общий speaker_names_how."""
    if "speaker_people" in meta:
        return dict(meta["speaker_people"]), dict(meta.get("speaker_how") or {})
    names = meta.get("speaker_names") or {}
    how = meta.get("speaker_names_how") or CONFIRMED
    return {label: "me" for label in names}, {label: how for label in names}


def rename(meeting_id: str, who: dict[str, str], how: dict[str, str], settings: dict | None = None) -> None:
    """Переписать transcript.md с именами (transcript.json хранит «Спикер N») и запомнить в meta.json."""
    folder = records.meeting_dir(meeting_id)
    md = (folder / "transcript.md").read_text(encoding="utf-8").splitlines()
    title = md[0].removeprefix("# Расшифровка:").strip() if md else meeting_id
    header = []
    for line in md[2:]:
        if not line.strip():
            break
        header.append(line.rstrip())
    header = [h for h in header if not h.startswith((NOTE_PREFIX, PEOPLE_PREFIX))] + notes(who, how, settings)
    names = {label: display_name(key, settings) for label, key in who.items()}
    utterances = json.loads((folder / "transcript.json").read_text(encoding="utf-8"))
    (folder / "transcript.md").write_text(transcribe.render_markdown(title, header, named(utterances, names)),
                                          encoding="utf-8")
    meta_path = folder / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta.pop("speaker_names_how", None)
    meta.update(speaker_names=names, speaker_people=who, speaker_how={k: v for k, v in how.items() if k in who})
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


GUESS_RE = re.compile(r"^\s*[-*]\s*(Спикер \d+)\s*[—–-]\s*(.+)$")


def guesses(meeting_id: str, meta: dict | None = None) -> dict[str, dict]:
    """Догадки Claude, кто из спикеров кто: из meta.json (./ops meetings guess), а для разборов,
    где их нет, — из раздела «Кто есть кто в записи» в summary.md."""
    meta = meta if meta is not None else json.loads(
        (records.meeting_dir(meeting_id) / "meta.json").read_text(encoding="utf-8"))
    if meta.get("speaker_guesses"):
        return meta["speaker_guesses"]
    out, inside = {}, False
    for line in records.read_meeting_file(meeting_id, "summary.md").splitlines():
        if line.startswith("#"):
            inside = "кто есть кто" in line.lower()
            continue
        m = GUESS_RE.match(line) if inside else None
        if not m:
            continue
        # «Спикер 10 — Никита (уверенно: …). Его реплики…»: имя — до скобки или запятой, почему — в скобках
        name = re.split(r"[(,.;:]", m.group(2), maxsplit=1)[0].strip(" *")
        why = re.search(r"\(([^)]*)\)", m.group(2))
        why = why.group(1).strip() if why else ""
        if not name[:1].isupper() or re.search(r"\?|неизвест", name):
            continue  # «руководитель, который ушёл…», «вероятно, маркетинг» — роль, а не имя
        if name.casefold() == owner_name().casefold() or re.search(r"\bвы\b|директор", why, re.I):
            continue  # директор отмечает себя сам («Это я») — догадкой не подменяем
        out[m.group(1)] = {"name": name, "why": why[:140] + ("…" if len(why) > 140 else "")}
    return out


def speakers_overview(meeting_id: str) -> list[dict]:
    """Для страницы встречи: кто сколько говорил и пример фразы — чтобы узнать себя и других."""
    path = records.meeting_dir(meeting_id) / "transcript.json"
    if not path.exists():
        return []
    utterances = json.loads(path.read_text(encoding="utf-8"))
    out: dict[str, dict] = {}
    for u in utterances:
        label = u.get("speaker")
        if not label or not LABEL_RE.fullmatch(label):
            continue
        s = out.setdefault(label, {"label": label, "seconds": 0.0, "sample": "", "at": None})
        s["seconds"] += float(u["end"]) - float(u["start"])
        text = (u.get("text") or "").strip()
        if not s["sample"] or (len(s["sample"]) < 25 and len(text) > len(s["sample"])):
            s["sample"], s["at"] = text[:90] + ("…" if len(text) > 90 else ""), transcribe.fmt_ts(float(u["start"]))
    for s in out.values():
        s["short"] = s["seconds"] < SHORT_SECONDS
    return sorted(out.values(), key=lambda s: -s["seconds"])


def confirm(meeting_id: str, label: str, key: str, settings: dict | None = None) -> dict:
    """Директор подтвердил: «Спикер N» — это он сам (key="me") или человек из справочника (key=id).
    Запоминаем голос и подписываем речь; другие ещё не подписанные части записи с тем же голосом —
    тоже (разметка могла разделить человека)."""
    settings = settings or config.load_settings()
    folder = records.meeting_dir(meeting_id)
    meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
    utterances = json.loads((folder / "transcript.json").read_text(encoding="utf-8"))
    if label not in spans(utterances):
        raise ValueError(f"В расшифровке нет «{label}»")
    who, how = speaker_map(meta)
    for lbl in [lbl for lbl, k in who.items() if k == key or lbl == label]:  # переотметка заменяет прежнюю
        who.pop(lbl)
        how.pop(lbl, None)
        _drop_sample(meeting_id, lbl)
    voice_saved, also = False, []
    try:
        prints = meeting_prints(meeting_id, settings)
    except Exception as e:  # noqa: BLE001 — без голоса имя в расшифровке всё равно исправим
        store.log("voice", f"{meeting_id}: не удалось снять голос — {e}", "warning")
        prints = {}
    p = prints.get(label)
    if p is not None:
        seconds = min(p["seconds"], SAMPLE_SECONDS)
        if key == "me":
            remember(meeting_id, label, p["vector"], seconds)
        else:
            remember_person(int(key), meeting_id, label, p["vector"], seconds)
        voice_saved = True
        mine = voices().get(key)
        for other, q in prints.items():
            if (other != label and other not in who and mine is not None
                    and identify(q["vector"], {key: mine}, MERGE_THRESHOLD)):
                also.append(other)
    who[label], how[label] = key, CONFIRMED
    for other in also:
        who[other], how[other] = key, "тот же голос, что и у отмеченного вами"
    rename(meeting_id, who, how, settings)
    store.log("voice", f"{meeting_id}: «{label}» — {display_name(key, settings)}; голос "
                       f"{'запомнен' if voice_saved else 'не снят'}" + (f"; тот же голос: {', '.join(also)}" if also else ""))
    return {"voice_saved": voice_saved, "also": also}


def retidy(meeting_id: str, settings: dict | None = None) -> dict:
    """Склеить части одного голоса и узнать знакомых в уже расшифрованной записи — без новой расшифровки.
    Номера спикеров не меняются (у группы — номер самой длинной части): ссылки в разборе остаются верными."""
    settings = settings or config.load_settings()
    folder = records.meeting_dir(meeting_id)
    audio = _audio(meeting_id, settings)
    if audio is None:
        raise ValueError("Нет файла записи — голоса не по чему сравнить")
    utterances = json.loads((folder / "transcript.json").read_text(encoding="utf-8"))
    res = tidy(audio, utterances, renumber=False)
    meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
    old_who, old_how = speaker_map(meta)
    who, how = res["who"], res["how"]
    for old, key in old_who.items():  # отмеченное вами важнее узнанного по голосу
        new = res["mapping"].get(old, old)
        if new not in who or old_how.get(old) == CONFIRMED:
            who[new], how[new] = key, old_how.get(old, CONFIRMED)
    (folder / "transcript.json").write_text(json.dumps(res["utterances"], ensure_ascii=False, indent=1), encoding="utf-8")
    save_prints(meeting_id, res["prints"])
    meta["speakers"] = sorted(spans(res["utterances"]))
    (folder / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    rename(meeting_id, who, how, settings)
    store.log("voice", f"{meeting_id}: спикеров было {res['before']}, стало {res['after']} (заметно говорят {res['main']}); "
                       f"узнаны: {len(who)}")
    return {"before": res["before"], "after": res["after"], "main": res["main"], "known": len(who)}


def mark_person(meeting_id: str, label: str, person_id: int, settings: dict | None = None) -> dict:
    """«Это Никита»: запомнить голос человека из справочника и подписать его в расшифровке."""
    return confirm(meeting_id, label, str(person_id), settings)


def mark_me(meeting_id: str, label: str, settings: dict | None = None) -> dict:
    """«Это я»: подписать речь директора, запомнить голос, убрать итоги прошлого разбора.
    Сам разбор заново ставит вызывающий (задание process-meeting)."""
    from opsdash import proposals

    settings = settings or config.load_settings()
    voice_saved = confirm(meeting_id, label, "me", settings)["voice_saved"]
    # итоги прошлого разбора строились на перепутанных спикерах — убираем то, что ещё можно убрать
    removed = store.execute(
        "DELETE FROM commitments WHERE source_type='meeting' AND source_ref=? AND status='open' AND kaiten_card_id IS NULL",
        (meeting_id,))
    rejected = 0
    for p in proposals.list_proposals("pending", batch=f"meeting:{meeting_id}"):
        proposals.reject(p["id"])
        rejected += 1
    in_kaiten = store.scalar("SELECT COUNT(*) FROM proposals WHERE batch=? AND status='done'", (f"meeting:{meeting_id}",)) or 0
    store.log("voice", f"{meeting_id}: «{label}» — директор; голос {'запомнен' if voice_saved else 'не снят'}, "
                       f"задач убрано: {removed}, предложений отклонено: {rejected}")
    return {"voice_saved": voice_saved, "commitments_removed": removed, "proposals_rejected": rejected,
            "already_in_kaiten": in_kaiten}
