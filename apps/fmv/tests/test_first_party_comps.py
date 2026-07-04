"""Tests for BUI-286: injecting the user's own auction outcomes as first-party
comps into the FMV pool.

Two layers are covered here:
  - `_fetch_first_party_outcomes` — the HTTP client that pulls outcomes from
    the comics server's GET /api/comics/outcomes (mocked; no real network).
  - The merge in `_compute_and_upsert_one` — first-party comps folded into the
    pool fed to `fmv_math.compute_fmv`, alongside a plain `fmv_math` check that
    a `source`-tagged comp flows through `build_pool`/`iqr_trim` untouched.

Server-side query correctness (comic resolution, wins+losses together,
tombstone/NULL-price exclusion, is_primary lot scoping, recency) is covered by
plugins/gixen-overlay/tests/test_gixen_overlay_routes.py — this file only
exercises the apps/fmv side of the seam.
"""

import inspect
from unittest.mock import MagicMock, patch

import fmv_math
import fmv_runner


def _make_comp(price, grade, product_id="x"):
    return {"product_id": product_id, "title": f"comic {price}",
            "price": price, "grade": grade, "sold_date": "", "buying_format": ""}


def _outcome_row(price, grade, status, sold_date="2026-01-01T00:00:00+00:00"):
    return {"price": price, "grade": grade, "sold_date": sold_date, "status": status}


def server_url():
    return "http://test-server:8080"


# ─── _fetch_first_party_outcomes ──────────────────────────────────────────────

class TestFetchFirstPartyOutcomes:
    def test_no_identity_returns_empty_without_network_call(self):
        """Neither locg_id nor title+issue given → [] with no HTTP attempt at
        all (defensive: never ask the server for 'every outcome ever')."""
        with patch("fmv_runner.requests.get") as get_mock:
            out = fmv_runner._fetch_first_party_outcomes(
                server_url(), target_grade=9.0)
        get_mock.assert_not_called()
        assert out == []

    def test_happy_path_tags_source_first_party(self):
        rows = [_outcome_row(100.0, 9.0, "LOST"), _outcome_row(120.0, 9.2, "WON")]
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = rows
        with patch("fmv_runner.requests.get", return_value=mock_resp):
            out = fmv_runner._fetch_first_party_outcomes(
                server_url(), target_grade=9.0, locg_id=100)
        assert len(out) == 2
        assert all(c["source"] == "first_party" for c in out)
        assert {c["price"] for c in out} == {100.0, 120.0}

    def test_params_pass_max_grade_window_and_recency_default(self):
        """The client always scans a generous grade band (fmv_math's own
        ceiling, passed explicitly) and the documented recency default —
        build_pool's own progressive widening decides the effective window,
        not this call."""
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = []
        with patch("fmv_runner.requests.get", return_value=mock_resp) as get_mock:
            fmv_runner._fetch_first_party_outcomes(
                server_url(), target_grade=9.4, locg_id=42)
        params = get_mock.call_args.kwargs["params"]
        assert params["grade"] == 9.4
        assert params["window"] == fmv_math.MAX_GRADE_WINDOW
        assert params["days"] == fmv_runner.FIRST_PARTY_RECENCY_DAYS
        assert params["locg_id"] == 42

    def test_title_issue_year_path_used_when_no_locg_id(self):
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = []
        with patch("fmv_runner.requests.get", return_value=mock_resp) as get_mock:
            fmv_runner._fetch_first_party_outcomes(
                server_url(), target_grade=9.4, title="X-Men", issue="1",
                year=1963)
        params = get_mock.call_args.kwargs["params"]
        assert params["title"] == "X-Men"
        assert params["issue"] == "1"
        assert params["year"] == 1963
        assert "locg_id" not in params

    def test_network_error_returns_empty(self):
        """Fail-soft, same posture as `_db_lookup` — an unreachable outcomes
        endpoint must not block pricing."""
        import requests
        with patch("fmv_runner.requests.get",
                   side_effect=requests.ConnectionError("nope")):
            out = fmv_runner._fetch_first_party_outcomes(
                server_url(), target_grade=9.0, locg_id=1)
        assert out == []

    def test_rows_missing_price_or_grade_are_dropped(self):
        rows = [
            {"price": None, "grade": 9.0, "sold_date": "", "status": "LOST"},
            {"price": 50.0, "grade": None, "sold_date": "", "status": "WON"},
            {"price": 60.0, "grade": 9.0, "sold_date": "", "status": "WON"},
        ]
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = rows
        with patch("fmv_runner.requests.get", return_value=mock_resp):
            out = fmv_runner._fetch_first_party_outcomes(
                server_url(), target_grade=9.0, locg_id=1)
        assert len(out) == 1
        assert out[0]["price"] == 60.0

    def test_no_status_parameter_exists(self):
        """R2/KTD-3 structural guard: there must be no way for a caller to ask
        for wins only. Assert the function signature has no status/mode/
        wins_only parameter a future edit could wire up to narrow the query."""
        sig = inspect.signature(fmv_runner._fetch_first_party_outcomes)
        forbidden = {"status", "statuses", "wins_only", "mode"}
        assert not (forbidden & set(sig.parameters)), (
            "a status-narrowing parameter would let a caller request wins "
            "alone, reintroducing the truncated-from-above deflation spiral"
        )


