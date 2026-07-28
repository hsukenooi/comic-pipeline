---
title: "A reopened ticket's premise may already be stale — verify it against the code before implementing"
date: 2026-07-20
last_updated: 2026-07-28
category: conventions
module: "general (Linear ticket handling, any package) — these batches: locg-cli, gixen-cli"
problem_type: convention
component: development_workflow
severity: medium
applies_when:
  - "Picking up a reopened Linear ticket, or a ticket filed as a review residual / follow-up from an earlier fix"
  - "A ticket's description asserts a specific root cause or names a specific fix ('add a YYYY-01-02 day', 'rename X to Y')"
  - "The ticket references code, a deployed label, or a data shape that may have changed since it was filed"
  - "A ticket cites a statistic or row count as evidence for its diagnosis"
  - "A ticket attributes a gap to a named component, or suggests a concrete optimisation ('cache X per Y')"
  - "A ticket describes a CLASS of data problem inferred from one or two named examples, and proposes a detector or guard gated on a predicate"
  - "A ticket's diagnosis depends on a correlation where the thing being measured is both a candidate cause and a consequence of the outcome"
tags:
  - process
  - linear
  - reopened-ticket
  - premise-verification
  - evidence-verification
  - bui-210
  - bui-459
  - bui-461
  - bui-464
  - bui-465
  - bui-470
  - bui-559
  - bui-562
related_docs:
  - "docs/solutions/design-patterns/guard-strictness-must-match-consequence.md"
---

# A reopened ticket's premise may already be stale — verify it against the code before implementing

## Context

Three tickets in the BUI-210/459/460/461/462 batch each specified a concrete fix. In all
three cases, implementing the fix as written would have been wrong — not because the fix
was poorly designed, but because the premise behind it no longer matched the code or the
deployed system. Two of the three would have shipped a knowingly-wrong change; the third
would have been redundant work re-fixing something already fixed. This doc captures the
discipline that caught all three: read the current code (and, where relevant, the current
deployed state) before implementing a ticket's specified fix, especially a reopened one.

## Guidance

**Treat "the ticket says do X" as a hypothesis, not an instruction, whenever the ticket
is a reopen, a review residual, or references a root cause by name.** The filer's mental
model of the code was accurate *at filing time*. A reopen exists precisely because
something didn't land the way it was expected to — which means the gap between the
ticket's model and the current code is the whole reason you're looking at it. Verify the
premise first; implement second.

### Example 1 — the premise was already false (BUI-210, part a)

BUI-210's reopen asked record-win to stop stamping a `{year}-01-01` placeholder date on a
Metron miss, on the stated premise that the placeholder is what ships rows to LOCG
dateless. Reading `_row_to_csv_dict` in `collection_io.py` shows the export already blanks
any placeholder via `_is_placeholder_release_date` before it's written — a placeholder row
and a genuinely dateless row produce the identical empty CSV cell. There was no export bug
behind the premise. Worse, reading the reconcile path shows removing the placeholder
*creates* a bug: the year is the only discriminator `_reconcile_score` has for two
undecorated volumes of the same masthead, so a dateless win would fail open into a
wrong-volume match and get silently auto-healed away (see the sibling doc,
`guard-strictness-must-match-consequence.md`, pattern 1). This one was implemented,
reviewed, and reverted — the review is what caught it, but reading the export code first
would have caught it before any implementation time was spent.

### Example 2 — the fix would have fabricated data to route around a check that no longer applies (BUI-461)

BUI-461's ticket proposed writing a fabricated `YYYY-01-02` day (instead of the real
`01-01`) onto backfilled placeholder rows, reasoning that this would dodge
`_is_placeholder_release_date`'s regex and let a genuine January date reach the export.
Reading `_is_placeholder_release_date` shows it is **not** a shape check — it already
requires both `source == "agent_win"` **and** `metron_id is None`:

```python
def _is_placeholder_release_date(row: dict[str, Any]) -> bool:
    """True only for a BUI-105 placeholder date, detected by INTENT not shape.
    [...]
    """
    if row.get("source") != "agent_win":
        return False
    if row.get("metron_id") is not None:
        return False
    return bool(_PLACEHOLDER_DATE_RE.match(str(row.get("release_date") or "")))
```

