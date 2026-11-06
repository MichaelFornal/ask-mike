"""The support judge's planted calibration: known-good and known-bad claims built from the public corpus only.

120 claims, each from its own unit. The easy slice (`planted`, corpus only, 80):
- 40 positive: a unit's own claim, citing itself.
- 20 wrong_unit: a claim that names its own subject, citing a unit of another subject that never names it.
  Person-level units (subject "Mike") take no part, since they restate project work.
- 10 number: the claim's first digit token tripled, citing its own unit. A year first is skipped.
- 10 skill: the claim plus " using <a one-word none-depth ledger term>" the unit never names, citing itself.

The hard slice (40), which a judge that only matches text cannot pass:
- 10 sibling (corpus only): a claim citing another unit of the same canonical subject that shares no non-year
  number with it and little wording (token Jaccard < 0.3, against its claim and its whole text).
- 20 paraphrase: a unit's claim rewritten by claude-sonnet-5-5 (ledgered), kept only when it is no
  substring of the unit's evidence line and every number and name in it is grounded in that unit alone.
  It cites its own unit.
- 10 paraphrase_number: a kept paraphrase of another unit with its first non-year number tripled, citing
  its own unit.

A partial or unsupported verdict on a negative is right. The judge must reach pos_acc >= 0.95 and
neg_acc >= 0.90 over all 120 before any support number is published. judge-calib writes the set to
$ASK_PRIVATE_DIR/work/answers/calibration/planted.jsonl (its sha1 goes in the score file) and the verdicts
to support_<judge>.jsonl beside it. The paraphrases are fixed by paraphrase_ledger.jsonl there.
"""
from __future__ import annotations

import hashlib
import re
from decimal import Decimal
from pathlib import Path

from askmike.answer.evidence import _ledger, canonical_subject
from askmike.answer.gates import _grounded
from askmike.answer.prompt import evidence_line
from askmike.evals.judge import JUDGE_B, _line
from askmike.evals.steps import _map
from askmike.evals.support import JUDGES, VERDICTS, judge_claims, template_sha
from askmike.gates.checks import _NUM, _is_year, _norm, digit_tokens
from askmike.gates.text import _term_re
from askmike.llm.ask import ask_json

N_POS, N_WRONG, N_NUMBER, N_SKILL = 40, 20, 10, 10
N_SIBLING, N_PARA, N_PARA_NUMBER = 10, 20, 10
KINDS = ("positive", "paraphrase", "wrong_unit", "number", "skill", "sibling", "paraphrase_number")
BATCH = 10  # planted claims per judge call, shuffled so each call mixes good and bad
PARA_BATCH = 10  # claims per paraphrase call
PARA_MODEL = JUDGE_B  # claude-sonnet-5-5
PARA_TEMPLATE = Path(__file__).parent / "prompts" / "paraphrase.md"
JACCARD_MAX = 0.3
POS_BAR, NEG_BAR = 0.95, 0.90
PERSON = "Mike"  # the person-level canonical subject


class PlantedIncomplete(RuntimeError):
    """A paraphrase call failed (an error or the deadline): the planted set is not built; rerun."""


def _h(*parts) -> str:
    return hashlib.sha1(":".join(map(str, parts)).encode("utf-8")).hexdigest()


def _triple(text: str, skip_years: bool = False):
    """(new text, old token, new token) with the first digit token tripled, or None when there is none, it is
    a year, or tripling leaves it as written. With skip_years, the first non-year token is tripled instead.
    Commas, decimals and a K/M/B/% suffix keep their form."""
    found = [m for m in _NUM.finditer(text) if not (skip_years and _is_year(m.group(0)))]
    m = found[0] if found else None
    if m is None or _is_year(m.group(0)):
        return None
    whole, frac, suffix = m.group(1), m.group(2) or "", m.group(3) or ""
    value = Decimal(_norm(whole) + frac) * 3
    places = max(len(frac) - 1, 0)
    new = (f"{value:,.{places}f}" if "," in whole else f"{value:.{places}f}") + suffix
    if new == m.group(0):
        return None
    return text[:m.start()] + new + text[m.end():], m.group(0), new


def triple_first_number(text: str) -> str | None:
    t = _triple(text)
    return t[0] if t else None


def _names(u: dict) -> set[str]:
    return {n for n in (u.get("subject") or "", canonical_subject(u.get("subject") or "")) if n.strip()}


