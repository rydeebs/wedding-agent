"""
Evals stage: turn non-determinism into evidence.

Runs the agent once (mock mode) over the same criteria, then grades each
hand-labeled case in golden_dataset.json on:

  1. classification correct     (did it filter noise / recognize venues)
  2. decision correct           (recommend / reject / escalate / filtered)
  3. pricing correct            (anti-hallucination: request_only vs listed vs unknown)

Emits evals/eval_report.md with a pass rate and failure categories, in the
shape the FDE guide's evaluation report uses.

Run:  python -m evals.run_evals
"""

from __future__ import annotations

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("AGENT_MODE", "mock")

from src.agent import Agent  # noqa: E402
from src import criteria_io  # noqa: E402


def load_json(p):
    with open(p) as f:
        return json.load(f)


def run_criteria_intake(cases):
    """Grade the criteria intake path -- the boundary the dashboard writes through.

    Same contract as the venue cases: a labeled expectation, a pass/fail, and a
    failure category. Criteria are agent input, so validating them is agent
    behavior, not plumbing, and it belongs behind the same gate.
    """
    rows, failures = [], 0

    for case in cases:
        problems = []
        try:
            clean, warnings = criteria_io.validate(case["payload"])
            got = "accept"
        except criteria_io.CriteriaError:
            clean, warnings, got = None, [], "reject"

        if got != case["expect"]:
            problems.append(f"expected {case['expect']}, got {got}")

        if got == "accept":
            if "expect_warning" in case and bool(warnings) != case["expect_warning"]:
                problems.append(
                    f"expected warning={case['expect_warning']}, got {bool(warnings)}"
                )
            # assert_clean checks that accepted input is not just accepted, but
            # normalized the way we claim (flattened text, deduped regions).
            for key, expected in case.get("assert_clean", {}).items():
                if clean.get(key) != expected:
                    problems.append(f"{key}={clean.get(key)!r}, expected {expected!r}")

        failures += bool(problems)
        rows.append({
            "name": case["name"],
            "category": case["category"],
            "expected": case["expect"] + (" +warn" if case.get("expect_warning") else ""),
            "actual": got + (" +warn" if warnings else ""),
            "result": "PASS" if not problems else "FAIL(" + "; ".join(problems) + ")",
        })

    return rows, failures


def outcome_for(agent, url):
    """Derive what the agent actually did with a url, from its final state."""
    blank = {"decision": None, "pricing": None, "classification": None,
             "capacity": None, "grounding_flagged": False, "confidence": None}

    # scored venues carry the full record
    for s in agent.state["scored"]:
        if s["record"]["url"] == url:
            return {
                "decision": s["decision"],
                "pricing": s["record"]["pricing_signal"],
                "classification": s["record"]["classification"],
                "capacity": s["record"]["capacity_max"],
                "grounding_flagged": bool(s.get("grounding_flags")),
                "confidence": s.get("confidence"),
            }

    # filtered / deduped / failed venues never get scored; they live in `seen`
    seen = agent.state["seen"].get(url)
    if seen and seen.startswith("filtered:"):
        return {**blank, "decision": "filtered", "classification": seen.split(":", 1)[1]}
    if seen and seen.startswith("duplicate:"):
        return {**blank, "decision": "duplicate", "classification": "venue"}
    if seen in ("fetch_failed", "extract_failed", "score_failed", "partial_page"):
        # Every tool/parse failure path escalates by design -- it is never a
        # silent drop. The `seen` value records which failure it was.
        return {**blank, "decision": "escalate", "classification": "venue"}
    return blank


