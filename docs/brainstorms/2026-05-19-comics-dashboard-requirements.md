---
date: 2026-05-19
topic: comics-dashboard
---

# Comics Dashboard with Condition and FMV Data (PER-40)

## Problem Frame

The `/comics` tab at `plugins/gixen-overlay/src/gixen_overlay/static/v2-comics.html` is a thin read-only view of the `comics` table — sorted alphabetically, no snipe context, no auction lifecycle. The default Gixen dashboard at `/` is now generic (PER-39 landed in gixen-cli commit `4d490dd`): no comic-specific JS, no comic-themed demo fixtures, no FMV/condition columns. There is currently no place that combines auction lifecycle (active/ended, time-to-end, current vs max bid) with comic context (condition, FMV range).

Net effect: the user, who only snipes comics, has to context-switch between `/` (live auctions, no comic data) and `/v2/comics` (just the comics table, no auction lifecycle) to make bidding decisions.

The primary need is **active monitoring** during live auctions: deciding whether to raise or lower a max bid based on what a comic is actually worth. Post-mortem on ended auctions is a secondary use — understanding how close they came on losses, how much room they left on wins.

## Requirements

**Filter scope and row model**
- R1. Show all snipes (do not filter to comic-linked only). Snipes without a linked fmv are edge cases (lots that didn't parse, occasional non-comic auctions) that should remain visible so they can be diagnosed or hand-linked.
- R2. Use one row per bid (`bids.item_id`). Lots that link to multiple fmvs via `bid_fmvs` collapse into a single row.
- R3. Title column displays `bids.ebay_title` verbatim — the raw eBay listing title. A previously-tried parsed-comic-title approach lost too much information from the listing.

**Active section (primary use)**
- R4. Active section mirrors the snipes dashboard at `/` structurally: sortable by time-to-end, urgent (<1hr) styling, overbid (current ≥ max) row highlight + attention-required banner.
- R5. Inline-edit `max_bid` works the same as on `/` — click to edit, Enter/blur to commit, Esc to cancel, optimistic UI with save/error states, AbortController timeout, suppress-refresh during in-flight edits.
- R6. Per-row remove works the same as on `/` — two-click confirm, fade animation, suppress-refresh guard.
- R7. Active columns add three comic-enrichment columns: `cond` (grade label, e.g. "9.4 NM+"), `fmv` (range, e.g. "$1,700–$2,100"), and `value` (current bid as % of FMV midpoint, e.g. "62% of FMV"). The `value` column is colored to surface deals: green when current is well under FMV midpoint, neutral near midpoint, red when above.
- R8. For snipes with no linked fmv (R1 edge cases), cond/fmv/value cells render as `—` with a subtle visual cue (dim color or icon) that indicates the row needs linking, not that the data is missing-by-design.

**Ended section (secondary use)**
- R9. Ended section mirrors `/` structurally: sorted by end time desc, outcome pill (won/outbid/missed), winning bid + max bid columns.
- R10. Ended columns add: `cond`, `fmv` range, and a `spread` column showing the winner's bid spread vs the user's max — on losses, "+$135 over" (red); on wins, "−$63 under" (dim, to surface headroom left). Sniping-specific post-mortem framing.

**Lot rendering**

A "lot" in this section means an eBay auction (one `bids.item_id`) that has more than one comic linked via the `bid_fmvs` junction table — i.e., one bid with N ≥ 2 fmv rows. This is distinct from the eBay UI concept of a multi-item listing, though it usually coincides.

- R11. Lots show the raw `ebay_title` (carries lot context) plus a small `lot of N` badge next to it where N > 1.
- R12. Cond column displays the primary fmv's grade.
- R13. FMV column displays the aggregated low–high range summed across all `bid_fmvs` rows for that bid. This reflects the auction's true comp value rather than only the primary comic.

**Navigation and shared UX**
- R14. The top-bar nav and keybar match `/` — same `r` refresh key, same status pill, same clock and host segments. The `▸ comics` segment is active on this page.
- R15. Auto-refresh cadence and in-page repaint behavior match `/` (30s API refresh + 1s repaint for countdowns).

## Success Criteria

- During an active auction the user can look at `/comics`, see grade and FMV range for every comic snipe, see a value-vs-FMV signal for whether to raise/lower their max, and edit the max bid inline without leaving the page.
- For ended auctions the user can see at a glance, per row, how far over their max the winning bid was (or how much headroom they had left).
- Lots show as one row with a clear lot indicator, lot-aware FMV totals, and the original eBay listing title intact.
- Non-comic-linked snipes still appear (visibly flagged as needing linking) rather than silently disappearing.
- The two dashboards (`/` after PER-39, `/comics`) share enough structure that fixing a bug in the snipe table on one place doesn't require finding and fixing it in two.

## Scope Boundaries

- Not changing the existing route mounting. The plugin's route is `/comics` (gixen-cli commit `5e5b171` renamed from `/v2/comics`). The existing `v2-comics.html` template still has stale `/v2/comics` references at lines 17 and 163 — fixing those is in-scope cleanup for this dashboard rewrite.
- Not redesigning the underlying snipes data flow on the gixen-cli side. The dashboard reads snipes/history through some endpoint (new or extended — to be decided in planning) and mutates via existing `PATCH /api/bids/{item_id}` and `DELETE /api/bids/{item_id}` on gixen-cli.
- Not implementing extract-comics improvements, FMV-pipeline changes, or LOCG linking UI. The dashboard surfaces what the data layer provides; improving the data layer is separate work.
- Not building expandable per-row lot drilldowns. One row per bid; lot detail can be a future addition if collapsed-row signal turns out to be insufficient.
- Not a public/OSS-ready dashboard — `/comics` is plugin-owned and comic-specific by design, unlike `/` which PER-39 is making generic.

## Key Decisions

- **All snipes, not comic-linked only**: This is a comic-sniping tool; un-linked rows are anomalies worth surfacing, not noise to hide.
- **`ebay_title` as the title source**: Preserves listing-specific information (variants, signed copies, lot composition) that parsed comic titles strip out.
- **Primary use is active bidding, not post-mortem**: Drives column emphasis (value % on active, spread on ended) and confirms full active UX is needed (inline-edit, remove, flags).
- **Sniping-specific spread framing on ended**: "+$135 over" / "−$63 under" is more directly useful than a generic FMV-margin %.
- **Lots collapse to one row with aggregated FMV**: Single-row visual simplicity wins; the eBay title already carries the lot signal; aggregating FMV across linked comics reflects true comp value.

## Dependencies / Assumptions

- **PER-39 has already landed** (gixen-cli commit `4d490dd`): `/` is now generic, with no comic-aware JS or demo fixtures. Comic-specific UI is owned entirely by `/comics`; there is no double-source-of-truth risk to manage.
- Assumes `bids.ebay_title` is populated for the snipes the user cares about (it is, via the existing eBay-title caching path).
- Assumes the existing `PATCH /api/bids/{item_id}` and `DELETE /api/bids/{item_id}` endpoints in gixen-cli remain stable and work identically when called from the comics dashboard.

## Outstanding Questions

### Resolve Before Planning

_(none — all product decisions captured above)_

### Deferred to Planning

- [Affects R1, R7][Technical] What endpoint(s) feed the comics dashboard? Options: (a) extend `/api/snipes` and `/api/history` to include cond/fmv when the plugin is loaded; (b) add a new plugin endpoint like `/api/comics/snipes` that does the join server-side; (c) two-call client-side join. Affects coupling between gixen-cli core and the plugin.
- [Affects R4–R6][Technical] How to share JS/CSS between `/` and `/comics` without drift. Options: extract a shared module loaded by both pages, build-step inclusion, or accept some duplication with a single source of truth elsewhere. Decide during planning.
- [Affects R7][Needs research] Confirm "% of FMV midpoint" color thresholds against real comp data — what % range counts as a deal vs a fair price vs overbid? May want to use confidence-weighted thresholds.
- [Affects R8][Technical] Visual treatment of "needs linking" rows — dim row, icon, badge, or sort to a separate "unlinked" sub-section. Light prototype during planning to pick.

## Next Steps

`-> /ce:plan` for structured implementation planning
