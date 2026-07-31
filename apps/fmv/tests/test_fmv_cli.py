"""Tests for fmv_cli.py's --version flag (BUI-305).

A stale `uv tool install` of comic-fmv (apps/fmv is uv-tool-managed, not a
workspace member synced by `uv sync`) previously had no printable signal
distinguishing it from a current install. `--version` prints the pyproject
version plus the git SHA/date the wheel was built from (see hatch_build.py).
"""

import importlib.metadata
import re

from click.testing import CliRunner

from fmv_cli import cli


def test_version_flag_prints_version_and_exits_zero():
    result = CliRunner().invoke(cli, ["--version"])

    assert result.exit_code == 0
    # Version-shaped: "comic-fmv <semver> (git <sha-or-unknown>, <date-or-unknown>)".
    # Don't assert a real git SHA — running from an unbuilt source checkout
    # (as tests do) has no build stamp and correctly falls back to "unknown".
    assert re.search(r"comic-fmv \d+\.\d+\.\d+ \(git \S+, \S+\)", result.output)


def test_version_flag_short_circuits_before_batch_processing():
    """--version must not require --batch or touch fmv_runner at all."""
    result = CliRunner().invoke(cli, ["--version"])

    assert result.exit_code == 0
    assert "Usage:" not in result.output


def test_version_flag_falls_back_when_package_not_installed(monkeypatch):
    """The `comic-fmv` distribution metadata is normally present (it's how the
    CLI is installed), so the PackageNotFoundError branch never fires in the
    happy-path test above. Force it to prove the fallback actually degrades to
    "unknown" instead of raising."""
    def _raise(name):
        raise importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(importlib.metadata, "version", _raise)

    result = CliRunner().invoke(cli, ["--version"])

    assert result.exit_code == 0
    assert re.search(r"comic-fmv unknown \(git \S+, \S+\)", result.output)


def test_brief_flag_threads_through_to_runner(tmp_path, monkeypatch):
    """BUI-362: `--brief` must reach fmv_runner.run as brief=True (and default
    to False when omitted) — the flag is pure plumbing at the CLI layer."""
    import fmv_cli

    batch = tmp_path / "batch.json"
    batch.write_text("[]")

    calls = []
    monkeypatch.setattr(fmv_cli.fmv_runner, "run",
                        lambda **kwargs: calls.append(kwargs))

    result = CliRunner().invoke(
        cli, ["--batch", str(batch), "--brief", "--server-url", "http://x"])
    assert result.exit_code == 0
    assert calls[0]["brief"] is True

    result = CliRunner().invoke(
        cli, ["--batch", str(batch), "--server-url", "http://x"])
    assert result.exit_code == 0
    assert calls[1]["brief"] is False


def test_inversion_sweep_short_circuits_the_pricing_run(monkeypatch):
    """BUI-583: the sweep must reach run_inversion_sweep and NOT run(). If the
    short-circuit ever regressed, a `--inversion-sweep` call would fall through
    into a real pricing pass — spending provider requests the flag promises it
    will not, and writing rows a read-only report must never write."""
    import fmv_cli

    swept, priced = [], []
    monkeypatch.setattr(fmv_cli.fmv_runner, "run_inversion_sweep",
                        lambda **kwargs: swept.append(kwargs))
    monkeypatch.setattr(fmv_cli.fmv_runner, "run",
                        lambda **kwargs: priced.append(kwargs))

    result = CliRunner().invoke(
        cli, ["--inversion-sweep", "--server-url", "http://x"])

    assert result.exit_code == 0
    assert swept == [{"server_url": "http://x"}]
    assert priced == []


def test_inversion_sweep_needs_no_batch(monkeypatch):
    """The sweep reads existing rows, so it must not be gated on --batch the
    way a pricing run is (run() exits 2 without one)."""
    import fmv_cli

    monkeypatch.setattr(fmv_cli.fmv_runner, "run_inversion_sweep",
                        lambda **kwargs: None)

    result = CliRunner().invoke(
        cli, ["--inversion-sweep", "--server-url", "http://x"])

    assert result.exit_code == 0
    assert "--batch is required" not in result.output
