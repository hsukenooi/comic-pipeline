---
title: "An endpoint's success report is not a write — verify against the store"
date: 2026-08-10
category: conventions
module: "plugins/gixen-overlay (POST /api/comics/backfill-year, db.upsert_comic PER-104 guard); any comics-server write endpoint"
problem_type: convention
component: tooling
severity: high
mechanized_by: test
enforced_by_test:
  - plugins/gixen-overlay/tests/test_gixen_overlay_db.py::test_upsert_comic_allcaps_skips_yearless_promotion_on_yeared_sibling_conflict
  - plugins/gixen-overlay/tests/test_gixen_overlay_db.py::test_upsert_comic_skip_reason_signals_per104_guard
  - plugins/gixen-overlay/tests/test_gixen_overlay_routes.py::test_backfill_year_guard_skip_reports_unresolved_not_resolved
  - plugins/gixen-overlay/tests/test_gixen_overlay_routes.py::test_backfill_year_offset_pages_past_unresolvable_head
applies_when:
  - "Running a bulk backfill or repair through a write endpoint and reporting how many rows changed"
  - "An endpoint returns a per-row result that a caller treats as proof of persistence"
  - "A write path has a guard, conflict check, or skip-and-warn branch that can legitimately decline the write"
  - "Opening a WAL-mode sqlite3 .backup file to diff a production run"
symptoms:
  - "resolved:true with a year that was never written — the row is still NULL"
  - "The same row reappears in a later scan after earlier rounds reported it fixed"
  - "mode=ro open of a .backup file fails — no -shm sidecar"
root_cause: logic_error
related_components:
  - "database"
tags:
  - write-verification
  - backfill
  - reported-vs-persisted
  - wal-backup
  - immutable-open
  - bui-715
  - bui-721
---

# An endpoint's success report is not a write — verify against the store

## Context

During the BUI-715 backfill run (2026-08-10), `POST /api/comics/backfill-year`
reported `resolved: true` with a concrete year for rows it had not written.
`upsert_comic` carries a PER-104 guard: promoting a yearless row when a yeared
sibling exists at a *different* year would create two yeared siblings, so it
logs a warning and returns the yearless row **unchanged**. The endpoint counts
any non-`None` resolution as resolved. Hulk Annual #1 (comic 328) was reported
`year: 2024` in rounds 2 and 4 while its `year` stayed `NULL` — and the guard
was right to refuse: the 1968 twin (row 312) is the real book, filed on Metron
as "Incredible Hulk Special", so Metron's 2024 answer was wrong for it. Correct
guard, honest data, lying report. The report defect was fixed in BUI-721 (see
"After," below); what generalizes is the verification practice that caught
it.

## Guidance

**Treat any success field computed before the write as a claim, not evidence.**
The response shape shows how the phantom hides:

```python
resolved_count += 1
entry = {..., "resolved": True, "year": resolution.year}   # routes.py — set BEFORE the write
if not dry_run:
    new_comic_id = upsert_comic(db, ..., year=resolution.year, ...)
    entry["comic_id_after"] = new_comic_id
    entry["merged"] = new_comic_id != row["id"]
```

`resolved` and `year` come from the *resolution*, never re-read from the row.
On the guard path `upsert_comic` returns the yearless row's own id — so
`merged: false` and `comic_id_after == comic_id`, byte-identical to a
successful in-place write. Nothing in the response distinguishes them.

Three detections, cheapest first:

1. **Watch for the same key reappearing in a later scan.** The backfill scan is
   `WHERE year IS NULL ORDER BY id LIMIT ?`; a row reported resolved that shows
   up again next round was never written. Free, and catches the class
   immediately.
2. **Diff the live DB against a pre-run backup row by row, attributing every
   change to a named writer.** Unattributed change *or* missing expected change
   is the finding. Take the backup with `sqlite3 .backup` — a WAL database
   copied with `cp` is stale.
3. **Grep the server log for the guard's own warning:**
   `"skipping yearless promotion — yeared sibling conflict"` fires on exactly
   the silent path.

### Opening the backup you just took

