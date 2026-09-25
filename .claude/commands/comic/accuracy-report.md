---
name: comic:accuracy-report
description: Diagnostic-only fixed-window FMV accuracy report — scores every resolved auction's final price against the FMV band midpoint it was actually bid against (no admit gate, unlike the calibration report). Use to see how well FMV pricing is doing overall, by month, and by WON/LOST. Never bids, snipes, or writes to FMV.
---

# Comic FMV Accuracy Report

A real-estate-style fixed-window accuracy report: for every resolved
(WON/LOST) auction, score the final price (`winning_bid`) against the FMV
band midpoint `(low+high)/2` **that was actually in force when the bid was
added** — not the current `fmv` row, which can already have drifted (and,
via the comp-pool feedback loop, could even have absorbed that same
auction's own price). This is distinct from `/comic:calibration-report`,
which ranks a *subset* of books whose `fmv.high` looks too low; this report
scores *every* eligible resolved auction, with no admit gate, so it answers
"how accurate is FMV pricing overall" rather than "which books need a
repricing pass."

**Diagnostic only — zero writes.** It reads `GET /api/comics/accuracy` on
the comics server and prints summary tables. Nothing here changes an `fmv`
row or a bid.

## Prerequisites

`comics-api` resolves the comics server and health-gates it before every
call — see `docs/conventions/comics-server-call.md`. On an unrecognised
machine, export `COMICS_SERVER_URL` explicitly.

## Run the report

```bash
comics-api GET /api/comics/accuracy || exit 1
```

**If the call fails: STOP and report the error** — a failed call must never
render as "no data" (the hard-fail-loud rule every `/comic:*` server call
shares).

Optional `days` query param — unlike `/comic:calibration-report` (default
180), this report defaults to **no recency bound at all**: every resolved
auction on file:

```bash
comics-api GET "/api/comics/accuracy?days=90" || exit 1
```

Optional `include_rows=true` to also get one row per scored auction (bid id,
item id, comic id, title, issue, grade, certifier, status, price, low, high,
band source, fmv_history id, confidence, comps, notes, ended) — pass it when
you need to drill into individual auctions rather than just the aggregates:

```bash
comics-api GET "/api/comics/accuracy?include_rows=true" || exit 1
```

## Response shape

```json
{
  "days": null,
  "overall": {
    "n": 462,
    "share_within_10pct": 25.5,
    "share_within_20pct": 43.3,
    "mdape_pct": 25.0,
    "mean_signed_error_pct": 4.1,
    "in_band_pct": 39.8,
    "above_band_pct": 38.5,
    "below_band_pct": 21.6,
    "median_band_width_pct": 55.2,
    "by_status": { "WON": { "n": 210, "...": "..." }, "LOST": { "n": 252, "...": "..." } }
  },
  "by_month": [
    { "month": "2026-08", "n": 41, "...": "...", "by_status": { "WON": {"...": "..."}, "LOST": {"...": "..."} } }
  ],
  "band_source_counts": { "history": 401, "current_fmv": 61 },
  "by_width_bucket": {
    "zero": { "n": 64, "in_band_pct": 6.3, "...": "..." },
    "under_30pct": { "n": 106, "in_band_pct": 27.4, "...": "..." },
    "30_50pct": { "n": 69, "in_band_pct": 40.6, "...": "..." },
    "50pct_plus": { "n": 206, "in_band_pct": 57.3, "...": "..." }
  },
  "by_prepost_bui528": {
    "pre_bui_528": { "n": 313, "...": "..." },
    "post_bui_528": { "n": 132, "...": "..." },
    "unknown": { "n": 0, "...": "..." }
  }
}
```

- **`n`** — count of eligible auctions in the slice. Every other metric is
  `null` when `n` is 0 (no data, never rendered as a false 0%/100%).
- **`share_within_10pct` / `share_within_20pct`** — % of prices within that
  band of the midpoint.
- **`mdape_pct`** — median absolute percentage error vs the midpoint.
- **`mean_signed_error_pct`** — mean signed error vs the midpoint;
  **positive means the price cleared above the midpoint, i.e. FMV priced the
  book low** relative to what it actually sold for. Negative means FMV
  priced it high.
- **`in_band_pct` / `above_band_pct` / `below_band_pct`** — where the price
  landed relative to the stored `[low, high]` band (these three sum to
  ~100%; a wider band scores better here by construction, which is exactly
  why the fixed-window metrics above it matter more).
- **`median_band_width_pct`** — median `(high-low)/midpoint`, as context for
  reading the coverage numbers.
- **`by_status`** — the same metric set, split WON vs LOST, at both the
  `overall` level and inside each `by_month` entry.
- **`band_source_counts`** — how many scored rows used a fixed `fmv_history`
  snapshot from before the bid was placed ("history", the trustworthy case)
  vs. the current `fmv` row because no such snapshot existed ("current_fmv",
  a fallback that can be less reliable — see `fmv_accuracy_report`'s
  docstring in `plugins/gixen-overlay/src/gixen_overlay/db.py` for the
  leakage this distinction exists to surface). With `include_rows=true`,
  each row also carries its own `band_source`.
- **`by_width_bucket`** (BUI-983) — the same metric set as `overall`, sliced
  by band width `(high-low)/midpoint`: `zero` (`low == high` — the legacy
  shape BUI-528 stopped producing on 2026-07-24), `under_30pct`, `30_50pct`,
  `50pct_plus`. **The zero-width bucket alone depresses the whole baseline by
  about 6 points** — those 64 rows sit at ~6% in band vs. ~46% for everything
  else (`docs/audit/2026-09-24-above-band-misses.md`), so as they age out of
  the recency window a later run's `overall.in_band_pct` can rise even though
  current pricing hasn't improved. Read `by_width_bucket` alongside `overall`
  to tell the two apart, and don't compare one run's `overall` against
  another's unless their `zero` counts are similar.
- **`by_prepost_bui528`** (BUI-983) — the same metric set as `overall`,
  sliced by each row's own band-write timestamp (the `fmv_history` snapshot's
  `recorded_at` for a `band_source="history"` row, or the `fmv` row's
  `updated_at` for `"current_fmv"`) against BUI-528's merge cutoff:
  `pre_bui_528`, `post_bui_528`, `unknown` (no usable timestamp — reported
  explicitly, never silently dropped). **Compare a post-BUI-528 run against
  `by_prepost_bui528.post_bui_528` of an earlier run, not its `overall`** —
  `overall` still blends in whatever pre-fix bands remain on file.

