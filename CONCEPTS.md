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

Win-sourced entries carry a publisher when the issue could be resolved (record-win captures it from the issue's full detail record), but the publisher is null on a lookup miss and the release date is often a best-guess — which is why reconciling them against a LOCG export must still tolerate a missing publisher and match on year rather than exact date.

### Placeholder Date
A sentinel release date stamped on a [[Win-Sourced Entry]] when the record-win lookup cannot establish the issue's real date — conventionally the first of January of the identify year. It marks the entry as *dated-unknown*, a distinct state from a genuinely dateless entry.

Detected by **intent, not shape**: an entry is a placeholder only when it is win-sourced and carries no resolved issue identity, never by the date's calendar value alone — so a real January-first cover date is never mistaken for one. The export blanks it to an empty cell, so a placeholder and a truly dateless entry reach LOCG identically (this is *how* a book with no [[Era Evidence]] is "left undated" downstream despite carrying a sentinel internally). It **must not be deleted**: the year it carries is the only discriminator reconcile has between two undecorated volumes of the same [[Masthead]], so removing it makes a dateless win fail open into a wrong-volume match and be silently healed away. A missing date is loud; a wrong one is silent.

### Import-Sourced Entry
A Collection entry that originated from — or has round-tripped through — a LOCG export. *Known in code and tickets as:* `locg_export`. The counterpart to a Win-Sourced Entry.

A [[Collection Sync]] converts a [[Win-Sourced Entry]] into one of these on reconcile, so the win-sourced population drains toward empty as syncs run and every entry converges on import-sourced. Any check whose logic depends on win-sourced entries still existing quietly loses its power as that happens — see [[Duplicate Identity]].

### Identity Key
The set of fields that decides whether an incoming import updates an existing Collection entry or inserts a new one: publisher, series name, full title, and release date, compared as stored.

Every component is a provider-supplied string rather than a stable id, so ordinary upstream relabelling changes the key and makes an import insert a second entry for a book already held — a volume's end year closing when a series stops being current (which fires for every ongoing series the January after it ends), or a provider switching from cover date to on-sale date. The rename detector meant to catch this holds three of those same fields steady to spot a changed title, which leaves it blind in exactly the cases where one of the three is what drifted.

The opposite state also occurs and is worse: several entries holding the **identical** key, so an import has no way to choose which one it is updating. Collapsing such a group is a merge, and the survivor must be chosen by which entry the provider most recently confirmed — never by insertion order, which reliably picks a stale entry and can discard the purchase record that makes the book count as owned at all.

### Duplicate Identity
Two or more Collection entries that are the same book held once — distinct from a genuine second copy, which is a [[Copy Count]] of 2 on a single entry. Recognized as a shared title with release dates compatible within the reconciler's tolerance; the date agreement is what separates a real duplicate from two [[Masthead]] volumes that spell an issue identically, and from a base printing legitimately held alongside its later [[Printing]].

A count of these is only meaningful while entries of both sources are present. The check compares a [[Win-Sourced Entry]] against an [[Import-Sourced Entry]], so once a [[Collection Sync]] has converted every win-sourced entry it can no longer form a pair — and a reported zero then means "none findable" rather than "none present."

Two entries differing only by publisher are **not** a duplicate by default — see [[Licensed Edition]].

The count is a **blocking** check: a [[Collection Sync]] asserts it is zero before proceeding, on the reasoning that an unresolved duplicate would otherwise be pushed onward. That makes what the check admits a deliberate decision rather than a matter of accuracy — a duplicate class with **no local remedy** must not be folded into it, because a blocking check over unfixable data halts the pipeline indefinitely and teaches its operator to bypass it. Such classes are reported on a separate advisory count that gates nothing. Before widening any blocking check, ask whether anyone can act on what it will newly catch.

### Licensed Edition
A foreign-market publication of a book already published domestically, carrying the same series and title but its own publisher and its own, later release date. It is a genuinely different edition, not a relabelling of the domestic one — which makes it the standing counterexample to reading a publisher difference as provider drift.

The release-date gap is the discriminator and it is substantial and systematic: the foreign edition trails by months, consistently in one direction across a publisher's whole run. So a tolerant check that pairs entries by requiring an *identical* release date can never see these at all, and one that tolerates the gap risks merging two books that are legitimately held separately. When a licensed edition appears in the Collection unbidden, the cause is normally [[Record-Win]] resolving a win onto it instead of the domestic edition; such an entry carries the purchase fingerprint (a price and purchase date) that a genuinely-foreign holding lacks. Because the provider holds the ownership and re-emits it, deleting such an entry locally does not stick — and clearing it deliberately runs through the ownership-flag path that a [[Collection Sync]] reads as a removal instruction.

**The population is self-amplifying, which is why the standing entry count understates the risk.** An import-sourced licensed edition is not inert: it joins the pool of volumes a later [[Record-Win]] chooses from, and because era matching prefers the volume whose publication window most tightly contains the win's year, a narrow foreign volume can beat the long-running domestic one outright. Every subsequent win on that [[Masthead]] in the foreign volume's era is then filed onto the foreign edition too. Reading the resolution against the win's own publisher — a signal independent of the candidate pool — is what breaks the loop; it must fail open and act only on a demonstrated publisher conflict, or it will perturb resolutions that were already correct.

### Quarantine
A mark on a Collection entry that hides it from every ownership and wish matcher without removing it — the disposition for an entry that is real data the provider will keep re-emitting, but that must stop answering *"do I own this?"*. The standing case is a [[Licensed Edition]] filed against a domestic purchase.

What makes it safe is the layer it deliberately leaves alone: a quarantined entry stays fully present in the owned-safe export, because it is still a book you own and dropping it from the export's owned view is the same instruction to the provider as a removal. Quarantine narrows what an entry **matches**, never what it **asserts about ownership**. It is refused when it would hide the last owned copy of a book — the buy path would then read not-owned and re-buy it — and that refusal is overridable only with a separately recorded reason, stored on the entry, saying why hiding the last copy was safe. Every mark names who applied it, why, and under which ticket, because a quarantine nobody can attribute is one nobody can safely lift. It is reversible, survives a re-import untouched, and when two entries merge it carries onto the survivor rather than being silently lifted.

Quarantining is a **disposition, not a suppression**: a count it drives to zero must still find its cases when the mark is ignored, or the number is only measuring that the detector stopped looking — the same trap [[Duplicate Identity]] describes from the other direction.

### Copy Count
How many copies of an issue the Collection holds — **a count, not an ownership flag**. *Known in code as:* `in_collection`, where `0` means tracked-but-not-owned, `1` is the common case, and `2+` is a genuine duplicate (a second copy, or a condition upgrade held alongside the original). Treating it as a boolean is a recurring trap in both directions: reading it as truthy makes a text-formatted `"0"` from a LOCG export mean *owned* (`bool("0")` is `True`, BUI-469), while writing it as a flag silently discards a second copy when two entries are merged (BUI-470). Any read must coerce to `int` and compare, and any merge of two genuinely distinct copies must **increment** the survivor rather than drop the loser.

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

### Era Evidence
Whatever independently establishes *which era* — and therefore which volume — a book belongs to, so a lookup result can be accepted or rejected. Usually the listing's Cover Year; when that is missing, the volume publication window carried by the matched LOCG canonical series name (`"The X-Men (Vol. 1) (1963 - 1981)"`) can stand in.

The defining requirement is **independence**: the evidence must not derive from the same lookup hit it is being used to judge. A range taken from the hit's own series decoration would gate the candidate against itself and always pass — a *tautological guard*, which has the shape of a check and validates nothing (BUI-464). With no independent evidence at all there is no era guard, which is why a book whose era cannot be established is left undated rather than dated by guess: a wrong date is silent, a missing one is loud.

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

The export is **owned-safe**: it never instructs LOCG to un-collect a book you own. LOCG's bulk import treats an `In Collection=0` row as "remove from collection," so the export pushes only genuinely-new wishes you do not already own. The re-import is reconciliation-based: it matches a pending Win-Sourced Entry to its LOCG counterpart even when LOCG has canonicalized the publisher or release date, and will not reconcile one onto an entry that already exists.

That reconciliation covers **pending** entries only, so it is not a general guarantee against a [[Duplicate Identity]]. An entry that has already reconciled is matched on its [[Identity Key]] alone thereafter — so when LOCG later relabels the series or shifts the release date, the re-import inserts a second entry instead of updating the first, and the duplicate it creates is between two Import-Sourced Entries, where the pending-entry check cannot see it. As of BUI-208 the up-CSV is **wins-only by default** — the code refuses to emit any `In Collection=0` row unless an explicit owned-safe wish push is requested (a machine-enforced gate, on top of the human-reviewed LOCG import preview). There is **no row-count limit** on uploads; the importer hangs only on incomplete/dateless rows (the old "≤20 rows" advice was a misdiagnosis).

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

### Pre-Trade Check
A policy check run at order entry — any write that sets or raises a snipe's max bid (a new add, a re-add upsert, an edit, a batch row) — before the Gixen write commits. It challenges out-of-policy commitments (a bid over FMV, a stale FMV, an unpriced entry, an ungrouped duplicate comic, aggregate pending exposure past a ceiling) but is advisory-first: v1 never blocks, and even a check later flipped to blocking honors an explicit, ledger-recorded bypass. Scoped to order entry only — outcome classification and the [[Phantom WON]] inference are never gated. (BUI-609.)

### Decisions Ledger
An append-only record, one row per accepted order-entry write, of what the system knew when money committed: the FMV inputs consulted (or their absence), the computed bid cap and the rule that set it, every [[Pre-Trade Check]] advisory raised, and any bypass. Retro disputes become row lookups instead of transcript archaeology. A ledger write failure never blocks the snipe — the audit trail must not become a new way to miss an auction. Distinct from [[Group-Win Evidence]], which records outcomes; this records commitments. (BUI-609.)

## FMV & Pricing

### Money Path
The chain of computation whose output the system will act on financially — a bid cap, max bid, or FMV band that real money follows. Guards on the money path are asymmetric by design: they may only ever move a price **down**, never up (a too-high cap overpays with real money; a too-low cap only misses an auction). A statistic feeding the money path must be outlier-robust before it is trusted — a median resists a single outlier only from three samples up; below that it is the sample itself or the plain mean. Diagnostic-only statistics (the [[Calibration Report]]'s metrics) are outside the money path and may deliberately trade robustness for coverage.

### First-Party Comp
A sold-price comp sourced from **your own** resolved eBay auctions (`bids.winning_bid`), merged into the FMV comp pool alongside external eBay sold comps (BUI-286). Because a proxy-auction win's price is only ever *at or below* your max, a wins-only set is **truncated from above** and biases FMV down — so first-party comps are always pulled as wins **and** losses together, and a book whose in-window set is wins-only is dropped rather than merged (see the deflation-guard learning in `docs/solutions/best-practices/`).

### Comps Ledger
The durable record of individual sold comps (BUI-610), keyed on `comics.id` and carrying each comp's price, sold date, parsed grade, provider, the exact query and tier that surfaced it, and when the underlying response was fetched. It exists because `fmv` stores only a band and a comp *count*, under a `UNIQUE(comic_id, grade)` upsert that destroys its predecessor — so the comps a price was built from were discarded the moment the price was written. The ledger is written by the pricing path and read by nothing in it: it is an archive, never an input to a band. It holds exactly what the pipeline treated as a comp — same parsers, same hard-excludes — so a ledger row and a pool row are the same object; everything that judgment drops survives one tier down in the [[Tier-0 Capture]]. Identity is recorded or absent, never inferred: a comp whose book cannot be resolved unambiguously keeps a NULL `comic_id` and stays market data. Rows are frozen on insert under [[First-Observation-Wins]].

### Tier-0 Capture
The append-only file of **verbatim** provider responses written at fetch time (BUI-614), before any parsing, filtering, or identity resolution. It is deliberately dumber than the [[Comps Ledger]] and deliberately separate from the response cache, which is digest-keyed and TTL-evicted and therefore overwrites and expires. Tier 0 rotates by size into compressed segments and is **never pruned** — the whole premise is that historical sold data cannot be re-acquired, so the only safe deletion policy is none. A capture failure is logged and swallowed: it shadows the fetch, it must never fail it.

Capture happens **before** the response is validated, so Tier 0 also holds bodies the live path **refused** — each record carries the verdict that was reached on it. This is the sharpest form of "dumber than the ledger": the ledger holds only what the pipeline accepted as a comp, while Tier 0 holds what arrived, including what was thrown away. Anything reading Tier 0 back must therefore honor that verdict rather than treat every record as a comp source — importing a refused body would manufacture rows the pipeline had already judged unusable, and under [[First-Observation-Wins]] those rows would win.

### First-Observation-Wins
The rule that a sold listing is **immutable**: once a comp has been recorded in the [[Comps Ledger]], a later observation of the same listing never overwrites its price, sold date, or observation time. Only bookkeeping updates — last-seen, seen count, and a count of how often a later observation *disagreed*. Also written as the keep-the-first-answer rule, or KTD4 after the decision that established it.

The consequence that catches people: because a re-import or backfill can only ever **add**, never revise, the order in which observations are presented is a **correctness** property rather than a performance detail — whichever observation arrives first is the one that survives forever. So an ordering must come from the timestamp each observation carries, never from a proxy for it such as the name or modification time of the file the observation happens to sit in. When a later observation does disagree, the disagreement is counted but the stored side is kept: the count reveals *that* two sources conflicted, never *which* one was right.

### FMV History
The append-only record of every value written to an `fmv` row — band, comp count, confidence, flag reason, notes, and when. Appended at `POST /api/comics`, the single choke point every FMV writer passes through (comic-fmv, its CGC-proxy re-upsert, a hand edit), so no writer can bypass it. It appends on every write, including one that changed nothing: "we re-measured and the market did not move" is itself a fact, and a conditional append needs a comparison that can be wrong. A history append never blocks the upsert it records.

### Calibration Report
A **diagnostic-only** audit (BUI-288, `/comic:calibration-report`, `GET /api/comics/calibration`) that ranks issues whose FMV is set too low, so you know which books to re-price. It never bids, snipes, or writes FMV. **Headlined by confirmed win-based exceedance** (`contested_win_margin > 1`, exact and uncensored — admits a row on its own, even with zero losses), with **Overshoot** kept as a labeled, censored secondary signal (BUI-532/BUI-543). It never keys on raw win/loss rate — losing is the *intended* outcome of the 80% bid haircut, so a high loss count is not a mispricing signal by itself. Every row is self-describing via `win_backed`/`loss_backed` booleans.

### Overshoot
The Calibration Report's **secondary** ranking metric: `median(winning_bid / fmv_high)` over a book's **losing** auctions. Persistently `> 1` means the market keeps clearing above your stated fair-value ceiling, i.e. FMV is too low — but it's a censored, confounded upper bound (a LOST auction's recorded price is a floor, and a moving `fmv_high` can inflate it), so confirmed win-based exceedance (`contested_win_margin`) headlines instead when present. A minimum loss count (`min_losses`) gates single-loss noise out of the *loss-based* signal specifically — it does not gate whether a row appears in the report at all, since a qualifying win-based margin admits a row independently of loss count (BUI-543).

### Grade-Curve Interpolation
Estimating an FMV for a comic at a grade with no direct sold comps by reading a price off the curve implied by comps at neighbouring grades. It is a **fallback only when the target grade's bucket is empty** — never used when real comps exist at the target grade — requires a minimum number of supporting comps, and its output is marked as interpolated at **low confidence** (including through cache reuse) so it is never conflated with a direct-comp price (see the over-bid-guards learning in `docs/solutions/best-practices/`).

### Envelope Clamp
An upper bound applied when a price is read from a comp bucket too thin to be outlier-robust: the direct bucket value is capped at the price its trustworthy neighboring grades imply, taking the lower of the two. It never rejects the thin bucket outright — a genuinely sparse key still gets priced — and it can only ever lower a price, never raise one ([[Money Path]] asymmetry). When no trustworthy neighbors exist to form the bound, the direct value is used unchecked; that residual case is the irreducible sparse-key exposure.

### Ungraded Anchor
The median of a book's **raw/ungraded** sold prices, held alongside the priced band as an independent reference for what the bulk of actual raw trades cleared at. Its job is comparison, not pricing: the band is computed from the grade-targeted pool, and the anchor asks whether that pool was quietly pricing a *different market* — typically a set of high-grade slabbed copies standing in for a raw book.

The anchor is a check, never an input. It never re-prices a book and a caller must never re-derive the comp pool to "resolve" a divergence between them. It is skipped when built on too few raw sales, on the same reasoning as every other thin-pool guard: an anchor from a handful of sales is noise, and a noisy reference produces false alarms rather than caught errors. Because both the band and the anchor snap to a coarse rounding grid, a divergence smaller than that grid measures the rounding rather than the market — which is why the divergence test carries an absolute floor as well as a proportional one.

### Advisory Mark
A signal attached to an FMV row that informs the reader **without withholding the price** — surfaced as a token in the row's notes rather than as a flag reason. The contrast with [[needs_manual]] is mechanical, not stylistic: a flag reason empties the band and max bid, so routing an advisory signal through the flag slot would **suppress the price on every book it fires on**. A mark that is merely worth knowing must never be delivered as a flag.

Advisory marks are held to a precision bar rather than a recall bar. An existing mark that fires only on true problems is more useful than a broader one that cries wolf, so a proposed addition — or a widening of an existing mark's threshold — has to justify itself on the *new* rows it would catch, and dilutes the mark if those turn out to be false. Repeated attempts to derive a new mark from the *shape* of a comp pool (its spread, its depth, its internal price ladder) have failed this bar; the defects that survive measurement have instead been individual bad comps that reach the price. A signal too imprecise to be a mark can still be a **report** — a diagnostic run on demand, outside the row entirely, which is where a low-precision-but-useful observation belongs.

### needs_manual
The FMV verdict emitted when even the fallbacks can't defensibly price a book (raw sold comps too thin, target grade's bucket empty and interpolation unsupported). It is a deliberate **punt to a human/LLM**, not a failure — the book gets hand-priced with judgment inside the `/comic:fmv` skill rather than auto-bid on a shaky estimate. Automating away a `needs_manual` on a high-value key removes the human check exactly where a mistake costs the most.

