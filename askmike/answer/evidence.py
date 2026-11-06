"""The evidence pack: which reviewed units the answer model sees, and the ledger lines for named skills."""
from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from askmike.answer.config import K_PER_REQ, K_QUESTION, MAX_PACK, PREV_MAX
from askmike.answer.route import PERSONAL_GOLD
from askmike.gates.text import as_skill, claimable_hits, low_depth_hits

RUNG = "R4"
SUBJECTS_PATH = Path(__file__).with_name("subjects.json")
MAX_LEDGER_LINES = 15
_CAN = "can claim"
_NONE = "has not used; say so, then name the closest thing he has done"
_LIGHT = "only light exposure; say he hasn't used it in production, then name the closest thing"
_LABELS = {"strong": _CAN, "working": _CAN, "none": _NONE, "familiar": _LIGHT, "learning": _LIGHT}


@dataclass(frozen=True)
class Claim:
    type: str  # match | direct | gap
    need: str
    req: str  # an rNN id for a jd claim, else ""
    text: str
    units: tuple[str, ...]


@dataclass(frozen=True)
class Turn:
    """An earlier exchange. Build it with `turn_from`, never by hand.

    `text` is what a later prompt and retrieval query may show of that input: the question text for a
    question turn, and the kept requirement texts joined by "\\n" for a jd turn (never the raw posting).
    """
    text: str
    kept: tuple[Claim, ...]  # the claims its answer kept


def turn_from(kind: str, text: str, requirements, kept) -> Turn:
    """The Turn a follow-up sees. For a jd it never reads `text`, so the raw posting cannot reach a later prompt."""
    if kind == "jd":
        return Turn("\n".join(r.text for r in requirements or ()), tuple(kept))
    return Turn(text, tuple(kept))


@dataclass(frozen=True)
class Pack:
    unit_ids: tuple[str, ...]
    req_units: dict[str, tuple[str, ...]]  # rNN -> that requirement's R4 hits that made the pack, in rank order
    ledger_lines: tuple[str, ...]


@lru_cache(maxsize=1)
def _subjects() -> dict:
    return json.loads(SUBJECTS_PATH.read_text(encoding="utf-8"))


def load_subjects() -> dict[str, str]:
    """Every corpus subject spelling -> its one public display name."""
    return dict(_subjects())


def canonical_subject(subject: str) -> str:
    """The display name for a corpus subject. An unmapped subject (a test pins full coverage) shows as written."""
    return _subjects().get(subject, subject)


def req_key(req_id: str) -> str:
    """'<jd_id>-r03' -> 'r03', the id a claim's `req` carries."""
    return req_id.rsplit("-", 1)[-1]


def _ledger(skills) -> list:
    return [s if hasattr(s, "depth") else as_skill(s) for s in skills]


def ledger_lines(text: str, skills, allow: frozenset) -> tuple[str, ...]:
    """One line per ledger skill whose term or (non-allowed) alias the text names, in ledger order."""
    ledger = _ledger(skills)
    named = {id(s) for s in low_depth_hits(text, ledger, allow) + claimable_hits(text, ledger, allow)}
    return tuple(f"{s.term}: {_LABELS[s.depth]}" for s in ledger if id(s) in named)[:MAX_LEDGER_LINES]


def _merge(ranked: list[list[str]]) -> list[str]:
    """Best rank first; within a rank, the earlier list (requirement) first."""
    out: list[str] = []
    for rank in range(max((len(r) for r in ranked), default=0)):
        for hits in ranked:
            if rank < len(hits) and hits[rank] not in out:
                out.append(hits[rank])
    return out


def build_pack(ladder, kind: str, text: str, topics, requirements, previous: Turn | None, skills,
               allow: frozenset) -> Pack:
    """jd: R4 top K_PER_REQ per requirement. question: R4 top K_QUESTION on the text, prefixed by the
    previous turn's text when there is one. Order: personal gold units for the topics, the previous
    turn's kept units, then the retrieved units; deduped and capped at MAX_PACK."""
    if kind == "jd":
        keys = [req_key(r.id) for r in requirements]
        ranked = [[h.unit_id for h in ladder.run(RUNG, r.text, K_PER_REQ)] for r in requirements]
        match_text = "\n".join(r.text for r in requirements)
    elif kind == "question":
        query = previous.text[:PREV_MAX] + "\n" + text if previous else text
        keys, ranked = [], [[h.unit_id for h in ladder.run(RUNG, query, K_QUESTION)]]
        match_text = text
    else:
        raise ValueError(f"no evidence pack for kind {kind!r}")

    gold = [PERSONAL_GOLD[t] for t in topics or () if t in PERSONAL_GOLD]
    kept = [u for c in previous.kept for u in c.units] if previous else []
    ids: list[str] = []
    for u in gold + kept + _merge(ranked):
        if u not in ids:
            ids.append(u)
    ids = ids[:MAX_PACK]
    in_pack = set(ids)
    req_units = {k: tuple(u for u in hits if u in in_pack) for k, hits in zip(keys, ranked)}
    return Pack(tuple(ids), req_units, ledger_lines(match_text, skills, allow))
