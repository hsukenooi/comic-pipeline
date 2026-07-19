---
title: "/comic:collection-check false positives: series conflation, masthead-alias volume collisions, and printing conflicts"
date: 2026-07-19
category: docs/solutions/ui-bugs
module: locg-cli collection matcher (packages/locg-cli) / comic:collection-check skill
problem_type: ui_bug
component: comic-collection-check
severity: medium
symptoms:
  - "A row renders '✅ In collection' but the matched copy is a different, same-masthead line (Giant-Size/Annual/King-Size/Special vs. the base series)"
  - "A row renders '✅ In collection' but the matched copy is a different volume than the listing"
  - "A row renders '✅ In collection' but the matched copy is a different printing than the listing (base vs. Nth printing)"
  - "The user skips a book they don't actually own, or (printing case) skips a book they explicitly wish-listed"
root_cause: logic_error
resolution_type: process_change
status: mitigated (advisory flags in the skill; see BUI-249, BUI-364/373, BUI-316 for the mechanized/upstream parts; Pattern A's series-conflation guard is not mechanized, still heuristic)
tags: [collection-check, giant-size, annual, masthead-alias, cross-volume, printing-conflict, false-positive, bui-249, bui-364, bui-373, matcher]
related_files:
  - .claude/commands/comic/collection-check.md
  - packages/locg-cli/src/locg/collection_cache.py
  - packages/locg-cli/src/locg/commands.py
related_docs:
  - ../best-practices/collection-check-cover-year-forwarding-vs-bui129.md
---

# /comic:collection-check false positives: series conflation, masthead-alias volume collisions, and printing conflicts

## Problem

`/comic:collection-check`'s ownership matcher has documented blind spots that all
produce the same shape of false positive — a row renders `✅ In collection`
because *some* owned row satisfied the match, even though the owned row is not the
same collectible as the listing. The masthead-alias and printing cases are now
mechanized in the API response (`match_kind`, `printing_conflict`) and surfaced as
advisory flags in Step 2.5 of the skill (Patterns D and E); the series-conflation
case (Pattern A) remains a heuristic flag. This doc keeps the incident history and
reasoning the skill body no longer needs to restate on every run.

## Case 0: Giant-Size / Annual / King-Size conflation (Pattern A)

A confirmed, repeating case: a query for *Giant-Size Fantastic Four* #N falsely
matched an owned *Fantastic Four Annual* #N — two different books that happen to
share a masthead. The cache/matcher has no dedicated guard against this class of
same-masthead-different-line conflation (`Giant-Size …`, `… Annual`, `King-Size
…`, `… Special` vs. the base series), so it remains a heuristic pattern in the
skill (flag on any `in_collection` verdict where the queried series is one of
these distinct lines) rather than a mechanized field on the response.

## Case 1: masthead-alias match, unconfirmed volume (Pattern D, BUI-249)

The matcher's masthead-alias fallback (`_MASTHEAD_ALIAS_PAIRS` in
`collection_cache.py`) lets a query under one masthead name match an owned row
filed under a different but equivalent masthead — e.g. querying "The Mighty Thor"
matches an owned row filed as "Thor". This fallback exists to survive
inconsistent masthead naming between `/comic:identify` output and the LOCG
catalog, but it has **no notion of which volume or era** the two names refer to.

The illustrative case from the BUI-249 commit: owning *Thor* #5 in Vol. 1 (1966)
falsely satisfies a query for "The Mighty Thor #5" when the listing is actually
Vol. 3 (2015) — a different, unowned book. Before BUI-249, the check response was
a near-binary verdict with no way for a caller to detect that the match came
through the alias fallback rather than an exact series-key hit.

**Fix (BUI-249):** `cmd_collection_check` now projects `matched_series_name`,
`matched_release_date`, and `match_kind` (`"exact"` | `"alias"` | `null`) on every
verdict. `match_kind == "alias"` is the mechanized signal — the skill's Pattern D
reads it straight off the response (no re-query needed) and flags the row for the
user to confirm the volume, rather than trusting an alias match blindly. See
`.claude/commands/comic/collection-check.md` Step 2.5 Pattern D for the current
detect → flag rule.

