---
title: "Guard strictness must match the consequence: load-bearing placeholders, fail-open vs fail-closed, near-synonym quantities, and eager resolvers"
date: 2026-07-20
category: design-patterns
module: "locg-cli (packages/locg-cli/src/locg/commands.py, collection_io.py) + gixen-cli (packages/gixen-cli/server/install.sh, server/db.py)"
problem_type: design_pattern
component: service_object
severity: high
applies_when:
  - "Deciding whether to remove a fabricated/placeholder value from a stored row because a ticket calls it 'fake'"
  - "Writing a guard whose failure mode needs to differ across a read/rewrite path and a destructive (delete/retire) path"
  - "Comparing two dates or quantities that look interchangeable but come from different sources (a cover date vs an on-sale date, an identified year vs a looked-up year)"
  - "Two call sites independently re-implement 'the same' date/identity/era check"
  - "A rename or migration introduces a 'prefer the new path if it exists' resolver alongside any script that could create that path as a side effect"
tags:
  - record-win
  - reconcile
  - data-safety
  - fail-open-fail-closed
  - metron
  - locg-cli
  - gixen-cli
  - bui-210
  - bui-461
  - bui-462
  - bui-459
related_docs:
  - "docs/solutions/design-patterns/enrich-win-rows-from-full-issue-detail-not-lightweight-lookup.md"
  - "docs/solutions/integration-issues/locg-export-deletes-owned-wished-books.md"
  - "docs/solutions/conventions/verify-ticket-premise-before-implementing.md"
---

# Guard strictness must match the consequence: load-bearing placeholders, fail-open vs fail-closed, near-synonym quantities, and eager resolvers

## Context

BUI-210 (reopened), BUI-461 (backfill), and BUI-462 (reconcile auto-heal) all touch the same
record-win date/era machinery in `packages/locg-cli/src/locg/commands.py` and
`collection_io.py`; BUI-459 touches an unrelated package (`gixen-cli`'s
`server/install.sh`) but turned out to be the same shape of mistake wearing a different
costume. Four traps recur across the batch, and all four have the same structure: a
guard, comparison, or resolver that is perfectly reasonable for the common case becomes
dangerous the moment something destructive, external, or irreversible starts depending
on it. The fix in every case was to make the strict thing stricter, not to loosen or
delete it.

## Guidance

### 1. A placeholder can be the only discriminator a downstream gate has

BUI-210's reopen asked record-win to stop stamping a `{year}-01-01` placeholder
`release_date` on a Metron miss, so misses "degrade gracefully to blank" instead. It
looked like pure cleanup — the date is admittedly fabricated. It was implemented,
reviewed, and reverted, because removing it does two things, only one of which is
harmless:

- **Nothing changes about the export.** `_row_to_csv_dict` (`collection_io.py`) already
  blanks a placeholder via `_is_placeholder_release_date` before it reaches the CSV, so a
  placeholder row and a genuinely dateless row emit the identical empty `Release Date`
  cell. There was no export bug to fix.
- **It silently deletes wins.** `_reconcile_score`'s year compare is the only
  discriminator left between two undecorated volumes of one masthead (e.g. `"The X-Men
  (1963 - 1981)"` vs `"X-Men (1991 - 2011)"`, which normalize to the same series key).
  Dateless, that compare fails open (see pattern 2 below), the wrong volume scores a
  match, and the BUI-122 collision guard in `_reconcile_phase` auto-heals the pending win
  away with no warning. A win stuck pending is recoverable; a win silently dropped on
  import is not.

The code now carries a `DO NOT REMOVE THIS` block in `_build_win_row` documenting all
three reproduced harms (a third — `_match_owned_issue`'s `require_dated` alias pass
rejecting a dateless owned row, buying a duplicate — is the R11-direction cost) rather
than leaving the next editor to rediscover them:

```python
# DO NOT REMOVE THIS. BUI-210's reopen asked for it ("Metron misses
# degrade gracefully to blank — never a placeholder"), on the premise
# that the placeholder is what ships rows dateless to LOCG. That premise
# is false, and removing the stamp was implemented, reviewed, and
# reverted. [...]
#
# The year in this stamp is real (it is the identified cover year); only
# its month/day are fabricated, which is why the export drops it and the
# era guards keep it.
```

**The generalizable trap:** before deleting a value because a ticket calls it "fake,"
enumerate every consumer that reads it. Here the year in the stamp was real; only
month/day were fabricated, and a downstream gate (`_reconcile_score`) depended on the
year alone being present — not on the date being exact.

### 2. Fail-open is correct for reversible paths, wrong for destructive ones

`_reconcile_score` (`collection_io.py`) fails open on its year compare — it only rejects
a match when **both** sides name a year and they disagree:

```python
cache_year = (cache_row.get("release_date") or "")[:4]
xlsx_year = (xlsx_row.get("release_date") or "")[:4]
if cache_year and xlsx_year and cache_year != xlsx_year:
    return 0
```

