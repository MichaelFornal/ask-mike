"""The eight answer gates and the private gate-config bundle. Every config, unit, company and pattern
here is invented."""
import json
import re
import time

import pytest

from askmike.answer import evidence, gates
from askmike.answer.config import NEED_MAX, TEXT_MAX
from askmike.answer.evidence import Claim, Pack
from askmike.answer.gates import AnswerGateConfig, bundle_stale, gate_claims, write_bundle
from askmike.corpus.extract import Skill
from askmike.gates.checks import CompanyPattern

LEDGER = [Skill("Python", "strong"), Skill("SQL", "working", ("postgres",)),
          Skill("Kubernetes", "none", ("k8s",)), Skill("Rust", "familiar"), Skill("Airflow", "none", ("dagster",))]


def _unit(uid, claim, detail="", subject="Lab"):
    return {"id": uid, "claim": claim, "detail": detail, "subject": subject, "kind": "project", "skills": []}


UNITS = {u["id"]: u for u in (
    _unit("u1", "Mike cut nightly run time by 40% across 1,200 jobs.", "He moved the reports to Python."),
    _unit("u2", "Mike ran 77 load tests on the queue."),
    _unit("u3", "Mike wrote the Quillfeather Labs importer."),
    _unit("u4", "Mike deployed Kubernetes clusters for the lab.", "The K8s clusters ran the lab jobs."),
    _unit("u5", "Mike ran 9 sites."),  # in the corpus, never in the pack
    _unit("u6", "Mike ran the nightly reports.", subject="lab-q"),
    {**_unit("u7", "Mike ran the quarterly close."), "when": "2021-03..2023"},
)}
PACK = Pack(("u1", "u2", "u3", "u4", "u6", "u7"), {"r01": ("u1",), "r02": ("u2",)}, ())

VISITOR_CO, OTHER_CO = "Zorblax Industries", "Quillfeather Labs"


def cfg(**kw):
    base = dict(patterns=[re.compile("zzq[0-9]+x", re.I)],
                companies=[CompanyPattern(VISITOR_CO), CompanyPattern(OTHER_CO)],
                honesty_allow=frozenset(), skills=LEDGER)
    base.update(kw)
    return AnswerGateConfig(**base)


def C(text, type="direct", units=("u1",), need="", req=""):
    return Claim(type, need, req, text, tuple(units))


def run(claims, kind="question", input_text="", c=None):
    return gate_claims(claims, PACK, UNITS, c or cfg(), input_text, kind)


def each(claims, **kw):
    """One verdict per claim, each gated alone, so the MAX_CLAIMS cap never applies."""
    return [run([c], **kw)[0][0] for c in claims]


# ---- structure -------------------------------------------------------------

def test_unknown_ids_stripped_claim_kept_if_one_valid_id_left():
    t = "Mike ran 77 load tests on the queue."
    vs, kept = run([C(t, units=("u-bogus", "u2", "u5", "u2")), C(t, units=("u-bogus",))])
    assert vs[0].kept and vs[0].stripped_ids == 2 and vs[0].claim.units == ("u2",)
    assert not vs[1].kept and "structure" in vs[1].reasons and vs[1].stripped_ids == 1
    assert kept == [vs[0].claim]

    # the type must suit the kind, the text must be non-empty, and a bad req is blanked, not failed
    vs, kept = run([C(t, type="direct", units=("u2",), need="load tests", req="r02"),
                    C(t, type="match", units=("u2",), need="load tests", req="r99"),
                    C(t, type="match", units=("u2",), need="load tests", req="r02"),
                    C("   ", type="match", units=("u2",), req="r02")], kind="jd")
    assert [v.kept for v in vs] == [False, True, True, False]
    assert vs[0].reasons == ("structure",) and vs[3].reasons == ("structure",)
    assert [c.req for c in kept] == ["", "r02"]
    vs, kept = run([C(t, type="match", units=("u2",)), C(t, units=("u2",), req="r02")])
    assert vs[0].reasons == ("structure",) and vs[1].kept and kept[0].req == ""


# ---- grounding -------------------------------------------------------------

