"""
Observe stage, richer surface: a four-quadrant "scouting command center".

  top-left      interactive 3D globe with a marker per venue, colored by decision
  top-right     mission criteria (the input the couple gave)
  bottom-left   recommended venues, ranked, with score bars
  bottom-right  escalations + run telemetry (counts, steps, cost per venue)

Reads runs/<id>/report.json + criteria.json and writes out/dashboard.html, a
self-contained page (globe.gl from CDN; the data is inlined). Opens in any
browser; no server needed.

Region coordinates are a small static lookup keyed by the region strings in
criteria.json. In real mode you'd geocode; for a handful of destination regions a
lookup is honest and keeps the agent offline-friendly.
"""

from __future__ import annotations

import hashlib
import json
import os

# Static geocode for the searchable regions. This lookup is also the ALLOWLIST:
# criteria_io rejects any region that is not a key here, because a region the
# globe cannot place is one the command center cannot honestly show a result for.
#
# Two granularities are supported, and they mix freely in one search:
#   "City, Country"  -- a specific place, when the couple has one in mind
#   "Country"        -- the whole country, for "somewhere in Italy that fits"
#
# Country entries are centroids. A venue found under "Italy" is somewhere in
# Italy and we do not know where, so its marker carries a deliberately wide
# spread (below) rather than a tight cluster that would imply we do.
#
# Replacing this with a real geocoder is the production fix; then any region
# resolves and the allowlist disappears. See the README.
REGION_COORDS = {
    # --- City, Country -------------------------------------------------
    "Tulum, Mexico": (20.2114, -87.4654),
    "Cabo San Lucas, Mexico": (22.8905, -109.9167),
    "Sedona, Arizona": (34.8697, -111.7610),
    # --- Country -------------------------------------------------------
    "Mexico": (23.6345, -102.5528),
    "United States": (39.8283, -98.5795),
    "Italy": (42.8333, 12.8333),
    "Portugal": (39.3999, -8.2245),
    "Spain": (40.4637, -3.7492),
    "Greece": (39.0742, 21.8243),
    "France": (46.2276, 2.2137),
    "Croatia": (45.1000, 15.2000),
    "Ireland": (53.4129, -8.2439),
    "Morocco": (31.7917, -7.0926),
    "Costa Rica": (9.7489, -83.7534),
    "Thailand": (15.8700, 100.9925),
}

# Degrees of scatter for a region's markers. A city gets a small nudge purely so
# venues do not stack on one pixel; a country gets a wide one because the marker
# genuinely means "somewhere in here", and a tight cluster on the centroid would
# read as precision we do not have.
DEFAULT_SPREAD = 1.4
COUNTRY_SPREAD = 7.0
REGION_SPREAD = {r: COUNTRY_SPREAD for r in REGION_COORDS if "," not in r}

HOME = {"name": "Home base", "lat": 27.9506, "lng": -82.4572}  # Tampa, FL

DECISION_COLOR = {
    "recommend": "#5FB3A3",   # sea teal
    "escalate": "#E8B04B",    # champagne amber
    "reject": "#C97A82",      # muted rose
}


