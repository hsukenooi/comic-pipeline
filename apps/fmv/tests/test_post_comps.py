"""Tests for BUI-658 — comic-fmv posts its comps to the comps ledger.

Covers, in order:
  - `_comp_to_ledger_item` — the CompItem field projection (explicit field
    selection, pool/provenance tagging).
  - `_post_comps` — the soft-fail POST helper: empty input, success, and
    every failure shape (mocked at the `_post_json` boundary, and separately
    end-to-end through the real `_post_json` with only `requests.post`
    faked, per the ticket's "test the failure path as hard as the happy
    path").
  - `_compute_and_upsert_one` — posts exactly once per book, immediately
    after the primary upsert; never for a book that bails before pricing
    (fetch-err, missing grade, the BUI-565 truncated-pool guard); a failed
    post never changes the priced numbers (byte-identical FMV output).
  - The CGC-proxy rescue (`_apply_cgc_proxy_rescue`) and cross-check
    (`_apply_cgc_cross_check`) re-upserts never post comps a second time.
  - `run()`'s comps-write-failure summary: silent on an all-success run,
    counted and printed whenever a post failed, silent when there was
    nothing to post, and never changes the exit code.

We mock `_post_json`/`_upsert_fmv`/`_fetch_comps`/`_fetch_first_party_outcomes`
throughout (never hit the real network), matching test_fmv_runner.py's
established convention for this module.
"""

import json
from unittest.mock import MagicMock, patch

import pytest
import requests

import fmv_runner


@pytest.fixture
def server_url():
    return "http://test-server:8080"


def _make_comp(price, grade, product_id="x", provider="serpapi", tier="base",
               query="q", from_cache=False,
               observed_at="2026-08-01T00:00:00+00:00", title=None,
               sold_date="2026-07-01", buying_format="Auction",
               link="https://ebay.com/itm/1"):
    """A comp in the exact shape `parse_comp`/`parse_comp_sold_comps` +
    BUI-657's provenance stamping produce — everything `_comp_to_ledger_item`
    reads."""
    return {
        "product_id": product_id,
        "title": title or f"comic {price}",
        "price": price,
        "grade": grade,
        "sold_date": sold_date,
        "buying_format": buying_format,
        "link": link,
        "provider": provider,
        "tier": tier,
        "query": query,
        "from_cache": from_cache,
        "observed_at": observed_at,
    }


def _slab(price, grade):
    return {"grade": grade, "price": price,
            "title": f"Amazing Spider-Man 50 CGC {grade}"}


_ASM50_SLABS = [
    _slab(636, 4.0), _slab(780, 5.0), _slab(880, 5.0),
    _slab(1200, 6.5), _slab(1800, 7.0), _slab(2143, 7.0),
]


def _graded_result(req_id, ladder_comps):
    return {"input": {"_req_id": req_id}, "comps": ladder_comps, "queries_used": []}


# ─── _comp_to_ledger_item ───────────────────────────────────────────────────

class TestCompToLedgerItem:
    def test_projects_known_fields_and_tags_pool_and_provenance(self):
        comp = _make_comp(100.0, 9.0)
        item = fmv_runner._comp_to_ledger_item(comp, pool="raw")
        assert item["pool"] == "raw"
        assert item["provenance"] == "live"
        assert item["product_id"] == comp["product_id"]
        assert item["title"] == comp["title"]
        assert item["price"] == 100.0
        assert item["grade"] == 9.0
        assert item["sold_date"] == comp["sold_date"]
        assert item["buying_format"] == comp["buying_format"]
        assert item["link"] == comp["link"]
        assert item["provider"] == comp["provider"]
        assert item["tier"] == comp["tier"]
        assert item["query"] == comp["query"]
        assert item["from_cache"] == comp["from_cache"]
        # KTD7: observed_at is the response fetch time already stamped on the
        # comp (a cache mtime on a hit) — never overwritten with "now" here.
        assert item["observed_at"] == comp["observed_at"]

    def test_slab_pool_tag(self):
        item = fmv_runner._comp_to_ledger_item(_make_comp(1200.0, 6.5), pool="slab")
        assert item["pool"] == "slab"

    def test_ignores_extraneous_fields_not_in_the_ledger_contract(self):
        """A future field added to the parsed-comp shape upstream must not
        silently ride along into the ledger POST unreviewed (the exact
        producer-side-drift failure mode BUI-588 already demonstrated on this
        cross-package HTTP contract) — explicit field selection, not
        `**comp`."""
        comp = _make_comp(100.0, 9.0)
        comp["_req_id"] = 3
        comp["some_future_internal_field"] = "should not leak"
        item = fmv_runner._comp_to_ledger_item(comp, pool="raw")
        assert "_req_id" not in item
        assert "some_future_internal_field" not in item

    def test_missing_fields_default_to_none(self):
        item = fmv_runner._comp_to_ledger_item({"product_id": "p1"}, pool="raw")
        assert item["product_id"] == "p1"
        assert item["title"] is None
        assert item["price"] is None
        assert item["provider"] is None


