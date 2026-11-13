"""Эмбеддинги «Памяти» (этап 3): модель multilingual-e5-small, векторы фактов и фрагментов, поиск по смыслу.

- **Модель** — `Xenova/multilingual-e5-small` с Hugging Face (MIT): сжатая ONNX-модель и токенизатор, 135 МБ.
  PULSE скачивает её сам в `data/models/e5-small/` (Настройки → Память → «Скачать модель») и проверяет по
  контрольной сумме. Работает на `onnxruntime`, `tokenizers` и `numpy` — они уже стоят ради расшифровки.
- **Держит её только работающий PULSE**: загружает при первой надобности и выгружает после 10 минут простоя
  (в памяти это 0,7–1 ГБ). `./ops recall` и MCP-сервер просят у него ближайших по смыслу (`/api/memory/near`);
  PULSE не запущен или модели нет — поиск только по словам, связи по смыслу не строятся, остальное работает.
- **Векторы** — 384 числа float32 в `memory_vectors`: `f:<id факта>` и `c:<hash фрагмента>` (фрагмент нарезали
  заново, а текст тот же — вектор остаётся). Досчитываются в лёгкой очереди порциями (`fill`), чтобы не держать сбор.
- Префиксы e5: «query: » у вопроса и «passage: » у текста; усреднение по маске внимания и нормировка.
"""

from __future__ import annotations

import hashlib
import threading
import time
from pathlib import Path

from opsdash import config, store

REPO = "Xenova/multilingual-e5-small"
FILES = {  # файл в папке модели: (путь в репозитории, размер, sha256)
    "model_quantized.onnx": ("onnx/model_quantized.onnx", 118_308_185,
                             "f80102d3f2a1229f387d3c81909990d8945513e347b0eab049f7de3c6f98c193"),
    "tokenizer.json": ("tokenizer.json", 17_082_730,
                       "0b44a9d7b51c3c62626640cda0e2c2f70fdacdc25bbbd68038369d14ebdf4c39"),
}
DIM = 384
IDLE_SEC = 600          # выгрузить модель после 10 минут простоя
FILL_BATCH = 24
FILL_PER_RUN = 300      # векторов за одно задание лёгкой очереди (~10 с): сбор сообщений не ждёт
NEAR_TIMEOUT = 4.0      # ./ops recall ждёт ответа PULSE не дольше: первая загрузка модели — 1–2 с


class EmbedError(Exception):
    pass


def model_dir() -> Path:
    return config.MODELS_DIR / "e5-small"


# --- файлы модели -----------------------------------------------------------------------------------------------

def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def ready() -> bool:
    """Модель на месте и проверена (сумму считаем один раз на размер и время изменения файла)."""
    d = model_dir()
    sig = []
    for name, (_, size, _) in FILES.items():
        p = d / name
        try:
            st = p.stat()
        except OSError:
            return False
        if st.st_size != size:
            return False
        sig.append(f"{name}:{st.st_size}:{int(st.st_mtime)}")
    sig = "|".join(sig)
    if store.get_state("embed_verified") == sig:
        return True
    if all(_sha256(d / name) == sha for name, (_, _, sha) in FILES.items()):
        if not store._query_only:
            store.set_state("embed_verified", sig)
        return True
    return False


def enabled() -> bool:
    return bool(config.load_settings()["memory"].get("embeddings", True))


def _base_url() -> str:
    endpoint = (config.load_settings()["recordings"].get("hf_endpoint") or "").rstrip("/")
    return endpoint or "https://huggingface.co"


