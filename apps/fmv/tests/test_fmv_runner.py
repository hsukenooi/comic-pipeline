"""Tests for fmv_runner.py — the orchestrator.

We mock requests (DB cache + upsert) and subprocess (ebay-sold-comps),
so these tests don't hit the network or shell out.
"""

import json

from unittest.mock import MagicMock, patch
import pytest

import fmv_runner


# ─── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def server_url():
    return "http://test-server:8080"


@pytest.fixture(autouse=True)
def _no_first_party_outcomes_by_default(monkeypatch):
    """BUI-286: `_compute_and_upsert_one` now also calls out to
    `_fetch_first_party_outcomes` (a real HTTP GET). None of the tests in this
    file are about that feature — it's covered end-to-end in
    test_first_party_comps.py — so default it to a no-op here rather than
    editing every existing `_compute_and_upsert_one`/`run` call site to mock
    yet another network call it was never testing."""
    monkeypatch.setattr(fmv_runner, "_fetch_first_party_outcomes",
                        lambda *a, **k: [])


def _make_book(item_id, title, issue, year, grade, locg_id=None):
    book = {"item_id": item_id, "title": title, "issue": issue,
            "year": year, "grade": grade}
    if locg_id is not None:
        book["locg_id"] = locg_id
    return book


def _make_comp(price, grade, product_id="x"):
    return {"product_id": product_id, "title": f"comic {price}",
            "price": price, "grade": grade, "sold_date": "", "buying_format": ""}


# ─── _is_fetch_error / fetch-error vs no-comps (BUI-143) ──────────────────────

class TestFetchErrorSignal:
    def test_all_queries_errored_is_fetch_error(self):
        """A SerpApi quota/outage leaves comps empty with every query carrying an
        'error' — distinct from a genuinely illiquid book."""
        r = {"comp_count_total": 0,
             "queries_used": [{"tier": "base", "error": "RateLimiter 10001"}]}
        assert fmv_runner._is_fetch_error(r) is True

    def test_clean_empty_pool_is_not_fetch_error(self):
        """A book that genuinely has zero comps ran its queries cleanly (no
        'error') — must NOT be flagged as a fetch error."""
        r = {"comp_count_total": 0,
             "queries_used": [{"tier": "base", "nkw": 0}]}
        assert fmv_runner._is_fetch_error(r) is False

    def test_book_with_comps_is_not_fetch_error(self):
        r = {"comp_count_total": 5,
             "queries_used": [{"tier": "base", "error": "x"}]}
        assert fmv_runner._is_fetch_error(r) is False

    def test_table_renders_fetch_err_distinct_from_na(self, capsys):
        """The printed table must mark a fetch-failed book 'fetch-err', not the
        same 'n/a' a legitimately empty book gets, and warn loudly."""
        rows = [
            {"input": {"title": "Outage Book", "issue": "1", "grade": 9.4},
             "fmv": {"fmv_low": None}, "comp_count_total": 0,
             "queries_used": [{"tier": "base", "error": "quota"}],
             "source": "fresh"},
            {"input": {"title": "Illiquid Book", "issue": "2", "grade": 9.4},
             "fmv": {"fmv_low": None}, "comp_count_total": 0,
             "queries_used": [{"tier": "base", "nkw": 0}],
             "source": "fresh"},
        ]
        fmv_runner._print_table(rows)
        out = capsys.readouterr()
        combined = out.out + out.err
        assert "fetch-err" in combined
        assert "do not treat these as illiquid" in combined
        # The genuinely-empty row still reads n/a (only one fetch-err row).
        assert combined.count("fetch-err") >= 1


# ─── _split_by_db_cache ───────────────────────────────────────────────────────

class TestSplitByDbCache:
    def test_force_skips_lookup(self, server_url):
        books = [_make_book("1", "X", "1", 1990, 9.0, locg_id=100)]
        with patch("fmv_runner._db_lookup") as lookup:
            cached, needs = fmv_runner._split_by_db_cache(
                books, server_url=server_url, max_age_days=7, force=True)
            lookup.assert_not_called()
        assert cached == {}
        assert len(needs) == 1
        assert needs[0]["_idx"] == 0

    def test_book_without_locg_id_goes_to_compute(self, server_url):
        # BUI-153: the DB-FMV cache-skip requires a locg_id, so a title-derived
        # book (grade set, no locg_id) always falls through to a fresh compute —
        # which is why --max-age-days is inert in the orchestrated /comic:buy flow.
        books = [_make_book("1", "X", "1", 1990, 9.0)]
        with patch("fmv_runner._db_lookup") as lookup:
            cached, needs = fmv_runner._split_by_db_cache(
                books, server_url=server_url, max_age_days=7, force=False)
            lookup.assert_not_called()
        assert cached == {}
        assert len(needs) == 1

    def test_cache_hit_returns_row(self, server_url):
        books = [_make_book("1", "X", "1", 1990, 9.0, locg_id=100)]
        row = {"id": 1, "fmv_low": 50, "fmv_high": 75, "fmv_comps": 8,
               "fmv_confidence": "high", "fmv_updated_at": "2026-05-09T...",
               "title": "X", "issue": "1", "year": 1990, "grade": 9.0}
        with patch("fmv_runner._db_lookup", return_value=row):
            cached, needs = fmv_runner._split_by_db_cache(
                books, server_url=server_url, max_age_days=7, force=False)
        assert cached == {0: row}
        assert needs == []

    def test_cache_miss_falls_through(self, server_url):
        books = [_make_book("1", "X", "1", 1990, 9.0, locg_id=100)]
        with patch("fmv_runner._db_lookup", return_value=None):
            cached, needs = fmv_runner._split_by_db_cache(
                books, server_url=server_url, max_age_days=7, force=False)
        assert cached == {}
        assert len(needs) == 1
        assert needs[0]["_idx"] == 0


