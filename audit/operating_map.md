# Audit Artifact — Operating Map

The audit decides *what* to automate before any building starts. This is the one
artifact that comes out of the Audit stage.

## Who does the work today

My brother and his fiancée, planning a destination wedding. Over ~6 weeks they
evaluate destination venues across three candidate regions (Tulum, Cabo, Sedona).

## Current-state workflow (how it actually happens)

The documented process is "look at venues online." The *real* process, traced
step by step:

1. **Find candidates** — Google "Tulum wedding venue", open 10+ tabs. Half the
   results are listicles and planner sites, not actual venues.
2. **Read each site** — hunt for capacity, on-site lodging, indoor rain backup.
   Every site buries these in different places; some never state them.
3. **Figure out price** — almost no destination venue lists a price. It's
   "contact us for a quote", so there's no way to filter on budget up front.
4. **Track it** — retype findings into a messy spreadsheet, one row per venue,
   fields half-filled.
5. **Reach out** — fill in a contact form or write an email per venue, re-typing
   the same details about guest count and dates each time.

**Measured pain (from watching him):** ~8–10 minutes per venue, ~40 venues
across the three regions = **5–7 hours**, most of it re-keying and chasing
pricing that isn't published. Errors show up later: venues shortlisted that turn
out to be over budget or too small, discovered only after a back-and-forth.

## The judgment point

The hard, human part isn't finding venues — it's **deciding a venue fits when the
page doesn't say enough**, and **not chasing prices that don't exist**. Any
automation that pretends to know capacity or invents a price is worse than
useless; it sends him down false paths.

## Future-state workflow (rebuilt around AI)

Deterministic intake (criteria) → the agent searches, filters non-venues,
extracts a normalized record per venue, scores against the couple's must-haves,
and **drafts** a personalized inquiry email — but only for venues it can
actually confirm. Anything with missing information or genuine ambiguity is
**escalated to a human** instead of guessed. Sending stays human.

## Selected use case

Venue evaluation + outreach drafting. High volume (dozens of venues), repetitive
(same loop each time), and the improvement is large (hours → minutes for the
first pass).

## Boundaries — what the system may and may not do

| May | May not |
|---|---|
| Search, fetch, and read public venue pages | Send any email (human sends) |
| Extract facts stated on the page | Infer capacity/price not on the page |
| Score against criteria; recommend/reject | Auto-book or contact venues |
| Escalate ambiguous cases to a human | Assign a price without a source quote |

## Where autonomy stops

- **Deterministic software:** intake validation, budget disqualifier.
- **Agent:** search, classify, extract, score, draft.
- **Human:** any venue with missing must-have info, and every email send
  (irreversible, relationship-carrying action).

## Expected business value

- **Time:** ~5–7 hours of manual research → a few minutes of agent runtime plus
  human review of a ranked shortlist and pre-drafted emails.
- **Errors reduced:** over-budget / under-capacity venues are disqualified or
  escalated automatically instead of surfacing after outreach.
- **Cost:** ~$0.01–0.02 per venue evaluated (measured, see run report).
