"""
Local criteria server -- the write path for the command center.

    pip install -r requirements.txt      # includes the optional server extras
    python server.py                     # http://localhost:8420
    # or: uvicorn server:app --host localhost --port 8420

Endpoints:

    GET  /             -> the existing dashboard (out/dashboard.html), unchanged
    GET  /report       -> the current run's data, in exactly the shape the page
                          renders from, plus which venues have been sent to
    GET  /criteria     -> current criteria.json + the regions the globe can
                          plot + the validator's limits
    POST /criteria     -> validate and save. Does NOT run the agent.
    POST /run          -> run the agent on the saved criteria, return the fresh
                          report in that same render shape. Blocks (~a minute
                          in real mode); the UI shows a running state.
    POST /send         -> send ONE venue's reviewed draft, after re-validating
                          the recipient against the stored venue record
    GET  /auth/status  -> is Gmail connected (and in which mode)

WHAT THIS IS NOT. There is no auth: the only thing standing between a caller
and your criteria.json is that the socket is bound to 127.0.0.1. That is
adequate for a single-user tool on a laptop and nothing more. Do not put this
on a network interface. See the README for what a production version needs.

SENDING. /send delivers exactly one inquiry, for one venue, from one explicit
click that the user confirmed on a screen showing the final recipient, subject
and body. There is no batch endpoint, no scheduled send, and no way to ask the
agent to send anything; `auto_send` remains unimplemented. The recipient is
re-derived from the stored venue record and the submitted address must match it
-- see src/sending.py. In mock mode (the default) a send writes to
runs/<id>/drafts/ and touches no network.

RUNS are serialized under a lock with a timeout. There is no job queue; a
second request while one is running is refused rather than queued.

The agent never imports this module. Mock mode, the evals, and `python main.py`
all work with FastAPI uninstalled and the server stopped.
"""

from __future__ import annotations

import glob
import importlib
import json
import os
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit

ROOT = os.path.dirname(os.path.abspath(__file__))
CRITERIA_PATH = os.path.join(ROOT, "criteria.json")
DASHBOARD_PATH = os.path.join(ROOT, "out", "dashboard.html")
RUNS_DIR = os.path.join(ROOT, "runs")
# A committed snapshot of one real run, used only when runs/ is empty (a fresh
# deploy). Contact emails are stripped from it -- see demo/README.md.
DEMO_DIR = os.path.join(ROOT, "demo")

# --- read-only demo mode --------------------------------------------------
# This server has no authentication. On a laptop that is fine, because the
# socket is the boundary: only this machine can reach it. A public URL removes
# that boundary entirely, and the three POST routes are exactly the ones you
# would not want a stranger to have -- /run spends real API credit for minutes
# at a time, /send emails a real venue, /criteria rewrites the agent's input.
#
# So a public deployment runs READ-ONLY. Every GET works (that is the whole
# demo: the dashboard, the run walkthrough, the prompts); every POST returns
# 403. This is enforced here rather than by hiding buttons, because a hidden
# button is not a control -- anyone can still post to the route.
# Railway (and most PaaS) inject $PORT and expect a routable bind.
_PLATFORM_PORT = os.environ.get("PORT")
HOST = os.environ.get("CRITERIA_SERVER_HOST") or ("0.0.0.0" if _PLATFORM_PORT else "localhost")
PORT = int(_PLATFORM_PORT or os.environ.get("CRITERIA_SERVER_PORT", "8420"))

_LOOPBACK = HOST in ("localhost", "127.0.0.1", "::1")
_RO = os.environ.get("DEMO_READONLY", "").strip().lower()

# The default follows the BIND, not a flag someone has to remember.
#
# The first version required DEMO_READONLY explicitly and refused to start
# without it. That is safe but it is the wrong default: forgetting one
# environment variable produced a container that crash-looped, so the failure
# mode of a mistake was "the whole site is down". Defaulting on a routable bind
# means the mistake now produces a working, read-only site instead -- still
# nothing a stranger can POST to, but people can see it.
#
# Opting OUT is still possible and still explicit (DEMO_READONLY=0), and that
# is the one combination that refuses to start, because it is the only one that
# actually publishes a run button.
if _RO in ("1", "true", "yes"):
    DEMO_READONLY = True
elif _RO in ("0", "false", "no"):
    DEMO_READONLY = False
else:
    DEMO_READONLY = not _LOOPBACK

if not _LOOPBACK and not DEMO_READONLY:
    sys.exit(
        f"Refusing to start: bound to {HOST} with DEMO_READONLY={_RO!r}.\n"
        "This server has no authentication, so a routable bind with writes\n"
        "enabled would expose POST /run (spends API credit), POST /send\n"
        "(emails a venue) and POST /criteria to anyone who finds the URL.\n\n"
        "  Public demo:  unset DEMO_READONLY (read-only is the default here)\n"
        "  Local use:    unset CRITERIA_SERVER_HOST (defaults to localhost)\n"
    )
# Real-mode runs are long, and deliberately unbounded in breadth: search is
# uncapped per region and every venue found is evaluated. One live region
# returned 25 venues, so a four-country run can be ~100 venues -- search plus
# a page fetch and ~4 model calls each. This is a "did something wedge"
# ceiling, not a workload budget.
RUN_TIMEOUT_SECONDS = int(os.environ.get("RUN_TIMEOUT_SECONDS", "7200"))

# Any of these as an Origin means the request came from a page on this machine.
# Port-independent on purpose -- see the local_only middleware.
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}

try:
    from fastapi import Body, FastAPI, HTTPException, Request
    from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
except ModuleNotFoundError:  # pragma: no cover - dependency guidance
    sys.exit(
        "This server needs FastAPI, which the agent itself does not.\n"
        "  pip install -r requirements.txt\n"
        "The agent and evals run fine without it: python main.py --criteria criteria.json"
    )

sys.path.insert(0, ROOT)
from src import config, criteria_io, dashboard, prompts, sending, tools  # noqa: E402

app = FastAPI(title="Wedding Venue Agent — criteria", docs_url=None, redoc_url=None)

_run_lock = threading.Lock()


