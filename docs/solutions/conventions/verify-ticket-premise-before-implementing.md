---
title: "A reopened ticket's premise may already be stale — verify it against the code before implementing"
date: 2026-07-20
last_updated: 2026-07-21
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
   `MISSING_YEAR_PRICE_THRESHOLD` / `REASON_MISSING_YEAR` live in
   `packages/gixen-cli/record_win_prep.py`. `grep -r needs_review packages/locg-cli/src`
   returns **nothing** — the concept the ticket said to edit does not exist in the named
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
| BUI-475 | Edit `needs_review`/`$25` gate in `_build_win_row` (`locg-cli`) | Gate lives in `gixen-cli/record_win_prep.py`; `needs_review` grep-absent from `locg-cli/src`; signals span an HTTP boundary | Stopped, re-grounded, design choice escalated — no speculative code |
| BUI-474 | `_disambiguate_series` trusts a sole hit; `issues_list()[0]` unfiltered | Neither path executed; all 18 died at `_disambiguate_series` returning None over a 433-wide candidate set — a *miss*, not a wrong write | Fix direction inverted: name-exactness pre-filter that narrows toward None (BUI-485) |

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
6. **Search for fixes merged since the filing date** in the same area (`git log --since`).
7. **For any deployed name, path, or label, check the live system**, not the docs.
8. **If you add a guard, name its evidence source** and confirm it is independent of the
   thing being guarded.
9. **When the premise is wrong, say so in the PR and the ticket** — and put the test where
   the bug actually is, not where the ticket said it was. A well-reasoned no-code
   stop-and-report is a successful outcome, not a failure to deliver.
