"""Judge A's question-kind labels: evidence | personal | handoff for every recruiter question.

They are the ground truth for handoff correctness, independent of the router's regexes. The labels go to
$ASK_PRIVATE_DIR/work/answers/kinds_A.jsonl; the calls resume through a ledger beside it.
"""
from __future__ import annotations

import re
from pathlib import Path

from askmike.answer.route import question_kind
from askmike.evals.answer_sets import KINDS
from askmike.evals.judge import JUDGE_A, _line
from askmike.evals.steps import _map
from askmike.llm.ask import ask_json

BATCH = 8
TEMPLATE = Path(__file__).parent / "prompts" / "judge_kind.md"


def build_kind_prompt(rows) -> str:
    """rows: [{id, text}]. Each question is a JSON line whose "</" is escaped, so its text cannot close the
    block; the template is filled in one pass, so a placeholder typed inside a question stays as typed."""
    block = "\n".join(_line({"id": r["id"], "text": r["text"]}) for r in rows)
    return re.sub(r"\{questions\}", lambda m: block, TEMPLATE.read_text(encoding="utf-8"))


def label_kinds(rows, runner, ledger, workers: int = 4) -> dict[str, str]:
    """{question id: kind} for every row judge A labelled with one of KINDS, in batches of BATCH by id.

    A batch that fails or does not parse labels nothing, and a label outside KINDS is no label; either way
    the id is left out, so a rerun (answered from `ledger` where it can) fills it in. A runner with a
    `note(ok, error)` method (steps.Gate) hears how each batch went."""
    uniq = sorted({r["id"]: r for r in rows}.values(), key=lambda r: r["id"])
    batches = [uniq[i:i + BATCH] for i in range(0, len(uniq), BATCH)]
    note = getattr(runner, "note", None)

    def one(numbered):
        n, batch = numbered
        res = ask_json(f"kindsA:{n}", build_kind_prompt(batch), JUDGE_A, runner, ledger, dict)
        if note is not None:
            note(res.ok, res.error)
        if not res.ok:
            return {}
        return {r["id"]: res.value[r["id"]] for r in batch if res.value.get(r["id"]) in KINDS}

    out: dict[str, str] = {}
    for labels in _map(one, list(enumerate(batches)), workers):
        out.update(labels)
    return dict(sorted(out.items()))


def router_disagreements(rows, labels: dict) -> list[str]:
    """Ids of labelled rows whose router kind (route.question_kind) differs from judge A's label."""
    return sorted({r["id"] for r in rows if r["id"] in labels and question_kind(r["text"]) != labels[r["id"]]})
