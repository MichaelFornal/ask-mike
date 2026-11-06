"""M1 facts: every number docs/m1/REPORT.md prints, each as {value, display, cmd, inputs}.

Pattern from deal-terms-desk `facts/build.py` (`build`, `check`: recompute from the inputs, values rounded to 4
places, report drift by key), adapted 2026-11-03:
- each fact stores its display string, the command that produced the value and the sha256 of every file it read;
- headline facts read judge A's results (`results/A/`) on the report split; kappa and tau read both judges;
- judge B's sample sizes count judge B's own label rows (final review, 2026-11-03).

`cmd` is the step whose output the value is copied from (`run --labels A`, `tune`), or `facts` when this module
computes the value (counts, kappa, tau), or a `python -c` line for a code constant. `inputs` keys are file names
relative to `$ASK_PRIVATE_DIR/work/evals/` (results/A and results/B hold files with the same names, so the
folder stays in the name) or to the repo (corpus/units.jsonl, the settings file); never an absolute path.
No fact counts applied-to jobs or application forms, or names a company.
"""
from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path

from askmike.evals.bootstrap import CI_HIGH, CI_LOW, N_BOOT
from askmike.evals.metrics import cohen_kappa, kendall_tau
from askmike.evals.run import ALL_RUNGS, METRICS, RUNGS, SETS
from askmike.evals.sets import FAMILIES
from askmike.evals.steps import read_jsonl
from askmike.retrieval import expand
from askmike.retrieval.requirements import MAX_REQS

PREFIX = "m1_"
CMD_FACTS = "uv run python -m askmike.evals facts"
CMD_RUN_A = "uv run python -m askmike.evals run --labels A"
CMD_TUNE = "uv run python -m askmike.evals tune"
CMD_CI_PCT = ('uv run python -c "from askmike.evals.bootstrap import CI_LOW, CI_HIGH; '
              'print(round((CI_HIGH - CI_LOW) * 100))"')
CMD_N_BOOT = 'uv run python -c "from askmike.evals.bootstrap import N_BOOT; print(N_BOOT)"'
CMD_REQ_CAP = 'uv run python -c "from askmike.retrieval.requirements import MAX_REQS; print(MAX_REQS)"'
CMD_N_RUNGS = 'uv run python -c "from askmike.evals.run import ALL_RUNGS; print(len(ALL_RUNGS))"'
CMD_TAU_STEP = ('uv run python -c "from askmike.evals.run import ALL_RUNGS; n = len(ALL_RUNGS); '
                'print(2 / (n * (n - 1) / 2))"')
CMD_SHORT_NAME = ('uv run python -c "from askmike.facts.build import short_name_max_len; '
                  'print(short_name_max_len())"')
UNITS = "corpus/units.jsonl"
SETTINGS = "askmike/retrieval/settings.json"
SHARES = ("m1_supportable_rate", "m1_jds_at_req_cap_share")  # printed as whole percents


def display(key: str, value) -> str:
    """How the report prints a fact. Levels to 2 places, paired differences to 3, shares as percents."""
    if value is None:
        return "undefined" if key == "m1_kappa_AB" else "n/a"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if key == "m1_ci_pct":
        return f"{value}%"
    if key in SHARES:
        return f"{value * 100:.0f}%"
    if isinstance(value, int):
        return f"{value:,}"
    if key == "m1_tau_step":
        s = f"{value:.1f}"
    elif key.startswith("m1_cmp_"):
        s = f"{value:.3f}"
    else:
        s = f"{value:.2f}"
    return s[1:] if s.startswith("-") and float(s) == 0 else s  # never "-0.00"


def _round(v):
    if isinstance(v, float):
        v = round(v, 4)
        return 0.0 if v == 0 else v  # no -0.0
    return v


