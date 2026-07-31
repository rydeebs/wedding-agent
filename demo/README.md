# demo/

A snapshot of one real run, committed so a fresh deployment has something to
show. `runs/` is git-ignored, so a new container starts with no history at all;
without this the public URL would render "No dashboard yet."

`server.py` falls back to this directory only when `runs/` contains no finished
run. On your own machine your real runs always win.

**Every email address has been stripped.** The original run extracted 30+ venue
contact addresses from public pages — including at least one personal Gmail.
Republishing scraped contact details on a public site is not something a demo
needs to do, so `contact_value` is nulled, `contact_method` is downgraded to
`form`, and a full-text pass replaced any address embedded in a rationale or
detail field with `[email removed]`.

Everything else is the real run: 150 candidates, 50 venues scored, 6
recommended, 43 escalated, $1.97.
