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


@pytest.fixture(autouse=True)
def _no_printing_guard_credentials(monkeypatch, tmp_path):
    """BUI-929: pin the printing guard's Browse API credentials to 'absent'
    for every test, same reasoning as `_no_sold_comps_secondary` above — a
    dev machine's real environment/.env can hold real EBAY_CLIENT_ID/
    EBAY_CLIENT_SECRET values (ebay-fetch's own config needs them). Without
    this, any test whose slab_comps pool happens to contain a price outlier
    would have `_printing_guard` silently make a REAL OAuth token request
    and Browse API call instead of exercising the guard's own logic —
    exactly the local-vs-CI divergence class `_no_sold_comps_secondary`
    exists to prevent, now for a different credential pair. Tests that
    exercise the guard's real-credential path set these explicitly via
    monkeypatch.setenv() after this fixture runs.
    """
    monkeypatch.delenv("EBAY_CLIENT_ID", raising=False)
    monkeypatch.delenv("EBAY_CLIENT_SECRET", raising=False)
    # The guard also falls back to ebay-fetch's config file (production has
    # no env credentials); point it at a path that does not exist.
    import ebay_fetch
    monkeypatch.setattr(ebay_fetch, "CONFIG_FILE", tmp_path / "no-ebay-config.json")


@pytest.fixture(autouse=True)
def _no_condition_defect_network_calls(monkeypatch):
    """BUI-968: pin apply_condition_defect_gate()'s per-candidate getItem
    fetch to a no-op ("no seller note") for every test, same reasoning as
    `_no_printing_guard_credentials` above (BUI-929) — without this, ANY
    seller_scan/wishlist_sellers test whose stubbed verify_with_claude
    returns a non-empty matches/survivors list would have
    apply_condition_defect_gate() make a REAL HTTP call to eBay's Browse API
    get_item_by_legacy_id endpoint (fail-open on the resulting 401/timeout,
    so it wouldn't fail the test outright, but it's a real external network
    dependency and a real API call neither test intends to make). Tests that
    exercise the gate itself re-patch seller_scan.get_condition_description
    after this fixture runs.
    """
    monkeypatch.setattr(seller_scan, "get_condition_description", lambda *a, **k: None)


@pytest.fixture(autouse=True)
def _isolate_raw_response_capture(monkeypatch, tmp_path):
    """BUI-614: redirect the raw-response capture file to a per-test tmp path.

    Without this, any test that drives fetch()/fetch_sold_comps() to a real
    success path appends to the real
    ``~/.local/share/ebay-sold-comps-capture/raw_responses.jsonl`` — a stray
    write into the dev machine's real home directory on every test run.
    """
    capture_dir = tmp_path / "raw-capture"
    monkeypatch.setattr(sold_comps, "CAPTURE_DIR", capture_dir)
    monkeypatch.setattr(sold_comps, "CAPTURE_PATH", capture_dir / "raw_responses.jsonl")
