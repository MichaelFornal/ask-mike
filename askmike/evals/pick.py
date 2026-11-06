"""Mike's pick packet: the per-model metric table on the report split, ten report answers from each model side
by side, the fallback page and the subject map, then the open questions for Mike when there are any. Written to
the private $ASK_PRIVATE_DIR/work/answers/pick.md (it shows postings' requirements, recruiter questions and
rendered answers); nothing here is printed or tracked. The open questions are private too: the CLI reads them from
$ASK_PRIVATE_DIR/work/answers/open_questions.md (OPEN_QUESTIONS_NAME), Markdown kept as written.

The ten items are drawn by sha1("pick:" + item id) from report-split items every model has a record for,
so the draw is the same for any order of the inputs and for every rerun of the same records.
"""
from __future__ import annotations

import hashlib
import math
import re
from statistics import median

from askmike.answer.evidence import req_key
from askmike.evals import support as sup
from askmike.evals.answer_run import turns

DRAW = (("jd", 4), ("evidence", 3), ("gap", 1), ("personal", 1), ("injection", 1))
SALT = "pick:"
GATE_ORDER = ("structure", "grounding", "honesty", "slop", "length", "privacy", "voice", "verdict")

TABLE_TITLE = "Metrics, report split"
ITEMS_TITLE = "Ten report-split answers, side by side"
FALLBACK_TITLE = "The fallback page"
SUBJECTS_TITLE = "The subject map"
QUESTIONS_TITLE = "Open questions for Mike"
OPEN_QUESTIONS_NAME = "open_questions.md"  # beside pick.md, under $ASK_PRIVATE_DIR/work/answers


# ---- the numbers ------------------------------------------------------------

def _p90(xs: list) -> float:
    s = sorted(xs)
    return s[max(0, math.ceil(0.9 * len(s)) - 1)]


def latency(records) -> dict | None:
    """Wall time of every turn that made a model call (usage recorded), median and nearest-rank p90."""
    ms = [t["ms"] for r in records for _, t in turns(r) if t.get("usage") and isinstance(t.get("ms"), int)]
    return {"median": median(ms), "p90": _p90(ms), "n": len(ms)} if ms else None


def report_summary(answers: dict, records, verdicts_A: dict, verdicts_B: dict, audit_hits: int) -> dict:
    """One model's packet numbers on the report split. `answers` is summarise()'s report dict; `records`
    that model's report-split records; verdicts are the support_<judge>.jsonl rows keyed by claim key.
    Judge A covers every record, judge B its salted sample; kappa reads the report claims both judged."""
    records = list(records)
    sample = [r for r in records if sup.in_b_sample(r["item_id"])]

    def report(v):
        return {k: row for k, row in v.items() if (row or {}).get("split") == "report"}

    return {"answers": answers, "audit_hits": audit_hits, "support_A": sup.rates(verdicts_A, records),
            "support_B": sup.rates(verdicts_B, sample) if verdicts_B else None,
            "kappa_AB": sup.kappa(report(verdicts_A), report(verdicts_B)) if verdicts_B else None,
            "latency_ms": latency(records)}


# ---- the draw ---------------------------------------------------------------

def _order(item_id: str) -> str:
    return hashlib.sha1((SALT + item_id).encode("utf-8")).hexdigest()


def draw(items, records_by_model: dict) -> list:
    """DRAW's counts per set, from report-split items with a record in every model, lowest sha1 first."""
    have = [set(recs) for recs in records_by_model.values()]
    out = []
    for set_, n in DRAW:
        pool = [i for i in items if i.set == set_ and i.split == "report" and all(i.item_id in h for h in have)]
        out += sorted(pool, key=lambda i: _order(i.item_id))[:n]
    return out


# ---- Markdown -----------------------------------------------------------------

def _fence(text: str) -> str:
    """`text` exactly as written, inside a backtick fence longer than any backtick run in it."""
    run = max((len(m) for m in re.findall(r"`+", text)), default=0)
    bar = "`" * max(4, run + 1)
    return f"{bar}\n{text}\n{bar}"


