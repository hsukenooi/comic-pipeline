# BUI-600 phase 1 — analysis and plan

> **Status (2026-08-02): the write has NOT been run.** Analysis is complete and
> the plan is validated end to end against a scratchpad copy, but remediation is
> deferred to an explicit user gate. Numbers below were measured on 2026-08-02
> against `~/.comics-server/db.sqlite` (674 comics / 803 fmv / 613 bid_fmvs /
> 641 bids, post-BUI-596-remediation); re-run the dry run before applying, since
> the table moves.

Read-only analysis of the 12 `comics` rows that store an eBay listing title as
row identity **without repeating the issue number** — the class BUI-596's rule
deliberately excluded. **Nothing was written to the live DB.** Verified after the
fact: the live DB still reads 674 / 803 / 613 / 641, unchanged.

## Files

| file | what it is |
| -- | -- |
| `adjudication.md` | **Read this first.** Occupies BUI-596's `rule.md` slot, named differently because the finding is that there is no rule. Per-row adjudication, the measured classifier table, the two new safety gates, and residual risk. |
| `rows.tsv` | All 12 rows: id, title, issue, year, variant, action, proposed title, collision verdict, twin, source bid + eBay title, fmv/link/ref counts, and the reason. Regenerate with `plan.py --tsv`. |
| `plan.py` | The adjudicated list and the plan as code. Single source of truth — `remediate.py` imports it, so the reviewed plan and the executed plan cannot drift. Read-only; run it directly to print the stats. |
| `remediate.py` | The remediation script. **Dry run by default**, writes only with `--apply`. |

## Headline numbers

- **12** rows adjudicated by hand. Proposed: **delete 8, rewrite 4**.
- **This is a hardcoded id list, not a rule.** BUI-596's rule was *generative* —
  it derived the corrected title by splitting on `#<issue>`. These rows have no
  `#`, so nothing can be derived; every title came from the source eBay listing.
  Two rewrites are not derivable from the stored string at all (328 needs the
  word *Incredible*, which the row does not contain).
- A vocabulary rule was still measured: **age-marker-only flags exactly the 12,
  0 false positives, 0 misses.** `DC Comics Presents` (ids 55, 56) is flagged
  **only** by the publisher-token variant — dropping that token avoids the
  ticket's named risk entirely. The rule is nonetheless wired in as a **drift
  tripwire that selects nothing**; the plan's confidence comes from the hand
  adjudication, not from the score.
- **BUI-596's central safety fact does not hold here: `bid_links` is NOT 0.**
  4 of 12 rows (201, 204, 315, 328) carry a `bid_fmvs` link *and* a `bids.fmv_id`
  reference — one of them for a **WON** auction. Those four are the rewrites.
- **`bids.fmv_id` has no foreign key**, so a cascade delete would leave it
  dangling silently. New preflight gate + new post-write assertion.
- **0 index collisions, 0 end-state unique-index violations.**
- 2 `fmv` rows cascade away, both pure empty shells.

## The command to run

Dry run first — it writes nothing and opens no transaction:

```sh
python3 docs/plans/bui-600-listing-titled-comic-rows/remediate.py
```

Then, to apply:

```sh
python3 docs/plans/bui-600-listing-titled-comic-rows/remediate.py --apply
```

`--apply` performs the BUI-514 ritual in order, and **refuses to write if the
backup step fails**:

1. `sqlite3 .backup` to `~/.comics-server/backups/db.sqlite.bui-600.<ts>.bak`,
   then verifies it opens and passes `integrity_check` (never `cp` — the DB is
   WAL-mode and a `cp` captures a stale snapshot);
2. rebuilds the plan **from the backup**, so a concurrent writer cannot shift
   the plan between planning and applying;
3. preflight gates (all must pass, else abort):
   - every one of the 12 rows still matches the exact
     `(title, issue, year, variant)` fingerprint a human adjudicated,
   - the vocabulary tripwire returns exactly those 12 ids — no new
     listing-titled row has appeared, none has vanished,
   - every delete target has **0** `bid_fmvs` links, **0** `bids.fmv_id`
     references, and **0** `fmv` rows carrying any price data,
   - 0 rewrite collisions and 0 simulated unique-index violations;
4. applies inside **one** transaction, every statement keyed on `comics.id` and
   asserting `rowcount == 1` (the BUI-500 lesson);
5. diffs the live DB against the backup, proving only the intended rows and
   fields changed, that `bid_fmvs`/`bids` are byte-identical, and that **no
   `bids.fmv_id` or `bids.comic_id` was left dangling**, plus a row-count check.

