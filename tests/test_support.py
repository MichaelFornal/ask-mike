"""The claim-support judge and its planted calibration, on invented units and fake runners (no model call)."""
import hashlib
import json
import re
from collections import Counter
from types import SimpleNamespace

import pytest

from askmike.answer.evidence import canonical_subject, ledger_lines
from askmike.answer.prompt import ANSWER_TEMPLATE_SHA, evidence_line
from askmike.evals import support, support_calib
from askmike.gates.checks import digit_tokens
from askmike.llm.ledger import MemoryLedger

SKILLS = [{"term": "Python", "depth": "strong", "aliases": []},
          {"term": "SQL", "depth": "working", "aliases": ["postgres"]},
          {"term": "Kubernetes", "depth": "none", "aliases": ["k8s"]},
          {"term": "Rust", "depth": "none", "aliases": []},
          {"term": "people management", "depth": "none", "aliases": []}]


def _unit(uid, claim, subject="Lab", detail=""):
    return {"id": uid, "claim": claim, "detail": detail, "subject": subject, "kind": "project", "skills": [],
            "when": ""}


UNITS = {u["id"]: u for u in (
    _unit("u1", "Mike cut nightly run time by 40% across 1,200 jobs with Python."),
    _unit("u2", "Mike ran 77 load tests on the queue."),
    _unit("u3", "Mike moved the nightly reports to SQL."),
    _unit("u4", "Mike wrote the depot importer.", subject="Zorvane"),
)}


def raw(text, units=("u1",), type="direct"):
    return {"type": type, "need": "", "req": "", "text": text, "units": list(units)}


def entry(c, kept=True, reasons=(), valid=None):
    """A raw claim and its Verdict as answer_run stores them; `valid` is the ids the gates kept."""
    claim = dict(c, units=[u for u in c["units"] if u in UNITS] if valid is None else list(valid))
    return {"raw": c, "verdict": {"claim": claim, "kept": kept, "reasons": list(reasons), "tidied": False,
                                  "stripped_ids": 0}}


def record(item_id, claims, first=None, split="tune", sha=ANSWER_TEMPLATE_SHA):
    out = {"item_id": item_id, "set": "evidence", "split": split, "cluster": item_id, "model": "claude-test-1",
           "outcome": "answer", "state": "answered", "template_sha": sha, "claims": list(claims)}
    if first is not None:
        out["first"] = {"outcome": "answer", "state": "answered", "template_sha": sha, "claims": list(first)}
    return out


def _claim_lines(prompt):
    block = prompt.split("<claims>\n", 1)[1].split("\n</claims>", 1)[0]
    return [json.loads(x) for x in block.splitlines() if x.strip()]


def _unit_block(prompt):
    return prompt.split("<units>\n", 1)[1].split("\n</units>", 1)[0]


SUFFIX = ", as the record shows."


def rewrite(claim):
    """A fake paraphraser: the claim reworded just enough to be no substring of its unit's line."""
    return claim.rstrip().rstrip(".") + SUFFIX


class Scripted:
    """A fake judge: answers each claim in the prompt by `rule(claim line, units block)`, and records prompts.
    A paraphrase prompt is answered by `para(statement)` (an Exception it returns is raised)."""

    def __init__(self, rule=None, reply=None, para=rewrite):
        self.rule, self.reply, self.para, self.prompts = rule, reply, para, []

    def __call__(self, prompt, model):
        self.prompts.append((prompt, model))
        if "<statements>\n" in prompt:
            block = prompt.split("<statements>\n", 1)[1].split("\n</statements>", 1)[0]
            out = {s["id"]: self.para(s["text"]) for s in (json.loads(x) for x in block.splitlines() if x.strip())}
            if any(isinstance(v, Exception) for v in out.values()):
                raise next(v for v in out.values() if isinstance(v, Exception))
            return {"result": json.dumps(out)}
        if self.reply is not None:
            r = self.reply(prompt) if callable(self.reply) else self.reply
            if isinstance(r, Exception):
                raise r
            return {"result": r}
        units = _unit_block(prompt)
        out = {c["id"]: {"reason": "r", "verdict": self.rule(c, units)} for c in _claim_lines(prompt)}
        return {"result": json.dumps(out)}


def verbatim(c, units_block):
    """supported when the claim text appears word for word in a cited unit's line, else unsupported."""
    lines = [ln for ln in units_block.splitlines() if any(ln.startswith(f"[{u}]") for u in c["cites"])]
    return "supported" if any(c["text"] in ln for ln in lines) else "unsupported"


