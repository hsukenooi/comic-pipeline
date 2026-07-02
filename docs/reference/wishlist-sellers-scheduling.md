# Scheduling recurring `/comic:wishlist-sellers` runs

Reference material for `.claude/commands/comic/wishlist-sellers.md` — moved out
of the skill body (BUI-282) because it's setup-time reference, not something a
runtime invocation needs to read.

`/comic:wishlist-sellers` is designed to run on a **recurring schedule** —
daily or weekly — and notify you only when new multi-match sellers are found.
An empty result is always silent.

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
output, and delivers a completion notification when the run finishes. An empty
result (no sellers with ≥2 matches) is silent; a non-empty result surfaces the
full per-seller table in the notification.

**Option B — local cron** (if you prefer a local trigger):

```bash
# Run every Sunday at 9 AM; notify via terminal-notifier on non-empty output
0 9 * * 0 COMICS_SERVER_URL=http://localhost:8080 wishlist-sellers 2>/dev/null \
  | tee /tmp/wishlist-sellers-last.txt \
  | grep -q "Seller:" && terminal-notifier -title "Wish List Sellers" \
      -message "$(wc -l < /tmp/wishlist-sellers-last.txt) lines — check terminal"
```

Adjust the URL and notification command to match your machine and preferred
alerting tool.

## Notification behavior

- **Non-empty result** → notify. The per-seller table is the notification
  payload when run via a cloud agent; route it to whatever channel reaches
  you (Slack, push notification, email).
- **Empty result** → silent. No sellers with ≥2 matches means nothing
  actionable; the run exits 0 with no output.

The script itself does not push notifications — it only writes to
stdout/stderr. The scheduling layer (cloud agent or cron wrapper) is
responsible for detecting non-empty output and routing it.

---

Plan: `docs/plans/2026-06-26-001-feat-multi-seller-wishlist-scan-plan.md` — BUI-221.
