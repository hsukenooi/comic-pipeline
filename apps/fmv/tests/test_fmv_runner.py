"""Tests for fmv_runner.py — the orchestrator.

We mock requests (DB cache + upsert) and subprocess (ebay-sold-comps),
so these tests don't hit the network or shell out.
"""

import json

from unittest.mock import MagicMock, patch
import pytest

import fmv_math
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

    def test_malformed_json_warns_and_returns_none(self, server_url, capsys):
        """_get_json_or_warn's docstring promises a non-JSON body warns to
        stderr — a malformed comics-server response must be visible, not a
        silent cache-miss."""
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        # Real requests raises requests.exceptions.JSONDecodeError (a subclass
        # of both ValueError and RequestException), not a bare ValueError.
        mock_resp.json.side_effect = fmv_runner.requests.exceptions.JSONDecodeError(
            "Expecting value", "", 0)
        with patch("fmv_runner.requests.get", return_value=mock_resp):
            row = fmv_runner._db_lookup(server_url, locg_id=1, grade=9.0,
                                        max_age_days=7)
        assert row is None
        err = capsys.readouterr().err
        assert "Warning" in err and "invalid JSON" in err


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

    def test_interpolated_book_upserts_priced_row_with_flag_cleared(self, server_url):
        # BUI-306: a too_wide pool that interpolates must POST a real fmv_high
        # with fmv_flag_reason=None — a non-null flag makes the server wipe the
        # price as needs_manual. The interpolation provenance rides fmv_notes.
        # Both brackets carry ≥2 comps (BUI-318): 5.0 median $50, 9.0 median
        # $310 → target 7.0 interpolates to $180.
        comps = [_make_comp(40, 5.0), _make_comp(60, 5.0),
                 _make_comp(300, 9.0), _make_comp(320, 9.0)]
        result = {
            "input": {"title": "X-Men", "issue": "96", "year": 1975, "grade": 7.0},
            "comps": comps,
        }
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"comic_id": 9, "fmv_id": 3, "id": 9}
        with patch("fmv_runner.requests.post", return_value=mock_resp) as post_mock:
            out = fmv_runner._compute_and_upsert_one(
                result, {"title": "X-Men", "issue": "96", "grade": 7.0},
                server_url=server_url)

        body = post_mock.call_args.kwargs["json"]
        assert body["fmv_flag_reason"] is None       # cleared → server keeps price
        assert body["fmv_high"] == 180 and body["fmv_low"] == 180
        assert body["fmv_confidence"] == "low"       # §7: confidence reduced
        assert "interpolated=grade 5→9" in body["fmv_notes"]
        assert out["fmv"]["interpolated"] is True

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

    def test_build_notes_carries_ungraded_anchor(self):
        # BUI-522: the ungraded-market anchor (median + raw-copy count off the
        # dropped grade-less comps) is surfaced as a fmv_notes token.
        fmv = {"window": 0.5, "cv_pct": "20%", "confidence": "HIGH",
               "flag_reason": None, "bid_factor": 0.80,
               "ungraded_anchor": {"median": 50.0, "n": 3}}
        notes = fmv_runner._build_notes(fmv)
        assert "ungraded_anchor=$50 (n=3 raw)" in notes

    def test_build_notes_omits_ungraded_anchor_when_absent(self):
        # A fetch with no grade-less comp (or a cached row that can't
        # reconstruct it) carries no anchor → no token, no crash.
        fmv = {"window": 0.5, "cv_pct": "20%", "confidence": "HIGH",
               "flag_reason": None, "bid_factor": 0.80, "ungraded_anchor": None}
        assert "ungraded_anchor" not in fmv_runner._build_notes(fmv)

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

    # ─── BUI-306: interpolation + monotonicity presentation ──────────────────

    def test_build_notes_states_interpolation_explicitly(self):
        # §7: notes must state the price was interpolated (naming the buckets)
        # and that confidence is reduced — so an interpolated value is never
        # read as a direct comp.
        fmv = {"window": 2.0, "cv_pct": "n/a", "confidence": "LOW",
               "flag_reason": None, "grade_span": 4.0, "bid_factor": 0.80,
               "grade_confidence": None, "interpolated": True,
               "interpolation": {"grade_below": 5.0, "grade_above": 9.0,
                                 "median_below": 50.0, "median_above": 310.0,
                                 "target_price": 180.0},
               "suspect_buckets": []}
        notes = fmv_runner._build_notes(fmv)
        assert "interpolated=grade 5→9" in notes
        assert "confidence reduced" in notes
        assert "manual_review" not in notes  # cleared once priced

    def test_build_notes_flags_suspect_grade_curve(self):
        # §5: a monotonicity violation is surfaced, not silently blended.
        fmv = {"window": 1.0, "cv_pct": "20%", "confidence": "MEDIUM",
               "flag_reason": None, "grade_span": 1.5, "bid_factor": 0.80,
               "interpolated": False, "interpolation": None,
               "suspect_buckets": [(7.0, 8.5)]}
        notes = fmv_runner._build_notes(fmv)
        assert "suspect_grade_curve=7>8.5" in notes

    def test_print_table_marks_interpolated_value(self, capsys):
        rows = [
            {"input": {"title": "Interp", "issue": "1", "grade": 7.0},
             "fmv": {"flag_reason": None, "interpolated": True,
                     "fmv_low": 180, "fmv_high": 180, "median": 180,
                     "max_bid": 140, "n": 3, "cv_pct": "n/a",
                     "confidence": "LOW"}, "source": "fresh"},
        ]
        fmv_runner._print_table(rows)
        out = capsys.readouterr().out
        assert "interp" in out           # marked, not a bare range
        assert "$180–$180" not in out    # never rendered as a real comp range

    def test_cached_interpolated_row_keeps_interp_marker(self):
        # BUI-306: an interpolated book persists a real number (flag cleared) and
        # is cache-reusable. On reuse it must still report interpolated=True from
        # the persisted notes, so it renders "$X interp" not "$X–$X".
        row = {"fmv_low": 180, "fmv_high": 180, "fmv_comps": 3,
               "fmv_confidence": "low",
               "fmv_notes": "window=±2.0 | interpolated=grade 5→9 "
                            "(median $50→$310); confidence reduced"}
        out = fmv_runner._fmv_from_db_row(row)
        assert out["interpolated"] is True

    def test_cached_priced_row_not_marked_interpolated(self):
        row = {"fmv_low": 100, "fmv_high": 150, "fmv_comps": 8,
               "fmv_confidence": "high", "fmv_notes": "window=±0.5 | cv=20%"}
        out = fmv_runner._fmv_from_db_row(row)
        assert out["interpolated"] is False

    def test_cached_interpolated_row_applies_haircut(self):
        # BUI-318: a cached interpolated row persists a plain "low" confidence, so
        # bid_factor("LOW", None) would return the full 0.80× and silently undo
        # the interpolated-LOW haircut on reuse. The reuse path must re-apply the
        # cap so a cached interpolated book bids at 0.60×, matching a fresh
        # recompute (no photo grade_confidence supplied).
        row = {"fmv_low": 180, "fmv_high": 180, "fmv_comps": 4,
               "fmv_confidence": "low",
               "fmv_notes": "window=±2.0 | interpolated=grade 5→9 "
                            "(median $50→$310); confidence reduced"}
        out = fmv_runner._fmv_from_db_row(row)
        assert out["interpolated"] is True
        assert out["bid_factor"] == fmv_math.INTERPOLATED_BID_FACTOR
        assert out["max_bid"] == fmv_math.clean_round(
            180 * fmv_math.INTERPOLATED_BID_FACTOR)

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

    def test_interpolated_book_marked_in_json_output(self, tmp_path, server_url):
        # BUI-306 acceptance: end-to-end, an interpolated book's JSON output must
        # let a downstream consumer tell it from a real direct comp.
        batch = [{"item_id": "1", "title": "X-Men", "issue": "96", "year": 1975,
                  "grade": 7.0}]
        batch_path = tmp_path / "batch.json"
        batch_path.write_text(json.dumps(batch))
        out_path = tmp_path / "out.json"

        fake_result = [{
            "input": {"_req_id": 0, "title": "X-Men", "issue": "96",
                      "year": 1975, "grade": 7.0, "item_id": "1"},
            "comps": [_make_comp(40, 5.0), _make_comp(60, 5.0),
                      _make_comp(300, 9.0), _make_comp(320, 9.0)],
            "queries_used": [{"tier": "base", "cached": False}],
        }]
        with patch("fmv_runner._fetch_comps", return_value=fake_result), \
             patch("fmv_runner._upsert_fmv", return_value={"id": 9}):
            fmv_runner.run(batch_path=str(batch_path), out_path=str(out_path),
                           max_age_days=7, force=False, quiet=True,
                           server_url=server_url)

        fmv = json.loads(out_path.read_text())[0]["fmv"]
        assert fmv["interpolated"] is True
        assert fmv["interpolation"]["target_price"] == 180.0
        assert fmv["fmv_high"] == 180 and fmv["flag_reason"] is None

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

    def test_fetch_comps_forwards_publisher_to_subprocess(self, tmp_path,
                                                          monkeypatch):
        """BUI-315: the book's publisher must reach ebay-sold-comps in the batch
        payload — that's what lets build_query activate the Marvel qualifier.
        Dropping it here silently disables the whole feature."""
        captured = {}

        def fake_run(cmd, capture_output, text, timeout=None):
            in_path = cmd[cmd.index("--batch") + 1]
            out_path = cmd[cmd.index("--out") + 1]
            with open(in_path) as fh:
                captured["payload"] = json.load(fh)
            with open(out_path, "w") as fh:
                fh.write("[]")
            return type("R", (), {"returncode": 0, "stderr": ""})()

        monkeypatch.setattr(fmv_runner.shutil, "which", lambda _b: "/usr/bin/ebay")
        monkeypatch.setattr(fmv_runner.subprocess, "run", fake_run)

        books = [{"_idx": 0, "title": "Amazing Spider-Man", "issue": "300",
                  "publisher": "Marvel"}]
        fmv_runner._fetch_comps(books, force=False)

        assert captured["payload"][0]["publisher"] == "Marvel"


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