def knows(c, units_block):
    """A judge that reads past the fake paraphraser's rewording, and otherwise matches text."""
    text = c["text"]
    return verbatim(dict(c, text=text[:-len(SUFFIX)] if text.endswith(SUFFIX) else text), units_block)


# ---- the judge prompt --------------------------------------------------------

def test_judge_prompt_shows_cited_units_and_ledger_lines_only():
    sql = raw("Mike moved the nightly reports to SQL.", ("u3",))
    gap = raw("Mike hasn't used Kubernetes; the closest is his nightly Python batch work.", ("u1",), "gap")
    ghost = raw("Mike wrote the zzq importer.", ("u404",))  # no corpus id: not judged
    bare = raw("Mike ran load tests.", ())  # no id at all: not judged
    rec = record("q-1", [entry(sql), entry(gap, kept=False, reasons=("honesty",)),
                         entry(ghost, kept=False, reasons=("structure", "grounding")),
                         entry(bare, kept=False, reasons=("structure",))])
    handoff = record("h-1", [])  # nothing to judge: no call
    run = Scripted(lambda c, u: "supported")
    out = support.judge_answers([rec, handoff], UNITS, SKILLS, "A", run, MemoryLedger())

    assert len(run.prompts) == 1 and run.prompts[0][1] == support.JUDGES["A"]
    prompt = run.prompts[0][0]
    units = _unit_block(prompt).splitlines()
    assert units == [evidence_line("u3", UNITS["u3"]), evidence_line("u1", UNITS["u1"])]  # cited units, once each
    assert "[u2]" not in prompt and "[u4]" not in prompt and "u404" not in prompt and "zzq" not in prompt
    lines = _claim_lines(prompt)
    assert [c["text"] for c in lines] == [sql["text"], gap["text"]]
    assert lines[0]["cites"] == ["u3"] and lines[1]["cites"] == ["u1"]
    assert lines[0]["ledger"] == ["SQL: can claim"]  # only the skills this claim names
    assert lines[1]["ledger"] == list(ledger_lines(gap["text"], SKILLS, frozenset()))
    assert any(x.startswith("Kubernetes: has not used") for x in lines[1]["ledger"])
    assert not any("Rust" in x for c in lines for x in c["ledger"])
    # the judge never sees the item id, the gate outcome or the claim type
    assert "q-1" not in prompt and "honesty" not in prompt and '"gap"' not in prompt and '"kept"' not in prompt
    assert sorted(out) == ["q-1#0", "q-1#1"]
    assert out["q-1#0"]["verdict"] == "supported" and out["q-1#0"]["item_id"] == "q-1"


def test_followup_record_is_one_call_with_unique_keys_across_turns():
    a, b = raw("Mike ran 77 load tests on the queue.", ("u2",)), raw("Mike moved the nightly reports to SQL.", ("u3",))
    rec = record("fu-1", [entry(b)], first=[entry(a)])
    run = Scripted(verbatim)
    out = support.judge_answers([rec], UNITS, SKILLS, "A", run, MemoryLedger())
    assert len(run.prompts) == 1
    assert sorted(out) == ["fu-1#0", "fu-1#1"]  # the first turn's claims, then the follow-up's
    assert {k: v["verdict"] for k, v in out.items()} == {"fu-1#0": "supported", "fu-1#1": "supported"}


def test_judge_sees_the_ids_the_gates_judged():
    """A claim the gates judged cites the ids they kept (what a visitor sees); when they kept none, its raw
    ids that are in the corpus."""
    both = raw("Mike moved the nightly reports to SQL.", ("u3", "u2"))
    outside = raw("Mike ran 77 load tests on the queue.", ("u2",))
    gone = raw("Mike wrote the depot importer.", ("u4", "u-gone"))  # a unit the corpus has since dropped
    lost = raw("Mike ran the old importer.", ("u-gone",))
    rec = record("q-2", [entry(both, valid=("u3",)),
                         entry(outside, kept=False, reasons=("structure",), valid=()),
                         entry(gone, valid=("u4", "u-gone")), entry(lost, valid=("u-gone",))])
    run = Scripted(lambda c, u: "supported")
    out = support.judge_answers([rec], UNITS, SKILLS, "A", run, MemoryLedger())
    assert [c["cites"] for c in _claim_lines(run.prompts[0][0])] == [["u3"], ["u2"], ["u4"]]
    assert sorted(out) == ["q-2#0", "q-2#1", "q-2#2"]


