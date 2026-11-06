# Ask Mike: design

*2026-11-03. Approved by Michael Fornal. Working name: repo `ask-mike`, answer host `ask.forn.al`.*

This is the public version of the spec. A private copy carries what can't be published: the exact don't-print terms, the source paths inside the private job-search repo, and the list of conflicting facts Mike rules on in M0. It lives at `$ASK_PRIVATE_DIR/docs/design-private.md`. Wherever this document says *(private)*, the detail is in that file.

## 1. What it is, and who uses it

forn.al's homepage becomes one blank box. A hiring manager or recruiter who clicked through from Mike's resume does one of two things:

- types a question about him ("has he shipped RAG in production?"), or
- pastes a job description as plain text.

The AI never fetches a URL. What comes back is the case for why Mike fits: the strongest real matches between what they need and what he has done, each backed by evidence. There is no score, no percentage and no verdict. In Mike's words: "I don't want to lose possibilities from people using my own AI."

The AI is the proof of the skills it describes: retrieval-augmented generation, evals, grounding, and running a model-backed service in production. It succeeds Deal Terms Desk as Mike's lead showcase. Deal Terms Desk stays live at deals.forn.al as a maintained side project.

## 2. Goals

1. One ask returns a short case for Mike's fit, or a direct answer to the question, with every claim tied to evidence.
2. Every claim traces to an evidence unit Mike has reviewed. Nothing ships under his name that he can't defend in an interview.
3. Quality is measured. Evals are built before the app, and the results are published.
4. It runs within **$25/month all-in**, a total that covers the server it shares with Deal Terms Desk and both projects' model caps, and a visitor never sees an error.
5. Mike can read every question and job description that visitors submit, in a private log.

## 3. Non-goals

- No fit score, ranking or "not a fit" verdict.
- No URL fetching. Visitors paste text only.
- No per-application links and no changes to cover letters or outreach. The link lives on forn.al and in one line of the resume summary.
- No JavaScript on forn.al.
- Not a general chatbot. Off-topic requests get one line steering back to Mike's fit.
- No first person. The AI never speaks as Mike.
- The homepage copy is written later, in a separate pass with Mike.

## 4. Anti-goals (failures even if everything technically works)

- It talks a recruiter out of calling him.
- It claims a skill the honesty ledger marks "cannot defend", or states a number that no source supports.
- It reads as AI-written:
  - hype adjectives ("exceptional", "world-class", "passionate")
  - em dashes, "end to end", "leverage", filler openers
  - walls of text
- It leaks personal data, items on the don't-print list *(private)*, job-search details, or salary.
- It shows an error or a blank page when the cap or a rate limit is hit.
- Raw private material ends up in this public repo.

## 5. How an answer reads

- **Voice.** Third person, as a plain-spoken assistant who knows his record: "Mike built…".
- **For a pasted job description:** about three of the strongest matches, one sentence each, in the form *their need → specific evidence (project, role, number)*. Then the next step: the resume PDF and mike@forn.al.
- **For a question:** a direct answer in a few sentences.
- **Gaps: straight, then pivot.** "Mike hasn't run Kubernetes in production. The closest thing he has done is X."
- **Handoffs.** Some topics get "that one's for Mike" plus his email: salary, phone, home address, job-search history. Which personal facts it *may* answer (work authorization, location/remote, relocation, start date) is ruled in M0.
- **Follow-ups.** The box appears again under the answer, and a follow-up sees the previous turn.
- **Style rules.** Answers meet the same slop rules as Mike's site and resume.

## 6. The evidence (the corpus)

**Unit shape.** Each unit is one atomic fact with these fields:
- `id`
- `claim`, in third person
- `detail`
- `numbers`, each tied to its source
- `skills`, each with its honesty level
- `role` or `project`
- `when`
- `source`, a path inside the private pull

Mike reviews every unit before it is committed. The evidence is public along with the code: anything in the corpus can be pulled out by asking, so a unit goes in only if he would say it to a hiring manager's face.

**Which numbers a unit may state.** A unit may state:
- a number the canonical resume or the evidence store already prints, once Mike has reviewed the unit. This includes self-reported employer results; the resume already sends them to every employer.
- a project number only if it comes from a facts file that stores the command behind it.

The don't-print list *(private)* still applies on top.