# ─── BUI-346: title normalization at the buy→FMV handoff ─────────────────────

class TestTitleNormalizationHelpers:
    def test_strip_leading_article(self):
        assert fmv_runner._strip_leading_article("The Amazing Spider-Man") == "Amazing Spider-Man"
        assert fmv_runner._strip_leading_article("A Man Called X") == "Man Called X"
        assert fmv_runner._strip_leading_article("An X-Men Story") == "X-Men Story"
        assert fmv_runner._strip_leading_article("Amazing Spider-Man") == "Amazing Spider-Man"

    def test_strip_embedded_issue(self):
        assert fmv_runner._strip_embedded_issue("Amazing Spider-Man #50", "50") == "Amazing Spider-Man"
        assert fmv_runner._strip_embedded_issue("Amazing Spider-Man 50", "50") == "Amazing Spider-Man"
        # A different number (not the separate issue field) survives.
        assert fmv_runner._strip_embedded_issue("Spider-Man 2099", "50") == "Spider-Man 2099"
        # (?<!\d) guard: issue="99" must not chew into "2099".
        assert fmv_runner._strip_embedded_issue("X-Men 2099", "99") == "X-Men 2099"

    def test_normalize_book_title_acceptance(self):
        """BUI-346 acceptance criterion: a working-list row with
        title="The Amazing Spider-Man #50", issue="50" must normalize to the
        same title as a row already clean: title="Amazing Spider-Man",
        issue="50" — the real ASM #50 incident's doubled-phrase bug."""
        doubled = {"title": "The Amazing Spider-Man #50", "issue": "50"}
        clean = {"title": "Amazing Spider-Man", "issue": "50"}
        fmv_runner._normalize_book_title(doubled)
        fmv_runner._normalize_book_title(clean)
        assert doubled["title"] == clean["title"] == "Amazing Spider-Man"

    def test_normalize_book_title_noop_without_title_or_issue(self):
        no_title = {"issue": "50"}
        fmv_runner._normalize_book_title(no_title)
        assert no_title == {"issue": "50"}

        no_issue = {"title": "The Amazing Spider-Man #50"}
        fmv_runner._normalize_book_title(no_issue)
        assert no_issue["title"] == "The Amazing Spider-Man #50"  # untouched


class TestRunNormalizesTitlesAtHandoff:
    def test_run_normalizes_titles_before_fetch_comps(self, tmp_path, server_url):
        """The batch read from disk (the buy→FMV handoff) must be normalized
        BEFORE it reaches _fetch_comps' subprocess call to ebay-sold-comps —
        not left for build_query's defense-in-depth alone to catch."""
        batch = [
            {"item_id": "1", "title": "The Amazing Spider-Man #50",
             "issue": "50", "year": 1967, "grade": 4.5},
        ]
        batch_path = tmp_path / "batch.json"
        batch_path.write_text(json.dumps(batch))
        out_path = tmp_path / "out.json"

        fake_result = [{
            "input": {"_req_id": 0, "title": "Amazing Spider-Man", "issue": "50",
                      "year": 1967, "grade": 4.5, "item_id": "1"},
            "comps": [], "queries_used": [{"tier": "base", "nkw": 0}],
        }]
        with patch("fmv_runner._fetch_comps", return_value=fake_result) as fetch_mock, \
             patch("fmv_runner._upsert_fmv", return_value={"id": 1}):
            fmv_runner.run(batch_path=str(batch_path), out_path=str(out_path),
                           max_age_days=7, force=False, quiet=True,
                           server_url=server_url)

        sent_books = fetch_mock.call_args[0][0]
        assert sent_books[0]["title"] == "Amazing Spider-Man"

    def test_run_normalized_title_reaches_db_upsert(self, tmp_path, server_url):
        # The DB `title` column is documented as "series name only, no issue
        # number" (fmv.md) — the normalized title must reach the upsert too,
        # not just the ebay-sold-comps subprocess call.
        batch = [
            {"item_id": "1", "title": "The Amazing Spider-Man #50",
             "issue": "50", "year": 1967, "grade": 4.5},
        ]
        batch_path = tmp_path / "batch.json"
        batch_path.write_text(json.dumps(batch))
        out_path = tmp_path / "out.json"

        fake_comps = [_make_comp(p, 4.5) for p in [400, 450, 500, 550, 600]]
        fake_result = [{
            "input": {"_req_id": 0, "title": "Amazing Spider-Man", "issue": "50",
                      "year": 1967, "grade": 4.5, "item_id": "1"},
            "comps": fake_comps,
            "queries_used": [{"tier": "base", "cached": False}],
        }]
        with patch("fmv_runner._fetch_comps", return_value=fake_result), \
             patch("fmv_runner._upsert_fmv", return_value={"id": 1}) as upsert:
            fmv_runner.run(batch_path=str(batch_path), out_path=str(out_path),
                           max_age_days=7, force=False, quiet=True,
                           server_url=server_url)

        upserted_input = upsert.call_args[0][1]
        assert upserted_input["title"] == "Amazing Spider-Man"


