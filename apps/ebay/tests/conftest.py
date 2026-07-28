"""Shared pytest fixtures for apps/ebay's test suite."""

import pytest

import seller_scan
import sold_comps


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


@pytest.fixture(autouse=True)
def _no_sold_comps_secondary(monkeypatch):
    """BUI-545: pin the secondary-provider config to 'absent' for every test.

    run_batch() resolves SOLD_COMPS_KEY from the real environment/.env — on a
    dev machine that file holds a real key, which would silently activate the
    failover tier inside legacy SerpApi-failure tests (changing their
    expected attempt counts and issuing mock calls the tests never routed)
    and make local runs diverge from CI. Tests that exercise the failover
    pass sold_comps_key explicitly to fetch_book_comps(), or re-patch
    load_sold_comps_key after this fixture.
    """
    monkeypatch.setattr(sold_comps, "load_sold_comps_key", lambda: None)
    monkeypatch.delenv(sold_comps.PROVIDERS_ENV_VAR, raising=False)
    monkeypatch.delenv("SOLD_COMPS_KEY", raising=False)