def main():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    # Pinned, NOT the live criteria.json. The golden dataset labels specific
    # venues in specific regions, so grading has to run against fixed criteria.
    # criteria.json is user-editable from the dashboard now; if the suite read
    # it, editing the guest count in a browser would "fail" the agent.
    criteria = load_json(os.path.join(root, "evals", "eval_criteria.json"))
    golden = load_json(os.path.join(root, "evals", "golden_dataset.json"))["cases"]

    run_dir = tempfile.mkdtemp(prefix="eval_run_")
    agent = Agent(criteria, run_dir)
    agent.run()

    rows = []
    failures = {"classification": 0, "decision": 0, "pricing": 0,
                "capacity": 0, "grounding": 0, "confidence": 0}
    passed = 0

    for case in golden:
        url = case["url"]
        got = outcome_for(agent, url)

        checks = []

        # classification: a filtered/deduped item is graded on the decision path
        if got["classification"] != case["expect_classification"]:
            checks.append("classification")

        if got["decision"] != case["expect_decision"]:
            checks.append("decision")

        # pricing: null in the case means "not applicable", not "expect null"
        if case["expect_pricing"] is not None and got["pricing"] != case["expect_pricing"]:
            checks.append("pricing")

        # capacity: present only on cases that guard against an invented number.
        # `null` here is a REAL expectation (the agent must report no capacity),
        # so presence of the key is what makes it apply.
        if "expect_capacity" in case and got["capacity"] != case["expect_capacity"]:
            checks.append("capacity")

        # grounding: did the agent catch the record making a claim the page did
        # not support? Guards against passing a trap case for the wrong reason.
        if "expect_grounding_flagged" in case:
            if got["grounding_flagged"] != case["expect_grounding_flagged"]:
                checks.append("grounding")

        # confidence: a CEILING, not a target. Every other penalty in
        # scoring.py keys on something a recommended venue cannot have, so
        # without a case like this the confidence of everything on the
        # shortlist silently collapses to whatever number the model said.
        if "expect_max_confidence" in case:
            conf = got["confidence"]
            if conf is None or conf > case["expect_max_confidence"] + 1e-9:
                checks.append("confidence")

        for c in checks:
            failures[c] += 1

        is_pass = not checks
        passed += is_pass
        rows.append({
            "url": url,
            "category": case["category"],
            "expected": f"{case['expect_decision']}/{case['expect_pricing']}",
            "actual": f"{got['decision']}/{got['pricing']}",
            "result": "PASS" if is_pass else "FAIL(" + ",".join(checks) + ")",
        })

    # --- criteria intake cases -------------------------------------------
    intake_cases = load_json(os.path.join(root, "evals", "criteria_intake.json"))["cases"]
    intake_rows, intake_failures = run_criteria_intake(intake_cases)
    if intake_failures:
        failures["criteria_intake"] = intake_failures

    total = len(golden) + len(intake_rows)
    passed += len(intake_rows) - intake_failures
    rate = passed / total if total else 0.0

    # --- write report -----------------------------------------------------
    lines = []
    lines.append("# Evaluation Report — Wedding Venue Agent\n")
    lines.append(f"**Pass rate: {passed}/{total} ({rate:.0%})**\n")
    lines.append("## Failure categories\n")
    for k, v in failures.items():
        if v:
            lines.append(f"- {k}: {v}")
    if not any(failures.values()):
        lines.append("- none\n")
    lines.append("\n## Operating rules\n")
    lines.append("- Below 0.80 confidence, the agent escalates instead of recommending.")
    lines.append("- Listed price over budget is a hard disqualifier (deterministic, not model-judged).")
    lines.append("- A price without a verbatim source quote is rejected at the schema layer.")
    lines.append("- A quote that is not on the page, states no money, or is not about a wedding "
                 "is stripped, and the venue escalates (`grounding.py`).")
    lines.append("- A number is only a capacity if it appears on the page next to a guest word; "
                 "room counts, years, and square footage are not capacities.")
    lines.append("- Must-haves are re-checked in code against the record. Unmet -> reject; "
                 "unconfirmed -> escalate. A null field never counts as a pass.")
    lines.append("- Model confidence is a ceiling, not a floor: it is lowered by unconfirmed "
                 "must-haves, unaddressed pricing, and ungrounded claims, and never raised.")
    lines.append("- Missing must-have information escalates to a human; it is never guessed.\n")

    lines.append("## Failure taxonomy\n")
    lines.append("| Failure | Detected by | Agent behavior |")
    lines.append("|---|---|---|")
    lines.append("| Search returns nothing / errors | `tools.search_venues` + backoff | region skipped and logged, run continues |")
    lines.append("| Page unreachable | `tools.fetch_page` + backoff | escalate: \"page unreachable\" |")
    lines.append("| Page is a 200 but unusable (app shell, error page, empty) | `tools.page_problem` | escalate before extraction; no workhorse call spent |")
    lines.append("| Non-venue result (listicle, vendor) | classify step | filtered, never extracted |")
    lines.append("| Duplicate venue across regions/domains | `dedupe.find_duplicate` | collapsed before classify, first occurrence kept |")
    lines.append("| Malformed model output | Pydantic + one retry | escalate if still invalid |")
    lines.append("| Self-contradictory pricing record | `VenueRecord` model validator | rejected, retried, then escalated |")
    lines.append("| Fabricated or misattributed evidence | `grounding.py` | claim stripped, venue escalated |")
    lines.append("| Unmet must-have | `scoring.check_must_haves` | deterministic reject |")
    lines.append("| Unconfirmed must-have / low calibrated confidence | `scoring.apply` | escalate |")
    lines.append("| Listed price over budget | `agent` budget disqualifier | deterministic reject |\n")
    lines.append("## Cases — venue decisions\n")
    lines.append("| Case | Category | Expected (decision/pricing) | Actual | Result |")
    lines.append("|---|---|---|---|---|")
    for r in rows:
        short = r["url"].split("/")[-1]
        lines.append(f"| {short} | {r['category']} | {r['expected']} | {r['actual']} | {r['result']} |")

    lines.append("\n## Cases — criteria intake\n")
    lines.append("Criteria are the agent's input, and the dashboard can now write them. "
                 "These grade the validator that stands between the two.\n")
    lines.append("| Case | Category | Expected | Actual | Result |")
    lines.append("|---|---|---|---|---|")
    for r in intake_rows:
        lines.append(f"| {r['name']} | {r['category']} | {r['expected']} | {r['actual']} | {r['result']} |")

    report_md = "\n".join(lines) + "\n"

    out = os.path.join(root, "evals", "eval_report.md")
    with open(out, "w") as f:
        f.write(report_md)

    print(report_md)
    print(f"cost of eval run: ${agent.meter.summary()['usd_total']}")
    print(f"report written: {out}")

    # Non-zero exit if any case failed -- usable as a CI gate.
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
