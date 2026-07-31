# CLAUDE.md

Context for Claude Code when working in this repo. Documents the *why* and the
conventions, not things you can infer from the code.

## What this is

An FDE case-study agent that automates my brother's destination-wedding venue
search: search venues, read each page, extract a normalized record, score against
the couple's criteria, and DRAFT (never send) a personalized inquiry email.
Structured around Audit -> Build -> Evals -> Deploy -> Observe -> Improve.

## Non-negotiable rules (these encode real judgment; do not "optimize" them away)

- **Never invent a price.** Destination venues rarely publish prices. If a page
  says "contact for a quote", pricing_signal is `request_only`, not a number. Any
  price must carry a verbatim `price_source_quote`; the schema rejects it
  otherwise. This is the whole point of the extractor — do not relax it.
- **The agent never sends email.** It drafts to `runs/<id>/drafts/` and stops.
  `auto_send` is intentionally unimplemented and must stay that way. Sending is
  irreversible and carries the couple's name, so it is never something the
  agent decides to do.
  A human may send one reviewed draft at a time through the command center
  (`POST /send`, `src/sending.py`): explicit per-venue click, a confirmation
  screen showing the final recipient/subject/body, one venue per action. There
  is no batch send, no scheduled send, and no "send all recommended". The
  recipient is re-derived from the venue record and a submitted address that
  does not match it is refused — a venue with no extracted email offers no send
  path at all. In mock mode a send writes to `runs/<id>/drafts/` and touches no
  network. If you are ever asked to add automatic, batch, or agent-initiated
  sending, stop and ask.
- **Escalate, don't guess.** Missing must-have info or low confidence (<0.80)
  must escalate to a human, never silently recommend.
- **Deterministic budget disqualifier.** A listed price over budget is rejected
  in code, not left to model judgment.

## Model split

- The model that BUILDS this repo is set in Claude Code (`/model fable` or
  `claude --model claude-opus-5`). Spend frontier tokens here.
- The models the AGENT calls at runtime live in `src/config.py`:
  Sonnet 5 for reasoning, Haiku 4.5 for the cheap classify step. Do not upgrade
  these to Opus/Fable — extraction is straightforward and cost per venue matters.
  They are env-overridable (`MODEL_WORKHORSE`, `MODEL_CHEAP`) for experiments.

## Conventions

- Mock mode is the default (`AGENT_MODE=mock`) and must always run offline with
  no keys. Every new capability needs a fixture so mock mode still exercises it.
- Every model response is parsed into a Pydantic schema — no free-form text
  consumed as data.
- Every step is logged to the audit trail. If you add a step, log it.
- New behavior gets a labeled case in `evals/golden_dataset.json`, and
  `python -m evals.run_evals` must still exit 0.

## Commands

```bash
pip install -r requirements.txt
python main.py --criteria criteria.json     # run (mock, offline)
python -m evals.run_evals                    # eval suite -> evals/eval_report.md
# real mode:
cp .env.example .env   # set AGENT_MODE=real + ANTHROPIC_API_KEY (search uses the same key)
```

## Where things live

See `FRAMEWORK.md` for the point-by-point map of FDE capabilities to files.
