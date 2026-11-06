"""The answer eval items: every posting, recruiter question, gap, injection, off-topic ask and follow-up pair
the answer run puts to a model, one Item each.

Private sets come from $ASK_PRIVATE_DIR/work/evals (M1's sets and requirements). The off-topic asks, the
follow-up pairs and the injection canaries are invented and public, in tests/fixtures/evals.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from askmike.answer.route import PERSONAL_GOLD, personal_topics
from askmike.evals.split import split_of
from askmike.evals.steps import read_jsonl
from askmike.retrieval.requirements import Requirement

FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "evals"
SETS = ("jd", "evidence", "personal", "handoff", "gap", "injection", "offtopic", "followup")
KINDS = ("evidence", "personal", "handoff")  # judge A's question kinds; each is also a set


class KindsIncomplete(ValueError):
    """A recruiter question has no judge-A kind label: the answer run refuses rather than guess its set."""


@dataclass(frozen=True)
class Item:
    item_id: str
    set: str  # one of SETS
    split: str  # split_of(item_id)
    cluster: str  # the item id
    text: str  # the input; for a follow-up pair, the first ask
    expected: tuple[str, ...] = ()  # personal: the gold unit ids; gap: the missing term; else empty
    requirements: tuple[Requirement, ...] = ()  # jd: M1's judged requirements, in order
    must_not: tuple[str, ...] = ()  # injection: canary phrases a rendered answer must not contain
    followup: str = ""  # follow-up pair: the pronoun-only second ask


def _item(item_id: str, set_: str, text: str, **kw) -> Item:
    return Item(item_id, set_, split_of(item_id), item_id, text, **kw)


def question_rows(evals) -> list[dict]:
    """Recruiter questions and handoff phrasings unioned by id (the handoff file repeats some question rows),
    as {id, text, gold}, sorted by id. `gold` is M1's row gold, empty when the row has none."""
    evals = Path(evals)
    rows: dict = {}
    for name in ("questions", "handoffs"):
        for r in read_jsonl(evals / "sets" / f"{name}.jsonl"):
            rows.setdefault(r["id"], {"id": r["id"], "text": r["text"], "gold": list(r.get("gold") or [])})
    return sorted(rows.values(), key=lambda r: r["id"])


def _gold(row: dict) -> tuple[str, ...]:
    if row["gold"]:
        return tuple(row["gold"])
    return tuple(PERSONAL_GOLD[t] for t in personal_topics(row["text"]) if t in PERSONAL_GOLD)


def load_items(evals, kinds: dict, fixtures=FIXTURES) -> list[Item]:
    """Every answer eval item, ordered by set then id. A question's set is its judge-A kind (`kinds`, from
    work/answers/kinds_A.jsonl); a question without a valid label raises KindsIncomplete. A personal
    question's gold is M1's row gold when present, else the PERSONAL_GOLD units of personal_topics(text)."""
    evals, fixtures = Path(evals), Path(fixtures)
    items: list[Item] = []

    reqs: dict = {}
    for r in read_jsonl(evals / "requirements.jsonl"):
        reqs.setdefault(r["jd_id"], []).append(Requirement(r["id"], r["text"], bool(r["required"]), r["kind"]))
    for jd in read_jsonl(evals / "sets" / "jds.jsonl"):
        items.append(_item(jd["id"], "jd", jd["text"], requirements=tuple(reqs.get(jd["id"], ()))))

    rows = question_rows(evals)
    unlabelled = [r["id"] for r in rows if kinds.get(r["id"]) not in KINDS]
    if unlabelled:
        raise KindsIncomplete(f"{len(unlabelled)} of {len(rows)} recruiter questions have no kind label; "
                              "rerun `python -m askmike.evals kinds`")
    for r in rows:
        kind = kinds[r["id"]]
        items.append(_item(r["id"], kind, r["text"], expected=_gold(r) if kind == "personal" else ()))

    for g in read_jsonl(evals / "sets" / "gaps.jsonl"):
        items.append(_item(g["id"], "gap", g["text"], expected=(g["term"],)))

    canaries = {r["id"]: r.get("must_not") or [] for r in read_jsonl(fixtures / "injections.jsonl")}
    for r in read_jsonl(evals / "sets" / "injections.jsonl"):
        must_not = tuple(r.get("must_not") or canaries.get(r["id"]) or ())
        if not must_not:
            raise ValueError(f"injection {r['id']} has no must_not canaries")
        items.append(_item(r["id"], "injection", r["text"], must_not=must_not))

    for r in read_jsonl(fixtures / "offtopic.jsonl"):
        items.append(_item(r["id"], "offtopic", r["text"]))
    for r in read_jsonl(fixtures / "followups.jsonl"):
        items.append(_item(r["id"], "followup", r["first"], followup=r["followup"]))

    dupes = sorted(x for x, n in Counter(i.item_id for i in items).items() if n > 1)
    if dupes:
        raise ValueError(f"item ids repeat across sets: {', '.join(dupes)}")
    return sorted(items, key=lambda i: (SETS.index(i.set), i.item_id))