Carrying the resolved `metron_id` onto a backfilled row is what already makes a genuine
`YYYY-01-01` cover date survive to the export — no fabricated day required. Implementing
the ticket as written would have shipped a knowingly-wrong day into a real dataset to work
around a check that had been intent-based (not shape-based) since a fix a month earlier.

### Example 3 — the ticket's target state was ahead of what's actually deployed (BUI-459)

BUI-459's ticket named the post-BUI-220-rename identifiers (`com.comics.server` label,
`~/.comics-server` data dir) as the correct values for `install.sh`. Checking the live
Mac Mini (not just the docs) showed the rename had only ever been done in
documentation (BUI-425) — at that time the running LaunchAgent was PID-confirmed
`com.gixen.server` against `~/.gixen-server`. Implementing the ticket's specified label
as written would have made a routine re-deploy bootstrap a same-labeled job that hijacks
the real server (see the sibling doc's pattern 5 for the mechanism:
`resolve_server_dir()` prefers the new directory the instant it exists, and
`install.sh` creates it via `mkdir -p`). The correct fix was a revert to match deployed
reality, with a comment explaining that this is deliberate, not drift — not the rename
the ticket asked for.

> **Since resolved (BUI-463, 2026-07-20).** The migration was subsequently performed
> deliberately: the Mini now runs `com.comics.server` from `~/.comics-server`, and
> `install.sh` was moved forward to match. The lesson is unchanged and the sequencing is
> the point — the ticket's target state was *eventually* correct, just not yet true when
> it was filed. "Right eventually" and "right now" are different claims, and a deploy
> script must encode the second.

## The second failure mode: the premise is *partly* true, but mis-attributed

The three examples above are all "the specified fix would be wrong." A later batch
(BUI-463..471) surfaced a distinct and subtler mode: the ticket describes a **real**
problem, but its *evidence*, its *attribution*, or its *suggested direction* is wrong.
These do not announce themselves — the ticket reads as coherent, and the fix it asks for
looks reasonable. Only checking the claim against real data separates them.

### Example 4 — the cited statistic was not evidence (BUI-465)

BUI-465 claimed a whole-batch Metron breaker latch, citing both "58 of 78 rows carry a
placeholder date" and "77 of 78 rows had a null publisher." The headline claim was
correct — but **the publisher figure was not evidence for it.** BUI-458 added the
publisher fetch *after* nearly every one of those rows was written; the nulls meant
nothing had fetched a publisher, not that a fetch had failed.

The real evidence came from replaying the actual store backup and grouping rows by the
run that wrote them: 40 of the 58 placeholders came from a single 41-row run, monotone
after row 1 (row 1 carried a `metron_id`; rows 2–41 carried none). That ordering *is* the
latch signature. The other 18 sat in runs whose good and bad rows interleaved — those runs
never latched, and were correctly excluded from the fix.

**A ticket citing two numbers is not citing two independent confirmations.** Check when
each quantity started being recorded before treating it as evidence of anything.

### Example 5 — the gap was real but lived in a different component (BUI-470)

BUI-470 asserted that a newsstand/variant distinction is "invisible to the reconciler."
The described gap is real — but it belongs to record-win's *coarse* `(series, issue)`-keyed
`owned_index` lookup, which BUI-267 had already fixed. The reconciler's own collision key
is **finer**: `make_identity` carries the raw `full_title`, and `_reconcile_score` requires
an identical trailing issue token with nothing after it, or an exact case-insensitive
`full_title` match, before a row is even a heal candidate. Both already force suffix
agreement, so the failure mode was structurally unreachable there.

The right response was neither to skip the work nor to fake a passing end-to-end test: the
unification shipped as genuine defense-in-depth, covered by a **direct unit test** of the
new predicate rather than an end-to-end test that would have implied a live bug that does
not exist. **Where a test lives is itself a claim about where the bug is.**

### Example 6 — two of three premises were dead, killed by fixes days earlier (BUI-464)

