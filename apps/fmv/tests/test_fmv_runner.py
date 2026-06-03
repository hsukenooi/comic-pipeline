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


def _make_book(item_id, title, issue, year, grade, locg_id=None):
    book = {"item_id": item_id, "title": title, "issue": issue,
            "year": year, "grade": grade}
    if locg_id is not None:
        book["locg_id"] = locg_id
    return book


def _make_comp(price, grade, product_id="x"):
    return {"product_id": product_id, "title": f"comic {price}",
            "price": price, "grade": grade, "sold_date": "", "buying_format": ""}


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

        assert out["source"] == "fresh"
        assert out["fmv"]["n"] == 0
        assert out["fmv"]["fmv_low"] is None
        assert out["comic_id"] == 7
        assert out["fmv_id"] == 3

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
            "input": {"title": "X", "issue": "1", "year": 1990, "grade": 9.0,
                      "item_id": "1"},
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
