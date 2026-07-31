"""
The wedding-venue agent.

FDE framework points implemented here:
  - Agent loop: prompt -> model -> response -> next step, until done or MAX_STEPS.
  - Two tools: search_venues + fetch_page, called on the agent's decision.
  - Guardrails: input validation, max-step cap, output filtering, hard disqualifiers.
  - Structured outputs + schema validation: every model response parsed into a
    Pydantic model; malformed output is retried once, then the item escalates.
  - Deliberate memory: a flat JSON of venues already seen (no vector DB -- nothing
    here needs to outlive the run).
  - Audit trail: every step, prompt tag, tool call, result, error, timestamp logged.
  - Checkpointing + resume: state saved every few venues; a run can restart from
    the last checkpoint.
  - Explicit failure paths: dead page / timeout / malformed output each have a
    defined behavior rather than crashing the run.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone

from pydantic import ValidationError

from . import config, dedupe, grounding, scoring, tools
from .llm import CostMeter, complete
from .prompts import CLASSIFY, EXTRACT, SCORE, EMAIL
from .schemas import (
    Classification,
    OutreachEmail,
    PricingSignal,
    ScoredVenue,
    VenueRecord,
)


_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.S)


def _loads_lenient(raw: str):
    """Parse a model's JSON, tolerating the wrappers real models add.

    Fixtures are always bare JSON, so mock mode never exercises this -- but a
    live model routinely answers with ```json fences, or a line of preamble
    before the object, however firmly the prompt says "return ONLY JSON".
    Treating that as malformed would burn the retry and escalate a venue whose
    data was perfectly good.

    Strictly a wrapper-stripper: it never repairs or guesses at the JSON
    itself. Genuinely malformed output still fails, retries, and escalates.
    """
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        pass

    text = (raw or "").strip()
    fenced = _FENCE.search(text)
    if fenced:
        try:
            return json.loads(fenced.group(1))
        except json.JSONDecodeError:
            pass

    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        return json.loads(text[start:end + 1])   # raises on real malformation
    raise json.JSONDecodeError("no JSON object found in response", text or "", 0)


class Agent:
    def __init__(self, criteria: dict, run_dir: str):
        self.criteria = self._validate_criteria(criteria)
        self.run_dir = run_dir
        os.makedirs(run_dir, exist_ok=True)
        self.meter = CostMeter()
        self.steps = 0
        # A runaway guard, never a workload cap. It cannot be derived up front
        # any more: with no per-region venue cap, how much work a run contains
        # is not knowable until search returns. So it starts at the floor and
        # grows by the actual venue count as each region is searched (see
        # _process_region). A genuine loop still trips it; a wide search never
        # does.
        self.max_steps = config.MAX_STEPS
        self.audit_path = os.path.join(run_dir, "audit_trail.jsonl")
        self.checkpoint_path = os.path.join(run_dir, "checkpoint.json")
        self.state = {
            "seen": {},          # url -> decision  (deliberate memory)
            "identity": {},      # identity key -> first occurrence (cross-region dedupe)
            "scored": [],        # list[ScoredVenue dicts]
            "escalations": [],   # items a human must review
            "duplicates": [],    # collapsed repeat listings, kept for the audit
            "pending_regions": list(self.criteria["regions"]),
        }
        self._resume_if_possible()

    # --- Guardrail: input validation --------------------------------------
    @staticmethod
    def _validate_criteria(c: dict) -> dict:
        required = ["regions", "guest_count", "budget_ceiling_usd", "must_haves"]
        missing = [k for k in required if k not in c]
        if missing:
            raise ValueError(f"criteria missing required keys: {missing}")
        if not isinstance(c["regions"], list) or not c["regions"]:
            raise ValueError("criteria.regions must be a non-empty list")
        return c

    # --- Audit trail ------------------------------------------------------
    def _log(self, event: str, **fields):
        rec = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "step": self.steps,
            "event": event,
            **fields,
        }
        with open(self.audit_path, "a") as f:
            f.write(json.dumps(rec) + "\n")

    # --- Checkpointing ----------------------------------------------------
    def _checkpoint(self):
        # The cost meter is checkpointed too. Without it a resumed run reported
        # only what the resumed portion spent, so a run interrupted after an
        # expensive region finished would show a confidently wrong -- and much
        # too small -- number on the dashboard.
        with open(self.checkpoint_path, "w") as f:
            json.dump({
                "state": self.state,
                "steps": self.steps,
                "meter": {
                    "calls": self.meter.calls,
                    "tokens_in": self.meter.tokens_in,
                    "tokens_out": self.meter.tokens_out,
                    "usd": self.meter.usd,
                    "by_model": self.meter.by_model,
                },
            }, f, indent=2)
        self._log("checkpoint_saved")

    def _resume_if_possible(self):
        if os.path.exists(self.checkpoint_path):
            with open(self.checkpoint_path) as f:
                data = json.load(f)
            self.state = data["state"]
            self.steps = data["steps"]
            # Restore spend so far. Absent in checkpoints written before the
            # meter was persisted -- those resume at zero, which under-reports
            # rather than crashing.
            m = data.get("meter") or {}
            self.meter.calls = m.get("calls", 0)
            self.meter.tokens_in = m.get("tokens_in", 0)
            self.meter.tokens_out = m.get("tokens_out", 0)
            self.meter.usd = m.get("usd", 0.0)
            self.meter.by_model = m.get("by_model", {})
            # A checkpoint written by an older build may predate a state key.
            # Resume must tolerate that rather than KeyError halfway through.
            for key, default in (("seen", {}), ("identity", {}), ("scored", []),
                                 ("escalations", []), ("duplicates", []),
                                 ("pending_regions", list(self.criteria["regions"]))):
                self.state.setdefault(key, default)
            # Rebuild the identity index from `seen` so a checkpoint written
            # before dedupe existed does not re-evaluate (and re-append) venues
            # it already scored. Only URL identities can be recovered -- `seen`
            # never stored names -- which degrades to exactly the old behavior.
            for url in self.state["seen"]:
                dedupe.remember(self.state["identity"], "", url, "resumed")
            self._log("resumed_from_checkpoint", steps=self.steps,
                      known_venues=len(self.state["seen"]))

    # --- Guardrail: step cap ----------------------------------------------
    def _tick(self):
        self.steps += 1
        if self.steps > self.max_steps:
            self._log("halted_max_steps", cap=self.max_steps)
            raise RuntimeError(f"MAX_STEPS ({self.max_steps}) exceeded -- halting.")

    # --- Structured output + schema validation with one retry -------------
    def _structured(self, model_cls, prompt_tag, prompt, *, model):
        """Call the model, parse JSON, and (if `model_cls` is a Pydantic model)
        validate against it. On JSON or schema failure, retry once with an error
        hint. If still invalid, raise so the caller can escalate.

        Pass `dict` as `model_cls` to accept any well-formed JSON object.
        """
        last_err = None
        for attempt in range(config.SCHEMA_RETRIES + 1):
            self._tick()
            hint = "" if attempt == 0 else (
                f"\n\nYour previous response failed validation: {last_err}\n"
                "Return ONLY valid JSON matching the schema. Do not invent data; "
                "if a field is not stated on the page, use null or the 'unknown' option."
            )
            raw = complete(prompt + hint, model=model, meter=self.meter, tag=prompt_tag)
            self._log("model_call", tag=prompt_tag, attempt=attempt)
            try:
                data = _loads_lenient(raw)
                if model_cls is dict:
                    return data
                return model_cls(**data)   # Pydantic validation happens here
            except (json.JSONDecodeError, ValidationError, TypeError) as e:
                last_err = str(e)
                self._log("validation_failed", tag=prompt_tag, attempt=attempt, error=last_err[:300])
        raise RuntimeError(f"structured output invalid after retries: {last_err}")

    # --- Main loop --------------------------------------------------------
    def run(self):
        self._log("run_started", criteria=self.criteria, mode=config.MODE,
                  autonomy=config.AUTONOMY, max_steps=self.max_steps)
        while self.state["pending_regions"]:
            region = self.state["pending_regions"][0]
            try:
                self._process_region(region)
            except Exception as e:  # noqa: BLE001
                # Explicit failure path: a region blowing up must not kill the run.
                self._log("region_failed", region=region, error=str(e)[:300])
            self.state["pending_regions"].pop(0)
            self._checkpoint()

        report = self._finalize()
        self._log("run_finished", **report["summary"])
        return report

    def _process_region(self, region: str):
        try:
            results = tools.search_venues(region)
        except tools.ToolError as e:
            # Explicit failure path: search dead -> skip region, record it.
            self._log("search_failed", region=region, error=str(e)[:200])
            return
        # NOTE: the cap is NOT applied here. MAX_VENUES_PER_REGION counts
        # VENUES, not search hits. Applying it to raw results meant roughly
        # three in five slots were spent on directories and planner sites --
        # a 50-slot region yielded 17-22 actual venues. Triage is a Haiku call
        # costing ~$0.0003, so classifying past the cap is nearly free; what
        # was expensive was the venues that never got looked at.
        self.max_steps += len(results) * config.STEPS_PER_VENUE

        # Remembered so the report can say which regions came back empty. A
        # region that returned nothing is a real finding -- in mock mode it means
        # there is no fixture for it, in real mode it means search found nothing
        # -- and either way it must not look like "we searched and it's thin".
        self.state.setdefault("region_stats", {})[region] = len(results)
        self._log("region_search", region=region, n_results=len(results))

        kept = 0
        for r in results:
            if config.MAX_VENUES_PER_REGION and kept >= config.MAX_VENUES_PER_REGION:
                self._log("region_cap_reached", region=region,
                          venues=kept, candidates_left=len(results) - results.index(r))
                break
            url, name = r["url"], r.get("name", "")

            # Deliberate memory + cross-region dedupe. Search returns the same
            # venue under two regions, two domains, and with tracking params;
            # collapsing it here keeps it off the shortlist twice and saves the
            # classify/extract/score calls entirely.
            if url in self.state["seen"]:      # exact URL already handled
                self._log("skip_seen", url=url, prior=self.state["seen"][url])
                continue

            match = dedupe.find_duplicate(self.state["identity"], name, url)
            if match:
                key, first = match
                self.state["seen"][url] = f"duplicate:{first['url']}"
                self.state["duplicates"].append({
                    "url": url, "name": name, "region": region,
                    "duplicate_of": first["url"], "first_seen_region": first["region"],
                    "matched_by": key.split(":", 1)[0],
                })
                self._log("duplicate_skipped", url=url, name=name, region=region,
                          duplicate_of=first["url"], first_seen_region=first["region"],
                          matched_by=key.split(":", 1)[0])
                continue

            dedupe.remember(self.state["identity"], name, url, region)
            if self._process_candidate(region, r):
                kept += 1          # a real venue -- this is what the cap counts

    def _process_candidate(self, region: str, r: dict):
        url, name = r["url"], r["name"]

        # 1) classify (cheap model) -- output filtering guardrail
        try:
            cls = self._structured(dict, "classify", CLASSIFY.format(name=name, url=url, snippet=r.get("snippet", "")), model=config.MODEL_CHEAP)
            classification = cls.get("classification", "irrelevant")
        except Exception as e:  # noqa: BLE001
            self._log("classify_error", url=url, error=str(e)[:200])
            classification = "irrelevant"

        if classification != Classification.VENUE.value:
            self.state["seen"][url] = f"filtered:{classification}"
            self._log("filtered_non_venue", url=url, classification=classification)
            return False        # not a venue: consumes no slot

        # 2) fetch page -- explicit failure path on dead/timeout
        try:
            page = tools.fetch_page(url)
        except tools.ToolError as e:
            self.state["seen"][url] = "fetch_failed"
            self._log("fetch_failed", url=url, error=str(e)[:200])
            self.state["escalations"].append({"url": url, "name": name, "reason": "page unreachable"})
            return True

        # 2b) explicit failure path: a 200 that is not a usable page.
        # Distinct from a dead page, and distinct from a page that is simply
        # silent on our questions -- extracting from an app shell would
        # manufacture a record of all-nulls that reads like the latter.
        problem = tools.page_problem(page)
        if problem:
            self.state["seen"][url] = "partial_page"
            self._log("partial_page", url=url, reason=problem, chars=len(page))
            self.state["escalations"].append(
                {"url": url, "name": name, "reason": f"page did not render usable content: {problem}"}
            )
            return True

        # 3) extract to schema (workhorse) -- validation + retry
        try:
            record: VenueRecord = self._structured(
                VenueRecord, "extract",
                EXTRACT.format(name=name, url=url, region=region, page=page),
                model=config.MODEL_WORKHORSE,
            )
        except Exception as e:  # noqa: BLE001
            self.state["seen"][url] = "extract_failed"
            self._log("extract_failed", url=url, error=str(e)[:200])
            self.state["escalations"].append({"url": url, "name": name, "reason": "extraction failed validation"})
            return True

        # 3b) ground the record against the page it came from.
        # The schema checks the record against itself; this checks it against
        # the source. Anything the page does not support is stripped, and the
        # venue can no longer be silently recommended.
        record, ground_flags = grounding.strip_ungrounded(record, page)
        if ground_flags:
            self._log("grounding_failed", url=url, flags=ground_flags,
                      pricing_signal=record.pricing_signal.value, capacity_max=record.capacity_max)

        # 4) score against criteria
        try:
            sc = self._structured(
                dict, "score",
                SCORE.format(criteria=json.dumps(self.criteria), record=record.model_dump_json()),
                model=config.MODEL_WORKHORSE,
            )
            scored = ScoredVenue(record=record, **sc)
        except Exception as e:  # noqa: BLE001
            self._log("score_error", url=url, error=str(e)[:200])
            self.state["escalations"].append({"url": url, "name": name, "reason": "scoring failed"})
            self.state["seen"][url] = "score_failed"
            return True

        # Guardrail: hard budget disqualifier, independent of model judgment.
        # Uses whichever end of the range the page actually published -- a venue
        # that only prints a ceiling is still over budget.
        if record.pricing_signal == PricingSignal.LISTED:
            listed = record.price_low_usd if record.price_low_usd is not None else record.price_high_usd
            if listed is not None and listed > self.criteria["budget_ceiling_usd"]:
                scored.disqualified = True
                scored.decision = "reject"
                scored.disqualify_reason = (
                    f"listed price {listed} > budget "
                    f"{self.criteria['budget_ceiling_usd']}"
                )

        # Guardrail: deterministic must-have checks + confidence calibration.
        # This can only make the decision more conservative, never less.
        scored = scoring.apply(self.criteria, scored, ground_flags)

        # Guardrail: low confidence must escalate, never auto-recommend.
        if scored.confidence < config.CONFIDENCE_ESCALATION and scored.decision == "recommend":
            scored.decision = "escalate"
            scored.rationale += (
                f"\n  Decision: escalate -- calibrated confidence {scored.confidence:.2f}"
                f" < {config.CONFIDENCE_ESCALATION} threshold"
            )

        self.state["seen"][url] = scored.decision
        self.state["scored"].append(scored.model_dump(mode="json"))
        if scored.decision == "escalate":
            self.state["escalations"].append(
                {"url": url, "name": name, "reason": scoring.escalation_reason(scored)}
            )
        self._log("scored", url=url, decision=scored.decision, score=scored.score,
                  confidence=scored.confidence, model_confidence=scored.model_confidence,
                  must_haves={c.status: sum(1 for x in scored.must_have_checks if x.status == c.status)
                              for c in scored.must_have_checks})

        # 5) draft outreach for recommended venues only (deployment control)
        if scored.decision == "recommend" and config.AUTONOMY == "draft_only":
            self._draft_email(record)
        return True

    def _draft_email(self, record: VenueRecord):
        try:
            data = self._structured(
                dict, "email",
                EMAIL.format(criteria=json.dumps(self.criteria), record=record.model_dump_json()),
                model=config.MODEL_WORKHORSE,
            )
            email = OutreachEmail(**data)
        except Exception as e:  # noqa: BLE001
            self._log("email_error", url=record.url, error=str(e)[:200])
            return
        # HUMAN GATE: write to disk, never send.
        outdir = os.path.join(self.run_dir, "drafts")
        os.makedirs(outdir, exist_ok=True)
        slug = record.url.rsplit("/", 1)[-1]
        with open(os.path.join(outdir, f"{slug}.txt"), "w") as f:
            f.write(f"TO: {email.to or '[find on venue site]'}\n")
            f.write(f"SUBJECT: {email.subject}\n\n{email.body}\n")
        self._log("email_drafted", url=record.url, to=email.to, references=email.references_detail)

    # --- Finalize ---------------------------------------------------------
    def _finalize(self) -> dict:
        recommended = [s for s in self.state["scored"] if s["decision"] == "recommend"]
        recommended.sort(key=lambda s: s["score"], reverse=True)
        report = {
            "recommended": recommended,
            "escalations": self.state["escalations"],
            "duplicates": self.state["duplicates"],
            "all_scored": self.state["scored"],
            "summary": {
                "mode": config.MODE,
                "region_results": self.state.get("region_stats", {}),
                "empty_regions": sorted(
                    r for r, n in self.state.get("region_stats", {}).items() if n == 0
                ),
                "regions": len(self.criteria["regions"]),
                "venues_evaluated": len(self.state["scored"]),
                "recommended": len(recommended),
                "escalated": len(self.state["escalations"]),
                "duplicates_collapsed": len(self.state["duplicates"]),
                "steps": self.steps,
                "cost": self.meter.summary(),
                "cost_per_venue_usd": round(
                    self.meter.usd / max(1, len(self.state["scored"])), 4
                ),
            },
        }
        with open(os.path.join(self.run_dir, "report.json"), "w") as f:
            json.dump(report, f, indent=2)
        return report
