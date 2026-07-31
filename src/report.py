"""
Observe stage: turn the run's report.json into a human-readable HTML comparison
table. This is the artifact the couple actually looks at, and it films well.
"""

from __future__ import annotations

import html
import json
import os


def _pill(text, kind):
    colors = {
        "recommend": ("#0f7b3f", "#e6f4ec"),
        "escalate": ("#8a5a00", "#fbf0da"),
        "reject": ("#9b1c1c", "#fce8e8"),
    }
    fg, bg = colors.get(kind, ("#333", "#eee"))
    return f'<span style="background:{bg};color:{fg};padding:2px 8px;border-radius:999px;font-size:12px;font-weight:600">{html.escape(text)}</span>'


def _price(rec):
    sig = rec.get("pricing_signal")
    if sig == "listed":
        lo = rec.get("price_low_usd")
        hi = rec.get("price_high_usd")
        s = f"${lo:,}" + (f"–${hi:,}" if hi else "+")
        return f"{s} <span style='color:#888;font-size:11px'>(listed)</span>"
    if sig == "request_only":
        return "<span style='color:#888'>by request</span>"
    return "<span style='color:#bbb'>not stated</span>"


def render(report: dict, out_path: str):
    rows = []
    for s in report["all_scored"]:
        rec = s["record"]
        cap = rec.get("capacity_max")
        rows.append(f"""
        <tr>
          <td><strong>{html.escape(rec['name'])}</strong><br>
              <span style="color:#888;font-size:12px">{html.escape(rec['region'])}</span></td>
          <td>{cap if cap else '<span style="color:#bbb">?</span>'}</td>
          <td>{'✓' if rec.get('has_onsite_lodging') else '<span style="color:#bbb">?</span>'}</td>
          <td>{'✓' if rec.get('has_indoor_backup') else '<span style="color:#bbb">?</span>'}</td>
          <td>{_price(rec)}</td>
          <td>{s['score']:.2f}</td>
          <td>{_pill(s['decision'], s['decision'])}</td>
          <td style="font-size:12px;color:#555;white-space:pre-line">{html.escape(s['rationale'])}</td>
        </tr>""")

    summ = report["summary"]
    cost = summ["cost"]
    doc = f"""<!doctype html><meta charset="utf-8">
<title>Wedding Venue Shortlist</title>
<div style="font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;max-width:1000px;margin:32px auto;color:#1a1a1a">
  <h1 style="margin-bottom:4px">Wedding Venue Shortlist</h1>
  <p style="color:#666;margin-top:0">
    {summ['venues_evaluated']} venues evaluated across {summ['regions']} regions ·
    {summ['recommended']} recommended · {summ['escalated']} escalated to human ·
    {summ.get('duplicates_collapsed', 0)} duplicate listings collapsed ·
    {summ['steps']} agent steps · ${cost['usd_total']} total
    (${summ['cost_per_venue_usd']}/venue)
  </p>
  <table style="border-collapse:collapse;width:100%;font-size:14px">
    <thead>
      <tr style="text-align:left;border-bottom:2px solid #222">
        <th style="padding:8px">Venue</th><th>Cap</th><th>Lodging</th><th>Backup</th>
        <th>Price</th><th>Score</th><th>Decision</th><th>Why</th>
      </tr>
    </thead>
    <tbody>{''.join(rows)}</tbody>
  </table>
  <p style="color:#888;font-size:12px;margin-top:16px">
    Emails are <strong>drafted, not sent</strong> — a human reviews and sends each one.
    Venues with missing information are escalated rather than guessed.
  </p>
</div>"""
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as f:
        f.write(doc)
    return out_path
