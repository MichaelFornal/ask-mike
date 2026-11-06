"""Answer-time limits, model constants and the fixed display strings."""
from __future__ import annotations

# Limits
MAX_INPUT_CHARS = 20000
JD_MIN_CHARS = 600
JD_MIN_LINES = 6
K_PER_REQ = 5
K_QUESTION = 10
MAX_PACK = 40
PREV_MAX = 1000
MAX_CLAIMS = 4
MIN_KEPT = {"jd": 2, "question": 1}
TEXT_MAX = 300
NEED_MAX = 120
ANSWER_MAX = 1200

# Model calls (JUDGE_A / JUDGE_B stay in askmike/evals/judge.py)
ANSWER_MODELS = ("claude-haiku-5-5", "claude-sonnet-5-5")
MODEL_SLUGS = {"claude-haiku-5-5": "haiku", "claude-sonnet-5-5": "sonnet"}
ANSWER_EFFORT = "medium"
MAX_TOKENS = 8000

# Fixed display strings (placeholder copy until M4)
HANDOFF_TEXT = "That one's for Mike: mike@forn.al."
NEXT_STEP = "Resume: https://forn.al/resume.pdf · Email: mike@forn.al"
OFF_TOPIC_TEXT = ("Ask Mike answers questions about Mike's work and how it fits a role. "
                  "Ask about his experience, or paste a job description.")
EMPTY_TEXT = "Ask a question about Mike, or paste a job description."
TOO_LONG_TEXT = "That is longer than a job description. Paste the main part of the posting."
