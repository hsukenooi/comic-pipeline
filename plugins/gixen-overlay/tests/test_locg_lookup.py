"""Unit tests for the LOCG year-fallback resolver."""
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


def test_resolver_returns_year_and_locg_id_on_clean_match(monkeypatch):
    fake = _mock_run([
        [{"locg_id": 1081721, "locg_variant_id": None}],
        {"store_date": "October 1, 1986", "cover_date": "November 1986"},
    ])
    monkeypatch.setattr(subprocess, "run", fake)

    result = resolve_year_and_locg("Uncanny X-Men", "211")
    assert result == LocgResolution(year=1986, locg_id=1081721, locg_variant_id=None)
    # Two CLI calls: lookup then comic
    assert len(fake.calls) == 2
    assert fake.calls[0][1] == "lookup"
    assert fake.calls[1][1] == "comic"


def test_resolver_propagates_variant_id(monkeypatch):
    fake = _mock_run([
        [{"locg_id": 100, "locg_variant_id": 200}],
        {"store_date": "1988"},
    ])
    monkeypatch.setattr(subprocess, "run", fake)
    result = resolve_year_and_locg("Amazing Spider-Man", "300")
    assert result is not None
    assert result.locg_variant_id == 200


def test_resolver_returns_none_when_lookup_returns_error(monkeypatch):
    fake = _mock_run([[{"error": "Series not found"}]])
    monkeypatch.setattr(subprocess, "run", fake)
    assert resolve_year_and_locg("Nonexistent Series", "1") is None


def test_resolver_returns_none_when_detail_has_no_year(monkeypatch):
    fake = _mock_run([
        [{"locg_id": 999, "locg_variant_id": None}],
        {"name": "Detail with no date"},
    ])
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


def test_resolver_falls_back_to_cover_date_when_store_date_missing(monkeypatch):
    fake = _mock_run([
        [{"locg_id": 1, "locg_variant_id": None}],
        {"cover_date": "March 1963"},
    ])
    monkeypatch.setattr(subprocess, "run", fake)
    result = resolve_year_and_locg("X-Men", "1")
    assert result is not None and result.year == 1963


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