A same-masthead **same-name** collision (multiple volumes owned under the
*identical* series name, no alias fallback involved) is a distinct, further-along
case — see Pattern D2 (BUI-284, `ambiguous_cross_volume`) and Pattern D3 (BUI-308,
the single-owned-wrong-volume residual) in the skill, and
`../best-practices/collection-check-cover-year-forwarding-vs-bui129.md` for why
the real fix for D3 is forwarding a correct per-issue cover year (BUI-316) rather
than loosening the matcher's volume model.

## Case 2: printing conflict (Pattern E, BUI-364/373)

**Confirmed incident, 2026-07-16:** a check for *Absolute Martian Manhunter* #1
(the base/first printing) read as `in_collection` because the matcher's owned
row was actually the **2nd Printing** of the same issue — a distinct collectible.
The base printing itself sat wish-listed, untouched. The orchestrator treated the
false `in_collection` as a duplicate and **skipped an explicitly wanted ~$30
book** — a missed-purchase, not a data-loss, but a real one: the user had already
signaled they wanted this specific book by wish-listing it.

Printings are not fungible with each other for collecting purposes — owning a
reprint is not owning the base printing, and vice versa — but the matcher's
series+issue key does not distinguish them, so a query with no explicit printing
can be satisfied by any owned printing of that issue.

**Fix (BUI-364, ordinal semantics BUI-373):** the check response now includes
`printing_conflict: true` whenever the matched row's `full_title` names a
printing the query never asked for, plus a `printing_candidates` list — every
same-era printing of the issue with its own owned/wish state and a
`printing_ordinal` (`1` = base printing, `2+` = a specifically-numbered reprint,
`null` = a same-era row labeled with a bare "Reprint"/"Re-Print" and no explicit
number). This is mechanized, not heuristic: the skill's Pattern E reads
`printing_conflict` and `printing_candidates` directly and flags the row,
including calling out when the query's own printing shows up wish-listed (the
strongest signal the book is still explicitly wanted). See
`.claude/commands/comic/collection-check.md` Step 2.5 Pattern E for the current
detect → flag rule.

## Why advisory, not automatic (R11)

All three patterns are surfaced as **flags**, never as an automatic verdict flip.
The matched row genuinely is owned (a real Fantastic Four Annual, a real Thor #5,
a real 2nd printing) — the question the flag raises is whether it's the *same
collectible as the listing*, which only the user can answer from the listing
itself. Auto-resolving either direction risks the two failure modes the skill
exists to prevent: silently skipping a book you don't actually own (missed
purchase), or silently treating a distinct owned copy as covering the listing
(also a missed purchase, e.g. the Martian Manhunter incident). This is the same
R11 hard-fail discipline that governs the rest of `/comic:collection-check` —
flag, never decide.

## Related

- **BUI-26** — the matcher bugfix series this conflation trap is threshold-guarded against in `cmd_collection_series_names_resolve`'s fuzzy fallback; origin of the Giant-Size/Annual awareness behind Pattern A.
- **BUI-249** — mechanized `match_kind`/`matched_series_name`/`matched_release_date`, the fix behind Pattern D.
- **BUI-284** — cross-volume ambiguity guard (`ambiguous_cross_volume`), Pattern D2.
- **BUI-308 / BUI-316** — single-owned-wrong-volume residual (Pattern D3) and its upstream fix; full essay in `../best-practices/collection-check-cover-year-forwarding-vs-bui129.md`.
- **BUI-364 / BUI-373** — printing-conflict detection and `printing_ordinal` semantics, the fix behind Pattern E.
- **BUI-45** — the unrelated leading-article false negative (see `purged-snipes-shown-as-won-2026-06-01.md`); the matcher's `_normalize_series_key` fix for that case means Step 2.5 no longer needs an article-toggle re-query pattern (removed, BUI-444).
- **BUI-449** — the series-name reconciliation endpoint Pattern C uses, which absorbed Pattern B's genuine residual (punctuation/abbreviation spelling drift) once the article-toggle case was confirmed dead work.
