from askmike.evals.keyword import keyword_rank

UNITS = [
    {"id": "a", "claim": "Built webhooks for billing.", "detail": "", "subject": "Acme", "skills": []},
    {"id": "b", "claim": "Wrote python services and python tests.", "detail": "", "subject": "Lab", "skills": []},
    {"id": "c", "claim": "Hiking and cooking.", "detail": "", "subject": "Home", "skills": []},
    {"id": "d", "claim": "Ran a pythonic codebase.", "detail": "", "subject": "X", "skills": []},
]
SKILLS = [{"term": "API integration", "depth": "strong", "aliases": ["api", "webhook", "webhooks"]}]


def test_keyword_rank_counts_whole_word_and_alias_hits():
    hits = keyword_rank("Python and billing with API integration", UNITS, SKILLS)
    ids = [h.unit_id for h in hits]
    # a: "billing" + the ledger entry (webhooks alias) = 2; b: "python" once = 1; "pythonic" is not a whole-word hit
    assert {h.unit_id: h.score for h in hits} == {"a": 2.0, "b": 1.0}
    assert [h.unit_id for h in hits] == ["a", "b"]


def test_keyword_ties_break_by_unit_id():
    hits = keyword_rank("python billing", UNITS, [], k=5)
    assert [h.unit_id for h in hits] == ["a", "b"]
    assert keyword_rank("python", UNITS, [], k=1)[0].unit_id == "b"
