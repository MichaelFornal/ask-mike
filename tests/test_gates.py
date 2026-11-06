import json
import re
from pathlib import Path

import pytest

from askmike.corpus.extract import FactsIndex, Skill
from askmike.corpus.unit import SkillRef, Unit
from askmike.gates.checks import (
    load_honesty_allow,
    Finding, GateContext, ground_numbers, honesty, load_private_patterns,
    numbers, privacy, run_gates, slop, voice,
)

RAW = Path(__file__).parent / "fixtures" / "raw"


def _private(tmp_path, deny=("zzsecret",), dont=("hushword",), jds=True):
    p = tmp_path / "priv"
    p.mkdir()
    if deny is not None:
        (p / "denylist.local.txt").write_text("# header\n" + "\n".join(deny) + "\n")
    if dont is not None:
        (p / "dontprint.local.txt").write_text("# header\n" + "\n".join(dont) + "\n")
    if jds:
        (p / "raw/jds/applied").mkdir(parents=True)
        (p / "raw/jds/top50_index.json").write_text(json.dumps([{"company": "Widget"}]))
    return p


def U(claim="Mike built a queue.", **kw):
    base = dict(id="u-one", claim=claim, subject="Acme Robotics", kind="role",
                source="x.txt", origin="extract")
    base.update(kw)
    return Unit(**base)


@pytest.fixture
def ctx(tmp_path):
    ledger = [Skill("Rust", "none"), Skill("Kubernetes", "familiar", ("k8s",)),
              Skill("Terraform", "learning"), Skill("Python", "strong")]
    return GateContext(
        ledger=ledger,
        facts=FactsIndex({"fact:site/facts.json:w": {"4321000", "4,321,000", "4.32M"}}),
        private_patterns=load_private_patterns(_private(tmp_path)),
        source_text=lambda u: "Handled 1,200 jobs a day for 40% of 9 sites in 1996.",
    )


def test_privacy_flags_generic_phone_and_private_pattern_without_echoing_it(ctx):
    u = U("Mike can be reached at 617-555-0123 and knows Zzsecret.", detail="12 Main Street")
    fs = privacy(u, ctx)
    msgs = " ".join(f.message for f in fs)
    assert len(fs) >= 3 and all(f.gate == "privacy" and f.unit_id == "u-one" for f in fs)
    assert "zzsecret" not in msgs.lower() and "555" not in msgs and "Main" not in msgs
    assert "#0" in msgs


def test_privacy_allows_public_contact_address(ctx):
    assert privacy(U("Mike is at mike@forn.al."), ctx) == []
    assert privacy(U("Mike is at someone@example.com."), ctx) != []


def test_privacy_scans_quote_and_dontprint(ctx):
    assert len(privacy(U(quote="the hushword here"), ctx)) == 1


def test_slop_flags_em_dash_and_hype(ctx):
    fs = slop(U("Mike built a robust queue — world-class."), ctx)
    assert len(fs) >= 3 and all(f.gate == "slop" for f in fs)
    assert slop(U("Mike built a queue."), ctx) == []


def test_voice_flags_first_person_and_missing_subject(ctx):
    assert voice(U("Mike built a queue."), ctx) == []
    assert voice(U("Built a queue."), ctx) != []
    assert voice(U("Mike built my queue."), ctx) != []
    assert voice(U("Mike built it. I did."), ctx) != []
    assert voice(U("Mike " + "x" * 300), ctx) != []
    assert voice(U(detail="y" * 601), ctx) != []


def test_honesty_flags_none_and_familiar_terms_and_aliases(ctx):
    assert honesty(U("Mike wrote Rust."), ctx)
    assert honesty(U("Mike ran K8s clusters."), ctx)
    assert honesty(U(detail="Some Terraform."), ctx)
    assert honesty(U(skills=(SkillRef("Python", "familiar"),)), ctx)
    assert honesty(U(skills=(SkillRef("Rust", "strong"),)), ctx)
    assert honesty(U("Mike wrote Python and trusted it.", skills=(SkillRef("Python", "strong"),)), ctx) == []


def test_numbers_grounded_by_source_text_or_fact_and_years_exempt(ctx):
    u = U("Mike ran 1200 jobs for 40% of 9 sites since 2021 and indexed 4.32M.")
    assert numbers(u, ctx) == []
    nums, bad = ground_numbers(u, ctx)
    assert bad == []
    assert [(n.value, n.source) for n in nums] == [
        ("1200", "text"), ("40%", "text"), ("9", "text"),
        ("4.32M", "fact:site/facts.json:w")]


