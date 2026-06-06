"""Tests for the subprocess-based eBay fallback fetch (BUI-66).

The server no longer module-imports ebay_fetch; _fetch_ebay_item_sync shells
out to the `ebay-fetch` console script with --json. These tests mock the
subprocess so they don't hit eBay or require the binary to be installed.
"""

import json
import subprocess
from unittest.mock import MagicMock, patch

import pytest

import server.main as m


def _completed(stdout="", returncode=0, stderr=""):
    return subprocess.CompletedProcess(
        args=["ebay-fetch", "x", "--json"],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


@pytest.fixture
def ebay_ready(monkeypatch):
    """Pretend the binary is on PATH and credentials are present."""
    monkeypatch.setattr(m, "_EBAY_AVAILABLE", True)
    monkeypatch.setattr(m, "_ebay_creds_available", lambda: True)


def test_fetch_returns_first_item(ebay_ready):
    item = {"item_id": "123", "title": "Amazing Spider-Man #300",
            "current_price": "$250.00", "end_date_iso": "2026-06-01T12:00:00.000Z"}
    with patch.object(m.subprocess, "run", return_value=_completed(json.dumps([item]))) as run:
        result = m._fetch_ebay_item_sync("123")
    assert result == item
    # invoked the console script with --json for that item id
    args = run.call_args[0][0]
    assert args[0] == m._EBAY_FETCH_BIN and "123" in args and "--json" in args


def test_fetch_unavailable_returns_none(monkeypatch):
    monkeypatch.setattr(m, "_EBAY_AVAILABLE", False)
    with patch.object(m.subprocess, "run") as run:
        assert m._fetch_ebay_item_sync("123") is None
    run.assert_not_called()  # no subprocess spawned when binary missing


def test_fetch_no_creds_returns_none(monkeypatch):
    monkeypatch.setattr(m, "_EBAY_AVAILABLE", True)
    monkeypatch.setattr(m, "_ebay_creds_available", lambda: False)
    with patch.object(m.subprocess, "run") as run:
        assert m._fetch_ebay_item_sync("123") is None
    run.assert_not_called()


def test_fetch_nonzero_exit_returns_none(ebay_ready):
    with patch.object(m.subprocess, "run",
                      return_value=_completed("", returncode=1, stderr="boom")):
        assert m._fetch_ebay_item_sync("123") is None


def test_fetch_empty_array_returns_none(ebay_ready):
    with patch.object(m.subprocess, "run", return_value=_completed("[]")):
        assert m._fetch_ebay_item_sync("123") is None


def test_fetch_bad_json_returns_none(ebay_ready):
    with patch.object(m.subprocess, "run", return_value=_completed("not json")):
        assert m._fetch_ebay_item_sync("123") is None


def test_fetch_timeout_returns_none(ebay_ready):
    with patch.object(m.subprocess, "run",
                      side_effect=subprocess.TimeoutExpired(cmd="ebay-fetch", timeout=30)):
        assert m._fetch_ebay_item_sync("123") is None
