"""Suite-wide isolation for the BUI-1282 PriceCharting append.

Every graded test runs through `_compute_graded_one`, which would otherwise
call `firecrawl` (a paid network scrape) and read the real page cache. The
append is OFF and the cache points at a per-test temp dir unless a test turns
it back on (see test_pricecharting.py, which mocks the scrape).
"""

import pytest

import pricecharting


@pytest.fixture(autouse=True)
def _pricecharting_off(monkeypatch, tmp_path):
    monkeypatch.setenv("FMV_PRICECHARTING", "0")
    monkeypatch.setenv("FMV_PRICECHARTING_CACHE_DIR", str(tmp_path / "pc-cache"))
    monkeypatch.setattr(pricecharting, "_FAILED_THIS_PROCESS", {})

    def _no_network(url):
        raise AssertionError(f"a test reached the real firecrawl scrape: {url}")

    monkeypatch.setattr(pricecharting, "scrape", _no_network)
