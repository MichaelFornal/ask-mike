import pytest

import askmike.answer.route as route_mod
from askmike.answer.route import Route, route
from askmike.evals import sets

_BODY = ("The role builds internal tools for a fictional logistics firm called Examplecorp Freight. "
         "You will work with ops teams, write services, and own releases. ")
JD_LONG = ("Platform Engineer at Examplecorp Freight (invented). " + _BODY * 4
           + "Compensation: competitive base plus equity.")
JD_LINES = "\n".join(f"Requirement line number {i} for an invented role" for i in range(8))


def test_fixture_shapes():
    assert len(JD_LONG) >= 600 and "\n" not in JD_LONG
    assert 250 <= len(JD_LINES) < 600 and len(JD_LINES.splitlines()) == 8


@pytest.mark.parametrize("text,kind,topics", [
    ("", "empty", ()),
    ("   ", "empty", ()),
    ("x" * 20001, "too_long", ()),
    (JD_LONG, "jd", ()),
    (JD_LINES, "jd", ()),
    ("What is your compensation?", "handoff", ()),
    ("What's his phone number?", "handoff", ()),
    ("what's his salary and has he used Python?", "handoff", ()),
    ("Has Mike done phone support?", "question", ()),
    ("Has Mike worked in private equity?", "question", ()),
    ("has he done rag in prod?", "question", ()),
    ("python?", "question", ()),
    ("Is Mike authorized to work in the US?", "question", ("work_auth",)),
    ("Where is Mike based, and has he built agents?", "question", ("location",)),
])
def test_route_table(text, kind, topics):
    assert route(text) == Route(kind, topics)


@pytest.mark.parametrize("text", [
    "What is his phone?", "Can I call him on his cell?", "What is his cell phone", "Whats his mobile?",
    "What is his phone no.?", "What number can I reach him at?", "What is his pay?",
    "his comp expectations", "How much does he want to make?", "What equity does he want?",
    "What is his address?",
])
def test_personal_asks_hand_off(text):
    assert route(text).kind == "handoff"


@pytest.mark.parametrize("text", [
    "Did Mike run phone screens?", "Has Mike built payments with Stripe?",
    "Has Mike worked with equity research data?", "Has he built a contact form?",
    "What is Mike's address validation experience?",
])
def test_similar_evidence_stays_question(text):
    assert route(text) == Route("question", ())


# One invented phrasing per shape of the recruiter questions judge A labels handoff that the router once missed.
@pytest.mark.parametrize("text", [
    "Why is Mike interested in this specific position?",  # an adjective between "this" and "role"
    "Why are you interested in Quillon?",  # why-interested in a named company
    "What attracted you to Examplecorp and this opening?",  # what drew/interested you ... this position
    "Where did Mike see this opening, and what appeals to him about it?",  # where did he come across it
    "How did you come across this role?",
    "What is Mike looking for in a new job and team?",  # what he wants from his next job
    "Why does Mike care about our vision?",  # why our mission
    "Why do our values matter to you?",
    "Is there an employment gap Mike can explain?",  # gaps in his work history
    "What explains the break in his career?",
    "GenderSelect yourWomanManPrefer not to say",  # EEO form labels run into other words
    "a1b2c3d4__customfield_eeoc_section",
    "field__race_select",
    "Prefer to self-identify?",
])
def test_judge_labelled_handoff_shapes_hand_off(text):
    assert route(text).kind == "handoff"


# Pay, equity and contact words hand off only when they are about the person: a possessive, a personal
# subject near a verb, or a bare form-field label.
@pytest.mark.parametrize("text", [
    "What is Mike's hourly rate?", "What daily rate does he charge?", "Desired hourly rate", "Hourly rate*",
    "How much is Mike paid?", "How much would you charge per hour?", "How much money does Mike make?",
    "What comp does Mike want?", "What is his total comp?",
    "Does Mike expect equity?", "Would he take equity?",
    "Phone number*", "What is the best contact number for him?",
    # pay, equity, contact and discovery shapes the anchoring must not lose (review round 1)
    "Hourly rate expectations?", "Day rate requirements?", "Hourly rate for this contract?",
    "What hourly rate would work?", "Hourly rate (USD)", "Comp?", "Would Mike consider equity?",
    "Contact phone number:", "Phone number (with country code)", "How did you hear about Examplecorp?",
])
def test_person_anchored_asks_hand_off(text):
    assert route(text).kind == "handoff"


