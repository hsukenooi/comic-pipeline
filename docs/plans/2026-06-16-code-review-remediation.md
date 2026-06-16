---
title: Full-Repo Code Review Remediation — Execution Prompt
date: 2026-06-16
linear_project: Full-Repo Code Review Remediation (BUI, slug 98e44a4f91b7)
issues: BUI-174 .. BUI-191
status: not started
---

# Execution Prompt — Full-Repo Code Review Remediation

> Paste everything below into a fresh Claude Code session at the repo root
> (`/Users/hsukenooi/Projects/comic-pipeline`). It is self-contained: it tells
> you what to fix, in what order, on which branches, and how to commit/PR.

---

## 0. Mission

A full-repo code review (2026-06-16) filed **18 Linear issues** (BUI-174…BUI-191)
under the **Build (`BUI`)** project **"Full-Repo Code Review Remediation"**
(slug `98e44a4f91b7`). Work through all of them in the **clustered branches and
order** defined in §3. Every change happens on a branch and ships as a PR — never
commit to `main`. Each branch carries **multiple commits** (one per logical fix),
never a single squashed blob.

Read each issue body before touching code: `linear issue view BUI-XXX`. The body
has the file:line, the failure scenario, the suggested fix, and the current test
gap.

## 1. Ground rules (read once, apply throughout)

- **Branch off the latest `main`.** Before each new branch: `git checkout main && git pull --rebase`.
- **One cluster per branch, multiple commits per branch.** Group commits by
  logical fix. Do **not** combine unrelated fixes into one commit, and do **not**
  collapse a whole branch into one commit.
- **Sequential, not parallel.** Several branches touch the same files
  (`sold_comps.py`, `commands.py`, `fmv_runner.py`). Finish a branch → open PR →
  merge → pull `main` → start the next. This avoids same-file conflicts.
- **Test before you commit.** This repo has **no repo-wide test runner** — test
  each package from its own dir (see §4). CI now runs every suite, so a red suite
  blocks merge.
- **Write the regression test first where feasible.** Most issues note "no test
  today" — the fix is not done until a test exists that would have caught it.
- **Follow the `linear-method` skill** for all Linear state changes (§5). Move an
  issue to **In Progress** the moment you start it; add a closing comment and set
  **Done** only after its PR merges.
- **Commit message format:** Conventional Commits, reference the issue, and end
  with the trailer:
  ```
  Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
  ```
- **PR body** ends with:
  ```
  🤖 Generated with [Claude Code](https://claude.com/claude-code)
  ```
- Use `gh` for PRs. Link issues with `Closes BUI-XXX` in the PR body so merge
  auto-closes them (still add the Linear closing comment per §5).

## 2. Repo facts you must know (so a fix lands at the right depth)

- **uv workspace:** `packages/*` + `plugins/*` are members → `uv sync --all-packages`
  makes one shared `.venv`. `apps/*` are **not** members — they're `uv tool install`ed
  (`./scripts/install.sh`) and the FMV pipeline **shells out** across them
  (`comic-fmv` → `ebay-sold-comps` console script on PATH).
- **The overlay is a plugin of gixen-cli**, importing private `server.main` /
  `server.db` helpers. A canary (`plugins/gixen-overlay/tests/test_workspace_imports.py`)
  fails if that surface drifts — keep it green.
- **R11 hard rule:** ownership/collection checks must **fail loud** on an
  unreachable/failed call — never render "not owned" from a failed call (it buys a
  duplicate). Several fixes here exist to enforce this; do not regress it.
- **`bids.status`:** `PENDING → WON/LOST/ENDED/FAILED` + tombstone `REMOVED`
  (older code tolerates `PURGED`). "Results" views must exclude the tombstone.
- **Skill ↔ code contract harness** (`plugins/gixen-overlay/tests/test_skill_contracts.py`,
  BUI-173): asserts the `/comic:*` skill docs match the real endpoints. Extend it,
  don't bypass it.
