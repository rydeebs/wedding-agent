"""
LLM access layer.

Responsibilities:
  - Wrap Claude API calls behind one function.
  - Track token usage and compute cost per call (Observe stage).
  - Support MOCK mode: deterministic canned responses from fixtures so the whole
    agent runs offline. This makes the demo repeatable and lets evals run in CI
    without spending money.

Everything returns plain text; the agent is responsible for parsing that text
into a schema (structured outputs live one layer up, in agent.py).
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field

from . import config


@dataclass
class CostMeter:
    """Accumulates token usage across a run so we can report cost per query."""
    calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    usd: float = 0.0
    by_model: dict = field(default_factory=dict)

    def add(self, model: str, tin: int, tout: int):
        self.calls += 1
        self.tokens_in += tin
        self.tokens_out += tout
        rate = config.RATES.get(model, {"in": 0.0, "out": 0.0})
        cost = (tin / 1_000_000) * rate["in"] + (tout / 1_000_000) * rate["out"]
        self.usd += cost
        m = self.by_model.setdefault(model, {"calls": 0, "in": 0, "out": 0, "usd": 0.0})
        m["calls"] += 1
        m["in"] += tin
        m["out"] += tout
        m["usd"] += cost

    def summary(self) -> dict:
        return {
            "calls": self.calls,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "usd_total": round(self.usd, 4),
            "by_model": {
                k: {**v, "usd": round(v["usd"], 4)} for k, v in self.by_model.items()
            },
        }


# --- Mock fixtures --------------------------------------------------------

def _load_mock():
    path = os.path.join(os.path.dirname(__file__), "..", "fixtures", "llm_responses.json")
    with open(path) as f:
        return json.load(f)


_MOCK = None
_MOCK_CALLS: dict[str, int] = {}   # per-key attempt counter for mock mode


def complete(prompt: str, *, model: str, meter: CostMeter, tag: str) -> str:
    """
    One completion. `tag` identifies which step this is (e.g. "classify",
    "extract", "score", "email") so mock mode can return the right fixture and
    the audit trail can label the call.

    A fixture value may be a single response dict OR a list of them. A list is
    consumed one entry per attempt, which lets us model a first-attempt failure
    followed by a successful retry (see hotel-esencia extraction).
    """
    if config.MODE == "mock":
        global _MOCK
        if _MOCK is None:
            _MOCK = _load_mock()
        key = _mock_key(tag, prompt)
        entry = _MOCK.get(key, {"text": "{}", "in": 300, "out": 5})
        if isinstance(entry, list):
            i = _MOCK_CALLS.get(key, 0)
            _MOCK_CALLS[key] = i + 1
            entry = entry[min(i, len(entry) - 1)]
        meter.add(model, entry["in"], entry["out"])
        return entry["text"]

    # --- REAL mode --------------------------------------------------------
    from anthropic import Anthropic  # imported lazily so mock mode needs no key

    client = Anthropic(api_key=config.ANTHROPIC_API_KEY)
    resp = client.messages.create(
        model=model,
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )
    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    meter.add(model, resp.usage.input_tokens, resp.usage.output_tokens)
    return text


_URL_RE = re.compile(r"https?://[^\s\"'<>]+")


def _mock_key(tag: str, prompt: str) -> str:
    """Derive a fixture key from the venue slug the prompt carries.

    The slug is read out of the first URL in the prompt (every prompt puts the
    venue URL near the top). This used to be a hardcoded list of slugs, which
    meant a new fixture silently fell through to the `{}` default and failed
    schema validation for a reason that looked nothing like the real cause.
    """
    m = _URL_RE.search(prompt)
    if m:
        path = m.group(0).split("?", 1)[0].split("#", 1)[0].rstrip("/")
        slug = path.rsplit("/", 1)[-1]
        if slug and "." not in slug:      # guard against a bare domain
            return f"{tag}:{slug}"
    return f"{tag}:default"
