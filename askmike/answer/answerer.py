"""The answerer: one visitor input in, one gated Outcome out.

It routes the input, builds the evidence pack, makes one structured-output model call, gates every claim
and returns an Outcome. It never raises and never returns ungated text: every failure (a runner error or
timeout, a refusal, a truncated or unparseable reply, a bad state, a bug in pack building or gating) is a
fallback Outcome with a reason. render.py turns an Outcome into what a visitor sees.

`model` is one model id, or a map from route kind ("question", "jd") to the model that answers that route
(Mike's pick in settings.json). A jd's requirement extraction runs on the jd model. Each Outcome names the
model its route was sent to.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field, replace

from askmike.answer.config import ANSWER_EFFORT, MIN_KEPT, PREV_MAX
from askmike.answer.evidence import Claim, Pack, Turn, build_pack, turn_from
from askmike.answer.gates import AnswerGateConfig, Verdict, gate_claims
from askmike.answer.prompt import ANSWER_TEMPLATE_SHA, render_prompt
from askmike.answer.route import route
from askmike.answer.schema import ANSWER_SCHEMA, STATES
from askmike.llm.ask import first_json, prompt_sha
from askmike.llm.ledger import MemoryLedger
from askmike.retrieval.requirements import Requirement, extract_requirements

OUTCOMES = ("answer", "handoff", "off_topic", "fallback", "empty", "too_long")
# "budget": the spend ledger refused a model call (the runner raised a RuntimeError whose kind is "budget"). "busy"
# and "limited" are built by the service desk, which answers them without asking the Answerer.
FALLBACK_REASONS = ("", "no_evidence", "gated_empty", "parse", "runner_error", "extract", "budget", "busy", "limited")
JD_ID = "input"  # a visitor's posting has no id; its requirements are input-r01, input-r02, ...
# routes answered with a fixed line and no model call; their outcomes never become a follow-up's previous turn
NO_CALL = ("empty", "too_long", "handoff")


def gate_input(text, previous: Turn | None) -> str:
    """The visitor's own words exactly as the model saw them: the previous turn's text cut to PREV_MAX (the
    slice the prompt and the retrieval query take), then this turn's text. The gates read it as the input
    text, and the eval audit recomputes it, so both allow the same company names."""
    text = text if isinstance(text, str) else ""
    return f"{previous.text[:PREV_MAX]}\n{text}" if previous is not None else text


@dataclass(frozen=True)
class Outcome:
    outcome: str  # one of OUTCOMES
    state: str  # the model's state; "" when no model reply was read
    kept: tuple[Claim, ...]  # the claims a visitor sees; empty for every outcome but "answer"
    verdicts: tuple[Verdict, ...]  # one per model claim when the gates ran
    fallback_reason: str  # one of FALLBACK_REASONS; "" unless outcome is "fallback"
    prompt_sha: str  # "" when no answer prompt was rendered
    template_sha: str
    pack: Pack | None
    requirements: tuple[Requirement, ...]  # a jd's requirements; empty for a question
    usage: dict
    ms: int
    kind: str = ""  # the route kind: empty | too_long | handoff | jd | question ("" if routing failed)
    model: str = ""  # the model this route was sent to; "" when no model call was needed

    def turn(self, text: str) -> Turn | None:
        """The Turn a follow-up to this input sees, built by turn_from (a jd keeps its requirement lines,
        never the posting). None for an empty, over-long or handed-off input, or one that never routed, so
        a raw over-limit posting or a None text never becomes a previous turn."""
        if self.outcome in NO_CALL or self.kind not in ("jd", "question"):
            return None
        return turn_from(self.kind, text, self.requirements, self.kept)


@dataclass
class _Progress:
    """What an ask has built so far, so a failure at any step still reports it."""
    kind: str = ""
    model: str = ""
    state: str = ""
    requirements: tuple = ()
    pack: Pack | None = None
    prompt_sha: str = ""
    usage: dict = field(default_factory=dict)
    verdicts: tuple = ()


def model_for(model: str | dict[str, str], kind: str) -> str:
    """The model that answers a route kind: `model` itself when it is one id, else the map's entry for `kind`."""
    return model if isinstance(model, str) else model[kind]


