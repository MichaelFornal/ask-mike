"""The claim-support judge: does each claim say only what the units it cites say?

One ledgered call per answer record covers every raw claim, kept or dropped (both turns of a follow-up
pair), that cites at least one corpus unit. The prompt shows each claim's text, its cited units as the
answer model saw them (prompt.evidence_line) and ledger lines for the skills it names (the pack's labels,
evidence.ledger_lines). It never shows the item id, the claim type or what the gates decided.

A verdict is supported | partial | unsupported, keyed "<item_id>#<n>" (n counts the record's raw claims
across its turns). An omitted or bad verdict is None and counts nowhere; a failed call leaves its claims
out, so a rerun asks again. Verdicts go to $ASK_PRIVATE_DIR/work/answers/<slug>/support_<judge>.jsonl.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from askmike.answer.evidence import _ledger, ledger_lines
from askmike.answer.prompt import evidence_line
from askmike.evals.answer_run import turns
from askmike.evals.bootstrap import cluster_bootstrap
from askmike.evals.judge import JUDGE_A, JUDGE_B, _line
from askmike.evals.metrics import cohen_kappa
from askmike.evals.steps import _map
from askmike.llm.ask import ask_json

VERDICTS = ("supported", "partial", "unsupported")
JUDGES = {"A": JUDGE_A, "B": JUDGE_B}
TEMPLATE = Path(__file__).parent / "prompts" / "judge_support.md"


def template_sha() -> str:
    """sha1[:12] of judge_support.md as it stands: each prompt revision has its own."""
    return hashlib.sha1(TEMPLATE.read_bytes()).hexdigest()[:12]


def in_b_sample(item_id: str) -> bool:
    return hashlib.sha1(("supportB:" + item_id).encode("utf-8")).digest()[0] % 10 < 4


def claim_sha(text: str, units) -> str:
    """A fingerprint of a claim as judged, so a verdict for a claim that has since changed is never read."""
    return hashlib.sha1(json.dumps([text, list(units)]).encode("utf-8")).hexdigest()[:12]


# ---- the prompt and one call ------------------------------------------------

def build_support_prompt(claims, units: dict, skills, allow: frozenset = frozenset()) -> str:
    """claims: [(local id, text, cited ids)]. One JSON line per claim ("</" escaped, so a claim cannot close
    its block), then each cited unit once, in first-cited order. Filled in one pass, so a placeholder
    written inside a claim stays as written."""
    ledger = _ledger(skills)
    lines, cited = [], []
    for cid, text, ids in claims:
        lines.append(_line({"id": cid, "text": text, "cites": list(ids),
                            "ledger": list(ledger_lines(text, ledger, allow))}))
        cited += [u for u in ids if u not in cited]
    ulines = [evidence_line(u, units[u]).replace("</", "<\\/") for u in cited]
    fill = {"claims": "\n".join(lines), "units": "\n".join(ulines)}
    return re.sub(r"\{(claims|units)\}", lambda m: fill[m.group(1)], TEMPLATE.read_text(encoding="utf-8"))


def _verdict(value):
    """A reply's verdict, or None: an object with a "verdict", or a bare verdict string."""
    if isinstance(value, dict):
        value = value.get("verdict")
    return value if isinstance(value, str) and value in VERDICTS else None


def judge_claims(key, claims, units, skills, model, runner, ledger, allow=frozenset()):
    """One ledgered call for `claims` ([(local id, text, cited ids)]). Returns ({local id: verdict or None},
    AskResult); the map is empty when the call failed, and all None when the reply did not parse."""
    res = ask_json(key, build_support_prompt(claims, units, skills, allow), model, runner, ledger, dict)
    note = getattr(runner, "note", None)  # steps.Gate counts failures in a row
    if note is not None:
        note(res.ok, res.error)
    if res.ok:
        return {cid: _verdict(res.value.get(cid)) for cid, _, _ in claims}, res
    if res.error == "parse":
        return {cid: None for cid, _, _ in claims}, res
    return {}, res


# ---- answer records -----------------------------------------------------------

def _entries(rec: dict):
    """(key, raw claim, verdict or None) for every raw claim, numbered across the turns in order."""
    n = 0
    for _, t in turns(rec):
        for e in t.get("claims") or []:
            e = e if isinstance(e, dict) else {}
            raw = e.get("raw") if isinstance(e.get("raw"), dict) else {}
            v = e.get("verdict") if isinstance(e.get("verdict"), dict) else None
            yield f"{rec['item_id']}#{n}", raw, v
            n += 1