# ─── _post_comps ─────────────────────────────────────────────────────────────

class TestPostComps:
    def test_empty_input_posts_nothing_and_returns_none(self, server_url):
        """Nothing to post (a genuine n=0 book) is NOT a failure — and the
        endpoint 422s an empty comps list, so this must short-circuit before
        ever calling _post_json."""
        with patch("fmv_runner._post_json") as post_mock:
            result = fmv_runner._post_comps(server_url, 5, [], [])
        post_mock.assert_not_called()
        assert result is None

    def test_success_posts_raw_and_slab_tagged_correctly(self, server_url):
        raw = [_make_comp(100.0, 9.0, product_id="r1")]
        slab = [_make_comp(1200.0, 6.5, product_id="s1")]
        with patch("fmv_runner._post_json",
                   return_value={"comic_id": 5, "inserted": 2, "updated": 0,
                                 "conflicts": 0}) as post_mock:
            result = fmv_runner._post_comps(server_url, 5, raw, slab)
        assert result is True
        post_mock.assert_called_once()
        (url, body), kwargs = post_mock.call_args.args, post_mock.call_args.kwargs
        assert url == f"{server_url}/api/comics/comps"
        assert body["comic_id"] == 5
        assert len(body["comps"]) == 2
        by_id = {c["product_id"]: c for c in body["comps"]}
        assert by_id["r1"]["pool"] == "raw"
        assert by_id["s1"]["pool"] == "slab"
        assert by_id["r1"]["provenance"] == "live"
        assert by_id["s1"]["provenance"] == "live"
        # Never a hard-failing POST — a ledger write must never abort a run
        # or raise _UpsertRejected the way the primary FMV upsert can.
        assert kwargs["hard_fail"] is False

    def test_only_slab_comps_still_posts(self, server_url):
        with patch("fmv_runner._post_json", return_value={"comic_id": 5}) as post_mock:
            result = fmv_runner._post_comps(
                server_url, 5, [], [_make_comp(1200.0, 6.5)])
        assert result is True
        post_mock.assert_called_once()

    def test_failure_returns_false_never_raises(self, server_url):
        with patch("fmv_runner._post_json", return_value=None):
            result = fmv_runner._post_comps(
                server_url, 5, [_make_comp(100.0, 9.0)], [])
        assert result is False

    def test_malformed_comp_entry_returns_false_never_raises(self, server_url):
        """Adversarial case: a `None`/non-dict entry in `comps` (an upstream
        schema break, not a network failure) must degrade to `False`, not
        crash the run — `_compute_and_upsert_one`'s caller only catches
        `_UpsertRejected`, so an uncaught exception here would take down the
        ENTIRE batch, not just this book (the exact BUI-565/570
        per-book-crash-reads-as-clean class)."""
        with patch("fmv_runner._post_json") as post_mock:
            result = fmv_runner._post_comps(
                server_url, 5, [None, _make_comp(100.0, 9.0)], [])
        assert result is False
        post_mock.assert_not_called()  # blew up before ever reaching the POST

    def test_comic_id_none_is_posted_as_null_not_inferred(self, server_url):
        """KTD3: comic_id is nullable and never inferred. A book whose
        upsert yielded no comic_id must still post its comps, with
        comic_id: null in the body — not skipped, not guessed."""
        with patch("fmv_runner._post_json",
                   return_value={"comic_id": None}) as post_mock:
            result = fmv_runner._post_comps(
                server_url, None, [_make_comp(100.0, 9.0)], [])
        assert result is True
        _, body = post_mock.call_args.args
        assert body["comic_id"] is None


