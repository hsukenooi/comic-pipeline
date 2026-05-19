# Gixen CLI

[![CI](https://github.com/hsukenooi/gixen-cli/actions/workflows/tests.yml/badge.svg)](https://github.com/hsukenooi/gixen-cli/actions/workflows/tests.yml)
[![PyPI](https://img.shields.io/pypi/v/gixen-cli.svg)](https://pypi.org/project/gixen-cli/)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

A command-line tool for managing your [Gixen](https://www.gixen.com) eBay
snipes.

Gixen's official API is disabled for some accounts. `gixen-cli` works by
automating the Gixen web interface directly, submitting the same forms your
browser would.

## Prerequisites

- Python 3.11 or newer
- A free [Gixen.com](https://www.gixen.com) account
- An eBay account

## Installation

Install from PyPI:

```bash
pip install gixen-cli
```

Or, recommended for CLI tools, with [pipx](https://pipx.pypa.io/) so it lives
in its own isolated environment:

```bash
pipx install gixen-cli
```

## Configuration

Create a `.env` file in the directory where you'll run `gixen` (or export the
variables):

```
GIXEN_USERNAME=your_username
GIXEN_PASSWORD=your_password
```

## Usage

```bash
# List all current snipes
gixen list

# Add a snipe
gixen add <item_id> <max_bid>
gixen add 123456789 25.50 --offset 3 --group 1

# Edit an existing snipe
gixen edit <item_id> <new_max_bid>

# Remove a snipe
gixen remove <item_id>

# Purge completed/ended snipes
gixen purge
```

### Options

- `--offset` — Seconds before auction end to place the bid (1–15, default: 6)
- `--group` — Snipe group (0=none, 1–10). Items in the same group are
  mutually exclusive: Gixen will only bid on one.

## Self-Hosted Server (Optional)

`gixen-cli` also ships with an optional FastAPI server that stores bid
history in SQLite and serves a small web dashboard. Use this if you want a
single shared backend for multiple machines or a browser-accessible view of
your snipes.

```bash
# Run in development
uvicorn server.main:app --reload
```

Once the server is running, point the CLI at it by setting
`GIXEN_SERVER_URL` in your `.env` — writes (add/edit/remove/purge) are
proxied to the server and reads pull from its database.

## Plugin Architecture

`gixen-cli` is a generic sniping tool. Domain-specific features can be added
via separate plugin packages that extend `gixen-cli` through the
`gixen.plugins` entry-point group. Plugins can register additional routes,
database tables, and dashboard tabs. See `gixen/plugins.py` for the
extension API.

## Tests

```bash
# Unit tests (no credentials needed)
pytest -m "not integration"

# Integration tests (requires GIXEN_USERNAME and GIXEN_PASSWORD)
pytest -m integration
```

## Contributing

Contributions are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md) for dev
setup, test instructions, and the branch/PR convention.

## License

[MIT](LICENSE)
