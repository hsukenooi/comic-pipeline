---
title: "feat: Move comic skills into comic-pipeline and update paths"
type: feat
status: active
date: 2026-05-19
---

# feat: Move Comic Skills into comic-pipeline and Update Paths

**Target repo:** comic-pipeline

## Overview

Relocate the 9 `/comic:` skill markdown files from the Brain vault (`~/Projects/Brain v3.0/.claude/commands/comic/`) to `comic-pipeline/skills/`. Update all hardcoded `~/Projects/<old-repo>` paths inside the skills. Replace the original vault directory with a symlink pointing at `comic-pipeline/skills/` so future skill development happens in the monorepo.

## Problem Frame

The comic workflow skill files live in the Obsidian Brain vault, not in the `comic-pipeline` monorepo. PER-31 and PER-32 moved the underlying tools (`ebay-fetch`, `ezship-cli`) into `comic-pipeline/apps/`, but the skills still reference the old standalone repo paths. Consolidating skills into `comic-pipeline/skills/` completes the migration and makes `comic-pipeline` the single development home for the comic stack.

## Requirements Trace

- R1. All 9 skill files exist in `skills/` in the `comic-pipeline` repo
- R2. All internal path references updated to point at new monorepo locations
- R3. `~/Projects/Brain v3.0/.claude/commands/comic/` is a symlink to `comic-pipeline/skills/` (so Claude Code continues to serve `/comic:` skills without any Claude config change)
- R4. `/comic:buy`, `/comic:identify`, `/comic:ezship-add`, and other skills remain invocable without error after the move

## Scope Boundaries

- No changes to `gixen-cli` paths (`~/Projects/gixen-cli`) — that repo stays in place
- No changes to `locg-cli` paths (`~/Projects/locg-cli`) — that repo stays in place
- No changes to skill logic, workflow steps, or behavior — pure relocation and path fixups
- No new Claude config or CLAUDE.md changes — the symlink makes the new location transparent

## Key Technical Decisions

- **Symlink direction: vault → monorepo.** The Brain vault directory (`~/Projects/Brain v3.0/.claude/commands/comic/`) becomes the symlink; `comic-pipeline/skills/` is the canonical location. This is correct because: (a) Claude Code resolves `/comic:` skills from `~/.claude/commands/` and vault-linked paths, which continue to work via the symlink; (b) future git commits happen in the monorepo.
- **Path updates in grade.md:** The inline Python script in `grade.md` references `~/Projects/ebay-sniper/.env` with `EBAY_APP_ID`/`EBAY_CERT_ID` env vars. After migration, `ebay-fetch` uses `~/.config/ebay-fetch/config.json` with `EBAY_CLIENT_ID`/`EBAY_CLIENT_SECRET`. Both the dotenv path and the env var names need updating.
- **identify.md run command:** Changes from `cd ~/Projects/ebay-cli && .venv/bin/python ebay_fetch.py` to `cd ~/Projects/comic-pipeline/apps/ebay && python src/ebay_fetch.py`. The venv setup instruction also updates to match.

## Path Update Map

| Old path | New path | Files affected |
|---|---|---|
| `~/Projects/Brain v3.0/.claude/commands/comic/` | `~/Projects/comic-pipeline/skills/` | `buy.md` (4 self-references) |
| `~/Projects/ebay-cli` | `~/Projects/comic-pipeline/apps/ebay` | `identify.md`, `grade.md`, `fmv.md` |
| `~/Projects/ebay-cli/ebay_fetch.py` | `~/Projects/comic-pipeline/apps/ebay/src/ebay_fetch.py` | `grade.md` |
| `~/Projects/ebay-cli/.env` / `~/Projects/ebay-sniper/.env` | `~/.config/ebay-fetch/config.json` | `grade.md`, `fmv.md` |
| `EBAY_APP_ID` / `EBAY_CERT_ID` | `EBAY_CLIENT_ID` / `EBAY_CLIENT_SECRET` | `grade.md` inline Python |
| `~/Projects/ezship-cli` | `~/Projects/comic-pipeline/apps/ezship` | `ezship-add.md` |
| `.venv/bin/python ebay_fetch.py` | `python src/ebay_fetch.py` | `identify.md` |

## Output Structure

