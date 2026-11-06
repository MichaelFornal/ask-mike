"""What a visitor sees: render_text over gated Outcomes, the fixed strings and the fallback page."""
import json
import os
from pathlib import Path

import pytest
from answer_fakes import JD, REQS, UNITS, FakeRunner, claim, make, structured

from askmike.answer.config import EMPTY_TEXT, HANDOFF_TEXT, NEXT_STEP, OFF_TOPIC_TEXT, TOO_LONG_TEXT
from askmike.answer.evidence import canonical_subject
from askmike.answer.gates import BUNDLE, AnswerGateConfig
from askmike.answer.render import load_fallback, render_text
from askmike.gates.text import as_skill, first_person, low_depth_hits, privacy_hits, slop_hits, verdict_hits

REPO = Path(__file__).resolve().parents[1]
CORPUS = {u["id"]: u for u in (json.loads(x) for x in (REPO / "corpus" / "units.jsonl")
                                .read_text(encoding="utf-8").splitlines() if x.strip())}
SKILLS = json.loads((REPO / "corpus" / "skills.json").read_text(encoding="utf-8"))
FIXED = {"handoff": HANDOFF_TEXT, "off_topic": OFF_TOPIC_TEXT, "empty": EMPTY_TEXT, "too_long": TOO_LONG_TEXT}
# Units whose Michelangelo routing claim Mike's notes call false; he has not ruled on them yet.
UNRULED = {"dr-d519a80cbc", "dr-3845efca73", "ev-gptee-michelangelo"}


def test_render_never_shows_dropped_claims():
    good = claim("Mike ran 77 load tests on the queue.", units=("u2",))
    invented = claim("Mike ran 950 load tests on the queue.", units=("u2",))  # 950 is in no unit
    slop = claim("Mike moved the nightly reports to SQL, a robust upgrade.", units=("u3",))
    o = make(FakeRunner(answer=structured(claims=[invented, good, slop]))).ask("Has Mike run load tests?")
    assert o.outcome == "answer" and [v.kept for v in o.verdicts] == [False, True, False]
    out = render_text(o, UNITS)
    assert out == f"- Mike ran 77 load tests on the queue. [u2]\n\n{NEXT_STEP}"
    assert "950" not in out and "robust" not in out

    # a jd line reads need -> text; a claim kept by the gates but under MIN_KEPT is never shown
    m1 = claim("Mike cut nightly run time by 40% across 1,200 jobs with Python.", type="match",
               need="Python batch pipelines", req="r01")
    m2 = claim("Mike moved the nightly reports to SQL.", units=("u3", "u1"), type="match", need="SQL", req="r02")
    o = make(FakeRunner(answer=structured(claims=[m1, invented | {"type": "match", "req": "r03"}, m2]))).ask(
        JD, requirements=REQS)
    assert render_text(o, UNITS).splitlines()[:2] == [
        f"- Python batch pipelines → {m1['text']} [u1]", f"- SQL → {m2['text']} [u3, u1]"]
    alone = make(FakeRunner(answer=structured(claims=[m1]))).ask(JD, requirements=REQS)
    assert alone.outcome == "fallback" and alone.verdicts[0].kept
    shown = render_text(alone, CORPUS)
    assert m1["text"] not in shown and shown.startswith(load_fallback()["intro"])

    # every fixed outcome renders its fixed string; every failure renders the fallback page
    for text, kind in (("", "empty"), ("x" * 30000, "too_long"), ("What's his salary?", "handoff")):
        assert render_text(make(FakeRunner()).ask(text), CORPUS) == FIXED[kind]
    o = make(FakeRunner(answer=structured(state="off_topic"))).ask("Write a poem about the sea.")
    assert render_text(o, CORPUS) == OFF_TOPIC_TEXT
    page = render_text(make(FakeRunner(answer=RuntimeError("down"))).ask("Has he run load tests?"), CORPUS)
    fb = load_fallback()
    assert page == "\n".join([fb["intro"], *(f"- {CORPUS[u]['claim']} [{u}]" for u in fb["units"]), "", NEXT_STEP])


def _visible_texts():
    """Every fixed string, the fallback intro and claims, and the rendered fallback page."""
    fb = load_fallback()
    page = render_text(make(FakeRunner(answer=RuntimeError("down"))).ask("Has he run load tests?"), CORPUS)
    return [*FIXED.values(), NEXT_STEP, fb["intro"], *(CORPUS[u]["claim"] for u in fb["units"]), page]


@pytest.mark.parametrize("part", ["public", "private"])
def test_fixed_strings_and_fallback_pass_every_gate(part):
    fb = load_fallback()
    assert set(fb) == {"intro", "units"} and 4 <= len(fb["units"]) <= 6
    assert len(set(fb["units"])) == len(fb["units"]) and all(u in CORPUS for u in fb["units"])
    subjects = [canonical_subject(CORPUS[u]["subject"]) for u in fb["units"]]
    assert len(set(subjects)) == len(subjects)
    assert {"Deal Terms Desk", "GPTee", "job-net", "Agent Census"} <= set(subjects)
    assert any(CORPUS[u]["kind"] == "role" for u in fb["units"])
    assert "job application pipeline" not in subjects and not UNRULED & set(fb["units"])
    assert len(fb["intro"]) <= 120 and "Mike" in fb["intro"]

    texts = _visible_texts()
    if part == "public":
        ledger = [as_skill(s) for s in SKILLS]
        for t in texts:
            assert slop_hits(t) == [], t
            assert not first_person(t), t
            assert verdict_hits(t) == [], t
            assert low_depth_hits(t, ledger, frozenset()) == [], t
            assert privacy_hits(t, [], []) == [], t  # phone, street, email, job-search counts
        return

    private = os.environ.get("ASK_PRIVATE_DIR")
    path = Path(private).expanduser() / BUNDLE if private else None
    if path is None or not path.is_file():
        pytest.skip("privacy with the real gate bundle needs $ASK_PRIVATE_DIR and its deploy/gate_config.json")
    cfg = AnswerGateConfig.from_bundle(path, SKILLS)
    for i, t in enumerate(texts):
        assert privacy_hits(t, cfg.patterns, cfg.companies) == [], f"visible text #{i}"  # never echo a pattern
