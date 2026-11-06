from askmike.corpus.dupes import near_dupes
from askmike.corpus.unit import Unit


def U(id, claim, subject="Widget Index"):
    return Unit(id=id, claim=claim, subject=subject, kind="project", source="x", origin="draft")


def test_near_dupes_within_subject_only():
    a = U("aaa-1", "Mike built the Widget Index search ranking pipeline in Python.")
    b = U("bbb-2", "Mike built the Widget Index search ranking pipeline with Python.")
    c = U("ccc-3", "Mike built the Widget Index search ranking pipeline in Python.", subject="Other")
    d = U("ddd-4", "Mike enjoys hiking on weekends.")
    out = near_dupes([a, b, c, d])
    assert out == {"aaa-1": ["bbb-2"], "bbb-2": ["aaa-1"]}