def test_numbers_flags_a_figure_found_nowhere(ctx):
    fs = numbers(U("Mike cut 9% and 77 things."), ctx)
    assert [f.gate for f in fs] == ["numbers", "numbers"]
    nums, bad = ground_numbers(U("Mike cut 9% and 77 things."), ctx)
    assert bad == ["9%", "77"] and nums == ()


def test_bare_digit_not_grounded_by_year_in_source(tmp_path):
    c = GateContext([], FactsIndex({}), [], lambda u: "Founded in 1996.")
    assert [f.gate for f in numbers(U("Mike ran 9 sites."), c)] == ["numbers"]


def test_run_gates_runs_all_five_in_order(ctx):
    fs = run_gates(U("built my Rust thing — 77"), ctx)
    order = [f.gate for f in fs]
    assert order == sorted(order, key=["privacy", "slop", "voice", "honesty", "numbers"].index)
    assert set(order) == {"slop", "voice", "honesty", "numbers"}


def test_applied_company_names_derived_from_raw_jds(tmp_path):
    p = _private(tmp_path)
    (p / "raw/jds/applied/acme-robotics-J123.txt").write_text("x")
    (p / "raw/jds/applied/ats_lever_blue_fox-J9.txt").write_text("x")
    (p / "raw/jds/top50_index.json").write_text(json.dumps(
        [{"company": "gadget works"}, {"company": "Widget"}, {"company": "AB"}, {"company": "Skipme"}]))
    (p / "company_allow.local.txt").write_text("# ok\nSkipme\n")
    pats = load_private_patterns(p)
    hit = lambda t: any(x.search(t) for x in pats)
    assert hit("Acme Robotics") and hit("Blue Fox") and hit("Gadget Works") and hit("Widget")
    assert not hit("Skipme") and not hit("AB")


def test_applied_company_match_flexible_case_and_separators(tmp_path):
    p = _private(tmp_path)
    (p / "raw/jds/applied/blue-fox-J1.txt").write_text("x")
    (p / "raw/jds/applied/acme-J2.txt").write_text("x")
    pats = load_private_patterns(p)
    c = GateContext([], FactsIndex({}), pats, lambda u: "")
    for t in ("Mike used WidgetIQ.", "Mike met ACME.", "Mike used BlueFox.", "BLUE-FOX", "Blue Fox"):
        assert privacy(U(t), c), t
    for t in ("Mike counted widget totals.", "Mike ran tool.acme.build.", "blue fox den"):
        assert not privacy(U(t), c), t
    assert privacy(U("Mike met ZZSECRET."), c)  # identity stays case-insensitive
    msgs = " ".join(f.message for f in privacy(U("Mike used Widget daily."), c))
    assert "applied-company #" in msgs and "Widget" not in msgs


def test_no_finding_message_echoes_digits_from_the_claim(ctx):
    u = U("Mike lived at 4821 Oak Street, ZIP 02139, call 617-555-0123, paid 98765.")
    fs = run_gates(u, ctx)
    assert {"privacy", "numbers"} <= {f.gate for f in fs}
    text = " ".join(f.message for f in fs)
    for group in ("4821", "02139", "617", "555", "0123", "98765"):
        assert group not in text


def test_missing_jds_inputs_fail_closed(tmp_path):
    p = _private(tmp_path, jds=False)
    with pytest.raises(FileNotFoundError, match="top50_index"):
        load_private_patterns(p)
    (p / "raw/jds/applied").mkdir(parents=True)
    (p / "raw/jds/top50_index.json").write_text("[]")
    with pytest.raises(ValueError):
        load_private_patterns(p)
    (p / "raw/jds/top50_index.json").unlink()
    (p / "raw/jds/applied").rmdir()
    (p / "raw/jds").mkdir(exist_ok=True)
    (p / "raw/jds/top50_index.json").write_text(json.dumps([{"company": "Widget"}]))
    with pytest.raises(FileNotFoundError, match="applied"):
        load_private_patterns(p)


def test_short_terms_match_spelling_or_caps_only_long_terms_any_case():
    c = GateContext([Skill("Go", "none"), Skill("Rust", "none")], FactsIndex({}), [], lambda u: "")
    assert honesty(U("Mike wrote Go."), c) and honesty(U("Mike wrote GO."), c)
    assert not honesty(U("Mike ran go-to-market."), c) and not honesty(U("Mike can go."), c)
    assert honesty(U("Mike wrote Rust."), c) and honesty(U("Mike wrote rust."), c)


@pytest.mark.parametrize("missing", ["denylist.local.txt", "dontprint.local.txt"])
def test_load_private_patterns_fails_closed(tmp_path, missing):
    p = _private(tmp_path)
    (p / missing).unlink()
    with pytest.raises(FileNotFoundError, match=missing):
        load_private_patterns(p)