def test_number_not_in_cited_unit_fails_grounding_years_pass():
    vs, kept = run([C("Mike cut nightly run time by 40% across 1200 jobs in 2024."),  # u1 writes 1,200
                    C("Mike cut nightly run time by 45% across 1,200 jobs."),
                    C("Mike ran 77 load tests."),  # 77 is in u2, which it does not cite
                    C("Mike ran 77 load tests.", units=("u1", "u2"))])
    assert [v.kept for v in vs] == [True, False, False, True]
    assert vs[1].reasons == ("grounding",) and vs[2].reasons == ("grounding",)


def test_invented_project_name_fails_grounding_input_names_pass(monkeypatch):
    monkeypatch.setattr(evidence, "_subjects", lambda: {"lab-q": "Hyperion Works"})
    question = "Has Mike worked with Velostat?"
    vs = each([
        C("Mike moved the reports to Python and SQL."),  # ledger terms
        C("Mike built the Hyperloom engine for the reports."),  # an invented project name
        C("Mike moved the Velostat reports to Python."),  # an input name does not ground a direct claim
        C("BlorpDB stores the reports."),  # sentence-initial, but an inner capital makes it a name
        C("Mike's reports moved to Python in March 2024. His jobs run on SQL."),  # Mike's, a month, a starter
        C("Mike ran the Hyperion Works reports.", units=("u6",)),  # the cited unit's canonical subject
        C("Mike ran the Hyperion Works reports."),  # the same name, from a unit with another subject
        C("Mike moved the reports to Postgres."),  # a ledger alias, any case
        C("Mike Fornal moved the reports to Python."),  # his surname is exempt too
    ], input_text=question)
    assert [v.kept for v in vs] == [True, False, False, False, True, True, False, True, True]
    assert {v.reasons for v in vs if not v.kept} == {("grounding",)}


def test_input_names_ground_only_gap_claims():
    question = "Has Mike used Snowflake?"
    jd = "We need a data engineer with Snowflake and Python for nightly pipelines."
    assert run([C("Mike built Snowflake pipelines for the reports.")], input_text=question)[0][0].reasons \
        == ("grounding",)
    assert run([C("Mike built Snowflake pipelines for the reports.", type="match", req="r01")], kind="jd",
               input_text=jd)[0][0].reasons == ("grounding",)
    gap = C("Mike hasn't used Snowflake; the closest thing he has done is the Python reports.", type="gap")
    assert run([gap], input_text=question)[0][0].reasons == ()
    assert run([gap], input_text="")[0][0].reasons == ("grounding",)
    # in a gap, the input grounds only a name the negation governs, never one in the positive pivot
    vs = each([C("Mike hasn't used Snowflake, but he has built Snowflake pipelines for the reports.", type="gap"),
               C("Mike hasn't used Snowflake; the closest he has come is the Snowflake pipelines for the reports.",
                 type="gap")], input_text=question)
    assert [v.reasons for v in vs] == [("grounding",)] * 2


def test_invented_name_at_sentence_start_fails_grounding():
    vs = each([C("Hyperloom cut nightly run time by 40%."),
               C("Mike moved the reports to Python. Hyperloom now runs them."),
               C("Mike moved reports, e.g. Hyperloom, to Python."),
               C("Mike moved one tool, i.e. Hyperloom, to Python."),
               C("Mike moved reports, e.g. Each, to Python."),  # a starter after e.g. is not sentence-initial
               C("Mike built the Each pipeline for the reports."),  # a starter is a name mid-sentence
               C("The reports moved to Python. After that, his jobs ran on SQL.")])  # starters are exempt
    assert [v.reasons for v in vs] == [("grounding",)] * 6 + [()]


def test_year_shaped_amounts_and_counts_must_be_grounded():
    vs = each([C("Mike saved $2000 on the nightly reports."),
               C("Mike cut run time across 2025 jobs."),
               C("Mike joined in 2022 and cut nightly run time by 40%."),
               C("Mike ran the nightly reports from mid-2022 through early 2023."),
               C("Mike ran the nightly reports 2019-2021."),
               C("Mike ran the 2023 quarterly close.", units=("u7",)),  # the cited unit's `when` holds it
               C("Mike ran the 2024 quarterly close.", units=("u7",)),
               C("Mike saved $2023 on the quarterly close.", units=("u7",))])  # an amount, though `when` has 2023
    assert [v.reasons for v in vs] == [("grounding",)] * 2 + [()] * 4 + [("grounding",)] * 2


