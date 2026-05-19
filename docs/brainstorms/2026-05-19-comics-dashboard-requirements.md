---
date: 2026-05-19
topic: comics-dashboard
---

# Comics Dashboard with Condition and FMV Data (PER-40)

## Problem Frame

The `/comics` tab at `plugins/gixen-overlay/src/gixen_overlay/static/v2-comics.html` is a thin read-only view of the `comics` table — sorted alphabetically, no snipe context, no auction lifecycle. The default Gixen dashboard at `/` is now generic (PER-39 landed in gixen-cli commit `4d490dd`): no comic-specific JS, no comic-themed demo fixtures, no FMV/condition columns. There is currently no place that combines auction lifecycle (active/ended, time-to-end, current vs max bid) with comic context (condition, FMV range).

Net effect: the user, who only snipes comics, has to context-switch between `/` (live auctions, no comic data) and `/comics` (just the comics table, no auction lifecycle) to make bidding decisions.

The primary need is **active monitoring** during live auctions: deciding whether to raise or lower a max bid based on what a comic is actually worth. Post-mortem on ended auctions is a secondary use — understanding how close they came on losses, how much room they left on wins.

## Requirements

**Filter scope and row model**
- R1. Show all snipes (do not filter to comic-linked only). Snipes without a linked fmv are edge cases (lots that didn't parse, occasional non-comic auctions) that should remain visible so they can be diagnosed or hand-linked.
- R2. Use one row per bid (`bids.item_id`). Lots that link to multiple fmvs via `bid_fmvs` collapse into a single row.
- R3. Title column displays `bids.ebay_title` verbatim — the raw eBay listing title. A previously-tried parsed-comic-title approach lost too much information from the listing.

**Data path**
- R16. `/comics` is fed by new plugin-owned endpoints — `/api/comics/snipes` (active) and `/api/comics/history` (ended) — that join gixen-cli's `bids` table with the plugin's `fmv` and `comics` tables server-side, using the shared SQLite connection. gixen-cli's existing `/api/snipes` and `/api/history` are untouched and stay comic-free per PER-39. Mutations (`PATCH /api/bids/{item_id}`, `DELETE /api/bids/{item_id}`) continue to hit gixen-cli core.

**Active section (primary use)**
- R4. Active section follows the same shape as the snipes dashboard at `/`: sorted by time-to-end ascending, urgent (<1hr) styling, overbid (current ≥ max) row highlight + attention-required banner. Implementation is independent — patterns from `/` are referenced, not shared at runtime.
- R5. Inline-edit `max_bid` follows the same UX pattern as `/` — click to edit, Enter/blur to commit, Esc to cancel, optimistic UI with save/error states, AbortController timeout, suppress-refresh during in-flight edits.
- R6. Per-row remove follows the same UX pattern as `/` — two-click confirm, fade animation, suppress-refresh guard.
- R7. Active columns add three comic-enrichment columns:
  - `cond` — grade label (e.g. "9.4 NM+")
  - `fmv` — range (e.g. "$1,700–$2,100")
  - `value` — current bid as % of FMV midpoint (e.g. "62% of FMV"), color-coded for v1 as: **green < 80%**, **neutral 80–100%**, **red > 100%**
- R8. For snipes with no linked fmv (R1 edge cases), `cond`/`fmv`/`value` cells render as `—`. The row gets a dim visual treatment and a small "needs linking" icon adjacent to the title, indicating the row needs hand-linking — not that the data is missing-by-design. Unlinked rows stay inline with the rest (no separate sub-section).

**Ended section (secondary use)**
- R9. Ended section follows the same shape as `/`: sorted by end time desc, outcome pill (won/outbid/missed), `winning_bid` + `max_bid` columns.
- R10. Ended columns add `cond`, `fmv` range, and a `spread` column. Spread is always computed against the user's max:
  - Loss: `winning_bid − max_bid` → "+$135 over" (red)
  - Win: `max_bid − winning_bid` → "−$63 under" (dim, indicates headroom left)
  - Missed (no `winning_bid` recorded): `—`

**Lot rendering**

A "lot" in this section means an eBay auction (one `bids.item_id`) that has more than one comic linked via the `bid_fmvs` junction table — i.e., one bid with N ≥ 2 fmv rows. This is distinct from the eBay UI concept of a multi-item listing, though it usually coincides.

- R11. Lots show the raw `ebay_title` (carries lot context) plus a small `lot of N` badge inline after the title where N > 1.
- R12. Cond column shows the **primary fmv's grade** plus a `(+N more)` hint inline (e.g. "9.4 NM+ (+2 more)") where N > 1. The hint signals the row is heterogeneous without claiming a single composite grade.
- R13. FMV column shows the **aggregated** low–high range summed across all `bid_fmvs` rows for that bid — the auction's true comp value across all linked comics.
- R17. `value` % column is blank (`—`) for lots. A summed midpoint across mixed-grade comics is not a meaningful single reference for the green/neutral/red signal. The raw `current_bid` vs aggregated `fmv` columns still let the user eyeball deal potential on lots.

**Empty + state-interaction edges**
- R18. Empty active section renders the section header + `// no active snipes` message (matches the existing `/comics` empty-state pattern at `plugins/gixen-overlay/src/gixen_overlay/static/v2-comics.html:99-103`).
- R19. Empty ended section renders the section header + `// none` message.
- R20. When both signals fire on the same row, the **overbid row highlight (R4) takes visual precedence** over the `value`-column red coloring (R7). The overbid signal implies action (raise max or cancel), which dominates the price-vs-FMV observation.

**Navigation and shared UX**
- R14. The top-bar nav and keybar follow `/`'s shape — same `r` refresh key, same status pill, same clock and host segments. The nav fetches `/api/dashboard-tabs` (the dynamic plugin-tab injection mechanism introduced in gixen-cli commit `b2c77f7`) rather than hard-coding tab links. This keeps the comics page's nav in sync if other plugins later register tabs.
- R15. Auto-refresh cadence and in-page repaint behavior follow `/` — 30s API refresh + 1s repaint for time-to-end countdowns.
- R21. Columns are not user-sortable in v1. Active sort is fixed at time-to-end ascending; ended at end-time descending.

## Success Criteria

- During an active auction the user can look at `/comics`, see grade and FMV range for every comic snipe, see a value-vs-FMV signal for whether to raise/lower their max, and edit the max bid inline without leaving the page.
- For ended auctions the user can see at a glance, per row, how far over their max the winning bid was (or how much headroom they had left).
- Lots show as one row with a clear lot indicator, aggregated FMV totals, and the original eBay listing title intact.
- Non-comic-linked snipes still appear, visibly flagged as needing linking, rather than silently disappearing.

## Scope Boundaries

- Not changing the existing route mounting. The plugin's route is `/comics` (gixen-cli commit `5e5b171` renamed from `/v2/comics`). The `v2-comics.html` template still has stale `/v2/comics` references at lines 17 and 163 — fixing those is in-scope cleanup for this rewrite.
- gixen-cli core endpoints (`/api/snipes`, `/api/history`) are not modified. The OSS boundary stays intact post-PER-39. Mutations continue to reuse the existing `PATCH/DELETE /api/bids/{item_id}`.
- Not implementing extract-comics improvements, FMV-pipeline changes, or LOCG linking UI. The dashboard surfaces what the data layer provides; improving the data layer is separate work.
- Not building expandable per-row lot drilldowns. One row per bid; lot detail can be a future addition.
- **Not sharing JS/CSS modules** between `/` and `/comics`. The two dashboards drift independently — each owns its rendering. Patterns from `/`'s snipes-table logic are the reference; copy what's useful, don't import at runtime or copy at build time.
- Not surfacing FMV `confidence` as a column in v1. The threshold-tuning step (deferred research) is where confidence might become relevant.

## Key Decisions

- **All snipes, not comic-linked only**: This is a comic-sniping tool; un-linked rows are anomalies worth surfacing, not noise to hide.
- **`ebay_title` as the title source**: Preserves listing-specific information (variants, signed copies, lot composition) that parsed comic titles strip out.
- **Primary use is active bidding, not post-mortem**: Drives column emphasis (`value` % on active, `spread` on ended) and confirms full active UX is needed (inline-edit, remove, flags).
- **Spread is always referenced to user's max** (loss = winner − max; win = max − winner): symmetric formula, sniping-native framing rather than a generic FMV margin.
- **Lots: aggregated FMV + primary-grade cond with `(+N more)` hint + no value %**: aggregated FMV preserves comp-value signal; cond stays honest about heterogeneity; value % opts out because a summed midpoint across mixed grades isn't a meaningful reference.
- **New plugin endpoint for the join, not extending core**: keeps gixen-cli's OSS surface comic-free per PER-39, and avoids adding plugin-hook infrastructure (e.g., a generic response-augmentation hook) for one consumer.
- **Two dashboards drift independently — no shared JS/CSS module**: the parity-success criterion was dropped. Bug fixes in one place do not have to propagate. Trade: a bit more code; a lot less cross-repo coupling.
- **Value % thresholds locked at 80% / 100% for v1** (green / neutral / red): simple, explainable, anchored on the midpoint. Tunable post-launch if comp data warrants.
- **Overbid row highlight takes visual precedence** when it conflicts with `value`-column red: the overbid signal implies an action (raise or cancel max).

## Dependencies / Assumptions

- **PER-39 has already landed** (gixen-cli commit `4d490dd`): `/` is now generic, with no comic-aware JS or demo fixtures.
- Assumes `bids.ebay_title` is populated for the snipes the user cares about (it is, via the existing eBay-title caching path).
- Assumes the existing `PATCH /api/bids/{item_id}` and `DELETE /api/bids/{item_id}` endpoints in gixen-cli remain stable and work identically when called from the comics dashboard.
- Assumes the plugin can reach gixen-cli's `bids` table via the shared SQLite connection (it already does for the `bid_fmvs` junction; same connection serves the join).

## Outstanding Questions

### Resolve Before Planning

_(none — all product decisions captured above)_

### Deferred to Planning

- [Affects R16][Technical] Exact JSON shape of `/api/comics/snipes` and `/api/comics/history` — what fields, what sort order, whether they unify or stay separate. Likely a thin wrapper around `/api/snipes`'s shape plus `cond`, `fmv_low`, `fmv_high`, `value_pct`, `lot_count`. Planning step.
- [Affects R10][Needs research] Does gixen-cli's history endpoint capture `max_bid` at auction close, or the live max at the moment of the read? If live, a mid-auction max edit (R5) followed by a loss can make the ended `spread` look misleading. Worth verifying against the data model during planning.
- [Affects R7][Deferred research] Post-launch: validate the green/neutral/red thresholds (80% / 100%) against real comp data. May warrant confidence-weighted bands once enough comps are accumulated. Not blocking v1.

## Next Steps

`-> /ce:plan` for structured implementation planning
