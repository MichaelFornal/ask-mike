"""Eval pipeline: ASK_PRIVATE_DIR=... uv run python -m askmike.evals sets. Prints counts only.

M2 steps: `kinds` (judge A's question kinds), `answers --model M --split S` (answer every item through
claude -p, then print the split's metrics), `audit --model M` (re-gate what the records showed; exit 1
on any hit), `support [--model M] --judge A|B [--records PATH]` (the claim-support judge), `judge-calib`
(that judge on the planted set; exit 1 unless it passes), `pick` (Mike's private pick packet,
work/answers/pick.md), `calibrate --model M|picked` (the paid API calibration; `picked` is Mike's per-route pick
from askmike/answer/settings.json), `drift` (re-extraction with the picked jd model, only when it is not M1's
extractor), `facts-m2 [--check]` (the m2_ facts; refuses on a failed bar, a stale record or an incomplete
calibration) and `report-m2` (docs/m2/REPORT.md from facts.json). They print counts, rates, gate names and item
ids, never question, posting or answer text.

M3 step: `facts-m3 [--check]` (the m3_ facts: the month budget and the caps the box enforces, from
deploy/budget.json; it needs no private files).
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from askmike.answer.config import ANSWER_MODELS, MODEL_SLUGS
from askmike.evals.answer_sets import KINDS, KindsIncomplete
from askmike.evals.sets import build_sets
from askmike.evals.steps import ExtractIncomplete
from askmike.private import PrivateDirError, raw_dir, work_dir
from askmike.retrieval.index import StaleIndex

REFUSALS = (ExtractIncomplete, StaleIndex)  # printed to stderr, exit 1

REPO = Path(__file__).resolve().parents[2]
CORPUS = REPO / "corpus"
FACTS = REPO / "facts.json"
REPORT_M1 = REPO / "docs" / "m1" / "REPORT.md"
REPORT_M2 = REPO / "docs" / "m2" / "REPORT.md"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="askmike.evals")
    sub = ap.add_subparsers(dest="step", required=True)
    sub.add_parser("sets", help="build the eval sets under $ASK_PRIVATE_DIR/work/evals/sets")
    for name in ("extract", "pool", "judge"):
        p = sub.add_parser(name)
        if name != "pool":
            p.add_argument("--workers", type=int, default=4)
            p.add_argument("--max-seconds", type=float, default=None)
        if name == "judge":
            p.add_argument("--judge", choices=("A", "B"), required=True)
            p.add_argument("--sample", action="store_true")
    pr = sub.add_parser("run", help="score R1-R4 and KW on every set; writes work/evals/results/{A|B}/")
    pr.add_argument("--labels", choices=("A", "B"), required=True)
    sub.add_parser("tune", help="grid depth x rrf_k0 on tune-split jdreq (labels A); writes retrieval/settings.json")
    pk = sub.add_parser("kinds", help="judge A labels every recruiter question; writes work/answers/kinds_A.jsonl")
    pa = sub.add_parser("answers", help="answer the eval items through claude -p; writes work/answers/<slug>/")
    pa.add_argument("--model", choices=ANSWER_MODELS, required=True)
    pa.add_argument("--split", choices=("tune", "report", "all"), required=True)
    for p in (pk, pa):
        p.add_argument("--workers", type=int, default=4)
        p.add_argument("--max-seconds", type=float, default=None)
    pu = sub.add_parser("audit", help="re-gate everything the answer records showed; exit 1 on any hit")
    pu.add_argument("--model", choices=ANSWER_MODELS, required=True)
    ps = sub.add_parser("support", help="judge each claim against its cited units; writes support_<judge>.jsonl "
                                        "beside the records")
    ps.add_argument("--model", choices=ANSWER_MODELS, default=None,
                    help="whose records: needed without --records; with it, every record must be this model's "
                         "(without it, each record names its own answer model, as the picked calibration's do)")
    ps.add_argument("--judge", choices=("A", "B"), required=True)
    ps.add_argument("--records", default=None,
                    help="a records.jsonl under $ASK_PRIVATE_DIR (relative paths start there); "
                         "default work/answers/<slug>/records.jsonl")
    pc = sub.add_parser("judge-calib", help="the support judge on the planted set; writes work/answers/calibration/; "
                                            "exit 1 unless it passes")
    pc.add_argument("--judge", choices=("A", "B"), default="A")
    for p in (ps, pc):
        p.add_argument("--workers", type=int, default=4)
        p.add_argument("--max-seconds", type=float, default=None)
    sub.add_parser("pick", help="Mike's private pick packet from both models' report records; writes "
                                "work/answers/pick.md (refuses a missing or stale report record)")
    pq = sub.add_parser("calibrate", help="~40 tune items through the real API vs claude -p (needs ANTHROPIC_API_KEY); "
                                          "--summarise compares the two and needs no key")
    pq.add_argument("--model", choices=(*ANSWER_MODELS, "picked"), required=True,
                    help="one model, or `picked`: each item on its own route's model from askmike/answer/settings.json")
    pq.add_argument("--n", type=int, default=40)
    pq.add_argument("--max-usd", type=float, default=3.00)
    pq.add_argument("--summarise", action="store_true")
    pf = sub.add_parser("facts", help="write the m1_ facts to facts.json from work/evals and the corpus")
    pf.add_argument("--check", action="store_true", help="recompute and exit 1 on drift; writes nothing")
    sub.add_parser("report", help="render docs/m1/REPORT.md from facts.json (needs no private files)")
    pd = sub.add_parser("drift", help="re-extract the job descriptions through claude -p with the picked jd model, "
                                      "only when it is not M1's EXTRACT_MODEL; writes work/evals/drift/<slug>/")
    pd.add_argument("--workers", type=int, default=4)
    pd.add_argument("--max-seconds", type=float, default=None)
    pm = sub.add_parser("facts-m2", help="write the m2_ facts to facts.json from work/answers and work/evals; "
                                         "refuses on a failed bar, a stale record or an incomplete calibration")
    pm.add_argument("--check", action="store_true", help="recompute and exit 1 on drift; writes nothing")
    sub.add_parser("report-m2", help="render docs/m2/REPORT.md from facts.json (needs no private files)")
    p3 = sub.add_parser("facts-m3", help="write the m3_ facts (the month budget and the caps) to facts.json from "
                                         "deploy/budget.json (needs no private files)")
    p3.add_argument("--check", action="store_true", help="recompute and exit 1 on drift; writes nothing")
    args = ap.parse_args(argv)
    if args.step == "calibrate" and not args.summarise and not os.environ.get("ANTHROPIC_API_KEY"):
        print("calibrate: ANTHROPIC_API_KEY is not set; refusing before any client or call", file=sys.stderr)
        return 2
    if args.step == "report":
        return _report()
    if args.step == "report-m2":
        return _report_m2()
    if args.step == "facts-m3":
        return _facts_m3(args.check)
    if args.step == "drift":
        return _drift(args)
    evals = work_dir() / "evals"
    if args.step == "facts":
        return _facts(evals, args.check)
    if args.step in ANSWER_STEPS:
        try:
            return ANSWER_STEPS[args.step](args, evals, work_dir() / "answers")
        except (KindsIncomplete, StaleIndex, PrivateDirError, FileNotFoundError, ValueError) as e:
            print(f"{args.step}: {e}", file=sys.stderr)  # messages name files, ids and counts only
            return 1
    if args.step != "sets":
        try:
            if args.step == "tune":
                return _tune(evals)
            if args.step == "run":
                return _run_eval(args.labels, evals)
            return _run_step(args, evals)
        except REFUSALS as e:
            print(f"{args.step}: {e}", file=sys.stderr)
            return 1
    qmap = evals / "question_map.json"
    st = build_sets(raw_dir(), CORPUS, evals / "sets", question_map=qmap)
    for name, n in st.rows.items():
        s = st.splits[name]
        print(f"{name}: {n} rows (tune {s['tune']}, report {s['report']})")
    print("jd families: " + ", ".join(f"{k}={v}" for k, v in sorted(st.jd_families.items())))
    print("question kinds: " + ", ".join(f"{k}={v}" for k, v in sorted(st.question_kinds.items())))
    print(f"personal questions: real={st.personal_real}, synthetic={st.personal_synthetic}")
    print(f"questions from the map: {st.map_questions}" + (" (map missing)" if st.map_missing else ""))
    return 0


def _facts(evals, check_only: bool) -> int:
    import json

    from askmike.facts.build import PREFIX, build_m1, check

    if check_only:
        drift = check(evals, REPO, FACTS)
        for key in drift:
            print(f"drift: {key}")
        print(f"facts --check: {len(drift)} m1 fact(s) differ from facts.json")
        return 1 if drift else 0
    facts = build_m1(evals, REPO)
    old = json.loads(FACTS.read_text(encoding="utf-8")) if FACTS.exists() else {}
    merged = {k: v for k, v in old.items() if not k.startswith(PREFIX)} | facts
    part = FACTS.with_name(FACTS.name + ".part")
    part.write_text(json.dumps(dict(sorted(merged.items())), indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    part.replace(FACTS)
    print(f"facts: {len(facts)} m1 facts written to {FACTS.name}")
    return 0


def _facts_m3(check_only: bool) -> int:
    """The m3_ facts merged into facts.json, every other key kept; --check exits 1 on drift."""
    import json

    from askmike.facts.build_m3 import PREFIX, FactsRefused, build_m3, check

    try:
        if check_only:
            drift = check(REPO, FACTS)
            for key in drift:
                print(f"drift: {key}")
            print(f"facts-m3 --check: {len(drift)} m3 fact(s) differ from facts.json")
            return 1 if drift else 0
        facts = build_m3(REPO)
    except FactsRefused as e:
        print(f"facts-m3: refused: {e}", file=sys.stderr)
        return 1
    old = json.loads(FACTS.read_text(encoding="utf-8")) if FACTS.exists() else {}
    merged = {k: v for k, v in old.items() if not k.startswith(PREFIX)} | facts
    part = FACTS.with_name(FACTS.name + ".part")
    part.write_text(json.dumps(dict(sorted(merged.items())), indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    part.replace(FACTS)
    print(f"facts-m3: {len(facts)} m3 facts written to {FACTS.name}")
    return 0


def _report() -> int:
    import json

    from askmike.facts.report import render

    if not FACTS.exists():
        print("facts.json missing; run the facts step first", file=sys.stderr)
        return 2
    REPORT_M1.parent.mkdir(parents=True, exist_ok=True)
    REPORT_M1.write_text(render(json.loads(FACTS.read_text(encoding="utf-8"))), encoding="utf-8")
    print(f"report: wrote {REPORT_M1.relative_to(REPORT_M1.parents[2])}")
    return 0


def _report_m2() -> int:
    import json

    from askmike.facts.report_m2 import render

    if not FACTS.exists():
        print("facts.json missing; run the facts and facts-m2 steps first", file=sys.stderr)
        return 2
    try:
        text = render(json.loads(FACTS.read_text(encoding="utf-8")))
    except KeyError as e:
        print(f"report-m2: facts.json has no {e}; run the facts-m2 step first", file=sys.stderr)
        return 2
    REPORT_M2.parent.mkdir(parents=True, exist_ok=True)
    REPORT_M2.write_text(text, encoding="utf-8")
    print(f"report-m2: wrote {REPORT_M2.relative_to(REPORT_M2.parents[2])}")
    return 0


def _drift(args) -> int:
    """Live extraction runs on the picked jd model. When that is M1's EXTRACT_MODEL the evals' requirements are what
    it would extract, so there is nothing to measure and no private file is read."""
    from askmike.answer.settings import load_answer_settings
    from askmike.retrieval.requirements import EXTRACT_MODEL

    try:
        jd = load_answer_settings().model_for("jd")
    except (FileNotFoundError, ValueError) as e:
        print(f"drift: {e}", file=sys.stderr)
        return 1
    if jd == EXTRACT_MODEL:
        print("drift: not needed (jd model equals EXTRACT_MODEL)")
        return 0
    from askmike.corpus.claude import run_claude
    from askmike.evals import drift
    from askmike.evals.steps import require_complete_extract

    try:
        evals = work_dir() / "evals"
        require_complete_extract(evals)  # M1's requirements are the baseline
        r = drift.run_drift(evals, run_claude, jd, args.workers, args.max_seconds)
    except (PrivateDirError, ExtractIncomplete, ValueError) as e:
        print(f"drift: {e}", file=sys.stderr)
        return 1
    print(f"drift {MODEL_SLUGS[jd]}: jds={r['n_jds']} extracted={r['n_extracted']} remaining={r['remaining']} "
          f"n_requirements_mean old={_fmt(r['n_requirements_mean_old'])} new={_fmt(r['n_requirements_mean_new'])} "
          f"matched_share={_fmt(r['matched_share'])}")
    return 0


def _run_eval(label, evals) -> int:
    import json

    from askmike.evals.keyword import keyword_rank
    from askmike.evals.run import RUNGS, SETS, run_labels
    from askmike.retrieval.index import default_db, open_index
    from askmike.retrieval.ladder import Ladder
    from askmike.retrieval.models import Embedder

    class CachedEmbedder:
        def __init__(self, inner):
            self.inner, self.cache = inner, {}

        def embed_query(self, q):
            if q not in self.cache:
                self.cache[q] = self.inner.embed_query(q)
            return self.cache[q]

    units = [json.loads(x) for x in (CORPUS / "units.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
    skills = json.loads((CORPUS / "skills.json").read_text(encoding="utf-8"))
    emb0 = Embedder()
    ladder = Ladder(open_index(default_db(), CORPUS / "units.jsonl", emb0.name), CachedEmbedder(emb0), skills)
    rungs = {r: (lambda q, r=r: [h.unit_id for h in ladder.run(r, q, 10)]) for r in RUNGS}
    rungs["KW"] = lambda q: [h.unit_id for h in keyword_rank(q, units, skills, 10)]
    res = run_labels(evals, label, rungs)
    for s in SETS:
        for rung, r in ((rg, res[(s, rg)]) for rg in (*RUNGS, "KW")):
            rep = r["by_split"]["report"]
            m = rep["recall_at_10"]
            line = (f"{label} {s} {rung}: recall@10 " + (f"{m['mean']:.3f} [{m['lo']:.3f}, {m['hi']:.3f}] n={m['n_items']}" if m else "n/a"))
            c = rep.get("coverage_at_10")
            if c:
                line += f" coverage@10 {c['mean']:.3f} [{c['lo']:.3f}, {c['hi']:.3f}] n_jd={c['n_clusters']}"
            jm = rep["judged_at_10_min"]
            line += f" unsupported={rep['n_unsupported']} unlabelled={rep['n_unlabelled']} judged_min=" + ("n/a" if jm is None else f"{jm:.2f}")
            print(line)
    return 0


def _tune(evals) -> int:
    import json

    from askmike.evals.pool import OnceEmbedder
    from askmike.evals.run import load_queries
    from askmike.evals.tune import tune, write_settings
    from askmike.retrieval.index import default_db, open_index
    from askmike.retrieval.ladder import SETTINGS_PATH, Ladder, Settings
    from askmike.retrieval.models import Embedder

    skills = json.loads((CORPUS / "skills.json").read_text(encoding="utf-8"))
    emb0 = Embedder()
    conn, emb = open_index(default_db(), CORPUS / "units.jsonl", emb0.name), OnceEmbedder(emb0)
    items = [q for q in load_queries(evals, "jdreq", "A") if q["split"] == "tune"]
    r = tune(items, lambda d, k0: Ladder(conn, emb, skills, Settings(depth=d, rrf_k0=k0)), "A")
    write_settings(r, SETTINGS_PATH)
    print(f"tune: depth={r.depth} rrf_k0={r.rrf_k0} items={r.tuned_on['items']} clusters={r.tuned_on['clusters']}")
    for rung in ("R3", "R4"):
        print(rung, r.evidence[rung])
    return 0


def _run_step(args, evals) -> int:
    import json

    from askmike.corpus.claude import run_claude
    from askmike.evals import steps
    if args.step == "extract":
        r = steps.run_extract(evals, run_claude, args.workers, args.max_seconds)
        print(f"extract: jds={r['jds']} extracted={r['extracted']} errors={r['errors']} remaining={r['remaining']}"
              f" requirements={r['requirements']} median={r['median']} p90={r['p90']} dropped_invalid={r['dropped_invalid']}")
        return 0
    steps.require_complete_extract(evals)  # before the model or the index is loaded
    units = [json.loads(x) for x in (CORPUS / "units.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
    if args.step == "pool":
        from askmike.retrieval.index import default_db, open_index
        from askmike.retrieval.models import Embedder
        skills = json.loads((CORPUS / "skills.json").read_text(encoding="utf-8"))
        emb0 = Embedder()
        r = steps.run_pool(evals, open_index(default_db(), CORPUS / "units.jsonl", emb0.name), emb0, units, skills)
        print(f"pool: queries={r['queries']} median={r['median']} max={r['max']} cap_hits={r['cap_hits']}")
        return 0
    r = steps.run_judge(evals, args.judge, args.sample, run_claude, units, args.workers, args.max_seconds)
    print(f"judge {args.judge}: " + " ".join(f"{k}={v}" for k, v in r.items()))
    return 0


# ---- M2: kinds, answers, audit ----

def _kinds(args, evals, answers) -> int:
    from collections import Counter

    from askmike.answer.route import question_kind
    from askmike.corpus.claude import run_claude
    from askmike.evals.answer_sets import question_rows
    from askmike.evals.judge import JUDGE_A
    from askmike.evals.kinds import label_kinds, router_disagreements
    from askmike.evals.steps import Gate, write_jsonl
    from askmike.llm.ledger import Ledger

    rows = question_rows(evals)
    if not rows:
        raise FileNotFoundError("no recruiter questions under work/evals/sets; run the sets step first")
    answers.mkdir(parents=True, exist_ok=True)
    gate = Gate(run_claude, args.max_seconds)
    labels = label_kinds(rows, gate, Ledger(answers / "kinds_ledger.jsonl"), args.workers)
    write_jsonl(answers / "kinds_A.jsonl", [{"id": k, "kind": v, "judge": "A", "model": JUDGE_A}
                                            for k, v in labels.items()])
    counts = Counter(labels.values())
    print(f"kinds: questions={len(rows)} labelled={len(labels)} remaining={len(rows) - len(labels)}"
          + (" (stopped: 20 failed batches in a row)" if gate.stopped else ""))
    print("kinds: " + ", ".join(f"{k}={counts.get(k, 0)}" for k in KINDS))
    dis = router_disagreements(rows, labels)
    moves = Counter((question_kind(r["text"]), labels[r["id"]]) for r in rows if r["id"] in dis)
    print(f"kinds: router disagrees on {len(dis)}" + (": " + ", ".join(dis) if dis else ""))
    for (router, judge), n in sorted(moves.items()):
        print(f"kinds:   router {router} -> judge {judge}: {n}")
    return 0


def _corpus():
    import json

    units = {u["id"]: u for u in (json.loads(x) for x in (CORPUS / "units.jsonl").read_text(encoding="utf-8")
                                  .splitlines() if x.strip())}
    return units, json.loads((CORPUS / "skills.json").read_text(encoding="utf-8"))


def _fmt(v) -> str:
    if isinstance(v, float):
        return f"{v:.3f}"
    if isinstance(v, dict) and {"mean", "lo", "hi"} <= set(v):
        return f"{v['mean']:.3f} [{v['lo']:.3f}, {v['hi']:.3f}] n_jd={v['n_clusters']}"
    if isinstance(v, list):
        return "[" + ", ".join(map(str, v)) + "]"
    return "n/a" if v is None else str(v)


def _answers(args, evals, answers) -> int:
    from askmike.answer.__main__ import gate_config
    from askmike.answer.answerer import Answerer
    from askmike.corpus.claude import run_claude
    from askmike.evals.answer_metrics import summarise
    from askmike.evals.answer_run import run_answers
    from askmike.evals.answer_sets import load_items
    from askmike.evals.run import write_json
    from askmike.evals.steps import read_jsonl
    from askmike.retrieval.index import default_db, open_index
    from askmike.retrieval.ladder import Ladder, LockedLadder, load_settings
    from askmike.retrieval.models import Embedder

    kinds = {r["id"]: r["kind"] for r in read_jsonl(answers / "kinds_A.jsonl")}
    items = load_items(evals, kinds)
    chosen = [i for i in items if args.split == "all" or i.split == args.split]
    units, skills = _corpus()
    cfg = gate_config(skills)
    emb, settings, units_path = Embedder(), load_settings(), CORPUS / "units.jsonl"

    def ladder():  # one per worker thread: an index connection stays on the thread that opened it
        return Ladder(open_index(default_db(), units_path, emb.name), emb, skills, settings)

    ladder().conn.close()  # refuse a missing or stale index before any model call
    answerer = Answerer(LockedLadder(ladder), units, skills, cfg, run_claude, args.model)
    out = answers / MODEL_SLUGS[args.model]
    r = run_answers(chosen, answerer, out / "records.jsonl", args.workers, args.max_seconds)
    print(f"answers {MODEL_SLUGS[args.model]} {args.split}: items={r['items']} recorded={r['recorded']} "
          f"live={r['live']} cached={r['cached']} failed={r['failed']} remaining={r['remaining']} "
          f"internal_errors={r['internal_errors']}" + (" (stopped: 20 failed items in a row)" if r["stopped"] else ""))
    if r["last_error"]:
        print(f"answers: last error: {r['last_error']}")
    labels_A = {row["query_id"]: row["relevant"] for row in read_jsonl(evals / "labels_A.jsonl")}
    summary = summarise(read_jsonl(out / "records.jsonl"), items, labels_A)
    write_json(out / "summary.json", summary)
    for split in (("tune", "report") if args.split == "all" else (args.split,)):
        for key, v in summary[split].items():
            print(f"{split} {key}: {_fmt(v)}")
    return 0


def _audit(args, evals, answers) -> int:
    from collections import Counter

    from askmike.answer.__main__ import gate_config
    from askmike.evals.answer_metrics import audit
    from askmike.evals.steps import read_jsonl

    slug = MODEL_SLUGS[args.model]
    records = read_jsonl(answers / slug / "records.jsonl")
    if not records:
        raise FileNotFoundError(f"no records in work/answers/{slug}/records.jsonl; run the answers step first")
    units, skills = _corpus()
    hits = audit(records, units, gate_config(skills))
    ids = sorted({h["item_id"] for h in hits})
    print(f"audit {slug}: {len(hits)} hit(s) in {len(ids)} of {len(records)} records")
    for gate, n in sorted(Counter(h["gate"] for h in hits).items()):
        print(f"audit {slug}:   {gate}: {n}")
    for iid in ids:
        where = sorted({f"{h['turn']} {h['where']} {h['gate']}" for h in hits if h["item_id"] == iid})
        print(f"audit {slug}:   {iid}: " + "; ".join(where))
    return 1 if hits else 0


# ---- M2: the support judge and its planted calibration ----

def _records_path(arg, default: Path) -> Path:
    """--records, resolved under $ASK_PRIVATE_DIR (a relative path starts there). The judge writes private
    files beside it, so a path outside the private folder is refused."""
    from askmike.private import private_dir

    if arg is None:
        return default
    root, given = private_dir(), Path(arg).expanduser()
    path = (given if given.is_absolute() else root / given).resolve()
    if not path.is_relative_to(root):
        raise ValueError("--records must name a file under $ASK_PRIVATE_DIR")
    return path


def _rate_line(r) -> str:
    if r is None:
        return "n/a"
    return f"{r['mean']:.3f} [{r['lo']:.3f}, {r['hi']:.3f}] n={r['n_items']} items={r['n_clusters']}"


def _support(args, evals, answers) -> int:
    from askmike.answer.__main__ import gate_config
    from askmike.answer.prompt import ANSWER_TEMPLATE_SHA
    from askmike.corpus.claude import run_claude
    from askmike.evals import support as sup
    from askmike.evals.answer_metrics import SPLITS
    from askmike.evals.answer_run import turns
    from askmike.evals.run import write_json
    from askmike.evals.steps import Gate, read_jsonl, write_jsonl
    from askmike.llm.ledger import Ledger
    from askmike.private import private_dir

    if args.model is None and args.records is None:
        raise ValueError("--model is needed without --records (it names work/answers/<slug>/records.jsonl)")
    path = _records_path(args.records, answers / MODEL_SLUGS[args.model] / "records.jsonl" if args.model else None)
    where = path.relative_to(private_dir())
    records = read_jsonl(path)
    if not records:
        raise FileNotFoundError(f"no records in {where}; run the answers step first")
    if args.model:
        others = sum(1 for r in records if r.get("model") != args.model)
        if others:
            raise ValueError(f"{others} of {len(records)} records in {where} are not answers by {args.model}")
        slug = MODEL_SLUGS[args.model]
    else:  # the picked calibration: each record names the answer model that served its route
        others = sum(1 for r in records if r.get("model") not in ANSWER_MODELS)
        if others:
            raise ValueError(f"{others} of {len(records)} records in {where} are not answers by an answer model")
        slug = "+".join(sorted({MODEL_SLUGS[r["model"]] for r in records}))
    current = [r for r in records if all(t.get("template_sha") == ANSWER_TEMPLATE_SHA for _, t in turns(r))]
    scope = [r for r in current if args.judge == "A" or sup.in_b_sample(r["item_id"])]  # B judges its sample
    units, skills = _corpus()
    gate = Gate(run_claude, args.max_seconds)
    verdicts = sup.judge_answers(scope, units, skills, args.judge, gate, Ledger(path.with_name("support_ledger.jsonl")),
                                 allow=gate_config(skills).honesty_allow, workers=args.workers)
    write_jsonl(path.with_name(f"support_{args.judge}.jsonl"), [dict(v, key=k) for k, v in verdicts.items()])
    rows = {r["item_id"]: sup.claim_rows(r, units) for r in scope}
    want, said = sum(len(x) for x in rows.values()), [v["verdict"] for v in verdicts.values()]
    print(f"support {slug} {args.judge}: records={len(records)} stale={len(records) - len(current)} "
          f"in_scope={len(scope)} with_claims={sum(1 for x in rows.values() if x)} claims={want} "
          f"verdicts={sum(1 for v in said if v)} "
          f"none={sum(1 for v in said if v is None)} remaining={want - len(verdicts)} "
          f"template_sha={sup.template_sha()}" + (" (stopped: 20 failed calls in a row)" if gate.stopped else ""))
    summary: dict = {}
    for split in SPLITS:
        recs = [r for r in scope if r.get("split") == split]
        if not recs:
            continue
        summary[split] = r = sup.rates(verdicts, recs)
        print(f"{split} support_rate {_rate_line(r['support_rate'])}")
        print(f"{split} partial_rate {_rate_line(r['partial_rate'])}")
        print(f"{split} kept={r['n_kept']} kept_unjudged={r['n_kept_unjudged']} grounding_only={r['n_grounding_only']} "
              f"grounding_only_unjudged={r['n_grounding_only_unjudged']} "
              f"grounding_false_drop_rate={_fmt(r['grounding_false_drop_rate'])}")
    first = path.with_name("support_A.jsonl")
    if args.judge == "B" and first.exists():
        summary["kappa_AB"] = k = sup.kappa({row["key"]: row for row in read_jsonl(first)}, verdicts)
        print(f"support {slug} kappa A-B (supported vs not): {_fmt(k['kappa'])} n_pairs={k['n_pairs']}")
    write_json(path.with_name(f"support_summary_{args.judge}.json"),
               dict(summary, judge=args.judge, template_sha=sup.template_sha()))
    return 0


def _judge_calib(args, evals, answers) -> int:
    from askmike.answer.__main__ import gate_config
    import hashlib

    from askmike.corpus.claude import run_claude
    from askmike.evals import support as sup
    from askmike.evals.run import write_json
    from askmike.evals.steps import Gate, read_jsonl, write_jsonl
    from askmike.evals.support_calib import PlantedIncomplete, judge_planted, planted_set, score
    from askmike.llm.ledger import Ledger

    units, skills = _corpus()
    calib = answers / "calibration"
    calib.mkdir(parents=True, exist_ok=True)
    gate = Gate(run_claude, args.max_seconds)
    try:  # the paraphrases come from claude -p (sonnet), fixed by their ledger once made
        rows, para = planted_set(units, skills, gate, Ledger(calib / "paraphrase_ledger.jsonl"))
    except PlantedIncomplete as e:
        print(f"judge-calib: {e}; the planted set is not built, rerun", file=sys.stderr)
        return 1
    print("judge-calib paraphrases: " + " ".join(f"{k}={v}" for k, v in para.items()))
    write_jsonl(calib / "planted.jsonl", rows)
    planted_sha1 = hashlib.sha1((calib / "planted.jsonl").read_bytes()).hexdigest()
    verdicts = judge_planted(rows, units, skills, args.judge, gate, Ledger(calib / "planted_ledger.jsonl"),
                             allow=gate_config(skills).honesty_allow, workers=args.workers)
    write_jsonl(calib / f"support_{args.judge}.jsonl", [dict(v, id=k) for k, v in verdicts.items()])
    s, tsha = score(rows, verdicts), sup.template_sha()
    write_json(calib / f"score_{args.judge}.json", dict(s, judge=args.judge, model=sup.JUDGES[args.judge],
                                                        template_sha=tsha, planted_sha1=planted_sha1))
    print(f"judge-calib {args.judge}: planted={s['n']} positives={s['n_positive']} negatives={s['n_negative']} "
          f"unjudged={s['n_unjudged']} template_sha={tsha} planted_sha1={planted_sha1[:12]}"
          + (" (stopped: 20 failed calls in a row)" if gate.stopped else ""))
    for kind, b in s["by_kind"].items():
        print(f"judge-calib {args.judge}:   {kind}: n={b['n']} judged={b['n_judged']} acc={_fmt(b['acc'])}")
    print(f"judge-calib {args.judge}: pos_acc={_fmt(s['pos_acc'])} neg_acc={_fmt(s['neg_acc'])} pass_={s['pass_']}")
    first = calib / "support_A.jsonl"
    if args.judge == "B" and first.exists():
        k = sup.kappa({row["id"]: row for row in read_jsonl(first)}, verdicts)
        print(f"judge-calib kappa A-B (supported vs not): {_fmt(k['kappa'])} n_pairs={k['n_pairs']}")
    return 0 if s["pass_"] else 1


def _calibrate(args, evals, answers) -> int:
    import json

    from askmike.answer.__main__ import gate_config
    from askmike.answer.answerer import Answerer
    from askmike.answer.config import MAX_TOKENS
    from askmike.answer.settings import load_answer_settings
    from askmike.evals import calibrate as cal
    from askmike.evals.answer_sets import load_items
    from askmike.evals.run import write_json
    from askmike.evals.steps import read_jsonl, write_jsonl
    from askmike.llm.api_runner import make_api_runner
    from askmike.llm.ledger import Ledger
    from askmike.llm.prices import load_prices
    from askmike.retrieval.index import default_db, open_index
    from askmike.retrieval.ladder import Ladder, LockedLadder, load_settings
    from askmike.retrieval.models import Embedder

    if args.model == cal.PICKED:  # the setup that ships: one model per route
        model, slug = load_answer_settings().model, cal.PICKED
    else:
        model, slug = args.model, MODEL_SLUGS[args.model]
    out = answers / "calibration" / slug
    kinds = {r["id"]: r["kind"] for r in read_jsonl(answers / "kinds_A.jsonl")}
    chosen = cal.sample(load_items(evals, kinds), args.n)
    prices = {m: load_prices(m) for m in cal.models_of(model)}
    if args.summarise:
        s = cal.summarise_files(answers, slug, model, chosen, prices)
        write_json(out / "summary.json", s)
        print(json.dumps(s, indent=1, sort_keys=True))
        if not s["complete"]:
            print(f"calibrate {slug}: incomplete: compared {s['n_compared']} of {s['n']} items, called "
                  f"{s['called']} of the {s['n_to_call']} that make a call; rerun calibrate (spend.jsonl keeps "
                  f"the cap) before acting on the verdict; {s['n_api_unjudged']} kept API claim(s) unjudged "
                  f"by judge A (run support --records ... --judge A)")
        return 0
    units, skills = _corpus()
    cfg = gate_config(skills)
    emb, settings, units_path = Embedder(), load_settings(), CORPUS / "units.jsonl"

    def ladder():
        return Ladder(open_index(default_db(), units_path, emb.name), emb, skills, settings)

    ladder().conn.close()
    answerer = Answerer(LockedLadder(ladder), units, skills, cfg, make_api_runner(MAX_TOKENS), model)
    r = cal.run(chosen, answerer, Ledger(out / "ledger.jsonl"), args.max_usd, prices, MAX_TOKENS,
                spend_path=out / "spend.jsonl")
    path = out / "records.jsonl"
    merged = {x["item_id"]: x for x in read_jsonl(path)}
    for rec in r["records"]:
        if not rec["cached"] or rec["item_id"] not in merged:  # a ledger-only rerun keeps the live run's timings
            merged[rec["item_id"]] = cal.keep_call_ms(rec, merged.get(rec["item_id"]))  # and so does a mixed one
    write_jsonl(path, [merged[k] for k in sorted(merged)])
    print(f"calibrate {slug}: items={len(chosen)} recorded={len(r['records'])} remaining={r['remaining']} "
          f"prior_spend_usd={r['prior']:.4f} spent_usd={r['spent']:.4f} (total, all invocations) max_usd={args.max_usd}" + (" (stopped at the cap)" if r["stopped"] else ""))
    return 0


# ---- M2: the pick packet ----

def _pick(args, evals, answers) -> int:
    import json
    from types import SimpleNamespace

    from askmike.answer.__main__ import gate_config
    from askmike.answer.evidence import SUBJECTS_PATH
    from askmike.answer.render import render_text
    from askmike.evals import pick
    from askmike.evals.answer_metrics import audit, summarise
    from askmike.evals.answer_sets import load_items
    from askmike.evals.steps import read_jsonl

    kinds = {r["id"]: r["kind"] for r in read_jsonl(answers / "kinds_A.jsonl")}
    items = load_items(evals, kinds)
    report = {i.item_id for i in items if i.split == "report"}
    labels_A = {row["query_id"]: row["relevant"] for row in read_jsonl(evals / "labels_A.jsonl")}
    units, skills = _corpus()
    cfg = gate_config(skills)

    def verdicts(path):
        return {row["key"]: row for row in read_jsonl(path)}

    records_by_model, summaries = {}, {}
    for model in ANSWER_MODELS:
        slug = MODEL_SLUGS[model]
        out = answers / slug
        records = read_jsonl(out / "records.jsonl")
        rep = summarise(records, items, labels_A)["report"]
        if rep["n_missing"] or rep["n_stale"]:
            raise ValueError(f"{slug}: {rep['n_missing']} missing and {rep['n_stale']} stale report record(s) of "
                             f"{rep['n_items']}; rerun answers --split all")
        recs = [r for r in records if r["item_id"] in report]
        summaries[slug] = pick.report_summary(rep, recs, verdicts(out / "support_A.jsonl"),
                                              verdicts(out / "support_B.jsonl"), len(audit(recs, units, cfg)))
        records_by_model[slug] = {r["item_id"]: r for r in recs}
    fallback = render_text(SimpleNamespace(outcome="fallback", kind=None, kept=()), units)
    subjects = json.loads(SUBJECTS_PATH.read_text(encoding="utf-8"))
    qpath = answers / pick.OPEN_QUESTIONS_NAME  # private; the packet leaves the section out without it
    questions = (qpath.read_text(encoding="utf-8") if qpath.exists() else "").strip() or None
    path = answers / "pick.md"
    part = path.with_name(path.name + ".part")
    part.write_text(pick.packet(records_by_model, items, summaries, fallback, subjects, questions), encoding="utf-8")
    part.replace(path)
    drawn = pick.draw(items, records_by_model)
    print(f"pick: wrote work/answers/pick.md: models={','.join(records_by_model)} items={len(drawn)} "
          f"({', '.join(i.item_id for i in drawn)}) "
          f"open_questions={'work/answers/' + qpath.name if questions else 'none'}")
    for slug, s in summaries.items():
        a, sa = s["answers"], s["support_A"]
        print(f"pick {slug}: report items={a['n_items']} audit_hits={s['audit_hits']} "
              f"handoff_correct_rate={_fmt(a['handoff_correct_rate'])} canary_hits={a['injection_canary_hits']} "
              f"fallback_rate={_fmt(a['fallback_rate'])} support_A={_rate_line(sa['support_rate'])} "
              f"coverage={_fmt(a['coverage'])}")
    return 0


# ---- M2: the published facts ----

def _facts_m2(args, evals, answers) -> int:
    import json

    from askmike.facts.build_m2 import PREFIX, FactsRefused, build_m2, check

    work = answers.parent
    try:
        if args.check:
            drift = check(work, REPO, FACTS)
            for key in drift:
                print(f"drift: {key}")
            print(f"facts-m2 --check: {len(drift)} m2 fact(s) differ from facts.json")
            return 1 if drift else 0
        facts = build_m2(work, REPO)
    except FactsRefused as e:  # each reason names files, counts, ids and gates only
        print(f"facts-m2: refused: {e}", file=sys.stderr)
        return 1
    old = json.loads(FACTS.read_text(encoding="utf-8")) if FACTS.exists() else {}
    merged = {k: v for k, v in old.items() if not k.startswith(PREFIX)} | facts
    part = FACTS.with_name(FACTS.name + ".part")
    part.write_text(json.dumps(dict(sorted(merged.items())), indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    part.replace(FACTS)
    print(f"facts-m2: {len(facts)} m2 facts written to {FACTS.name}")
    return 0


ANSWER_STEPS = {"calibrate": _calibrate, "kinds": _kinds, "answers": _answers, "audit": _audit, "support": _support,
                "judge-calib": _judge_calib, "pick": _pick, "facts-m2": _facts_m2}


if __name__ == "__main__":
    sys.exit(main())