def test_bad_or_missing_verdict_is_none_not_guessed():
    texts = [f"Mike ran {n} load tests." for n in (1, 2, 3, 4, 5)]
    rec = record("q-3", [entry(raw(t, ("u2",))) for t in texts])
    reply = json.dumps({"c1": {"reason": "ok", "verdict": "supported"}, "c2": {"verdict": "mostly supported"},
                        "c3": {"verdict": 1}, "c5": "partial"})  # c4 omitted
    out = support.judge_answers([rec], UNITS, SKILLS, "A", Scripted(reply=reply), MemoryLedger())
    assert [out[f"q-3#{i}"]["verdict"] for i in range(5)] == ["supported", None, None, None, "partial"]

    # a reply with no JSON object (asked twice) leaves every claim None; a failed call leaves them unjudged
    junk = support.judge_answers([rec], UNITS, SKILLS, "A", Scripted(reply="no idea"), MemoryLedger())
    assert sorted(junk) == [f"q-3#{i}" for i in range(5)] and all(v["verdict"] is None for v in junk.values())
    failed = support.judge_answers([rec], UNITS, SKILLS, "A", Scripted(reply=RuntimeError("claude exited 1")),
                                   MemoryLedger())
    assert failed == {}

    r = support.rates(out, [rec])
    assert r["n_kept"] == 5 and r["n_kept_judged"] == 2 and r["n_kept_unjudged"] == 3
    assert r["support_rate"]["mean"] == 0.5 and r["partial_rate"]["mean"] == 0.5


def test_b_sample_uses_its_own_hash():
    recs = [record(f"q-{i:02d}", [entry(raw("Mike ran 77 load tests on the queue.", ("u2",)))]) for i in range(40)]
    out = support.judge_answers(recs, UNITS, SKILLS, "B", Scripted(verbatim), MemoryLedger(), sample=True)
    want = {r["item_id"] for r in recs
            if hashlib.sha1(("supportB:" + r["item_id"]).encode("utf-8")).digest()[0] % 10 < 4}
    assert 0 < len(want) < 40 and {v["item_id"] for v in out.values()} == want
    assert all(v["model"] == support.JUDGES["B"] for v in out.values())


def test_rerun_answers_from_the_ledger():
    rec = record("q-4", [entry(raw("Mike ran 77 load tests on the queue.", ("u2",)))])
    ledger, run = MemoryLedger(), Scripted(verbatim)
    first = support.judge_answers([rec], UNITS, SKILLS, "A", run, ledger)
    again = support.judge_answers([rec], UNITS, SKILLS, "A", run, ledger)
    assert first == again and len(run.prompts) == 1


# ---- rates -----------------------------------------------------------------

def _v(item_id, n, verdict, rec):
    """A verdict row for claim n of `rec`, fingerprinted the way judge_answers fingerprints it."""
    rows = {r["key"]: r for r in support.claim_rows(rec, UNITS)}
    return {"verdict": verdict, "item_id": item_id, "claim_sha": rows[f"{item_id}#{n}"]["claim_sha"]}


def test_false_drop_rate_counts_grounding_only_drops():
    t = [raw(f"Mike ran {n} load tests on the queue.", ("u2",)) for n in range(8)]
    rec = record("q-5", [
        entry(t[0]), entry(t[1]), entry(t[2]),  # kept: supported, partial, unsupported
        entry(t[3], kept=False, reasons=("grounding",)),  # grounding only, supported: a false drop
        entry(t[4], kept=False, reasons=("grounding",)),  # grounding only, unsupported: a right drop
        entry(t[5], kept=False, reasons=("grounding", "honesty")),  # not grounding only
        entry(t[6], kept=False, reasons=("honesty",)),
        entry(t[7], kept=False, reasons=("grounding",)),  # grounding only, not judged yet
    ])
    other = record("q-6", [entry(raw("Mike moved the nightly reports to SQL.", ("u3",)))])
    said = ["supported", "partial", "unsupported", "supported", "unsupported", "supported", "supported"]
    verdicts = {f"q-5#{i}": _v("q-5", i, v, rec) for i, v in enumerate(said)}
    verdicts["q-6#0"] = _v("q-6", 0, "supported", other)
    r = support.rates(verdicts, [rec, other])
    assert r["n_kept"] == 4 and r["n_kept_judged"] == 4
    assert r["support_rate"]["mean"] == pytest.approx(2 / 4) and r["partial_rate"]["mean"] == pytest.approx(1 / 4)
    assert r["support_rate"]["n_clusters"] == 2  # bootstrapped by item
    assert r["n_grounding_only"] == 2 and r["grounding_false_drop_rate"] == pytest.approx(1 / 2)
    assert r["n_grounding_only_unjudged"] == 1


