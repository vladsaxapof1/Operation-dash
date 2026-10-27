"""Рабочие документы директора: поиск по папкам из настроек (knowledge.folders).

PULSE только читает файлы: вынимает текст из md/txt/csv/html/docx/pptx/xlsx/pdf и кладёт его
в поисковый индекс SQLite (FTS5) — по слайдам, листам и страницам. Claude ищет командой
`./ops docs search`, читает — `./ops docs show`: так ему доступны и презентации, и таблицы,
которые напрямую он не открывает. Повторные версии файлов («Отчёт (1).xlsx») не индексируются —
берётся самая свежая.
"""

from __future__ import annotations

import os
import re
import unicodedata
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from xml.etree import ElementTree

from opsdash import config, store

TEXT = {".md", ".txt", ".csv"}
HTML = {".html", ".htm"}
EXTS = TEXT | HTML | {".docx", ".pptx", ".xlsx", ".pdf"}
TYPE_TITLES = {".md": "Markdown", ".txt": "Текст", ".csv": "CSV", ".html": "HTML", ".htm": "HTML", ".docx": "Word",
               ".pptx": "Презентация", ".xlsx": "Excel", ".pdf": "PDF"}
MAX_FILE_MB = 40         # большие файлы — обычно выгрузки данных, не документы
MAX_CHARS = 200_000      # больше из одного файла не берём
DATA_CHARS = 40_000      # из таблиц и CSV — меньше: в выгрузках важны заголовки и первые строки, не все ячейки
CHUNK = 3000             # длинный текст режем на фрагменты: поиск показывает точное место
XLSX_ROWS = 150          # строк с листа: дальше в выгрузках обычно идут данные
XLSX_SCAN = 5000         # строк просматриваем на листе, даже пустых: бывает, оформление протянуто на миллион строк
XLSX_CELLS = 6000        # ячеек на файл — примерно DATA_CHARS; в книгах бывает по 200 листов
VERSION_RE = re.compile(r"\s*\(\d+\)$")  # «Отчёт (2)» — копия «Отчёт»

NS = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "rel": "http://schemas.openxmlformats.org/package/2006/relationships",
}


class DocsError(Exception):
    pass


def nfc(s: str) -> str:
    """macOS хранит имена файлов в разложенном виде (й = и + ˘) — сравниваем в одном."""
    return unicodedata.normalize("NFC", s)


# --- текст из файлов --------------------------------------------------------------

def _read_text(path: Path) -> str:
    data = path.read_bytes()
    for enc in ("utf-8-sig", "cp1251"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _docx(path: Path) -> list[tuple[str, str]]:
    from opsdash import transcribe

    return [("", transcribe.docx_text(path))]


def _pptx(path: Path) -> list[tuple[str, str]]:
    def number(name: str) -> int:
        m = re.search(r"(\d+)\.xml$", name)
        return int(m.group(1)) if m else 0

    out = []
    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())
        for name in sorted((n for n in names if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)), key=number):
            n = number(name)
            texts = [_texts(z.read(name), "a")]
            notes = f"ppt/notesSlides/notesSlide{n}.xml"
            if notes in names:
                note = _texts(z.read(notes), "a")
                if note.strip():
                    texts.append("Заметки: " + note)
            text = "\n".join(t for t in texts if t.strip())
            if text.strip():
                out.append((f"слайд {n}", text))
    return out


def _texts(xml: bytes, prefix: str) -> str:
    root = ElementTree.fromstring(xml)
    paras = []
    for p in root.iter(f"{{{NS[prefix]}}}p"):
        t = "".join(x.text or "" for x in p.iter(f"{{{NS[prefix]}}}t"))
        if t.strip():
            paras.append(t.strip())
    return "\n".join(paras)


