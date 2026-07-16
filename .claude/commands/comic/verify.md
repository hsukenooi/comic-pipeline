---
name: comic:verify
description: Verify a working list of comics is fully linked end-to-end in the comics server's DB (bids → bid_fmvs → fmv → comics). Use after /comic:buy or /comic:snipe-add to confirm the pipeline didn't silently drop a write.
---

# Comic Verify

Walks the bid → bid_fmvs → fmv → comics chain for each comic in a working list and reports per-row gaps. Born from the PER-70 / PER-90 / PER-98 cascade where `/comic:buy` ran to apparent completion but left rows partially populated (missing comic, wrong year, FMV stub, junction never inserted, `bids.fmv_id` null).

This is a **warn-only** verification — it doesn't fix anything, just surfaces gaps so you (or future-you next session) can act.

## How to read this file (BUI-361)

This skill is split into two sections:

- **EXECUTOR CONTRACT** — everything the agent performing the verification call
  must do (pre-flight, the call, hard-fail semantics, presentation),
  self-contained like collection-check.md's — the executor reads the whole
  file and follows the contract. It has one declared cross-reference: its
  Presentation step reads the verdict ladder from ORCHESTRATOR NOTES § Verdict
  ladder instead of restating it (kept as a single copy on purpose so the two
  can't drift apart). Because the executor reads the whole file, not just this
  section, that reference resolves within the same read — it isn't a promise
  of isolation the file then breaks. This is the section a dispatched executor
  — or a standalone `/comic:verify` run — executes.
- **ORCHESTRATOR NOTES** — the verdict ladder, per-verdict guidance, and
  when-to-invoke. **`/comic:buy` Step 6 reads only this section**: BUI-360 folded
  the verify call into `gixen add-batch --verify`, so in the buy flow there is
  no verify executor — the orchestrator just interprets the embedded verdicts
  with the ladder below.

**Standalone invocation**: you are both roles — execute the EXECUTOR CONTRACT,
then interpret and present the results with the ORCHESTRATOR NOTES.

---

## EXECUTOR CONTRACT

### Pre-flight

Resolve and health-gate the server through the shared comics-server convention
(BUI-172, `docs/conventions/comics-server-call.md`):

```bash
source "$(git rev-parse --show-toplevel)/scripts/comics-server.sh"
comics_resolve_server || exit 1
comics_health_gate     || exit 1
```

If either fails, stop with: "Cannot verify — the comics server isn't reachable. Skipping verification step."

> **Editor note — fenced blocks don't share shell state (BUI-375):** each
> fenced bash block below runs in its own fresh shell — a freshly-spawned
> executor invokes them as separate Bash tool calls, so `$COMICS_SERVER_URL`
> and the sourced `comics_*` functions from Pre-flight do **not** carry
> forward. The Call block below re-sources `comics-server.sh` and re-runs
> `comics_resolve_server` at its own top — keep that pattern on any block you
> add. This is the exact BUI-352 trap: an un-resourced block curls an empty
> host, and a swallowing fallback can turn that into a silent false all-clear.

### Input

A working list. Each entry needs `item_id` (eBay ID) and ideally `grade`. `locg_id` is optional but tightens matching when present.

```json
{
  "items": [
    {"item_id": "123456789", "grade": 9.2, "locg_id": 6977652},
    {"item_id": "987654321", "grade": 9.4}
  ]
}
```

Lots (item_ids linked to multiple comics): pass one row per `(item_id, grade)` you want to confirm. The endpoint walks all `bid_fmvs` for the bid and matches by grade (and `locg_id` if given).

### Call

Route the POST through `comics_curl` so a non-200 (a 422 on a malformed
working list, a 500, or a server drop after the health check) **hard-fails and
surfaces the error body** instead of silently returning an empty string
(BUI-169):

```bash
source "$(git rev-parse --show-toplevel)/scripts/comics-server.sh"
comics_resolve_server || exit 1
comics_curl -X POST "$COMICS_SERVER_URL/api/comics/verify" \
  -H 'content-type: application/json' \
  -d @working_list.verify.json || {
    echo "Verification call failed — could not confirm linkage. Do NOT report all-clear." >&2
    exit 1
  }
```

**If the POST fails or returns no parseable JSON, STOP** with the message above —
never render a table or summary from a failed/empty response. This skill runs
warn-only, so a silent empty response would read as a false "nothing to flag"
all-clear rather than "verification failed."

This skill does not write — it's read-only against the comics server's DB. Safe to run repeatedly.

### Output

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

Each result's `verdict` is one of the ladder values defined in ORCHESTRATOR
NOTES § Verdict ladder (the single copy — don't restate it here).

### Presentation

Surface a table for the user. Use the verdict column to scan for issues:

```
| # | Item ID | Comic | Grade | Verdict | Missing |
|---|---|---|---|---|---|
| 1 | 123456789 | Amazing Spider-Man #300 | 9.2 | ✅ fully_linked | — |
| 2 | 987654321 | Spawn #9 | 9.4 | ⚠️ fmv_stub | fmv.low, fmv.high |
| 3 | 555555555 | Hulk #181 | 9.8 | ⚠️ no_fmv_at_grade | fmv row at grade 9.8 |
| 4 | 666666666 | (unknown) | 9.0 | ❌ no_bid | bids row |
```

If `summary.issues > 0`, after the table give the user one-line guidance per
verdict from ORCHESTRATOR NOTES § Per-verdict guidance — as the executing agent
you have the full file in context; the ladder is shelved there because
`/comic:buy` Step 6 interprets `add-batch --verify` verdicts without dispatching
an executor.

---

## ORCHESTRATOR NOTES

### Verdict ladder

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

`bids.fmv_id` mismatch with the matched fmv shows up as `partial` — this is the PER-90 footgun (denormalized pointer drifted from the canonical primary row).

### Per-verdict guidance

One line per non-`fully_linked` verdict:

- `needs_manual` → "This book is flagged `needs_manual` (reason: `<flag_reason>`) — its comp pool can't be auto-priced. Hand-price it via grade-curve interpolation or the CGC proxy (see `/comic:fmv` §7/§7a), or skip. Do NOT re-run `/comic:fmv` — it will just re-flag it."
- `fmv_stub` → "Run `/comic:fmv` for this comic at the missing grade(s)."
- `no_fmv_at_grade` → "The bid's grade doesn't have an FMV row yet. Run `/comic:fmv` at this grade."
- `no_comic` → "No comic linked. Run `POST /api/extract-comics` or re-run `/comic:snipe-add` with `--locg-id` set."
- `partial` → "Junction or `bids.fmv_id` is out of sync. Surface to user for manual reconciliation."
- `no_bid` → "Snipe never landed in the DB. Confirm `COMICS_SERVER_URL` was set during `/comic:snipe-add` and the snipe is on Gixen."

### Never report a false all-clear

If the verification call failed — the executor STOPPED per its contract, or (in
the `/comic:buy` flow) `add-batch`'s top-level `verify_error` is non-null — do
**not** report an all-clear for verification. Say linkage could not be confirmed
for the affected rows and point at a manual `/comic:verify` follow-up. A failed
call is "verification failed", never "nothing to flag" (BUI-169).

### When to invoke

- **End of `/comic:buy`** — Step 6. Since BUI-360 the verify call itself rides
  along with Step 5 (`gixen add-batch --verify`); Step 6 interprets each row's
  embedded `verify` verdict using the ladder + guidance above. No executor
  dispatch, no second call.
- **After ad-hoc backfills** — when reconciling history (PER-70-style cleanup), pass the patched item_ids in to confirm.
- **Sanity-check before `/comic:collection-add`** — if the FMV side is broken, the LOCG collection write is going to be confused too.

### Scope

- Warn-only — surface gaps, don't block, don't fix.
- **LOCG collection verification** (did the comic land in LOCG with the right state?) is handled by step 7 of `/comic:collection-add` — it runs inline in the same Playwright session and checks `in_collection`, `wish_removed`, and `db_linked`. This skill covers the bid→fmv→comic DB chain only.