# ─── _db_lookup ───────────────────────────────────────────────────────────────

class TestDbLookup:
    def test_returns_freshest_row(self, server_url):
        rows = [
            {"id": 1, "fmv_updated_at": "2026-05-01T00:00:00", "title": "old",
             "locg_id": 1, "grade": 9.0, "fmv_low": 40},
            {"id": 2, "fmv_updated_at": "2026-05-09T00:00:00", "title": "new",
             "locg_id": 1, "grade": 9.0, "fmv_low": 50},
        ]
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = rows
        with patch("fmv_runner.requests.get", return_value=mock_resp):
            row = fmv_runner._db_lookup(server_url, locg_id=1, grade=9.0,
                                        max_age_days=7)
        assert row["title"] == "new"

    def test_skips_stub_rows(self, server_url):
        """BUI-44: a stub fmv row (null fmv_low, written when n=0 comps) links
        the comic but has no pricing to reuse — it must NOT count as a cache
        hit, so the book falls through to a fresh recompute."""
        rows = [
            {"id": 1, "title": "stub", "locg_id": 1, "grade": 9.0,
             "fmv_updated_at": "2026-05-31T00:00:00", "fmv_low": None},
        ]
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = rows
        with patch("fmv_runner.requests.get", return_value=mock_resp):
            row = fmv_runner._db_lookup(server_url, locg_id=1, grade=9.0,
                                        max_age_days=7)
        assert row is None  # stub is not a reusable cache hit

    def test_filters_out_mismatched_locg_id(self, server_url):
        """Defensive: even if the server returns extra rows (because it's
        running an older version that ignores the locg_id filter), the
        client re-filters and only accepts exact matches."""
        rows = [
            {"id": 1, "title": "wrong comic", "locg_id": 999, "grade": 9.0,
             "fmv_updated_at": "2026-05-09T00:00:00"},
        ]
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = rows
        with patch("fmv_runner.requests.get", return_value=mock_resp):
            row = fmv_runner._db_lookup(server_url, locg_id=1, grade=9.0,
                                        max_age_days=7)
        assert row is None  # locg_id mismatch → cache miss

    def test_variant_blind_lookup_returns_correct_variant(self, server_url):
        """BUI-139: a base cover and a Newsstand variant of one issue share the
        same issue-level locg_id (only locg_variant_id differs), so a
        locg_id+grade match alone is variant-blind and could reuse the wrong
        price tier. A base request (locg_variant_id=None) returns only the
        NULL-variant row; a specific-variant request returns only that variant.
        The server here returns BOTH rows (simulating an old server that ignores
        the param), so this also pins the client-side re-check."""
        rows = [
            {"id": 1, "locg_id": 1, "locg_variant_id": None, "grade": 9.4,
             "fmv_low": 40, "fmv_updated_at": "2026-05-09T00:00:00"},
            {"id": 2, "locg_id": 1, "locg_variant_id": 77, "grade": 9.4,
             "fmv_low": 120, "fmv_updated_at": "2026-05-10T00:00:00"},
        ]
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = rows
        with patch("fmv_runner.requests.get", return_value=mock_resp):
            base = fmv_runner._db_lookup(server_url, locg_id=1, grade=9.4,
                                         locg_variant_id=None, max_age_days=7)
            variant = fmv_runner._db_lookup(server_url, locg_id=1, grade=9.4,
                                            locg_variant_id=77, max_age_days=7)
        assert base["id"] == 1 and base["fmv_low"] == 40       # base, not variant
        assert variant["id"] == 2 and variant["fmv_low"] == 120  # variant, not base

    def test_empty_returns_none(self, server_url):
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = []
        with patch("fmv_runner.requests.get", return_value=mock_resp):
            row = fmv_runner._db_lookup(server_url, locg_id=1, grade=9.0,
                                        max_age_days=7)
        assert row is None

    def test_network_error_returns_none(self, server_url):
        import requests
        with patch("fmv_runner.requests.get",
                   side_effect=requests.ConnectionError("nope")):
            row = fmv_runner._db_lookup(server_url, locg_id=1, grade=9.0,
                                        max_age_days=7)
        assert row is None  # fail-soft so we still try to compute fresh


# ─── _compute_and_upsert_one ──────────────────────────────────────────────────

