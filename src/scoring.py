"""
The deterministic half of scoring.

The model proposes a score, a confidence, and a decision. This module is what
the agent actually acts on. Everything here is code, not judgment, because
these are the parts a wrong answer is expensive on:

  - MUST-HAVES are checked against the extracted record, field by field. A
    must-have is met, unmet, or UNKNOWN -- and "unknown" is not a weak "unmet".
    Unmet means the page said no (reject). Unknown means the page never said,
    which is precisely the case a human has to resolve (escalate).

  - CONFIDENCE is a ceiling, not a suggestion. The model's own number is only
    ever lowered, never raised: an extractor with three blank fields does not
    get to be 0.95 sure. Since <0.80 escalates, calibration is what makes the
    escalation threshold mean something.

  - The RATIONALE carries the checks. Anyone reading the shortlist can see
    which must-have was confirmed by which field, and why the confidence moved.
    A rationale that only says "great fit!" is not auditable.

A must-have this module cannot map to a field is reported as unknown, not
quietly passed -- so adding a new must-have to criteria.json escalates until
someone teaches the extractor to check it. Failing loudly is the point.
"""

from __future__ import annotations

import re

from . import grounding
from .schemas import MustHaveCheck, PricingSignal, ScoredVenue, VenueRecord

# --- must-have matching ---------------------------------------------------
# Ordered most-specific first: "on-site lodging for guests" contains "guests",
# but it is a lodging requirement, not a capacity one.
_LODGING = re.compile(r"lodging|accommodat|on-?site room|sleep|stay on|villa|casita|suite")
_BACKUP = re.compile(r"indoor|backup|back-up|rain|weather|covered|tent")
_CAPACITY = re.compile(r"capacit|guest|people|attendee|at least \d+|fits?\b|seat")

_AT_LEAST = re.compile(r"(?:at least|minimum of|up to|for)\s+(\d[\d,]*)")


def _capacity_needed(criteria: dict, must_have_text: str):
    """How many guests this must-have demands: its own number if it names one,
    otherwise the criteria's guest count."""
    m = _AT_LEAST.search(must_have_text)
    if m:
        return int(m.group(1).replace(",", ""))
    return criteria.get("guest_count")


def _tri(value, met_msg: str, unmet_msg: str, unknown_msg: str):
    if value is True:
        return "met", met_msg
    if value is False:
        return "unmet", unmet_msg
    return "unknown", unknown_msg


def check_must_haves(criteria: dict, record: VenueRecord) -> list[MustHaveCheck]:
    """Check every must-have in the criteria against the extracted record."""
    guest_count = criteria.get("guest_count")
    checks: list[MustHaveCheck] = []

    for mh in criteria.get("must_haves", []):
        text = mh.lower()

        if _LODGING.search(text):
            status, evidence = _tri(
                record.has_onsite_lodging,
                "page states on-site lodging",
                "page states there is no on-site lodging",
                "page never addresses on-site lodging",
            )

        elif _BACKUP.search(text):
            status, evidence = _tri(
                record.has_indoor_backup,
                "page states an indoor/covered backup space",
                "page states there is no indoor backup",
                "page never addresses an indoor weather backup",
            )

        elif _CAPACITY.search(text):
            needed = _capacity_needed(criteria, text)
            if needed is None:
                status, evidence = "unknown", "criteria state no guest count to check against"
            elif record.capacity_max is None:
                status, evidence = "unknown", f"page states no capacity; need {needed}"
            elif record.capacity_max >= needed:
                status, evidence = "met", f"stated capacity {record.capacity_max} >= {needed}"
            else:
                status, evidence = "unmet", f"stated capacity {record.capacity_max} < {needed}"

        else:
            # No field can speak to this must-have. Say so; do not pass it.
            status, evidence = "unknown", "no extracted field can confirm this -- needs a human"

        checks.append(MustHaveCheck(must_have=mh, status=status, evidence=evidence))

    return checks


# --- confidence calibration -----------------------------------------------

UNKNOWN_MUST_HAVE_PENALTY = 0.15   # per must-have the page never addressed
UNKNOWN_PRICING_PENALTY = 0.10     # page never addressed pricing at all
GROUNDING_PENALTY = 0.25           # we caught the extractor claiming what the page didn't say
UNUSABLE_PRICE_PENALTY = 0.10      # a real published price we cannot use (wrong currency)

# Certainty about a capacity verdict is not binary. "Seats 120" for 105 guests
# passes the check, but only just, and several ordinary things would flip it: a
# seated-dinner number quoted where a ceremony number was meant, the capacity of
# the largest of several spaces rather than the one being booked, or a page
# rounding. "Seats 500" leaves no room for that doubt.
#
# Without this term the confidence of a RECOMMENDED venue carries no information
# at all. Every other penalty here keys on something a recommendation cannot
# have -- an unmet must-have, an unconfirmed one, an ungrounded claim -- so the
# ceiling is 1.0 for every venue that reaches the shortlist, min() never binds,
# and the number shown is whatever the model self-reported. Models quantize that
# to 0.9, so an entire shortlist reads 0.90 regardless of how tight the fit is.
#
# (ratio_below, penalty), narrowest first. Capped at 0.10 deliberately: headroom
# alone must not push a venue that met every must-have from 0.90 under the 0.80
# escalation threshold on its own.
CAPACITY_MARGIN_PENALTIES = ((1.10, 0.10), (1.25, 0.06), (1.50, 0.03))


