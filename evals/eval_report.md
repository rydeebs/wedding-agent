# Evaluation Report — Wedding Venue Agent

**Pass rate: 39/39 (100%)**

## Failure categories

- none


## Operating rules

- Below 0.80 confidence, the agent escalates instead of recommending.
- Listed price over budget is a hard disqualifier (deterministic, not model-judged).
- A price without a verbatim source quote is rejected at the schema layer.
- A quote that is not on the page, states no money, or is not about a wedding is stripped, and the venue escalates (`grounding.py`).
- A number is only a capacity if it appears on the page next to a guest word; room counts, years, and square footage are not capacities.
- Must-haves are re-checked in code against the record. Unmet -> reject; unconfirmed -> escalate. A null field never counts as a pass.
- Model confidence is a ceiling, not a floor: it is lowered by unconfirmed must-haves, unaddressed pricing, and ungrounded claims, and never raised.
- Missing must-have information escalates to a human; it is never guessed.

## Failure taxonomy

| Failure | Detected by | Agent behavior |
|---|---|---|
| Search returns nothing / errors | `tools.search_venues` + backoff | region skipped and logged, run continues |
| Page unreachable | `tools.fetch_page` + backoff | escalate: "page unreachable" |
| Page is a 200 but unusable (app shell, error page, empty) | `tools.page_problem` | escalate before extraction; no workhorse call spent |
| Non-venue result (listicle, vendor) | classify step | filtered, never extracted |
| Duplicate venue across regions/domains | `dedupe.find_duplicate` | collapsed before classify, first occurrence kept |
| Malformed model output | Pydantic + one retry | escalate if still invalid |
| Self-contradictory pricing record | `VenueRecord` model validator | rejected, retried, then escalated |
| Fabricated or misattributed evidence | `grounding.py` | claim stripped, venue escalated |
| Unmet must-have | `scoring.check_must_haves` | deterministic reject |
| Unconfirmed must-have / low calibrated confidence | `scoring.apply` | escalate |
| Listed price over budget | `agent` budget disqualifier | deterministic reject |

## Cases — venue decisions

| Case | Category | Expected (decision/pricing) | Actual | Result |
|---|---|---|---|---|
| casa-jaguar | normal | recommend/request_only | recommend/request_only | PASS |
| hotel-esencia | normal-listed-price | recommend/listed | recommend/listed | PASS |
| listicle-17best | high-risk-noise | filtered/None | filtered/None | PASS |
| villa-lodging-unknown | incomplete-information | escalate/unknown | escalate/unknown | PASS |
| cabo-cliff-estate | normal | recommend/request_only | recommend/request_only | PASS |
| over-budget-resort | high-risk-budget | reject/listed | reject/listed | PASS |
| no-capacity-hacienda | incomplete-information | escalate/unknown | escalate/unknown | PASS |
| sedona-sky-ranch | normal | recommend/request_only | recommend/request_only | PASS |
| casa-numeros | high-risk-hallucination | escalate/unknown | escalate/unknown | PASS |
| finca-las-palmas | high-risk-hallucination | escalate/request_only | escalate/request_only | PASS |
| mirador-del-mar | high-risk-hallucination | escalate/request_only | escalate/request_only | PASS |
| ghost-villa | failure-malformed-page | escalate/None | escalate/None | PASS |
| weddings | failure-duplicate-venue | duplicate/None | duplicate/None | PASS |
| villa-cielo | normal-country-region | recommend/request_only | recommend/request_only | PASS |
| borgo-antico | normal-foreign-currency | recommend/request_only | recommend/request_only | PASS |
| quinta-do-mar | normal-country-region | recommend/request_only | recommend/request_only | PASS |
| villa-stretta | confidence calibration | recommend/request_only | recommend/request_only | PASS |

## Cases — criteria intake

Criteria are the agent's input, and the dashboard can now write them. These grade the validator that stands between the two.

| Case | Category | Expected | Actual | Result |
|---|---|---|---|---|
| current-criteria | normal | accept | accept | PASS |
| edited-values | normal | accept | accept | PASS |
| unknown-region | malformed-unknown-region | reject | reject | PASS |
| regions-empty | malformed | reject | reject | PASS |
| regions-missing | malformed | reject | reject | PASS |
| budget-zero | malformed | reject | reject | PASS |
| budget-negative | malformed | reject | reject | PASS |
| budget-not-a-number | malformed | reject | reject | PASS |
| guest-count-zero | malformed | reject | reject | PASS |
| guest-count-fractional | malformed | reject | reject | PASS |
| guest-count-boolean | malformed | reject | reject | PASS |
| guest-count-string | malformed | reject | reject | PASS |
| unknown-field-smuggled | high-risk-shape | reject | reject | PASS |
| vibe-too-long | high-risk-freetext | reject | reject | PASS |
| freetext-injection-flattened | high-risk-freetext | accept | accept | PASS |
| must-haves-empty | edge-weakened-gate | accept +warn | accept +warn | PASS |
| must-have-item-empty | malformed | reject | reject | PASS |
| regions-deduped | edge | accept | accept | PASS |
| not-an-object | malformed | reject | reject | PASS |
| country-region | normal | accept | accept | PASS |
| mixed-granularity | normal | accept | accept | PASS |
| region-case-insensitive | edge | accept | accept | PASS |