A `.backup` file of a WAL database is itself WAL-mode with no `-shm` sidecar,
so a `mode=ro` open fails (`unable to open database file`, on the first query,
not on connect). Use `immutable=1`, and **only** for backups — it promises the
file will not change; on a live DB it serves stale pages:

```sh
sqlite3 "file:$HOME/.comics-server/db.sqlite.backup-2026-08-10?immutable=1" \
  "SELECT id, title, issue, year FROM comics WHERE id IN (312, 328);"
```

(The live DB keeps `mode=ro`: the server writes it.)

### Operational notes from the same run

Because the backfill scan had no offset, unresolved rows piled up at the head
and a fixed-limit batch re-scanned the same stalled prefix forever — escalating
limits (40 → 65 → 110 → 162) cleared it at ~2.3× Metron spend. Per-round retry
self-healed transient 429 failures (Captain America #110 failed three rounds,
then resolved), so a round's failures are not terminal. BUI-721 fixed both:
`upsert_comic` now takes an optional `skip_reason` out-param the PER-104 guard
populates (so a caller can tell a guard-refused no-op from a real write
without re-reading the row itself), and the endpoint takes an `offset` plus
returns `next_offset`, so a repeated small-limit run pages past an
unresolvable head instead of re-scanning it — see `POST
/api/comics/backfill-year`'s docstring in `routes.py` for the exact paging
protocol (a naive `round_number * limit` offset is wrong, because successful
writes shrink the population but failures/skips do not).

## Why This Matters

A silent no-op write is worse than a failed one. A failure is loud and gets
retried; a phantom success is recorded as done, closes the ticket, and leaves
the store quietly wrong. Here it burned Metron budget re-resolving the same
rows across four rounds while reporting progress, and would have closed the
backfill with rows still `NULL`. The same shape — return the intent, not the
outcome — is how guarded writes, conflict-refused upserts, and swallowed
constraint violations all disappear. These rows feed identity resolution,
which feeds the FMV linkage chain, so a phantom "resolved" is not cosmetic.

## When to Apply

- After any bulk/backfill endpoint run, before reporting it complete.
- Whenever a write path contains a guard, a conflict check, or a
  skip-and-warn branch — assume the caller's success field does not know
  about it.
- Any remediation where you plan to report counts: verify them against the
  store, not the response body.
- When a run reports progress across rounds but the remaining count is not
  falling as fast as the reported resolutions.

## Examples

**Before:** Round 2 returns `{"comic_id": 328, "resolved": true, "year": 2024,
"comic_id_after": 328, "merged": false}`. Round 4 returns the identical entry.
Both read as successful in-place year writes.

**After:** `SELECT id, title, year FROM comics WHERE id = 328` returns
`year = NULL` — twice. The server log carries `upsert_comic: skipping yearless
promotion — yeared sibling conflict (... incoming_year=2024 ...)`, and row 312
holds the same title/issue at `year = 1968`. The guard was protecting the
correct book from a wrong Metron answer while the endpoint reported the wrong
year as written. Fixed in BUI-721: the same request now returns
`{"comic_id": 328, "resolved": false, "skipped": "yeared_sibling_conflict",
"year": 2024}` — `resolved: false` and no `comic_id_after`/`merged`, so the
response itself no longer claims a write that never happened.

## Related

- `docs/solutions/conventions/timed-out-write-is-indeterminate-reconcile-first.md` —
  the mirror direction (BUI-697): a write reported FAILED that actually landed.
  Together: a status field is a claim about the transport, never a fact about
  the store — in either direction, READ the store.
- `docs/solutions/workflow-issues/verification-whose-failure-is-indistinguishable-from-success.md` —
  the general class of checks whose passing looks like their breaking.
- `docs/solutions/database-issues/stub-fmv-null-after-extract-comics-2026-05-23.md` —
  the narrow precedent: use GET to verify actual state after a POST.
- `docs/solutions/best-practices/a-probe-of-a-write-endpoint-is-a-write.md` —
  deploy-probe adjacency.
- Tickets: BUI-715 (the run), BUI-721 (the report defect, fixed), BUI-697 (the
  mirror), BUI-593 (fetch succeeded / write 422d — the family's first member).