def download(progress=None) -> str:
    """Скачать модель с проверкой суммы: во временный файл, потом на место. Уже есть — ничего не качает."""
    import httpx

    d = model_dir()
    d.mkdir(parents=True, exist_ok=True)
    total = sum(size for _, size, _ in FILES.values())
    done = 0
    for name, (path, size, sha) in FILES.items():
        target = d / name
        if target.exists() and target.stat().st_size == size and _sha256(target) == sha:
            done += size
            continue
        tmp = target.with_suffix(target.suffix + ".part")
        url = f"{_base_url()}/{REPO}/resolve/main/{path}"
        h = hashlib.sha256()
        with httpx.stream("GET", url, follow_redirects=True, timeout=60) as r:
            if r.status_code != 200:
                raise EmbedError(f"{name}: сервер ответил {r.status_code}")
            with tmp.open("wb") as f:
                for chunk in r.iter_bytes(1 << 20):
                    f.write(chunk)
                    h.update(chunk)
                    done += len(chunk)
                    store.set_state("embed_download", {"done": done, "total": total, "at": store.utcnow()})
                    if progress:
                        progress(done, total)
        if h.hexdigest() != sha or tmp.stat().st_size != size:
            tmp.unlink(missing_ok=True)
            raise EmbedError(f"{name}: контрольная сумма не сошлась — файл повреждён, скачайте ещё раз")
        tmp.replace(target)
    store.set_state("embed_download", None)
    if not ready():
        raise EmbedError("модель скачана, но не прошла проверку")
    return f"модель на месте: {total / 1e6:.0f} МБ"


def remove() -> None:
    release()
    for name in FILES:
        (model_dir() / name).unlink(missing_ok=True)
    store.set_state("embed_verified", None)
    store.execute("DELETE FROM fact_links WHERE kind='semantic'")


def status() -> dict:
    dl = store.get_state("embed_download") or None
    downloading = bool(dl) and _job_running("memory-model")
    return {"ready": ready(), "loaded": _model["emb"] is not None, "downloading": downloading,
            "progress": f"{dl['done'] / 1e6:.0f} из {dl['total'] / 1e6:.0f} МБ" if dl and downloading else "",
            "error": store.get_state("embed_error") or "", "enabled": enabled(),
            "vectors": store.scalar("SELECT COUNT(*) FROM memory_vectors") or 0}


def _job_running(name: str) -> bool:
    return bool(store.scalar("SELECT running FROM jobs WHERE name=?", (name,)))


# --- модель -----------------------------------------------------------------------------------------------------

class Embedder:
    def __init__(self, d: Path, threads: int = 4):
        import numpy as np
        import onnxruntime as ort
        from tokenizers import Tokenizer

        self.np = np
        self.tok = Tokenizer.from_file(str(d / "tokenizer.json"))
        self.tok.enable_truncation(max_length=512)
        self.tok.enable_padding(pad_id=self.tok.token_to_id("<pad>"), pad_token="<pad>")
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = threads
        opts.log_severity_level = 3
        self.sess = ort.InferenceSession(str(d / "model_quantized.onnx"), opts, providers=["CPUExecutionProvider"])
        self.inputs = [i.name for i in self.sess.get_inputs()]

    def _run(self, texts: list[str]):
        np = self.np
        enc = self.tok.encode_batch(texts)
        ids = np.array([e.ids for e in enc], dtype=np.int64)
        mask = np.array([e.attention_mask for e in enc], dtype=np.int64)
        feed = {"input_ids": ids, "attention_mask": mask}
        if "token_type_ids" in self.inputs:
            feed["token_type_ids"] = np.zeros_like(ids)
        hidden = self.sess.run(None, feed)[0]
        m = mask[:, :, None].astype(np.float32)
        vec = (hidden * m).sum(1) / np.clip(m.sum(1), 1e-9, None)
        return (vec / np.linalg.norm(vec, axis=1, keepdims=True)).astype(np.float32)

    def passages(self, texts: list[str], batch: int = FILL_BATCH):
        np = self.np
        order = sorted(range(len(texts)), key=lambda i: len(texts[i]))
        out = np.zeros((len(texts), DIM), dtype=np.float32)
        for start in range(0, len(order), batch):
            idx = order[start:start + batch]
            out[idx] = self._run(["passage: " + texts[i] for i in idx])
        return out

    def query(self, text: str):
        return self._run(["query: " + text])[0]


_model: dict = {"emb": None, "used": 0.0}
_lock = threading.RLock()


def model() -> Embedder:
    """Модель в памяти этого процесса — загрузить при первой надобности."""
    with _lock:
        if _model["emb"] is None:
            if not ready():
                raise EmbedError("модель не скачана")
            _model["emb"] = Embedder(model_dir())
        _model["used"] = time.time()
        return _model["emb"]


