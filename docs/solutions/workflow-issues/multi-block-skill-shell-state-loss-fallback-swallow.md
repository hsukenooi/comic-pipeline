---
title: "Multi-block Claude skills lose shell state between Step blocks — and a swallowing fallback hides it"
date: 2026-07-16
category: workflow-issues
module: comic-collection-add
problem_type: workflow_issue
component: development_workflow
severity: high
applies_when:
  - "Authoring a multi-step `.claude/commands/*.md` skill where each `## Step` is its own fenced bash block"
  - "A skill sets or exports shell state (env var, sourced function, `set -e`, `cd`) in one block and a later block depends on it"
  - "A shell pipeline uses `|| echo \"\"` / `|| true` / `2>/dev/null` after a curl or URL-construction step to produce a default on failure"
tags:
  - claude-skill-authoring
  - bash-block-isolation
  - fallback-swallow
  - seen-set-dedup
  - error-handling
  - bui-352
  - bui-353
---

# Multi-block Claude skills lose shell state between Step blocks — and a swallowing fallback hides it

## Context

`.claude/commands/comic/collection-add.md` is a multi-step Claude skill: each `## Step` heading is followed by its own fenced bash block. Step 0 did the expected thing — `source scripts/comics-server.sh; comics_resolve_server` to export `COMICS_SERVER_URL` — and every later step's prose assumed that variable was still live. It wasn't. **Each `## Step`'s bash block runs in a fresh shell**, so anything set in one block (env vars, sourced functions, `set -e`, `cd`) is invisible in the next.

Step 1b needed `COMICS_SERVER_URL` to fetch the BUI-121 seen-set (item IDs already recorded, used to dedup wins across runs). With the var empty, the interpolated URL was malformed (`/api/comics/...` with no host), `curl` exited 3 ("no host part in the URL"), and a defensive-looking `|| echo ""` caught that failure and returned an **empty seen-set**. Every previously-recorded win then looked "new," so the skill fed **130** won auctions into the record pipeline instead of the real **8**. Nothing crashed or printed red — the run "succeeded" against wrong data. Only the independent BUI-34 server-side already-owned dedup (a second, unrelated net) kept it from writing duplicate collection rows.

Two coupled traps produced this, and both generalize well beyond this one skill.

## Guidance

### Trap 1 — per-block shell-state loss

Authors write multi-`## Step` markdown as if it were one continuous script, but the harness runs each fenced block as an independent shell. Nothing persists across blocks. Do **not** rely on a variable, sourced function, `cd`, or `set -e` from an earlier block.

Remediation, in preference order (pick the cheapest that fits):

1. **Collapse the multi-block logic into one helper subcommand that owns the whole join (best).** One process = one shell state, one env read, one place to encode the failure policy correctly — instead of re-deriving it (and re-risking an env miss or an off-by-one) every run. This is what BUI-353's `gixen record-win-prep` did.
2. **Re-resolve/re-source needed state at the top of every block that uses it.** Cheap and idempotent; appropriate when blocks must stay separate for user-gating between steps.
3. **Run as a single end-to-end block.** Lowest effort, least composable — only when no user-approval gate is needed between steps.

### Trap 2 — fallback-swallow conflation

`|| echo ""` after a network call treats two categorically different failures as the same "safe to continue with a default" case:

- **Local / connectivity-class failure** — empty or malformed URL, connection refused, DNS failure, timeout, TLS error. The request never reached the server, so it tells you *nothing* about server state. Falling back here is a **bug**: it silently substitutes "nothing is seen" for "we don't know what's seen." This is the common, silent case.
- **Genuine server 5xx** — the server was reached and errored internally. Falling back to an empty seen-set here is a *bounded* risk, defensible only because an independent downstream check (BUI-34's already-owned dedup) catches the resulting duplicate-processing.

In any fetch fallback, **distinguish the two: hard-stop on local/connectivity failure, fall through only on a real 5xx** — and only when an independent second net actually makes the fall-through safe. If there's no second net, don't fall back at all.

## Why This Matters

- **Silent correctness loss riding on a secondary net.** No error, no stack trace, no non-zero exit. It was caught only because 130 looked wrong for a batch that should have been ~8, and because an *unrelated* server-side dedup happened to exist. Without that net, this writes duplicate/incorrect collection rows (a data-loss class, cousin to the BUI-122 already-owned wish-list trap).
- **Token/context blowup as a side effect.** Feeding 130 titles through `comic-identify --batch` and the downstream steps instead of 8 is >15× the LLM/subprocess cost per run — a correctness bug that is also a cost bug.
- **It reads as robustness.** A `|| echo ""` after a network call looks like defensive hardening, not danger. That's exactly why it needs to be named, not trusted to review-by-eyeball.

## When to Apply

- Any multi-block `.claude/commands/*` skill that sets shell state expected by a later block, **or** uses an `|| fallback` after a network / URL-construction call.
- Prevention checklist:
  - [ ] Does any `## Step` block read a variable exported by an *earlier* block? If so: collapse into a helper (preferred) or re-derive it at the top of the later block.
  - [ ] Does the skill body warn future editors that blocks don't share shell state, so an edit doesn't reintroduce the assumption?
  - [ ] For every `curl`/network call with `|| fallback`: is the fallback triggered *only* by a genuine 5xx — never by a connection failure, timeout, malformed URL, or unexpected status?
  - [ ] Is there an independent downstream check that actually makes the fallback path safe? If not, hard-stop instead.
  - [ ] For logic reused across more than ~2 runs, or with a positional-mapping risk (zipping two parallel lists back together), prefer a tested helper subcommand over inline shell/Python re-authored per edit.

## Examples

**Broken (pre-fix, paraphrased):**

```bash
## Step 0
source scripts/comics-server.sh
comics_resolve_server        # exports COMICS_SERVER_URL — in THIS shell only

## Step 1b  (separate block → fresh shell → COMICS_SERVER_URL is unset)
SEEN_IDS=$(curl -sf "$COMICS_SERVER_URL/api/comics/collection/record-win/seen" \
  | jq -r '.item_ids[]' 2>/dev/null || echo "")   # empty URL → curl exit 3 → swallowed → empty seen-set
```

**Fixed — the helper owns the failure policy** (`packages/gixen-cli/record_win_prep.py`, `fetch_seen_ids`):

```python
try:
    resp = session.get(url, timeout=timeout)
except requests.ConnectionError as exc:
    raise RecordWinPrepError(...)      # HARD STOP
except requests.Timeout as exc:
    raise RecordWinPrepError(...)      # HARD STOP
except requests.RequestException as exc:
    raise RecordWinPrepError(...)      # HARD STOP (catch-all: SSL, malformed URL, ...)

if 500 <= resp.status_code < 600:
    print("warning: ... falling back to an empty seen-set this run "
          "(BUI-34 already-owned dedup is the net)", file=sys.stderr)
    return set()                       # the ONLY case allowed to fall back

if resp.status_code != 200:
    raise RecordWinPrepError(...)      # HARD STOP (4xx or anything unexpected)
```

The CLI command hard-fails immediately if `COMICS_SERVER_URL` is unset, so the malformed-URL path is unreachable in the first place. The skill body was also rewritten to re-source `comics_resolve_server` at the top of every server-calling block and to call the loss-of-shell-state trap out explicitly in its Common Mistakes table (so a future edit can't quietly reintroduce it).

**Test pattern** — boundary-test the status-code split rather than trusting one example value (`packages/gixen-cli/tests/test_record_win_prep.py`): parametrized 500/599 fall-back tests, a 600 "not a real 5xx → hard-stop" upper-boundary test, one test per hard-stop trigger (connection error, timeout, 4xx, unparseable body, missing key, other request exception), plus a line-count-mismatch test guarding the `identify_titles` positional-mapping join. The pattern generalizes: for any fetch-with-fallback, write one test per hard-stop trigger and boundary-test the numeric range that defines "safe to fall back."

## Related

- `design-patterns/drain-started-cancel-tail-seen-set-loops.md` — the same seen-set data-loss *class* (a side-effect-recording seen-set silently losing entries on a failure path), different mechanism (thread-pool drain in seller-scan vs. shell-state + fallback here).
- `best-practices/collection-add-workflow-patterns-2026-06-18.md` — same skill, earlier era. Its original Sections 1–2 (the cutoff-date dedup, and the `/api/comics/history`-vs-`gixen list` selection) described mechanisms superseded by the BUI-121 seen-set and the 2026-07-05 refactor; a 2026-07-17 `ce-compound-refresh` (BUI-374) removed those two sections and re-verified the remaining pending-push-semantics / manual-resolution content against current code.
- Tickets: BUI-352 (the bug), BUI-353 (the `record-win-prep` helper), BUI-354 (the confidence-gate fix). Shipped in PR #187.