class TestComputeOne:
    def test_skips_when_no_grade(self, server_url):
        result = {"input": {"title": "X", "issue": "1"}, "comps": []}
        out = fmv_runner._compute_and_upsert_one(
            result, {"title": "X", "issue": "1"}, server_url=server_url)
        assert out["source"] == "error"
        assert "no target grade" in out["error"]

    def test_runs_math_and_upserts(self, server_url):
        comps = [_make_comp(p, 8.0) for p in [10, 11, 12, 13, 14]]
        result = {
            "input": {"title": "X", "issue": "1", "year": 1990, "grade": 8.0},
            "comps": comps,
        }
        upsert_mock = MagicMock()
        with patch("fmv_runner._upsert_fmv", upsert_mock):
            upsert_mock.return_value = {"id": 99}
            out = fmv_runner._compute_and_upsert_one(
                result, {"title": "X", "issue": "1", "grade": 8.0,
                         "locg_id": 100},
                server_url=server_url)
        assert out["source"] == "fresh"
        assert out["fmv"]["n"] == 5
        assert out["db_row"]["id"] == 99
        # Upsert should have been called with locg_id from the original book
        body = upsert_mock.call_args.args[1]
        assert body.get("locg_id") == 100

    def test_grade_window_threads_through_without_bypassing_guard(self, server_url):
        """BUI-86 AE4: --grade-window raises the ceiling but a one-sided book
        stays flagged — the flag is never manufactured into a price."""
        comps = [_make_comp(p, 9.0) for p in [40, 42, 44, 45, 41]]
        result = {
            "input": {"title": "FF", "issue": "63", "year": 1967, "grade": 9.6},
            "comps": comps,
        }
        with patch("fmv_runner._upsert_fmv", return_value={"id": 1}):
            out = fmv_runner._compute_and_upsert_one(
                result, {"title": "FF", "issue": "63", "grade": 9.6},
                server_url=server_url, grade_window=2.5)
        assert out["fmv"]["flag_reason"] == "one_sided"
        assert out["fmv"]["max_bid"] is None

    def test_lower_grade_window_caps_reach(self, server_url):
        """A tightened ceiling can't widen far enough → flags too_sparse for a
        book that would otherwise have widened to gather a pool."""
        comps = [_make_comp(100, 7.0), _make_comp(110, 8.0), _make_comp(120, 8.0)]
        result = {
            "input": {"title": "X", "issue": "1", "year": 1990, "grade": 7.0},
            "comps": comps,
        }
        with patch("fmv_runner._upsert_fmv", return_value={"id": 1}):
            out = fmv_runner._compute_and_upsert_one(
                result, {"title": "X", "issue": "1", "grade": 7.0},
                server_url=server_url, grade_window=0.5)
        assert out["fmv"]["window"] == 0.5
        assert out["fmv"]["flag_reason"] == "too_sparse"

    def test_grade_confidence_threads_into_haircut(self, server_url):
        """BUI-51: grade_confidence on the batch envelope must reach compute_fmv
        and haircut the bid, and the upsert notes must surface it."""
        comps = [_make_comp(p, 8.0) for p in
                 [100, 110, 120, 130, 140, 150, 160, 170, 180]]
        result = {
            "input": {"title": "X", "issue": "1", "year": 1990, "grade": 8.0},
            "comps": comps,
        }
        captured = {}
        def _capture(server, inp, fmv):
            captured["fmv"] = fmv
            return {"id": 1}
        with patch("fmv_runner._upsert_fmv", side_effect=_capture):
            out = fmv_runner._compute_and_upsert_one(
                result,
                {"title": "X", "issue": "1", "grade": 8.0,
                 "grade_confidence": "low"},
                server_url=server_url)
        assert out["fmv"]["bid_factor"] == 0.60          # haircut applied
        assert out["fmv"]["grade_confidence"] == "low"
        assert out["input"]["grade_confidence"] == "low"  # echoed in input summary
        # Notes (persisted to fmv_notes) explain the lowered bid
        notes = fmv_runner._build_notes(captured["fmv"])
        assert "bid_haircut=0.60" in notes

    def test_absent_grade_confidence_no_haircut_in_runner(self, server_url):
        comps = [_make_comp(p, 8.0) for p in
                 [100, 110, 120, 130, 140, 150, 160, 170, 180]]
        result = {
            "input": {"title": "X", "issue": "1", "year": 1990, "grade": 8.0},
            "comps": comps,
        }
        with patch("fmv_runner._upsert_fmv", return_value={"id": 1}):
            out = fmv_runner._compute_and_upsert_one(
                result, {"title": "X", "issue": "1", "grade": 8.0},
                server_url=server_url)
        assert out["fmv"]["bid_factor"] == fmv_runner.fmv_math.BASE_BID_FACTOR
        assert "bid_haircut" not in fmv_runner._build_notes(out["fmv"])

    def test_coerces_letter_grade_string(self, server_url):
        """Wish-list caches sometimes carry letter grades. The runner must
        coerce them to numeric so fmv_math doesn't silently return n=0."""
        comps = [_make_comp(p, 8.5) for p in [20, 22, 24, 26, 28]]
        result = {
            "input": {"title": "X", "issue": "1", "year": 1990, "grade": "VF+"},
            "comps": comps,
        }
        with patch("fmv_runner._upsert_fmv", return_value={"id": 1}):
            out = fmv_runner._compute_and_upsert_one(
                result, {"title": "X", "issue": "1", "grade": "VF+"},
                server_url=server_url)
        assert out["source"] == "fresh"
        assert out["fmv"]["n"] == 5  # not silently 0
        assert out["input"]["grade"] == 8.5  # coerced

    def test_string_grade_does_not_clobber_numeric(self, server_url):
        """If sold_comps resolved a numeric grade, an original-book string
        grade must not overwrite it during the merge."""
        comps = [_make_comp(p, 8.0) for p in [10, 11, 12, 13, 14]]
        result = {
            "input": {"title": "X", "issue": "1", "year": 1990, "grade": 8.0},
            "comps": comps,
        }
        with patch("fmv_runner._upsert_fmv", return_value={"id": 1}):
            out = fmv_runner._compute_and_upsert_one(
                result, {"title": "X", "issue": "1", "grade": "VF"},
                server_url=server_url)
        assert out["source"] == "fresh"
        assert out["input"]["grade"] == 8.0
        assert out["fmv"]["n"] == 5

    def test_returns_comic_id_and_fmv_id_when_present(self, server_url):
        """PER-146: surface comic_id and fmv_id from the upsert response so
        the /comic:buy orchestrator can thread them into snipe-add."""
        comps = [_make_comp(p, 8.0) for p in [10, 11, 12, 13, 14]]
        result = {
            "input": {"title": "X", "issue": "1", "year": 1990, "grade": 8.0},
            "comps": comps,
        }
        with patch("fmv_runner._upsert_fmv",
                   return_value={"comic_id": 42, "fmv_id": 99}):
            out = fmv_runner._compute_and_upsert_one(
                result, {"title": "X", "issue": "1", "grade": 8.0},
                server_url=server_url)
        assert out["comic_id"] == 42
        assert out["fmv_id"] == 99

    def test_ids_are_none_when_server_omits_them(self, server_url):
        """Graceful with old server versions that only return the comics row
        (no comic_id / fmv_id keys yet)."""
        comps = [_make_comp(p, 8.0) for p in [10, 11, 12, 13, 14]]
        result = {
            "input": {"title": "X", "issue": "1", "year": 1990, "grade": 8.0},
            "comps": comps,
        }
        with patch("fmv_runner._upsert_fmv", return_value={"id": 1, "title": "X"}):
            out = fmv_runner._compute_and_upsert_one(
                result, {"title": "X", "issue": "1", "grade": 8.0},
                server_url=server_url)
        assert out["comic_id"] is None
        assert out["fmv_id"] is None

    def test_upserts_stub_comic_when_no_comps(self, server_url):
        """BUI-44: with n=0 comps, still upsert the comics row + a stub fmv
        (null low/high, comps=0, confidence low) and surface comic_id, so the
        bid links to a comic and verify shows no_fmv_at_grade, not no_comic.

        Exercises the real _upsert_fmv (mocking only requests.post) so the
        stub POST body is asserted end to end."""
        result = {
            "input": {"title": "Godzilla: The Half-Century War",
                      "issue": "1", "year": 2012, "grade": 9.8},
            "comps": [],  # no sold comps found -> n=0
        }
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"comic_id": 7, "fmv_id": 3, "id": 7}
        with patch("fmv_runner.requests.post", return_value=mock_resp) as post_mock:
            out = fmv_runner._compute_and_upsert_one(
                result,
                {"title": "Godzilla: The Half-Century War", "issue": "1",
                 "grade": 9.8},
                server_url=server_url)

        # The upsert must happen even with zero comps.
        post_mock.assert_called_once()
        body = post_mock.call_args.kwargs["json"]
        assert body["grade"] == 9.8
        assert body["fmv_low"] is None
        assert body["fmv_high"] is None
        assert body["fmv_comps"] == 0
        assert body["fmv_confidence"] == "low"
        # BUI-132: an n=0 stub is NOT flagged — it posts a null flag_reason so the
        # server's COALESCE keeps any prior real price (the n=0 stub guard).
        assert body["fmv_flag_reason"] is None

        assert out["source"] == "fresh"
        assert out["fmv"]["n"] == 0
        assert out["fmv"]["fmv_low"] is None
        assert out["comic_id"] == 7
        assert out["fmv_id"] == 3

    def test_flagged_book_upserts_stub_with_manual_token(self, server_url):
        """BUI-86: a needs_manual book (one-sided pool) writes the same stub
        shape as n=0 — null pricing, comps=pool size, confidence low — but its
        fmv_notes carry the manual_review token, and comic_id is returned so it
        stays linked."""
        comps = [_make_comp(p, 9.0) for p in [40, 42, 44, 45, 41]]
        result = {
            "input": {"title": "FF", "issue": "63", "year": 1967, "grade": 9.6},
            "comps": comps,
        }
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"comic_id": 11, "fmv_id": 5, "id": 11}
        with patch("fmv_runner.requests.post", return_value=mock_resp) as post_mock:
            out = fmv_runner._compute_and_upsert_one(
                result, {"title": "FF", "issue": "63", "grade": 9.6, "locg_id": 200},
                server_url=server_url)

        body = post_mock.call_args.kwargs["json"]
        assert body["fmv_low"] is None and body["fmv_high"] is None
        assert body["fmv_comps"] == 5            # un-priced pool size preserved
        assert body["fmv_confidence"] == "low"   # forced LOW even though dense
        assert "manual_review=one_sided" in body["fmv_notes"]
        # BUI-132: the flag now also rides a structured column, not just the
        # fmv_notes token, so the server can verdict needs_manual + clear stale price.
        assert body["fmv_flag_reason"] == "one_sided"
        assert out["fmv"]["flag_reason"] == "one_sided"
        assert out["comic_id"] == 11

    def test_unrecognized_grade_string_errors(self, server_url):
        """If the grade string can't be coerced, log and return an error
        row rather than silently passing it to fmv_math."""
        result = {
            "input": {"title": "X", "issue": "1", "grade": "ZZ?"},
            "comps": [_make_comp(10, 9.0)],
        }
        out = fmv_runner._compute_and_upsert_one(
            result, {"title": "X", "issue": "1", "grade": "ZZ?"},
            server_url=server_url)
        assert out["source"] == "error"
        assert "ZZ?" in out["error"]

    def test_upserts_stub_when_pool_empty(self, server_url):
        """BUI-44: ungraded comps yield an empty pool (n=0), but we still upsert
        the comics row + stub fmv so the bid links to a comic (no_fmv_at_grade,
        not no_comic). Previously this path skipped the upsert."""
        comps = [_make_comp(10, None)]  # ungraded → excluded → empty pool
        result = {
            "input": {"title": "X", "issue": "1", "grade": 9.0},
            "comps": comps,
        }
        with patch("fmv_runner._upsert_fmv",
                   return_value={"comic_id": 5, "fmv_id": 2}) as upsert_mock:
            out = fmv_runner._compute_and_upsert_one(
                result, {"title": "X", "issue": "1", "grade": 9.0},
                server_url=server_url)
            upsert_mock.assert_called_once()
        assert out["fmv"]["fmv_low"] is None
        assert out["fmv"]["n"] == 0
        assert out["comic_id"] == 5


