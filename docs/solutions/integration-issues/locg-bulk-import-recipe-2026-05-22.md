---
title: LOCG Bulk Import CSV Recipe (empirically validated)
date: 2026-05-22
category: docs/solutions/integration-issues
module: locg-cli
problem_type: integration_issue
severity: medium
related_components:
  - locg-cli
  - gixen-overlay
  - development_workflow
tags:
  - locg
  - bulk-import
  - csv
  - league-of-comic-geeks
  - collection-cache
  - metron
  - cross-repo
  - api-deprecation
---

# LOCG Bulk Import CSV Recipe (empirically validated)

The recipe `locg collection export` uses to produce a CSV that LOCG's **Bulk
Import** page ingests cleanly. Validated 2026-05-22 by uploading test CSVs to LOCG
and inspecting the resulting Excel re-exports. Promoted from the
`locg-collection-cache` brainstorm Appendix (BUI-24) into a permanent reference;
this is the contract `collection_io.generate_csv` (locg-cli) implements (R17,
R21–R31).

## Problem

LOCG has no write API; the only path to push the local collection cache up is the
**manual CSV upload** on LOCG's Bulk Import page. A CSV that is "obviously
correct" still fails or silently corrupts data: rows land as `Not Found`, or
matched-existing rows get `My Rating=5.0` and `Marked Read=1` stamped on them. The
rules below are non-obvious and were established empirically, not from docs.

## The recipe

For a CSV row to bulk-import cleanly against LOCG:

1. **All 21 columns present, in LOCG's export header order.** Omitting columns
   triggers LOCG's "auto-enrich" path, which stamps unwanted defaults
   (`My Rating=5.0`, `Marked Read=1`) on matched-existing rows. (R21)
2. **`Publisher Name`** = LOCG canonical convention (`Marvel Comics`, `DC Comics`,
   `Image Comics`, …). Bare Metron names need a small mapping table (Metron
   `Marvel` → LOCG `Marvel Comics`). (R22)
3. **`Series Name`** = exact LOCG canonical form, which is **inconsistent across
   series** — some carry `(Vol. N)` + year range (`Fantastic Four (Vol. 1) (1961 -
   1996)`), some don't (`Spawn (1992 - Present)`). It must be **looked up in the
   cache** (learned from prior exports), not derived algorithmically. Net-new
   series need a one-time learn. (R23)
4. **`Full Title`** = LOCG's canonical pattern: `{series_short_name} #{issue}` for
   canonical issues (keeps "The", drops Vol/year); for variants the **exact**
   variant string (e.g. `Spawn #313 Cover C Greg Capullo Variant`). A bare Full
   Title **variant-spreads** — it matches both the canonical and the user's
   pre-existing variant row. (R24)
5. **`Release Date`** present (Metron `store_date`, fallback `cover_date`). It is
   **required for net-new matches**; LOCG silently overrides it with its own
   canonical value, so "close enough" suffices. (R25)
6. **`In Collection=1`, `In Wish List=0`, `Marked Read=0`** set explicitly. (R26)
7. **`My Rating` explicitly present-but-blank.** *The single most important
   non-obvious rule:* omitting the column makes LOCG default `My Rating=5.0` AND
   flip `Marked Read=1` on matched-existing rows. Present-but-blank keeps the
   values you set. (R27)
8. **`Media Format = "Print"`, `Purchase Store = "eBay"`, `Signature=0`,
   `Slabbing=0`.** (R28, R31)
9. **`Price Paid`** from the win's `current_bid` (float, currency stripped);
   **`Date Purchased`** from the auction-end timestamp (`end_date_iso`), fallback
   today. (R29, R30)
10. **All other columns blank** (Condition, Notes, Tags, Storage Box, Owner,
    Grading, Grading Company). LOCG handles blanks correctly — it does not wipe
    pre-existing values on matched-existing rows, and defaults blanks for new
    rows. (R31)

## Appendix: test methodology and evidence

Six bulk-import tests run against LOCG between 2026-05-22 10:30–11:50 (UTC+8),
each isolating one or two variables. Results are from LOCG's `Bulk Import -
Success` screen and the subsequent Excel re-exports.

- **Test 1 — Probe defaults.** 4 rows, minimal columns, no `My Rating`. 3 rows
  `Not Found`; ASM #151 matched (it was pre-existing — the importer fuzzy-matches
  against prior library state). *Comics not already in the library need more
  disambiguation.*
- **Test 2 — Punctuation.** Added a comma to `Doctor Strange, Sorcerer Supreme`.
  Still 3 `Not Found`. *Punctuation alone wasn't the issue.*
- **Test 3 — Exact canonical Series Name.** Used exact canonical names from a
  prior export. Still 3 `Not Found`. *Exact Series Name necessary but not
  sufficient.*
- **Test 4 — Add Release Date.** Same 3 rows + Metron `cover_date`. All 3 matched.
  *Release Date is the missing variable for net-new matches.* Also: bare
  `Spawn #313` matched BOTH the canonical and the user's pre-existing
  `Spawn #313 Cover C Greg Capullo Variant` — **bare Full Title variant-spreads.**
- **Test 5 — All 21 fields explicit, isolate Marked Read.** 2 fresh comics, all
  columns incl. explicit blank `My Rating`. Both matched cleanly. *`Marked Read=0`
  sticks when `My Rating` is present-but-blank* (omitting it defaulted Marked
  Read to 1 in tests 1 & 4). LOCG silently corrected Release Date to its canonical
  value (FF #86 sent `1969-05-01`, stored `1969-02-11`). Adding to collection
  auto-removed FF #86 from the wish list. Bulk import is **non-destructive** of
  fields the CSV doesn't set.
- **Test 6 — Scale + variant patterns.** 28 rows, all 21 fields, exact known
  variant Full Titles. 28/28 matched, all `Marked Read=0`; the two Spawn #299 rows
  (explicit Virgin Variant vs bare canonical) resolved to two distinct LOCG
  entries with no spread. *The recipe scales and eliminates variant-spread when
  the exact variant Full Title is supplied* — confirming the value of the cache
  learning exact variant strings from prior exports.

## Related: deprecated plugin route

The pre-cache linkage path — `POST /api/bids/{item_id}/comics/locg` in
gixen-overlay — is **deprecated** by this local-first model (BUI-24, BUI-25). It
stays functional for legacy snipes (it preserves existing values) but the
canonical flow is now `locg collection record-win` → `locg collection export` →
manual Bulk Import → `locg collection import`. v2 may remove the route.
