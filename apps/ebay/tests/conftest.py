"""Shared pytest fixtures for apps/ebay's test suite."""

import pytest

import seller_scan


@pytest.fixture(autouse=True)
def _isolate_rejected_candidate_cache(monkeypatch, tmp_path):
    """BUI-301: redirect seller_scan's rejected-candidate cache to a per-test
    tmp path for every test in this suite.

    Without this, any test that drives verify_with_claude to a genuine model
    rejection (directly or via main()'s candidate loop) writes to the real
    ``~/.cache/seller-scan/rejected.json``. A later test in the same pytest
    session reusing the same item_id (a common test fixture pattern, e.g.
    item_id "1") would then find it pre-cached as rejected and skip
    verification entirely — an order-dependent false pass/fail that has
    nothing to do with what that test is actually exercising.
    """
    monkeypatch.setattr(seller_scan, "_REJECTED_CACHE_PATH", tmp_path / "rejected.json")
