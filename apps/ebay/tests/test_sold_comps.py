"""Tests for sold_comps.py — eBay sold-listings via SerpApi.

Pure-function tests only. Network calls are exercised through fetch() with
a mocked requests.get; no real SerpApi calls happen in CI.
"""

import json
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
import requests

import sold_comps as sc


# ─── Grade parsing ────────────────────────────────────────────────────────────

class TestParseGrade:
    @pytest.mark.parametrize("title,expected", [
        # Numeric grades — including the previously-broken 9.2/9.4/9.6/9.9
        ("ASM #270 NM 9.2 Marvel 1985", 9.2),
        ("ASM #270 NM 9.4 Marvel 1985", 9.4),
        ("Wolverine #1 9.6 condition", 9.6),
        ("Hulk #181 9.8 OW pages", 9.8),
        ("X-Men 9.9 unicorn", 9.9),
        ("ASM #142 5.5 OW pages", 5.5),
        ("VG 4.0 Bronze Age", 4.0),
        ("(8.5) cover detached", None),  # cover-detached hard-excludes downstream, but grade still parses
        # Letter grades
        ("Uncanny X-Men #185 NM-", 9.2),
        ("Uncanny X-Men #185 NM", 9.4),
        ("Batman #224 FN+", 6.5),
        ("Batman #224 FN/VF", 7.0),
        ("VF/NM bright cover", 9.0),
        ("Spider-Man VG+", 4.5),
        # Numeric beats letter when both present
        ("ASM #270 NM 9.2", 9.2),
        # No grade
        ("Just a comic title 1985", None),
    ])
    def test_parse(self, title, expected):
        if expected is None and "(8.5)" in title:
            # `(8.5)` should still parse — boundaries on `(` are word-boundary safe
            assert sc.parse_grade(title) == 8.5
        else:
            assert sc.parse_grade(title) == expected

    def test_letter_priority(self):
        # 'NM-' should win over 'NM' (longer pattern matched first by ordering)
        assert sc.parse_grade("Some NM- copy") == 9.2

    def test_combined_letters(self):
        assert sc.parse_grade("VF/NM 9.0 bright") == 9.0
        # The numeric 9.0 wins, but if it weren't there, VF/NM would still give 9.0
        assert sc.parse_grade("VF/NM bright") == 9.0

    # ── BUI-183: price/measurement context must NOT be mis-read as a grade ──

    @pytest.mark.parametrize("title", [
        # Price context ($ prefix)
        "$9.5 shipping",
        "price $9.5",
        # Measurement units following the number
        "lot 3.5 inches",
        "lot 3.5 inch",
        "ships 9.5 oz",
        "size 2.5 cm",
        "3.5 mm wide",
        "2.5 lbs weight",
        "3.5 lb box",
        "9.5 in long",
        # Dimension separator (x)
        "ruler 2.5 x 3.5",
        "2.5 x 3.5 size",
        # X.X0 price-like forms: a trailing digit means it's a number, not a
        # one-decimal grade (the restored trailing boundary, BUI-183).
        "comic 5.50 dollars",
        "lot of 9.50 value",
    ])
    def test_price_measurement_context_excluded(self, title):
        """Numbers in price or measurement context must never be parsed as a grade."""
        assert sc.parse_grade(title) is None

    @pytest.mark.parametrize("title,expected", [
        # Bare numeric grades with trailing non-unit words must still parse
        ("ASM #300 9.8", 9.8),
        ("X-Men #1 6.0 nice", 6.0),
        # Titles starting with X- must not be blocked by the dimension-x lookbehind
        ("X-Men #1 9.8", 9.8),
        ("X-Factor #6 9.4", 9.4),
    ])
    def test_bare_numeric_grades_survive(self, title, expected):
        """Bare numeric grades with non-unit trailing words must still be detected."""
        assert sc.parse_grade(title) == expected


# ─── Hard excludes ────────────────────────────────────────────────────────────

