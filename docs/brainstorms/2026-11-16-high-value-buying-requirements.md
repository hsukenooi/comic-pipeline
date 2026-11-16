# High-Value Buying Workflow

Date: 2026-11-16. Final copy of the reviewed [Claude Doc](https://claude.ai/artifact/QZBpBfcETNj5h5xgwms78n).

## Goal

Buy fewer, higher-value comics ($150 to $750 each) for the collection, at or below market, from any seller. Start with CGC slabs on eBay. Every purchase stays a manual decision.

## How the Workflow Works

1. **Pick Targets** – Keep a hunt list of 15 to 25 books. Each book has a minimum grade and a ceiling price.
2. **Value Each Book** – The existing slab price model sets today's market price at each grade. For a Best Offer listing, the system also estimates the likely accepted price.
3. **Watch the Market** – Every 12 hours, a scan checks eBay for new listings and price drops on each target.
4. **Check the Copy** – Before any alert, the scan checks each copy: seller feedback, return policy, and the cert number and label in the listing's item details. A copy with a weak seller or no returns gets no alert. A copy with no cert number still alerts, marked unverified. CGC's terms forbid automated cert lookups, so you confirm the cert on CGC's site before buying.
5. **Get an Alert** – Each scan sends one Telegram message listing every copy that meets all the alert conditions in requirement 4. Each copy shows the check results, an opening offer, a walk-away price, and your remaining budget.
6. **Buy** – Snipe an auction through the existing flow. Make an offer or buy a Buy It Now listing by hand.
7. **Record It** – The purchase goes into the purchase record, the collection, and the monthly budget. The offer outcome goes into the offer log, which tunes the accepted-price estimate in workflow step 2. The book then leaves the hunt list, or its minimum grade rises above the copy you bought.

## Valuing a Book (Workflow Step 2)

Each hunt-list book gets a price for every CGC grade, plus a confidence level for each price. The existing slab model already does this, so this plan reuses it as is.

1. **Gather Sales** – When a book joins the hunt list, the system collects every CGC blue-label sale of it from the comps ledger. It adds PriceCharting's CGC sales of the same book and drops duplicates. More sales per grade means more grades reach Medium confidence. Each sale is tagged as an auction, a Buy It Now, or a Best Offer. About 40% of slab sales lack the Best Offer tag, so a one-time backfill comes first.
2. **Fix Best Offer Prices** – A Best Offer sale is recorded at its asking price. The model scales it to 0.89 of the ask, the median accepted share across 907 PriceCharting sales.
3. **Weight Recent Sales** – Sales from the last 90 days count fully, and sales from 91 to 365 days count half. Older sales drop out. Stronger recency weights tested worse (BUI-1231).
4. **Fill Thin Grades** – A grade with few sales borrows from its neighbors. For example, a 7.5 with one sale is priced between the 7.0 and 8.0 prices. A borrowed price is always Low confidence, so it never triggers an alert. A cross-grade price curve tested worse (BUI-1232).
5. **Score Confidence** – Each grade's price gets a confidence level from three things: how many recent sales back it, how much those prices vary, and how far it borrows from other grades. A price below Medium never triggers an alert.
6. **Refresh** – New sales update the prices as they arrive, so the market price tracks the current market.

For a Best Offer listing, the system also estimates what the seller will likely accept. It starts from the 0.89 baseline. It then adjusts for three things:

- **Listing Age** – A listing up 30 days or more likely takes a bigger discount.
- **Price Drops** – A seller who has already cut the price is motivated.
- **Seller History** – The seller's past accepted offers, from your offer log, show how far they usually go.

## Watching the Market (Workflow Step 3)

Every 12 hours, the scan looks at every live copy of every hunt-list book and passes only the promising ones to the checks.

1. **Search** – One eBay search per book finds every live copy: auctions, Buy It Now, and Best Offer listings.
2. **Drop the Wrong Copies** – The scan removes reprints, variants, lots, signed copies, and Signature Series, restored, qualified, or conserved labels, and anything below your minimum grade.
3. **Skip What You've Seen** – The scan remembers each listing. A copy comes back only when something changes: a price drop, a new Best Offer option, or an auction entering its last day.
4. **Price Each Copy** – Every price includes shipping and tax. Buy It Now uses the asking price. Best Offer uses the likely accepted price. An auction uses its current bid, checked again when it has less than a day left. Its maximum bid is the alert threshold or your ceiling, whichever is lower.
5. **Compare** – A copy goes forward when its expected price is at or under your ceiling and at or under the alert threshold: 90% of the market price for its grade. That market price must be Medium confidence or better. The 10% margin is a starting value to tune.
6. **Hand Off** – Copies that pass go to the pre-alert checks (workflow step 4). Copies that pass those checks go into one Telegram message per scan, largest discount first.

On a quiet scan, nothing is sent.

## Problem

The pipeline serves high-volume, low-value buying. `seller-scan` and `wishlist-sellers` start from sellers and filler series. Sniping covers auctions only. Higher-value books are mostly CGC slabs that many different sellers list as Buy It Now or Best Offer. A seller-first scan doesn't fit them.

Buying history shows the gap. Of 326 priced collection entries (by price paid in the collection store), 12 cost $200 to $500 and none cost more than $500.

The tools for valuing one slab exist: `/comic:slab-deals`, `comic-fmv` graded mode, and the slab-watch list. The slab price model is already backtested (BUI-1231, BUI-1232, BUI-1234). Three gaps remain:

- **Asks Overstate Prices** – Sellers set high Buy It Now prices and accept lower offers. A screen on the asking price skips most real Best Offer deals.
- **Scans Are Manual** – `slab-deals` checks one book per run. Good Buy It Now copies often sell within hours.
- **Purchases Go Unrecorded** – BUI-609 checks run only on snipes. A Buy It Now purchase never reaches the collection, the budget, or a check.

## Key Decisions

- **Collection First** – The goal is buying wanted books at or below market. Resale is not the goal.
- **Price Band** – $150 to $750 per book.
- **Graded First** – The cheap single-photo grader used to label comps scored 43% within ±1.0 against an 80% bar (`docs/audit/2026-09-28-photo-grading-accuracy.md`). The full /comic:grade grader has never been tested for accuracy. Raw-versus-graded buying waits until a grader passes.
- **Reuse the Price Model** – The existing slab model is already backtested. This plan adds PriceCharting sales, a Best Offer backfill, and an offer-log adjustment. Neither reaches alerts until it beats the current model in a backtest.
- **Best Offer Value** – Value a Best Offer listing at the likely accepted price, not the ask.
- **eBay First** – Other marketplaces join only after a terms-of-use check. We skip any site that forbids automated access, such as CGC's cert lookup.
- **Real Constraints** – Build cost is low. Upkeep, eBay API quota, paid sold-comps calls, alert volume, and site terms are the limits.

## Requirements

Requirements are in build order. Each depends on the ones before it unless noted.

1. **Price Model Upkeep** – Reuse the existing slab model as is. Backfill the Best Offer tag on older sales. Once the offer log has data, adjust the 0.89 baseline by seller and listing age. Each change must beat the current model in a backtest before alerts use it.
   - **PriceCharting Sales** – Add PriceCharting's CGC sales to the slab model, deduplicated against the comps ledger. The October audit (BUI-1226) measured a 0.125 Winkler gain and 115 newly priced sales, so more grades reach Medium confidence. Cache each book page, and fall back to ledger-only prices when a fetch fails. Keep PriceCharting data off any page someone else can see. Confirm the personal-use reading of PriceCharting's terms before merge.
2. **Purchase Record and Offer Log** – Reuse the existing eBay order pull (shipped_submit.py) to record every purchase in the collection and against the monthly budget. Run it at least every 30 days. Log each offer: ask, offer, counteroffer, and final price. This requirement doesn't depend on requirement 1.
3. **Hunt List** – Narrow the 90 slab-watch books to 15 to 25 targets. Add minimum grade and ceiling fields to the slab-watch override (`POST /api/comics/{id}/slab-watch`), which today stores only on or off. For a book you already own, set the minimum grade above your copy. This requirement doesn't depend on requirement 1.
4. **Alerts** – Scan targets every 12 hours for new listings and price drops. Send one Telegram message per scan, listing each copy where all of these hold:
   - The grade meets the minimum.
   - The expected price is at or under the ceiling.
   - The market price at that grade is Medium confidence or better.
   - The expected price, with shipping and tax, is at or under 90% of market (a starting margin to tune).
   - The copy passes the pre-alert checks (requirement 5).

   For a Best Offer listing, the expected price is the likely accepted offer. Three signals adjust it: listing age, past price drops, and the seller's discount history. Each copy shows an opening offer, a walk-away price, and the remaining budget. An auction shows a maximum bid for the snipe flow. Expect about 50 searches a day for 25 targets, plus one item fetch per new or changed copy. Requirement 5 ships before alerts go live.
5. **Pre-Alert Checks** – Check each copy in the scan before it can alert: seller feedback, return policy, and the cert number and label in the item details. Drop a copy with a weak seller or no returns. Mark a copy with no cert number as unverified instead of dropping it. The CGC cert lookup stays manual because CGC's terms forbid automated access.
6. **Dashboard Tab** – Show each target's market price, live copies, last alert, and remaining budget.
7. **More Data and Marketplaces** – Add sold data from other sources. Prefer one that shows real accepted offer prices (130point). Add live listings from Heritage, ComicLink, and MyComicShop. Each site passes a terms check first.
8. **Raw Grading Research** – A separate ticket against the 80% bar. It joins the buying flow only if it passes.

## Success Criteria

- **Price Model Upkeep** – Each change beats the current slab model's Winkler score on held-out sales, at every confidence level that can alert.
- **Alerts** – Each copy in an alert has Act and Pass buttons. After 30 days and at least 10 alerted copies, at least half are rated Act.
- **Purchase Record** – Every eBay purchase in the period appears in it.
- **Offer Log** – Once it holds at least 15 offers, the offer-log adjustment predicts accepted prices better than the flat 0.89 factor.
- **Scan Speed** – The scan logs when it first saw each copy and when the copy ended. After 30 days, you can see how many qualifying Buy It Now copies sold before any scan saw them.

## Out of Scope

- **Raw Books** – Raw-versus-graded buying waits until requirement 8 passes.
- **Automatic Buying** – Nothing places offers or buys. Every purchase stays manual.
- **Special Labels** – Signature Series, Qualified, Restored, and Conserved slabs.
- **Price and Purpose** – Books over $750, and resale or flipping.
- **Restricted Sites** – Any site whose terms forbid automated access.

## Open Questions

- [ ] What is the monthly budget for high-value buys? Alerts need it before requirement 4 ships.
- [ ] Which 15 to 25 books make the first hunt list? The proposal is to narrow slab-watch by hand together.
- [ ] Is 10% below market the right alert margin?

## Sources

All paths are in the `comic-pipeline` repo.

- `docs/brainstorms/2026-09-20-cgc-slab-support-requirements.md` (Best Offer was out of scope there)
- `docs/brainstorms/2026-08-02-capital-commitment-layer-requirements.md`
- `docs/audit/2026-09-28-photo-grading-accuracy.md`
- `docs/audit/2026-09-24-cgc-proxy-ratio.md`
- `docs/audit/2026-09-30-cgc-cert-harvest.md`
- `docs/audit/2026-10-11-slab-price-drift.md`
- `docs/audit/2026-10-11-grade-premium-curves.md`
- `.claude/commands/comic/slab-deals.md`
- `docs/audit/2026-10-18-slab-fmv-option-ranking.md`
- `docs/audit/2026-10-11-best-offer-comp-basis.md`
- `apps/fmv/src/fmv_math.py`
- `apps/ebay/src/shipped_submit.py`
