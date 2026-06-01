---
date: 2026-05-19
topic: per-39-default-dashboard-comics-free
---

# PER-39: Redesign Default Dashboard to be Comics-Free

## Problem Frame

The default Gixen dashboard at `/` (`server/static/index.html`) still renders comic-specific UI: a `cond` (CGC grade) column, an `fmv` (fair-market-value range) column, helper functions that interpret those fields, and a `?demo=1` fixture populated entirely with comics (Spider-Man #300, Hulk #181, Action Comics, etc.) and comic-themed seller handles.

This is the last piece of comic surface in the default dashboard. PER-25–PER-30 already extracted comic Python, routes, DB, and the `/v2/comics` plugin tab into the `gixen-overlay` plugin. After PER-30 lands, `/api/snipes` no longer returns `comic_grade`, `fmv_low`, or `fmv_high` — so the dashboard's comic columns will silently render empty for every user, including the OSS audience this work is meant to land for.

The default dashboard must read as a generic eBay snipe manager. Comic UI is owned by the `gixen-overlay` plugin's `/v2/comics` tab.

## Requirements

**Default Dashboard (`server/static/index.html`)**
- R1. The active snipes table renders these columns only: `item`, `title`, `current`, `max`, `t-minus`, `seller`. No `cond`, no `fmv`.
- R2. The recently-ended table renders these columns only: `item`, `title`, `winning`, `max`, `result`, `seller`. No `cond`, no `fmv`.
- R3. The page contains no helper function whose purpose is to interpret comic fields. Specifically: `gradeLabel`, `displayCondition`, and `fmtFmv` are removed.
- R4. The `displayTitle` helper falls back to `"—"` when `r.title` is missing. It does not consult `r.comic_title` or `r.comic_issue`.

**Demo Fixture (`?demo=1`)**
- R5. The fixture contains a mix of generic eBay categories — vintage watch, mechanical keyboard, film camera, sneakers, vinyl LP, power tool, trading cards, audio gear, etc. — chosen to make it instantly obvious the tool is not category-specific.
- R6. The fixture exercises the same state spread the current one does: at least one urgent (<5 min), one urgent (<1 hr), one overbid (current ≥ max), several healthy active, one ended-won, one ended-outbid, one ended-network-error, one ended-OUTBID, and one ended-WON-cleanly. Total row count remains roughly 15.
- R7. No row contains `fmv_low`, `fmv_high`, `fmv_confidence`, `comic_grade`, `comic_title`, or `comic_issue` fields. Seller handles are generic (e.g., `vintage_dial_co`, `kbd_lab`, no `comicvault77`, no `silverage_books`).

**Verification**
- R8. `grep -in 'comic\|fmv\|cgc\|grade' server/static/index.html` returns no matches after the change.
- R9. Loading `/` against a real Gixen account (with `gixen-overlay` not installed) renders without console errors and shows the six-column active table.
- R10. Loading `/?demo=1` renders the new fixture without console errors and the dashboard layout reads as generic.

## Success Criteria

- An OSS user installing `gixen-cli` fresh sees a generic eBay snipe dashboard at `/` with no hint of the comics use case.
- A reviewer skimming `server/static/index.html` finds no comic vocabulary in markup, JS, or fixture data.
- The user's own production dashboard (with `gixen-overlay` installed) still shows comic data in `/v2/comics`; the default view stays generic.

## Scope Boundaries

- **Out of scope:** the bids tab (`server/static/v2-bids.html`) — already comics-free, no work needed.
- **Out of scope:** the comics plugin tab (`/v2/comics`, lives in `gixen-overlay`) — owned by the plugin repo.
- **Out of scope:** Python decontamination (`server/`, `gixen_client.py`) — owned by PER-30.
- **Out of scope:** adding any replacement columns. The dashboard goes from 8 to 6 active columns; that is the intended end state.
- **Out of scope:** restyling, reflow, or any visual change beyond column removal. CSS specific to removed columns may be deleted if it is unambiguously dead, but no new styling.
- **Out of scope:** the `bids.comic_id` opaque column and any DB-shape concerns.
- **Preserved as-is** (named explicitly so the implementer doesn't drift): the overbid row highlight (`tr.overbid` + `.warn-cell`), the urgent t-minus highlight (`.urgent-cell`), the empty-state strings (`// no active snipes`, `// none`), the `[DEMO]` tag in the session line, the existing `t-minus` countdown format (`<1m` / `${m}m` / `${h}h` / `${d}d ${h}h`), inline-edit and remove behavior on the `max` cell, and the refresh sweep bar. The "no visual changes beyond column removal" clause keeps all of these intact; no new banner, badge, or zero-state UI is added.

## Key Decisions

- **No replacement columns.** The ticket's intent is removal, not substitution. Adding a generic placeholder column (`bid_offset`, `snipe_group`, etc.) would be scope creep and adds carrying cost.
- **Demo fixture: mixed categories.** Confirmed with user. Reads as obviously not-comics to OSS reviewers and demonstrates the tool's category-agnostic nature.
- **`displayTitle` loses its comic fallback.** Once `/api/snipes` stops returning `comic_title`/`comic_issue` (post-PER-30), the fallback is dead code. Leaving it would also be a comic vocabulary leak. Replace with `r.title || "—"`.
- **No bid-enrichment hook.** PER-30 deferred plugin re-injection of comic columns into `/api/snipes` to PER-38. PER-39 does not depend on or anticipate that hook; if it lands later, the comics tab is where comic data should render anyway.

## Dependencies / Assumptions

- Assumes PER-30 (extract comic overlay plugin) is in flight or merged. If PER-30 lands first, the comic columns are already silently empty and PER-39 just cleans up the dead rendering. If PER-39 lands first, the columns simply stop rendering for users with the plugin installed — they regain that view via `/v2/comics`. Both orderings are safe.
- Assumes the user's own production dashboard tolerates losing the inline comic columns; comic data is still available one click away in `/v2/comics`.

## Outstanding Questions

### Resolve Before Planning
- *(none)*

### Deferred to Planning
- [Affects R6][Technical] Exact seller/title strings for the 15 fixture rows — pick during implementation, no product impact beyond "mixed and generic."
- [Affects R3][Technical] Whether any CSS rules in the `<style>` block become unambiguously dead after column removal (e.g., none currently reference `.cond` or `.fmv`, so likely no CSS deletions needed — verify during implementation).

## Next Steps

-> `/ce:plan` for structured implementation planning
