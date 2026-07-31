"""
Structured outputs + schema validation.

FDE framework points covered here:
  - Structured outputs: every model response is parsed into one of these models,
    never consumed as free-form text.
  - Schema validation: callers validate against these models and retry/escalate
    on failure (see agent.py).

The single most important design decision in this file is on PricingSignal:
destination wedding venues almost never publish prices. The naive version of
this agent invented dollar figures. We make "not listed" a first-class,
required-to-justify state so the model is allowed -- and forced -- to say
"I don't know" instead of hallucinating. See evals/golden_dataset.json for the
labeled case that guards this behavior.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, field_validator, model_validator


class PricingSignal(str, Enum):
    LISTED = "listed"              # an actual number/range was published on the page
    REQUEST_ONLY = "request_only"  # "contact us for a quote" -- the common case
    UNKNOWN = "unknown"            # page did not address pricing at all


class ContactMethod(str, Enum):
    EMAIL = "email"
    FORM = "form"
    PHONE = "phone"
    NONE = "none"


class Classification(str, Enum):
    """Is this search result an actual bookable venue, or noise?"""
    VENUE = "venue"                # a real, bookable venue page
    LISTICLE = "listicle"          # "17 best Tulum wedding venues" -- not a venue
    VENDOR = "vendor"              # planner/photographer/caterer, not a venue
    IRRELEVANT = "irrelevant"      # unrelated page


class VenueRecord(BaseModel):
    """The normalized record we extract for every candidate. One schema, always."""
    name: str
    url: str
    region: str
    classification: Classification

    capacity_max: Optional[int] = Field(
        default=None, description="Max guest capacity if stated; None if not stated."
    )
    has_onsite_lodging: Optional[bool] = None
    has_indoor_backup: Optional[bool] = None
    setting: Optional[str] = Field(
        default=None, description="e.g. beachfront, jungle, cliffside, vineyard"
    )

    pricing_signal: PricingSignal = PricingSignal.UNKNOWN
    price_low_usd: Optional[int] = None
    price_high_usd: Optional[int] = None
    price_source_quote: Optional[str] = Field(
        default=None,
        description="VERBATIM snippet from the page that justifies any price. "
        "Required whenever pricing_signal == LISTED.",
    )

    contact_method: ContactMethod = ContactMethod.NONE
    contact_value: Optional[str] = None
    standout_detail: Optional[str] = Field(
        default=None,
        description="One specific, real detail from the page to personalize outreach.",
    )

    @model_validator(mode="after")
    def pricing_is_internally_consistent(self):
        """Guardrail: the anti-hallucination check, enforced at the schema layer.

        This is a MODEL validator, not a field validator, deliberately. A field
        validator on `price_source_quote` only runs when that key is present in
        the payload -- a model that simply omits the key walked a priced record
        straight through the guard. A model validator always runs.

        Three ways a pricing record can be dishonest, all rejected here:
          1. a price with no verbatim source quote  (the invented number)
          2. pricing_signal=listed with no number   (claims a price it can't show)
          3. a price under request_only/unknown     (a number it just said wasn't published)
        """
        priced = self.price_low_usd is not None or self.price_high_usd is not None

        if priced and not (self.price_source_quote or "").strip():
            raise ValueError(
                "A price was provided without price_source_quote. "
                "Do not invent prices -- set pricing_signal=request_only if the "
                "page does not publish numbers."
            )
        if self.pricing_signal is PricingSignal.LISTED and not priced:
            raise ValueError(
                "pricing_signal='listed' requires price_low_usd (and price_high_usd "
                "for a range). If the page publishes no number, use 'request_only' "
                "or 'unknown'."
            )
        if priced and self.pricing_signal is not PricingSignal.LISTED:
            raise ValueError(
                f"pricing_signal={self.pricing_signal.value!r} cannot carry a price. "
                "A number means the page published it -- use 'listed' with a "
                "verbatim price_source_quote, or drop the number."
            )
        if self.price_low_usd is not None and self.price_high_usd is not None:
            if self.price_high_usd < self.price_low_usd:
                raise ValueError("price_high_usd must be >= price_low_usd")
        return self


class MustHaveCheck(BaseModel):
    """One must-have, checked deterministically in code against the record.

    `status` is three-valued on purpose. "unknown" is not a soft "unmet" -- it
    is the state that must escalate to a human, because the page never said.
    """
    must_have: str
    status: str = Field(description="one of: met | unmet | unknown")
    evidence: str

    @field_validator("status")
    @classmethod
    def status_is_valid(cls, v):
        allowed = {"met", "unmet", "unknown"}
        if v not in allowed:
            raise ValueError(f"status must be one of {allowed}, got {v!r}")
        return v


class ScoredVenue(BaseModel):
    """A VenueRecord after scoring against the couple's criteria."""
    record: VenueRecord
    score: float = Field(ge=0.0, le=1.0)
    confidence: float = Field(ge=0.0, le=1.0)
    disqualified: bool = False
    disqualify_reason: Optional[str] = None
    rationale: str

    # --- deterministic layer, filled in by scoring.py after the model answers.
    # The model proposes; these fields are what the agent actually acted on.
    must_have_checks: list[MustHaveCheck] = Field(default_factory=list)
    grounding_flags: list[str] = Field(
        default_factory=list,
        description="Claims the page did not support, stripped before scoring.",
    )
    model_confidence: Optional[float] = Field(
        default=None, description="Confidence the model reported, before calibration."
    )
    # Decision the agent reached for this venue. "escalate" means: do not
    # auto-recommend; a human should look. This is how non-determinism becomes
    # an auditable, safe decision rather than a silent guess.
    decision: str = Field(description="one of: recommend | reject | escalate")

    @field_validator("decision")
    @classmethod
    def decision_is_valid(cls, v):
        allowed = {"recommend", "reject", "escalate"}
        if v not in allowed:
            raise ValueError(f"decision must be one of {allowed}, got {v!r}")
        return v


class OutreachEmail(BaseModel):
    """A drafted inquiry email. NEVER sent by the agent -- written to disk for
    human review. This is the deployment control / human-in-the-loop gate."""
    venue_name: str
    to: Optional[str] = None
    subject: str
    body: str
    references_detail: str = Field(
        description="The specific venue detail this email references, "
        "to prove it is personalized and not a mail merge."
    )
