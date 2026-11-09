# forn.al homepage and the Ask Mike experience: design addendum

*2026-11-09. A draft for Michael Fornal's approval. It amends `2026-11-03-ask-mike-design.md` §1 and the M4 plan's (`docs/superpowers/plans/2026-11-06-m4-launch.md`) Task 3 and Task 5.*

## 1. Why this exists

The M4 branch of forn.al turned the homepage into an ask box with no explanation and moved Mike's other projects to `/projects`. Mike's ruling (2026-11-08):

- **Ask Mike is one feature of the portfolio, not the whole homepage.** This replaces spec §1's "forn.al's homepage becomes one blank box".
- **A recruiter skims first, then asks.** The first screen says who Mike is. Ask Mike follows with a plain explanation of both ways to use it, and the rest of his work sits on the same page.

A second problem showed up in review. After a visitor clicks Ask, nothing on screen changes for the whole model call: `facts.json` `m2_cal_latency_ms_p50` / `_p95`. `/ask` renders the full page before sending a byte (`askmike/service/app.py`, `ask()`), and neither site runs JavaScript. A wait that long with nothing on screen is where visitors decide the page is broken.

## 2. Goals

1. A recruiter arriving from the résumé can tell within one screen who Mike is, what Ask Mike does, and that his other projects are below.
2. Both uses are explained up front: ask a question, or paste a job description.
3. Within a fraction of a second of clicking Ask, the visitor sees that their question was received and is being worked on.
4. An answer ends with one clear next step, which is to email Mike.

## 3. Non-goals

- No JavaScript on forn.al or ask.forn.al. This is unchanged from the spec.
- No suggested follow-up questions after an answer.
- No new figures, and no change to the answer pipeline, its gates or its prompt.
- No change to how the box posts (the M3 form contract: `POST https://ask.forn.al/ask`, field `q`, optional `prev`).

## 4. Anti-goals

- The homepage reads as a chatbot landing page: a persona, chat bubbles, or the AI ahead of the person.
- The waiting line stays on screen after the answer arrives, or a failure partway through leaves a blank or half-drawn page.
- An example question that the gates turn into the fallback, so the first tap looks like a failure.

## 5. Constraints carried over

- **Design.** forn.al's three rules: no borders, no hue, and mono for machine output with serif for people's sentences. No inline style, because both sites' CSP is `style-src 'self'`.
- **Voice.** forn.al is first person ("my work"). ask.forn.al is third person ("Mike's work"). No slop and no hype adjectives.
- **Numbers.** On forn.al, only counted facts that pass its page gate. On ask.forn.al, only `facts.json` display strings that pass `askmike.facts.copycheck`. This design adds no number.
- **CSP.** forn.al's only CSP change is still `form-action 'none'` → `form-action https://ask.forn.al` (already on the M4 branch). ask.forn.al's CSP is unchanged.
- **Money.** The build adds no API spend of its own. The example-question check runs through `claude -p` at $0. The corpus refresh in §10 re-sends only the calibration items whose prompts changed. That spend falls under the planned calibration budget.
- **Public repo.** forn.al's site lives in jb, which is private. Its paths are in the private companion `$ASK_PRIVATE_DIR/docs/m4-private.md` §1, never in this repo.

## 6. forn.al's homepage (Mike decided: layout A, explainer A2)

From top to bottom:

1. **Masthead.** Name on the left. Nav: `mike@forn.al · Résumé · LinkedIn · GitHub`. "Projects" leaves the nav.
2. **Intro**, unchanged: the h1 "I build products with AI and run them myself." and the sub-line "AI Engineer · Looking for an early-stage team · Contract-to-hire welcome · Massachusetts or US-remote".
3. **Ask Mike section:**
   - Heading **Ask Mike**, set as an h2 sized between the h1 and body text (the mockup used `clamp(1.25rem, 3vw, 1.625rem)`, weight 600, mono). This is a new class, because forn.al's existing `h2` is 1rem.
   - A serif line: "An AI that knows my work. Two ways to use it:"
   - Two list items in forn.al's résumé-list style (hanging indent, `·` marker), each starting with a short bold mono phrase:
     - "**Ask a question** and get a direct answer."
     - "**Paste a job description** and get the strongest matches between what the role needs and what I've done."
   - In small muted mono: "It only uses a record of my work that I've reviewed. Every line of the answer links to its evidence."
   - The box form, as on the M4 branch, posting to `https://ask.forn.al/ask` with one field `q`:
     - label "Your question, or a job description"
     - button "Ask"
     - note "Questions are saved so I can read them."
   - "Or try one:" and three example questions:
     - Has Mike shipped RAG in production?
     - What has he built with the Claude API?
     - How does he check what an AI says?
   - The links line: `how it's measured` (ask.forn.al/results) · `code` (the ask-mike repo).
