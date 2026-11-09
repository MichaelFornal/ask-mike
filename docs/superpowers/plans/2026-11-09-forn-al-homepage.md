# forn.al homepage and the two-part answer page: implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** land the reviewed corpus update with a fresh eval run; send ask.forn.al's model answers in two parts; give ask.forn.al the same explainer and example questions as the homepage; and rebuild forn.al's homepage as a portfolio with an Ask Mike section.

**Architecture:**
- `Desk.ask` splits into `prepare` and `finish`. Whatever `prepare` decides before the model call is still served whole.
- A cache miss becomes a `StreamingResponse`:
  - part 1 is the page head, the question and a waiting line
  - part 2 is the answer, or the fallback view if anything fails
- A CSS `:has` rule hides the waiting line once the answer is in.
- forn.al stays static HTML. Its example questions are separate one-field forms posting to the existing form contract.

**Tech Stack:** Python 3, FastAPI/Starlette, Jinja2, uvicorn, Caddy; forn.al is static HTML/CSS on Vercel; evals through `claude -p`.

**Spec:** `docs/superpowers/specs/2026-11-09-forn-al-homepage-design.md` (and, behind it, `docs/superpowers/specs/2026-11-03-ask-mike-design.md`). Both forn.al's site and the résumé live in jb, which is private. Their paths and commands are in `$ASK_PRIVATE_DIR/docs/m4-private.md` §1 (forn.al) and §2 (résumé), and nowhere in this repo.

## Global Constraints

- **No JavaScript** on forn.al or ask.forn.al: no `<script>`, no inline `style=`, no `on*=` handlers. CSP stays `style-src 'self'`.
- forn.al's three design rules apply: **no borders, no hue; mono is for machine output, serif for people's sentences.** `--highlight` is used once per page, by the textarea.
- **Voice:**
  - forn.al is first person ("my work").
  - ask.forn.al is third person ("Mike's work").
  - No slop, no hype adjectives, no em dashes, never "end to end".
- **Numbers:**
  - ask.forn.al prints only `facts.json` display strings, and they must pass `askmike.facts.copycheck`.
  - forn.al prints only counted facts, and they must pass its page gate.
  - This plan adds no new figure.
- **Form contract:** `POST https://ask.forn.al/ask`, urlencoded, field `q`, optional `prev`. forn.al's forms send `q` only.
- **CSP:** forn.al keeps `form-action https://ask.forn.al`, and ask.forn.al's CSP is unchanged.
- **Money:** no API spend except Mike's paid `calibrate --model picked` rerun (Task 3), which goes only to items whose prompts changed, and the smoke test's one `--ask` call (Task 10).
- **Public repo:**
  - Don't put absolute home-directory paths in tracked files.
  - Export `ASK_PRIVATE_DIR` before any pipeline command or commit.
  - Every commit uses the repo-local noreply identity, and the pre-commit scan has to pass.
- **Nothing is pushed or promoted without Mike's go.** The paid calibration runs in Mike's own terminal.

## Review Focus

1. **A visitor closes the tab after part 1 while the model is still working.** The slot is released exactly once, and nothing raises out of the route. This is Task 7's `test_a_stream_closed_early_releases_the_slot`.
2. **The model call, or the budget, fails after part 1 has gone out.** The same page ends with the fallback view and `NEXT_STEP` and closes its tags. The status stays 200, and the log line says `outcome: "error"` with file:line. This is Task 7's `test_a_failure_after_the_first_part_ends_the_page_with_the_fallback`.
3. **A pasted job description streams.** Neither part ever shows the posting's text. This is Task 7's `test_a_streamed_jd_never_shows_the_posting`.
4. **The same question is asked twice.** The second ask is a cache hit, served whole in one body chunk with no waiting line. This is Task 7's `test_a_repeat_question_is_served_whole_from_the_cache`.
5. **Visitor text holding HTML shows up in part 1.** It is escaped. This is Task 6's `test_waiting_head_escapes_the_question`.

---

### Task 1: Land the reviewed corpus update

**Files:** already modified in the working tree; commit them unchanged unless Mike's review edits them:
- `corpus/units.jsonl`, `corpus/skills.json`
- `askmike/answer/subjects.json`, `askmike/answer/fallback.json`
- `tests/test_pick.py`, `tests/test_support.py`
- `tools/pull_from_jb.py`