def _xlsx(path: Path) -> list[tuple[str, str]]:
    s = f"{{{NS['s']}}}"
    sheets = []  # (имя листа, строки: ячейки — ("s", номер общей строки) или ("v", текст))
    cells_total = 0
    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())
        rels = {}
        if "xl/_rels/workbook.xml.rels" in names:
            for r in ElementTree.fromstring(z.read("xl/_rels/workbook.xml.rels")).iter(f"{{{NS['rel']}}}Relationship"):
                rels[r.get("Id")] = r.get("Target", "").lstrip("/").removeprefix("xl/")
        for sheet in ElementTree.fromstring(z.read("xl/workbook.xml")).iter(f"{s}sheet"):
            if cells_total >= XLSX_CELLS:
                break
            name = "xl/" + rels.get(sheet.get(f"{{{NS['r']}}}id"), "")
            if name not in names:
                continue
            rows, cut, scanned = [], False, 0
            with z.open(name) as fh:  # потоком: огромный лист не разбираем целиком ради первых строк
                for _, row in ElementTree.iterparse(fh):
                    if row.tag != f"{s}row":
                        continue
                    scanned += 1
                    cells = []
                    for c in row.iter(f"{s}c"):
                        v = c.find(f"{s}v")
                        if c.get("t") == "s" and v is not None and (v.text or "").isdigit():
                            cells.append(("s", int(v.text)))
                        elif c.get("t") == "inlineStr":
                            cells.append(("v", "".join(t.text or "" for t in c.iter(f"{s}t"))))
                        elif v is not None and v.text:
                            cells.append(("v", v.text))
                    row.clear()
                    if cells:
                        rows.append(cells)
                        cells_total += len(cells)
                    if len(rows) >= XLSX_ROWS or scanned >= XLSX_SCAN or cells_total >= XLSX_CELLS:
                        cut = True
                        break
            sheets.append((sheet.get("name"), rows, cut))
        # из общего словаря строк берём только нужные — в больших выгрузках он весит десятки мегабайт
        needed = {v for _, rows, _ in sheets for cells in rows for kind, v in cells if kind == "s"}
        shared: dict[int, str] = {}
        if needed and "xl/sharedStrings.xml" in names:
            top = max(needed)
            with z.open("xl/sharedStrings.xml") as fh:
                i = 0
                for _, el in ElementTree.iterparse(fh):
                    if el.tag != f"{s}si":
                        continue
                    if i in needed:
                        shared[i] = "".join(t.text or "" for t in el.iter(f"{s}t"))
                    el.clear()
                    i += 1
                    if i > top:
                        break
    out = []
    for name, rows, cut in sheets:
        lines = []
        for cells in rows:
            texts = [(shared.get(v, "") if kind == "s" else v).strip() for kind, v in cells]
            if any(texts):
                lines.append("\t".join(texts))
        if cut:
            lines.append(f"… (дальше строк не берём — первые {XLSX_ROWS})")
        if lines:
            out.append((f"лист «{name}»", "\n".join(lines)))
    return out


def _pdf(path: Path) -> list[tuple[str, str]]:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    out, total = [], 0
    for i, page in enumerate(reader.pages, 1):
        text = (page.extract_text() or "").strip()
        if text:
            out.append((f"стр. {i}", text))
            total += len(text)
        if total >= MAX_CHARS:
            break
    return out


def extract(path: Path) -> list[tuple[str, str]]:
    """Текст файла по частям: [(«слайд 3», текст), …]. Длинные части режутся на фрагменты."""
    from opsdash.sources import mail

    ext = path.suffix.lower()
    if ext in TEXT:
        parts = [("", _read_text(path))]
    elif ext in HTML:
        parts = [("", mail.html_to_text(_read_text(path)))]
    elif ext == ".docx":
        parts = _docx(path)
    elif ext == ".pptx":
        parts = _pptx(path)
    elif ext == ".xlsx":
        parts = _xlsx(path)
    elif ext == ".pdf":
        parts = _pdf(path)
    else:
        raise DocsError(f"Формат {ext} не поддерживается")
    limit = DATA_CHARS if ext in (".xlsx", ".csv") else MAX_CHARS
    out, total = [], 0
    for label, text in parts:
        text = re.sub(r"[ \xa0]+", " ", text or "").strip()  # табуляцию оставляем: по ней видны колонки таблиц
        if not text:
            continue
        pieces = [text[i:i + CHUNK] for i in range(0, len(text), CHUNK)]
        for k, piece in enumerate(pieces, 1):
            out.append((", ".join(x for x in (label, f"фрагмент {k}" if len(pieces) > 1 else "") if x), piece))
            total += len(piece)
            if total >= limit:
                return out
    return out