# ---- honesty ---------------------------------------------------------------

GAP_TAIL = "; the closest thing he has done is the lab clusters."


def test_none_skill_outside_gap_fails_honesty():
    vs, _ = run([C("Mike deployed Kubernetes clusters for the lab.", units=("u4",)),  # u4 says so
                 C("Mike deployed K8s clusters for the lab.", units=("u4",)),
                 C("Mike hasn't run Kubernetes in production" + GAP_TAIL, type="gap", units=("u4",))])
    assert vs[0].reasons == ("honesty",) and vs[1].reasons == ("honesty",) and vs[2].kept
    # an alias on the honesty allow list reads as an ordinary word, not the skill
    vs, _ = run([C("Mike deployed K8s clusters for the lab.", units=("u4",))],
                c=cfg(honesty_allow=frozenset({"k8s"})))
    assert vs[0].kept


def test_gap_without_negation_fails():
    vs, _ = run([C("Mike is new to Kubernetes" + GAP_TAIL, type="gap", units=("u4",))])
    assert vs[0].reasons == ("honesty",)
    forms = ["Mike hasn't run Kubernetes", "Mike hasn\u2019t run Kubernetes", "Mike has not run Kubernetes",
             "Mike is not yet running Kubernetes", "Mike has never run Kubernetes",
             "Mike has no Kubernetes experience", "Mike has no hands-on Kubernetes experience"]
    vs = each([C(f + GAP_TAIL, type="gap", units=("u4",)) for f in forms])
    assert [v.reasons for v in vs] == [()] * len(forms)


def test_gap_denying_claimable_skill_fails():
    vs = each([C("Mike hasn't used Python; the closest thing he has done is the nightly reports.", type="gap"),
               C("Mike has no SQL experience" + GAP_TAIL, type="gap", units=("u4",)),
               C("Mike hasn't used Python. The closest thing he has done is the nightly reports.", type="gap"),
               # a low-depth skill in the pivot does not excuse denying a claimable one in the clause
               C("Mike hasn't used Python; the closest thing he has done is Kubernetes clusters.",
                 type="gap", units=("u4",)),
               C("Mike hasn't run Kubernetes; the closest thing he has done is the Python reports.",
                 type="gap", units=("u1", "u4"))])
    assert [v.reasons for v in vs] == [("honesty",)] * 4 + [()]


def test_gap_honesty_reads_each_segment():
    # a low-depth skill may appear only as a negation's object (no comma or clause word between them), and no
    # negated segment may deny a claimable skill; segments end at ";", a sentence break, "closest" and a
    # conjunction opening a clause
    drops = ["Mike has run Kubernetes in production and never had an outage.",
             "Mike hasn't run Kubernetes; he has never written SQL.",
             "Mike has never used Rust. He also has no Python experience.",
             "The closest Mike has come is the reports; he has never written SQL.",
             "Mike hasn't had an outage and has run Kubernetes in production.",
             "Mike has never run Kubernetes and has not used SQL.",
             "Mike hasn't used tools, e.g. Python, in production.",  # e.g. does not end the segment
             # a clause boundary between the negation and the skill: the skill is asserted, not denied
             "Mike has never had an outage while running Kubernetes in production.",
             "Mike hasn't had an outage, and ran Kubernetes in production.",
             "Mike hasn't had an outage, but ran Kubernetes for the lab.",
             "Mike hasn't had an outage, running Kubernetes in production."]  # a comma alone is a boundary
    keeps = ["Mike hasn't run Kubernetes in production; the closest thing he has done is the Python reports.",
             "Mike hasn't run Kubernetes, but he has moved the reports to Python.",
             "Mike has never used Rust or Kubernetes; the closest thing he has done is the lab clusters.",
             "Mike has not yet used Kubernetes; the closest thing he has done is the lab clusters.",
             "Mike has never deployed or operated Kubernetes clusters; the closest thing he has done is the lab."]
    vs = each([C(t, type="gap", units=("u1", "u4")) for t in drops + keeps])
    assert [v.reasons for v in vs] == [("honesty",)] * len(drops) + [()] * len(keeps)


