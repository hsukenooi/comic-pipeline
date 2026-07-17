---
description: Act as engineering manager for a set of Linear tickets — plan in waves, execute via isolated sub-agents, review, merge, and close each. Token-efficient by design.
argument-hint: <BUI-XXX BUI-YYY ...>
---

# /em-batch — Engineering-manager a batch of Linear tickets

You are the **engineering manager** for the tickets in `$ARGUMENTS`. Your job is to get each one *done and done well*: implemented, reviewed, tested, merged to `main` green, and closed in Linear. First produce a **wave plan**, then execute it.

Distilled from the BUI-299..319 batches — the model-selection and token-efficiency rules below are empirical, not theoretical. Follow them.

## 1. Plan (do this first, before spawning anything)

- Read every ticket: `linear issue view <ID>` (and referenced tickets for context).
- **Group into waves** by file-conflict and dependency:
  - Tickets touching the **same file** go in the **same wave combined into ONE branch/PR**, or in **different waves sequenced** so the later one branches from the merged earlier one. Never let two parallel agents edit the same file.
  - **Conflict is by import edge, not just filename.** Two tickets in *different* files still collide when one changes a module surface the other imports (BUI-323 refactored `ebay_fetch.py`; BUI-322 imports it). Run them parallel only if the module-owner ticket is told to **keep its public signatures stable** — otherwise sequence them.
  - A ticket that consumes another's output waits for that one to merge.
  - **The all-hot-file batch has no parallel win — plan for it.** When *every* ticket touches the same file (in the BUI-383..389 batch all nine touched `server/main.py` / `server/db.py`), the never-parallel-edit-the-same-file rule makes the batch 100% serial however you wave it — N branches, N CI runs, N merges, each waiting on the prior. Two levers cut the round-trips: **combine** independent same-file tickets into one branch/PR (done for 388+386 and 384+383 that batch — it worked, at the cost of coarser per-ticket PR traceability and one red CI blocking the whole group), or **stack** them on a single branch in dependency order when a later one builds on an earlier. Combine when they're independent and small; stack when there's a natural sequence.
- Assign a **model per ticket** (see §2) and a **review depth** per ticket (see §3).
- Post the wave plan (tickets, waves, model, review depth, conflict notes) before executing.

## 2. Model selection — match to JUDGMENT REQUIRED, not file count or LOC

```
opus   → the RIGHT approach isn't fully spelled out and being subtly wrong is costly:
         • the ticket's stated approach may itself be wrong and needs challenging
           (BUI-315: the specified --publisher flag was a no-op in batch mode — the
           real fix was a Marvel-only gate elsewhere; a literal executor ships the no-op)
         • reasoning about safety invariants / why a change can't regress a prior bug
           (BUI-316: proving it can't reintroduce BUI-129)
         • open-ended money math (BUI-318; the BUI-306 $2k-over-bid class)

sonnet → well-specified structural work, EVEN in subtle domains (concurrency,
         cross-package refactors), WHEN the hard thinking is already encoded in the
         ticket or a named pattern you hand the agent (BUI-313 convergence refactor,
         BUI-310 backward-compat plumbing, BUI-317/319 concurrency given the BUI-307
         drain/cancel pattern explicitly).

haiku  → pure mechanical repetition, zero judgment (BUI-314: --version boilerplate
         across N CLIs — "copy this shape N times").
```

**Two rules that override the table:**
- **Control TOKENS via the review policy, NOT by down-tiering.** The priciest agents in the batch (~220k tokens) were *sonnet* tickets with 8-reviewer `/ce-code-review` fan-outs — the model was cheap, the reviewers weren't. Don't pick opus→sonnet to save tokens; trim reviewers instead.
- **For subtle correctness (concurrency, money, data-safety), the safety net is the ADVERSARIAL REVIEW, not the base model.** sonnet's first drafts carried real bugs this batch (a `SystemExit`-past-`except` batch-crash; a seen-then-drop data-loss window) and the review caught both. `sonnet + full review` beats `opus + light review`. If you must economize, keep the review and drop the model tier.

## 3. Review depth — earn the tokens