# ─── Merge into _compute_and_upsert_one ───────────────────────────────────────

class TestFirstPartyMergeIntoComputeOne:
    def test_empty_first_party_prices_identically_to_baseline(self):
        """A book with no resolved auctions must price EXACTLY as today."""
        comps = [_make_comp(p, 9.0) for p in [40, 42, 44, 45, 41]]
        result = {
            "input": {"title": "X", "issue": "1", "year": 1990, "grade": 9.0},
            "comps": comps,
        }
        book = {"title": "X", "issue": "1", "grade": 9.0}
        with patch("fmv_runner._fetch_first_party_outcomes", return_value=[]), \
             patch("fmv_runner._upsert_fmv", return_value={"id": 1}):
            out = fmv_runner._compute_and_upsert_one(
                result, book, server_url=server_url())
        baseline = fmv_math.compute_fmv(comps, target_grade=9.0)
        assert out["fmv"]["fmv_low"] == baseline["fmv_low"]
        assert out["fmv"]["fmv_high"] == baseline["fmv_high"]
        assert out["fmv"]["n"] == baseline["n"]
        assert out["fmv"]["first_party_count"] == 0

    def test_happy_path_two_losses_one_win_shift_fmv_vs_baseline(self):
        """Two losses + one win in-window fold three first-party comps into
        the pool; the resulting fmv_low/high reflects their inclusion vs a
        baseline pool without them."""
        comps = [_make_comp(p, 9.0) for p in [40, 42, 44, 45, 41]]
        result = {
            "input": {"title": "X", "issue": "1", "year": 1990, "grade": 9.0},
            "comps": comps,
        }
        book = {"title": "X", "issue": "1", "grade": 9.0}
        first_party = [
            {"price": 90.0, "grade": 9.0, "sold_date": "", "source": "first_party"},
            {"price": 95.0, "grade": 9.0, "sold_date": "", "source": "first_party"},
            {"price": 100.0, "grade": 9.0, "sold_date": "", "source": "first_party"},
        ]
        with patch("fmv_runner._fetch_first_party_outcomes",
                  return_value=first_party), \
             patch("fmv_runner._upsert_fmv", return_value={"id": 1}):
            out = fmv_runner._compute_and_upsert_one(
                result, book, server_url=server_url())
        baseline = fmv_math.compute_fmv(comps, target_grade=9.0)
        assert out["fmv"]["n"] == 8  # 5 SerpApi + 3 first-party
        assert out["fmv"]["first_party_count"] == 3
        assert out["fmv"]["fmv_high"] > baseline["fmv_high"]
        # comp_count_total stays the SerpApi-only count (BUI-143 fetch-error
        # signal keys off this) — first-party rows never inflate it.
        assert out["comp_count_total"] == 5
        # notes must mention the first-party contribution (verification req).
        assert "first_party=3" in fmv_runner._build_notes(out["fmv"])

    def test_lone_first_party_outlier_is_still_iqr_trimmed(self):
        """KTD-4: first-party comps are NOT exempt from IQR trim. A lone
        first-party loss far above the SerpApi pool must be trimmable, not
        force-included."""
        comps = [_make_comp(p, 9.0) for p in [40, 41, 42, 43, 44]]
        result = {
            "input": {"title": "X", "issue": "1", "year": 1990, "grade": 9.0},
            "comps": comps,
        }
        book = {"title": "X", "issue": "1", "grade": 9.0}
        outlier = [{"price": 5000.0, "grade": 9.0, "sold_date": "",
                    "source": "first_party"}]
        with patch("fmv_runner._fetch_first_party_outcomes",
                  return_value=outlier), \
             patch("fmv_runner._upsert_fmv", return_value={"id": 1}):
            out = fmv_runner._compute_and_upsert_one(
                result, book, server_url=server_url())
        assert 5000.0 not in out["fmv"]["trimmed_pool"]
        assert out["fmv"]["n"] == 5  # outlier trimmed, only the 5 SerpApi remain

    def test_first_party_fetch_uses_the_resolved_target_grade(self):
        """A string wish-list grade coerces to numeric before the first-party
        lookup runs (mirrors the existing coercion for compute_fmv itself)."""
        comps = [_make_comp(p, 9.0) for p in [40, 42, 44]]
        result = {
            "input": {"title": "X", "issue": "1", "year": 1990, "grade": "VF/NM"},
            "comps": comps,
        }
        book = {"title": "X", "issue": "1"}
        with patch("fmv_runner._fetch_first_party_outcomes",
                  return_value=[]) as fp_mock, \
             patch("fmv_runner._upsert_fmv", return_value={"id": 1}):
            fmv_runner._compute_and_upsert_one(
                result, book, server_url=server_url())
        assert fp_mock.call_args.kwargs["target_grade"] == 9.0