def test_low_depth_need_fails_honesty_outside_gap():
    need = "Kubernetes in production"
    vs, _ = run([C("Mike deployed clusters for the lab.", type="match", units=("u4",), need=need, req="r01"),
                 C("Mike hasn't run Kubernetes in production" + GAP_TAIL, type="gap", units=("u4",), need=need,
                   req="r01")], kind="jd")
    assert [v.reasons for v in vs] == [("honesty",), ()]


def test_gap_pivot_may_name_a_claimable_skill():
    # only the negated clause, before "closest", ";" or the first sentence break, is checked for a denial
    vs = each([C("Mike hasn't worked in healthcare; the closest thing he has done is the postgres reports.",
                 type="gap"),
               C("Mike hasn't worked in healthcare, but the closest thing he has done is the Python reports.",
                 type="gap"),
               C("Mike hasn't worked in healthcare. He moved the reports to Python.", type="gap")])
    assert [v.reasons for v in vs] == [()] * 3


def test_gap_naming_low_depth_ledger_term_passes_grounding():
    # "Airflow" is in no unit and not in the input (which wrote the alias); the ledger term grounds it
    gap = C("Mike hasn't run Airflow; the closest thing he has done is the nightly reports.", type="gap",
            units=("u6",))
    for question in ("Has he used dagster?", ""):
        assert run([gap], input_text=question)[0][0].reasons == ()
    # honesty still bars asserting it outside a gap
    assert run([C("Mike ran Airflow for the nightly reports.", units=("u6",))])[0][0].reasons == ("honesty",)


# ---- slop, privacy, voice, verdict -----------------------------------------

def test_em_dash_tidied_not_dropped_hype_dropped():
    vs, kept = run([
        C("Mike cut nightly run time by 40% \u2014 across 1,200 jobs from 2019\u20132021 and 2022 \u2013 2023.",
          type="match", need="faster runs \u2013 nightly", req="r01"),
        C("Mike built an exceptional nightly pipeline.", type="match", req="r01"),
        C("Mike cut nightly run time by 40%.", type="match", need="a robust pipeline", req="r01"),
        C("Mike cut nightly run time by 40%.", type="match", need="nightly runs", req="r01"),
    ], kind="jd")
    assert vs[0].kept and vs[0].tidied and vs[0].reasons == ()
    assert kept[0].text == "Mike cut nightly run time by 40%, across 1,200 jobs from 2019-2021 and 2022-2023."
    assert kept[0].need == "faster runs, nightly"
    assert vs[1].reasons == ("slop",) and not vs[1].tidied
    assert vs[2].reasons == ("slop",)
    assert vs[3].kept and not vs[3].tidied


def test_input_company_allowed_other_applied_company_blocked():
    jd = f"{VISITOR_CO} is hiring a data engineer to speed up its nightly pipelines."
    own = C("Mike cut nightly run time by 40% across 1,200 jobs.",  # the visitor's company, in `need`
            type="match", need=f"faster pipelines at {VISITOR_CO}", req="r01")
    other = C(f"Mike wrote the {OTHER_CO} importer.", type="match", units=("u3",), req="r01")  # u3 grounds it
    other_need = C("Mike cut nightly run time by 40%.", type="match", need=f"{OTHER_CO} pipelines", req="r01")
    plain = C("Mike tagged the zzq42x build.", type="match", req="r01")
    vs, kept = run([own, other, other_need, plain], kind="jd", input_text=jd + " zzq42x")
    assert [v.kept for v in vs] == [True, False, False, False]
    assert {v.reasons for v in vs[1:]} == {("privacy",)}
    assert kept == [own]

    # privacy allows the visitor's company, but it never grounds `text`: no cited unit names it
    vs, _ = run([C(f"Mike built {VISITOR_CO} pipelines for the reports.", type="match", req="r01")], kind="jd",
                input_text=jd)
    assert vs[0].reasons == ("grounding",)

    # with no mention in the input, the same claim is blocked; a lowercase mention does not count
    no_name = "A data engineer to speed up nightly pipelines."
    for text in (no_name, no_name + " " + VISITOR_CO.lower()):
        vs, _ = run([C("Mike cut nightly run time by 40%.", type="match", need=f"faster pipelines at {VISITOR_CO}",
                       req="r01")], kind="jd", input_text=text)
        assert vs[0].reasons == ("privacy",)


