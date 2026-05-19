---
title: "feat: Migrate ezship-cli into comic-pipeline as apps/ezship"
type: feat
status: active
date: 2026-05-19
---

# feat: Migrate ezship-cli into comic-pipeline as apps/ezship

**Target repo:** comic-pipeline

## Overview

Move the ezship-cli TypeScript codebase from the standalone `ezship-cli` repo into `comic-pipeline/apps/ezship/`. The app lands as a self-contained directory with its own `package.json` (no workspace root). Archive the original repo when done.

## Problem Frame

`ezship-cli` is a CLI for ezbuy ezShip order management. Like `ebay-fetch`, it belongs in the `comic-pipeline` monorepo under `apps/` so all comic-workflow tooling lives in one place.

## Requirements Trace

- R1. `apps/ezship/` contains all source and test files from `ezship-cli`
- R2. `npm test` passes from within `apps/ezship/`
- R3. `npm run build` produces `dist/` from within `apps/ezship/`
- R4. Cross-language monorepo layout decision is documented (pnpm workspace vs standalone `package.json`)
- R5. Original `hsukenooi/ezship-cli` GitHub repo is archived

## Scope Boundaries

- No behavioral changes to the CLI source — pure migration
- No pnpm workspace root — see Key Technical Decisions
- No CI/CD wiring — each app manages its own dev workflow

## Key Technical Decisions

- **Standalone `package.json` (not pnpm workspace):** The `apps/` directory is a loose collection of independent tools, not a tightly-coupled monorepo. `apps/ebay/` uses self-contained hatchling packaging with no workspace root. pnpm workspace would require a root `package.json` + `pnpm-workspace.yaml` for a single JS app — overhead with no benefit. Standalone `package.json` is consistent, simple, and maintainable.
- **Mirror source layout exactly:** `src/` and `test/` top-level directories (same as origin). All relative imports (`"./auth.js"`, `"../src/auth.js"`) remain valid without modification.

## Context & Research

### Relevant Code and Patterns

- `apps/ebay/` — self-contained Python app with its own `pyproject.toml`; established the per-app isolation pattern
- `apps/ebay/src/ebay_fetch.py` — flat module layout (different from ezship's package layout, but the isolation principle is the same)

### Source Layout (ezship-cli)

```
apps/ezship/
  package.json         # from ezship-cli, name stays "ezship-cli"
  package-lock.json    # copy from origin
  tsconfig.json        # from ezship-cli
  src/
    cli.ts             # Commander entrypoint, `ezship new` and `ezship set-cookie`
    api.ts             # callRpc + submitNewOrder against ezbuy RPC API
    auth.ts            # loadConfig, saveCookie, getHeaders (reads ~/.config/ezship/config.json)
    types.ts            # Config interface, WAREHOUSE_* and CARRIER_MAP constants, mapWarehouse
  test/
    auth.test.ts       # 8 tests: loadConfig happy paths, error paths, getHeaders
    types.test.ts      # 9 tests: WAREHOUSE_VALUES, WAREHOUSE_MAP, mapWarehouse
```

## Output Structure

```
apps/ezship/
├── package.json
├── package-lock.json
├── tsconfig.json
├── src/
│   ├── cli.ts
│   ├── api.ts
│   ├── auth.ts
│   └── types.ts
└── test/
    ├── auth.test.ts
    └── types.test.ts
```

## Implementation Units

- [ ] **Unit 1: Scaffold apps/ezship/**

**Goal:** Copy all source, test, and config files from the origin repo into `apps/ezship/`

**Requirements:** R1, R2, R3, R4

**Dependencies:** None

**Files:**
- Create: `apps/ezship/package.json`
- Create: `apps/ezship/package-lock.json`
- Create: `apps/ezship/tsconfig.json`
- Create: `apps/ezship/src/cli.ts`
- Create: `apps/ezship/src/api.ts`
- Create: `apps/ezship/src/auth.ts`
- Create: `apps/ezship/src/types.ts`
- Create: `apps/ezship/test/auth.test.ts`
- Create: `apps/ezship/test/types.test.ts`

**Approach:**
- Copy files verbatim from `~/Projects/ezship-cli/`; no content changes needed
- All relative imports (`"./auth.js"`, `"../src/auth.js"`) are already correct relative to `src/` and `test/`
- `package.json` keeps `name: "ezship-cli"`, `version: "0.1.0"`, all existing scripts and deps
- Do not add `node_modules/` or `dist/` — install fresh in Unit 2

**Patterns to follow:**
- `apps/ebay/pyproject.toml` — per-app self-contained packaging, no workspace root

**Test scenarios:**
- Test expectation: none — this unit is pure file scaffolding with no behavioral change

**Verification:**
- `apps/ezship/` directory exists with all 9 files listed above
- `package.json` retains original name, scripts, dependencies, and devDependencies

---

- [ ] **Unit 2: Verify build and tests pass**

**Goal:** Confirm the migrated package installs, builds, and passes all tests from its new location

**Requirements:** R2, R3

**Dependencies:** Unit 1

**Files:**
- No new files; run npm commands within `apps/ezship/`

**Approach:**
- `cd apps/ezship && npm install` — resolves deps locally
- `npm run build` — TypeScript compile to `dist/`
- `npm test` — 17 vitest tests across `test/auth.test.ts` and `test/types.test.ts`
- `dist/` and `node_modules/` should be gitignored — add to `apps/ezship/.gitignore` if not already excluded by root `.gitignore`
- Check whether comic-pipeline has a root `.gitignore` that already covers `node_modules/` and `dist/`; if not, create `apps/ezship/.gitignore`

**Patterns to follow:**
- `apps/ebay/` — no generated artifacts committed; dev workflow is local

**Test scenarios:**
- Happy path: `npm test` runs 17 tests (8 in auth.test.ts, 9 in types.test.ts) and all pass
- Happy path: `npm run build` produces `dist/cli.js`, `dist/api.js`, `dist/auth.js`, `dist/types.js`, `dist/*.d.ts`
- Edge case: if any test imports fail with module-not-found, fix relative paths in the test file (not expected but check)

**Verification:**
- `npm test` exits 0 with 17 tests passing
- `npm run build` exits 0 and `dist/cli.js` exists

---

- [ ] **Unit 3: Archive the original repo**

**Goal:** Archive `hsukenooi/ezship-cli` on GitHub

**Requirements:** R5

**Dependencies:** Units 1 and 2 (migration verified before archiving)

**Files:** None (GitHub metadata change only)

**Approach:**
- `gh repo archive hsukenooi/ezship-cli --yes`

**Test scenarios:**
- Test expectation: none — GitHub metadata change, no code behavior

**Verification:**
- `gh repo view hsukenooi/ezship-cli --json isArchived` returns `"isArchived": true`

## System-Wide Impact

- **Unchanged invariants:** ezship CLI behavior, config file path (`~/.config/ezship/config.json`), and command interface (`ezship new`, `ezship set-cookie`) are all unchanged — this is a pure relocation
- **No cross-app dependencies:** `apps/ezship/` is fully isolated; no other app in comic-pipeline imports from it

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| `node_modules/` or `dist/` accidentally committed | Add `apps/ezship/.gitignore` if root doesn't cover them; verify with `git status` before committing |
| GitHub archive is irreversible (repo goes read-only) | Confirm migration passes tests (Unit 2) before archiving (Unit 3) |

## Sources & References

- Related issue: PER-32 (Linear)
- Prior art: PER-31 / `apps/ebay/` migration (same isolation pattern, different language)
- Origin repo: `hsukenooi/ezship-cli` (to be archived)
