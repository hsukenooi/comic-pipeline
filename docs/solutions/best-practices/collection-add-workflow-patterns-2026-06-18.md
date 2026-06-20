---
title: collection-add workflow patterns — state tracking, endpoint selection, and pending semantics
date: 2026-06-18
category: docs/solutions/best-practices
module: gixen-overlay / comic skills
problem_type: best_practice
component: tooling
severity: medium
applies_when:
  - Running /comic:collection-add
  - Deciding whether to use /api/comics/history or gixen list --json
  - Diagnosing a suspiciously high oldest_pending_days value
  - Clearing manual-resolution (needs_manual_series_canonical) pending items
related_components:
  - gixen-overlay
  - locg-cli
  - development_workflow
tags:
  - collection-add
  - collection-sync
  - pending-push
  - gixen
  - locg
  - workflow
---

# collection-add workflow patterns

Learnings from the 2026-06-18 session where 92 won snipes were reprocessed even
though 90 (97%) were already owned — and from diagnosing why `oldest_pending_days`
showed 22 after a same-day collection-sync.

## 1. Track a cutoff date — never reprocess historical wins

**Problem:** `gixen list --json` returns all 310+ snipes. Filtering to WON gives
the full win history (92 items in June 2026). Building `identify_data` for all of
them takes most of the session; 90/92 are skipped by the server as already owned.

**Fix:** Store the `end_date_iso` of the most recently processed win in the
`collection-add-state` memory entry. On the next run, only process wins with
`end_date_iso` strictly after that cutoff.

**Where:** `.claude/projects/.../memory/project_collection_add_state.md` —
updated after every successful POST.

## 2. Use `/api/comics/history` for recent runs; fall back to `gixen list` for gaps

`/api/comics/history` returns the past **7 days** of ended snipes (hardcoded in
`plugins/gixen-overlay/src/gixen_overlay/routes.py:698`). It includes richer data
(`cond_grade`, `fmv_low/high`, `winning_bid` as a numeric) and avoids the
310-item CLI dump.

- **Cutoff within 7 days** (most runs): use `/api/comics/history`, filter to
  `status == "WON"` and `end_date_iso > cutoff`.
- **Cutoff older than 7 days** (first run after a long gap): fall back to
  `gixen list --json`, filter to `time_to_end == "ENDED"` + `status` contains
  `WON` + `end_date_iso > cutoff`.

`gixen list` is a CLI (`gixen` console script); the history endpoint is a server
API call — no local install required beyond `GIXEN_SERVER_URL`.

## 3. `oldest_pending_days` ≠ "days since last sync"

**What it actually is:** The age of the oldest collection entry with
`pushed_to_locg_at IS NULL` — i.e., the oldest win that hasn't been confirmed in a
LOCG re-import.

**Why it stays stale:** Items with `needs_manual_series_canonical=true` are
excluded from every CSV export. They never enter any LOCG bulk-import, so they
are never cleared by a collection-sync re-import. They sit at max age indefinitely,
making `oldest_pending_days` look alarming even after a recent sync.

**The shared "last sync" indicator** is `last_full_import` in the status endpoint
(`/api/comics/collection/status`). That field is on the Mac Mini server, so it is
accessible from both machines and reflects the actual last LOCG import timestamp.

## 4. Clearing manual-resolution items requires two steps

Items with `needs_manual_series_canonical=true` cannot be cleared automatically.
The only path to clear them:

1. **Manually add the comic to LOCG** via the LOCG web UI (My Comics → Add).
2. **Run `/comic:collection-sync`** — specifically the re-import step: download the
   fresh LOCG XLSX and import it via the `/api/comics/collection/import` endpoint.
   This sets `pushed_to_locg_at` on any row now confirmed present in LOCG, clearing
   it from the pending count.

Skipping the re-import (e.g., only doing the CSV upload without re-importing the
resulting XLSX) does **not** clear these items. The re-import is what sets
`pushed_to_locg_at`.

## 5. Common known manual-resolution series

Series that have consistently failed Metron resolution and need manual LOCG adds:

| Series (as submitted) | Issue(s) |
|---|---|
| Ghost Rider | #4 (1973) |
| Godzilla: The Half-Century War | #2–5 (IDW 2012–2013) — #1 resolves, rest do not |
| Spawn: Director's Cut | #1 |
| Amazing Spider-Man (1970, Silver Age) | #83, #84 — Metron mis-resolves to "Amazing Fantasy #15: Spider-Man! (2012)" |

For ASM #83/#84: Metron's resolution matched the wrong series. Add them manually
to LOCG as *Amazing Spider-Man #83* and *#84* (1970, vol 1).
