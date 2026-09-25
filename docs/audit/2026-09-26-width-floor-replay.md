# Minimum band-width floor replay (BUI-982)

**Date:** 2026-09-26. **Source:** `uv run --project plugins/gixen-overlay python docs/audit/2026-09-26-width-floor-replay.py`, against the live comics DB opened read-only. It uses the 586 resolved auctions from BUI-977, the in-force band for each bid, and BUI-979's `band_comparison` with alpha 0.5.

**Decision: ship a 40% floor.** Both floors improve the Winkler score in every cut, and 40% scores best in all three. The money cost is small: in the post-BUI-528 cut, no replayed purchase clears above today's band top. BUI-528's concern holds for the displayed band top but hardly touches the bid cap.

## Method

- **Floor:** a band narrower than the floor (width = (high − low) / midpoint) widens to exactly the floor around the same midpoint. Wider bands are unchanged. The replay doesn't clean-round.
- **Cuts:** all rows. History means `band_source='history'`. Post-528 means history bands recorded on or after 2026-07-25.
- **max_bid:** each bid's actual `max_bid` scales by new high / old high, so haircuts (0.60 to 0.80 × `fmv_high`) and manual caps carry over.
- **Overpay:** dollars paid above the unfloored band top on auctions that the floor turns from LOST to WON. A WON auction costs nothing extra. eBay proxy bidding and a last-second snipe set its price from the runner-up, not from our max. A LOST auction flips when the final price is above our real `max_bid` and at or below the replayed one. We would pay at least the final price and at most the replayed `max_bid`.

## Results

| Cut | Set | Bands changed | Median Winkler / price | In band | Above | Below | MdAPE | Paired better / worse vs base |
|---|---|---|---|---|---|---|---|---|
| All (586) | base | – | 0.727 | 43.5% | 35.3% | 21.2% | 23.1% | – |
| | 30% | 223 | 0.712 | 51.7% | 29.5% | 18.8% | 23.1% | 153 / 70 |
| | 40% | 254 | **0.691** | 55.5% | 27.6% | 16.9% | 23.1% | 136 / 98 |
| History (445) | base | – | 0.769 | 40.0% | 37.5% | 22.5% | 24.2% | – |
| | 30% | 170 | 0.742 | 48.8% | 31.5% | 19.8% | 24.2% | 122 / 48 |
| | 40% | 185 | **0.714** | 52.4% | 29.7% | 18.0% | 24.2% | 104 / 63 |
| Post-528 (132) | base | – | 0.684 | 43.9% | 32.6% | 23.5% | 22.3% | – |
| | 30% | 56 | 0.667 | 52.3% | 28.0% | 19.7% | 22.3% | 36 / 20 |
| | 40% | 63 | **0.655** | 55.3% | 27.3% | 17.4% | 22.3% | 36 / 26 |

The ±10% hit rate, the ±20% hit rate, and MdAPE don't change, because the midpoint doesn't move. Median width stays at 40%. On the changed bands alone, the floor cuts the median Winkler score per dollar of price from 0.60 to 0.47 in the history cut. Head to head, 30% and 40% split the auctions (history: 88 versus 97).

| Overpay | Flips LOST → WON | Paid | Above unfloored top | Above real max_bid | WON rows with a raised cap |
|---|---|---|---|---|---|
| History, 30% | 31 | $1,101–1,144 | $18–27 (15 flips) | $48–91 | 39 (+$92 cap, not spent) |
| History, 40% | 45 | $1,615–1,716 | $26–44 (19 flips) | $85–186 | 42 (+$156) |
| Post-528, 30% | 9 | $362–376 | $0 | $10–24 | 6 (+$16) |
| Post-528, 40% | 13 | $589–621 | $0 | $31–63 | 7 (+$33) |
| All, 40% | 69 | $3,125–3,328 | $87–143 (27 flips) | $131–333 | 55 (+$251) |

The flip counts are an upper bound. In 27 of the 31 history flips at 30%, the final price was within one bid increment of our own `max_bid`. That means our bid set the price, and the winner's real max, which is unknown, may clear the replayed cap too.

## BUI-528's concern: a band top above the highest real comp

The highest real comp is the highest comp in the ledger for the same book and pool, within the band's grade window, sold before the bid. It covers 54 of the 56 changed post-528 bands. Its pool is wider than the band's, so these counts are lower bounds.

- The floored `fmv_high` is above the highest comp on 20 of 54 post-528 bands at 30% and 30 of 61 at 40%. The unfloored band top already is on 12 to 13. The concern is real for the displayed band.
- The replayed `max_bid` is above the highest comp on 0 post-528 bands and 1 history band. The haircut keeps the bid cap under the comps.

## Follow-up

- **Enforce a 40% Minimum FMV Band Width in comic-fmv.** Widen narrow published bands around the same midpoint, and keep `max_bid = bid_factor × floored fmv_high`. Pool changes need a live re-run after deploy. Re-score with `band_compare` once about 60 post-deploy auctions resolve.
