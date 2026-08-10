---
title: "The modern raw:slab factor is unmeasurable exactly where the CGC proxy would use it"
date: 2026-08-09
category: best-practices
module: "apps/fmv/src/fmv_runner.py (_is_vintage, _apply_cgc_proxy_rescue), apps/fmv/src/fmv_math.py (cgc_proxy_fmv, cgc_ladder_price)"
problem_type: best_practice
component: fmv_pipeline
severity: high
status: corrected
superseded_by: "BUI-717 (Canceled on measurement 2026-08-10): the out-of-scope aside below is wrong on both halves — no policy check reads fmv.low (all read high; verified by grep of policy.py), and the cross-title exclusion class dissolved on oracle measurement (1 of 19 rows moves, toward a LOOSER cap). See docs/solutions/best-practices/grep-the-consumer-before-claiming-a-field-weakens-a-guard.md. The doc's core claim (modern raw:slab factor unmeasurable, refusals stay) stands."
mechanized_by: test
enforced_by_test:
  - apps/fmv/tests/test_fmv_runner.py::TestModernCgcProxyStaysRefused
related_components:
  - "fmv"
applies_when:
  - "A ticket proposes lifting the CGC-proxy tier's `_is_vintage` year gate to modern books"
  - "A ticket proposes a second, separately-calibrated raw:slab multiplier for any era"
  - "Reasoning about why a valuable modern key with a deep slab ladder still lands needs_manual"
  - "About to relax `cgc_ladder_price`'s no-extrapolation refusal to reach a target grade"
tags:
  - "fmv"
  - "cgc-proxy"
  - "raw-slab-factor"
  - "oracle-bound"
  - "no-extrapolation"
  - "needs-manual"
  - "measurement"
  - "negative-result"
---

# The modern raw:slab factor is unmeasurable exactly where the CGC proxy would use it

## Context

BUI-714 proposed extending the BUI-348 CGC-proxy rescue past its `year < 2000`
gate, using a separately-measured **modern** raw:slab factor (the vintage tier
uses 0.50–0.55). The motivating book was Invincible #2 (2003): it went
`needs_manual` on a one-sided raw pool while eBay's slab ladder was deep and
clean, and one datum suggested a modern factor near 0.67.

Measured before writing any mechanism, against the prod comps ledger plus one
bounded `--include-graded` fetch (8 modern books, 10 provider queries, no
`--force`). The tier was not extended. Three findings, in increasing order of
how much they settle the question.

## 1. The stored ledger holds zero modern raw:slab pairs