4. **Projects.** A "Projects" label, then the three entries (Deal Terms Desk, job-net, GPTee), moved back from `projects.html` exactly as they are, figures included. Ask Mike isn't listed again.
5. **Footer**, unchanged.

**Example questions without JavaScript.** Each example is its own small form posting to `https://ask.forn.al/ask`, holding `<input type="hidden" name="q" value="…">` and a submit button styled as a serif link. That's the `.linkish` pattern from this repo's `style.css`. They can't be buttons inside the main form, because its textarea is `required` and an empty box would block the submit. Each example form must pass forn.al's page-gate form checks (M4 Task 2): the action is the contract URL and the form has a field named `q`.

**Cleanup.**
- `projects.html` is deleted, along with its `sitemap.xml` entry.
- Every link to `/projects` is removed.

**Phones.**
- Every field stays at 16px or larger so iOS doesn't zoom in. forn.al's textarea is `1.0625rem`, so keep it.
- A long paste scrolls inside the textarea rather than pushing the button off screen. Cap the textarea's height (for example `max-height: 50vh`) and keep `resize: vertical`.
- No horizontal scroll at 375 px.

## 7. ask.forn.al

### 7.1 Its own front page

A visitor who lands on ask.forn.al directly sees the same Ask Mike explanation as on forn.al, in third person:
- the heading "Ask Mike"
- "An AI that knows Mike's work. Two ways to use it:"
- the same two uses ("…and what Mike has done")
- the same small line on reviewed evidence
- the same three example questions, each its own form posting to `/ask`
- the existing box

This replaces the current one-line lede. Leave `EMPTY_TEXT` alone: the answerer also uses it for an empty input. The new text belongs to the home template.

### 7.2 The answer page arrives in two parts (Mike decided: stream)

A request that needs the model gets one HTML response, sent in two flushes:

- **Part 1, sent before the model call:**
  - the page head, the masthead, and the "You asked" label with the question as typed
  - for a job description, a label in place of the question (for example "Your job description"), because the page never shows the posting back (`ASKED_KINDS`)
  - a waiting line in the existing `.reading` / `.wait` / `.dots` styles: "Reading Mike's evidence…" for a question, and "Reading the job description and Mike's evidence…" for a JD
  - no number in the waiting line
- **Part 2, sent when the outcome is ready:**
  - for a JD, the requirements list
  - the answer section, the next-step line, the follow-up box, and the closing tags
- **The waiting line hides itself** with a stylesheet rule along the lines of `main:has(.answer) .reading { display: none }`. A browser without `:has` leaves it visible above the answer, which is acceptable.

**Where the split falls.** `Desk.ask` (`askmike/service/desk.py`) decides several outcomes before any model call. These are still served whole, in one response with their current status codes, and with no waiting line:
- the no-call kinds: handoff, off-topic, empty, too-long
- the rate limit (429 with `Retry-After`)
- a cache hit
- no free slot or no fresh-call allowance (503 busy)

Only once the desk has a slot and is about to call the answerer does the route start streaming, with status 200. The desk therefore needs a two-phase shape: a pre-check that either returns a finished `Served` or hands over a go-ahead holding the slot, and then the model call. The slot is released in a `finally` either way. Logging (`_served`, the visitor log and the request log) records exactly what it records today.

**Failure after part 1.** An exception or a budget stop after part 1 has been sent must render the fallback view (`_fallback_view`) as part 2, inside the same page, and close the document properly. The status is already 200. `RequestLog` can't replace a response that has started (`started` is True), so the route itself has to catch the error.

**Caddy.**
- Before building, confirm that part 1 reaches the client before part 2 through the live Caddy, with `encode zstd gzip` on. Use `curl -N --compressed` against a test route, or check locally with the same Caddyfile.
- If compression holds part 1 back, the smallest fix is in `deploy/ask.caddy`: exclude `/ask` from `encode`, or set `flush_interval -1` on the `reverse_proxy`.
- Caddy's 502/504 fallback page can't fire mid-stream. That's expected, since part 2 handles failures.

**Reload.** Reloading the answer page makes the browser offer to resend the form, and resending runs the model again. This is accepted for launch: the cache and the per-client limit bound the cost.

### 7.3 The end of an answer (Mike decided: the email is the one next step)

