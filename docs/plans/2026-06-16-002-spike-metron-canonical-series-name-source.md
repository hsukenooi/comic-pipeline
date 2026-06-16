---
title: "spike: Metron as canonical series-name source vs. extended alias table (BUI-193)"
date: 2026-06-16
type: spike
status: decided
linear: BUI-193
depth: standard
decision: "Extend the hardcoded _SERIES_ALIASES table (Option B); defer Metron-ID normalization."
---

# spike: Metron as canonical series-name source (BUI-193)

## Decision (TL;DR)

**Adopt Option B — extend the hardcoded `_SERIES_ALIASES` table** with the known
name-drift series (masthead-adjective and short-form aliases like
`x-men ↔ uncanny x-men`, `incredible hulk ↔ hulk`), keeping the existing
year-gating. **Defer Option A (Metron stable-series-ID normalization)** — it
solves the same class of miss but at an order-of-magnitude larger change surface
and risk, and the data it would key on (a Metron `series_id`) is **not stored on
any collection row today**, so Option A is not even a small patch — it is a new
import-pipeline + backfill project. Capture Option A as a follow-up ticket only
if the alias table starts to feel unbounded.

---

## 1. The bug class, and the X-Men case still slips through

**Bug class.** Two spellings of the same series that differ by a *word choice*
(not just punctuation, article, volume, or year) are not treated as aliases by
`_normalize_series_key`, so an owned book queried under the other spelling
returns `not_in_cache` and surfaces as a buy/wish candidate. Concretely:
`The X-Men ↔ Uncanny X-Men`, `The Incredible Hulk ↔ Hulk`,
`The Mighty Thor ↔ Thor`, `Invincible Iron Man ↔ Iron Man`.

**Confirmed still live.** `_normalize_series_key`
(`packages/locg-cli/src/locg/collection_cache.py:127-138`) strips `(Vol. N)`,
`(YYYY - YYYY)`, `(YYYY - Present)`, bare `(YYYY)`, and a leading
article (`The`/`A`/`An`), then lowercases. Running the exact production regexes
on the immediate case:

| Query A | normalized | Query B | normalized | match |
|---|---|---|---|---|
| `The X-Men` | `x-men` | `Uncanny X-Men` | `uncanny x-men` | **no** |
| `The Incredible Hulk` | `incredible hulk` | `Hulk` | `hulk` | **no** |
| `The Mighty Thor` | `mighty thor` | `Thor` | `thor` | **no** |

