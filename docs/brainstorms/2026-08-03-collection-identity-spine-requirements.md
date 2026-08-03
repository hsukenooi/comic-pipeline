---
date: 2026-08-03
topic: collection-identity-spine
---

# Collection Identity Spine + Quarantine (BUI-611)

## Summary

Give the collection store two things it does not have: a **quarantine state** — the missing
third state between full-citizen row and unsafe-delete — and a **versioned authority table**
that holds provider naming facts as data instead of as normalizer patches. Quarantine ships
first and alone. The authority table follows, with two disjoint entry kinds so the matcher
and identity normalizations keep their opposite pressures and never share a function. Whether
identity ever re-keys onto `metron_id` is decided by a measurement, not by this document.

---

## Problem Frame

Every identity incident in this store has been closed the same way: a patch to a normalizer,
a branch, a test, a falsification risk, and a doc. BUI-546 (punctuation), BUI-554 (end-year
fold), BUI-560 (bare year), BUI-561 (stale persisted index), BUI-556 (60-row cleanup),
BUI-563/564 (foreign editions) — six tickets in one week, all one class: *the provider
renamed something and our key moved with it*. Two of that run's proposals were falsified on
measurement (BUI-559, BUI-574), which is the honest cost of fixing naming facts with regexes.

Two structural gaps sit underneath:

**There is no way to say "this row is real but must not answer questions."** The six Panini
rows measured below are owned twice, created by our own record-win push. They cannot be
deleted (LOCG re-emits them on the next export) and their ownership cannot be cleared
(that runs the BUI-122 `In Collection=0` data-loss path). BUI-563 shipped an advisory
counter precisely because there was no third option. The `bids` table solved the same
problem years earlier with a tombstone; the collection store never got one.

**Naming facts live in code.** `_MASTHEAD_ALIAS_PAIRS` is a five-entry Python tuple. Adding
"the provider renamed this run" is a code ticket, so the marginal cost of recording a naming
fact is a full development cycle — and facts that are expensive to record do not get
recorded.

### Measured, live store (2026-08-03, `~/.comics-server/collection-store`)

| Fact | Value |
|---|---|
| Rows | 2843 — **all** `locg_export`; 2191 owned, 653 wish-flagged |
| Rows carrying `metron_id` | **11 (0.4%)** |
| `owned_duplicate_identities` (hard stop) | 0 |
| Cross-edition twins (BUI-563 advisory) | **6** — Panini editions of Absolute Flash #10, Absolute Green Lantern #8/#9, Spider-Man #13, The X-Men #59/#118 |
| Identity tuples held by >1 row | **3** — all `Absolute Martian Manhunter`, all wish-side |
| Series on `(YYYY - Present)` | 52 of 353, covering 496 rows |
| Normalized keys covering >1 raw series name | 28 — every one a legitimate multi-volume masthead |

Two of these reframe the ideation's premise:

- **`metron_id` is threaded through five modules and populated on 0.4% of rows.** The import
  hard-sets `metron_id = None` on every new row (`collection_io.py:1866`); it survives only
  on rows the import *matches*, as local-only win provenance. "Re-key identity to
  `metron_id`" is therefore a ~2832-row Metron backfill wearing a schema change's clothing,
  and nobody has measured what fraction of those rows Metron can resolve.
- **The 3 duplicate identity tuples are a new defect class, not leftovers.** Both spellings
  `(2025 - Present)` and `(2025 - 2026)` fold to the same key under today's
  `identity_series_key` — so these rows *collide right now*. BUI-556's cleanup was scoped to
  owned rows; these are wish-side and survived it. A fold shipped without a re-key sweep
  leaves colliding rows behind, and nothing in the store can currently notice: `import`
  assigns `identity_to_idx[identity] = index`, so of two colliding rows only the last is
  ever reachable again. The other can never be updated, only duplicated.

---

## Key Decisions

- **Quarantine ships first, alone, and is useful on its own.** It needs no alias table, no
  `metron_id`, and no schema redesign — a per-row state plus one pool seam. It is also the
  only part of this project with rows waiting for it today.

