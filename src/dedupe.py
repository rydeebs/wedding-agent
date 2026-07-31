"""
Venue identity: is this the same place we already looked at?

Search does not respect the region boundaries we search by. The same venue
comes back under two regions (a Tulum venue surfacing in a Cabo query), under
its own domain and an aggregator's, with tracking parameters bolted on. Left
alone, the couple sees the same venue twice on the shortlist -- and we pay to
extract and score it twice, and may draft it two different emails.

Two identity keys, checked in order:

  1. CANONICAL URL -- scheme, "www.", trailing slash, fragment, and tracking
     parameters removed. Cheap and safe: same page, different link.
  2. CANONICAL NAME -- the same venue on a different domain. Deliberately
     conservative: normalized names must match EXACTLY and must carry at least
     two meaningful tokens, because a false merge silently deletes a real
     candidate from the shortlist, which is worse than showing a duplicate.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlsplit

_TRACKING = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "mc_cid", "mc_eid", "ref", "source",
}

# Words that decorate a venue name without identifying it.
_NAME_NOISE = {
    "official", "site", "website", "home", "page", "wedding", "weddings",
    "venue", "venues", "the", "and", "a", "an", "of", "at", "in",
}


def canonical_url(url: str) -> str:
    """A URL reduced to the page it actually identifies."""
    p = urlsplit((url or "").strip().lower())
    host = p.netloc[4:] if p.netloc.startswith("www.") else p.netloc
    path = p.path.rstrip("/")
    query = sorted((k, v) for k, v in parse_qsl(p.query) if k not in _TRACKING)
    return f"{host}{path}" + (f"?{urlencode(query)}" if query else "")


def canonical_name(name: str) -> str:
    """A venue name reduced to its identifying tokens.

    'Casa Jaguar Tulum (Official Site)' and 'Casa Jaguar Tulum' both become
    'casa jaguar tulum'.
    """
    n = re.sub(r"\([^)]*\)", " ", (name or "").lower())   # drop "(Official Site)"
    n = re.sub(r"[^\w\s]", " ", n)                        # punctuation -> space
    tokens = [t for t in n.split() if t not in _NAME_NOISE]
    return " ".join(tokens)


def identity_keys(name: str, url: str) -> list[str]:
    """Every key under which this candidate should be remembered."""
    keys = [f"url:{canonical_url(url)}"]
    cname = canonical_name(name)
    if len(cname.split()) >= 2:      # too-generic names are not identities
        keys.append(f"name:{cname}")
    return keys


def find_duplicate(index: dict, name: str, url: str) -> tuple[str, dict] | None:
    """Return (matched_key, first_occurrence) if this candidate is already in
    `index`, else None. `index` maps identity key -> {url, name, region}."""
    for key in identity_keys(name, url):
        if key in index:
            return key, index[key]
    return None


def remember(index: dict, name: str, url: str, region: str) -> None:
    entry = {"url": url, "name": name, "region": region}
    for key in identity_keys(name, url):
        index.setdefault(key, entry)
