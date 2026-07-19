# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

`comic-pipeline` is a **monorepo** for the comic-collecting use case. It bundles two CLIs that used to live in separate repos (grafted under `packages/` with full history preserved) plus the comic-specific glue:

- `packages/gixen-cli/` — eBay auction sniping: the FastAPI server + `bids` SQLite table + dashboard. Exposes the `gixen` console script.
- `packages/locg-cli/` — League of Comic Geeks collection/wish-list cache + matcher. Exposes the `locg` console script (`locg collection ...`). The store resolves via `config._cache_dir`: `LOCG_DATA_DIR` env → `<repo>/data/locg` → `~/.cache/locg`. **As of BUI-87/BUI-93 the source of truth is the comics server (on the Mac Mini), not `data/locg/`** — the overlay serves the collection + wish-list over `/api/comics/*` from a server-owned store, and `data/locg/` is now gitignored (a local-only working cache, not repo-versioned). The `locg` package + console script keep their names (R13) — they genuinely are the LOCG integration (Playwright login, XLSX import, the record-win→CSV→LOCG round-trip).
- `plugins/gixen-overlay/` — a gixen-cli **plugin** (Python) that adds the `/comics` dashboard tab, comic-specific tables, and `/api/comics/*` endpoints.
- `apps/` — standalone CLIs: `ebay` + `fmv` (Python), `ezship` (TypeScript).
- `.claude/commands/comic/` — the `/comic:*` Claude Code skills that orchestrate the whole buying workflow by shelling out to the console scripts (`gixen`, `locg`, `ebay-*`, `comic-fmv`) and endpoints.

A root **uv workspace** (`packages/*` + `plugins/*`) editable-installs these into one shared environment, so imports resolve without path hacks. The overlay → gixen-cli coupling that used to span repos is now a normal intra-repo dependency: a rename and its caller change atomically in one commit, caught by one CI run. (`apps/*` stay `uv tool install`-managed — they shell out on PATH and have no cross-import problem; see `scripts/install.sh`.) The package boundaries still exist (`git subtree split` can re-extract them); they're just no longer separate repos.

## Commands

The repo is a **uv workspace**: `packages/*` + `plugins/*` are members, and `uv sync` from the root creates one shared `.venv` (Python pinned via `.python-version`). `apps/*` are **not** workspace members — they're installed separately via `uv tool install` (see `scripts/install.sh`). Each package still has its own `pyproject.toml`; there is no repo-wide test runner — test each package from its own directory with `uv run pytest`.

```sh
# Install the user-facing CLIs (ebay-fetch, ebay-sold-comps, seller-scan, comic-fmv, gixen, locg)
./scripts/install.sh            # uv tool install for apps/ebay + apps/fmv + packages/gixen-cli + packages/locg-cli
# Re-run this (or `uv tool install --force --no-cache ./packages/<pkg>`) on the Mac Mini after merging any packages/* change —
# a uv tool install is a frozen copy and goes stale (BUI-365: a post-merge `gixen add` crashed with
# `ModuleNotFoundError: No module named 'record_win_prep'` until reinstalled). Plain `--force` is NOT
# enough when the package version is unchanged (BUI-455): uv keys its wheel cache on name+version, so
# `--force` alone silently reinstalls the STALE cached wheel — e.g. after merging BUI-435 (adds `gixen
# build-batch`), `uv tool install --force ./packages/gixen-cli` still reported "No such command" because
# it served the pre-merge wheel. `--no-cache` (or `uv cache clean <pkg>` first) is required to actually
# pick up new source. `scripts/install.sh` itself is unaffected: it uses `--reinstall`, which implies
# `--refresh` and busts the cache, so it already picks up fresh source without `--no-cache`.
# After merging overlay/server changes (gixen-cli server/, plugins/gixen-overlay), additionally (BUI-377):
#   uv sync --all-packages
#   launchctl kickstart -k gui/$(id -u)/com.gixen.server
# (the comics server runs via launchd out of the workspace .venv, which install.sh does NOT refresh;
# the loaded launchd label is still com.gixen.server — the BUI-220 comics-server rename never reached it, BUI-425)
# The comics server is also unaffected by the BUI-455 stale-wheel trap above — it runs from this same
# editable workspace .venv (source, not a frozen wheel); only the frozen `uv tool install`ed console
# scripts (gixen, locg, comic-identify, grade-photos) can go stale.

# Sync the workspace env (packages/* + plugins/*) for development + tests
uv sync --all-packages

# Python tests (run from the package dir)
cd packages/gixen-cli    && uv run pytest -m "not integration"
cd packages/locg-cli     && uv run pytest
cd plugins/gixen-overlay && uv run pytest

# Single test
uv run pytest tests/test_sold_comps.py::test_name -q

