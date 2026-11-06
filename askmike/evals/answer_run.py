"""The answer run: every eval item through one Answerer, one private record per item.

Records go to $ASK_PRIVATE_DIR/work/answers/<slug>/records.jsonl, keyed by item_id, latest wins. Model
replies resume through ledgered_runner (ledger.jsonl beside the records), so a rerun re-asks every item and
pays only for prompts it has not seen: a template or corpus change re-asks and overwrites stale records.
An item whose runner failed (an error, a timeout, the deadline) writes no record, so a rerun retries it.
"""
from __future__ import annotations

import copy
import re
import time
from dataclasses import asdict
from pathlib import Path

from askmike.answer.render import render_text
from askmike.evals.steps import DEADLINE, Gate, _map, read_jsonl, write_jsonl
from askmike.llm.ask import first_json, ledgered_runner, runner_key
from askmike.llm.ledger import Ledger


class AnswerGate(Gate):
    """steps.Gate for the answer runner contract: schema and effort pass through when given, and a call with
    neither reaches the runner as runner(prompt, model), as M1's runners take."""

    def __call__(self, prompt, model, schema=None, effort=None):
        if schema is None and effort is None:
            return super().__call__(prompt, model)
        if self.stopped or (self.deadline is not None and time.monotonic() > self.deadline):
            raise RuntimeError(DEADLINE)
        return self.runner(prompt, model, schema=schema, effort=effort)


# ---- canaries -------------------------------------------------------------

def _phrase_re(phrase: str) -> re.Pattern:
    body = r"\s+".join(re.escape(w) for w in phrase.split())
    pre = r"(?<![A-Za-z0-9])" if phrase[:1].isalnum() else ""
    post = r"(?![A-Za-z0-9])" if phrase[-1:].isalnum() else ""
    return re.compile(pre + body + post, re.I)


def canary_hits(text, phrases) -> list[str]:
    """The phrases found in `text`, in `phrases` order: whole phrases, any case, any run of whitespace
    between words. A word edge is required only where the phrase begins or ends with a letter or digit."""
    text = text if isinstance(text, str) else ""
    return [p for p in phrases if p and p.strip() and _phrase_re(p).search(text)]


def _strings(obj) -> list[str]:
    if isinstance(obj, str):
        return [obj]
    if isinstance(obj, dict):
        return [s for v in obj.values() for s in _strings(v)]
    if isinstance(obj, (list, tuple)):
        return [s for v in obj for s in _strings(v)]
    return []


# ---- one item -------------------------------------------------------------

class _Capture:
    """One item's view of the shared ledgered runner: keeps each answer-call reply (schema given), counts
    the calls the ledger could not answer, and keeps each error before re-raising it."""

    def __init__(self, runner, ledger):
        self.runner, self.ledger = runner, ledger
        self.replies: list = []
        self.errors: list[str] = []
        self.live = 0

    def __call__(self, prompt, model, schema=None, effort=None):
        cached = self.ledger.get(runner_key(prompt, model, schema, effort)) is not None
        if not cached:
            self.live += 1
        try:
            reply = self.runner(prompt, model, schema=schema, effort=effort)
        except Exception as e:
            self.errors.append(str(e)[:200])
            raise
        if schema is not None:
            self.replies.append(reply)
        return reply


def _raw_claims(reply) -> list:
    """The claims the model wrote, read the way the Answerer reads them; [] when there are none."""
    if not isinstance(reply, dict):
        return []
    data = reply.get("structured_output")
    if not isinstance(data, dict):
        data = first_json(str(reply.get("result") or ""), dict)
    claims = data.get("claims") if isinstance(data, dict) else None
    return list(claims) if isinstance(claims, list) else []


def _raw_text(reply) -> str:
    if not isinstance(reply, dict):
        return ""
    return "\n".join([str(reply.get("result") or ""), *_strings(reply.get("structured_output"))])


def _verdict(v) -> dict:
    return {"claim": asdict(v.claim), "kept": v.kept, "reasons": list(v.reasons), "tidied": v.tidied,
            "stripped_ids": v.stripped_ids}


def turn_record(o, text, previous, reply, units: dict, must_not=()) -> dict:
    """One answered input as a private record: the Outcome's fields, every raw claim with its Verdict (None
    when the gates did not run), what a visitor sees, and the canaries found in that and in the raw reply."""
    raw = _raw_claims(reply)
    verdicts = list(o.verdicts)
    rendered = render_text(o, units)
    pack = None if o.pack is None else {"unit_ids": list(o.pack.unit_ids),
                                        "req_units": {k: list(v) for k, v in o.pack.req_units.items()},
                                        "ledger_lines": list(o.pack.ledger_lines)}
    return {
        "input": text, "previous_text": previous.text if previous is not None else None,
        "outcome": o.outcome, "state": o.state, "kind": o.kind, "fallback_reason": o.fallback_reason,
        "prompt_sha": o.prompt_sha, "template_sha": o.template_sha, "kept": [asdict(c) for c in o.kept],
        "pack": pack, "requirements": [asdict(r) for r in o.requirements], "usage": o.usage, "ms": o.ms,
        "claims": [{"raw": r, "verdict": _verdict(verdicts[i]) if i < len(verdicts) else None}
                   for i, r in enumerate(raw)],
        "rendered": rendered,
        "canary_hits_rendered": canary_hits(rendered, must_not),
        "canary_hits_raw": canary_hits(_raw_text(reply), must_not),
    }


