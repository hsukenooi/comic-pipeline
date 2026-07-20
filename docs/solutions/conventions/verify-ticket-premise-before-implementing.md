---
title: "A reopened ticket's premise may already be stale — verify it against the code before implementing"
date: 2026-07-20
category: conventions
module: "general (Linear ticket handling, any package) — this batch: locg-cli, gixen-cli"
problem_type: convention
component: development_workflow
severity: medium
applies_when:
  - "Picking up a reopened Linear ticket, or a ticket filed as a review residual / follow-up from an earlier fix"
  - "A ticket's description asserts a specific root cause or names a specific fix ('add a YYYY-01-02 day', 'rename X to Y')"
  - "The ticket references code, a deployed label, or a data shape that may have changed since it was filed"
tags:
  - process
  - linear
  - reopened-ticket
  - premise-verification
  - bui-210
  - bui-459
  - bui-461
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
documentation (BUI-425) — the running LaunchAgent is still PID-confirmed
`com.gixen.server` against `~/.gixen-server`. Implementing the ticket's specified label
as written would have made a routine re-deploy bootstrap a same-labeled job that hijacks
the real server (see the sibling doc's pattern 5 for the mechanism:
`resolve_server_dir()` prefers the new directory the instant it exists, and
`install.sh` creates it via `mkdir -p`). The correct fix was a revert to match deployed
reality, with a comment explaining that this is deliberate, not drift — not the rename
the ticket asked for.

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
| BUI-459 | `install.sh` should use the post-rename `com.comics.server` label | Live Mac Mini never had the BUI-220 rename actually performed | Reverted to match deployed reality, not "fixed" to the ticket's spec |
