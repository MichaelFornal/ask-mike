# ported from deal-terms-desk tests/fakes.py on 2026-11-03 (FakeReranker dropped)
import hashlib
import math
import re

TOKEN = re.compile(r"[A-Za-z0-9]+")
DIM = 384


class FakeEmbedder:
    """Bag of hashed words, L2-normalised: deterministic and similar for texts sharing words."""
    name = "fake-embedder"

    def __init__(self):
        self.embedded = 0

    def _vec(self, text: str) -> list[float]:
        v = [0.0] * DIM
        for t in TOKEN.findall(text.lower()):
            v[int(hashlib.sha1(t.encode()).hexdigest(), 16) % DIM] += 1.0
        norm = math.sqrt(sum(x * x for x in v))
        if norm == 0:
            v[0], norm = 1.0, 1.0
        return [x / norm for x in v]

    def embed_passages(self, texts):
        self.embedded += len(texts)
        return [self._vec(t) for t in texts]

    def embed_query(self, q):
        return self._vec(q)

    def count_tokens(self, text):
        return len(TOKEN.findall(text)) + 2


def fake_claude(result: str, input_tokens: int = 100, output_tokens: int = 20):
    calls = []

    def runner(prompt: str, model: str) -> dict:
        calls.append((prompt, model))
        return {"result": result, "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
                "duration_api_ms": 50}
    runner.calls = calls
    return runner
