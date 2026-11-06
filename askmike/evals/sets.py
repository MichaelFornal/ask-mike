"""Build the eval sets (JDs, recruiter questions, gaps, handoffs, injections).

Raw inputs are private ($ASK_PRIVATE_DIR/raw); outputs go to a private folder. No output row
records where a JD or question came from.
"""
from __future__ import annotations

import hashlib
import json
import random
import re
import warnings
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from askmike.answer.route import PERSONAL_GOLD, personal_topics, question_kind  # noqa: F401  (moved; re-exported)
from askmike.evals.split import split_of

FAMILIES = ("ai_ml", "fde_solutions", "software", "gtm", "data", "product", "other")
LOW_DEPTHS = ("none", "familiar", "learning")
FIXTURE_INJECTIONS = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "evals" / "injections.jsonl"

# ordered, first match wins
_FAMILY_RULES = [
    ("ai_ml", r"\b(ai|a\.i\.|ml|llm|genai|nlp|mlops|llmops|machine learning|deep learning|artificial intelligence|prompt|agentic|applied scientist|computer vision)\b|member of technical staff|research (engineer|scientist)|researcher"),
    ("data", r"data (engineer|scientist)|analytics engineer"),
    ("product", r"product (manager|owner|lead)"),
    ("fde_solutions", r"forward[- ]deployed|\bsolutions? (engineer|architect|consultant)|sales engineer|implementation|customer engineer|deployment engineer|\bfde\b|technical account"),
    ("gtm", r"customer success"),
    ("software", r"\b(software|developer|engineer|engineering|sre|devops|backend|back-end|frontend|front-end|full[- ]?stack|platform|programmer|architect)\b"),
    ("gtm", r"\b(sales|marketing|growth|account|business development|bdr|sdr|revenue|partnership|go[- ]to[- ]market|gtm|strategy|demand gen|operations|support|consultant|consulting|recruit\w*)\b"),
    ("data", r"\b(data|analytics|analyst|bi|business intelligence|scientist|statistician)\b"),
    ("product", r"\b(product|program manager|project manager|designer|ux|ui)\b"),
]
_FAMILY_RE = [(f, re.compile(p, re.I)) for f, p in _FAMILY_RULES]


# invented, public handoff phrasings
HANDOFF_PHRASES = [
    "What are Mike's compensation expectations?",
    "What compensation is Mike looking for?",
    "What is Mike's phone number?",
    "What is Mike's home address?",
    "How does Mike describe his gender and ethnicity for our EEO form?",
    "Is Mike a protected veteran?",
    "Can Mike provide two professional references?",
    "Why does Mike want to work at our company?",
    "Why is Mike interested in this particular role?",
    "Why is Mike leaving his current job?",
    "How did Mike hear about this opening?",
    "Is Mike interviewing with other companies right now?",
]

# invented, public personal-question phrasings: (text, expected topic)
SYNTHETIC_PERSONAL = [
    ("Is Mike legally authorized to work in the US?", "work_auth"),
    ("Does Mike need visa sponsorship now or in the future?", "work_auth"),
    ("Is Mike a US citizen or permitted to work in the US without sponsorship?", "work_auth"),
    ("Does Mike have the right to work in the US?", "work_auth"),
    ("Where is Mike located?", "location"),
    ("Is Mike open to relocating for this role?", "location"),
    ("Is Mike able to work onsite?", "location"),
    ("Does Mike prefer remote, hybrid or onsite work?", "location"),
    ("When can Mike start?", "start_date"),
    ("What is Mike's earliest start date?", "start_date"),
    ("How much notice does Mike need to give?", "start_date"),
    ("What is Mike's availability to start?", "start_date"),
]


@dataclass
class SetStats:
    rows: dict = field(default_factory=dict)
    jd_families: dict = field(default_factory=dict)
    splits: dict = field(default_factory=dict)  # set name -> {"tune": n, "report": n}
    question_kinds: dict = field(default_factory=dict)
    map_questions: int = 0
    map_missing: bool = False
    personal_real: int = 0
    personal_synthetic: int = 0


def sha8(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]


def role_family(title: str) -> str:
    for fam, rx in _FAMILY_RE:
        if rx.search(title or ""):
            return fam
    return "other"


def jd_title(text: str, index_title: str | None) -> str:
    if index_title and index_title.strip():
        return index_title.strip()
    lines = text.splitlines()
    for line in lines:
        if line.strip().upper().startswith("TITLE:"):
            return line.strip()[len("TITLE:"):].strip()
    for line in lines:
        s = line.strip()
        if s and len(s) <= 120 and not re.match(r"(URL|COMPANY)\s*:", s, re.I):
            return s
    return ""


def load_question_map(path: Path) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return {str(k): str(v) for k, v in data.items() if isinstance(v, str) and v.strip()}


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _slug(term: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", term.lower()).strip("-")


def _write(path: Path, rows: list) -> None:
    path.write_text("".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in rows), encoding="utf-8")


def _allocate(sizes: dict, n: int) -> dict:
    """Proportional allocation with a floor of min(2, size) per family, capped at size."""
    total = sum(sizes.values())
    if n >= total:
        return dict(sizes)
    alloc = {f: min(2, s) for f, s in sizes.items()}
    quota = {f: n * s / total for f, s in sizes.items()}
    for f in alloc:
        alloc[f] = min(sizes[f], max(alloc[f], int(quota[f])))
    order = sorted(sizes, key=lambda f: (-(quota[f] - alloc[f]), f))
    while sum(alloc.values()) < n:
        grew = False
        for f in order:
            if sum(alloc.values()) >= n:
                break
            if alloc[f] < sizes[f]:
                alloc[f] += 1
                grew = True
        if not grew:
            break
    while sum(alloc.values()) > n:
        cand = [f for f in alloc if alloc[f] > min(2, sizes[f])]
        if not cand:
            break
        f = max(cand, key=lambda f: (alloc[f] - quota[f], f))
        alloc[f] -= 1
    return alloc