The verdict is a **withheld** price, not a missing one: the band and max bid are deliberately empty, so the row is not a stub to be re-run but a decision to be made. Which is why it is distinct from a [[Fetch Error]] (nothing looked), from a genuine zero (nothing exists), and from an [[Advisory Mark]] (priced, with a caveat). Each verdict carries a structured **flag reason** naming *why* the pool couldn't be trusted — the pool was one-sided about the target grade, too wide across grades, too sparse to survive trimming, or measured a different edition than the one being priced (see [[Variant Drop]]).

### Variant Drop
The case where a book's **variant descriptor** — a collector/catalog term like "White Logo 1st Print" — appears in no actual listing title, so every query carrying it returns nothing and the pool can only be found by dropping it. What comes back then prices the **base cover**, not the variant.

That is a defensible floor for most variants and a wrong anchor for a scarce few, which is a judgment the pipeline must not make silently. So a variant drop is recorded as its own [[needs_manual]] flag reason rather than being priced through: the book reaches a human with the comps attached and the bid cap withheld. Distinctive among the flag reasons in that it does not clear by re-running — the query was impossible, not unlucky — it clears by fixing or removing the input variant.

### Fetch Error
A comp lookup that **failed** rather than one that found nothing — the sold-comps provider errored or was rate-limited, or the query crashed before it ever ran. *Known in code and in the results table as:* `fetch-err`.