# ─── _upsert_fmv ──────────────────────────────────────────────────────────────

class TestUpsertFmv:
    def test_posts_payload(self, server_url):
        inp = {"title": "X", "issue": "1", "year": 1990, "grade": 9.0,
               "locg_id": 42}
        fmv = {"fmv_low": 100, "fmv_high": 150, "n": 8, "confidence": "HIGH",
               "window": 0.5, "cv_pct": "20%"}

        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"id": 1}
        with patch("fmv_runner.requests.post", return_value=mock_resp) as post:
            fmv_runner._upsert_fmv(server_url, inp, fmv)
            body = post.call_args.kwargs["json"]
        assert body["title"] == "X"
        assert body["fmv_low"] == 100
        assert body["fmv_high"] == 150
        assert body["fmv_confidence"] == "high"
        assert body["locg_id"] == 42

    def test_upsert_fmv_fails_loud_on_post_error(self, server_url):
        """BUI-186: a failed FMV upsert aborts the run (fail loud) instead of
        returning None and proceeding with a book that was priced but never
        linked (the downstream snipe-add FMV link would silently break)."""
        import requests
        inp = {"title": "X", "issue": "1", "year": 1990, "grade": 9.0}
        fmv = {"fmv_low": 100, "fmv_high": 150, "n": 8, "confidence": "HIGH",
               "window": 0.5, "cv_pct": "20%"}
        with patch("fmv_runner.requests.post",
                   side_effect=requests.ConnectionError("server down")):
            with pytest.raises(SystemExit):
                fmv_runner._upsert_fmv(server_url, inp, fmv)

    def test_collapses_finegrained_confidence(self, server_url):
        # MEDIUM-HIGH and MEDIUM both map to "medium"
        # MEDIUM-LOW and LOW both map to "low"
        cases = [
            ("HIGH", "high"),
            ("MEDIUM-HIGH", "medium"),
            ("MEDIUM", "medium"),
            ("MEDIUM-LOW", "low"),
            ("LOW", "low"),
        ]
        for label, expected in cases:
            assert fmv_runner._confidence_to_db_label(label) == expected


