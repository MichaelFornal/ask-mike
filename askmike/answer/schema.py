"""The answer's structured-output schema.

It uses only type, properties, required, enum, items and additionalProperties, the subset that both
`claude -p --json-schema` and the API's output_config.format accept. Lengths, counts and id validity
are checked by the answer gates, not here.
"""
from __future__ import annotations

STATES = ("answered", "off_topic", "handoff", "no_evidence")
CLAIM_TYPES = ("match", "direct", "gap")
CLAIM_FIELDS = ("type", "need", "req", "text", "units")

ANSWER_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "state": {"type": "string", "enum": list(STATES)},
        "claims": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "type": {"type": "string", "enum": list(CLAIM_TYPES)},
                    "need": {"type": "string"},
                    "req": {"type": "string"},
                    "text": {"type": "string"},
                    "units": {"type": "array", "items": {"type": "string"}},
                },
                "required": list(CLAIM_FIELDS),
                "additionalProperties": False,
            },
        },
    },
    "required": ["state", "claims"],
    "additionalProperties": False,
}
