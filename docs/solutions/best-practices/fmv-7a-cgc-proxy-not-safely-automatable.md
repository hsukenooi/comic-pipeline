---
title: "Why fmv.md §7a (CGC-proxy fallback) is not safely automatable — keep it human/LLM-gated"
date: 2026-07-11
last_updated: 2026-07-14
category: docs/solutions/best-practices
module: comic-fmv (apps/fmv)
problem_type: best_practice
component: service_object
severity: high
related_components:
  - comic-fmv
  - ebay-sold-comps
applies_when:
  - "Someone proposes automating fmv.md §7a (the CGC census/price proxy fallback) so a thin-comp book yields an FMV instead of needs_manual"
  - "Reasoning about which fmv.md steps can move from the /comic:fmv skill into deterministic code in a bid-cap path"
  - "Wiring a Google/Heritage/GoCollect snippet source into any code that feeds a bid cap"
tags: [fmv, cgc-proxy, 7a, bid-cap, money-path, over-bid, needs-manual, human-gated, serpapi]
---

# Why fmv.md §7a (CGC-proxy fallback) is not safely automatable — keep it human/LLM-gated

## Context

`comic-fmv` prices a raw book from eBay sold comps (fmv.md §7 grade-curve interpolation, hardened in BUI-318 — see [fmv-grade-curve-interpolation-overbid-guards.md](./fmv-grade-curve-interpolation-overbid-guards.md)). When raw comps are too thin to price a grade, the pipeline returns `needs_manual`. fmv.md §7a describes a **CGC census/price proxy** fallback — query Google/Heritage/GoCollect, read realized graded prices, and derive a defensible FMV instead of punting.

BUI-326 asked to *automate* §7a. It was investigated during the BUI-320..327 batch and **closed Won't Do**. This doc records why, so a future engineer/agent picking up "automate §7a" does not re-attempt it and ship a snippet-price extractor into the bid path. Automating §7 (interpolation) was correct and shipped; automating §7a is a different, unsafe proposition. The distinction is the whole point.

## Guidance

**Keep §7a human/LLM-gated inside the `/comic:fmv` skill. Do not port it into deterministic code that feeds a bid cap.** A thin-comp key should land in `needs_manual` and be hand-priced with judgment — that is a safety feature, not a coverage gap.

Four load-bearing reasons, each independently sufficient:

1. **The premise "reuse existing SerpApi plumbing in apps/fmv" is false.** `apps/fmv/src/fmv_runner.py` makes **zero** SerpApi calls — it `subprocess`es to the `ebay-sold-comps` console script and otherwise only `requests`-talks to the comics server. SerpApi / `load_serpapi_key` / fetch / caching all live in `apps/ebay/src/sold_comps.py`, reached only through that subprocess. (fmv_runner *mentions* SerpApi in timeout comments, which misleads — it doesn't call it.) Any §7a automation must add a new `engine=google`/graded mode **in apps/ebay**, not "reuse apps/fmv plumbing." The work cannot be done in the file the ask named.

2. **§7a's core step is an LLM comprehension task, not deterministic parsing.** §7 interpolation works on `engine=ebay` results, which have a structured `price.extracted` field and parseable `"CGC 9.8"` titles. §7a Step 1 ("extract realized prices from Google/Heritage snippets") is unstructured prose with **no** sold-price field. Porting it to regex invents the fragile extraction heuristic the spec hand-waves to a human. In a **live bid-cap path**, one mis-grabbed number — a shipping cost, an asking (not sold) price, or a 9.8 price attributed to a 4.0 target — is an **unbounded over-bid**, the exact failure class BUI-318's guards exist to prevent.

3. **The ">$200 estimated book value" trigger gate is circular.** §7a is supposed to fire only for valuable books, gated on an estimated value. But §7a fires *because raw comps are too thin to price* — so by definition there is no value estimate to gate on at that moment. No non-circular value source exists in the code at that point.

4. **Automating §7a removes the human check exactly where a mistake is most expensive.** Today a >$200 key with thin comps punts to `needs_manual` → a human/LLM runs §7a *with judgment* (which is what the `/comic:fmv` skill already operationalizes). Replacing that with a deterministic auto-bid raises risk precisely on the highest-value, hardest-to-price keys. §7a being human-gated is by design.

## Why This Matters

The FMV pipeline feeds a **bid cap** — a wrong high number spends real money on Gixen with no human in the loop. The BUI-318 lineage (thin-bracket suppression, interpolated-LOW haircut) exists because *automating a documented pricing heuristic into the bid path is where over-bids get born*. §7 interpolation was safe to automate because its inputs are structured and bounded. §7a is not: its inputs are unstructured prose, its trigger is circular, and its whole purpose is to price the books where being wrong costs the most. "It's in fmv.md, so it should be automated like §7" is the trap — being in the spec does not make a step deterministic-code-safe. The spec is a **human runbook** at that step, not a code spec.

## When to Apply

- Any ticket or proposal to "automate §7a", "make thin-comp books return an FMV instead of needs_manual via a CGC proxy", or "wire a Google/Heritage/GoCollect price source into comic-fmv."
- More generally: before moving *any* fmv.md step from the skill into deterministic code, ask whether its inputs are structured (like `engine=ebay`) or prose (like `engine=google`), and whether its trigger gate is computable at the point it fires.

## The one defensible automation path — TAKEN in BUI-348 (2026-07-14)

Not the google-snippet extractor. The only deterministic, testable path is an **eBay graded-slab proxy**: drop the `-cgc` exclusion, fetch CGC slabs at the target grade via `engine=ebay` (structured, parseable), apply a conservative raw-discount, and cap the bid. This still bakes a raw/graded divergence discount into a bid cap and still needs a non-circular value gate — so it is a real product/risk decision that had to be scoped as **its own risk-gated ticket** with the discount, robustness thresholds (min graded-comp count / CV), and quantile anchor all decided up front. It is a re-spec (eBay slabs, not Heritage), not "implementing §7a."

**This path was revisited and shipped as BUI-348** — a distinct CGC-proxy *tier*, not an automation of §7a's prose extractor. It does exactly what this section prescribed: it fires only as a **rescue** on a sparse-pool book the raw math left unpriced (a `needs_manual`), runs a second graded-only `engine=ebay` pass, builds a CGC/CBCS slab grade→price ladder, and prices `raw ≈ conservative_factor × slab[target]` as a **MEDIUM-LOW** band with the bid factor hard-capped, "CGC proxy" in Notes. The non-circular value gate is a **vintage-year gate** (the discount factor is calibrated to vintage eBay CGC *sold* prices), plus a minimum ladder-comp floor, a monotonicity refusal (an inverted/premium ladder is not priced), and a slab-title-only filter so raw copies can't pollute the ladder. See [fmv-doubled-title-issue-empty-comps-false-fetch-err.md](../logic-errors/fmv-doubled-title-issue-empty-comps-false-fetch-err.md) for the sibling ASM #50 incident that motivated the vintage-key batch (BUI-346/347/348).

**The core guidance of this doc is unchanged.** BUI-348 automated the *eBay-slab* proxy (structured `price.extracted`, bounded discount); it did **not** — and must not — automate the §7a **Heritage/Google-prose** extractor. Those remain two different things: the prose extractor's inputs are unstructured, its trigger is circular, and it stays human/LLM-gated inside `/comic:fmv`. A discount-basis caveat surfaced in BUI-348 reinforces the split: the eBay-CGC-*sold* factor is calibrated to a different price source than fmv.md §7a's Heritage *realized* basis — do not cross the multipliers.

## References

- **BUI-348** — shipped the eBay graded-slab proxy tier described in "The one defensible automation path" above; validated the re-spec framing (a distinct tier, not §7a-prose automation).
- **BUI-326** (Won't Do) — the automation ask; this doc is its writeup.
- **BUI-328** — filed spec correction: fmv.md §7a should state SerpApi isn't in apps/fmv and note the >$200 trigger circularity.
- **BUI-318** / [fmv-grade-curve-interpolation-overbid-guards.md](./fmv-grade-curve-interpolation-overbid-guards.md) — the sibling learning: automating §7 interpolation *was* safe, with guards. §7a is where automation stops being safe.
- `apps/fmv/src/fmv_runner.py` — subprocesses `ebay-sold-comps`; no SerpApi.
- `apps/ebay/src/sold_comps.py` — where SerpApi actually lives (`load_serpapi_key`, the `engine=ebay` fetch).
