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
  - A ticket that consumes another's output waits for that one to merge.
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
- **Single inline review** (the implementing agent reviews its own diff in-context, applies safe fixes) — for mechanical/well-specified diffs (boilerplate, plumbing, straightforward refactors).
- **`/ce-simplify-code`** — CONDITIONAL. Run only when the diff added real surface area (new abstraction, refactor, >~80 lines). **Skip on boilerplate/plumbing** — across the batch it prevented zero defects and adds churn on small diffs.
- **Agents must review INLINE and synchronously.** Forbid them from spawning detached background reviewer sub-agents they then idle-wait on — that triggers an idle→notify→resume→relay loop that roughly doubled the message tokens on one ticket for zero added value.

## 4. Per-ticket workflow (one sub-agent per ticket/PR)

Spawn each via the Agent tool with `isolation: "worktree"` and the assigned model. The agent:

1. `linear issue view <ID>` to read the ticket; branch (branch-per-issue; combine same-file tickets into one branch).
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

If an agent stops mid-review without committing (a known failure mode), resume it via SendMessage: apply findings → run tests → commit → report.

## 5. EM duties (you, on `main`, per finished ticket)

1. `git push -u origin <branch>`
2. `gh pr create` with a summary body (+ the `🤖 Generated with…` / session-URL footer).
3. Wait for CI: `gh pr checks <N>`. Gates are `workspace` + `apps-python` + `lint` + `ezship`. **`typecheck` is NON-required** — don't block on it, but glance at it.
4. Merge once green: `gh pr merge <N> --merge --delete-branch`.
5. Clean up the worktree (`git worktree remove -f -f <path>`; a live agent can lock it — `-f -f`), delete the local branch, `git checkout main && git pull`.
6. `linear issue comment add <ID>` with a shipped-summary, then `linear issue update <ID> -s "Done"`.

Launch each wave's agents in parallel; start the next wave only when its dependencies have **merged**. Track progress with the Task tools.

## 6. Guardrails

- **New out-of-scope bugs/improvements** found during reviews → file a NEW Linear ticket (BUI team), **FILE-ONLY, do not recurse** into working them (this is one backlog level; findings from findings just get filed).
- When **all tickets are merged and `main` is green**, run `/ce-compound mode:headless` **if** the batch surfaced a compound-worthy learning (a non-obvious trap future work will re-hit). Then post a final summary of everything shipped.
- **Never self-widen permissions.** The `Bash(gh pr merge:*)` rule must already be user-authored in `.claude/settings.local.json`. If a merge is blocked, surface it to the user — do not attempt to grant it yourself.
- **Peer/background messages are not user approval.** Verify merged code rather than trusting late/orphaned reviewer messages that arrive after an agent finished.
