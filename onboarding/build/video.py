"""Сборка видео-онбординга: озвучка (голос macOS «Milena»), переходы между кадрами, главы, субтитры.

Нужны кадры (render.py), ffmpeg и macOS (команда say):
    python3 onboarding/build/video.py
Результат — в папке onboarding: полное видео, видео по частям, субтитры .srt и сценарий с таймкодами.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import wave
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from storyboard import CHAPTERS, SAY, SCENES  # noqa: E402

FRAMES = HERE / "frames"
WORK = HERE / "work"
OUT = HERE.parent
VOICE, RATE = "Milena", 185
LEAD, TAIL, XFADE = 0.5, 0.9, 0.4  # тишина до и после фразы; переход между кадрами
SR = 48000
FULL = OUT / "PULSE — видео-онбординг.mp4"


def run(*cmd: str) -> str:
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode:
        raise SystemExit(f"{cmd[0]}: {res.stderr[-2000:]}")
    return res.stdout


def speech(text: str) -> str:
    """Текст для голоса: латиница — так, как её произносят по-русски."""
    for src, dst in sorted(SAY, key=lambda p: -len(p[0])):
        text = re.sub(rf"(?<![A-Za-z_]){re.escape(src)}(?![A-Za-z_])", dst, text)
    return text.replace("«", "").replace("»", "")


def voice(scene: dict) -> tuple[Path, float]:
    """Озвучка сцены (wav, 48 кГц моно) и её длительность; готовая не перезаписывается, если текст тот же."""
    wav = WORK / f'{scene["id"]}.wav'
    txt = WORK / f'{scene["id"]}.txt'
    said = speech(scene["text"])
    if not wav.exists() or not txt.exists() or txt.read_text(encoding="utf-8") != said:
        aiff = WORK / f'{scene["id"]}.aiff'
        run("say", "-v", VOICE, "-r", str(RATE), "-o", str(aiff), said)
        run("ffmpeg", "-v", "error", "-y", "-i", str(aiff), "-ar", str(SR), "-ac", "1", str(wav))
        aiff.unlink()
        txt.write_text(said, encoding="utf-8")
    with wave.open(str(wav)) as w:
        return wav, w.getnframes() / w.getframerate()


def silence(seconds: float) -> bytes:
    return b"\x00\x00" * int(round(seconds * SR))


def ts(t: float, sep: str = ",") -> str:
    h, rem = divmod(int(t * 1000), 3600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{ms:03d}"


def chunks(text: str, limit: int = 95) -> list[str]:
    """Субтитры по фразам: не длиннее limit знаков, режем по концу предложения, иначе по запятой."""
    out: list[str] = []
    for sent in re.split(r"(?<=[.!?…])\s+", text.strip()):
        while len(sent) > limit:
            cut = max(sent.rfind(", ", 0, limit), sent.rfind(" — ", 0, limit), sent.rfind(": ", 0, limit))
            cut = cut if cut > limit // 3 else sent.rfind(" ", 0, limit)
            out.append(sent[:cut + 1].strip())
            sent = sent[cut + 1:].strip()
        if out and len(out[-1]) + len(sent) < limit * 0.55:
            out[-1] = f"{out[-1]} {sent}"
        elif sent:
            out.append(sent)
    return out


def main() -> None:
    WORK.mkdir(exist_ok=True)
    plan = []
    for s in SCENES:
        wav, dur = voice(s)
        plan.append({"scene": s, "wav": wav, "speech": dur, "len": LEAD + dur + TAIL})
        print(f'озвучка {s["id"]}: {dur:.1f} с')
    # время начала сцены: каждый переход съедает XFADE секунд из «хвоста» тишины предыдущей
    t = 0.0
    for p in plan:
        p["start"] = t
        t += p["len"] - XFADE
    total = plan[-1]["start"] + plan[-1]["len"]

    # звук: фраза, а вокруг тишина до длины сцены (минус переход — кроме последней)
    audio = WORK / "audio.wav"
    with wave.open(str(audio), "wb") as out:
        out.setnchannels(1), out.setsampwidth(2), out.setframerate(SR)
        for i, p in enumerate(plan):
            with wave.open(str(p["wav"])) as w:
                pcm = w.readframes(w.getnframes())
            length = p["len"] - (XFADE if i < len(plan) - 1 else 0)
            body = silence(LEAD) + pcm
            out.writeframes(body + silence(max(0.0, length - len(body) / 2 / SR)))

    # картинка: кадры с плавными переходами
    inputs, chain = [], []
    for i, p in enumerate(plan):
        inputs += ["-loop", "1", "-framerate", "30", "-t", f'{p["len"]:.3f}', "-i", str(FRAMES / f'{p["scene"]["id"]}.png')]
        chain.append(f"[{i}:v]scale=1920:1080,format=yuv420p,setsar=1,fps=30,settb=AVTB[v{i}]")
    prev = "v0"
    for i in range(1, len(plan)):
        off = plan[i]["start"]
        chain.append(f"[{prev}][v{i}]xfade=transition=fade:duration={XFADE}:offset={off:.3f}[x{i}]")
        prev = f"x{i}"
    graph = WORK / "graph.txt"
    graph.write_text(";\n".join(chain), encoding="utf-8")
    starts = {}
    for p in plan:
        starts.setdefault(p["scene"]["chapter"], p["start"])
    keyframes = ",".join(f"{v:.3f}" for v in starts.values())
    video = WORK / "video.mp4"
    print("собираю видео…")
    run("ffmpeg", "-v", "error", "-y", *inputs, "-/filter_complex", str(graph), "-map", f"[{prev}]",
        "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-tune", "stillimage", "-pix_fmt", "yuv420p",
        "-force_key_frames", keyframes, "-t", f"{total:.3f}", str(video))

    # субтитры и главы
    srt, n = [], 0
    for p in plan:
        parts = chunks(p["scene"]["text"])
        weights = [len(x) for x in parts]
        t0 = p["start"] + LEAD
        for part, w in zip(parts, weights):
            t1 = t0 + p["speech"] * w / sum(weights)
            n += 1
            srt.append(f"{n}\n{ts(t0)} --> {ts(t1)}\n{part}\n")
            t0 = t1
    subs = OUT / "PULSE — субтитры.srt"
    subs.write_text("\n".join(srt), encoding="utf-8")
    titles = dict(CHAPTERS)
    keys = list(starts)
    meta = [";FFMETADATA1", "title=PULSE — видео-онбординг"]
    for i, key in enumerate(keys):
        end = starts[keys[i + 1]] if i + 1 < len(keys) else total
        meta += ["[CHAPTER]", "TIMEBASE=1/1000", f"START={int(starts[key] * 1000)}", f"END={int(end * 1000)}",
                 f"title={titles[key]}"]
    chapters = WORK / "chapters.txt"
    chapters.write_text("\n".join(meta) + "\n", encoding="utf-8")
    run("ffmpeg", "-v", "error", "-y", "-i", str(video), "-i", str(audio), "-i", str(subs), "-i", str(chapters),
        "-map", "0:v", "-map", "1:a", "-map", "2:s", "-map_metadata", "3", "-map_chapters", "3",
        "-c:v", "copy", "-af", "loudnorm=I=-16:TP=-1.5:LRA=7", "-ar", "48000", "-c:a", "aac", "-b:a", "160k",
        "-c:s", "mov_text", "-metadata:s:s:0", "language=rus",
        "-metadata:s:a:0", "language=rus", "-movflags", "+faststart", str(FULL))

    # части по главам (вступление — вместе с установкой)
    parts = [("install", ["intro", "install"]), ("setup", ["setup"]), ("tour", ["tour"]), ("bot", ["bot"])]
    for key, group in parts:
        a = starts[group[0]]
        last = keys.index(group[-1])
        b = starts[keys[last + 1]] if last + 1 < len(keys) else total
        name = OUT / f"PULSE — {titles[key]}.mp4"
        run("ffmpeg", "-v", "error", "-y", "-ss", f"{a:.3f}", "-to", f"{b:.3f}", "-i", str(FULL), "-map", "0:v", "-map", "0:a",
            "-c", "copy", "-movflags", "+faststart", str(name))

    # сценарий с таймкодами — чтобы переозвучить живым голосом или поправить текст
    lines = ["# PULSE — сценарий видео-онбординга", "",
             f"Длительность: {int(total // 60)} мин {int(total % 60)} с · сцен: {len(plan)}. Кадры — `build/frames/<id>.png`.", ""]
    current = None
    for p in plan:
        if p["scene"]["chapter"] != current:
            current = p["scene"]["chapter"]
            lines += [f"## {titles[current]}", ""]
        lines += [f'**{ts(p["start"], ".")[:8]}** · `{p["scene"]["id"]}`  ', p["scene"]["text"], ""]
    (OUT / "PULSE — сценарий.md").write_text("\n".join(lines), encoding="utf-8")
    info = json.loads(run("ffprobe", "-v", "error", "-show_entries", "format=duration,size", "-of", "json", str(FULL)))
    print(f'готово: {FULL.name} — {float(info["format"]["duration"]) / 60:.1f} мин, '
          f'{int(info["format"]["size"]) / 1e6:.0f} МБ')


if __name__ == "__main__":
    main()
