---
date: 2026-06-03
topic: fmv-thin-comp-pool
---

# FMV: Honest Handling of Thin Grade-Specific Comp Pools (BUI-86)

## Summary

When `comic-fmv` can't find enough sold comps near a book's target grade, replace the current two-step fixed widen (±0.5 → ±1.0) with **progressive widening up to a ceiling**, gated by two honesty guards. A book whose comps are **one-sided** (all above or all below the target) or **smeared across too wide a grade spread** is flagged for **manual pricing** — no bid-able number — instead of emitting a confidently-wrong FMV. Pools that widen but stay bracketed and bounded still price, but a wide window caps their confidence. A `--grade-window` flag exposes the widen ceiling as an escape hatch.

## Problem Frame

`fmv_math.build_pool()` filters sold comps to ±0.5 grade points, falling back to ±1.0 only when the narrow pool has fewer than 5 comps. For books that sell primarily in grade tiers far from the target, this leaves an empty or near-empty pool, and the fallback is both too coarse (one fixed step) and silent (the window used doesn't affect the confidence label).

The 2026-06-03 session surfaced two *distinct* failure shapes that the ticket's framing ("widen the pool") conflates:

- **Bracketed-but-smeared** — Iron Man #124, target FN/VF 7.0, comps at \[4.0, 5.0, 9.0, 9.0, 9.4, 9.6\]. The ±1.0 window returned 0 comps; a manual ±2.0 patch yielded 3 comps at 5.0/9.0/9.0. The target *is* bracketed, but the pool spans 4 grade points and its median is meaningless because price is monotonic in grade.
- **One-sided** — Fantastic Four #63, target NM+ 9.6, all 19 comps top out at 9.0. Widening can only reach *downward* into cheaper sales, so it drags the NM+ estimate *below* the 9.0 comps — the opposite of the truth. No amount of symmetric widening fixes a top-of-range target.

The cost shape is asymmetric: the output feeds a real-money bid cap. A *wrong* FMV is worse than a *missing* one — it makes you overbid, or silently underbid and lose books you wanted. The current workaround (patch `WIDE_GRADE_WINDOW` 1.0 → 2.0, reinstall, run, restore, reinstall) is a two-reinstall cycle, and even when it produces a number, that number is the untrustworthy kind.

## Key Decisions

- **Honesty over coverage (governing principle).** Because the FMV drives a bid cap, the pipeline must distinguish "I can price this" from "I can't price this honestly." Emitting a number for every book is the most dangerous option; flagging the un-priceable ones is the safer default even though it sometimes asks for manual work.

- **Two guards define un-priceable.** A book is flagged for manual pricing — rather than priced — when, after widening to the ceiling, **either** (a) all comps fall strictly above or strictly below the target grade (one-sided / no bracket), **or** (b) the surviving pool's grade span exceeds a threshold (too smeared to mean anything), **or** (c) the pool is still too sparse to compute a range. One-sided alone misses Iron Man #124; spread alone misses nothing the others catch but is needed because a smeared bracket passes the one-sided test. All three are needed together.

- **Flagged books travel the existing stub rails.** A flagged book reuses the BUI-44 n=0 path: upsert the comics row + a stub fmv row carrying the flag and reason, return `comic_id`, write no bid-able `max_bid`. `/comic:verify` then reports it as linked-but-unpriced (`no_fmv_at_grade`), not the scarier `no_comic`. No new persistence concept is introduced.

- **No new mid-flow prompt; the existing gate handles judgment.** With no `max_bid`, snipe-add has nothing to auto-add, so it skips a flagged book naturally. The "needs-manual" books surface in the FMV table, and `/comic:buy`'s existing per-step user gate is where the human decides to hand-price or skip.

- **Wide windows can't claim high confidence.** Any FMV built at a window wider than ±1.0 is capped at MEDIUM confidence regardless of comp count, and the window + spread are surfaced in `fmv_notes`. A 6-comp pool smeared ±2.0 is not a HIGH-confidence price.

- **`--grade-window` is a reach knob, not a force-a-number override.** The flag sets the *maximum* auto-widen ceiling; progressive widening still steps up to it. It does **not** bypass the one-sided / spread guards. Forcing a price onto a guarded book is the manual-pricing path, not this flag.

## Requirements

**Pool building**

- R1. `build_pool()` widens progressively (±0.5 → ±1.0 → … → ceiling) rather than the current single ±0.5 → ±1.0 step, stopping as soon as the minimum pool size is reached.
- R2. The window actually used is returned and surfaced to the caller (it already rides `fmv` output and `fmv_notes`; progressive widening must keep that accurate).
- R3. Comps with no parsed grade continue to be dropped from the pool (unchanged from today).

**Pricing guards (flag-for-manual)**

- R4. After widening to the ceiling, a pool is classified **un-priceable** when any of: (a) all comps strictly above OR strictly below the target (one-sided), (b) pool grade-span exceeds the spread threshold, (c) pool size below the minimum needed to compute a range.
- R5. An un-priceable book emits no bid-able number (`fmv_low`/`fmv_high`/`max_bid` absent) and carries a machine-readable reason distinguishing one-sided / too-wide / too-sparse.
- R6. A priceable pool (bracketed, bounded spread, enough comps) produces an FMV as today.

**Output & confidence**

- R7. FMV built at a window wider than ±1.0 is capped at MEDIUM confidence regardless of comp count.
- R8. `fmv_notes` records the window used and the pool grade-span; for flagged books it records the flag reason.
- R9. The FMV table distinguishes priced books from `needs-manual` books at a glance.

**CLI flag**

- R10. `comic-fmv` accepts a `--grade-window <float>` option that sets the maximum auto-widen ceiling, threaded through to `build_pool()`.
- R11. `--grade-window` does not bypass the guards in R4 — a guarded book stays flagged even at the widened ceiling.

**Downstream behavior**

- R12. A flagged book reuses the existing stub-upsert path: comics row + stub fmv row written, `comic_id` returned, no `max_bid`.
- R13. snipe-add skips a flagged book (no `max_bid` to add); the decision to hand-price surfaces at the existing `/comic:buy` gate, not via a new prompt.

## Acceptance Examples

- AE1. One-sided (FF #63 shape).
  - **Covers R4, R5, R12, R13.**
  - **Given:** target grade 9.6; all comps at or below 9.0 even at the ceiling window.
  - **When:** FMV runs.
  - **Then:** book is flagged un-priceable with reason `one-sided`; no `max_bid`; comics row + stub fmv row written with `comic_id`; snipe-add skips it; it shows as `needs-manual` in the table.

- AE2. Bracketed but smeared (Iron Man #124 shape).
  - **Covers R4, R5.**
  - **Given:** target 7.0; widening to the ceiling yields comps at 5.0/9.0/9.0 (span 4 grade points, brackets target).
  - **When:** FMV runs.
  - **Then:** one-sided test passes (it brackets) but the spread guard fires; book is flagged `too-wide`, not priced.

- AE3. Defensible widen.
  - **Covers R1, R6, R7, R8.**
  - **Given:** target 7.0; thin at ±0.5 but ±1.5 yields enough comps spanning 6.0–8.0.
  - **When:** FMV runs.
  - **Then:** an FMV is emitted; confidence is capped at MEDIUM because window > ±1.0; `fmv_notes` records `window=±1.5` and the span.

- AE4. `--grade-window` doesn't force a guarded book.
  - **Covers R10, R11.**
  - **Given:** the FF #63 one-sided book and `--grade-window 2.5`.
  - **When:** FMV runs.
  - **Then:** widening reaches 2.5 but the pool is still one-sided, so the book stays flagged — the flag did not manufacture a price.

## Scope Boundaries

**Deferred for later**

- Grade-curve interpolation / extrapolation (the ticket's option 4) — a grade→price model that would *convert* one-sided and smeared cases into real auto-priced numbers (interpolate 7.0 from a 5.0/9.0 bracket; extrapolate 9.6 *above* the 9.0 comps). This is the genuine fix for the cases this work flags rather than prices, but it needs calibration and carries real complexity. The flag-for-manual behavior is the honest interim; interpolation is the upgrade that shrinks the manual set.

**Outside this work**

- Ungraded-comp fallback (the ticket's option 3) — folding grade-unknown comps into a thin pool. Rejected: the problem is specifically grade precision, and grade-unknown sales are the noisiest possible signal for it.
- Changes to `ebay-sold-comps` windowing — it has no grade-window logic; it returns comps with parsed grades and all windowing lives in `comic-fmv` / `fmv_math`. The ticket's "add the flag to both" only lands in `comic-fmv`.

## Outstanding Questions

**Deferred to planning** (pick sane defaults, tune against the BUI-51 fixture):

- Auto-widen ceiling default — proposed ±2.0 (matches the manual patch in use today).
- Progressive step size — proposed 0.5 (±0.5 → ±1.0 → ±1.5 → ±2.0).
- Spread threshold for the too-wide guard — proposed pool grade-span > 2.0 points flags.
- Minimum pool size to compute a range vs. flag too-sparse — current code emits a 1–2 comp "range"; decide whether that becomes a flag.

## Sources

- `apps/fmv/src/fmv_math.py` — `build_pool()` (lines 27–42), `compute_fmv()`, `confidence_label()`, the existing `DEFAULT_GRADE_WINDOW` / `WIDE_GRADE_WINDOW` / `MIN_NARROW_POOL` constants.
- `apps/fmv/src/fmv_runner.py` — `_compute_and_upsert_one()` and `_upsert_fmv()`: the BUI-44 stub-upsert path a flagged book reuses; `_build_notes()` where window/spread/flag surface.
- `apps/fmv/src/fmv_cli.py` — Click command where `--grade-window` is added.
- `apps/fmv/tests/test_fmv_math.py` — existing `TestBuildPool` / `TestComputeFmv` to extend with guard + progressive-widen cases.
- `.claude/commands/comic/fmv.md` — skill doc (window references at lines 181, 264, 298) that documents ±0.5/±1.0 behavior and needs the new states reflected.
- Linear BUI-86; prior FMV linkage solutions under `docs/solutions/` (`fmv-bid-linkage-gap-2026-05-23.md`, `database-issues/stub-fmv-null-after-extract-comics-2026-05-23.md`).
