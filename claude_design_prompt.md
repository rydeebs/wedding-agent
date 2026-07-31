# Claude Design prompt — Venue Scouting Command Center

Paste this into Claude Design. It describes the frontend as a self-contained,
interactive HTML prototype. (I built a working version of this in
`src/dashboard.py` / `out/dashboard.html`; use this prompt if you want Claude
Design to regenerate or restyle it on the canvas, or to explore variants.)

---

Design an interactive, single-screen **"command center" dashboard** for an AI
agent that scouts destination wedding venues. One screen, no scrolling on
desktop, filling the viewport as a **2x2 quadrant grid** with a hairline divider
between quadrants and a slim top bar above.

## Subject & feeling

This is destination-wedding scouting shown through an operational, mission-control
lens. The feeling is **"a wedding at dusk seen from orbit"** — romantic but
precise. Deliberately NOT a generic hacker terminal (no acid green), and NOT a
warm cream editorial layout. It's dark, elegant, and a little cinematic.

## Layout

```
┌───────────────────────────────────────────────┐
│  TOP BAR: title (serif) · run status · cost    │
├───────────────────────┬───────────────────────┤
│ 01  CANDIDATE MAP      │ 02  MISSION CRITERIA  │
│  interactive 3D globe  │  the couple's input:  │
│  markers per venue,    │  guests, budget, date,│
│  colored by decision,  │  regions, vibe,        │
│  arcs from home base   │  must-/nice-to-haves   │
├───────────────────────┼───────────────────────┤
│ 03  RECOMMENDED·RANKED │ 04  ESCALATIONS &     │
│  ranked venue cards    │      TELEMETRY        │
│  with score bars,      │  human-review flags + │
│  price signal, why     │  stat tiles + cost    │
└───────────────────────┴───────────────────────┘
```

**Top-left (the signature): an interactive 3D globe** using globe.gl (loads
`https://unpkg.com/globe.gl`, night-earth texture
`https://unpkg.com/three-globe/example/img/earth-night.jpg`). Auto-rotating
slowly (pause under prefers-reduced-motion). One point per venue at its real
lat/lng, point color = decision, point height scaled by score. Animated arcs from
a home base to each candidate region. Hover shows a venue tooltip; clicking a
marker flies the camera to it and highlights the matching card in quadrant 03. A
small legend (teal=recommend, amber=escalate, rose=reject) bottom-left.

## Palette (dark, gold, semantic)

- Background ink: `#0A0E17` with a soft radial lift to `#131a2b` top-left
- Primary accent (champagne gold): `#E8C39E`, dim variant `#B99873`
- Decision semantics: recommend `#5FB3A3` (sea teal), escalate `#E8B04B`
  (champagne amber), reject `#C97A82` (muted rose)
- Text `#EDE7DD`, muted `#8C93A3`, hairlines gold at ~14% opacity

## Type

- Display: **Fraunces** (an elegant, high-contrast serif — wedding-invitation
  character) for the title, quadrant numerals (italic), and big stat numbers.
  Used with restraint.
- Utility/body/data: **IBM Plex Mono** for labels, criteria, venue text, and
  telemetry — gives the operational, instrument-panel feel.
- Quadrant headers: a small italic serif numeral (01–04) + an uppercase, wide-
  tracked mono label.

## Quadrant content

- **02 Criteria:** a one-line serif hero sentence ("An intimate wedding for 85
  guests, Oct–Nov 2026, under $40,000.") then a compact key/value block (regions,
  vibe) and two chip rows: must-haves (gold outline) and nice-to-haves (muted).
- **03 Recommended:** ranked cards (01, 02, …). Each: venue name + region eyebrow,
  a one-line rationale, a thin gold score bar (fill = score), and a price line
  ("price by request" / "$32,000 listed" / "price not stated"). Hover + click
  states; clicking cross-highlights the globe.
- **04 Escalations & telemetry:** a short list of venues flagged for human review
  with reasons (amber ▲ marker), then a 3-column grid of stat tiles (evaluated,
  recommend, escalated, agent steps, $/venue, run total), and a footer line:
  "Emails are drafted, not sent. Venues with missing information are escalated,
  not guessed."

## Data

