# locg

Command-line interface for [League of Comic Geeks](https://leagueofcomicgeeks.com). Browse releases, search series, manage your collection, pull list, wish list, and read list.

All output is JSON, making it easy to pipe into `jq` or other tools.

## Installation

```bash
pip install .
# or
pipx install .
```

For development:

```bash
pip install -e ".[test]"
```

## Authentication

Some commands (collection, pull-list, wish-list, read-list, add, remove, update) require a League of Comic Geeks account.

```bash
locg login
```

This stores your session cookie in `~/.config/locg/cookies.json`.

## Commands

### search

Search for comic series by title.

```bash
locg search "batman"
locg search "amazing spider-man" --pretty
```

### releases

View new comic releases for a given week. Defaults to the current week (Wednesday is new comic day).

```bash
locg releases
locg releases --date 2026-04-02 --pretty
```

### check

Check which lists a comic belongs to. Accepts one or more comic IDs (requires login).

```bash
locg check 9559460
locg check 9559460 8823401 --pretty
```

### lookup

Resolve LOCG comic IDs in batch from `Series:Issue[:Variant]` specs. Groups requests by series so each unique series is searched only once, then uses a title-filtered query against that series_id to pinpoint each issue (a small response per issue, no pagination dance through 140-issue pages). By default also fetches your collection once and reports `in_collection` per row; pass `--no-collection` to skip the auth + fetch.

```bash
locg lookup "Uncanny X-Men:185" "Batman:224" "Amazing Spider-Man:142" --pretty
locg lookup "Uncanny X-Men:179:Newsstand" --no-collection
locg lookup "Batman: The Long Halloween:9"   # series names with internal ":" are fine
```

Output per row:

```json
{
  "series_name": "Uncanny X-Men", "issue_number": "185", "variant": null,
  "series_id": 108806, "locg_id": 1081721, "locg_variant_id": null,
  "issue_name": "Uncanny X-Men #185", "in_collection": false
}
```

Series matching prefers the canonical run (exact name, then preferred publisher, then oldest start year, then highest issue count), so `"Batman"` resolves to the 1940 DC run instead of a recent one-shot. If a series or issue can't be resolved, that row gets an `"error"` field; the rest of the batch still completes.

### comic

Get full details for a specific comic by ID (publisher, price, creators, description, etc.).

```bash
locg comic 9559460
locg comic 9559460 --pretty
```

### series

Get a series overview and its issue list.

```bash
locg series 149498
locg series 149498 --pretty
```

### collection

View your collected comics (requires login). Filter by title with `--title`. Check if a specific title is in your collection with `has`.

```bash
locg collection --pretty
locg collection --title "batman" --pretty
locg collection has "Amazing Spider-Man #300"
```

### pull-list

View your pull list (requires login).

```bash
locg pull-list --pretty
```

### wish-list

View your wish list (requires login).

```bash
locg wish-list --pretty
```

### read-list

View your read list (requires login).

```bash
locg read-list --pretty
```

### add

Add a comic to a list (requires login). Lists: `pull`, `collection`, `wish`, `read`. When adding to `collection`, optionally record grade and price in the same step.

```bash
locg add collection 9559460
locg add pull 9559460
locg add collection 9559460 --grade 8.5 --price 390
```

`--grade` accepts CGC scale values: `0`, `0.1`, `0.3`, `0.5`, `1.0` ... `9.8`, `9.9`, `10.0`. Only valid for `collection`.

### remove

Remove a comic from a list (requires login). Lists: `pull`, `collection`, `wish`, `read`.

```bash
locg remove collection 9559460
locg remove wish 9559460
```

### update

Update grade, price, or condition notes on a comic already in your collection (requires login). At least one flag is required.

```bash
locg update 9559460 --grade 9.2
locg update 9559460 --price 500 --condition "white pages"
locg update 9559460 --grade 9.8 --price 450 --condition "off-white pages"
```

### login

Log in to League of Comic Geeks. Prompts for username and password.

```bash
locg login
```

## Global Flags

| Flag | Description |
|------|-------------|
| `--pretty` | Pretty-print JSON output with indentation |
| `--fields name,id` | Limit output to specific fields (works on any command) |
| `--debug` | Print HTTP request/response debug info to stderr |
| `--version` | Show version and exit |

## Output Format

All commands output JSON. Errors are written to stderr as `{"error": "message"}`.

Example output for `locg search "batman" --pretty`:

```json
[
  {
    "id": 149498,
    "name": "Batman",
    "publisher": "DC Comics",
    "start_year": 2016,
    "end_year": 2020,
    "issue_count": 150,
    "cover_url": "https://...",
    "url": "https://leagueofcomicgeeks.com/comics/series/149498/batman"
  }
]
```

## Configuration

Config and cookies are stored in `~/.config/locg/` (respects `XDG_CONFIG_HOME`).

## License

MIT