def capacity_margin_penalty(criteria: dict, checks: list[MustHaveCheck],
                            record: VenueRecord) -> tuple[float, str | None]:
    """Penalty for a capacity that clears the requirement only narrowly."""
    if record.capacity_max is None:
        return 0.0, None

    needed = None
    for c in checks:
        if c.status == "met" and _CAPACITY.search(c.must_have.lower()):
            n = _capacity_needed(criteria, c.must_have.lower())
            if n:
                needed = n if needed is None else max(needed, n)
    if not needed:
        return 0.0, None

    ratio = record.capacity_max / needed
    for threshold, penalty in CAPACITY_MARGIN_PENALTIES:
        if ratio < threshold:
            return penalty, (f"stated capacity {record.capacity_max} clears {needed} "
                             f"by only {ratio:.2f}x")
    return 0.0, None


def confidence_ceiling(criteria: dict, checks: list[MustHaveCheck], record: VenueRecord,
                       grounding_flags: list[str]) -> tuple[float, list[str]]:
    """How sure the EVIDENCE allows us to be, regardless of how sure the model felt."""
    ceiling, why = 1.0, []

    margin_penalty, margin_why = capacity_margin_penalty(criteria, checks, record)
    if margin_penalty:
        ceiling -= margin_penalty
        why.append(margin_why)

    n_unknown = sum(1 for c in checks if c.status == "unknown")
    if n_unknown:
        ceiling -= UNKNOWN_MUST_HAVE_PENALTY * n_unknown
        why.append(f"{n_unknown} must-have(s) unconfirmed by the page")
    if record.pricing_signal is PricingSignal.UNKNOWN:
        ceiling -= UNKNOWN_PRICING_PENALTY
        why.append("pricing not addressed on the page")
    if grounding.integrity_flags(grounding_flags):
        ceiling -= GROUNDING_PENALTY
        why.append("extracted claims were not supported by the page")
    elif grounding_flags:
        # Only the currency flag. The extractor was right; we just cannot store
        # or compare the number, so the venue's price is effectively unknown.
        ceiling -= UNUSABLE_PRICE_PENALTY
        why.append("published price is in a currency we cannot compare to the budget")

    return max(0.0, min(1.0, ceiling)), why


# --- applying it ----------------------------------------------------------

_MARK = {"met": "[+]", "unmet": "[-]", "unknown": "[?]"}


def apply(criteria: dict, scored: ScoredVenue, grounding_flags: list[str]) -> ScoredVenue:
    """Overwrite the model's decision/confidence with what the evidence supports,
    and write the reasoning into the rationale. Mutates and returns `scored`.

    Precedence, strongest first:
      1. an unmet must-have          -> reject   (the page said no)
      2. an unknown must-have, an
         ungrounded claim, or low
         calibrated confidence       -> escalate (the page didn't say)
      3. otherwise                   -> the model's decision stands
    """
    record = scored.record
    checks = check_must_haves(criteria, record)
    scored.must_have_checks = checks
    scored.grounding_flags = grounding_flags

    ceiling, ceiling_why = confidence_ceiling(criteria, checks, record, grounding_flags)
    if scored.model_confidence is None:
        scored.model_confidence = scored.confidence
    scored.confidence = min(scored.model_confidence, ceiling)

    unmet = [c for c in checks if c.status == "unmet"]
    unknown = [c for c in checks if c.status == "unknown"]

    decision_reason = None
    if unmet:
        scored.decision = "reject"
        scored.disqualified = True
        reason = "unmet must-have: " + "; ".join(f"{c.must_have} ({c.evidence})" for c in unmet)
        scored.disqualify_reason = scored.disqualify_reason or reason
        decision_reason = f"reject -- {reason}"
    elif scored.decision == "recommend":
        if unknown:
            scored.decision = "escalate"
            decision_reason = ("escalate -- cannot confirm: "
                               + "; ".join(c.must_have for c in unknown))
        elif grounding.integrity_flags(grounding_flags):
            # Only INTEGRITY failures block a recommendation. A price we cannot
            # convert is a note for the human, not evidence the record is wrong:
            # the price is already stripped to request_only, which is exactly how
            # every "contact us for a quote" venue is treated, and those are
            # recommended every day.
            scored.decision = "escalate"
            decision_reason = "escalate -- extracted claims were not supported by the page"

    lines = [scored.rationale.strip(), "", "Checks (deterministic):"]
    lines += [f"  {_MARK[c.status]} {c.must_have} -- {c.evidence}" for c in checks]
    lines += [f"  [!] ungrounded: {f}" for f in grounding_flags]
    conf_line = f"  Confidence {scored.model_confidence:.2f} (model)"
    if ceiling < scored.model_confidence:
        conf_line += f" -> {scored.confidence:.2f}, capped at {ceiling:.2f}: " + "; ".join(ceiling_why)
    elif ceiling_why:
        conf_line += f"; evidence ceiling {ceiling:.2f} ({'; '.join(ceiling_why)})"
    lines.append(conf_line)
    if decision_reason:
        lines.append(f"  Decision: {decision_reason}")
    scored.rationale = "\n".join(lines)

    return scored


def escalation_reason(scored: ScoredVenue) -> str:
    """A one-line 'why a human is being asked', for the escalation list."""
    unknown = [c.must_have for c in scored.must_have_checks if c.status == "unknown"]
    bits = []
    if unknown:
        bits.append("cannot confirm: " + "; ".join(unknown))
    if scored.grounding_flags:
        bits.append("unsupported claims: " + "; ".join(scored.grounding_flags))
    if not bits:
        bits.append(scored.rationale.split("\n")[0])
    return f"{'; '.join(bits)} (confidence {scored.confidence:.2f})"
