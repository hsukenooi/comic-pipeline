# BUI-596 phase 1 — analysis and classification

> **Status (2026-08-01): the write has NOT been run.** Phase 1 (analysis) is
> complete and the plan below is validated, but remediation is deliberately
> deferred until the class B/C/D **write boundary** is closed — see "Before this
> is worth doing" at the bottom. Cleaning the table while the writer still
> produces these shapes makes this a one-off that recurs. Numbers below were
> measured on 2026-08-01; re-run the dry run before applying, since the table
> moves.

Read-only analysis of the 173 malformed `comics.title` rows left behind by
BUI-591's writer fix. **Nothing was written to the live DB.** Verified after the
fact: `~/.comics-server/db.sqlite` still reads 808 comics / 173 `#`-titled / 945
fmv, unchanged.

## Files

| file | what it is |
| -- | -- |
| `rule.md` | **Read this first.** The tightened rule, the class breakdown, the collision analysis, and the premise corrections. |
| `rows.tsv` | All 173 rows: id, title, issue, year, variant, class, action, proposed new title, collision verdict, fmv counts, bid links. |
| `plan.py` | The rule and the plan as code. Single source of truth — `remediate.py` imports it, so the reviewed plan and the executed plan cannot drift. Read-only; run it directly to print the stats. |
| `remediate.py` | The remediation script. **Dry run by default**, writes only with `--apply`. |

Supporting scripts kept for audit (`dump.py`, `analyze.py`, `collide.py`,
`fmvcmp.py`, `exceptions.py`, `build_rows.py`, `emit.py`) — all read-only, all
superseded by `plan.py`. `emit.py` regenerates `rows.tsv` from `plan.py`.

## Headline numbers

- **173** rows match the tightened rule — **exactly the same set** as the
  `#`-in-title heuristic. Zero legitimate rows to carve out.
- **4 classes**, not the ticket's 2: 99 doubled-issue-only, **60** full listing
  titles (ticket estimated 29), 8 variant designations, 6 multi-issue lots.
- **`bid_links = 0` across all 173 — confirmed**, not taken on faith.
- **78 (45%) would hard-collide** on a unique index if rewritten in place.
- **181 of 183** attached `fmv` rows are empty shells. The ticket's reason to
  prefer rewrite over delete does not hold.
- Proposed: **delete 134, rewrite 39**, merge 0.

## The command to run

Dry run first — it writes nothing and opens no transaction:

```sh
python3 docs/plans/bui-596-malformed-comic-titles/remediate.py
```

Then, to apply:

```sh
python3 docs/plans/bui-596-malformed-comic-titles/remediate.py --apply
```

`--apply` performs the BUI-514 ritual in order, and **refuses to write if the
backup step fails**:

1. `sqlite3 .backup` to `~/.comics-server/backups/db.sqlite.bui-596.<ts>.bak`,
   then verifies it opens and passes `integrity_check` (never `cp` — the DB is
   WAL-mode and a `cp` captures a stale snapshot);
2. rebuilds the plan **from the backup**, so a concurrent writer cannot shift
   the plan between planning and applying;
3. preflight gates: `bid_links` must be 0, the end-state simulation must show 0
   unique-index violations, and no row scheduled for delete may carry real FMV;
4. applies inside **one** transaction, every statement keyed on `comics.id` and
   asserting `rowcount == 1` (the BUI-500 lesson);
5. diffs the live DB against the backup, proving only the intended rows and
   fields changed, plus a row-count check.

Expected output on success: `comics 808 -> 674`, `fmv 945 -> 803`, `bid_fmvs`
and `bids` unchanged.

### Before running

The comics server writes to this DB. Stop it first so the transaction is not
racing a live writer:

```sh
launchctl bootout gui/$(id -u)/com.comics.server
# ... run remediate.py --apply ...
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.comics.server.plist
```

### Rollback

The script prints this if verification fails. The backup path is in its output:

```sh
launchctl bootout gui/$(id -u)/com.comics.server
cp <backup> ~/.comics-server/db.sqlite && rm -f ~/.comics-server/db.sqlite-wal ~/.comics-server/db.sqlite-shm
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.comics.server.plist
```

## Validation already performed

The full `--apply` path was exercised against a **scratchpad copy** of the live
DB (taken with `sqlite3 .backup`), never the live DB itself. Result: 808 → 674
comics, 945 → 803 fmv, `bid_fmvs`/`bids` untouched, `integrity_check` ok,
`foreign_key_check` clean, 0 orphaned fmv or bid_fmvs rows, and **0 malformed
rows remaining**. The test copy and its backups were then deleted.

One real bug was found and fixed during that run, worth knowing about
independently: **a database just produced by `sqlite3 .backup` cannot be opened
with `mode=ro`**. It is in WAL mode with no `-shm` sidecar yet, and read-only
mode forbids SQLite from creating one. The failure surfaces as a bare
`unable to open database file` on the first *query*, not on connect. Any future
backup→verify ritual will hit this; `plan.connect_read()` handles it.

## Before this is worth doing

BUI-591 closed the **doubled-issue-number** class at the write boundary. It did
**not** close classes B, C or D — 74 of the 173 rows. Remediating now without
closing that boundary means the same shape returns on the next `comic-fmv` run
against a listing-titled book. Recommend deciding on the writer-side follow-up
first, or accepting that this cleanup is a one-off that will need repeating.
