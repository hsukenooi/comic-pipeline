---
name: comic:verify
description: Verify a working list of comics is fully linked end-to-end in the comics server's DB (bids → bid_fmvs → fmv → comics). Use after /comic:buy or /comic:snipe-add to confirm the pipeline didn't silently drop a write.
---

# Comic Verify

Walks the bid → bid_fmvs → fmv → comics chain for each comic in a working list and reports per-row gaps. Born from the PER-70 / PER-90 / PER-98 cascade where `/comic:buy` ran to apparent completion but left rows partially populated (missing comic, wrong year, FMV stub, junction never inserted, `bids.fmv_id` null).

This is a **warn-only** verification — it doesn't fix anything, just surfaces gaps so you (or future-you next session) can act.

## Pre-flight

Resolve and health-gate the server through the shared comics-server convention
(BUI-172, `docs/conventions/comics-server-call.md`):

```bash
source "$(git rev-parse --show-toplevel)/scripts/comics-server.sh"
comics_resolve_server || exit 1
comics_health_gate     || exit 1
```

If either fails, stop with: "Cannot verify — the comics server isn't reachable. Skipping verification step."

## Input

A working list. Each entry needs `item_id` (eBay ID) and ideally `grade`. `locg_id` is optional but tightens matching when present.

```json
{
  "items": [
    {"item_id": "123456789", "grade": 9.2, "locg_id": 6977652},
    {"item_id": "987654321", "grade": 9.4}
  ]
}
```

## Call

Route the POST through `comics_curl` so a non-200 (a 422 on a malformed
working list, a 500, or a server drop after the health check) **hard-fails and
surfaces the error body** instead of silently returning an empty string
(BUI-169):

```bash
comics_curl -X POST "$COMICS_SERVER_URL/api/comics/verify" \
  -H 'content-type: application/json' \
  -d @working_list.verify.json || {
    echo "Verification call failed — could not confirm linkage. Do NOT report all-clear." >&2
    exit 1
  }
```

**If the POST fails or returns no parseable JSON, STOP** with the message above —
never render a table or summary from a failed/empty response. This skill is the
final wrap step of `/comic:buy` and runs warn-only, so a silent empty response
would read as a false "nothing to flag" all-clear rather than "verification
failed."

## Output

The endpoint returns:

```json
{
  "summary": {"total": 3, "fully_linked": 2, "issues": 1},
  "results": [
    {"item_id": "...", "verdict": "fully_linked", "missing": [], ...},
    {"item_id": "...", "verdict": "fmv_stub", "missing": ["fmv.low", "fmv.high"], ...}
  ]
}
```

Verdicts (ladder — first failure wins):

| Verdict | Meaning |
|---|---|
| `fully_linked` | All five checks pass — comic, fmv (with low+high), junction, bids.fmv_id |
| `needs_manual` | Comic + fmv at grade exist but the fmv is **intentionally unpriceable** — flagged `needs_manual` (BUI-86) with a structured `flag_reason` (`one_sided` / `too_wide` / `too_sparse`). `fmv.low`/`fmv.high` are NULL *by design*; this is NOT a missing-FMV stub. Re-running `/comic:fmv` is a no-op — hand-price it. |
| `fmv_stub` | Comic + fmv at grade exist but `fmv.low`/`fmv.high` are NULL and the row is NOT flagged — `/comic:fmv` never computed FMV |
| `partial` | fmv populated but `bids.fmv_id` is null or mismatches the matched fmv |
| `no_fmv_at_grade` | Comic linked, but no `fmv` row at the bid's grade |
| `no_comic` | No comic linked to the bid (and no match via `locg_id` if given) |
| `no_bid` | The `bids` row itself is missing — snipe never landed |

## Presentation

Surface a table for the user. Use the verdict column to scan for issues:

```
| # | Item ID | Comic | Grade | Verdict | Missing |
|---|---|---|---|---|---|
| 1 | 123456789 | Amazing Spider-Man #300 | 9.2 | ✅ fully_linked | — |
| 2 | 987654321 | Spawn #9 | 9.4 | ⚠️ fmv_stub | fmv.low, fmv.high |
| 3 | 555555555 | Hulk #181 | 9.8 | ⚠️ no_fmv_at_grade | fmv row at grade 9.8 |
| 4 | 666666666 | (unknown) | 9.0 | ❌ no_bid | bids row |
```

If `summary.issues > 0`, after the table give the user one-line guidance per verdict:

- `needs_manual` → "This book is flagged `needs_manual` (reason: `<flag_reason>`) — its comp pool can't be auto-priced. Hand-price it via grade-curve interpolation or the CGC proxy (see `/comic:fmv` §7/§7a), or skip. Do NOT re-run `/comic:fmv` — it will just re-flag it."
- `fmv_stub` → "Run `/comic:fmv` for this comic at the missing grade(s)."
- `no_fmv_at_grade` → "The bid's grade doesn't have an FMV row yet. Run `/comic:fmv` at this grade."
- `no_comic` → "No comic linked. Run `POST /api/extract-comics` or re-run `/comic:snipe-add` with `--locg-id` set."
- `partial` → "Junction or `bids.fmv_id` is out of sync. Surface to user for manual reconciliation."
- `no_bid` → "Snipe never landed in the DB. Confirm `COMICS_SERVER_URL` was set during `/comic:snipe-add` and the snipe is on Gixen."

## When to invoke

- **End of `/comic:buy`** — final wrap step (Step 6) after `snipe-add`. Confirms the full pipeline took.
- **After ad-hoc backfills** — when reconciling history (PER-70-style cleanup), pass the patched item_ids in to confirm.
- **Sanity-check before `/comic:collection-add`** — if the FMV side is broken, the LOCG collection write is going to be confused too.

## Notes

- This skill does not write — it's read-only against the comics server's DB. Safe to run repeatedly.
- Lots (item_ids linked to multiple comics): pass one row per `(item_id, grade)` you want to confirm. The endpoint walks all `bid_fmvs` for the bid and matches by grade (and `locg_id` if given).
- `bids.fmv_id` mismatch with the matched fmv shows up as `partial` — this is the PER-90 footgun (denormalized pointer drifted from the canonical primary row).
- **LOCG collection verification** (did the comic land in LOCG with the right state?) is handled by step 7 of `/comic:collection-add` — it runs inline in the same Playwright session and checks `in_collection`, `wish_removed`, and `db_linked`. This skill covers the bid→fmv→comic DB chain only.