def _jitter(url: str, spread: float = DEFAULT_SPREAD) -> tuple[float, float]:
    """Deterministic offset so venues in the same region don't overlap."""
    h = int(hashlib.md5(url.encode()).hexdigest(), 16)
    dlat = ((h % 1000) / 1000 - 0.5) * spread
    dlng = (((h // 1000) % 1000) / 1000 - 0.5) * spread
    return dlat, dlng


def _points(report: dict) -> list[dict]:
    pts = []
    for s in report["all_scored"]:
        rec = s["record"]
        base = REGION_COORDS.get(rec["region"])
        if not base:
            continue
        dlat, dlng = _jitter(rec["url"], REGION_SPREAD.get(rec["region"], DEFAULT_SPREAD))
        pts.append({
            "name": rec["name"],
            "region": rec["region"],
            "lat": base[0] + dlat,
            "lng": base[1] + dlng,
            "decision": s["decision"],
            "score": s["score"],
            "color": DECISION_COLOR.get(s["decision"], "#8892a6"),
        })
    return pts


def _arcs(report: dict) -> list[dict]:
    seen = set()
    arcs = []
    for s in report["all_scored"]:
        region = s["record"]["region"]
        if region in seen:
            continue
        seen.add(region)
        base = REGION_COORDS.get(region)
        if base:
            arcs.append({
                "startLat": HOME["lat"], "startLng": HOME["lng"],
                "endLat": base[0], "endLng": base[1],
            })
    return arcs


# Why a venue landed in front of a human. Order is display order -- the
# must-have gaps first (a human can answer those with one email), then the
# record problems, then the things that never got far enough to have a record.
ESCALATION_TAGS = [
    ("lodging",     "Lodging unconfirmed"),
    ("capacity",    "Capacity unconfirmed"),
    ("backup",      "Weather backup unconfirmed"),
    ("other",       "Other must-have unconfirmed"),
    ("grounding",   "Unsupported claims"),
    ("currency",    "Price in another currency"),
    ("confidence",  "Low confidence"),
    ("unreachable", "Page unreachable"),
    ("failed",      "Extraction/scoring failed"),
]


def _tags_for(entry: dict, scored: dict | None) -> list[str]:
    """Which buckets one escalation belongs in.

    Read off the STRUCTURED record -- must_have_checks and grounding_flags --
    not off the reason string. The reason is prose assembled for a human and
    partly written by the model; grouping on substrings of it would silently
    reshuffle the filters whenever the wording changed.

    A venue can be in several buckets at once, because it usually is: the
    common case is a page that never mentioned the rain plan AND quoted its
    price in euros.
    """
    from . import grounding, scoring    # local: keeps import order simple

    if scored is None:
        # Never scored, so there is no record to inspect -- these come from the
        # fetch/extract failure paths, which write a fixed reason string.
        reason = (entry.get("reason") or "").lower()
        if "unreachable" in reason or "no content" in reason or "partial" in reason:
            return ["unreachable"]
        return ["failed"]

    tags = []
    for check in scored.get("must_have_checks", []):
        if check.get("status") != "unknown":
            continue
        text = (check.get("must_have") or "").lower()
        if scoring._LODGING.search(text):
            tags.append("lodging")
        elif scoring._BACKUP.search(text):
            tags.append("backup")
        elif scoring._CAPACITY.search(text):
            tags.append("capacity")
        else:
            tags.append("other")

    flags = scored.get("grounding_flags") or []
    if grounding.integrity_flags(flags):
        tags.append("grounding")
    if any(f == grounding.NON_USD_FLAG for f in flags):
        tags.append("currency")

    # Everything checked out and it still escalated: the calibrated confidence
    # was under the threshold. Worth its own bucket -- it is the one group a
    # human can often clear by just reading the page.
    if not tags:
        tags.append("confidence")
    return tags


def _group_escalations(report: dict) -> tuple[list[dict], list[dict]]:
    """Escalations tagged by what is missing, plus the counts for the filters."""
    by_url = {s["record"]["url"]: s for s in report.get("all_scored", [])
              if s.get("decision") == "escalate"}

    tagged = []
    for e in report.get("escalations", []):
        tags = _tags_for(e, by_url.get(e.get("url")))
        tagged.append({**e, "tags": tags})

    counts = [{"key": k, "label": lbl,
               "n": sum(1 for e in tagged if k in e["tags"])}
              for k, lbl in ESCALATION_TAGS]
    return tagged, [c for c in counts if c["n"]]


def build_data(report: dict, criteria: dict, sent: dict | None = None) -> dict:
    """The payload the page renders from.

    Extracted so the server can hand the SAME shape back from /run and /report
    that generation inlines at build time. One shape, one renderer -- the page
    cannot drift between "opened from disk" and "served live".
    """
    escalations, escalation_filters = _group_escalations(report)
    return {
        "criteria": criteria,
        "summary": report["summary"],
        "recommended": report["recommended"],
        "escalations": escalations,
        "escalation_filters": escalation_filters,
        "points": _points(report),
        "arcs": _arcs(report),
        "home": HOME,
        "decisionColor": DECISION_COLOR,
        "sent": sent or {},
    }


def render(report: dict, criteria: dict, out_path: str) -> str:
    data = build_data(report, criteria)
    html = _TEMPLATE.replace("/*__DATA__*/", json.dumps(data))
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as f:
        f.write(html)
    return out_path


_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Wedding Venue Agent — Command Center</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,400;9..144,600;9..144,900&family=IBM+Plex+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<script src="https://unpkg.com/globe.gl"></script>
<style>
  :root{
    --ink:#0A0E17; --ink2:#0E1420; --panel:rgba(255,255,255,.03);
    --line:rgba(232,195,158,.14); --line2:rgba(255,255,255,.06);
    --gold:#E8C39E; --gold-dim:#B99873;
    --teal:#5FB3A3; --amber:#E8B04B; --rose:#C97A82;
    --text:#EDE7DD; --muted:#8C93A3; --muted2:#5C6273;
  }
  *{box-sizing:border-box}
  html,body{height:100%}
  body{
    margin:0; background:
      radial-gradient(1200px 800px at 18% 22%, #131a2b 0%, transparent 60%),
      var(--ink);
    color:var(--text); font-family:"IBM Plex Mono",ui-monospace,monospace;
    overflow:hidden;
  }
  .frame{display:flex; flex-direction:column; height:100vh}
  header.bar{
    display:flex; align-items:baseline; gap:16px; padding:14px 22px;
    border-bottom:1px solid var(--line); flex:0 0 auto;
  }
  /* clamp, not a fixed 22px: the title is materially longer than it used to be
     and wrapped to two lines on a narrow window, which pushed the header to
     83px. Scaling it down is better than a header that changes height. */
  header .title{font-family:"Fraunces",serif; font-weight:900;
                font-size:clamp(15px, 1.9vw, 22px); letter-spacing:.3px; white-space:nowrap}
  header .title em{color:var(--gold); font-style:italic; font-weight:600}
  header .status{margin-left:auto; display:flex; gap:18px; align-items:center; font-size:11px; color:var(--muted)}
  .dot{width:7px;height:7px;border-radius:50%;background:var(--teal);box-shadow:0 0 10px var(--teal);display:inline-block;margin-right:6px}

  .grid{
    display:grid; grid-template-columns:1.15fr .85fr; grid-template-rows:1fr 1fr;
    gap:1px; background:var(--line2); flex:1 1 auto; min-height:0;
  }
  .quad{background:linear-gradient(180deg,var(--ink2),var(--ink)); position:relative; min-height:0; display:flex; flex-direction:column}
  .quad > .qhead{display:flex; align-items:center; gap:10px; padding:12px 18px 8px}
  .qhead .idx{font-family:"Fraunces",serif; font-style:italic; color:var(--gold-dim); font-size:13px}
  .qhead .label{font-size:10.5px; letter-spacing:.2em; text-transform:uppercase; color:var(--muted)}
  .qbody{padding:2px 18px 16px; overflow:auto; min-height:0}
  .qbody::-webkit-scrollbar{width:8px} .qbody::-webkit-scrollbar-thumb{background:var(--line2);border-radius:4px}

  /* globe quad */
  #globeQuad{padding:0}
  #globe{position:absolute; inset:0}
  #globeQuad .qhead{position:absolute; top:0; left:0; z-index:2}
  .legend{position:absolute; left:18px; bottom:16px; z-index:2; display:flex; gap:16px; font-size:11px; color:var(--muted)}
  .legend b{font-weight:500; color:var(--text)}
  .swatch{width:9px;height:9px;border-radius:50%;display:inline-block;margin-right:6px;vertical-align:middle}

  /* criteria */
  .crit-hero{font-family:"Fraunces",serif; font-size:15px; color:var(--text); margin:6px 0 14px; line-height:1.4}
  .crit-hero span{color:var(--gold)}
  .kv{display:grid; grid-template-columns:auto 1fr; gap:6px 14px; font-size:12.5px; align-items:baseline}
  .kv dt{color:var(--muted); white-space:nowrap}
  .kv dd{margin:0; color:var(--text)}
  .chips{display:flex; flex-wrap:wrap; gap:6px; margin-top:4px}
  .chip{border:1px solid var(--line); border-radius:999px; padding:2px 10px; font-size:11px; color:var(--gold)}
  .chip.soft{color:var(--muted); border-color:var(--line2)}

  /* criteria editor */
  .crit-actions{display:flex; gap:8px; align-items:center; margin-top:14px; flex-wrap:wrap}
  .btn{font:inherit; font-size:11px; letter-spacing:.12em; text-transform:uppercase;
       background:transparent; color:var(--gold); border:1px solid var(--line);
       border-radius:999px; padding:5px 14px; cursor:pointer; transition:background .15s,border-color .15s}
  .btn:hover:not(:disabled){background:rgba(232,195,158,.08); border-color:var(--gold-dim)}
  .btn:disabled{opacity:.45; cursor:default}
  .btn.ghost{color:var(--muted); }
  .edit label{display:block; font-size:10.5px; letter-spacing:.14em; text-transform:uppercase;
              color:var(--muted); margin:11px 0 4px}
  .edit input, .edit textarea{width:100%; box-sizing:border-box; font:inherit; font-size:12.5px;
       color:var(--text); background:rgba(255,255,255,.03); border:1px solid var(--line2);
       border-radius:6px; padding:6px 9px; outline:none}
  .edit input:focus, .edit textarea:focus{border-color:var(--gold-dim); background:rgba(255,255,255,.05)}
  .edit textarea{resize:vertical; min-height:52px; line-height:1.45}
  .edit .pair{display:grid; grid-template-columns:1fr 1fr; gap:0 12px}
  .edit .hint{font-size:10.5px; color:var(--muted2); margin-top:4px; line-height:1.55}
  .edit .hint b{color:var(--muted); font-weight:500}
  .edit input.bad, .edit textarea.bad{border-color:#C97A82}
  .msg{margin-top:12px; font-size:11.5px; line-height:1.5; border-radius:6px; padding:8px 11px}
  .msg.err{color:#E9A7AD; background:rgba(201,122,130,.10); border:1px solid rgba(201,122,130,.35)}
  .msg.warn{color:#E8B04B; background:rgba(232,176,75,.09); border:1px solid rgba(232,176,75,.30)}
  .msg.ok{color:#7FC9B8; background:rgba(95,179,163,.10); border:1px solid rgba(95,179,163,.30)}
  .msg ul{margin:5px 0 0; padding-left:16px}
  .msg li{margin:2px 0}

  /* send control (quadrant 03 only -- escalations stay read-only) */
  .btn.small{font-size:10px; padding:4px 11px}
  .send-wrap{margin:7px 0 2px 30px}
  .sent-badge{margin:7px 0 2px 30px; font-size:10.5px; letter-spacing:.08em;
              text-transform:uppercase; color:var(--teal)}
  .no-email{margin:7px 0 2px 30px; font-size:10.5px; color:var(--muted2)}
  .draft{margin-top:8px; border-left:1px solid var(--line); padding-left:11px}
  .draft label{display:block; font-size:10px; letter-spacing:.14em; text-transform:uppercase;
               color:var(--muted); margin:9px 0 3px}
  .draft input, .draft textarea{width:100%; box-sizing:border-box; font:inherit; font-size:12px;
       color:var(--text); background:rgba(255,255,255,.03); border:1px solid var(--line2);
       border-radius:6px; padding:6px 9px; outline:none}
  .draft input:focus, .draft textarea:focus{border-color:var(--gold-dim)}
  .draft textarea{resize:vertical; line-height:1.5}
  .draft-to{font-size:11.5px; color:var(--muted)}
  .draft-to b{color:var(--text); font-weight:500}
  .draft-to .fixed{color:var(--muted2); font-size:10px}
  .draft-note{font-size:11.5px; color:var(--amber); margin-bottom:7px}
  .confirm .kv{font-size:12px; margin:2px 0 8px}
  .final-body{font-size:11.5px; color:var(--muted); line-height:1.55; white-space:pre-line;
              background:rgba(255,255,255,.02); border:1px solid var(--line2);
              border-radius:6px; padding:9px 11px; max-height:190px; overflow:auto}

  /* recommended list */
  .venue{padding:11px 0; border-bottom:1px solid var(--line2); cursor:pointer; transition:background .15s}
  .venue:hover,.venue.active{background:rgba(232,195,158,.05)}
  .venue .row1{display:flex; align-items:baseline; gap:8px}
  .venue .rank{font-family:"Fraunces",serif; font-style:italic; color:var(--gold-dim); font-size:13px; width:22px}
  .venue .name{font-size:13.5px; color:var(--text)}
  a.vlink{text-decoration:none; border-bottom:1px solid transparent}
  a.vlink:hover{color:var(--gold); border-bottom-color:var(--gold)}
  a.vlink .ext{font-size:10px; opacity:.45; margin-left:5px; vertical-align:1px}
  a.vlink:hover .ext{opacity:1}
  .venue .region{margin-left:auto; font-size:10.5px; color:var(--muted2); letter-spacing:.08em; text-transform:uppercase}
  .venue .why{font-size:11.5px; color:var(--muted); margin:5px 0 6px 30px; line-height:1.45; white-space:pre-line}
  /* Scoped to .venue on purpose: the page header is <header class="bar">, and an
     unscoped .bar rule also matched it -- header.bar sets no height, so this
     height:3px + overflow:hidden won the cascade and clipped the title to a
     sliver (plus shunted it 30px right). */
  .venue .bar{height:3px; background:var(--line2); border-radius:2px; margin-left:30px; overflow:hidden}
  .venue .bar > i{display:block; height:100%; background:linear-gradient(90deg,var(--gold-dim),var(--gold))}
  .price{font-size:10.5px; color:var(--muted2); margin-left:30px}

  /* escalations + telemetry */
  .esc{display:flex; gap:9px; padding:8px 0; border-bottom:1px solid var(--line2); font-size:12px}
  .esc .mk{color:var(--amber); flex:0 0 auto}
  .esc .en{color:var(--text)} .esc .er{color:var(--muted); font-size:11px}

  /* Escalation filters. 43 rows of amber triangles is a wall, not a queue --
     these split it by what is actually missing, so a human can work one kind
     of gap at a time (every "capacity unconfirmed" is the same email). */
  .chips{display:flex; flex-wrap:wrap; gap:6px; margin:7px 0 10px}
  .chip{font-family:inherit; font-size:10.5px; letter-spacing:.04em; cursor:pointer;
        color:var(--muted); background:rgba(255,255,255,.03);
        border:1px solid var(--line2); border-radius:999px; padding:3px 10px}
  .chip:hover{color:var(--text); border-color:var(--line)}
  .chip.on{color:var(--ink); background:var(--gold); border-color:var(--gold); font-weight:500}
  .chip .n{opacity:.65; margin-left:5px}
  .chip.on .n{opacity:.75}
  .more{font-family:inherit; font-size:11px; cursor:pointer; color:var(--gold);
        background:none; border:none; padding:8px 0 0; text-decoration:underline}
  .tele{display:grid; grid-template-columns:repeat(3,1fr); gap:10px; margin-top:14px}
  .stat{border:1px solid var(--line2); border-radius:8px; padding:10px 12px}
  .stat .n{font-family:"Fraunces",serif; font-size:24px; color:var(--gold)}
  .stat .l{font-size:9.5px; letter-spacing:.16em; text-transform:uppercase; color:var(--muted); margin-top:2px}
  .foot{margin-top:12px; font-size:11px; color:var(--muted2); line-height:1.5}
  .foot b{color:var(--gold-dim); font-weight:500}
  /* mode badge: mock runs are fixtures, not searches, and the page must say so */
  .mode{font-size:9.5px; letter-spacing:.16em; text-transform:uppercase;
        border:1px solid var(--amber); color:var(--amber); border-radius:999px; padding:2px 9px}
  .steps-link{color:var(--gold); text-decoration:none; border-bottom:1px solid var(--line)}
  /* live run progress, under the criteria buttons */
  .prog{margin-top:13px}
  .prog-stage{font-size:11.5px; color:var(--text)}
  .prog-bar{height:4px; background:var(--line2); border-radius:2px; overflow:hidden; margin:6px 0 5px}
  .prog-bar > i{display:block; height:100%; border-radius:2px;
                background:linear-gradient(90deg,var(--gold-dim),var(--gold));
                transition:width .6s ease}
  .prog-meta{font-size:10.5px; color:var(--muted2); letter-spacing:.04em}
  .steps-link:hover{border-bottom-color:var(--gold)}
  .empty-note{margin-top:10px; font-size:11px; line-height:1.5; color:var(--amber);
              background:rgba(232,176,75,.08); border:1px solid rgba(232,176,75,.28);
              border-radius:6px; padding:8px 11px}
  .empty-note b{font-weight:500}

  @media (max-width:860px){
    body{overflow:auto}
    .grid{grid-template-columns:1fr; grid-template-rows:auto}
    #globeQuad{height:52vh}
  }
  @media (prefers-reduced-motion:reduce){ .dot{box-shadow:none} }
  :focus-visible{outline:2px solid var(--gold); outline-offset:2px}
</style>
</head>
<body>
<div class="frame">
  <header class="bar">
    <div class="title">Wedding Venue Agent <em>— Command Center</em></div>
    <div class="status">
      <span id="statusMode"></span>
      <span id="statusCost"></span>
    </div>
  </header>

  <div class="grid">
    <!-- TOP LEFT: globe -->
    <section class="quad" id="globeQuad">
      <div class="qhead"><span class="idx">01</span><span class="label">Candidate map</span></div>
      <div id="globe"></div>
      <div class="legend" id="legend"></div>
    </section>

    <!-- TOP RIGHT: criteria -->
    <section class="quad">
      <div class="qhead"><span class="idx">02</span><span class="label">Mission criteria</span></div>
      <div class="qbody" id="criteria"></div>
    </section>

    <!-- BOTTOM LEFT: recommended -->
    <section class="quad">
      <div class="qhead"><span class="idx">03</span><span class="label">Recommended · ranked</span></div>
      <div class="qbody" id="recommended"></div>
    </section>

    <!-- BOTTOM RIGHT: escalations + telemetry -->
    <section class="quad">
      <div class="qhead"><span class="idx">04</span><span class="label">Escalations &amp; telemetry</span></div>
      <div class="qbody" id="telemetry"></div>
    </section>
  </div>
</div>

<script>
/* The generated payload is now a SEED, not the source of truth. When the page
   is served by server.py it refetches /report on load and after every run, so
   edits and reruns show live. Opened straight off disk it just keeps rendering
   this snapshot, exactly as it always did. Same shape either way -- both come
   from dashboard.build_data(). */
let DATA = /*__DATA__*/;
let LIVE = false;                 // is server.py behind this page?
const $ = s => document.querySelector(s);
/* Escapes quotes too, so a value is safe in an attribute and not just in a
   text node. Criteria are user-editable now, so this string can contain
   anything the couple typed. (Form fields are still populated via .value
   rather than markup -- this is the belt to that's braces.) */
const esc = s => String(s==null?"":s).replace(/[&<>"']/g,c=>(
  {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));

/* Venue URLs arrive from web search, so they are untrusted input that ends up
   in an href. Only http(s) is allowed to become a link -- a javascript: or
   data: URL renders as plain text instead of an executable one. */
function safeUrl(u){
  try {
    const p = new URL(String(u));
    return (p.protocol === "http:" || p.protocol === "https:") ? p.href : null;
  } catch (e) { return null; }
}

/* ---- Globe ---- */
const world = Globe()
  .globeImageUrl("https://unpkg.com/three-globe/example/img/earth-night.jpg")
  .backgroundColor("rgba(0,0,0,0)")
  .atmosphereColor("#E8C39E").atmosphereAltitude(0.18)
  .pointsData(DATA.points)
  .pointLat("lat").pointLng("lng").pointColor("color")
  .pointAltitude(d => 0.02 + d.score*0.14).pointRadius(0.42)
  .pointLabel(d => `<div style="font-family:'IBM Plex Mono';background:#0A0E17;border:1px solid rgba(232,195,158,.4);padding:6px 9px;border-radius:6px;color:#EDE7DD;font-size:12px">
      <b>${esc(d.name)}</b><br><span style="color:#8C93A3">${esc(d.region)} · ${d.decision} · ${(d.score*100|0)}%</span></div>`)
  .arcsData(DATA.arcs)
  .arcColor(() => ["rgba(232,195,158,.05)","rgba(232,195,158,.5)"])
  .arcAltitude(0.22).arcStroke(0.4).arcDashLength(0.5).arcDashGap(0.25).arcDashAnimateTime(3600)
  .onPointClick(d => focusVenue(d.name))
  (document.getElementById("globe"));

function sizeGlobe(){
  const q = document.getElementById("globeQuad").getBoundingClientRect();
  world.width(q.width).height(q.height);
}
window.addEventListener("resize", sizeGlobe); sizeGlobe();

world.controls().autoRotate = !window.matchMedia("(prefers-reduced-motion: reduce)").matches;
world.controls().autoRotateSpeed = 0.55;
world.controls().enableZoom = true;
// frame the candidate regions
setTimeout(()=>world.pointOfView({lat:26, lng:-100, altitude:2.1}, 1200), 200);

/* ---- Legend ---- */
$("#legend").innerHTML = Object.entries(DATA.decisionColor).map(([k,v])=>
  `<span><span class="swatch" style="background:${v}"></span><b>${k}</b></span>`).join("");

/* ---- Criteria (read-only view + editor) ----
   The editor writes through the local criteria server (server.py). Opened
   straight off disk (file://) there is nothing to write to, so the quadrant
   stays exactly what it always was: read-only. The page remains a
   self-contained artifact either way. */
let renderCriteria;   // defined below; called by renderAll()
(function(){
  const CRIT = $("#criteria");
  let known = [];                   // regions the globe can plot
  let limits = {couple:120, region:120, regions:12, date_window:120, vibe:400,
                must_have:200, must_haves:20, nice_to_have:200, nice_to_haves:20,
                guest_count_max:10000, budget_max_usd:100000000};
  /* Criteria live in DATA so a rerun refreshes them like everything else. */
  const crit = () => DATA.criteria || {};

  const lines = v => String(v||"").split("\n").map(s=>s.trim()).filter(Boolean);
  const msg = (kind, title, items) => `<div class="msg ${kind}">${esc(title)}` +
    (items && items.length ? `<ul>${items.map(i=>`<li>${esc(i)}</li>`).join("")}</ul>` : "") +
    `</div>`;

  /* ---- read-only view ---- */
  function renderView(banner){
    const c = crit();
    const must = (c.must_haves||[]).map(m=>`<span class="chip">${esc(m)}</span>`).join("");
    const nice = (c.nice_to_haves||[]).map(m=>`<span class="chip soft">${esc(m)}</span>`).join("");
    CRIT.innerHTML = `
      <div class="crit-hero">An intimate wedding for <span>${esc(c.guest_count)}</span> guests, ${esc(c.date_window||"")}, under <span>$${Number(c.budget_ceiling_usd).toLocaleString()}</span>.</div>
      <dl class="kv">
        <dt>Regions</dt><dd>${(c.regions||[]).map(esc).join(" · ")}</dd>
        <dt>Vibe</dt><dd>${esc(c.vibe||"—")}</dd>
      </dl>
      <div style="margin-top:14px;font-size:10.5px;letter-spacing:.16em;text-transform:uppercase;color:var(--muted)">Must have</div>
      <div class="chips">${must}</div>
      <div style="margin-top:12px;font-size:10.5px;letter-spacing:.16em;text-transform:uppercase;color:var(--muted)">Nice to have</div>
      <div class="chips">${nice}</div>
      <div class="crit-actions">
        ${LIVE ? `<button class="btn" id="crit-edit">Edit criteria</button>
                  <button class="btn" id="crit-run">Run agent</button>` : ``}
      </div>
      ${LIVE ? `` : `<div style="margin-top:9px;font-size:10.5px;color:var(--muted2);line-height:1.5">
         Read-only snapshot. Start the server and open it there to edit and rerun:
         <span style="color:var(--muted)">uvicorn server:app</span> &rarr; http://127.0.0.1:8000</div>`}
      ${banner || ""}`;
    if (LIVE){
      $("#crit-edit").onclick = () => renderEdit();
      $("#crit-run").onclick = () => runAgent();
    }
  }

  /* ---- live progress ----
     A real run is 30-60 minutes. A static "running…" banner for that long is
     indistinguishable from a hung process, so poll /progress and show where it
     actually is. Works for a run started from the terminal too: boot() checks
     for one in flight and attaches the same bar. */
  let polling = false;

  function progressBox(){
    let box = $("#run-progress");
    if (!box){
      box = document.createElement("div");
      box.id = "run-progress";
      box.className = "prog";
      box.innerHTML = `<div class="prog-stage">Starting…</div>
        <div class="prog-bar"><i style="width:1%"></i></div>
        <div class="prog-meta"></div>`;
      CRIT.appendChild(box);
    }
    return box;
  }

  async function trackProgress(){
    if (polling) return;
    polling = true;
    const box = progressBox();
    while (polling){
      try {
        const p = await (await fetch("/progress")).json();
        if (!p.running){ polling = false; break; }
        const d = p.decisions || {};
        box.querySelector(".prog-stage").textContent = p.stage || "Working…";
        box.querySelector(".prog-bar i").style.width = (p.percent || 1) + "%";
        box.querySelector(".prog-meta").textContent = [
          `${p.percent}%`,
          p.regions_total ? `region ${Math.min(p.regions_started || 1, p.regions_total)} of ${p.regions_total}` : null,
          p.scored ? `${p.scored} scored` : null,
          d.recommend ? `${d.recommend} recommended` : null,
          p.elapsed_s != null ? `${Math.round(p.elapsed_s / 60)} min` : null,
        ].filter(Boolean).join(" · ");
      } catch (e) { /* server busy mid-run; try again */ }
      await new Promise(r => setTimeout(r, 4000));
    }
  }

  /* ---- run the agent on the saved criteria ---- */
  async function runAgent(){
    const btns = CRIT.querySelectorAll("button");
    btns.forEach(b => b.disabled = true);
    const box = progressBox();
    trackProgress();
    try {
      const res = await fetch("/run", {method:"POST", headers:{"Content-Type":"application/json"}, body:"{}"});
      const j = await res.json();
      polling = false;
      if (!res.ok){
        btns.forEach(b => b.disabled = false);
        box.outerHTML = msg("err", "The run failed:", [j.detail || res.statusText]);
        return;
      }
      DATA = j;                 // fresh report, same shape
      renderAll();              // every quadrant reflects the new run
    } catch (e) {
      polling = false;
      btns.forEach(b => b.disabled = false);
      box.outerHTML = msg("err", "Could not reach the server:", [String(e)]);
    }
  }
  window.trackProgress = trackProgress;

  /* ---- editor ---- */
  function renderEdit(banner){
    CRIT.innerHTML = `
      <div class="edit">
        <div class="pair">
          <div><label for="f-guests">Guest count</label><input id="f-guests" type="number" min="1" step="1"></div>
          <div><label for="f-budget">Budget ceiling (USD)</label><input id="f-budget" type="number" min="1" step="100"></div>
        </div>
        <label for="f-dates">Date window</label><input id="f-dates" type="text">
        <label for="f-regions">Regions — one per line</label><textarea id="f-regions" rows="4"></textarea>
        <div class="hint">A whole country for a broad search, or “City, Country” for a specific place. Mix freely.
          ${known.length ? `<br><b>Countries:</b> ${known.filter(r=>!r.includes(",")).map(esc).join(" · ")}
                            <br><b>Cities:</b> ${known.filter(r=>r.includes(",")).map(esc).join(" · ")}` : ``}</div>
        <label for="f-vibe">Vibe</label><textarea id="f-vibe" rows="2"></textarea>
        <label for="f-must">Must-haves — one per line</label><textarea id="f-must" rows="3"></textarea>
        <label for="f-nice">Nice-to-haves — one per line</label><textarea id="f-nice" rows="3"></textarea>
        <div class="crit-actions">
          <button class="btn" id="f-save">Update criteria</button>
          <button class="btn ghost" id="f-cancel">Cancel</button>
        </div>
        <div id="f-msg">${banner || ""}</div>
      </div>`;

    /* Populate by property, never by markup: no parsing, nothing to escape. */
    const c = crit();
    $("#f-guests").value  = c.guest_count ?? "";
    $("#f-budget").value  = c.budget_ceiling_usd ?? "";
    $("#f-dates").value   = c.date_window || "";
    $("#f-regions").value = (c.regions || []).join("\n");
    $("#f-vibe").value    = c.vibe || "";
    $("#f-must").value    = (c.must_haves || []).join("\n");
    $("#f-nice").value    = (c.nice_to_haves || []).join("\n");

    $("#f-cancel").onclick = () => renderView();
    $("#f-save").onclick   = () => submit();
  }

  /* ---- client-side validation ----
     Mirrors src/criteria_io.py so mistakes are caught before a round trip.
     It is a convenience, not the guard: the server re-validates everything and
     is the only thing that decides what reaches the agent. */
  function collect(){
    const errors = [], bad = [];
    const mark = (id, m) => { errors.push(m); bad.push(id); };

    const guests = $("#f-guests").value.trim();
    const budget = $("#f-budget").value.trim();
    const gn = Number(guests), bn = Number(budget);

    if (!guests || !Number.isFinite(gn) || !Number.isInteger(gn) || gn <= 0)
      mark("f-guests", "Guest count must be a whole number greater than zero.");
    else if (gn > limits.guest_count_max)
      mark("f-guests", `Guest count is unreasonably large (max ${limits.guest_count_max.toLocaleString()}).`);

    if (!budget || !Number.isFinite(bn) || bn <= 0)
      mark("f-budget", "Budget must be a number greater than zero.");
    else if (bn > limits.budget_max_usd)
      mark("f-budget", `Budget is unreasonably large (max ${limits.budget_max_usd.toLocaleString()}).`);

    const regions = lines($("#f-regions").value);
    if (!regions.length) mark("f-regions", "At least one region is required.");
    else if (regions.length > limits.regions) mark("f-regions", `Too many regions (max ${limits.regions}).`);
    else if (regions.some(r => r.length > limits.region)) mark("f-regions", `A region name is too long (max ${limits.region} chars).`);

    const dates = $("#f-dates").value.trim();
    if (dates.length > limits.date_window) mark("f-dates", `Date window is too long (max ${limits.date_window} chars).`);

    const vibe = $("#f-vibe").value.trim();
    if (vibe.length > limits.vibe) mark("f-vibe", `Vibe is too long (max ${limits.vibe} chars).`);

    const must = lines($("#f-must").value), nice = lines($("#f-nice").value);
    if (must.length > limits.must_haves) mark("f-must", `Too many must-haves (max ${limits.must_haves}).`);
    if (must.some(m => m.length > limits.must_have)) mark("f-must", `A must-have is too long (max ${limits.must_have} chars).`);
    if (nice.length > limits.nice_to_haves) mark("f-nice", `Too many nice-to-haves (max ${limits.nice_to_haves}).`);
    if (nice.some(m => m.length > limits.nice_to_have)) mark("f-nice", `A nice-to-have is too long (max ${limits.nice_to_have} chars).`);

    document.querySelectorAll(".edit input, .edit textarea").forEach(el => el.classList.remove("bad"));
    bad.forEach(id => { const el = $("#" + id); if (el) el.classList.add("bad"); });

    if (errors.length) return {errors};

    /* Start from the current criteria so fields the editor does not expose
       (couple) survive the round trip instead of being dropped. */
    const next = Object.assign({}, crit(), {
      regions, guest_count: gn, budget_ceiling_usd: bn,
      date_window: dates, vibe, must_haves: must, nice_to_haves: nice,
    });

    /* The server is authoritative on this and rejects unknown regions; flagging
       it here just saves a round trip. Case- and spacing-insensitive to match
       criteria_io.canonical_region -- a stricter client check would reject
       "italy" locally even though the server accepts it. */
    const canon = s => s.trim().replace(/\s*,\s*/g, ", ").toLowerCase();
    const knownCanon = new Set(known.map(canon));
    const unknownRegions = known.length ? regions.filter(r => !knownCanon.has(canon(r))) : [];
    if (unknownRegions.length){
      /* " · " not ", " -- region names contain commas, so a comma-joined list
         of them is unreadable. */
      mark("f-regions", `Unknown region(s): ${unknownRegions.join(" · ")}. Known: ${known.join(" · ")}`);
      document.querySelectorAll(".edit input, .edit textarea").forEach(el => el.classList.remove("bad"));
      bad.forEach(id => { const el = $("#" + id); if (el) el.classList.add("bad"); });
      return {errors};
    }

    return {criteria: next};
  }

  async function submit(){
    const out = collect();
    const box = $("#f-msg");
    if (out.errors){ box.innerHTML = msg("err", "Fix these before saving:", out.errors); return; }

    const buttons = ["f-save","f-cancel"].map(i => $("#" + i));
    buttons.forEach(b => b.disabled = true);
    box.innerHTML = msg("ok", "Saving…");

    try {
      const res = await fetch("/criteria", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({criteria: out.criteria}),
      });
      const body = await res.json();

      if (!res.ok || body.saved === false){
        box.innerHTML = msg("err", "The server rejected these criteria:", body.errors || [body.detail || res.statusText]);
        buttons.forEach(b => b.disabled = false);
        return;
      }

      DATA.criteria = body.criteria;
      let banner = "";
      if ((body.warnings || []).length) banner += msg("warn", "Saved, with warnings:", body.warnings);
      banner += msg("ok", "Saved to criteria.json. Use “Run agent” to apply it.");
      renderView(banner);
    } catch (e) {
      box.innerHTML = msg("err", "Could not reach the criteria server:", [String(e)]);
      buttons.forEach(b => b.disabled = false);
    }
  }

  /* Fetch the validator's rules once, so the editor can enforce what the
     server enforces. Called by boot() before the first render. */
  window.loadCriteriaMeta = async function(){
    try {
      const r = await fetch("/criteria", {headers:{"Accept":"application/json"}});
      if (r.ok){
        const j = await r.json();
        if (!DATA.criteria) DATA.criteria = j.criteria;
        known = j.known_regions || [];
        limits = Object.assign(limits, j.limits || {});
      }
    } catch (e) { /* no server: the editor never opens anyway */ }
  };

  renderCriteria = () => renderView();
})();

/* ---- Recommended ---- */
function renderRecommended(){
  const rec = DATA.recommended || [];
  if(!rec.length){ $("#recommended").innerHTML = `<p style="color:var(--muted);font-size:12px">No venues cleared every must-have. See escalations.</p>`; return; }
  const sent = DATA.sent || {};
  $("#recommended").innerHTML = rec.map((s,i)=>{
    const r=s.record, price = r.pricing_signal==="listed"
      ? `$${Number(r.price_low_usd).toLocaleString()} (listed)`
      : r.pricing_signal==="request_only" ? "price by request" : "price not stated";
    /* The recipient exists only if the extractor found an email on the venue's
       own page. No email -> no send control, by design. */
    const canSend = r.contact_method==="email" && r.contact_value;
    const done = sent[r.url];
    let action = "";
    if (done) action = `<div class="sent-badge">✓ inquiry sent · ${esc(fmtTime(done.sent_at))}${done.mode==="mock"?" (demo)":""}</div>`;
    else if (!LIVE) action = "";
    else if (!canSend) action = `<div class="no-email">no email found — send manually</div>`;
    else action = `<div class="send-wrap"><button class="btn small" data-send="${esc(r.url)}">Send inquiry</button></div>`;
    const href = safeUrl(r.url);
    const nameHtml = href
      ? `<a class="name vlink" href="${esc(href)}" target="_blank" rel="noopener noreferrer"
            title="Open ${esc(r.name)} in a new tab">${esc(r.name)}<span class="ext">↗</span></a>`
      : `<span class="name">${esc(r.name)}</span>`;
    return `<div class="venue" data-name="${esc(r.name)}" tabindex="0">
      <div class="row1"><span class="rank">${String(i+1).padStart(2,"0")}</span>
        ${nameHtml}
        <span class="region">${esc(r.region)}</span></div>
      <div class="why">${esc(s.rationale)}</div>
      <div class="bar"><i style="width:${Math.round(s.score*100)}%"></i></div>
      <div class="price">${esc(price)}</div>
      ${action}
    </div>`;
  }).join("");
  document.querySelectorAll(".venue").forEach(el=>{
    const go=e=>{ if(e.target.closest("[data-send]") || e.target.closest("a")) return; focusVenue(el.dataset.name); };
    el.addEventListener("click",go);
    el.addEventListener("keydown",e=>{if(e.key==="Enter")go(e);});
  });
  document.querySelectorAll("[data-send]").forEach(b=>
    b.addEventListener("click", e=>{ e.stopPropagation(); openDraft(b.dataset.send, b); }));
}

const fmtTime = ts => { try { return new Date(ts).toLocaleString(); } catch(e){ return ts; } };

/* ---- Escalations + telemetry ---- */
/* Which escalation bucket is on screen, and whether the list is expanded.
   Module state rather than a re-derived value so a re-render (a send, a
   /report refresh) does not silently throw away the filter you picked. */
let ESC_FILTER = "all";
let ESC_EXPANDED = false;
const ESC_PREVIEW = 8;

function renderTelemetry(){
  const s = DATA.summary, all = DATA.escalations||[];
  const filters = DATA.escalation_filters||[];

  /* A tagless payload means an older report.json (generated before grouping
     existed). Show the plain list rather than an empty one. */
  const groupable = filters.length && all.some(e=>e.tags);
  const shown = (!groupable || ESC_FILTER==="all")
    ? all : all.filter(e=>(e.tags||[]).includes(ESC_FILTER));
  const visible = ESC_EXPANDED ? shown : shown.slice(0, ESC_PREVIEW);

  const chips = groupable ? `<div class="chips">
    <button class="chip${ESC_FILTER==="all"?" on":""}" data-esc="all">All<span class="n">${all.length}</span></button>
    ${filters.map(f=>`<button class="chip${ESC_FILTER===f.key?" on":""}" data-esc="${esc(f.key)}"
        >${esc(f.label)}<span class="n">${f.n}</span></button>`).join("")}
  </div>` : "";

  const rows = visible.map(e=>{
    const href = safeUrl(e.url);
    /* The point of an escalation is that a human goes and reads the page, so
       the name is the link to it. */
    const nm = href
      ? `<a class="en vlink" href="${esc(href)}" target="_blank" rel="noopener noreferrer">${esc(e.name)}<span class="ext">↗</span></a>`
      : `<span class="en">${esc(e.name)}</span>`;
    return `<div class="esc"><span class="mk">▲</span><div>${nm}<br>
      <span class="er">${esc(e.reason)}</span></div></div>`;
  }).join("");

  const hidden = shown.length - visible.length;
  const moreBtn = hidden > 0
    ? `<button class="more" data-esc-more="1">show ${hidden} more</button>`
    : (ESC_EXPANDED && shown.length > ESC_PREVIEW
        ? `<button class="more" data-esc-more="0">show fewer</button>` : "");

  const escHtml = all.length
    ? chips + (shown.length
        ? rows + moreBtn
        : `<p style="color:var(--muted);font-size:12px">No escalations in this group.</p>`)
    : `<p style="color:var(--muted);font-size:12px">Nothing flagged for human review.</p>`;
  const cpv = s.cost_per_venue_usd, tot = s.cost.usd_total;
  $("#telemetry").innerHTML = `
    <div style="font-size:10.5px;letter-spacing:.16em;text-transform:uppercase;color:var(--muted);margin-bottom:2px">Escalated to a human</div>
    ${escHtml}
    <div class="tele">
      <div class="stat"><div class="n">${s.venues_evaluated}</div><div class="l">evaluated</div></div>
      <div class="stat"><div class="n">${s.recommended}</div><div class="l">recommend</div></div>
      <div class="stat"><div class="n">${s.escalated}</div><div class="l">escalated</div></div>
      <div class="stat"><div class="n">${s.steps}</div><div class="l">agent steps</div></div>
      <div class="stat"><div class="n">$${cpv}</div><div class="l">per venue</div></div>
      <div class="stat"><div class="n">$${tot}</div><div class="l">run total</div></div>
    </div>
    ${emptyRegionsNote(s)}
    <div class="foot">Emails are <b>drafted, not sent</b> — a human reviews and sends each one.
      Venues with missing information are <b>escalated, not guessed</b>.</div>`;
  /* "N steps" links to the run walkthrough. Only when a server is behind the
     page -- opened off disk there is nothing to serve /steps. */
  /* Prefer the phase count over the raw model-call counter -- "11 steps" is the
     workflow, "266 steps" is how hard it worked. Falls back to s.steps when the
     page is opened off disk, where there is no /steps to derive phases from. */
  const nSteps = s.overview_steps ?? s.steps;
  $("#statusCost").innerHTML = LIVE
    ? `$${cpv}/venue · <a class="steps-link" href="/steps">${nSteps} steps</a>`
    : `$${cpv}/venue · ${nSteps} steps`;

  /* Mock runs replay fixtures; they do not search. Saying so in the header is
     the difference between "only 3 venues match" and "only 3 venues exist in
     the offline fixture set" -- so the mock warning stays even though the "live"
     badge and the "run complete" status were removed as noise. A real run needs
     no badge; a fixture replay that looks like a real one is a wrong answer. */
  $("#statusMode").innerHTML = s.mode === "mock"
    ? `<span class="mode" title="Offline fixtures — no live search. Set AGENT_MODE=real to search for real.">mock data</span>`
    : ``;

  /* Bound after the innerHTML above -- these nodes are recreated every render,
     so a listener attached once at boot would be discarded by the first one. */
  $("#telemetry").querySelectorAll("[data-esc]").forEach(b=>{
    b.addEventListener("click", ()=>{
      ESC_FILTER = b.dataset.esc;
      ESC_EXPANDED = false;      // a new group starts at the top, not mid-list
      renderTelemetry();
    });
  });
  $("#telemetry").querySelectorAll("[data-esc-more]").forEach(b=>{
    b.addEventListener("click", ()=>{
      ESC_EXPANDED = b.dataset.escMore === "1";
      renderTelemetry();
    });
  });
}

/* A region that returned nothing is a finding, not an absence. Without this the
   run reads as "we searched six regions and found three venues". */
function emptyRegionsNote(s){
  const empty = s.empty_regions || [];
  if (!empty.length) return "";
  const why = s.mode === "mock"
    ? `no offline fixture exists for ${empty.length > 1 ? "them" : "it"}. Mock mode replays a fixture file rather than searching — run with <b>AGENT_MODE=real</b> to search these for real.`
    : `search returned no results for ${empty.length > 1 ? "them" : "it"}.`;
  return `<div class="empty-note"><b>${empty.length} region${empty.length > 1 ? "s" : ""} returned nothing:</b>
    ${empty.map(esc).join(" · ")} — ${why}</div>`;
}

/* ---- Cross-highlight ---- */
function focusVenue(name){
  const p = DATA.points.find(x=>x.name===name);
  if(p) world.pointOfView({lat:p.lat, lng:p.lng, altitude:1.5}, 900);
  document.querySelectorAll(".venue").forEach(el=>
    el.classList.toggle("active", el.dataset.name===name));
}

/* ---- Send: draft -> confirm -> send ----
   Three deliberate steps for one venue. The recipient is displayed, never
   edited: it comes from the venue record and the server refuses anything else.
   There is no "send all" and never will be. */
async function openDraft(url, btn){
  const wrap = btn.closest(".send-wrap");
  wrap.innerHTML = `<div class="draft-note">loading draft…</div>`;
  let d;
  try { d = await (await fetch("/draft?url=" + encodeURIComponent(url))).json(); }
  catch(e){ wrap.innerHTML = `<div class="msg err">Could not load the draft.</div>`; return; }

  if (!d.to){ wrap.innerHTML = `<div class="no-email">no email found — send manually</div>`; return; }

  wrap.innerHTML = `
    <div class="draft">
      <div class="draft-to">To <b>${esc(d.to)}</b> <span class="fixed">from the venue's page — not editable</span></div>
      <label>Subject</label><input class="d-subj" type="text">
      <label>Message</label><textarea class="d-body" rows="7"></textarea>
      <div class="crit-actions">
        <button class="btn small d-review">Review &amp; send</button>
        <button class="btn small ghost d-cancel">Cancel</button>
      </div>
      <div class="d-msg"></div>
    </div>`;
  wrap.querySelector(".d-subj").value = d.subject || "";
  wrap.querySelector(".d-body").value = d.body || "";
  if (!d.has_draft) wrap.querySelector(".d-msg").innerHTML =
    `<div class="msg warn">No draft was generated for this venue — write one before sending.</div>`;

  wrap.querySelector(".d-cancel").onclick = () => renderRecommended();
  wrap.querySelector(".d-review").onclick = () => confirmSend(url, d.to, wrap);
}

function confirmSend(url, to, wrap){
  const subject = wrap.querySelector(".d-subj").value.trim();
  const body = wrap.querySelector(".d-body").value.trim();
  const box = wrap.querySelector(".d-msg");
  if (!subject || !body){
    box.innerHTML = `<div class="msg err">Subject and message are both required.</div>`; return;
  }
  /* Final review: exactly what will be sent, nothing editable. */
  wrap.innerHTML = `
    <div class="draft confirm">
      <div class="draft-note">Send this inquiry? This cannot be undone.</div>
      <dl class="kv">
        <dt>To</dt><dd>${esc(to)}</dd>
        <dt>Subject</dt><dd>${esc(subject)}</dd>
      </dl>
      <div class="final-body">${esc(body)}</div>
      <div class="crit-actions">
        <button class="btn small c-send">Confirm send</button>
        <button class="btn small ghost c-back">Back</button>
      </div>
      <div class="d-msg"></div>
    </div>`;
  wrap.querySelector(".c-back").onclick = () => openDraft(url, wrap.querySelector(".c-back"));
  wrap.querySelector(".c-send").onclick = async (e) => {
    const btns = wrap.querySelectorAll("button");
    btns.forEach(b => b.disabled = true);
    const msg = wrap.querySelector(".d-msg");
    msg.innerHTML = `<div class="msg ok">Sending…</div>`;
    try {
      const res = await fetch("/send", {
        method:"POST", headers:{"Content-Type":"application/json"},
        body: JSON.stringify({url, to, subject, body}),
      });
      const j = await res.json();
      if (!res.ok || !j.sent){
        msg.innerHTML = `<div class="msg err">${esc(j.error || j.detail || "Send refused.")}</div>`;
        btns.forEach(b => b.disabled = false);
        return;
      }
      DATA.sent = DATA.sent || {};
      DATA.sent[url] = j;
      renderRecommended();
    } catch (err) {
      msg.innerHTML = `<div class="msg err">Could not reach the server.</div>`;
      btns.forEach(b => b.disabled = false);
    }
  };
}

/* ---- Render everything from the current DATA ---- */
function renderAll(){
  world.pointsData(DATA.points).arcsData(DATA.arcs);
  renderCriteria();
  renderRecommended();
  renderTelemetry();
}

/* ---- Boot: prefer live server data, fall back to the inlined snapshot ---- */
(async function boot(){
  if (location.protocol === "http:" || location.protocol === "https:"){
    try {
      const r = await fetch("/report", {headers:{"Accept":"application/json"}});
      if (r.ok){ DATA = await r.json(); LIVE = true; }
      else if (r.status === 404){ LIVE = true; }   // server is up, just no run yet
    } catch(e){ /* no server: the inlined snapshot stands */ }
  }
  if (LIVE) await window.loadCriteriaMeta();
  renderAll();
  /* A run may already be going -- started from the terminal, or from this
     button before a reload. Attach the bar to it rather than showing a stale
     report with no sign anything is happening. */
  if (LIVE){
    try {
      const p = await (await fetch("/progress")).json();
      if (p.running){
        /* Query the document, NOT the criteria module's `CRIT` -- that is a
           const inside its own IIFE and is not in scope here. Referencing it
           threw a ReferenceError that the catch below swallowed, so a run in
           progress silently rendered no bar at all. */
        document.querySelectorAll("#criteria button").forEach(b => b.disabled = true);
        window.trackProgress();
      }
    } catch (e) {
      /* Never swallow this silently again: an empty catch here hid the bug
         above completely -- no bar, no error, nothing to go on. */
      console.error("could not attach to the run in progress:", e);
    }
  }
})();
</script>
</body>
</html>
"""
