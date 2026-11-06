"""Retrieval pipeline: uv run python -m askmike.retrieval index [--db PATH]. Prints counts only."""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from askmike.retrieval.index import StaleIndex, build_index, default_cache, default_db, open_index
from askmike.retrieval.ladder import RUNGS, Ladder, load_settings
from askmike.retrieval.models import Embedder

CORPUS = Path(__file__).resolve().parents[2] / "corpus"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="askmike.retrieval")
    sub = ap.add_subparsers(dest="step", required=True)
    ix = sub.add_parser("index", help="build the FTS5 + vector index")
    ix.add_argument("--db", type=Path, default=default_db())
    ix.add_argument("--cache", type=Path, default=None)
    se = sub.add_parser("search", help="run one query through a ladder rung")
    se.add_argument("query")
    se.add_argument("--rung", default="R3", choices=RUNGS)
    se.add_argument("--k", type=int, default=10)
    se.add_argument("--db", type=Path, default=default_db())
    a = ap.parse_args(argv)
    if a.step == "search":
        claims = {}
        for line in (CORPUS / "units.jsonl").read_text().splitlines():
            if line.strip():
                u = json.loads(line)
                claims[u["id"]] = u.get("claim") or ""
        try:
            emb = Embedder()
            conn = open_index(a.db, CORPUS / "units.jsonl", emb.name)
        except StaleIndex as e:
            print(f"search: {e}", file=sys.stderr)
            return 1
        ladder = Ladder(conn, emb, settings=load_settings())
        for i, h in enumerate(ladder.run(a.rung, a.query, a.k), start=1):
            print(f"{i:<4} {h.unit_id}  {claims.get(h.unit_id, '')[:100]}")
        return 0
    t0 = time.time()
    st = build_index(CORPUS, a.db, Embedder(), a.cache or (default_cache() if a.db == default_db()
                                                          else a.db.with_name("emb_cache.db")))
    print(f"indexed {st.units} units ({st.embedded} embedded, {st.cached} cached) "
          f"in {time.time() - t0:.1f}s -> {a.db}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