# --- guardrail: keep this thing local -------------------------------------

@app.middleware("http")
async def local_only(request: Request, call_next):
    """Refuse anything that did not come from this machine.

    The bind address already limits who can connect. This additionally blocks
    DNS-rebinding style requests, where a page on the open internet resolves a
    hostname to 127.0.0.1 and posts to it from the victim's own browser -- the
    bind address does not help there, but the Origin header does.
    """
    origin = request.headers.get("origin")
    if origin is not None:
        # Match on the origin's HOST, not on a hardcoded port. The port is a
        # runtime choice (uvicorn --port, $CRITERIA_SERVER_PORT), and pinning it
        # here meant serving on any other port 403'd every write from the page
        # the server itself had just served.
        #
        # Host-based is also the check that actually expresses the rule: a
        # browser sets Origin itself and a page on the public internet cannot
        # forge a loopback one, so "origin is loopback" means "this came from a
        # page on this machine".
        try:
            host = urlsplit(origin).hostname
        except ValueError:
            host = None
        # Same-origin is always fine: on a deployed host the page the server
        # itself served has that host in its Origin, not a loopback one, and
        # refusing it would 403 the site's own fetches. Comparing against the
        # request's own Host header keeps the anti-rebinding property -- a page
        # on another domain still cannot forge this.
        same_origin = host is not None and host == urlsplit(f"//{request.headers.get('host','')}").hostname
        if host not in LOOPBACK_HOSTS and not same_origin:
            return JSONResponse(
                {"detail": f"cross-origin request refused (origin {origin})"},
                status_code=403,
            )

    # Read-only demo: the socket is no longer the boundary, so the method is.
    if DEMO_READONLY and request.method not in ("GET", "HEAD", "OPTIONS"):
        return JSONResponse(
            {"detail": "This is a read-only demo. Running the agent, editing "
                       "criteria and sending inquiries are disabled here — "
                       "clone the repo to use them."},
            status_code=403,
        )
    return await call_next(request)


# --- read -----------------------------------------------------------------

@app.get("/")
def serve_dashboard():   # not `dashboard` -- that name is the imported module
    """Re-render the page from the CURRENT dashboard.py before serving it.

    out/dashboard.html is written once, at the end of a run, by whatever
    dashboard.py that run's process imported when it started. So a UI change
    made while a run is in flight is overwritten by the run's own older code
    the moment it finishes -- the server restarts, the endpoints are new, and
    the page is still stale with no way to tell from the browser.

    The file is a shell whose data is refetched from /report anyway, so
    rebuilding it per request costs one render and makes "reload the page" mean
    what everyone assumes it means. It stays a real file on disk (rather than a
    string) so out/dashboard.html remains openable offline.
    """
    try:
        _reload_dashboard_if_edited()
        run_dir = latest_run_dir()
        report_path = os.path.join(run_dir or "", "report.json")
        if run_dir and os.path.exists(report_path):
            with open(report_path) as f:
                report = json.load(f)
            dashboard.render(report, criteria_io.load(CRITERIA_PATH), DASHBOARD_PATH)
    except Exception:   # noqa: BLE001 - a stale page beats a 500 on the landing page
        pass

    if not os.path.exists(DASHBOARD_PATH):
        return PlainTextResponse(
            "No dashboard yet. Generate one first:\n\n"
            "    python main.py --criteria criteria.json\n",
            status_code=404,
        )
    return FileResponse(DASHBOARD_PATH, media_type="text/html")


@app.get("/criteria")
def get_criteria():
    """Current criteria, plus everything the UI needs to validate the same way."""
    try:
        current = criteria_io.load(CRITERIA_PATH)
    except FileNotFoundError:
        raise HTTPException(404, f"criteria.json not found at {CRITERIA_PATH}")
    except ValueError as e:
        raise HTTPException(500, f"criteria.json is not valid JSON: {e}")

    return {
        "criteria": current,
        "known_regions": criteria_io.known_regions(),
        "limits": criteria_io.LIMITS,
        "allowed_keys": sorted(criteria_io.ALLOWED_KEYS),
    }


# --- write ----------------------------------------------------------------

@app.post("/criteria")
def post_criteria(payload: dict = Body(...)):
    """Validate and save criteria. Does not run the agent -- that is /run.

    Validation failures return 422 with EVERY problem found, so the editor can
    mark all the bad fields at once rather than one per round trip.
    """
    if not isinstance(payload, dict):
        raise HTTPException(400, "body must be a JSON object")

    submitted = payload.get("criteria", payload)

    try:
        clean, warnings = criteria_io.validate(submitted)
    except criteria_io.CriteriaError as e:
        return JSONResponse({"saved": False, "errors": e.errors}, status_code=422)

    try:
        criteria_io.save(clean, CRITERIA_PATH)
    except OSError as e:
        raise HTTPException(500, f"could not write criteria.json: {e}")

    return {
        "saved": True,
        "criteria": clean,
        "warnings": warnings,
        "path": CRITERIA_PATH,
    }


_dashboard_mtime = 0.0


def _reload_dashboard_if_edited() -> None:
    """Re-import dashboard.py when the file on disk has changed.

    Re-rendering the page per request is not enough on its own: render() runs
    off the module imported at server start, so an edit to the page still
    needed a restart to appear, which is the same staleness in a different
    place. The page markup, CSS and JS all live in that one module, so
    reloading it is what makes "edit, reload the browser" work.

    Only the presentation module is reloaded. config/criteria_io/sending hold
    state and validation rules the running process is relying on.
    """
    global _dashboard_mtime
    try:
        mtime = os.path.getmtime(dashboard.__file__)
    except OSError:
        return
    if mtime > _dashboard_mtime:
        if _dashboard_mtime:            # skip the import at startup
            importlib.reload(dashboard)
        _dashboard_mtime = mtime


# --- run ------------------------------------------------------------------

