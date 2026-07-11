# Scheduling recurring `/comic:wishlist-sellers` runs

Reference material for `.claude/commands/comic/wishlist-sellers.md` — moved out
of the skill body (BUI-282) because it's setup-time reference, not something a
runtime invocation needs to read.

`/comic:wishlist-sellers` is designed to run on a **recurring schedule** —
daily or weekly — and notify you when new multi-match sellers are found, or
when a run is partial (exit 3 — some candidates were never verified; see the
exit-code table below). A clean empty result (exit 0, no sellers) is silent.

## Why re-runs are cheap

Three layers make steady-state runs near-free:

1. **7-day eBay search cache** — keyword search results are stored under
   `~/.cache/wishlist-sellers/`, keyed by `(mode, keyword)`. A second run with
   the same `--buying-options` within the week skips all eBay calls for items
   whose cache is still fresh; only new or expired items hit the API. Changing
   `--buying-options` (e.g. switching from `auction` to `all`) starts a fresh
   cache fill because the namespaces are separate.
2. **Verdict cache** — Haiku's "is this really the book?" verdict for each
   `(title_key, wish_name)` pair is stored in a SQLite DB at
   `~/.cache/wishlist-sellers/verdicts.db`. `title_key` is the listing title
   after stripping grade tokens (e.g. "CGC 9.8", "VF/NM") and normalizing to
   lowercase alphanumeric — not the `item_id`. Two benefits follow (BUI-223):
   the same comic title from multiple sellers is verified once per run
   (cross-seller dedup), and a relisted item with a new item ID but the same
   title is an instant cache hit. On a warm re-run, Haiku is called only for
   titles not seen in any prior run.
3. **Seen-item filter** — listings already surfaced to you are dropped before
   grouping and before verify, so they contribute zero LLM cost and zero
   output noise.

A typical weekly re-run does: full cache hit on searches → zero eBay calls →
a small verify pass on new listings only → output only if a seller crosses the
≥2 threshold with new material.

## Setting up a recurring run

**Option A — `/schedule` cloud agent (recommended for unattended
notification):**

Ask Claude to schedule this as a recurring cloud agent:

```
/schedule
Run /comic:wishlist-sellers every Sunday at 9 AM. Notify me only if sellers are found.
```

The cloud agent runs `wishlist-sellers` on the Mac Mini (where
`COMICS_SERVER_URL=http://localhost:8080` is already set), captures the
output, and delivers a completion notification when the run finishes. A clean
empty result (no sellers with ≥2 matches, exit 0) is silent; a non-empty
result surfaces the full per-seller table in the notification. **A partial run
(exit 3 — see the exit-code table below) should also notify** even with zero
sellers, because it means some candidates were never verified and the run
should be re-triggered.

**Option B — local cron** (if you prefer a local trigger):

```bash
# Run every Sunday at 9 AM; notify on new sellers OR a partial (exit-3) run.
# Capture wishlist-sellers' OWN exit code — a bare pipe would report grep's
# instead, hiding exit 3 (BUI-309). Notify when sellers were found (grep) or
# the run was incomplete (exit 3 = some candidates never verified).
0 9 * * 0 COMICS_SERVER_URL=http://localhost:8080 bash -c '\
  wishlist-sellers > /tmp/wishlist-sellers-last.txt 2>/dev/null; ec=$?; \
  if grep -q "Seller:" /tmp/wishlist-sellers-last.txt || [ $ec -eq 3 ]; then \
    terminal-notifier -title "Wish List Sellers" \
      -message "exit $ec — check /tmp/wishlist-sellers-last.txt"; \
  fi'
```

Adjust the URL and notification command to match your machine and preferred
alerting tool.

## Exit codes (BUI-309)

`wishlist-sellers` mirrors `seller-scan`'s exit-code-first contract so an
unattended scheduler can branch on the exit code before parsing output:

| Exit | Meaning | Scheduler action |
|------|---------|------------------|
| `0` | Clean run (any sellers found are in the output) | Notify only if sellers were found |
| `3` | Partial run — one or more candidates were NEVER verified (claude CLI timeout / transport failure). They stay uncached + unseen and resurface next run | Notify; re-run is safe and cheap (caches warm) |
| `1` | Verifier globally down (missing/broken `claude` CLI) — nothing could be verified | Alert; a re-run won't help until `claude` auth is fixed |

Run with `--json` for a machine-readable payload: a top-level object
`{"incomplete": bool, "sellers": [...], "dropped_candidates": [...]}`.
`incomplete` is `true` exactly when the exit code is `3`; `dropped_candidates`
lists the never-verified listings.

## Notification behavior

- **Sellers found (exit 0, non-empty)** → notify. The per-seller table is the
  notification payload when run via a cloud agent; route it to whatever channel
  reaches you (Slack, push notification, email).
- **Partial run (exit 3)** → notify. Some candidates were never verified; the
  run is worth re-triggering (they resurface next run by design). The stdout
  `INCOMPLETE:` banner (or `--json` `dropped_candidates`) names how many.
- **Clean empty result (exit 0, no sellers)** → silent. No sellers with ≥2
  matches and nothing left unverified means nothing actionable.

The script itself does not push notifications — it only writes to
stdout/stderr and sets the exit code. The scheduling layer (cloud agent or
cron wrapper) is responsible for detecting a non-empty result or a non-zero
exit and routing it.

---

Plan: `docs/plans/2026-06-26-001-feat-multi-seller-wishlist-scan-plan.md` — BUI-221.
