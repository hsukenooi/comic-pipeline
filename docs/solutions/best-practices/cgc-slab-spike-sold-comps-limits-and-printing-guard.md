---
title: "CGC slab spike (BUI-929/930): sold-comps.com has no grade/date filter, vintage exact-grade pools are n≈1, second printings hide in descriptions"
date: 2026-09-21
category: best-practices
module: apps/ebay (sold_comps.py), apps/fmv (fmv_math.py graded ladder)
problem_type: best_practice
component: service_object
severity: medium
mechanized_by: test
enforced_by_test:
  - apps/ebay/tests/test_sold_comps.py::TestPrintingGuard
  - apps/ebay/tests/test_sold_comps.py::TestBuildQueryGraded
  - apps/fmv/tests/test_fmv_math.py::TestGradedLadderTier
  - apps/fmv/tests/test_golden_fmv_math.py::test_graded_fmv_matches_golden
related_components:
  - ebay-sold-comps
  - comic-fmv
  - grade_tokens
applies_when:
  - "Designing or reviewing the graded (slab) FMV pricing mode's pool-building or ladder rules"
  - "Assuming a sold-comps provider filter (grade, date range) exists before checking"
  - "Deciding whether a ladder rung with only one sale is trustworthy enough to anchor an interpolation"
  - "Writing or reviewing a comp-exclusion guard that keys on a title string alone"
symptoms:
  - "A vintage slab target's exact-grade bucket is empty or n=1 even though the book sold recently"
  - "A comp priced far below its rung's other sales turns out to be a later printing, not a bad grade"
  - "A '90-day window' assumption is baked into pricing logic with no corroborating provider parameter"
tags: [fmv, cgc-slab, graded-pricing, sold-comps, printing-guard, ladder, spike, BUI-929, BUI-930]
---

# CGC slab spike (BUI-929/930): sold-comps.com has no grade/date filter, vintage exact-grade pools are n≈1, second printings hide in descriptions

## Context

Before building the graded (slab) FMV pricing mode (BUI-929/930, plan `docs/plans/2026-09-21-001-feat-cgc-slab-support-plan.md`), an 8-listing spike measured what a real slab comp pool from sold-comps.com looks like (2026-09-19), and the shipped implementation's own spike corpus (2026-09-21) measured the printing guard against it. Four findings from that measurement shaped the design in ways worth keeping visible, because each one contradicts a reasonable-sounding default assumption.

## Guidance

**1. sold-comps.com takes no grade parameter and no date-range parameter.** There is no server-side way to ask for "CGC 9.6 sales in the last 90 days" — every filter (grade, printing, certifier) has to happen client-side, on titles and item specifics, after the fact. The 90-day sold window every book gets is eBay's own retention, not a parameter this pipeline chose or can widen. Don't design a graded-pool feature around a filter the provider doesn't have; `graded_target` (BUI-929's `build_query` parameter) only steers the *query terms*, not what the provider is willing to return.

**2. A vintage slab's exact-grade pool is routinely n=1, so the ladder has to accept single-sale rungs.** The 2026-09-19 8-listing spike measured 7–10 rungs per vintage book; the 2026-09-21 BUI-929 spike corpus measured 8–12 — both with roughly one sale per rung in the 90-day window, not the 2+ minimum `MIN_BRACKET_COMPS` requires on the raw path. `GRADED_LADDER_MIN_BUCKET_N = 1` (`apps/fmv/src/fmv_math.py`) is a deliberate, measured departure from the raw path's stricter anchor requirement, not an oversight: refusing every single-sale rung would leave almost nothing to interpolate a vintage ladder from. The `ladder` tier's LOW confidence and 0.60 bid factor (§7b) are what keep this safe — the single-sale rungs are trusted to *anchor a line*, never to *be* the price.