BUI-464 asserted that a null identify year (a) falls through to the newest Metron volume
and (b) is not gated by `needs_review`. Both were false. `_disambiguate_series` returns
`None` on multiple candidates with no year, and **BUI-421 Fix A** had already removed the
last-writer index guess; **BUI-422**, merged *two days before the ticket was filed*, added
the null-year review gate.

Most instructive: the ticket's three named examples (FF #16, ASM #89, Batman #240) were
in the store **correctly resolved, carrying real years**. They were a different bug class
— BUI-465's placeholder rows — conflated with the one being reported. A ticket naming
specific records as proof is making a checkable claim; check those records.

### The corollary: find the independent evidence, don't drop the guard

When a premise turns out to be stale, the tempting fix is to relax whatever is blocking
progress. BUI-464 is the counter-example worth copying. The ticket admitted a null year
leaves "no era guard at all" and demanded era evidence "from somewhere else" — and it
already existed: when a win resolves through `series_name_index`, the LOCG canonical name
carries the volume's publication window (`"The X-Men (Vol. 1) (1963 - 1981)"`), which is
independent of the Metron hit being judged.

The anti-pattern it avoided is the sharper lesson. Metron's own `format_series_name`
also yields a range — but it derives from **the very hit under judgement**, so gating the
candidate against it would always pass. That is a *tautological guard*: it has the shape
of a check, passes review, and validates nothing. When adding a guard, name the source of
its evidence and confirm that source is independent of the thing being checked.

### And: a real problem can carry an unimplementable suggestion

BUI-465 suggested "cache the detail fetch per series so a multi-issue run of one series
costs one call." The problem was real, but the suggestion cannot work: `lookup_issue_detail`
is keyed by per-issue `metron_id`, so a run of issues from one series has all-distinct ids
and such a cache gets **zero** hits. The genuine saving is elsewhere — `lookup_issue`'s
`series_list` half *is* per-series and reusable (filed as BUI-473).

Record *why* a suggested direction was rejected. Otherwise the next agent reads the same
plausible sentence and re-attempts it.

## The third failure mode: the premise names code that does not exist there

Examples 1–3 are "the specified fix would be wrong"; 4–6 are "the problem is real but
mis-attributed." A later batch (BUI-472..476) surfaced the sharpest version yet: a ticket
whose named code target **is not where the ticket says, and in one case does not exist at
all in the named package.** These fail a `grep` in seconds — but only if you run it before
you start implementing rather than after.

### Example 7 — the premise named the wrong package entirely (BUI-475)

BUI-475 asked to change `needs_review` gating in `_build_win_row`
(`packages/locg-cli/src/locg/commands.py`), replacing BUI-422's `$25` price threshold with
an era-evidence gate keyed on `index_series_range`. Three independent facts, each found by
reading rather than trusting:

1. **The `$25` gate is not in `_build_win_row`, and not in `locg-cli` at all.**
   `MISSING_YEAR_PRICE_THRESHOLD` / `REASON_MISSING_YEAR` lived in
   `packages/gixen-cli/record_win_prep.py`. `grep -r needs_review packages/locg-cli/src`
   returned **nothing** — the concept the ticket said to edit did not exist in the named
   package.
2. **The two signals sit on opposite sides of an HTTP boundary.** `record_win_prep` runs
   client-side *before* the POST; `index_series_range` is computed server-side *inside*
   `cmd_collection_record_win`. The correlation the ticket wanted cannot happen at one site
   because the two quantities never coexist in one process.
3. The ticket's *intent* was sound and the risk was real — but building it requires a design
   decision (a new endpoint vs a server-side hold that changes the record-win contract) the
   ticket never made.

The right move was a no-code **stop-and-report**: re-ground the ticket in the actual code,
record the two viable designs, and escalate the choice — not ship a speculative
cross-package change against a premise that named a symbol that isn't there. A disciplined
stop is a success, not a failure. **The one-command check** — `grep` for the named symbol
in the named package — would have flagged this before any design time was spent.

