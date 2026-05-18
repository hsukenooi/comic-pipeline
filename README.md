# comic-pipeline

Comic overlay plugin for [gixen-cli](https://github.com/hsukenooi/gixen-cli), plus future homes for `ebay-cli` and `ezship-cli` migrations.

## Structure

- `plugins/gixen-overlay/` — FastAPI routes, SQLite tables, and dashboard tab for comic sniping workflow
- `apps/` — standalone apps (PER-31: ebay, PER-32: ezship)
- `skills/` — Claude Code skills for the comic workflow (PER-33)

## Status

Bootstrapped in PER-29. Plugin stub created; full implementation in PER-30.
