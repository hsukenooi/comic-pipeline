"""Shared fixtures for locg tests."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixtures_dir():
    return FIXTURES


@pytest.fixture
def releases_json():
    with open(FIXTURES / "releases.json") as f:
        return json.load(f)


@pytest.fixture
def search_series_json():
    with open(FIXTURES / "search_series.json") as f:
        return json.load(f)


@pytest.fixture
def series_issues_json():
    with open(FIXTURES / "series_issues.json") as f:
        return json.load(f)


@pytest.fixture
def comic_detail_html():
    with open(FIXTURES / "comic_detail.html") as f:
        return f.read()


@pytest.fixture
def comic_detail_my_details_html():
    with open(FIXTURES / "comic_detail_my_details.html") as f:
        return f.read()


@pytest.fixture
def comic_detail_my_details_not_collected_html():
    with open(FIXTURES / "comic_detail_my_details_not_collected.html") as f:
        return f.read()


@pytest.fixture
def mock_client():
    """A mock LOCGClient with get/post as MagicMocks."""
    client = MagicMock()
    client.is_authenticated = True
    client.require_auth = MagicMock()
    client.close = MagicMock()
    return client


@pytest.fixture(autouse=True)
def _isolate_cache_dir(tmp_path, monkeypatch):
    """Redirect the entire LOCG cache dir to a per-test tmp dir.

    BUI-84 moved the cache into the repo at ``data/locg/`` (tracked files), so
    an un-isolated test would now read and *write the real tracked repo files*
    — far worse than the old ~/.cache pollution. We isolate at the resolver
    (``config._cache_dir``) rather than per-public-function: setting
    ``LOCG_DATA_DIR`` makes ``cache_path()``, ``collection_cache_path()``,
    ``wish_list_cache_path()``, ``import_history_path()`` AND the directly-called
    ``ensure_cache_dir()`` / ``_cache_dir()`` all resolve under tmp, so no caller
    — patched or not — can touch the repo's tracked cache.

    Tests that exercise the resolver itself (``test_cache_paths.py``) set or
    ``delenv`` ``LOCG_DATA_DIR`` in their own bodies, which overrides this.
    """
    monkeypatch.setenv("LOCG_DATA_DIR", str(tmp_path))