- **One matchable-rows seam, not a filter per call site.** Nine functions read
  `payload["comics"]` as a matcher pool. Adding a `quarantined` check to each is the
  duplicated-predicate shape this repo has already been bitten by; a single
  `matchable_rows()` (the `owned_match_keys` pattern — one source of an equivalence, many
  consumers) is the only version where a new pool cannot silently forget.

- **Safety direction is per surface, and asymmetric on purpose.** Quarantine may make the
  buy path *more* cautious (a quarantined row stops answering "owned", so a book might be
  re-bought). It may never make the export *more* aggressive. The owned-safe export index
  and `wish_rows_for_export` therefore keep quarantined rows — the enforcement layer is
  untouched by construction, not by discipline. The re-buy risk is closed by a hard refusal
  to quarantine the last non-quarantined owned row for a book.

- **The authority table is data, but git-versioned data.** A schema-versioned JSON file
  shipped in `locg-cli`, loaded at import, validated in CI. An identity incident becomes a
  small reviewed PR that touches no matcher logic — not a normalizer patch, and not an
  unreviewed edit on the Mini that can drift from the repo on a data-loss-adjacent surface.

- **Two entry kinds, two readers, one file — the doctrine survives.** Matcher normalization
  widens so eBay's spelling can *meet* the catalog's; identity normalization narrows so
  distinct books stay distinct. They must never share a function, and they don't:
  - `alias` entries are **symmetric** assertions that two series names name the same run.
    Read only by `owned_match_keys`. Widening.
  - `relabel` entries are **directed** assertions that the provider rewrote string A into
    string B for the same volume. Read only by `identity_series_key`. Narrowing, and
    incapable of over-folding — an entry names exactly two strings.

  The table is an authority record, not a normalizer. Generative rules (the end-year fold,
  the bare-year fold) stay in code, because they are rules; one-off provider rewrites become
  entries, because they are facts.

- **Every identity-side change owes a re-key sweep, and the store must be able to notice
  when one is missing.** The 3 live collisions exist because a fold shipped without one. A
  post-import `identity_collisions` count — identity tuples held by more than one row —
  makes the omission self-announcing. It reports 3 today, which is the correct first proof
  that it can fail.

- **`metron_id` re-keying is gated on a measurement, not planned on faith.** A spike
  measures resolvability and cost over the live store and reports; the re-key is designed
  only if the number supports it. This is the BUI-629 rule (measure the oracle bound before
  designing the mechanism) applied to a data migration.

---

## Requirements

**Quarantine**

- R1. A collection row can carry a durable `quarantined` state recording who set it, when,
  why, and under which ticket. Absence of the field means not quarantined.
- R2. A quarantined row is excluded from every matcher candidate pool: the ownership match,
  the wish match, cross-volume candidates, the printing probe, the record-win series index,
  volume candidates, publisher scoping, and the conflicts audit.
- R3. A quarantined row remains present in the store and remains a match target for import,
  so the next LOCG export updates it instead of inserting a twin.
- R4. A quarantined row is never emitted in the pending-push CSV — the same exclusion the
  `needs_manual_*` flags already carry, for the same reason.
- R5. A quarantined row stays in the owned-safe export index and in `wish_rows_for_export`'s
  ownership tests. The BUI-122 enforcement layer is not modified by this project.
- R6. Quarantining the last non-quarantined owned row for a (series-equivalence, issue) pair
  is refused. An explicit override exists and records its reason.
- R7. Quarantine counts surface in the import summary and in `collection status`; a
  quarantined row never silently vanishes from a count it used to appear in.
- R8. Quarantine is reversible, and the reversal is audited like the application.

**Authority table**

- R9. A schema-versioned JSON authority table ships with `locg-cli`, holding `alias` and
  `relabel` entries, each carrying evidence (a ticket or an observation) and a date.
- R10. `alias` entries are read only by the matcher path; `relabel` entries are read only by
  the identity path. Neither reader can see the other's entries.
- R11. Every table key is derived by running the stored display string through the relevant
  normalizer at load time, never hand-normalized, so a normalizer change cannot silently
  orphan an entry.