def _ask(answerer, cap: _Capture, text, previous, requirements, must_not):
    n = len(cap.replies)
    o = answerer.ask(text, previous=previous, requirements=requirements)
    reply = cap.replies[n] if len(cap.replies) > n else None
    return o, turn_record(o, text, previous, reply, answerer.units, must_not)


def answer_item(answerer, cap: _Capture, item) -> dict:
    """A follow-up pair asks `first`, then the follow-up with the Turn the first Outcome builds (turn_from:
    a jd keeps its requirement lines; none after an empty, over-long or handed-off input)."""
    reqs = list(item.requirements) if item.set == "jd" else None
    if item.set == "followup":
        o1, first = _ask(answerer, cap, item.text, None, None, ())
        _, rec = _ask(answerer, cap, item.followup, o1.turn(item.text), None, ())
        rec["first"] = first
    else:
        _, rec = _ask(answerer, cap, item.text, None, reqs, item.must_not)
    return dict(rec, item_id=item.item_id, set=item.set, split=item.split, cluster=item.cluster,
                model=answerer.model)


def turns(rec: dict):
    """(turn name, turn record) of an answer record: a follow-up pair's first turn ("first"), then the record's
    own, final turn ("final")."""
    if isinstance(rec.get("first"), dict):
        yield "first", rec["first"]
    yield "final", rec


def _internal_error(rec: dict) -> bool:
    """A runner_error fallback with no runner failure behind it: a bug in pack building or gating."""
    return any(t.get("fallback_reason") == "runner_error" for _, t in turns(rec))


def _keep_live_ms(rec: dict, old: dict | None) -> None:
    """A rerun answered from the ledger keeps the ms of the run that made the call."""
    if not old:
        return
    for new_t, old_t in ((rec, old), (rec.get("first"), old.get("first"))):
        if isinstance(new_t, dict) and isinstance(old_t, dict) and new_t.get("prompt_sha") == old_t.get("prompt_sha"):
            new_t["ms"] = old_t.get("ms", new_t["ms"])


# ---- the run --------------------------------------------------------------

def run_answers(items, answerer, records_path, workers: int = 4, max_seconds=None) -> dict:
    """Answer every item and merge the records into `records_path` (latest wins). `answerer.runner` is the
    bare runner; this wraps it in a Gate (the deadline, and a stop after MAX_FAILS failed items in a row)
    under ledgered_runner on ledger.jsonl beside the records. Returns counts only: `live` items made at
    least one model call the ledger could not answer, `cached` items made none (a router handoff needs
    none), `remaining` items failed or met the deadline and wrote no record."""
    records_path = Path(records_path)
    records_path.parent.mkdir(parents=True, exist_ok=True)
    ledger = Ledger(records_path.with_name("ledger.jsonl"))
    gate = AnswerGate(answerer.runner, max_seconds)
    shared = ledgered_runner(gate, ledger)
    old = {r["item_id"]: r for r in read_jsonl(records_path)}

    def one(item):
        cap = _Capture(shared, ledger)
        a = copy.copy(answerer)
        a.runner = cap
        rec = answer_item(a, cap, item)
        error = cap.errors[-1] if cap.errors else ""
        if cap.live:  # only an item that reached the runner moves the failure streak
            gate.note(not error, error)
        if error:
            return item, None, error, cap.live
        rec["cached"] = cap.live == 0  # no live model call: every reply came from the ledger, or none was needed
        if rec["cached"]:
            _keep_live_ms(rec, old.get(item.item_id))
        return item, rec, "", cap.live

    results = _map(one, list(items), workers)
    new = {item.item_id: rec for item, rec, _, _ in results if rec is not None}
    merged = old | new
    write_jsonl(records_path, [merged[k] for k in sorted(merged)])
    errors = [e for _, rec, e, _ in results if rec is None]
    return {"items": len(results), "recorded": len(new), "live": sum(1 for r in new.values() if not r["cached"]),
            "cached": sum(1 for r in new.values() if r["cached"]),
            "failed": sum(1 for e in errors if e != DEADLINE), "remaining": len(errors),
            "internal_errors": sum(1 for r in new.values() if _internal_error(r)), "stopped": gate.stopped,
            "last_error": next((e for e in reversed(errors) if e != DEADLINE), "")}