def test_rates_ignore_a_verdict_for_a_claim_that_changed():
    old = record("q-7", [entry(raw("Mike ran 77 load tests on the queue.", ("u2",)))])
    new = record("q-7", [entry(raw("Mike moved the nightly reports to SQL.", ("u3",)))])
    v = {"q-7#0": _v("q-7", 0, "supported", old)}
    r = support.rates(v, [new])
    assert r["n_kept"] == 1 and r["n_kept_judged"] == 0 and r["support_rate"] is None
    assert r["grounding_false_drop_rate"] is None


def test_kappa_on_shared_judged_claims():
    a = {"k1": {"verdict": "supported", "claim_sha": "s"}, "k2": {"verdict": "partial", "claim_sha": "s"},
         "k3": {"verdict": "supported", "claim_sha": "s"}, "k4": {"verdict": None, "claim_sha": "s"},
         "k5": {"verdict": "unsupported", "claim_sha": "s"}}
    b = {"k1": {"verdict": "supported", "claim_sha": "s"}, "k2": {"verdict": "unsupported", "claim_sha": "s"},
         "k3": {"verdict": "partial", "claim_sha": "s"}, "k4": {"verdict": "supported", "claim_sha": "s"},
         "k5": {"verdict": "unsupported", "claim_sha": "s"}, "k9": {"verdict": "supported", "claim_sha": "s"}}
    k = support.kappa(a, b)
    assert k["n_pairs"] == 4  # k1, k2, k3, k5: both judged, the same claim
    # supported-vs-not: a = [1, 0, 1, 0], b = [1, 0, 0, 0] -> po .75, pe .5, kappa .5
    assert k["kappa"] == pytest.approx(0.5)
    assert support.kappa({}, b) == {"kappa": None, "n_pairs": 0}


# ---- the planted set -------------------------------------------------------

SUBJECTS = ("Lab", "Zorvane", "Wexmoor", "Quarry", "Orchard")


def _corpus():
    units = []
    for i in range(240):
        s = SUBJECTS[i % len(SUBJECTS)]
        if i % 3 == 0:
            claim = f"At {s}, Mike ran {i + 3} load tests on the queue."
        elif i % 3 == 1:
            claim = f"Mike wrote the {s} importer in Python."
        else:
            claim = f"In 2021 Mike moved {i} reports to SQL."  # a year comes first: no number negative
        units.append(_unit(f"t-{i:03d}", claim, subject=s))
    units += [_unit(f"m-{i}", f"Mike has worked on queue tooling {i} ways.", subject="Mike") for i in range(10)]
    units.append(_unit("t-rust", "At Lab, Mike ported the parser from Rust.", subject="Lab"))
    return {u["id"]: u for u in units}


def test_planted_set_is_deterministic_and_balanced():
    units = _corpus()
    p = support_calib.planted(units, SKILLS, seed=0)
    assert p == support_calib.planted(dict(reversed(list(units.items()))), SKILLS, seed=0)  # input order is moot
    other = support_calib.planted(units, SKILLS, seed=1)
    assert {(x["kind"], x["source"]) for x in p} != {(x["kind"], x["source"]) for x in other}  # the seed picks units
    assert Counter(x["kind"] for x in p) == {"positive": 40, "wrong_unit": 20, "number": 10, "skill": 10}
    assert len({x["source"] for x in p}) == 80 and len({x["id"] for x in p}) == 80  # one claim per unit
    for x in p:
        assert re.fullmatch(r"pl-[0-9a-f]{10}", x["id"])  # an opaque id: the kind never reaches the judge
        src = units[x["source"]]
        assert x["expect"] == ("supported" if x["kind"] == "positive" else "not_supported")
        if x["kind"] == "positive":
            assert x["text"] == src["claim"] and x["units"] == [src["id"]]
        elif x["kind"] == "wrong_unit":
            cited = units[x["units"][0]]
            assert x["text"] == src["claim"] and len(x["units"]) == 1 and src["subject"] in src["claim"]
            assert canonical_subject(cited["subject"]) != canonical_subject(src["subject"])
            assert "Mike" not in (src["subject"], cited["subject"])  # person-level units restate project work
        elif x["kind"] == "number":
            assert x["units"] == [src["id"]]
            old, new = digit_tokens(src["claim"])[0], digit_tokens(x["text"])[0]
            assert int(new.replace(",", "")) == 3 * int(old.replace(",", ""))
            assert x["text"].replace(new, old, 1) == src["claim"]
        else:
            assert x["units"] == [src["id"]]
            term = x["text"][len(src["claim"]) - 1:].removeprefix(" using ").rstrip(".")
            assert x["text"] == src["claim"][:-1] + f" using {term}."
            assert term in ("Kubernetes", "Rust") and term not in src["claim"]  # one-word none-depth terms