- [ ] **Step 1: Write the review packet** at `$ASK_PRIVATE_DIR/work/corpus-2026-11-07-diff.md`. Build it with a throwaway script that compares `git show HEAD:corpus/units.jsonl` with the working tree. It has four sections:
  - **Added:** id and claim
  - **Removed:** id and claim
  - **Claim changed:** id, old claim, new claim
  - **Fallback:** the `fallback.json` swap, with both units' claims

  Don't save the script in the repo.
- [ ] **Step 2: STOP for Mike.** He reads the packet and either signs off, or edits `$ASK_PRIVATE_DIR/work/review.md` in place. If he edits, run `uv run python -m askmike.corpus ingest`, then `gate`, then `review`, then `export`, until export succeeds, then rebuild the packet. The spec's rule applies: no unit is committed without his review.
- [ ] **Step 3: Verify.**
  - `ASK_PRIVATE_DIR=… python3 tools/pull_from_jb.py --check` must exit 0.
  - `uv run pytest -q` must pass.
  - `uv run python tools/scan_public.py` must be clean.
- [ ] **Step 4: Commit** the files above as "Corpus: Ask Mike's own evidence, the job-net figures and the fallback swap, reviewed by Mike". `facts --check` and `facts-m2 --check` fail from here until Task 3. That's expected, and nothing deploys before Task 10.

### Task 2: The answer ends with the email

**Files:**
- Modify: `askmike/answer/config.py` (`NEXT_STEP`)
- Test: `tests/test_pages.py`

**Interfaces:**
- Produces: `NEXT_STEP == "Email Mike: mike@forn.al · Résumé: https://forn.al/resume.pdf"`. `render_text`, the pages, `answer_metrics` and `deploy/smoke.py` all read it from this constant.

- [ ] **Step 1: Write the failing test** `test_next_step_leads_with_the_email`:
  - `NEXT_STEP` equals the string above.
  - In `pages.answer_page(...)` for an answer outcome, the `.next` paragraph's link hrefs are, in order, `["mailto:mike@forn.al", "https://forn.al/resume.pdf"]`.
- [ ] **Step 2: Run it:** `uv run pytest tests/test_pages.py::test_next_step_leads_with_the_email -v`. Expected: FAIL.
- [ ] **Step 3: Change `NEXT_STEP`** in `askmike/answer/config.py`.
- [ ] **Step 4: Run the full suite:** `uv run pytest -q`. Expected: pass. Any test that pinned the old literal instead of the constant gets the constant.
- [ ] **Step 5: Commit** as "Answers end with the email first". The M2 audit's render check fails until Task 3 re-renders the records.

### Task 3: Refresh the evals on the new corpus

This follows the 2026-11-06 refresh (`88f75e8`): everything runs through `claude -p` except the calibration items whose prompts changed. Every command here needs `ASK_PRIVATE_DIR` exported and starts with `uv run python -m`.

- [ ] **Step 1: Back up the eval work:** `tar czf $ASK_PRIVATE_DIR/backups/work-evals-answers-before-2026-11-09.tgz -C $ASK_PRIVATE_DIR work/evals work/answers`.
- [ ] **Step 2: Rebuild the index:** `askmike.retrieval index`. Expected: it exits 0 and writes `build/index.db`.
- [ ] **Step 3: Redo M1, in this order.** Expected: each exits 0. Running ~1.5 h in total is normal, so use `run_in_background` and wait for each to finish.
  - `askmike.evals pool`
  - `askmike.evals judge --judge A`
  - `askmike.evals judge --judge B`
  - `askmike.evals run --labels A`
  - `askmike.evals run --labels B`
  - `askmike.evals tune`
- [ ] **Step 4: Check `askmike/retrieval/settings.json` is unchanged** (`git diff --exit-code askmike/retrieval/settings.json`). If tune moved it, **STOP for Mike**, because it changes live retrieval.
- [ ] **Step 5: Write the M1 facts:** `askmike.evals facts`, then `askmike.evals report`. If `facts` refuses, run the step its message names, then rerun it.
- [ ] **Step 6: Redo M2.** For `M` in `claude-haiku-5-5` and `claude-sonnet-5-5`:
  - `askmike.evals answers --model M --split all`
  - `askmike.evals audit --model M` (expected: exit 0)
  - `askmike.evals support --model M --judge A`
  - `askmike.evals support --model M --judge B`

  Then `askmike.evals judge-calib`. Expected: exit 0.