# ─── _stitch ──────────────────────────────────────────────────────────────────

class TestStitch:
    def test_preserves_input_order(self):
        books = [
            _make_book("a", "A", "1", 1990, 9.0),
            _make_book("b", "B", "2", 1991, 8.0),
            _make_book("c", "C", "3", 1992, 7.0),
        ]
        cached = {0: {"fmv_low": 5, "fmv_high": 10, "fmv_comps": 5,
                      "fmv_confidence": "low",
                      "title": "A", "issue": "1", "year": 1990, "grade": 9.0}}
        fresh = {
            1: {"input": {"title": "B"}, "fmv": {"fmv_low": 20, "fmv_high": 30,
                "n": 8, "median": 25, "max_bid": 25,
                "confidence": "HIGH", "window": 0.5, "cv_pct": "10%",
                "trimmed_pool": [], "cv": 0.1},
                "comp_count_total": 8, "queries_used": [], "db_row": None,
                "source": "fresh"},
            2: {"input": {"title": "C"}, "fmv": {"fmv_low": 40, "fmv_high": 50,
                "n": 6, "median": 45, "max_bid": 40,
                "confidence": "MEDIUM", "window": 0.5, "cv_pct": "30%",
                "trimmed_pool": [], "cv": 0.3},
                "comp_count_total": 6, "queries_used": [], "db_row": None,
                "source": "fresh"},
        }
        out = fmv_runner._stitch(books, cached, fresh)
        assert len(out) == 3
        assert out[0]["source"] == "cached"
        assert out[1]["source"] == "fresh"
        assert out[2]["source"] == "fresh"
        assert out[1]["input"]["title"] == "B"

    def test_records_error_when_neither(self):
        books = [_make_book("a", "A", "1", 1990, 9.0)]
        out = fmv_runner._stitch(books, {}, {})
        assert out[0]["source"] == "error"
        assert "no comps fetched" in out[0]["error"]

    def test_cached_path_applies_grade_haircut(self):
        """BUI-51: a cache hit on a freshly low-confidence grade must still be
        haircut — reusing a recent FMV at full 80% is the gap this closes."""
        book = _make_book("a", "A", "1", 1990, 9.0)
        book["grade_confidence"] = "low"
        cached = {0: {"fmv_low": 50, "fmv_high": 100, "fmv_comps": 8,
                      "fmv_confidence": "high",
                      "title": "A", "issue": "1", "year": 1990, "grade": 9.0}}
        out = fmv_runner._stitch([book], cached, {})
        assert out[0]["source"] == "cached"
        # high comp confidence + low grade confidence → conservative LOW → 0.60
        assert out[0]["fmv"]["bid_factor"] == 0.60
        assert out[0]["fmv"]["max_bid"] == fmv_runner.fmv_math.clean_round(100 * 0.60)

    def test_cached_path_no_grade_confidence_unchanged(self):
        book = _make_book("a", "A", "1", 1990, 9.0)  # no grade_confidence
        cached = {0: {"fmv_low": 50, "fmv_high": 100, "fmv_comps": 8,
                      "fmv_confidence": "high",
                      "title": "A", "issue": "1", "year": 1990, "grade": 9.0}}
        out = fmv_runner._stitch([book], cached, {})
        assert out[0]["fmv"]["bid_factor"] == fmv_runner.fmv_math.BASE_BID_FACTOR
        assert out[0]["fmv"]["max_bid"] == fmv_runner.fmv_math.clean_round(100 * 0.80)