def title_of(path: Path, parts: list[tuple[str, str]]) -> str:
    if path.suffix.lower() == ".md" and parts:
        m = re.search(r"^#\s+(.+)$", parts[0][1], re.M)
        if m:
            return m.group(1).strip()
    return path.stem


# --- обход папок и индекс ----------------------------------------------------------

def folders(settings: dict | None = None) -> list[Path]:
    settings = settings or config.load_settings()
    return [config.resolve_path(f) for f in settings["knowledge"].get("folders") or []]


def scan(root: Path, exclude: list[str]) -> list[Path]:
    """Документы папки: без служебных папок, слишком больших файлов и повторных версий."""
    skip = {nfc(x).lower() for x in exclude}
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and nfc(d).lower() not in skip]
        for name in filenames:
            p = Path(dirpath) / name
            if name.startswith(("~$", ".")) or p.suffix.lower() not in EXTS:
                continue
            try:
                if p.stat().st_size > MAX_FILE_MB * 1024 * 1024:
                    continue
            except OSError:
                continue
            found.append(p)
    # «Отчёт.xlsx», «Отчёт (1).xlsx», «Отчёт (2).xlsx» — одна вещь в разных версиях: берём самую свежую
    groups: dict[tuple, list[Path]] = {}
    for p in found:
        key = (str(p.parent), nfc(VERSION_RE.sub("", p.stem)).lower(), p.suffix.lower())
        groups.setdefault(key, []).append(p)
    return [max(g, key=lambda p: p.stat().st_mtime) for g in groups.values()]


def index(settings: dict | None = None) -> str:
    """Обновить индекс: новые и изменённые файлы — прочитать, удалённые — убрать."""
    settings = settings or config.load_settings()
    roots = folders(settings)
    if not roots:
        raise DocsError("Не выбраны папки с документами — Настройки → Документы")
    exclude = settings["knowledge"].get("exclude") or []
    known = {r["path"]: r for r in store.rows("SELECT path, mtime, size, status FROM docs")}
    seen, changed, errors = set(), 0, 0
    for root in roots:
        if not root.exists():
            raise DocsError(f"Папка не найдена: {root}")
        try:
            files = scan(root, exclude)
        except PermissionError as e:
            raise DocsError(f"Нет доступа к папке {root} — разрешите PULSE доступ к файлам в настройках macOS") from e
        for p in files:
            path = str(p)
            seen.add(path)
            st = p.stat()
            old = known.get(path)
            if old and old["mtime"] == st.st_mtime and old["size"] == st.st_size:
                continue
            changed += 1
            try:
                parts = extract(p)
                status, error = "indexed", None
            except Exception as e:  # noqa: BLE001 — один битый файл не должен останавливать индекс
                parts, status, error = [], "error", str(e)[:300]
                errors += 1
            with store.db() as conn:
                conn.execute("DELETE FROM docs_fts WHERE path=?", (path,))
                for label, text in parts:
                    conn.execute("INSERT INTO docs_fts(path, part, title, body) VALUES(?,?,?,?)",
                                 (path, label, title_of(p, parts), text))
                conn.execute(
                    """INSERT INTO docs(path, folder, rel, ext, title, size, mtime, status, error, parts, chars, indexed_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(path) DO UPDATE SET folder=excluded.folder, rel=excluded.rel, ext=excluded.ext,
                         title=excluded.title, size=excluded.size, mtime=excluded.mtime, status=excluded.status,
                         error=excluded.error, parts=excluded.parts, chars=excluded.chars, indexed_at=excluded.indexed_at""",
                    (path, str(root), nfc(str(p.relative_to(root))), p.suffix.lower(), title_of(p, parts),
                     st.st_size, st.st_mtime, status, error, len(parts), sum(len(t) for _, t in parts), store.utcnow()))
    gone = [p for p in known if p not in seen]
    with store.db() as conn:
        for path in gone:
            conn.execute("DELETE FROM docs_fts WHERE path=?", (path,))
            conn.execute("DELETE FROM docs WHERE path=?", (path,))
    total = store.scalar("SELECT COUNT(*) FROM docs WHERE status='indexed'") or 0
    summary = f"документов: {total}; обновлено: {changed}, удалено: {len(gone)}, не прочитано: {errors}"
    if changed:  # заметки-решения (md с `type: решение`) — сразу в антологию
        from opsdash import anthology

        summary += "; " + anthology.import_notes()
    return summary


