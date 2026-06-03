---
title: "refactor: Store LOCG collection and wish-list cache in the repository (BUI-84)"
type: refactor
status: completed
date: 2026-06-03
issue: BUI-84
---

# refactor: Store LOCG collection and wish-list cache in the repository (BUI-84)

## Summary

The LOCG collection, wish-list, ID, and import-history caches currently live in
`~/.cache/locg/`, outside the repo. On a fresh machine or after a cache clear,
`/comic:collection-check` and `/comic:wishlist-add` have no ownership data to
check against. This plan moves the cache into a repo-versioned `data/locg/`
directory so it travels with the pipeline.

The change collapses the two duplicated `_cache_dir()` functions into a single
resolver with an explicit precedence order, defaults that resolver to the repo's
`data/locg/` (located by walking up from the package source — reliable because
`locg` is installed `--editable`), seeds the new directory with the user's
current cache contents, and updates `.gitignore`, tests, and docs to match.

Out of scope: cache file formats, the `locg` command surface, LOCG-network
behavior, and the separate eBay sold-comps cache (`apps/ebay/src/sold_comps.py`,
unrelated).

---

## Problem Frame

`packages/locg-cli/src/locg/cache.py` and `packages/locg-cli/src/locg/config.py`
each define an identical `_cache_dir()` that returns `$XDG_CACHE_HOME/locg`
(default `~/.cache/locg`). All four cache artifacts resolve from there:

| Artifact | Path function | File |
| --- | --- | --- |
| ID cache | `cache_path()` | `ids.json` |
| Collection | `collection_cache_path()` | `collection.json` (+ `.bak.{0,1,2}`, `collection.lock`) |
| Wish-list | `wish_list_cache_path()` | `wish-list.json` |
| Import history | `import_history_path()` | `import-history.jsonl` |

Because the directory lives in the user's home, it is invisible to the repo: not
versioned, not present on a new checkout, and silently empty after `rm -rf
~/.cache`. The fix is to relocate the default to a tracked repo path while
preserving an explicit escape hatch and a safe fallback for non-editable
installs.

**Key enabler:** `scripts/install.sh` installs `locg` with `uv tool install
--editable`, so the package source stays in the repo tree. `Path(__file__)` for
`config.py`/`cache.py` therefore resolves *inside the repo*, letting the resolver
walk up to the repo root deterministically rather than guessing from `cwd` (the
CLI runs from anywhere on `PATH`).

---

## Requirements

- **R1** — The default cache location is a repo-tracked `data/locg/` directory,
  resolved correctly regardless of the caller's working directory.
- **R2** — A single source of truth resolves the cache directory; the duplicated
  `_cache_dir()` is eliminated.
- **R3** — Resolution precedence is explicit and documented:
  `LOCG_DATA_DIR` env override → repo `data/locg/` (repo root found via marker
  walk) → `~/.cache/locg/` fallback (when no repo root is found, e.g. a packaged
  wheel install).
- **R4** — The user's existing `~/.cache/locg/` contents are migrated into
  `data/locg/` and committed, so the versioned baseline is the real collection
  and wish-list, not an empty cache.
- **R5** — `collection.json`, `wish-list.json`, `ids.json`, and
  `import-history.jsonl` are tracked; the transient `collection.lock` and
  `collection.json.bak.*` backups are git-ignored.
- **R6** — Existing tests still pass (conftest monkeypatches the path functions
  directly, so it is insulated from the default-resolution change), plus new
  coverage for the precedence order.
- **R7** — Docs that reference `~/.cache/locg/` or `XDG_CACHE_HOME` for the cache
  are updated to the new location and precedence.

---

## Key Technical Decisions

### KTD1 — Single resolver with three-tier precedence
Replace both `_cache_dir()` copies with one resolver. `cache.py` imports it from
`config.py` (config.py has no dependency on cache.py, so the import direction is
clean and avoids a cycle). Precedence:

1. **`LOCG_DATA_DIR`** — if set, use it verbatim. Explicit override for CI, tests,
   alternate machines, and the multi-worktree case (see Risks).
2. **Repo `data/locg/`** — walk up from `Path(__file__).resolve()` looking for a
   repo-root marker; if found, use `<root>/data/locg`.
