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
"The signal, and why it changed (BUI-532)" below for why loss-only data can't
carry the headline on its own.

**This report is diagnostic only.** It performs **zero writes** — no snipe,
no bid, no FMV upsert, no automated re-pricing. It reads
`GET /api/comics/calibration` on the comics server and prints a ranked table
for a human to act on. Any auto-nudge to `fmv_high` is explicitly out of
scope for this skill.

## The signal, and why it changed (BUI-532)

> **Headline: confirmed win-based exceedance — `contested_win_margin` where
> it is non-null and `> 1`. Secondary: loss-based `overshoot`, always labeled
> a censored upper bound, never a literal "raise `fmv_high` by this factor"
> number. Still never raw `loss_count` or a win/loss rate.**

Losing is the *intended* outcome of the 80% (or 60%, on low confidence) bid
haircut: you deliberately bid below fair value to bargain-hunt, so you are
*designed* to lose most auctions. A book with a huge loss count is not
mispriced by that fact alone — it's the haircut working exactly as designed,
as long as those losses clear **at or below** `fmv_high`. **Do not rank or
surface a book on `loss_count` or a win/loss ratio** — that reintroduces the
exact deflation/mispricing trap this report exists to avoid (R4 in the plan).

**What changed in BUI-532, and why:** earlier versions of this skill banned
`contested_win_margin` outright ("never `contested_win_margin`"). That ban
conflated two different claims: "don't treat a big bargain-win as if it makes
a book *safer* or less urgent" (still true, see below) with "win data can
never be evidence of mispricing" (false — and the actual bug BUI-532 fixes).
`overshoot` is broken two ways:

1. **Right-censored.** On a LOST auction, the recorded `winning_bid` is a
   floor (often just our `max_bid` plus one bid increment) — gixen has no way
   to observe what the actual winner paid. The true clearing price was
   **higher** than that floor, so any single loss's ratio *understates* how
   far that auction really cleared above `fmv_high`.
2. **Confounded by a moving `fmv_high`.** The `fmv` row a bid links to holds
   the **current**, recomputed value, but `max_bid` was frozen at snipe time.
   26% of rows have `max_bid >= fmv_high` today (the book's `fmv_high` has
   since moved, usually down) — a loss on one of those rows registers
   `overshoot > 1` by construction, regardless of what actually happened in
   the auction. That's a measurement artifact, not market evidence.

These two effects pull in different directions on paper — censoring alone
would *understate* the true ratio, while the frozen-`max_bid` confound
*inflates* it for the rows it touches — so `overshoot` isn't a clean bound by
construction alone. BUI-527's back-test
(`apps/fmv/scripts/fmv_high_calibration.py`, reference only — do not fork a
second copy of this analysis) shows which effect wins in this dataset: the
raw/naive exceedance rate (the fraction of rows whose recorded ratio exceeds
1) fell from 39% to 19.4% once collapsed-point rows (BUI-528, fixed
2026-07-24) and the frozen-`max_bid` rows were excluded — the confound, not
the censoring, is doing most of the inflating here. So treat any `overshoot`
value this report shows as a **censored upper bound**: it is more likely to
overstate the real signal than to understate it.

Wins carry no such distortion: a WON row's `winning_bid` is the exact price
you paid (bounded by your own `max_bid`, never floored at it), so
`contested_win_margin` is the one field in this report backed by fully
observed, uncensored data. BUI-527's evidence across the current dataset —
n=114 wins, median clearing at 0.57x `fmv_high`, only 4.4% exceedance — is
exactly why a win that *does* clear above `fmv_high` is trustworthy: it is
rare, and it is real. **A row with `contested_win_margin > 1` is the
strongest evidence this report can produce that `fmv_high` is too low** —
rank it ahead of every row that only has loss-based `overshoot` behind it.

A low `contested_win_margin` (well below 1) is *not* a counter-signal to
chase, and does not make a row "safer" or lower-priority than another
Unconfirmed row — it just means that particular win was a bargain. The only
promotion this rule allows is ranking a row **up** when its win-based margin
**exceeds 1**; never rank, filter, or promote a row because its margin is
*low*.

Every other mention of this rule below (response shape, Present the results,
Common mistakes) is a one-line pointer back to this section, not a separate
restatement — if you're tempted to relax it further, come edit it here. If
you are editing this skill or the server-side aggregate (`calibration_report`
in `plugins/gixen-overlay/src/gixen_overlay/db.py`), re-read this section and
the Problem Frame in
`docs/plans/2026-07-04-001-feat-fmv-auction-outcome-feedback-plan.md` first.
Note the `calibration_report` docstring and the plan's R4 text still describe
the pre-BUI-532 "`overshoot` is the ranking key, `contested_win_margin` is
context only" framing — that server-side documentation (and the server's own
sort order and surfacing gate) predates this skill's rebase and was left
unchanged by BUI-532 (doc-only ticket); this section is the current, correct
read.

A book with **only wins**, or **no resolved auctions at all**, never appears
in this report — there is no loss to measure `overshoot` from, and the
server-side gate (unchanged, and **independent** of the Confirmed/Unconfirmed
split above) requires at least one qualifying loss regardless of how strong a
book's win-based signal is. This is a real gap BUI-532 does not close: a book
that wins every auction with `contested_win_margin > 1` several times, but
never loses `min_losses` times, would never surface here even though that
would be the strongest possible evidence of underpricing — flag it as a
follow-up if it matters for your use case. A book whose losses all cleared at
or below `fmv_high` — however many losses it has — is likewise never
surfaced (the server-side R4 guard).

