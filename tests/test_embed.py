"""Эмбеддинги и связи «Памяти» (opsdash.embed, memory.build_links, карта): без настоящей модели — подменённая,
векторы из слов; проверяем устройство, а не качество модели."""

import hashlib

import numpy as np
import pytest

from opsdash import embed, jobs, memory, recall, records, store


class FakeModel:
    """Вектор из слов текста: тексты с общими словами — близки по косинусу."""

    def _vec(self, text):
        v = np.zeros(embed.DIM, dtype=np.float32)
        for w in recall.stems(text):
            v[int(hashlib.md5(w.encode()).hexdigest(), 16) % embed.DIM] += 1
        n = np.linalg.norm(v)
        return v / n if n else v

    def passages(self, texts, batch=24):
        return np.stack([self._vec(t) for t in texts])

    def query(self, text):
        return self._vec(text)


@pytest.fixture
def fake(monkeypatch):
    monkeypatch.setattr(embed, "ready", lambda: True)
    monkeypatch.setattr(embed, "model", lambda: FakeModel())
    embed._cache.clear()
    yield
    embed._cache.clear()


def meeting(mid, started="2026-11-03T08:00:00+00:00", transcript=""):
    store.execute("INSERT INTO meetings(id, title, started_at, status) VALUES(?,?,?,?)", (mid, mid, started, "processed"))
    d = records.meeting_dir(mid)
    d.mkdir(parents=True, exist_ok=True)
    if transcript:
        (d / "transcript.md").write_text(transcript, encoding="utf-8")


def add(text, about, source="meeting:m1", **kw):
    return memory.import_facts({"source": source, "facts": [{"kind": kw.pop("kind", "state"), "text": text,
                                                            "about": about, "quote": "[00:00:01] q", **kw}]})


def test_files_and_checksums_are_the_published_model():
    assert embed.model_dir().name == "e5-small" and embed.model_dir().parent.name == "models"
    assert embed.FILES["model_quantized.onnx"][1] == 118_308_185
    assert embed.FILES["tokenizer.json"][2].startswith("0b44a9d7")
    assert embed.ready() is False and embed.status()["ready"] is False  # в тестовой папке модели нет


def test_without_model_recall_and_links_still_work():
    meeting("m1", transcript="[00:00:01] Олег: склад переезжает в марте\n")
    add("Склад переезжает в марте", ["Склад"])
    add("Склад работает до 20:00", ["Склад"])
    assert embed.neighbours("склад") is None
    assert "Склад" in recall.recall("склад переезд")
    links = memory.build_links()
    assert links.get("entity") and links.get("temporal") and "semantic" not in links


def test_fill_vectors_and_semantic_links_and_hybrid(fake, monkeypatch):
    meeting("m1", transcript="[00:00:01] Олег: стеллажи для склада привезут в пятницу\n")
    meeting("m2", started="2026-11-10T08:00:00+00:00",
            transcript="[00:00:01] Анна: погрузчик сломался, ремонт до среды\n")
    a = add("Стеллажи для склада привезут в пятницу", ["Склад"])["added"][0]
    b = add("Стеллажи для склада привезут в пятницу утром", ["Ромашка"], source="meeting:m2")["added"][0]
    recall.refresh()
    n = embed.fill(limit=None)
    assert n == store.scalar("SELECT COUNT(*) FROM memory_vectors") and embed.missing() == 0
    monkeypatch.setattr(embed, "SEMANTIC_MIN", 0.5)
    pairs = embed.semantic_pairs(min_sim=0.5)
    assert (min(a, b), max(a, b)) in {(x, y) for x, y, _ in pairs}
    links = memory.build_links()
    assert links["semantic"] >= 1
    monkeypatch.setattr(jobs, "in_daemon", lambda: True)  # поиск по смыслу — в процессе PULSE
    near = embed.neighbours("стеллажи склад")
    assert near["facts"][0] in (a, b) and near["chunks"]
    text = recall.recall("стеллажи для склада")
    assert "Стеллажи" in text
    log = store.row("SELECT mode FROM recall_log ORDER BY id DESC LIMIT 1")
    assert log["mode"] == "слова + смысл"
    store.execute("DELETE FROM memory_chunks")
    assert embed.drop_orphans() >= 2  # векторы удалённых фрагментов уходят