class _Inputs:
    """sha256 of each file read, by its relative name; hashed once."""

    def __init__(self, evals: Path, repo: Path):
        self.evals, self.repo, self._sha = Path(evals), Path(repo), {}

    def path(self, name: str) -> Path:
        return self.repo / name if name in (UNITS, SETTINGS) else self.evals / name

    def of(self, *names: str) -> dict:
        out = {}
        for name in sorted(set(names)):
            if name not in self._sha:
                self._sha[name] = hashlib.sha256(self.path(name).read_bytes()).hexdigest()
            out[name] = self._sha[name]
        return out


def kappa_ab(rows_a: list, rows_b: list) -> tuple:
    """Cohen's kappa over the (query_id, unit_id) pairs in both judges' pools; True when the unit is in that
    judge's `relevant`. Returns (kappa or None when undefined, number of shared pairs)."""
    def pairs(rows):
        return {(r["query_id"], u): u in set(r["relevant"]) for r in rows for u in r["pool"]}
    a, b = pairs(rows_a), pairs(rows_b)
    return cohen_kappa(a, b), len(set(a) & set(b))


def rung_order(means: dict) -> list:
    """Rungs from highest mean to lowest; ties break by rung name, ascending."""
    return sorted(means, key=lambda r: (-means[r], r))


def tau_ab(items_a: dict, items_b: dict) -> tuple:
    """Kendall tau between the rung orders by recall@10 under judge A and judge B, on the same items: report-split
    item ids present in every rung's B items and A items. Each judge's mean skips the items that judge found
    unsupported (metric None), as every published mean does. items_x: {rung: {item_id: row}}.
    Returns (tau, n_items, n_clusters)."""
    ids = None
    for r in ALL_RUNGS:
        rb = {i for i, row in items_b[r].items() if row["split"] == "report"}
        got = rb & set(items_a[r])
        ids = got if ids is None else ids & got
    if not ids:
        raise ValueError("no report-split items labelled by both judges")
    orders = []
    for items in (items_a, items_b):
        means = {}
        for r in ALL_RUNGS:
            vals = [items[r][i]["recall_at_10"] for i in ids if items[r][i]["recall_at_10"] is not None]
            if not vals:
                raise ValueError(f"rung {r}: no supportable items to rank")
            means[r] = sum(vals) / len(vals)
        orders.append(rung_order(means))
    return kendall_tau(*orders), len(ids), len({items_b["R1"][i]["cluster"] for i in ids})


def short_name_max_len(limit: int = 20) -> int:
    """The longest skill name R4's alias expansion matches only in its Capitalised or ALL-CAPS form, measured on
    the matcher `askmike.retrieval.expand` uses (a lowercase name of this length does not match itself)."""
    short = [n for n in range(1, limit + 1) if not expand._term_re("a" * n).search("a" * n)]
    if not short or short != list(range(1, len(short) + 1)):
        raise ValueError(f"unexpected short-name rule: case-sensitive lengths {short}")
    return len(short)


def judge_b_sizes(rows_b: list) -> tuple:
    """(job descriptions, recruiter questions) judge B labelled: distinct clusters of its jdreq rows and distinct
    query ids of its question rows. The report calls the job descriptions report-split, so a tune row raises."""
    jd = [r for r in rows_b if r["set"] == "jdreq"]
    if any(r["split"] != "report" for r in jd):
        raise ValueError("judge B labelled job-description requirements outside the report split")
    return len({r["cluster"] for r in jd}), len({r["query_id"] for r in rows_b if r["set"] == "questions"})


def cap_share(jds: list, reqs: list) -> float | None:
    """Share of job descriptions whose extracted requirement count equals the cap (MAX_REQS)."""
    per = Counter(r["jd_id"] for r in reqs)
    return sum(1 for j in jds if per.get(j["id"], 0) == MAX_REQS) / len(jds) if jds else None


def _one_value(values: set, what: str):
    if len(values) != 1:
        raise ValueError(f"{what} differs across rungs: {sorted(values)}")
    return values.pop()