**A book must have lost at least `min_losses` times in-window to surface at
all (default 2).** A single loss — however far above `fmv_high` it cleared —
is one bidding-war outlier, not a persistent pattern; the report suppresses
it as noise rather than ranking a book on one data point. Pass `min_losses`
as a query param to change the floor (e.g. `min_losses=3` for a stricter
gate); it can never be used to relax the loss-count-is-not-the-signal rule
above, only to raise or lower how many *qualifying* (above-`fmv_high`-median)
losses are required before a row surfaces at all.

## Prerequisites

**`COMICS_SERVER_URL` must be set.** Set it once in `~/.zshrc`:

```bash
# MacBook (connects to Mac Mini over Tailscale)
export COMICS_SERVER_URL=http://mac-mini.tail9b7fa5.ts.net:8080

# Mac Mini (running locally)
export COMICS_SERVER_URL=http://localhost:8080
```

`GIXEN_SERVER_URL` is a deprecated alias — it still works but emits a
warning. Migrate to `COMICS_SERVER_URL`.

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

Optional `min_losses` query param (default 2 — a book must have lost at least
this many times in-window to surface; see "The signal, and why it changed
(BUI-532)" above for why a single loss doesn't count):

```bash
comics-api GET "/api/comics/calibration?min_losses=3" || exit 1
```

## Response shape

One object per flagged `(issue, grade)`. The comics-server response itself is
still ordered by loss-based `overshoot` descending (unchanged server-side
behavior) — **do not present rows in that order.** Partition and re-sort per
"Present the results" below; the API's own ordering is not the headline
ranking as of BUI-532.

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
  "contested_win_margin": 0.4
}
```

- `contested_win_margin` — `median(winning_bid / fmv_high)` over **wins**, or
  `null` when `win_count` is 0. **The headline field as of BUI-532** whenever
  it is non-null and `> 1`: uncensored, exact evidence `fmv_high` is too low
  for that row. A non-null value `<= 1` is not a counter-signal — see "The
  signal, and why it changed (BUI-532)" above — it just means treat the row
  as Unconfirmed, the same as a row where this field is `null`.
- `overshoot` — `median(winning_bid / fmv_high)` over **losses**. A
  **censored, confounded upper bound**, not a literal re-price factor — see
  "The signal, and why it changed (BUI-532)" above. Only rows with
  `overshoot > 1` appear at all (the server-side gate, unchanged).
- `above_fmv_loss_rate` — % of losses where `winning_bid > fmv_high`. Context
  only, subject to the same censoring/confound caveats as `overshoot` — never
  re-sort by it.
- `win_count` — how many resolved wins back `contested_win_margin`. `0` means
  this row has **no uncensored data at all**; its only evidence is the
  censored `overshoot`. A Confirmed row can rest on as few as one win — that
  number is exact (no floor/censoring uncertainty), but it's still one data
  point; weigh `win_count` the same way you'd already weigh a thin
  `loss_count`, rather than treating every Confirmed row as equally solid.

## Present the results

Partition the response into two tiers and render **Confirmed first**:

1. **Confirmed** — `contested_win_margin` is non-null **and** `> 1`. Sort by
   `contested_win_margin` descending. This is the headline list (BUI-532):
   real money actually cleared above `fmv_high`.
2. **Unconfirmed (censored)** — everything else that still passed the
   server's loss-based gate. Sort by `overshoot` descending (same ordering
   the API already returns them in), but label the column so a reader never
   mistakes it for a confirmed number.

A `jq` split to do this after the `comics-api` call above:

```bash
comics-api GET /api/comics/calibration | jq '
  { confirmed:   ([.[] | select(.contested_win_margin != null and .contested_win_margin > 1)]
                  | sort_by(-.contested_win_margin)),
    unconfirmed: ([.[] | select(.contested_win_margin == null or .contested_win_margin <= 1)]
                  | sort_by(-.overshoot)) }'
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
| Treating a high `loss_count` as the signal | It isn't — see "The signal, and why it changed (BUI-532)" above. |
| Presenting rows in the API's own (loss-`overshoot`-sorted) order | Partition into Confirmed/Unconfirmed and re-sort per "Present the results" — the API order is not the headline ranking as of BUI-532. |
| Treating `overshoot` as a literal "raise `fmv_high` by this factor" number | It's a censored, confounded upper bound — see "The signal, and why it changed (BUI-532)" above. |
| Ranking an Unconfirmed row above a Confirmed one | `contested_win_margin > 1` is always the more trustworthy signal, regardless of how large an Unconfirmed row's `overshoot` looks. |
| Rendering an empty table on a failed `comics-api` call | STOP and report the error instead — the hard-fail-loud rule this skill shares with every other `/comic:*` server call. |
| Assuming this report writes anything | It never does. `fmv_high` only changes when you explicitly re-run `/comic:fmv` afterward. |

---

Plan: `docs/plans/2026-07-04-001-feat-fmv-auction-outcome-feedback-plan.md` — BUI-288 (Issue C).
Metric rebase: BUI-532, evidenced by BUI-527's back-test
(`apps/fmv/scripts/fmv_high_calibration.py`, PR #330).