def test_wrong_unit_cites_only_a_disjoint_subject_that_never_names_the_source():
    """Every source names "Lab"; the one unit a wrong-unit negative may cite is the Zorvane unit. Excluded:
    person-level units, a unit whose subject shares a part with Lab, and a unit that names Lab."""
    units = [_unit(f"t-{i:03d}", f"At Lab, Mike ran {i + 3} load tests." if i % 2 else f"Mike wrote Lab tool {i}.",
                   subject="Lab") for i in range(120)]
    units += [_unit(f"m-{i}", f"Mike likes queue tooling {i}.", subject="Mike") for i in range(30)]
    units += [_unit("d-1", "Mike wrote the importer.", subject="Zorvane"),
              _unit("q-1", "Mike ran the scheduler.", subject="Lab and Quarry"),
              _unit("h-1", "Mike reused the Lab importer.", subject="Wexmoor")]
    p = support_calib.planted(units, SKILLS, seed=0)
    assert {tuple(x["units"]) for x in p if x["kind"] == "wrong_unit"} == {("d-1",)}


@pytest.mark.parametrize("src, cited, ok", [
    ("Lab", "Zorvane", True),
    ("Lab", "Mike", False),  # person-level
    ("Mike", "Zorvane", False),
    ("Lab", "Lab", False),
    ("Capgemini and Bridge", "Bridge", False),  # shares a part, though neither name contains the other's
    ("Bridge", "Capgemini and Bridge", False),
])
def test_can_cite_needs_disjoint_subjects(src, cited, ok):
    source = _unit("s", "Mike did it.", subject=src)
    assert support_calib.can_cite(source, _unit("c", "Mike ran it.", subject=cited)) is ok
    named = _unit("c", f"Mike reused the {src} tool.", subject="Zorvane")  # a unit that names the source's subject
    assert support_calib.can_cite(source, named) is False


def test_planted_batches_mix_positives_and_negatives():
    p = support_calib.planted(_corpus(), SKILLS, seed=0)
    batches = support_calib.batches(p, seed=0)
    assert [len(b) for b in batches] == [10] * 8
    assert sorted(x["id"] for b in batches for x in b) == sorted(x["id"] for x in p)
    assert all({x["expect"] for x in b} == {"supported", "not_supported"} for b in batches)


@pytest.mark.parametrize("text, want", [
    ("Mike ran 77 load tests.", "Mike ran 231 load tests."),
    ("Mike cut run time by 40% across 1,200 jobs.", "Mike cut run time by 120% across 1,200 jobs."),
    ("Mike indexed 1,200 boards.", "Mike indexed 3,600 boards."),
    ("Mike raised 1.5M rows.", "Mike raised 4.5M rows."),
    ("In 2021 Mike ran 5 tests.", None),  # a year first: skipped, never a quantity
    ("Mike had 0 outages.", None),  # tripling changes nothing
    ("Mike wrote the importer.", None),
])
def test_triple_first_number(text, want):
    assert support_calib.triple_first_number(text) == want


def _planted(n_pos=40, n_neg=40):
    kinds = ["wrong_unit", "number", "skill"]
    return ([{"id": f"p{i}", "kind": "positive", "expect": "supported"} for i in range(n_pos)]
            + [{"id": f"n{i}", "kind": kinds[i % 3], "expect": "not_supported"} for i in range(n_neg)])


def _verdicts(p, pos_wrong=0, neg_wrong=0, missing=()):
    out, pw, nw = {}, 0, 0
    for x in p:
        if x["id"] in missing:
            continue
        if x["kind"] == "positive":
            v = "partial" if pw < pos_wrong else "supported"  # a partial positive is a miss
            pw += 1
        else:
            v = "supported" if nw < neg_wrong else ("partial" if nw % 2 else "unsupported")
            nw += 1
        out[x["id"]] = {"verdict": v}
    return out


