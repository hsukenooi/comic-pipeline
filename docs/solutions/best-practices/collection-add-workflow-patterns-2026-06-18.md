---
title: collection-add workflow patterns — pending-push semantics and manual-resolution clearing
date: 2026-06-18
category: docs/solutions/best-practices
module: gixen-overlay / comic skills
problem_type: best_practice
component: tooling
severity: medium
applies_when:
  - Running /comic:collection-add
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
  - manual-resolution
  - locg
  - workflow
---

# collection-add workflow patterns

Learnings from the 2026-06-18 session diagnosing why `oldest_pending_days`
showed 22 after a same-day collection-sync.

**2026-07-17 refresh (BUI-374):** this doc originally also covered two other
learnings from that session — tracking a memory-stored cutoff date to avoid
reprocessing historical wins, and choosing between `/api/comics/history` and
`gixen list --json` as the win source. Both are gone from the current
`/comic:collection-add` skill: BUI-121 replaced the cutoff-date memory entry
with a server-side seen-set, and the 2026-07-05 refactor (BUI-291..295,
`docs/plans/2026-07-05-001-refactor-collection-add-simplification-plan.md`)
dropped the `/api/comics/history`-vs-`gixen list` branch entirely — Step 1 now
always sources from `gixen list --json` via the `gixen record-win-prep`
subcommand, which owns the ENDED+WON filter, the seen-set fetch/subtract, and
the identify join in one tested place. See
`docs/solutions/workflow-issues/multi-block-skill-shell-state-loss-fallback-swallow.md`
for the incident that hardened the seen-set fetch itself (BUI-352/353/354).
Those two sections have been removed here rather than updated in place — the
problem they addressed (a memory-tracked cutoff, a two-source decision) no
longer exists as a decision point in the workflow. The sections below (pending
semantics, manual-resolution clearing) were re-verified against current code
and remain accurate.

## 1. `oldest_pending_days` ≠ "days since last sync"

**What it actually is:** The age of the oldest collection entry still counted
"pending" — `pushed_to_locg_at IS NULL OR local_added_at > pushed_to_locg_at`
(`packages/locg-cli/src/locg/collection_io.py`, `_pending_push_rows`) — i.e.,
the oldest win that hasn't been confirmed in a LOCG re-import since it was
last added or re-touched locally.

**Why it stays stale:** Items with `needs_manual_series_canonical=true` are
excluded from every CSV export. They never enter any LOCG bulk-import, so they
are never cleared by a collection-sync re-import. They sit at max age indefinitely,
making `oldest_pending_days` look alarming even after a recent sync.

**The shared "last sync" indicator** is `last_full_import` in the status endpoint
(`/api/comics/collection/status`). That field is on the Mac Mini server, so it is
accessible from both machines and reflects the actual last LOCG import timestamp.

## 2. Clearing manual-resolution items requires two steps

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

## 3. Common known manual-resolution series (as of 2026-06-18)

Series that had consistently failed Metron resolution and needed manual LOCG
adds in the original session. This is a point-in-time record, not a live
status — it was not re-verified against the current collection store as part
of the 2026-07-17 refresh (no server access from this session), so treat it as
a starting hint, not ground truth. Confirm against a fresh `/comic:collection-add`
export's `.notes.md` (its `manual_series_count`, from
`/api/comics/collection/export`) before assuming any of these are still
unresolved — a manual LOCG add followed by a `/comic:collection-sync`
re-import (Section 2 above) would have cleared any of them in the interim.
(`/api/comics/collection/status` does not carry this count in its default,
non-verbose response — only `pending_push_count` and `oldest_pending_days`.)

| Series (as submitted) | Issue(s) |
|---|---|
| Ghost Rider | #4 (1973) |
| Godzilla: The Half-Century War | #2–5 (IDW 2012–2013) — #1 resolves, rest do not |
| Spawn: Director's Cut | #1 |
| Amazing Spider-Man (1970, Silver Age) | #83, #84 — Metron mis-resolves to "Amazing Fantasy #15: Spider-Man! (2012)" |

For ASM #83/#84: Metron's resolution matched the wrong series. Add them manually
to LOCG as *Amazing Spider-Man #83* and *#84* (1970, vol 1).
