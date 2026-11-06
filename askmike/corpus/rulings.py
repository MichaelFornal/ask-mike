"""Mike's rulings: answers to source conflicts, applied as ops to units and the skill ledger."""
from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from pathlib import Path

from askmike.corpus.extract import Skill
from askmike.corpus.unit import DEPTHS, Number, SkillRef, Unit, validate


class PendingRulings(Exception):
    def __init__(self, ids: list[str]):
        super().__init__(f"unanswered rulings: {', '.join(ids)}")
        self.ids = ids


@dataclass
class Ruling:
    id: str
    question: str
    options: list
    answer: object  # None until Mike answers
    ops: list = field(default_factory=list)


def load_rulings(path) -> list[Ruling]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("rulings file must be a JSON list")
    out = []
    for i, d in enumerate(data):
        if not isinstance(d, dict) or "id" not in d or "question" not in d:
            raise ValueError(f"ruling #{i + 1} is missing id or question")
        out.append(Ruling(d["id"], d["question"], list(d.get("options", [])), d.get("answer"), list(d.get("ops", []))))
    return out


def _unit_from_dict(d: dict) -> Unit:
    d = dict(d)
    d["skills"] = tuple(SkillRef(**s) for s in d.get("skills", []))
    d["numbers"] = tuple(Number(**n) for n in d.get("numbers", []))
    u = Unit(**d)
    errs = validate(u)
    if errs:
        raise ValueError(f"added unit {u.id!r} invalid: {'; '.join(errs)}")
    return u


def _apply_op(op: dict, units: list[Unit], ledger: list[Skill]) -> tuple[list[Unit], list[Skill]]:
    kind = op.get("op")
    if kind == "set_depth":
        term, depth = op["term"].lower(), op["depth"]
        if depth not in DEPTHS:
            raise ValueError(f"bad depth: {depth!r}")
        if not any(s.term.lower() == term for s in ledger):
            raise ValueError("set_depth matched no ledger term")
        ledger = [replace(s, depth=depth) if s.term.lower() == term else s for s in ledger]
        units = [
            replace(u, skills=tuple(replace(r, depth=depth) if r.term.lower() == term else r for r in u.skills))
            for u in units
        ]
    elif kind == "replace":
        find, new = op["find"], op["with"]
        if not find:
            raise ValueError("replace needs a non-empty find")
        units = [replace(u, claim=u.claim.replace(find, new), detail=u.detail.replace(find, new)) for u in units]
    elif kind == "drop":
        ids = list(op["ids"])
        have = {u.id for u in units}
        # name unmatched ids by position in the ruling, never by text (an id can carry a private name)
        missing = [str(i) for i, x in enumerate(ids, 1) if x not in have]
        if missing:
            raise ValueError(f"drop matched no unit for {len(missing)} of {len(ids)} ids "
                             f"(positions {', '.join(missing)})")
        gone = set(ids)
        units = [u for u in units if u.id not in gone]
    elif kind == "add":
        new = _unit_from_dict(op["unit"])
        if any(u.id == new.id for u in units):
            raise ValueError("add would duplicate an existing unit id")
        units = [*units, new]
    else:
        raise ValueError(f"unknown ruling op: {kind!r}")
    return units, ledger


def apply_rulings(units, ledger, rulings, allow_pending: bool = False) -> tuple[list[Unit], list[Skill]]:
    """Apply answered rulings in order. Structure is re-validated per changed unit;
    content gates (run_gates) are re-run by the caller (the gate step and the export)."""
    pending = [r.id for r in rulings if r.answer is None]
    if pending and not allow_pending:
        raise PendingRulings(pending)
    units, ledger = list(units), list(ledger)
    for r in rulings:
        if r.answer is None:
            continue
        for op in r.ops:
            before = {u.id: u for u in units}
            try:
                units, ledger = _apply_op(op, units, ledger)
            except ValueError as e:
                raise ValueError(f"ruling {r.id}: {e}") from None
            except (KeyError, TypeError, AttributeError) as e:
                raise ValueError(f"ruling {r.id}: malformed op {op!r}: {type(e).__name__} {e}") from None
            for u in units:
                if before.get(u.id) is not u and validate(u):
                    raise ValueError(f"ruling {r.id}: unit {u.id} invalid: {'; '.join(validate(u))}")
    return units, ledger
