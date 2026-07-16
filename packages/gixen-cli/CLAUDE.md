# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

A Python CLI for managing eBay snipes on Gixen.com. Since Gixen's official API is disabled for some accounts, this client works by web-scraping the Gixen web UI (submitting the same HTML forms a browser would).

## Commands

```bash
# Run unit tests (mocked, no credentials needed)
pytest tests/test_gixen_client.py
pytest tests/test_server_api.py
pytest tests/test_server_db.py
pytest tests/test_add_batch.py
pytest tests/test_cli_add_batch.py
pytest tests/test_cli_record_win_prep.py
pytest tests/test_ebay_fallback.py
pytest tests/test_log_config.py
pytest tests/test_record_win_prep.py
pytest tests/test_skill_migration.py
pytest tests/test_standalone_server.py

# Run integration tests (requires GIXEN_USERNAME and GIXEN_PASSWORD in .env)
pytest -m integration

# Run the CLI (direct mode)
python cli.py list
python cli.py add <item_id> <max_bid> [--offset 6] [--group 0]
python cli.py edit <item_id> <max_bid>
python cli.py remove <item_id>
python cli.py purge

# Run the CLI (thin-client mode — set COMICS_SERVER_URL in .env)
python cli.py add <item_id> <max_bid> [--offset 6] [--group 0]
python cli.py sync                  # pull latest Gixen state into server DB

# Run the server (development)
uvicorn server.main:app --reload

# Deploy the server on Mac Mini
bash server/install.sh
```

## Architecture

Four components:

- **`gixen_client.py`** — `GixenClient` class that manages a `requests.Session`, handles login via HTML form POST, extracts session IDs from meta-refresh redirects, and parses the snipe table from raw HTML using regex. All Gixen operations (add/modify/remove/purge) work by POSTing form data to `home_2.php` with the session ID as a query param. Auto-re-logins on session expiration.
- **`cli.py`** — Click CLI. When `COMICS_SERVER_URL` is set in `.env` (the deprecated alias `GIXEN_SERVER_URL` is still honored), routes writes (add/edit/remove/purge) to the FastAPI server and reads (`list`) from `GET /api/snipes`. When not set, talks directly to Gixen (existing behavior).
- **`server/`** — FastAPI app (`main.py`) with SQLite storage (`db.py`) and LaunchAgent installer (`install.sh`). Proxies Gixen operations, stores bid history, and serves the web dashboard. `/api/snipes` pulls live state from Gixen synchronously on each visit (deduped within `_SYNC_TTL=5s` across concurrent calls) and reads cached rows from SQLite — no background sync loop. eBay's Browse API is invoked only as a fire-and-forget fallback when an auction has ended without a captured `winning_bid`. Server credentials and DB path are configured via `~/.gixen-server/.env`. Plugins register additional routes, DB tables, and dashboard tabs via the `gixen.plugins` entry-point group (see `gixen/plugins.py`).
- **`record_win_prep.py`** (BUI-352/353/354) — backs the `record-win-prep` CLI command. One call does gixen-list → filter to ENDED+WON+dedup → subtract the BUI-121 seen-set (fetched from the comics server) → shell out to `comic-identify --batch` → positionally map identities back onto wins, building the `/comic:collection-add` record-win payload (`{"wins": [...], "needs_review": [...]}`). Raises `RecordWinPrepError` (hard stop) instead of degrading silently on a seen-set connectivity failure or a comic-identify launch/timeout/line-count mismatch — the positional join is exactly the silent-misattribution bug BUI-353 exists to prevent.

**gixen-cli → comic-identify: a workspace package shelling out to a non-workspace console script.** `record_win_prep.py`'s `identify_titles()` runs `subprocess.run(["comic-identify", "--batch"], ...)` — the same pattern as the root `CLAUDE.md`'s `comic-fmv` → `ebay-sold-comps` precedent, except here the caller (gixen-cli) is itself a **uv workspace member**, while `comic-identify` (`apps/ebay`, entry point `comic_identify:main`) is not — it's reachable only via `uv tool install` on PATH (`scripts/install.sh`). Same operational implication as the FMV case: if apps/ebay isn't installed, `subprocess.run` raises `OSError` (missing binary), which `identify_titles` catches and re-raises as `RecordWinPrepError` pointing at `./scripts/install.sh`. There's no import here, so the failure mode is "command not found," not `ModuleNotFoundError` — but the root cause (the console script missing from PATH) is the same class of problem as the FMV precedent.

**`add-batch` is server-mode-only — unlike `add`/`edit`/`remove`, it has no direct-Gixen fallback.** `add`, `edit`, and `remove` each check `_server_url()` and, when `COMICS_SERVER_URL` is unset, fall back to talking directly to Gixen via `_make_client()`/`GixenClient` (`cli.py`'s `add`/`edit`/`remove` commands). `add_batch_cmd` doesn't: it resolves `_server_url()` once and, if unset, prints an error and `sys.exit(1)` immediately — there is no equivalent direct-Gixen branch to fall back to. This is intentional (BUI-360), not a gap to fill in later: `add-batch` exists specifically to encode BUI-168's mid-batch failure semantics as deterministic code instead of an LLM-followed skill loop — marking a failed row FAILED with its error, re-checking server health before the next row, halting and marking every remaining row NOT_ATTEMPTED if the server is down, and never emitting an all-success summary after a failure (see `add_batch.py`'s module docstring and `run_batch()`). Every one of those mechanics is built from comics-server-only primitives — `GET /health` as the inter-row health check and `POST /api/bids` per row, plus `POST /api/comics/verify` for the optional `--verify` flag — and `GixenClient`'s direct-Gixen path has no equivalent for any of them (no health endpoint, no verify endpoint; a direct-mode failure is just a raised `GixenError` with no separate "is the service still up" signal to gate a halt decision on). Without the comics server there's nothing left to build BUI-168's semantics out of, so a "direct-Gixen add-batch" isn't a smaller version of the feature — it's not the feature at all.

## Key Details

- Credentials come from environment variables or `.env` file: `GIXEN_USERNAME`, `GIXEN_PASSWORD`
- The HTML parsing in `_parse_snipe_table` is fragile by nature — it relies on specific HTML patterns from Gixen's desktop table (hidden inputs named `edititemid_<ID>`, `editmaxbid_<ID>`, etc.). Changes to Gixen's HTML will break parsing.
- Modify and remove operations require a `dbidid` (Gixen's internal row ID), which is obtained by first listing all snipes and finding the matching item.
- Exception hierarchy: `GixenError` is the base; `GixenLoginError`, `GixenSessionExpiredError`, `GixenItemError` (has `.code` and `.message`), `GixenSnipeNotFoundError`, `GixenParseError`, `GixenAddNotConfirmedError` (POST returned no error but the snipe never appeared in the list) all inherit from it.