def test_calib_score_pass_rule():
    p = _planted()
    s = support_calib.score(p, _verdicts(p, pos_wrong=2, neg_wrong=4))  # .95 and .90 exactly: passes
    assert (s["pos_acc"], s["neg_acc"], s["pass_"]) == (0.95, 0.90, True)
    assert s["n_positive"] == 40 and s["n_negative"] == 40 and s["n_unjudged"] == 0
    assert support_calib.score(p, _verdicts(p, pos_wrong=3))["pass_"] is False  # .925
    assert support_calib.score(p, _verdicts(p, neg_wrong=5))["pass_"] is False  # .875
    # an unjudged claim is not a correct one: accuracies keep every planted claim in the denominator
    s = support_calib.score(p, _verdicts(p, missing={"p0", "p1", "p2"}))
    assert s["n_unjudged"] == 3 and s["pos_acc"] == pytest.approx(37 / 40) and s["pass_"] is False
    assert support_calib.score(p, {x["id"]: {"verdict": None} for x in p})["pass_"] is False
    by = support_calib.score(p, _verdicts(p, neg_wrong=3))["by_kind"]
    assert set(by) == {"positive", "wrong_unit", "number", "skill"} and by["positive"]["acc"] == 1.0


def test_score_counts_paraphrases_as_positives_and_reports_each_kind():
    p = _planted() + [{"id": "pp", "kind": "paraphrase", "expect": "supported"},
                      {"id": "ps", "kind": "sibling", "expect": "not_supported"}]
    v = _verdicts(p) | {"pp": {"verdict": "partial"}, "ps": {"verdict": "unsupported"}}
    s = support_calib.score(p, v)
    assert s["n_positive"] == 41 and s["n_negative"] == 41 and s["pos_acc"] == pytest.approx(40 / 41)
    assert s["by_kind"]["paraphrase"] == {"n": 1, "n_judged": 1, "acc": 0.0}
    assert s["by_kind"]["sibling"]["acc"] == 1.0


def _numbers(text):
    return [t for t in digit_tokens(text) if not re.fullmatch(r"(199\d|20[0-2]\d|2030)", t)]


def fussy(claim):
    """A fake paraphraser that copies some claims and invents a name in others, so draws must go on."""
    if "importer" in claim:
        return claim
    if "reports" in claim:
        return "Mike worked with Zelphar: " + claim
    return rewrite(claim)


def test_hard_slice_draws_until_enough_rewrites_pass():
    units = _corpus()
    run, ledger = Scripted(para=fussy), MemoryLedger()
    rows, stats = support_calib.planted_set(units, SKILLS, run, ledger, seed=0)
    assert Counter(x["kind"] for x in rows) == {"positive": 40, "wrong_unit": 20, "number": 10, "skill": 10,
                                                "sibling": 10, "paraphrase": 20, "paraphrase_number": 10}
    assert rows[:80] == support_calib.planted(units, SKILLS, seed=0)  # the easy slice is unchanged
    assert len({x["source"] for x in rows}) == 120 and len({x["id"] for x in rows}) == 120
    assert stats["verbatim"] > 0 and stats["ungrounded"] > 0 and stats["calls"] == len(run.prompts)
    for x in rows:
        src = units[x["source"]]
        if x["kind"] == "paraphrase":
            assert x["expect"] == "supported" and x["units"] == [src["id"]]
            assert x["text"] == rewrite(src["claim"]) and x["text"] not in evidence_line(src["id"], src)
        elif x["kind"] == "paraphrase_number":
            assert x["expect"] == "not_supported" and x["units"] == [src["id"]]
            para = rewrite(src["claim"])
            old, new = _numbers(para)[0], _numbers(x["text"])[0]
            assert int(new) == 3 * int(old) and x["text"].replace(new, old, 1) == para
        elif x["kind"] == "sibling":
            cited = units[x["units"][0]]
            assert x["expect"] == "not_supported" and x["text"] == src["claim"] and cited["id"] != src["id"]
            assert canonical_subject(cited["subject"]) == canonical_subject(src["subject"])
            assert support_calib.sibling_ok(src, cited)
    n = len(run.prompts)
    assert support_calib.planted_set(units, SKILLS, run, ledger, seed=0)[0] == rows and len(run.prompts) == n

    with pytest.raises(support_calib.PlantedIncomplete):  # a failed paraphrase call builds no set
        support_calib.planted_set(units, SKILLS, Scripted(para=lambda c: RuntimeError("claude exited 1")),
                                  MemoryLedger())