def _mentions(text: str, name: str) -> bool:
    return re.search(r"(?<!\w)" + re.escape(name) + r"(?!\w)", text, re.I) is not None


def _parts(canon: str) -> frozenset:
    return frozenset(p.strip().lower() for p in canon.split(" and "))


def _hay(u: dict) -> str:
    skills = " ".join(s.get("term", "") for s in u.get("skills") or [] if isinstance(s, dict))
    return "\n".join([u.get("claim") or "", u.get("detail") or "", skills])


def _canon(u: dict) -> str:
    return canonical_subject(u.get("subject") or "")


def can_cite(src: dict, v: dict) -> bool:
    """A wrong-unit negative built from `src` may cite `v`: neither is person-level, their canonical subjects
    share no part ("Capgemini and Bridge" shares one with "Bridge"), and `v` never names src's subject."""
    if PERSON in (_canon(src), _canon(v)) or _parts(_canon(src)) & _parts(_canon(v)):
        return False
    return not any(_mentions(_hay(v) + "\n" + (v.get("subject") or ""), n) for n in _names(src))


def planted(units, skills, seed: int = 0) -> list[dict]:
    """The planted set: deterministic for (units, skills, seed), whatever order `units` (a list or an id map)
    comes in. Each row: an opaque id, kind, expect (supported | not_supported), text, cited units, source."""
    pool = sorted((u for u in (units.values() if isinstance(units, dict) else units)
                   if isinstance(u.get("claim"), str) and u["claim"].strip()),
                  key=lambda u: _h("planted", seed, u["id"]))
    ledger = _ledger(skills)
    terms = [(s.term, (s.term, *s.aliases)) for s in sorted(ledger, key=lambda s: s.term)
             if s.depth == "none" and not re.search(r"\s", s.term) and _term_re(s.term).search(s.term)]
    used: set[str] = set()
    out: list[dict] = []

    def number(u):
        t = _triple(u["claim"])
        if t is None or _norm(t[2]) in {_norm(x) for x in digit_tokens(_hay(u))}:
            return None
        return t[0], [u["id"]]

    def skill(u):
        free = [t for t, names in terms if not any(_term_re(n).search(_hay(u)) for n in names)]
        if not free:
            return None
        term = min(free, key=lambda t: _h("term", seed, u["id"], t))
        claim = u["claim"].rstrip()
        body, end = (claim[:-1], claim[-1]) if claim[-1] in ".!?" else (claim, "")
        return f"{body} using {term}{end}", [u["id"]]

    def wrong(u):
        if _canon(u) == PERSON or not any(_mentions(u["claim"], n) for n in _names(u)):
            return None  # only a claim that names its own subject is sure to be wrong about another
        cands = [v for v in pool if can_cite(u, v)]
        if not cands:
            return None
        return u["claim"], [min(cands, key=lambda v: _h("cite", seed, u["id"], v["id"]))["id"]]

    def take(kind, n, make):
        got = 0
        for u in pool:
            if got == n:
                break
            made = None if u["id"] in used else make(u)
            if made is None:
                continue
            used.add(u["id"])
            got += 1
            out.append({"id": "pl-" + _h("id", seed, kind, u["id"])[:10], "kind": kind,
                        "expect": "supported" if kind == "positive" else "not_supported",
                        "text": made[0], "units": list(made[1]), "source": u["id"]})
        if got < n:
            raise ValueError(f"the corpus gives only {got} of {n} {kind} planted claims")

    take("positive", N_POS, lambda u: (u["claim"], [u["id"]]))
    take("number", N_NUMBER, number)
    take("skill", N_SKILL, skill)
    take("wrong_unit", N_WRONG, wrong)
    return out


# ---- the hard slice ------------------------------------------------------------

def _row(kind: str, u: dict, text: str, cites, seed) -> dict:
    return {"id": "pl-" + _h("id", seed, kind, u["id"])[:10], "kind": kind,
            "expect": "supported" if kind in ("positive", "paraphrase") else "not_supported",
            "text": text, "units": list(cites), "source": u["id"]}


def _pool(units, tag: str, seed) -> list[dict]:
    return sorted((u for u in (units.values() if isinstance(units, dict) else units)
                   if isinstance(u.get("claim"), str) and u["claim"].strip()), key=lambda u: _h(tag, seed, u["id"]))


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def _jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if a | b else 1.0


