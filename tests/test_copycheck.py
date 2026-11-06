"""Copy check, ported from deal-terms-desk tests/test_copycheck.py on 2026-11-03.

Changes: facts are {value, display, cmd, inputs}; the machine-built label tests are gone (that logic was dropped);
new tests for `@k` identifiers and display-string matches.
"""
import json

from askmike.facts import copycheck
from askmike.facts.copycheck import FLAGGED, check, render


def fact(value, display):
    return {"value": value, "display": display, "cmd": "uv run python -m askmike.evals facts", "inputs": {}}


FACTS = {
    "m1_jdreq_R4_report_recall_at_10": fact(0.5394, "0.54"),
    "m1_n_requirements": fact(1446, "1,446"),
    "m1_ci_pct": fact(95, "95%"),
    "m1_supportable_rate": fact(0.8882, "89%"),
    "m1_kappa_AB": fact(None, "undefined"),       # None never matches a number
    "m1_invented_flag": fact(True, "yes"),        # a boolean must never match "1"
    "m1_invented_day": fact("2026-10-05", "2026-10-05"),
}


def one(text: str, facts: dict = FACTS):
    found = check(text, facts)
    assert len(found) == 1, found
    return found[0]


def test_copycheck_treats_at_k_as_identifier_and_matches_display():
    assert check("recall@5, recall@10, MRR@10, nDCG@10 and coverage@10 are names.", FACTS) == []
    f = one("R4 reached recall@10 of 0.54 on the report split.")
    assert (f.line, f.raw, f.status, f.keys) == (1, "0.54", "fact", ("m1_jdreq_R4_report_recall_at_10",))
    assert one("Mike's evidence supports 89% of them.").keys == ("m1_supportable_rate",)
    assert one("with 95% intervals").status == "fact"
    assert one("1,446 requirements").keys == ("m1_n_requirements",)


def test_copycheck_flags_unknown_number_and_accepts_rounded():
    f = one("it answers 17 questions")
    assert f.status == "no fact" and f.status in FLAGGED and f.keys == ()
    assert one("about 0.539 of them").status == "rounded"
    f = one("about 54% of them")
    assert (f.raw, f.status, f.keys) == ("54%", "rounded", ("m1_jdreq_R4_report_recall_at_10",))
    assert one("exactly 0.5394").status == "fact"


def test_one_significant_digit_is_never_a_rounding():
    assert one("roughly 0.5 of them").status == "no fact"


def test_none_and_booleans_never_match():
    assert one("1 of them").status == "no fact"
    assert check("kappa was undefined", FACTS) == []


def test_dates_match_string_facts():
    f = one("snapshot of 2026-10-05")
    assert (f.raw, f.status, f.keys) == ("2026-10-05", "fact", ("m1_invented_day",))
    assert one("written on 2026-10-07").status == "no fact"


def test_code_links_list_markers_and_names_are_not_numbers():
    text = ("1. Run `uv run python -m askmike.facts.copycheck README.md` first[^2]\n"
            "See [the M1 report](docs/m1/REPORT.md#3) or https://forn.al/v4/5.\n"
            "M1 ranks with R4, BM25 over FTS5, bge-small-en-v1.5, claude-opus-5-5 and k0.\n"
            "[7]: https://example.com/8\n"
            "```\nuv run pytest -q  # 870 tests\n```\n")
    assert check(text, FACTS) == []


def test_a_less_than_sign_is_not_an_html_tag():
    assert [f.raw for f in check("n < 5 and m > 3", {})] == ["5", "3"]


def test_markdown_underscore_emphasis_does_not_hide_a_number():
    assert [f.raw for f in check("_54%_ of them, __1,446__ requirements, _2026-10-05_", FACTS)] == [
        "54%", "1,446", "2026-10-05"]


def test_flat_values_still_work():
    assert one("0.5394", {"m1_x": 0.5394}).keys == ("m1_x",)


def test_the_report_lists_every_number_and_counts_the_flags():
    out = render("README.md", check("Recall 0.54.\n\nIt answers 17 questions.\n", FACTS))
    assert out.splitlines() == ['README.md:1  "0.54"  fact  m1_jdreq_R4_report_recall_at_10',
                                'README.md:3  "17"  no fact',
                                "README.md: 2 numbers (1 exact, 0 rounded), 1 flagged"]
    out = render("post.md", check("About 54% of them.", FACTS)).splitlines()
    assert out[-1] == "post.md: 1 number (0 exact, 1 rounded: check each names the fact you mean), 0 flagged"


def test_a_number_many_facts_hold_names_a_few_and_counts_the_rest():
    many = {f"m1_n{i}": fact(7, "7") for i in range(6)}
    assert 'post.md:1  "7"  fact  m1_n0, m1_n1, m1_n2, m1_n3 +2 more' in render("post.md", check("7 of them", many))


def test_cli_reports_and_never_writes(tmp_path, monkeypatch, capsys):
    (tmp_path / "facts.json").write_text(json.dumps(FACTS))
    monkeypatch.setattr(copycheck, "FACTS", tmp_path / "facts.json")
    clean, flagged = tmp_path / "README.md", tmp_path / "post.md"
    clean.write_text("Recall@10 was 0.54.\n")
    flagged.write_text("It answers 17 questions.\n")
    before = {p: p.read_bytes() for p in (clean, flagged)}
    assert copycheck.main([str(clean)]) == 0
    assert copycheck.main([str(clean), str(flagged)]) == 1
    assert f'{flagged}:1  "17"  no fact' in capsys.readouterr().out
    assert {p: p.read_bytes() for p in (clean, flagged)} == before
    assert copycheck.main([str(tmp_path / "missing.md")]) == 2
    monkeypatch.setattr(copycheck, "FACTS", tmp_path / "none.json")
    assert copycheck.main([str(clean)]) == 2
