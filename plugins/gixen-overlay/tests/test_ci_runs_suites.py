"""Meta-guard for BUI-140: CI must actually run the package test suites.

CI previously only smoke-imported symbols and AST-parsed plugin.py, so any
regression to a guarded invariant merged green (see BUI-140 in the seam audit).
This test reads .github/workflows/ci.yml and asserts each package's suite is
invoked, so the gate cannot be silently dropped again without failing CI itself.
"""
from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
CI_YML = REPO_ROOT / ".github" / "workflows" / "ci.yml"


def _ci_text() -> str:
    assert CI_YML.is_file(), f"CI workflow missing at {CI_YML}"
    return CI_YML.read_text()


@pytest.mark.parametrize(
    "working_dir",
    [
        "packages/gixen-cli",
        "packages/locg-cli",
        "plugins/gixen-overlay",
        "apps/ebay",
        "apps/fmv",
    ],
)
def test_ci_runs_pytest_for_each_python_package(working_dir: str) -> None:
    """Every Python package dir must appear as a pytest step's working-directory."""
    text = _ci_text()
    assert f"working-directory: {working_dir}" in text, (
        f"{working_dir} has no CI step — its test suite would not gate merges"
    )


def test_ci_invokes_pytest_and_ezship_vitest() -> None:
    """The suites must be invoked, not merely declared as working dirs."""
    text = _ci_text()
    assert "pytest" in text, "CI never invokes pytest — Python suites do not run"
    assert "npm test" in text, "CI never runs the ezship (vitest) suite"
    assert "working-directory: apps/ezship" in text