def _numbers(text: str) -> set[str]:
    return {_norm(t) for t in digit_tokens(text) if not _is_year(t)}


def sibling_ok(src: dict, v: dict) -> bool:
    """`v` may stand as the wrong evidence for src's claim: the same canonical subject, no shared non-year
    number, and token Jaccard below JACCARD_MAX against v's claim and against its whole text."""
    if v["id"] == src["id"] or _canon(v) != _canon(src) or _numbers(src["claim"]) & _numbers(_hay(v)):
        return False
    claim = _tokens(src["claim"])
    return max(_jaccard(claim, _tokens(v.get("claim") or "")), _jaccard(claim, _tokens(_hay(v)))) < JACCARD_MAX


def siblings(units, seed: int = 0, used=frozenset()) -> list[dict]:
    """N_SIBLING same-subject negatives from units outside `used`; corpus only."""
    pool, out = _pool(units, "sibling", seed), []
    for u in pool:
        if len(out) == N_SIBLING:
            break
        cands = [v for v in pool if sibling_ok(u, v)] if u["id"] not in used else []
        if cands:
            v = min(cands, key=lambda v: _h("sibling-cite", seed, u["id"], v["id"]))
            out.append(_row("sibling", u, u["claim"], [v["id"]], seed))
    if len(out) < N_SIBLING:
        raise ValueError(f"the corpus gives only {len(out)} of {N_SIBLING} sibling planted claims")
    return out


def build_paraphrase_prompt(claims) -> str:
    """claims: [(local id, claim)], as JSON lines ("</" escaped); the template is filled in one pass."""
    block = "\n".join(_line({"id": sid, "text": text}) for sid, text in claims)
    return re.sub(r"\{statements\}", lambda m: block, PARA_TEMPLATE.read_text(encoding="utf-8"))


def _paraphrase_round(key, batch, runner, ledger) -> dict[str, str | None]:
    """{unit id: rewrite or None} for one call. A reply that does not parse gives None for each; a failed
    call raises PlantedIncomplete, since every later draw depends on this one."""
    local = [(f"s{i}", u) for i, u in enumerate(batch, 1)]
    res = ask_json(key, build_paraphrase_prompt([(sid, u["claim"]) for sid, u in local]), PARA_MODEL, runner,
                   ledger, dict)
    note = getattr(runner, "note", None)
    if note is not None:
        note(res.ok, res.error)
    if not res.ok and res.error != "parse":
        raise PlantedIncomplete(f"paraphrase call {key} failed: {res.error}")
    value = res.value if res.ok else {}
    out = {}
    for sid, u in local:
        text = value.get(sid)
        out[u["id"]] = " ".join(text.split()) if isinstance(text, str) and text.strip() else None
    return out


def _check(u: dict, text, names: str, stats: dict) -> bool:
    """A rewrite stands as a supported claim: present, no substring of the unit's evidence line, and every
    number and name grounded in the unit alone (`names` is ""; stricter than the answer gate, which also
    accepts ledger terms)."""
    if text is None:
        stats["no_reply"] += 1
    elif text in evidence_line(u["id"], u) or text == u["claim"]:
        stats["verbatim"] += 1
    elif not _grounded(text, [u], names, "", ()):
        stats["ungrounded"] += 1
    else:
        return True
    return False