def test_hybrid_keeps_meetings_words_first():
    kinds = {1: "meet", 2: "sum", 3: "msg", 4: "meet", 5: "msg"}
    out = recall.hybrid([1, 2, 3], [4, 5, 3], kinds)
    assert out.index(1) < out.index(4) and out.index(2) < out.index(4)  # встречи: слова раньше смысла
    assert set(out) == {1, 2, 3, 4, 5}


def test_causal_and_temporal_links_and_graph_api():
    from starlette.testclient import TestClient

    from opsdash.web.app import app

    meeting("m1")
    memory.import_facts({"source": "meeting:m1", "facts": [
        {"ref": "x", "kind": "state", "text": "Подрядчик по стеллажам не успевает к февралю", "about": ["Склад"],
         "quote": "[00:01:00] не успевают"},
        {"kind": "event", "text": "Переезд склада перенесли на март", "about": ["Склад"], "because": "x",
         "quote": "[00:01:30] поэтому перенесли"}]})
    links = memory.build_links()
    assert links["causal"] == 1 and links["temporal"] >= 1 and links["entity"] >= 1
    client = TestClient(app, base_url="http://localhost:8765")
    g = client.get("/api/memory/graph", params={"days": 0}).json()
    assert len(g["nodes"]) == 2 and {l[2] for l in g["links"]} >= {"causal", "temporal", "entity"}
    fid = g["nodes"][0]["id"]
    card = client.get(f"/api/memory/fact/{fid}").json()
    assert card["neighbours"] and card["source"].startswith("встреча")
    ents = client.get("/api/memory/graph", params={"view": "entities"}).json()
    assert ents["nodes"][0]["name"] == "Склад"
    page = client.get("/memory", params={"tab": "map"}).text
    assert "data-mem-map" in page and "Скачать, 135 МБ" in page and "Память в цифрах" in page
    assert client.get("/api/memory/near", params={"q": "склад"}).json() == {"ok": False}


def test_release_after_idle(monkeypatch):
    embed._model.update(emb=object(), used=0.0)
    assert embed.release_if_idle(idle_seconds=10) is True and embed._model["emb"] is None


def test_today_line_and_leads(capsys):
    from opsdash import cli

    meeting("m1", started=store.utcnow())
    memory.import_facts({"source": "meeting:m1", "facts": [
        {"kind": "metric", "text": "Конверсия в оплату — 10%", "key": "конверсия", "about": ["Ромашка"],
         "quote": "[00:00:01] 10", "when": "2026-10-01"}]})
    memory.import_facts({"source": "manual", "facts": [
        {"kind": "metric", "text": "Конверсия в оплату — 12%", "key": "конверсия", "about": ["Ромашка"]}]},
        by="director")
    cli.main(["memory", "today"])
    out = capsys.readouterr().out
    assert out.startswith("Узнал за день: 2") and "10% → Конверсия в оплату — 12%" in out
    things = ["аренда", "охрана", "уборка", "отопление", "погрузчик", "стеллажи", "рампа", "весы", "сканеры", "пропуска"]
    for i, w in enumerate(things):
        memory.add_fact(text=f"У «Ромашки» {w}: договор до {i + 1}.12", about="Ромашка", source="manual",
                        by="director")
    assert [r["name"] for r in memory.leads_due()] == ["Ромашка"]
    assert memory.set_leads({"Ромашка": "Ключевой клиент склада; конверсия на 03.11 — 12%."}) == ["Ромашка"]
    assert memory.leads_due() == []
    assert "Ключевой клиент склада" in memory.page_text("Ромашка")