def latest_run_dir(*, finished_only: bool = True) -> str | None:
    """Newest run directory.

    `finished_only` skips a run that is still in flight. A running agent creates
    its directory immediately but writes report.json only at the end, so naively
    taking the newest directory made /report 404 for the entire duration of a
    run -- and the dashboard, seeing a 404, silently fell back to the stale
    snapshot inlined at generation time. The page then showed a *previous* run's
    numbers with no indication anything was happening.
    """
    runs = sorted(glob.glob(os.path.join(RUNS_DIR, "run_*")))
    if not finished_only:
        return runs[-1] if runs else None
    for d in reversed(runs):
        if os.path.exists(os.path.join(d, "report.json")):
            return d
    # Nothing has run here. On a deployed demo that is the normal state --
    # runs/ is git-ignored, so a fresh container has no history at all and the
    # site would render "no dashboard yet". demo/ is a committed snapshot of a
    # real run (contact emails stripped) so the public URL shows real results
    # instead of an empty shell.
    if os.path.exists(os.path.join(DEMO_DIR, "report.json")):
        return DEMO_DIR
    return None


# How long a run may go without writing an audit event before it is presumed
# dead. A run that is killed -- the server restarted, the terminal closed,
# ctrl-C -- leaves a directory with no report.json behind, and "newest run has
# no report" would call that run in flight forever: the progress bar would
# reappear on every page load and sit at whatever percent it died at, with no
# way to clear it short of deleting the directory.
#
# Generous on purpose. A region search is minutes of silence with nothing
# written (one live region took 11 minutes), so anything tight would declare a
# healthy run dead mid-search. Thirty minutes is longer than any single silent
# phase and still bounded.
STALE_RUN_SECONDS = 1800


def run_in_progress() -> str | None:
    """The in-flight run directory, if the newest one has no report yet."""
    newest = latest_run_dir(finished_only=False)
    if not newest or os.path.exists(os.path.join(newest, "report.json")):
        return None

    trail = os.path.join(newest, "audit_trail.jsonl")
    last_write = os.path.getmtime(trail) if os.path.exists(trail) else os.path.getmtime(newest)
    if time.time() - last_write > STALE_RUN_SECONDS:
        return None                      # abandoned, not running
    return newest


def _report_payload(run_dir: str | None):
    """The render-shape payload for a run, or None if there is not one yet."""
    if not run_dir:
        return None
    report_path = os.path.join(run_dir, "report.json")
    if not os.path.exists(report_path):
        return None
    with open(report_path) as f:
        report = json.load(f)
    criteria = criteria_io.load(CRITERIA_PATH)
    data = dashboard.build_data(report, criteria, sending.read_sent_log())
    data["run_id"] = os.path.basename(run_dir)
    # The page uses this to drop the write controls. The middleware is what
    # actually enforces read-only; this just stops the demo offering buttons
    # that would 403.
    data["readonly"] = DEMO_READONLY

    # Headline the workflow, not the model-call counter. summary.steps stays as
    # it was (the agent's own count, still in the report and the audit trail);
    # the dashboard prefers this when present.
    trail = os.path.join(run_dir, "audit_trail.jsonl")
    if os.path.exists(trail):
        try:
            with open(trail) as f:
                ev = [json.loads(l) for l in f if l.strip()]
            data["summary"]["overview_steps"] = len(overview(ev))
        except (OSError, ValueError):
            pass
    return data


# --- steps -----------------------------------------------------------------
# What each audit event means, in the couple's language rather than the code's.
# The audit trail already records every step; this only translates it.
STEP_MEANINGS = {
    "run_started":            ("Run started", "Criteria loaded and validated; mode and the runaway-step ceiling recorded."),
    "resumed_from_checkpoint":("Resumed", "Picked up from a saved checkpoint instead of starting over."),
    "region_search":          ("Searched a region", "Asked the web for candidate venues in one region."),
    "search_failed":          ("Search failed", "That region could not be searched; the run continued without it."),
    "skip_seen":              ("Skipped — already seen", "This exact URL was already evaluated."),
    "duplicate_skipped":      ("Collapsed a duplicate", "Same venue under another domain or region; evaluated once."),
    "model_call":             ("Asked the model", "One call: classify, extract, score, or draft."),
    "validation_failed":      ("Model output rejected", "The reply did not match the schema — retried with the error."),
    "filtered_non_venue":     ("Filtered out", "A listicle, planner, or vendor page rather than a bookable venue."),
    "fetch_failed":           ("Page unreachable", "The venue's site could not be loaded; escalated to a human."),
    "partial_page":           ("Page had no content", "A live URL that rendered nothing usable; escalated without spending an extraction call."),
    "extract_failed":         ("Extraction failed", "Output stayed invalid after a retry; escalated rather than guessed."),
    "grounding_failed":       ("Unsupported claim stripped", "The record asserted something the page did not say; the claim was removed and the venue escalated."),
    "scored":                 ("Scored", "Checked against the must-haves, with confidence capped by the evidence."),
    "score_error":            ("Scoring failed", "Escalated to a human."),
    "email_drafted":          ("Drafted an inquiry", "Written to disk for review. Never sent by the agent."),
    "email_error":            ("Draft failed", "No inquiry was written for this venue."),
    "checkpoint_saved":       ("Checkpoint saved", "Run state written so it can resume after an interruption."),
    "halted_max_steps":       ("Halted — step ceiling", "The runaway guard tripped."),
    "halted_spend_cap":       ("Halted — spend ceiling", "A cost ceiling was reached."),
    "region_failed":          ("Region failed", "That region errored out; the rest of the run continued."),
    "run_finished":           ("Run finished", "Report, shortlist, and dashboard written."),
}

_DETAIL_KEYS = ("region", "n_results", "url", "name", "tag", "attempt", "decision",
                "confidence", "reason", "error", "flags", "duplicate_of", "mode",
                "max_steps", "classification", "to", "chars")


