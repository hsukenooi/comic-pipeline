---
name: comic:calibration-report
description: Diagnostic-only report ranking issues whose FMV is set too low. Headlined by confirmed win-based exceedance over fmv_high (BUI-532), with loss-based overshoot kept as a labeled, censored secondary signal. Use to decide which books need comic-fmv recomputed. Never bids, snipes, or writes to FMV.
---

# Comic Calibration Report

Rank priced `(issue, grade)` books whose FMV is set too low — the "learn from
losing, without learning the wrong lesson" loop (Issue C / BUI-288 in the
auction-outcome-feedback plan). The **headline signal (BUI-532)** is
**confirmed win-based exceedance**: `contested_win_margin`, the median
`winning_bid / fmv_high` over auctions you actually **won**. A WON row's
`winning_bid` is the exact price paid — no estimation involved — so a book
whose *wins* clear above `fmv_high` is unambiguous, uncensored evidence that
`fmv_high` is too low. `overshoot` (the median ratio over **losses**) is kept
as a secondary signal, explicitly labeled a **censored upper bound** — see
"The signal" below for why loss-only data can't
carry the headline on its own.

**This report is diagnostic only.** It performs **zero writes** — no snipe,
no bid, no FMV upsert, no automated re-pricing. It reads
`GET /api/comics/calibration` on the comics server and prints a ranked table
for a human to act on. Any auto-nudge to `fmv_high` is explicitly out of
scope for this skill.

## The signal

> **Headline: confirmed win-based exceedance — `contested_win_margin` where
> it is non-null and `> 1`. Secondary: loss-based `overshoot`, always labeled
> a censored upper bound, never a literal "raise `fmv_high` by this factor"
> number. Never raw `loss_count` or a win/loss rate.**

Losing is the *intended* outcome of the 80% (or 60%, on low confidence) bid
haircut: you deliberately bid below fair value to bargain-hunt, so you are
*designed* to lose most auctions. A book with a huge loss count is not
mispriced by that fact alone — it's the haircut working exactly as designed,
as long as those losses clear **at or below** `fmv_high`. **Do not rank or
surface a book on `loss_count` or a win/loss ratio** — that reintroduces the
exact deflation/mispricing trap this report exists to avoid (R4 in the plan).

**Why wins carry the headline and losses cannot.** `overshoot` (the median
`winning_bid / fmv_high` over losses) is distorted two ways:

1. **Right-censored.** On a LOST auction the recorded `winning_bid` is a
   floor (often just our `max_bid` plus one increment) — gixen never observes
   what the winner actually paid, so a loss's ratio *understates* how far the
   auction really cleared above `fmv_high`.
2. **Confounded by a moving `fmv_high`.** The `fmv` row a bid links to holds
   the **current**, recomputed value, while `max_bid` was frozen at snipe
   time; a loss on a row whose `fmv_high` has since moved down registers
   `overshoot > 1` by construction. BUI-527's back-test
   (`apps/fmv/scripts/fmv_high_calibration.py`, reference only — do not fork
   a second copy) showed this confound, not the censoring, does most of the
   inflating, so treat any `overshoot` as a **censored upper bound** that is
   more likely to overstate the real signal than understate it.