def test_company_allowlist_is_case_insensitive(tmp_path):
    p = _private(tmp_path)
    (p / "raw/jds/applied/readme-J5.txt").write_text("x")
    (p / "company_allow.local.txt").write_text("ReadMe\n")
    pats = load_private_patterns(p)
    assert not any(x.search("See the README.") for x in pats)


def test_applied_company_equal_to_ledger_term_is_excluded(tmp_path):
    p = _private(tmp_path)
    (p / "raw/jds/top50_index.json").write_text(json.dumps([{"company": "Widget"}, {"company": "Sprocketdb"}]))
    (p / "raw/profile").mkdir(parents=True)
    (p / "raw/profile/profile.json").write_text(json.dumps(
        {"skill_depth": [{"term": "SprocketDB", "depth": "strong", "aliases": []}]}))
    pats = load_private_patterns(p)
    hit = lambda t: any(x.search(t) for x in pats)
    assert hit("Widget") and not hit("Sprocketdb")


# ---- pattern-file self-check (invented values) ------------------------------

@pytest.mark.parametrize("line", ["cost $77 flat", "+1 (555) 555-0100", r"\bzzword\b", "zz(q"])
def test_pattern_line_that_does_not_match_itself_or_compile_names_file_and_line_only(tmp_path, line):
    p = _private(tmp_path, deny=("zzsecret", line))
    with pytest.raises(ValueError) as e:
        load_private_patterns(p)
    msg = str(e.value)
    assert "denylist.local.txt line 3" in msg and "re.escape" in msg
    assert line not in msg and "zzsecret" not in msg and "555" not in msg and "zzword" not in msg


def test_dontprint_line_failure_names_dontprint_file(tmp_path):
    p = _private(tmp_path, dont=("hushword", "", "# note", "a$b"))
    with pytest.raises(ValueError, match="dontprint.local.txt line 5"):
        load_private_patterns(p)


def test_re_prefix_opts_out_of_the_self_match_check(tmp_path):
    p = _private(tmp_path, deny=("zzsecret", r"re:\bzzword\b", r"re: \d{3}-zz"))
    pats = load_private_patterns(p)
    hit = lambda t: any(x.search(t) for x in pats)
    assert hit("the ZZWORD here") and not hit("zzwordy") and hit("x 123-zz")
    assert not hit("re:")


def test_escaped_literal_loads_and_matches(tmp_path):
    p = _private(tmp_path, deny=("zzsecret", re.escape("cost $77 flat")))
    assert any(x.search("a cost $77 flat fee") for x in load_private_patterns(p))


@pytest.mark.parametrize("line", ["re:", "re:(zz)?", "x*"])
def test_pattern_that_matches_empty_text_is_refused(tmp_path, line):
    p = _private(tmp_path, deny=("zzsecret", line))
    with pytest.raises(ValueError, match="denylist.local.txt line 3"):
        load_private_patterns(p)


@pytest.mark.parametrize("which", ["deny", "dont"])
def test_pattern_file_with_no_patterns_is_refused(tmp_path, which):
    p = _private(tmp_path, **{which: ("# only a comment", "")})
    name = "denylist.local.txt" if which == "deny" else "dontprint.local.txt"
    with pytest.raises(ValueError, match=f"{name} has no patterns"):
        load_private_patterns(p)


def test_r27_short_terms_and_aliases_match_capitalized_or_caps_only():
    c = GateContext([Skill("Go", "none", ("go",)), Skill("Identity Suite", "none", ("ids",))],
                    FactsIndex({}), [], lambda u: "")
    assert not honesty(U("Mike led go-to-market."), c)
    assert honesty(U("Mike wrote Go services."), c)
    assert not honesty(U("Mike listed endpoint ids."), c)
    assert honesty(U("Mike triaged IDS alerts."), c)
    assert honesty(U("Mike triaged Ids alerts."), c)


def test_r27_honesty_allow_file_skips_text_but_not_skills(tmp_path):
    (tmp_path / "honesty_allow.local.txt").write_text("# note\nCrawler\n")
    allow = load_honesty_allow(tmp_path)
    assert allow == frozenset({"crawler"})
    assert load_honesty_allow(tmp_path / "nowhere") == frozenset()
    c = GateContext([Skill("Scraping", "familiar", ("crawler",))], FactsIndex({}), [], lambda u: "", allow)
    assert not honesty(U("Mike built a crawler for Acme."), c)
    assert honesty(U("Mike built Scraping tools."), c)
    assert honesty(U("Mike built it.", skills=(SkillRef("crawler", "working"),)), c)
