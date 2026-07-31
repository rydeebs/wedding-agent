"""
Grounding checks: does the extracted record survive contact with the page?

The schema (schemas.py) can only check a record against ITSELF -- it has no
access to the page text, so it cannot tell a real quote from a fluent
fabrication. This module closes that gap. It is the second half of the
anti-hallucination guard, and it is deliberately deterministic: no model call,
no judgment, just "show me where on the page this came from."

Two failure shapes it catches, both seen in real extractions:

  1. FABRICATED EVIDENCE -- the model reports a price and invents a
     plausible-sounding quote that is nowhere on the page.
  2. MISATTRIBUTED EVIDENCE -- the harder one. The number IS on the page and
     the quote IS verbatim, but the number is not the thing the model says it
     is: a room count read as a guest capacity, a founding year read as a
     capacity, a square footage or a nightly room rate read as a wedding price.
     A page full of numbers is the natural habitat of this error.

Ungrounded fields are STRIPPED, not corrected -- we drop back to
"unknown"/"request_only", which are correct answers here -- and the venue is
forced to escalate, because we no longer know what we thought we knew.
"""

from __future__ import annotations

import re

from .schemas import PricingSignal, VenueRecord

# --- text normalization ---------------------------------------------------

def _norm(text: str) -> str:
    """Lowercase + collapse whitespace, so a quote survives the reflow that
    happens between page HTML and model output. Punctuation is preserved --
    a 'verbatim' quote should still be verbatim."""
    return re.sub(r"\s+", " ", (text or "")).strip().lower()


def _number_forms(n: int) -> list[str]:
    """The ways an integer can legitimately appear in prose: 32000, 32,000."""
    return [str(n), f"{n:,}"]


# --- signals we look for in a quote ---------------------------------------

# The quote must actually be talking about money.
_PRICE_MARKER = re.compile(r"\$|\busd\b|\bmxn\b|\bpric|\bcost|\bfee\b|\bminimum\b|\brate")

# ...and about a WEDDING, not something else the venue sells.
_EVENT_MARKER = re.compile(
    r"\bwedding|\bevent|\bceremon|\breception|\bpackage|\bcollection|\bbuyout|\belopement"
)

# ...and NOT about a per-night stay. A nightly room rate is a real published
# price that is not a venue price; recording it as one is how a $450 page
# becomes a "$450 wedding".
_LODGING_RATE = re.compile(r"per night|per-night|nightly|per room|room rate|per guest room")

# ...and in a currency we can actually compare to a USD budget ceiling.
# The schema field is price_low_USD. A venue in Tuscany quoting "28,000 EUR" or
# one in Tulum quoting "45,000 MXN" states a real price, but writing that number
# into a USD field misstates it -- 45,000 MXN is about $2,400, not $45,000, and
# the budget disqualifier would compare it to a USD ceiling and reach a
# confident wrong answer. There is no currency field to hold this honestly, so
# a non-USD price is treated as no USD price: it falls back to request_only and
# escalates for a human to convert. Same rule as everywhere else here -- report
# nothing rather than report it wrong.
_NON_USD = re.compile(
    r"\beur\b|€|\bgbp\b|£|\bmxn\b|\bchf\b|\bthb\b|฿|\bmad\b|\bcrc\b|\bjpy\b|¥|"
    r"\beuros?\b|\bpesos?\b|\bpounds?\b|\bbaht\b|\bdirhams?\b|\bcolones\b"
)

# A number is a guest capacity only if it sits next to a guest word.
_GUEST_WORD = re.compile(
    r"guest|people|person|seat|capacit|accommodat|attendee|host(s|ed|ing)?\b|party of"
)
_CAPACITY_WINDOW = 60   # chars either side of the number

# Does the page invite you to ask for a price? Decides what a stripped price
# falls back to: "request_only" (they have one, they won't print it) vs
# "unknown" (the page never addressed pricing at all).
_REQUEST_MARKER = re.compile(
    r"contact (us|our|the)|upon request|on request|by request|request a quote|"
    r"custom quote|for a quote|inquir|get in touch"
)


def _quote_is_on_page(quote: str, page: str) -> bool:
    return _norm(quote) in _norm(page)


def _number_in(n: int, text: str) -> bool:
    t = _norm(text)
    return any(form in t for form in _number_forms(n))


# --- the checks -----------------------------------------------------------