- [ ] **Step 7: Check calibration parity:** `askmike.evals calibrate --model picked --summarise`.
  - If it reports changed prompts, **STOP for Mike**. In his own terminal, with his `ANTHROPIC_API_KEY`, he runs `! ASK_PRIVATE_DIR=<private dir> uv run python -m askmike.evals calibrate --model picked`. That re-sends only the changed items, under the default `--max-usd 3.00`.
  - Then rerun `--summarise`. Expected verdict: go.
- [ ] **Step 8: Write the M2 facts:** `askmike.evals facts-m2`, then `askmike.evals report-m2`. If either refuses, run the step it names, then rerun it.
- [ ] **Step 9: Run every gate.**
  - `askmike.evals facts --check`, `facts-m2 --check` and `facts-m3 --check` must each exit 0.
  - `uv run python -m askmike.facts.copycheck README.md docs/m1/REPORT.md docs/m2/REPORT.md` must be clean.
  - `uv run python tools/scan_public.py` must be clean.
  - `uv run pytest -q` must pass.
- [ ] **Step 10: Commit** `facts.json`, `docs/m1/REPORT.md` and `docs/m2/REPORT.md` as "Refresh the evals on the reviewed corpus". The message names each headline figure that moved: R4 recall@10, support rates, cost per answer, model time, and the unit count. Write those moves to `$ASK_PRIVATE_DIR/work/homepage/moved-figures.md`, because Task 11 reads that file.

### Task 4: Check the three example questions

**Interfaces:**
- Produces: `$ASK_PRIVATE_DIR/work/homepage/examples.md`, holding the final three strings. Task 9 and Task 12 copy those strings verbatim. Start from these three:
  1. `Has Mike shipped RAG in production?`
  2. `What has he built with the Claude API?`
  3. `How does he check what an AI says?`

- [ ] **Step 1: Run each one** through `ASK_PRIVATE_DIR=… uv run python -m askmike.answer ask "<question>"`, which is `claude -p` on Mike's picked model. A question passes when both of these hold:
  - the output doesn't start with the fallback intro ("Ask Mike can't answer that one right now.")
  - at least two claim lines come before the `NEXT_STEP` line
- [ ] **Step 2: Replace any question that fails.**
  - Write a replacement in the same voice, about the same area of skill, that passes Step 1.
  - Record the original, the replacement and both outputs' claim counts in `examples.md`.
  - Tell Mike which question changed.

### Task 5: The desk decides in two phases

**Files:**
- Modify: `askmike/service/desk.py`
- Test: `tests/test_desk.py`

**Interfaces:**
- Produces:
  - `class Pending` is a dataclass with these fields:
    - `visit_id: str`, `kind: str`, `text: str`
    - `prev_id: str | None`, `client: str`
    - `previous: Turn | None`, `key: str`, `t0: float`
    - plus a private released flag guarded by a `threading.Lock`
  - `Desk.prepare(text, prev_id, client) -> Served | Pending`:
    - It returns a finished `Served` for exactly the cases `ask` serves today without calling the answerer: the `NO_CALL` kinds, the 429 limit, a cache hit, and 503 busy.
    - Otherwise it returns `Pending`, holding one acquired slot.
    - When the fresh-call check says busy after a slot was acquired, it releases that slot before returning the 503.
  - `Desk.finish(p: Pending) -> Served`:
    - It calls `self.answerer.ask(p.text, p.previous)`, caches an `"answer"` outcome, and logs through `_served`.
    - It returns status 200 with `served_from="model"`.
    - It always calls `release(p)` in a `finally`.
  - `Desk.release(p: Pending) -> None` is idempotent: the slot is released only the first time it's called for a given `p`.
  - `Desk.ask(text, prev_id, client) -> Served` keeps its contract, as `prepare`, then `finish` when the result is `Pending`.

