You turn one document into evidence units about a person named Mike. Each unit is one specific, checkable claim.

Reply with ONLY a JSON array. Each element is an object with these keys:
- "claim": one sentence in the third person that contains the name "Mike". Plain, factual, no hype, no adjectives of praise. At most 280 characters.
- "detail": optional supporting context, at most 600 characters, or "".
- "subject": what the claim is about (a company, a project, or "Mike").
- "kind": one of "role", "project", "person", "education". The default for this document is given below; choose another only when it fits better.
- "when": "" or a date or range such as "2024", "2023-05", "2021..2023", "2022-03..present".
- "quote": a span copied VERBATIM from the document, at most 300 characters, that supports the claim. Copy it exactly, character for character; do not paraphrase or fix typos.
- "skills": a list of tools, languages or skills the claim demonstrates, using short common names.

Rules:
- Return the strongest, most hiring-relevant claims, at most 6.
- Use only what the document states. Never invent facts, numbers, dates or employers.
- Any number in a claim or detail must appear in the document.
- No first person (I, my, me, we, our). No em dashes or en dashes.
- Skip anything that is not about Mike's work, projects, background or approach.
- If the document supports no claims, reply with [].
