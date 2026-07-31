"""
Entrypoint.

    python main.py --criteria criteria.json

Env:
    AGENT_MODE=mock|real     (default mock; mock runs offline on fixtures)
    AGENT_AUTONOMY=draft_only (default; sending stays human)
    ANTHROPIC_API_KEY=...     (real mode)
    (search uses the Claude API's web_search tool -- no second key needed)
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime

from src.agent import Agent
from src.report import render
from src.dashboard import render as render_dashboard


def _new_run_dir() -> str:
    """A fresh run directory, guaranteed not to collide.

    The name is second-resolution, so two runs started in the same second used
    to land in the same directory -- and because the agent resumes from any
    checkpoint it finds there, the second run silently resumed the first,
    reported the first's venues, and did no work. Rare in real mode where a run
    takes minutes, but it makes back-to-back mock runs lie about what happened.
    """
    base = os.path.join("runs", datetime.now().strftime("run_%Y%m%d_%H%M%S"))
    candidate, n = base, 2
    while os.path.exists(candidate):
        candidate = f"{base}_{n}"
        n += 1
    return candidate


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--criteria", default="criteria.json")
    ap.add_argument("--run-dir", default=None)
    ap.add_argument("--resume", default=None, help="path to an existing run dir to resume")
    args = ap.parse_args()

    with open(args.criteria) as f:
        criteria = json.load(f)

    run_dir = args.resume or args.run_dir or _new_run_dir()

    agent = Agent(criteria, run_dir)
    report = agent.run()

    html_path = render(report, os.path.join("out", "shortlist.html"))
    dash_path = render_dashboard(report, criteria, os.path.join("out", "dashboard.html"))

    s = report["summary"]
    print("\n=== RUN COMPLETE ===")
    print(f"run dir:            {run_dir}")
    print(f"venues evaluated:   {s['venues_evaluated']}")
    print(f"recommended:        {s['recommended']}")
    print(f"escalated (human):  {s['escalated']}")
    print(f"agent steps:        {s['steps']}")
    print(f"cost total:         ${s['cost']['usd_total']}  (${s['cost_per_venue_usd']}/venue)")
    print(f"audit trail:        {os.path.join(run_dir, 'audit_trail.jsonl')}")
    print(f"drafted emails:     {os.path.join(run_dir, 'drafts/')}")
    print(f"shortlist (simple): {html_path}")
    print(f"dashboard (globe):  {dash_path}")


if __name__ == "__main__":
    main()
