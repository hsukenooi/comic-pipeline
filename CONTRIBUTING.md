# Contributing to gixen-cli

Thanks for your interest in contributing! This is a small, solo-maintained
project, so the bar here is just enough context for an occasional contributor
to send a useful PR.

## Prerequisites

- Python 3.11 or newer
- A [Gixen](https://www.gixen.com/) account (only needed if you want to run
  the integration tests)

## Dev Setup

```bash
git clone https://github.com/hsukenooi/gixen-cli.git
cd gixen-cli
pip install -e .
```

## Running Tests

Unit tests are mocked and don't need any credentials:

```bash
pytest tests/test_gixen_client.py tests/test_server_api.py tests/test_server_db.py
```

Integration tests hit the real Gixen site and require a `.env` file at the
repo root with your credentials:

```bash
# .env
GIXEN_USERNAME=your-username
GIXEN_PASSWORD=your-password
```

Then run:

```bash
pytest -m integration
```

## Branch and PR Convention

Every change — including docs and one-line tweaks — goes through a feature
branch and a pull request. Don't commit directly to `main`.

```bash
git checkout -b your-feature-branch
# make changes, commit
git push -u origin your-feature-branch
# open a PR on GitHub
```

Keep PRs small and focused; one logical change per PR makes review easier.

## Code of Conduct

This project follows the [Contributor Covenant](CODE_OF_CONDUCT.md). By
participating, you agree to abide by its terms.