# ─── BUI-346: malformed/doubled query is 0-results, never fetch-err ──────────

class TestMalformedQueryIsNotFetchError:
    def test_doubled_title_zero_results_is_not_fetch_error(self):
        """The other half of BUI-346's acceptance criterion: a 0-results-on-
        all-tiers outcome caused by a malformed/empty query (e.g. the doubled
        "...#50 50" phrase before normalization) must NOT be reported as
        'fetch-err' — that implies a SerpApi quota/outage, sending an operator
        chasing the API key instead of noticing the query itself was bad. A
        malformed-but-syntactically-valid query still gets a clean 200 from
        SerpApi with zero organic_results — no 'error' key on any tier — so
        _is_fetch_error must read this as a genuine empty pool, not a fetch
        failure."""
        r = {
            "comp_count_total": 0,
            "queries_used": [
                {"tier": "base", "nkw": '"The Amazing Spider-Man #50 50" 1967',
                 "raw_results": 0, "new_comps": 0, "cached": False},
                {"tier": "broader", "nkw": '"The Amazing Spider-Man #50 50"',
                 "raw_results": 0, "new_comps": 0, "cached": False},
            ],
        }
        assert fmv_runner._is_fetch_error(r) is False


# ─── CGC-proxy rescue (BUI-348) ───────────────────────────────────────────────

def _graded_result(req_id, ladder_comps):
    """A graded ebay-sold-comps result echoing _req_id, carrying slab comps."""
    return {"input": {"_req_id": req_id}, "comps": ladder_comps, "queries_used": []}


def _slab(price, grade):
    """A slab comp as ebay-sold-comps returns it: grade + price + CGC in title."""
    return {"grade": grade, "price": price,
            "title": f"Amazing Spider-Man 50 CGC {grade}"}


_ASM50_SLABS = [
    _slab(636, 4.0), _slab(780, 5.0), _slab(880, 5.0),
    _slab(1200, 6.5), _slab(1800, 7.0), _slab(2143, 7.0),
]


class TestSlabCompsOnly:
    def test_keeps_cgc_and_cbcs_drops_raw(self):
        comps = [
            {"grade": 6.5, "price": 1200, "title": "ASM 50 CGC 6.5"},
            {"grade": 6.0, "price": 700, "title": "ASM 50 CBCS 6.0"},
            {"grade": 6.0, "price": 650, "title": "ASM 50 FN 6.0 raw"},  # raw → dropped
            {"grade": 5.5, "price": 600, "title": "ASM 50 ungraded VG/FN"},  # dropped
        ]
        out = fmv_runner._slab_comps_only(comps)
        prices = sorted(c["price"] for c in out)
        assert prices == [700, 1200]  # only the two certified slabs survive

    def test_drops_comps_missing_grade_or_price(self):
        comps = [
            {"grade": None, "price": 1200, "title": "ASM 50 CGC"},
            {"grade": 6.5, "price": None, "title": "ASM 50 CGC 6.5"},
        ]
        assert fmv_runner._slab_comps_only(comps) == []


class TestIsUnpricedRaw:
    def test_n0_no_number_is_candidate(self):
        r = {"input": {"grade": 6.5}, "fmv": {"fmv_high": None, "interpolated": False}}
        assert fmv_runner._is_unpriced_raw(r) is True

    def test_priced_book_is_not_candidate(self):
        r = {"input": {"grade": 6.5}, "fmv": {"fmv_high": 200, "interpolated": False}}
        assert fmv_runner._is_unpriced_raw(r) is False

    def test_interpolated_book_is_not_candidate(self):
        r = {"input": {"grade": 6.5}, "fmv": {"fmv_high": 200, "interpolated": True}}
        assert fmv_runner._is_unpriced_raw(r) is False

    def test_no_numeric_grade_is_not_candidate(self):
        r = {"input": {"grade": None}, "fmv": {"fmv_high": None, "interpolated": False}}
        assert fmv_runner._is_unpriced_raw(r) is False


class TestIsThinOrLowConfidencePriced:
    """BUI-529: the ADDITIONAL cross-check candidate population — a book the
    raw math DID price, but thinly (n<5) or with LOW confidence. Disjoint from
    _is_unpriced_raw's population by construction (fmv_high is None there)."""

    def test_priced_thin_n_is_candidate(self):
        r = {"fmv": {"fmv_high": 200, "n": 3, "confidence": "MEDIUM",
                     "interpolated": False}}
        assert fmv_runner._is_thin_or_low_confidence_priced(r) is True

    def test_priced_low_confidence_is_candidate(self):
        r = {"fmv": {"fmv_high": 200, "n": 8, "confidence": "LOW",
                     "interpolated": False}}
        assert fmv_runner._is_thin_or_low_confidence_priced(r) is True

    def test_priced_healthy_is_not_candidate(self):
        r = {"fmv": {"fmv_high": 200, "n": 8, "confidence": "MEDIUM-HIGH",
                     "interpolated": False}}
        assert fmv_runner._is_thin_or_low_confidence_priced(r) is False

    def test_unpriced_is_not_candidate(self):
        # That population belongs to _is_unpriced_raw / the rescue, not here.
        r = {"fmv": {"fmv_high": None, "n": 0, "confidence": "LOW",
                     "interpolated": False}}
        assert fmv_runner._is_thin_or_low_confidence_priced(r) is False

    def test_interpolated_is_not_candidate(self):
        # BUI-306 §7 is its own already-reduced-confidence tier — out of scope.
        r = {"fmv": {"fmv_high": 200, "n": 1, "confidence": "LOW",
                     "interpolated": True}}
        assert fmv_runner._is_thin_or_low_confidence_priced(r) is False