@app.get("/steps")
def get_steps(run: str | None = None):
    """A plain-language walkthrough of every step in a run.

    Reads the same audit_trail.jsonl the run wrote -- this page invents nothing
    and adds no instrumentation; it is the audit trail, made readable.
    """
    # Default to the newest run that HAS a trail, finished or not. Preferring
    # finished runs here would show the last completed run while the one you
    # just started -- the one you actually want to watch -- is ignored.
    if run:
        run_dir = os.path.join(RUNS_DIR, run)
    else:
        run_dir = next(
            (d for d in sorted(glob.glob(os.path.join(RUNS_DIR, "run_*")), reverse=True)
             if os.path.exists(os.path.join(d, "audit_trail.jsonl"))),
            None,
        )
        # Same fallback as latest_run_dir(): a fresh deploy has no runs/.
        if run_dir is None and os.path.exists(os.path.join(DEMO_DIR, "audit_trail.jsonl")):
            run_dir = DEMO_DIR
    if not run_dir or not os.path.isdir(run_dir):
        return PlainTextResponse("No run to show yet.", status_code=404)

    trail = os.path.join(run_dir, "audit_trail.jsonl")
    if not os.path.exists(trail):
        return PlainTextResponse("That run has no audit trail.", status_code=404)

    events = []
    with open(trail) as f:
        for line in f:
            try:
                events.append(json.loads(line))
            except ValueError:
                continue

    running = run_in_progress() == run_dir
    mode = next((e.get("mode") for e in events if e["event"] == "run_started"), "?")
    return HTMLResponse(_steps_html(os.path.basename(run_dir), mode, running, events))


def overview(events: list) -> list[dict]:
    """The run as a handful of phases, not a log.

    "266 steps" is the model-call counter -- true, and useless as a headline: it
    says how hard the agent worked, not what it did. This collapses a run to the
    ~10 things it actually did, each with the count that makes it meaningful.
    Phases with nothing in them are dropped, so a clean run reads short.
    """
    import collections
    c = collections.Counter(e["event"] for e in events)
    phases = []

    start = next((e for e in events if e["event"] == "run_started"), None)
    if start:
        crit = start.get("criteria", {}) or {}
        phases.append(("Loaded the criteria",
                       f"{len(crit.get('regions', []))} regions · {len(crit.get('must_haves', []))} must-haves",
                       f"Validated the couple's criteria and set the step ceiling. Mode: {start.get('mode')}."))

    for e in events:
        if e["event"] == "region_search":
            phases.append((f"Searched {e['region']}", f"{e['n_results']} candidates",
                           "Asked the web for venues in this region."))
        elif e["event"] == "search_failed":
            phases.append((f"Search failed — {e.get('region')}", "0 candidates",
                           "That region could not be searched; the run continued."))

    noise = c.get("filtered_non_venue", 0)
    if noise:
        kinds = collections.Counter(e.get("classification") for e in events
                                    if e["event"] == "filtered_non_venue")
        phases.append(("Filtered out the noise", f"{noise} removed",
                       "Listicles, planners and vendor pages dropped before any page was read: "
                       + ", ".join(f"{v} {k}" for k, v in kinds.most_common())))

    dupes = c.get("duplicate_skipped", 0) + c.get("skip_seen", 0)
    if dupes:
        phases.append(("Collapsed duplicates", f"{dupes} skipped",
                       "The same venue reached twice — another region, another domain — evaluated once."))

    bad = c.get("fetch_failed", 0) + c.get("partial_page", 0)
    if bad:
        phases.append(("Set aside unreadable pages", f"{bad} escalated",
                       "Dead links and JavaScript shells that returned no usable text."))

    read = c.get("scored", 0) + c.get("extract_failed", 0)
    if read:
        phases.append(("Read and extracted venue pages", f"{read} venues",
                       "Each page fetched, stripped to text, and turned into a structured record."))

    ground = c.get("grounding_failed", 0)
    if ground:
        phases.append(("Stripped unsupported claims", f"{ground} venues",
                       "Prices and capacities the page did not actually support were removed "
                       "before scoring — never guessed at."))

    scored = c.get("scored", 0)
    if scored:
        dec = collections.Counter(e.get("decision") for e in events if e["event"] == "scored")
        phases.append(("Scored against the must-haves", f"{scored} venues",
                       "Deterministic must-have checks plus calibrated confidence: "
                       + ", ".join(f"{v} {k}" for k, v in dec.most_common())))

    drafts = c.get("email_drafted", 0)
    if drafts:
        phases.append(("Drafted inquiries", f"{drafts} emails",
                       "Written to disk for review. The agent never sends."))

    if any(e["event"] == "run_finished" for e in events):
        phases.append(("Finished", "report written",
                       "Shortlist, dashboard and audit trail saved."))

    return [{"n": i, "title": t, "count": n, "why": w}
            for i, (t, n, w) in enumerate(phases, 1)]


def _condense(events: list) -> list[dict]:
    """Collapse the per-venue chatter into one row per venue.

    A run is mostly the same four events repeating: classify, extract, score,
    draft, times every venue. Listed raw that is 266 rows of near-identical
    text, which hides the handful of rows that actually say something. Each
    venue becomes a single row carrying its outcome and anything notable that
    happened to it; region and lifecycle events stay on their own lines.
    """
    rows, pending, cur = [], [], None

    def close():
        nonlocal cur
        if cur:
            rows.append(cur)
            cur = None

    for e in events:
        ev, url = e["event"], e.get("url")

        if ev in _STRUCTURAL:
            close()
            pending = []
            if ev == "checkpoint_saved" and rows and rows[-1].get("kind") == "checkpoint":
                rows[-1]["count"] += 1                # collapse consecutive checkpoints
                continue
            rows.append({"kind": "checkpoint" if ev == "checkpoint_saved" else "event",
                         "count": 1, "e": e})
            continue

        if not url:
            # A model call. It belongs to whichever venue the next url-bearing
            # event names -- the calls come first, the verdict follows.
            pending.append(e)
            continue

        if cur is None or cur["url"] != url:
            close()
            cur = {"kind": "venue", "url": url, "name": e.get("name", ""),
                   "first": e, "notes": [], "outcome": None, "calls": 0}
        cur["calls"] += sum(1 for p in pending if p["event"] == "model_call")
        cur["notes"].extend(p for p in pending if p["event"] == "validation_failed")
        pending = []

        if ev in _OUTCOMES:
            cur["outcome"] = e
        elif ev in ("grounding_failed", "validation_failed", "email_drafted"):
            cur["notes"].append(e)
        if not cur["name"] and e.get("name"):
            cur["name"] = e["name"]

    close()
    return rows


