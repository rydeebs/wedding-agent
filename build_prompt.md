# Claude Code build prompt

Paste everything below the line into a fresh Claude Code session to build this
agent from scratch. Recommended model: `/model fable` (or `claude --model
claude-opus-5`). The prompt is written outcome-first so a frontier model can plan
the path; the acceptance criteria at the bottom are the goal to hold to.

If you want to demo the *building process* for the Stripe video, run this live
and narrate the model's choices, then show the deliberate failure moment
(pricing hallucination) and the eval suite going green.

---

## Goal

Build a production-shaped AI agent that automates a real, recurring workflow: my
brother evaluating destination wedding venues and reaching out to them. The agent
searches candidate venues, reads each page, extracts a normalized record, scores
it against the couple's must-haves, and DRAFTS a personalized inquiry email. It
must never send anything, and it must escalate to a human anything it cannot
confirm from the page. Structure the whole thing as an FDE case study around the
loop: Audit -> Build -> Evals -> Deploy -> Observe -> Improve.

Work until the acceptance criteria hold. Build a MOCK mode (offline fixtures) so
the agent and its evals run deterministically with no API keys, plus a REAL mode
that calls the Claude API + serper.dev.

## The workflow being automated (Audit)

Write `audit/operating_map.md` first: who does the work, the current step-by-step
process, the measured pain (~8-10 min/venue x ~40 venues = 5-7 hours, mostly
re-keying and chasing prices that aren't published), the judgment point (deciding
fit when the page is silent; never inventing prices), the future-state workflow,
explicit boundaries (may read pages / may not send email or invent facts), where
autonomy stops, and quantified expected value.

## Architecture (Build)

Language: Python. Keep each module single-purpose.

- `src/schemas.py` — Pydantic models for structured output. Core model
  `VenueRecord` with: name, url, region, classification, capacity_max (nullable),
  has_onsite_lodging, has_indoor_backup, setting, and a pricing block:
  `pricing_signal` enum = {listed, request_only, unknown}, price_low_usd,
  price_high_usd, and `price_source_quote`. Add a validator that REJECTS any
  record with a price but no verbatim source_quote — this is the anti-
  hallucination guard. Also `ScoredVenue` (score, confidence, disqualified,
  decision in {recommend, reject, escalate}, rationale) and `OutreachEmail`.

- `src/prompts.py` — all prompts in one file. Keep the naive v1 extraction prompt
  as a comment ("what is the price of this venue?") and the hardened v2 that
  forbids inventing data, makes request_only a valid answer, and requires a
  source quote for any number. This documents the prompt evolution.

- `src/llm.py` — one `complete(prompt, model, meter, tag)` function. Track token
  usage and compute USD cost per call (a CostMeter). MOCK mode returns canned
  responses from `fixtures/llm_responses.json`, keyed by step+venue; support a
  list value per key so the first attempt can fail and a retry can succeed.

- `src/tools.py` — exactly two tools: `search_venues(region)` and
  `fetch_page(url)`. Wrap both in retry-with-exponential-backoff (1,2,4,8, cap
  16s). MOCK mode reads `fixtures/search_results.json` and `fixtures/pages.json`.
  REAL mode uses serper.dev and requests.

- `src/agent.py` — the agent loop and all controls:
  - loop: for each region, search -> for each result: classify (cheap model) ->
    if venue: fetch -> extract -> score -> if recommend: draft email.
  - guardrails: validate input criteria; MAX_STEPS cap; filter non-venues;
    deterministic hard budget disqualifier independent of the model; low
    confidence (<0.80) forces escalate.
  - structured outputs + schema validation: every model response parsed into its
    schema; on invalid JSON or failed validation, retry ONCE with an error hint,
    then escalate the item.
  - deliberate memory: a flat `seen` map (no vector DB — say why in a comment).
  - audit trail: log every step/prompt-tag/tool-call/result/error with timestamp
    to `runs/<id>/audit_trail.jsonl`.
  - checkpointing + resume: save state each region; on startup, resume from
    checkpoint if present.
  - explicit failure paths: dead page, malformed output, failed region — each
    handled, never a crash.

- `src/report.py` — render `report.json` into `out/shortlist.html`: a comparison
  table with capacity, lodging, backup, price signal, score, decision pill, and
  rationale, plus a header line with cost per venue and a note that emails are
  drafted not sent.

- `src/config.py` — MODEL_WORKHORSE=claude-sonnet-5, MODEL_CHEAP=claude-haiku-4-5
  (env-overridable), cost RATES table, MAX_STEPS, CONFIDENCE_ESCALATION=0.80,
  AUTONOMY="draft_only" (implement draft_only only; leave auto_send deliberately
  unimplemented and comment why), MODE="mock"|"real".

- `main.py` — `python main.py --criteria criteria.json`; runs the agent, renders
  the HTML, prints a summary (venues, recommended, escalated, steps, cost).

## Deployment control (Deploy)

Emails are written to `runs/<id>/drafts/*.txt` for human review — never sent.
Sending is the irreversible, relationship-carrying step and stays human by design.

## Evals (Evals)

- `evals/golden_dataset.json` — ~8-12 hand-labeled cases spanning four categories:
  normal, edge, incomplete-information, high-risk/ambiguous. Each case labels the
  expected classification, decision, and pricing_signal. Include a case where the
  page says "contact for a quote" whose correct pricing is request_only (this
  guards the hallucination fix), and an over-budget case that must be rejected.
- `evals/run_evals.py` — run the agent once in mock mode, grade each case on
  classification / decision / pricing, write `evals/eval_report.md` with a pass
  rate and failure categories and the operating rules, and exit non-zero on any
  failure so it works as a CI gate.

## Observe

Cost metered per venue; audit trail is the observability surface. The HTML report
is the human-facing artifact.

## Fixtures to include (so mock mode is realistic)

Cover: a normal venue (request-only pricing, meets must-haves); a venue with a
genuinely listed price under budget; a listicle that must be filtered out; a
venue missing capacity (must escalate); an over-budget resort (must reject); and
one venue whose FIRST extraction returns a price with no source quote (so the
schema validator rejects it and the retry succeeds — the demo failure moment).

## Acceptance criteria (the goal — do not stop until all hold)

1. `python main.py --criteria criteria.json` runs offline in mock mode and
   produces a shortlist HTML, drafted emails, an audit trail, and a report.json.
2. Exactly one venue's first extraction fails schema validation and its retry
   succeeds — visible as a `validation_failed` line in the audit trail.
3. Over-budget venue is rejected; venues with missing must-have info escalate;
   only recommended venues get drafted emails; no email is ever sent.
4. `python -m evals.run_evals` prints a pass rate and writes eval_report.md, and
   exits 0 when all cases pass.
5. `FRAMEWORK.md` maps each FDE point (agent loop, two tools, guardrails, memory,
   audit trail, structured outputs, schema validation, checkpointing, resume,
   failure paths, retries+backoff, failure taxonomy, golden dataset, eval suite,
   cost per query, workflow audit, architecture, eval report, deployment control,
   business case) to where it lives, marking each built / partial / designed.
6. Interrupting a run and restarting it resumes from the checkpoint with memory
   intact.

Keep total build scope to a few hours of equivalent work. Where a point is pure
production hardening (multi-agent, full rollback/alerts), design and document it
rather than over-building, and explain the choice — deciding where to stop is the
point.
