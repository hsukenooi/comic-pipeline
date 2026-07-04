# Concepts

> Shared domain vocabulary for this project — entities, named processes, and status concepts with project-specific meaning. Seeded with core domain vocabulary, then accretes as ce-compound and ce-compound-refresh process learnings; direct edits are fine. Glossary only, not a spec or catch-all.

## Naming (BUI-220)

> **Gixen names the bidding service only; the thing that stores your data is the comics server, which runs on the Mac Mini.**

"Gixen" is overloaded and easy to misapply. It correctly names the **external** bidding service (gixen.com) we push snipe bids to. It does **not** name our self-hosted server, its URL, its data dir, or its launchd job — those store the collection/wish-list/listings/FMV/bids and have nothing to do with Gixen the company. The conflation is at the **server** layer, not the CLI layer: the `gixen-cli` package and the `gixen` console script are named correctly (they automate the Gixen bidding service); the FastAPI server they host was mislabeled "the gixen server" and is really **the comics server**.

| Term | Meaning |
| --- | --- |
| **Gixen** | The external bidding service at gixen.com that we push snipe bids to. Keep "gixen" wording for the `gixen` console script, the `bids` table, snipe/sniping operations, and the bidding service itself. |
| **Comics server** | Our self-hosted FastAPI app (the host of `gixen-cli`'s server + the gixen-overlay plugin). Stores the collection, wish-list, listings, FMV, and bids; serves `/api/comics/*` and `/api/snipes`. This is what was wrongly called "the gixen server." |
| **Mac Mini** | The physical host the comics server runs on. |
| **LOCG** | League of Comic Geeks — the external collection tool we sync to (a downstream mirror, not the source of truth). |

The canonical env var for the comics server URL is **`COMICS_SERVER_URL`**; `GIXEN_SERVER_URL` is a deprecated alias that is still accepted.

## Collection & Lists

### Collection
The canonical record of the comics you own. The **comics server store (on the Mac Mini) is the source of truth**; League of Comic Geeks (LOCG) is a downstream mirror used for browsing and bulk sync, not the system of record.

### Wish List
Comics you want but do not own. Distinct from the Pull List. The **Mac Mini (server) is authoritative** for the wish list (BUI-208, Option B): wishes are added via `/comic:wishlist-add` and reads (e.g. seller scanning) come from the server, never LOCG. Wish state lives in a single store (`wish-list.json`) keyed on an explicit `source: local | export` field; the LOCG import does **not** source wishes, so a server-side removal is durable across an import (this dissolves the old BUI-206 resurrection bug). LOCG is a downstream mirror; mirroring wishes *up* to LOCG is an opt-in, owned-safe step, deferred by default.

### Pull List
Comics you subscribe to receive as new releases through your local comic shop. Managed on LOCG and **never modified by the collection sync** — the bulk-import format has no pull-list field, so syncing cannot add to or remove from it.

### Win-Sourced Entry
A Collection entry created by recording a won eBay auction, before it has round-tripped through LOCG. *Known in code and tickets as:* `agent_win`.

Win-sourced entries carry no publisher (record-win does not supply one) and often a best-guess release date, which is why reconciling them against a LOCG export must tolerate a missing publisher and match on year rather than exact date.

### Import-Sourced Entry
A Collection entry that originated from — or has round-tripped through — a LOCG export. *Known in code and tickets as:* `locg_export`. The counterpart to a Win-Sourced Entry.

### Pending Push
A Collection entry that has been recorded locally but not yet confirmed present on LOCG. Clearing pending entries is the goal of a Collection Sync; an entry stays pending until it reappears in a LOCG export and reconciles.

## Sync Processes

### Record-Win
The process of recording a won eBay auction into the Collection as a Win-Sourced Entry.

### Collection Sync
The round-trip that mirrors the Collection up to LOCG and reconciles it back: export the pending entries to a bulk-import file, upload it to LOCG, re-export from LOCG, and re-import to clear pending.

The export is **owned-safe**: it never instructs LOCG to un-collect a book you own. LOCG's bulk import treats an `In Collection=0` row as "remove from collection," so the export pushes only genuinely-new wishes you do not already own. The re-import is reconciliation-based: it matches a pending Win-Sourced Entry to its LOCG counterpart even when LOCG has canonicalized the publisher or release date, and never creates a duplicate-identity entry. As of BUI-208 the up-CSV is **wins-only by default** — the code refuses to emit any `In Collection=0` row unless an explicit owned-safe wish push is requested (a machine-enforced gate, on top of the human-reviewed LOCG import preview). There is **no row-count limit** on uploads; the importer hangs only on incomplete/dateless rows (the old "≤20 rows" advice was a misdiagnosis).

## FMV & Pricing

### First-Party Comp
A sold-price comp sourced from **your own** resolved eBay auctions (`bids.winning_bid`), merged into the FMV comp pool alongside external eBay sold comps (BUI-286). Because a proxy-auction win's price is only ever *at or below* your max, a wins-only set is **truncated from above** and biases FMV down — so first-party comps are always pulled as wins **and** losses together, and a book whose in-window set is wins-only is dropped rather than merged (see the deflation-guard learning in `docs/solutions/best-practices/`).

### Calibration Report
A **diagnostic-only** audit (BUI-288, `/comic:calibration-report`, `GET /api/comics/calibration`) that ranks issues whose FMV is set too low, so you know which books to re-price. It never bids, snipes, or writes FMV. It keys on **Overshoot vs `fmv_high`**, never on raw win/loss rate — losing is the *intended* outcome of the 80% bid haircut, so a high loss count is not a mispricing signal.

### Overshoot
The Calibration Report's ranking metric: `median(winning_bid / fmv_high)` over a book's **losing** auctions. Persistently `> 1` means the market keeps clearing above your stated fair-value ceiling, i.e. FMV is too low. A minimum loss count gates single-loss noise out of the ranking.
