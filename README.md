# comic-pipeline

A **monorepo** for the comic-collecting workflow: the eBay-sniping and League-of-Comic-Geeks CLIs (`gixen-cli`, `locg-cli`), the `gixen-overlay` plugin, standalone apps, and the `/comic:*` Claude Code skills that tie them together. The `gixen-cli` and `locg-cli` packages were previously separate repos; they were merged in (full history preserved) and can be re-extracted via `git subtree split`.

## Structure

- `packages/gixen-cli/` — eBay auction sniping CLI + FastAPI server + dashboard (`gixen` console script)
- `packages/locg-cli/` — League of Comic Geeks collection/wish-list cache (`locg` console script)
- `plugins/gixen-overlay/` — FastAPI routes, SQLite tables, and dashboard tab for the comic sniping workflow
- `apps/` — standalone apps: `ebay` + `fmv` (Python), `ezship` (TypeScript)
- `.claude/commands/comic/` — Claude Code skills for the comic workflow (`/comic:buy`, `/comic:grade`, etc.)

`packages/*` and `plugins/*` form a root **uv workspace** (one shared env via `uv sync`); `apps/*` are installed separately via `uv tool install`.

## Installing the Python CLIs

`scripts/install.sh` `uv tool install`s every user-facing console script into
`~/.local/bin`: `ebay-fetch`, `ebay-sold-comps`, `seller-scan` (apps/ebay),
`comic-fmv` (apps/fmv), `gixen` (packages/gixen-cli), and `locg`
(packages/locg-cli). Install with [uv](https://docs.astral.sh/uv/):

```sh
./scripts/install.sh
```

It also removes any stale wrappers (the BUI-27 failure mode). After it finishes,
the CLIs work from any directory:

```sh
comic-fmv --help
gixen --help
locg --help
```

`comic-fmv` shells out to `ebay-sold-comps` at runtime, so install both. No
`PYTHONPATH` workaround is needed. For development against the workspace
(running the server, the plugin, or the suites), use `uv sync --all-packages`.

## Plugin: gixen-overlay

Provides the `/comics` dashboard tab and comic-specific endpoints:

- `GET /comics` — active + ended snipes enriched with condition, FMV range, and (for active rows) a current-vs-FMV value % signal. Lots collapse to one row with aggregated FMV and a `lot of N` badge. The page reuses gixen-cli's snipes-dashboard interactions (inline-edit max_bid, two-click remove, attention flags, 30s refresh).
- `GET /api/comics/snipes` and `GET /api/comics/history` — server-side join of gixen-cli's `bids` with the plugin's `comics` / `fmv` / `bid_fmvs` tables. Feeds the dashboard. Calls gixen-cli's `_ensure_fresh_sync` so live data stays in sync with `/`.
- `GET /api/comics` (list), `POST /api/comics` (upsert), `POST /api/extract-comics`, `POST /api/bids/{item_id}/comics/locg` — CRUD + linking helpers used by the `/comic:*` Claude Code skills.

### Coupling to gixen-cli

The plugin imports private helpers (`_ensure_fresh_sync`, `_spawn_fallback_task`, `_iso_to_relative`) from gixen-cli's `server.main`. Now that both live in this monorepo, the overlay declares `gixen-cli` as a workspace dependency and a rename of one of these + its caller land in the same commit and CI run — the canary `plugins/gixen-overlay/tests/test_workspace_imports.py` fails loudly if the surface drifts. See `plugins/gixen-overlay/src/gixen_overlay/routes.py`.

## Status

Bootstrapped in PER-29. Plugin stub in PER-30. FMV schema split in PER-37. Comics dashboard rewrite in PER-40.
