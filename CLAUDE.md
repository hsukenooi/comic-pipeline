# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

`comic-pipeline` is a **monorepo** for the comic-collecting use case. It bundles two CLIs that used to live in separate repos (grafted under `packages/` with full history preserved) plus the comic-specific glue:

- `packages/gixen-cli/` — eBay auction sniping: the FastAPI server + `bids` SQLite table + dashboard. Exposes the `gixen` console script.
- `packages/locg-cli/` — League of Comic Geeks collection/wish-list cache + matcher. Exposes the `locg` console script (`locg collection ...`). The store resolves via `config._cache_dir`: `LOCG_DATA_DIR` env → `<repo>/data/locg` → `~/.cache/locg`. **As of BUI-87/BUI-93 the source of truth is the comics server (on the Mac Mini), not `data/locg/`** — the overlay serves the collection + wish-list over `/api/comics/*` from a server-owned store, and `data/locg/` is gitignored (a local-only working cache, not repo-versioned). The `locg` package + console script keep their names (R13) — they genuinely are the LOCG integration (Playwright login, XLSX import, the record-win→CSV→LOCG round-trip).
- `plugins/gixen-overlay/` — a gixen-cli **plugin** (Python) that adds the `/comics` dashboard tab, comic-specific tables, and `/api/comics/*` endpoints.
- `apps/` — standalone CLIs: `ebay` + `fmv` (Python), `ezship` (TypeScript).
- `.claude/commands/comic/` — the `/comic:*` Claude Code skills that orchestrate the whole buying workflow by shelling out to the console scripts (`gixen`, `locg`, `ebay-*`, `comic-fmv`) and endpoints.

A root **uv workspace** (`packages/*` + `plugins/*`) editable-installs these into one shared environment, so imports resolve without path hacks. The overlay → gixen-cli coupling that used to span repos is now a normal intra-repo dependency: a rename and its caller change atomically in one commit, caught by one CI run. (`apps/*` stay `uv tool install`-managed — they shell out on PATH and have no cross-import problem; see `scripts/install.sh`.) The package boundaries still exist (`git subtree split` can re-extract them); they're just not separate repos.

## Commands

The repo is a uv workspace (`packages/*` + `plugins/*`); `apps/*` are installed with `uv tool install`. Test each package from its own directory with `uv run pytest` (`apps/*` need `uv run --with pytest pytest`). Key commands: `./scripts/install.sh` (user CLIs), `./scripts/deploy.sh` (full deploy ritual plus SHA assertion on the Mac Mini, BUI-612), `uv sync --all-packages`, `./scripts/gates.sh` (local required CI gates), `./scripts/solutions-lint`. The full command block, CI jobs, and branch protection (`typecheck` is deliberately non-required: check `gh pr checks`, not just mergeability) are in [docs/reference/commands-and-ci.md](docs/reference/commands-and-ci.md).

## Architecture

### FMV pipeline

`comic-fmv` (apps/fmv) shells out to the `ebay-sold-comps` console script on PATH, so both `apps/ebay` and `apps/fmv` must be `uv tool install`ed. Sold comps come from sold-comps.com only; `SOLD_COMPS_KEY` in `apps/ebay/.env` is required (exit 2 without it). **Deploy the comics server before a client that speaks certifier (BUI-930):** an older server drops the `certifier`/`label` identity, so `comic-fmv` refuses certified rows (`skipped_schema_mismatch`) rather than risk a mispriced slab. The same deploy-server-first rule applies to `FMV_CEILING_CAP`, the median bid cap, and the published slab band. Full pipeline text, policy checks, env vars (`POLICY_FMV_MULTIPLE`, `POLICY_FMV_STALE_DAYS`, `FMV_CEILING_CAP`), median cap, best-offer comps, and slab band: [docs/reference/fmv-pipeline-and-policy.md](docs/reference/fmv-pipeline-and-policy.md).

**Soak gate:** do not set any `POLICY_BLOCK_*` flag until reviewing `GET /api/decisions` after real buys; keep `POLICY_BLOCK_OVER_FMV` off while lot briefs remain single-identity.

### gixen-overlay is a plugin

`plugins/gixen-overlay` has no server of its own: gixen-cli loads it through the `gixen.plugins` hookspecs and it shares one SQLite DB with the `bids` table. Its `routes.py` reaches into gixen-cli internals, so a rename in `packages/gixen-cli` and its caller land in one commit (canary: `plugins/gixen-overlay/tests/test_workspace_imports.py`). Hook list and coupling detail: [docs/reference/gixen-overlay-plugin.md](docs/reference/gixen-overlay-plugin.md).

**Endpoint parity matters:** `/api/comics/snipes` and `/api/comics/history` both read the shared `bids` table and must apply the same status filters (notably excluding the tombstone via `status NOT IN (<TOMBSTONE_STATUSES_SQL>)` — the shared `'PURGED', 'REMOVED'` constant in `server.db`, centralized in BUI-272). A drift here caused the BUI-50 false-"won" bug — see `docs/solutions/ui-bugs/purged-snipes-shown-as-won-2026-06-01.md`.