class TestPostJsonIntegration:
    """_post_comps through the REAL _post_json — only requests.post is
    faked — proving the soft-fail shape end-to-end for a 500, a 422 (the
    closed-vocabulary rejection shape), and a connection-refused."""

    def test_500_response_returns_false(self, server_url):
        err_resp = MagicMock(status_code=500)
        resp = MagicMock()
        resp.raise_for_status.side_effect = requests.HTTPError(
            "500 Server Error", response=err_resp)
        with patch("fmv_runner.requests.post", return_value=resp):
            result = fmv_runner._post_comps(
                server_url, 5, [_make_comp(100.0, 9.0)], [])
        assert result is False

    def test_422_unrecognized_pool_returns_false_not_raise(self, server_url):
        """A 422 (e.g. the closed-vocabulary `pool`/`provenance` rejection)
        must degrade exactly like any other failure. _post_comps always
        calls _post_json with hard_fail=False, so the BUI-639
        _UpsertRejected carve-out (which only fires under hard_fail=True,
        and only matters to the PRIMARY fmv upsert) never engages here."""
        err_resp = MagicMock(status_code=422)
        err_resp.json.return_value = {
            "detail": "pool must be one of: raw, slab"}
        resp = MagicMock()
        resp.raise_for_status.side_effect = requests.HTTPError(
            "422 Client Error", response=err_resp)
        with patch("fmv_runner.requests.post", return_value=resp):
            result = fmv_runner._post_comps(
                server_url, 5, [_make_comp(100.0, 9.0)], [])
        assert result is False

    def test_connection_refused_returns_false(self, server_url):
        with patch("fmv_runner.requests.post",
                   side_effect=requests.exceptions.ConnectionError("refused")):
            result = fmv_runner._post_comps(
                server_url, 5, [_make_comp(100.0, 9.0)], [])
        assert result is False

    def test_timeout_returns_false(self, server_url):
        with patch("fmv_runner.requests.post",
                   side_effect=requests.exceptions.Timeout("timed out")):
            result = fmv_runner._post_comps(
                server_url, 5, [_make_comp(100.0, 9.0)], [])
        assert result is False


# ─── _compute_and_upsert_one wiring ─────────────────────────────────────────