Expected output on success: `comics 674 -> 666`, `fmv 803 -> 801`, `bid_fmvs`
(613) and `bids` (641) unchanged.

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
DB (taken with `sqlite3 .backup`), never the live DB itself. Result:
674 → 666 comics, 803 → 801 fmv, `bid_fmvs` (613) and `bids` (641) untouched,
`integrity_check` ok, `foreign_key_check` clean, 0 orphaned `fmv` or `bid_fmvs`
rows, 0 dangling `bids.fmv_id`, and **0 rows of the adjudicated class remaining**.

Separately, the four rewrites were pushed through the **real** plugin function
`_merge_yearless_into_yeared()` on that copy, to prove the shape they leave
behind is safe. All four bid links survived and three landed on strictly better
FMV data — notably bid 168 (WON) moved from an empty shell to `40–60 / comps=4`.
The test copies and their backups were then deleted; the live DB was re-checked
at 674 / 803 / 613 / 641.

**Every preflight gate was also proven to fire**, by mutating a throwaway copy
and confirming the script aborts with `rc=4` having written nothing:

| injected fault | gate that caught it |
| -- | -- |
| row 340 renamed out from under the plan | fingerprint drift **and** tripwire `missing=[340]` |
| a new `'Sub-Mariner Bronze age Key VF'` row inserted | tripwire `extra=[839]` — re-adjudicate |
| a delete target given a `bid_fmvs` link + `bids.fmv_id` ref | both link gates |
| a delete target's `fmv` given `low`/`high` | price-data gate |
| twin 172 made yearless, so 201's rewrite collides | end-state unique-index simulation |

A gate that has never been observed to fire is not known to work; these were.

### Post-write verification (independent of the script)

`remediate.py` verifies itself, but these read-only queries confirm the outcome
from outside it. Run after `--apply`, before restarting the server:

```sql
-- 1. expected shape
SELECT (SELECT COUNT(*) FROM comics)   AS comics,    -- want 666
       (SELECT COUNT(*) FROM fmv)      AS fmv,       -- want 801
       (SELECT COUNT(*) FROM bid_fmvs) AS bid_fmvs,  -- want 613
       (SELECT COUNT(*) FROM bids)     AS bids;      -- want 641

-- 2. the adjudicated class is gone; the 4 rewrites landed
SELECT id, title, issue, year FROM comics WHERE id IN (201,204,315,328);
--   201|Avengers|85|            204|Avengers|81|
--   315|Avengers|52|            328|Incredible Hulk Annual|1|
SELECT COUNT(*) FROM comics WHERE id IN (202,203,314,316,339,340,341,342);  -- want 0

-- 3. nothing was orphaned (the FK-less columns are the risk)
SELECT COUNT(*) FROM bids b WHERE b.fmv_id IS NOT NULL
   AND NOT EXISTS (SELECT 1 FROM fmv f WHERE f.id = b.fmv_id);              -- want 0
SELECT COUNT(*) FROM bids b WHERE b.comic_id IS NOT NULL
   AND NOT EXISTS (SELECT 1 FROM comics c WHERE c.id = b.comic_id);         -- want 0
SELECT COUNT(*) FROM fmv f
   WHERE NOT EXISTS (SELECT 1 FROM comics c WHERE c.id = f.comic_id);       -- want 0

-- 4. the four bid links that made these rewrites mandatory still resolve
SELECT b.id, b.status, f.id, f.comic_id, f.grade
  FROM bids b JOIN fmv f ON f.id = b.fmv_id
 WHERE b.id IN (152,155,168,244);   -- want 4 rows; 168 is the WON one

PRAGMA integrity_check;    -- ok
PRAGMA foreign_key_check;  -- empty
```

## Optional follow-up: converging the four rewrites

The write leaves each rewritten row beside its yeared twin as a yearless orphan
(17 such pairs total, up from 13). They are inert, not index violations, and are
the documented input to the existing sweep:

```sh
comics-api POST '/api/sweep-orphans?dry_run=true'    # preview
comics-api POST '/api/sweep-orphans?dry_run=false'   # merge
```

**This plan does not run it, and it is not a no-op beyond this ticket:** the
sweep would also merge the **13 pre-existing pairs** left by BUI-596 (mostly
`Amazing Spider-man` and `X-men` case-variants). That is a separate decision.
Preview with `dry_run=true` first.

## Known gaps this plan does not close

- **Two more malformed rows are knowingly left behind: ids 330 and 335**
  (`Thor` #126/1966 and #149/1968). Same root cause, a third signature — the
  title was *partially* normalized, leaving mangled grade fragments, and they are
  yeared rather than yearless. Both would be clean deletes, but neither was
  adjudicated by this ticket and a rewrite would hard-collide with their twin.
  They are recorded as `OUT_OF_SCOPE` in `plan.py` and printed by every dry run.
  **Recommend a follow-up ticket.**
- **The write boundary is still open.** BUI-599 covers the writer that produces
  these shapes. Remediating without closing it means the class recurs — the same
  caveat BUI-596 recorded for its own classes B/C/D.
