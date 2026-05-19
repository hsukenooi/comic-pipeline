# Changelog

## [1.0.0] — 2026-05-19

### Summary

First stable release. Separates the generic Gixen sniping tool from the comic-specific overlay that used to ship with it. The comic overlay now lives in the `comic-pipeline` plugin package.

### Added

- **Plugin architecture** (`gixen.plugins` entry-point group, pluggy-based hookspec). External packages can register routes, database tables, and dashboard tabs without modifying gixen-cli.
- **`GET /api/dashboard-tabs`** — returns plugin-contributed nav tabs as `[{"label", "path"}]`. Returns `[]` when no plugins are installed.
- **SQLite schema improvements** — `bid_comics` junction table for many-to-many bid↔comic relationships; `locg_id` / `locg_variant_id` columns for League of Comic Geeks integration; WAL mode enabled on startup.
- **Standalone verification tests** (`tests/test_standalone_server.py`) — assert that the server starts, core CRUD works, and no comic routes appear when no plugin is installed.
- **CI lint** (`.github/workflows/comic-lint.yml`) — fails the build if comic-specific terms appear in non-legacy source files, preventing future contamination.
- **Static migration validation tests** (`tests/test_skill_migration.py`) — verify that the comic-pipeline skill files are present, symlinked correctly, and free of stale paths.
- **`pyproject.toml`** — formal package manifest; `pip install -e .` now works out of the box.
- **Manual E2E checklist** (`docs/per-34-manual-test-checklist.md`) for validating the full `/comic:buy` flow against a live eBay listing.

### Changed

- Dashboard consolidation: single `index.html` replaces the earlier split v1/v2 layout; `/api/history` shows bids ended in the past 7 days.
- Session expiration detection expanded: re-login now triggers on server-invalidated session IDs in addition to cookie expiry.
- Gixen `OUTBID` and `BID UNDER ASKING PRICE` statuses mapped to terminal `LOST` state.
- `--locg-id` / `--locg-variant-id` CLI flags and `cli locg link` command for persisting resolved League of Comic Geeks IDs on bids.

### Removed

- (Pending PER-30) Inline comic routes, `server/comic_routes.py`, `server/title_parser.py`, and comic-specific columns from `server/db.py` will be removed once the comic-pipeline plugin extraction is complete.

### Migration

No breaking changes to the core Gixen sniping workflow. If you were using the comic overlay features (`--comic`, `--fmv-*`, `--locg-*` flags), install the `comic-pipeline` plugin package to restore that functionality.