- [ ] **Step 1: Write the failing tests** in `tests/test_desk.py`, built on its `make_desk` and fake runner:
  - `test_prepare_serves_outcomes_decided_before_the_model_whole`. Each of these returns a `Served`:
    - a handoff gives 200 with `served_from == "none"` and the runner uncalled
    - a rate-limited address gives 429
    - a repeated question gives `served_from == "cache"`
    - `make_desk(..., fresh=0)` gives 503
  - `test_prepare_holds_a_slot_until_finish_releases_it`, with `slots=1`:
    - `p = prepare(q1)` is `Pending`
    - `prepare(q2, client="other")` is a 503 `Served`
    - `finish(p)` is a 200 `Served` with `served_from == "model"`
    - `prepare(q3, client="third")` is `Pending` again
  - `test_release_is_idempotent_and_frees_an_abandoned_slot`, with `slots=1`:
    - `p = prepare(q1)`, then `release(p)` twice
    - `prepare(q2, client="b")` is `Pending`
    - `prepare(q3, client="c")` is 503, so the slot count never went past one
  - `test_finish_releases_the_slot_when_the_answerer_raises`:
    - the runner raises `RuntimeError`
    - `finish(p)` raises it
    - the next `prepare` from another client is `Pending`
- [ ] **Step 2: Run them:** `uv run pytest tests/test_desk.py -k "prepare or release or finish_releases" -v`. Expected: FAIL (no `prepare`).
- [ ] **Step 3: Implement** `Pending`, `prepare`, `finish` and `release` in `askmike/service/desk.py`, and rewrite `ask` on top of them. Move today's `ask` body into `prepare` and `finish` without changing its order of checks.
- [ ] **Step 4: Run the full suite:** `uv run pytest -q`. Expected: pass, including every existing desk and app test.
- [ ] **Step 5: Commit** as "Desk: prepare and finish, so a model answer can stream".

### Task 6: The answer page in two parts

**Files:**
- Modify: `askmike/service/pages.py`, `askmike/service/templates/base.html` and `askmike/service/templates/answer.html`. Split the page into parts or partials however reads cleanest; the joined HTML must stay valid.
- Modify: `askmike/service/static/style.css`, in the "Ask Mike additions" section only.
- Test: `tests/test_pages.py`

**Interfaces:**
- Produces, in `askmike/service/pages.py`:
  - `WAIT_QUESTION = "Reading Mike's evidence"`, `WAIT_JD = "Reading the job description and Mike's evidence"` and `JD_LABEL = "Your job description"`
  - `answer_head(kind: str, text, waiting: bool) -> str`:
    - It covers everything from `<!doctype html>` through what was asked.
    - When `kind in ASKED_KINDS`, it shows "You asked" and the text.
    - For `kind == "jd"` with `waiting=True`, it shows `JD_LABEL` and never the text.
    - When `waiting=True`, it adds `<div class="reading"><p class="wait">{WAIT_*}<span class="dots">...</span></p></div>` under what was asked. There's no second label, because "You asked" or `JD_LABEL` is already above it.
    - It ends with `<main>` still open.
  - `answer_tail(o, units: dict, prev: str | None) -> str`:
    - It covers the requirements list (for a JD), the answer section, `.next`, the box and the closing tags.
  - `failed_tail(units: dict) -> str`:
    - It is the tail for the fallback view, with no `prev`.
  - `answer_page(o, units, text, prev)` returns `answer_head(o.kind, text, False) + answer_tail(o, units, prev)`.
- Produces, in `style.css`: `main:has(.answer) .reading { display: none; }`

- [ ] **Step 1: Write the failing tests:**
  - `test_head_and_tail_make_one_valid_page`:
    - For a question answer, a JD answer and a handoff, `answer_page(...) == answer_head(o.kind, text, False) + answer_tail(...)`.
    - The joined page parses with `page_html.Page` and has exactly one `<html` and one `</html>`.
  - `test_waiting_head_shows_the_question_and_the_wait_line`:
    - `answer_head("question", QUESTION, True)` contains QUESTION, `WAIT_QUESTION` and `class="reading"`.
    - It contains no `</main>` and no `class="answer"`.
  - `test_waiting_head_for_a_jd_never_shows_the_posting`:
    - `answer_head("jd", POSTING, True)` contains `JD_LABEL` and `WAIT_JD`.
    - None of POSTING's lines appear in it.
  - `test_waiting_head_escapes_the_question`:
    - With `text="<b>x</b> & y"`, the head contains `&lt;b&gt;x&lt;/b&gt; &amp; y` and not `<b>x</b>`.
  - `test_failed_tail_closes_the_page_with_the_fallback`:
    - `answer_head("question", QUESTION, True) + failed_tail(units)` holds the fallback intro, each fallback unit's claim and `NEXT_STEP`.
    - It has no hidden `prev` input and ends with `</html>`.
  - `test_stylesheet_hides_the_wait_line_once_the_answer_is_in`:
    - `style.css` contains `main:has(.answer) .reading { display: none; }` after the "Ask Mike additions" marker.