3. **`~/.cache/locg/`** — fallback when no marker is found (non-editable / wheel
   install), preserving today's behavior for that case.

**Rationale:** `LOCG_DATA_DIR` is a single, purpose-named override that reads more
clearly than the generic `XDG_CACHE_HOME`. The marker walk is robust to `cwd`
because the editable install keeps source in the repo. The home fallback means a
non-editable install degrades to current behavior rather than crashing.

### KTD2 — Drop `XDG_CACHE_HOME` for the cache directory
The resolver no longer consults `XDG_CACHE_HOME` (confirmed with the user). It was
only ever an override; `LOCG_DATA_DIR` replaces that role with a clearer name.
Tests don't rely on it (they monkeypatch the path functions), so nothing in the
suite breaks. Note: `XDG_CONFIG_HOME` for the *config* dir (`_config_dir()`,
cookies/credentials) is unrelated and stays untouched.

### KTD3 — Repo-root marker: `.git` (file or dir) with a `pyproject.toml` co-check
Walk parents until a directory contains **both** a `.git` entry (a directory in a
normal clone, a *file* in a Conductor worktree — `exists()` covers both) **and** a
root `pyproject.toml`. Requiring both avoids false-positives on nested package
`pyproject.toml` files (e.g. stopping at `packages/locg-cli/`). If the walk
reaches the filesystem root without a match, return the tier-3 home fallback.

### KTD4 — Seed-and-commit migration, no `migrate` subcommand
Migration is a one-time copy of the current `~/.cache/locg/` contents into
`data/locg/` performed during execution and committed in this PR. A
`locg cache migrate` command would be standing machinery for a one-shot move in a
solo workspace — YAGNI. Tracked files: the four JSON/JSONL artifacts. Backups and
the lock are left behind (regenerated locally).

### KTD5 — Commit real collection data
`collection.json` is ~1.8 MB and contains the user's actual collection. Per the
issue ("versioned alongside the rest of the pipeline") and explicit user
confirmation, it is committed. This puts collection data into git history — an
accepted trade-off for a private solo repo.

---

## Output Structure

```
data/
└── locg/
    ├── collection.json          # tracked (seeded from ~/.cache/locg)
    ├── wish-list.json           # tracked
    ├── ids.json                 # tracked
    ├── import-history.jsonl     # tracked
    ├── collection.lock          # git-ignored (transient flock)
    └── collection.json.bak.*    # git-ignored (local backup chain)
```

---

## Implementation Units

### U1. Single repo-relative cache-directory resolver

**Goal:** Replace the two duplicated `_cache_dir()` functions with one resolver
implementing the KTD1 precedence, defaulting to repo `data/locg/`.

**Requirements:** R1, R2, R3, KTD1, KTD2, KTD3

**Dependencies:** none

**Files:**
- `packages/locg-cli/src/locg/config.py` (modify — owns the resolver + a
  `_find_repo_root()` helper; rewrite `_cache_dir()` body)
- `packages/locg-cli/src/locg/cache.py` (modify — delete local `_cache_dir()`,
  import the shared resolver from `config`)

**Approach:**
- Add `_find_repo_root() -> Path | None` to `config.py`: walk
  `Path(__file__).resolve().parents` until a directory has both a `.git` entry
  (`(p / ".git").exists()`) and a `pyproject.toml`; return it, or `None`.
- Rewrite `_cache_dir()` to apply precedence: `LOCG_DATA_DIR` env →
  `<repo_root>/data/locg` when `_find_repo_root()` is not `None` → else
  `~/.cache/locg`. Keep the function name `_cache_dir()` so `cache_path()`,
  `collection_cache_path()`, etc. need no edits.
- In `cache.py`, remove the duplicate `_cache_dir()` and
  `from .config import _cache_dir` (or a public alias). `cache_path()` and
  `ensure_cache_dir()` call the imported resolver.
- `ensure_cache_dir()` already `mkdir(parents=True)` + `chmod 700`; the 700 mode
  is appropriate for `~/.cache` but redundant inside the repo — leave it (a
  repo-local 700 dir is harmless and keeps one code path).

**Patterns to follow:** Mirror existing `_config_dir()`/`_cache_dir()` style in
`config.py` (small pure functions returning `Path`). Keep `from __future__ import
annotations` so `Path | None` works on the pinned Python.