Drive everything from an inlined JSON object named `DATA` with keys: `criteria`,
`summary` (venues_evaluated, recommended, escalated, steps, cost{usd_total},
cost_per_venue_usd), `recommended[]` (each {record:{name,region,pricing_signal,
price_low_usd}, score, rationale}), `escalations[]` ({name, reason}), `points[]`
({name, region, lat, lng, decision, score, color}), `arcs[]`, `home`,
`decisionColor`. Populate with the sample below so the prototype renders fully.

```json
{ "criteria": {"guest_count":85,"budget_ceiling_usd":40000,"date_window":"Oct–Nov 2026","regions":["Tulum, Mexico","Cabo San Lucas, Mexico","Sedona, Arizona"],"vibe":"intimate, natural, not a big-box resort ballroom","must_haves":["on-site lodging","capacity for 85","indoor backup"],"nice_to_haves":["beachfront or dramatic setting","in-house catering","getting-ready suite"]},
  "summary": {"venues_evaluated":7,"recommended":4,"escalated":2,"steps":27,"cost":{"usd_total":0.097},"cost_per_venue_usd":0.014},
  "recommended": [
    {"record":{"name":"Casa Jaguar Tulum","region":"Tulum, Mexico","pricing_signal":"request_only"},"score":0.93,"rationale":"Meets every must-have; jungle-and-beach setting fits the intimate vibe."},
    {"record":{"name":"Cabo Cliff Estate","region":"Cabo San Lucas, Mexico","pricing_signal":"request_only"},"score":0.90,"rationale":"Clifftop views, on-site villa lodging, indoor hall backup."},
    {"record":{"name":"Sedona Sky Ranch","region":"Sedona, Arizona","pricing_signal":"request_only"},"score":0.88,"rationale":"Red-rock ranch, on-site casitas, restored barn for backup."},
    {"record":{"name":"Hotel Esencia","region":"Tulum, Mexico","pricing_signal":"listed","price_low_usd":32000},"score":0.82,"rationale":"29 on-property suites; listed price under budget."}
  ],
  "escalations": [
    {"name":"Villa Lodging Tulum","reason":"capacity not stated — cannot confirm the 85-guest must-have"},
    {"name":"Hacienda del Mar","reason":"no capacity, lodging, or backup on the page"}
  ],
  "points": [
    {"name":"Casa Jaguar Tulum","region":"Tulum, Mexico","lat":20.8,"lng":-87.8,"decision":"recommend","score":0.93,"color":"#5FB3A3"},
    {"name":"Hotel Esencia","region":"Tulum, Mexico","lat":20.0,"lng":-87.1,"decision":"recommend","score":0.82,"color":"#5FB3A3"},
    {"name":"Cabo Cliff Estate","region":"Cabo San Lucas, Mexico","lat":22.9,"lng":-109.9,"decision":"recommend","score":0.90,"color":"#5FB3A3"},
    {"name":"Sedona Sky Ranch","region":"Sedona, Arizona","lat":34.9,"lng":-111.8,"decision":"recommend","score":0.88,"color":"#5FB3A3"},
    {"name":"Grand Cabo Mega Resort","region":"Cabo San Lucas, Mexico","lat":22.4,"lng":-109.4,"decision":"reject","score":0.15,"color":"#C97A82"},
    {"name":"Villa Lodging Tulum","region":"Tulum, Mexico","lat":20.4,"lng":-87.3,"decision":"escalate","score":0.40,"color":"#E8B04B"},
    {"name":"Hacienda del Mar","region":"Cabo San Lucas, Mexico","lat":23.0,"lng":-109.7,"decision":"escalate","score":0.35,"color":"#E8B04B"}
  ],
  "arcs": [
    {"startLat":27.95,"startLng":-82.46,"endLat":20.21,"endLng":-87.47},
    {"startLat":27.95,"startLng":-82.46,"endLat":22.89,"endLng":-109.92},
    {"startLat":27.95,"startLng":-82.46,"endLat":34.87,"endLng":-111.76}
  ],
  "home": {"name":"Home base","lat":27.95,"lng":-82.46},
  "decisionColor": {"recommend":"#5FB3A3","escalate":"#E8B04B","reject":"#C97A82"}
}
```

## Quality bar

Fills the viewport with no desktop scroll; stacks to one column under ~860px with
the globe given a fixed height; visible keyboard focus (gold outline); reduced
motion respected (globe stops auto-rotating); the globe is the one bold element
and every other panel stays quiet and precise. Spend all the boldness on the
globe; keep the rest disciplined.