def _cell(text) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def _num(v) -> str:
    if v is None:
        return "n/a"
    return f"{v:.3f}" if isinstance(v, float) else str(v)


def _ci(r) -> str:
    """A cluster-bootstrap summary as mean [lo, hi] (claims n, items k)."""
    if not r:
        return "n/a"
    return f"{r['mean']:.3f} [{r['lo']:.3f}, {r['hi']:.3f}] (n={r['n_items']}, items={r['n_clusters']})"


def _ids(xs) -> str:
    return ", ".join(xs) if xs else "none"


def _rows(summaries: dict) -> list[tuple[str, list[str]]]:
    def each(f):
        return [f(s or {}) for s in summaries.values()]

    def a(s):
        return s.get("answers") or {}

    def sa(s):
        return s.get("support_A") or {}

    def gates(s):
        x = a(s)
        return " / ".join(str(x.get(f"pregate_{g}_claims", "n/a")) for g in GATE_ORDER)

    def rate_with(s, key, ids_key, label):
        x = a(s)
        return f"{_num(x.get(key))} ({label}: {_ids(x.get(ids_key))})"

    def kappa(s):
        k = s.get("kappa_AB")
        return "n/a" if not k or k.get("kappa") is None else f"{k['kappa']:.3f} (n={k['n_pairs']})"

    def lat(s):
        x = s.get("latency_ms")
        return "n/a" if not x else f"{x['median']:.0f} / {x['p90']:.0f} (turns={x['n']})"

    def fallback(s):
        x = a(s)
        reasons = ", ".join(f"{k} {v}" for k, v in (x.get("fallback_reasons") or {}).items())
        return _num(x.get("fallback_rate")) + (f" ({reasons})" if reasons else "")

    return [
        ("items: answered / missing / stale", each(lambda s: f"{a(s).get('n_answers', 'n/a')} of "
                                                          f"{a(s).get('n_items', 'n/a')} / {a(s).get('n_missing', 'n/a')}"
                                                          f" / {a(s).get('n_stale', 'n/a')}")),
        ("audit hits (zero-bar)", each(lambda s: _num(s.get("audit_hits")))),
        ("handoff_correct_rate (zero-bar)", each(lambda s: rate_with(s, "handoff_correct_rate", "handoff_missed_ids",
                                                                      "missed"))),
        ("injection_canary_hits, rendered (zero-bar)", each(lambda s: _num(a(s).get("injection_canary_hits")))),
        ("injection_canary_hits_raw (before the gates)", each(lambda s: _num(a(s).get("injection_canary_hits_raw")))),
        ("false_handoff_rate", each(lambda s: rate_with(s, "false_handoff_rate", "false_handoff_ids", "ids"))),
        ("fallback_rate (reasons)", each(fallback)),
        ("pregate_claims", each(lambda s: _num(a(s).get("pregate_claims")))),
        ("pre-gate violations, claims failing each gate: " + " / ".join(GATE_ORDER), each(gates)),
        ("drops_per_answer", each(lambda s: _num(a(s).get("drops_per_answer")))),
        ("coverage [95% CI]", each(lambda s: _ci(a(s).get("coverage")))),
        ("support_rate (judge A) [95% CI]", each(lambda s: _ci(sa(s).get("support_rate")))),
        ("partial_rate (judge A) [95% CI]", each(lambda s: _ci(sa(s).get("partial_rate")))),
        ("kept claims: total / unjudged by A", each(lambda s: f"{sa(s).get('n_kept', 'n/a')} / "
                                                           f"{sa(s).get('n_kept_unjudged', 'n/a')}")),
        ("grounding_false_drop_rate (judge A; grounding-only drops)",
         each(lambda s: f"{_num(sa(s).get('grounding_false_drop_rate'))} (n={sa(s).get('n_grounding_only', 'n/a')})")),
        ("support_rate (judge B, sample) [95% CI]", each(lambda s: _ci((s.get("support_B") or {}).get("support_rate")))),
        ("kappa A-B, supported vs not", each(kappa)),
        ("gap_pivot_rate", each(lambda s: _num(a(s).get("gap_pivot_rate")))),
        ("personal_correct_rate", each(lambda s: _num(a(s).get("personal_correct_rate")))),
        ("offtopic_correct_rate", each(lambda s: _num(a(s).get("offtopic_correct_rate")))),
        ("followup_answered_rate", each(lambda s: _num(a(s).get("followup_answered_rate")))),
        ("ms per model turn, median / p90 (claude -p wall time)", each(lat)),
    ]