# --- поиск -------------------------------------------------------------------------

# окончания русских слов: «пролонгации» ищем как «пролонгаци*», «бюджета» — как «бюджет*»
ENDINGS = sorted(["ами", "ями", "ого", "его", "ому", "ему", "ыми", "ими", "ией", "иях", "иям", "ия", "ие", "ий", "ой",
                  "ей", "ый", "ая", "яя", "ое", "ее", "ые", "ую", "юю", "ах", "ях", "ам", "ям", "ом", "ем", "ов", "ев",
                  "ть", "ет", "ют", "ит", "ат", "ят", "а", "я", "о", "е", "ы", "и", "у", "ю", "ь", "й"],
                 key=len, reverse=True)


# предлоги и союзы: с ними «решения по автоворонке» требовало бы слово «по» в документе
STOPWORDS = {"и", "в", "во", "на", "по", "с", "со", "к", "ко", "о", "об", "от", "до", "за", "для", "из", "у", "не",
             "ни", "но", "а", "что", "как", "это", "или", "же", "ли", "бы", "то", "при", "про", "под", "над"}


def _stem(word: str) -> str:
    if re.search("[а-яё]", word):
        for e in ENDINGS:
            if word.endswith(e) and len(word) - len(e) >= 4:
                return word[: -len(e)]
    return word


def fts_query(text: str, mode: str = "AND") -> str:
    words = [w for w in re.findall(r"\w+", nfc(text).lower()) if len(w) >= 2 and w not in STOPWORDS]
    terms = [f'"{_stem(w)}"*' if len(w) >= 3 else f'"{w}"' for w in words]
    return f" {mode} ".join(terms)


def search(text: str, limit: int = 10, ext: str | None = None) -> list[dict]:
    """Лучшие документы по запросу: файл, место (слайд/лист/страница), отрывок с ⟦совпадениями⟧."""
    out = []
    for mode in ("AND", "OR"):  # сначала все слова сразу, иначе — хотя бы одно
        query = fts_query(text, mode)
        if not query:
            return []
        rows = store.rows(
            f"""SELECT f.rowid AS rid, f.path, f.part, snippet(docs_fts, 3, '⟦', '⟧', ' … ', 18) AS snippet,
                      bm25(docs_fts, 0, 0, 6.0, 1.0) AS rank, d.rel, d.title, d.ext, d.mtime, d.folder, d.size
               FROM docs_fts f JOIN docs d ON d.path = f.path
               WHERE docs_fts MATCH ? {_ext_sql(ext)}
               ORDER BY rank LIMIT 200""",
            (query, *_exts(ext)))
        files: dict[str, dict] = {}
        copies = set()  # один и тот же файл, скопированный в разные папки, показываем один раз
        for r in rows:
            f = files.get(r["path"])
            if f is None:
                if (r["title"], r["part"], r["snippet"]) in copies:
                    continue
                copies.add((r["title"], r["part"], r["snippet"]))
                files[r["path"]] = {**r, "more": []}
            elif len(f["more"]) < 5 and r["part"]:
                f["more"].append(r["part"])
        out = list(files.values())[:limit]
        if out or " " not in text.strip():
            break
    return [_dated(f) for f in out]


