---
title: "Unified LOCG ↔ Mac Mini sync model (BUI-208): diff-before-commit, Option B wishlist, source-keyed storage"
date: 2026-06-22
category: docs/solutions/integration-issues
module: locg-cli
problem_type: architecture_decision
related_components:
  - locg-cli
  - gixen-overlay
  - comics
  - development_workflow
tags:
  - locg
  - collection-sync
  - wish-list
  - bui-208
  - bui-206
  - bui-122
  - source-of-truth
  - data-loss
  - in-collection
  - diff-before-commit
---

# Unified LOCG ↔ Mac Mini sync model (BUI-208)

The collection + wishlist sync to LOCG had accreted in pieces (BUI-87/93/122/124/130/184/206).
BUI-208 replaced that with one deliberately-designed model. The full design is in
`docs/plans/2026-06-21-001-design-unified-locg-sync-plan.md`; this is the durable summary of
the decisions and the non-obvious traps, so future work doesn't re-derive them.

## The two immovable facts the model is built on

1. **There is no reliable automated write to LOCG.** Cloudflare binds the session to the
   browser's TLS fingerprint; the authenticated write path was retired (ADR
   `0001-pivot-locg-cli-to-local-first`). LOCG is reachable to a *human* in a browser, not to
   automation. The only interfaces are a **manual XLSX export down** and a **manual owned-safe
   CSV upload up** (with LOCG's import preview as the last gate).
2. **`In Collection=0` deletes.** LOCG's bulk import reads it as "un-collect this book," and the
   wishlist contains books you later own — the BUI-122 coupling that twice caused real data loss
   (18 owned books, then 26 owned X-Men).

## The model (decisions)

- **Authority is per-entity, per-field-class, not global.** The Mac Mini is authoritative for
  *membership/ownership*; LOCG is authoritative only for *canonical strings* (Series Name / Full
  Title / Release Date), which are knowable solely from a prior LOCG export. (This resolves the
  CONCEPTS-vs-issue-title contradiction over "who is the source of truth.")
- **Diff-before-commit is the architecture, not a convenience.** Because no LOCG side-effect can
  be automated, the safe unit of work is a reviewed diff: the Mac Mini computes a sync plan, the
  human approves it (Gate 1), then applies the LOCG-side change behind LOCG's preview (Gate 2).
- **Collection = additive.** LOCG un-collects are *held* for review (never auto-applied — a wrong
  un-own costs a duplicate purchase); pull-list arrivals owned on LOCG flow down.
- **Wishlist = Option B (Mac Mini authoritative).** Wishes are managed on the Mac Mini; the import
  does **not** source wishes. The only collection↔wishlist link is **fulfillment-drop** (an owned
  book drops the matching wish, keeps owned — the BUI-130 conflicts audit, run at sync). Mirroring
  wishes *up* to LOCG is opt-in and **deferred** (it's the only `In Collection=0` emitter).
- **Cost-asymmetry justifies the entity difference.** A wrongly-dropped wish is free to recover; a
  wrongly-un-owned book costs money. So wishlist removals auto-apply, collection un-owns are held.
  "Bringing the wishlist to collection parity" on removal would be *wrong*.

## Non-obvious traps (the ones that bit, or nearly did)

- **The load-bearing wish sentinel is the EXPORT-push gate, not the import filter.**
  `wish_rows_for_export` decides what reaches LOCG; it used "has `series_name` → LOCG already has
  it → skip." BUI-208 keys it on an explicit `source: local|export` field instead (with a
  `series_name`-absence fallback for un-migrated data, so the change is behavior-preserving). If
  you ever stamp a Metron-resolved `series_name` onto a *local* add without the `source` field,
  the old sentinel would silently misclassify it as "LOCG has it" and drop it from the push.
- **Single-home storage dissolves BUI-206.** Wish state was dual-stored (an `in_wish_list` flag on
  collection rows *and* `wish-list.json`), reconciled only one way. The fix isn't to clear the
  stale flag — it's to stop the import rewriting `wish-list.json` at all, so a removal can't be
  resurrected. The `in_wish_list` flag had exactly one reader (the import filter, now removed); it
  is left stored verbatim but inert.
- **Deletion safety rests on the owned-safe backstop, which inherits the matcher's masthead gap.**
  "Compare the same way the destructive match does" is necessary but not sufficient — it inherits
  the `The X-Men ↔ Uncanny X-Men` blind spot (BUI-129/F6). The owned-safe export filter
  over-excludes the masthead-alias family, and a machine gate refuses to emit any `In Collection=0`
  row in the default (wins-only) export. Keep the owned-safe checks unconditional — a regression
  there is the deletion hole (`test_wish_export_excludes_owned_when_source_defeats_gate` guards it).
- **There is NO row-count limit on LOCG uploads.** The "≤20 rows per batch" advice was a
  misdiagnosis — the importer hangs on **incomplete/dateless rows**, not batch size. Upload
  complete + exact rows at any size.

## Where it lives

- Code: `packages/locg-cli/src/locg/collection_io.py` (`_wish_source`, `wish_rows_for_export`,
  `generate_csv` machine gate, `migrate_wish_list_source`, `import_xlsx` no longer touches the
  cache), `commands.py` (`cmd_collection_export(push_wishes=False)`, `cmd_wish_list_*`),
  `plugins/gixen-overlay/src/gixen_overlay/routes.py` (`/api/comics/collection/export?push_wishes=`).
- Skill: `.claude/commands/comic/collection-sync.md` (wins-only by default; fulfillment-drop step;
  opt-in wish push; no row-count batching).
- Migration: `locg wish-list migrate-source` (backup-verified field-backfill; one-time).

## Related

- `docs/plans/2026-06-21-001-design-unified-locg-sync-plan.md` — the full design + §14 review.
- `integration-issues/locg-export-deletes-owned-wished-books.md` — the BUI-122 data-loss post-mortem.
- `integration-issues/locg-bulk-import-sync-learnings-2026-06-17.md` — lesson 2 (the ≤20-row misdiagnosis).
- ADR `packages/locg-cli/docs/decisions/0001-pivot-locg-cli-to-local-first.md` — why no automated LOCG write.
