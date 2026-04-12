# Gixen CLI

A command-line tool for managing your [Gixen](https://www.gixen.com) eBay snipes.

Gixen's official API is disabled for some accounts. This CLI works by automating the Gixen web interface directly, submitting the same forms your browser would.

## Setup

```bash
pip install -r requirements.txt
```

Create a `.env` file (or export the variables):

```
GIXEN_USERNAME=your_username
GIXEN_PASSWORD=your_password
```

## Usage

```bash
# List all current snipes
python cli.py list

# Add a snipe
python cli.py add <item_id> <max_bid>
python cli.py add 123456789 25.50 --offset 3 --group 1

# Edit an existing snipe
python cli.py edit <item_id> <new_max_bid>

# Remove a snipe
python cli.py remove <item_id>

# Purge completed/ended snipes
python cli.py purge
```

### Options

- `--offset` — Seconds before auction end to place the bid (1-15, default: 6)
- `--group` — Snipe group (0=none, 1-10). Items in the same group are mutually exclusive: Gixen will only bid on one.

## Tests

```bash
# Unit tests (no credentials needed)
pytest tests/test_gixen_client.py

# Integration tests (requires credentials)
pytest -m integration
```