class TestComputeAndUpsertOnePostsComps:
    def test_posts_once_after_primary_upsert(self, server_url):
        comps = [_make_comp(p, 8.0) for p in [10, 11, 12, 13, 14]]
        result = {
            "input": {"title": "X", "issue": "1", "year": 1990, "grade": 8.0},
            "comps": comps,
        }
        with patch("fmv_runner._upsert_fmv", return_value={"comic_id": 5, "id": 5}), \
             patch("fmv_runner._fetch_first_party_outcomes", return_value=[]), \
             patch("fmv_runner._post_comps", return_value=True) as post_mock:
            out = fmv_runner._compute_and_upsert_one(
                result, {"title": "X", "issue": "1", "grade": 8.0},
                server_url=server_url)
        post_mock.assert_called_once_with(server_url, 5, comps, [])
        assert out["comps_posted"] is True

    def test_threads_slab_comps_when_bui524_inclusive_tier_fired(self, server_url):
        comps = [_make_comp(p, 8.0) for p in [10, 11, 12, 13, 14]]
        slabs = [_make_comp(1200.0, 6.5, product_id="s1")]
        result = {
            "input": {"title": "X", "issue": "1", "year": 1990, "grade": 8.0},
            "comps": comps, "slab_comps": slabs,
        }
        with patch("fmv_runner._upsert_fmv", return_value={"comic_id": 5, "id": 5}), \
             patch("fmv_runner._fetch_first_party_outcomes", return_value=[]), \
             patch("fmv_runner._post_comps", return_value=True) as post_mock:
            fmv_runner._compute_and_upsert_one(
                result, {"title": "X", "issue": "1", "grade": 8.0},
                server_url=server_url)
        post_mock.assert_called_once_with(server_url, 5, comps, slabs)

    def test_comic_id_none_from_upsert_still_posts_comps(self, server_url):
        """A malformed 2xx upsert response (or an old server) can yield
        comic_id=None from _extract_ids — the comps POST must still be
        attempted (KTD3), not silently skipped."""
        comps = [_make_comp(p, 8.0) for p in [10, 11, 12, 13, 14]]
        result = {
            "input": {"title": "X", "issue": "1", "year": 1990, "grade": 8.0},
            "comps": comps,
        }
        with patch("fmv_runner._upsert_fmv", return_value={"id": 1}), \
             patch("fmv_runner._fetch_first_party_outcomes", return_value=[]), \
             patch("fmv_runner._post_comps", return_value=True) as post_mock:
            out = fmv_runner._compute_and_upsert_one(
                result, {"title": "X", "issue": "1", "grade": 8.0},
                server_url=server_url)
        assert out["comic_id"] is None
        post_mock.assert_called_once_with(server_url, None, comps, [])

    def test_not_called_when_book_bails_on_missing_grade(self, server_url):
        result = {"input": {"title": "X", "issue": "1"}, "comps": []}
        with patch("fmv_runner._post_comps") as post_mock:
            out = fmv_runner._compute_and_upsert_one(
                result, {"title": "X", "issue": "1"}, server_url=server_url)
        post_mock.assert_not_called()
        assert out.get("comps_posted") is None
        assert out["source"] == "error"

    def test_not_called_on_fetch_error(self, server_url):
        result = {
            "input": {"title": "X", "issue": "1", "grade": 8.0},
            "comps": [], "error": "boom",
        }
        with patch("fmv_runner._post_comps") as post_mock:
            out = fmv_runner._compute_and_upsert_one(
                result, {"title": "X", "issue": "1", "grade": 8.0},
                server_url=server_url)
        post_mock.assert_not_called()
        assert out["source"] == "error"
        assert out.get("comps_posted") is None

    def test_not_called_on_truncated_pool_fetch_error_bui565(self, server_url):
        """The BUI-536/565 fetch-err classification: every queries_used
        entry carries an 'error' key and comp_count_total==0 — a truncated
        pool of unknown size, so no upsert AND no comps post."""
        result = {
            "input": {"title": "X", "issue": "1", "grade": 8.0},
            "comps": [],
            "queries_used": [{"tier": "base", "error": "boom"}],
        }
        with patch("fmv_runner._post_comps") as post_mock:
            out = fmv_runner._compute_and_upsert_one(
                result, {"title": "X", "issue": "1", "grade": 8.0},
                server_url=server_url)
        post_mock.assert_not_called()
        assert "fetch-err" in out["error"]

    def test_not_called_when_primary_upsert_is_permanently_rejected(self, server_url):
        """BUI-639: a 422 on the PRIMARY FMV upsert raises _UpsertRejected
        from inside _upsert_fmv, which sits physically before the new
        _post_comps call — so the comps post must never even be attempted
        for a book whose write was permanently rejected (it has no
        comic_id to post against)."""
        comps = [_make_comp(p, 8.0) for p in [10, 11, 12, 13, 14]]
        result = {
            "input": {"title": "X", "issue": "1", "year": 1990, "grade": 8.0},
            "comps": comps,
        }
        with patch("fmv_runner._upsert_fmv",
                   side_effect=fmv_runner._UpsertRejected("lot listing")), \
             patch("fmv_runner._fetch_first_party_outcomes", return_value=[]), \
             patch("fmv_runner._post_comps") as post_mock:
            with pytest.raises(fmv_runner._UpsertRejected):
                fmv_runner._compute_and_upsert_one(
                    result, {"title": "X", "issue": "1", "grade": 8.0},
                    server_url=server_url)
        post_mock.assert_not_called()

    def test_empty_comps_pool_posts_nothing_not_a_failure(self, server_url):
        """A genuine n=0 book (BUI-44's unconditional upsert) reaches
        _post_comps, which itself returns None (nothing to post) — this is
        NOT the same as a failed post."""
        result = {
            "input": {"title": "X", "issue": "1", "year": 1990, "grade": 8.0},
            "comps": [], "queries_used": [],
        }
        with patch("fmv_runner._upsert_fmv", return_value={"comic_id": 5, "id": 5}), \
             patch("fmv_runner._fetch_first_party_outcomes", return_value=[]), \
             patch("fmv_runner._post_json") as post_json_mock:
            out = fmv_runner._compute_and_upsert_one(
                result, {"title": "X", "issue": "1", "grade": 8.0},
                server_url=server_url)
        post_json_mock.assert_not_called()  # _post_comps short-circuits
        assert out["comps_posted"] is None

    def test_failed_post_does_not_change_priced_numbers(self, server_url):
        """Covers AE6: FMV output is byte-identical whether the comps POST
        succeeds or fails — the whole point of KTD5's non-fatal half. Same
        cached comps/result replayed twice, only _post_comps's outcome
        differs."""
        comps = [_make_comp(p, 8.0) for p in [10, 11, 12, 13, 14]]

        def _run(post_outcome):
            result = {
                "input": {"title": "X", "issue": "1", "year": 1990, "grade": 8.0},
                "comps": comps,
            }
            book = {"title": "X", "issue": "1", "grade": 8.0}
            with patch("fmv_runner._upsert_fmv",
                       return_value={"comic_id": 5, "id": 5, "fmv_id": 9}), \
                 patch("fmv_runner._fetch_first_party_outcomes", return_value=[]), \
                 patch("fmv_runner._post_comps", return_value=post_outcome):
                return fmv_runner._compute_and_upsert_one(
                    result, book, server_url=server_url)

        out_success = _run(True)
        out_failure = _run(False)

        assert out_success["fmv"]["fmv_low"] == out_failure["fmv"]["fmv_low"]
        assert out_success["fmv"]["fmv_high"] == out_failure["fmv"]["fmv_high"]
        assert out_success["fmv"]["n"] == out_failure["fmv"]["n"]
        assert out_success["fmv"]["confidence"] == out_failure["fmv"]["confidence"]
        assert out_success["comic_id"] == out_failure["comic_id"] == 5
        assert out_success["fmv_id"] == out_failure["fmv_id"]
        assert out_success["source"] == out_failure["source"] == "fresh"
        # Only the comps_posted flag itself differs.
        assert out_success["comps_posted"] is True
        assert out_failure["comps_posted"] is False


