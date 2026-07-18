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

A subset needs manual resolution first: when the matcher cannot confidently determine an entry's canonical series (or its variant), the entry is flagged and excluded from every automated bulk-import — no Collection Sync can clear it as-is. It only clears once a person adds the title directly in LOCG and a subsequent Collection Sync re-import reconciles it. Because these entries never enter an automated import, they can sit at maximum pending age indefinitely without that age indicating a stale or missed Collection Sync.

## Matching & Volumes

### Masthead
A long-running comic title (Amazing Spider-Man, X-Men, Fantastic Four, Batman) that has been relaunched as multiple numbered **volumes** over its history. Because the same issue number recurs across a masthead's volumes, ownership and pricing must disambiguate *which* volume, not just the series name and issue number — the mastheads you collect most heavily are exactly where volume collisions bite.

### Cross-Volume Ambiguity
The ownership-matcher state where a queried issue number is owned under more than one volume of the same Masthead and no Cover Year was supplied to disambiguate — a verdict that is **neither owned nor not-owned**. *Known in code and tickets as:* `ambiguous_cross_volume` / `match_kind == "cross_volume"`. Resolved by re-checking with the listing's Cover Year; the matcher must never guess a volume on its own. Its harder-to-detect sibling is the **single-owned-wrong-volume** residual — when only one volume is owned there is no detectable ambiguity, yet that single owned volume may still be the wrong one, so a no-year match can confidently report owned against a volume you did not mean.

### Cover Year
The publication year printed on an issue's cover, used as the **per-issue** key the matcher's year gate compares against a stored release date (within a small tolerance for cover-vs-onsale skew). Distinct from a series' **start year** (`year_began`): feeding a series start year into the per-issue gate is the wrong-year error that hides owned books, whereas the correct per-issue Cover Year disambiguates volumes without that risk.

