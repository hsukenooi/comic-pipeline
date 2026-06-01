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
def _isolate_id_cache(tmp_path, monkeypatch):
    """Redirect the IDCache default path to a per-test tmp dir.

    Without this, tests that exercise `cmd_lookup` (or anything that
    instantiates :class:`locg.cache.IDCache` without an explicit path)
    would read and write the developer's real ~/.cache/locg/ids.json.
    That makes test outcomes depend on local cache state — exactly the
    kind of cross-run pollution that bit us when the cache integration
    landed.
    """
    import locg.cache as cache_mod
    monkeypatch.setattr(cache_mod, "cache_path", lambda: tmp_path / "ids.json")


@pytest.fixture(autouse=True)
def _isolate_collection_cache(tmp_path, monkeypatch):
    """Redirect the collection cache and audit log paths to a per-test tmp dir.

    Mirrors _isolate_id_cache.  Prevents tests from reading or writing the
    developer's real ~/.cache/locg/collection.json and import-history.jsonl.
    """
    import locg.collection_cache as cc_mod
    monkeypatch.setattr(cc_mod, "collection_cache_path", lambda: tmp_path / "collection.json")
    monkeypatch.setattr(cc_mod, "import_history_path", lambda: tmp_path / "import-history.jsonl")


@pytest.fixture(autouse=True)
def _isolate_wish_list_cache(tmp_path, monkeypatch):
    """Redirect wish_list_cache_path to a per-test tmp dir.

    Patches every module that imports wish_list_cache_path at module level so
    that tests never touch the real ~/.cache/locg/wish-list.json.
    """
    wish_path = tmp_path / "wish-list.json"
    import locg.collection_io as cio_mod
    import locg.commands as cmd_mod
    import locg.cli as cli_mod
    monkeypatch.setattr(cio_mod, "wish_list_cache_path", lambda: wish_path)
    monkeypatch.setattr(cmd_mod, "wish_list_cache_path", lambda: wish_path)
    monkeypatch.setattr(cli_mod, "wish_list_cache_path", lambda: wish_path)
