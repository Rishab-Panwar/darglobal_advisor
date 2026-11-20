"""
Retrieval over the scraped pages: chunking + BM25 + dense embeddings, fused with RRF.

- BM25 is strong on exact terms (project names, cities, prices).
- Dense (bge-small, ONNX on CPU via fastembed) catches paraphrases ("who runs the company" -> leadership).
- Reciprocal Rank Fusion combines both without score calibration.
Embeddings are precomputed at image build time (python -m app.rag) and cached in data/embeddings.npy.
"""
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from rank_bm25 import BM25Okapi

TOKEN = re.compile(r"[a-z0-9؀-ۿ]+")
STOP = set("""a an the and or of to in on for with is are was were be by at as it this that from
what which who whom how when where why do does did can could should would i you me my your we our
about tell any some there their they them its into than then so if not no yes please show give""".split())
EMBED_MODEL = os.getenv("EMBED_MODEL", "BAAI/bge-small-en-v1.5")


def tokenize(s: str) -> list[str]:
    return [t for t in TOKEN.findall(s.lower()) if t not in STOP]


@dataclass
class Chunk:
    site: str
    url: str
    title: str
    text: str


def chunk_text(text: str, size: int = 900, overlap: int = 150) -> list[str]:
    lines, chunks, cur = [], [], ""
    for ln in text.split("\n"):  # hard-wrap very long lines so no chunk is oversized
        lines.extend(ln[i:i + size] for i in range(0, max(len(ln), 1), size))
    for ln in lines:
        if len(cur) + len(ln) + 1 > size and cur:
            chunks.append(cur.strip())
            cur = cur[-overlap:]
        cur += ln + "\n"
    if cur.strip():
        chunks.append(cur.strip())
    return chunks


class Index:
    def __init__(self, data_dir: Path, use_dense: bool = True):
        self.data_dir = data_dir
        self.chunks: list[Chunk] = []
        self.pages = {}
        seen_text, seen_url = set(), set()
        for f in sorted(data_dir.glob("*.jsonl")):
            for line in f.open(encoding="utf-8"):
                rec = json.loads(line)
                url_key = rec["url"].replace("://www.", "://").rstrip("/")
                if url_key in seen_url:
                    continue
                seen_url.add(url_key)
                self.pages[rec["site"]] = self.pages.get(rec["site"], 0) + 1
                header = " ".join(x for x in (rec.get("title"), rec.get("h1"), rec.get("description")) if x)
                for c in chunk_text(rec["text"]):
                    key = c[:300]
                    if key in seen_text:  # drop repeated nav/footer blocks
                        continue
                    seen_text.add(key)
                    self.chunks.append(Chunk(rec["site"], rec["url"], rec.get("title", ""), f"{header}\n{c}"))
        site_name = {"darglobal": "dar global darglobal", "wasalt": "wasalt"}
        corpus = [tokenize(f"{site_name.get(c.site, c.site)} {c.url} {c.text}") for c in self.chunks]
        self.bm25 = BM25Okapi(corpus) if corpus else None
        self.embedder, self.emb = None, None
        if use_dense and self.chunks:
            self._load_dense()

    # ---- dense ---------------------------------------------------------------
    def _passages(self):
        return [f"{c.site} {c.title}\n{c.text}"[:1500] for c in self.chunks]

    def _load_dense(self):
        try:
            from fastembed import TextEmbedding
        except ImportError:
            return
        cache = self.data_dir / "embeddings.npy"
        meta = self.data_dir / "embeddings.meta.json"
        self.embedder = TextEmbedding(EMBED_MODEL)
        sig = {"model": EMBED_MODEL, "n": len(self.chunks), "first": self.chunks[0].text[:80]}
        if cache.exists() and meta.exists() and json.loads(meta.read_text()) == sig:
            self.emb = np.load(cache)
        else:
            self.emb = np.array(list(self.embedder.embed(self._passages(), batch_size=32)), dtype=np.float32)
            try:
                np.save(cache, self.emb)
                meta.write_text(json.dumps(sig))
            except OSError:
                pass
        self.emb /= np.linalg.norm(self.emb, axis=1, keepdims=True) + 1e-9

    def modes(self):
        return ["bm25", "dense", "hybrid"] if self.emb is not None else ["bm25"]

    # ---- search --------------------------------------------------------------
    def _rank_bm25(self, query):
        q = tokenize(query)
        if not q or not self.bm25:
            return []
        s = self.bm25.get_scores(q)
        return [i for i in np.argsort(-s)[:100] if s[i] > 0]

    def _rank_dense(self, query):
        if self.emb is None:
            return []
        qv = np.array(next(iter(self.embedder.query_embed([query]))), dtype=np.float32)
        qv /= np.linalg.norm(qv) + 1e-9
        return list(np.argsort(-(self.emb @ qv))[:100])

    def search(self, query: str, k: int = 6, mode: str = "hybrid") -> list[Chunk]:
        if mode == "bm25" or self.emb is None:
            ranked = self._rank_bm25(query)
        elif mode == "dense":
            ranked = self._rank_dense(query)
        else:  # reciprocal rank fusion
            score = {}
            for lst in (self._rank_bm25(query), self._rank_dense(query)):
                for r, i in enumerate(lst):
                    score[i] = score.get(i, 0) + 1 / (60 + r)
            ranked = sorted(score, key=lambda i: -score[i])
        out, per_url = [], {}
        for i in ranked:
            if len(out) >= k:
                break
            c = self.chunks[i]
            if per_url.get(c.url, 0) >= 2:  # diversity: max 2 chunks per page
                continue
            per_url[c.url] = per_url.get(c.url, 0) + 1
            out.append(c)
        return out


if __name__ == "__main__":  # build-time: precompute embeddings into data/
    ix = Index(Path(os.getenv("DATA_DIR", Path(__file__).resolve().parent.parent / "data")))
    print(f"indexed {len(ix.chunks)} chunks; modes={ix.modes()}")