- `NEXT_STEP` (`askmike/answer/config.py`) becomes "Email Mike: mike@forn.al · Résumé: https://forn.al/resume.pdf". `linked()` makes both into links.
  - The prompt doesn't include it, so no answer changes. But `render_text` ends every answer with it, and the M2 audit compares each stored record's `rendered` text with a fresh render (`askmike/evals/answer_metrics.py`). So until the records are refreshed, the new string fails `facts-m2 --check`, and with it `deploy/push.sh`. It ships together with the corpus refresh in §10. Don't change the audit to get around this.
- No suggested follow-ups.
- The follow-up box stays under the next-step line, as it is now.
- Handoffs keep `HANDOFF_TEXT` unchanged.

## 8. Checks the build must pass

- **Example questions.** Run each of the three through `ASK_PRIVATE_DIR=… uv run python -m askmike.answer ask "<question>"`, which is `claude -p` at $0. Each must come back as an answer with at least two kept claims, not the fallback. If one fails, swap in a question that passes and tell Mike which one changed.
- **forn.al:**
  - the page gate with no flags: no form, link, sitemap, number or slop errors
  - screenshots at 375 and 1280 px, light and dark, with no horizontal scroll
  - the example forms are checked by their markup (the gate's form checks). One form holding a handoff question ("What is Mike's salary?") is submitted locally to the live ask.forn.al and reaches the handoff page with no model call. Submitting the three real examples live would cost three model calls, so they're checked only through `claude -p` (above).
- **ask.forn.al:**
  - `uv run pytest`, with new tests for the two-part response:
    - part 1 is flushed before the answerer returns
    - the pre-check outcomes are served whole with their status codes
    - a failure after part 1 yields a complete page holding the fallback view
    - the slot is released in every path
  - `uv run python tools/scan_public.py` and the copycheck
  - the deployed site sends part 1 before part 2. Only a cache miss streams, so this needs a real model call. It rides on the smoke test's existing `--ask` call, which M4 already budgets as one Haiku call: the smoke test learns to check that part 1 arrived before the answer. That adds no other live call.
  - both smoke tests (Ask Mike's and DTD's)
- **The copy sheet.** Update `$ASK_PRIVATE_DIR/work/m4/copy.md` slots 5–12 to this design: slots 9–12 change, because the Projects page goes away.

## 9. Left to the builder

- How the templates split into part 1 and part 2. Two templates, or blocks rendered separately: either is fine as long as the HTML is valid once joined.
- The exact class names and spacing on both sites, within the design rules.
- How the hidden-input example forms are styled, provided they read as serif links and keep the 2px focus outline.

## 10. The pending corpus update lands first (Mike decided, 2026-11-09)

The working tree holds an uncommitted corpus update from 2026-11-07:
- `corpus/units.jsonl`, `corpus/skills.json` and `askmike/answer/subjects.json`
- one swapped unit in `askmike/answer/fallback.json`
- test updates
- new sources in `tools/pull_from_jb.py` that give Ask Mike evidence about itself

As of 2026-11-09 it fails the release gates:
- `facts --check` reports M1 facts that differ from `facts.json`
- `facts-m2 --check` refuses on audit hits
- `facts-m3 --check` is clean

It lands as the first part of this work, in this order:
1. **Refresh the evals, the same way the 2026-11-06 refresh did (commit `88f75e8`).**
   - Rebuild the index.
   - Redo the M1 pools, both judges' labels, the M2 answer runs and their support verdicts through `claude -p`.
   - Re-send through the API only the calibration items whose prompts changed. That run shows in `m2_cal_spend_total`.
   - The `NEXT_STEP` change (§7.3) is made before the M2 answer runs, so the refreshed records carry it.
2. **Regenerate `facts.json`, the reports and the results page.** Run every `facts* --check` and the copycheck until clean.
   - Published figures may move.
   - If an Ask Mike figure the résumé prints moves, M4 Task 4's facts sync picks it up from `main`.
3. **Check the example questions** (§8) against this refreshed corpus and index, never against the old one.
4. **Then do the streaming change, ask.forn.al's front page and forn.al's page**, in that order.

## 11. Calls Claude made (Mike to check while reading)

- The "Résumé: https://forn.al/resume.pdf" wording in `NEXT_STEP`. It keeps the existing link mechanism; "Résumé (PDF)" as the link text would need a template change.
- The JD label in part 1 ("Your job description") and both waiting lines.
- The textarea height cap on phones.
- Deleting `projects.html` instead of keeping it as a second copy of the list.
- Leaving the reload re-run for after launch.
- Keeping the M2 audit as it is, and shipping `NEXT_STEP` with the corpus refresh rather than weakening the render check.
- Running the live streaming check on the smoke test's one budgeted `--ask` call, and checking the example questions only through `claude -p`, so verification makes no further live model calls.
