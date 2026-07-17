# Wish-List Cover-Year Backfill (BUI-387)

One-time pass to stamp a per-issue **Cover Year** onto the existing
cross-volume decoy holds so they stop re-flagging in every conflicts audit.

## Why

A wish-list entry is name-keyed and (before BUI-387) carried no year, so the
conflicts audit was year-blind: a vintage grail wish (e.g. `The X-Men #1`, 1963)
matched **any** owned volume of that issue number — including an owned modern
volume (the 1991 Jim Lee run) — and reported a false "conflict". The
2026-07-17 cleanup left **33 permanent decoys** (ASM #1/#3/#28–30, Avengers #1,
FF #4/#18, Incredible Hulk #1, Mighty Thor #1, The X-Men #1–#17/#41, plus a few
specific-cover X-Men wishes) that recur every audit. See the
`wishlist-conflict-holds` memory note for the full list and eras.

BUI-387 lets a wish carry an optional per-issue `year` (Cover Year). A stamped
wish only conflicts with the matching-volume owned copy, clearing the decoys
**structurally**. New adds are stamped automatically by `/comic:wishlist-add`
(and the `POST /api/comics/wish-list` endpoint). This doc covers the **one-time
backfill of the pre-existing holds**.

## The BUI-129 hard rule

**Stamp the issue's own Cover Year, never the series' start year
(`year_began`).** Feeding a series start year into the per-issue gate is the
exact bug that hid 16 owned X-Men (BUI-129). If you cannot get a specific
issue's cover year, **leave that wish UNSTAMPED** — it keeps today's safe,
year-blind behavior (it can over-flag a decoy, never miss an owned book). Do not
guess.

The `locg wish-list set-year` command sanity-checks only that the value is a
4-digit year (so a `1963 - 2011` range paste is rejected). It **cannot** tell a
cover year from a start year — that judgment is yours, made per issue against
Metron.

## Prerequisite: reinstall `locg` on the Mac Mini (BUI-365)

`wish-list set-year` is a **new subcommand**. The Mac Mini's `locg` is a frozen
`uv tool install` copy, so it will reject `set-year` with an argparse "invalid
choice" error until reinstalled. Before Step 3, on the Mac Mini:

```bash
cd <repo> && uv tool install --force ./packages/locg-cli
```

(Same post-merge convention documented in `scripts/install.sh` / the root
CLAUDE.md.) No server restart is needed for the store write itself (below), but
the CLI binary must be current to have the subcommand.

## Procedure (run on the Mac Mini, against the server store)

The wish-list lives in the server-owned store, and the comics server re-reads
`wish-list.json` from disk on every request — so a CLI write to that file is
picked up immediately, no server restart needed.

### 1. List the current holds

From any machine, get the decoy names and the owned volume each matched (its
`series_name` carries the volume years — that's your era cross-check):

```bash
curl -sf "$COMICS_SERVER_URL/api/comics/wish-list/conflicts" | \
  jq -r '.conflicts[] | "\(.name)\t→ owned as \(.series_name) (\(.release_date))"'
```

### 2. Resolve each held issue's Cover Year on Metron

For each held wish, look up **that specific issue** on Metron and read its
`cover_date` (never the series `year_began`). The `/comic:wishlist-add` skill's
Step 2 shows the exact `metron_paginate .../api/issue/?series_id=<id>` call that
yields per-issue `cover_date`. Take the 4-digit year of the issue's cover date.

Cross-check against the era: the wish is a decoy precisely because its intended
Cover Year is OLDER than the owned volume's `series_name` years from step 1
(e.g. wish `The X-Men #1` → intended 1963 vs owned `X-Men (1991 - ...)`). If the
intended year matches the owned volume's era, it is a **genuine** conflict, not
a decoy — do not stamp it to dodge a real owned book.

### 3. Stamp each hold on the Mac Mini

SSH to the Mac Mini and point `locg` at the server store (same store the server
uses). Run one `set-year` per held wish with its resolved Cover Year:

```bash
# On the Mac Mini:
export LOCG_DATA_DIR=~/.gixen-server/collection-store
locg wish-list set-year "The X-Men #1" 1963
locg wish-list set-year "Fantastic Four #18" 1963
locg wish-list set-year "Incredible Hulk #1" 1962
# … one per held decoy, using THAT issue's cover year
```

Each call prints `{"status": "ok", "name": ..., "year": ..., "matched": 1}`.
`{"error": "... not found ..."}` means the name doesn't match an entry verbatim
(copy it exactly from step 1). Re-running with the same year is idempotent.

### 4. Verify the decoys cleared

```bash
curl -sf "$COMICS_SERVER_URL/api/comics/wish-list/conflicts" | jq '.conflicts | length'
```

The stamped holds drop out of `conflicts`. Any that remain either weren't
stamped, were stamped with a year that still lands in the owned volume's era
(a genuine conflict — review it), or are a different (still year-blind) hold.

## Scripted variant (optional)

If you have resolved every hold's Cover Year into a `name → year` map, drive the
same primitive in a loop rather than by hand. Keep the map in a file you
reviewed — the map IS the BUI-129 decision, so it must be verified per issue
before it runs:

```bash
# holds.tsv: one "<exact wish name>\t<cover_year>" per line, reviewed by hand.
export LOCG_DATA_DIR=~/.gixen-server/collection-store
while IFS=$'\t' read -r name year; do
  locg wish-list set-year "$name" "$year"
done < holds.tsv
```

There is deliberately **no auto-migration**: nothing can derive a per-issue
Cover Year for an arbitrary wish name without a Metron lookup and a human era
cross-check, and a wrong guess reintroduces the BUI-129 data-loss class. The
stamping primitive persists exactly what you give it; the correctness lives in
the year you resolved.

## Durability of the stamps

The stamped `year` is a **local-only annotation**. It is durable across a
`locg collection import` (BUI-208: import no longer touches `wish-list.json`),
so a normal sync round-trip preserves it. But it is **not** written to the LOCG
bulk-import CSV and LOCG has no wish cover-year field — so the ONE way to lose
every stamp is a **manual re-seed** of the server `wish-list.json` from a fresh
LOCG-derived export (e.g. re-running the one-time seed
`cp data/locg/wish-list.json ~/.gixen-server/collection-store/`). A LOCG-derived
wish-list carries no year to restore, so that overwrites the stamps with
year-blind entries. If you ever re-seed, re-run this backfill afterward.
