---
name: comic:calibration-report
description: Diagnostic-only report ranking issues whose FMV is set too low, based on how far their LOST auctions cleared above fmv_high. Use to decide which books need comic-fmv recomputed. Never bids, snipes, or writes to FMV.
---

# Comic Calibration Report

Rank priced `(issue, grade)` books whose **losses are clearing above `fmv_high`**
— the signal that a book's FMV is set too low and `comic-fmv` should be
recomputed for it. This is the "learn from losing, without learning the wrong
lesson" loop (Issue C / BUI-288 in the auction-outcome-feedback plan).

**This report is diagnostic only.** It performs **zero writes** — no snipe,
no bid, no FMV upsert, no automated re-pricing. It reads
`GET /api/comics/calibration` on the comics server and prints a ranked table
for a human to act on. Any auto-nudge to `fmv_high` is explicitly out of
scope for this skill.

## The one rule that must never be "fixed"

> **The signal is OVERSHOOT vs `fmv_high` — never raw win/loss rate, and
> never `contested_win_margin`.**

Losing is the *intended* outcome of the 80% (or 60%, on low confidence) bid
haircut: you deliberately bid below fair value to bargain-hunt, so you are
*designed* to lose most auctions. A book with a huge loss count is not
mispriced by that fact alone — it's the haircut working exactly as designed,
as long as those losses clear **at or below** `fmv_high`. The only honest
signal that FMV is too low is that losses persistently clear **above**
`fmv_high`. **Do not rank or surface a book on `loss_count`, a win/loss
ratio, or `contested_win_margin` instead of `overshoot`** — that
reintroduces the exact deflation/mispricing trap this report exists to avoid
(R4 in the plan). Every other mention of this rule below (response shape,
Common mistakes) is a one-line pointer back to this section, not a separate
restatement — if you're tempted to relax the rule anywhere, come edit it
here. If you are editing this skill or the server-side aggregate
(`calibration_report` in `plugins/gixen-overlay/src/gixen_overlay/db.py`) and
find yourself reaching for one of those banned fields as a ranking key —
stop, re-read this section, and re-read the Problem Frame in
`docs/plans/2026-07-04-001-feat-fmv-auction-outcome-feedback-plan.md` first
(the plan and the `calibration_report` docstring carry the extended
rationale; this section is intentionally the short version).

A book with **only wins**, or **no resolved auctions at all**, never appears
in this report — there is no loss to measure overshoot from. A book whose
losses all cleared at or below `fmv_high` — however many losses it has — is
likewise never surfaced (the server-side R4 guard).

**A book must have lost at least `min_losses` times in-window to surface at
all (default 2).** A single loss — however far above `fmv_high` it cleared —
is one bidding-war outlier, not a persistent pattern; the report suppresses
it as noise rather than ranking a book on one data point. Pass `min_losses`
as a query param to change the floor (e.g. `min_losses=3` for a stricter
gate); it can never be used to relax R4's loss-count-is-not-the-signal rule,
only to raise or lower how many *qualifying* (above-`fmv_high`-median) losses
are required before a row surfaces.

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

Per the shared comics-server call convention (BUI-172,
`docs/conventions/comics-server-call.md`) — don't hand-roll URL resolution or
the health check here:

```bash
source "$(git rev-parse --show-toplevel)/scripts/comics-server.sh"
comics_resolve_server || exit 1
comics_health_gate     || exit 1

comics_get "$COMICS_SERVER_URL/api/comics/calibration" || exit 1
```

**If either resolve/health-gate step or the `comics_get` call fails: STOP and
report the error** — a failed call must never render as "nothing to
re-price" (a hard-fail-loud rule shared with every other `/comic:*` server
call). A genuine "no calibration signal" result is the JSON array `[]` with
exit 0.

Optional `days` query param (default 180 — matches the recency window
`/api/comics/outcomes` uses for first-party comps):

```bash
comics_get "$COMICS_SERVER_URL/api/comics/calibration?days=90"
```

Optional `min_losses` query param (default 2 — a book must have lost at least
this many times in-window to surface; see "The one rule that must never be
'fixed'" above for why a single loss doesn't count):

```bash
comics_get "$COMICS_SERVER_URL/api/comics/calibration?min_losses=3"
```

## Response shape

One object per flagged `(issue, grade)`, already sorted by `overshoot`
descending (highest first — the top of the list is the most urgent
re-price):

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

- `overshoot` — `median(winning_bid / fmv_high)` over losses. **The ranking
  key.** Only rows with `overshoot > 1` appear at all.
- `above_fmv_loss_rate` — % of losses where `winning_bid > fmv_high`.
  Context only, reported alongside `overshoot` — never re-sort by it (see
  "The one rule that must never be 'fixed'" above).
- `contested_win_margin` — `median(winning_bid / fmv_high)` over **wins**, or
  `null` if there were no wins. Context only — never rank, suppress, or
  promote a row by it (see "The one rule that must never be 'fixed'" above).

## Present the results

Render a table, most urgent first:

```
| Issue                              | Grade | FMV High | Overshoot | Above-FMV Loss Rate | Losses | Wins (context) |
|---|---|---|---|---|---|---|
| The Amazing Spider-Man #129 (1973) | 8.0   | $100.00  | 1.20x     | 75%                  | 4      | 1 @ 0.40x       |
```

- **Overshoot** as a multiplier (`1.20x`), not a raw ratio — it reads more
  clearly as "losses are clearing 20% above FMV."
- **Wins (context)** shows the win count and `contested_win_margin` (if any)
  in parens, clearly labeled as context — never merge it into the ranking
  column or imply it affects the row's position.
- An empty response (`[]`) means no book currently needs re-pricing —
  report this plainly and stop; there is nothing else to do.

## After the report

For each flagged issue (start from the top of the list — highest overshoot
first), re-run `/comic:fmv` for that `(issue, grade)` so it recomputes with
fresh comps (which by now likely include the very auctions that flagged it,
via BUI-286's first-party-comp injection). This skill does not do that
automatically — recomputing FMV, and any resulting change to future bid
caps, is a deliberate, reviewed human action, not something this report
triggers on its own.

## Scheduling

Designed to run **unattended on a recurring schedule** (e.g. weekly via
`/schedule` or local cron) — it's a single cheap read (one aggregate query on
the comics server, no eBay calls, no LLM calls), so there is no caching
concern like `/comic:wishlist-sellers` has. A steady-state run that returns
`[]` should be silent; only notify when the list is non-empty.

## Common mistakes

| Mistake | Fix |
|---|---|
| Treating a high `loss_count` as the signal | It isn't — see "The one rule that must never be 'fixed'" above. |
| Sorting or filtering by `contested_win_margin` | It's context only — see "The one rule that must never be 'fixed'" above. |
| Rendering an empty table on a failed `comics_get` call | STOP and report the error instead — the hard-fail-loud rule this skill shares with every other `/comic:*` server call. |
| Assuming this report writes anything | It never does. `fmv_high` only changes when you explicitly re-run `/comic:fmv` afterward. |

---

Plan: `docs/plans/2026-07-04-001-feat-fmv-auction-outcome-feedback-plan.md` — BUI-288 (Issue C).