def check_price(record: VenueRecord, page: str) -> list[str]:
    """Return a list of reasons the record's price is not grounded in the page."""
    if record.pricing_signal is not PricingSignal.LISTED:
        return []   # nothing claimed, nothing to ground

    problems = []
    quote = record.price_source_quote or ""
    price = record.price_low_usd if record.price_low_usd is not None else record.price_high_usd

    if not _quote_is_on_page(quote, page):
        problems.append("price_source_quote does not appear on the page (fabricated evidence)")
        return problems   # a quote that isn't on the page can't corroborate anything else

    if price is not None and not _number_in(price, quote):
        problems.append(f"price {price} does not appear in its own source quote")
    # A foreign-currency quote DOES state money -- it just states it in a
    # currency we cannot record. Counting it here keeps the reason accurate
    # ("convert this") instead of the misleading "states no money".
    if not (_PRICE_MARKER.search(_norm(quote)) or _NON_USD.search(_norm(quote))):
        problems.append("price_source_quote states no money (no currency, price, or fee wording)")
    if not _EVENT_MARKER.search(_norm(quote)):
        problems.append("price_source_quote is not about a wedding or event")
    if _LODGING_RATE.search(_norm(quote)):
        problems.append("price_source_quote is a per-night lodging rate, not a venue price")
    if _NON_USD.search(_norm(quote)):
        problems.append(NON_USD_FLAG)

    return problems


# Not every grounding problem is the same kind of problem.
#
# A fabricated quote, a misattributed number, a nightly rate sold as a wedding
# price -- those say the extractor asserted something the page does not support,
# and the rest of its record is suspect. A price in EUR says the opposite: the
# extractor read the page correctly and we have nowhere to put the answer,
# because the schema field is price_low_USD. Blaming the extractor for our own
# missing currency field made every European venue with a published price
# escalate, which on a European search is most of them.
NON_USD_FLAG = (
    "price is quoted in a non-USD currency; it cannot be recorded in a USD "
    "field or compared to a USD budget without conversion"
)


def integrity_flags(flags: list[str]) -> list[str]:
    """The flags that impugn the extractor, as opposed to our schema."""
    return [f for f in flags if f != NON_USD_FLAG]


def check_capacity(record: VenueRecord, page: str) -> list[str]:
    """Return reasons the record's capacity is not grounded in the page.

    Requiring the number to sit near a guest word is what separates
    'up to 120 guests' from '48 casitas', 'founded in 1918', and
    '3,500 square feet' -- all of which are numbers a page happily offers.
    """
    cap = record.capacity_max
    if cap is None:
        return []

    if cap <= 0:
        return [f"capacity {cap} is not a possible guest count"]

    page_n = _norm(page)
    hits = [
        m for form in _number_forms(cap)
        for m in re.finditer(rf"(?<!\d){re.escape(form)}(?!\d)", page_n)
    ]
    if not hits:
        return [f"capacity {cap} does not appear anywhere on the page"]

    for m in hits:
        window = page_n[max(0, m.start() - _CAPACITY_WINDOW): m.end() + _CAPACITY_WINDOW]
        if _GUEST_WORD.search(window):
            return []   # grounded
    return [f"capacity {cap} appears on the page, but not as a guest count"]


def check(record: VenueRecord, page: str) -> list[str]:
    """All grounding problems with this record, as human-readable strings."""
    return check_price(record, page) + check_capacity(record, page)


# --- stripping ------------------------------------------------------------

def strip_ungrounded(record: VenueRecord, page: str) -> tuple[VenueRecord, list[str]]:
    """Remove every field the page does not support, and say what was removed.

    Returns (clean_record, flags). Callers must treat a non-empty `flags` as a
    reason to escalate: we did not just lose a field, we caught the extractor
    asserting something the page never said.
    """
    price_problems = check_price(record, page)
    capacity_problems = check_capacity(record, page)
    if not price_problems and not capacity_problems:
        return record, []

    data = record.model_dump()

    if price_problems:
        data["price_low_usd"] = None
        data["price_high_usd"] = None
        data["price_source_quote"] = None
        data["pricing_signal"] = (
            PricingSignal.REQUEST_ONLY if _REQUEST_MARKER.search(_norm(page))
            else PricingSignal.UNKNOWN
        )
    if capacity_problems:
        data["capacity_max"] = None

    return VenueRecord(**data), price_problems + capacity_problems