**Technical design** (directional, not specification):
```
def _find_repo_root() -> Path | None:
    for parent in Path(__file__).resolve().parents:
        if (parent / ".git").exists() and (parent / "pyproject.toml").exists():
            return parent
    return None

def _cache_dir() -> Path:
    override = os.environ.get("LOCG_DATA_DIR")
    if override:
        return Path(override)
    root = _find_repo_root()
    if root is not None:
        return root / "data" / "locg"
    return Path(os.path.expanduser("~/.cache")) / "locg"
```

**Test scenarios:** (implemented in U2)
- `LOCG_DATA_DIR` set → resolver returns exactly that path.
- Repo root found → resolver returns `<root>/data/locg`.
- No repo root (simulated) → resolver returns `~/.cache/locg`.
- `cache.py` and `config.py` resolve to the *same* directory (no drift).

**Verification:** `cache_path()`, `collection_cache_path()`,
`wish_list_cache_path()`, `import_history_path()` all sit under one directory; a
`locg cache stats` run from an unrelated `cwd` reports a path inside the repo.

---

### U2. Tests for resolution precedence

**Goal:** Lock in the KTD1 precedence and the cache.py/config.py single-source
behavior.

**Requirements:** R6

**Dependencies:** U1

**Files:**
- `packages/locg-cli/tests/test_cache_paths.py` (new)
- `packages/locg-cli/tests/conftest.py` (verify only — no change expected; the
  autouse fixtures monkeypatch `cache_path`/`collection_cache_path`/etc.
  directly, so they remain correct)

**Approach:** Unit-test `_cache_dir()` / `_find_repo_root()` directly with
`monkeypatch.setenv`/`delenv` and `monkeypatch.setattr` on `__file__` or a patched
`_find_repo_root`. These tests must not depend on the autouse cache-isolation
fixtures (which patch the *public* path functions); test the resolver internals.

**Patterns to follow:** Existing `monkeypatch` usage in `conftest.py`. Use
`tmp_path` to fabricate a fake repo tree (`tmp/.git`, `tmp/pyproject.toml`) for
the marker-walk test.

**Test scenarios:**
- `LOCG_DATA_DIR=/x/y` → `_cache_dir() == Path("/x/y")` (override wins even when a
  repo root exists).
- Fake repo tree with `.git` + `pyproject.toml` → `_find_repo_root()` returns it;
  `_cache_dir()` returns `<root>/data/locg`.
- `.git` present but no root `pyproject.toml` (and a nested `pyproject.toml`
  below) → walk does not stop at the nested package; returns the true root or
  `None`.
- No marker anywhere up-tree (patch `_find_repo_root` to `None`) →
  `_cache_dir()` falls back to `~/.cache/locg`.
- `locg.cache.cache_path()` parent == `locg.config.collection_cache_path()`
  parent (single source of truth).

**Verification:** `cd packages/locg-cli && uv run pytest tests/test_cache_paths.py
-q` passes; full suite `uv run pytest` stays green.

---

### U3. Git tracking + migrate existing cache into `data/locg/`

**Goal:** Create and seed `data/locg/` from the current `~/.cache/locg/`, and set
`.gitignore` so the right artifacts are tracked.

**Requirements:** R4, R5, KTD4, KTD5

**Dependencies:** U1 (so the running CLI reads from the new location after the
copy)

**Files:**
- `data/locg/collection.json` (new — copied from `~/.cache/locg/`)
- `data/locg/wish-list.json` (new — copied)
- `data/locg/ids.json` (new — copied)
- `data/locg/import-history.jsonl` (new — copied)
- `.gitignore` (modify — ignore the lock + backups under `data/locg/`)

**Approach:**
- Copy the four artifacts from `~/.cache/locg/` into `data/locg/` (the live files
  only — not `.bak.*`, not `.lock`).
- Append to `.gitignore`:
  ```
  data/locg/collection.lock
  data/locg/collection.json.bak.*
  ```
- `git add` the four artifacts explicitly. Confirm `git status` shows them tracked
  and shows no `.lock`/`.bak` entries.
- If `~/.cache/locg/` is absent on the executing machine, create `data/locg/` with
  empty-but-valid seed files matching each loader's expected shape (e.g. the empty
  structures `CollectionCache.load()` / wish-list reader tolerate) and note the
  no-source case in the PR description.