Wins carry no such distortion: a WON row's `winning_bid` is the exact price
paid, so `contested_win_margin` (the median ratio over wins) is the one field
backed by fully observed data. Wins that clear above `fmv_high` are rare (4.4%
of 114 wins in BUI-527's dataset), which is exactly why one that does is
trustworthy. **A row with `contested_win_margin > 1` is the strongest evidence
this report can produce that `fmv_high` is too low** — rank it ahead of every
row that only has loss-based `overshoot` behind it.

A low `contested_win_margin` (well below 1) is *not* a counter-signal, and
does not make a row "safer" than an Unconfirmed row — it just means that win
was a bargain. The only promotion this rule allows is ranking a row **up**
when its win-based margin **exceeds 1**; never rank, filter, or promote a row
because its margin is *low*.

**Admit paths.** A book surfaces when *either* fires:

- **win-backed** — `contested_win_margin > 1`, with **no loss requirement**
  (a book that won every auction above `fmv_high` still surfaces); or
- **loss-backed** — at least `min_losses` losses in-window (default 2) with
  `overshoot > 1`. A single loss, however far above `fmv_high`, is one
  bidding-war outlier, not a pattern, so it is suppressed as noise.

`min_losses` governs only the loss-based path (pass it as a query param, e.g.
`min_losses=3`, to tighten it); it never gates the win-based path and can
never relax the loss-count-is-not-the-signal rule. Only a book with no
resolved auctions at all, or one whose losses *and* wins all cleared at or
below `fmv_high`, is omitted (the server-side R4 guard). Each row carries
`win_backed` / `loss_backed` booleans (see "Response shape" below) so you can
tell which path fired without knowing the `min_losses` the call used.

If you are editing this skill or the server-side aggregate (`calibration_report`
in `plugins/gixen-overlay/src/gixen_overlay/db.py`), re-read this section and
the Problem Frame in
`docs/plans/2026-07-04-001-feat-fmv-auction-outcome-feedback-plan.md` first;
the history of the metric rebase is in BUI-532 and BUI-543.

## Prerequisites

`comics-api` resolves the comics server itself (`COMICS_SERVER_URL` if set,
else the Mac Mini / MacBook hostname convention in `scripts/comics-server.sh`)
and health-gates it before every call, so no per-shell setup is needed. On an
unrecognised machine, export `COMICS_SERVER_URL` explicitly.

## Run the report

Per the shared comics-server call convention (BUI-172/BUI-510,
`docs/conventions/comics-server-call.md`) — don't hand-roll URL resolution or
the health check here, just call `comics-api`:

```bash
comics-api GET /api/comics/calibration || exit 1
```

**If the call fails: STOP and report the error** — a failed call must never
render as "nothing to re-price" (a hard-fail-loud rule shared with every
other `/comic:*` server call). A genuine "no calibration signal" result is
the JSON array `[]` with exit 0.

Optional `days` query param (default 180 — matches the recency window
`/api/comics/outcomes` uses for first-party comps):

```bash
comics-api GET "/api/comics/calibration?days=90" || exit 1
```

Optional `min_losses` query param (default 2 — the loss-based admit path needs
at least this many in-window losses; see "The signal" above):

```bash
comics-api GET "/api/comics/calibration?min_losses=3" || exit 1
```

## Response shape

One object per flagged `(issue, grade)`. The comics-server response is
ordered win-backed-first (each tier sorted by its own metric descending). You
still need to **partition into two labeled tiers** for
presentation (see "Present the results" below); use the `win_backed` /
`loss_backed` booleans rather than re-deriving the split from
`contested_win_margin`/`overshoot` thresholds.

```json
{
  "comic_id": 42,
  "title": "The Amazing Spider-Man (1963)",
  "issue": "129",
  "year": 1973,
  "grade": 8.0,
  "fmv_high": 100.0,
  "loss_count": 4,
  "above_fmv_loss_count": 3,
  "above_fmv_loss_rate": 75.0,
  "overshoot": 1.2,
  "win_count": 1,
  "contested_win_margin": 0.4,
  "win_backed": false,
  "loss_backed": true
}
```

- `contested_win_margin` — `median(winning_bid / fmv_high)` over **wins**, or
  `null` when `win_count` is 0. **The headline field (BUI-532)** whenever
  it is non-null and `> 1`: uncensored, exact evidence `fmv_high` is too low
  for that row. A non-null value `<= 1` is not a counter-signal — see "The
  signal" above — it just means treat the row
  as Unconfirmed, the same as a row where this field is `null`.
- `overshoot` — `median(winning_bid / fmv_high)` over **losses**, or `null`
  when `loss_count` is 0. A **censored, confounded upper bound**, not a
  literal re-price factor — see "The signal"
  above. Reported as context on every row; it is the row's *ranking* metric
  only when `loss_backed` is `true`.
- `above_fmv_loss_rate` — % of losses where `winning_bid > fmv_high`, or
  `null` when `loss_count` is 0. Context only, subject to the same
  censoring/confound caveats as `overshoot` — never re-sort by it.
- `win_count` — how many resolved wins back `contested_win_margin`. `0` means
  this row has **no uncensored data at all**; its only evidence is the
  censored `overshoot`. A Confirmed row can rest on as few as one win — that
  number is exact (no floor/censoring uncertainty), but it's still one data
  point; weigh `win_count` the same way you'd already weigh a thin
  `loss_count`, rather than treating every Confirmed row as equally solid.
- `win_backed` (bool) — `true` iff `contested_win_margin` is
  non-null and `> 1`. This is the Confirmed/headline signal; equivalent to,
  and simpler than, re-checking `contested_win_margin` yourself.
- `loss_backed` (bool) — `true` iff the row independently clears
  the loss-based gate (`loss_count >= min_losses` and `overshoot > 1`). At
  least one of `win_backed` / `loss_backed` is always `true`; a row can have
  both `true` at once (won convincingly *and* lost persistently above
  `fmv_high`).

## Present the results

Partition the response into two tiers and render **Confirmed first**:

1. **Confirmed** — `win_backed` is `true`. Sort by `contested_win_margin`
   descending (the server already returns this tier in this order, but sort
   defensively rather than depend on it). This is the headline list: real
   money actually cleared above
   `fmv_high`, whether or not the book has any qualifying losses.
2. **Unconfirmed (censored)** — `win_backed` is `false` (every row remaining
   here has `loss_backed: true`, since that's the only other way to be in
   the response). Sort by `overshoot` descending, and label the column so a
   reader never mistakes it for a confirmed number.

A `jq` split to do this after the `comics-api` call above:

```bash
comics-api GET /api/comics/calibration | jq '
  { confirmed:   ([.[] | select(.win_backed)]  | sort_by(-.contested_win_margin)),
    unconfirmed: ([.[] | select(.win_backed | not)] | sort_by(-.overshoot)) }'
```

Render each tier as its own table, most urgent first within the tier:

```
| Issue                              | Grade | FMV High | Signal                                          | Losses | Wins (context) |
|---|---|---|---|---|---|
| The Amazing Spider-Man #129 (1973) | 8.0   | $100.00  | Confirmed 1.35x (win)                          | 4      | 2 @ 1.35x       |
| Uncanny X-Men #142 (1980)          | 9.2   | $250.00  | Unconfirmed — overshoot 1.20x (censored upper bound) | 3 | 0             |
```

- **Signal** carries either `Confirmed <margin>x (win)` or `Unconfirmed —
  overshoot <ratio>x (censored upper bound)` — never blend the two numbers
  into one column, and never let an Unconfirmed row outrank a Confirmed one.
- **Wins (context)** shows the raw win count and `contested_win_margin` (if
  any) in parens, whether or not the row is Confirmed — a Confirmed row's
  win count/margin here should match the number driving its Signal.
- An empty response (`[]`) means no book currently needs re-pricing — report
  this plainly and stop; there is nothing else to do.

## After the report

For each flagged issue — Confirmed rows first, then Unconfirmed rows starting
from the highest `overshoot` — re-run `/comic:fmv` for that `(issue, grade)`
so it recomputes with fresh comps (which by now likely include the very
auctions that flagged it, via BUI-286's first-party-comp injection). This
skill does not do that automatically — recomputing FMV, and any resulting
change to future bid caps, is a deliberate, reviewed human action, not
something this report triggers on its own.

## Scheduling

Designed to run **unattended on a recurring schedule** (e.g. weekly via
`/schedule` or local cron) — it's a single cheap read (one aggregate query on
the comics server, no eBay calls, no LLM calls), so there is no caching
concern like `/comic:wishlist-sellers` has. A steady-state run that returns
`[]` should be silent; only notify when the list is non-empty.

## Common mistakes

| Mistake | Fix |
|---|---|
| Treating a high `loss_count` as the signal | It isn't — see "The signal" above. |
| Rendering the raw API order without labeled tiers | The API returns win-backed-first order, but still render Confirmed/Unconfirmed as separately labeled tables per "Present the results" — an unlabeled combined list still hides which rows are exact vs. censored evidence. |
| Assuming a zero-loss book can never surface | A `win_backed: true` row surfaces regardless of `loss_count`, including 0. Only a book with no resolved auctions at all, or one where neither admit path fires, is omitted. |
| Treating `overshoot` as a literal "raise `fmv_high` by this factor" number | It's a censored, confounded upper bound — see "The signal" above. |
| Ranking an Unconfirmed row above a Confirmed one | `win_backed: true` (`contested_win_margin > 1`) is always the more trustworthy signal, regardless of how large an Unconfirmed row's `overshoot` looks. |
| Rendering an empty table on a failed `comics-api` call | STOP and report the error instead — the hard-fail-loud rule this skill shares with every other `/comic:*` server call. |
| Assuming this report writes anything | It never does. `fmv_high` only changes when you explicitly re-run `/comic:fmv` afterward. |

---

Plan: `docs/plans/2026-07-04-001-feat-fmv-auction-outcome-feedback-plan.md` — BUI-288 (Issue C).
Metric history: BUI-532 (win-based headline, evidenced by BUI-527's back-test in
`apps/fmv/scripts/fmv_high_calibration.py`, PR #330) and BUI-543 (server-side
win-backed admit path + the `win_backed`/`loss_backed` fields).
