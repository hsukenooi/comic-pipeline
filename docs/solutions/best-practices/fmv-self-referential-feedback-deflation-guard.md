---
title: "Guarding a self-referential valuation feedback loop against deflation bias"
date: 2026-07-04
category: best-practices
module: comic-fmv (apps/fmv) + comics-server FMV pricing (plugins/gixen-overlay)
problem_type: best_practice
component: service_object
severity: high
related_components:
  - comic-fmv
  - gixen-overlay
applies_when:
  - "Folding a system's own outcomes back into the valuation that drives those outcomes"
  - "Working on first-party comps, recency weighting, or the calibration report in comic-fmv"
  - "Feeding any censored or truncated sample back into a price, bid cap, or score"
tags: [fmv, feedback-loop, deflation-bias, money-path, truncation, bid-cap]
---

# Guarding a self-referential valuation feedback loop against deflation bias

## Context

The FMV auction-outcome feedback loop (BUI-286/287/288, PR #122) folds your own resolved eBay auctions back into the FMV that sets your Gixen bid caps. FMV is a **money path**: `max_bid = 0.80 x fmv_high`. The moment a valuation consumes its own downstream outcomes, it can enter a feedback spiral, and several of the guards that make it safe are non-obvious — two of them were only fully closed after adversarial review. This captures the guardrails so the next person touching first-party comps, recency weighting, or the calibration report does not silently reopen them.

## Guidance

**1. A truncated sample fed back into a valuation deflates it. Enforce "wins AND losses together" at TWO levels, not one.**

On a proxy-auction *win*, `winning_bid` is the underbidder's max — but the set of wins you observe is *truncated from above*: the auctions that cleared above your max became *losses*, so a wins-only sample is systematically low. Feeding wins-only back in drags FMV down → lowers `max_bid` → makes future wins cheaper → drags FMV down again. Losses are the missing right tail, so wins and losses must always enter together.

- **Query level** (`plugins/gixen-overlay/src/gixen_overlay/db.py`): `_OUTCOME_STATUSES_SQL = "'WON', 'LOST'"` is hardcoded with **no wins-only parameter** — no caller can *ask* for wins alone.
- **Per-book level** (`apps/fmv/src/fmv_runner.py` `_fetch_first_party_outcomes`): the query guard does **not** guarantee a given book actually *has* losses in-window (a book you have only ever won, or whose losses aged past the recency window). So after fetching, re-check the *actual composition*: `if "WON" in statuses and "LOST" not in statuses: return []` — drop the whole first-party contribution and price as today. A structural "you can't ask for wins-only" guard is **not** the same as a "this book's sample contains a loss" guarantee.

**2. Do NOT exempt first-party comps from IQR trimming — that is the deliberate seam between inline pricing and the calibration report (KTD-4).**

A lone loss far above the comp pool is, from one data point, indistinguishable from a two-bidder war. Let the inline IQR trim bound it (so a single outlier can't spike a bid cap), and let the **calibration report** surface the *systematic* case across many auctions. The calibration report keys on **overshoot vs `fmv_high`** (`median(winning_bid / fmv_high)` over losses), **never raw win/loss rate** — losing is the *intended* outcome of the 80% haircut, so a high loss count is not a mispricing signal. Ranking on loss rate would chase FMV upward until you overpay, defeating the haircut.

**3. Keep pure valuation math clock-free so its golden tests stay deterministic.**

`apps/fmv/src/fmv_math.py` must never call `datetime.now()`. Recency weighting decays each comp relative to the **newest `sold_date` in the pool** (a deterministic in-pool reference), not wall-clock time. And equal-weight pools **short-circuit to the byte-identical unweighted path** (`_weights_equal` → delegate to `quartile`/`statistics.median`), so no-date and same-date pools price exactly as before — which is why adding recency weighting left the 10-case golden fixture untouched.

## Why This Matters

FMV sets real bid caps. A deflation spiral doesn't announce itself in tests — it compounds slowly across pricing runs and shows up as "why am I winning everything cheap and losing the good stuff." The two-level guard, the IQR/calibration seam, and the clock-free reduction are each cheap to write and expensive to rediscover. The per-book guard specifically was a real gap that passed the first review (query-level guard looked sufficient) and was only caught adversarially.

## When to Apply

- Adding, weighting, or filtering first-party comps in the FMV pipeline.
- Building any feature that feeds a system's own outcomes back into the input that produced them (recommendation scores, dynamic prices, ranking signals).
- Changing the calibration report — resist any suggestion to "also surface high-loss-rate books"; that reintroduces the trap.

## Examples

Per-book truncation guard (the non-obvious half — the query already asked for both):

```python
# _fetch_first_party_outcomes — after the /api/comics/outcomes fetch:
statuses = {r.get("status") for r in rows}
if "WON" in statuses and "LOST" not in statuses:
    # wins-only in-window set is truncated-from-above → would deflate FMV
    return []   # drop the contribution; price as today
```

Clock-free degenerate reduction (keeps golden tests green):

```python
def weighted_median(prices, weights):
    if _weights_equal(weights):
        return statistics.median(prices)   # byte-identical to pre-weighting
    return weighted_quartile(prices, weights, 0.5)
```

Calibration ranks on overshoot, gated on a minimum loss count so a single blowout is not "persistent" (note: at the default `min_losses=2`, `median` of two values is their mean, so the gate reduces but does not eliminate single-outlier influence — the human weighs `loss_count`, not `overshoot` alone).

## Related

- Plan: `docs/plans/2026-07-04-001-feat-fmv-auction-outcome-feedback-plan.md` (Problem Frame + KTD-4).
- `docs/solutions/fmv-bid-linkage-gap-2026-05-23.md` — earlier FMV-linkage learning.
- Reusable Python gotcha from this work: `requests.exceptions.JSONDecodeError` subclasses **both** `ValueError` and `requests.RequestException`, so a bare `except ValueError` placed *after* `except requests.RequestException` is dead code — order the JSON-decode catch first (see `_get_json_or_warn` in `apps/fmv/src/fmv_runner.py`).