That is the right call for `_reconcile_score`'s own job — rewriting a row's identity in
place from an LOCG export — because a wrongly-permissive match there just means the
`(cache_year and xlsx_year)` guard didn't fire; nothing is destroyed, and a later pass
can still correct the row. But BUI-211's auto-heal branch reuses this same permissiveness
to *retire* a row (drop a pending `agent_win` it judges to be a duplicate of an owned
`locg_export` row), and there the fail-open case is exactly where a dateless win
fuzzy-matches the wrong volume of a masthead and gets folded into a book it isn't.

BUI-462 added `_era_confirmed` as the fail-**closed** complement, scoped to exactly the
destructive branch:

```python
def _era_confirmed(cache_row: dict[str, Any], xlsx_row: dict[str, Any]) -> bool:
    """Positive same-era evidence for the *destructive* auto-heal branch (BUI-462).

    ``_reconcile_score``'s year compare fails **OPEN** [...] That is the right
    call for the non-destructive paths [...] It is the wrong call for the
    auto-heal branch, which *retires* a row [...]
    """
```

It requires *presence* of a year on both sides (or, for the TPB/HC/OGN title-match
branch where no `#N` ambiguity exists, an identical title with no issue token on either
side) — the mirror image of `_reconcile_score`'s "reject only on an explicit
disagreement." A dateless win is never healed; it is left pending for the operator, which
is a visible non-clear rather than a silent wrong drop.

**Principle:** when one predicate (or one piece of shared logic) feeds both a reversible
action and an irreversible one, the irreversible action needs its own stricter gate. Do
not weaken the shared predicate to "fix" the destructive branch — that reopens the
false-negatives the permissive version exists to avoid on the reversible path.

### 3. An exact match between two near-synonym quantities is a bug waiting on a boundary case

BUI-210's actual defect (not the placeholder, which was a red herring): the reprint guard
compared `year_raw` — the **cover** year `comic-identify` reads off the book — against
Metron's `store_date` — the **on-sale** date — with an exact year match. A January-cover
book routinely ships the previous November, so a genuine `1969-11-10` `store_date` for a
`1970`-cover book was classified a reprint and the whole hit discarded, taking
`metron_id` and (since BUI-458) `publisher_name` down with it.

The fix, `_metron_release_date`, treats the two candidate dates differently **because
they are not the same quantity**:

```python
* ``store_date`` gets the shared :func:`_year_gate_accepts` window — the
  symmetric ±1 that ``_match_owned_issue``, ``_match_wishlisted_issue`` and
  ``_dedup_era_compatible`` use [...] The slack is earned: ``year_raw`` is the
  COVER year comic-identify reads off the book while ``store_date`` is the
  on-sale date, and a January-cover issue ships the previous November.
* ``cover_date`` gets an EXACT year match. It is the same quantity as
  ``year_raw`` — both are cover dates — so there is no skew to forgive here,
  and a one-year disagreement is not skew but evidence that ``lookup_issue``
  matched a different book.
```

A real reprint is decades away (a 1970 book reprinted in 2005), so the ±1 window on
`store_date` still rejects genuine reprints; it only rescues the one-year cover-vs-onsale
skew that is normal for any January book.

**Trap:** an exact equality between two values that sound like the same thing — "the
year," "the date" — is worth a second look at *where each one comes from*. If they are
measuring different events (cover date vs on-sale date, identify year vs lookup year),
an exact match will eventually reject a boundary case that is actually correct.

### 4. Duplicated guards drift onto different inputs — consolidate

The reprint guard above existed at two call sites in record-win: the site that decides
whether to *accept* a Metron hit (the date-only lookup) and the site that *stores* the
resulting date on the row. They had been copy-pasted and, by the time of the reopen, had
already diverged on which candidate date each one examined — so the "accept" site and the
"store" site could disagree about the same win. BUI-210 consolidated both into one
function, `_metron_release_date`, with the rationale stated directly in its docstring:

```python
"""[...] ONE helper for the whole record-win date decision, so the site that
decides whether to *accept* a Metron hit [...] and the site that *stores* the
date [...] can never drift on which date they picked or how they judged it.
They were duplicated before, and the duplication is exactly how the two ended
up examining different candidate dates.
"""
```

A second instance of the same class of drift surfaced in `_dedup_era_compatible`
(the BUI-267 cross-era dedup guard): its own fallback compared years **exactly**, so a
prior-year `store_date` on an undecorated series — the very date `_metron_release_date`
had just accepted with its ±1 window — failed the dedup's own era check on a retry. Since
`/comic:collection-add` resubmits a whole batch after a `partial_failure` and
`write_wins` overwrites by `gixen_item_id`, the dedup miss silently **rebuilt** the good
row, downgrading a real date and publisher back to a placeholder and null whenever Metron
was unreachable on the retry (a common reason the first attempt failed at all). The fix
was not a new rule; it was making `_dedup_era_compatible` call the same
`_year_gate_accepts` window `_metron_release_date` already uses, so the write gate and
the gates that read the row back agree on what "same era" means.