@pytest.mark.parametrize("claim, other, ok", [
    ("Mike ran load tests on the queue.", _unit("v", "Mike wrote the billing docs for each team."), True),
    ("Mike ran load tests on the queue.", _unit("v", "Mike wrote the billing docs.", subject="Wexmoor"), False),
    ("Mike ran 40 load tests on the queue.", _unit("v", "Mike cut 40 billing jobs for every team."), False),
    ("Mike ran load tests in 2021 on the queue.",
     _unit("v", "Mike wrote the billing docs for every customer team during 2021."), True),  # a year is no match
    ("Mike ran load tests on the queue.", _unit("v", "Mike ran load tests on the old queue."), False),
    ("Mike ran load tests on the queue.", _unit("v", "Mike wrote docs.", detail="Mike ran load tests on the queue."),
     False),  # the whole text counts, not only the claim
])
def test_sibling_needs_same_subject_no_shared_number_and_low_overlap(claim, other, ok):
    assert support_calib.sibling_ok(_unit("s", claim), other) is ok
    assert support_calib.sibling_ok(_unit("s", claim), dict(other, id="s")) is False


def test_triple_skips_years_when_asked():
    assert support_calib._triple("In 2021 Mike ran 5 tests.") is None
    assert support_calib._triple("In 2021 Mike ran 5 tests.", skip_years=True)[0] == "In 2021 Mike ran 15 tests."


def test_a_substring_judge_fails_the_full_planted_set():
    units = _corpus()
    rows, _ = support_calib.planted_set(units, SKILLS, Scripted(), MemoryLedger())
    run = Scripted(verbatim)
    v = support_calib.judge_planted(rows, units, SKILLS, "A", run, MemoryLedger())
    assert len(run.prompts) == 12 and sorted(v) == sorted(x["id"] for x in rows)
    for prompt, _ in run.prompts:  # neither the planted id nor its kind reaches the judge
        assert "pl-" not in prompt and not any(k in prompt for k in ("positive", "wrong_unit", "sibling",
                                                                    "paraphrase", "not_supported"))
    s = support_calib.score(rows, v)
    assert s["pass_"] is False and s["pos_acc"] == pytest.approx(40 / 60) and s["neg_acc"] == 1.0
    assert s["by_kind"]["paraphrase"]["acc"] == 0.0 and s["by_kind"]["positive"]["acc"] == 1.0
    # a judge that reads past the rewording passes the same set
    k = support_calib.judge_planted(rows, units, SKILLS, "A", Scripted(knows), MemoryLedger())
    assert support_calib.score(rows, k)["pass_"] is True


# ---- the CLI ----------------------------------------------------------------

def _cli(tmp_path, monkeypatch, runner, name="private"):
    from askmike.answer import __main__ as answer_cli
    from askmike.corpus import claude
    from askmike.evals import __main__ as cli

    priv = tmp_path / name
    monkeypatch.setenv("ASK_PRIVATE_DIR", str(priv))
    monkeypatch.setattr(answer_cli, "gate_config", lambda skills: SimpleNamespace(honesty_allow=frozenset()))
    monkeypatch.setattr(claude, "run_claude", runner)
    return cli, priv


def test_judge_calib_cli_writes_private_files_and_prints_numbers_only(tmp_path, monkeypatch, capsys):
    run = Scripted(knows)
    cli, priv = _cli(tmp_path, monkeypatch, run)
    assert cli.main(["judge-calib"]) == 0
    out = capsys.readouterr().out
    cal = priv / "work" / "answers" / "calibration"
    assert {f.name for f in cal.iterdir()} >= {"planted.jsonl", "support_A.jsonl", "score_A.json",
                                                "paraphrase_ledger.jsonl", "planted_ledger.jsonl"}
    score = json.loads((cal / "score_A.json").read_text())
    assert score["pass_"] is True and score["template_sha"] == support.template_sha() and score["n"] == 120
    assert score["planted_sha1"] == hashlib.sha1((cal / "planted.jsonl").read_bytes()).hexdigest()
    assert set(score["by_kind"]) == set(support_calib.KINDS)
    assert "pos_acc=1.000 neg_acc=1.000 pass_=True" in out and "paraphrase: n=20" in out
    planted = [json.loads(x) for x in (cal / "planted.jsonl").read_text().splitlines()]
    assert all(x["text"] not in out for x in planted)
    n = len(run.prompts)
    assert cli.main(["judge-calib"]) == 0 and len(run.prompts) == n  # the rerun is answered from the ledgers

    cli, priv = _cli(tmp_path, monkeypatch, Scripted(verbatim), name="other")  # a text matcher does not pass
    assert cli.main(["judge-calib"]) == 1
    assert json.loads((priv / "work" / "answers" / "calibration" / "score_A.json").read_text())["pass_"] is False


def test_judge_calib_cli_judges_nothing_when_a_paraphrase_call_fails(tmp_path, monkeypatch, capsys):
    cli, priv = _cli(tmp_path, monkeypatch, Scripted(knows, para=lambda c: RuntimeError("claude exited 1")))
    assert cli.main(["judge-calib"]) == 1
    assert "not built" in capsys.readouterr().err
    assert not (priv / "work" / "answers" / "calibration" / "planted.jsonl").exists()


