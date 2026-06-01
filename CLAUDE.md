# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

`comic-pipeline` is the comic-collecting use case built on top of two sibling CLIs that live in **separate repos**:

- `~/Projects/gixen-cli` — eBay auction sniping (the FastAPI server + `bids` SQLite table + dashboard this plugin extends).
- `~/Projects/locg-cli` — League of Comic Geeks collection/wish-list cache (`locg collection ...`).

This repo holds three things that wire those together for comics:
- `plugins/gixen-overlay/` — a gixen-cli **plugin** (Python) that adds the `/comics` dashboard tab, comic-specific tables, and `/api/comics/*` endpoints.
- `apps/` — standalone CLIs: `ebay` + `fmv` (Python), `ezship` (TypeScript).
- `.claude/commands/comic/` — the `/comic:*` Claude Code skills that orchestrate the whole buying workflow by shelling out to the CLIs and endpoints.

Cross-repo work is common. When a skill or the plugin breaks, the cause is often in `gixen-cli` or `locg-cli`, not here.

## Commands

Each Python package is independent (own `pyproject.toml`, own `.venv`), built with **hatchling** and managed with **uv**. There is no repo-wide test runner — test each package from its own directory.

```sh
# Install the user-facing CLIs (ebay-fetch, ebay-sold-comps, seller-scan, comic-fmv)
./scripts/install.sh            # uv tool install --reinstall for apps/ebay + apps/fmv

# Python tests (run from the package dir; uv resolves pytest + pythonpath from pyproject.toml)
cd apps/ebay             && uv run pytest
cd apps/fmv              && uv run pytest
cd plugins/gixen-overlay && uv run pytest

# Single test
uv run pytest tests/test_sold_comps.py::test_name -q

# ezship (TypeScript / Node, in apps/ezship)
npm test            # vitest run
npm run build       # tsc -> dist/
npm run dev         # tsx src/cli.ts
```

CI (`.github/workflows/ci.yml`) only AST-parses `plugin.py` as a smoke check — it does **not** run the test suites. Run the relevant package's tests locally before committing.

## Architecture

### The FMV pipeline shells out across package boundaries
`comic-fmv` (apps/fmv) does **not** import eBay code — at runtime it shells out to the `ebay-sold-comps` **console script** installed on PATH. So both `apps/ebay` and `apps/fmv` must be `uv tool install`ed for FMV to work end to end (that's what `scripts/install.sh` guarantees). A `ModuleNotFoundError` or "command not found" from `comic-fmv` usually means the install step was skipped or a stale wrapper is shadowing the uv-installed binary (see BUI-27, documented in install.sh).

### gixen-overlay is a plugin, not a standalone server
`plugins/gixen-overlay/src/gixen_overlay/plugin.py` registers via gixen-cli's `gixen.plugins` hookspec (`register_db_tables`, `register_routes`, `register_dashboard_tabs`). It has **no server of its own** — gixen-cli loads it. The plugin's tests set `pythonpath = ["src", "/Users/hsukenooi/Projects/gixen-cli"]` so they can import the host.

**Cross-repo coupling is load-bearing and fragile:** `routes.py` imports private helpers from gixen-cli's `server.main` (`_ensure_fresh_sync`, `_spawn_fallback_task`, `_iso_to_relative`) and `server.db` (`get_bid_by_item_id`). If those are renamed upstream the plugin fails at import time. The comic tables (`comics`, `fmv`, `bid_fmvs`) live in the plugin's `db.py` but JOIN against gixen-cli's `bids` table — one shared SQLite DB.

**Endpoint parity matters:** `/api/comics/snipes` and `/api/comics/history` both read the shared `bids` table and must apply the same status filters (notably `status != 'PURGED'`). A drift here caused the BUI-50 false-"won" bug — see `docs/solutions/ui-bugs/purged-snipes-shown-as-won-2026-06-01.md`.

### `bids.status` lifecycle (owned by gixen-cli)
Statuses: `PENDING → WON/LOST/ENDED/FAILED`, plus `PURGED`. **`PURGED` is a soft-delete tombstone**, written either when a live snipe is removed (`delete_bid`) or when completed bids are swept (`mark_bids_purged`) — it is *not* a terminal auction outcome. Filter it out of any user-facing "results" view. (Conflation of those two meanings is tracked in BUI-49.)

### The `/comic:*` skill workflow
`/comic:buy` is the orchestrator; it reads and runs the leaf skills in sequence with a user gate at each step: **identify → collection-check → (conditional) grade → fmv → snipe-add**. Leaf skills are also usable standalone. The skills shell out to:
- `gixen-cli/cli.py` for snipe add/edit/list (run adds **sequentially** — Gixen sessions are stateful, parallel adds fail).
- `locg collection check` / wish-list for ownership (fully offline against a local cache that can lag LOCG by N days).
- the overlay's `/api/comics/*` endpoints for FMV linking (`bids → bid_fmvs → fmv → comics`).

`skills/` is a symlink to `.claude/skills/`; the runnable skill bodies are in `.claude/commands/comic/`.

## Conventions

- **Linear issues use the `BUI` (Build) team** for work in this repo; `PER` for personal. Issue IDs (`BUI-50`, `PER-140`) are referenced throughout commits, docs, and code comments — look them up with `linear issue view <ID>`.
- **Branch + commit per issue.** Don't commit directly to `main` for feature/fix work.
- `docs/solutions/` — documented solutions to past problems (bugs, best practices, workflow patterns), organized by category with YAML frontmatter (`module`, `tags`, `problem_type`). Relevant when implementing or debugging in documented areas, especially gixen-overlay endpoints and the FMV linkage chain. `docs/plans/` and `docs/brainstorms/` hold per-feature planning history.