**Patterns to follow:** Existing `.gitignore` ignores transient/local artifacts
(`*.sqlite`, `.venv/`); the lock + backups are the same category.

**Test scenarios:** `Test expectation: none — data seeding + gitignore, no
behavioral code.` Validate via verification below.

**Verification:**
- `git check-ignore data/locg/collection.lock data/locg/collection.json.bak.0`
  prints both (ignored).
- `git status --porcelain data/locg/` lists the four tracked artifacts and nothing
  else.
- `locg collection status` and `locg wish-list` (from any `cwd`) report non-empty
  data sourced from `data/locg/`.

---

### U4. Update documentation

**Goal:** Point every doc reference at `data/locg/` and the new precedence;
correct stale `~/.cache/locg/` / `XDG_CACHE_HOME` mentions.

**Requirements:** R7

**Dependencies:** U1

**Files:**
- `packages/locg-cli/README.md` (modify — cache path + add `LOCG_DATA_DIR` /
  precedence note near the existing `cache` section, ~lines 81–91)
- `packages/locg-cli/CLAUDE.md` (modify — lines ~52–55 ID-cache path; line ~134
  `config.py` comment if it describes cache dir)
- `packages/locg-cli/src/locg/cache.py` (modify — module docstring lines 1–10
  describe `$XDG_CACHE_HOME/locg/ids.json`; update to the new default + precedence)
- `.claude/commands/comic/wishlist-add.md` (modify — line ~11 references
  `~/.cache/locg/wish-list.json`)
- `CLAUDE.md` (root — add a one-line note under locg-cli describing the
  repo-versioned cache, only if it currently implies a home-dir cache; check
  before editing)

**Approach:** Replace path strings; add a short precedence list
(`LOCG_DATA_DIR` → repo `data/locg/` → `~/.cache/locg/` fallback) in the README
cache section. Keep edits minimal and factual.

**Test scenarios:** `Test expectation: none — documentation.`

**Verification:** `grep -rn "XDG_CACHE_HOME\|~/.cache/locg" packages/locg-cli
.claude/commands/comic` returns only intentional fallback mentions (the tier-3
note), no stale "default" claims.

---

## Risks & Dependencies

- **Multi-worktree write divergence (medium).** Conductor runs parallel
  workspaces, but only one `locg` editable install exists (whichever ran
  `install.sh` last). Its `__file__` resolves to *that* workspace's repo root, so
  all `locg` writes land in one workspace's `data/locg/` regardless of where the
  user is working — a commit in workspace B won't capture writes made through the
  install pointing at workspace A. **Mitigation:** the `LOCG_DATA_DIR` override
  lets a specific workspace pin `locg` to its own `data/locg/`; document this in
  the README. Inherent to single-PATH-install + worktrees; out of scope to fully
  solve here.
- **Committing a 1.8 MB collection + future churn (low).** Each
  `collection import` rewrites `collection.json`, producing sizable diffs and
  growing history. Accepted per KTD5; revisit with Git LFS only if history bloat
  becomes a real problem.
- **Stale editable install (low).** If `install.sh` hasn't been re-run since the
  merge, the on-PATH `locg` may be a non-editable or old build whose `__file__`
  is outside the repo → tier-3 home fallback, silently using the old location.
  **Mitigation:** call out "re-run `./scripts/install.sh`" in the PR description;
  the `cache stats` path output makes the active location visible.
- **Privacy of committed data (low).** Collection/wish-list JSON enters git
  history; fine for a private repo, but note it so it isn't a surprise if the repo
  is ever shared.

---

## Verification Strategy

1. `cd packages/locg-cli && uv run pytest` — full suite green (U2 added, existing
   tests unaffected).
2. From an unrelated directory, `locg cache stats` and `locg collection status`
   report a `data/locg/` path inside the repo with non-empty data.
3. `git status` / `git check-ignore` confirm the tracked-vs-ignored split (U3).
4. `grep` sweep confirms docs no longer present `~/.cache/locg` as the default
   (U4).
5. Sanity: set `LOCG_DATA_DIR=/tmp/x`, run `locg cache stats`, confirm it honors
   the override; unset and confirm it returns to `data/locg/`.
