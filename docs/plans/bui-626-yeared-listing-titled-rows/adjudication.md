# BUI-626 — the yeared listing-title signature

> **Status (2026-08-03): APPLIED.** Backup `~/.comics-server/db.sqlite.pre-bui626-20260803`
> (`sqlite3 .backup`, `integrity_check` ok). Live DB went 666 → 662 comics,
> 801 → 797 fmv; `bids` and `bid_fmvs` untouched. Verified by an independent
> diff against the backup, not only by the script's own before/after snapshot.

The third and final signature of the listing-titled-`comics.title` class that
BUI-596 and BUI-600 each closed one signature of.

| signature | ticket | shape | why it needed its own pass |
| -- | -- | -- | -- |
| 1 | BUI-596 | repeats `#<issue>` in the title | generative rule: split on the `#<issue>` token |
| 2 | BUI-600 | listing title, **yearless** | no `#` to split on; a rewrite could sit beside the yeared twin as an orphan and be collapsed later |
| 3 | **BUI-626** | listing title, **already yeared** | a rewrite would **hard-collide** on `idx_comics_tiyv`, so every row here is a delete-or-nothing decision |

## The ticket named 2 rows. There were 4.

BUI-626 named ids 330 and 335. A scan of the live table for the signature —
a partially-normalized title carrying leaked grade notation (`FA/ .5`, `.0`) or
a cover blurb, with a non-NULL `year` — found **332** and **334** carrying the
identical shape. Both were adjudicated here rather than left for a fourth
cleanup ticket, since the ticket's own acceptance criterion is *"0 listing-titled
rows remain under any of the three known signatures."*

## Per-row adjudication

All four resolved to **delete**. None was a candidate for rewrite: each has a
clean `Thor` twin at the same `(issue, year)`, so a rewrite would collide on
`idx_comics_tiyv`, and each twin already holds the real FMV data — so there is
nothing to preserve by rewriting.

| id | stored title | issue/year | twin | own fmv | twin fmv |
| -- | -- | -- | -- | -- | -- |
| 330 | `Thor FA/ .5 1st issue Hercules` | 126/1966 | 295 `Thor` | 356 — 2.0, all NULL | 317 — 1.5 / 45–60 / comps 2 |
| 332 | `Thor .5 Maddening Menace of Super-Beast! Jack Kirby Art` | 135/1966 | 297 `Thor` | 358 — 6.5, all NULL | 319 — 6.5 / 30–50 / comps 2 |
| 334 | `Thor .0 Scourge Super Skrull! Jack Kirby` | 142/1967 | 299 `Thor` | 360 — 7.0, all NULL | 322 — 7.0 / 10–10 / comps 1, 417 — 4.5 / 15–15 / comps 5 |
| 335 | `Thor .5 2nd Wrecker! Origin Black Bolt! Inhumans` | 149/1968 | 301 `Thor` | 361 — 6.5, all NULL | 324 — 6.5 / 10–10 / comps 3, 399 — 6.0 / 10–15 / comps 4 |

Every one of the four `fmv` rows that cascaded away was a **pure empty shell** —
`low`, `high`, `comps` and `confidence` all NULL. Nothing carrying any price
signal was destroyed.

## The bid-link check, which BUI-600 proved you cannot skip

BUI-596 assumed `bid_links == 0` for its rows. BUI-600 **disproved** that
assumption for its own — 4 of its 12 rows carried live links, one for a WON
auction. That assumption does not transfer, so it was re-derived here from
scratch, and queried **directly rather than through a JOIN**:

- `bid_fmvs` rows whose `fmv_id` is one of the four shells — **0**
- `bids.fmv_id` pointing at one of the four shells — **0**
- `bids.comic_id` pointing at one of the four rows — **0**

The direct query matters because **`bids.fmv_id` is a bare `INTEGER` with no
foreign key**. `PRAGMA foreign_key_check` returns clean on a dangling
`bids.fmv_id`, so a cascade delete can strand one invisibly. `remediate.py`
therefore asserts `dangling == 0` explicitly after the write.

## Provenance: where these titles came from, and why the delete is safe

Each row's source eBay listing still has its `bids` row — and in every case that
bid's `fmv_id` **already points at the twin's real fmv**, not at the shell:

| row | source bid | status | `bids.fmv_id` | points at |
| -- | -- | -- | -- | -- |
| 330 | 254 | REMOVED | 317 | twin 295 |
| 332 | 256 | **WON** | 319 | twin 297 |
| 334 | 259 | LOST | 322 | twin 299 |
| 335 | 261 | LOST | 324 | twin 301 |

Row 332's source bid is a **WON** auction — exactly the case BUI-600's warning
was about. It comes out clean here because the link already sits on the twin, so
deleting the shell severs nothing.

## Gates

`remediate.py` is dry-run by default and refuses to write unless all pass:

- **fingerprint** — the exact `(title, issue, year, variant)` read during
  adjudication must still match, so the plan cannot act on a row that drifted
- **twin** — the twin must exist and still match `(issue, year)`
- **price-signal** — no cascading `fmv` row may carry `low`, `high` or `comps`
- **bid-link** — `bid_fmvs`, `bids.fmv_id` and `bids.comic_id` must all be 0
- **post-write diff** — only the planned `comics` rows and their cascaded `fmv`
  rows changed; `bid_fmvs` and `bids` byte-identical
- **post-write** — `dangling bids.fmv_id == 0`, `integrity_check`,
  `foreign_key_check`

## A note on reading the diff against a live server

The independent backup-vs-live diff showed **29 `bids` rows changed**, which is
*not* fallout from this remediation — a `DELETE FROM comics` cannot write `bids`,
and `bid_fmvs` was byte-identical. The comics server is running, and its sync
loop wrote those rows in the ~12 minutes between the backup and the diff.

Running that down surfaced a genuine, unrelated bug, filed as **BUI-636**:
`update_bid_status` re-stamps `resolved_at` unconditionally, so 27 of those 29
rows kept the same status and had their resolution timestamp overwritten for
nothing. When diffing against a backup of a **live** database, expect
sync-window drift and classify it before trusting or dismissing it.

## Acceptance

Re-scanned after the write — **0 rows remain** under any of the three
signatures (`#`-repeating, age-marker/yearless, yeared listing junk).

## Left open, deliberately

16 yearless orphans still share `(title, issue, variant)` with a yeared twin
(BUI-600's 4 plus BUI-596's residual). `POST /api/sweep-orphans?dry_run=false`
would collapse them losslessly, but that is a separate decision affecting rows
this ticket never adjudicated, and it was explicitly kept out of scope.