The distinction carries weight because a fetch error and a genuinely illiquid book are **shape-identical**: both surface as zero comps. Reading the first as the second reports that a book has no market when in truth nothing ever looked — a [[Money Path]] error in the expensive direction, since it invites bidding blind or dismissing a book as worthless. A fetch error is a loud failure to be retried, never a price, and never a reason to call a book illiquid.

What makes it treacherous is that the *evidence trail* is the only thing separating the two, so what an **empty** trail defaults to is load-bearing. A crash before any query runs leaves no trail at all, and a classifier reading "nothing was attempted" as "nothing was found" emits a confident zero for a book it never priced — invisible to every downstream guard, because the resulting row looks clean rather than broken. Before trusting a zero, ask what an absent evidence trail defaults to.

### CGC Proxy
Pricing a book off graded-slab (CGC/CBCS) prices instead of raw sold comps, discounted to a raw-equivalent. Two distinct forms exist, and they must not be conflated:

- **§7a Heritage-prose proxy** — the `docs/conventions/fmv-math-spec.md` §7a step reading realized graded prices from Google/Heritage/GoCollect **prose**. It is **human/LLM-gated by design and deliberately not automated**: its inputs are unstructured (no extractable sold-price field), its value-based trigger is circular (no value estimate exists precisely when comps are too thin to price), and a mis-read number would be an unbounded over-bid in the bid-cap path. A future ask to automate *this* form should stop here (see the not-safely-automatable learning in `docs/solutions/best-practices/`; BUI-326 Won't Do).
- **eBay-slab proxy tier** — the automated form: a second graded-only eBay-sold pass builds a slab grade→price ladder, and a raw price is read off it at a conservative discount, emitted at capped (low) confidence and only as a **rescue** for a sparse-pool book that would otherwise be [[needs_manual]]. Deterministic because its inputs are structured eBay sold prices, and bounded by a non-circular trigger, a minimum ladder depth, a monotonic-ladder requirement, an [[Envelope Clamp]] on thin grade buckets, and a hard bid-factor cap.

The discount factor differs by price source — an eBay CGC *sold* basis is not an auction-house *realized* basis — so a factor calibrated to one source must not be applied to the other.