# ─── Flagged-state presentation (BUI-86) ─────────────────────────────────────

class TestFlaggedPresentation:
    def test_build_notes_carries_manual_token_and_span(self):
        fmv = {"window": 1.0, "cv_pct": "n/a", "confidence": "LOW",
               "flag_reason": "one_sided", "grade_span": 1.5, "bid_factor": 0.80}
        notes = fmv_runner._build_notes(fmv)
        assert "manual_review=one_sided" in notes
        assert "span=1.5" in notes

    def test_build_notes_omits_manual_token_when_priced(self):
        fmv = {"window": 0.5, "cv_pct": "20%", "confidence": "HIGH",
               "flag_reason": None, "grade_span": 0.0, "bid_factor": 0.80}
        notes = fmv_runner._build_notes(fmv)
        assert "manual_review" not in notes

    def test_build_notes_no_bid_haircut_on_flagged_book(self):
        # A flagged book's forced-LOW label yields factor 0.60, but it has no
        # max bid — the bid_haircut token would be misleading, so it's suppressed.
        fmv = {"window": 2.0, "cv_pct": "n/a", "confidence": "LOW",
               "flag_reason": "too_wide", "grade_span": 4.0, "bid_factor": 0.60,
               "grade_confidence": None}
        notes = fmv_runner._build_notes(fmv)
        assert "manual_review=too_wide" in notes
        assert "bid_haircut" not in notes

    def test_print_table_distinguishes_three_states(self, capsys):
        rows = [
            {"input": {"title": "Priced", "issue": "1", "grade": 8.0},
             "fmv": {"flag_reason": None, "fmv_low": 100, "fmv_high": 150,
                     "median": 125, "max_bid": 120, "n": 8, "cv_pct": "20%",
                     "confidence": "HIGH"}, "source": "fresh"},
            {"input": {"title": "Flagged", "issue": "2", "grade": 9.6},
             "fmv": {"flag_reason": "one_sided", "fmv_low": None, "fmv_high": None,
                     "median": None, "max_bid": None, "n": 5, "cv_pct": "n/a",
                     "confidence": "LOW"}, "source": "fresh"},
            {"input": {"title": "NoComps", "issue": "3", "grade": 7.0},
             "fmv": {"flag_reason": None, "fmv_low": None, "fmv_high": None,
                     "median": None, "max_bid": None, "n": 0, "cv_pct": "n/a",
                     "confidence": "LOW"}, "source": "fresh"},
        ]
        fmv_runner._print_table(rows)
        out = capsys.readouterr().out
        assert "manual:one_sided" in out   # flagged row
        assert "$100–$150" in out          # priced row
        assert "n/a" in out                # no-comps row
        # The flagged and no-comps rows must NOT render identically
        assert out.count("manual:one_sided") == 1

    def test_fmv_from_db_row_has_new_keys(self):
        row = {"fmv_low": 50, "fmv_high": 100, "fmv_comps": 8,
               "fmv_confidence": "high"}
        out = fmv_runner._fmv_from_db_row(row)
        assert "flag_reason" in out and out["flag_reason"] is None
        assert "grade_span" in out and out["grade_span"] is None

    def test_falsy_zero_fmv_high_yields_zero_max_bid_not_none(self):
        """BUI-182: a legitimate fmv_high of 0 must round to a 0 max_bid, not be
        nulled by a falsy check."""
        row = {"fmv_low": 0, "fmv_high": 0, "fmv_comps": 5,
               "fmv_confidence": "high", "fmv_notes": "window=±0.5"}
        out = fmv_runner._fmv_from_db_row(row)
        assert out["max_bid"] == 0

    def test_wide_window_caps_confidence_on_cached_reuse(self):
        """BUI-182: a stored row built past the wide-window boundary must reuse at
        MEDIUM even if its persisted confidence label is HIGH."""
        row = {"fmv_low": 60, "fmv_high": 100, "fmv_comps": 8,
               "fmv_confidence": "high", "fmv_notes": "window=±1.5 | cv=20%"}
        out = fmv_runner._fmv_from_db_row(row)
        assert out["window"] == 1.5
        assert out["confidence"] == "MEDIUM"

    def test_narrow_window_keeps_stored_confidence(self):
        row = {"fmv_low": 60, "fmv_high": 100, "fmv_comps": 8,
               "fmv_confidence": "high", "fmv_notes": "window=±1.0 | cv=20%"}
        out = fmv_runner._fmv_from_db_row(row)
        assert out["window"] == 1.0
        assert out["confidence"] == "HIGH"

    def test_unparseable_notes_window_is_none_and_no_cap(self):
        row = {"fmv_low": 60, "fmv_high": 100, "fmv_comps": 8,
               "fmv_confidence": "high", "fmv_notes": ""}
        out = fmv_runner._fmv_from_db_row(row)
        assert out["window"] is None
        assert out["confidence"] == "HIGH"


# ─── End-to-end (with mocks) ──────────────────────────────────────────────────

