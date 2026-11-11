"""Пути к файлам в текстах PULSE — кликабельные.

Claude в чате, разборе встреч, брифах и ходе задачи пишет, где лежит файл: `data/briefs/….md`,
«Работа/Презентация по итогам октября», /Users/…/Итоги октября — Коммерция.pptx — описание. Такой путь становится
кнопкой: щелчок — показать файл в Finder (на Windows — в Проводнике), значок рядом — скопировать полный путь.

Кнопкой становится только то, что есть на диске — в папке PULSE, в папках «Офиса» и «Документов» или в домашней
папке; `secrets/` — никогда. Путь проходим папка за папкой и сверяем с диском: так видно, где он кончается, даже
если в именах пробелы, а после пути идёт описание. Относительный путь ищем от папки PULSE (`data/…`), от рабочих
папок, от их родителя («Работа/…») и от их подпапок. Опечатку в одну-две буквы в имени (Claude пишет имена
по памяти) прощаем, если похожее имя в папке одно.
"""

from __future__ import annotations

import html
import os
import re
import subprocess
import sys
import time
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path

from markupsafe import Markup, escape

from opsdash import config

MAX_LEN = 400
_STOP = "\n`«»“”\"<>|*"  # где путь в тексте точно кончился
_NOT_PATH = r"\s<>\"'«»“”`|*"
_EDGE = r"(?<![\w/\\.~:\-])"  # перед путём — не буква, не точка и не часть адреса сайта
# начало абсолютного пути: /…, ~/…, C:\…
ABS_RE = re.compile(_EDGE + r"(~[/\\]|/(?=[^\s/])|[A-Za-z]:[/\\])")
# слово с «/» без пробелов: opsdash/web/app.py:120, отчёты/2026/итоги.xlsx
PLAIN_RE = re.compile(rf"{_EDGE}([^{_NOT_PATH}/\\:]+(?:[/\\][^{_NOT_PATH}/\\]+)+)")
# путь в кавычках или `коде` — целиком, может быть с пробелами
QUOTED_RE = re.compile(r"«([^«»\n]{3,400})»|“([^“”\n]{3,400})”|`([^`\n]{3,400})`")
SEP_RE = re.compile(r"[/\\]")
LINE_SUFFIX = re.compile(r":\d+(?::\d+)?$")  # app.py:120 — путь со строкой, как пишет Claude Code
TRAIL = ".,;:!?)]»”'"
LEAD = "(["
FUZZY_MIN, FUZZY_RATIO = 8, 0.9

# разметка: блоки кода и ссылки — целиком, остальные теги — по одному; между ними — текст
_HTML_RE = re.compile(r"(<pre\b.*?</pre>|<code\b[^>]*>.*?</code>|<a\b[^>]*>.*?</a>|<[^>]+>)", re.S | re.I)
_HREF_RE = re.compile(r'\bhref="([^"]*)"', re.I)

ICON_FOLDER = ('<svg class="fpath-i" viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" '
               'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
               '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>')
ICON_COPY = ('<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" '
             'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="9" y="9" width="11" '
             'height="11" rx="2"/><path d="M5 15V6a2 2 0 0 1 2-2h9"/></svg>')
# пакеты macOS — для Finder это папки, но `open` их запускает: такие только показываем в папке
_PACKAGES = {".app", ".bundle", ".framework", ".pkg", ".mpkg", ".plugin", ".kext", ".workflow", ".action", ".prefPane"}


def _fold(name: str) -> str:
    """Имя для сравнения: кириллица на Mac бывает в разной записи (NFC / NFD), регистр не важен."""
    return unicodedata.normalize("NFC", name).casefold()


def _key(path: Path | str) -> str:
    return os.path.normcase(unicodedata.normalize("NFC", str(path))).rstrip("/\\")


def _inside(path: Path, root: Path) -> bool:
    p, r = _key(path), _key(root)
    return p == r or p.startswith(r + os.sep)


_WS: dict = {"at": 0.0, "key": None, "value": None}