@pytest.mark.parametrize("text", [
    "Has Mike built a comp engine?",  # bare "comp"
    "Has Mike built an hourly rate limiter?", "Did Mike tune the daily rate limit on the API?",  # "hourly rate"
    "How much did his app charge per request?", "How much did Mike's tool charge per call?",  # "how much ... charge"
    "How much hands-on (paid) work has Mike done with Kafka?", "How much experience does Mike have making dashboards?",
    "Has Mike built tools that equity analysts want?", "Does Mike know equity research tooling?",  # equity-want
    "Has Mike built an equity grant tracker?",
    "Has Mike built a phone number validator?", "Did Mike write contact number parsing?",  # "phone number"
    "Has Mike debugged a race condition?",  # "race"
    "How did Mike learn Rust?", "How did Mike find the root cause?",  # "how did he find/learn"
    "Has Mike built a reference implementation?",  # "reference"
    "Has Mike built a compensation planning tool?", "Has Mike worked with salary data?",
    "What drew Mike to retrieval work?",
    # the recall rules name a role, company or posting as their object (review round 1)
    "Why is Mike interested in retrieval systems?", "Why are you excited about eval tooling?",
    "What motivated Mike to automate the reports for his team?",
    "What drew Mike to build the retrieval system for the team?",
    "Where did Mike see the biggest cost savings for the company?",
    "Where did Mike find the slowest queries in the job runner?",
    "What equity data did Mike get from the vendor?", "What equity research did he need for the model?",
    "Has he built a model that takes equity prices as input?",
])
def test_technical_asks_with_handoff_words_stay_questions(text):
    assert route(text) == Route("question", ())


# A name in "why are you interested in X" or "how did you hear about X" is a company unless the public skills
# ledger lists it (any depth, term or alias), in which case it is a technology and the ask stays a question.
@pytest.mark.parametrize("text", [
    "How did Mike hear about Kafka?", "Where did Mike hear about RAG?", "Why is Mike interested in Kubernetes?",
    "Why was Mike drawn to Postgres?", "Why is Mike excited about Rust?",
])
def test_ledger_technology_names_stay_questions(text):
    assert route(text) == Route("question", ())


@pytest.mark.parametrize("text", [
    "Why are you interested in Quillon?", "How did you hear about this role?", "How did you hear about Quillon?",
    "What motivated you to apply to this role?",
])
def test_company_names_and_role_phrases_hand_off(text):
    assert route(text).kind == "handoff"


def test_ledger_names_come_from_the_public_skills_file():
    assert route_mod.SKILLS_PATH.name == "skills.json" and route_mod.SKILLS_PATH.parent.name == "corpus"
    assert route_mod._is_ledger_name("Kubernetes") and not route_mod._is_ledger_name("Quillon")


@pytest.mark.parametrize("text", ["City", "City, State", "City / State", "Current city:", "Preferred location*"])
def test_location_field_label_is_location(text):
    assert route(text) == Route("question", ("location",))


def test_location_field_rule_needs_the_whole_input():
    assert route("Has Mike built location tracking?") == Route("question", ())
    assert route("City data pipelines?") == Route("question", ())


def test_where_live_is_location():
    assert route("Where does Mike live?") == Route("question", ("location",))


def test_extra_handoff_forms():
    assert route("Contact number?").kind == "handoff"
    assert route("Can I reach him by phone?").kind == "handoff"
    assert route("What equity package does he expect?").kind == "handoff"


def test_sets_uses_route_rules():
    assert sets.question_kind is route_mod.question_kind
    assert sets.personal_topics is route_mod.personal_topics
    assert sets.PERSONAL_GOLD is route_mod.PERSONAL_GOLD