def test_first_person_and_verdict_fail():
    t = "Mike ran 77 load tests on the queue."
    vs, _ = run([C("I ran 77 load tests on the queue.", type="match", units=("u2",), req="r02"),
                 C(t, type="match", units=("u2",), need="our load testing", req="r02"),
                 C("Mike is a strong fit, he ran 77 load tests.", type="match", units=("u2",), req="r02"),
                 C(t, type="match", units=("u2",), need="a perfect match for load tests", req="r02")], kind="jd")
    assert [v.reasons for v in vs] == [("voice",), ("voice",), ("verdict",), ("verdict",)]


def test_reflexive_first_person_fails_voice():
    vs, _ = run([C("Mike ran 77 load tests on the queue myself.", type="match", units=("u2",), req="r02"),
                 C("Mike ran 77 load tests on the queue.", type="match", units=("u2",), need="load tests of mine",
                   req="r02"),
                 C("Mike ran 77 load tests on the queue.", type="match", units=("u2",), need="load tests run by ourselves",
                   req="r02")], kind="jd")
    assert [v.reasons for v in vs] == [("voice",), ("voice",), ("voice",)]


# ---- length and caps -------------------------------------------------------

def test_caps_keep_first_four_in_model_order(monkeypatch):
    words = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot"]
    claims = [C(f"Mike ran 77 load tests on the {w} queue.", units=("u2",)) for w in words]
    claims.insert(1, C("Mike ran 77 robust load tests.", units=("u2",)))  # fails slop, takes no slot
    vs, kept = run(claims)
    assert len(vs) == len(claims) and [v.claim.text for v in vs] == [c.text for c in claims]
    assert [v.reasons for v in vs] == [(), ("slop",), (), (), (), ("length",), ("length",)]
    assert kept == [claims[i] for i in (0, 2, 3, 4)]

    # TEXT_MAX and NEED_MAX are inclusive
    at_max = ("Mike ran 77 load tests on the queue " + "x" * TEXT_MAX)[:TEXT_MAX - 1] + "."
    vs, _ = run([C(at_max, units=("u2",)), C(at_max[:-1] + "x.", units=("u2",))])
    assert len(vs[0].claim.text) == TEXT_MAX and [v.reasons for v in vs] == [(), ("length",)]
    t = "Mike ran 77 load tests on the queue."
    vs, _ = run([C(t, type="match", units=("u2",), need="n" * NEED_MAX, req="r02"),
                 C(t, type="match", units=("u2",), need="n" * (NEED_MAX + 1), req="r02")], kind="jd")
    assert [v.reasons for v in vs] == [(), ("length",)]

    # ANSWER_MAX counts kept text; once it would be passed, no later claim is kept, even a short one
    monkeypatch.setattr(gates, "ANSWER_MAX", 100)
    t = "Mike ran 77 load tests on the queue today."  # 42 characters
    vs, kept = run([C(t, units=("u2",)), C(t, units=("u2",)), C(t, units=("u2",)),
                    C("Mike ran 77.", units=("u2",))])
    assert [v.reasons for v in vs] == [(), (), ("length",), ("length",)] and len(kept) == 2


# ---- the gate-config bundle ------------------------------------------------

def _private(tmp_path):
    p = tmp_path / "priv"
    (p / "raw" / "jds" / "applied").mkdir(parents=True)
    (p / "raw" / "profile").mkdir(parents=True)
    (p / "denylist.local.txt").write_text("# invented\nre:zzq[0-9]+x\n")
    (p / "dontprint.local.txt").write_text("# invented\nhushword\n")
    (p / "honesty_allow.local.txt").write_text("K8s\n")
    (p / "raw" / "jds" / "top50_index.json").write_text(json.dumps([{"company": VISITOR_CO}]))
    (p / "raw" / "jds" / "applied" / "quillfeather-labs-J7.txt").write_text("x")
    (p / "raw" / "profile" / "profile.json").write_text(json.dumps({"skill_depth": [
        {"term": "Python", "depth": "strong"}]}))
    return p