def _read_texts(folder: Path) -> list:
    return [(p.stem, p.read_text(encoding="utf-8", errors="replace")) for p in sorted(folder.glob("*.txt"))]


def _build_jds(raw: Path, n_jds: int, seed: int) -> list:
    seen: set = set()
    rows: list = []

    def add(text: str, title: str | None):
        text = text.strip()
        if not text:
            return None
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if digest in seen:
            return None
        seen.add(digest)
        return {"id": f"jd-{digest[:8]}", "family": role_family(jd_title(text, title)), "text": text}

    for _, text in _read_texts(raw / "jds" / "applied"):
        row = add(text, None)
        if row:
            rows.append(row)
    titles: dict = {}
    idx = raw / "jds" / "top50_index.json"
    if idx.exists():
        for r in json.loads(idx.read_text(encoding="utf-8")):
            titles.setdefault(r.get("key"), r.get("title"))
    pool: dict = {}
    for key, text in _read_texts(raw / "jds" / "top50"):
        row = add(text, titles.get(key))
        if row:
            pool.setdefault(row["family"], []).append(row)
    need = n_jds - len(rows)
    if need > 0 and pool:
        rng = random.Random(seed)
        alloc = _allocate({f: len(v) for f, v in pool.items()}, need)
        for fam in sorted(pool):
            cands = sorted(pool[fam], key=lambda r: r["id"])
            rows.extend(rng.sample(cands, alloc.get(fam, 0)))
    rows.sort(key=lambda r: r["id"])
    return rows


def _question_texts(raw: Path, question_map: Path | None, stats: SetStats) -> list:
    texts: list = []
    ans = raw / "answers"
    for name, key in (("apply_db_answers.json", "question_text"), ("apply_db_ref_answers.json", "question")):
        p = ans / name
        if p.exists():
            texts += [r.get(key, "") for r in json.loads(p.read_text(encoding="utf-8"))]
    if question_map is not None and Path(question_map).exists():
        mapped = list(load_question_map(question_map).values())
        stats.map_questions = len(mapped)
        texts += mapped
    else:
        stats.map_missing = True
        warnings.warn("question map missing: questions come from the application database only", stacklevel=2)
    return texts


def build_sets(raw: Path, corpus_dir: Path, out: Path, n_jds: int = 100, seed: int = 0,
               question_map: Path | None = None) -> SetStats:
    raw, corpus_dir, out = Path(raw), Path(corpus_dir), Path(out)
    out.mkdir(parents=True, exist_ok=True)
    stats = SetStats()

    jds = _build_jds(raw, n_jds, seed)
    stats.jd_families = dict(Counter(r["family"] for r in jds))

    questions: dict = {}
    for text in _question_texts(raw, question_map, stats):
        text = _norm(text)
        if not text:
            continue
        qid = f"q-{sha8(text)}"
        kind = question_kind(text)
        gold = [PERSONAL_GOLD[t] for t in personal_topics(text)]
        questions.setdefault(qid, {"id": qid, "text": text, "kind": kind, "gold": gold, "synthetic": False})
    for text, _topic in SYNTHETIC_PERSONAL:
        qid = f"q-{sha8(text)}"
        questions.setdefault(qid, {"id": qid, "text": text, "kind": "personal",
                                   "gold": [PERSONAL_GOLD[t] for t in personal_topics(text)], "synthetic": True})
    qrows = sorted(questions.values(), key=lambda r: r["id"])
    stats.question_kinds = dict(Counter(r["kind"] for r in qrows))
    stats.personal_real = sum(1 for r in qrows if r["kind"] == "personal" and not r["synthetic"])
    stats.personal_synthetic = sum(1 for r in qrows if r["synthetic"])

    skills = json.loads((corpus_dir / "skills.json").read_text(encoding="utf-8"))
    gaps: dict = {}
    for s in skills:
        if s.get("depth") in LOW_DEPTHS:
            gid = f"gap-{_slug(s['term'])}"
            n = 2
            while gid in gaps:
                gid = f"gap-{_slug(s['term'])}-{n}"
                n += 1
            gaps[gid] = {"id": gid, "text": f"Has Mike worked with {s['term']}?", "term": s["term"], "depth": s["depth"]}

    handoffs = {r["id"]: {"id": r["id"], "text": r["text"]} for r in qrows if r["kind"] == "handoff"}
    for phrase in HANDOFF_PHRASES:
        hid = f"h-{sha8(phrase)}"
        handoffs.setdefault(hid, {"id": hid, "text": phrase})

    injections = [json.loads(line) for line in FIXTURE_INJECTIONS.read_text(encoding="utf-8").splitlines() if line.strip()]

    sets = {
        "jds": jds,
        "questions": qrows,
        "gaps": sorted(gaps.values(), key=lambda r: r["id"]),
        "handoffs": sorted(handoffs.values(), key=lambda r: r["id"]),
        "injections": injections,
    }
    for name, rows in sets.items():
        rows = [dict(r, split=split_of(r["id"])) for r in rows]
        _write(out / f"{name}.jsonl", rows)
        c = Counter(r["split"] for r in rows)
        stats.rows[name] = len(rows)
        stats.splits[name] = {"tune": c.get("tune", 0), "report": c.get("report", 0)}
    return stats