14,655 raw comps over 417 comics — and **206 slab comps over 35 comics, every
one of them vintage** (newest: X-Force #11, 1992). This is not an accident of
what we happened to buy: BUI-524's inclusive raw+slab pass is itself gated
`is_vintage`, so a modern book's fetch has never once requested slab comps.

The vintage side cannot self-validate either. Pairing every stored slab ladder
against `compute_fmv` run on the same comic's raw comps yields **18 cells across
8 books, none clearing the tier's own $400 slab floor**, with the raw side
visibly polluted by cross-title matches. The shipped 0.50–0.55 rests on the
single hand-checked ASM #50 observation it always did.

## 2. Where a modern slab ladder is deep, the raw side is empty — by the same market fact the tier is built on

One inclusive fetch per book returns both sides from the same pool, so the ratio
is not confounded by fetch timing. Across 8 modern keys (Invincible #19/#61/#51/
#120/#2, Walking Dead #1, Ultimate Fallout #4, Edge of Spider-Verse #2), the
whole exercise produced **3 usable calibration cells across 2 books, and 0 in
the tier's own regime (slab ≥ $400)**.

The books with the deepest, most expensive ladders are the ones with no
priceable raw side at all — Walking Dead #1 came back 10 slab / 2 raw, Ultimate
Fallout #4 12 slab / 8 raw with `compute_fmv` unable to price a single grade.
That is not a sampling shortfall to be fixed with more fetches. It is the same
fact the proxy tier exists to exploit, read from the other end: on a modern key
the certified copies are the market, so the quantity we would need in order to
calibrate the substitution is precisely the quantity that has gone missing.

The three cells that do exist say the opposite of the premise:

| book | grade | raw fmv_high | slab[G] | ratio |
|---|---|---|---|---|
| Invincible #61 | 7.0 | $325 | $200 | **1.62** |
| Invincible #51 | 9.2 | $140 | $110 | **1.27** |
| Invincible #61 | 9.2 | $200 | $261 | **0.77** |

Median 1.27, 2.1× spread on n=3 — and a 2.1× swing **within a single book**
across two grade bands, which is the grade-dependence that killed BUI-713's
anchor gate showing up again on a different estimator. Neither the ticket's 0.67
nor the shipped code comment's "modern ratio is far lower" survives contact with
this: modern raw trades at or *above* the slab price at the same grade, not
below it. **When both the ticket's premise and the code's stated rationale point
the wrong way, there is no factor left to choose conservatively.**

## 3. The factor is not the blocker anyway — the no-extrapolation guard is

The decisive number needs no factor at all. Feed each measured modern ladder to
the shipped `cgc_proxy_fmv` at the grade the book is actually graded at, with
the year gate hypothetically lifted and nothing else changed:

**It fires 0 times out of 9.** Not once, including on Invincible #2 itself.

The refusals are all pre-existing guards, and the dominant one is structural:

| refusal | books |
|---|---|
| target grade below the observed slab ladder (no extrapolation) | Invincible #2, #120, Walking Dead #1 |
| slab price under the $400 floor | Invincible #61, #51, Ultimate Fallout #4 |
| non-monotonic ladder | Invincible #19 |
| ladder too thin | Edge of Spider-Verse #2 |

**Of 57 modern slab comps, 3 (5%) were at or below grade 8.5.** Moderns get
slabbed at 9.4–9.8, while the five keys the ticket names (Invincible
#2/#7/#10/#11/#13) are graded 7.5–8.5. So the raw book we are pricing sits
*below the entire certified ladder*, and `cgc_ladder_price` correctly refuses
rather than extrapolate — which means a modern extension is inert unless someone
also relaxes that money guard.

**What relaxing it would cost, in dollars.** Extrapolating flat off the lowest
observed slab, at the ticket's proposed 0.67 factor and the tier's 0.70 bid cap:

| book | ladder bottom | implied fmv_high at 8.0 | implied max_bid | reality |
|---|---|---|---|---|
| Invincible #2 | 9.2 = $550 | $368 | $258 | hand-priced cap $234.50 |
| Walking Dead #1 | 9.4 = $1,400 | $938 | **$657** | a raw 8.0 trades ~$250–400 |

The first is a $23 error. The second is a ~2× over-bid on a four-figure book,
generated entirely by pricing an 8.0 copy off a 9.4 slab — the wild-guess case
the no-extrapolation guard was written for. The guard is what stands between the
two rows, not the factor.

(The wider live modern `needs_manual` population runs 7.5–9.0; the 9.0 rows are
cheap mid-run books — anchors $25–$31 — that the $400 slab floor refuses
independently.)

## Guidance

- **Keep the `_is_vintage` gate.** It is right, but write the right reason on
  it: not "the modern ratio is lower" (measured false), but "the modern factor
  is unmeasurable in the regime it would be used, and the ladder does not reach
  the grades we buy."
- **Do not relax `cgc_ladder_price`'s no-extrapolation refusal to make a modern
  book reachable.** That refusal, not the factor, is what makes the extension
  inert — and it is inert in the safe direction.
- **A modern key with a deep slab ladder and no raw comps is correctly
  `needs_manual`.** All five live modern candidates were hand-priced to sane
  caps ($161–$275, ~$1,182 total already capped). The tier would not have
  improved one of them.
- **Before proposing a second multiplier for any estimator, ask whether the
  calibration cohort and the firing cohort can co-exist in the same book.** Here
  they cannot, by construction: a book with both a deep slab ladder and a
  priceable raw pool is a book that never needed the rescue.

## Out-of-scope findings

- **21 prod `fmv` rows carry `low = $0`**, the largest being X-Men #96 @8.5
  (`$0–$90`). Its stored raw comps include cross-title matches — "Ultimate X-Men
  #96", "Uncanny X-Men '96 Special", "X-Men '96 #1" — filed under the 1975 key.
  ~~A `$0` band is not a price, and `fmv.low` is read by the overlay's policy
  checks.~~ **CORRECTED (BUI-717, 2026-08-10): no policy check reads `fmv.low` —
  every guard reads `high` — and the $0 lows are `clean_round`'s $5-step
  artifact on $2–7 books, not pollution. The cross-title class dissolved on
  oracle measurement; only fmv 917 (this X-Men #96 row) was genuinely mispriced
  (BUI-720). See the `status:` stamp above.**
- **Invincible #19's modern ladder is non-monotonic** (9.4 = $835 > 9.6 =
  $733.50) because the 9.4 bucket is a single comp — the BUI-349 lone-outlier
  shape, appearing on a modern book.

## See also

- `docs/solutions/best-practices/ungraded-anchor-is-not-a-price-2026-08-09.md` —
  BUI-713, which named BUI-714 as "the place the triangulation idea can still be
  tested; it needs its own oracle first". This is that oracle: it is empty.
- `docs/solutions/best-practices/size-the-oracle-ceiling-before-designing-a-classifier.md`
  — the discipline applied here, in both directions (calibration ceiling and
  coverage ceiling).
- `docs/solutions/best-practices/fmv-7a-cgc-proxy-not-safely-automatable.md` —
  why the proxy tier is narrow on purpose; this result reinforces it.
