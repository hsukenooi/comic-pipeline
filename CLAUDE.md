# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

A Python CLI for managing eBay snipes on Gixen.com. Since Gixen's official API is disabled for some accounts, this client works by web-scraping the Gixen web UI (submitting the same HTML forms a browser would).

## Commands

```bash
# Run unit tests (mocked, no credentials needed)
pytest tests/test_gixen_client.py

# Run integration tests (requires GIXEN_USERNAME and GIXEN_PASSWORD in .env)
pytest -m integration

# Run the CLI
python cli.py list
python cli.py add <item_id> <max_bid> [--offset 6] [--group 0]
python cli.py edit <item_id> <max_bid>
python cli.py remove <item_id>
python cli.py purge
```

## Architecture

Two files, no package structure:

- **`gixen_client.py`** — `GixenClient` class that manages a `requests.Session`, handles login via HTML form POST, extracts session IDs from meta-refresh redirects, and parses the snipe table from raw HTML using regex. All Gixen operations (add/modify/remove/purge) work by POSTing form data to `home_2.php` with the session ID as a query param. Auto-re-logins on session expiration.
- **`cli.py`** — Thin Click CLI that reads `GIXEN_USERNAME`/`GIXEN_PASSWORD` from env (via dotenv), instantiates `GixenClient`, and maps subcommands to client methods.

## Key Details

- Credentials come from environment variables or `.env` file: `GIXEN_USERNAME`, `GIXEN_PASSWORD`
- The HTML parsing in `_parse_snipe_table` is fragile by nature — it relies on specific HTML patterns from Gixen's desktop table (hidden inputs named `edititemid_<ID>`, `editmaxbid_<ID>`, etc.). Changes to Gixen's HTML will break parsing.
- Modify and remove operations require a `dbidid` (Gixen's internal row ID), which is obtained by first listing all snipes and finding the matching item.
- Exception hierarchy: `GixenError` is the base; `GixenLoginError`, `GixenSessionExpiredError`, `GixenItemError` (has `.code` and `.message`), `GixenSnipeNotFoundError`, `GixenParseError` all inherit from it.