BUNDLE_CLAIMS = [
    C("Mike tagged the zzq42x build."),
    C("Mike kept the hushword list."),
    C(f"Mike wrote the {OTHER_CO} importer.", units=("u3",)),
    C("Mike ran the nightly reports.", units=("u6",), need=f"reports for {VISITOR_CO}"),
    C("Mike deployed K8s clusters for the lab.", units=("u4",)),
    C("Mike ran 77 load tests on the queue.", units=("u2",)),
]


def test_bundle_roundtrip_and_stale_detection(tmp_path):
    priv = _private(tmp_path)
    path = priv / "deploy" / "gate_config.json"
    assert bundle_stale(path, priv)  # no bundle yet
    assert write_bundle(priv, path) == {"patterns": 2, "companies": 2, "honesty_allow": 1}
    assert (path.stat().st_mode & 0o777) == 0o600
    data = json.loads(path.read_text())
    assert set(data) == {"patterns", "companies", "honesty_allow", "built_from"}

    a = AnswerGateConfig.from_private(priv, LEDGER)
    b = AnswerGateConfig.from_bundle(path, LEDGER)
    assert [p.pattern for p in a.patterns] == [p.pattern for p in b.patterns]
    assert [c.name for c in a.companies] == [c.name for c in b.companies] == sorted([OTHER_CO, VISITOR_CO])
    assert a.honesty_allow == b.honesty_allow == frozenset({"k8s"})
    for input_text in ("", f"{VISITOR_CO} is hiring."):
        va = gate_claims(BUNDLE_CLAIMS, PACK, UNITS, a, input_text, "question")
        assert va == gate_claims(BUNDLE_CLAIMS, PACK, UNITS, b, input_text, "question")
        assert [v.kept for v in va[0]] == [False, False, False, bool(input_text), True, True]

    assert not bundle_stale(path, priv)
    applied = priv / "raw" / "jds" / "applied"
    changes = [
        lambda: (applied / "blorp-co-J8.txt").write_text("x"),
        lambda: (applied / "blorp-co-J8.txt").unlink(),
        lambda: (priv / "dontprint.local.txt").write_text("# invented\nhushword\nquietword\n"),
        lambda: (priv / "denylist.local.txt").write_text("re:zzq[0-9]+y\n"),
        lambda: (priv / "company_allow.local.txt").write_text("Blorp Co\n"),
        lambda: (priv / "honesty_allow.local.txt").write_text("K8s\nPostgres\n"),
        lambda: (priv / "raw" / "jds" / "top50_index.json").write_text(json.dumps([{"company": "Gadgetron"}])),
        lambda: (priv / "raw" / "profile" / "profile.json").write_text(json.dumps({"skill_depth": []})),
        lambda: (priv / "raw" / "profile" / "profile.json").unlink(),
    ]
    for change in changes:
        change()
        assert bundle_stale(path, priv)
        write_bundle(priv, path)
        assert not bundle_stale(path, priv)
    path.write_text("{not json")
    assert bundle_stale(path, priv)


def test_from_bundle_fails_closed(tmp_path):
    priv = _private(tmp_path)
    path = tmp_path / "gate_config.json"
    write_bundle(priv, path)
    good = json.loads(path.read_text())
    for bad in ({k: v for k, v in good.items() if k != "patterns"}, {**good, "patterns": []},
                {**good, "companies": []}, {**good, "patterns": ["("]}, {**good, "patterns": ["x?"]},
                {**good, "honesty_allow": "k8s"}):
        path.write_text(json.dumps(bad))
        with pytest.raises(ValueError) as e:
            AnswerGateConfig.from_bundle(path, LEDGER)
        assert "zzq" not in str(e.value) and "hushword" not in str(e.value)
    path.unlink()
    with pytest.raises(FileNotFoundError):
        AnswerGateConfig.from_bundle(path, LEDGER)