- [ ] **Step 2: Run them:** `uv run pytest tests/test_pages.py -v`. Expected: the new tests FAIL.
- [ ] **Step 3: Implement** the functions, the template split and the CSS rule.
- [ ] **Step 4: Run the full suite:** `uv run pytest -q`. Expected: pass, including `test_pages_have_no_script_inline_style_or_handlers` and the fallback-html match.
- [ ] **Step 5: Commit** as "Answer page renders as a head and a tail".

### Task 7: `/ask` streams a model answer

**Files:**
- Modify: `askmike/service/app.py` (the `ask` route)
- Test: `tests/test_app.py`

**Interfaces:**
- Consumes: Task 5's `prepare`, `finish`, `release` and `Pending`, and Task 6's `answer_head`, `answer_tail` and `failed_tail`.
- Behavior:
  - The route awaits `desk.prepare` in the threadpool.
  - A `Served` result is answered as today, in one `HTMLResponse` with its status and `Retry-After`.
  - A `Pending` result is answered with a `StreamingResponse(media_type="text/html; charset=utf-8")` whose async generator:
    1. yields `answer_head(p.kind, p.text, True)`
    2. awaits `desk.finish(p)` in the threadpool
    3. yields `answer_tail(served.outcome, units, served.visit_id)` and sets the log fields `outcome` and `served_from`
    4. on any exception from step 2 or 3, yields `failed_tail(units)` and sets `outcome="error"`, `served_from="none"` and `**_failure(e)`
    5. calls `desk.release(p)` in its `finally`
  - The pre-stream catch-all and `RequestLog` are unchanged.

- [ ] **Step 1: Add the test helpers** to `tests/test_app.py`:
  - `asgi_post(app, body: bytes, disconnect_after_first=False) -> list[bytes]`. It calls the ASGI app directly and collects each `http.response.body` message's bytes in order. With `disconnect_after_first`, `receive` returns `{"type": "http.disconnect"}` once the first body message has been sent.
  - `StreamDesk`, a fake whose `prepare` returns a `Pending` and whose `finish` returns a set `Served` or raises a set exception. It records `release` calls.
  - `FakeDesk` gains `prepare` (it returns the `Served` it holds), `finish` and `release`.
- [ ] **Step 2: Write the failing tests:**
  - `test_a_model_answer_streams_the_question_before_the_answer`:
    - `StreamDesk` answering C1 gives at least 2 chunks.
    - Chunk 0 contains QUESTION and `WAIT_QUESTION`, and no `class="answer"`.
    - The joined chunks equal a valid page that contains C1's text and `NEXT_STEP`, with status 200.
  - `test_a_failure_after_the_first_part_ends_the_page_with_the_fallback`:
    - `StreamDesk.finish` raises `RuntimeError("visitor text here")`.
    - The joined page contains the fallback intro and `NEXT_STEP` and ends with `</html>`. Status is 200.
    - The log line has `outcome == "error"` and a `where` pointing at the raise, and never "visitor text here".
    - `release` was called once.
  - `test_a_stream_closed_early_releases_the_slot`:
    - With `asgi_post(..., disconnect_after_first=True)` on `StreamDesk`, no exception escapes, and `release` was called exactly once.
  - `test_outcomes_decided_before_the_model_are_served_whole`:
    - With `FakeDesk` returning a handoff, a limited 429 and a busy 503, each gives exactly one non-empty body chunk with no `WAIT_QUESTION`.
    - Statuses and `Retry-After` are as today.
  - `test_a_streamed_jd_never_shows_the_posting`:
    - The `real` fixture answers a JD (POSTING) with a fake runner.
    - The joined body contains no POSTING line, and it contains `JD_LABEL` and both requirements.
  - `test_a_repeat_question_is_served_whole_from_the_cache`:
    - With the `real` fixture, asking QUESTION twice gives a first response of at least 2 chunks, and a second response of exactly 1 chunk with no `WAIT_QUESTION`.
