"""The review sheet: Mike ticks, edits or rejects each proposed unit in a markdown file."""
from __future__ import annotations

import re
from dataclasses import dataclass, replace

from askmike.corpus.unit import Unit, content_hash
from askmike.gates.checks import Finding

HEADER = """# Review sheet

How to review:
- Tick approve or reject with an x. Leave both empty to decide later.
- If a claim or detail is close but wrong, edit that text in place on its claim: or detail: line.
- To remove a detail, leave its detail: line empty. A unit needs a claim: to drop a claim, tick reject.
- Keep each field on one line. A line break inside a claim or detail is an error; write \\n for a line break.
- Reject anything you would not say to a hiring manager's face.
- Reject anything about the job search itself: application counts, companies applied to, salary.
- "gates: OK" means the automatic checks passed. It does not mean the unit is safe to publish; you are the last check.
- The automatic company check only knows companies from two sources (the top-50 index and the applied folder), so watch for any other company Mike applied to.
- Leave the "## id" lines alone. Only claim: and detail: lines are read back; the other field lines are ignored.
- "FAIL" lines are gate problems. Fix them by editing the text or reject the unit: export refuses an approved unit that still fails a gate.
- Save, then ask Claude to run ingest.
"""

STATUS = "- [ ] approve   - [ ] reject"
_STATUS = re.compile(r"^- \[([ xX])\] approve\s+- \[([ xX])\] reject\s*$")
_ESC = re.compile(r"\\(\\|n|u[0-9a-f]{4})")
_SEPS = "\r\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029"  # str.splitlines() would split on these
_FIELDS = ("claim:", "detail:", "subject:", "skills:", "numbers:", "gates:", "similar:", "source:", "rev:", "edited:")


@dataclass(frozen=True)
class Decision:
    status: str  # approve | reject | pending
    claim: str | None  # edited claim; None when untouched (a claim is never blanked)
    detail: str | None  # edited detail; None when untouched, "" when Mike blanked it
    hash: str  # content_hash of the UNEDITED unit when decided


def _esc(s: str) -> str:
    s = s.replace("\\", "\\\\").replace("\n", "\\n")
    for c in _SEPS:
        s = s.replace(c, f"\\u{ord(c):04x}")
    return s


def _unesc(s: str) -> str:
    def one(m):
        g = m.group(1)
        return "\n" if g == "n" else "\\" if g == "\\" else chr(int(g[1:], 16))
    return _ESC.sub(one, s)


def _flat(s: str) -> str:
    return " ".join(s.split())


def render_review(units, findings, dupes, decisions) -> str:
    by_unit: dict[str, list] = {}
    for f in findings:
        f = f if isinstance(f, Finding) else Finding(f["gate"], f["unit_id"], f["message"])
        by_unit.setdefault(f.unit_id, []).append(f)
    order = sorted(
        units,
        key=lambda u: (u.subject, 0 if by_unit.get(u.id) else 1, 0 if u.origin == "extract" else 1, u.id),
    )
    out = [HEADER]
    for u in order:
        h = content_hash(u)
        d = decisions.get(u.id)
        if d is not None and d.hash != h:
            d = None  # stale: the content under the decision changed
        claim = d.claim if d and d.claim is not None else u.claim
        detail = d.detail if d and d.detail is not None else u.detail
        edited = [n for n, v in (("claim", d.claim if d else None), ("detail", d.detail if d else None))
                  if v is not None]
        a, r = (d.status == "approve", d.status == "reject") if d else (False, False)
        lines = [
            f"## {u.id}",
            f"- [{'x' if a else ' '}] approve   - [{'x' if r else ' '}] reject",
        ]
        if edited:
            lines.append("edited: " + ", ".join(edited))
        meta = " · ".join(p for p in (u.subject, u.kind, u.when) if p)
        lines += [
            f"claim: {_esc(claim)}",
            f"detail: {_esc(detail)}",
            f"subject: {meta}",
            "skills: " + ", ".join(f"{s.term} ({s.depth})" for s in u.skills),
            "numbers: " + ", ".join(f"{n.value} ({n.source})" for n in u.numbers),
        ]
        fs = by_unit.get(u.id, [])
        lines += [f"gates: FAIL {f.gate}: {_flat(f.message)}" for f in fs] or ["gates: OK"]
        lines += [
            "similar: " + ", ".join(dupes.get(u.id, [])),
            f"source: {u.source}",
            f"rev: {h[:12]}",
        ]
        out.append("\n".join(lines) + "\n")
    return "\n".join(out)