class TestCgcProxyRescue:
    def test_sparse_high_value_book_is_rescued(self, server_url):
        books = [{"item_id": "1", "title": "Amazing Spider-Man", "issue": "50",
                  "year": 1967, "grade": 6.5}]
        fresh = {0: {"input": {"title": "Amazing Spider-Man", "issue": "50",
                               "year": 1967, "grade": 6.5},
                     "fmv": {"fmv_high": None, "interpolated": False,
                             "flag_reason": None},
                     "source": "fresh"}}
        upserts = []
        with patch("fmv_runner._fetch_comps",
                   return_value=[_graded_result(0, _ASM50_SLABS)]) as fetch_mock, \
             patch("fmv_runner._upsert_fmv",
                   side_effect=lambda *a, **k: upserts.append(a[2])
                   or {"comic_id": 7, "fmv_id": 9}):
            fmv_runner._apply_cgc_proxy_rescue(
                fresh, books, server_url=server_url, force=False)
        # Graded pass ran with include_graded=True on the candidate book.
        graded_books = fetch_mock.call_args[0][0]
        assert graded_books[0]["include_graded"] is True
        assert graded_books[0]["_idx"] == 0
        # Result replaced with a proxy band, re-upserted, ids refreshed.
        assert fresh[0]["source"] == "cgc-proxy"
        assert fresh[0]["fmv"]["cgc_proxy"] is True
        assert 600 <= fresh[0]["fmv"]["fmv_low"] <= fresh[0]["fmv"]["fmv_high"] <= 680
        assert fresh[0]["fmv"]["confidence"] == "MEDIUM-LOW"
        assert fresh[0]["comic_id"] == 7 and fresh[0]["fmv_id"] == 9
        assert fresh[0]["db_row"] == {"comic_id": 7, "fmv_id": 9}
        assert len(upserts) == 1

    def test_modern_book_is_not_rescued(self, server_url):
        # The 0.50-0.55 factor is vintage-calibrated; a modern book (year >=
        # cutoff) must never reach the proxy even with a sparse raw pool.
        fresh = {0: {"input": {"grade": 9.8, "year": 2021}, "source": "fresh",
                     "fmv": {"fmv_high": None, "interpolated": False}}}
        with patch("fmv_runner._fetch_comps") as fetch_mock, \
             patch("fmv_runner._upsert_fmv") as upsert_mock:
            fmv_runner._apply_cgc_proxy_rescue(
                fresh, [{"grade": 9.8, "year": 2021}],
                server_url=server_url, force=False)
        fetch_mock.assert_not_called()
        upsert_mock.assert_not_called()
        assert fresh[0]["source"] == "fresh"

    def test_book_without_year_is_not_rescued(self, server_url):
        # Conservative: no cover year → can't confirm vintage → no proxy.
        fresh = {0: {"input": {"grade": 6.5}, "source": "fresh",
                     "fmv": {"fmv_high": None, "interpolated": False}}}
        with patch("fmv_runner._fetch_comps") as fetch_mock:
            fmv_runner._apply_cgc_proxy_rescue(
                fresh, [{"grade": 6.5}], server_url=server_url, force=False)
        fetch_mock.assert_not_called()
        assert fresh[0]["source"] == "fresh"

    def test_proxy_upsert_failure_leaves_needs_manual(self, server_url, capsys):
        # A server blip on the best-effort proxy WRITE must not promote the
        # in-memory result to a price the DB doesn't hold, nor abort the run.
        fresh = {0: {"input": {"title": "Amazing Spider-Man", "issue": "50",
                               "year": 1967, "grade": 6.5},
                     "fmv": {"fmv_high": None, "interpolated": False},
                     "source": "fresh"}}
        books = [{"title": "Amazing Spider-Man", "issue": "50",
                  "year": 1967, "grade": 6.5}]
        with patch("fmv_runner._fetch_comps",
                   return_value=[_graded_result(0, _ASM50_SLABS)]), \
             patch("fmv_runner._upsert_fmv", return_value=None):  # soft-fail
            fmv_runner._apply_cgc_proxy_rescue(
                fresh, books, server_url=server_url, force=False)
        assert fresh[0]["source"] == "fresh"          # NOT promoted
        assert fresh[0]["fmv"].get("cgc_proxy") is None
        assert "CGC-proxy upsert failed" in capsys.readouterr().err

    def test_multi_candidate_maps_by_req_id_not_position(self, server_url):
        # Two vintage sparse candidates (idx 0 and 2); the graded pass returns
        # results in REVERSE order. Each must get its OWN ladder-derived band —
        # mapping by _req_id, never by list position (BUI-174/187).
        low_ladder = [_slab(636, 4.0), _slab(780, 5.0), _slab(880, 5.0),
                      _slab(1200, 6.5), _slab(1800, 7.0), _slab(2143, 7.0)]
        high_ladder = [_slab(1500, 6.0), _slab(1600, 6.0), _slab(2000, 8.0),
                       _slab(2100, 8.0), _slab(2600, 9.0), _slab(2700, 9.0)]
        fresh = {
            0: {"input": {"grade": 6.5, "year": 1967}, "source": "fresh",
                "fmv": {"fmv_high": None, "interpolated": False}},
            1: {"input": {"grade": 9.2, "year": 1975},   # priced → not a candidate
                "fmv": {"fmv_high": 100, "fmv_low": 80, "interpolated": False},
                "source": "fresh"},
            2: {"input": {"grade": 8.0, "year": 1968}, "source": "fresh",
                "fmv": {"fmv_high": None, "interpolated": False}},
        }
        books = [{"grade": 6.5, "year": 1967}, {"grade": 9.2, "year": 1975},
                 {"grade": 8.0, "year": 1968}]
        # Results deliberately reversed relative to candidate order.
        graded = [_graded_result(2, high_ladder), _graded_result(0, low_ladder)]
        with patch("fmv_runner._fetch_comps", return_value=graded), \
             patch("fmv_runner._upsert_fmv", return_value={"comic_id": 1}):
            fmv_runner._apply_cgc_proxy_rescue(
                fresh, books, server_url=server_url, force=False)
        # idx 0 priced off the low ladder (slab 6.5=$1200 → ~$600-650).
        assert 600 <= fresh[0]["fmv"]["fmv_low"] <= 660
        # idx 2 priced off the high ladder (slab 8.0=$2050 → ~$1025-1125).
        assert fresh[2]["fmv"]["fmv_low"] >= 1000
        # idx 1 (already priced) untouched.
        assert fresh[1]["source"] == "fresh"
        assert fresh[1]["fmv"].get("cgc_proxy") is None

    def test_priced_book_is_never_touched(self, server_url):
        # Regression invariant: a book the raw math already priced must not
        # trigger any graded fetch or upsert — proxy tier is strictly additive.
        priced_fmv = {"fmv_high": 150, "fmv_low": 100, "interpolated": False,
                      "confidence": "MEDIUM"}
        fresh = {0: {"input": {"grade": 9.2}, "fmv": dict(priced_fmv),
                     "source": "fresh"}}
        with patch("fmv_runner._fetch_comps") as fetch_mock, \
             patch("fmv_runner._upsert_fmv") as upsert_mock:
            fmv_runner._apply_cgc_proxy_rescue(
                fresh, [{"grade": 9.2}], server_url=server_url, force=False)
        fetch_mock.assert_not_called()
        upsert_mock.assert_not_called()
        assert fresh[0]["fmv"] == priced_fmv  # unchanged
        assert fresh[0]["source"] == "fresh"

    def test_soft_fetch_failure_leaves_needs_manual(self, server_url, capsys):
        fresh = {0: {"input": {"grade": 6.5, "year": 1967}, "source": "fresh",
                     "fmv": {"fmv_high": None, "interpolated": False}}}
        with patch("fmv_runner._fetch_comps", return_value=None), \
             patch("fmv_runner._upsert_fmv") as upsert_mock:
            fmv_runner._apply_cgc_proxy_rescue(
                fresh, [{"grade": 6.5, "year": 1967}],
                server_url=server_url, force=False)
        upsert_mock.assert_not_called()
        assert fresh[0]["source"] == "fresh"          # not rescued
        assert fresh[0]["fmv"]["fmv_high"] is None
        assert "CGC-proxy graded fetch failed" in capsys.readouterr().err

    def test_thin_ladder_leaves_needs_manual(self, server_url):
        fresh = {0: {"input": {"grade": 6.5, "year": 1967}, "source": "fresh",
                     "fmv": {"fmv_high": None, "interpolated": False}}}
        thin = [_slab(1200, 6.5)]  # 1 slab comp < MIN_LADDER_COMPS
        with patch("fmv_runner._fetch_comps",
                   return_value=[_graded_result(0, thin)]), \
             patch("fmv_runner._upsert_fmv") as upsert_mock:
            fmv_runner._apply_cgc_proxy_rescue(
                fresh, [{"grade": 6.5, "year": 1967}],
                server_url=server_url, force=False)
        upsert_mock.assert_not_called()
        assert fresh[0]["source"] == "fresh"
        assert fresh[0]["fmv"].get("cgc_proxy") is None  # never overwritten