class TestRunEndToEnd:
    def test_cached_path_skips_subprocess(self, tmp_path, server_url, capsys):
        batch = [
            {"item_id": "1", "title": "X", "issue": "1", "year": 1990,
             "grade": 9.0, "locg_id": 100},
        ]
        batch_path = tmp_path / "batch.json"
        batch_path.write_text(json.dumps(batch))
        out_path = tmp_path / "out.json"

        cached_row = {
            "id": 1, "title": "X", "issue": "1", "year": 1990, "grade": 9.0,
            "fmv_low": 50, "fmv_high": 75, "fmv_comps": 8,
            "fmv_confidence": "high", "fmv_notes": "",
            "fmv_updated_at": "2026-05-09T00:00:00",
            "locg_id": 100, "locg_variant_id": None,
        }

        with patch("fmv_runner._db_lookup", return_value=cached_row), \
             patch("fmv_runner._fetch_comps") as fetch_mock:
            fmv_runner.run(batch_path=str(batch_path), out_path=str(out_path),
                           max_age_days=7, force=False, quiet=True,
                           server_url=server_url)
            fetch_mock.assert_not_called()  # cache hit → no subprocess

        out = json.loads(out_path.read_text())
        assert len(out) == 1
        assert out[0]["source"] == "cached"

    def test_fresh_path_runs_subprocess(self, tmp_path, server_url, capsys):
        batch = [
            {"item_id": "1", "title": "X", "issue": "1", "year": 1990,
             "grade": 9.0},  # no locg_id → must compute fresh
        ]
        batch_path = tmp_path / "batch.json"
        batch_path.write_text(json.dumps(batch))
        out_path = tmp_path / "out.json"

        fake_comps = [_make_comp(p, 9.0) for p in [50, 55, 60, 65, 70]]
        fake_result = [{
            # _req_id echoes the original input index (BUI-174/187); the real
            # _fetch_comps + ebay-sold-comps carry it, so the mock must too.
            "input": {"_req_id": 0, "title": "X", "issue": "1", "year": 1990,
                      "grade": 9.0, "item_id": "1"},
            "comps": fake_comps,
            "queries_used": [{"tier": "base", "cached": False}],
        }]

        upserted = {"id": 99}
        with patch("fmv_runner._fetch_comps", return_value=fake_result), \
             patch("fmv_runner._upsert_fmv", return_value=upserted) as upsert:
            fmv_runner.run(batch_path=str(batch_path), out_path=str(out_path),
                           max_age_days=7, force=False, quiet=True,
                           server_url=server_url)
            upsert.assert_called_once()

        out = json.loads(out_path.read_text())
        assert out[0]["source"] == "fresh"
        assert out[0]["fmv"]["n"] == 5

    def test_no_server_url_fails(self, tmp_path):
        batch_path = tmp_path / "b.json"
        batch_path.write_text("[]")
        with pytest.raises(SystemExit):
            fmv_runner.run(batch_path=str(batch_path), out_path=None,
                           max_age_days=7, force=False,
                           quiet=True, server_url=None)

    def test_fresh_results_mapped_by_id_not_position(self, tmp_path, server_url):
        """BUI-174/187: if the subprocess returns results in a different order,
        each book must still get ITS OWN comps — not its neighbour's."""
        batch = [
            {"item_id": "A", "title": "Aaa", "issue": "1", "year": 1990, "grade": 9.0},
            {"item_id": "B", "title": "Bbb", "issue": "2", "year": 1991, "grade": 9.0},
        ]
        batch_path = tmp_path / "batch.json"
        batch_path.write_text(json.dumps(batch))
        out_path = tmp_path / "out.json"

        low = [_make_comp(p, 9.0, product_id=f"l{p}") for p in [10, 11, 12, 13, 14]]
        high = [_make_comp(p, 9.0, product_id=f"h{p}") for p in
                [1000, 1100, 1200, 1300, 1400]]
        # Returned REVERSED relative to the input order; each carries its _req_id
        # (book A == idx 0 == low pool; book B == idx 1 == high pool).
        reordered = [
            {"input": {"_req_id": 1, "title": "Bbb", "issue": "2", "year": 1991,
                       "grade": 9.0, "item_id": "B"}, "comps": high, "queries_used": []},
            {"input": {"_req_id": 0, "title": "Aaa", "issue": "1", "year": 1990,
                       "grade": 9.0, "item_id": "A"}, "comps": low, "queries_used": []},
        ]
        with patch("fmv_runner._fetch_comps", return_value=reordered), \
             patch("fmv_runner._upsert_fmv", return_value={"id": 1}):
            fmv_runner.run(batch_path=str(batch_path), out_path=str(out_path),
                           max_age_days=7, force=False, quiet=True,
                           server_url=server_url)

        out = json.loads(out_path.read_text())
        # Output stays in input order; book A keeps the LOW pool, book B the HIGH.
        assert out[0]["input"]["title"] == "Aaa"
        assert out[1]["input"]["title"] == "Bbb"
        assert out[0]["fmv"]["fmv_high"] < 100      # would be ~1300 if mapped by position
        assert out[1]["fmv"]["fmv_high"] > 500

    def test_result_count_mismatch_fails_loud(self, tmp_path, server_url):
        """A dropped result (count mismatch) must abort, never map positionally."""
        batch = [
            {"item_id": "A", "title": "Aaa", "issue": "1", "year": 1990, "grade": 9.0},
            {"item_id": "B", "title": "Bbb", "issue": "2", "year": 1991, "grade": 9.0},
        ]
        batch_path = tmp_path / "batch.json"
        batch_path.write_text(json.dumps(batch))

        only_one = [{"input": {"_req_id": 0, "title": "Aaa", "issue": "1",
                               "year": 1990, "grade": 9.0, "item_id": "A"},
                     "comps": [], "queries_used": []}]
        with patch("fmv_runner._fetch_comps", return_value=only_one), \
             patch("fmv_runner._upsert_fmv", return_value={"id": 1}):
            with pytest.raises(SystemExit):
                fmv_runner.run(batch_path=str(batch_path), out_path=None,
                               max_age_days=7, force=False, quiet=True,
                               server_url=server_url)

    def test_missing_req_id_fails_loud(self, tmp_path, server_url):
        """A result without a _req_id (e.g. a stale ebay-sold-comps that doesn't
        echo it) must fail loud, not silently mis-map (version-skew guard)."""
        batch = [{"item_id": "A", "title": "Aaa", "issue": "1", "year": 1990,
                  "grade": 9.0}]
        batch_path = tmp_path / "batch.json"
        batch_path.write_text(json.dumps(batch))

        no_id = [{"input": {"title": "Aaa", "issue": "1", "year": 1990,
                            "grade": 9.0, "item_id": "A"},
                  "comps": [], "queries_used": []}]
        with patch("fmv_runner._fetch_comps", return_value=no_id), \
             patch("fmv_runner._upsert_fmv", return_value={"id": 1}):
            with pytest.raises(SystemExit):
                fmv_runner.run(batch_path=str(batch_path), out_path=None,
                               max_age_days=7, force=False, quiet=True,
                               server_url=server_url)

    def test_fetch_comps_threads_req_id_into_subprocess_payload(self, tmp_path,
                                                                monkeypatch):
        """BUI-174/187 (fmv→ebay direction): _fetch_comps must send a _req_id with
        each book so the subprocess can echo it back."""
        captured = {}

        def fake_run(cmd, capture_output, text, timeout=None):
            # cmd = [bin, --batch, in_path, --out, out_path, --quiet, ...]
            in_path = cmd[cmd.index("--batch") + 1]
            out_path = cmd[cmd.index("--out") + 1]
            with open(in_path) as fh:
                captured["payload"] = json.load(fh)
            with open(out_path, "w") as fh:
                fh.write("[]")
            return type("R", (), {"returncode": 0, "stderr": ""})()

        monkeypatch.setattr(fmv_runner.shutil, "which", lambda _b: "/usr/bin/ebay")
        monkeypatch.setattr(fmv_runner.subprocess, "run", fake_run)

        books = [{"_idx": 3, "title": "X", "issue": "1"},
                 {"_idx": 7, "title": "Y", "issue": "2"}]
        fmv_runner._fetch_comps(books, force=False)

        sent_ids = [b["_req_id"] for b in captured["payload"]]
        assert sent_ids == [3, 7]
        assert all("_idx" not in b for b in captured["payload"])