# ─── CGC-proxy rescue / cross-check must not repost ─────────────────────────

class TestCgcRescueAndCrossCheckDoNotRepostComps:
    """BUI-658 acceptance: the CGC-proxy rescue and cross-check re-upserts do
    NOT post comps again — a second post would bump `seen_count` misleadingly
    for comps that were only ever observed once. Reuses the exact fixture
    shapes TestCgcProxyRescue/TestCgcCrossCheckApply already establish in
    test_fmv_runner.py (duplicated locally, not imported, to avoid coupling
    this file to that one's internals)."""

    def test_proxy_rescue_never_calls_post_comps(self, server_url):
        fresh = {0: {"input": {"title": "Amazing Spider-Man", "issue": "50",
                               "year": 1967, "grade": 6.5},
                     "fmv": {"fmv_high": None, "interpolated": False,
                             "flag_reason": None},
                     "source": "fresh"}}
        books = [{"item_id": "1", "title": "Amazing Spider-Man", "issue": "50",
                  "year": 1967, "grade": 6.5}]
        with patch("fmv_runner._fetch_comps",
                   return_value=[_graded_result(0, _ASM50_SLABS)]), \
             patch("fmv_runner._upsert_fmv",
                   return_value={"comic_id": 7, "fmv_id": 9}), \
             patch("fmv_runner._post_comps") as post_mock:
            fmv_runner._apply_cgc_proxy_rescue(
                fresh, books, server_url=server_url, force=False)
        assert fresh[0]["source"] == "cgc-proxy"  # the rescue DID fire
        post_mock.assert_not_called()

    def test_cross_check_never_calls_post_comps(self, server_url):
        fresh = {0: {"input": {"title": "Amazing Spider-Man", "issue": "50",
                               "year": 1967, "grade": 6.5},
                     "fmv": {"fmv_high": 200, "fmv_low": 150, "median": 100.0,
                             "n": 3, "confidence": "MEDIUM-LOW",
                             "interpolated": False},
                     "source": "fresh",
                     "slab_comps": _ASM50_SLABS}}
        books = [{"title": "Amazing Spider-Man", "issue": "50",
                  "year": 1967, "grade": 6.5}]
        with patch("fmv_runner._fetch_comps"), \
             patch("fmv_runner._upsert_fmv",
                   return_value={"comic_id": 1, "fmv_id": 2}), \
             patch("fmv_runner._post_comps") as post_mock:
            fmv_runner._apply_cgc_cross_check(
                fresh, books, server_url=server_url, force=False)
        assert fresh[0]["fmv"]["cgc_cross_check"] is not None  # the check DID fire
        post_mock.assert_not_called()


