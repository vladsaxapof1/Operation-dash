import os
import time
import unicodedata
import zipfile
from pathlib import Path

from fastapi.testclient import TestClient

from opsdash import cli, docs, store

W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
A = ('xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
     'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"')
S = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
R = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'


def _zip(path: Path, files: dict[str, str]) -> Path:
    with zipfile.ZipFile(path, "w") as z:
        for name, body in files.items():
            z.writestr(name, body)
    return path


def make_docx(path, *paras):
    body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paras)
    return _zip(path, {"word/document.xml": f"<w:document {W}><w:body>{body}</w:body></w:document>"})


def make_pptx(path, slides, notes=None):
    files = {}
    for i, text in enumerate(slides, 1):
        files[f"ppt/slides/slide{i}.xml"] = f"<p:sld {A}><a:p><a:r><a:t>{text}</a:t></a:r></a:p></p:sld>"
    for i, text in (notes or {}).items():
        files[f"ppt/notesSlides/notesSlide{i}.xml"] = f"<p:notes {A}><a:p><a:r><a:t>{text}</a:t></a:r></a:p></p:notes>"
    return _zip(path, files)


def make_xlsx(path, sheet_name, rows):
    shared, xml_rows = [], []
    for r, row in enumerate(rows, 1):
        cells = []
        for value in row:
            if isinstance(value, str):
                shared.append(value)
                cells.append(f'<c t="s"><v>{len(shared) - 1}</v></c>')
            else:
                cells.append(f"<c><v>{value}</v></c>")
        xml_rows.append(f'<row r="{r}">{"".join(cells)}</row>')
    return _zip(path, {
        "xl/workbook.xml": f'<workbook {S} {R}><sheets><sheet name="{sheet_name}" sheetId="1" r:id="rId1"/></sheets></workbook>',
        "xl/_rels/workbook.xml.rels": '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                                      '<Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>',
        "xl/sharedStrings.xml": f'<sst {S}>{"".join(f"<si><t>{s}</t></si>" for s in shared)}</sst>',
        "xl/worksheets/sheet1.xml": f'<worksheet {S}><sheetData>{"".join(xml_rows)}</sheetData></worksheet>',
    })


def make_pdf(path, text):
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
            b"<< /Length %d >>stream\n" % len(stream) + stream + b"\nendstream",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out, offsets = b"%PDF-1.4\n", []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1) + b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    path.write_bytes(out)
    return path


def library(root: Path) -> Path:
    (root / "QBR").mkdir(parents=True)
    make_pptx(root / "QBR" / "QBR Q3 2026 — Коммерция.pptx",
              ["Итоги квартала", "Конверсия пролонгации выросла до 41%"], notes={2: "Сказать про автоворонку"})
    make_xlsx(root / "Лиды и MAU 2027 — прогноз.xlsx", "План", [["Канал", "Лиды"], ["Восток", 120], ["Запад", 80]])
    old = make_xlsx(root / "Лиды и MAU 2027 — прогноз (1).xlsx", "План", [["Устаревшая версия", 1]])
    os.utime(old, (time.time() - 3600, time.time() - 3600))  # копия старее основной
    make_docx(root / "Краткая справка по МСБ.docx", "Прогноз бюджета на 2027 год", "Риски: снижение маржи")
    make_pdf(root / "Budget memo.pdf", "Budget forecast approved for 2027")
    (root / "Решения.md").write_text("# Журнал решений\n\nРешили запускать автоворонку ЛК 27.10", encoding="utf-8")
    (root / "wins.html").write_text("<h1>Удачи</h1><p>Сработала рассылка по триалам</p>", encoding="utf-8")
    for skipped in (root / ".git" / "x.md", root / "node_modules" / "y.md", root / "_staging" / "z.md"):
        skipped.parent.mkdir(parents=True, exist_ok=True)
        skipped.write_text("автоворонка", encoding="utf-8")
    (root / "~$Краткая справка по МСБ.docx").write_text("lock", encoding="utf-8")
    return root


def test_extract_every_format(tmp_path):
    root = library(tmp_path / "Компания")
    slides = docs.extract(root / "QBR" / "QBR Q3 2026 — Коммерция.pptx")
    assert [label for label, _ in slides] == ["слайд 1", "слайд 2"] and "Заметки: Сказать про автоворонку" in slides[1][1]
    sheet = docs.extract(root / "Лиды и MAU 2027 — прогноз.xlsx")
    assert sheet == [("лист «План»", "Канал\tЛиды\nВосток\t120\nЗапад\t80")]
    assert "Прогноз бюджета" in docs.extract(root / "Краткая справка по МСБ.docx")[0][1]
    assert docs.extract(root / "Budget memo.pdf")[0] == ("стр. 1", "Budget forecast approved for 2027")
    assert "Сработала рассылка" in docs.extract(root / "wins.html")[0][1]
    long = root / "long.txt"
    long.write_text("слово " * 2000, encoding="utf-8")
    assert [label for label, _ in docs.extract(long)][:2] == ["фрагмент 1", "фрагмент 2"]


