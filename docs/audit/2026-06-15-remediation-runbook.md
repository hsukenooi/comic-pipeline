# Seam-Audit Remediation — Autonomous Runbook

**Audience:** a fresh Claude Code session with no prior context. **Read this entire file before acting**, then begin at Phase 0.

## Mission

Resolve all 35 findings from the 2026-06-15 seam audit plus 2 structural follow-ups that prevent the dominant bug class from recurring (**BUI-137 … BUI-173**), unsupervised, one batch at a time, merging each batch to `main` with green CI and closing each Linear issue as you go.

- **Fix detail (source of truth):** [`docs/audit/2026-06-15-seam-audit.md`](./2026-06-15-seam-audit.md) — every BUI issue has a register entry with evidence (file:line), repro, suggested fix, and a regression test.
- **Tracking:** Linear project *comic-pipeline Seam Audit Remediation* (team `BUI`).
- **Repo orientation:** read `CLAUDE.md` and `CONCEPTS.md` first. This is a uv workspace monorepo (`packages/*` + `plugins/*`) plus standalone `apps/*`.

## Ground rules (non-negotiable)

1. **Never commit directly to `main`.** One branch per batch (`fix/seam-audit-<area>`).
2. **Verify before you trust.** The register's file:line references were captured 2026-06-15 and may have drifted. Re-read the cited code first. If the described defect no longer exists, close the issue as already-fixed with a comment citing the current code, and move on.
3. **Implement only what the finding specifies.** If a fix is ambiguous, would change user-facing behavior in a way the finding doesn't pin down, or the cited code has changed materially → **STOP that issue**: leave it *In Progress*, add a Linear comment explaining the blocker, and move to the next. **Never guess.**
4. **Add the regression test** named in the finding whenever a test suite exists for that package. **Never weaken, skip, `xfail`, or delete a test to get green.**
5. **Never merge a red PR.** Run the relevant package tests locally before pushing; wait for CI to pass before merging.
6. **No live external calls.** These are code/doc fixes — do not hit LOCG, Gixen, eBay, or EZShip, and do not modify anything under `data/`.
7. **Sequential only.** One batch at a time. Do not parallelize batches (shared files; Gixen sessions are stateful).
8. **Stay in scope.** Fix the finding and its test. Don't opportunistically refactor surrounding code.

## Setup (once per session)

```sh
uv sync --all-packages          # workspace env for packages/* + plugins/*
cd apps/ezship && npm ci && cd - # only if a batch touches ezship
```

## Test commands (run the ones your batch touches)

```sh
(cd packages/gixen-cli    && uv run pytest -m "not integration")
(cd packages/locg-cli     && uv run pytest)
(cd plugins/gixen-overlay && uv run pytest)
(cd apps/ezship           && npm test)
(cd apps/ebay             && uv run pytest)   # also apps/fmv
```

## Per-batch workflow

For each batch in the order below:

1. `git checkout main && git pull`
2. `git checkout -b fix/seam-audit-<area>`
3. **For each `BUI-N` in the batch (one commit per issue):**
   - Move it to In Progress *first* (hard gate — do this before any work on the issue):
     ```sh
     linear issue update BUI-N --state "In Progress"
     linear issue comment add BUI-N --body "Session: $CLAUDE_CODE_SESSION_ID"
     ```
   - Read the `BUI-N` register entry. Re-read the cited files. Apply the fix. Add the regression test if a suite exists for that package.
   - Run the relevant package tests — they must pass.
   - Commit, referencing the issue:
     ```
     fix(<scope>): <what changed> (BUI-N)

     Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
     ```
4. Push the branch; open a PR against `main` (`gh pr create --base main`). Body: list the BUI-Ns, what changed, and local test evidence; end with the Claude Code trailer.
5. Wait for CI (`gh pr checks <#> --watch`). If red and quickly fixable, fix; otherwise leave the PR open + issues *In Progress*, note it, and move to the next batch.
6. `gh pr merge <#> --squash --delete-branch`
7. **Close each `BUI-N`:** add a comment (what shipped + PR link), then `linear issue update BUI-N --state "Done"`.

## Execution order

