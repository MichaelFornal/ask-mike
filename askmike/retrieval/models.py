# ported from deal-terms-desk retrieval/models.py on 2026-11-03
import os
from pathlib import Path

EMBED_MODEL = "BAAI/bge-small-en-v1.5"
EMBED_DIM = 384
MAX_TOKENS = 512
QUERY_PREFIX = "Represent this sentence for searching relevant passages: "


def cache_dir() -> Path:
    env = os.environ.get("FASTEMBED_CACHE_PATH")
    return Path(env) if env else Path.home() / ".cache" / "askmike" / "fastembed"


class Embedder:
    def __init__(self, name: str = EMBED_MODEL, threads: int | None = None):
        self.name = name
        self._threads = threads
        self._model = None

    def _m(self):
        if self._model is None:
            from fastembed import TextEmbedding
            self._model = TextEmbedding(self.name, threads=self._threads, cache_dir=str(cache_dir()))
        return self._model

    def embed_passages(self, texts: list[str]) -> list[list[float]]:
        return [[float(x) for x in v] for v in self._m().embed(texts, batch_size=16)]

    def embed_query(self, q: str) -> list[float]:
        return [float(x) for x in next(iter(self._m().embed([QUERY_PREFIX + q])))]

    def count_tokens(self, text: str) -> int:
        tok = self._m().model.tokenizer
        tok.no_truncation()
        try:
            return len(tok.encode(text).ids)
        finally:
            tok.enable_truncation(MAX_TOKENS)
