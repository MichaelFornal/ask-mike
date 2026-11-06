"""Reads a served page for the app and page tests: its tags and attributes, its links, its visible lines, and the
lines of its answer section and lists. No browser and no JavaScript: the pages have none."""
from __future__ import annotations

import re
from html.parser import HTMLParser

BLOCK = frozenset("html head title body div header nav main section form label button p li ol ul h1 h2 h3 footer"
                  " textarea table thead tbody tr th td".split())
CAPTURE = frozenset({"section", "title", "li"})


def lines(text: str) -> list[str]:
    """Visible lines: one per block element, whitespace collapsed, blank lines dropped."""
    return [s for s in (re.sub(r"\s+", " ", x).strip() for x in text.split("\n")) if s]


def one(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


class Page(HTMLParser):
    def __init__(self, html: str):
        super().__init__(convert_charrefs=True)
        self.html = html
        self.tags: list[tuple[str, dict]] = []  # every start tag with its attributes, in order
        self.links: list[tuple[str, str]] = []  # (href, link text)
        self._captured: dict[str, list[str]] = {}
        self._items: list[tuple[str, str]] = []  # (the list tag holding an li, the li's text)
        self._open: list[tuple[str, list[str]]] = []
        self._lists: list[str] = []
        self._body: list[str] = []
        self._in_head = False
        self._a: list | None = None
        self.feed(html)
        self.close()

    # ---- parsing -------------------------------------------------------------------------------------------------

    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        self.tags.append((tag, d))
        if tag == "head":
            self._in_head = True
        if tag == "a":
            self._a = [d.get("href"), []]
        if tag in ("ol", "ul"):
            self._lists.append(tag)
        if tag in CAPTURE:
            self._open.append((tag, []))
        if tag in BLOCK:
            self._text("\n")

    def handle_startendtag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))

    def handle_endtag(self, tag):
        if tag in BLOCK:
            self._text("\n")
        if tag == "head":
            self._in_head = False
        if tag == "a" and self._a is not None:
            self.links.append((self._a[0], one("".join(self._a[1]))))
            self._a = None
        if tag in ("ol", "ul") and self._lists:
            self._lists.pop()
        if tag in CAPTURE:
            for i in range(len(self._open) - 1, -1, -1):
                if self._open[i][0] == tag:
                    _, buf = self._open.pop(i)
                    text = "".join(buf)
                    self._captured.setdefault(tag, []).append(text)
                    if tag == "li":
                        self._items.append((self._lists[-1] if self._lists else "", one(text)))
                    break

    def handle_data(self, data):
        self._text(data)
        if self._a is not None:
            self._a[1].append(data)

    def _text(self, s: str) -> None:
        if not self._in_head:
            self._body.append(s)
        for _, buf in self._open:
            buf.append(s)

    # ---- reading -------------------------------------------------------------------------------------------------

    @property
    def lines(self) -> list[str]:
        """The visible lines of the body (the head's title is `title`)."""
        return lines("".join(self._body))

    @property
    def title(self) -> str:
        return one((self._captured.get("title") or [""])[0])

    def section(self) -> list[str]:
        """The visible lines of the answer section (the page has at most one section)."""
        return lines((self._captured.get("section") or [""])[0])

    def items(self, list_tag: str) -> list[str]:
        """The text of each li inside an `ol` (the answer's claims) or a `ul` (a job description's requirements)."""
        return [text for tag, text in self._items if tag == list_tag]

    def attrs(self, tag: str) -> list[dict]:
        return [d for t, d in self.tags if t == tag]