- [ ] **Step 3: Run them:** `uv run pytest tests/test_app.py -v`. Expected: the new tests FAIL.
- [ ] **Step 4: Implement** the route as described.
- [ ] **Step 5: Run the full suite:** `uv run pytest -q`. Expected: pass. The existing `real`-fixture tests, such as the follow-up `prev`, now go through the stream, and `TestClient` buffers the body, so they should pass unchanged.
- [ ] **Step 6: Commit** as "/ask streams a model answer: the question and a waiting line first".

### Task 8: Caddy flushes `/ask`, and the smoke test checks the two parts

**Files:**
- Modify: `deploy/ask.caddy`, `tests/test_deploy.py` (the `ASK_CADDY` constant) and `deploy/smoke.py`
- Test: `tests/test_deploy.py`

**Interfaces:**
- Produces, in `deploy/ask.caddy`: the line `\tencode zstd gzip` becomes these two lines, so the encoder never buffers the stream:
  ```
  	@compressible not path /ask
  	encode @compressible zstd gzip
  ```
  `reverse_proxy` already flushes a response of unknown length, so `flush_interval` doesn't change. `push.sh`'s `caddy validate` checks the syntax on the box.
- Produces, in `deploy/smoke.py`:
  - `ask_chunks(base, q, headers=None) -> tuple[int, list[tuple[float, bytes]]]` posts like `ask()` and reads the body with `read1()`, timing each chunk.
  - `check_ask` passes when all of these hold:
    - status 200
    - `NEXT_STEP` is on the page
    - `WAIT_QUESTION` is on the page
    - the first chunk holding `class="answer"` arrived at least 1.0 s after the chunk holding `WAIT_QUESTION`

    Otherwise it reports "ask: the answer came in one piece (cache hit?), so streaming wasn't checked; use a question not asked since this deploy", or "ask: the waiting line and the answer arrived together".

- [ ] **Step 1: Write the failing tests:**
  - Update `ASK_CADDY` to the new file text.
  - `test_ask_caddy_never_compresses_the_streamed_answer`:
    - The file contains `@compressible not path /ask` and `encode @compressible zstd gzip`, and no bare `encode zstd gzip` line.
  - Using the smoke tests' existing fake-server pattern:
    - `test_smoke_passes_an_answer_sent_in_two_parts`: the fake sends the head, sleeps 1.2 s, then sends the tail.
    - `test_smoke_reports_an_answer_sent_in_one_piece`.
- [ ] **Step 2: Run them:** `uv run pytest tests/test_deploy.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** the Caddy change and the smoke changes.
- [ ] **Step 4: Run the full suite:** `uv run pytest -q`. Expected: pass.
- [ ] **Step 5: Commit** as "Caddy leaves /ask uncompressed; smoke checks the answer arrives in two parts".

### Task 9: ask.forn.al's front page explains both uses

**Files:**
- Modify: `askmike/service/templates/home.html`, `askmike/service/templates/base.html` (a block after the box), `askmike/service/pages.py`, and `askmike/service/static/style.css` (the Ask Mike additions)
- Create: `askmike/service/templates/not_found.html` (today's `home.html` body: h1 "Ask Mike" and the lede)
- Test: `tests/test_app.py` and `tests/test_pages.py`

**Interfaces:**
- Consumes: Task 4's three strings.
- Produces:
  - `pages.EXAMPLE_QUESTIONS: tuple[str, str, str]`
  - `home_page()`, with the exact copy below
  - `not_found_page()`, which renders `not_found.html` with `OFF_TOPIC_TEXT`
  - the box label on every page becomes "Your question, or a job description" (follow-ups keep "Ask a follow-up")

  The home page's copy, in this order:
  ```
  h1:      Ask Mike
  p.prose: An AI that knows Mike's work. Two ways to use it:
  ul.uses: <b>Ask a question</b> and get a direct answer.
           <b>Paste a job description</b> and get the strongest matches between what the role needs and what Mike has done.
  p.small: It only uses a record of Mike's work that he has reviewed. Every line of the answer links to its evidence.
  [the box, then the saved line]
  p:       Or try one:
  each:    <form method="post" action="/ask"><input type="hidden" name="q" value="{q}"><button type="submit" class="linkish">{q}</button></form>
  ```

- [ ] **Step 1: Write the failing tests:**
  - `test_home_explains_both_uses_and_offers_three_examples`:
    - The page lines contain the four explainer sentences exactly.
    - The forms are the box (textarea `q`, action `/ask`) and then three example forms, each with method post, action `/ask`, one hidden input `q` equal to `EXAMPLE_QUESTIONS[i]`, and a submit button whose text is that same string.
    - The page contains no `prev` input.
  - `test_an_example_tap_reaches_the_desk_as_its_question`:
    - `client.post("/ask", data={"q": EXAMPLE_QUESTIONS[0]})` records exactly that text on `FakeDesk.calls`.
  - `test_not_found_keeps_the_off_topic_line_without_examples`.
  - Update `test_home_has_the_form_posting_q_and_the_saved_line` to the new form list and label. `EMPTY_TEXT` stays the meta description and is no longer a page line.
- [ ] **Step 2: Run them:** `uv run pytest tests/test_app.py tests/test_pages.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** the templates, constants and CSS:
  - `.uses` is a hanging-indent list with a `· ` marker.
  - `.small` is the 0.75rem muted line.
  - Example buttons reuse `.linkish` in serif and keep the 2px focus outline.