class TestHardExclude:
    @pytest.mark.parametrize("title", [
        "Coverless ASM #5 1963",
        "ASM #142 missing pages",
        "Lot of 5 Spider-Man comics",
        "ASM #1-5 lot",
        "ASM #5 facsimile edition 2024",
        "ASM #5 Marvel Tales reprint",
        "Batman #608 UK pence variant",
        "X-Men vol 2 #1",
        "ASM #5 PSA 8 graded",
        "Spider-Man action figure 1:6 scale",
        "Spawn #1 trading card Upper Deck",
        "ASM #129 signed by Stan Lee",
        "WW Live Sale ASM result",
        "X-Men #1 restored copy",
        # BUI-269: previously sold_comps-only markers, now unioned into
        # comic_identity — pin that reconciling the two lexicons didn't drop
        # coverage on the sold_comps side.
        "ASM #300 9d variant UK",
        "Fantastic Four #1 rare Brazil edition",
        "Amazing Spider-Man #1 rare Mexico edition",
        "Batman #1 Norway edition",
        "Superman #1 Australia edition",
        "ASM #1 Italian edition",
        "ASM #1 Spain edition",
        "ASM #1 Ebal edition",
        "Spawn #1 Johnny Lightning promo",
        # BUI-269 (Opus PR#119 review): multi-issue lot shapes the reconciled
        # lexicon must still exclude from the comp pool — the shared _LOT_RE
        # misses these, so is_comp_excluded's comp-only _FMV_LOT_RE covers them.
        "Amazing Spider-Man #1 #2 #3 CGC",   # space-separated hash list (3)
        "Hulk #181 #182",                     # space-separated hash pair (2)
        "ASM #64, #65",                       # 2-member comma pair (both hashed)
        "ASM #64, 65",                        # 2-member comma pair (2nd unhashed)
    ])
    def test_excludes(self, title):
        assert sc.hard_exclude(title)

    @pytest.mark.parametrize("title", [
        "ASM #142 FN+ 1975",
        "Uncanny X-Men #185 VF Marvel",
        "Batman #226 1970 Neal Adams cover",
        "Aliens vs Predator #2 Dark Horse 1990",
        # BUI-269 (Opus PR#119 review): a single issue must NOT be caught by the
        # new comp-only lot regex — it has only one # token.
        "Amazing Spider-Man #300",
        "X-Men #266 CGC 9.8",
        # comma edge: "#1, 2018" is a hash then a YEAR, not a 2-issue lot —
        # _FMV_LOT_RE bounds the comma member to 1-3 digits to avoid this.
        "Detective Comics #1, 2018",
    ])
    def test_keeps(self, title):
        assert not sc.hard_exclude(title)


# ─── Comp parsing ─────────────────────────────────────────────────────────────

class TestParseComp:
    def _make(self, **overrides):
        base = {
            "product_id": "147295505028",
            "title": "Uncanny X-Men #185 VF Marvel 1984",
            "price": {"raw": "$11.99", "extracted": 11.99},
            "sold_date": "Sold Oct 12, 2026",
            "buying_format": "auction",
            "link": "https://example.com",
        }
        base.update(overrides)
        return base

    def test_full_parse(self):
        c = sc.parse_comp(self._make())
        assert c["product_id"] == "147295505028"
        assert c["price"] == 11.99
        assert c["grade"] == 8.0
        assert c["sold_date"].startswith("Sold")

    def test_falls_back_to_extracted_price(self):
        c = sc.parse_comp(self._make(price={"extracted": 5.99}))
        assert c["price"] == 5.99

    def test_falls_back_to_raw_price(self):
        c = sc.parse_comp(self._make(price={"raw": "$8.00"}))
        assert c["price"] == 8.0

    def test_drops_no_price(self):
        assert sc.parse_comp(self._make(price={})) is None

    def test_drops_no_title(self):
        assert sc.parse_comp(self._make(title="")) is None

    def test_uses_item_id_fallback(self):
        c = sc.parse_comp({
            "title": "X",
            "item_id": "123",
            "price": {"raw": "$5"},
        })
        assert c["product_id"] == "123"

    def test_price_out_of_range_drops(self):
        assert sc.parse_comp(self._make(price={"extracted": 0.10})) is None
        assert sc.parse_comp(self._make(price={"extracted": 100000})) is None


# ─── Query construction ─────────────────────────────────────────────────────