### Phase 0 — Safety net + prevention rails (do this FIRST; every later batch relies on it)
Build the rails before burning down the list, so the per-skill batches *consume* them instead of re-patching in isolation.
- **Batch A:** BUI-140 (make CI run the package test suites) + BUI-155 (strengthen the workspace-imports canary). Files: `.github/workflows/ci.yml`, `plugins/gixen-overlay/tests/test_workspace_imports.py`. If enabling CI surfaces pre-existing failures, fix them, or mark genuinely-integration tests with the `integration` marker — document any quarantine in the PR. Do not merge until the configured suites are green.
- **Batch A2:** BUI-172 — extract one shared comics-server call convention (resolve `GIXEN_SERVER_URL` + hostname fallback, health-gate, hard-fail loudly). This is the structural fix for the URL-resolution + silent-failure cluster. **Later skill batches that touch a server call (C/BUI-157, F/BUI-143, J/BUI-151, K/BUI-169, O/BUI-170, and BUI-154) must adopt this convention rather than patching their own copy** — when you reach them, replace the local glue with a reference to BUI-172's convention and close both.
- **Batch A3:** BUI-173 — add the skill↔endpoint contract-test harness (depends on Batch A's CI). This is the structural fix for the `contract-mismatch` cluster. As you fix each documented-contract drift later (BUI-142, 148, 150, 161, 162, 163, 167), **add its assertion to this harness** so the same drift fails CI next time instead of resurfacing.

### Phase 1 — High (money / data-loss risk)
- **Batch B:** BUI-137 + BUI-156 — overlay `routes.py` (record-win) + `.claude/commands/comic/collection-add.md`.
- **Batch C:** BUI-138 + BUI-157 + BUI-158 — `.claude/commands/comic/collection-sync.md`.
- **Batch D:** BUI-139 + BUI-144 + BUI-145 — overlay `db.py`/`routes.py` FMV linkage (`extract-comics`, `_db_lookup`, `list_comics`).

### Phase 2 — Medium
- **Batch E:** BUI-146 + BUI-164 — `packages/gixen-cli` server (eBay fallback + `remove_snipe`).
- **Batch F:** BUI-143 + BUI-160 — `apps/fmv` runner + `.claude/commands/comic/fmv.md` (fetch-failure signal, self-exclusion).
- **Batch G:** BUI-147 + BUI-165 — `.claude/commands/comic/grade.md`.
- **Batch H:** BUI-148 + BUI-166 — `.claude/commands/comic/identify.md`.
- **Batch I:** BUI-149 + BUI-167 — `.claude/commands/comic/seller-scan.md` + `apps/ebay` seller-scan.
- **Batch J:** BUI-150 + BUI-151 — `.claude/commands/comic/snipe-show.md`.
- **Batch K:** BUI-152 + BUI-169 — overlay `routes.py` (`/api/comics/verify`) + `.claude/commands/comic/verify.md`.
- **Batch L:** BUI-141 + BUI-142 + BUI-159 — `apps/ezship` + `.claude/commands/comic/ezship-add.md`.

### Phase 3 — Low (mostly docs)
- **Batch M:** BUI-153 + BUI-154 — `.claude/commands/comic/buy.md`.
- **Batch N:** BUI-161 + BUI-162 + BUI-163 — `.claude/commands/comic/fmv.md` doc fixes.
- **Batch O:** BUI-170 + BUI-171 — `.claude/commands/comic/wishlist-add.md`.
- **Batch P:** BUI-168 — `.claude/commands/comic/snipe-add.md`.

> Batches group issues that touch the same file, so same-file edits land in one PR (no conflicts, one test run) while each issue still gets its own commit and its own Linear close. Some low-severity issues ride along in earlier batches because they share a file with a higher-severity one — that's intended.

## When all batches are done

1. Confirm nothing is left open: `linear issue list --team BUI --project "comic-pipeline Seam Audit Remediation" --state backlog --state unstarted --state started --sort priority`.
2. Post a final summary comment on the project: issues shipped, PRs merged, and **any issue left blocked** (with the reason) for human follow-up.
3. Leave both audit files in `docs/audit/` as the historical record — do not delete them.