def _table(summaries: dict) -> list[str]:
    models = list(summaries)
    out = ["| metric | " + " | ".join(models) + " |", "|---|" + "---|" * len(models)]
    out += [f"| {name} | " + " | ".join(_cell(v) for v in vals) + " |" for name, vals in _rows(summaries)]
    return out


def _input(item) -> list[str]:
    if item.set == "jd":
        lines = [f"- {req_key(r.id)} ({'required' if r.required else 'nice to have'}, {r.kind}): {r.text}"
                 for r in item.requirements]
        return ["Requirements (the posting itself is not shown):", "", *(lines or ["- none extracted"])]
    out = ["Input:", "", _fence(item.text)]
    if item.set == "gap" and item.expected:
        out += ["", f"Ledger gap term: {', '.join(item.expected)}"]
    if item.set == "personal" and item.expected:
        out += ["", f"Gold units: {', '.join(item.expected)}"]
    return out


def _answer(model: str, rec: dict) -> list[str]:
    reason = rec.get("fallback_reason")
    head = f"**{model}**: outcome {rec.get('outcome')}" + (f" ({reason})" if reason else "")
    return [head, "", _fence(rec.get("rendered") or "")]


def _subjects(subjects: dict) -> list[str]:
    groups: dict = {}
    for raw, shown in subjects.items():
        groups.setdefault(shown, []).append(raw)
    out = [f"{len(subjects)} corpus subjects map to {len(groups)} display names (askmike/answer/subjects.json).", "",
           "| display name | corpus subjects |", "|---|---|"]
    out += [f"| {_cell(shown)} | " + "; ".join(_cell(r) for r in sorted(groups[shown])) + " |"
            for shown in sorted(groups)]
    return out


def packet(records_by_model: dict, items, summaries: dict, fallback: str, subjects: dict,
           questions: str | None = None) -> str:
    """The packet as Markdown. `records_by_model` is {model: {item_id: record}}, in column order; `items` the
    answer eval items (only the report split is drawn); `summaries` {model: report_summary()}; `fallback`
    the fallback page as render_text shows it; `subjects` subjects.json; `questions` the open questions as
    Markdown, written as given under their own heading, last (no text, no section)."""
    models = list(records_by_model)
    drawn = draw(items, records_by_model)
    counts = ", ".join(f"{n} {s}" for s, n in DRAW)
    out = ["# Ask Mike: model pick packet (private)", "",
           (f"Models: {', '.join(models)}. Every number below reads the report split; the answers are the "
            "rendered text a visitor would see, exactly as written."), "",
           f"## {TABLE_TITLE}", "", *_table({m: summaries.get(m) for m in models}), "",
           f"## {ITEMS_TITLE}", "",
           f"Drawn by sha1(\"{SALT}\" + item id) from report-split items both models answered: {counts}.", ""]
    for n, item in enumerate(drawn, 1):
        out += [f"### {n}. {item.set} · {item.item_id}", "", *_input(item), ""]
        for m in models:
            out += [*_answer(m, records_by_model[m][item.item_id]), ""]
    out += [f"## {FALLBACK_TITLE}", "",
            ("Shown when no claim survives the gates (askmike/answer/fallback.json: the intro and its unit ids), "
             "as render_text shows it:"), "", _fence(fallback), "",
            f"## {SUBJECTS_TITLE}", "", *_subjects(subjects)]
    if questions and questions.strip():
        out += ["", f"## {QUESTIONS_TITLE}", "", questions.strip()]
    return "\n".join(out) + "\n"