# ezship (TypeScript / Node, in apps/ezship)
npm test            # vitest run
npm run build       # tsc -> dist/
npm run dev         # tsx src/cli.ts
```

CI (`.github/workflows/ci.yml`, BUI-140) runs the per-package test suites as the merge gate: the `workspace` job runs the `gixen-cli`/`locg-cli`/`gixen-overlay` pytest suites (plus a `plugin.py` AST smoke-parse), and `apps-python` runs `apps/ebay` + `apps/fmv` (each with `uv run --with pytest pytest`). Also on CI: `lint` (ruff exception-hygiene), `ezship` (tsc + vitest), and `typecheck` (BUI-188: non-strict mypy over `fmv_runner.py` + `routes.py`). Note `typecheck` is a **non-required** check — its failures don't block merges, so a type error can sit red on `main`; check `gh pr checks` rather than just mergeability. Still run the relevant package's tests locally before committing.

## Architecture

### The FMV pipeline shells out across package boundaries
`comic-fmv` (apps/fmv) does **not** import eBay code — at runtime it shells out to the `ebay-sold-comps` **console script** installed on PATH. So both `apps/ebay` and `apps/fmv` must be `uv tool install`ed for FMV to work end to end (that's what `scripts/install.sh` guarantees). A `ModuleNotFoundError` or "command not found" from `comic-fmv` usually means the install step was skipped or a stale wrapper is shadowing the uv-installed binary (see BUI-27, documented in install.sh).

### gixen-overlay is a plugin, not a standalone server
`plugins/gixen-overlay/src/gixen_overlay/plugin.py` registers via gixen-cli's `gixen.plugins` hookspec (`register_db_tables`, `register_routes`, `register_dashboard_tabs`). It has **no server of its own** — gixen-cli loads it. The overlay declares `gixen-cli` as a workspace dependency (`[tool.uv.sources] workspace = true`), so it imports the host (`server.*`, `gixen.plugins`) via the editable workspace install — no `pythonpath` hack.

**The overlay → gixen-cli coupling is load-bearing but now atomically changeable:** `routes.py` imports `server.main`'s `_ensure_fresh_sync` and `_spawn_fallback_task` (private — they mutate gixen-cli lifecycle globals), the public `iso_to_relative`, and `server.db`'s public `get_bid_by_item_id`/`resolve_server_dir`/`TOMBSTONE_STATUSES_SQL`. Since the merge, a rename of one of these in `packages/gixen-cli` and its caller in `routes.py` land in the **same commit** and the same CI run — the canary `plugins/gixen-overlay/tests/test_workspace_imports.py` fails loudly if the surface drifts. (The deeper smell — reaching into gixen-cli internals at all — survives the merge; it's made visible, not dissolved.) The comic tables (`comics`, `fmv`, `bid_fmvs`) live in the plugin's `db.py` but JOIN against gixen-cli's `bids` table — one shared SQLite DB.

**Endpoint parity matters:** `/api/comics/snipes` and `/api/comics/history` both read the shared `bids` table and must apply the same status filters (notably excluding the tombstone via `status NOT IN (<TOMBSTONE_STATUSES_SQL>)` — the shared `'PURGED', 'REMOVED'` constant in `server.db`, centralized in BUI-272). A drift here caused the BUI-50 false-"won" bug — see `docs/solutions/ui-bugs/purged-snipes-shown-as-won-2026-06-01.md`.

### The collection + wish-list are served from the comics server (BUI-87)
The overlay wraps locg-cli's existing collection/wish-list functions (the matcher with its four documented bugfixes, plus the three write paths) behind `/api/comics/*` — it does **not** port any of it to SQL (no `/comic:*` skill needs a relational JOIN against the collection). Reads: `GET /api/comics/collection/{check,status,export}`, `POST /api/comics/collection/check/batch` (BUI-204: a list of `{series, issue, year?}` pairs → one result per pair, same matcher/verdict shape as the single-item check; preserves R11 by 409-ing the whole call against a never-imported store — used by `/comic:wishlist-add` to replace the serial per-issue fan-out), `GET /api/comics/wish-list`, `GET /api/comics/wish-list/conflicts` (BUI-130: wish-list items already owned — the dry-run audit). Writes: `POST /api/comics/collection/import`, `POST /api/comics/collection/record-win/commit` (BUI-428: merges `{wins, resolved_reviews}` server-side, records via `cmd_collection_record_win`, and — only on full success, never on a `partial_failure` — marks exactly the item_ids it just committed seen and folds in a fresh `pending_push_count`/`oldest_pending_days`; collapses `/comic:collection-add`'s old inline-merge + client-side mark-seen + separate status re-fetch into one atomic call), `POST /api/comics/wish-list`, `POST /api/comics/wish-list/remove-conflicts` (BUI-130: bulk-remove the audited conflicts in one call). Endpoint names are **provider-neutral** (never `/api/comics/locg/*`). **A wish-listed book you already own is the BUI-122 data-loss trigger** — `/comic:collection-sync` exports it with `In Collection=0`, which tells LOCG to *remove* it from the collection. So `POST /api/comics/wish-list` rejects an already-owned title with **409** (pass `force=true` to override), and the conflicts audit/remove pair retroactively finds and clears any that slipped in (e.g. via a pre-guard add). The audit never forwards a series start-year as `year` (that was the BUI-129 bug that hid 16 owned X-Men); both conflict endpoints 409 if the collection was never imported (R11 — an empty store would falsely report zero conflicts). The server points locg-cli's store at a server-owned, neutrally-named dir via `_ensure_collection_store()` in `routes.py` (sets `LOCG_DATA_DIR` → `<dir(DB_PATH)>/collection-store` when unset). **`collection-check` must hard-fail on an unreachable server — never render "not owned" from a failed call (R11), or it buys a duplicate.** **Never use the `locg collection check` CLI directly for ownership checks — it reads the MacBook's local store, which is never seeded and always returns `not_in_cache`. Always use `curl $COMICS_SERVER_URL/api/comics/collection/check?series=...&issue=...` to hit the Mac Mini's authoritative store.** `seller-scan` lives in non-workspace `apps/ebay`, so it fetches the wish-list over HTTP, not by importing locg. **One-time server seed** (run once on the Mac Mini): `mkdir -p ~/.gixen-server/collection-store && cp data/locg/{collection,wish-list}.json ~/.gixen-server/collection-store/`. `ids.json` stays local (only `locg lookup` uses it).

### `bids.status` lifecycle (owned by gixen-cli)
Statuses: `PENDING → WON/LOST/ENDED/FAILED`, plus the soft-delete tombstone. **The tombstone is `REMOVED`** (renamed from `PURGED` in BUI-49); it is written when a live snipe is removed (`delete_bid`), when completed bids are swept (`mark_bids_purged`), and — since BUI-371 — at three classification sites in `packages/gixen-cli/server/main.py` (`_sync_gixen`'s group-cancel check, `_sync_gixen`'s vanished-while-live check, and `_run_ebay_fallback`'s cancelled-before-end check) whenever positive evidence shows a bid-group sibling was cancelled before its own auction ended. None of these is a terminal auction outcome. Filter the tombstone out of any user-facing "results" view. A `notes` marker records *why* a given row was tombstoned so the causes can be told apart in a later audit: `"cancelled before end BUI-371"` for the three classification sites above, versus the pre-existing `"deduped BUI-67"` for the unrelated duplicate-PENDING-row collapse. The overlay tolerates **both** `'PURGED'` and `'REMOVED'` so it stays correct whether or not the gixen-cli rename migration has run (package version skew). BUI-49 chose a pure rename (Option A), not splitting live-cancel vs completed-sweep into distinct statuses.

### The `/comic:*` skill workflow
`/comic:buy` is the orchestrator; it reads and runs the leaf skills in sequence with a user gate at each step: **identify → collection-check → (conditional) grade → fmv → snipe-add**. Leaf skills are also usable standalone. The skills shell out to:
- the `gixen` console script for snipe add/edit/list/fmv (run adds **sequentially** — Gixen sessions are stateful, parallel adds fail).
- `locg collection check` / wish-list for ownership (fully offline against a local cache that can lag LOCG by N days).
- the overlay's `/api/comics/*` endpoints for FMV linking (`bids → bid_fmvs → fmv → comics`).

`skills/` is a symlink to `.claude/skills/`; the runnable skill bodies are in `.claude/commands/comic/`.

## Conventions

- **Naming (BUI-220): Gixen names the bidding service only; the thing that stores your data is the comics server, which runs on the Mac Mini.** Keep "gixen" wording only for the genuine bidding side — the `gixen` console script, the `bids` table, snipe/sniping operations, and gixen.com itself. Our self-hosted FastAPI app is **the comics server** (never "the gixen server"), and the box it runs on is **the Mac Mini**. The canonical env var for its URL is **`COMICS_SERVER_URL`**; `GIXEN_SERVER_URL` is a deprecated alias still accepted. See `CONCEPTS.md` → "Naming" for the full vocabulary.
- **Linear issues use the `BUI` (Build) team** for work in this repo; `PER` for personal. Issue IDs (`BUI-50`, `PER-140`) are referenced throughout commits, docs, and code comments — look them up with `linear issue view <ID>`.
- **Branch + commit per issue.** Don't commit directly to `main` for feature/fix work.
- `docs/solutions/` — documented solutions to past problems (bugs, best practices, workflow patterns), organized by category with YAML frontmatter (`module`, `tags`, `problem_type`). Relevant when implementing or debugging in documented areas, especially gixen-overlay endpoints and the FMV linkage chain. `docs/plans/` and `docs/brainstorms/` hold per-feature planning history.
- `CONCEPTS.md` (repo root) — shared domain vocabulary (entities, named processes, status concepts: the Collection, win-sourced vs import-sourced entries, pending push, the collection sync). Relevant when orienting to the codebase or discussing domain concepts.