# ─── Always-on vintage cross-check (BUI-529) ──────────────────────────────────

class TestCgcCrossCheckApply:
    def test_flags_divergence_using_already_fetched_slab_comps(self, server_url):
        # BUI-524's inclusive tier already supplied enough slab comps — zero
        # extra fetch needed (the whole point of the two tickets feeding
        # each other).
        fresh = {0: {"input": {"title": "Amazing Spider-Man", "issue": "50",
                               "year": 1967, "grade": 6.5},
                     "fmv": {"fmv_high": 200, "fmv_low": 150, "median": 100.0,
                             "n": 3, "confidence": "MEDIUM-LOW",
                             "interpolated": False},
                     "source": "fresh",
                     "slab_comps": _ASM50_SLABS}}
        books = [{"title": "Amazing Spider-Man", "issue": "50",
                  "year": 1967, "grade": 6.5}]
        with patch("fmv_runner._fetch_comps") as fetch_mock, \
             patch("fmv_runner._upsert_fmv",
                   return_value={"comic_id": 1, "fmv_id": 2}) as upsert_mock:
            fmv_runner._apply_cgc_cross_check(
                fresh, books, server_url=server_url, force=False)
        fetch_mock.assert_not_called()  # no dedicated second fetch needed
        check = fresh[0]["fmv"]["cgc_cross_check"]
        assert check is not None
        assert check["diverges"] is True  # raw median 100 vs slab-implied 625
        # A flag, never a re-price — the priced number is untouched.
        assert fresh[0]["fmv"]["fmv_high"] == 200
        assert fresh[0]["fmv"]["fmv_low"] == 150
        upsert_mock.assert_called_once()

    def test_falls_back_to_dedicated_fetch_when_no_slab_comps(self, server_url):
        fresh = {0: {"input": {"title": "Amazing Spider-Man", "issue": "50",
                               "year": 1967, "grade": 6.5},
                     "fmv": {"fmv_high": 200, "fmv_low": 150, "median": 100.0,
                             "n": 3, "confidence": "MEDIUM-LOW",
                             "interpolated": False},
                     "source": "fresh",
                     "slab_comps": []}}
        books = [{"title": "Amazing Spider-Man", "issue": "50",
                  "year": 1967, "grade": 6.5}]
        with patch("fmv_runner._fetch_comps",
                   return_value=[_graded_result(0, _ASM50_SLABS)]) as fetch_mock, \
             patch("fmv_runner._upsert_fmv",
                   return_value={"comic_id": 1, "fmv_id": 2}):
            fmv_runner._apply_cgc_cross_check(
                fresh, books, server_url=server_url, force=False)
        graded_books = fetch_mock.call_args[0][0]
        assert graded_books[0]["include_graded"] is True
        assert graded_books[0]["_idx"] == 0
        assert fresh[0]["fmv"]["cgc_cross_check"] is not None

    def test_skips_book_already_rescued_by_proxy(self, server_url):
        # n=3 (<5) would otherwise make this a candidate — the source guard
        # must be what excludes it, not the thinness predicate.
        fresh = {0: {"input": {"grade": 6.5, "year": 1967}, "source": "cgc-proxy",
                     "fmv": {"fmv_high": 650, "n": 3, "confidence": "MEDIUM-LOW",
                             "interpolated": False}}}
        with patch("fmv_runner._fetch_comps") as fetch_mock:
            fmv_runner._apply_cgc_cross_check(
                fresh, [{"grade": 6.5, "year": 1967}],
                server_url=server_url, force=False)
        fetch_mock.assert_not_called()
        assert fresh[0]["fmv"].get("cgc_cross_check") is None

    def test_healthy_priced_book_not_touched(self, server_url):
        fresh = {0: {"input": {"grade": 9.2, "year": 1975}, "source": "fresh",
                     "fmv": {"fmv_high": 200, "n": 8, "confidence": "MEDIUM-HIGH",
                             "interpolated": False}}}
        with patch("fmv_runner._fetch_comps") as fetch_mock, \
             patch("fmv_runner._upsert_fmv") as upsert_mock:
            fmv_runner._apply_cgc_cross_check(
                fresh, [{"grade": 9.2, "year": 1975}],
                server_url=server_url, force=False)
        fetch_mock.assert_not_called()
        upsert_mock.assert_not_called()

    def test_modern_thin_book_not_touched(self, server_url):
        # The 0.50-0.55 factor is vintage-calibrated — a modern book must
        # never reach the cross-check even with a thin/LOW-confidence price.
        fresh = {0: {"input": {"grade": 9.2, "year": 2015}, "source": "fresh",
                     "fmv": {"fmv_high": 200, "n": 2, "confidence": "LOW",
                             "interpolated": False}}}
        with patch("fmv_runner._fetch_comps") as fetch_mock:
            fmv_runner._apply_cgc_cross_check(
                fresh, [{"grade": 9.2, "year": 2015}],
                server_url=server_url, force=False)
        fetch_mock.assert_not_called()

    def test_soft_fetch_failure_leaves_unflagged(self, server_url, capsys):
        fresh = {0: {"input": {"grade": 6.5, "year": 1967}, "source": "fresh",
                     "fmv": {"fmv_high": 200, "median": 100.0, "n": 2,
                             "confidence": "LOW", "interpolated": False},
                     "slab_comps": []}}
        with patch("fmv_runner._fetch_comps", return_value=None), \
             patch("fmv_runner._upsert_fmv") as upsert_mock:
            fmv_runner._apply_cgc_cross_check(
                fresh, [{"grade": 6.5, "year": 1967}],
                server_url=server_url, force=False)
        upsert_mock.assert_not_called()
        assert fresh[0]["fmv"].get("cgc_cross_check") is None
        assert "CGC cross-check graded fetch failed" in capsys.readouterr().err

    def test_upsert_failure_keeps_flag_in_memory_only(self, server_url, capsys):
        fresh = {0: {"input": {"title": "Amazing Spider-Man", "issue": "50",
                               "year": 1967, "grade": 6.5},
                     "fmv": {"fmv_high": 200, "fmv_low": 150, "median": 100.0,
                             "n": 3, "confidence": "MEDIUM-LOW",
                             "interpolated": False},
                     "source": "fresh",
                     "slab_comps": _ASM50_SLABS,
                     "db_row": {"comic_id": 1}}}
        books = [{"title": "Amazing Spider-Man", "issue": "50",
                  "year": 1967, "grade": 6.5}]
        with patch("fmv_runner._fetch_comps") as fetch_mock, \
             patch("fmv_runner._upsert_fmv", return_value=None):
            fmv_runner._apply_cgc_cross_check(
                fresh, books, server_url=server_url, force=False)
        fetch_mock.assert_not_called()
        # The flag IS set in memory even though the best-effort persistence
        # write failed — a write blip must not silently discard the finding.
        assert fresh[0]["fmv"]["cgc_cross_check"] is not None
        assert fresh[0]["db_row"] == {"comic_id": 1}  # unchanged, not clobbered
        assert "CGC cross-check notes update failed" in capsys.readouterr().err

    def test_thin_ladder_produces_no_flag(self, server_url):
        fresh = {0: {"input": {"grade": 6.5, "year": 1967}, "source": "fresh",
                     "fmv": {"fmv_high": 200, "median": 100.0, "n": 2,
                             "confidence": "LOW", "interpolated": False},
                     "slab_comps": []}}
        with patch("fmv_runner._fetch_comps",
                   return_value=[_graded_result(0, [_slab(1200, 6.5)])]) as fetch_mock, \
             patch("fmv_runner._upsert_fmv") as upsert_mock:
            fmv_runner._apply_cgc_cross_check(
                fresh, [{"grade": 6.5, "year": 1967}],
                server_url=server_url, force=False)
        fetch_mock.assert_called_once()
        upsert_mock.assert_not_called()
        assert fresh[0]["fmv"].get("cgc_cross_check") is None

    def test_rescue_pricing_for_unpriced_books_is_byte_identical(self, server_url):
        """Hard invariant (BUI-529 spec): promoting the CGC-proxy heuristic to
        an always-on cross-check must NOT alter the unpriced-book rescue's own
        pricing in any way. `_apply_cgc_proxy_rescue` is untouched by this
        ticket — this test locks that in by running the exact rescue fixture
        from TestCgcProxyRescue.test_sparse_high_value_book_is_rescued and
        pinning the identical output, so a future refactor that merges the two
        functions can't silently drift the unpriced path."""
        books = [{"item_id": "1", "title": "Amazing Spider-Man", "issue": "50",
                  "year": 1967, "grade": 6.5}]
        fresh = {0: {"input": {"title": "Amazing Spider-Man", "issue": "50",
                               "year": 1967, "grade": 6.5},
                     "fmv": {"fmv_high": None, "interpolated": False,
                             "flag_reason": None},
                     "source": "fresh"}}
        with patch("fmv_runner._fetch_comps",
                   return_value=[_graded_result(0, _ASM50_SLABS)]), \
             patch("fmv_runner._upsert_fmv",
                   side_effect=lambda *a, **k: {"comic_id": 7, "fmv_id": 9}):
            fmv_runner._apply_cgc_proxy_rescue(
                fresh, books, server_url=server_url, force=False)
        assert fresh[0]["source"] == "cgc-proxy"
        assert fresh[0]["fmv"]["cgc_proxy"] is True
        assert 600 <= fresh[0]["fmv"]["fmv_low"] <= fresh[0]["fmv"]["fmv_high"] <= 680
        assert fresh[0]["fmv"]["confidence"] == "MEDIUM-LOW"
        # No $400-floor bypass leaked into the rescue path: cgc_proxy_fmv is
        # untouched, so a below-floor ladder still refuses to price (unlike
        # the cross-check, which explicitly drops that floor).
        assert fmv_math.cgc_proxy_fmv(
            [{"grade": 6.5, "price": 300, "title": "x CGC 6.5"},
             {"grade": 6.5, "price": 310, "title": "x CGC 6.5"},
             {"grade": 6.5, "price": 305, "title": "x CGC 6.5"}],
            target_grade=6.5,
        ) is None


