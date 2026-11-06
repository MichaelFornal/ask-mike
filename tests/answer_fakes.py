"""Invented fixtures shared by the Answerer and render tests: units, skills, a job description, a fake
ladder and a fake runner. No real model or index is touched."""
import json
import re

from askmike.answer.answerer import JD_ID, Answerer
from askmike.answer.gates import AnswerGateConfig
from askmike.answer.prompt import ANSWER_TEMPLATE_SHA
from askmike.gates.checks import CompanyPattern
from askmike.retrieval.bm25 import Hit
from askmike.retrieval.requirements import Requirement

MODEL = "claude-test-1"
SKILLS = [{"term": "Python", "depth": "strong", "aliases": []},
          {"term": "SQL", "depth": "working", "aliases": ["postgres"]},
          {"term": "Kubernetes", "depth": "none", "aliases": ["k8s"]}]


def _unit(uid, claim, subject="Lab"):
    return {"id": uid, "claim": claim, "detail": "", "subject": subject, "kind": "project", "skills": [],
            "when": ""}


UNITS = {u["id"]: u for u in (
    _unit("u1", "Mike cut nightly run time by 40% across 1,200 jobs with Python."),
    _unit("u2", "Mike ran 77 load tests on the queue."),
    _unit("u3", "Mike moved the nightly reports to SQL."),
    _unit("u4", "Mike wrote the depot importer."),
)}

JD = "\n".join([
    "Data Engineer, Gadgetron Robotics",
    "You will build nightly batch pipelines in Python.",
    "You will write and tune SQL for reporting.",
    "You will run load tests before each release.",
    "Nice to have: experience with Kubernetes.",
    "We work in small teams and ship weekly.",
])
REQS = [Requirement(f"{JD_ID}-r01", "Python batch pipelines", True, "skill"),
        Requirement(f"{JD_ID}-r02", "SQL for reporting", True, "skill"),
        Requirement(f"{JD_ID}-r03", "load testing", False, "experience")]
EXTRACTED = [{"text": r.text, "required": r.required, "kind": r.kind} for r in REQS]


class FakeLadder:
    """Returns the same invented units for every query, and records each call."""

    def __init__(self, ids=("u1", "u2", "u3", "u4")):
        self.ids, self.calls = list(ids), []

    def run(self, rung, query, k=10):
        self.calls.append((rung, query, k))
        return [Hit(u, 1.0) for u in self.ids[:k]]


class FakeRunner:
    """Answers an extraction call (no schema) with `extract`, and an answer call with `answer`. Either
    may be a reply dict, an exception to raise, or a callable taking the prompt."""

    def __init__(self, answer=None, extract=None):
        self.answer, self.extract, self.calls = answer, extract, []

    def __call__(self, prompt, model, schema=None, effort=None):
        self.calls.append({"prompt": prompt, "model": model, "schema": schema, "effort": effort})
        reply = self.answer if schema is not None else self.extract
        if isinstance(reply, BaseException):
            raise reply
        return reply(prompt) if callable(reply) else reply


def structured(state="answered", claims=()):
    out = {"state": state, "claims": list(claims)}
    return {"result": json.dumps(out), "structured_output": out, "usage": {"input_tokens": 50, "output_tokens": 9}}


def claim(text, units=("u1",), type="direct", need="", req=""):
    return {"type": type, "need": need, "req": req, "text": text, "units": list(units)}


GOOD = claim("Mike cut nightly run time by 40% across 1,200 jobs with Python.")
INVENTED_NUMBER = claim("Mike cut nightly run time by 65% across 9,000 jobs.")


def cfg():
    return AnswerGateConfig([re.compile("zzq[0-9]+x", re.I)], [CompanyPattern("Quillfeather Labs")],
                            frozenset(), SKILLS)


def make(runner, ladder=None):
    return Answerer(ladder or FakeLadder(), UNITS, SKILLS, cfg(), runner, MODEL)


def assert_fallback(o, reason):
    assert o.outcome == "fallback" and o.fallback_reason == reason
    assert o.kept == () and o.template_sha == ANSWER_TEMPLATE_SHA and isinstance(o.ms, int)