**3. Second (and later) printings hide in listing descriptions, not titles — a title-only guard misses them.** `TestBuildQueryGraded`'s corpus and the BUI-929 printing guard exist because a comp's title routinely reads identically to a first print while its description names "2nd Printing." The guard (`apps/ebay/src/sold_comps.py::_printing_guard`) fires only on a **price outlier** — a comp priced below half its rung's leave-one-out median — and then fetches one Browse API description to check for an ordinal printing token (`2nd`, `second printing`, `3rd`, …) or `facsimile`; it never scans every comp's description (that would burn a Browse API call per comp for no measured benefit — the guard is precision-first). On the spike corpus this dropped exactly two $215 "Ultimate Fallout #4" comps that were second printings masquerading as first-print sales in the title. **Bare `reprint` is deliberately excluded from the drop tokens** (BUI-645: some genuine first prints carry "reprint" in the title for unrelated reasons — a title-keyword scan on that word alone produces false positives, which is why the guard reads the *description*, gated on a *price* signal, rather than pattern-matching titles).

**4. The positive certifier query term measurably shapes the graded pool without narrowing it away.** `build_query(..., graded_target="cgc")` (BUI-929) drops the raw path's `-cgc -cbcs -graded -slab` exclusions and appends the certifier name instead, so a graded query says "yes, CGC" rather than "not graded." Measured on the spike corpus: 11–20 slab comps per book — comparable in depth to (and for common books, deeper than) a raw pool, so asking positively for the certifier does not starve the pool the way a naive keyword-narrowing might.

**Corollary — `identify` can still drop the grade on a slab title.** The spike found `/comic:identify` missed the grade on 2 of 8 slab listings and that Signature Series comps are hard-excluded from the pool entirely (by design — SS trades in its own market, see `docs/conventions/fmv-math-spec.md` §7b and `CONCEPTS.md` → Label). Neither is a printing-guard finding, but both mean a certified working-list row should be spot-checked, not assumed complete, before it reaches `comic-fmv`.

## Why This Matters

- **A provider-capability assumption baked into pricing logic is a silent wrong answer, not a crash.** If the graded mode had assumed a grade/date filter existed and filtered client-side data as if the server had already scoped it, a mis-scoped query would return *something* — just the wrong pool — with no error to catch it.
- **The single-sale-rung decision is the one most likely to look like a bug to a future reader.** Without this doc, `GRADED_LADDER_MIN_BUCKET_N = 1` reads as a guard someone forgot to raise to match the raw path's 2. It is the opposite: raising it would make most vintage ladders unpriceable.
- **A precision-first guard (price-outlier-gated, not every-comp) is a deliberate trade, not incomplete coverage.** A reviewer expecting every comp's description to be checked would flag the guard as under-built; the outlier gate is what keeps it from spending a Browse API call per comp for a check that, on this corpus, only ever fired on outliers anyway.

## When to Apply

- Extending the graded pool to a new provider — check what it actually filters on before assuming parity with sold-comps.com.
- Proposing to raise `GRADED_LADDER_MIN_BUCKET_N` or otherwise tighten the ladder's anchor requirement for slabs — re-run the measurement above before assuming the raw path's threshold transfers.
- Adding a new comp-exclusion guard anywhere in this pipeline — prefer a guard gated on a measurable outlier signal plus a corroborating fetch over a bare title-keyword match (see BUI-645's `reprint` false-positive precedent).

## Related

- `docs/conventions/fmv-math-spec.md` §7b — the graded pricing mode's full math (exact/ladder tiers, ledger weighting, page-quality preference, the printing guard, the BIN rule).
- `CONCEPTS.md` → Graded Pricing Mode, Certified Grade, Label, Page Quality, Pricing Basis, Comps Ledger.
- `docs/solutions/best-practices/fmv-outlier-robust-bucket-n-guard.md` — the sibling n≥3 money-path principle this doc's single-sale-rung exception deliberately departs from, and why that departure is safe (LOW confidence + 0.60 factor, never unclamped).
- Linear: BUI-923 (identify), BUI-929 (sold-comps graded mode + printing guard), BUI-930 (comic-fmv graded pricing mode), BUI-931 (this documentation pass).