The matcher gates a `not_in_cache` verdict behind an *exact* normalized
series-key equality (`commands.py:1661`, deliberately exact since BUI-26 so
`Fantastic Four Annual` can't satisfy a `Fantastic Four` query). So the UXM #137
case in the ticket — collection stores `The X-Men #137`, wish-list/query says
`Uncanny X-Men #137` — normalizes to `x-men` vs `uncanny x-men`, misses, and
the listing surfaces as a buy candidate. **Confirmed.**

**What the existing mitigations cover — and don't.**

- **`_SERIES_ALIASES` (commands.py:1625-1628)** already collapses
  `mighty thor → thor` and `invincible iron man → iron man`, **year-gated**
  (only retried when the query carries a `year`, so the era filter in
  `_match_owned_issue` keeps it from colliding with a distinct same-masthead
  series like *The Mighty Thor (Vol. 3)*, 2015). This is exactly the mechanism
  Option B extends. **It does not contain an X-Men or Hulk entry today**, so the
  ticket's case is uncovered.
- **Series-names endpoint + Pattern C** (`routes.py:936` →
  `cmd_collection_series_names` at `commands.py:1747`; skill at
  `collection-check.md:164-182`) and the **BUI-171 reconciliation** in
  `wishlist-add.md:95-111` only **flag** the suspect miss for a human ("did you
  mean *Uncanny X-Men*?"). They are advisory disambiguators driven by the LLM
  skill layer — they never auto-resolve, and they only fire when the model
  notices the name "looks Metron-style / short / wrong-volume." They reduce the
  blast radius (a human gets a warning) but the *matcher itself* still returns
  `not_in_cache`, and any non-interactive consumer (the conflicts audit below)
  gets no benefit.
- **The conflicts audit** (`cmd_wish_list_conflicts`, `commands.py:759-801`,
  behind `/api/comics/wish-list/conflicts`) calls the **same**
  `cmd_collection_check` → same `_normalize_series_key`, with **no** Pattern-C
  flagging and deliberately **no year**. So an already-owned `The X-Men #137`
  that got onto the wish-list as `Uncanny X-Men #137` is **invisible** to the
  audit — the exact BUI-122 data-loss precursor the audit exists to catch.

**Net:** the class is real, the immediate case is live in both the buy-time
check and the retroactive audit, and the only thing standing between it and a
duplicate buy / collection-row deletion is a human noticing a soft flag.

---

## 2. Option A — Metron stable-series-ID normalization

**Idea.** Resolve every series (on LOCG import *and* on wish-list add) to a
Metron stable `series_id`, store that ID on the row, and match on ID equality
instead of (or before) the normalized name. Metron *does* expose what we'd need:
`MetronClient.lookup_issue` already returns `series_id`
(`metron.py:115`) alongside `series_name`.

**API coverage.** Adequate but not total for our corpus:

- Metron is well-populated for mainstream US Marvel/DC back-issues — the exact
  long-running mastheads this bug afflicts (X-Men, Hulk, Thor, ASM). The
  existing record-win enrichment already leans on it and the matcher's BUI-32
  disambiguation works.
- **Coverage gaps that become correctness gaps under ID-matching:** a series
  Metron lacks, or a `series_list({"name": ...})` call that returns multiple
  candidates and **can't be disambiguated** (`_disambiguate_series` returns
  `None` without a year, `metron.py:38-65`), yields *no* `series_id`. Under
  name-matching a gap is harmless (you fall back to the name); under ID-matching
  you must define and test a fallback for every un-resolvable series, or you
  re-introduce the same miss with extra moving parts.
- Wish-list names carry **only series + issue, no year** (see
  `cmd_wish_list_conflicts` docstring) — so the disambiguator that needs a year
  is least reliable exactly on the wish-list side where we'd want it most.

**Change surface (large).** This is the load-bearing finding:

1. **No `series_id` is stored on any collection row today.** The LOCG import
   (`collection_io.py:644`) hard-sets `new_row["metron_id"] = None` — it never
   calls Metron at all. The record-win path *does* call Metron but persists only
   the issue-level `metron_id` (`commands.py:~2077`), **never the
   `series_id`**. So matching on `series_id` requires:
   - Adding a Metron lookup to the **LOCG import pipeline** (today purely
     offline XLSX parsing) — a network dependency, rate-limit handling, and a
     per-row latency hit on a 2400+ row import.
   - A **one-time backfill** to stamp `series_id` onto every existing row, or a
     dual name-or-ID matcher during the transition.
2. **`_normalize_series_key` and `_match_owned_issue`** gain an ID branch (and
   must still keep the name path for un-resolved rows).
3. **Wish-list add + the conflicts audit** must resolve to `series_id` too, or
   the asymmetry (collection keyed by ID, wish-list keyed by name) reproduces
   the miss.
4. The matcher's four documented bugfixes (BUI-26/46/105/176) all reason over
   names + titles; an ID layer sits *beside* them, it doesn't replace them —
   `full_title` is still a string we parse for the issue token.

**Cost / risk.** High. New runtime network dependency on the previously-offline
import; a backfill migration; rate-limit + ambiguity fallbacks to design and
test; and a transition window where rows are half-ID-keyed. The blast radius of
a *wrong* Metron disambiguation is also worse than a name miss — an incorrect
`series_id` collapse could make two genuinely different series look identical
(a false `in_collection`, which *suppresses* a wanted buy silently), whereas the
name-miss failure mode is the more visible false `not_in_cache`.

---

## 3. Option B — extend the hardcoded `_SERIES_ALIASES` table

**Idea.** Add the known name-drift pairs to the existing year-gated alias map and
nothing else:

```python
_SERIES_ALIASES: dict[str, str] = {
    "mighty thor": "thor",
    "invincible iron man": "iron man",
    "x-men": "uncanny x-men",          # The X-Men (1963) catalog == Uncanny X-Men
    "incredible hulk": "hulk",         # confirm catalog spelling before adding
    # ...extend conservatively, verified against the real catalog
}
```

**Change surface (tiny).** One dict literal in `commands.py`. The retry already
exists (`commands.py:1726-1731`): when the direct match misses *and a year is
present*, it re-runs `_match_owned_issue` against the aliased key. No new code
path, no storage change, no network call, no migration.

**Two real gaps to close as part of B (cheap):**

- **Direction.** The current map is one-way (`query → owned-spelling`) and the
  immediate case is *backwards* from the existing entries: the ticket has the
  **collection** storing the masthead (`The X-Men`) and the **query** using the
  base (`Uncanny X-Men`), where Thor was the reverse. Pick the catalog spelling
  as the canonical target and map the other(s) to it; verify against the real
  store's `series-names` before committing each entry.
- **The audit is year-blind.** `_SERIES_ALIASES` is only consulted when a `year`
  is passed, but `cmd_wish_list_conflicts` passes none (by design, BUI-129). So
  adding entries fixes buy-time `/comic:collection-check` but **not** the
  retroactive conflicts audit. Closing that means letting the conflicts audit
  attempt the alias fallback without a year — acceptable for the
  `x-men`/`hulk`/`thor` mastheads specifically (they are single dominant
  long-runs; the year gate was added for *Vol. 3*-style collisions, which these
  alias targets don't have within the owned corpus). Scope this as a guarded
  sub-task: alias-without-year only for an allowlisted set, not globally.

**Cost / risk.** Low. Risk is a *wrong* alias entry causing a false
`in_collection` (suppressing a wanted buy). Mitigated by the existing convention
— "extend conservatively, only when the LOCG series name genuinely drops the
adjective, verified against the real catalog" (commands.py:1620-1624) — plus the
year gate on the buy path. The maintenance cost is the obvious one: it's a
manual list that grows by one entry per newly-discovered drift.

---

## 4. Pros / cons and recommendation

### Option A — Metron series-ID normalization

**Pros**
- Principled: collapses *all* spellings of a series, including ones nobody has
  hit yet, without a human curating a list.
- Reuses an integration that already returns `series_id`.

**Cons**
- `series_id` is stored **nowhere** today → import-pipeline change + backfill,
  not a patch.
- Adds a network dependency + rate-limit + ambiguity fallback to a previously
  offline import.
- Wish-list names lack the year Metron needs to disambiguate — weakest exactly
  where it'd matter.
- Worse failure mode (a mis-disambiguation = silent false `in_collection`).
- Doesn't retire the name-based matcher; layers on top of it.

### Option B — extend `_SERIES_ALIASES`

**Pros**
- One dict literal; reuses the existing year-gated retry. No storage, network,
  or migration.
- Fixes the ticket's case immediately and is trivially reviewable per entry.
- Same proven mechanism already shipped for Thor / Iron Man.

**Cons**
- Manual curation; one entry per discovered drift (long-tail never fully closed).
- Needs the small extra work to (a) handle the reversed direction and (b) let
  the year-blind conflicts audit use the alias for an allowlisted masthead set.

### Recommendation: **Option B.**

The bug class is narrow in practice — it's the handful of famous mastheads with a
"The X / Uncanny X / plain X" naming history, not a broad long tail. A curated,
year-gated alias table covers the real cases at near-zero cost and zero new
failure modes, and it's the same pattern the codebase already chose and
documented for this exact problem (BUI-46). Option A's appeal is theoretical
completeness, but it pays for that with a new network dependency on the offline
import, a backfill, and a *more dangerous* failure mode — and it can't even start
without first storing a `series_id` that doesn't exist on any row today. Reach
for A only if the alias table later proves it's growing without bound.

### Implementation sketch / follow-up ticket scope (Option B)

A single small fix-ticket (BUI-193 → implementation), roughly:

1. Add verified entries to `_SERIES_ALIASES` (commands.py:1625) for the known
   mastheads. **Verify each against the live store** via
   `GET /api/comics/collection/series-names` so the alias target is the *actual*
   catalog spelling; map the other spelling(s) to it. Start with
   `x-men → uncanny x-men` (the ticket case) and `incredible hulk → hulk`.
2. Let the **conflicts audit** benefit: allow `cmd_wish_list_conflicts` →
   `cmd_collection_check` to attempt the alias fallback for an allowlisted set
   even without a `year` (the year gate guards *Vol. 3* collisions, which these
   single-dominant-run mastheads don't have). Keep the global path year-gated.
3. Tests: a `_normalize_series_key` + alias unit test proving
   `The X-Men #137` (owned) is found by a `Uncanny X-Men #137` query, and a
   conflicts-audit test proving the same book is now reported as a conflict.
4. Drop a one-line note in `wishlist-add.md` / `collection-check.md` that these
   specific mastheads are now auto-reconciled (the Pattern-C flag stays as the
   catch-all for everything not in the table).

No migration, no schema change, no Metron import-time dependency.