- [ ] **Step 4: Run every gate:** `uv run pytest -q`, `tools/scan_public.py` and the copycheck. Expected: clean.
- [ ] **Step 5: Commit** as "ask.forn.al's front page: both uses explained, three example questions".

### Task 10: Ship ask.forn.al

- [ ] **Step 1: Run the release gates locally.** `uv run pytest -q`, the three `facts* --check`, the copycheck and the scan. Expected: all clean. This is what `deploy/push.sh` will run.
- [ ] **Step 2: STOP for Mike's go.** Then Mike runs `! git push origin main`.
- [ ] **Step 3: Deploy.** Run `ASK_HOST=<ssh target> ASK_PRIVATE_DIR=<private dir> deploy/push.sh`; Claude may run this, as on 2026-11-07. Expected: exit 0, and "deployed <sha>".
- [ ] **Step 4: Run both smoke tests.**
  - `uv run python deploy/smoke.py https://ask.forn.al --ask "<a question not asked before this deploy>"`. It's one Haiku call, and the new deploy changes the cache key, so it streams. Expected: `smoke: ok`, including the two-part check.
  - DTD's `deploy/smoke.py https://deals.forn.al`. Expected: `smoke: ok`.
  - `/health` reports the new `git_sha`.
- [ ] **Step 5: If the two-part check fails through Caddy:** add `flush_interval -1` to `reverse_proxy` in `deploy/ask.caddy`, using Task 8's test pattern, then redeploy and rerun Step 4. If it still fails, **STOP for Mike**.

### Task 11: The résumé's Ask Mike figures (only if Task 3 moved one)

- [ ] **Step 1: Read `moved-figures.md`.** If none of the Ask Mike figures the résumé prints has moved, mark this task done. Those figures are AM2's recall pair and interval, and AM4's average cost per answer.
- [ ] **Step 2: Otherwise, update the résumé.**
  - Run the résumé's facts sync `--check`, then `--apply` for the Ask Mike source only (companion §2).
  - Rebuild the 2-page résumé. Expected: build exit 0, two pages, no ATS FAIL.
  - Copy the PDF to the canonical path and to forn.al's `resume.pdf` (companion §2).
  - Commit in jb.

### Task 12: forn.al's homepage, layout A (forn.al repo; companion §1)