def test_gate_config_cli_writes_bundle_and_prints_counts_only(tmp_path, monkeypatch, capsys):
    from askmike.answer import __main__ as cli

    priv = _private(tmp_path)
    monkeypatch.setenv("ASK_PRIVATE_DIR", str(priv))
    assert cli.main(["gate-config"]) == 0
    out = capsys.readouterr().out
    assert "2 patterns" in out and "2 companies" in out and "1 honesty-allow" in out
    for secret in ("zzq", "hushword", "Zorblax", "Quillfeather", "k8s", "K8s"):
        assert secret.lower() not in out.lower()
    assert not bundle_stale(priv / "deploy" / "gate_config.json", priv)
    assert AnswerGateConfig.from_bundle(priv / "deploy" / "gate_config.json", LEDGER).companies


# ---- links and length ------------------------------------------------------

LINK_UNITS = dict(UNITS, **{
    "u8": _unit("u8", "Mike built the shop site at gpett.com.", "The tool lives at github.com/example/tool."),
    "u9": _unit("u9", "Mike ran the queue.")})
LINK_PACK = Pack(("u8", "u9"), {}, ())


def _link(claims, input_text="", kind="question"):
    return gate_claims(claims, LINK_PACK, LINK_UNITS, cfg(), input_text, kind)[0]


def test_ungrounded_link_fails_grounding():
    ok, bad, own, forn, sub, url = _link([
        C("Mike built the shop site at gpett.com.", units=("u8",)),
        C("Mike ran the queue at evil.example.com.", units=("u9",)),
        C("Mike ran the queue.", units=("u9",)),
        C("Mike ran the queue, see forn.al.", units=("u9",)),
        C("Mike ran the queue, see census.forn.al.", units=("u9",)),
        C("Mike ran the queue, see https://apply-here.example.org/x.", units=("u9",)),
    ])
    assert ok.kept and own.kept and forn.kept and sub.kept
    assert not bad.kept and bad.reasons == ("grounding",)
    assert not url.kept and url.reasons == ("grounding",)
    assert _link([C("Mike built the shop site at GPETT.com.", units=("u8",))])[0].kept  # case-insensitive


def test_link_in_need_may_come_from_the_input():
    t = "Mike ran the queue."
    v_in, v_unit, v_none = _link([C(t, type="match", units=("u9",), need="apply at apply-here.example.org", req=""),
                                  C(t, type="match", units=("u8",), need="a site like gpett.com"),
                                  C(t, type="match", units=("u9",), need="apply at other.example.org")],
                                 input_text="Please apply at apply-here.example.org", kind="jd")
    assert v_in.kept and v_unit.kept
    assert not v_none.kept and v_none.reasons == ("grounding",)
    # the input never grounds a link in the claim text
    assert not _link([C("Mike ran the queue at apply-here.example.org.", units=("u9",))],
                     input_text="apply-here.example.org")[0].kept


def test_overlong_claim_fails_length_before_other_gates():
    t0 = time.perf_counter()
    v = _link([C("Mike ran the queue. " * 1500, units=("u9",)), C("Mike ran the queue.", units=("u9",), need="x" * (4 * NEED_MAX + 1)),
               C("I " + "a" * (4 * TEXT_MAX), units=("zz",))])
    assert time.perf_counter() - t0 < 0.2
    assert all(x.reasons == ("length",) and not x.kept for x in v)
    # just under the guard, the ordinary gates run and report their own reasons
    v = _link([C("I ran the queue. " + "a" * (4 * TEXT_MAX - 40), units=("u9",))])[0]
    assert "voice" in v.reasons and "length" in v.reasons


def test_link_path_must_be_stated_by_the_unit():
    bad, same, host = _link([C("Mike has a tool at github.com/attacker/repo.", units=("u8",)),
                             C("Mike has a tool at github.com/example/tool.", units=("u8",)),
                             C("Mike has a tool at github.com.", units=("u8",))])
    assert not bad.kept and bad.reasons == ("grounding",)
    assert same.kept and host.kept
