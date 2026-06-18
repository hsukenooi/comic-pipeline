---
title: "First Real LOCG Collection Sync — Hard-Won Lessons (Wins-First, Exact Matching, Source-of-Truth)"
date: 2026-06-17
category: docs/solutions/integration-issues
module: locg-cli
problem_type: integration_issue
component: service_object
related_components:
  - locg-cli
  - gixen-overlay
  - comics
  - development_workflow
symptoms:
  - "LOCG Bulk Import reports 'Deleted from Collection' for owned books after pushing wish rows"
  - "26 owned X-Men issues deleted from the LOCG collection during the first real sync"
  - "LOCG Bulk Import hangs / times out when every row has a missing or wrong Release Date"
  - "rows land as 'Not Found' despite looking correct (blank cell, wrong date, wrong series-name variant)"
  - "re-import reconciliation hard-rejects on a volume mismatch because the store and the LOCG re-export disagree"
  - "scripts/comics-server.sh fails with 'status: read-only variable' when sourced from zsh"
root_cause: integration_issue
resolution_type: process_change
severity: critical
tags:
  - locg
  - bulk-import
  - wish-list
  - collection-sync
  - data-loss
  - in-collection
  - exact-matching
  - firecrawl
  - record-win
  - x-men
  - bui-122
  - bui-199
---

# First Real LOCG Collection Sync — Hard-Won Lessons

## Problem

The first real run of `/comic:collection-sync` against the production LOCG account
(BUI-122) was a multi-hour slog that deleted 26 owned X-Men issues, hung the LOCG
importer repeatedly, and burned many uploads on misdiagnosed failures. Almost none
of the pain was a code bug — it was the *operating procedure*: pushing wish rows
casually, sending incomplete/inexact data to a stateful importer, patching the CSV
instead of the source store, and a zsh-specific environment trap. This doc is the
runbook a future agent should read **before** the next sync so the same hours are
not re-spent. It is the field-experience companion to the empirical
`locg-bulk-import-recipe-2026-05-22.md` and the data-loss post-mortem
`locg-export-deletes-owned-wished-books.md`.

## Lessons

### 1. Sync collection WINS only first; never bulk-upload wish rows casually

The CSV carries two kinds of rows that behave **opposite** ways in LOCG's Bulk
Import:

- **Win rows** (`In Collection=1`): add-only. The importer can add a collection
  entry but *cannot delete* one from an `In Collection=1` row. These are **safe**.
- **Wish rows** (`In Collection=0, In Wish List=1`): an `In Collection=0` cell is
  an instruction to **un-collect** the matched book. The LOCG wish list contains
  books the user already owns, so pushing the wish list blindly tells LOCG to
  delete the owned copies.

In this session, "fixing" wish-row series names to match LOCG's canonical form
(`Uncanny X-Men` → `The X-Men`) made the previously-unmatched wish rows suddenly
*match* owned collection entries — and LOCG promptly **deleted 26 owned X-Men
issues** from the collection. The owned-safe export filter missed them precisely
because the owned-check compared one series-name variant while the wish row now
carried the other; the normalization that "fixed" the match also defeated the
guard. Recovery was a re-upload of the same issues with `In Collection=1`.

**Procedure:**

- Run the **wins sync as its own pass** (safe, add-only) and confirm it before
  touching wishes at all.
- Treat **wish-push as a separate, gated step** that runs only *after* the
  conflicts audit (`GET /api/comics/wish-list/conflicts`) is clean and conflicts
  have been removed (`POST /api/comics/wish-list/remove-conflicts`). See BUI-130.