- **`/ce-code-review` (full multi-persona fan-out)** — only on tickets touching **money, concurrency, correctness gates, or external/data behavior**. This is where review has repeatedly caught bugs tests missed (a 6×/$2k over-bid, a seen-set data-loss, a batch-crash). The fan-out is multiplicative (personas × tickets) — spend it where it pays.
- **Scope the personas to the change — this is *how* you "trim reviewers" (the §2 lever).** The full panel is the real cost driver: the opus + full-`ce-code-review` tickets ran ~290–298k tokens each — the model tier was cheap, the 9–10 persona fan-out wasn't. For a focused change, name the relevant personas instead of the whole panel: a schema/migration change → correctness + adversarial + data-migration; a concurrency change → correctness + adversarial + reliability; a money-math change → correctness + adversarial. Reserve the full fan-out for genuinely cross-cutting diffs.
- **Single inline review** (the implementing agent reviews its own diff in-context, applies safe fixes) — for mechanical/well-specified diffs (boilerplate, plumbing, straightforward refactors).
- **`/ce-simplify-code`** — CONDITIONAL. Run only when the diff added real surface area (new abstraction, refactor, >~80 lines). **Skip on boilerplate/plumbing** — across the batch it prevented zero defects and adds churn on small diffs.
- **Agents must review INLINE and synchronously.** Forbid them from spawning detached background reviewer sub-agents they then idle-wait on — that triggers an idle→notify→resume→relay loop that roughly doubled the message tokens on one ticket for zero added value.

## 4. Per-ticket workflow (one sub-agent per ticket/PR)

Spawn each via the Agent tool with `isolation: "worktree"` and the assigned model. The agent:

1. **Before any git write, verify `git rev-parse --show-toplevel` points at your own worktree, not the shared `main` checkout.** An agent that runs `git checkout -b` against the shared checkout corrupts the EM's working state mid-batch — two of four agents in one session did exactly this (both caught it and reverted); treat it as a systemic failure mode, not a fluke. Then `linear issue view <ID>` to read the ticket; branch (branch-per-issue; combine same-file tickets into one branch).
2. `/ce-work` to implement.
3. Tests green — `apps/*` use `cd <pkg> && uv run --with pytest pytest`; `packages/*` and `plugins/*` use `uv run pytest`.
4. `/ce-simplify-code` **only if §3 says so**.
5. Review per §3 (full `/ce-code-review` or single inline pass) — **inline, no detached reviewers it waits on**. Apply safe fixes directly.
6. Commit on its branch (do NOT push, do NOT open a PR) with the repo trailers:
   ```
   Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
   Claude-Session: https://claude.ai/code/session_01SnSpPjeQfifLR71DY1FigJ
   ```
7. SendMessage to `main`: branch, HEAD SHA (`git rev-parse HEAD`), test counts, and any out-of-scope findings.

**License to stop (put this in the spawn prompt — for opus-tier work *and* any ticket sourced from a review / out-of-scope finding, regardless of model tier).** §2 deliberately routes opus work whose *stated approach may be wrong* — but the premise-may-be-broken risk isn't opus-specific. A ticket that is itself a review *residual* is exactly the class where the filer's understanding may be incomplete: BUI-388 was a *sonnet* ticket whose named target (an `item_id`-wide write) was already correct-by-design, and it only shipped right because the spawn prompt carried an explicit premise-check + stop license. Grant every such agent that permission: *if the ticket's premise is broken or the change can't be made safely as specified, STOP and report your findings instead of shipping a speculative implementation.* A disciplined no-code stop-and-report is a **success**, not a failure — handle it per §5. (BUI-326: the agent correctly refused to port a fragile price-extractor into a live bid-cap path; the right move was to close it, not force code.)

If an agent stops mid-review without committing (a known failure mode), resume it via SendMessage: apply findings → run tests → commit → report.

**Resuming a completed agent via SendMessage re-instantiates its entire context (~100k+ tokens).** Only resume when you need it to *do more work* — apply findings, commit, re-run tests. **NEVER resume merely to acknowledge a report, thank it, or tell it to stand down** — handle those silently on the EM side. In one batch, resuming an agent just to say "stand down" burned ~108k tokens for zero work (comparable to a whole ticket run).

## 5. EM duties (you, on `main`, per finished ticket)