def release() -> None:
    with _lock:
        _model["emb"] = None
        _cache.clear()


def release_if_idle(idle_seconds: int = IDLE_SEC) -> bool:
    with _lock:
        if _model["emb"] is not None and time.time() - _model["used"] > idle_seconds:
            release()
            return True
    return False


# --- векторы ----------------------------------------------------------------------------------------------------

def _pack(vec) -> bytes:
    return vec.astype("float32").tobytes()


def fact_text(f: dict) -> str:
    return " ".join(x for x in (f.get("about") or "", f.get("text") or "") if x)


def fill(chunks: bool = True, facts: bool = True, limit: int | None = FILL_PER_RUN, progress=None) -> int:
    """Досчитать недостающие векторы: сначала факты (их мало, они нужны карте), потом фрагменты. limit — не больше
    стольких за раз (None — все)."""
    if store._query_only or not ready():
        return 0
    todo: list[tuple[str, str]] = []
    if facts:
        rows = store.rows(
            "SELECT f.id, f.text, (SELECT group_concat(e.name, ', ') FROM fact_entities fe JOIN entities e "
            "ON e.id=fe.entity_id WHERE fe.fact_id=f.id) AS about FROM facts f WHERE f.status IN ('active','replaced',"
            "'proposed') AND NOT EXISTS (SELECT 1 FROM memory_vectors v WHERE v.key = 'f:' || f.id)")
        todo += [(f"f:{r['id']}", fact_text(r)) for r in rows]
    if chunks:
        rows = store.rows("SELECT c.hash, c.title, c.text FROM memory_chunks c WHERE NOT EXISTS "
                          "(SELECT 1 FROM memory_vectors v WHERE v.key = 'c:' || c.hash)")
        seen = set()
        for r in rows:
            if r["hash"] not in seen:
                seen.add(r["hash"])
                todo.append((f"c:{r['hash']}", f"{r['title']}\n{r['text']}"))
    if limit:
        todo = todo[:limit]
    if not todo:
        return 0
    emb = model()
    n = 0
    for start in range(0, len(todo), 96):
        part = todo[start:start + 96]
        vecs = emb.passages([t for _, t in part])
        with store.db() as conn:
            for (key, _), v in zip(part, vecs):
                conn.execute("INSERT OR REPLACE INTO memory_vectors(key, vec) VALUES(?, ?)", (key, _pack(v)))
        n += len(part)
        _model["used"] = time.time()
        if progress:
            progress(f"  векторы: {n}/{len(todo)}")
    _cache.clear()
    store.set_state("memory_vectors_at", store.utcnow())
    return n


def missing() -> int:
    """Сколько фактов и фрагментов ещё без вектора."""
    return (store.scalar("SELECT COUNT(*) FROM facts f WHERE f.status IN ('active','replaced','proposed') AND NOT EXISTS "
                         "(SELECT 1 FROM memory_vectors v WHERE v.key = 'f:' || f.id)") or 0) + \
        (store.scalar("SELECT COUNT(DISTINCT c.hash) FROM memory_chunks c WHERE NOT EXISTS "
                      "(SELECT 1 FROM memory_vectors v WHERE v.key = 'c:' || c.hash)") or 0)


def drop_orphans() -> int:
    """Векторы фрагментов, которых больше нет, и забытых фактов."""
    return store.execute("DELETE FROM memory_vectors WHERE (key LIKE 'c:%' AND substr(key, 3) NOT IN "
                         "(SELECT hash FROM memory_chunks)) OR (key LIKE 'f:%' AND CAST(substr(key, 3) AS INTEGER) "
                         "NOT IN (SELECT id FROM facts))")


_cache: dict = {}


def matrix(kind: str):
    """Все векторы вида (f — факты, c — фрагменты): (ключи, матрица). Кэш — до следующего fill."""
    import numpy as np

    key = (kind, store.get_state("memory_vectors_at"), store.scalar("SELECT COUNT(*) FROM memory_vectors"))
    if _cache.get(kind, (None,))[0] != key:
        rows = store.rows("SELECT key, vec FROM memory_vectors WHERE key LIKE ?", (f"{kind}:%",))
        keys = [r["key"][2:] for r in rows]
        mat = np.frombuffer(b"".join(r["vec"] for r in rows), dtype=np.float32).reshape(-1, DIM) if rows \
            else np.zeros((0, DIM), dtype=np.float32)
        _cache[kind] = (key, keys, mat)
    return _cache[kind][1], _cache[kind][2]


