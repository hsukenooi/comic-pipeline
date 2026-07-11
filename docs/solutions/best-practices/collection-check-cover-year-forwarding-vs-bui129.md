---
title: "The year-gate false-negative was a WRONG-year bug, not a year bug — a correct per-issue cover year is Pareto-better"
date: 2026-07-11
category: docs/solutions/best-practices
module: locg-cli collection matcher (packages/locg-cli)
problem_type: best_practice
component: service_object
severity: high
related_components:
  - locg-cli
  - comic-collection-check
applies_when:
  - "Fixing a cross-volume collection-check false positive (owned issue matched to the wrong volume)"
  - "Deciding whether/how to forward a year into the ownership matcher's year gate"
  - "Tempted to soften matcher-side volume metadata to resolve a wrong-volume match"
tags: [collection-check, year-gate, cross-volume, masthead, false-positive, bui-129, matcher]
---

# The year-gate false-negative was a WRONG-year bug, not a year bug — a correct per-issue cover year is Pareto-better

## Context

BUI-129 is remembered as "passing a year to the ownership matcher hides owned books" (it once hid 16 owned X-Men). That framing makes "supply a year" look dangerous, which stalls the obvious fix for the *opposite* bug: the BUI-308 single-owned-wrong-volume false positive, where a no-year check on a rebootable masthead confidently reports `in_collection` against the wrong volume. This learning reframes BUI-129 so the correct fix stops looking risky, and records the corollary trap that future work will otherwise re-hit.

The relevant code lives in `packages/locg-cli/src/locg/commands.py` (`_year_gate_accepts`, `_has_cross_volume_ambiguity`, `_match_owned_issue`) and `collection_cache.py` (`_normalize_series_key`, `build_volume_candidates`).

## Guidance

**1. The reframing: BUI-129 came from supplying the WRONG year, not from supplying a year at all.**

The 16 owned X-Men were hidden because a series `year_began` (the volume's *start* year — 1963) was fed into a **per-issue** year gate that expects the *issue's* cover year. A 1980s issue gated against 1963 fails the gate and reads as not-owned. The defect was a category error (series-level year used as an issue-level gate), not the mere presence of a year.

**2. The consequence: a correct per-issue cover year is Pareto-better — it fixes the cross-volume false positive AND cannot reintroduce BUI-129.**

Forwarding the *listing's actual cover year* resolves the cross-volume collision (the year gate picks the right volume) and, because the year now genuinely matches the owned issue's release date within the ±1 cover-vs-onsale tolerance (BUI-214/251), it cannot recreate the BUI-129 false-negative. Wrong year → hides owned books; right year → strictly better than no year. The two are not the same lever.

**3. The corollary trap: do NOT "fix" the wrong-volume false positive by softening matcher-side volume metadata.**

The tempting shortcut — loosen how `_normalize_series_key` / `build_volume_candidates` distinguish volumes so a wrong-volume match stops firing — **over-fires on exactly the most-collected mastheads**. ASM, X-Men, and Fantastic Four all carry multiple volumes in any broad collection, so softening volume metadata turns real distinct-volume ownership into collisions across the whole heavy part of the collection, *and* it still misses the single-owned-wrong-volume case (only one row matches, so there is nothing to disambiguate). The fix belongs at the **input** (forward a correct cover year), not in the matcher's volume model.

**4. Until the input carries a confidence-gated cover year, the residual is an accepted operator-vigilance case, not a matcher bug.**

The single-owned-wrong-volume false positive is documented as **Pattern D3** in `.claude/commands/comic/collection-check.md` and treated as an accepted residual (BUI-146-style). Direction matters: it reports owned when you don't own the volume you meant → a **missed purchase**, not a BUI-122 data-loss — so vigilance (eyeball the Matched Volume column on rebootable mastheads; re-check with `?year=<cover-year>` when in doubt) is a safe stopgap. The proper fix — forwarding a confidence-gated per-issue cover year from the identify step — is tracked as **BUI-316**.

## Why This Matters

The wrong mental model ("year = dangerous") blocks the one change that is strictly safe (forward the *right* year) while making the actively harmful change (soften volume metadata) look reasonable. Both are cross-collection correctness bugs on the mastheads you collect most. Naming BUI-129 precisely — wrong-year, not year — is what lets the next person reach for the correct fix instead of relitigating whether years are safe at all.

## When to Apply

- Implementing BUI-316 (or any confidence-gated cover-year forwarding from identify into collection-check).
- Triaging a new cross-volume false positive or false negative in the ownership matcher — first ask "is the year wrong, or just present?"
- Reviewing any PR that proposes relaxing `_normalize_series_key` / `build_volume_candidates` volume distinctions to fix a match — treat it as an over-fire risk on ASM/X-Men/FF and push the fix to the input year instead.

## Examples

The category error at the heart of BUI-129:

```
issue:        Uncanny X-Men #250   (cover year 1989)
WRONG gate:   year = 1963  (series year_began, a SERIES-level value)
              1989 issue vs 1963 gate → fails → "not owned"  (BUI-129 false negative)
RIGHT gate:   year = 1989  (the issue's cover year)
              matches owned release date within ±1 → "owned"  (correct; cannot reintroduce BUI-129)
```

Why matcher-side softening over-fires (the trap to reject):

```
Fantastic Four #18 owned in Vol. 1 (1961) AND Vol. 7 (2022)
  soften volume metadata → the two volumes collide → real distinct ownership misreported
  ...and single-owned-wrong-volume STILL slips through (one row, nothing to disambiguate)
Correct lever: forward the listing's cover year; let _year_gate_accepts pick the volume.
```

## Related

- **BUI-308** — the single-owned-wrong-volume residual, documented as Pattern D3 in `.claude/commands/comic/collection-check.md`.
- **BUI-129** — the original false-negative (series `year_began` used as a per-issue gate; hid 16 owned X-Men).
- **BUI-284** — cross-volume ambiguity guard (`ambiguous_cross_volume`, Pattern D2) — the multi-owned sibling of the D3 residual.
- **BUI-214 / BUI-251** — the ±1 cover-vs-onsale year tolerance that makes a correct cover year safe against the gate.
- **BUI-146** — accepted-residual precedent (a directional false positive left un-mechanized when the only real fix is upstream disambiguation).
- **BUI-316** — the proper fix: confidence-gated cover-year forwarding from identify.
