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

# Static geocode for the demo regions. Extend as criteria grow.
REGION_COORDS = {
    "Tulum, Mexico": (20.2114, -87.4654),
    "Cabo San Lucas, Mexico": (22.8905, -109.9167),
    "Sedona, Arizona": (34.8697, -111.7610),
}
HOME = {"name": "Home base", "lat": 27.9506, "lng": -82.4572}  # Tampa, FL

DECISION_COLOR = {
    "recommend": "#5FB3A3",   # sea teal
    "escalate": "#E8B04B",    # champagne amber
    "reject": "#C97A82",      # muted rose
}


def _jitter(url: str) -> tuple[float, float]:
    """Deterministic small offset so venues in the same region don't overlap."""
    h = int(hashlib.md5(url.encode()).hexdigest(), 16)
    dlat = ((h % 1000) / 1000 - 0.5) * 1.4
    dlng = (((h // 1000) % 1000) / 1000 - 0.5) * 1.4
    return dlat, dlng


def _points(report: dict) -> list[dict]:
    pts = []
    for s in report["all_scored"]:
        rec = s["record"]
        base = REGION_COORDS.get(rec["region"])
        if not base:
            continue
        dlat, dlng = _jitter(rec["url"])
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


def render(report: dict, criteria: dict, out_path: str) -> str:
    data = {
        "criteria": criteria,
        "summary": report["summary"],
        "recommended": report["recommended"],
        "escalations": report["escalations"],
        "points": _points(report),
        "arcs": _arcs(report),
        "home": HOME,
        "decisionColor": DECISION_COLOR,
    }
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
<title>Venue Scouting — Command Center</title>
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
  header .title{font-family:"Fraunces",serif; font-weight:900; font-size:22px; letter-spacing:.3px}
  header .title em{color:var(--gold); font-style:italic; font-weight:600}
  header .sub{color:var(--muted); font-size:11px; letter-spacing:.16em; text-transform:uppercase}
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

  /* recommended list */
  .venue{padding:11px 0; border-bottom:1px solid var(--line2); cursor:pointer; transition:background .15s}
  .venue:hover,.venue.active{background:rgba(232,195,158,.05)}
  .venue .row1{display:flex; align-items:baseline; gap:8px}
  .venue .rank{font-family:"Fraunces",serif; font-style:italic; color:var(--gold-dim); font-size:13px; width:22px}
  .venue .name{font-size:13.5px; color:var(--text)}
  .venue .region{margin-left:auto; font-size:10.5px; color:var(--muted2); letter-spacing:.08em; text-transform:uppercase}
  .venue .why{font-size:11.5px; color:var(--muted); margin:5px 0 6px 30px; line-height:1.45}
  .bar{height:3px; background:var(--line2); border-radius:2px; margin-left:30px; overflow:hidden}
  .bar > i{display:block; height:100%; background:linear-gradient(90deg,var(--gold-dim),var(--gold))}
  .price{font-size:10.5px; color:var(--muted2); margin-left:30px}

  /* escalations + telemetry */
  .esc{display:flex; gap:9px; padding:8px 0; border-bottom:1px solid var(--line2); font-size:12px}
  .esc .mk{color:var(--amber); flex:0 0 auto}
  .esc .en{color:var(--text)} .esc .er{color:var(--muted); font-size:11px}
  .tele{display:grid; grid-template-columns:repeat(3,1fr); gap:10px; margin-top:14px}
  .stat{border:1px solid var(--line2); border-radius:8px; padding:10px 12px}
  .stat .n{font-family:"Fraunces",serif; font-size:24px; color:var(--gold)}
  .stat .l{font-size:9.5px; letter-spacing:.16em; text-transform:uppercase; color:var(--muted); margin-top:2px}
  .foot{margin-top:12px; font-size:11px; color:var(--muted2); line-height:1.5}
  .foot b{color:var(--gold-dim); font-weight:500}

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
    <div class="title">Venue Scouting <em>Command Center</em></div>
    <div class="sub">destination wedding · agent run</div>
    <div class="status">
      <span id="statusRun"><span class="dot"></span>run complete</span>
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
const DATA = /*__DATA__*/;
const $ = s => document.querySelector(s);
const esc = s => String(s==null?"":s).replace(/[&<>]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));

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

/* ---- Criteria ---- */
(function(){
  const c = DATA.criteria;
  const must = (c.must_haves||[]).map(m=>`<span class="chip">${esc(m)}</span>`).join("");
  const nice = (c.nice_to_haves||[]).map(m=>`<span class="chip soft">${esc(m)}</span>`).join("");
  $("#criteria").innerHTML = `
    <div class="crit-hero">An intimate wedding for <span>${esc(c.guest_count)}</span> guests, ${esc(c.date_window||"")}, under <span>$${Number(c.budget_ceiling_usd).toLocaleString()}</span>.</div>
    <dl class="kv">
      <dt>Regions</dt><dd>${(c.regions||[]).map(esc).join(" · ")}</dd>
      <dt>Vibe</dt><dd>${esc(c.vibe||"—")}</dd>
    </dl>
    <div style="margin-top:14px;font-size:10.5px;letter-spacing:.16em;text-transform:uppercase;color:var(--muted)">Must have</div>
    <div class="chips">${must}</div>
    <div style="margin-top:12px;font-size:10.5px;letter-spacing:.16em;text-transform:uppercase;color:var(--muted)">Nice to have</div>
    <div class="chips">${nice}</div>`;
})();

/* ---- Recommended ---- */
(function(){
  const rec = DATA.recommended || [];
  if(!rec.length){ $("#recommended").innerHTML = `<p style="color:var(--muted);font-size:12px">No venues cleared every must-have. See escalations.</p>`; return; }
  $("#recommended").innerHTML = rec.map((s,i)=>{
    const r=s.record, price = r.pricing_signal==="listed"
      ? `$${Number(r.price_low_usd).toLocaleString()} (listed)`
      : r.pricing_signal==="request_only" ? "price by request" : "price not stated";
    return `<div class="venue" data-name="${esc(r.name)}" tabindex="0">
      <div class="row1"><span class="rank">${String(i+1).padStart(2,"0")}</span>
        <span class="name">${esc(r.name)}</span>
        <span class="region">${esc(r.region)}</span></div>
      <div class="why">${esc(s.rationale)}</div>
      <div class="bar"><i style="width:${Math.round(s.score*100)}%"></i></div>
      <div class="price">${esc(price)}</div>
    </div>`;
  }).join("");
  document.querySelectorAll(".venue").forEach(el=>{
    const go=()=>focusVenue(el.dataset.name);
    el.addEventListener("click",go);
    el.addEventListener("keydown",e=>{if(e.key==="Enter")go();});
  });
})();

/* ---- Escalations + telemetry ---- */
(function(){
  const s = DATA.summary, escs = DATA.escalations||[];
  const escHtml = escs.length ? escs.map(e=>`
    <div class="esc"><span class="mk">▲</span><div><span class="en">${esc(e.name)}</span><br>
      <span class="er">${esc(e.reason)}</span></div></div>`).join("")
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
    <div class="foot">Emails are <b>drafted, not sent</b> — a human reviews and sends each one.
      Venues with missing information are <b>escalated, not guessed</b>.</div>`;
  $("#statusCost").textContent = `$${cpv}/venue · ${s.steps} steps`;
})();

/* ---- Cross-highlight ---- */
function focusVenue(name){
  const p = DATA.points.find(x=>x.name===name);
  if(p) world.pointOfView({lat:p.lat, lng:p.lng, altitude:1.5}, 900);
  document.querySelectorAll(".venue").forEach(el=>
    el.classList.toggle("active", el.dataset.name===name));
}
</script>
</body>
</html>
"""
