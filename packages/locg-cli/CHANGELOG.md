# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - 2026-05-19

### Added

- `search` — search for comic series by title
- `series` — list all issues in a series
- `find` — find a specific issue within a series by number and optional variant
- `comic` — get full details for a specific comic (creators, price, description)
- `releases` — view new releases for the current or a specific week
- `lookup` — batch-resolve series:issue specs to LOCG IDs with on-disk caching
- `cache` — inspect or clear the lookup ID cache
- `collection`, `pull-list`, `wish-list`, `read-list` — view your lists (requires login)
- `add`, `remove` — add or remove comics from any list
- `update` — update grade, price, or condition on a collection item
- `check` — check which lists a comic belongs to
- `login` — interactive and non-interactive login; auto-login from `~/.config/locg/.env`
- Playwright-based HTTP client using real Chrome to bypass Cloudflare TLS fingerprinting
- JSON output on stdout; errors as JSON on stderr
- `--pretty`, `--fields`, `--debug`, `--version` global flags
