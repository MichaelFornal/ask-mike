"""Rung dispatch and settings, adapted from deal-terms-desk retrieval/ladder.py on 2026-11-03.

R1 BM25, R2 dense, R3 RRF(R1, R2), R4 R3 with the BM25 leg on the alias-expanded query.
"""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from pathlib import Path

from askmike.retrieval.bm25 import Hit, search_bm25
from askmike.retrieval.dense import search_dense
from askmike.retrieval.expand import expand_query
from askmike.retrieval.hybrid import rrf

RUNGS = ("R1", "R2", "R3", "R4")
SETTINGS_PATH = Path(__file__).with_name("settings.json")
SKILLS_PATH = Path(__file__).resolve().parents[2] / "corpus" / "skills.json"


@dataclass(frozen=True)
class Settings:
    depth: int = 50
    rrf_k0: int = 60


def load_settings(path=SETTINGS_PATH) -> Settings:
    d = json.loads(Path(path).read_text())
    return Settings(depth=int(d["depth"]), rrf_k0=int(d["rrf_k0"]))


class Ladder:
    def __init__(self, conn, embedder, skills=None, settings: Settings | None = None):
        self.conn, self.embedder = conn, embedder
        self.skills = skills if skills is not None else json.loads(SKILLS_PATH.read_text())
        self.settings = settings or load_settings()

    def run(self, rung: str, query: str, k: int = 10) -> list[Hit]:
        if rung not in RUNGS:
            raise ValueError(f"unknown rung {rung!r}; expected one of {RUNGS}")
        d, k0 = self.settings.depth, self.settings.rrf_k0
        if rung == "R1":
            return search_bm25(self.conn, query, k)
        qvec = self.embedder.embed_query(query)
        if rung == "R2":
            return search_dense(self.conn, qvec, k)
        text = expand_query(query, self.skills) if rung == "R4" else query
        return rrf([search_bm25(self.conn, text, d), search_dense(self.conn, qvec, d)], k0=k0, k=k)


class LockedLadder:
    """A retrieval ladder for worker threads (the eval runs and the service desk): one Ladder per thread from
    `factory` (a sqlite connection may only be used on the thread that opened it), and one lock around every run,
    so the shared embedder serves one query at a time. Retrieval is quick; the model calls are what run in
    parallel. Moved here from askmike/evals/answer_run.py so the service does not import the eval code."""

    def __init__(self, factory):
        self.factory, self._local, self._lock = factory, threading.local(), threading.Lock()

    def run(self, rung, query, k=10):
        with self._lock:
            ladder = getattr(self._local, "ladder", None)
            if ladder is None:
                ladder = self._local.ladder = self.factory()
            return ladder.run(rung, query, k)
