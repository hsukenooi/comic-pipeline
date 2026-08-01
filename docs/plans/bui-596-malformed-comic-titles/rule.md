# BUI-596 — the tightened classification rule

Phase 1 deliverable. Measured read-only against `~/.comics-server/db.sqlite` on
2026-08-01. **No write was made to the live DB.**

## The rule

> A `comics` row is **malformed** iff its own `issue` value appears in its own
> `title` as a `#<issue>` token:
>
> ```python
> re.search(rf'#\s*{re.escape(str(issue))}\b', title, re.IGNORECASE)
> ```

This is strictly tighter than the ticket's `title LIKE '%#%'` heuristic: it
requires the `#` to be a **duplication of the `issue` column**, not just any `#`.

### It matches exactly the same 173 rows as the heuristic

| set | count |
| -- | -- |
| `comics` total | 808 |
| heuristic: `title LIKE '%#%'` | 173 |
| **rule: `#<own issue>` in title** | **173** |
| in heuristic but not rule (would be carved out as legitimate) | **0** |
| in rule but not heuristic | 0 |

**There are zero legitimate `#`-in-title rows.** That is not an assumption — it
is proved by the two sets coinciding. Every one of the 173 has its own issue
number embedded after a `#`, with non-empty series text before it. No series in
this table has a canonical name containing an incidental `#`, and no row uses
`#` for a variant designation. The carve-out list is empty.

The rule is worth stating separately anyway, because it is the form that stays
correct as the table grows — a future `Kill Shakespeare: #1 Special` would be
caught by the heuristic and correctly spared by the rule.

## What the rule deliberately excludes

**Rows whose title is listing-derived but does not repeat the issue number.**
There are **12** of these, and the rule does not touch them:

```
201 'Avengers Bronze age 1st Squadron Supreme'            issue=85
202 'Avengers Bronze age Daredevil Fine+'                 issue=82
203 'Avengers Bronze age 1st Lethal Legion Story FVF'     issue=79
204 'Avengers Bronze age Vision Declares his love for Wanda' issue=81
314 'Avengers Silver age'                                 issue=10
315 'Avengers Silver age Black Panther Joins'             issue=52
316 'Avengers Silver age'                                 issue=54
328 'Hulk Annual Silver age Steranko Inhumans'            issue=1
339 'Iron Man Bronze age Layton Sub-mariner Fine'         issue=120
340 'Iron Man Bronze age'                                 issue=122
341 'Iron Man Bronze age Layton'                          issue=125
342 'Iron Man Bronze age Layton'                          issue=126
```

These are the same root cause (a listing title stored as identity) but they are
**not** what BUI-596 measured or scoped, and cleaning them needs a different
rule — one keyed on listing vocabulary (`Bronze age`, `Silver age`, grade
tokens) rather than on the issue number. That rule has real false-positive risk:
the same scan flags `DC Comics Presents` (ids 55, 56), a genuine series name,
purely because it contains "DC Comics". **Recommend a separate ticket**; do not
fold it into this remediation.

## Sub-classes within the 173

Classified by the **remainder** — what is left of the title after the
`#<issue>` token is removed.

| class | count | remainder | example |
| -- | --: | -- | -- |
| **A** doubled-issue-only | **99** | empty | `Thor #130` (issue 130) |
| **B** full listing title | **60** | grade / publisher / month / key notes | `Iron Man #126 (Marvel Comics September 1979) VF Condition!` |
| **C** variant designation | **8** | a cover-artist credit | `Absolute Flash #10 Nick Robles Cover` |
| **D** multi-issue lot | **6** | names several issues | `Amazing Spider-man #18,19,20,21,22 lot of 5 NM Gems Wow` |

The ticket predicted 2 classes and ~29 class-B rows. The real split is 4 classes
and **60** class-B rows. Classes C and D are new:

- **C** is a variant designation that belongs in the `variant` column, not the
  title. The plan does not move it there — that would change row identity on
  the `COALESCE(variant,'')` index and is a bigger change than this ticket.
- **D** is semantically broken in a way no title fix repairs: one row stands for
  five books. The `issue` column holds only the first.

Class A is the only class BUI-591's writer normalizer closes. **B, C and D
remain open at the write boundary** — BUI-591's comment says so explicitly for
B, and C/D were not identified at the time.

## The central hazard: rewriting collides with the clean twin

The two partial unique indexes

```
idx_comics_tiyv       ON comics(LOWER(title), issue, year, COALESCE(variant,'')) WHERE year IS NOT NULL
idx_comics_tiv_nullyear ON comics(LOWER(title), issue, COALESCE(variant,''))     WHERE year IS NULL
```

mean a rewrite can land on an occupied key. Measured:

| | count |
| -- | --: |
| would **hard-collide** with an existing row on rewrite | **78** (45%) |
| have a clean peer at the same (series, issue) but a different year | 51 |
| converge onto **each other** after rewrite (4 groups, 5 losers) | 5 |
| genuinely free to rewrite in place | **39** |

So **in-place rewrite is impossible for 134 of 173 rows.** The ticket's stated
preference — "prefer rewriting a malformed title in place over deleting" —
cannot be followed for the majority.

## The premise that inverts the recommendation

The ticket prefers rewrite over delete because *"a delete loses any FMV history
attached to that `comic_id`."* **That FMV history does not exist.**

