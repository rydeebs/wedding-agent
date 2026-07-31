"""
The agent's two tools.

  1. search_venues(region)  -> list of candidate results (name, url, snippet)
  2. fetch_page(url)        -> raw text content of a venue page

Both wrapped with retry + exponential backoff (1s, 2s, 4s, 8s, cap 16s) on
external failures, matching the FDE guide. Both support MOCK mode via fixtures.

The agent decides *when* to call these; they don't drive the loop themselves.
"""

from __future__ import annotations

import html
import json
import os
import re
import time
from typing import Callable

import requests

from . import config


class ToolError(Exception):
    pass


class PermanentToolError(ToolError):
    """A failure that retrying cannot fix: a bad API key, a disabled API, an
    exhausted daily quota. Backing off five times just delays the same answer
    and buries the real message under a retry wrapper."""


def with_backoff(fn: Callable, *, tries: int = 5, base: float = 1.0, cap: float = 16.0):
    """Retry `fn` with exponential backoff. Raises ToolError after `tries`."""
    delay = base
    last = None
    for attempt in range(tries):
        try:
            return fn()
        except PermanentToolError:
            raise                      # configuration problem -- report it now
        except Exception as e:  # noqa: BLE001 - we deliberately catch broadly here
            last = e
            if attempt == tries - 1:
                break
            time.sleep(min(delay, cap))
            delay *= 2
    raise ToolError(f"tool failed after {tries} attempts: {last}")


# --- Fixtures -------------------------------------------------------------

def _fixtures(name: str):
    path = os.path.join(os.path.dirname(__file__), "..", "fixtures", name)
    with open(path) as f:
        return json.load(f)


# --- search_venues --------------------------------------------------------

def search_venues(region: str) -> list[dict]:
    if config.MODE == "mock":
        data = _fixtures("search_results.json")
        return data.get(region, [])

    if not config.ANTHROPIC_API_KEY:
        # Fail loudly and early: a missing key would otherwise surface as an
        # empty region, which reads exactly like "no venues found there".
        raise PermanentToolError(
            "real mode needs ANTHROPIC_API_KEY in .env (search runs through the "
            "Claude API's server-side web_search tool)"
        )

    def _call():
        from anthropic import Anthropic   # lazy: mock mode needs no key

        client = Anthropic(api_key=config.ANTHROPIC_API_KEY)

        tool = {"type": "web_search_20260209", "name": "web_search"}
        if config.SEARCH_MAX_USES > 0:      # 0 = uncapped, the default
            tool["max_uses"] = config.SEARCH_MAX_USES

        messages = [{"role": "user", "content": SEARCH_PROMPT.format(region=region)}]
        found, continuations = [], 0

        while True:
            resp = client.messages.create(
                model=config.SEARCH_MODEL,
                max_tokens=config.SEARCH_MAX_TOKENS,
                tools=[tool],
                messages=messages,
            )
            found.extend(_results_from(resp))

            # The server-side tool loop pauses after ~10 iterations rather than
            # ending. Resuming is how an uncapped search actually stays
            # uncapped: stopping here would keep only the first batch and look
            # indistinguishable from "that is all search found".
            if resp.stop_reason != "pause_turn":
                break
            continuations += 1
            if continuations > config.SEARCH_MAX_CONTINUATIONS:
                break
            messages = messages[:1] + [{"role": "assistant", "content": resp.content}]

        return _dedupe(found)

    # Fewer retries than the default: one search takes minutes and costs real
    # money, so five attempts on a wedged region would burn a quarter hour and
    # several dollars before reporting the same failure.
    return with_backoff(_call, tries=2)


SEARCH_PROMPT = (
    "Find as many individual wedding venues in {region} as you can — each "
    "venue's own website, not directories, listicles, or planner roundups.\n\n"
    "Be exhaustive. Search repeatedly with different angles: sub-regions and "
    "well-known wedding areas, venue types (villa, estate, castle, farmhouse, "
    "vineyard, hotel, finca, masseria), and phrasings couples actually use. "
    "Keep going until further searches stop surfacing venues you have not "
    "already seen.\n\n"
    "Do not summarize or shortlist — the search results themselves are what "
    "matter, and every distinct venue site is worth returning."
)


def _results_from(resp) -> list[dict]:
    """Pull candidates out of the SEARCH RESULT BLOCKS ONLY.

    The model's prose is deliberately ignored. When the search tool hits
    `max_uses`, Claude will helpfully pad its written answer with venues from
    memory -- plausible names, plausible URLs, no search behind them. Reading
    that text would inject invented venues at the very top of the pipeline,
    upstream of every anti-hallucination guard downstream. The structured
    `web_search_tool_result` blocks contain only what search actually returned,
    so that is the only thing this function looks at.
    """
    out, seen = [], set()
    for block in resp.content:
        if getattr(block, "type", "") != "web_search_tool_result":
            continue
        content = block.content
        if not isinstance(content, list):
            # An error arrives as a single object, not a list, and does NOT
            # raise -- it rides back on a normal 200.
            code = getattr(content, "error_code", None) or str(content)[:120]
            raise ToolError(f"web search failed: {code}")
        for item in content:
            url = (getattr(item, "url", "") or "").strip()
            if not url or url in seen:
                continue
            seen.add(url)
            # A web_search_result carries url/title/page_age -- there is no
            # snippet. Leaving it empty is deliberate: page_age ("3 days ago")
            # in the snippet slot would reach the classify prompt looking like
            # a venue description. Classification runs on name + URL instead,
            # and anything it lets through still has to survive fetch,
            # extraction, and grounding.
            out.append({
                "name": (getattr(item, "title", "") or "").strip(),
                "url": url,
                "snippet": "",
            })
    return out