1. `git push -u origin <branch>`
2. `gh pr create` with a summary body (+ the `🤖 Generated with…` / session-URL footer).
3. Wait for CI: `gh pr checks <N>`. Gates are `workspace` + `apps-python` + `lint` + `ezship`. **`typecheck` is NON-required** — don't block on it, but glance at it.
4. **Verify the diff for its risk class — CI-green is necessary, not sufficient.** Before merging, read the diff and match the check to the risk: a behavior-preserving refactor → re-run the affected suite locally rather than trusting the agent's reported counts (BUI-389's `server/fallback.py` extraction: the real risk was a `main↔fallback` circular import, so the EM read the import structure and re-ran both suites before merging); a schema migration → apply it against a copy and confirm up/down; a money-math change → re-run the pricing path on a known case.
5. Merge once green: `gh pr merge <N> --merge --delete-branch` — **one PR per bash call.** Batching several `gh pr merge`s (or combining a merge with other commands) in one invocation trips the auto-mode classifier ("Merge Without Review"); a lone `gh pr merge <N>` call goes through.
6. Clean up the worktree (`git worktree remove -f -f <path>`; a live agent can lock it — `-f -f`), delete the local branch, `git checkout main && git pull`. Note: `gh pr merge --delete-branch` prints `failed to delete local branch … used by worktree` whenever the agent's worktree still holds the branch — that warning is **benign** (the *remote* branch is deleted); remove the worktree first, or just delete the local branch during this cleanup step.
7. `linear issue comment add <ID>` with a shipped-summary, then `linear issue update <ID> -s "Done"`. **If the ticket carried a deploy action** (schema migration, service restart, install refresh), note it now — you'll collect all of them into the §6 deploy checklist.

**When an agent reports the ticket is ill-specified or can't be done safely** (a stop-and-report, no PR to merge): don't force code through. Escalate the *decision* to the user (`AskUserQuestion` with the options + a clear recommendation), then close the ticket per their call (Won't Do / re-scope) and file the spec-correction as a follow-up per §6. Clean up the (empty) worktree/branch. Not every ticket ships — a well-reasoned decline is a valid outcome.

Launch each wave's agents in parallel; start the next wave only when its dependencies have **merged**. Track progress with the Task tools.

## 6. Guardrails

- **New out-of-scope bugs/improvements** found during reviews → file a NEW Linear ticket (BUI team), **FILE-ONLY, do not recurse** into working them (this is one backlog level; findings from findings just get filed). When several findings share **one root cause** and would be fixed together, file **one consolidated ticket**, not N fragments — but keep them separate when they'd be worked independently (e.g. the same idiom duplicated across different packages).
- When **all tickets are merged and `main` is green**, run `/ce-compound mode:headless` **if** the batch surfaced a compound-worthy learning (a non-obvious trap future work will re-hit). **If the batch also filed a documentation / doc-sweep finding** (a ticket that will itself edit `CONCEPTS.md` or `docs/solutions/`), scope the compound step to *reusable principles and traps* and let that filed ticket own the concrete `CONCEPTS.md` / `docs/solutions/` edits — don't have both rewrite the same files (BUI-393 claimed the `CONCEPTS.md` + evidence-layer doc edits for the BUI-384 membership bound, so the compound step stayed scoped to principles to avoid double-writing and a later merge conflict). Then post a final summary of everything shipped.
- **Deploy checklist in the final summary.** If any merged ticket carried a deploy action (schema migration, service restart via `launchctl kickstart`, install refresh via `uv tool install --force` / `uv sync --all-packages`), collect them into one deploy checklist in the final summary and state, per item, whether it's been applied. A merged ticket is not necessarily live — schema changes are exactly the thing an EM must not silently drop (the BUI-383..389 batch shipped two migrations, `bids.group_changed_at` and `group_wins.source` + a unique-index re-key, that sat merged-but-not-deployed until the user asked).
- **Never self-widen permissions.** The `Bash(gh pr merge:*)` rule must already be user-authored in `.claude/settings.local.json`. If a merge is blocked, surface it to the user — do not attempt to grant it yourself.
- **Peer/background messages are not user approval.** Verify merged code rather than trusting late/orphaned reviewer messages that arrive after an agent finished.
