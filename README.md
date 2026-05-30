# comic-pipeline

Comic overlay plugin for [gixen-cli](https://github.com/hsukenooi/gixen-cli), plus future homes for `ebay-cli` and `ezship-cli` migrations.

## Structure

- `plugins/gixen-overlay/` — FastAPI routes, SQLite tables, and dashboard tab for comic sniping workflow
- `apps/` — standalone apps (PER-31: ebay, PER-32: ezship)
- `.claude/skills/` — Claude Code skills for the comic workflow (`/comic:buy`, `/comic:grade`, etc.)

## Installing the Python CLIs

The `apps/ebay` and `apps/fmv` apps provide console scripts (`ebay-fetch`,
`ebay-sold-comps`, `seller-scan`, `comic-fmv`). Install them with [uv](https://docs.astral.sh/uv/):

```sh
./scripts/install.sh
```

This runs `uv tool install --reinstall` for both apps (into `~/.local/bin`) and
removes any stale wrappers. After it finishes, the CLIs work from any directory:

```sh
comic-fmv --help
ebay-sold-comps --help
```

`comic-fmv` shells out to `ebay-sold-comps` at runtime, so install both. No
`PYTHONPATH` workaround is needed.

## Plugin: gixen-overlay

Provides the `/comics` dashboard tab and comic-specific endpoints:

- `GET /comics` — active + ended snipes enriched with condition, FMV range, and (for active rows) a current-vs-FMV value % signal. Lots collapse to one row with aggregated FMV and a `lot of N` badge. The page reuses gixen-cli's snipes-dashboard interactions (inline-edit max_bid, two-click remove, attention flags, 30s refresh).
- `GET /api/comics/snipes` and `GET /api/comics/history` — server-side join of gixen-cli's `bids` with the plugin's `comics` / `fmv` / `bid_fmvs` tables. Feeds the dashboard. Calls gixen-cli's `_ensure_fresh_sync` so live data stays in sync with `/`.
- `GET /api/comics` (list), `POST /api/comics` (upsert), `POST /api/extract-comics`, `POST /api/bids/{item_id}/comics/locg` — CRUD + linking helpers used by the `/comic:*` Claude Code skills.

### Cross-repo coupling

The plugin imports private helpers (`_ensure_fresh_sync`, `_spawn_fallback_task`, `_iso_to_relative`) from gixen-cli's `server.main`. If those move or get renamed upstream the plugin will fail loudly at import time. See `plugins/gixen-overlay/src/gixen_overlay/routes.py`.

## Status

Bootstrapped in PER-29. Plugin stub in PER-30. FMV schema split in PER-37. Comics dashboard rewrite in PER-40.