def _dedupe(results: list[dict]) -> list[dict]:
    """Collapse repeats across continuation rounds.

    No cap here on purpose. MAX_VENUES_PER_REGION counts VENUES, and whether a
    search hit is a venue is not knowable until the classify step has looked at
    it -- capping the raw list spends the budget on directories.
    """
    out, seen = [], set()
    for r in results:
        if r["url"] in seen:
            continue
        seen.add(r["url"])
        out.append(r)
    return out


# --- fetch_page -----------------------------------------------------------

# Of TEXT, after markup is removed -- and the extractor now sees all of it.
# It used to see page[:6000], which on a real venue site cuts off mid-page: one
# venue mentioned its indoor hall at character 6850 and the must-have came back
# "unknown" because nothing ever read that far. Sonnet has a 1M context; 6k
# characters was a false economy paid for in escalations.
MAX_PAGE_CHARS = 30_000
_MAX_RAW_CHARS = 600_000       # guard against a pathological download

# Content that is never page text. noscript is deliberately NOT here: its text
# ("You need to enable JavaScript") is exactly the signal that a page is an
# unrendered shell, and page_problem() needs to see it.
_DROP_CONTENT = re.compile(r"(?is)<(script|style|svg|template|iframe)\b[^>]*>.*?</\1>")
_BLOCK_END = re.compile(r"(?i)</(p|div|section|article|li|tr|h[1-6]|blockquote)>|<br\s*/?>")


def html_to_text(raw: str) -> str:
    """Render markup down to the text a human would actually read.

    Real mode gets HTML off the wire; every layer downstream was designed for
    text, and handing it markup breaks them in ways that look like model errors:

      - EXTRACT sees page[:6000], which on a real site is <head>, analytics, and
        nav -- the venue's capacity and email are 50KB further down and never
        reach the model at all.
      - grounding.py matches a quote against the page character by character. A
        model correctly quoting the rendered "Pricing by request — email us"
        fails against the source's "&mdash;", and a true quote gets reported as
        fabricated evidence.

    So this runs on BOTH modes' output, not just real mode: if mock and real
    hand the pipeline differently shaped input, mock stops being evidence about
    real behavior, which is the entire point of having it.
    """
    if not raw:
        return ""
    text = _DROP_CONTENT.sub(" ", raw[:_MAX_RAW_CHARS])
    text = _BLOCK_END.sub("\n", text)          # keep paragraph boundaries
    text = re.sub(r"<[^>]+>", " ", text)       # drop the remaining tags
    text = html.unescape(text)                 # &mdash; -> em dash, &amp; -> &
    text = re.sub(r"[ \t ]+", " ", text)
    text = re.sub(r"\n\s*\n\s*", "\n\n", text)
    return text.strip()


def fetch_page(url: str) -> str:
    if config.MODE == "mock":
        pages = _fixtures("pages.json")
        if url not in pages:
            raise ToolError(f"mock page not found: {url}")  # exercises failure path
        return html_to_text(pages[url])[:MAX_PAGE_CHARS]

    def _call():
        resp = requests.get(url, timeout=20, headers={"User-Agent": "venue-agent/1.0"})
        resp.raise_for_status()
        # Convert BEFORE capping. Capping raw HTML at 20k can yield 20k of
        # <head> and zero venue text.
        return html_to_text(resp.text)[:MAX_PAGE_CHARS]

    return with_backoff(_call)


# --- page quality ---------------------------------------------------------
# A 200 OK is not the same as a usable page. Destination venues are heavily
# represented on JS-rendered site builders, so the common failure is not a dead
# link -- it is a live link that returns an app shell with no venue text in it.
# Extracting from that produces a confidently empty record, which then reads as
# "the page didn't say" rather than "we never saw the page". Catching it here
# also means we never spend a workhorse-model call on an empty shell.

# Deliberately LOW. This is a "there is nothing here at all" floor, not a
# "this page is thin" floor. A sparse venue page with two real sentences must
# still go to extraction and escalate on missing information -- that is a
# different verdict from "we never saw the page", and collapsing the two would
# hide fetch failures inside the incomplete-information bucket. The app-shell
# and error markers below are what actually do the detecting.
MIN_PAGE_WORDS = 12

_SHELL_MARKERS = re.compile(
    r"enable javascript|javascript is required|loading\.\.\.|please wait|"
    r"you need to enable|<div id=[\"']?(root|app)[\"']?>", re.I
)
_ERROR_MARKERS = re.compile(
    r"\b404\b|page not found|page cannot be found|access denied|forbidden|"
    r"temporarily unavailable|site is under construction", re.I
)


def page_problem(text: str) -> str | None:
    """Why this page is unusable for extraction, or None if it is fine."""
    if text is None or not text.strip():
        return "empty response body"

    # Script/style CONTENT must go before tags, or an app shell's inline bundle
    # counts as hundreds of "words" and reads as a full page.
    stripped = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", text)
    stripped = re.sub(r"<[^>]+>", " ", stripped)
    words = re.findall(r"[A-Za-z][A-Za-z'-]+", stripped)

    if _ERROR_MARKERS.search(text):
        return "page is an error or placeholder page"
    if _SHELL_MARKERS.search(text) and len(words) < 150:
        return "page is an unrendered app shell (JavaScript-only content)"
    if len(words) < MIN_PAGE_WORDS:
        return f"page has only {len(words)} words of text -- nothing to extract"
    return None
