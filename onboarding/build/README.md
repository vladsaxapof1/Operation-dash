# Как пересобрать видео-онбординг

Видео собирается из демо-стенда — отдельной папки данных с вымышленным директором «Анна Смирнова».
Ваши встречи, переписка и база не используются. Пути и почта того, кто собирает видео, в кадр не попадают.

Нужны macOS (голос «Milena» для озвучки), ffmpeg, Google Chrome и Playwright для Python
(`python3 -m pip install playwright`).

Из папки PULSE:

```bash
.venv/bin/python onboarding/build/seed_demo.py
```

```bash
OPSDASH_HOME=onboarding/build/.demo-home .venv/bin/python -m uvicorn opsdash.web.app:app --port 8790
```

Во втором окне Терминала, пока работает стенд:

```bash
python3 onboarding/build/render.py
```

```bash
python3 onboarding/build/video.py
```

- `storyboard.py` — сцены: что на экране, что выделено рамкой и закадровый текст. Поменяли текст —
  пересоберите `video.py`: заново озвучатся только изменённые сцены.
- `render.py` — кадры в `frames/`; можно перерисовать только нужные: `python3 render.py home-tiles board`.
- `video.py` — озвучка, переходы, главы, субтитры; результат — в папке `onboarding`.

Чтобы переозвучить живым голосом, возьмите текст из `PULSE — сценарий.md` или `PULSE — субтитры.srt`.
