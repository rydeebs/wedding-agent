"""
Central configuration. Kept out of code so decisions are visible and tunable.
"""

import os
import re


def _load_dotenv(path: str = None) -> None:
    """Load .env into the environment, if it exists.

    Without this, editing .env did nothing at all: every setting below reads
    os.environ, and only an `export` in the shell ever reached it. Someone who
    sets AGENT_MODE=real and an API key in .env, restarts, and watches it run on
    fixtures anyway has been given no way to find out why.

    Two deliberate details:
      - os.environ WINS over .env (setdefault), so an explicit
        `AGENT_MODE=mock python ...` still overrides the file. The eval suite
        and tests rely on exactly that to stay offline.
      - inline comments are stripped from unquoted values, because .env is
        normally copied from .env.example, whose lines carry trailing comments
        ("AGENT_MODE=real   # mock | real"). Without this, the mode would be
        the string "real   # mock | real" and never equal "real".
    """
    path = path or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    if not os.path.exists(path):
        return
    try:
        with open(path) as f:
            lines = f.readlines()
    except OSError:
        return

    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        if "=" not in line:
            continue
        key, val = line.split("=", 1)
        key, val = key.strip(), val.strip()
        if not key:
            continue
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        else:
            val = re.split(r"\s+#", val, maxsplit=1)[0].strip()
            # "KEY=    # explanatory comment" is an UNSET key, not a key whose
            # value is the comment. .env.example is written that way, so a
            # straight copy would otherwise load a sentence as a credential and
            # the "is it set?" check would answer yes.
            if val.startswith("#"):
                val = ""
        os.environ.setdefault(key, val)


_load_dotenv()

# --- Models (runtime reasoning INSIDE the agent) --------------------------
# These are the models the AGENT calls at runtime -- separate from the model
# Claude Code uses to BUILD this repo (that's set with /model in Claude Code).
#
# Recommendation: keep Sonnet 5 for reasoning and Haiku 4.5 for the cheap
# classify step. Extraction/scoring here is straightforward; Opus 5 or Fable 5
# would multiply cost per venue with no quality gain. Spend frontier tokens on
# building, not on grunt-work inference.
#
# Override via env if you want to experiment, e.g.:
#   MODEL_WORKHORSE=claude-opus-5 python main.py
MODEL_WORKHORSE = os.environ.get("MODEL_WORKHORSE", "claude-sonnet-5")
MODEL_CHEAP = os.environ.get("MODEL_CHEAP", "claude-haiku-4-5-20251001")

# --- Cost rates (USD per million tokens) ----------------------------------
# Set to current rates from https://www.anthropic.com/pricing. Cost per query is
# COMPUTED from these, not guessed. Values below are approximate placeholders.
RATES = {
    "claude-sonnet-5": {"in": 3.00, "out": 15.00},
    "claude-haiku-4-5-20251001": {"in": 0.80, "out": 4.00},
    "claude-opus-5": {"in": 5.00, "out": 25.00},
    "claude-fable-5": {"in": 10.00, "out": 50.00},
}

# --- Guardrails -----------------------------------------------------------
# Runaway guard, not a budget. A fixed number is the wrong shape here: it has to
# permit the work actually asked for, and that scales with the number of regions.
# At a flat 120 a four-country search (4 x 8 venues x ~4 calls) would halt
# mid-run and silently return a partial shortlist. The agent therefore sizes its
# own ceiling from the criteria (see Agent.__init__) and uses MAX_STEPS as the
# floor -- so the guard still catches a genuine loop, but never a healthy run.
MAX_STEPS = int(os.environ.get("AGENT_MAX_STEPS", "120"))
STEPS_PER_VENUE = 5            # classify + extract (+1 retry) + score + email

# How many VENUES per region actually get evaluated. 0 = no cap.
#
# It counts venues, not search hits. That distinction is the whole point: when
# it capped raw search results, roughly three slots in five went to directories
# and planner roundups, so a "50" region produced 17-22 real venues. Triage is a
# Haiku call at ~$0.0003, so classifying past the cap is nearly free -- the
# expensive thing was the venues that never got looked at.
#
# 50 venues is broad enough that the shortlist is not an artifact of the cap,
# and bounded enough that a three-region run stays about an hour.
MAX_VENUES_PER_REGION = int(os.environ.get("MAX_VENUES_PER_REGION", "50"))
SCHEMA_RETRIES = 1             # retry a malformed structured output once, then escalate
CONFIDENCE_ESCALATION = 0.80   # below this confidence -> escalate to a human

# --- Autonomy level -------------------------------------------------------
# The deployment control. Start at the LEAST autonomous useful setting.
#   "draft_only"  -> agent researches, scores, and DRAFTS emails. Never sends.
#   "auto_send"   -> would send. Intentionally NOT implemented. Sending is the
#                    irreversible step; per the audit it stays human.
AUTONOMY = os.environ.get("AGENT_AUTONOMY", "draft_only")

# --- Mode -----------------------------------------------------------------
# MOCK mode uses fixtures/ so the agent runs offline and deterministically.
# Great for the demo and for CI. REAL mode calls the Claude API for both
# reasoning and web search (server-side web_search tool).
MODE = os.environ.get("AGENT_MODE", "mock")   # "mock" | "real"

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# --- Search (real mode) ---------------------------------------------------
# Search runs through the Claude API's server-side web_search tool, so the only
# credential needed is ANTHROPIC_API_KEY. No second vendor, no second key.
#
# The tool version matters: web_search_20260209 (with dynamic filtering) needs
# Opus 5/4.8/4.7/4.6, Sonnet 5, or Sonnet 4.6 -- Haiku 4.5 is not supported, so
# search cannot use MODEL_CHEAP. Sonnet 5 keeps it on the workhorse tier.
SEARCH_MODEL = os.environ.get("SEARCH_MODEL", MODEL_WORKHORSE)

# No per-region search cap: the model searches until it has exhausted the angles
# it can think of. Set SEARCH_MAX_USES to a positive number to re-impose one.
SEARCH_MAX_USES = int(os.environ.get("SEARCH_MAX_USES", "0"))
SEARCH_MAX_TOKENS = int(os.environ.get("SEARCH_MAX_TOKENS", "1500"))

# The server-side tool loop pauses after ~10 iterations and returns
# stop_reason="pause_turn". Uncapped searching hits that routinely, and a caller
# that does not resume silently keeps only the first batch -- which would look
# exactly like "search found 10 venues" rather than "we stopped asking".
SEARCH_MAX_CONTINUATIONS = int(os.environ.get("SEARCH_MAX_CONTINUATIONS", "6"))
