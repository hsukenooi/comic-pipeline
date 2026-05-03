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

# Run integration tests (requires GIXEN_USERNAME and GIXEN_PASSWORD in .env)
pytest -m integration

# Run the CLI (direct mode)
python cli.py list
python cli.py add <item_id> <max_bid> [--offset 6] [--group 0]
python cli.py edit <item_id> <max_bid>
python cli.py remove <item_id>
python cli.py purge

# Run the CLI (thin-client mode — set GIXEN_SERVER_URL in .env)
python cli.py add <item_id> <max_bid> [--comic "Title"] [--issue N] [--year YYYY] [--grade 9.2] [--fmv-low N] [--fmv-high N] [--fmv-comps N] [--fmv-confidence high] [--fmv-notes "notes"]
python cli.py sync                  # pull latest Gixen state into server DB
python cli.py extract-comics        # auto-link bids to comics from cached eBay titles

# Run the server (development)
uvicorn server.main:app --reload

# Deploy the server on Mac Mini
bash server/install.sh
```

## Architecture

Three components:

- **`gixen_client.py`** — `GixenClient` class that manages a `requests.Session`, handles login via HTML form POST, extracts session IDs from meta-refresh redirects, and parses the snipe table from raw HTML using regex. All Gixen operations (add/modify/remove/purge) work by POSTing form data to `home_2.php` with the session ID as a query param. Auto-re-logins on session expiration.
- **`cli.py`** — Click CLI. When `GIXEN_SERVER_URL` is set in `.env`, routes writes (add/edit/remove/purge) to the FastAPI server and reads (`list`) from `GET /api/snipes`. When not set, talks directly to Gixen (existing behavior).
- **`server/`** — FastAPI app (`main.py`) with SQLite storage (`db.py`) and LaunchAgent installer (`install.sh`). Proxies Gixen operations, stores bid history and comic FMV data, and serves the web dashboard. `/api/snipes` pulls live state from Gixen synchronously on each visit (deduped within `_SYNC_TTL=5s` across concurrent calls) and reads cached rows from SQLite — no background sync loop. eBay's Browse API is invoked only as a fire-and-forget fallback when an auction has ended without a captured `winning_bid`. Server credentials and DB path are configured via `~/.gixen-server/.env`.
- **`server/title_parser.py`** — regex-based parser that extracts `(series, issue, year, grade)` from cached eBay listing titles. Used by `POST /api/extract-comics` (and `python cli.py extract-comics`) to backfill the `comics` table for snipes added without explicit `--comic` flags.

## Key Details

- Credentials come from environment variables or `.env` file: `GIXEN_USERNAME`, `GIXEN_PASSWORD`
- The HTML parsing in `_parse_snipe_table` is fragile by nature — it relies on specific HTML patterns from Gixen's desktop table (hidden inputs named `edititemid_<ID>`, `editmaxbid_<ID>`, etc.). Changes to Gixen's HTML will break parsing.
- Modify and remove operations require a `dbidid` (Gixen's internal row ID), which is obtained by first listing all snipes and finding the matching item.
- Exception hierarchy: `GixenError` is the base; `GixenLoginError`, `GixenSessionExpiredError`, `GixenItemError` (has `.code` and `.message`), `GixenSnipeNotFoundError`, `GixenParseError` all inherit from it.