| | malformed 173 | clean 635 |
| -- | --: | --: |
| attached `fmv` rows | 183 | 762 |
| of those, **empty** (`comps` 0/NULL, `low`+`high` NULL) | **181 (98.9%)** | 83 (10.9%) |
| carrying a `flag_reason` | 0 | 35 |

Only **two** of the 183 carry real data — `fmv` 543 on comic 498
(`Amazing Spider-Man #2`) and `fmv` 547 on comic 502 (`X-Men #1`). Both of those
comics are collision-free and are **rewritten, not deleted**, so nothing with
data is destroyed.

The head-to-head shows the mechanism. Comic 343 was written at `12:51:14` with
an empty FMV shell; its clean twin 311 already held real comps and was updated
at `12:52:43`, ninety seconds later:

```
MALFORMED 343 'Iron Man #126 (Marvel Comics September 1979) VF Condition!' 1979
    fmv g=8.0 low=None high=None comps=0
TWIN      311 'Iron Man' issue=126 year=1979
    fmv g=6.0 low=15.0 high=30.0 comps=11 conf=medium
    fmv g=8.0 low=15.0 high=20.0 comps=3  conf=low
```

These rows are **abandoned placeholders**, not lost history. The listing-title
write created a row, the sold-comps lookup under that title found nothing, and
the real computation then wrote under the normalized title. Deleting the
placeholder loses nothing.

## Resulting action distribution

| action | count | why |
| -- | --: | -- |
| **delete** | **134** | 78 collide with a clean twin · 51 have a clean peer at another year · 5 are convergence losers. All have 0 real FMV and 0 bid links. |
| **rewrite** | **39** | No collision, no clean peer. Includes the 2 rows carrying real FMV. |
| merge-into-`<id>` | **0** | Never needed — no row carrying real FMV collides, so no `fmv` re-pointing is required. The branch exists in `plan.py` and would fire if that changed. |

`ON DELETE CASCADE` removes **142 `fmv` rows**, of which **0** carry real comps.

### Convergence tiebreaker, and a premise correction

Four groups have two or more malformed rows rewriting onto the same identity —
e.g. 426 and 432 are both `THE MIGHTY THOR # 128`, at grades FN- and FN+. The
`comics` table is an identity table; grade belongs in `fmv`. One row is kept and
the rest deleted.

**Correction to the ticket:** it advises using `last_seen_in_export_at` rather
than `local_added_seq` to pick the current row of a duplicate pair. **Neither
column exists on `comics`** — both live on the LOCG collection JSON store, which
is a different data store entirely. `comics` has only `created_at`, and within
each of these bulk-written groups `created_at` is identical to the second, so it
cannot break the tie. The tiebreaker used is: **(1) the row carrying real FMV
data, (2) lowest `comics.id`** — deterministic and stable. Every row in every
convergent group has zero real FMV, so the choice is value-neutral.

## Safety facts, verified not assumed

- **`bid_links = 0` across all 173 — CONFIRMED.** Re-derived independently via
  `fmv JOIN bid_fmvs`. The ticket's claim holds. `remediate.py` re-checks it as
  a preflight gate and aborts if it is ever non-zero.
- Every statement is keyed on **`comics.id`** and asserts `rowcount == 1`. This
  is the BUI-500 lesson applied: that remediation keyed on `gixen_item_id`
  assuming uniqueness, and it was not unique.
- End-state simulation proves **0 unique-index violations** after the plan.
- Dry-run applied against a scratchpad copy of the live DB: 808 → 674 comics,
  945 → 803 fmv, `bid_fmvs` and `bids` untouched, `integrity_check` ok,
  `foreign_key_check` clean, 0 orphans, **0 malformed rows remaining**.

## False-positive risk

**For the rule itself: none identified.** The rule and the heuristic select the
same 173 rows, and every one demonstrably duplicates its own issue number.

**For the rewrite titles: moderate, and deliberately left in place.** The 39
rewrites take the text before the `#` verbatim, which preserves the source's
casing and masthead. That yields `THE MIGHTY THOR`, `WORLD'S FINEST`,
`Amazing Spider-man`, `X-men`, `Uncanny X-men` — correct, but not the canonical
spelling used elsewhere in the table (`The Mighty Thor`, `Amazing Spider-Man`,
`X-Men`). Because both unique indexes key on `LOWER(title)`, these do not
collide; they simply do not converge.

This is a conservative choice. Case-folding or masthead-aliasing to force
convergence would mean deciding that `THE MIGHTY THOR` #130 is the same book as
`Thor` #130 — a masthead-rename judgement (the BUI-581 class) that needs an
alias map. Guessing at it here would repeat the BUI-500 error in a new costume:
acting on an equivalence not proved. **14 yearless/yeared logical duplicate
groups remain after the plan** (down from 52 if every row were rewritten). They
are not index violations, they are inert, and `upsert_comic`'s existing
yearless-promotion reconciliation already handles that shape on the next write.

## Recommendation

**Proceed, but as delete-dominant rather than rewrite-dominant.** The ticket's
framing (rewrite preferred, delete as fallback) is inverted by the data: 98.9%
of the attached FMV is empty and 45% of rows cannot be rewritten at all. Deleting
134 inert placeholders and rewriting the 39 that are genuinely recoverable is
the smaller, safer change, and it is the one that leaves zero malformed rows.

Two things should be **separate tickets**, not folded in here:

1. the 12 listing-derived rows with no `#` (the rule's deliberate exclusion);
2. the class B/C/D write boundary, still open after BUI-591 — remediating now
   without closing it means these rows come back.