**Files** (in forn.al's site folder):
- Modify: `index.html`, `styles.css` and `sitemap.xml`
- Delete: `projects.html`

**Interfaces:**
- Consumes: Task 4's three strings.
- Produces: the homepage's Ask Mike section, with this exact copy and markup. The class names are free within forn.al's rules:

  ```html
  <section>
    <h2 class="sec">Ask Mike</h2>
    <p class="prose">An AI that knows my work. Two ways to use it:</p>
    <ul class="uses">
      <li><b>Ask a question</b> and get a direct answer.</li>
      <li><b>Paste a job description</b> and get the strongest matches between what the role needs and what I've done.</li>
    </ul>
    <p class="small">It only uses a record of my work that I've reviewed. Every line of the answer links to its evidence.</p>
    <form class="ask" method="post" action="https://ask.forn.al/ask">
      <label for="q">Your question, or a job description</label>
      <textarea id="q" name="q" rows="6" required></textarea>
      <button type="submit">Ask</button>
      <p class="note">Questions are saved so I can read them.</p>
    </form>
    <div class="tries">
      <p class="k">Or try one:</p>
      <!-- one per example, in Task 4's order -->
      <form method="post" action="https://ask.forn.al/ask"><input type="hidden" name="q" value="{q}"><button type="submit">{q}</button></form>
    </div>
    <p class="ask-links"><a href="https://ask.forn.al/results">how it's measured</a> · <a href="https://github.com/MichaelFornal/ask-mike">code</a></p>
  </section>
  ```

- [ ] **Step 1: Branch** `homepage-a` in forn.al's site repo.
- [ ] **Step 2: Rewrite `index.html`.**
  - The masthead nav loses `Projects`.
  - The h1, the `.h1sub` line and the footer stay as they are.
  - The section above goes after the intro.
  - Then add a `<section>` with `<p class="label">Projects</p>`, followed by the three `.role` entries moved verbatim from `projects.html` (DTD, job-net, GPTee), figures included.
- [ ] **Step 3: Update `styles.css`.**
  - `.sec`: mono, `clamp(1.25rem, 3vw, 1.625rem)`, weight 600, line-height 1.2, letter-spacing −0.02em, margin `0 0 var(--s3)`.
  - `.uses`: the résumé list style already on `main ul` / `main li`.
  - `.small`: 0.75rem, `--muted`.
  - `.tries` buttons: reset to look like serif links, underline in `--faint`, with a 2px `--ink` focus outline. They take no `--highlight`.
  - Add `max-height: 50vh` to `.ask textarea`.
- [ ] **Step 4: Remove the old Projects page.** Delete `projects.html`, and remove `/projects` from `sitemap.xml`.
- [ ] **Step 5: Run the page gate** with no flags, and its `--selftest` (companion §1). Expected: exit 0, with no form, link, sitemap, number or slop error. Every form posting to the contract URL has a field `q` and nothing else.
- [ ] **Step 6: Look at it.**
  - Serve the folder locally (`python3 -m http.server`).
  - With Playwright, screenshot `/` at 375 and 1280 px in light and dark. There must be no horizontal scroll.
  - Type "What is Mike's salary?" into the box and submit. It should reach the live handoff page with no model call.
  - Don't click the examples, since each one is a model call. Check their markup instead.
- [ ] **Step 7: Commit** on `homepage-a` as "Homepage: portfolio with an Ask Mike section; projects back on the page".

### Task 13: forn.al preview, Mike's review, promote

This task needs Task 10 to be live first, because the example forms post to it.

- [ ] **Step 1: Deploy a preview:** `vercel deploy --cwd <site folder>` (companion §1). Check it with `curl`:
  - `/` returns 200 with the box and three example forms, each posting to `https://ask.forn.al/ask`
  - the CSP still holds `form-action https://ask.forn.al`
  - `/projects` returns 404
  - `/sitemap.xml` has no `/projects`
- [ ] **Step 2: Submit from the preview with Playwright.** A handoff question in the box should reach the handoff page.
- [ ] **Step 3: STOP. Mike reviews the preview in his own browser.**
- [ ] **Step 4: Promote**, on his go: `vercel promote <preview> --yes --cwd <site folder>`. Verify production with the Step 1 checks. Fast-forward `homepage-a` into the site repo's `main`.

### Task 14: Follow-ons

- [ ] **Step 1: The profile README.** Its Ask Mike entry prints the evidence-unit count. Set it to `facts.json`'s current display for the unit count in Mike's `MichaelFornal/MichaelFornal` clone, and commit. Mike pushes, or gives his go for Claude to push.
- [ ] **Step 2: The copy sheet.** In `$ASK_PRIVATE_DIR/work/m4/copy.md`, replace slots 5–12 with this design's copy, and note that the Projects page is gone.
- [ ] **Step 3: Memory.** Update the project notes, the jb memory `ask-mike-project.md` and this repo's memory: the corpus size, the refreshed figures, the streaming answer, forn.al's layout A, and the production deployment ids.