# ─── fmv_math: source-tagged comps flow through untouched ─────────────────────

class TestSourceTaggedCompsFlowThroughFmvMath:
    def test_build_pool_ignores_source_key(self):
        """confirm a `source`-tagged comp flows through build_pool/iqr_trim
        untouched — they key only on price/grade."""
        comps = [
            _make_comp(40, 9.0), _make_comp(41, 9.0), _make_comp(42, 9.0),
            _make_comp(43, 9.0), _make_comp(44, 9.0),
            {"price": 45, "grade": 9.0, "sold_date": "", "source": "first_party"},
        ]
        pool, window = fmv_math.build_pool(comps, target_grade=9.0)
        assert len(pool) == 6
        assert window == fmv_math.DEFAULT_GRADE_WINDOW
        prices = [c["price"] for c in pool]
        trimmed = fmv_math.iqr_trim(prices)
        assert 45 in trimmed

    def test_grade_window_at_half_included_at_one_excluded_when_pool_full(self):
        """Grade window: a first-party comp at target±0.5 is included; one at
        ±1.0 is excluded once the SerpApi pool already meets MIN_NARROW_POOL,
        because build_pool stops widening once it has enough comps."""
        # 5 SerpApi comps already meet MIN_NARROW_POOL at the default ±0.5.
        comps = [_make_comp(p, 9.0) for p in [40, 41, 42, 43, 44]]
        comps.append({"price": 200, "grade": 9.5, "sold_date": "",
                      "source": "first_party"})   # within ±0.5
        comps.append({"price": 300, "grade": 10.0, "sold_date": "",
                      "source": "first_party"})   # ±1.0 — outside the ±0.5 stop
        pool, window = fmv_math.build_pool(comps, target_grade=9.0)
        assert window == fmv_math.DEFAULT_GRADE_WINDOW
        prices = {c["price"] for c in pool}
        assert 200 in prices
        assert 300 not in prices