class TestCgcProxyNotesAndTable:
    def test_notes_carry_cgc_proxy_token(self):
        proxy = fmv_math.cgc_proxy_fmv(_ASM50_SLABS, target_grade=6.5)
        proxy["first_party_count"] = 0
        notes = fmv_runner._build_notes(proxy)
        assert "CGC proxy" in notes
        assert "bid_haircut" in notes and "cgc_proxy" in notes

    def test_notes_annotate_n_as_graded_ladder_not_raw_depth(self):
        """BUI-350 (issue 2): `fmv_comps`/`n` for a proxy row is the GRADED
        ladder's comp count (here len(_ASM50_SLABS) == 6), not raw-market
        liquidity. A machine consumer reading `fmv_notes` in isolation (e.g.
        the calibration report) must be able to tell the two apart — assert
        the note explicitly names `n=<ladder count>` alongside the caveat,
        not just the bare "CGC proxy" token."""
        proxy = fmv_math.cgc_proxy_fmv(_ASM50_SLABS, target_grade=6.5)
        proxy["first_party_count"] = 0
        assert proxy["n"] == len(_ASM50_SLABS) == 6
        notes = fmv_runner._build_notes(proxy)
        assert "n=6 is graded-ladder comps, not raw-market depth" in notes

    def test_notes_carry_envelope_clamped_token_when_clamp_fires(self):
        # BUI-369: a lone (n=1) off-trend-high 6.5 slab ($1900) bracketed by
        # trustworthy 5.0/7.0 neighbors triggers the BUI-349 envelope clamp
        # (same fixture shape as fmv_math's
        # test_lone_offtrend_exact_slab_clamped_end_to_end). The notes must
        # explicitly flag the clamp so the slab_price vs. ladder[target]
        # mismatch reads as intentional, not a bug.
        offtrend = [_slab(800, 5.0), _slab(860, 5.0),
                    _slab(1900, 6.5),
                    _slab(1800, 7.0), _slab(2143, 7.0)]
        proxy = fmv_math.cgc_proxy_fmv(offtrend, target_grade=6.5)
        assert proxy["cgc_ladder"]["envelope_clamped"] is True
        proxy["first_party_count"] = 0
        notes = fmv_runner._build_notes(proxy)
        assert "envelope_clamped=" in notes
        assert "raw exact $1900" in notes
        # `:g` formatting matches the existing "CGC proxy: slab …" token's
        # precision (6 significant digits), same as production code.
        assert "clamped $1686.12" in notes

    def test_notes_omit_envelope_clamped_token_when_clamp_does_not_fire(self):
        # ASM #50 shape: the lone 6.5 slab sits BELOW its envelope, so the
        # clamp never fires. The notes must be unchanged from the pre-BUI-369
        # shape — no envelope_clamped token at all.
        proxy = fmv_math.cgc_proxy_fmv(_ASM50_SLABS, target_grade=6.5)
        assert proxy["cgc_ladder"]["envelope_clamped"] is False
        proxy["first_party_count"] = 0
        notes = fmv_runner._build_notes(proxy)
        assert "envelope_clamped" not in notes
        assert "CGC proxy" in notes  # unaffected: existing token still present

    def test_cached_proxy_row_recovers_marker_and_caps_bid(self):
        # A persisted proxy row: fmv_confidence collapses to "low", notes carry
        # the "CGC proxy" token. On reuse the factor must be re-capped at the
        # proxy rung (not the full 0.80×) and the marker recovered.
        row = {"fmv_low": 600, "fmv_high": 650, "fmv_comps": 6,
               "fmv_confidence": "low",
               "fmv_notes": "window=±0.5 | CGC proxy: slab 6.5=$1200 × 0.5-0.55 raw"}
        out = fmv_runner._fmv_from_db_row(row)
        assert out["cgc_proxy"] is True
        assert out["bid_factor"] <= fmv_math.CGC_PROXY_BID_FACTOR
        assert out["max_bid"] == fmv_math.clean_round(650 * out["bid_factor"])

    def test_cached_non_proxy_row_unaffected(self):
        row = {"fmv_low": 100, "fmv_high": 150, "fmv_comps": 8,
               "fmv_confidence": "high", "fmv_notes": "window=±0.5 | cv=20%"}
        out = fmv_runner._fmv_from_db_row(row)
        assert out["cgc_proxy"] is False

    def test_db_row_shape_parity_with_compute_fmv(self):
        # The cache-reuse projection must carry every key compute_fmv emits, so
        # downstream readers can iterate either dict uniformly (guards the
        # effective_n / cgc_proxy drift the reviewers flagged).
        computed = fmv_math.compute_fmv([{"price": 100, "grade": 9.2}],
                                        target_grade=9.2)
        row = {"fmv_low": 100, "fmv_high": 150, "fmv_comps": 8,
               "fmv_confidence": "high", "fmv_notes": "window=±0.5 | cv=20%"}
        projected = fmv_runner._fmv_from_db_row(row)
        # BUI-522: `ungraded_anchor` is the one compute_fmv key the cache-reuse
        # projection legitimately CANNOT carry — it's the median of the dropped
        # grade-less comps, which aren't persisted, so a cached row can't
        # reconstruct it. Unlike the bid-affecting keys this parity test exists
        # to guard (effective_n / cgc_proxy), it's purely informational and read
        # via `.get`, so its absence on a cached row is correct, not drift.
        for key in computed:
            if key == "ungraded_anchor":
                continue
            assert key in projected, f"_fmv_from_db_row missing key {key!r}"