**Trap:** when the "same" check is written twice, it is not actually the same check —
it is two checks that happen to agree today. The fix is a shared helper, not a second
careful copy.

### 5. A "prefer if exists" resolver turns creating an empty directory into a destructive act

Unrelated package, same shape of bug. `gixen-cli`'s `server/db.py::resolve_server_dir()`
implements the BUI-220 rename migration path:

```python
def resolve_server_dir() -> Path:
    """[...]
      1. ``~/.comics-server`` if it exists (post-migration / fresh installs), else
      2. ``~/.gixen-server`` if it exists (the live server keeps working), else
      3. ``~/.comics-server`` (the canonical default for a clean machine).
    """
    new = Path.home() / ".comics-server"
    legacy = Path.home() / ".gixen-server"
    if new.exists():
        return new
    if legacy.exists():
        return legacy
    return new
```

This is a reasonable migration-safety resolver on its own. The danger appeared when
`server/install.sh` was edited to use the post-migration names (`com.comics.server` /
`~/.comics-server`) even though the BUI-220 rename had only been done in docs (BUI-425),
never actually run on the live Mac Mini — which is still `com.gixen.server` /
`~/.gixen-server`. `install.sh` does `mkdir -p "$SERVER_DIR"` as a normal part of a
routine re-deploy. The instant that line ran with `SERVER_DIR=~/.comics-server`, the
directory would exist — empty — and `resolve_server_dir()` would prefer it on the very
next call, including the `launchctl kickstart -k gui/$(id -u)/com.comics.server` a few
lines later, which (same `Label`) would take over the **real running job** and repoint it
at a fresh, empty database. BUI-459 reverted every functional occurrence in `install.sh`
back to the pre-migration names and left a comment explaining that this is deliberate,
not drift — the real BUI-220 data-dir migration stays a separate, intentional runbook
(`docs/runbooks/comics-server-dir-migration.md`), never a side effect of a routine
install-script run.

**Trap:** a "prefer the new path if it exists" resolver is only as safe as every script
that can *cause* the new path to exist. `mkdir -p` looks inert; next to an
existence-preferring resolver on a live system, it is a silent takeover switch. Before
changing which name/path a deploy script writes, check what resolves off that path's
mere existence, not just its contents — an empty directory can be more dangerous than a
missing one.

## Why This Matters

- **Deleting a value is not neutral just because the value looks fabricated.** A stamped
  placeholder, once other code has come to rely on its mere presence (a year, in this
  case) as a discriminator, is load-bearing regardless of how it was produced.
- **The same predicate can be right in one branch and wrong in another.** Fail-open vs
  fail-closed is not a property of the check in isolation — it is a property of what the
  check gates. Scoping the stricter gate to only the destructive branch (rather than
  tightening the shared predicate globally) avoids reopening the false-negatives the
  permissive version exists to prevent elsewhere.
- **"Same field name" does not mean "same quantity."** `year_raw` and `store_date`'s year
  are both "a year" in the type system; they come from different events (cover print date
  vs on-sale date) and need different tolerances.
- **Copy-pasted guards are a liability the moment either copy is touched.** They will
  drift, and the drift is invisible until two call sites start disagreeing about the same
  input.
- **A resolver's "prefer if exists" branch makes directory/file creation itself a
  meaningful, reviewable action** — not just a setup step to run without a second glance.

## When to Apply

- Any time a ticket proposes deleting or blanking a value described as "fake,"
  "placeholder," or "just a stopgap" from a row that flows into a later reconciliation,
  matching, or dedup step — trace every consumer, not just the obvious display path.
- Any time a comparison or matching function backs both a read/rewrite path and a
  delete/retire/overwrite path — check whether the failure mode should differ, and add
  the stricter gate on the destructive branch specifically (`_era_confirmed` is the
  template).
- Any time two dates, years, or identifiers that "should be the same" are compared
  exactly — confirm they really are the same quantity, not two different real-world
  events that happen to share a field name.
- Any time the same check exists at two call sites — consolidate into one function before
  the next edit, not after the two copies have already disagreed in production.
- Any time a migration/rename touches a resolver with an "if it exists" preference —
  audit every script that could create the preferred path (even empty, even via
  `mkdir -p`) before changing which name a deploy path writes.

## Examples

- **BUI-210:** `packages/locg-cli/src/locg/commands.py` — `_build_win_row`'s `DO NOT
  REMOVE THIS` block; `_metron_release_date`; `_dedup_era_compatible`.
- **BUI-461:** reuses `_metron_release_date` verbatim for the backfill's date resolution,
  and re-derives the same "requires `metron_id`, not shape" intent check
  (`_is_placeholder_release_date`) rather than fabricating a `YYYY-01-02` day to dodge the
  export's placeholder regex.
- **BUI-462:** `packages/locg-cli/src/locg/collection_io.py` — `_reconcile_score`
  (fail-open) vs `_era_confirmed` (fail-closed complement on the auto-heal branch).
- **BUI-459:** `packages/gixen-cli/server/install.sh` vs
  `packages/gixen-cli/server/db.py::resolve_server_dir()`.
