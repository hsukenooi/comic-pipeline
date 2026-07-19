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
pytest tests/test_cli_build_batch.py
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
python cli.py add-batch <rows.json> [--verify] [--json-out results.json]  # BUI-360: batch add, server-mode only (no direct-Gixen fallback)
python cli.py build-batch <brief.json> <working_list.json> [--overrides overrides.json] [--out rows.json]  # BUI-435: build add-batch's rows.json deterministically
python cli.py sync                  # pull latest Gixen state into server DB

# Run the server (development)
uvicorn server.main:app --reload

# Deploy the server on Mac Mini
bash server/install.sh
```

## Architecture

Five components:

- **`gixen_client.py`** — `GixenClient` class that manages a `requests.Session`, handles login via HTML form POST, extracts session IDs from meta-refresh redirects, and parses the snipe table from raw HTML using regex. All Gixen operations (add/modify/remove/purge) work by POSTing form data to `home_2.php` with the session ID as a query param. Auto-re-logins on session expiration.
- **`cli.py`** — Click CLI. When `COMICS_SERVER_URL` is set in `.env` (the deprecated alias `GIXEN_SERVER_URL` is still honored), routes writes (add/edit/remove/purge) to the FastAPI server and reads (`list`) from `GET /api/snipes`. When not set, talks directly to Gixen (existing behavior).
- **`add_batch.py`** (BUI-360) — pure logic (no `click`, no `sys.exit`) backing the `gixen add-batch` command: encodes the BUI-168 mid-batch failure semantics as deterministic code — a failed row is marked `FAILED` with its error, server health is re-checked before the next row, and the batch halts (remaining rows `NOT_ATTEMPTED`) if the server goes down mid-run, never an all-success summary after a partial failure. `cli.py`'s `add-batch` command wires it to the real server request call and prints the human table + JSON summary; it's server-mode-only, with no direct-Gixen fallback (see the note below).
- **`server/`** — FastAPI app (`main.py`) with SQLite storage (`db.py`) and LaunchAgent installer (`install.sh`). Proxies Gixen operations, stores bid history, and serves the web dashboard. `/api/snipes` pulls live state from Gixen synchronously on each visit (deduped within `_SYNC_TTL=5s` across concurrent calls) and reads cached rows from SQLite. A background `_sync_loop` (BUI-263) also runs independently of those on-visit pulls — primarily to keep `auction_end_at` fresh for the local sniper — and it's what drives the BUI-371 vanish-time stamping (`_record_vanish_observations`) that the cancelled-before-end classification below depends on. eBay's Browse API is invoked as a fire-and-forget fallback when an auction has ended without a captured `winning_bid`; before applying its WON/LOST price inference, the fallback re-checks the same BUI-371 cancelled-before-end evidence the sync already applies, so a group-cancelled sibling that reached the fallback ahead of a sync still tombstones `REMOVED` instead of getting priced (the WON/LOST inference itself stays ungated per BUI-146). That evidence survives a purge: the BUI-381 `group_wins` append-only ledger records a bid-group win at the moment `update_bid_status` classifies it WON, and outlives `mark_bids_purged` sweeping the winner's own row to `REMOVED`. Server credentials and DB path are configured via `~/.gixen-server/.env`. Plugins register additional routes, DB tables, and dashboard tabs via the `gixen.plugins` entry-point group (see `gixen/plugins.py`).
- **`record_win_prep.py`** (BUI-352/353/354) — backs the `record-win-prep` CLI command. One call does gixen-list → filter to ENDED+WON+dedup → subtract the BUI-121 seen-set (fetched from the comics server) → shell out to `comic-identify --batch` → positionally map identities back onto wins, building the `/comic:collection-add` record-win payload (`{"wins": [...], "needs_review": [...]}`). Raises `RecordWinPrepError` (hard stop) instead of degrading silently on a seen-set connectivity failure or a comic-identify launch/timeout/line-count mismatch — the positional join is exactly the silent-misattribution bug BUI-353 exists to prevent.

**gixen-cli → comic-identify: a workspace package shelling out to a non-workspace console script.** `record_win_prep.py`'s `identify_titles()` runs `subprocess.run(["comic-identify", "--batch"], ...)` — the same pattern as the root `CLAUDE.md`'s `comic-fmv` → `ebay-sold-comps` precedent, except here the caller (gixen-cli) is itself a **uv workspace member**, while `comic-identify` (`apps/ebay`, entry point `comic_identify:main`) is not — it's reachable only via `uv tool install` on PATH (`scripts/install.sh`). Same operational implication as the FMV case: if apps/ebay isn't installed, `subprocess.run` raises `OSError` (missing binary), which `identify_titles` catches and re-raises as `RecordWinPrepError` pointing at `./scripts/install.sh`. There's no import here, so the failure mode is "command not found," not `ModuleNotFoundError` — but the root cause (the console script missing from PATH) is the same class of problem as the FMV precedent.

**`add-batch` is server-mode-only — unlike `add`/`edit`/`remove`, it has no direct-Gixen fallback.** `add`, `edit`, and `remove` each check `_server_url()` and, when `COMICS_SERVER_URL` is unset, fall back to talking directly to Gixen via `_make_client()`/`GixenClient` (`cli.py`'s `add`/`edit`/`remove` commands). `add_batch_cmd` doesn't: it resolves `_server_url()` once and, if unset, prints an error and `sys.exit(1)` immediately — there is no equivalent direct-Gixen branch to fall back to. This is intentional (BUI-360), not a gap to fill in later: `add-batch` exists specifically to encode BUI-168's mid-batch failure semantics as deterministic code instead of an LLM-followed skill loop — marking a failed row FAILED with its error, re-checking server health before the next row, halting and marking every remaining row NOT_ATTEMPTED if the server is down, and never emitting an all-success summary after a failure (see `add_batch.py`'s module docstring and `run_batch()`). Every one of those mechanics is built from comics-server-only primitives — `GET /health` as the inter-row health check and `POST /api/bids` per row, plus `POST /api/comics/verify` for the optional `--verify` flag — and `GixenClient`'s direct-Gixen path has no equivalent for any of them (no health endpoint, no verify endpoint; a direct-mode failure is just a raised `GixenError` with no separate "is the service still up" signal to gate a halt decision on). Without the comics server there's nothing left to build BUI-168's semantics out of, so a "direct-Gixen add-batch" isn't a smaller version of the feature — it's not the feature at all.

## Key Details

- Credentials come from environment variables or `.env` file: `GIXEN_USERNAME`, `GIXEN_PASSWORD`
- The HTML parsing in `_parse_snipe_table` is fragile by nature — it relies on specific HTML patterns from Gixen's desktop table (hidden inputs named `edititemid_<ID>`, `editmaxbid_<ID>`, etc.). Changes to Gixen's HTML will break parsing.
- Modify and remove operations require a `dbidid` (Gixen's internal row ID), which is obtained by first listing all snipes and finding the matching item.
- Exception hierarchy: `GixenError` is the base; `GixenLoginError`, `GixenSessionExpiredError`, `GixenItemError` (has `.code` and `.message`), `GixenSnipeNotFoundError`, `GixenParseError`, `GixenAddNotConfirmedError` (POST returned no error but the snipe never appeared in the list) all inherit from it.
- **Direct-mode CLI writes are not safe to run concurrently against the same `item_id` (BUI-414).** Server-mode edits serialize under `_api_lock` (BUI-402) because the FastAPI server is one long-lived process — one `asyncio.Lock` instance can gate every request. Direct mode has no equivalent: `python cli.py edit` (run with `COMICS_SERVER_URL` unset) is a short-lived, stateless process that does its own `list_snipes` resolve + `modify_snipe` with no lock manager, so two concurrent direct-mode invocations on the same `item_id` can interleave and race. An in-process lock can't fix this — there's no shared process for it to live in; a real fix would need an OS-level file lock, with the stale-lock/cleanup complexity that implies. This is deliberately not built: direct mode is a single-human-terminal fallback path (used when the comics server is unreachable), not a concurrent service, and triggering the race requires deliberately running two overlapping edits on the same item by hand. Don't run direct-mode `edit`/`add`/`remove` concurrently against the same `item_id` from multiple terminals or scripts.
