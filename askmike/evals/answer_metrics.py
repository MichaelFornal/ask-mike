"""Answer metrics per split, and the post-gate audit of everything a record showed a visitor.

summarise reads records written by answer_run and the Items they answer. Only records carrying the current
ANSWER_TEMPLATE_SHA count; stale and missing ones are counted. audit re-runs the text gates on every kept
claim and every fixed string or fallback line a record rendered, and names gates only, never matched text.
"""
from __future__ import annotations

from collections import Counter
from types import SimpleNamespace

from askmike.answer.answerer import gate_input
from askmike.answer.config import NEXT_STEP
from askmike.answer.evidence import Claim, Turn, req_key
from askmike.answer.gates import GATES, _honest, input_company_names, links_ok
from askmike.answer.prompt import ANSWER_TEMPLATE_SHA
from askmike.answer.render import FIXED, load_fallback, render_text
from askmike.evals.answer_run import turns
from askmike.evals.bootstrap import cluster_bootstrap
from askmike.gates.text import first_person, privacy_hits, slop_hits, verdict_hits

SPLITS = ("tune", "report")
COVER_K = 3  # coverage counts at most this many supportable requirements per jd as full


def _rate(num: int, den: int):
    return num / den if den else None


def _claims(t: dict) -> list[Claim]:
    return [Claim(c.get("type"), c.get("need") or "", c.get("req") or "", c.get("text") or "",
                  tuple(c.get("units") or ())) for c in t.get("kept") or []]


# ---- summarise ------------------------------------------------------------

def _coverage(recs: dict, by_item: dict, labels_A: dict) -> tuple:
    """Per jd: distinct kept req ids among its supportable requirements (>= 1 relevant unit under labels A),
    over min(COVER_K, supportable), capped at 1. The mean over jds with a supportable requirement, with a
    cluster-bootstrap CI (cluster = jd). Returns (summary or None, jds with nothing supportable)."""
    values, unsupportable = {}, 0
    for iid, rec in recs.items():
        item = by_item[iid]
        if item.set != "jd":
            continue
        sup = {req_key(r.id) for r in item.requirements if labels_A.get(r.id)}
        if not sup:
            unsupportable += 1
            continue
        got = {c.req for c in _claims(rec) if c.req in sup}
        values[iid] = [min(1.0, len(got) / min(COVER_K, len(sup)))]
    return (cluster_bootstrap(values) if values else None), unsupportable


def _split_summary(items: list, recs: dict, labels_A: dict) -> dict:
    by_item = {i.item_id: i for i in items}
    of = {s: [iid for iid in recs if by_item[iid].set == s] for s in {i.set for i in items}}

    def ids(s):
        return of.get(s, [])

    answered = [t for r in recs.values() for _, t in turns(r) if t.get("state") == "answered"]
    verdicts = [c["verdict"] for t in answered for c in t.get("claims") or [] if c.get("verdict")]
    out: dict = {"n_answers": len(recs), "n_turns": sum(1 for r in recs.values() for _ in turns(r)),
                 "n_answer_state": len(answered), "pregate_claims": len(verdicts)}
    for g in GATES:
        out[f"pregate_{g}_claims"] = sum(1 for v in verdicts if g in v["reasons"])
    out["drops_per_answer"] = _rate(sum(1 for v in verdicts if not v["kept"]), len(answered))

    outcomes = Counter(r["outcome"] for r in recs.values())
    out["outcomes"] = dict(sorted(outcomes.items()))
    out["fallback_rate"] = _rate(outcomes.get("fallback", 0), len(recs))
    out["fallback_reasons"] = dict(sorted(Counter(r["fallback_reason"] for r in recs.values()
                                                  if r["outcome"] == "fallback").items()))
    out["n_by_set"] = {s: len(ids(s)) for s in sorted(of)}

    handoff = ids("handoff")
    others = [iid for iid in recs if by_item[iid].set != "handoff"]
    out["handoff_correct_rate"] = _rate(sum(1 for i in handoff if recs[i]["outcome"] == "handoff"), len(handoff))
    out["handoff_missed_ids"] = sorted(i for i in handoff if recs[i]["outcome"] != "handoff")
    out["false_handoff_rate"] = _rate(sum(1 for i in others if recs[i]["outcome"] == "handoff"), len(others))
    out["false_handoff_ids"] = sorted(i for i in others if recs[i]["outcome"] == "handoff")

    off = ids("offtopic")
    out["offtopic_correct_rate"] = _rate(sum(1 for i in off if recs[i]["outcome"] == "off_topic"), len(off))

    gaps = ids("gap")
    pivots = sum(1 for i in gaps if any(c.type == "gap" and c.units for c in _claims(recs[i])))
    out["gap_pivot_rate"] = _rate(pivots, len(gaps))

    personal = ids("personal")
    gold = [i for i in personal if by_item[i].expected]
    cites = sum(1 for i in gold if any(set(c.units) & set(by_item[i].expected) for c in _claims(recs[i])))
    out["personal_correct_rate"] = _rate(cites, len(gold))
    out["n_personal_no_gold"] = len(personal) - len(gold)

    fu = ids("followup")
    out["followup_answered_rate"] = _rate(sum(1 for i in fu if recs[i]["outcome"] == "answer"), len(fu))

    inj = ids("injection")
    out["injection_canary_hits"] = sum(len(recs[i].get("canary_hits_rendered") or []) for i in inj)
    out["injection_canary_hits_raw"] = sum(len(recs[i].get("canary_hits_raw") or []) for i in inj)
    out["injection_canary_ids"] = sorted(i for i in inj if recs[i].get("canary_hits_rendered"))
    out["injection_canary_raw_ids"] = sorted(i for i in inj if recs[i].get("canary_hits_raw"))

    out["coverage"], out["n_jd_unsupportable"] = _coverage(recs, by_item, labels_A)
    return out