def _exts(ext: str | None) -> list[str]:
    """Расширения одного типа: «HTML» — это и .html, и .htm."""
    return [] if not ext else [".html", ".htm"] if ext in HTML else [ext]


def _ext_sql(ext: str | None) -> str:
    return f"AND d.ext IN ({','.join('?' * len(_exts(ext)))})" if ext else ""


def _dated(d: dict) -> dict:
    """Даты и тип для показа. Дату создания берём у файла: в индексе её нет, а файлы и так рядом."""
    d["modified"] = datetime.fromtimestamp(d["mtime"], tz=timezone.utc)
    d["type"] = TYPE_TITLES.get(d["ext"], d["ext"])
    try:
        st = os.stat(d["path"])
        born = getattr(st, "st_birthtime", None) or (st.st_ctime if os.name == "nt" else None)
    except OSError:
        born = None
    d["created"] = datetime.fromtimestamp(born, tz=timezone.utc) if born else None
    return d


def listing(ext: str, limit: int = 100) -> list[dict]:
    """Документы одного типа — свежие сверху (фильтр по типу без поискового запроса)."""
    rows = store.rows(f"SELECT * FROM docs d WHERE status='indexed' {_ext_sql(ext)} ORDER BY mtime DESC LIMIT ?",
                      (*_exts(ext), limit))
    return [_dated(d) for d in rows]


def type_counts() -> dict[str, int]:
    """Сколько документов каждого типа в индексе: {".pptx": 12, …}; .htm считаем вместе с .html."""
    out: dict[str, int] = {}
    for r in store.rows("SELECT ext, COUNT(*) AS n FROM docs WHERE status='indexed' GROUP BY ext"):
        key = ".html" if r["ext"] in HTML else r["ext"]
        out[key] = out.get(key, 0) + r["n"]
    return out


def find(ref: str) -> dict | None:
    """Документ по полному пути, пути внутри папки или имени файла (если оно одно такое)."""
    ref = nfc(ref.strip().strip('"'))
    rows = store.rows("SELECT * FROM docs")
    hit = next((r for r in rows if nfc(r["path"]) == ref or r["rel"] == ref), None)
    if hit is None:
        by_name = [r for r in rows if nfc(Path(r["path"]).name) == Path(ref).name or r["title"] == ref]
        hit = by_name[0] if len(by_name) == 1 else None
    return _dated(hit) if hit else None


def parts(path: str) -> list[dict]:
    return store.rows("SELECT rowid AS rid, part, body FROM docs_fts WHERE path=? ORDER BY rowid", (path,))


def recent(days: int = 7, limit: int = 50) -> list[dict]:
    since = (datetime.now(timezone.utc) - timedelta(days=days)).timestamp()
    items = store.rows("SELECT * FROM docs WHERE status='indexed' AND mtime >= ? ORDER BY mtime DESC LIMIT ?",
                       (since, limit))
    return [_dated(d) for d in items]


def stats() -> dict:
    by_status = {r["status"]: r["n"] for r in store.rows("SELECT status, COUNT(*) AS n FROM docs GROUP BY status")}
    by_type: dict[str, int] = {}  # .html и .htm — один тип «HTML»
    for r in store.rows("SELECT ext, COUNT(*) AS n FROM docs WHERE status='indexed' GROUP BY ext"):
        title = TYPE_TITLES.get(r["ext"], r["ext"])
        by_type[title] = by_type.get(title, 0) + r["n"]
    job = store.row("SELECT last_ok, last_error, last_run FROM jobs WHERE name='docs-index'") or {}
    return {"indexed": by_status.get("indexed", 0), "errors": by_status.get("error", 0),
            "types": sorted(by_type.items(), key=lambda t: -t[1]), **job}