class TestBuildQuery:
    def test_minimal(self):
        q = sc.build_query("Amazing Spider-Man", "300")
        assert '"Amazing Spider-Man 300"' in q
        assert "-cgc" in q and "-slab" in q

    def test_with_year_and_publisher(self):
        q = sc.build_query("Invincible", "1", year=2003, publisher="image comics")
        assert "2003" in q
        assert "image comics" in q

    def test_with_grade_label(self):
        q = sc.build_query("Batman", "224", year=1970, grade_label="FN")
        assert " FN " in q or q.endswith(" FN") or " FN -" in q


class TestCanonicalUrl:
    def test_excludes_api_key(self):
        url = sc.canonical_serpapi_url('"X-Men 1"', "secret-key")
        assert "secret-key" not in url
        assert "show_only=Sold" in url
        assert "engine=ebay" in url

    def test_deterministic(self):
        url1 = sc.canonical_serpapi_url('"X-Men 1"', "k")
        url2 = sc.canonical_serpapi_url('"X-Men 1"', "k")
        assert url1 == url2


# ─── Cache layer ──────────────────────────────────────────────────────────────

class TestCache:
    def test_roundtrip(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sc, "CACHE_DIR", tmp_path)
        path = sc._cache_path("https://example.com/q?foo=bar")
        sc._cache_put(path, {"hello": "world"})
        assert sc._cache_get(path, ttl_sec=60) == {"hello": "world"}

    def test_expired(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sc, "CACHE_DIR", tmp_path)
        path = sc._cache_path("k")
        sc._cache_put(path, {"x": 1})
        # Backdate mtime past TTL
        old = time.time() - 100
        import os
        os.utime(path, (old, old))
        assert sc._cache_get(path, ttl_sec=10) is None

    def test_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sc, "CACHE_DIR", tmp_path)
        assert sc._cache_get(tmp_path / "nope.json", 60) is None


# ─── Fetch with verification ──────────────────────────────────────────────────

class TestFetch:
    def _mock_response(self, organic_results=None, ebay_url=None, error=None):
        m = MagicMock()
        m.raise_for_status = MagicMock()
        body = {}
        if error:
            body["error"] = error
        else:
            body["organic_results"] = organic_results or []
            body["search_metadata"] = {"ebay_url": ebay_url or ""}
        m.json = MagicMock(return_value=body)
        return m

    def test_rejects_when_lh_sold_missing(self, tmp_path, monkeypatch):
        # If SerpApi silently dropped show_only=Sold, the returned eBay URL
        # won't contain LH_Sold=1 — we must fail loudly.
        monkeypatch.setattr(sc, "CACHE_DIR", tmp_path)
        bad_url = "https://www.ebay.com/sch/i.html?_nkw=test"  # no LH_Sold=1
        with patch("sold_comps.requests.get",
                   return_value=self._mock_response(ebay_url=bad_url)):
            with pytest.raises(sc.SerpApiError, match="LH_Sold=1"):
                sc.fetch("test", "key")

    def test_accepts_when_lh_sold_present(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sc, "CACHE_DIR", tmp_path)
        good_url = "https://www.ebay.com/sch/i.html?_nkw=test&LH_Sold=1"
        with patch("sold_comps.requests.get",
                   return_value=self._mock_response(ebay_url=good_url,
                                                    organic_results=[{"product_id": "1"}])):
            data, cache_hit = sc.fetch("test", "key")
            assert cache_hit is False
            assert data["organic_results"] == [{"product_id": "1"}]

    def test_serves_from_cache(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sc, "CACHE_DIR", tmp_path)
        good_url = "https://www.ebay.com/sch/i.html?_nkw=t&LH_Sold=1"
        # First call: real fetch
        with patch("sold_comps.requests.get",
                   return_value=self._mock_response(ebay_url=good_url)) as m:
            sc.fetch("t", "key")
            assert m.call_count == 1
            # Second call: should hit cache
            sc.fetch("t", "key")
            assert m.call_count == 1, "expected cache hit, but a second HTTP call happened"

    def test_force_bypasses_cache(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sc, "CACHE_DIR", tmp_path)
        good_url = "https://www.ebay.com/sch/i.html?_nkw=t&LH_Sold=1"
        with patch("sold_comps.requests.get",
                   return_value=self._mock_response(ebay_url=good_url)) as m:
            sc.fetch("t", "key")
            sc.fetch("t", "key", force=True)
            assert m.call_count == 2

    def test_serpapi_error_raises(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sc, "CACHE_DIR", tmp_path)
        with patch("sold_comps.requests.get",
                   return_value=self._mock_response(error="Invalid API key")):
            with pytest.raises(sc.SerpApiError, match="Invalid API key"):
                sc.fetch("t", "key")


