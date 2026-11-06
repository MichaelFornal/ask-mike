"""AI relevance judge: one ledgered call per batch of queries with their own candidate lists."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from askmike.llm.ask import ask_json

JUDGE_A = "claude-opus-5-5"
JUDGE_B = "claude-sonnet-5-5"
_TEMPLATE = Path(__file__).parent / "prompts" / "judge_relevance.md"


@dataclass
class JudgeResult:
    labels: dict = field(default_factory=dict)
    dropped_unknown: int = 0
    parse_error: bool = False
    missing_queries: list = field(default_factory=list)
    error: str = ""
    prompt_sha: str = ""


def _line(obj) -> str:
    # "<\/" is valid JSON, so a closing tag inside data cannot end its block
    return json.dumps(obj, ensure_ascii=True, sort_keys=True).replace("</", "<\\/")


def build_judge_prompt(queries, candidates, pools=None) -> str:
    """queries: [(id, text)]; candidates: unit dicts; pools: {query id: [unit ids]} (default: all candidates)."""
    all_ids = [c["id"] for c in candidates]
    qlines = [_line({"id": qid, "text": text, "candidates": list((pools or {}).get(qid, all_ids))})
              for qid, text in queries]
    ulines = [_line({"id": c["id"], "claim": c.get("claim") or "", "detail": c.get("detail") or ""})
              for c in candidates]
    fill = {"queries": "\n".join(qlines), "units": "\n".join(ulines)}
    # one pass, so a placeholder written inside a query or unit is never expanded
    return re.sub(r"\{(queries|units)\}", lambda m: fill[m.group(1)], _TEMPLATE.read_text(encoding="utf-8"))


def judge_batch(batch_key, queries, candidates, model, runner, ledger, pools=None) -> JudgeResult:
    prompt = build_judge_prompt(queries, candidates, pools)
    res = ask_json(batch_key, prompt, model, runner, ledger, dict)
    if not res.ok:
        return JudgeResult(parse_error=res.error == "parse", error=res.error, prompt_sha=res.prompt_sha)
    out = JudgeResult(prompt_sha=res.prompt_sha)
    all_ids = [c["id"] for c in candidates]
    for qid, _ in queries:
        allowed = set((pools or {}).get(qid, all_ids))
        reply = res.value.get(qid)
        if not isinstance(reply, list):
            out.missing_queries.append(qid)
            continue
        kept: list = []
        for uid in reply:
            if isinstance(uid, str) and uid in allowed and uid not in kept:
                kept.append(uid)
            else:
                out.dropped_unknown += 1
        out.labels[qid] = kept
    return out