def paraphrased(units, runner, ledger, seed: int = 0, used=frozenset()) -> tuple[list[dict], dict]:
    """N_PARA paraphrase positives, then N_PARA_NUMBER paraphrase_number negatives from other units, each
    drawn PARA_BATCH candidates per call, in a seeded order, until enough are kept. Returns (rows, stats)."""
    stats = {"calls": 0, "drawn": 0, "no_reply": 0, "verbatim": 0, "ungrounded": 0, "no_number": 0}
    used = set(used)

    def draw(kind, n, cands, make):
        out = []
        for r, i in enumerate(range(0, len(cands), PARA_BATCH)):
            batch = cands[i:i + PARA_BATCH]
            got = _paraphrase_round(f"para:{seed}:{kind}:{r}", batch, runner, ledger)
            stats["calls"] += 1
            for u in batch:
                if len(out) == n:
                    break
                stats["drawn"] += 1
                row = make(u, got[u["id"]]) if _check(u, got[u["id"]], "", stats) else None
                if row is not None:
                    out.append(row)
                    used.add(u["id"])
            if len(out) == n:
                return out
        raise ValueError(f"the corpus gives only {len(out)} of {n} {kind} planted claims")

    def number(u, text):
        t = _triple(text, skip_years=True)
        if t is None or _norm(t[2]) in {_norm(x) for x in digit_tokens(_hay(u))}:
            stats["no_number"] += 1
            return None
        return _row("paraphrase_number", u, t[0], [u["id"]], seed)

    pos = draw("paraphrase", N_PARA, [u for u in _pool(units, "paraphrase", seed) if u["id"] not in used],
               lambda u, text: _row("paraphrase", u, text, [u["id"]], seed))
    neg = draw("paraphrase_number", N_PARA_NUMBER,
               [u for u in _pool(units, "paraphrase-number", seed) if u["id"] not in used and _numbers(u["claim"])],
               number)
    return pos + neg, stats


def planted_set(units, skills, runner, ledger, seed: int = 0) -> tuple[list[dict], dict]:
    """The full 120: the easy slice, then the siblings, then the paraphrases (drawn last, so the corpus-only
    rows never depend on a model reply). Returns (rows, paraphrase stats)."""
    easy = planted(units, skills, seed)
    used = {r["source"] for r in easy}
    sib = siblings(units, seed, used)
    para, stats = paraphrased(units, runner, ledger, seed, used | {r["source"] for r in sib})
    return easy + sib + para, stats


def batches(rows, seed: int = 0) -> list[list[dict]]:
    """The planted claims in a seeded hash order, BATCH per call, so good and bad mix within a call."""
    order = sorted(rows, key=lambda p: _h("order", seed, p["id"]))
    return [order[i:i + BATCH] for i in range(0, len(order), BATCH)]


def judge_planted(rows, units: dict, skills, judge, runner, ledger, allow=frozenset(), workers: int = 4,
                  seed: int = 0) -> dict[str, dict]:
    """{planted id: verdict row}, one ledgered call per batch, through the same prompt answers get."""
    model, tsha = JUDGES[judge], template_sha()

    def one(numbered):
        n, batch = numbered
        local = [(f"c{i}", p["text"], tuple(p["units"])) for i, p in enumerate(batch, 1)]
        got, res = judge_claims(f"calib{judge}:{seed}:{n}", local, units, skills, model, runner, ledger, allow)
        return {p["id"]: {"verdict": got[cid], "kind": p["kind"], "judge": judge, "model": model,
                          "prompt_sha": res.prompt_sha, "template_sha": tsha}
                for (cid, _, _), p in zip(local, batch) if cid in got}

    out: dict[str, dict] = {}
    for part in _map(one, list(enumerate(batches(rows, seed))), workers):
        out.update(part)
    return dict(sorted(out.items()))


def score(rows, verdicts: dict) -> dict:
    """pos_acc = supported / positives; neg_acc = (partial + unsupported) / negatives. An unjudged claim
    stays in the denominator (it is not a right answer); pass_ needs both bars."""
    def said(p):
        row = verdicts.get(p["id"])
        v = row.get("verdict") if isinstance(row, dict) else None
        return v if v in VERDICTS else None

    def right(p):
        v = said(p)
        return v is not None and (v == "supported") == (p["expect"] == "supported")

    def acc(ps):
        return sum(1 for p in ps if right(p)) / len(ps) if ps else None

    pos = [p for p in rows if p["expect"] == "supported"]
    neg = [p for p in rows if p["expect"] != "supported"]
    pos_acc, neg_acc = acc(pos), acc(neg)
    by_kind = {}
    for k in [k for k in KINDS if any(p["kind"] == k for p in rows)]:
        ps = [p for p in rows if p["kind"] == k]
        by_kind[k] = {"n": len(ps), "n_judged": sum(1 for p in ps if said(p) is not None), "acc": acc(ps)}
    return {"n": len(rows), "n_positive": len(pos), "n_negative": len(neg),
            "n_unjudged": sum(1 for p in rows if said(p) is None), "pos_acc": pos_acc, "neg_acc": neg_acc,
            "pass_": pos_acc is not None and neg_acc is not None and pos_acc >= POS_BAR and neg_acc >= NEG_BAR,
            "by_kind": by_kind}