def _workspace() -> tuple[list[Path], dict[str, list[Path]], re.Pattern | None]:
    """Рабочие папки («Офис», «Документы») и начала относительных путей: имя → откуда идти. «Работа/…» — от
    самой рабочей папки, «Презентация…/…» — от её подпапки, «data/…» — от папки PULSE. Кэш — на 15 секунд."""
    try:  # настройки поменялись — читаем заново; иначе раз в 15 секунд (новые подпапки)
        st = config.SETTINGS_FILE.stat()
        key = (str(config.ROOT), st.st_mtime_ns, st.st_size)
    except OSError:
        key = (str(config.ROOT), None, None)
    if _WS["key"] == key and time.monotonic() - _WS["at"] < 15:
        return _WS["value"]
    s = config.load_settings()
    raw = [*((s.get("office") or {}).get("folders") or []), *((s.get("knowledge") or {}).get("folders") or [])]
    folders: list[Path] = []
    for f in raw:
        path = config.resolve_path(str(f)) if f else None
        if path and _key(path) not in {_key(p) for p in folders}:
            folders.append(path)
    anchors: dict[str, list[Path]] = {}

    def add(name: str, base: Path) -> None:
        if len(name) >= 3 and base not in anchors.setdefault(_fold(name), []):
            anchors[_fold(name)].append(base)

    add("data", config.ROOT / "data")
    for root in folders:
        add(root.name, root)
        try:
            subs = [p for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")][:300]
        except OSError:
            subs = []
        for p in subs:
            add(unicodedata.normalize("NFC", p.name), p)
    names = sorted(anchors, key=len, reverse=True)
    pattern = re.compile(_EDGE + "(" + "|".join(re.escape(n) for n in names) + r")[/\\]", re.I) if names else None
    _WS.update(at=time.monotonic(), key=key, value=(folders, anchors, pattern))
    return _WS["value"]


def _both(path: Path) -> list[Path]:
    """Путь как записан и как есть на диске: /var/… на Mac — это /private/var/…."""
    try:
        real = path.resolve()
    except (OSError, ValueError):
        return [path]
    return [path] if real == path else [path, real]


class Resolver:
    """Ищет пути на диске. Один на текст: рабочие папки и списки файлов в папках читаются один раз."""

    def __init__(self):
        self.folders, self.anchors, self.anchor_re = _workspace()
        self._dirs: dict[str, list[tuple[str, str, bool]]] = {}
        self._roots: list[Path] | None = None

    def allowed(self, path: Path) -> bool:
        """Можно показывать: папка PULSE (кроме secrets/), рабочие папки, домашняя папка."""
        if self._roots is None:
            self._roots = [r for root in (config.ROOT, Path.home(), *self.folders) for r in _both(root)]
        if any(_inside(path, s) for s in _both(config.SECRETS_DIR)):
            return False
        return any(_inside(path, root) for root in self._roots)

    def _ok(self, path: Path) -> Path | None:
        try:
            real = path.resolve()
        except (OSError, ValueError):
            return None
        return real if self.allowed(real) else None

    # --- шаг по пути -----------------------------------------------------------------------------------

    def _entries(self, folder: Path) -> list[tuple[str, str, bool]]:
        k = str(folder)
        if k not in self._dirs:
            try:
                with os.scandir(folder) as it:
                    self._dirs[k] = [(_fold(e.name), e.name, e.is_dir()) for _, e in zip(range(2000), it)]
            except OSError:
                self._dirs[k] = []
        return self._dirs[k]

    def _child(self, cur: Path, name: str, dirs_only: bool, fuzzy: bool) -> tuple[Path, bool] | None:
        """Папка или файл name в cur: точно, а если нет — единственное очень похожее имя (опечатка)."""
        if not name or name in (".", "..") and not dirs_only:
            return None
        p = cur / name
        try:
            if p.is_dir() if dirs_only else p.exists():
                return p, False
        except (OSError, ValueError):
            return None
        if not fuzzy or len(name) < FUZZY_MIN:
            return None
        key = _fold(name)
        scored = sorted(((SequenceMatcher(None, key, k).ratio(), real) for k, real, is_dir in self._entries(cur)
                         if (is_dir or not dirs_only) and abs(len(k) - len(key)) <= max(2, len(key) // 10)),
                        reverse=True)
        if scored and scored[0][0] >= FUZZY_RATIO and (len(scored) == 1 or scored[1][0] < scored[0][0] - 0.02):
            return cur / scored[0][1], True
        return None

    def _walk(self, base: Path, text: str, pos: int, whole: bool) -> tuple[int, Path] | None:
        """Пройти путь в text с позиции pos от папки base. whole — путь занимает весь текст (`код`, «кавычки»);
        иначе после пути может идти что угодно («….pptx — текущая версия деки»). → (где путь кончился, путь)."""
        end = len(text)
        if not whole:
            stops = [i for i in (text.find(ch, pos) for ch in _STOP) if i >= 0]
            end = min([*stops, pos + MAX_LEN, end])
        cur, typos = base, 2
        while True:
            m = SEP_RE.search(text, pos, end)
            if m:  # «папка/» — есть такая папка? идём дальше
                got = self._child(cur, text[pos:m.start()], True, typos > 0)
                if got:
                    cur, pos, typos = got[0], m.end(), typos - got[1]
                    continue
            seg = text[pos:m.start() if m else end]
            if not seg.strip() and cur != base:  # путь кончился на «/» — это папка
                return (pos, cur) if not m else None
            for fuzzy in (False, True):  # последнее имя: сначала точно, потом с опечаткой
                if fuzzy and typos <= 0:
                    break
                for cut, name in self._names(seg, whole):
                    got = self._child(cur, name, False, fuzzy)
                    if got:
                        return pos + cut, got[0]
            return None

    @staticmethod
    def _names(seg: str, whole: bool) -> list[tuple[int, str]]:
        """Каким может быть последнее имя: весь кусок, а если после пути идёт текст — его начала до пробела,
        от длинного к короткому (без точки и запятой в конце). → (сколько символов взять, имя)."""
        ends = [len(seg)] if whole else [len(seg)] + [i for i in range(len(seg) - 1, 0, -1) if seg[i].isspace()][:40]
        out: dict[str, int] = {}
        for e in ends:
            name = seg[:e].rstrip()
            if not whole:
                while name and name[-1] in TRAIL and not (name[-1] == ")" and name.count("(") >= name.count(")")):
                    name = name[:-1]
            for n in (name, LINE_SUFFIX.sub("", name)):
                if n and n not in out:
                    out[n] = len(name)
        return [(cut, n) for n, cut in out.items()]

    # --- целиком и в тексте ----------------------------------------------------------------------------

    def _bases(self, text: str) -> list[tuple[Path, int]]:
        """Откуда идти по пути из текста: (папка, с какого символа текста)."""
        m = ABS_RE.match(text)
        if m:
            start = m.group(1)
            base = Path.home() if start[0] == "~" else Path(start if start != "/" else "/")
            return [(base, m.end() if start != "/" else 1)]
        if text.startswith(("/", "\\")):
            return []
        skip = 2 if text.startswith(("./", ".\\")) else 0
        out = [(config.ROOT, skip), *((f, skip) for f in self.folders)]
        m = self.anchor_re.match(text, skip) if self.anchor_re else None
        if m:
            out[:0] = [(b, m.end()) for b in self.anchors.get(_fold(m.group(1)), [])]
        return out

    def resolve(self, text: str) -> Path | None:
        """Путь целиком (из `кода`, «кавычек», ссылки) → файл или папка на диске; None — нет или нельзя."""
        raw = (text or "").strip()
        if raw.startswith("file://"):
            raw = raw[7:]
        if not raw or len(raw) > MAX_LEN or "://" in raw or "\n" in raw or not SEP_RE.search(raw):
            return None
        raw = os.path.expanduser(raw) if raw.startswith("~") else raw
        for base, pos in self._bases(raw):
            got = self._walk(base, raw, pos, whole=True)
            if got and not raw[got[0]:].strip("/\\ ") and (path := self._ok(got[1])):
                return path
        return None

    def scan(self, text: str, quoted: bool = True) -> list[tuple[int, int, Path, bool]]:
        """Все пути в простом тексте → [(начало, конец, путь, моноширинно)], без пересечений."""
        spans: list[tuple[int, int, Path, bool]] = []

        def free(a: int, b: int) -> bool:
            return all(b <= s or a >= e for s, e, *_ in spans)

        if quoted:
            for m in QUOTED_RE.finditer(text):
                g = next(i for i in (1, 2, 3) if m.group(i) is not None)
                path = self.resolve(m.group(g))
                if path:  # кавычки «» остаются текстом, `обратные` — уходят
                    a, b = (m.start(), m.end()) if g == 3 else (m.start(g), m.end(g))
                    spans.append((a, b, path, g == 3))
        starts: list[tuple[int, list[tuple[Path, int]]]] = []
        for m in ABS_RE.finditer(text):
            s = m.group(1)
            starts.append((m.start(), [(Path.home() if s[0] == "~" else Path(s if s != "/" else "/"),
                                        m.end() if s != "/" else m.start() + 1)]))
        if self.anchor_re:
            for m in self.anchor_re.finditer(text):
                starts.append((m.start(), [(b, m.end()) for b in self.anchors.get(_fold(m.group(1)), [])]))
        for start, bases in sorted(starts, key=lambda x: x[0]):
            if not free(start, start + 1):
                continue
            for base, pos in bases:
                got = self._walk(base, text, pos, whole=False)
                if got and free(start, got[0]) and (path := self._ok(got[1])):
                    spans.append((start, got[0], path, False))
                    break
        for m in PLAIN_RE.finditer(text):
            token, a = m.group(1), m.start(1)
            while token and token[0] in LEAD:  # «(data/x.md)» — скобка не часть пути
                token, a = token[1:], a + 1
            while token and token[-1] in TRAIL and not (token[-1] == ")" and token.count("(") >= token.count(")")):
                token = token[:-1]
            if token and free(a, a + len(token)) and (path := self.resolve(token)):
                spans.append((a, a + len(token), path, False))
        return sorted(spans, key=lambda x: x[0])


def button(display: str, path: Path, mono: bool = False) -> str:
    """Кнопка пути: щелчок — показать в Finder / Проводнике, значок справа — скопировать полный путь. Сам путь —
    ссылкой, а не <button>: кнопка не переносится по строкам, и длинный путь выпадал бы из абзаца целиком."""
    full = str(path)
    where = "в Finder" if sys.platform == "darwin" else "в Проводнике" if os.name == "nt" else "в папке"
    action = ("Открыть папку " if path.is_dir() else "Показать ") + where
    return (f'<span class="fpath{" mono" if mono else ""}" data-fpath="{escape(full)}">'
            f'<a href="#" role="button" class="fpath-go" title="{escape(action)}\n{escape(full)}">{ICON_FOLDER}'
            f'<span class="fpath-t">{escape(display)}</span></a>'
            f'<button type="button" class="fpath-copy" title="Скопировать полный путь" '
            f'aria-label="Скопировать полный путь">{ICON_COPY}</button></span>')


def _plain(value: str, r: Resolver) -> str | None:
    """Текст → безопасный HTML с кнопками путей; None — путей нет (текст можно не трогать)."""
    spans = r.scan(value)
    if not spans:
        return None
    out, pos = [], 0
    for a, b, path, mono in spans:
        shown = value[a:b]
        if mono and shown.startswith("`"):
            shown = shown.strip("`")
        out += [str(escape(value[pos:a])), button(shown, path, mono)]
        pos = b
    out.append(str(escape(value[pos:])))
    return "".join(out)


def text(value: str | None) -> Markup:
    """Простой текст (ход задачи, «Сейчас», суть) → HTML: пути — кнопками, остальное экранировано."""
    value = value or ""
    if "/" not in value and "\\" not in value:
        return Markup(escape(value))
    out = _plain(value, Resolver())
    return Markup(out if out is not None else escape(value))


def html_paths(markup: str) -> str:
    """Готовый HTML (ответ Claude, разбор встречи, бриф) → тот же HTML, где пути в `коде`, «кавычках», ссылках
    и просто в тексте — кнопки. Блоки кода ``` и ссылки на сайты не трогаем."""
    if "/" not in markup and "\\" not in markup:
        return markup
    r: Resolver | None = None
    parts = _HTML_RE.split(markup)
    for i, part in enumerate(parts):
        if not part or ("/" not in part and "\\" not in part):
            continue
        r = r or Resolver()
        if i % 2 == 0:  # текст между тегами
            done = _plain(html.unescape(part), r)
            if done is not None:
                parts[i] = done
            continue
        low = part[:6].lower()
        if low.startswith("<code") and part.endswith("</code>"):
            inner = re.sub(r"^<code[^>]*>|</code>$", "", part)
            if "<" not in inner:
                shown = html.unescape(inner)
                path = r.resolve(shown)
                if path:
                    parts[i] = button(shown, path, mono=True)
        elif low.startswith("<a") and part.lower().endswith("</a>"):
            head = part[:part.index(">") + 1]
            m = _HREF_RE.search(head)
            href = html.unescape(m.group(1)) if m else ""
            if href and not href.startswith(("http:", "https:", "mailto:", "tg:", "#", "?")):
                path = r.resolve(href)
                if path:
                    label = html.unescape(re.sub(r"<[^>]+>", "", part[len(head):-4])) or href
                    parts[i] = button(label, path)
    return "".join(parts)


def reveal(path: Path) -> None:
    """Показать на этом компьютере: файл — выделенным в его папке, папку — открытой. Только показывает, не запускает."""
    p = str(path)
    folder = path.is_dir() and path.suffix not in _PACKAGES
    if sys.platform == "darwin":
        subprocess.run(["open", p] if folder else ["open", "-R", p], check=False, timeout=15)
    elif os.name == "nt":
        subprocess.run(["explorer", p] if folder else ["explorer", "/select,", p], check=False, timeout=15)
    else:
        subprocess.run(["xdg-open", p if folder else str(path.parent)], check=False, timeout=15)