# ─── run()'s comps-write-failure summary ────────────────────────────────────

class TestRunCompsWriteFailureSummary:
    def _batch(self):
        return [
            {"item_id": "1", "title": "A", "issue": "1", "year": 1990,
             "grade": 9.0},
            {"item_id": "2", "title": "B", "issue": "1", "year": 1990,
             "grade": 9.0},
        ]

    def _fake_results(self):
        comps = [_make_comp(p, 9.0) for p in [50, 55, 60, 65, 70]]
        return [
            {"input": {"_req_id": 0, "title": "A", "issue": "1",
                       "year": 1990, "grade": 9.0, "item_id": "1"},
             "comps": comps, "queries_used": [{"tier": "base", "cached": False}]},
            {"input": {"_req_id": 1, "title": "B", "issue": "1",
                       "year": 1990, "grade": 9.0, "item_id": "2"},
             "comps": comps, "queries_used": [{"tier": "base", "cached": False}]},
        ]

    @staticmethod
    def _upsert_side_effect(server_url, inp, fmv, hard_fail=True):
        if inp.get("title") == "A":
            return {"comic_id": 1, "fmv_id": 10}
        return {"comic_id": 2, "fmv_id": 20}

    def test_one_failure_in_a_two_book_run_is_counted_and_reported(
            self, tmp_path, server_url, capsys):
        """Covers AE3: comps endpoint fails for one book in a multi-book
        run → every book prices normally, the summary reports exactly one
        comps-write failure."""
        batch_path = tmp_path / "batch.json"
        batch_path.write_text(json.dumps(self._batch()))
        out_path = tmp_path / "out.json"

        def _post_comps_side_effect(srv, comic_id, comps, slab_comps):
            return comic_id != 1  # book A (comic_id 1) fails to post

        with patch("fmv_runner._fetch_comps", return_value=self._fake_results()), \
             patch("fmv_runner._upsert_fmv", side_effect=self._upsert_side_effect), \
             patch("fmv_runner._fetch_first_party_outcomes", return_value=[]), \
             patch("fmv_runner._post_comps", side_effect=_post_comps_side_effect):
            fmv_runner.run(batch_path=str(batch_path), out_path=str(out_path),
                           max_age_days=7, force=False, quiet=True,
                           server_url=server_url)

        cap = capsys.readouterr()
        assert "1 book(s) priced successfully" in cap.err
        assert "comps ledger POST" in cap.err
        assert "FAILED" in cap.err
        # Both books still priced and are linked — pricing is unaffected.
        out = json.loads(out_path.read_text())
        assert len(out) == 2
        assert all(row["source"] == "fresh" for row in out)
        assert {row["comic_id"] for row in out} == {1, 2}

    def test_run_completes_without_exiting_on_comps_post_failure(
            self, tmp_path, server_url):
        """A comps-write failure must never change comic-fmv's exit code —
        there is no pytest.raises wrapping this call, so an unwanted
        sys.exit would fail the test outright."""
        batch_path = tmp_path / "batch.json"
        batch_path.write_text(json.dumps(self._batch()))

        with patch("fmv_runner._fetch_comps", return_value=self._fake_results()), \
             patch("fmv_runner._upsert_fmv", side_effect=self._upsert_side_effect), \
             patch("fmv_runner._fetch_first_party_outcomes", return_value=[]), \
             patch("fmv_runner._post_comps", return_value=False):
            fmv_runner.run(batch_path=str(batch_path), out_path=None,
                           max_age_days=7, force=False, quiet=True,
                           server_url=server_url)

    def test_all_success_prints_no_comps_line(self, tmp_path, server_url, capsys):
        """No happy-path noise: every post landing means the summary stays
        silent about comps entirely."""
        batch_path = tmp_path / "batch.json"
        batch_path.write_text(json.dumps(self._batch()))

        with patch("fmv_runner._fetch_comps", return_value=self._fake_results()), \
             patch("fmv_runner._upsert_fmv", side_effect=self._upsert_side_effect), \
             patch("fmv_runner._fetch_first_party_outcomes", return_value=[]), \
             patch("fmv_runner._post_comps", return_value=True):
            fmv_runner.run(batch_path=str(batch_path), out_path=None,
                           max_age_days=7, force=False, quiet=True,
                           server_url=server_url)

        cap = capsys.readouterr()
        assert "comps ledger POST" not in cap.err

    def test_nothing_to_post_is_not_a_failure(self, tmp_path, server_url, capsys):
        """_post_comps returning None (nothing to post — e.g. every book in
        the batch genuinely had zero comps) must not trigger the failure
        line — only an explicit False (attempted and failed) does."""
        batch_path = tmp_path / "batch.json"
        batch_path.write_text(json.dumps(self._batch()))

        with patch("fmv_runner._fetch_comps", return_value=self._fake_results()), \
             patch("fmv_runner._upsert_fmv", side_effect=self._upsert_side_effect), \
             patch("fmv_runner._fetch_first_party_outcomes", return_value=[]), \
             patch("fmv_runner._post_comps", return_value=None):
            fmv_runner.run(batch_path=str(batch_path), out_path=None,
                           max_age_days=7, force=False, quiet=True,
                           server_url=server_url)

        cap = capsys.readouterr()
        assert "comps ledger POST" not in cap.err

    def test_out_json_byte_identical_regardless_of_comps_post_outcome(
            self, tmp_path, server_url):
        """Covers AE6 at the run() level: the --out JSON's pricing fields
        (fmv/comic_id/fmv_id/source) are identical whether every comps POST
        in the run succeeds or every one fails — only run()'s stderr summary
        differs."""
        batch_path = tmp_path / "batch.json"
        batch_path.write_text(json.dumps(self._batch()))

        def _run(post_outcome):
            out_path = tmp_path / f"out_{post_outcome}.json"
            with patch("fmv_runner._fetch_comps",
                       return_value=self._fake_results()), \
                 patch("fmv_runner._upsert_fmv",
                       side_effect=self._upsert_side_effect), \
                 patch("fmv_runner._fetch_first_party_outcomes", return_value=[]), \
                 patch("fmv_runner._post_comps", return_value=post_outcome):
                fmv_runner.run(batch_path=str(batch_path), out_path=str(out_path),
                               max_age_days=7, force=False, quiet=True,
                               server_url=server_url)
            return json.loads(out_path.read_text())

        out_success = _run(True)
        out_failure = _run(False)

        def _pricing_projection(rows):
            return [{"fmv": r["fmv"], "comic_id": r["comic_id"],
                     "fmv_id": r["fmv_id"], "source": r["source"]}
                    for r in rows]

        assert _pricing_projection(out_success) == _pricing_projection(out_failure)