class TestCgcCrossCheckNotes:
    def test_notes_carry_diverges_token(self):
        fmv = {"cv_pct": "20%", "confidence": "MEDIUM-LOW",
               "cgc_cross_check": {"implied_raw": 625, "raw_median": 100,
                                   "divergence_pct": 5.25, "diverges": True}}
        notes = fmv_runner._build_notes(fmv)
        assert "cgc_cross_check=DIVERGES" in notes
        assert "slab_implied=$625" in notes
        assert "raw_median=$100" in notes
        assert "(525%)" in notes

    def test_notes_carry_ok_token_when_no_divergence(self):
        fmv = {"cv_pct": "20%", "confidence": "MEDIUM",
               "cgc_cross_check": {"implied_raw": 625, "raw_median": 600,
                                   "divergence_pct": 0.0417, "diverges": False}}
        notes = fmv_runner._build_notes(fmv)
        assert "cgc_cross_check=ok" in notes

    def test_notes_omit_token_when_absent(self):
        fmv = {"cv_pct": "20%", "confidence": "HIGH"}
        notes = fmv_runner._build_notes(fmv)
        assert "cgc_cross_check" not in notes


# ─── --brief projection (BUI-362) ─────────────────────────────────────────────

class TestBriefProjection:
    """`_brief_row` must project the nine linkage/pricing fields under exactly
    the names /comic:buy's Step 3 documents — item_id, comic_id, fmv_id,
    max_bid, flag_reason, confidence, fmv_low, fmv_high, fmv_notes (BUI-505)
    — across all three row sources."""

    BRIEF_KEYS = {"item_id", "comic_id", "fmv_id", "max_bid",
                  "flag_reason", "confidence", "fmv_low", "fmv_high",
                  "fmv_notes"}

    def test_fresh_row_projects_top_level_ids(self):
        row = {
            "input": {"item_id": "111", "title": "X", "issue": "1", "grade": 9.0},
            "fmv": {"max_bid": 80, "flag_reason": None, "confidence": "HIGH",
                    "fmv_low": 90, "fmv_high": 100, "trimmed_pool": [1, 2, 3],
                    "cv_pct": "10%", "bid_factor": 0.80},
            "comp_count_total": 5, "queries_used": [{"tier": "base"}],
            "db_row": {"id": 42, "comic_id": 42, "fmv_id": 7},
            "comic_id": 42, "fmv_id": 7, "source": "fresh",
        }
        brief = fmv_runner._brief_row(row)
        assert set(brief) == self.BRIEF_KEYS
        assert brief == {"item_id": "111", "comic_id": 42, "fmv_id": 7,
                         "max_bid": 80, "flag_reason": None,
                         "confidence": "HIGH", "fmv_low": 90, "fmv_high": 100,
                         "fmv_notes": "window=n/a | cv=10% | label=HIGH"}

    def test_fresh_row_fmv_notes_matches_upsert_notes(self):
        # BUI-505: the brief line's fmv_notes must be exactly what
        # `_upsert_fmv` sent the server for this row (same fmv dict, same pure
        # `_build_notes` call) — no drift between the two.
        fmv = {"max_bid": 48, "flag_reason": None, "confidence": "LOW",
               "fmv_low": 90, "fmv_high": 100, "cv_pct": "n/a",
               "bid_factor": 0.60, "grade_confidence": "low"}
        row = {
            "input": {"item_id": "999", "title": "X", "issue": "1"},
            "fmv": fmv, "db_row": {"id": 1, "comic_id": 1, "fmv_id": 1},
            "comic_id": 1, "fmv_id": 1, "source": "fresh",
        }
        brief = fmv_runner._brief_row(row)
        assert brief["fmv_notes"] == fmv_runner._build_notes(fmv)
        assert "bid_haircut=0.60" in brief["fmv_notes"]

    def test_cached_row_falls_back_to_db_row_ids(self):
        # A cached _stitch row has NO top-level comic_id/fmv_id — its ids live
        # on the GET /api/comics db_row as `id` / `fmv_id`.
        row = {
            "input": {"item_id": "222", "title": "X", "issue": "1"},
            "fmv": {"max_bid": 60, "flag_reason": None, "confidence": "MEDIUM",
                    "fmv_low": 60, "fmv_high": 75},
            "db_row": {"id": 5, "fmv_id": 9, "fmv_low": 60, "fmv_high": 75,
                       "fmv_notes": "window=±0.5 | cv=20% | label=MEDIUM"},
            "source": "cached",
        }
        brief = fmv_runner._brief_row(row)
        assert brief["comic_id"] == 5
        assert brief["fmv_id"] == 9
        assert brief["max_bid"] == 60
        assert brief["fmv_low"] == 60
        assert brief["fmv_high"] == 75
        # Cached path reads the persisted fmv_notes verbatim off db_row rather
        # than recomputing it (the reconstructed cached fmv dict is a lossy
        # projection missing fields like first_party_count — see
        # _fmv_from_db_row — so recomputing could drop tokens the original had).
        assert brief["fmv_notes"] == "window=±0.5 | cv=20% | label=MEDIUM"

    def test_error_row_projects_nulls_not_missing_keys(self):
        # A _stitch error row (no comps, no cache) has neither top-level ids
        # nor a db_row nor an fmv dict — every field except item_id is null,
        # but every key must still exist for a uniform downstream reader.
        row = {
            "input": {"item_id": "333", "title": "X", "issue": "1"},
            "fmv": None, "db_row": None, "source": "error",
            "error": "no comps fetched and no cache",
        }
        brief = fmv_runner._brief_row(row)
        assert set(brief) == self.BRIEF_KEYS
        assert brief["item_id"] == "333"
        assert all(brief[k] is None for k in self.BRIEF_KEYS - {"item_id"})

    def test_needs_manual_row_projects_flag_reason_with_real_comic_id(self):
        # BUI-86: a flagged book still upserts a stub (real comic_id) but has
        # no max_bid — the brief line is how the orchestrator gates on it.
        row = {
            "input": {"item_id": "444", "title": "X", "issue": "1"},
            "fmv": {"max_bid": None, "flag_reason": "one_sided",
                    "confidence": "LOW", "cv_pct": "n/a"},
            "db_row": {"id": 8, "comic_id": 8, "fmv_id": 3},
            "comic_id": 8, "fmv_id": 3, "source": "fresh",
        }
        brief = fmv_runner._brief_row(row)
        assert brief["comic_id"] == 8
        assert brief["max_bid"] is None
        assert brief["flag_reason"] == "one_sided"
        assert brief["fmv_low"] is None
        assert brief["fmv_high"] is None
        assert "manual_review=one_sided" in brief["fmv_notes"]

    def test_partial_fmv_dict_degrades_notes_to_none_instead_of_crashing(self):
        # `_build_notes` reads a couple of fmv keys directly (not via .get) —
        # a partial fmv dict (e.g. a lightweight test double, or any future
        # caller that doesn't build the full compute_fmv/cgc_proxy_fmv shape)
        # must not blow up the whole brief projection over a cosmetic field.
        row = {
            "input": {"item_id": "1"}, "fmv": {"max_bid": 10},
            "db_row": None, "comic_id": 1, "fmv_id": 2, "source": "fresh",
        }
        brief = fmv_runner._brief_row(row)
        assert brief["max_bid"] == 10
        assert brief["fmv_notes"] is None

    def test_print_brief_emits_one_json_line_per_row(self, capsys):
        rows = [
            {"input": {"item_id": "1"}, "fmv": {"max_bid": 10},
             "db_row": None, "comic_id": 1, "fmv_id": 2, "source": "fresh"},
            {"input": {"item_id": "2"}, "fmv": None, "db_row": None,
             "source": "error"},
        ]
        fmv_runner._print_brief(rows)
        lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()]
        assert len(lines) == 2
        parsed = [json.loads(ln) for ln in lines]
        assert parsed[0]["item_id"] == "1" and parsed[0]["max_bid"] == 10
        assert parsed[1]["item_id"] == "2" and parsed[1]["max_bid"] is None

    def test_run_brief_prints_projection(self, tmp_path, server_url, capsys):
        # End-to-end: --quiet suppresses the table but --brief still prints
        # the JSON lines, carrying the ids the upsert returned.
        batch = [{"item_id": "1", "title": "X", "issue": "1", "year": 1990,
                  "grade": 9.0}]
        batch_path = tmp_path / "batch.json"
        batch_path.write_text(json.dumps(batch))

        fake_result = [{
            "input": {"_req_id": 0, "title": "X", "issue": "1", "year": 1990,
                      "grade": 9.0, "item_id": "1"},
            "comps": [_make_comp(p, 9.0) for p in [50, 55, 60, 65, 70]],
            "queries_used": [{"tier": "base", "cached": False}],
        }]
        upserted = {"id": 42, "comic_id": 42, "fmv_id": 7}
        with patch("fmv_runner._fetch_comps", return_value=fake_result), \
             patch("fmv_runner._upsert_fmv", return_value=upserted):
            fmv_runner.run(batch_path=str(batch_path), out_path=None,
                           max_age_days=7, force=False, quiet=True,
                           brief=True, server_url=server_url)

        lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()]
        assert len(lines) == 1
        brief = json.loads(lines[0])
        assert brief["item_id"] == "1"
        assert brief["comic_id"] == 42
        assert brief["fmv_id"] == 7
        assert brief["max_bid"] is not None
        assert brief["confidence"]

    def test_run_without_brief_prints_no_json_lines(self, tmp_path, server_url,
                                                    capsys):
        batch = [{"item_id": "1", "title": "X", "issue": "1", "year": 1990,
                  "grade": 9.0}]
        batch_path = tmp_path / "batch.json"
        batch_path.write_text(json.dumps(batch))

        fake_result = [{
            "input": {"_req_id": 0, "title": "X", "issue": "1", "year": 1990,
                      "grade": 9.0, "item_id": "1"},
            "comps": [_make_comp(p, 9.0) for p in [50, 55, 60, 65, 70]],
            "queries_used": [{"tier": "base", "cached": False}],
        }]
        with patch("fmv_runner._fetch_comps", return_value=fake_result), \
             patch("fmv_runner._upsert_fmv", return_value={"id": 42}):
            fmv_runner.run(batch_path=str(batch_path), out_path=None,
                           max_age_days=7, force=False, quiet=True,
                           server_url=server_url)

        assert capsys.readouterr().out.strip() == ""
