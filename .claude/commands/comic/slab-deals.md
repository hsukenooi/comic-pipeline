---
name: comic:slab-deals
description: Find which active CGC copies of one book are good buys right now. Prints the CGC price ladder from the comps ledger and active listings ranked by ask over that grade's sale median, then prices a short shortlist with comic-fmv. Never bids.
---

# Comic Slab Deals

Answer "which copies of this book are good buys right now?" for one vintage CGC key. Most slabs are Buy It Now and priced above market, so the skill ranks every like-for-like CGC blue-label listing against what that grade actually sold for.

## Input

A book as series title, issue, and cover year (for example `Amazing Spider-Man`, `50`, `1967`). Pass `--comic-id <N>` when you know it (ASM #50 is `685`). Without it, the ledger resolves the book by title, issue, and year, and a book the comics server has never priced hard-fails.

## Step 1: Resolve and health-gate the comics server

Every run. The script reads `COMICS_SERVER_URL` from the environment and the eBay keys from the canonical env file.

```bash
set -a && source ~/Projects/comic-pipeline/apps/ebay/.env && set +a
source "$(git rev-parse --show-toplevel)/scripts/comics-server.sh"
comics_resolve_server || exit 1
comics-api GET /health >/dev/null || exit 1
```

If either step fails, stop. The ledger cannot be read.

## Step 2: Ladder, listings, ranking

```bash
slab-deals --title "<series>" --issue <N> --year <YYYY> --comic-id <ID> \
  --shortlist 3 --fmv-batch /tmp/slab-deals-fmv.json
```

`slab-deals` is a console script (run `./scripts/install.sh` if it is not on PATH). Add `--json` for machine output. One Browse API search covers auctions and Buy It Now, so a run costs one search call per 200 listings. Do not loop it.

It prints, in order:

1. **The CGC ladder.** One row per grade from the Universal (blue label) CGC sales in the comps ledger: sale count, median, low, high. A rung with 1 or 2 sales is thin. Say so when you quote it.
2. **A ranked table.** Each surviving listing with grade, type, ask, that grade's median, ask over median, the rung's sale count, and the link. Auctions show the end time, Buy It Now rows show the seller. A starred row is on the shortlist.

The script drops, and counts in the header line: foreign editions, moderns and reprints, variants and incentive covers, homages, lots, signed copies, and Qualified, Restored, and Conserved labels, plus anything that is not CGC or has no readable grade. A listing at a grade with no sales ranks last with `n/a`. No sales means no honest comparison, not a cheap one.

If the header says the search hit its result cap, rerun with a higher `--max-results` once, not repeatedly.

If the script exits non-zero, report its message and stop. A failed ledger read or Browse search is never an empty result.

## Step 3: Price the shortlist with comic-fmv

The script writes the `--shortlist` cap (default 3) of listings, lowest ask over median first and only at grades that have sales, to the batch file. The ratio is a screen. `comic-fmv` graded mode supplies the authoritative band and its provenance. Run it once on the whole shortlist, never per listing:

```bash
comic-fmv --batch /tmp/slab-deals-fmv.json --out /tmp/slab-deals-fmv-out.json --brief
```

Read `fmv.md` § Graded (slab) pricing mode for the result fields. A row with `flag_reason` is refused: show the reason, not a price. A Buy It Now row carries a band and no max bid.

Skip this step when the shortlist is empty.

## Step 4: Report

Lead with the answer: which listings, if any, sit at or below the band, or say none do. Then show the ladder, the ranked table with links, and for each shortlisted row the FMV band, confidence, and the Provenance string.

- **Auctions:** offer to hand an approved auction to `/comic:snipe-add` with a max bid the user confirms.
- **Buy It Now:** show the ask against the band and stop. The user decides.

## Rules

- **Never bid, snipe, or buy.** This skill reads and prices only. `/comic:snipe-add` runs only on an explicit user approval of a specific auction and bid.
- **The ladder is Universal CGC only.** Signed, Qualified, Restored, and Conserved copies are a different market and never enter it.
- **Ask over median is a screen, not a price.** A thin rung (n of 1 or 2) can mislead, so lean on the `comic-fmv` band for the decision.
- **Do not re-fetch or hand-rebuild comp pools.** The ledger and `comic-fmv` are the pools.