def parse_review(text: str, units) -> tuple[dict[str, Decision], list[str], list[str]]:
    """Returns (decisions, errors, warnings). A block whose id is not a known unit (a ruling dropped
    it since the sheet was rendered) is a warning and yields no decision; every error blocks."""
    by_id = {u.id: u for u in units}
    blocks: list[tuple[str, int, list[tuple[int, str]]]] = []
    for n, line in enumerate(text.split("\n"), 1):
        line = line.removesuffix("\r")
        if line.startswith("## "):
            blocks.append((line[3:].strip(), n, []))
        elif blocks:
            blocks[-1][2].append((n, line))
    errors: list[str] = []
    warnings: list[str] = []
    seen: dict[str, int] = {}
    for bid, n, _ in blocks:
        if bid not in by_id:
            warnings.append(f"line {n}: block {bid} is not a known unit id; ignored")
        elif bid in seen:
            seen[bid] += 1
            errors.append(f"line {n}: duplicate block {bid}")
        else:
            seen[bid] = 1
    decisions: dict[str, Decision] = {}
    for bid, n, body in blocks:
        if bid not in by_id or seen[bid] > 1:
            continue
        u = by_id[bid]
        status, claim, detail, rev, bad = None, None, None, None, False
        claim_ln = n
        for ln, line in body:
            m = _STATUS.match(line)
            if m and status is None:
                a, r = m.group(1) != " ", m.group(2) != " "
                if a and r:
                    errors.append(f"line {ln}: block {bid} has both approve and reject ticked")
                    bad = True
                status = "pending" if not (a or r) else "approve" if a else "reject"
            elif line.startswith("claim:") and claim is None:
                claim, claim_ln = _unesc(line[6:].strip(" \t")), ln
            elif line.startswith("detail:") and detail is None:
                detail = _unesc(line[7:].strip(" \t"))
            elif line.startswith("rev:") and rev is None:
                rev = line[4:].strip()
            elif line.strip() and not m and not line.startswith(_FIELDS):
                errors.append(f"line {ln}: unexpected line in block {bid}; keep each field on one line")
                bad = True
        if status is None:
            errors.append(f"line {n}: block {bid} has no status line")
            continue
        if claim is None:
            errors.append(f"line {n}: block {bid} has no claim line")
            continue
        if bad:
            continue
        if not claim.strip():
            errors.append(f"line {claim_ln}: block {bid} has an empty claim; "
                          "a unit needs a claim, so tick reject instead")
            continue
        if not rev:
            errors.append(f"line {n}: block {bid} has no rev line")
            continue
        h = content_hash(u)
        if not h.startswith(rev):
            h = rev  # the sheet was rendered from older content; merge makes it pending
        same = lambda a, b: a.strip(" \t") == b.strip(" \t")
        edit_c = None if same(claim, u.claim) else claim
        if detail is None or same(detail, u.detail):
            edit_d = None
        elif not detail.strip():
            edit_d = "" if u.detail.strip() else None  # blanked
        else:
            edit_d = detail
        decisions[bid] = Decision(status, edit_c, edit_d, h)
    return decisions, errors, warnings


def merge_decisions(units, old, new) -> dict[str, Decision]:
    current = {u.id: content_hash(u) for u in units}
    out: dict[str, Decision] = {}
    for src in (old, new):
        for uid, d in src.items():
            if uid not in current:
                continue
            if d.hash != current[uid]:
                d = Decision("pending", None, None, d.hash)
            out[uid] = d
    return dict(sorted(out.items()))