class Answerer:
    def __init__(self, ladder, units: dict, skills, cfg: AnswerGateConfig, runner, model: str | dict[str, str],
                 effort: str = ANSWER_EFFORT):
        self.ladder, self.units, self.skills, self.cfg = ladder, units, skills, cfg
        self.runner, self.model, self.effort = runner, model, effort

    def model_for(self, kind: str) -> str:
        return model_for(self.model, kind)

    def ask(self, text, previous: Turn | None = None, requirements: list[Requirement] | None = None) -> Outcome:
        t0 = time.monotonic()
        p = _Progress()
        try:
            outcome, reason, kept = self._ask(text, previous, requirements, p)
        except Exception:  # never raise: anything unexpected is a runner_error fallback
            outcome, reason, kept = "fallback", "runner_error", ()
        return Outcome(outcome, p.state, kept, p.verdicts, reason, p.prompt_sha, ANSWER_TEMPLATE_SHA, p.pack,
                       p.requirements, p.usage, int((time.monotonic() - t0) * 1000), p.kind, p.model)

    def _ask(self, text, previous, requirements, p: _Progress) -> tuple[str, str, tuple]:
        r = route(text)
        p.kind = r.kind
        if r.kind in NO_CALL:
            return r.kind, "", ()
        model = p.model = self.model_for(r.kind)

        reqs: list = []
        if r.kind == "jd":
            if requirements is None:
                refused: list = []
                reqs, stats = extract_requirements(JD_ID, text, _noting_refusals(self.runner, refused),
                                                   MemoryLedger(), model=model)
                if stats.error:
                    return "fallback", "budget" if refused else "extract", ()
            else:
                reqs = list(requirements)
            p.requirements = tuple(reqs)
            if not reqs:
                return "fallback", "extract", ()

        previous = self._clean(previous)
        p.pack = build_pack(self.ladder, r.kind, text, r.topics, reqs, previous, self.skills,
                            self.cfg.honesty_allow)
        prompt = render_prompt(r.kind, text, reqs, p.pack, self.units, previous)
        p.prompt_sha = prompt_sha(prompt)
        try:
            reply = self.runner(prompt, model, schema=ANSWER_SCHEMA, effort=self.effort)
        except RuntimeError as e:
            return "fallback", "budget" if _refused(e) else "runner_error", ()
        if not isinstance(reply, dict):
            return "fallback", "runner_error", ()
        usage = reply.get("usage")
        p.usage = dict(usage) if isinstance(usage, dict) else {}

        data = reply.get("structured_output")
        if not isinstance(data, dict):
            data = first_json(str(reply.get("result") or ""), dict)
        if not isinstance(data, dict) or data.get("state") not in STATES:
            return "fallback", "parse", ()
        p.state = data["state"]
        if p.state in ("handoff", "off_topic"):
            return p.state, "", ()
        if p.state == "no_evidence":
            return "fallback", "no_evidence", ()
        raw = data.get("claims")
        if not isinstance(raw, list):
            return "fallback", "parse", ()

        # the visitor's own words, this turn's and the previous one's as the model saw them
        seen = gate_input(text, previous)
        verdicts, kept = gate_claims([_claim(c) for c in raw], p.pack, self.units, self.cfg, seen, r.kind)
        p.verdicts = tuple(verdicts)
        if len(kept) < MIN_KEPT[r.kind]:
            return "fallback", "gated_empty", ()
        return "answer", "", tuple(kept)

    def _clean(self, previous: Turn | None) -> Turn | None:
        """The caller's previous Turn with every cited id outside the corpus dropped, and every claim left
        with no ids dropped, so a stale or forged id never reaches the prompt."""
        if previous is None:
            return None
        kept = []
        for c in previous.kept:
            ids = tuple(u for u in c.units if isinstance(u, str) and u in self.units)
            if ids:
                kept.append(replace(c, units=ids))
        return replace(previous, kept=tuple(kept))


def _refused(e: BaseException) -> bool:
    """The spend ledger refused the call before it was made."""
    return getattr(e, "kind", None) == "budget"


def _noting_refusals(runner, refused: list):
    """`runner` for M1's extraction, which turns a runner error into a string: a call the spend ledger refused is
    noted in `refused`, so that failure still reads as the budget fallback."""
    def run(prompt, model):
        try:
            return runner(prompt, model)
        except RuntimeError as e:
            if _refused(e):
                refused.append(e)
            raise
    return run


def _claim(item) -> Claim:
    """A model claim as the gates read it. Fields are passed through unchecked (the structure gate judges
    them); an item that is not an object becomes an empty claim, which fails structure."""
    if not isinstance(item, dict):
        item = {}
    units = item.get("units")
    return Claim(item.get("type"), item.get("need"), item.get("req"), item.get("text"),
                 tuple(units) if isinstance(units, (list, tuple)) else ())