# ─── Tiered query strategy ────────────────────────────────────────────────────

class TestTieredStrategy:
    def _wire(self, tmp_path, monkeypatch, results_per_query):
        """Make fetch() return a different result list per call. Calls past
        the end of the fixture list return an empty result (so tests don't
        need to pad for tiers that may or may not fire)."""
        monkeypatch.setattr(sc, "CACHE_DIR", tmp_path)
        calls = []

        def fake_fetch(nkw, api_key, *, force=False, ttl_sec=0):
            calls.append(nkw)
            idx = len(calls) - 1
            results = results_per_query[idx] if idx < len(results_per_query) else []
            return ({
                "organic_results": results,
                "search_metadata": {"ebay_url": "ok&LH_Sold=1"},
            }, False)

        monkeypatch.setattr(sc, "fetch", fake_fetch)
        return calls

    def _comp(self, pid, title="ASM #142 FN+ Marvel 1975", price=10.0):
        return {
            "product_id": pid,
            "title": title,
            "price": {"extracted": price},
            "sold_date": "",
            "buying_format": "auction",
        }

    def test_only_base_when_results_plentiful(self, tmp_path, monkeypatch):
        # 12 grade-tagged comps from base — no broaden, no grade-targeted
        results = [[self._comp(str(i), f"ASM #142 NM {i}.0 Marvel") for i in range(12)]]
        calls = self._wire(tmp_path, monkeypatch, results)
        out = sc.fetch_book_comps({"title": "ASM", "issue": "142", "year": 1975, "grade": 6.5},
                                  "key")
        assert len(calls) == 1
        assert len(out["comps"]) == 12

    def test_broadens_when_thin(self, tmp_path, monkeypatch):
        # 2 base, then plenty when broader
        results = [
            [self._comp(str(i)) for i in range(2)],
            [self._comp(str(100 + i)) for i in range(8)],
        ]
        calls = self._wire(tmp_path, monkeypatch, results)
        out = sc.fetch_book_comps({"title": "X", "issue": "1", "year": 1990, "grade": 9.2},
                                  "key")
        assert len(calls) == 2  # base + broader (grade-targeted may also fire)

    def test_grade_targeted_when_few_grade_tagged(self, tmp_path, monkeypatch):
        # 8 results from base but only 2 have grades parsed — should fire grade-targeted
        ungraded_titles = [self._comp(str(i), title="ASM #142 Marvel 1975") for i in range(8)]
        graded = [self._comp("g1", title="ASM #142 NM Marvel 1975")]
        results = [ungraded_titles + graded, []]
        calls = self._wire(tmp_path, monkeypatch, results)
        sc.fetch_book_comps({"title": "ASM", "issue": "142", "year": 1975, "grade": 9.2},
                            "key")
        # base + grade-targeted (results > 5 so no broaden)
        assert len(calls) == 2
        assert "NM" in calls[1]

    def test_self_exclusion(self, tmp_path, monkeypatch):
        results = [[
            self._comp("147295505028"),  # the listing being valued
            self._comp("aaa", title="ASM #142 FN Marvel"),
        ] * 3]
        self._wire(tmp_path, monkeypatch, results)
        out = sc.fetch_book_comps({
            "title": "ASM", "issue": "142", "year": 1975, "grade": 6.5,
            "item_id": "147295505028",
        }, "key")
        # Self-listing dropped; only 'aaa' kept once due to dedup
        ids = {c["product_id"] for c in out["comps"]}
        assert "147295505028" not in ids
        assert "aaa" in ids

    def test_self_exclusion_misses_when_product_id_differs(self, tmp_path, monkeypatch):
        """BUI-160: comps are keyed by SerpApi product_id, a DIFFERENT identifier
        namespace from the eBay item_id the --batch path carries. When the
        self-listing surfaces under a product_id that isn't the seeded eBay
        item_id, self-exclusion silently misses it. This locks that documented,
        best-effort contract (self-exclusion is reliable only on a product_id
        match) so a future change to the keying is caught."""
        results = [[
            # The self-listing's relist — but SerpApi gave it product_id "999",
            # not the eBay item_id we seed below.
            self._comp("999", title="ASM #142 FN Marvel"),
            self._comp("aaa", title="ASM #142 VF Marvel"),
        ] * 3]
        self._wire(tmp_path, monkeypatch, results)
        out = sc.fetch_book_comps({
            "title": "ASM", "issue": "142", "year": 1975, "grade": 6.5,
            "item_id": "147295505028",  # eBay item_id — different namespace
        }, "key")
        ids = {c["product_id"] for c in out["comps"]}
        # Not excluded: product_id 999 != the seeded eBay item_id.
        assert "999" in ids

    def test_dedup_across_tiers(self, tmp_path, monkeypatch):
        # Same comp returned in tier 1 and tier 2 → only counted once
        c1 = self._comp("dup", title="ASM #142 FN+ Marvel 1975")
        results = [[c1], [c1]]
        self._wire(tmp_path, monkeypatch, results)
        out = sc.fetch_book_comps({"title": "ASM", "issue": "142", "year": 1975, "grade": 6.5},
                                  "key")
        assert len([c for c in out["comps"] if c["product_id"] == "dup"]) == 1

    def test_echoes_req_id_when_present(self, tmp_path, monkeypatch):
        """BUI-174/187: a caller-threaded correlation id round-trips in the echoed
        input so a batch driver can map results by identity, not list position."""
        self._wire(tmp_path, monkeypatch, [[self._comp("1")]])
        out = sc.fetch_book_comps(
            {"title": "ASM", "issue": "142", "year": 1975, "grade": 6.5, "_req_id": 7},
            "key",
        )
        assert out["input"]["_req_id"] == 7

    def test_omits_req_id_when_absent(self, tmp_path, monkeypatch):
        """A standalone caller (no _req_id) gets a clean input echo, no null key."""
        self._wire(tmp_path, monkeypatch, [[self._comp("1")]])
        out = sc.fetch_book_comps(
            {"title": "ASM", "issue": "142", "year": 1975, "grade": 6.5}, "key",
        )
        assert "_req_id" not in out["input"]