### Collection and wish-list live on the comics server (BUI-87)

The authoritative store is on the Mac Mini (`~/.comics-server/collection-store`), served over `/api/comics/*`. Never copy `data/locg/*.json` over it (recover through `POST /api/comics/collection/{backup,restore}`).

- **R11:** `collection-check` must hard-fail on an unreachable server. Never render "not owned" from a failed call, or it buys a duplicate.
- **Never run bare `locg collection check`.** It reads a local store that is not the server's and returns a false `not_in_cache`. Use `locg collection check-batch` (BUI-504) or the `comics-api` wrapper (BUI-510), not hand-rolled `curl $COMICS_SERVER_URL/...` (silently hits an empty host when unset, BUI-352).
- **A wish-listed book you already own is the BUI-122 data-loss trigger:** the collection sync would export it with `In Collection=0` and LOCG removes it. `POST /api/comics/wish-list` therefore 409s on an owned title (override with `force=true`).
- Endpoint names are provider-neutral (never `/api/comics/locg/*`).

Full endpoint list and behavior: [docs/reference/collection-wishlist-api.md](docs/reference/collection-wishlist-api.md).

### `bids.status` lifecycle (owned by gixen-cli)
Statuses: `PENDING → WON/LOST/ENDED/FAILED`, plus the soft-delete tombstone. **The tombstone is `REMOVED`** (renamed from `PURGED` in BUI-49); it is written when a live snipe is removed (`delete_bid`), when completed bids are swept (`mark_bids_purged`), and — since BUI-371 — at three classification sites in `packages/gixen-cli/server/main.py` (`_sync_gixen`'s group-cancel check, `_sync_gixen`'s vanished-while-live check, and `_run_ebay_fallback`'s cancelled-before-end check) whenever positive evidence shows a bid-group sibling was cancelled before its own auction ended. None of these is a terminal auction outcome. Filter the tombstone out of any user-facing "results" view. A `notes` marker records *why* a given row was tombstoned so the causes can be told apart in a later audit: `"cancelled before end BUI-371"` for the three classification sites above, versus the pre-existing `"deduped BUI-67"` for the unrelated duplicate-PENDING-row collapse. The overlay tolerates **both** `'PURGED'` and `'REMOVED'` so it stays correct whether or not the gixen-cli rename migration has run (package version skew). BUI-49 deliberately kept a single tombstone status rather than splitting live-cancel from completed-sweep.

### The `/comic:*` skill workflow
`/comic:buy` is the orchestrator; it reads and runs the leaf skills in sequence with a user gate at each step: **identify → collection-check → (conditional) grade → fmv → snipe-add**. Leaf skills are also usable standalone. The skills shell out to:
- the `gixen` console script for snipe add/edit/list/fmv (run adds **sequentially** — Gixen sessions are stateful, parallel adds fail).
- `locg collection check-batch` (BUI-504) or the `comics-api` wrapper (BUI-510) for ownership, and the `/api/comics/wish-list*` endpoints for wish-list — both hit the comics server's authoritative store on the Mac Mini, never a local offline cache (bare `locg collection check` is forbidden; see "The collection + wish-list are served from the comics server" above).
- the overlay's `/api/comics/*` endpoints for FMV linking (`bids → bid_fmvs → fmv → comics`).

The scan tools (`seller-scan`, `wishlist-sellers`) reject a certified (CGC/CBCS) listing by default — pass `--include-graded` (BUI-932, off by default) to surface slabs instead.

`skills/` is a symlink to `.claude/skills/`; the runnable skill bodies are in `.claude/commands/comic/`.

## Conventions

- **Naming (BUI-220): Gixen names the bidding service only; the thing that stores your data is the comics server, which runs on the Mac Mini.** Keep "gixen" wording only for the genuine bidding side — the `gixen` console script, the `bids` table, snipe/sniping operations, and gixen.com itself. Our self-hosted FastAPI app is **the comics server** (never "the gixen server"), and the box it runs on is **the Mac Mini**. The canonical env var for its URL is **`COMICS_SERVER_URL`**; `GIXEN_SERVER_URL` is a deprecated alias still accepted. See `CONCEPTS.md` → "Naming" for the full vocabulary.
- **Linear issues use the `BUI` (Build) team** for work in this repo; `PER` for personal. Issue IDs (`BUI-50`, `PER-140`) are referenced throughout commits, docs, and code comments — look them up with `linear issue view <ID>`.
- **Branch + commit per issue.** Don't commit directly to `main` for feature/fix work.
- **`docs/solutions/` conventions.** Every new doc must declare `mechanized_by:` (`lint`, `test`, or `advice-only`, with its companion key) and is checked by `./scripts/solutions-lint`. Stamp `status:` when evidence shows a doc is wrong or replaced. Never add a new doc to `UNMECHANIZED.txt`. Full contracts: [docs/reference/solutions-conventions.md](docs/reference/solutions-conventions.md).
- `CONCEPTS.md` (repo root) — shared domain vocabulary (entities, named processes, status concepts: the Collection, win-sourced vs import-sourced entries, pending push, the collection sync). Relevant when orienting to the codebase or discussing domain concepts.