### Printing
A specific press run of an issue — the base (first) printing, or a numbered reprint ("2nd Printing", "3rd Printing", a bare "Reprint", …). **Printings are distinct collectibles, not variants of one book**: owning a reprint is not owning the base printing, and vice versa (confirmed incident, BUI-364 — an owned "2nd Printing" of *Absolute Martian Manhunter #1* satisfied a check for the base printing, hiding the fact that the base was explicitly wish-listed and still unowned). The ownership matcher's series+issue core deliberately ignores everything after the issue token, so it can conflate printings unless a caller reads the mechanical `printing_conflict` flag (plus the `printing_candidates` list, each carrying a `printing_ordinal`) that every collection-check verdict, the `POST /api/comics/wish-list` 409, and the wish-list conflicts audit all carry (BUI-364/BUI-372/BUI-373) — advisory only (R11): the flag qualifies a verdict, it never flips one, and the conflicts audit keeps a printing-conflict match out of its removable set entirely rather than risk it being swept as a genuine duplicate.

## Sync Processes

### Record-Win
The process of recording a won eBay auction into the Collection as a Win-Sourced Entry.

### Seen-Set
The set of won-auction item IDs already recorded into the Collection, used by Record-Win to skip wins it processed in a prior run — the **primary** cross-run dedup for `/comic:collection-add`.

A second, independent net (the server's already-owned check) sits behind it: a book already in the Collection is rejected even if it slips past the seen-set. The two are not redundant — the seen-set prevents *reprocessing* at all (and the token/cost blowup of re-identifying dozens of already-recorded wins), while the already-owned check only prevents a duplicate *write*. Correctness and cost should ride on the seen-set; the already-owned check is a backstop, not a substitute. A fetch of the seen-set that fails locally (unreachable server, unset URL) must hard-stop, never fall back to an empty set — an empty seen-set silently reclassifies every prior win as new.

### Collection Sync
The round-trip that mirrors the Collection up to LOCG and reconciles it back: export the pending entries to a bulk-import file, upload it to LOCG, re-export from LOCG, and re-import to clear pending.

The export is **owned-safe**: it never instructs LOCG to un-collect a book you own. LOCG's bulk import treats an `In Collection=0` row as "remove from collection," so the export pushes only genuinely-new wishes you do not already own. The re-import is reconciliation-based: it matches a pending Win-Sourced Entry to its LOCG counterpart even when LOCG has canonicalized the publisher or release date, and never creates a duplicate-identity entry. As of BUI-208 the up-CSV is **wins-only by default** — the code refuses to emit any `In Collection=0` row unless an explicit owned-safe wish push is requested (a machine-enforced gate, on top of the human-reviewed LOCG import preview). There is **no row-count limit** on uploads; the importer hangs only on incomplete/dateless rows (the old "≤20 rows" advice was a misdiagnosis).

### Conflicts Audit
The audit of the Wish List for entries you already own, so a Collection Sync's wish push can drop them before uploading (`GET /api/comics/wish-list/conflicts`, BUI-130). It is a **surfacing** layer, not a data-safety guard: it decides which wishes to *show*, and its removal half deletes only from the **Wish List**, never from the Collection.

The guarantee that an owned book is never sent to LOCG for deletion lives entirely in the **owned-safe export** (above), independently and **year-blind** — so a mistake in this audit can only fail to clean a wish, never delete an owned book. The audit is year-blind by default (a Wish List name carries no year), which lets it match a wish against the wrong volume/era of the same issue number; since BUI-387 a wish may carry an optional per-issue **Cover Year** that scopes its check to the matching volume (an unstamped wish stays year-blind — the safe over-flagging default). It is also **Printing**-aware: a printing-conflict match is held out of the removable set rather than swept as a duplicate.

## Bidding & Snipes

### Snipe
A scheduled last-second bid on an eBay auction, placed through Gixen rather than directly on eBay. A snipe runs from pending to a terminal outcome (won, lost, ended-unresolved, failed); removal from the working set is a [[Tombstone]], never a terminal outcome.

An ended-unresolved snipe may have its true outcome recovered by inference from the auction's final price (a price under our max reads as a win). That inference is deliberately permissive — see [[Phantom WON]] for the failure class this creates and the guard that contains it.

A listing and a snipe are not one-to-one: the same auction listing can accumulate multiple snipe records over its life (one re-added after an earlier one resolved, or a duplicate collapsed into a [[Tombstone]]). Lifecycle state belongs to the individual snipe record, never to "all snipes on this listing" — resolving or removing by listing alone stamps records that are still pending.

### Bid Group
A set of snipes Gixen treats as alternatives for the same want: when one member wins, Gixen cancels the remaining siblings before their own auctions end. Group numbers come from a small fixed pool that Gixen recycles across campaigns, so a group number alone never identifies a campaign — any evidence keyed on a group must also be bounded by the individual snipe's own lifetime.

That lifetime bound was itself refined to group *membership*: a snipe can join a group after another member already won it — a retroactive grouping applied on Gixen's web UI, or a plain edit — and the snipe's own lifetime alone would predate that win even though it was never a member of the group when the win happened. A win from before a snipe's membership began is not cancel evidence for it. *Known in code and tickets as:* the `group_changed_at` column, stamped whenever a snipe's group actually changes (BUI-384).

A cancelled sibling that is never purged still reaches its auction's end and re-enters outcome classification, which is why cancelled-sibling handling is built into classification itself rather than depending on manual post-win cleanup.

### Group-Win Evidence
A durable, append-only record that a [[Bid Group]] member won, kept so a cancelled sibling can still be classified even after the winning snipe itself is swept to a [[Tombstone]]. Distinct from the live snipe records: purging the winner no longer destroys the proof that its siblings were cancelled, which is why post-win purge is now hygiene rather than a correctness requirement.

Only genuine auction ends are recorded (never an observation-time approximation, which could falsely implicate a sibling added after the real win), and the record is consulted permissively — an ambiguous or missing entry weakens the evidence but never fabricates a cancel. *Known in code and tickets as:* the `group_wins` ledger.

### Tombstone
The soft-delete status for a snipe removed from the working set — written when a live snipe is removed, when completed bids are swept, or when evidence shows the bid was cancelled before its auction ended (a group-cancelled sibling). It is **not** a terminal auction outcome and must be excluded from every results view and from outcome inference. *Known in code and tickets as:* `REMOVED` (formerly `PURGED`).

### Phantom WON
The failure class where the system records a win on an auction it never actually bid: a snipe cancelled while live still has its auction end, and a final price under the snipe's max reads as a win to price-based outcome inference. The guard is evidence-layer disambiguation — a row with positive evidence of a pre-end cancel (it vanished from Gixen well before its auction end, or a [[Bid Group]] sibling already won within its lifetime) is tombstoned before inference runs. The inference itself stays permissive: requiring bid evidence would suppress the genuine wins it exists to recover.

## FMV & Pricing

### Money Path
The chain of computation whose output the system will act on financially — a bid cap, max bid, or FMV band that real money follows. Guards on the money path are asymmetric by design: they may only ever move a price **down**, never up (a too-high cap overpays with real money; a too-low cap only misses an auction). A statistic feeding the money path must be outlier-robust before it is trusted — a median resists a single outlier only from three samples up; below that it is the sample itself or the plain mean. Diagnostic-only statistics (the [[Calibration Report]]'s metrics) are outside the money path and may deliberately trade robustness for coverage.

### First-Party Comp
A sold-price comp sourced from **your own** resolved eBay auctions (`bids.winning_bid`), merged into the FMV comp pool alongside external eBay sold comps (BUI-286). Because a proxy-auction win's price is only ever *at or below* your max, a wins-only set is **truncated from above** and biases FMV down — so first-party comps are always pulled as wins **and** losses together, and a book whose in-window set is wins-only is dropped rather than merged (see the deflation-guard learning in `docs/solutions/best-practices/`).

### Calibration Report
A **diagnostic-only** audit (BUI-288, `/comic:calibration-report`, `GET /api/comics/calibration`) that ranks issues whose FMV is set too low, so you know which books to re-price. It never bids, snipes, or writes FMV. It keys on **Overshoot vs `fmv_high`**, never on raw win/loss rate — losing is the *intended* outcome of the 80% bid haircut, so a high loss count is not a mispricing signal.

### Overshoot
The Calibration Report's ranking metric: `median(winning_bid / fmv_high)` over a book's **losing** auctions. Persistently `> 1` means the market keeps clearing above your stated fair-value ceiling, i.e. FMV is too low. A minimum loss count gates single-loss noise out of the ranking.

### Grade-Curve Interpolation
Estimating an FMV for a comic at a grade with no direct sold comps by reading a price off the curve implied by comps at neighbouring grades. It is a **fallback only when the target grade's bucket is empty** — never used when real comps exist at the target grade — requires a minimum number of supporting comps, and its output is marked as interpolated at **low confidence** (including through cache reuse) so it is never conflated with a direct-comp price (see the over-bid-guards learning in `docs/solutions/best-practices/`).

### Envelope Clamp
An upper bound applied when a price is read from a comp bucket too thin to be outlier-robust: the direct bucket value is capped at the price its trustworthy neighboring grades imply, taking the lower of the two. It never rejects the thin bucket outright — a genuinely sparse key still gets priced — and it can only ever lower a price, never raise one ([[Money Path]] asymmetry). When no trustworthy neighbors exist to form the bound, the direct value is used unchecked; that residual case is the irreducible sparse-key exposure.

### needs_manual
The FMV verdict emitted when even the fallbacks can't defensibly price a book (raw sold comps too thin, target grade's bucket empty and interpolation unsupported). It is a deliberate **punt to a human/LLM**, not a failure — the book gets hand-priced with judgment inside the `/comic:fmv` skill rather than auto-bid on a shaky estimate. Automating away a `needs_manual` on a high-value key removes the human check exactly where a mistake costs the most.

### CGC Proxy
Pricing a book off graded-slab (CGC/CBCS) prices instead of raw sold comps, discounted to a raw-equivalent. Two distinct forms exist, and they must not be conflated:

- **§7a Heritage-prose proxy** — the fmv.md §7a step reading realized graded prices from Google/Heritage/GoCollect **prose**. It is **human/LLM-gated by design and deliberately not automated**: its inputs are unstructured (no extractable sold-price field), its value-based trigger is circular (no value estimate exists precisely when comps are too thin to price), and a mis-read number would be an unbounded over-bid in the bid-cap path. A future ask to automate *this* form should stop here (see the not-safely-automatable learning in `docs/solutions/best-practices/`; BUI-326 Won't Do).
- **eBay-slab proxy tier** — the automated form: a second graded-only eBay-sold pass builds a slab grade→price ladder, and a raw price is read off it at a conservative discount, emitted at capped (low) confidence and only as a **rescue** for a sparse-pool book that would otherwise be [[needs_manual]]. Deterministic because its inputs are structured eBay sold prices, and bounded by a non-circular trigger, a minimum ladder depth, a monotonic-ladder requirement, an [[Envelope Clamp]] on thin grade buckets, and a hard bid-factor cap.

The discount factor differs by price source — an eBay CGC *sold* basis is not an auction-house *realized* basis — so a factor calibrated to one source must not be applied to the other.