- **CI** (`.github/workflows/ci.yml`) runs: `workspace` (gixen-cli `-m "not integration"`,
  locg-cli, overlay), `apps-python` (ebay, fmv), `ezship` (vitest). There is **no**
  lint/type gate yet (that's BUI-185/BUI-188).

## 3. Branch plan — clusters, order, and commit breakdown

Do the phases in order. **Phase 1 = correctness/money bugs first** (dup-buy and
overpay are the highest-impact and are independent of the refactors). **Phase 2 =
prevention infra**, which then absorbs/holds the Phase-1 fixes.

The secondary rollup **BUI-184** is a checklist; its items are distributed into the
subsystem branch they belong to (checked off there). Close **BUI-184** when every
box is ticked.

### Phase 1 — Correctness fixes (one branch per subsystem, in this order)

#### Branch 1 — `fix/locg-matcher`  *(highest value: stops duplicate buys)*
Issues: **BUI-175, BUI-176** + BUI-184 matcher items.
Files: `packages/locg-cli/src/locg/commands.py`; tests in `packages/locg-cli/tests/`.
Commits:
1. `fix(locg): match decimal/point issue numbers like #1.MU (BUI-175)` — widen
   `_ISSUE_TOKEN_RE`, align comparison; add matcher test.
2. `fix(locg): match owned base issue when a variant qualifier is supplied (BUI-176)` —
   make variant a soft preference in `_match_owned_issue`; add test.
3. `fix(locg): guard _resolve_price against empty current_bid (BUI-184)` —
   `commands.py:1830` IndexError; per-win loop must not abort the batch.
4. `fix(locg): dedup wins by a shared series key (BUI-184)` — `commands.py:1966`
   owned_index probe/key mismatch (the PLAUSIBLE one — confirm with a test first).

#### Branch 2 — `fix/fmv-pricing`  *(stops wrong-comic pricing + overpay)*
Issues: **BUI-174, BUI-187, BUI-182, BUI-179** + BUI-184 fmv items.
Files: `apps/fmv/src/fmv_runner.py`, `apps/fmv/src/fmv_math.py`,
`apps/ebay/src/sold_comps.py` (id echo only); tests in `apps/fmv/tests/`.
> BUI-187 (id-keyed subprocess mapping) **is** the systemic fix for BUI-174 — do
> them as one change and close both.
Commits:
1. `fix(fmv): map subprocess comps to books by id, not list position (BUI-174, BUI-187)` —
   echo an id from `sold_comps.run_batch`, match by it in `fmv_runner`, assert
   `len(results)==len(sent)`, fail loud on mismatch; add a mixed cached+fresh /
   reordered-result test.
2. `fix(fmv): re-apply wide-window confidence cap on cached FMV reuse (BUI-182)` —
   persist window/capped confidence; replace falsy-zero `if fmv_high` with `is not None`.
3. `fix(fmv): flag or trim outliers in 2-comp pools (BUI-179)` — `fmv_math.py:88`.
4. `fix(fmv): add subprocess timeout + guard json output parse (BUI-184)` —
   `fmv_runner.py:218` timeout, `:226` empty/partial-file guard.

#### Branch 3 — `fix/ebay-scraping`
Issues: **BUI-177, BUI-183** + BUI-184 ebay items.
Files: `apps/ebay/src/sold_comps.py`, `apps/ebay/src/ebay_fetch.py`,
`apps/ebay/src/seller_scan.py`; tests in `apps/ebay/tests/`.
> Do **after** Branch 2 (both touch `sold_comps.py`); pull `main` first.
Commits:
1. `fix(ebay): catch transient SerpApi errors + retry/backoff (BUI-177)` —
   catch `requests.RequestException`, bounded retry on Timeout/429/5xx.
2. `fix(ebay): stop the numeric grade regex matching prices in titles (BUI-183)` —
   exclude price/measurement context; add `TestParseGrade` cases.
3. `fix(ebay): retry token fetch + skip null-title listings (BUI-184)` —
   `ebay_fetch.py:127` get_token retry/429, `seller_scan.py:459` null-title skip,
   `seller_scan.py:163` add the trailing-boundary hardening.

#### Branch 4 — `fix/gixen-purge`
Issues: **BUI-178** + BUI-184 gixen items.
Files: `packages/gixen-cli/server/db.py`, `packages/gixen-cli/server/main.py`,
`packages/gixen-cli/cli.py`; tests in `packages/gixen-cli/tests/`.
Commits:
1. `fix(gixen-server): guard mark_bids_purged against live PENDING rows (BUI-178)` —
   add status filter to the UPDATE; add a test that hands it a PENDING+completed
   pair sharing an item_id.
2. `fix(gixen-server): set auction_end_at for a 0-second time_to_end (BUI-184)` —
   `main.py:472`.
3. `chore(gixen-cli): remove dead _calc_diff (BUI-184)` — `cli.py:186`.

#### Branch 5 — `fix/ezship`
Issues: **BUI-180, BUI-181** + BUI-184 ezship items.
Files: `apps/ezship/src/api.ts` (+ `cli.ts` if needed); tests in `apps/ezship/test/`.
Commits:
1. `fix(ezship): add idempotency key to order submission (BUI-180)` — dedup by
   `trackingNo`; vitest for the duplicate-submit path.
2. `fix(ezship): treat non-success result bodies as failures (BUI-181)` —
   require `result === true`; reject missing/non-boolean/error-shaped bodies.
3. `fix(ezship): add fetch timeout + map all login-redirect codes (BUI-184)` —
   `api.ts:18` AbortController/timeout, `api.ts:30` 301/307/308.

#### Branch 6 — `fix/overlay-errors`
Issues: BUI-184 overlay items only.
Files: `plugins/gixen-overlay/src/gixen_overlay/routes.py`; tests in
`plugins/gixen-overlay/tests/`.
Commits:
1. `fix(overlay): return empty wish-list on corrupt cache, not 500 (BUI-184)` —
   `routes.py:958` also catch `json.JSONDecodeError`.
2. `fix(overlay): surface partial record-win failures clearly (BUI-184)` —
   `routes.py:1120` handle non-`RuntimeError` mid-batch with a useful detail.
3. **DESIGN DECISION (do not just code it):** `routes.py:1165` — the wish-list-add
   409 guard omits `year`, so the year-gated masthead fallback can't catch a book
   stored under its base masthead; but omitting `year` is documented as intentional
   (avoids the BUI-129 false-negative). **Pause and ask the human** which way to
   resolve before changing. If unresolved, leave a code comment + keep the BUI-184
   box unchecked and note it in the PR.

### Phase 2 — Regression prevention (after all Phase-1 PRs merge)

#### Branch 7 — `chore/lint-typecheck-ci`
Issues: **BUI-185, BUI-188**.
Commits (multiple): add `[tool.ruff.lint]` to root + each app/package
`pyproject.toml` (apps aren't workspace members); fix the surfaced bare-except /
falsy-zero sites **per subsystem, one commit each**; add the `lint` CI job; add a
`typecheck` job (`tsc --noEmit` in the ezship job is nearly free; non-strict mypy
on `fmv_runner.py` + overlay `routes.py` to start).

#### Branch 8 — `refactor/fail-loud-http`
Issue: **BUI-186**. Shared `get_json`/`post_json` (timeout + `raise_for_status` +
raise on `RequestException`, never return `{}`/`None`); refactor callsites
(`fmv_runner.py:340` `_upsert_fmv`, `seller_scan.py`, `sold_comps.py`,
`locg/client.py`, `locg/commands.py`); add the shared skill `curl --fail --max-time`
wrapper next to `comics_resolve_server`; generalize the BUI-151 anti-`2>/dev/null`
test. One commit for the helper, one per refactored area.

#### Branch 9 — `refactor/parsing-module`
Issue: **BUI-189**. Consolidate price/issue/grade parsing into one tested module
(Hypothesis property tests). **Fold in the BUI-175/BUI-183 fixes** — move that
logic into the module and keep the existing tests green; do not reintroduce the old
truncating/over-matching behavior.

#### Branch 10 — `test/golden-fixtures`
Issue: **BUI-190**. Frozen matcher fixtures (capture GSFF, leading-article Hulks,
BUI-129 X-Men, plus the BUI-175/176 cases) and FMV-math fixtures (comps→fmv/
confidence/haircut). Bring the FMV baseline into the repo so CI runs it.

#### Branch 11 — `test/contract-harness`
Issue: **BUI-191**. Extend `test_skill_contracts.py` with the new seam guards from
Branches 5/8/2 (skill curl is fail-loud; ezship dedup documented; FMV batch carries
an id).

## 4. Test & build commands (run the ones for the package you touched)

```sh
uv sync --all-packages                                  # once, for workspace dev

cd packages/gixen-cli    && uv run pytest -m "not integration"
cd packages/locg-cli     && uv run pytest
cd plugins/gixen-overlay && uv run pytest
cd apps/ebay             && uv run --with pytest pytest
cd apps/fmv              && uv run --with pytest pytest
cd apps/ezship           && npm ci && npm test            # vitest
# single test:  uv run pytest tests/test_x.py::test_name -q
```

After Phase 2 lands lint/types:
```sh
uvx ruff check .            # from repo root
cd apps/ezship && npx tsc --noEmit
```

## 5. Linear workflow (per the `linear-method` skill)

For each issue, **the first action when you start it**:
```sh
linear issue update BUI-XXX --state "In Progress"
linear issue comment add BUI-XXX --body "Session: $CLAUDE_CODE_SESSION_ID"
```
When its PR is merged:
```sh
linear issue comment add BUI-XXX --body "<what shipped + any decisions>. PR: <url>"
linear issue update BUI-XXX --state "Done"
```
Notes:
- Use the team key `BUI` with `--team` (not the team name).
- **`linear issue create` breaks with `--json`** — omit it and parse the printed
  `BUI-NNN` from stdout. Project descriptions are capped at **255 chars**.
- Never jump Backlog → Done; pass through In Progress (the board/cycle-time
  depends on it).
- BUI-184 is a rollup: tick its checklist boxes as you land each item across the
  subsystem branches; set it Done when all are checked.

## 6. Definition of done (whole project)

- All 18 issues Done (BUI-174…BUI-191); BUI-184 fully checked.
- Each shipped fix has a regression test that fails on the old code.
- `ruff` + type checks green; every package suite green in CI.
- The overlay import canary and the skill-contract harness still pass.
- No new silent-failure paths: every network/subprocess call either succeeds or
  raises (R11).
