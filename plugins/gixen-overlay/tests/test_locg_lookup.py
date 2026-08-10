"""Unit tests for the Metron-backed year-fallback resolver (BUI-719)."""
from __future__ import annotations

import json
import subprocess
from unittest.mock import MagicMock

import pytest

from gixen_overlay import locg_lookup
from gixen_overlay.locg_lookup import LocgResolution, resolve_year_and_locg


def _mock_run(responses):
    """Build a subprocess.run replacement that pops a response per call.

    Each response is either a dict (json-encoded for stdout) or an exception
    instance to raise.
    """
    calls = []
    queue = list(responses)

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        item = queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        return MagicMock(returncode=0, stdout=json.dumps(item), stderr="")

    fake_run.calls = calls
    return fake_run


def test_resolver_returns_year_on_clean_match(monkeypatch):
    fake = _mock_run([
        {
            "status": "ok",
            "series": "Uncanny X-Men",
            "issue": "211",
            "year": 1986,
            "metron_id": 42,
            "series_id": 7,
            "series_name": "Uncanny X-Men",
        },
    ])
    monkeypatch.setattr(subprocess, "run", fake)

    result = resolve_year_and_locg("Uncanny X-Men", "211")
    assert result == LocgResolution(year=1986)
    assert result.locg_id is None
    assert result.locg_variant_id is None

    # A single CLI call, to the new resolve-year subcommand. `--` forces
    # argparse to treat series/issue as plain positionals regardless of a
    # leading dash (e.g. a "-1"-style one-shot issue label).
    assert len(fake.calls) == 1
    assert fake.calls[0] == [locg_lookup.LOCG_CMD, "resolve-year", "--", "Uncanny X-Men", "211"]


def test_resolver_returns_none_when_result_has_error(monkeypatch):
    fake = _mock_run([{"error": "Could not unambiguously resolve 'Nonexistent Series' #1 on Metron"}])
    monkeypatch.setattr(subprocess, "run", fake)
    assert resolve_year_and_locg("Nonexistent Series", "1") is None


def test_resolver_returns_none_when_year_missing(monkeypatch):
    fake = _mock_run([{"status": "ok", "series": "Series", "issue": "1"}])
    monkeypatch.setattr(subprocess, "run", fake)
    assert resolve_year_and_locg("Series", "1") is None


def test_resolver_returns_none_when_year_not_int(monkeypatch):
    fake = _mock_run([{"status": "ok", "year": "1986"}])
    monkeypatch.setattr(subprocess, "run", fake)
    assert resolve_year_and_locg("Series", "1") is None


def test_resolver_returns_none_when_cli_missing(monkeypatch):
    fake = _mock_run([FileNotFoundError("locg not on PATH")])
    monkeypatch.setattr(subprocess, "run", fake)
    assert resolve_year_and_locg("Series", "1") is None


def test_resolver_returns_none_on_timeout(monkeypatch):
    fake = _mock_run([subprocess.TimeoutExpired(cmd="locg", timeout=30)])
    monkeypatch.setattr(subprocess, "run", fake)
    assert resolve_year_and_locg("Series", "1") is None


def test_resolver_disabled_short_circuits(monkeypatch):
    monkeypatch.setenv("LOCG_FALLBACK_DISABLED", "1")

    def boom(*_a, **_kw):
        raise AssertionError("subprocess.run should not be invoked when disabled")

    monkeypatch.setattr(subprocess, "run", boom)
    assert resolve_year_and_locg("Series", "1") is None


@pytest.mark.parametrize("series,issue", [("", "1"), ("Series", ""), ("", "")])
def test_resolver_returns_none_for_empty_inputs(monkeypatch, series, issue):
    def boom(*_a, **_kw):
        raise AssertionError("subprocess.run should not be invoked for empty inputs")

    monkeypatch.setattr(subprocess, "run", boom)
    assert resolve_year_and_locg(series, issue) is None


def test_resolver_returns_none_on_nonzero_exit(monkeypatch):
    def fake(cmd, **kwargs):
        return MagicMock(returncode=2, stdout="", stderr="boom")

    monkeypatch.setattr(subprocess, "run", fake)
    assert resolve_year_and_locg("Series", "1") is None


def test_resolver_returns_none_on_invalid_json(monkeypatch):
    def fake(cmd, **kwargs):
        return MagicMock(returncode=0, stdout="not json", stderr="")

    monkeypatch.setattr(subprocess, "run", fake)
    assert resolve_year_and_locg("Series", "1") is None


def test_resolver_returns_none_on_non_dict_json(monkeypatch):
    """`locg resolve-year` always prints a JSON object; a stray list/scalar
    (e.g. from a mismatched LOCG_CMD pointing at some other tool) is treated
    the same as any other malformed response — fail-soft, never a guess."""
    fake = _mock_run([[{"year": 1986}]])
    monkeypatch.setattr(subprocess, "run", fake)
    assert resolve_year_and_locg("Series", "1") is None