# ─── _fetch_comps robustness (BUI-184) ────────────────────────────────────────

class TestFetchCompsRobustness:
    def _wire(self, monkeypatch):
        monkeypatch.setattr(fmv_runner.shutil, "which", lambda _b: "/usr/bin/ebay")

    def _book(self, idx=0):
        return {"_idx": idx, "title": "X", "issue": "1"}

    def test_timeout_fails_loud(self, monkeypatch):
        """A hung child must abort comic-fmv, not hang forever (BUI-184)."""
        self._wire(monkeypatch)

        def fake_run(cmd, capture_output, text, timeout=None):
            raise fmv_runner.subprocess.TimeoutExpired(cmd, timeout)

        monkeypatch.setattr(fmv_runner.subprocess, "run", fake_run)
        with pytest.raises(SystemExit):
            fmv_runner._fetch_comps([self._book()], force=False)

    def test_empty_output_fails_loud(self, monkeypatch):
        """returncode 0 but an empty out file must fail loud, not crash (BUI-184)."""
        self._wire(monkeypatch)

        def fake_run(cmd, capture_output, text, timeout=None):
            out_path = cmd[cmd.index("--out") + 1]
            with open(out_path, "w") as fh:
                fh.write("   ")
            return type("R", (), {"returncode": 0, "stderr": ""})()

        monkeypatch.setattr(fmv_runner.subprocess, "run", fake_run)
        with pytest.raises(SystemExit):
            fmv_runner._fetch_comps([self._book()], force=False)

    def test_unparseable_output_fails_loud(self, monkeypatch):
        """A partial/garbage out file fails loud with a clear error (BUI-184)."""
        self._wire(monkeypatch)

        def fake_run(cmd, capture_output, text, timeout=None):
            out_path = cmd[cmd.index("--out") + 1]
            with open(out_path, "w") as fh:
                fh.write("{not json")
            return type("R", (), {"returncode": 0, "stderr": ""})()

        monkeypatch.setattr(fmv_runner.subprocess, "run", fake_run)
        with pytest.raises(SystemExit):
            fmv_runner._fetch_comps([self._book()], force=False)

    def test_timeout_scales_with_batch_size(self, monkeypatch):
        self._wire(monkeypatch)
        seen = {}

        def fake_run(cmd, capture_output, text, timeout=None):
            seen["timeout"] = timeout
            out_path = cmd[cmd.index("--out") + 1]
            with open(out_path, "w") as fh:
                fh.write("[]")
            return type("R", (), {"returncode": 0, "stderr": ""})()

        monkeypatch.setattr(fmv_runner.subprocess, "run", fake_run)
        fmv_runner._fetch_comps([self._book(0), self._book(1), self._book(2)],
                                force=False)
        assert seen["timeout"] == (fmv_runner._SUBPROCESS_TIMEOUT_BASE
                                   + 3 * fmv_runner._SUBPROCESS_TIMEOUT_PER_BOOK)