def test_index_search_and_updates(tmp_path, settings_with):
    root = library(tmp_path / "Компания")
    settings_with(knowledge={"folders": [str(root)], "exclude": ["node_modules", "_staging"]})
    assert docs.index().startswith("документов: 6; обновлено: 6")
    names = {Path(r["path"]).name for r in store.rows("SELECT path FROM docs")}
    assert "Лиды и MAU 2027 — прогноз (1).xlsx" not in names and "x.md" not in names and "y.md" not in names

    hits = docs.search("пролонгации")  # окончание не мешает
    assert hits[0]["title"] == "QBR Q3 2026 — Коммерция" and hits[0]["part"] == "слайд 2"
    assert "⟦пролонгации⟧" in hits[0]["snippet"]
    assert docs.search("бюджеты 2027")[0]["title"] == "Краткая справка по МСБ"
    assert docs.fts_query("решения по автоворонке ЛК") == '"решен"* AND "автоворонк"* AND "лк"'  # без «по»
    assert docs.search("автоворонке", ext=".md")[0]["title"] == "Журнал решений"
    assert docs.search("Восток лиды")[0]["part"] == "лист «План»"
    assert docs.search("budget")[0]["ext"] == ".pdf"
    (root / "QBR" / "копия").mkdir()
    (root / "QBR" / "копия" / "Решения.md").write_bytes((root / "Решения.md").read_bytes())
    docs.index()
    assert len(docs.search("автоворонку", ext=".md")) == 1  # копия в другой папке — не дубль в выдаче
    assert docs.search("автоворонка несуществующееслово")  # не все слова нашлись — показываем хотя бы по одному

    assert docs.index().endswith("обновлено: 0, удалено: 0, не прочитано: 0")  # ничего не менялось — не перечитываем
    (root / "Решения.md").write_text("# Журнал решений\n\nОтменили запуск", encoding="utf-8")
    (root / "wins.html").unlink()
    assert "обновлено: 1, удалено: 1" in docs.index()
    (root / "QBR" / "копия" / "Решения.md").unlink()
    docs.index()
    assert not docs.search("автоворонку", ext=".md") and docs.search("отменили")


def test_find_show_cli_and_macos_names(tmp_path, settings_with, capsys):
    root = tmp_path / "Docs"
    root.mkdir()
    nfd = unicodedata.normalize("NFD", "Справка МСБ — май.md")  # так имена хранит macOS
    (root / nfd).write_text("# Справка\n\nМаржа выросла", encoding="utf-8")
    settings_with(knowledge={"folders": [str(root)]})
    docs.index()
    assert docs.find("Справка МСБ — май.md")["title"] == "Справка"
    cli.main(["docs", "search", "маржи"])
    assert "Справка МСБ — май.md" in unicodedata.normalize("NFC", capsys.readouterr().out)
    cli.main(["docs", "show", "Справка МСБ — май.md"])
    assert "Маржа выросла" in capsys.readouterr().out
    cli.main(["docs", "status"])
    assert "В индексе: 1" in capsys.readouterr().out


def test_docs_pages_and_open(tmp_path, settings_with, monkeypatch):
    from opsdash.web import app as web

    root = library(tmp_path / "Компания")
    settings_with(knowledge={"folders": [str(root)]})
    docs.index()
    client = TestClient(web.app, base_url="http://localhost:8765")
    assert "Изменились за неделю" in client.get("/docs").text
    page = client.get("/docs?q=пролонгации").text
    assert "<mark>пролонгации</mark>" in page and "слайд 2" in page and ">Создан<" in page and ">Изменён<" in page
    # тип без запроса — все документы этого типа; HTML — это и .html, и .htm
    decks = client.get("/docs?type=.pptx").text
    assert "Презентация: 1" in decks and "QBR Q3 2026 — Коммерция" in decks and "Журнал решений" not in decks
    assert 'class="tchip t-pptx active"' in decks
    assert "Журнал решений" in client.get("/docs?q=автоворонке&type=.md").text
    assert "Ничего не найдено" in client.get("/docs?q=автоворонке&type=.pdf").text
    deck = str(root / "QBR" / "QBR Q3 2026 — Коммерция.pptx")
    view = client.get("/docs/view", params={"path": deck}).text
    assert "Конверсия пролонгации выросла до 41%" in view and "<h2>слайд 2</h2>" in view

    opened = []
    monkeypatch.setattr(web.sys, "platform", "darwin")
    monkeypatch.setattr("subprocess.run", lambda cmd, **k: opened.append(cmd))
    client.post("/docs/open", data={"path": deck, "reveal": "1"}, follow_redirects=False)
    assert opened == [["open", "-R", deck]]
    r = client.post("/docs/open", data={"path": "/etc/passwd"}, follow_redirects=False)
    assert "err=" in r.headers["location"] and len(opened) == 1  # только файлы из индекса
