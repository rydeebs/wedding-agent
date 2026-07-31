"""
All prompts live here so the "prompt evolution" story is legible in one file.

The headline lesson is baked into EXTRACT. The naive v1 was literally:
    "What is the price of this venue?"
which made the model invent numbers for venues that only say "contact us for a
quote." v2 forbids invention, makes "request_only" a valid answer, and requires
a verbatim source quote for any number. The schema (schemas.py) then enforces it.
"""

CLASSIFY = """You are triaging a web search result for a wedding-venue search.
Decide if this result is an actual bookable VENUE, or something else.

Name: {name}
URL: {url}
Snippet: {snippet}

Return ONLY JSON: {{"classification": "venue" | "listicle" | "vendor" | "irrelevant"}}
- "listicle": a roundup like "17 best venues", not a single venue
- "vendor": a planner, photographer, or caterer, not a venue
- "irrelevant": unrelated page
"""

# NAIVE v1 (kept as a comment to show evolution in the video):
#   EXTRACT_V1 = "Read this page and tell me the venue's capacity and price: {page}"
#   -> hallucinated prices for request-only venues. Replaced by v2 below.
#
# v2 -> v3: v2 stopped the model inventing numbers out of nothing, but not the
# subtler error -- taking a number that IS on the page and calling it something
# it isn't. A venue page is full of numbers that are not wedding numbers: room
# counts, founding years, acreage, square footage, nightly rates. v3 names those
# traps explicitly and demands the quote be copy-pasted rather than paraphrased,
# because grounding.py checks the quote against the page character by character
# and a paraphrase fails that check just as an invention does.

EXTRACT = """Extract structured facts about this wedding venue from the page text.

Venue name: {name}
URL: {url}
Region: {region}

PAGE TEXT:
{page}

Rules:
- Only use facts stated in the page text. Do NOT infer or invent.
- If capacity/lodging/backup are not stated, use null.

- Venue pages almost never use the couple's wording. Read for the FACT, not the
  phrase, and set the field when the page states the fact in any words:
    * capacity_max — the largest guest number the page gives for an event:
      "seats 140", "up to 100 guests", "accommodates 120 for a reception",
      "our terrace holds 90". Take the largest event capacity, not a room count.
    * has_onsite_lodging — true if guests can sleep on the property: "12 suites",
      "eighteen rooms", "nine cottages", "sleeps 30", "on-site accommodation",
      "our hotel", "casitas", "guest apartments". It does NOT have to say
      "on-site lodging".
    * has_indoor_backup — true if the page names an indoor or covered space the
      event can move into: "indoor hall", "sala", "ballroom", "orangery",
      "restored barn", "vaulted cellar", "covered terrace", "marquee", "tented
      pavilion", or any "in case of rain / if the weather turns" arrangement.
  Set false only if the page says the thing is absent. Leave null only if the
  page genuinely never addresses it — null escalates the venue to a human, so a
  fact you skipped past becomes a venue nobody looks at.
- Pricing: set "pricing_signal" to "listed" ONLY if a number or range is printed
  on the page. If the page says to contact for a quote, use "request_only". If
  pricing is not mentioned at all, use "unknown".
- If (and only if) pricing_signal is "listed", set price_low_usd (and price_high_usd
  if a range) AND set price_source_quote to the VERBATIM sentence from the page that
  states the price. Never provide a price without its source quote.
- COPY the quote character for character. Do not tidy, shorten, or paraphrase it.
  It is checked against the page automatically; a paraphrase fails that check.

- A number on the page is not automatically the number being asked for. Pages
  are full of numbers that are NOT a wedding price and NOT a guest capacity:
    * room / casita / suite counts      ("48 casitas")        -> not capacity
    * founding or renovation years      ("founded in 1918")   -> not capacity
    * acreage or square footage         ("3,500 square feet") -> not a price
    * nightly room rates                ("$450 per night")    -> not a venue price
    * per-person or per-item rates, deposits, and taxes       -> not a venue price
  price_low_usd is the cost of holding the WEDDING. capacity_max is how many
  GUESTS the venue seats. If the page gives you one of the other numbers and not
  the one asked for, that field is null and the signal is request_only/unknown.
  Reporting the wrong number is worse than reporting nothing: nothing escalates
  to a human, a wrong number gets acted on.

- standout_detail: one specific, real detail useful for a personalized inquiry email.

Return ONLY JSON matching this shape:
{{"name": str, "url": str, "region": str, "classification": "venue",
 "capacity_max": int|null, "has_onsite_lodging": bool|null, "has_indoor_backup": bool|null,
 "setting": str|null, "pricing_signal": "listed"|"request_only"|"unknown",
 "price_low_usd": int|null, "price_high_usd": int|null, "price_source_quote": str|null,
 "contact_method": "email"|"form"|"phone"|"none", "contact_value": str|null,
 "standout_detail": str|null}}
"""

SCORE = """Score this venue against the couple's criteria.

CRITERIA:
{criteria}

VENUE RECORD:
{record}

Scoring rules:
- Every must_have that the record fails to satisfy is a serious problem.
- Distinguish "the page said no" from "the page never said". A stated fact that
  fails a must-have is a reject. A must-have the page is silent on is an
  escalate -- never score a null field as if it were a pass.
- If capacity is null or below guest_count, you cannot confirm the venue fits ->
  lower confidence and prefer "escalate" over "recommend".
- If a listed price exceeds budget_ceiling_usd, set disqualified=true, decision="reject".
- decision must be: "recommend" (clear fit), "reject" (clear fail/over budget),
  or "escalate" (missing info or genuine ambiguity a human should resolve).
- confidence is how sure you are about THE MUST-HAVE VERDICT — nothing else.
  Ask only: "how certain am I that this venue does or does not meet the couple's
  must-haves?" If the page states the capacity, the lodging, and the weather
  backup plainly, that is a high-confidence verdict (0.85+) even if the record
  is otherwise sparse.
  Blank fields that have no bearing on a must-have — price, setting, standout
  detail — must NOT lower it. Destination venues almost never publish prices;
  treating that as uncertainty would mark nearly every real venue uncertain.
  Lower confidence only when the evidence for a MUST-HAVE is thin, ambiguous, or
  second-hand.

Your answer is not the final word. The must-haves are re-checked in code
against the record, confidence is capped by how much the page actually
evidenced, and either can turn a "recommend" into an "escalate". Answer
honestly rather than agreeably -- an inflated confidence is simply overwritten,
and the rationale you write is shown to the couple next to the check results.

Return ONLY JSON:
{{"score": float 0-1, "confidence": float 0-1, "disqualified": bool,
 "disqualify_reason": str|null, "rationale": str, "decision": "recommend"|"reject"|"escalate"}}
"""

EMAIL = """Draft a short, warm inquiry email to this wedding venue.

CRITERIA (use guest count and date window):
{criteria}

VENUE RECORD:
{record}

Rules:
- Reference ONE specific real detail from standout_detail so it is clearly personalized.
- Ask for: availability in the date window, and an all-in quote for the guest count.
- If a must-have (like indoor backup) is confirmed, you may confirm it; do not assert
  facts not in the record.
- Keep it under 120 words. Sign as "Ryan". Plain, human tone. No corporate filler.

Return ONLY JSON:
{{"venue_name": str, "to": str|null, "subject": str, "body": str, "references_detail": str}}
"""