## Present the results

Render five tables:

1. **Overall** — the top-level metrics, one row.
2. **By status** — `overall.by_status.WON` and `overall.by_status.LOST` side
   by side.
3. **By month** — one row per `by_month` entry, sorted chronologically (the
   server already returns them that way), each showing at minimum `n`,
   `share_within_20pct`, `mdape_pct`, and `mean_signed_error_pct` so a trend
   is visible at a glance.
4. **By width bucket** — `by_width_bucket.zero` / `under_30pct` / `30_50pct`
   / `50pct_plus`, each showing at minimum `n` and `in_band_pct`.
5. **Pre/post BUI-528** — `by_prepost_bui528.pre_bui_528` / `post_bui_528` /
   `unknown`, same columns as the width table.

Call out `band_source_counts` once, near the top — a high `current_fmv`
share means a meaningful slice of the report is scored against the fallback
rather than a fixed-at-bid-time snapshot, which is worth knowing before
trusting the numbers too far.

An `overall.n` of 0 means no eligible resolved auctions are on file (or the
`days` window excluded all of them) — report that plainly and stop.

## Common mistakes

| Mistake | Fix |
|---|---|
| Treating `in_band_pct` as the headline accuracy number | It isn't — a wider band scores better on it by construction. Lead with `share_within_10pct`/`share_within_20pct` and `mdape_pct` instead. |
| Reading a positive `mean_signed_error_pct` as "FMV overpriced" | Backwards — positive means the price cleared *above* the midpoint, i.e. FMV priced the book *low*. |
| Assuming `days` defaults to 180 like `/comic:calibration-report` | It defaults to `null` (no bound) here — pass `days` explicitly to window it. |
| Rendering an empty report on a failed `comics-api` call | STOP and report the error instead, per the hard-fail-loud rule every other `/comic:*` server call follows. |
| Ignoring `band_source_counts` | A high `current_fmv` share is a caveat on the whole report's trustworthiness, not a detail to skip. |
| Comparing two runs' bare `overall.in_band_pct` to claim pricing improved | `overall` still blends in `by_width_bucket.zero` legacy rows, which age out of the recency window over time and can lift `overall` on their own — compare `by_prepost_bui528.post_bui_528` instead, or check that both runs' `zero` counts are similar. |

---

Ticket: BUI-977. See calibration-report.md § The signal for why the
existing calibration report is a ranked, admit-gated subset rather than the
overall accuracy picture this report gives.