> **Case study, not current code (BUI-475, shipped 2026-07-21).** The escalated Option A —
> a server-side era-evidence endpoint over `resolve_series_for_win`/`series_year_range` — was
> then built and proven to **fail open**: a null-year win with no competing same-title
> volume in the collection auto-recorded under the sole owned (and possibly wrong-era)
> volume, reproducing the exact BUI-421 mis-file BUI-422's price gate was meant to prevent.
> The owner chose the safe fallback instead: `MISSING_YEAR_PRICE_THRESHOLD` and the `$25`
> gate were removed outright, and every null-year win now holds for review
> unconditionally, regardless of price (`REASON_MISSING_YEAR` is the only symbol that
> survives, still in `packages/gixen-cli/record_win_prep.py`). The `MISSING_YEAR_PRICE_THRESHOLD`
> symbol named above no longer exists anywhere in the codebase — treat this example as a
> record of the reasoning that led to that outcome, not as a description of the gate as it
> stands today. A safe auto-record path for a *resolved* null-year win is tracked separately
> as future work (BUI-498, gated on the issue's own Metron cover year).

### Example 8 — the hypothesis was right, its named mechanism never executed, and the implied fix was backwards (BUI-474)

BUI-474 hypothesised "Metron series ambiguity" and named two specific defects:
`_disambiguate_series` "blindly trusts a sole name-search hit," and `lookup_issue` takes
`issues_list()[0]` "unfiltered." Replaying the 18 failing rows against live Metron showed
the hypothesis was correct **in substance** and wrong **in mechanism**: not one of the two
named defects executed. Every row died earlier, at `_disambiguate_series` returning `None`
over an over-permissive candidate set — Metron's substring search returns 433 series for
`"Batman"`, and `year_end is None` was read as "ongoing," so the year window could never
narrow them. The `len == 1` sole-hit branch never fired (smallest candidate set was 32),
and `issues_list` was never reached.

This inverts the fix direction. The named defects imply "trust less at the point of
selection"; the actual bug is over-permissiveness at the point of *candidate admission*,
and it produces **misses (placeholder dates), not wrong writes**. The shipped fix (BUI-485)
adds a name-exactness pre-filter that can only narrow *toward* `None` — the opposite of
tightening a trusted pick. A literal executor who "tightened the sole-hit branch" as the
ticket implied would have hardened a code path that never runs, and left the real one
untouched.

**When a diagnostic ticket proposes a fix direction, measure which code path actually
executes on the failing data before building.** A confirmed hypothesis is not a confirmed
mechanism, and the fix for a *miss* (widen/redirect) is the reverse of the fix for a
*wrong write* (tighten) — see `guard-strictness-must-match-consequence.md`.

## The fourth failure mode: the premise names a data class that does not exist

Examples 1–3 are "the specified fix would be wrong"; 4–6 are "the problem is real but
mis-attributed"; 7–8 are "the named code isn't there." The BUI-557..562 batch surfaced a
fourth: the ticket names a **class of data problem**, the code target is real, the fix is
implementable — and the predicate it proposes matches **zero rows in production**. Nothing
fails. Tests pass, review passes, CI is green, and the change is inert.

### Example 9 — the gate would have matched zero rows (BUI-559)

BUI-559 asked for a third tolerant merge pass in `collection_io.py` to catch a
`publisher_name` "drift" class, gated on identical `(series_name, full_title,
release_date)` with only the publisher differing. The two named identities were real. The
code target was real. The design was implementable.

But the pairs **never share a `release_date`.** Measured against the live store, the Panini
rows trail their DC twins by a monotone 147–211 days:

| full_title | Panini | DC | delta |
|---|---|---|---|
| Absolute Flash #10 | 2026-06-04 | 2025-12-17 | 169d |
| Absolute Green Lantern #8 | 2026-04-01 | 2025-11-05 | 147d |
| Absolute Green Lantern #9 | 2026-05-07 | 2025-12-03 | 155d |

A gate requiring an identical date matches **nothing**. The "publisher drift" class does
not exist — these are Italian **licensed editions**, a different book with a different
release, generated by our own record-win push resolving onto the foreign edition
(fingerprinted by `price_paid` / `gixen_item_id` present on exactly the generated rows and
absent on the genuinely-foreign ones).

**The inert change would have been worse than no change.** It would have left a comment
block asserting the drift class was now handled — so the next reader stops looking, and the
real generator (filed as BUI-563/BUI-564) stays invisible behind a guard that never fires.
This is the *tautological guard* of Example 6 in a new costume: it has the shape of a
check, passes review, and validates nothing.

**A ticket describing a data *class* has usually inferred that class from one or two
examples. Count how many live rows the proposed predicate would actually match before
building machinery for it.** The check costs minutes and can invalidate the whole ticket.

### Example 10 — the raw evidence inverts under the right conditioning (BUI-562)

BUI-562 reported the Gixen sync failing ~2/3 of attempts and asked for a shorter backoff.
Two premise problems, one cosmetic and one decisive.

The cosmetic one: the ticket described a flat 1200s backoff. The loop was **already**
exponential — the real defect was an off-by-one (`2 ** consecutive_failures` with the
exponent starting at 1) plus the base being `SYNC_INTERVAL`, so it could never back off
under 20 minutes. Same fix direction, wrong diagnosis.

The decisive one: **whether a shorter retry is safe or harmful depends entirely on whether
Gixen is rate-limiting us**, and the raw log reads like it is — failures cluster densely.
But failure *causes* retries, so density is a consequence, not a cause. Conditioning on
**the previous attempt having succeeded** removes the confound and inverts the signal:

| gap since last request | failure rate |
|---|---|
| 30–60s | 4.7% (n=149) |
| ~600s (normal cadence) | 9.0% |
| >1 hour | 53.8% (n=39) |

An inverted dose-response — failures get *less* likely the more often we poll. A flapping
host, not a throttle. Shipping the shorter retry against the unconditioned reading would
have been a coin-flip that happened to land right; the conditioning is what made it a
decision. **When request rate is both a candidate cause and a consequence of the outcome,
condition on the prior attempt succeeding before reading any correlation.**

## Why This Matters

- **A reopened ticket or a review residual is exactly where the filer's model is most
  likely out of date.** The first pass already changed the code once; the ticket
  describing "what's still wrong" was written against a snapshot that a later commit
  (elsewhere in the same area) may have already moved past. BUI-210's part (c) had been
  fixed a full month before the reopen (commit `9384176`, BUI-199 finding 5) — nobody
  re-checked before re-filing it as still-broken.
- **Two of these three would have shipped knowingly-wrong data or a dangerous deploy if
  implemented literally** — a fabricated date (BUI-461) and a script change that hijacks
  a live server's database (BUI-459). Neither failure would have been caught by tests
  written against the ticket's own stated premise, because the tests would have encoded
  the same wrong assumption.
- **Verifying the premise is cheap; shipping the wrong fix is not.** In every case here,
  the check was a few minutes of reading the relevant function or `launchctl list` output
  — far cheaper than implementing, testing, and later reverting (as literally happened
  with BUI-210 part a), or debugging a hijacked production database.

## When to Apply

Before implementing any ticket that:

- Is a reopen, or references an earlier fix by BUI number as "still not done."
- States a specific root cause in its description (verify the root cause against the
  current code, not just the symptom).
- Specifies the concrete fix rather than just the symptom (e.g. "add field X," "rename Y
  to Z," "remove the placeholder") — implement the *fix that fits the current code*, and
  if that differs from the ticket's specified fix, say so explicitly rather than silently
  ship the requested change.
- References a deployed name, path, label, or configuration value — check the actual
  deployed state, not just the docs describing it (docs can be ahead of, or behind,
  reality; see BUI-459).

## Examples

| Ticket | Stated premise | What the code/deploy actually showed | Outcome |
|---|---|---|---|
| BUI-210 (a) | Placeholder date ships rows dateless to LOCG | Export already blanks it; removing it deletes wins via a reconcile fail-open | Declined, reverted, `DO NOT REMOVE` comment added |
| BUI-210 (c) | A guard from an earlier finding is still unfixed | Already fixed a month earlier (commit `9384176`, BUI-199 finding 5) | No-op, documented as already-fixed rather than re-implemented |
| BUI-461 | Need a fabricated `YYYY-01-02` day to survive the placeholder check | `_is_placeholder_release_date` is intent-based (`metron_id is None`), not shape-based | Not implemented; real `metron_id` alone suffices |
| BUI-459 | `install.sh` should use the post-rename `com.comics.server` label | Live Mac Mini had not yet had the BUI-220 rename performed | Reverted to match deployed reality; migration later done deliberately in BUI-463 |
| BUI-465 | "77/78 rows had a null publisher" proves the breaker latched | BUI-458 added the publisher fetch *after* those rows were written — not evidence | Claim upheld on different evidence (run-grouped placeholder ordering); figure corrected |
| BUI-470 | A variant/newsstand distinction is invisible to the reconciler | Reconciler's key is `full_title`-exact; the gap was record-win's coarser lookup, fixed in BUI-267 | Shipped as defense-in-depth with a *unit* test, not a misleading end-to-end one |
| BUI-464 | Null year → newest volume; `needs_review` doesn't gate it | Both killed by BUI-421 Fix A and BUI-422; the named examples were correctly resolved | Only the third premise implemented; evidence sourced from the LOCG series window |
| BUI-475 | Edit `needs_review`/`$25` gate in `_build_win_row` (`locg-cli`) | Gate lived in `gixen-cli/record_win_prep.py`; `needs_review` grep-absent from `locg-cli/src`; signals span an HTTP boundary | Stopped, re-grounded, design choice escalated; the escalated option (server-side era-evidence endpoint) was then built and shown to fail open, so BUI-475 shipped instead as an unconditional null-year hold with the `$25` gate removed entirely |
| BUI-474 | `_disambiguate_series` trusts a sole hit; `issues_list()[0]` unfiltered | Neither path executed; all 18 died at `_disambiguate_series` returning None over a 433-wide candidate set — a *miss*, not a wrong write | Fix direction inverted: name-exactness pre-filter that narrows toward None (BUI-485) |
| BUI-559 | A `publisher_name` drift class needs a third tolerant merge pass | The pairs never share a `release_date` (147–211d apart, monotone) — the proposed gate matches **zero** rows; they are licensed editions, not a relabel | No-fix stop-and-report; anticipatory comment replaced with the measured finding + pinning tests; real generator filed as BUI-563/564 |
| BUI-562 | Sync backs off a flat 1200s; shorten it | Loop was already exponential (off-by-one + `SYNC_INTERVAL` base). Raw log reads as rate-limiting; conditioned on prior success it inverts — 4.7% failures at 30–60s vs 53.8% at >1hr | Shipped, but only after the conditioning proved a flapping host — the unconditioned reading would have made the fix harmful |

## Practical checklist

When the ticket is a reopen or a review residual, before writing code:

1. **Grep for the named symbol in the named package first** — before reading, before
   designing. A ticket can name the wrong file or the wrong package outright (BUI-475:
   `needs_review` was grep-absent from the package the ticket said to edit it in). One
   command decides whether the premise is even locatable.
2. **Re-read the named function**, not the ticket's paraphrase of it.
3. **Check when each cited quantity started being recorded** before treating it as evidence.
4. **Look up the specific records a ticket names as proof** — they are a checkable claim.
5. **When a ticket names the mechanism, measure which code path actually executes** on the
   failing data — a confirmed hypothesis is not a confirmed mechanism (BUI-474), and the fix
   for a *miss* is the reverse of the fix for a *wrong write*.
5b. **Count how many live rows the proposed predicate would match, before building it.**
   A ticket describing a data *class* usually inferred it from one or two examples
   (BUI-559: the gate would have matched zero rows). An inert guard is worse than none — it
   leaves a comment claiming the class is handled, so nobody looks again.
5c. **If request rate is both a candidate cause and a consequence, condition on the prior
   attempt succeeding** before reading any correlation (BUI-562: the raw log looks like
   rate-limiting; conditioned, it inverts to a flapping host).
6. **Search for fixes merged since the filing date** in the same area (`git log --since`).
7. **For any deployed name, path, or label, check the live system**, not the docs.
8. **If you add a guard, name its evidence source** and confirm it is independent of the
   thing being guarded.
9. **When the premise is wrong, say so in the PR and the ticket** — and put the test where
   the bug actually is, not where the ticket said it was. A well-reasoned no-code
   stop-and-report is a successful outcome, not a failure to deliver.