# ─── End-to-end-ish: batch driver ────────────────────────────────────────────

class TestBatch:
    def test_runs_all(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sc, "CACHE_DIR", tmp_path)
        def fake_fetch(nkw, api_key, *, force=False, ttl_sec=0):
            return ({
                "organic_results": [{
                    "product_id": "1",
                    "title": "ASM #142 FN+",
                    "price": {"extracted": 12.0},
                }],
                "search_metadata": {"ebay_url": "ok&LH_Sold=1"},
            }, False)
        monkeypatch.setattr(sc, "fetch", fake_fetch)

        books = [
            {"title": "ASM", "issue": "142", "year": 1975, "grade": 6.5},
            {"title": "ASM", "issue": "151", "year": 1975, "grade": 7.0},
        ]
        results = sc.run_batch(books, "key", max_workers=2)
        assert len(results) == 2
        assert all(len(r["comps"]) == 1 for r in results)

    def test_records_errors_per_book(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sc, "CACHE_DIR", tmp_path)
        def fake_fetch(nkw, api_key, *, force=False, ttl_sec=0):
            raise RuntimeError("kaboom")
        monkeypatch.setattr(sc, "fetch_book_comps",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))

        books = [{"title": "X", "issue": "1", "year": 1990, "grade": 9.0}]
        results = sc.run_batch(books, "key")
        assert len(results) == 1
        assert "error" in results[0]


# ─── Retry / transient-error tests ───────────────────────────────────────────

