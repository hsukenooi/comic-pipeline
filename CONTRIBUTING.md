# Contributing to locg-cli

## Development Setup

Requires Python 3.9+ and a system Chrome installation (used by the Playwright HTTP client).

```bash
git clone https://github.com/hsukenooi/locg-cli.git
cd locg-cli
pip install -e ".[test]"
```

## Running Tests

```bash
PYTHONPATH=src python3 -m pytest tests/ -v
```

## Submitting Changes

1. Fork the repo and create a branch from `main`.
2. Make your changes with tests where applicable.
3. Ensure all tests pass.
4. Open a pull request with a clear description of what changed and why.

## Reporting Bugs

Open a GitHub issue with steps to reproduce, the command you ran, and the full output (including any error messages).