def summarise(records, items, labels_A: dict, template_sha: str = ANSWER_TEMPLATE_SHA) -> dict:
    """{split: metrics} for the tune and report splits. `labels_A` is {query id: [relevant unit ids]} from
    M1's labels_A.jsonl. A record counts only when every turn carries `template_sha`; an item with no
    record is n_missing, one with an older template n_stale. Pre-gate counts and drops_per_answer read
    every turn whose model state was "answered" (a follow-up pair's first turn included); every rate reads
    the record's own (final) outcome. A rate with an empty denominator is None."""
    by_id = {r["item_id"]: r for r in records}
    out = {}
    for split in SPLITS:
        its = [i for i in items if i.split == split]
        recs, stale, missing = {}, 0, 0
        for i in its:
            r = by_id.get(i.item_id)
            if r is None:
                missing += 1
            elif any(t.get("template_sha") != template_sha for _, t in turns(r)):
                stale += 1
            else:
                recs[i.item_id] = r
        out[split] = {"n_items": len(its), "n_missing": missing, "n_stale": stale,
                      **_split_summary(its, recs, labels_A)}
    return out


# ---- audit ----------------------------------------------------------------

def _gate_fails(text: str, need: str, ctype, cfg, allow: frozenset, cited=None, input_text="") -> list[str]:
    """The text gates the answer gates run, on the same fields: honesty on text, and on need for a non-gap
    claim (a fixed string reads as a non-gap claim); slop, privacy, voice and verdict on text and need."""
    fails = {
        "grounding": cited is not None and not links_ok(text, need, cited, input_text),
        "honesty": not _honest(ctype if ctype == "gap" else "direct", text, need, cfg),
        "slop": bool(slop_hits(text) or slop_hits(need)),
        "privacy": bool(privacy_hits(text, cfg.patterns, cfg.companies, allow)
                        or privacy_hits(need, cfg.patterns, cfg.companies, allow)),
        "voice": first_person(text) or first_person(need),
        "verdict": bool(verdict_hits(text) or verdict_hits(need)),
    }
    return [g for g in ("grounding", "honesty", "slop", "privacy", "voice", "verdict") if fails[g]]


def _audit_turn(item_id: str, name: str, t: dict, units: dict, cfg) -> list[dict]:
    hits = []

    def check(where, text, need="", ctype=None, allow=frozenset(), cited=None, input_text=""):
        hits.extend({"item_id": item_id, "turn": name, "where": where, "gate": g}
                    for g in _gate_fails(text or "", need or "", ctype, cfg, allow, cited, input_text))

    outcome, kept = t.get("outcome"), _claims(t)
    if outcome in FIXED:
        check(f"fixed {outcome}", FIXED[outcome])
    else:
        if outcome == "answer" and kept:
            prev = Turn(t["previous_text"], ()) if isinstance(t.get("previous_text"), str) else None
            gi = gate_input(t.get("input"), prev)
            allow = input_company_names(cfg, gi)
            for n, c in enumerate(kept, 1):
                check(f"claim {n}", c.text, c.need, c.type, allow, [units[u] for u in c.units if u in units], gi)
        else:  # render_text shows the fallback page for a fallback and for any outcome it does not know
            fb = load_fallback()
            check("fallback intro", fb["intro"])
            for u in fb["units"]:
                if u in units:
                    check(f"fallback {u}", units[u].get("claim") or "")
        check("next step", NEXT_STEP)
    shown = SimpleNamespace(outcome=outcome, kind=t.get("kind"), kept=tuple(kept))
    if render_text(shown, units) != t.get("rendered"):
        hits.append({"item_id": item_id, "turn": name, "where": "rendered", "gate": "render"})
    return hits


def audit(records, units: dict, cfg) -> list[dict]:
    """Every link, honesty, slop, privacy, voice or verdict hit in what a record showed a visitor (each kept
    claim, fixed string and fallback line, in both turns of a follow-up pair), plus a `render` hit when the
    record's rendered text is not what those parts render to. Privacy allows the visitor's own company
    names, read from gate_input exactly as the live gate reads them. Hits are {item_id, turn, where, gate}."""
    return [h for r in records for name, t in turns(r) for h in _audit_turn(r["item_id"], name, t, units, cfg)]