class TestFetchRetry:
    """Tests for the bounded retry/backoff added to fetch() for transient errors."""

    def _mock_response(self, organic_results=None, ebay_url=None, status_code=200):
        """Build a mock requests.Response for a successful (2xx) reply."""
        m = MagicMock()
        body = {
            "organic_results": organic_results or [],
            "search_metadata": {"ebay_url": ebay_url or "https://www.ebay.com/?LH_Sold=1"},
        }
        m.json = MagicMock(return_value=body)
        if status_code == 200:
            m.raise_for_status = MagicMock()  # no-op
        else:
            http_err = requests.HTTPError(response=MagicMock(status_code=status_code))
            m.raise_for_status = MagicMock(side_effect=http_err)
        m.status_code = status_code
        return m

    def test_retry_then_succeed_timeout(self, tmp_path, monkeypatch):
        """fetch() retries on Timeout and returns data when the second call succeeds."""
        monkeypatch.setattr(sc, "CACHE_DIR", tmp_path)
        monkeypatch.setattr(sc.time, "sleep", lambda *_: None)

        good_response = self._mock_response(
            ebay_url="https://www.ebay.com/?LH_Sold=1",
            organic_results=[{"product_id": "42"}],
        )
        call_count = {"n": 0}

        def fake_get(url, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise requests.Timeout("timed out")
            return good_response

        with patch("sold_comps.requests.get", side_effect=fake_get):
            data, cache_hit = sc.fetch("test", "key")

        assert cache_hit is False
        assert data["organic_results"] == [{"product_id": "42"}]
        assert call_count["n"] == 2  # failed once, then succeeded

    def test_retry_then_succeed_503(self, tmp_path, monkeypatch):
        """fetch() retries on a 503 HTTPError and returns data on the second call."""
        monkeypatch.setattr(sc, "CACHE_DIR", tmp_path)
        monkeypatch.setattr(sc.time, "sleep", lambda *_: None)

        good_response = self._mock_response(
            ebay_url="https://www.ebay.com/?LH_Sold=1",
            organic_results=[{"product_id": "7"}],
        )
        call_count = {"n": 0}

        def fake_get(url, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                bad = MagicMock()
                bad.status_code = 503
                err = requests.HTTPError(response=bad)
                bad.raise_for_status = MagicMock(side_effect=err)
                bad.json = MagicMock(return_value={})
                # raise_for_status is called by fetch() on the response
                bad.raise_for_status.side_effect = err
                # Return the bad mock so fetch calls raise_for_status on it
                return bad
            return good_response

        with patch("sold_comps.requests.get", side_effect=fake_get):
            data, cache_hit = sc.fetch("test", "key")

        assert data["organic_results"] == [{"product_id": "7"}]
        assert call_count["n"] == 2

    def test_exhausted_transient_error_reraises(self, tmp_path, monkeypatch):
        """fetch() re-raises after exhausting all retries."""
        monkeypatch.setattr(sc, "CACHE_DIR", tmp_path)
        monkeypatch.setattr(sc.time, "sleep", lambda *_: None)

        with patch("sold_comps.requests.get",
                   side_effect=requests.ConnectionError("no route to host")):
            with pytest.raises(requests.ConnectionError):
                sc.fetch("test", "key")

    def test_non_retryable_4xx_not_retried(self, tmp_path, monkeypatch):
        """A 404 is NOT retried — requests.get is called exactly once."""
        monkeypatch.setattr(sc, "CACHE_DIR", tmp_path)
        monkeypatch.setattr(sc.time, "sleep", lambda *_: None)

        bad = MagicMock()
        bad.status_code = 404
        http_err = requests.HTTPError(response=bad)
        bad.raise_for_status = MagicMock(side_effect=http_err)
        bad.json = MagicMock(return_value={})

        with patch("sold_comps.requests.get", return_value=bad) as mock_get:
            with pytest.raises(requests.HTTPError):
                sc.fetch("test", "key")
            assert mock_get.call_count == 1

    def test_run_records_connection_error_not_crash(self, tmp_path, monkeypatch):
        """_run in fetch_book_comps records a RequestException as a query error,
        not as a top-level crash. comps remains empty and no exception propagates."""
        monkeypatch.setattr(sc, "CACHE_DIR", tmp_path)
        monkeypatch.setattr(sc.time, "sleep", lambda *_: None)

        def fake_fetch(nkw, api_key, *, force=False, ttl_sec=0):
            raise requests.ConnectionError("refused")

        monkeypatch.setattr(sc, "fetch", fake_fetch)

        out = sc.fetch_book_comps(
            {"title": "ASM", "issue": "142", "year": 1975, "grade": 6.5},
            "key",
        )

        # No exception propagated; comps empty
        assert out["comps"] == []
        # At least the base query recorded the error
        assert len(out["queries_used"]) >= 1
        errors = [q for q in out["queries_used"] if "error" in q]
        assert len(errors) >= 1
        assert "refused" in errors[0]["error"]