def _top(mat, q, k: int) -> list[int]:
    import numpy as np

    if not len(mat):
        return []
    scores = mat @ q
    k = min(k, len(scores))
    top = np.argpartition(-scores, k - 1)[:k]
    return [int(i) for i in top[np.argsort(-scores[top])]]


def near_local(query: str, k: int = 50) -> dict:
    """Ближайшие по смыслу факты и фрагменты — в этом процессе (он держит модель)."""
    q = model().query(query)
    fkeys, fmat = matrix("f")
    ckeys, cmat = matrix("c")
    facts = [int(fkeys[i]) for i in _top(fmat, q, k)]
    hashes = [ckeys[i] for i in _top(cmat, q, k)]
    chunk_ids = []
    if hashes:
        by_hash = {}
        for r in store.rows(f"SELECT id, hash FROM memory_chunks WHERE hash IN ({','.join('?' * len(hashes))})",
                            hashes):
            by_hash.setdefault(r["hash"], r["id"])
        chunk_ids = [by_hash[h] for h in hashes if h in by_hash]
    return {"facts": facts, "chunks": chunk_ids}


def neighbours(query: str, k: int = 50) -> dict | None:
    """Для «Вспомнить»: в процессе PULSE — сам; из ./ops recall и MCP — у работающего PULSE. Нет — None."""
    if not enabled():
        return None
    from opsdash import jobs

    if jobs.in_daemon():
        if not ready():
            return None
        return near_local(query, k)
    import httpx

    port = config.load_settings()["web"].get("port") or 8765
    try:
        r = httpx.get(f"http://127.0.0.1:{port}/api/memory/near", params={"q": query, "k": k}, timeout=NEAR_TIMEOUT)
    except httpx.HTTPError:
        return None
    if r.status_code != 200:
        return None
    data = r.json()
    return data if data.get("ok") else None


# --- связи по смыслу -------------------------------------------------------------------------------------------

SEMANTIC_MIN = 0.89  # подобран на копии: при 0,82 у e5 «связаны» почти все факты одной компании
SEMANTIC_K = 8


def semantic_pairs(min_sim: float | None = None, k: int = SEMANTIC_K) -> list[tuple[int, int, float]]:
    """До k ближайших соседей каждого факта с косинусной близостью от min_sim — без модели, по готовым векторам."""
    import numpy as np

    min_sim = SEMANTIC_MIN if min_sim is None else min_sim

    keys, mat = matrix("f")
    if len(keys) < 2:
        return []
    ids = [int(x) for x in keys]
    live = {r["id"] for r in store.rows("SELECT id FROM facts WHERE status='active'")}
    keep = [i for i, fid in enumerate(ids) if fid in live]
    if len(keep) < 2:
        return []
    mat = mat[keep]
    ids = [ids[i] for i in keep]
    out: dict[tuple[int, int], float] = {}
    for start in range(0, len(ids), 512):
        sims = mat[start:start + 512] @ mat.T
        for row in range(sims.shape[0]):
            i = start + row
            sims[row, i] = -1
            top = np.argpartition(-sims[row], min(k, len(ids) - 1) - 1)[:k]
            for j in top:
                s = float(sims[row, j])
                if s >= min_sim:
                    a, b = sorted((ids[i], ids[int(j)]))
                    out[(a, b)] = max(out.get((a, b), 0), s)
    return [(a, b, s) for (a, b), s in out.items()]


class LocalSearch:
    """Поиск по смыслу в этом процессе — для scripts/recall_bench.py (на копии базы, без работающего PULSE)."""

    def ready(self) -> bool:
        keys, _ = matrix("c")
        return ready() and bool(keys)

    def chunks(self, query: str, k: int = 50) -> list[int]:
        return near_local(query, k)["chunks"]
