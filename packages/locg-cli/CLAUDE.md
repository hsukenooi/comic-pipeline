# locg

CLI for [League of Comic Geeks](https://leagueofcomicgeeks.com). Scraping-based (no official API).

## CLI Usage

Run with `PYTHONPATH=src python3 -m locg <command>` (or `locg <command>` if installed).

All output is JSON to stdout. Use `--pretty` for human-readable output, `--debug` for HTTP details.

### Finding comics

```bash
# Search for a series by name — returns series ID, name, publisher, year
locg search "Batman"

# Get all issues in a series — returns issue IDs, names, dates
locg series <series_id>

# Find a specific issue (by number) within a series — paginates internally
locg find --series-id <series_id> --issue <N>
locg find --series-id <series_id> --issue <N> --variant newsstand
locg find --series-id <series_id> --issue <N> --exact

# Get full details for a specific comic (creators, price, description)
locg comic <comic_id>

# This week's new releases (or a specific week)
locg releases
locg releases --date 2024-01-15
```

### Workflow: adding comics by name

Comic IDs are required for add/remove/check. To find a comic ID:
1. `locg search "<series name>"` — find the series ID
2. `locg find --series-id <series_id> --issue <N>` — get the comic ID for issue N
   (or `locg series <series_id>` to browse all issues — note that `series` filters
   out annuals/giant-size; use `find` if you need those)
3. `locg add collection <comic_id>` — add by issue ID

For batches, prefer `locg lookup`:

```bash
locg lookup "Uncanny X-Men:185" "Batman:224" "Amazing Spider-Man:142"
```

It groups by series (one search per unique series), uses a title-filtered
issue query (avoiding the 140-issue series page limit), and by default
intersects the result with your collection to populate `in_collection`.

Resolved IDs are cached to `<repo>/data/locg/ids.json` and reused on
subsequent runs — repeat lookups skip the API entirely (`from_cache: true`
in the result). Pass `--no-cache` to bypass; use `locg cache stats` and
`locg cache clear` to manage. The cache dir is resolved by `config._cache_dir`
with precedence `LOCG_DATA_DIR` env → `<repo>/data/locg` → `~/.cache/locg`
fallback (BUI-84); it also holds `collection.json` and `wish-list.json`, which
are a local-only working cache, gitignored since BUI-87/93 — the comics server
on the Mac Mini is the source of truth for collection and wish-list state.

Note on the 140-issue limit: `locg series <id>` returns at most 140 issues
per call (date-desc by default). For series with more than 140 issues, the
oldest ones aren't visible. `locg lookup` works around this by querying
`?series_id=<id>&title=<exact issue name>`, which only returns matches for
the requested issue.

### Managing lists (requires login)

```bash
# View lists (collection, pull-list, wish-list, read-list)
locg collection
locg pull-list
locg wish-list
locg read-list

# Filter by title (case-insensitive substring match)
locg collection --title "batman"

# Check if a title is in your collection (fast, no full fetch)
locg collection has "Amazing Spider-Man #300"

# Limit output fields (works on any command)
locg collection --fields name,id

# Append a title to the local wish-list cache (no LOCG round-trip).
# As of BUI-208, `locg collection import` no longer touches wish-list.json —
# it is the single source of truth for wish state, so this local add (marked
# source: local) survives a subsequent import; only a server-side removal or
# a manual re-seed changes it.
locg wish-list add "Amazing Spider-Man #300"

# Add/remove a comic to/from a list (pull, collection, wish, read)
locg add collection <comic_id>
locg remove wish <comic_id>

# Add to collection with grade and price at the same time
locg add collection <comic_id> --grade 8.5 --price 390

# Update grade, price, or condition notes on a comic already in collection
locg update <comic_id> --grade 9.2 --price 500 --condition "white pages"

# Check which lists a comic belongs to (accepts multiple IDs)
locg check <comic_id> [<comic_id> ...]
```

### Authentication

```bash
# Interactive login (prompts for credentials, persists session cookie)
locg login

# Non-interactive
locg login -u <username> -p <password>
```

Session cookies are stored at `~/.config/locg/cookies.json`. Sessions can expire server-side; if you get "Session expired", run `locg login` again.
Every authenticated command verifies the session server-side once per invocation (one extra GET). Expired sessions produce `{"error": "Session expired. Run: locg login"}` on stderr and exit 1.

#### Auto-login from `.env`

If `LOCG_USERNAME` and `LOCG_PASSWORD` are set in the environment (or in `~/.config/locg/.env`, which is loaded at startup), an expired or missing session triggers an automatic login using those credentials before any error is raised. The `.env` path is fixed — it is not discovered via cwd walk — so behavior is deterministic regardless of where `locg` is invoked from.

```
# ~/.config/locg/.env
LOCG_USERNAME=your_username
LOCG_PASSWORD=your_password
```

If auto-login fails (wrong credentials, rate limit, network error) the command still exits 1 with the standard `Session expired` / `Not logged in` JSON error; the failure reason is logged at warning level (visible with `--verbose`).

## Architecture

```
src/locg/
├── __init__.py      # Package version
├── __main__.py      # `python -m locg` entry point
├── cli.py           # Argparse definitions, main() entry, JSON output
├── client.py        # HTTP client using Playwright + real Chrome (Cloudflare bypass), persistent profile
├── commands.py      # Command implementations (search, releases, comic, series, lists, add/remove, login)
├── config.py        # XDG config dir + cache dir resolver (_cache_dir, _find_repo_root)
├── models.py        # HTML → dict extraction (extract_issue, extract_series, extract_comic_detail)
├── parser.py        # Low-level HTML/JSON parsing helpers (BeautifulSoup wrappers, price/date extraction)
```

- **client.py** handles all HTTP. Uses Playwright (`channel="chrome"`, real system Chrome) to bypass Cloudflare TLS fingerprint checks. Session and `cf_clearance` cookies persist in `~/.config/locg/playwright-profile/`.
- **commands.py** orchestrates client calls and parser/model extraction. Each `cmd_*` function returns a dict or list of dicts.
- **models.py** extracts structured data from BeautifulSoup tags. `extract_issue` handles list items, `extract_series` handles search results, `extract_comic_detail` handles full comic pages.
- **parser.py** provides shared parsing utilities (JSON list response parsing, text cleaning, price/date extraction).
- **cli.py** wires argparse to commands and handles JSON serialization, error formatting, and exit codes.

`docs/solutions/` — documented solutions to past problems (bugs, best practices), organized by category with YAML frontmatter (`module`, `tags`, `problem_type`). Relevant when debugging integration or HTTP client issues.

## Conventions

- Python 3.9+, type hints throughout, `from __future__ import annotations`
- All output is JSON to stdout; errors are JSON to stderr
- Exit codes: 0 success, 1 general error / auth required, 2 no command, 4 unexpected error
- No third-party CLI framework (just argparse)

## Testing

```bash
PYTHONPATH=src python3 -m pytest tests/ -v
```

## Dependencies

- `playwright` — HTTP client via real Chrome (`channel="chrome"`); cookies persist in `~/.config/locg/playwright-profile/`. System Chrome must be installed. No `playwright install` needed.
- `beautifulsoup4` — HTML parsing
- `python-dotenv` — load `~/.config/locg/.env` at startup for auto-login
- `pytest` (test only)

**Note:** Existing `~/.config/locg/cookies.json` is no longer read. Run `locg login` once after upgrading to populate the new Playwright profile.
