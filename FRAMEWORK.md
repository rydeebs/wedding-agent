# How this maps to the FDE framework

Two views: the six-stage loop, and the 20-point capability checklist. The
scoping decision (Stripe capped building at "a few hours") is explicit — some
points are fully built, one expensive-but-differentiating point (evals) is built,
and the pure-production points are designed and defended rather than shipped.

## The loop: Audit → Build → Evals → Deploy → Observe → Improve

| Stage | Where it lives |
|---|---|
| **Audit** | `audit/operating_map.md`, `criteria.json` |
| **Build** | `src/agent.py`, `src/tools.py`, `src/schemas.py`, `src/prompts.py`, `src/llm.py`, `src/grounding.py`, `src/scoring.py`, `src/dedupe.py` |
| **Evals** | `evals/golden_dataset.json`, `evals/run_evals.py`, `evals/eval_report.md` |
| **Deploy** | `src/config.py` (`AUTONOMY`), human-gate in `agent._draft_email` |
| **Observe** | `runs/<id>/audit_trail.jsonl`, `report.json`, `src/report.py` → `out/shortlist.html` |
| **Intake** | `src/criteria_io.py` (validation), `server.py` (localhost read/write), editable criteria quadrant in `src/dashboard.py` |
| **Improve** | golden dataset + eval gate; `prompts.py` records the v1→v2 change |

## The 20-point checklist

| # | Point | Status | Where |
|---|---|---|---|
| 1 | Agent loop | ✅ built | `agent.run` / `_process_region` / `_process_candidate` |
| 2 | Two tools | ✅ built | `tools.search_venues`, `tools.fetch_page` |
| 3 | Guardrails | ✅ built | input validation, `MAX_STEPS`, output filter, budget disqualifier, grounding checks, deterministic must-haves |
| 4 | Context & memory | ✅ built (deliberate) | flat `state["seen"]` + `state["identity"]` for cross-region dedupe — no vector DB by design |
| 5 | Audit trail | ✅ built | `agent._log` → `audit_trail.jsonl` |
| 6 | Real workflow | ✅ built | brother's venue search (`audit/operating_map.md`) |
| 7 | Checkpoint (wk1) | ✅ built | working agent w/ tools, guardrails, memory, audit trail |
| 8 | Structured outputs | ✅ built | `schemas.py` Pydantic models |
| 9 | Schema validation | ✅ built | `_structured` validates + retries once, else escalates |
| 10 | Failure modes | ✅ built | dead page, unrendered/partial page, malformed output, over-budget, missing data, fabricated + misattributed evidence, duplicate venue (full table in `evals/eval_report.md`) |
| 11 | Checkpointing | ✅ built | `_checkpoint` every region |
| 12 | Resume | ✅ built | `_resume_if_possible` (tested: interrupt → restart) |
| 13 | Failure handling | ✅ built | explicit paths: filter / escalate / skip, never crash |
| 14 | Checkpoint (wk2) | ✅ built | resumable, structured, recovers |
| 15 | Retries + backoff | ✅ built | `tools.with_backoff` 1·2·4·8·16s |
| 16 | Failure categories | ✅ built | classification / decision / pricing / capacity / grounding (evals) + tool failures |
| 17 | Golden dataset | ✅ built | `evals/golden_dataset.json`, 13 cases (normal/edge/incomplete/high-risk-noise/high-risk-budget/high-risk-hallucination/malformed-page/duplicate) |
| 18 | Run evals | ✅ built | `evals/run_evals.py` → pass rate + failure categories |
| 19 | Optimize cost | ✅ built | Haiku for classify, Sonnet for reasoning; cost/query metered |
| 20 | Multi-agent | ⬜ designed | not warranted — decomposition wouldn't help this task; noted in README |
| — | Workflow audit | ✅ built | `audit/operating_map.md` |
| — | System architecture | ◐ designed | diagram + README; see `out/shortlist.html` for output |
| — | Evaluation report | ✅ built | `evals/eval_report.md` |
| — | Deployment controls | ◐ partial | draft-only human gate built; logs/alerts/rollback/gradual-autonomy described |
| — | Business case | ✅ built | value section in `operating_map.md` + measured cost/time |

Legend: ✅ built · ◐ partial (built the high-signal slice) · ⬜ designed & defended

## The one deliberate "no"

`auto_send` autonomy is intentionally **not** implemented. Sending an inquiry to a
real venue is irreversible and carries the couple's reputation. The audit puts
that step under a human, so the agent drafts and stops. Choosing where autonomy
ends is the point of the role — not a gap.
