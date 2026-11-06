"""Read the text behind a unit's source ref: <raw-relative path>[#<json-pointer>]."""
from __future__ import annotations

import html
import json
import re
from pathlib import Path

from askmike.corpus.unit import Unit

_BLOCK = re.compile(r"<(script|style)\b.*?</\1\s*>", re.I | re.S)
_TAG = re.compile(r"<[^>]*>")


def strip_tags(html_text: str) -> str:
    """Drop tags (and script/style bodies), unescape entities, collapse whitespace."""
    t = _BLOCK.sub(" ", html_text)
    t = _TAG.sub(" ", t)
    return re.sub(r"\s+", " ", html.unescape(t)).strip()


def _walk(node, segments):
    for seg in segments:
        if isinstance(node, list):
            node = node[int(seg)]
        else:
            node = node[seg]
    return node


def doc_text(raw: Path, ref: str) -> str:
    path, _, pointer = ref.partition("#")
    root = Path(raw).resolve()
    file = (root / path).resolve()
    if not file.is_relative_to(root):
        raise ValueError(f"ref escapes the raw folder: {ref!r}")
    if pointer:
        data = json.loads(file.read_text(encoding="utf-8"))
        segs = [s for s in pointer.split("/") if s != ""]
        if len(segs) == 4 and segs[0] in ("roles", "projects") and segs[2] == "evidence":
            parent = _walk(data, segs[:2])
            for item in parent.get("evidence", []):
                if item.get("id") == segs[3]:
                    parts = [item.get("claim"), item.get("bullet"), item.get("metric")]
                    return "\n".join(p for p in parts if p)
            raise KeyError(f"no evidence item {segs[3]!r} in {ref}")
        value = _walk(data, segs)
        if isinstance(value, str):
            return value
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    text = file.read_text(encoding="utf-8")
    if file.suffix.lower() == ".html":
        return strip_tags(text)
    return text


def source_text(raw: Path, u: Unit) -> str:
    """The text a unit's numbers (and quote) are grounded against."""
    return doc_text(raw, u.source)