def test_support_cli_writes_beside_the_records_and_refuses_paths_outside_private(tmp_path, monkeypatch, capsys):
    from askmike.evals.steps import read_jsonl, write_jsonl

    cli, priv = _cli(tmp_path, monkeypatch, Scripted(lambda c, u: "supported"))
    units = {u["id"]: u for u in (json.loads(x) for x in (cli.CORPUS / "units.jsonl").read_text().splitlines()
                                  if x.strip())}
    uid = sorted(units)[0]
    c = raw(units[uid]["claim"], (uid,))
    claim = {"raw": c, "verdict": {"claim": c, "kept": True, "reasons": [], "tidied": False, "stripped_ids": 0}}
    rows = [dict(record("q-9", [claim]), model="claude-haiku-5-5"),
            dict(record("q-8", [claim], sha="old"), model="claude-haiku-5-5")]  # stale: not judged
    path = priv / "work" / "answers" / "haiku" / "records.jsonl"
    path.parent.mkdir(parents=True)
    write_jsonl(path, rows)
    assert cli.main(["support", "--model", "claude-haiku-5-5", "--judge", "A"]) == 0
    out = capsys.readouterr().out
    got = read_jsonl(path.with_name("support_A.jsonl"))
    assert [(r["key"], r["verdict"]) for r in got] == [("q-9#0", "supported")]
    assert "stale=1" in out and "tune support_rate 1.000" in out and c["text"] not in out

    cal = priv / "work" / "answers" / "calibration" / "haiku" / "records.jsonl"
    cal.parent.mkdir(parents=True)
    write_jsonl(cal, rows[:1])
    rel = "work/answers/calibration/haiku/records.jsonl"
    assert cli.main(["support", "--model", "claude-haiku-5-5", "--judge", "A", "--records", rel]) == 0
    assert read_jsonl(cal.with_name("support_A.jsonl"))[0]["key"] == "q-9#0"
    outside = tmp_path / "elsewhere" / "records.jsonl"
    outside.parent.mkdir()
    write_jsonl(outside, rows[:1])
    assert cli.main(["support", "--model", "claude-haiku-5-5", "--judge", "A", "--records", str(outside)]) == 1
    assert not outside.with_name("support_A.jsonl").exists()
    assert cli.main(["support", "--model", "claude-sonnet-5-5", "--judge", "A", "--records", rel]) == 1  # wrong model


def test_support_cli_judges_picked_records_by_each_records_own_model(tmp_path, monkeypatch, capsys):
    """The picked calibration mixes models (jd on one, questions on the other): --records without --model
    judges every record whose model is an answer model."""
    from askmike.evals.steps import read_jsonl, write_jsonl

    cli, priv = _cli(tmp_path, monkeypatch, Scripted(lambda c, u: "supported"))
    units = {u["id"]: u for u in (json.loads(x) for x in (cli.CORPUS / "units.jsonl").read_text().splitlines()
                                  if x.strip())}
    uid = sorted(units)[0]
    c = raw(units[uid]["claim"], (uid,))
    claim = {"raw": c, "verdict": {"claim": c, "kept": True, "reasons": [], "tidied": False, "stripped_ids": 0}}
    rows = [dict(record("jd-9", [claim]), model="claude-sonnet-5-5"),
            dict(record("q-9", [claim]), model="claude-haiku-5-5")]
    path = priv / "work" / "answers" / "calibration" / "picked" / "records.jsonl"
    path.parent.mkdir(parents=True)
    write_jsonl(path, rows)
    rel = "work/answers/calibration/picked/records.jsonl"
    assert cli.main(["support", "--records", rel, "--judge", "A"]) == 0
    out = capsys.readouterr().out
    assert [r["key"] for r in read_jsonl(path.with_name("support_A.jsonl"))] == ["jd-9#0", "q-9#0"]
    assert "support haiku+sonnet A: records=2" in out and c["text"] not in out
    # --model still checks every record
    assert cli.main(["support", "--model", "claude-haiku-5-5", "--records", rel, "--judge", "A"]) == 1
    # a record by a model that answers nothing is refused
    write_jsonl(path, rows + [dict(record("q-8", [claim]), model="claude-opus-5-5")])
    assert cli.main(["support", "--records", rel, "--judge", "A"]) == 1
    assert "not answers by an answer model" in capsys.readouterr().err
    # with neither, there is nothing to name the default records file
    assert cli.main(["support", "--judge", "A"]) == 1
