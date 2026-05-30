---
title: collection-check False Matches — Substring Issues, Annual Conflation, Ignored Ownership, and Masthead Aliases
date: 2026-05-30
category: docs/solutions/logic-errors/
module: locg-cli
problem_type: logic_error
component: collection-cache
severity: high
symptoms:
  - A comic that is already owned passes /comic:collection-check and gets sniped (false negative)
  - collection-check reports a comic as owned when it is not (false positive)
  - Giant-Size Fantastic Four reported as owned, conflated with Fantastic Four Annual
  - Issue "2" matches cached "#32"; "Fantastic Four #6" matches "Fantastic Four Annual #6"
  - Wish-list / pull-list rows report as in_collection
root_cause: matcher_logic
resolution_type: code_fix
related_components:
  - cmd_collection_check
  - _normalize_series_key
tags:
  - collection-cache
  - matching
  - false-positive
  - false-negative
  - annual
  - variant
  - BUI-26
  - BUI-46
---

# collection-check False Matches (BUI-26)

## Problem

Thor #154 was won on eBay despite already being in the collection — it passed
`/comic:collection-check`. Investigating the matcher (`cmd_collection_check` in
`src/locg/commands.py`) against the real cache (`~/.cache/locg/collection.json`,
2368 rows) surfaced **four distinct defects**, two of which produce false
negatives (owned books slip through the buy gate) and two false positives.

All four were reproduced directly against the real cache.

## Root causes

### A — Series alias / cover-title divergence (FALSE NEGATIVE) — the Thor #154 bug

```
check("Thor", 154)            -> in_collection   (Thor #154)
check("The Mighty Thor", 154) -> not_in_cache    <-- BUG
```

Thor #154 is filed by LOCG under series `Thor (Vol. 1) (1966 - 1996)`
(normalized key `thor`). But Thor vol 1 of that era carries the cover/masthead
title **"The Mighty Thor"**, so `/comic:identify` reports the series as "The
Mighty Thor". `_normalize_series_key` strips the leading "The" → `mighty thor`,
which ≠ `thor`. No row matches, and the owned comic passes the buy gate.

This is **distinct from BUI-45** (leading-article stripping, already fixed): the
divergence is the extra masthead word "Mighty", not an article. It needs a
series-alias strategy (or an identify-side fix), so it is tracked separately as
**BUI-46** and is *not* fixed here. A skipped regression test documents it:
`test_check_mighty_thor_masthead_alias_known_gap`.

### B — Loose substring issue match (FALSE POSITIVE)

```
check("Fantastic Four", 2)  -> in_collection, matched "Fantastic Four #32"   <-- BUG
```

After a `#N` token mismatch the matcher fell back to `issue in full_title`, so
issue `"2"` matched `"#32"` / `"#12"` / `"#222"` — any title containing a `2`.

### C — Sub-series qualifier conflation (FALSE POSITIVE) — folds in the GSFF report

```
check("Fantastic Four", 6)             -> matched "Fantastic Four Annual #6"   <-- BUG
check("Giant-Size Fantastic Four", 2)  -> (via identify reporting "Fantastic Four") matched an Annual
```

Annuals / Giant-Size / King-Size specials are filed under the **base**
`series_name` (e.g. `Fantastic Four (Vol. 1)`) with the qualifier living only in
`full_title` (`Fantastic Four Annual #6`). The matcher keyed series identity off
`series_name`, so the qualifier was invisible and a base-series query matched the
special. This is the mechanism behind the **Giant-Size Fantastic Four ↔ Fantastic
Four Annual** false positive.

A scan of the real cache found 50 / 2316 issue-bearing rows where the
`full_title` prefix diverges from `series_name` — **every one** is an Annual /
King-Size Annual / Director's Cut, i.e. exactly the rows that must stay distinct.

### D — `in_collection` flag ignored (FALSE POSITIVE)

The matcher returned `in_collection` for any series+issue hit without checking
`in_collection`. That field is a **copies-owned count** (observed values
`{0, 1, 2, 4}` in the real import), where `0` means wish-list / pull / read but
**not owned**. Unowned rows were reported as owned.

The handoff's guess ("volume collision via `series_name_index`") was **not** a
cause: `cmd_collection_check` does not use `series_name_index` at all.

## Fix (B, C, D)

Rewrote the match loop in `cmd_collection_check`:

1. **Skip unowned rows** — `if not row.get("in_collection"): continue` (truthy,
   so multi-copy counts `2`/`4` still count). *(D)*
2. **Derive series identity from the `full_title` prefix**, not `series_name`,
   via a new `_split_full_title()` helper that returns
   `(series_portion, issue_token)`. `"Fantastic Four Annual #6"` →
   `("Fantastic Four Annual", "6")`, so the qualifier stays attached and a plain
   `Fantastic Four` query no longer matches it. Annuals remain findable by their
   qualified name. *(C)*
3. **Exact issue-token equality** (leading zeros ignored); dropped the substring
   fallback, so `"2"` no longer matches `"#32"`. A no-`#N` branch (TPB/OGN)
   still requires the issue token to appear verbatim. *(B)*

## Verification

- Reproductions on the real cache now return the correct verdicts for FF #2,
  FF #6, GSFF #2, FF Annual #2 (unowned), FF Annual #6 (owned).
- Regression tests added in `tests/test_collection_commands.py`
  (`test_check_rejects_substring_issue_match`,
  `test_check_rejects_annual_for_base_series_query`,
  `test_check_matches_annual_by_qualified_name`,
  `test_check_giant_size_not_confused_with_annual`,
  `test_check_ignores_unowned_rows`), plus a skipped test for the BUI-46 gap.
- Full suite: 327 passed, 2 skipped. No regressions (BUI-45 leading-article and
  the import→check pipeline tests still pass).

## Remaining live check

Re-run the real fixed CLI (`locg collection check ...`) after reinstalling the
updated package, against the live `~/.cache/locg/collection.json`, to confirm the
deployed binary (not just the repo source) behaves identically. Tracked on BUI-26
(left In Progress).