- R12. A validator rejects a malformed table at load and in CI: unknown kind, missing
  evidence, a `relabel` whose `from` and `to` normalize identically, an `alias` pair that is
  already equal, and any entry that would merge two series the live store holds as distinct
  volumes.
- R13. The five existing `_MASTHEAD_ALIAS_PAIRS` move into the table with no behavior change,
  proven by the existing alias tests passing unmodified.

**Identity integrity**

- R14. The import reports `identity_collisions`: the number of identity tuples held by more
  than one row. Advisory, never a sync blocker.
- R15. Applying a `relabel` entry (or any change to a generative fold) is accompanied by a
  re-key sweep that merges rows the change causes to collide, using the documented survivor
  rules: newest `last_seen_in_export_at` wins, never `local_added_seq`; fold fields the
  survivor lacks; never sum copy counts without positive evidence of a second copy.

**Remediation**

- R16. The 6 measured cross-edition twins are quarantined on the live store via
  backup → apply → diff → row-count, and the cross-edition advisory count reaches 0 because
  the rows were dispositioned, not because the check stopped looking.
- R17. The 3 Absolute Martian Manhunter identity collisions are merged, and
  `identity_collisions` reaches 0 for the same reason.

**Measurement**

- R18. A read-only spike measures, over the live store: what fraction of rows Metron can
  resolve to an issue id, the disagreement rate against existing evidence, wall-clock and
  request cost, and the failure modes. It reports; it does not migrate.

---

## Acceptance Examples

- **AE1.** Given the Panini `X-Men #118` row is quarantined and the Marvel `The X-Men #118`
  row is not, when `collection check` is called for X-Men #118, it returns `in_collection`
  and names the Marvel row.
- **AE2.** Given a book whose only owned row is quarantined, when `collection check` is
  called for it, it does not report `in_collection` — and the quarantine that produced that
  state was refused unless explicitly overridden with a recorded reason.
- **AE3.** Given a quarantined row, when a LOCG export containing that book is imported, the
  row is updated in place, no new row is inserted, and it remains quarantined.
- **AE4.** Given a quarantined owned row and a wish-list entry for the same book, when the
  export CSV is generated, the wish is **not** emitted with `In Collection=0`.
- **AE5.** Given the live store today, when the import runs, `identity_collisions` reports 3
  and names the colliding titles.
- **AE6.** Given the authority table with the X-Men alias moved out of Python, when the
  BUI-197 alias tests run unmodified, they pass.
- **AE7.** Given a `relabel` entry whose `from` and `to` already normalize to the same
  identity key, when the table loads, validation fails loudly rather than accepting a no-op.
- **AE8.** Given a proposed `alias` entry that would merge two series the live store holds as
  separate volumes, when validation runs, it fails and names both volumes.

---

## Scope Boundaries

**Not touched:**

- The owned-safe export enforcement layer (`_owned_series_issue_index`,
  `wish_rows_for_export`, `owned_match_keys`' role in them). Safety lives in exactly one
  layer and this project does not scope, condition, or relocate it.
- `metron_id` re-keying. Measured, then decided.
- The overlay's SQLite `comics` table (the BUI-591/596/600/626 title surface). A second
  identity surface with different consumers and its own remediation history; noted as
  follow-up.
- The `bids` tombstone, status classification, and anything on the money path.
- The generative folds themselves — `identity_series_key`'s end-year and bare-year rules stay
  as code and stay as they are.
- Bulk removal of wish-list conflicts (a documented dead end).

**Deferred:**

- A store-local authority overlay editable on the Mini without a deploy. Revisit only if
  incident latency proves to be the binding constraint.
- Auto-proposing authority entries from import diffs.
- Quarantine as an input to FMV or bid policy.

---

## Open Questions Carried into the Plan

- What exactly the re-key sweep does with a collision where both rows carry conflicting
  purchase provenance — abort, or merge and flag? (R15 names the survivor rule; the conflict
  case needs a decision at implementation.)
- Whether `identity_collisions` should ever become a hard stop after it reaches 0. BUI-563's
  lesson argues no while a class has no local remedy; it may argue yes once every known class
  does.