**Sources, canonical first** (exact paths *(private)*):
1. the evidence store in the private job-search repo: role and project evidence items, the honesty ledger, narrative, counted facts
2. the canonical two-page resume and its four-page master
3. the machine-counted figures behind forn.al
4. forn.al's published copy
5. drafted application answers
6. interview-prep story notes and verified story notes
7. project write-ups and the portfolio
8. the public project repos (Deal Terms Desk, Agent Census, job-net, the GitHub profile, Alder Creek, GrowthOS), plus one approved blurb from a private one

**Raw material** is copied by `tools/pull_from_jb.py` into `$ASK_PRIVATE_DIR/raw/`, with a `MANIFEST.json` holding the sha256 of every file. Stale drafts and superseded fact packs are never used as sources.

**Honesty filter.** The honesty ledger rates every skill:
- `none`: never claimed.
- `familiar` and `learning`: only in the "hasn't, but the closest thing is" form.
- `working` and `strong`: claimable.

**M0 rulings.** Before the corpus is final, Mike rules on every fact where his sources disagree (titles, dates, counts, how the gap is framed, the honesty ledger's internal contradictions) *(private list)*.

## 7. Architecture

**Retrieval.** Ported from Deal Terms Desk:
- BM25 on SQLite FTS5
- dense retrieval on sqlite-vec, with bge-small via fastembed
- RRF fusion

For a pasted job description, a first step extracts its requirements, then retrieval runs once per requirement and the results are merged. A question is retrieved directly.

**Answering.** Claude through the official `anthropic` Python SDK. The answer is structured output: a list of claims, each citing unit ids. The evals choose between:
- Haiku 5.5, about $0.002 per answer
- Sonnet 5.5, about $0.035 per answer

Both prices are estimates until the calibration measures them (§8).

**Gates.** Every gate runs before display, and no ungated text ever reaches a visitor. Claims that fail are dropped. If too little survives, the visitor gets the fallback page.
1. **Grounding.** Every claim cites at least one unit that supports it.
2. **Honesty.** Checked against the honesty ledger.
3. **Slop.** The same banned list as the resume.
4. **Privacy.** PII patterns and the don't-print terms *(private)*.
5. **Length.**

**Fallback page.** A fixed, reviewed summary of the strongest evidence, plus the resume and email. It is served when the cap or a rate limit is hit, the API fails, or the gates empty an answer.

**Server.** Deal Terms Desk's Hetzner VM ($7.09/month), shared with DTD rather than taken over: FastAPI, Caddy and systemd, with Ask Mike as a second service beside `dtd.service`. Measured 2026-11-03: 2 vCPU, 3.8 GB RAM with 2.8 GB available, and DTD peaks at 500 MB, so both fit. Ask Mike gets its own system user, release and state directories, env file, localhost port, spend ledger and API key, so neither service can spend or break the other's. It reuses DTD's spend ledger, rate limits, answer cache, API runner and deploy scripts. The model's month cap is $25 − $7.09 server − $2.91 DTD model cap = **$15.00**. The builder sets the day cap and input caps; the input caps go up so a full job description fits.

**Shared Caddy.** DTD's deploy today installs its own Caddyfile over the box's whole config, which would wipe an `ask.forn.al` block on every DTD deploy (and the reverse). Before Ask Mike's first deploy, the config is split: a base Caddyfile holds the global log filter and imports one site file per project, and each repo's deploy installs and validates only its own site file.

**Page flow.**
- The forn.al homepage `<form>` posts to `ask.forn.al`. The only CSP change on forn.al is `form-action 'none'` → `form-action https://ask.forn.al`.
- `ask.forn.al` serves server-rendered HTML in forn.al's design, and it works with no JavaScript.
- The page shows the question, the answer with each claim linked to its evidence, the next step, and the box again.

**Private log.**
- Stored in SQLite on the server: question text, pasted job description, timestamp, answer id, outcome. No IP addresses.
- The page carries one line: "Questions are saved so Mike can read them."
- A CLI command pulls the log into the private folder.
- It never enters the repo, CI or deploy logs.

**Prompt injection.** Pasted text is treated as data. The gates are the structural defense. The prompt and the evidence are public anyway, so leaking them costs nothing. The evals include an injection set.

## 8. Evals (built before the app, published)

**Sets:**
- **Job descriptions:** real postings across the role families Mike targets (AI engineer, FDE/solutions, GTM, SWE, product). They are third-party text, so they stay private and only the metrics are published.
- **Recruiter questions:** taken from real application forms.
- **Gaps:** one question per `none`, `familiar` or `learning` skill.
- **Handoffs:** salary, address and similar.
- **Injection attempts.**

**Bars:**
- **Zero allowed:** honesty violations, slop, privacy leaks.
- **Must be correct:** every handoff.
- **Measured and reported:**
  - claim-support rate, scored by a calibrated judge
  - retrieval recall from requirement to unit
  - requirement coverage, compared with a keyword-match baseline
  - latency and cost per answer

**How the evals run.**
- All eval answering and grading go through `claude -p` on the Max plan, at $0. The prompt sent is byte-identical to the live prompt, checked by prompt-sha parity.
- Before launch, one API calibration of about 40 items runs (roughly $2, from the first month's cap). Stop rule: if API-vs-CLI agreement falls below 0.8, or the quality gap is above 0.15, stop and bring the options to Mike.
- The calibration also measures the real cost per answer. Token counts from `claude -p` include Claude Code's own system prompt, so they can't price an answer.

**Model choice.**
- **Measured per model:**
  - violations before the gates
  - claims dropped per answer
  - fallback rate
  - judge support rate
  - requirement coverage

  Measuring before the gates matters: after the gates, both models pass the zero-bars by construction.
- **The pick.** Mike reads about 10 answers from each model side by side, next to that table, and picks. The cap allows about 500 Sonnet answers a month, more than the traffic expected, so quality outweighs cost unless the two models tie.

**Publishing.** Numbers of record go in `facts.json`, each with the command that produced it. A copy check covers the README, and a results page is linked from the answer page.

## 9. Repo and private data

- **Public repo:** `github.com/MichaelFornal/ask-mike`, MIT license.
- **Commit identity:** every commit uses the noreply identity.
- **Layout:** `CLAUDE.md` rules, `docs/superpowers/{specs,plans}`, `facts.json`, and CI (uv, offline pytest, the facts gate).
- **Pre-push scan:** the generic patterns are public; the identity denylist lives only in the private folder.
- **Private material lives outside the repo, in `$ASK_PRIVATE_DIR`.** A folder outside the repo can't be `git add`ed by accident, and `pull_from_jb.py` refuses a destination inside any git work tree. It holds:
  - the raw pull and its manifest
  - the private design
  - the identity denylist
  - the eval job descriptions
  - pulled visitor logs

## 10. Changes outside this repo (at launch)

**forn.al:**
- The homepage becomes Mike's name, the copy (written later) and the form.
- A "Projects" page in the top nav holds job-net, GPTee and Deal Terms Desk.
- The CSP `form-action` change.

**Resume:** the Deal Terms Desk sentence in the summary is replaced with this project plus a line pointing to the AI on forn.al. Once this project has measured numbers it becomes the first project; Deal Terms Desk stays as the second, trimmed, and is the first entry cut when the page runs short *(private: build steps)*.

**Deal Terms Desk stays live (a maintained side project):**
- `dtd.service` keeps running, and the same VM also serves `ask.forn.al`.
- `deals.forn.al` keeps serving. No redirect.
- The DTD repo stays public as it is, with no "retired" note.
- On GitHub, this repo takes the first pin and DTD stays pinned.
- The shared-Caddy split (§7) ships as a DTD maintenance change before M3's first deploy.
- DTD gets maintenance only (model deprecations, certificates, dependencies). Whether to work on its answer accuracy is decided after this project launches.

## 11. Milestones

Each milestone gets its own plan, written when the previous one merges.

| | Milestone | Done when |
|---|---|---|
| M0 | **Corpus:** units from the pull, rulings for Mike, the honesty and privacy gates over the corpus, Mike's review | Mike signs off; corpus committed |
| M1 | **Retrieval + eval sets:** DTD retrieval ported, requirement extraction, all eval sets, baseline recall | Baseline numbers in `facts.json` |
| M2 | **Answering + gates:** prompt, structured output, all gates, judge calibration, Haiku vs Sonnet, fallback page | Every zero-bar met on both models; model picked |
| M3 | **Service:** `ask.forn.al` on the server shared with DTD (after the Caddy split ships), budget, limits, log, deploy, answer page, smoke test, and DTD still passing its own smoke test | Live behind an unlinked URL; both smokes pass |
| M4 | **Launch:** forn.al homepage, Projects page (with DTD), CSP, copy pass with Mike, resume line with DTD as second project, GitHub pins, results page | Mike approves the promote |

## 12. Left to the builder

- The prompt wording.
- The chunking and unit granularity.
- The day cap and input caps.
- The wait-state UX.
- How follow-ups carry context.
- The judge design and its calibration set.
- The exact JD sample size.
- Table schemas.
- Whether dense retrieval earns its memory, decided on measurements.
