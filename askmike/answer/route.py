"""Input router: sorts any visitor input, and holds the question-kind rules (one source of truth).

askmike/evals/sets.py imports question_kind, personal_topics and PERSONAL_GOLD from here.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from askmike.answer.config import JD_MIN_CHARS, JD_MIN_LINES, MAX_INPUT_CHARS
from askmike.gates.text import _term_re

SKILLS_PATH = Path(__file__).resolve().parents[2] / "corpus" / "skills.json"  # the public ledger

PERSONAL_GOLD = {
    "work_auth": "person-work-authorization",
    "location": "person-location",
    "start_date": "person-start-date",
}

# Pieces the rules share. The words that also name technical things ("comp", "rate", "how much ... charge",
# "equity", "phone number") route to handoff only when they are about the person: a possessive ("his rate"), a
# personal subject near a verb ("does Mike charge"), or a bare form-field label ("Hourly rate", "Phone number").
# So "rate limiter", "comp engine" and "phone number validator" stay questions.
_WHO = r"(?:he|mike|you|the candidate)\b(?!['’]s)"  # a personal subject; "Mike's tool" is not one
_POSS = r"(?:his|your|mike['’]?s|the candidate['’]?s)"


def _field(body: str) -> str:
    """The whole input is one form-field label, with optional ':', '?' or required-field stars and one short
    trailing parenthetical ("Hourly rate (USD)", "Phone number (with country code)")."""
    return r"\A\s*" + body + r"\s*[:?*✱]*(?:\s*\([^()]{0,40}\))?\s*[:?*✱]*\s*\Z"


_TECH = (r"(?:data|engine|system|platform|tool|software|calculator|module|service|pipeline|api|app|database|table"
         r"|schema|planning|management|logic|feature|report|dashboard)s?\b")
_PHONE_TECH = (r"(?:validat|pars|format|verif|lookup|field|input|mask|normali[sz]|librar|extract|detect|regex|column"
               r"|type|check|clean|match|search|dedup|standardi[sz])\w*")
_RATE = r"(?:hourly|day|daily|bill|billing|contract|consulting)"
_ROLE = r"(?:role|position|job|opening|posting|listing|vacancy|opportunity|company|organi[sz]ation|team)\b(?!['’]s)"
_POSTING = (r"(?:role|position|job|opening|posting|listing|vacanc\w*|opportunity|company|organi[sz]ation)"
            r"\b(?![\w-]|['’]s)")
# "this role", "our company", "Examplecorp and this opening": a determiner (never "his") right before the role word
_THIS_ROLE = r"(?:[\w&.'-]+\s+(?:and|&)\s+)?(?:this|the|our|your|that)\s+(?:\w+\s+){0,2}?"
_NAME = r"(?P<name>(?-i:[A-Z])[\w&.'-]*(?:\s+(?-i:[A-Z])[\w&.'-]*){0,2})"  # up to three capitalised words
# equity as a direct object, not a modifier ("takes equity prices", "equity research")
_EQUITY_OBJ = (r"(?:(?:an?|some|any|more|less|the)\s+)?(?:(?:form|kind|amount|level|share|piece|portion) of\s+)?"
               r"equity\b(?![\s-]+(?:prices?|data|research|markets?|analysts?|funds?|deals?|valuations?|investors?"
               r"|instruments?|ind(?:ex|ices)|securities|trading|returns?|portfolios?|tokens?|tokeni[sz]\w*)\b)")

_PERSONAL_RULES = [
    ("work_auth", r"authori[sz]ed to work|work authori[sz]ation|legally authori|(legally|currently) (able|eligible|allowed|entitled) to work|eligible to work|right to work|sponsor|\bvisa\b|citizen"),
    ("location", r"current location|where .{0,20}live|what city|able to work (on-?site|in[- ]office|remotely)|remote, hybrid|hybrid or (on-?site|remote)|(on-?site|in[- ]office) (work|role|days)|where .{0,20}(based|located)|\bbased in\b|open to (remote|hybrid|on-?site|relocat)|willing to relocate|work (remotely|from home) (full[- ]time|for this role)|in[- ]office|commut"
                 + "|" + _field(r"(?:current |preferred )?(?:location|city|city,? (?:and )?state|city ?/ ?state)")),
    ("start_date", r"start date|earliest .{0,20}start|available to start|notice (do you need|period)|how much notice|when can (you|he|mike|the candidate) start|your availability|how soon can (you|he|mike|the candidate) start|date available|availability to start"),
]
_HANDOFF_RULES = [
    # pay and equity
    r"\bsalary\b(?![\s-]+" + _TECH + r")|\bcompensation(?![\s-]+" + _TECH + r")|\bpay (range|rate|expectation)"
    r"|\bdesired (pay|compensation)|\b" + _POSS + r" pay\b"
    r"|\b(?:" + _POSS + r"|desired|expected|target|total|current) comp\b"
    r"|\bcomp (?:expectations?|range|requirements?|package|band|target|ask)\b"
    r"|\bcomp\b.{0,30}\b" + _WHO + r".{0,20}\b(?:want|expect|need|look|seek|ask|target)"
    r"|\bhow much\b(?:\s+\w+){0,2}?\s+(?:do|does|did|would|will|can|could|should|is|was|are|were)\s+" + _WHO
    + r"(?:\s+\w+){0,3}?\s+(?:make|earn|earns|earning|paid|charge|charges|charging|bill|bills)\b"
    r"|\b" + _POSS + r"\s+(?:" + _RATE + r"\s+)?rates?\b(?![\s-]*(?:limit\w*|of)\b)"
    r"|\b(?:desired|expected|target|preferred|asking|minimum)\s+(?:" + _RATE + r"\s+)?rate\b(?![\s-]*(?:limit\w*|of)\b)"
    r"|\b" + _RATE + r" rate\b(?![\s-]*limit\w*).{0,25}\b" + _WHO
    + r".{0,15}\b(?:charge|want|expect|ask|bill|need|accept|look|seek|target)"
    r"|\b" + _RATE + r" rate (?:expectations?|requirements?|range|ask|target)\b"
    r"|\b" + _RATE + r" rate (?:for|on) (?:this|the|that|your) (?:contract|role|position|job|engagement|opportunity"
    r"|gig)\b"
    r"|\b(?:what|which) " + _RATE + r" rate (?:would|will|could|might)\s+(?:\w+\s+)?(?:work|suit"
    r"|be (?:acceptable|fair|ok|okay|right|reasonable))\b"
    r"|" + _field(r"(?:desired |expected |target |preferred |minimum )?" + _RATE + r" rate")
    + r"|" + _field(r"(?:(?:desired|expected|target|total|current)\s+)?comp(?:ensation)?(?:\s+(?:expectations?|range"
                    r"|requirements?))?")
    + r"|\bequity (?:expectations?|requirements?|ask)\b"
    r"|\bequity (?:package|grant|stake|split|offer)\b(?![\s-]*(?:track\w*|ledger|accounting|admin\w*|" + _TECH + r"))"
    r"|\b(?:what|which|how much)\s+(?:(?:kind|sort|level|amount) of\s+)?equity\s+(?:does|do|did|would|will|is|are)\s+"
    + _WHO + r"(?:\s+\w+){0,2}?\s+(?:want|expect|look|seek|ask|need|take|accept|get|consider)"
    r"|\b" + _WHO + r".{0,20}\b(?:wants?|expects?|expecting|looking for|seeks?|seeking|ask(?:s|ing)? for|requires?"
    r"|takes?|taking|accepts?|accepting|consider(?:s|ing)?|open to)\s+" + _EQUITY_OBJ,
    # contact details
    r"\b(?:what(?:['’]s| is)?|which|best|good|" + _POSS + r")\s+(?:(?:best|good|mobile|cell|direct|daytime)\s+)?"
    r"(?:phone|mobile|cell|contact|telephone) (?:number|no)\b(?![\s-]*" + _PHONE_TECH + r")"
    r"|\b(?:phone|mobile|cell|contact|telephone) (?:number|no)\b.{0,25}\b(?:for|reach|call|text|contact) "
    r"(?:him|mike|you|the candidate)\b"
    r"|" + _field(r"(?:(?:best|primary|preferred|mobile|cell|home|work|contact|daytime)\s+){0,2}"
                  r"(?:phone|mobile|cell|contact|telephone)(?:\s+(?:number|no\.?|#))?")
    + r"|\bby phone\b|\bwhat number\b|\b(call|text|reach|contact) (him|mike)\b"
    r"|\b(his|your|mike'?s|the candidate'?s) (cell|mobile|phone|number)\b(?! (support|screen|interview|system|validation))"
    r"|\b(his|your|mike'?s|the candidate'?s|home|mailing|street|residential|email) address\b(?! (validation|parsing|verification))",
    # demographics and EEO form fields, whose labels can run into other words ("GenderInput", "_eeoc_race")
    r"demographic|(?<!en)gender|(?<![a-z])race(?![a-z])(?![\s_-]*(?:conditions?|detector|free)\b)|ethnic|veteran"
    r"|disabilit|(?<![a-z])eeoc?(?![a-z])|pronoun|sexual orientation|self[- ]identif",
    r"\breferences?\b(?![\s-]+(?:architectures?|implementations?|docs?|documentation|data|designs?|guides?|manuals?|apps?"
    r"|solutions?|models?|tables?|types?|librar\w*|integrity|check\w*|pages?)\b)|\breferees?\b",
    # why this company or role, and how he found it
    r"why .{0,40}\b(this|the|our|that) (company|role|position|job|team|opening|organi[sz]ation|location)",
    r"\bwhy\b.{0,40}\b(?:our|your|this|the) (?:company['’]s )?(?:mission|vision|values|culture)\b",
    # why-interested names the role or company as its object: "this role", "us" or "joining" (a company name is
    # checked against the skills ledger in _named_company_ask), so "interested in retrieval systems" stays a question
    r"\bwhy (?:are|is|would|were|was|do|does) " + _WHO + r".{0,15}?\b(?:applying\b|want to (?:join|work (?:at|for|here"
    r"|with us))\b|(?:interested|drawn|excited|attracted|keen)\s+(?:in|about|by|to|for)\s+(?:" + _THIS_ROLE + _ROLE
    + r"|us\b|(?:joining|working (?:at|for|here|with us))\b))",
    r"\bwhat (?:interest|drew|draws|draw|attract|excit|appeal|motivat)\w*\s+(?:you|him|mike|the candidate)\b(?!['’]s)"
    r"\s+(?:to\s+(?:apply\s+(?:to|for)\s+|join\s+)?|about\s+|in\s+)"
    r"(?:" + _THIS_ROLE + r"(?:" + _ROLE + r"|mission)|us\b)",
    r"why .{0,30}\b(leav|quit|left|looking)",
    r"\b(?:how|where) (?:did|do|does|have|has|had|was|were) " + _WHO + r"(?:\s+\w+){0,2}?\s+(?:find|found|hear|heard"
    r"|learn|learned|learnt|discover|discovered|come across|came across|see|saw|stumbled? (?:on|upon|across))"
    r"(?:\s+out)?(?:\s+(?:about|of))?\s+(?:" + _THIS_ROLE + _POSTING + r"|us\b(?![\w-]))",
    # job-search history
    r"job[- ]search|other (offers|applications)|interviewing (elsewhere|with)|applied (here|before|previously)|where else"
    r"|relocation (package|assistance)",
    r"\blooking for in (?:a |an |the |your |his )?(?:new |next )?(?:role|job|position|company|team|employer|opportunity"
    r"|workplace)\b"
    r"|\b(?:wants?|seeks?|seeking|values?) in (?:a|an|the|your|his) (?:new |next )?(?:role|job|position|company|employer"
    r"|opportunity|workplace)\b",
    r"\b(?:employment|work|career|resume|r[eé]sum[eé]|cv) (?:gaps?|breaks?)\b"
    r"|\b(?:gaps?|breaks?)\b.{0,25}\b(?:employment|work history|working history|job history|career|resume"
    r"|r[eé]sum[eé]|cv)\b"
    r"|\bbetween (?:jobs|roles|positions)\b|\bunemploy",
]
_PERSONAL_RE = [(t, re.compile(p, re.I)) for t, p in _PERSONAL_RULES]
_HANDOFF_RE = [re.compile(p, re.I) for p in _HANDOFF_RULES]

# Asks that name a company: "Why are you interested in Quillon?" (the name ends the input) and "How did you
# hear about Quillon?". A name that is a skills-ledger term or alias, at any depth, is a technology ("Why is Mike
# interested in Kubernetes?", "How did Mike hear about Kafka?"), and the question stays a question.
_NAMED_ASKS = [re.compile(p, re.I) for p in (
    r"\bwhy (?:are|is|would|were|was|do|does) " + _WHO + r".{0,15}?\b(?:interested|drawn|excited|attracted|keen)\s+"
    r"(?:in|about|by|to|for)\s+" + _NAME + r"\s*[?.!]*\s*\Z",
    r"\b(?:how|where) (?:did|do|does|have|has|had|was|were) " + _WHO
    + r"(?:\s+\w+){0,2}?\s+(?:hear|heard)\s+(?:about|of)\s+" + _NAME + r"\b",
)]


def _ledger_names() -> list:
    rows = json.loads(SKILLS_PATH.read_text(encoding="utf-8"))
    return [_term_re(t) for r in rows for t in (r["term"], *(r.get("aliases") or ()))]


_LEDGER_NAMES = _ledger_names()  # loaded once at import; public, no model call


def _is_ledger_name(name: str) -> bool:
    return any(rx.search(name) for rx in _LEDGER_NAMES)


def _named_company_ask(text: str) -> bool:
    return any((m := rx.search(text)) and not _is_ledger_name(m.group("name")) for rx in _NAMED_ASKS)


def _is_handoff(text: str) -> bool:
    return any(rx.search(text) for rx in _HANDOFF_RE) or _named_company_ask(text)


def personal_topics(text: str) -> list:
    """Every personal topic the text matches; empty when a handoff term co-occurs."""
    if _is_handoff(text):
        return []
    return [t for t, rx in _PERSONAL_RE if rx.search(text)]


def question_kind(text: str) -> str:
    if personal_topics(text):
        return "personal"
    return "handoff" if _is_handoff(text) else "evidence"


@dataclass(frozen=True)
class Route:
    kind: str  # empty | too_long | handoff | jd | question
    topics: tuple = ()  # personal topics, questions only


def route(text: str) -> Route:
    stripped = (text or "").strip()
    if not stripped:
        return Route("empty")
    if len(stripped) > MAX_INPUT_CHARS:
        return Route("too_long")
    # a JD check comes before handoff rules: job descriptions state salaries
    nonempty = sum(1 for line in stripped.splitlines() if line.strip())
    if len(stripped) >= JD_MIN_CHARS or nonempty >= JD_MIN_LINES:
        return Route("jd")
    if _is_handoff(stripped):
        return Route("handoff")
    return Route("question", tuple(personal_topics(stripped)))
