# Contributing to comic-pipeline

This is a small, solo-maintained monorepo. The bar is just enough context for an
occasional contributor (or future maintainer) to send a useful PR.

## Layout

- `packages/gixen-cli/` — eBay sniping CLI + FastAPI server (`gixen` console script)
- `packages/locg-cli/` — League of Comic Geeks cache (`locg` console script)
- `plugins/gixen-overlay/` — comic overlay plugin for gixen-cli
- `apps/` — standalone `ebay` + `fmv` (Python) and `ezship` (TypeScript) CLIs

`packages/*` and `plugins/*` form a uv workspace; `apps/*` are installed via
`uv tool install`. See the root `README.md` and `CLAUDE.md` for the full picture.

## Dev setup

Requires [uv](https://docs.astral.sh/uv/) and Python 3.12 (pinned in
`.python-version`). `locg-cli` also needs a system Chrome (its Playwright HTTP
client uses `channel="chrome"`).

```sh
uv sync --all-packages   # one shared env for packages/* + plugins/*
./scripts/install.sh     # install the user-facing console scripts on PATH
```

## Tests

There is no repo-wide runner — test each package from its own directory:

```sh
cd packages/gixen-cli    && uv run pytest -m "not integration"   # integration needs real Gixen creds
cd packages/locg-cli     && uv run pytest
cd plugins/gixen-overlay && uv run pytest
cd apps/ebay             && uv run pytest
cd apps/fmv              && uv run pytest
```

Run the relevant package's tests locally before committing — CI does not run the
full suites.

## Conventions

- Branch + commit per issue; don't commit feature/fix work directly to `main`.
- Linear issues use the `BUI` (Build) team; reference IDs (`BUI-50`) in commits.
- Each package keeps its own `pyproject.toml`, `README.md`, `CHANGELOG.md`,
  `CLAUDE.md`, and `docs/`.
