# First paired band comparison: the August reprice (BUI-979)

**Date:** 2026-09-25. **Source:** `uv run python -m gixen_overlay.band_compare --db ~/.comics-server/db.sqlite --successive-snapshots --rows`, run from `plugins/gixen-overlay` against the live DB opened read-only. Alpha is 0.5, because the bands are roughly the pool's weighted Q25 to Q75.

## What was scored

The comparison scores two band sets on the same resolved auctions. It uses the same auction selection as the BUI-977 accuracy report (586 auctions, matching the baseline). For this run the two sets are:

- **Prior** – the `fmv_history` snapshot before the one in force when the bid was added.
- **In force** – the snapshot in force when the bid was added (the band BUI-977 scores).

Both snapshots predate the bid, so neither can contain the auction's own price. The change being scored is the `comic-fmv` re-run of 2026-08-07 (one book re-ran 2026-08-11). Most prior snapshots are from 2026-07-24 to 2026-08-03, and one is from 2026-04-28. That interval includes shipped comp-pool rules (BUI-668, BUI-675, BUI-678) as well as newer comps, and the comparison can't separate the two effects.

Only 12 auctions have two pre-bid snapshots, because `fmv_history` only began on 2026-08-03. Five of them got a different band, and 11 of the 12 are LOST auctions.

## Result

| Set | Paired n | Median Winkler / price | Median Winkler | Within ±10% | Within ±20% | MdAPE | In band | Above | Below | Median width |
|---|---|---|---|---|---|---|---|---|---|---|
| Prior | 12 | 0.65 | $30.38 | 25.0% | 58.3% | 19.2% | 25.0% | 25.0% | 50.0% | 22% |
| In force | 12 | 0.46 | $23.00 | 25.0% | 66.7% | 13.4% | 25.0% | 33.3% | 41.7% | 29% |

Per auction, the in-force band scores better on 4 auctions, the prior band on 1, and 7 tie with identical bands. The gains come from three prior bands that sat entirely above a lower final price (bids 652, 696, and 703). The one loss is bid 667, where the new $50 to $70 band was wider than it needed to be for a $52 sale.

## Reading it

- The direction favors the reprice on every summary metric, but n=12 with five changed bands is too few to generalize. Treat this as proof that the comparison works on real data, not as a verdict on those rules.
- To score a specific rule cleanly, recompute one band set with the rule turned off from comps observed before each bid (a replay from the comps ledger). This harness can score any such pair of band-set JSON files with `--a` and `--b`.
- The number of paired auctions grows as snapshot-bearing books are re-priced and their auctions resolve.