# Events that belong to the run rather than to any one venue.
_STRUCTURAL = {
    "run_started", "resumed_from_checkpoint", "region_search", "search_failed",
    "region_failed", "checkpoint_saved", "run_finished", "halted_max_steps",
    "halted_spend_cap",
}
# The event that decides a venue's fate -- the row's headline.
_OUTCOMES = {
    "scored", "filtered_non_venue", "duplicate_skipped", "skip_seen",
    "fetch_failed", "partial_page", "extract_failed", "score_error", "email_error",
}


def _steps_html(run_id: str, mode: str, running: bool, events: list) -> str:
    from html import escape

    condensed = _condense(events)
    rows = []
    for row in condensed:
        if row["kind"] in ("event", "checkpoint"):
            e = row["e"]
            title, why = STEP_MEANINGS.get(e["event"], (e["event"], ""))
            if row["count"] > 1:
                title += f" ×{row['count']}"
            bits = [f"<b>{escape(k)}</b> {escape((', '.join(map(str, e[k])) if isinstance(e[k], list) else str(e[k]))[:180])}"
                    for k in _DETAIL_KEYS if k in e and e[k] not in (None, "", [], {})]
            rows.append(
                f'<tr><td class="n">{e.get("step","")}</td><td class="t">{escape(e.get("ts","")[11:19])}</td>'
                f'<td><div class="ttl">{escape(title)}</div><div class="why">{escape(why)}</div>'
                + (f'<div class="det">{" · ".join(bits)}</div>' if bits else "")
                + f'<div class="ev">{escape(e["event"])}</div></td></tr>')
            continue

        out = row["outcome"] or row["first"]
        title, why = STEP_MEANINGS.get(out["event"], (out["event"], ""))
        head = escape(row["name"] or row["url"])
        det = [f'<span class="url">{escape(row["url"][:96])}</span>']
        if out["event"] == "scored":
            det.append(f'<b>decision</b> {escape(str(out.get("decision")))}'
                       f' · <b>confidence</b> {out.get("confidence")}')
        for k in ("classification", "reason", "duplicate_of"):
            if out.get(k):
                det.append(f"<b>{k}</b> {escape(str(out[k])[:120])}")
        for n in row["notes"]:
            t, _ = STEP_MEANINGS.get(n["event"], (n["event"], ""))
            extra = "; ".join(n["flags"])[:200] if n.get("flags") else n.get("tag", "")
            det.append(f'<span class="note">{escape(t)}{": " + escape(extra) if extra else ""}</span>')
        # Built outside the f-string on purpose. Nesting a same-quoted
        # expression inside an f-string (f'... {row['calls']} ...') is PEP 701
        # syntax and only parses on Python 3.12+. It ran fine locally and was a
        # hard SyntaxError on the deploy target, which is the worst place to
        # find out. Kept 3.11-compatible; tests/test_server.py enforces it.
        calls_note = f" ({row['calls']} model calls)" if row["calls"] else ""
        rows.append(
            f'<tr><td class="n">{out.get("step","")}</td><td class="t">{escape(out.get("ts","")[11:19])}</td>'
            f'<td><div class="ttl">{head}</div>'
            f'<div class="why">{escape(title)} — {escape(why)}{calls_note}</div>'
            f'<div class="det">{" · ".join(det)}</div></td></tr>')

    ov = overview(events)
    ov_rows = "".join(
        f'<tr><td class="n">{p["n"]}</td>'
        f'<td><div class="ttl">{escape(p["title"])}'
        f' <span class="cnt">{escape(p["count"])}</span></div>'
        f'<div class="why">{escape(p["why"])}</div></td></tr>'
        for p in ov)

    badge = ('<span class="badge run">running</span>' if running
             else f'<span class="badge">{escape(mode)}</span>')
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Run steps — {escape(run_id)}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,400;9..144,600;9..144,900&family=IBM+Plex+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>
 :root{{--ink:#0A0E17;--line:rgba(232,195,158,.14);--line2:rgba(255,255,255,.06);
        --gold:#E8C39E;--gold-dim:#B99873;--teal:#5FB3A3;--amber:#E8B04B;
        --text:#EDE7DD;--muted:#8C93A3;--muted2:#5C6273}}
 *{{box-sizing:border-box}}
 body{{margin:0;background:radial-gradient(1200px 800px at 18% 0%,#131a2b 0,transparent 60%),var(--ink);
       color:var(--text);font-family:"IBM Plex Mono",ui-monospace,monospace;font-size:13px}}
 .wrap{{max-width:1000px;margin:0 auto;padding:26px 22px 70px}}
 h1{{font-family:"Fraunces",serif;font-weight:900;font-size:23px;margin:0 0 3px}}
 h1 em{{color:var(--gold);font-style:italic;font-weight:600}}
 .sub{{color:var(--muted);font-size:11px;letter-spacing:.16em;text-transform:uppercase;margin-bottom:16px}}
 a.back{{color:var(--gold);text-decoration:none;border-bottom:1px solid var(--line)}}
 .badge{{font-size:9.5px;letter-spacing:.16em;text-transform:uppercase;border:1px solid var(--amber);
         color:var(--amber);border-radius:999px;padding:2px 9px;margin-left:8px}}
 .badge.run{{border-color:var(--teal);color:var(--teal)}}
 table{{border-collapse:collapse;width:100%}}
 td{{border-bottom:1px solid var(--line2);padding:9px 10px;vertical-align:top}}
 td.n{{color:var(--gold-dim);font-family:"Fraunces",serif;font-style:italic;width:44px;text-align:right}}
 td.t{{color:var(--muted2);width:74px;font-size:11.5px}}
 .ttl{{color:var(--text)}}
 .why{{color:var(--muted);font-size:11.5px;margin-top:2px;line-height:1.5}}
 .det{{color:var(--muted2);font-size:11px;margin-top:4px;line-height:1.55;word-break:break-word}}
 .det b{{color:var(--muted);font-weight:500}}
 .det .url{{color:var(--gold-dim)}}
 .det .note{{color:var(--amber)}}
 .ev{{color:#454b5c;font-size:10px;margin-top:3px;letter-spacing:.08em}}
 .note{{margin:10px 0 14px;font-size:11.5px;color:var(--muted);line-height:1.6;
        border-left:2px solid var(--line);padding-left:11px}}
 h2{{font-family:"Fraunces",serif;font-weight:600;font-size:15px;margin:30px 0 4px;color:var(--gold)}}
 table.ov td{{padding:11px 10px}}
 table.ov .ttl{{font-size:13.5px}}
 .cnt{{color:var(--gold-dim);font-size:11.5px;margin-left:7px}}
</style></head><body><div class="wrap">
<h1>Run <em>steps</em>{badge}</h1>
<div class="sub">{escape(run_id)} · {len(ov)} steps · {len(events)} underlying events</div>
<p><a class="back" href="/">&larr; back to the command center</a></p>

<h2>What the agent did</h2>
<div class="note">The run as a sequence of phases. Everything here is derived
 from <code>{escape(run_id)}/audit_trail.jsonl</code> — nothing is inferred.
 {"This run is still going — reload for more." if running else ""}</div>
<table class="ov">{ov_rows}</table>

<h2>Venue by venue</h2>
<div class="note">One row per candidate, with its outcome and how many model
 calls it took. Region searches and checkpoints keep their own lines.</div>
<table>{''.join(rows)}</table>
<p style="margin-top:20px"><a class="back" href="/">&larr; back to the command center</a></p>
</div></body></html>"""


# --- prompts ---------------------------------------------------------------
# Every prompt the agent sends, read from the LIVE module constants rather than
# copied into this file. A page that transcribes prompts drifts from the ones
# actually in use the first time someone edits prompts.py -- and a prompt page
# that lies is worse than no prompt page. Editing prompts.py changes this page.

PROMPT_STEPS = [
    {
        "tag": "search",
        "title": "Search — find candidate venues",
        "model": config.SEARCH_MODEL,
        "text": lambda: tools.SEARCH_PROMPT,
        "when": "Once per region, through the Claude API's server-side web_search tool.",
        "returns": "Search result blocks only — the model's prose is discarded.",
        "why": ("It asks for breadth rather than a shortlist. The model is told to keep "
                "searching different angles until new ones stop surfacing new venues, and "
                "explicitly NOT to summarize -- because a summarized answer is the model's "
                "memory, not search results. Only the structured web_search_tool_result "
                "blocks are read (tools._results_from), so a venue Claude 'remembers' can "
                "never enter the pipeline."),
    },
    {
        "tag": "classify",
        "title": "Classify — is this even a venue?",
        "model": config.MODEL_CHEAP,
        "text": lambda: prompts.CLASSIFY,
        "when": "Once per search result, before any page is fetched.",
        "returns": '{"classification": "venue" | "listicle" | "vendor" | "irrelevant"}',
        "why": ("Roughly two thirds of what search returns is a listicle, a planner, or a "
                "directory. Filtering here -- on the cheap model, from the name and URL "
                "alone -- means the expensive extraction call is never spent on '17 Best "
                "Tuscan Venues'. In the live run this dropped 95 of 150 candidates."),
    },
    {
        "tag": "extract",
        "title": "Extract — turn a page into a record",
        "model": config.MODEL_WORKHORSE,
        "text": lambda: prompts.EXTRACT,
        "when": "Once per venue page, on the full page text.",
        "returns": "A VenueRecord (schemas.py). Invalid output is retried once, then escalated.",
        "why": ("This is the prompt that carries the anti-hallucination rule, and the one "
                "that changed the most. 'request_only' is a first-class correct answer, any "
                "price must carry a VERBATIM source quote, and the trap list (room counts, "
                "founding years, nightly rates) exists because a number on the page is not "
                "automatically the number being asked for. The quote is checked against the "
                "page character by character afterwards, so a paraphrase fails exactly like "
                "an invention."),
    },
    {
        "tag": "score",
        "title": "Score — does it fit the couple's criteria?",
        "model": config.MODEL_WORKHORSE,
        "text": lambda: prompts.SCORE,
        "when": "Once per extracted record.",
        "returns": "A ScoredVenue: score, confidence, decision in {recommend, reject, escalate}.",
        "why": ("Note the last paragraph: the prompt tells the model its answer is not final. "
                "Must-haves are re-checked field by field in scoring.py, the budget "
                "disqualifier is arithmetic, and confidence is capped by what the page "
                "actually evidenced. The model is asked to be honest rather than agreeable "
                "because an inflated confidence is simply overwritten."),
    },
    {
        "tag": "email",
        "title": "Draft — write the inquiry",
        "model": config.MODEL_WORKHORSE,
        "text": lambda: prompts.EMAIL,
        "when": "Only for venues that survive as 'recommend'.",
        "returns": "An OutreachEmail, written to runs/<id>/drafts/. NEVER sent.",
        "why": ("It must reference one real detail from that venue's own page, which is what "
                "makes it a personalized note rather than a mail merge. It is forbidden from "
                "asserting anything not in the record. The agent has no send capability at "
                "all -- a human sends, one venue at a time."),
    },
]

# The naive first version, kept verbatim so the change is showable rather than
# described. This is the single most useful thing on the page.
PROMPT_V1 = 'EXTRACT_V1 = "Read this page and tell me the venue\'s capacity and price: {page}"'


@app.get("/prompts")
def get_prompts():
    """Every prompt the agent runs, with what it is for and which model runs it."""
    from html import escape

    blocks = []
    for i, step in enumerate(PROMPT_STEPS, 1):
        blocks.append(f"""
<section class="p">
  <div class="ph"><span class="n">{i}</span>
    <div><h2>{escape(step['title'])}</h2>
      <div class="meta"><b>model</b> {escape(step['model'])} ·
        <b>runs</b> {escape(step['when'])}</div>
      <div class="meta"><b>must return</b> {escape(step['returns'])}</div>
    </div>
    <span class="tag">{escape(step['tag'])}</span>
  </div>
  <p class="why">{escape(step['why'])}</p>
  <pre>{escape(step['text']())}</pre>
</section>""")

    return HTMLResponse(f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Agent prompts — Wedding Venue Agent</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,400;9..144,600;9..144,900&family=IBM+Plex+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>
 :root{{--ink:#0A0E17;--line:rgba(232,195,158,.14);--line2:rgba(255,255,255,.06);
        --gold:#E8C39E;--gold-dim:#B99873;--teal:#5FB3A3;--amber:#E8B04B;
        --text:#EDE7DD;--muted:#8C93A3;--muted2:#5C6273}}
 *{{box-sizing:border-box}}
 body{{margin:0;background:radial-gradient(1200px 800px at 18% 0%,#131a2b 0,transparent 60%),var(--ink);
       color:var(--text);font-family:"IBM Plex Mono",ui-monospace,monospace;font-size:13px}}
 .wrap{{max-width:1000px;margin:0 auto;padding:26px 22px 70px}}
 h1{{font-family:"Fraunces",serif;font-weight:900;font-size:23px;margin:0 0 3px}}
 h1 em{{color:var(--gold);font-style:italic;font-weight:600}}
 .sub{{color:var(--muted);font-size:11px;letter-spacing:.16em;text-transform:uppercase;margin-bottom:16px}}
 a.back{{color:var(--gold);text-decoration:none;border-bottom:1px solid var(--line)}}
 .note{{margin:10px 0 20px;font-size:11.5px;color:var(--muted);line-height:1.65;
        border-left:2px solid var(--line);padding-left:11px}}
 .p{{border-top:1px solid var(--line2);padding:18px 0 4px}}
 .ph{{display:flex;gap:12px;align-items:flex-start}}
 .ph .n{{font-family:"Fraunces",serif;font-style:italic;color:var(--gold-dim);font-size:17px;
         min-width:22px;text-align:right}}
 h2{{font-family:"Fraunces",serif;font-weight:600;font-size:15.5px;margin:0 0 4px;color:var(--text)}}
 .meta{{color:var(--muted2);font-size:11px;line-height:1.6}}
 .meta b{{color:var(--muted);font-weight:500}}
 .tag{{margin-left:auto;font-size:9.5px;letter-spacing:.16em;text-transform:uppercase;
       border:1px solid var(--line);color:var(--gold-dim);border-radius:999px;padding:2px 9px;
       white-space:nowrap}}
 .why{{color:var(--muted);font-size:11.5px;line-height:1.7;margin:10px 0 10px 34px;max-width:74ch}}
 pre{{background:rgba(255,255,255,.03);border:1px solid var(--line2);border-radius:6px;
      padding:13px 15px;overflow-x:auto;font-size:11.5px;line-height:1.65;color:var(--text);
      margin:0 0 4px 34px;white-space:pre-wrap;word-break:break-word}}
 .v1{{border-left:2px solid var(--amber);padding:11px 14px;margin:6px 0 22px 34px;
      background:rgba(232,176,75,.05);font-size:11.5px;line-height:1.7;color:var(--muted)}}
 .v1 code{{color:var(--amber);display:block;margin-bottom:7px;word-break:break-word}}
</style></head><body><div class="wrap">
<h1>Agent <em>prompts</em></h1>
<div class="sub">{len(PROMPT_STEPS)} prompts · read live from src/prompts.py and src/tools.py</div>
<p><a class="back" href="/">&larr; back to the command center</a></p>

<div class="note">These are the exact strings the agent sends, read from the running
 modules rather than copied here — editing <code>src/prompts.py</code> changes this page.
 Every response is parsed into a Pydantic schema; nothing free-form is ever used as data.</div>

<div class="v1"><code>{escape(PROMPT_V1)}</code>
 The first version of the extraction prompt, kept in <code>src/prompts.py</code> as a
 comment. It invented prices: asked for a price on a page that only said
 &ldquo;contact us for a quote&rdquo;, the model supplied a plausible number. Everything
 in step 3 below — request_only as a valid answer, the verbatim quote requirement,
 the list of numbers that are <em>not</em> the number being asked for — exists because
 of that failure.</div>
{''.join(blocks)}
<p style="margin-top:26px"><a class="back" href="/">&larr; back to the command center</a></p>
</div></body></html>""")


@app.get("/progress")
def get_progress():
    """Where the in-flight run has got to.

    Derived entirely from the audit trail the run is already writing -- no new
    instrumentation in the agent, and it works for a run started from the
    terminal just as well as one started from the button.

    The estimate is deliberately coarse. Region count is the only thing known up
    front; how many venues a region holds is not known until its search returns,
    and searching is itself minutes of silence. So progress is
    "regions finished + how far through the current one", and the stage label
    carries the detail a percentage cannot.
    """
    d = run_in_progress()
    if not d:
        return {"running": False}

    trail = os.path.join(d, "audit_trail.jsonl")
    if not os.path.exists(trail):
        return {"running": True, "stage": "Starting…", "percent": 0,
                "run_id": os.path.basename(d)}

    events = []
    with open(trail) as f:
        for line in f:
            try:
                events.append(json.loads(line))
            except ValueError:
                pass
    if not events:
        return {"running": True, "stage": "Starting…", "percent": 0,
                "run_id": os.path.basename(d)}

    start = next((e for e in events if e["event"] == "run_started"), {})
    regions = (start.get("criteria") or {}).get("regions") or []
    total = len(regions) or 1

    searches = [e for e in events if e["event"] == "region_search"]
    started = len(searches)

    # Progress within the region currently being evaluated.
    processed, candidates, current = set(), 0, None
    if searches:
        last = searches[-1]
        current, candidates = last["region"], max(1, last["n_results"])
        after = events[events.index(last) + 1:]
        for e in after:
            if e["event"] in _OUTCOMES and e.get("url"):
                processed.add(e["url"])

    frac = min(1.0, len(processed) / candidates) if searches else 0.0
    percent = int(round(((max(0, started - 1) + frac) / total) * 100)) if searches else 1

    if not searches:
        stage = f"Searching {regions[0]}…" if regions else "Searching…"
    elif frac >= 1.0 and started < total:
        stage = f"Searching {regions[started]}…"
    elif frac >= 1.0:
        stage = "Finishing up…"
    else:
        stage = f"Evaluating {current} — {len(processed)} of {candidates} candidates"

    dec = {}
    for e in events:
        if e["event"] == "scored":
            dec[e.get("decision")] = dec.get(e.get("decision"), 0) + 1

    first_ts, last_ts = events[0].get("ts"), events[-1].get("ts")
    elapsed = None
    try:
        from datetime import datetime
        elapsed = int((datetime.fromisoformat(last_ts)
                       - datetime.fromisoformat(first_ts)).total_seconds())
    except (TypeError, ValueError):
        pass

    return {
        "running": True,
        "run_id": os.path.basename(d),
        "stage": stage,
        "percent": max(1, min(99, percent)),   # never 0 or 100 while still going
        "regions_total": total,
        "regions_started": started,
        "current_region": current,
        "scored": sum(dec.values()),
        "decisions": dec,
        "elapsed_s": elapsed,
    }


@app.get("/report")
def get_report():
    """Current run data, in exactly the shape the dashboard renders from."""
    data = _report_payload(latest_run_dir())
    if data is None:
        raise HTTPException(404, "no run yet -- POST /run or: python main.py --criteria criteria.json")
    return data


@app.post("/run")
def post_run():
    """Run the agent on the saved criteria and return the fresh report.

    Blocks until the run finishes. Honors AGENT_MODE: the default (mock) is
    fully offline against fixtures; real mode makes live calls, exactly as the
    equivalent terminal command would. Serialized -- one run at a time.
    """
    if not _run_lock.acquire(blocking=False):
        raise HTTPException(409, "a run is already in progress")
    try:
        env = dict(os.environ)
        env.setdefault("AGENT_MODE", config.MODE)
        env["AGENT_AUTONOMY"] = "draft_only"   # explicit: the agent drafts, never sends
        try:
            proc = subprocess.run(
                [sys.executable, "main.py", "--criteria", CRITERIA_PATH],
                cwd=ROOT, env=env, capture_output=True, text=True,
                timeout=RUN_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            raise HTTPException(504, f"run exceeded {RUN_TIMEOUT_SECONDS}s and was stopped")

        if proc.returncode != 0:
            raise HTTPException(500, f"run failed:\n{(proc.stderr or proc.stdout)[-2000:]}")

        data = _report_payload(latest_run_dir())
        if data is None:
            raise HTTPException(500, "run finished but produced no report")
        data["mode"] = env.get("AGENT_MODE", config.MODE)
        return data
    finally:
        _run_lock.release()


# --- send -----------------------------------------------------------------

@app.get("/auth/status")
def auth_status():
    """Whether Gmail is connected. Safe to poll; never raises."""
    return sending.auth_status()


@app.get("/draft")
def get_draft(url: str):
    """The agent's drafted inquiry for one venue, for the human to review/edit.

    The recipient is NOT taken from the draft file -- it is re-derived from the
    venue record, which is the only address a send is allowed to go to. The
    draft supplies the subject and body a person is about to edit; it does not
    get a say in who receives it.
    """
    run_dir = latest_run_dir()
    if not run_dir:
        raise HTTPException(409, "no run yet")

    with open(os.path.join(run_dir, "report.json")) as f:
        report = json.load(f)
    record = next(
        (s["record"] for s in report.get("all_scored", []) if s["record"].get("url") == url),
        None,
    )
    if record is None:
        raise HTTPException(404, f"no venue record for {url}")

    subject, body = "", ""
    path = os.path.join(run_dir, "drafts", f"{sending._slug(url)}.txt")
    if os.path.exists(path):
        raw = open(path).read()
        for line in raw.splitlines():
            if line.startswith("SUBJECT:"):
                subject = line[len("SUBJECT:"):].strip()
                break
        parts = raw.split("\n\n", 1)
        body = parts[1].strip() if len(parts) > 1 else ""

    return {
        "url": url,
        "venue": record.get("name", ""),
        "to": sending.resolve_recipient(record),   # authoritative; may be null
        "subject": subject,
        "body": body,
        "has_draft": bool(subject or body),
        "sent": sending.read_sent_log().get(url),
    }


@app.post("/send")
def post_send(payload: dict = Body(...)):
    """Send ONE venue's reviewed draft.

    The request may carry an edited subject and body -- a human is expected to
    read and adjust the draft before sending. It may NOT carry a different
    recipient: `to` is checked against the address extracted from that venue's
    own page, and refused on any mismatch. See src/sending.py.
    """
    url = (payload.get("url") or "").strip()
    if not url:
        raise HTTPException(400, "url is required (identifies which venue record to check against)")

    run_dir = latest_run_dir()
    if not run_dir:
        raise HTTPException(409, "no run to send from")

    with open(os.path.join(run_dir, "report.json")) as f:
        report = json.load(f)

    record = next(
        (s["record"] for s in report.get("all_scored", []) if s["record"].get("url") == url),
        None,
    )
    if record is None:
        raise HTTPException(404, f"no venue record for {url} in the current run")

    try:
        entry = sending.send(
            record,
            to=payload.get("to", ""),
            subject=payload.get("subject", ""),
            body=payload.get("body", ""),
            run_dir=run_dir,
        )
    except sending.SendRefused as e:
        return JSONResponse({"sent": False, "error": str(e)}, status_code=422)
    except Exception as e:  # noqa: BLE001 - surface delivery failures as 502
        raise HTTPException(502, f"send failed: {e}")

    return {"sent": True, "url": url, **entry}


if __name__ == "__main__":
    try:
        import uvicorn
    except ModuleNotFoundError:
        sys.exit("This server needs uvicorn:  pip install -r requirements.txt")

    print(f"criteria server on http://{HOST}:{PORT}  (localhost only, no auth)")
    print(f"  criteria file: {CRITERIA_PATH}")
    print(f"  dashboard:     {DASHBOARD_PATH}")
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
