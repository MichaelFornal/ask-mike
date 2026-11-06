import pytest

from askmike.corpus.extract import FactsIndex, Skill
from askmike.corpus.unit import Unit
from askmike.gates.checks import CompanyPattern, GateContext, privacy
from askmike.gates.text import (
    as_skill, claimable_hits, first_person, link_tokens, low_depth_hits, privacy_hits, slop_hits, verdict_hits,
)


def _count_applied():  # built at runtime: the private scan blocks the literal
    return " ".join(["Mike", "applied", "to", str(40), "roles."])


def _count_interviews():
    return " ".join(["He", "had", str(3), "interviews", "last", "month."])


def test_privacy_hits_job_search_counts():
    assert privacy_hits(_count_applied(), [], [])
    assert privacy_hits(_count_interviews(), [], [])
    assert privacy_hits("the policy applied to PUBLIC", [], []) == []
    assert privacy_hits("Mike built an application-filling pipeline", [], []) == []


def test_privacy_hits_messages_name_pattern_not_text():
    msgs = " ".join(privacy_hits(_count_applied(), [], []))
    assert "40" not in msgs and "roles" not in msgs


def test_company_hit_allowed_when_in_allow_names():
    co = CompanyPattern("Zorblax Dynamics")
    text = "Mike worked with Zorblax Dynamics."
    assert privacy_hits(text, [], [co])
    assert privacy_hits(text, [], [co], allow_names=frozenset({"zorblax dynamics"})) == []


VERDICTS = [
    "a perfect fit", "an ideal match", "a great fit", "a strong match", "a good fit",
    "an excellent match", "a poor fit", "a weak match", "not a good fit", "not a right match",
    "not a strong fit", "the fit score", "a match rating", "an 87% fit", "a 90% match",
    "rated 8/10", "scored 70/100", "out of 10", "out of ten", "out of 5", "out of five",
    "out of 100", "highly qualified", "well-qualified", "over qualified", "under-qualified",
    "overqualified", "underqualified",
    "the best candidate", "a top candidate", "I recommend hiring Mike", "he should hire",
    "you should interview Mike",
]


@pytest.mark.parametrize("text", VERDICTS)
def test_verdict_hits_table(text):
    assert verdict_hits(text)


@pytest.mark.parametrize("text", ["Mike built a RAG system", "a match on 57% of postings",
                                  "Mike's strongest work"])
def test_verdict_hits_clean(text):
    assert verdict_hits(text) == []


LEDGER = [Skill("Go", "none"), Skill("Rust", "familiar", ("rustlang",)),
          Skill("Python", "strong"), Skill("SQL", "working", ("sql",))]


def test_low_depth_and_claimable_hits_respect_short_term_case():
    assert low_depth_hits("Mike used Go", LEDGER, frozenset())[0].term == "Go"
    assert low_depth_hits("go-to-market", LEDGER, frozenset()) == []
    assert low_depth_hits("a rustlang tool", LEDGER, frozenset())[0].term == "Rust"
    assert low_depth_hits("a rustlang tool", LEDGER, frozenset({"rustlang"})) == []
    assert [s.term for s in claimable_hits("Python and SQL", LEDGER, frozenset())] == ["Python", "SQL"]
    assert claimable_hits("Mike used Go", LEDGER, frozenset()) == []


def test_as_skill_adapts_skills_json_row():
    s = as_skill({"term": "AWS", "depth": "working", "aliases": ["ec2"]})
    assert (s.term, s.depth, s.aliases) == ("AWS", "working", ("ec2",))


def test_slop_and_first_person():
    assert slop_hits("a robust tool") and slop_hits("clean text") == []
    assert first_person("I built it") and first_person("our team")
    assert not first_person("Mike built it")


def test_first_person_catches_reflexive_and_possessive_forms():
    for text in ("Mike says he built it myself.", "The queue design is mine.", "We wired it up ourselves.",
                 "MYSELF", "Mine."):
        assert first_person(text), text
    # "us" stays out: it collides with "US" (work authorization)
    for text in ("Mike is authorized to work in the US.", "Mike built it himself.", "Mike mined the logs.",
                 "Mike set up the data-mining job.", "Mike wrote the self-serve tool."):
        assert not first_person(text), text


def test_unit_privacy_gate_flags_job_search_count():
    ctx = GateContext(ledger=[], facts=FactsIndex({}), private_patterns=[], source_text=lambda u: "")
    u = Unit(id="u-one", claim="Mike built a queue.", detail=_count_applied(), subject="s",
             kind="role", source="x.txt", origin="extract")
    fs = privacy(u, ctx)
    assert fs and all(f.gate == "privacy" for f in fs)
    assert "40" not in " ".join(f.message for f in fs)


def test_link_tokens_find_urls_and_bare_domains():
    got = link_tokens("See https://a.example.com/x, evil.example.com and gpett.com. Also deals.forn.al!")
    assert {"https://a.example.com/x", "evil.example.com", "gpett.com", "deals.forn.al"} <= set(got)
    assert link_tokens("Read HTTP://Foo.Bar/Baz).") == ["http://foo.bar/baz"]
    assert link_tokens("e.g. Next.js on U.S. data") == []
    assert link_tokens("write to mike@forn.al") == []


def test_bare_domain_token_keeps_its_path():
    assert link_tokens("see github.com/Attacker/repo.") == ["github.com/attacker/repo"]
    assert link_tokens("see github.com, then x.") == ["github.com"]