def _as_judged(raw: dict, v, units=None):
    """(text, cited ids) as the judge sees a claim: the gated text and the ids the gates kept (what a
    visitor sees); when the gates kept no id (or did not run), the raw ids. Given `units`, only ids in it.
    A kept or grounding-only claim always has gate-kept ids, so rates can fingerprint it without `units`."""
    def known(ids):
        return [u for u in ids or [] if isinstance(u, str) and (units is None or u in units)]

    claim = v.get("claim") if v and isinstance(v.get("claim"), dict) else raw
    text = claim.get("text")
    ids = known(claim.get("units")) if v else []
    if not ids and units is not None:
        ids = known(raw.get("units"))
    return (text if isinstance(text, str) else ""), tuple(dict.fromkeys(ids))


def claim_rows(rec: dict, units: dict) -> list[dict]:
    """The claims of one record the judge reads: text and >= 1 cited corpus unit."""
    out = []
    for key, raw, v in _entries(rec):
        text, ids = _as_judged(raw, v, units)
        if text.strip() and ids:
            out.append({"key": key, "text": text, "units": ids, "claim_sha": claim_sha(text, ids)})
    return out


def judge_answers(records, units, skills, judge, runner, ledger, sample=False, allow=frozenset(),
                  workers: int = 4) -> dict[str, dict]:
    """{"<item_id>#<n>": verdict row} for every judgeable claim of `records`, one call per record. With
    `sample`, only items in_b_sample. `allow` is the honesty allow list, so the ledger lines are the ones the
    answer model saw."""
    model, tsha = JUDGES[judge], template_sha()
    jobs = []
    for rec in sorted(records, key=lambda r: r["item_id"]):
        rows = claim_rows(rec, units)
        if rows and (not sample or in_b_sample(rec["item_id"])):
            jobs.append((rec, rows))

    def one(job):
        rec, rows = job
        local = [(f"c{i}", r["text"], r["units"]) for i, r in enumerate(rows, 1)]
        got, res = judge_claims(f"support{judge}:{rec['item_id']}", local, units, skills, model, runner, ledger,
                                allow)
        return {r["key"]: {"verdict": got[cid], "item_id": rec["item_id"], "split": rec.get("split"),
                           "judge": judge, "model": model, "prompt_sha": res.prompt_sha, "template_sha": tsha,
                           "claim_sha": r["claim_sha"]}
                for (cid, _, _), r in zip(local, rows) if cid in got}

    out: dict[str, dict] = {}
    for part in _map(one, jobs, workers):
        out.update(part)
    return dict(sorted(out.items()))


# ---- rates ----------------------------------------------------------------------

def _current(verdicts: dict, key: str, raw: dict, v) -> str | None:
    """The verdict for this claim as it now stands, or None (none, a bad one, or one for an older claim)."""
    row = verdicts.get(key)
    if not isinstance(row, dict) or row.get("verdict") not in VERDICTS:
        return None
    if row.get("claim_sha") != claim_sha(*_as_judged(raw, v)):
        return None
    return row["verdict"]


def _boot(values: dict):
    return cluster_bootstrap(values) if any(values.values()) else None


def rates(verdicts: dict, records) -> dict:
    """support_rate and partial_rate over kept claims with a current verdict, with cluster-bootstrap CIs
    (cluster = item); grounding_false_drop_rate: of the claims dropped by grounding alone, the share the
    judge calls supported. A rate with nothing to count is None."""
    sup: dict = {}
    part: dict = {}
    n_kept = n_g = n_g_unjudged = g_supported = 0
    for rec in records:
        for key, raw, v in _entries(rec):
            if not v:
                continue
            said = _current(verdicts, key, raw, v)
            if v.get("kept"):
                n_kept += 1
                if said is not None:
                    sup.setdefault(rec["item_id"], []).append(1.0 if said == "supported" else 0.0)
                    part.setdefault(rec["item_id"], []).append(1.0 if said == "partial" else 0.0)
            elif list(v.get("reasons") or []) == ["grounding"]:
                if said is None:
                    n_g_unjudged += 1
                    continue
                n_g += 1
                g_supported += said == "supported"
    judged = sum(len(x) for x in sup.values())
    return {"n_records": len(records), "n_kept": n_kept, "n_kept_judged": judged, "n_kept_unjudged": n_kept - judged,
            "support_rate": _boot(sup), "partial_rate": _boot(part), "n_grounding_only": n_g,
            "n_grounding_only_unjudged": n_g_unjudged,
            "grounding_false_drop_rate": g_supported / n_g if n_g else None}


def kappa(a: dict, b: dict) -> dict:
    """Cohen's kappa between two judges on supported-vs-not, over the claims both judged as the same claim."""
    keys = [k for k in sorted(set(a) & set(b))
            if (a[k] or {}).get("verdict") in VERDICTS and (b[k] or {}).get("verdict") in VERDICTS
            and a[k].get("claim_sha") == b[k].get("claim_sha")]
    if not keys:
        return {"kappa": None, "n_pairs": 0}
    return {"kappa": cohen_kappa({k: a[k]["verdict"] == "supported" for k in keys},
                                 {k: b[k]["verdict"] == "supported" for k in keys}), "n_pairs": len(keys)}