- **Safety tripwire:** read the import response on **every** upload (starting with
  upload #1) and stop immediately if you see **"Deleted from Collection"**. That
  string means a wish row matched an owned book — abort and recover before
  uploading anything else.

### 2. LOCG Bulk Import has an implicit, all-or-nothing matching contract

A row matches its catalog entry only when **all** of these are present and exact:

- `Publisher Name` (LOCG canonical, e.g. `Marvel Comics`)
- `Series Name` — the exact LOCG canonical string (see lesson 3)
- `Full Title` — exact (e.g. `The X-Men #95`)
- `Release Date` — an **accurate** date

Failure modes seen this session:

- A **partial or blank** cell → `Not Found`.
- A **wrong** Release Date → `Not Found` (not "close enough" — the recipe's
  "LOCG silently corrects the date" tolerance applies to dates that are already
  *roughly right*, not to placeholders).
- An **all-dateless batch** → the importer **HANGS / times out**. We misdiagnosed
  this for many uploads as a row-count problem, then a CRLF/line-ending problem,
  then a column-count problem. **It was none of those — it was data completeness
  and exactness.** A batch where every row is missing its Release Date will spin
  the importer indefinitely.

**Rule:** every column must be filled with correct data before uploading. Do not
upload a batch to "see what sticks" — fill the data first.

### 3. LOCG's series naming is exact and idiosyncratic — reuse the user's own export strings

Series Name is not derivable algorithmically; LOCG's canonical forms are
inconsistent and occasionally split a run across two series. **Reuse the exact
series strings from the user's own last LOCG export** — the collection store's
`locg_export` rows are the authoritative source for these strings. Look the string
up there before inventing one.

The canonical trap is **X-Men**, which LOCG splits by issue number:

- Issues **#1–141** → series `The X-Men (Vol. 1) (1963 - 1981)`, with Full Title
  `The X-Men #95`.
- Issues **#142+** → series `Uncanny X-Men (Vol. 1) (1980 - 2011)`, with Full
  Title `Uncanny X-Men #142`.

So `The X-Men #95` matches but `Uncanny X-Men #95` does **not** — and getting this
backwards is exactly what triggered the lesson-1 deletion. When the Full Title's
series prefix must agree with the Series Name's split point, get both right
together.

### 4. Fill missing data with your own web research — use Firecrawl, not Metron or SERPAPI

Many wins/issues arrive missing the Release Date the importer requires. Filling
hundreds of those needs a research tool that scales:

- **Metron is impractical for bulk enrichment.** Its API rate limit (~20 req/min)
  plus heavy pagination makes enriching hundreds of issues take far too long, and
  it is what the `/comic:wishlist-add` lookup already strains against.
- **SERPAPI is being retired** — do not build new enrichment on it.
- **Use the Firecrawl skill/CLI** for web research going forward (the `firecrawl`
  family of skills, or the CLI). It is the standard for this repo's web lookups.
- **comics.org (GCD) and Marvel Fandom return HTTP 403** to plain automated
  fetches (raw `WebFetch`). Firecrawl handles these sites where bare fetch fails —
  reach for Firecrawl first on those domains, not `WebFetch`.
- **For regular monthly runs, avoid per-issue lookups entirely:** compute cover
  dates from **one verified anchor issue + the publishing cadence**. LOCG's
  convention is the **1st of the month**, and most runs ship monthly, so
  `anchor_date + n months (1st-of-month)` reproduces the dates without any network
  calls.

### 5. Fix data at the SOURCE (store + record-win), not just in the CSV

The most time-wasting anti-pattern of the session: editing the upload CSV to make a
batch import, while the **server store kept the old wrong values**. The CSV then
diverged from the store, and the next round-trip's re-import **reconciliation
hard-rejected** because the store and LOCG's fresh re-export disagreed on the
volume — `_reconcile_score` rejects on a volume mismatch. Patching the CSV is
treating the symptom; the store keeps re-emitting the bad row.

The upstream root cause is **`record-win` producing bad rows** (tracked as
BUI-199):

- **Decorated full_titles** — extra variant/decoration text in `full_title` that
  doesn't match LOCG's canonical Full Title.
- **Placeholder dates** — `YYYY-01-01` stand-ins instead of real Release Dates
  (which then trip lessons 2's `Not Found` / hang behavior).
- **Volume mislabeling** — collapsing all volumes of a series into one. Concrete
  example: a **1979 Iron Man** issue was tagged **"Vol. 8 (2026)"**, which both
  fails the LOCG match and later fails reconciliation on the volume.

**Rule:** when you find a bad row, fix it in the **store** (and fix `record-win` so
it stops generating the class of error) — then re-export. Never let the CSV and the
store drift apart, or the next sync's reconciliation will reject.

### 6. Environment gotcha: `scripts/comics-server.sh` and zsh's read-only `status`

`scripts/comics-server.sh` declares `local status`, but **`status` is a read-only
special variable in zsh** (it mirrors `$?`). Sourcing the script from a zsh shell
fails with `status: read-only variable`. **Wrap any sourcing of it in `bash -c`**
(e.g. `bash -c 'source scripts/comics-server.sh && ...'`) so it runs under bash,
where `status` is an ordinary name.

### 7. Use LOCG's import PREVIEW before committing

LOCG Bulk Import shows a **preview before the actual import begins** ("you will
be able to preview it before the actual import begins"). The preview surfaces
every **"Deleted from Collection" / "Added" / "Not Found"** outcome *before*
anything is written. In this session we skipped it and only saw the deletions
**after** they had already happened — reviewing the preview would have caught the
26-owned-X-Men deletion (lesson 1) with **zero damage**.

**Rule:** always review the preview and **abort on any unexpected "Deleted from
Collection."** It is the cheapest possible tripwire — strictly better than the
read-the-response check in lesson 1, because it fires before any write.

### 8. Probe with a tiny mixed batch before uploading the whole file

To *learn* LOCG's matching behavior, upload **~4 representative rows first** — one
owned book, one not-owned, one with a Release Date, one without — which isolates
every variable (date correctness, column completeness, series-name exactness) in
one or two uploads. This session instead re-uploaded the **full file ~12 times**
re-testing hypotheses (row count? line endings? column count?) — exactly the
misdiagnosis chain in lesson 2. A small mixed probe batch would have cracked the
matching contract immediately.

**Rule:** when the matching behavior is uncertain, probe with a tiny mixed batch;
don't re-upload the whole file to re-test a hypothesis.

### 9. Identify the minimal safe path to the goal first — here, wins-only

The sync's *actual* job this session was **40 add-only collection wins** — a
roughly two-upload task. The ~200 **wish rows** produced nearly all of the
difficulty and the only **data-loss incident** (lesson 1), for **zero benefit** to
that goal. The minimal safe path was wins-only.

**Rule:** before starting, name the minimal safe path to the goal and default to
it. **Default to syncing wins**; treat **wish-list pushing as a separate,
explicitly-opted-in, conflict-cleaned step** (lesson 1), not part of the baseline
sync.

## Why This Matters

LOCG's Bulk Import is a **stateful, exact-match, no-API** ingest with no preview and
no undo. Every property of a safe sync follows from that: wins-before-wishes
(because `In Collection=0` deletes), complete-and-exact rows (because partial/wrong
data either silently drops or hangs the importer), source-of-truth fixes (because
the store re-emits whatever you didn't fix upstream), and exact reuse of the user's
own series strings (because LOCG's canonical names aren't derivable). Skipping any
one of them is what turned the first sync into a multi-hour data-loss incident.

## Prevention / Checklist for the next sync

1. **Wins pass first**, confirmed, before any wish rows touch LOCG.
2. **Wish-push only after** the conflicts audit is clean and conflicts removed.
3. **Watch every import response for "Deleted from Collection"** — abort on sight.
4. **Fill every column with correct data** before uploading; never upload to probe.
5. **Reuse exact `Series Name` / `Full Title` strings** from the store's
   `locg_export`; mind the X-Men #141/#142 split.
6. **Enrich dates via Firecrawl** (not Metron/SERPAPI); for monthly runs compute
   from an anchor + 1st-of-month cadence.
7. **Fix bad rows in the store and in `record-win`**, then re-export — never patch
   only the CSV.
8. **Source `scripts/comics-server.sh` via `bash -c`** from a zsh shell.

## Related Issues

- `integration-issues/locg-export-deletes-owned-wished-books.md` — the code-level
  data-loss post-mortem and the three owned-safe fixes (export filter,
  `wishlist-add` ownership check, owned-safe reconciliation). This doc is the
  field-procedure companion.
- `integration-issues/locg-bulk-import-recipe-2026-05-22.md` — the empirical
  21-column CSV recipe (the matching contract in lesson 2 is the lived experience
  of those rules at scale).
- `packages/locg-cli/docs/processes/locg-collection-wishlist-sync.md` — the
  operational runbook for the safe round-trip via `/comic:collection-sync`.
- Linear: **BUI-122** (the first real sync, umbrella), **BUI-199** (`record-win`
  root cause: decorated full_titles, placeholder dates, volume mislabeling).