def build_m1(work_evals: Path, repo: Path) -> dict[str, dict]:
    evals, repo = Path(work_evals), Path(repo)
    inp = _Inputs(evals, repo)
    facts: dict[str, dict] = {}

    def put(key, value, cmd, *names):
        value = _round(value)
        facts[key] = {"value": value, "display": display(key, value), "cmd": cmd,
                      "inputs": inp.of(*names) if names else {}}

    # ---- sets and sizes ----
    units = [x for x in (repo / UNITS).read_text(encoding="utf-8").splitlines() if x.strip()]
    put("m1_n_units", len(units), CMD_FACTS, UNITS)
    jds = read_jsonl(evals / "sets" / "jds.jsonl")
    put("m1_n_jds", len(jds), CMD_FACTS, "sets/jds.jsonl", UNITS)
    fam = Counter(j["family"] for j in jds)
    unknown = set(fam) - set(FAMILIES)
    if unknown:
        raise ValueError(f"unknown role families: {sorted(unknown)}")
    for f in FAMILIES:
        put(f"m1_jd_family_{f}", fam.get(f, 0), CMD_FACTS, "sets/jds.jsonl", UNITS)
    reqs = read_jsonl(evals / "requirements.jsonl")
    put("m1_n_requirements", len(reqs), CMD_FACTS, "requirements.jsonl", UNITS)
    put("m1_jds_at_req_cap_share", cap_share(jds, reqs), CMD_FACTS, "requirements.jsonl", "sets/jds.jsonl", UNITS)
    qs = read_jsonl(evals / "sets" / "questions.jsonl")
    put("m1_n_questions", sum(1 for q in qs if q["kind"] == "evidence"), CMD_FACTS, "sets/questions.jsonl", UNITS)
    personal = [q for q in qs if q["kind"] == "personal"]
    put("m1_n_personal", len(personal), CMD_FACTS, "sets/questions.jsonl", UNITS)
    put("m1_n_personal_invented", sum(1 for q in personal if q.get("synthetic")), CMD_FACTS,
        "sets/questions.jsonl", UNITS)

    # ---- code constants ----
    put("m1_ci_pct", round((CI_HIGH - CI_LOW) * 100), CMD_CI_PCT)
    put("m1_n_boot", N_BOOT, CMD_N_BOOT)
    put("m1_req_cap", MAX_REQS, CMD_REQ_CAP)
    n = len(ALL_RUNGS)
    put("m1_n_rungs", n, CMD_N_RUNGS)
    put("m1_tau_step", 2 / (n * (n - 1) / 2), CMD_TAU_STEP)
    put("m1_short_name_max_len", short_name_max_len(), CMD_SHORT_NAME)

    # ---- headline metrics: judge A, report split ----
    def summary(label, s, r):
        name = f"results/{label}/{s}_{r}.json"
        return name, json.loads((evals / name).read_text(encoding="utf-8"))["by_split"]["report"]

    judged, unlabelled = [], {}
    for s in SETS:
        names, n_unl, sizes = [], set(), set()
        for r in ALL_RUNGS:
            name, rep = summary("A", s, r)
            names.append(name)
            for m in METRICS + (("coverage_at_10",) if s == "jdreq" else ()):
                ci = rep.get(m)
                for suffix, part in (("", "mean"), ("_lo", "lo"), ("_hi", "hi")):
                    put(f"m1_{s}_{r}_report_{m}{suffix}", ci[part] if ci else None, CMD_RUN_A, name, UNITS)
            if rep["judged_at_10_min"] is not None:
                judged.append((rep["judged_at_10_min"], name))
            n_unl.add(rep["n_unlabelled"])
            r10 = rep["recall_at_10"]
            sizes.add((r10["n_items"], r10["n_clusters"]) if r10 else (0, 0))
        unlabelled[s] = (_one_value(n_unl, f"{s} n_unlabelled"), names)
        n_items, n_clusters = _one_value(sizes, f"{s} report size")
        put(f"m1_{s}_report_n_items", n_items, CMD_RUN_A, *names, UNITS)
        if s == "jdreq":
            put("m1_jdreq_report_n_jds", n_clusters, CMD_RUN_A, *names, UNITS)
    if not judged:
        raise ValueError("no judged@10 values in results/A")
    put("m1_judged_at_10_min", min(v for v, _ in judged), CMD_FACTS, *(nm for _, nm in judged), UNITS)
    put("m1_n_unlabelled", sum(v for v, _ in unlabelled.values()), CMD_FACTS,
        *(nm for _, names in unlabelled.values() for nm in names), UNITS)

    cmp = json.loads((evals / "results" / "A" / "cmp_jdreq.json").read_text(encoding="utf-8"))
    for r in RUNGS:
        c = (cmp.get(r) or {}).get("vs_KW", {}).get("recall_at_10")
        for part in ("delta", "lo", "hi"):
            put(f"m1_cmp_{r}_vs_KW_jdreq_recall_at_10_{part}", c[part] if c else None, CMD_RUN_A,
                "results/A/cmp_jdreq.json", UNITS)

    # ---- labels: supportable share, grader agreement ----
    rows_a = read_jsonl(evals / "labels_A.jsonl")
    rows_b = read_jsonl(evals / "labels_B.jsonl")
    rep_a = [x for x in rows_a if x["set"] == "jdreq" and x["split"] == "report"]
    put("m1_supportable_rate", sum(1 for x in rep_a if x["relevant"]) / len(rep_a) if rep_a else None,
        CMD_FACTS, "labels_A.jsonl", UNITS)
    kappa, n_pairs = kappa_ab(rows_a, rows_b)
    put("m1_kappa_AB", kappa, CMD_FACTS, "labels_A.jsonl", "labels_B.jsonl", UNITS)
    put("m1_n_pairs_AB", n_pairs, CMD_FACTS, "labels_A.jsonl", "labels_B.jsonl", UNITS)
    b_jds, b_questions = judge_b_sizes(rows_b)
    put("m1_B_n_jds", b_jds, CMD_FACTS, "labels_B.jsonl", UNITS)
    put("m1_B_n_questions", b_questions, CMD_FACTS, "labels_B.jsonl", UNITS)

    item_names = {j: {r: f"results/{j}/jdreq_{r}_items.jsonl" for r in ALL_RUNGS} for j in "AB"}
    items = {j: {r: {x["item_id"]: x for x in read_jsonl(evals / nm)} for r, nm in item_names[j].items()}
             for j in "AB"}
    tau, tau_items, tau_jds = tau_ab(items["A"], items["B"])
    all_items = [nm for j in "AB" for nm in item_names[j].values()]
    put("m1_tau_rungs_AB", tau, CMD_FACTS, *all_items, UNITS)
    put("m1_tau_n_items", tau_items, CMD_FACTS, *all_items, UNITS)
    put("m1_tau_n_jds", tau_jds, CMD_FACTS, *all_items, UNITS)

    # ---- tuned settings ----
    settings = json.loads((repo / SETTINGS).read_text(encoding="utf-8"))
    put("m1_tuned_depth", int(settings["depth"]), CMD_TUNE, SETTINGS)
    put("m1_tuned_rrf_k0", int(settings["rrf_k0"]), CMD_TUNE, SETTINGS)
    return dict(sorted(facts.items()))


def check(work_evals: Path, repo: Path, facts_path: Path) -> list[str]:
    """M1 keys whose recomputed fact differs from facts.json (missing and extra keys included), sorted."""
    fresh = build_m1(work_evals, repo)
    path = Path(facts_path)
    stored = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    stored = {k: v for k, v in stored.items() if k.startswith(PREFIX)}
    return sorted(k for k in set(fresh) | set(stored) if fresh.get(k) != stored.get(k))