```
skills/
├── buy.md
├── identify.md
├── grade.md
├── fmv.md
├── snipe-add.md
├── collection-check.md
├── collection-add.md
├── snipe-show.md
└── ezship-add.md
```

## Implementation Units

- [ ] **Unit 1: Copy skills to comic-pipeline/skills/ and apply path updates**

**Goal:** Populate `skills/` with all 9 skill files with corrected internal paths

**Requirements:** R1, R2

**Dependencies:** None

**Files:**
- Create: `skills/buy.md`
- Create: `skills/identify.md`
- Create: `skills/grade.md`
- Create: `skills/fmv.md`
- Create: `skills/snipe-add.md`
- Create: `skills/collection-check.md`
- Create: `skills/collection-add.md`
- Create: `skills/snipe-show.md`
- Create: `skills/ezship-add.md`

**Approach:**
- Copy files from `~/Projects/Brain v3.0/.claude/commands/comic/` to `skills/`
- Apply path replacements per the Path Update Map above
- `buy.md` has 4 self-references to the Brain vault path (Steps 1, 2, 2.5, 5); all 4 must be updated
- `grade.md` has an inline Python script with `load_dotenv(Path("~/Projects/ebay-sniper/.env").expanduser())` — change to `load_dotenv(Path("~/.config/ebay-fetch/config.json").expanduser())` and update `EBAY_APP_ID` → `EBAY_CLIENT_ID`, `EBAY_CERT_ID` → `EBAY_CLIENT_SECRET`
- `identify.md` has a venv setup instruction referencing `~/Projects/ebay-cli`; update the venv path and the `ebay_fetch.py` invocation
- `snipe-add.md`, `collection-add.md`, `collection-check.md`, `snipe-show.md` reference `~/Projects/gixen-cli` — those paths are unchanged

**Test scenarios:**
- Test expectation: none — pure file copy + text substitution; no behavioral code change

**Verification:**
- All 9 files exist in `skills/`
- No remaining references to `~/Projects/ebay-cli`, `~/Projects/ebay-sniper`, `~/Projects/ezship-cli`, or `~/Projects/Brain v3.0/.claude/commands/comic/`
- `grep -r "ebay-cli\|ebay-sniper\|ezship-cli\|Brain v3.0" skills/` returns empty

---

- [ ] **Unit 2: Replace Brain vault comic dir with symlink to comic-pipeline/skills/**

**Goal:** Replace the original vault directory with a symlink so `/comic:` skills remain discoverable by Claude Code

**Requirements:** R3, R4

**Dependencies:** Unit 1 (skills must exist in comic-pipeline/skills/ first)

**Files:** None in the repo — this is a filesystem operation outside the git tree

**Approach:**
- Remove the directory at `~/Projects/Brain v3.0/.claude/commands/comic/` (after verifying `skills/` contains all 9 files)
- Create symlink: `ln -s ~/Projects/comic-pipeline/skills ~/Projects/Brain\ v3.0/.claude/commands/comic`
- The symlink target uses the absolute path to `comic-pipeline/skills/`

**Test scenarios:**
- Happy path: `ls ~/Projects/Brain v3.0/.claude/commands/comic/` resolves to the 9 skill files in comic-pipeline/skills/
- Happy path: `readlink ~/Projects/Brain v3.0/.claude/commands/comic` returns the comic-pipeline/skills path
- Edge case: If the original vault directory contains any files not already in comic-pipeline/skills/ (unexpected additions), list them before deleting

**Verification:**
- `~/Projects/Brain v3.0/.claude/commands/comic/` is a symlink (not a directory)
- The symlink resolves to all 9 skill files
- `/comic:buy` remains invocable (Claude Code can read the skill from the symlinked path)

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| Skill self-reference in buy.md misses an occurrence | Grep for all remaining Brain vault path refs after copy; fix before commit |
| grade.md env var rename breaks eBay API calls | The migrated ebay_fetch.py confirmed to use EBAY_CLIENT_ID/EBAY_CLIENT_SECRET; the inline Python in grade.md mirrors the same naming |
| Symlink points to relative path that breaks if cwd changes | Use absolute path in `ln -s` |

## Sources & References

- Related issue: PER-33 (Linear)
- Prior art: PER-31 (apps/ebay), PER-32 (apps/ezship)
- Handoff context: `docs/refactor-split-handoff.md` (PER-33 section)
