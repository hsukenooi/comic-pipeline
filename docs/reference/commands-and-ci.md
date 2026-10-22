# Commands and CI

Moved verbatim from CLAUDE.md (BUI-1301).

The repo is a **uv workspace**: `packages/*` + `plugins/*` are members, and `uv sync` from the root creates one shared `.venv` (Python pinned via `.python-version`). `apps/*` are **not** workspace members — they're installed separately via `uv tool install` (see `scripts/install.sh`). Each package still has its own `pyproject.toml`; there is no repo-wide test runner — test each package from its own directory with `uv run pytest`.

```sh
# Install the user-facing CLIs (ebay-fetch, ebay-sold-comps, seller-scan, comic-fmv, gixen, locg)
# plus the comics-api wrapper (BUI-510: a symlinked bash script, not uv tool-installed —
# see scripts/comics-api and scripts/install.sh:87-99)
./scripts/install.sh            # uv tool install for apps/ebay + apps/fmv + packages/gixen-cli + packages/locg-cli

# Re-run after merging to main and pulling on the Mac Mini (BUI-612): one command runs the
# full documented deploy ritual (uv tool install --force --no-cache for all four packages,
# uv sync --all-packages, launchctl kickstart) idempotently, then asserts every deployed
# component reports the merged HEAD git SHA — via each console script's --version and the
# comics server's /health — failing loudly instead of leaving stale code running silently.
# See scripts/deploy.sh's header for the three incidents (BUI-365, BUI-455, BUI-377) this
# replaces having to remember by hand.
./scripts/deploy.sh

# Sync the workspace env (packages/* + plugins/*) for development + tests
uv sync --all-packages

# Run the docs/solutions lint pack (BUI-605) — the learnings that compile into checks.
# --self-test proves every registered lint is still able to fail; the bare run is the
# gate itself. A PEP 723 script: uv provisions its deps, no `uv sync` needed.
./scripts/solutions-lint --self-test
./scripts/solutions-lint

# Python tests (run from the package dir)
cd packages/gixen-cli    && uv run pytest -m "not integration"
cd packages/locg-cli     && uv run pytest
cd plugins/gixen-overlay && uv run pytest

# Single test
uv run pytest tests/test_sold_comps.py::test_name -q

# ezship (TypeScript / Node, in apps/ezship)
npm test            # vitest run
npm run build       # tsc -> dist/
npm run dev         # tsx src/cli.ts
```

