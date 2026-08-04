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


@pytest.fixture(autouse=True)
def _no_comps_post_by_default(monkeypatch):
    """BUI-658: `_compute_and_upsert_one` now also calls out to `_post_comps`
    (a real HTTP POST to /api/comics/comps) immediately after the primary
    upsert. None of the tests in this file are about that feature — it's
    covered end-to-end in test_post_comps.py — so default it to a no-op
    (`None` — "nothing to post", not a failure, matching `_post_comps`'s own
    empty-input return) here rather than editing every existing
    `_compute_and_upsert_one`/`run` call site to mock yet another network
    call it was never testing. Mirrors `_no_first_party_outcomes_by_default`
    directly above, which solved the identical problem for BUI-286."""
    monkeypatch.setattr(fmv_runner, "_post_comps", lambda *a, **k: None)


def _make_book(item_id, title, issue, year, grade, locg_id=None):
    book = {"item_id": item_id, "title": title, "issue": issue,
            "year": year, "grade": grade}
    if locg_id is not None:
        book["locg_id"] = locg_id
    return book


def _make_comp(price, grade, product_id="x"):
    return {"product_id": product_id, "title": f"comic {price}",
            "price": price, "grade": grade, "sold_date": "", "buying_format": ""}


def _stale_hand_lookup(row):
    """A `_db_lookup` side_effect: the normal freshness-gated lookup
    (max_age_days=7 etc.) misses (simulating a stale/absent row), but the
    BUI-533 any-age lookup (max_age_days=None) finds `row`."""
    def _lookup(server, *, locg_id, grade, locg_variant_id=None, max_age_days,
                strict=False):
        return row if max_age_days is None else None
    return _lookup


def _failing_provenance_lookup(fresh_row=None):
    """A `_db_lookup` side_effect for BUI-544: the age-unbounded provenance
    lookup (the `strict=True` one) FAILS, while the normal freshness-gated
    lookup behaves as usual (returns `fresh_row`, a miss by default)."""
    def _lookup(server, *, locg_id, grade, locg_variant_id=None, max_age_days,
                strict=False):
        if strict:
            raise fmv_runner._DbLookupFailed(
                f"comics-server FMV lookup failed for locg_id={locg_id} "
                f"grade={grade}")
        return fresh_row
    return _lookup


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

    def test_bui537_shaped_all_error_entries_still_fetch_error(self):
        """BUI-537 adversarial check: ebay-sold-comps now emits a fuller
        attempt trail (page/outcome on every entry, including multiple
        error entries per tier from retried attempts) — the extra fields and
        extra rows must not change the fetch-err verdict for a book where
        every attempt genuinely errored."""
        r = {"comp_count_total": 0,
             "queries_used": [
                 {"tier": "base", "nkw": "x", "page": 1,
                  "outcome": "error:ConnectionError", "error": "refused"},
                 {"tier": "base", "nkw": "x", "page": 1,
                  "outcome": "error:HTTPError", "error": "503"},
             ]}
        assert fmv_runner._is_fetch_error(r) is True

    def test_bui537_shaped_success_entry_never_misclassified_as_fetch_error(self):
        """BUI-537 adversarial check: the new 'page'/'outcome' fields added to
        EVERY entry (including successes) must never cause a successful book
        to be misread as fetch-err — a live/hit success entry has no 'error'
        key regardless of the new fields, so `all(q.get('error') ...)` must
        still be False the moment even one entry is a clean success, even
        when earlier attempts for the SAME tier errored (retry-then-succeed)."""
        r = {"comp_count_total": 3,
             "queries_used": [
                 {"tier": "base", "nkw": "x", "page": 1,
                  "outcome": "error:HTTPError", "error": "503"},
                 {"tier": "base", "nkw": "x", "raw_results": 3, "new_comps": 3,
                  "cached": False, "ebay_url": "ok", "page": 1,
                  "outcome": "live"},
             ]}
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
    def test_force_skips_normal_cache_but_still_checks_hand_priced(self, server_url):
        """BUI-533: --force always bypasses normal DB-cache reuse, but a single
        age-unbounded lookup per eligible book is still required to detect (and
        later echo) any hand-priced override it's about to overwrite — so
        _db_lookup is no longer literally uncalled under --force."""
        books = [_make_book("1", "X", "1", 1990, 9.0, locg_id=100)]
        with patch("fmv_runner._db_lookup", return_value=None) as lookup:
            (cached, needs, skipped_hand, force_notes,
             lookup_err) = fmv_runner._split_by_db_cache(
                books, server_url=server_url, max_age_days=7, force=True)
            lookup.assert_called_once_with(
                server_url, locg_id=100, grade=9.0, locg_variant_id=None,
                max_age_days=None, strict=True)
        assert cached == {}
        assert len(needs) == 1
        assert needs[0]["_idx"] == 0
        assert skipped_hand == {}
        assert force_notes == {}

    def test_book_without_locg_id_goes_to_compute(self, server_url):
        # BUI-153: the DB-FMV cache-skip requires a locg_id, so a title-derived
        # book (grade set, no locg_id) always falls through to a fresh compute —
        # which is why --max-age-days is inert in the orchestrated /comic:buy flow.
        books = [_make_book("1", "X", "1", 1990, 9.0)]
        with patch("fmv_runner._db_lookup") as lookup:
            (cached, needs, skipped_hand, force_notes,
             lookup_err) = fmv_runner._split_by_db_cache(
                books, server_url=server_url, max_age_days=7, force=False)
            lookup.assert_not_called()
        assert cached == {}
        assert len(needs) == 1
        assert skipped_hand == {}
        assert force_notes == {}

    def test_cache_hit_returns_row(self, server_url):
        books = [_make_book("1", "X", "1", 1990, 9.0, locg_id=100)]
        row = {"id": 1, "fmv_low": 50, "fmv_high": 75, "fmv_comps": 8,
               "fmv_confidence": "high", "fmv_updated_at": "2026-05-09T...",
               "title": "X", "issue": "1", "year": 1990, "grade": 9.0}
        with patch("fmv_runner._db_lookup", return_value=row) as lookup:
            (cached, needs, skipped_hand, force_notes,
             lookup_err) = fmv_runner._split_by_db_cache(
                books, server_url=server_url, max_age_days=7, force=False)
        assert cached == {0: row}
        assert needs == []
        assert skipped_hand == {}
        # A fresh cache hit short-circuits before the hand-priced check runs —
        # a fresh row is never recomputed regardless, so the extra lookup
        # would be pure waste.
        lookup.assert_called_once_with(
            server_url, locg_id=100, grade=9.0, locg_variant_id=None,
            max_age_days=7)

    def test_cache_miss_falls_through(self, server_url):
        books = [_make_book("1", "X", "1", 1990, 9.0, locg_id=100)]
        with patch("fmv_runner._db_lookup", return_value=None):
            (cached, needs, skipped_hand, force_notes,
             lookup_err) = fmv_runner._split_by_db_cache(
                books, server_url=server_url, max_age_days=7, force=False)
        assert cached == {}
        assert len(needs) == 1
        assert needs[0]["_idx"] == 0
        assert skipped_hand == {}
        assert force_notes == {}


# ─── Hand-priced row protection (BUI-533) ─────────────────────────────────────

class TestIsHandPriced:
    def test_hand_section_marker(self):
        assert fmv_runner._is_hand_priced("hand § anchored on the 4.0 sale") is True

    def test_hand_override_marker(self):
        assert fmv_runner._is_hand_priced("hand OVERRIDE: rejecting CLI pool") is True

    def test_plain_notes_not_hand_priced(self):
        assert fmv_runner._is_hand_priced("window=±0.5 | cv=20% | label=HIGH") is False

    def test_none_notes_not_hand_priced(self):
        assert fmv_runner._is_hand_priced(None) is False

    def test_marker_must_be_a_prefix_not_substring(self):
        """A note that merely MENTIONS a hand override mid-string (e.g. an
        automated note referencing why a book was NOT hand-priced) must not
        false-positive — the marker is a provenance prefix, not a keyword."""
        assert fmv_runner._is_hand_priced(
            "window=±0.5 | cv=20% | see hand OVERRIDE policy doc") is False


class TestSplitByDbCacheHandPriced:
    def test_stale_hand_priced_row_is_skipped_not_recomputed(self, server_url):
        """The exact 2026-07-24 incident this ticket exists for: a hand-priced
        row old enough that normal cache logic would recompute it must still
        be protected — the staleness must not defeat the protection."""
        books = [_make_book("1", "Batman", "251", 1972, 5.5, locg_id=100)]
        hand_row = {"id": 1, "fmv_low": 250, "fmv_high": 300, "fmv_comps": 1,
                   "fmv_confidence": "low",
                   "fmv_notes": "hand § anchored on the lone 4.0 sale",
                   "fmv_updated_at": "2020-01-01T00:00:00"}  # very stale

        def _lookup(server, *, locg_id, grade, locg_variant_id, max_age_days,
                    strict=False):
            # The normal (max_age_days=7) lookup misses (too stale); the
            # any-age lookup (max_age_days=None) finds it.
            if max_age_days is None:
                return hand_row
            return None

        with patch("fmv_runner._db_lookup", side_effect=_lookup):
            (cached, needs, skipped_hand, force_notes,
             lookup_err) = fmv_runner._split_by_db_cache(
                books, server_url=server_url, max_age_days=7, force=False)
        assert cached == {}
        assert needs == []
        assert skipped_hand == {0: hand_row}
        assert force_notes == {}

    def test_fresh_hand_priced_row_reused_via_normal_cache_path(self, server_url):
        """A hand-priced row that's still FRESH is simply a normal cache hit —
        it's never recomputed either way, so it's returned via `cached`, not
        `skipped_hand` (no need for the extra any-age lookup at all)."""
        books = [_make_book("1", "Batman", "251", 1972, 5.5, locg_id=100)]
        hand_row = {"id": 1, "fmv_low": 250, "fmv_high": 300, "fmv_comps": 1,
                   "fmv_confidence": "low",
                   "fmv_notes": "hand § anchored on the lone 4.0 sale",
                   "fmv_updated_at": "2026-07-24T00:00:00"}
        with patch("fmv_runner._db_lookup", return_value=hand_row) as lookup:
            (cached, needs, skipped_hand, force_notes,
             lookup_err) = fmv_runner._split_by_db_cache(
                books, server_url=server_url, max_age_days=7, force=False)
        assert cached == {0: hand_row}
        assert skipped_hand == {}
        lookup.assert_called_once()  # only the normal fresh lookup, no extra call

    def test_non_hand_priced_stale_row_still_recomputes(self, server_url):
        """Sanity: the new any-age lookup must not accidentally protect every
        stale row — only ones actually carrying the hand marker."""
        books = [_make_book("1", "X", "1", 1990, 9.0, locg_id=100)]
        normal_row = {"id": 1, "fmv_low": 50, "fmv_high": 75, "fmv_comps": 8,
                     "fmv_confidence": "high",
                     "fmv_notes": "window=±0.5 | cv=20% | label=HIGH",
                     "fmv_updated_at": "2020-01-01T00:00:00"}

        def _lookup(server, *, locg_id, grade, locg_variant_id, max_age_days,
                    strict=False):
            if max_age_days is None:
                return normal_row
            return None

        with patch("fmv_runner._db_lookup", side_effect=_lookup):
            (cached, needs, skipped_hand, force_notes,
             lookup_err) = fmv_runner._split_by_db_cache(
                books, server_url=server_url, max_age_days=7, force=False)
        assert cached == {}
        assert skipped_hand == {}
        assert len(needs) == 1
        assert needs[0]["_idx"] == 0

    def test_no_existing_row_at_all_recomputes_normally(self, server_url):
        books = [_make_book("1", "X", "1", 1990, 9.0, locg_id=100)]
        with patch("fmv_runner._db_lookup", return_value=None):
            (cached, needs, skipped_hand, force_notes,
             lookup_err) = fmv_runner._split_by_db_cache(
                books, server_url=server_url, max_age_days=7, force=False)
        assert skipped_hand == {}
        assert len(needs) == 1

    def test_force_overwrites_hand_priced_row_and_records_old_notes(self, server_url):
        """BUI-533 acceptance: --force proceeds to recompute (book lands in
        `needs`, not skipped), but the OLD hand notes are captured so the
        caller can echo them before they're overwritten."""
        books = [_make_book("1", "Batman", "251", 1972, 5.5, locg_id=100)]
        hand_row = {"id": 1, "fmv_low": 250, "fmv_high": 300,
                   "fmv_notes": "hand § anchored on the lone 4.0 sale"}
        with patch("fmv_runner._db_lookup", return_value=hand_row):
            (cached, needs, skipped_hand, force_notes,
             lookup_err) = fmv_runner._split_by_db_cache(
                books, server_url=server_url, max_age_days=7, force=True)
        assert skipped_hand == {}
        assert len(needs) == 1  # proceeds to recompute, not skipped
        assert force_notes == {0: "hand § anchored on the lone 4.0 sale"}

    def test_force_on_non_hand_priced_row_records_nothing(self, server_url):
        books = [_make_book("1", "X", "1", 1990, 9.0, locg_id=100)]
        normal_row = {"id": 1, "fmv_low": 50, "fmv_high": 75,
                     "fmv_notes": "window=±0.5 | cv=20% | label=HIGH"}
        with patch("fmv_runner._db_lookup", return_value=normal_row):
            (cached, needs, skipped_hand, force_notes,
             lookup_err) = fmv_runner._split_by_db_cache(
                books, server_url=server_url, max_age_days=7, force=True)
        assert force_notes == {}
        assert len(needs) == 1


class TestSplitByDbCacheLookupFailsClosed:
    """BUI-544: the hand-priced provenance check fails CLOSED.

    `_db_lookup`'s soft-fail posture made a FAILED lookup indistinguishable
    from "no row exists", so a transient comics-server error let a default run
    fall through and recompute — overwriting the hand-priced row the guard
    exists to protect, if the server recovered by upsert time.
    """

    def test_lookup_failure_skips_instead_of_recomputing(self, server_url):
        books = [_make_book("1", "Batman", "251", 1972, 5.5, locg_id=100)]
        with patch("fmv_runner._db_lookup",
                   side_effect=_failing_provenance_lookup()):
            (cached, needs, skipped_hand, force_notes,
             lookup_err) = fmv_runner._split_by_db_cache(
                books, server_url=server_url, max_age_days=7, force=False)
        assert needs == []          # never recomputed → never overwritten
        assert cached == {}
        assert list(lookup_err) == [0]
        assert "lookup failed" in lookup_err[0]

    def test_lookup_failure_is_not_counted_as_a_hand_priced_skip(self, server_url):
        """The binding rider: an outage skip must be its OWN category. Folding
        it into `skipped_hand` would let a transient failure under-compute a
        batch while reporting it as normal hand-price protection."""
        books = [_make_book("1", "Batman", "251", 1972, 5.5, locg_id=100)]
        with patch("fmv_runner._db_lookup",
                   side_effect=_failing_provenance_lookup()):
            (cached, needs, skipped_hand, force_notes,
             lookup_err) = fmv_runner._split_by_db_cache(
                books, server_url=server_url, max_age_days=7, force=False)
        assert skipped_hand == {}
        assert force_notes == {}
        assert len(lookup_err) == 1

    def test_force_does_not_bypass_the_fail_closed_skip(self, server_url):
        """`--force` may overwrite a hand-priced row, but BUI-533's contract is
        'overwrite AND echo the old notes first'. With the lookup dead we
        cannot echo, so the overwrite would destroy provenance unlogged."""
        books = [_make_book("1", "Batman", "251", 1972, 5.5, locg_id=100)]
        with patch("fmv_runner._db_lookup",
                   side_effect=_failing_provenance_lookup()):
            (cached, needs, skipped_hand, force_notes,
             lookup_err) = fmv_runner._split_by_db_cache(
                books, server_url=server_url, max_age_days=7, force=True)
        assert needs == []
        assert force_notes == {}
        assert list(lookup_err) == [0]

    def test_only_the_failing_book_is_skipped(self, server_url):
        """A per-book failure must not take the rest of the batch down with
        it — the other books still price normally."""
        books = [_make_book("1", "Batman", "251", 1972, 5.5, locg_id=100),
                 _make_book("2", "X-Men", "39", 1967, 9.0, locg_id=200)]

        def _lookup(server, *, locg_id, grade, locg_variant_id=None,
                    max_age_days, strict=False):
            if strict and locg_id == 100:
                raise fmv_runner._DbLookupFailed("comics-server lookup failed")
            return None

        with patch("fmv_runner._db_lookup", side_effect=_lookup):
            (cached, needs, skipped_hand, force_notes,
             lookup_err) = fmv_runner._split_by_db_cache(
                books, server_url=server_url, max_age_days=7, force=False)
        assert list(lookup_err) == [0]
        assert [b["_idx"] for b in needs] == [1]

    def test_fresh_lookup_failure_alone_still_protects_via_the_any_age_lookup(
            self, server_url):
        """The FIRST (freshness-gated) lookup keeps the soft-fail posture: when
        only it fails, the age-unbounded lookup — a superset of the same rows —
        still answers the hand-priced question correctly, so the row is
        protected and nothing lands in the lookup-error bucket."""
        hand_row = {"id": 1, "fmv_low": 250, "fmv_high": 300,
                    "fmv_notes": "hand § anchored on the lone 4.0 sale"}
        books = [_make_book("1", "Batman", "251", 1972, 5.5, locg_id=100)]

        def _lookup(server, *, locg_id, grade, locg_variant_id=None,
                    max_age_days, strict=False):
            # strict (any-age) succeeds; the fresh one "fails" soft → None.
            return hand_row if strict else None

        with patch("fmv_runner._db_lookup", side_effect=_lookup):
            (cached, needs, skipped_hand, force_notes,
             lookup_err) = fmv_runner._split_by_db_cache(
                books, server_url=server_url, max_age_days=7, force=False)
        assert skipped_hand == {0: hand_row}
        assert lookup_err == {}

    def test_real_transport_failure_flows_all_the_way_to_a_skip(self, server_url):
        """Seam-free: patch only the HTTP layer, so the real `_db_lookup` AND
        the real call site both participate. The other tests in this class stub
        `_db_lookup` itself, so they'd still pass if `strict=True` were dropped
        from the provenance call on the default (non---force) path — this one
        wouldn't."""
        import requests
        books = [_make_book("1", "Batman", "251", 1972, 5.5, locg_id=100)]
        with patch("fmv_runner.requests.get",
                   side_effect=requests.ConnectionError("server down")):
            (cached, needs, skipped_hand, force_notes,
             lookup_err) = fmv_runner._split_by_db_cache(
                books, server_url=server_url, max_age_days=7, force=False)
        assert needs == []
        assert skipped_hand == {}
        assert list(lookup_err) == [0]

    def test_ineligible_book_never_reaches_the_lookup(self, server_url):
        """A book with no locg_id can't be looked up by key at all, so it can't
        target an existing row either — it must keep going to compute rather
        than get caught by the new skip."""
        books = [_make_book("1", "X", "1", 1990, 9.0)]
        with patch("fmv_runner._db_lookup",
                   side_effect=_failing_provenance_lookup()) as lookup:
            (cached, needs, skipped_hand, force_notes,
             lookup_err) = fmv_runner._split_by_db_cache(
                books, server_url=server_url, max_age_days=7, force=False)
        lookup.assert_not_called()
        assert lookup_err == {}
        assert len(needs) == 1


class TestEchoHandOverrideNotes:
    def test_echoes_old_notes_to_stderr(self, capsys):
        books = [_make_book("1", "Batman", "251", 1972, 5.5)]
        fmv_runner._echo_hand_override_notes(
            {0: "hand § anchored on the lone 4.0 sale"}, books)
        err = capsys.readouterr().err
        assert "--force" in err
        assert "Batman #251" in err
        assert "hand § anchored on the lone 4.0 sale" in err

    def test_no_op_when_empty(self, capsys):
        fmv_runner._echo_hand_override_notes({}, [])
        assert capsys.readouterr().err == ""


class TestRunSkipsHandPricedRows:
    def test_table_shows_skipped_source_and_untouched_values(self, server_url, capsys):
        """With the human table on (not --quiet), the row must render with the
        `skipped_hand_priced` source and the row's own untouched $250-$300
        range — never a recomputed number."""
        batch = [{"item_id": "1", "title": "Batman", "issue": "251",
                 "year": 1972, "grade": 5.5, "locg_id": 100}]
        hand_row = {"id": 1, "fmv_low": 250, "fmv_high": 300, "fmv_comps": 1,
                   "fmv_confidence": "low",
                   "fmv_notes": "hand § anchored on the lone 4.0 sale",
                   "fmv_updated_at": "2020-01-01T00:00:00"}
        with patch("fmv_runner._read_batch", return_value=batch), \
             patch("fmv_runner._db_lookup", side_effect=_stale_hand_lookup(hand_row)), \
             patch("fmv_runner._fetch_comps") as fetch_mock:
            fmv_runner.run(batch_path="x.json", out_path=None,
                          max_age_days=7, force=False, quiet=False,
                          server_url=server_url)
        fetch_mock.assert_not_called()
        cap = capsys.readouterr()
        assert "skipped_hand_priced" in cap.out
        assert "$250" in cap.out and "$300" in cap.out
        # BUI-549: the skip-count summary is not part of the human table — it
        # goes to stderr unconditionally, never stdout.
        assert "skipped 1 hand-priced row" not in cap.out
        assert "skipped 1 hand-priced row" in cap.err


    def test_default_run_skips_and_reports_count(self, server_url, capsys):
        """End-to-end (mocked network): a hand-priced row is left completely
        untouched by a default run, and the skip is reported unconditionally
        (not gated by --quiet) so it surfaces even with --brief-only usage.
        BUI-549: the report goes to stderr, since stdout is the machine-read
        surface under --brief."""
        batch = [{"item_id": "1", "title": "Batman", "issue": "251",
                 "year": 1972, "grade": 5.5, "locg_id": 100}]
        hand_row = {"id": 1, "fmv_low": 250, "fmv_high": 300, "fmv_comps": 1,
                   "fmv_confidence": "low",
                   "fmv_notes": "hand § anchored on the lone 4.0 sale",
                   "fmv_updated_at": "2020-01-01T00:00:00",
                   "title": "Batman", "issue": "251", "year": 1972,
                   "grade": 5.5}
        batch_path = "/tmp/_bui533_batch.json"
        with patch("fmv_runner._read_batch", return_value=batch), \
             patch("fmv_runner._db_lookup", side_effect=_stale_hand_lookup(hand_row)), \
             patch("fmv_runner._fetch_comps") as fetch_mock, \
             patch("fmv_runner._upsert_fmv") as upsert_mock:
            fmv_runner.run(batch_path=batch_path, out_path=None,
                          max_age_days=7, force=False, quiet=True,
                          server_url=server_url)
        fetch_mock.assert_not_called()
        upsert_mock.assert_not_called()
        cap = capsys.readouterr()
        assert cap.out == ""
        assert "skipped 1 hand-priced row" in cap.err
        assert "--force" in cap.err

    def test_quiet_still_reports_skip_summary(self, server_url, capsys):
        """The skip-count message must not be suppressed by --quiet — an
        operator using --brief --quiet must still learn rows were skipped.
        BUI-549: the message is on stderr, and stdout under --brief is pure
        JSON Lines (no human-readable preamble ahead of the row)."""
        batch = [{"item_id": "1", "title": "Batman", "issue": "251",
                 "year": 1972, "grade": 5.5, "locg_id": 100}]
        hand_row = {"id": 1, "fmv_low": 250, "fmv_high": 300,
                   "fmv_notes": "hand § anchored on the lone 4.0 sale",
                   "fmv_updated_at": "2020-01-01T00:00:00"}
        with patch("fmv_runner._read_batch", return_value=batch), \
             patch("fmv_runner._db_lookup", side_effect=_stale_hand_lookup(hand_row)), \
             patch("fmv_runner._fetch_comps") as fetch_mock:
            fmv_runner.run(batch_path="x.json", out_path=None,
                          max_age_days=7, force=False, quiet=True, brief=True,
                          server_url=server_url)
        fetch_mock.assert_not_called()
        cap = capsys.readouterr()
        assert "skipped 1 hand-priced row" not in cap.out
        assert "skipped 1 hand-priced row" in cap.err
        # --brief JSON line for the row is also present, unaffected, and
        # stdout is nothing BUT that JSON line — every non-blank line parses.
        lines = [ln for ln in cap.out.splitlines() if ln.strip()]
        assert len(lines) == 1
        assert json.loads(lines[0])["item_id"] == "1"


class TestRunFailsClosedOnLookupError:
    """BUI-544 end-to-end (mocked network): fail-closed turns a comics-server
    outage into skipped books, so the skip has to be LOUD and impossible to
    mistake for the hand-priced skip."""

    def _batch(self):
        return [{"item_id": "1", "title": "Batman", "issue": "251",
                 "year": 1972, "grade": 5.5, "locg_id": 100}]

    def test_nothing_is_fetched_or_written(self, server_url, capsys):
        with patch("fmv_runner._read_batch", return_value=self._batch()), \
             patch("fmv_runner._db_lookup",
                   side_effect=_failing_provenance_lookup()), \
             patch("fmv_runner._fetch_comps") as fetch_mock, \
             patch("fmv_runner._upsert_fmv") as upsert_mock, \
             patch("fmv_runner.requests.post") as post_mock:
            fmv_runner.run(batch_path="x.json", out_path=None,
                           max_age_days=7, force=False, quiet=True,
                           server_url=server_url)
        fetch_mock.assert_not_called()
        upsert_mock.assert_not_called()
        post_mock.assert_not_called()

    def test_warning_is_loud_and_not_confusable_with_a_hand_priced_skip(
            self, server_url, capsys):
        with patch("fmv_runner._read_batch", return_value=self._batch()), \
             patch("fmv_runner._db_lookup",
                   side_effect=_failing_provenance_lookup()), \
             patch("fmv_runner._fetch_comps"):
            fmv_runner.run(batch_path="x.json", out_path=None,
                           max_age_days=7, force=False, quiet=True,
                           server_url=server_url)
        cap = capsys.readouterr()
        combined = cap.out + cap.err
        assert "skipped 1 book(s)" in combined
        assert "comics-server FMV lookup FAILED" in combined
        assert "NOT because they were hand-priced" in combined
        # The BUI-533 hand-priced count line must NOT fire — no book here was
        # hand-priced; we simply couldn't tell. Matched on that line's exact
        # phrasing, since the BUI-544 message legitimately says "hand-priced".
        assert "hand-priced row(s) (use --force to overwrite)" not in combined

    def test_warning_survives_quiet_and_force(self, server_url, capsys):
        with patch("fmv_runner._read_batch", return_value=self._batch()), \
             patch("fmv_runner._db_lookup",
                   side_effect=_failing_provenance_lookup()), \
             patch("fmv_runner._fetch_comps") as fetch_mock:
            fmv_runner.run(batch_path="x.json", out_path=None,
                           max_age_days=7, force=True, quiet=True, brief=True,
                           server_url=server_url)
        fetch_mock.assert_not_called()
        cap = capsys.readouterr()
        assert "comics-server FMV lookup FAILED" in cap.err
        # --brief still emits the row, so a machine consumer sees the book
        # rather than it vanishing from the output entirely.
        assert '"item_id": "1"' in cap.out
        # BUI-549: stdout is pure JSON Lines, and the row's `source` is how a
        # --brief-only consumer tells this apart from an ordinary unpriced
        # row (both have every pricing field null).
        lines = [ln for ln in cap.out.splitlines() if ln.strip()]
        assert len(lines) == 1
        brief = json.loads(lines[0])
        assert brief["source"] == "skipped_lookup_error"
        assert brief["max_bid"] is None

    def test_table_marks_the_row_distinctly_not_n_a(self, server_url, capsys):
        """Without its own token the row would print a bland 'n/a' and read as
        'this book is illiquid' — the BUI-143 trap, one layer down."""
        with patch("fmv_runner._read_batch", return_value=self._batch()), \
             patch("fmv_runner._db_lookup",
                   side_effect=_failing_provenance_lookup()), \
             patch("fmv_runner._fetch_comps"):
            fmv_runner.run(batch_path="x.json", out_path=None,
                           max_age_days=7, force=False, quiet=False,
                           server_url=server_url)
        out = capsys.readouterr().out
        assert "skip:db-err" in out
        assert "skipped_lookup_error" in out
        assert "n/a" not in out

    def test_mixed_batch_reports_the_two_skips_separately(self, server_url,
                                                          capsys):
        """The rider, end to end: one genuinely hand-priced book and one
        outage-skipped book in the same run must produce two distinct counts,
        not one merged 'skipped 2'."""
        batch = [{"item_id": "1", "title": "Batman", "issue": "251",
                  "year": 1972, "grade": 5.5, "locg_id": 100},
                 {"item_id": "2", "title": "X-Men", "issue": "39",
                  "year": 1967, "grade": 9.0, "locg_id": 200}]
        hand_row = {"id": 1, "fmv_low": 250, "fmv_high": 300, "fmv_comps": 1,
                    "fmv_confidence": "low",
                    "fmv_notes": "hand § anchored on the lone 4.0 sale",
                    "fmv_updated_at": "2020-01-01T00:00:00"}

        def _lookup(server, *, locg_id, grade, locg_variant_id=None,
                    max_age_days, strict=False):
            if not strict:
                return None            # both are stale/absent for the fresh pass
            if locg_id == 200:
                raise fmv_runner._DbLookupFailed("comics-server lookup failed")
            return hand_row

        with patch("fmv_runner._read_batch", return_value=batch), \
             patch("fmv_runner._db_lookup", side_effect=_lookup), \
             patch("fmv_runner._fetch_comps") as fetch_mock:
            fmv_runner.run(batch_path="x.json", out_path=None,
                           max_age_days=7, force=False, quiet=True,
                           server_url=server_url)
        fetch_mock.assert_not_called()
        cap = capsys.readouterr()
        # BUI-549: both skip-count lines are stderr-only now — stdout stays
        # pure JSON Lines under --brief.
        assert cap.out == ""
        assert "skipped 1 hand-priced row(s)" in cap.err
        assert "skipped 1 book(s) because the comics-server FMV lookup FAILED" \
            in cap.err


class TestRunSkipsPermanentWriteRejection:
    """BUI-639 end-to-end (mocked network): a 422 write rejection (e.g.
    BUI-625's multi-issue-lot refusal) on one book must skip only that book,
    not abort the whole run — and the skip must be LOUD in the summary, per
    the BUI-565/570/593 "must never look like a clean run" precedent the
    other two skip classes (BUI-533/BUI-544) already follow."""

    def _batch(self):
        return [
            {"item_id": "1", "title": "Lot", "issue": "1", "year": 1990,
             "grade": 9.0},
            {"item_id": "2", "title": "Solo", "issue": "1", "year": 1990,
             "grade": 9.0},
        ]

    def _fake_results(self):
        comps = [_make_comp(p, 9.0) for p in [50, 55, 60, 65, 70]]
        return [
            {"input": {"_req_id": 0, "title": "Lot", "issue": "1",
                       "year": 1990, "grade": 9.0, "item_id": "1"},
             "comps": comps, "queries_used": [{"tier": "base", "cached": False}]},
            {"input": {"_req_id": 1, "title": "Solo", "issue": "1",
                       "year": 1990, "grade": 9.0, "item_id": "2"},
             "comps": comps, "queries_used": [{"tier": "base", "cached": False}]},
        ]

    @staticmethod
    def _upsert_side_effect(server_url, inp, fmv, hard_fail=True):
        # Mocks the classification _post_json already has unit coverage for
        # (TestUpsertFmv) — here we're only proving run()'s per-book handling
        # of the exception it raises, not re-deriving the HTTP mechanics.
        if inp.get("title") == "Lot":
            raise fmv_runner._UpsertRejected(
                "multi-issue lot listing cannot be its first issue")
        return {"comic_id": 99, "fmv_id": 5}

    def test_second_book_still_priced_when_first_is_rejected(
            self, tmp_path, server_url):
        batch_path = tmp_path / "batch.json"
        batch_path.write_text(json.dumps(self._batch()))
        out_path = tmp_path / "out.json"

        with patch("fmv_runner._fetch_comps", return_value=self._fake_results()), \
             patch("fmv_runner._upsert_fmv", side_effect=self._upsert_side_effect):
            fmv_runner.run(batch_path=str(batch_path), out_path=str(out_path),
                           max_age_days=7, force=False, quiet=True,
                           server_url=server_url)

        out = json.loads(out_path.read_text())
        assert len(out) == 2
        assert out[0]["source"] == "skipped_rejected"
        assert out[0]["fmv"] is None
        assert out[1]["source"] == "fresh"
        assert out[1]["fmv"]["n"] == 5
        assert out[1]["comic_id"] == 99

    def test_run_completes_without_exiting(self, tmp_path, server_url):
        """The premise this ticket fixes: before BUI-639, ANY 422 aborted the
        whole run via sys.exit(1). If that regressed, this test would raise
        SystemExit and fail — there is no pytest.raises wrapping the call."""
        batch_path = tmp_path / "batch.json"
        batch_path.write_text(json.dumps(self._batch()))

        with patch("fmv_runner._fetch_comps", return_value=self._fake_results()), \
             patch("fmv_runner._upsert_fmv", side_effect=self._upsert_side_effect):
            fmv_runner.run(batch_path=str(batch_path), out_path=None,
                           max_age_days=7, force=False, quiet=True,
                           server_url=server_url)

    def test_summary_reports_exactly_one_skipped_book_with_reason(
            self, tmp_path, server_url, capsys):
        batch_path = tmp_path / "batch.json"
        batch_path.write_text(json.dumps(self._batch()))
        out_path = tmp_path / "out.json"

        with patch("fmv_runner._fetch_comps", return_value=self._fake_results()), \
             patch("fmv_runner._upsert_fmv", side_effect=self._upsert_side_effect):
            fmv_runner.run(batch_path=str(batch_path), out_path=str(out_path),
                           max_age_days=7, force=False, quiet=True,
                           server_url=server_url)
        cap = capsys.readouterr()
        # The aggregate stderr line is a COUNT, not a per-book reason dump —
        # same convention as the BUI-544 skipped_lookup_error line. Exactly
        # one book skipped, and it must be unmistakably a permanent rejection.
        assert "skipped 1 book(s)" in cap.err
        assert "REJECTED the write (422)" in cap.err
        assert "PERMANENT" in cap.err
        # Must not fire the OTHER two skip-count lines — nothing here was
        # hand-priced or lookup-failed, so those must stay silent.
        assert "hand-priced row(s)" not in cap.err
        assert "comics-server FMV lookup FAILED" not in cap.err
        # The per-book REASON lives on the stitched row's `error` field (the
        # --out/--brief surface), not in the aggregate stderr count line.
        out = json.loads(out_path.read_text())
        assert "multi-issue lot listing cannot be its first issue" in out[0]["error"]

    def test_table_marks_the_rejected_row_distinctly_not_n_a(
            self, tmp_path, server_url, capsys):
        batch_path = tmp_path / "batch.json"
        batch_path.write_text(json.dumps(self._batch()))

        with patch("fmv_runner._fetch_comps", return_value=self._fake_results()), \
             patch("fmv_runner._upsert_fmv", side_effect=self._upsert_side_effect):
            fmv_runner.run(batch_path=str(batch_path), out_path=None,
                           max_age_days=7, force=False, quiet=False,
                           server_url=server_url)
        out = capsys.readouterr().out
        assert "skip:422" in out
        assert "skipped_rejected" in out

    def test_brief_projects_both_rows_with_distinct_sources(
            self, tmp_path, server_url, capsys):
        batch_path = tmp_path / "batch.json"
        batch_path.write_text(json.dumps(self._batch()))

        with patch("fmv_runner._fetch_comps", return_value=self._fake_results()), \
             patch("fmv_runner._upsert_fmv", side_effect=self._upsert_side_effect):
            fmv_runner.run(batch_path=str(batch_path), out_path=None,
                           max_age_days=7, force=False, quiet=True, brief=True,
                           server_url=server_url)
        cap = capsys.readouterr()
        lines = [ln for ln in cap.out.splitlines() if ln.strip()]
        assert len(lines) == 2
        rows = [json.loads(ln) for ln in lines]
        assert rows[0]["source"] == "skipped_rejected"
        assert rows[0]["max_bid"] is None
        assert rows[1]["source"] == "fresh"
        assert rows[1]["max_bid"] is not None


class TestHandPricedAndFetchErrComposition:
    """BUI-533 x BUI-536 composition: --force on a hand-priced row whose
    fetch THEN errors out. The force-overwrite echo fires (it's a statement of
    intent, printed before the fetch even runs), but the actual row must stay
    completely untouched — the fetch-err guard always wins over --force."""

    def test_force_plus_fetch_err_leaves_row_untouched(self, server_url, capsys):
        batch = [{"item_id": "1", "title": "Batman", "issue": "251",
                 "year": 1972, "grade": 5.5, "locg_id": 100}]
        hand_row = {"id": 1, "fmv_low": 250, "fmv_high": 300,
                   "fmv_notes": "hand § anchored on the lone 4.0 sale"}
        # ebay-sold-comps result for the one needs_compute book: every tier
        # errored (comps empty, queries_used all-error) — a fetch-err.
        fetch_err_result = [{
            "input": {"title": "Batman", "issue": "251", "year": 1972,
                      "grade": 5.5, "_req_id": 0},
            "comps": [],
            "queries_used": [{"tier": "base", "error": "RateLimiter 10001"}],
        }]
        with patch("fmv_runner._read_batch", return_value=batch), \
             patch("fmv_runner._db_lookup", return_value=hand_row), \
             patch("fmv_runner._fetch_comps", return_value=fetch_err_result), \
             patch("fmv_runner._upsert_fmv") as upsert_mock, \
             patch("fmv_runner.requests.post") as post_mock:
            fmv_runner.run(batch_path="x.json", out_path=None,
                          max_age_days=7, force=True, quiet=True,
                          server_url=server_url)
        # The force-echo DID fire (it's printed before the fetch even runs)...
        err = capsys.readouterr().err
        assert "--force: about to overwrite" in err
        # ...but no write ever actually happened: the fetch-err guard in
        # _compute_and_upsert_one takes priority over --force unconditionally.
        upsert_mock.assert_not_called()
        post_mock.assert_not_called()

    def test_vintage_fetch_err_is_not_picked_up_by_proxy_rescue(self, server_url):
        """The narrower regression this composition case exposed: a vintage
        fetch-err result must not be treated as an `_is_unpriced_raw`
        candidate for the CGC-proxy rescue — that side door would let the
        rescue's own second fetch upsert a book BUI-536 said must stay
        untouched."""
        fresh_fmvs = {
            0: {"input": {"title": "X-Men", "issue": "39", "year": 1967,
                         "grade": 9.0},
               "fmv": None, "comp_count_total": 0,
               "queries_used": [{"tier": "base", "error": "outage"}],
               "db_row": None, "comic_id": None, "fmv_id": None,
               "source": "error", "error": "fetch-err: all tiers failed"},
        }
        assert fmv_runner._is_unpriced_raw(fresh_fmvs[0]) is False
        books = [{"title": "X-Men", "issue": "39", "year": 1967, "grade": 9.0}]
        with patch("fmv_runner._fetch_comps") as fetch_mock, \
             patch("fmv_runner._upsert_fmv") as upsert_mock:
            fmv_runner._apply_cgc_proxy_rescue(
                fresh_fmvs, books, server_url=server_url, force=False)
        fetch_mock.assert_not_called()
        upsert_mock.assert_not_called()
        assert fresh_fmvs[0]["source"] == "error"  # untouched


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


class TestDbLookupStrict:
    """BUI-544: `strict=True` separates "the GET failed" from "no row found",
    which the default soft-fail path collapses into the same None."""

    def test_network_error_raises(self, server_url):
        import requests
        with patch("fmv_runner.requests.get",
                   side_effect=requests.ConnectionError("nope")), \
             pytest.raises(fmv_runner._DbLookupFailed):
            fmv_runner._db_lookup(server_url, locg_id=1, grade=9.0,
                                  max_age_days=None, strict=True)

    def test_http_error_raises(self, server_url):
        import requests
        mock_resp = MagicMock()
        mock_resp.raise_for_status.side_effect = requests.HTTPError("503")
        with patch("fmv_runner.requests.get", return_value=mock_resp), \
             pytest.raises(fmv_runner._DbLookupFailed):
            fmv_runner._db_lookup(server_url, locg_id=1, grade=9.0,
                                  max_age_days=None, strict=True)

    def test_malformed_json_raises(self, server_url):
        """A non-JSON body is a failed lookup, not an empty one — it must not
        be answerable as 'no hand-priced row here'."""
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.side_effect = fmv_runner.requests.exceptions.JSONDecodeError(
            "Expecting value", "", 0)
        with patch("fmv_runner.requests.get", return_value=mock_resp), \
             pytest.raises(fmv_runner._DbLookupFailed):
            fmv_runner._db_lookup(server_url, locg_id=1, grade=9.0,
                                  max_age_days=None, strict=True)

    def test_genuine_empty_result_returns_none_without_raising(self, server_url):
        """The whole point of the distinction: a SUCCESSFUL lookup that finds
        nothing must still be a plain miss, or fail-closed would skip every
        never-priced book."""
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = []
        with patch("fmv_runner.requests.get", return_value=mock_resp):
            row = fmv_runner._db_lookup(server_url, locg_id=1, grade=9.0,
                                        max_age_days=None, strict=True)
        assert row is None

    def test_row_still_returned_normally(self, server_url):
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = [
            {"id": 1, "locg_id": 1, "grade": 9.0, "locg_variant_id": None,
             "fmv_low": 50, "fmv_updated_at": "2026-07-01T00:00:00"}]
        with patch("fmv_runner.requests.get", return_value=mock_resp):
            row = fmv_runner._db_lookup(server_url, locg_id=1, grade=9.0,
                                        max_age_days=None, strict=True)
        assert row["id"] == 1

    def test_soft_path_unchanged_by_the_new_sentinel(self, server_url):
        """Regression guard on the refactor: `_get_json_or_warn`'s default is
        now a private sentinel rather than None, and it must never leak out —
        the non-strict path still returns a plain None on failure."""
        import requests
        with patch("fmv_runner.requests.get",
                   side_effect=requests.ConnectionError("nope")):
            row = fmv_runner._db_lookup(server_url, locg_id=1, grade=9.0,
                                        max_age_days=None, strict=False)
        assert row is None


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


# ─── fetch-err must never touch the fmv DB row (BUI-536) ─────────────────────

class TestFetchErrDoesNotTouchDbRow:
    def test_all_tiers_failed_never_upserts(self, server_url):
        """The core regression test: every query tier ERRORED (mocked
        all-tiers-fail fetch) — _upsert_fmv must never be called, no matter
        whether a row already exists for this book."""
        result = {
            "input": {"title": "X-Men", "issue": "39", "year": 1967,
                      "grade": 9.0},
            "comps": [],
            "queries_used": [{"tier": "base", "error": "RateLimiter 10001"},
                             {"tier": "wide", "error": "RateLimiter 10001"}],
        }
        with patch("fmv_runner._upsert_fmv") as upsert_mock:
            out = fmv_runner._compute_and_upsert_one(
                result, {"title": "X-Men", "issue": "39", "grade": 9.0},
                server_url=server_url)
            upsert_mock.assert_not_called()
        assert out["source"] == "error"
        assert "fetch-err" in out["error"]
        assert out["fmv"] is None
        assert out["db_row"] is None
        assert out["comic_id"] is None
        assert out["fmv_id"] is None
        assert out["comp_count_total"] == 0
        assert out["queries_used"] == result["queries_used"]

    def test_all_tiers_failed_leaves_existing_row_byte_identical(self, server_url):
        """BUI-536 acceptance: after a fetch-err on a book with an existing fmv
        row, low/high/comps/confidence/notes/flag_reason/updated_at must be
        byte-identical to pre-run. Simulated here by asserting the network POST
        (the only way the row could change) is never even attempted — no
        upsert call means the server-side row is untouched by construction."""
        result = {
            "input": {"title": "Fantastic Four", "issue": "46", "year": 1963,
                      "grade": 8.0},
            "comps": [],
            "queries_used": [{"tier": "base", "error": "quota exceeded"}],
        }
        with patch("fmv_runner.requests.post") as post_mock:
            out = fmv_runner._compute_and_upsert_one(
                result, {"title": "Fantastic Four", "issue": "46", "grade": 8.0},
                server_url=server_url)
        post_mock.assert_not_called()
        assert out["source"] == "error"

    def test_all_tiers_failed_on_new_book_creates_nothing(self, server_url):
        """BUI-536 acceptance: a fetch-err on a book with NO existing row must
        create nothing either — same no-upsert path regardless of whether a
        row already exists (the function never even looks one up here)."""
        result = {
            "input": {"title": "Brand New Comic", "issue": "1", "year": 2026,
                      "grade": 9.8},
            "comps": [],
            "queries_used": [{"tier": "base", "error": "outage"}],
        }
        with patch("fmv_runner._upsert_fmv") as upsert_mock:
            out = fmv_runner._compute_and_upsert_one(
                result, {"title": "Brand New Comic", "issue": "1", "grade": 9.8},
                server_url=server_url)
            upsert_mock.assert_not_called()
        assert out["comic_id"] is None
        assert out["fmv_id"] is None

    def test_partial_tier_failure_with_comps_still_upserts(self, server_url):
        """Adversarial case: SOME tiers errored but at least one comp came
        back — this must NOT be classified as fetch-err (comp_count_total > 0
        takes priority in _is_fetch_error), so the normal BUI-44 upsert path
        still runs."""
        comps = [_make_comp(p, 8.0) for p in [10, 11, 12, 13, 14]]
        result = {
            "input": {"title": "X", "issue": "1", "year": 1990, "grade": 8.0},
            "comps": comps,
            "queries_used": [{"tier": "base", "error": "partial outage"},
                             {"tier": "wide", "nkw": 5}],
        }
        with patch("fmv_runner._upsert_fmv",
                   return_value={"comic_id": 1, "fmv_id": 1}) as upsert_mock:
            out = fmv_runner._compute_and_upsert_one(
                result, {"title": "X", "issue": "1", "grade": 8.0},
                server_url=server_url)
        upsert_mock.assert_called_once()
        assert out["source"] == "fresh"
        assert out["fmv"]["n"] == 5

    def test_mixed_error_and_clean_zero_result_queries_still_upserts(self, server_url):
        """Adversarial edge case: comp_count_total is 0, but only SOME queries
        carry an 'error' — one tier genuinely ran clean and found nothing. Per
        the existing BUI-143 `_is_fetch_error` contract (ALL queries must
        error), this is a genuine n=0, not a fetch-err — it must still upsert
        the stub row, not be silently dropped as an error."""
        result = {
            "input": {"title": "X", "issue": "1", "year": 1990, "grade": 8.0},
            "comps": [],
            "queries_used": [{"tier": "base", "error": "one tier failed"},
                             {"tier": "wide", "nkw": 3}],  # ran clean, 0 comps
        }
        with patch("fmv_runner._upsert_fmv",
                   return_value={"comic_id": 1, "fmv_id": 1}) as upsert_mock:
            out = fmv_runner._compute_and_upsert_one(
                result, {"title": "X", "issue": "1", "grade": 8.0},
                server_url=server_url)
        upsert_mock.assert_called_once()
        assert out["source"] == "fresh"
        assert out["fmv"]["n"] == 0

    def test_genuine_n0_with_error_free_queries_still_upserts(self, server_url):
        """The critical distinction this ticket must preserve (BUI-44's own
        acceptance criterion): a book that genuinely has zero comps ran its
        queries CLEANLY (no 'error' key at all) — that must still upsert the
        stub row unconditionally, never be misclassified as fetch-err."""
        result = {
            "input": {"title": "Godzilla: The Half-Century War", "issue": "1",
                      "year": 2012, "grade": 9.8},
            "comps": [],
            "queries_used": [{"tier": "base", "nkw": 0}],
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
        post_mock.assert_called_once()
        assert out["source"] == "fresh"
        assert out["fmv"]["n"] == 0
        assert out["fmv"]["fmv_low"] is None
        assert out["comic_id"] == 7

    def test_no_queries_used_key_at_all_is_not_fetch_error(self, server_url):
        """A result dict missing `queries_used` entirely (e.g. an older
        ebay-sold-comps version, or the BUI-44 no-comps stub fixture shape)
        must not be misread as a fetch error — _is_fetch_error's own guard
        (`if not queries: return False`) already covers this; pin it at the
        _compute_and_upsert_one boundary too."""
        result = {
            "input": {"title": "X", "issue": "1", "grade": 9.0},
            "comps": [],
        }
        with patch("fmv_runner._upsert_fmv",
                   return_value={"comic_id": 5, "fmv_id": 2}) as upsert_mock:
            out = fmv_runner._compute_and_upsert_one(
                result, {"title": "X", "issue": "1", "grade": 9.0},
                server_url=server_url)
            upsert_mock.assert_called_once()
        assert out["source"] == "fresh"


# ─── BUI-535: breaker_tripped passthrough ─────────────────────────────────────

class TestBreakerTrippedPassthrough:
    """_compute_and_upsert_one must thread ebay-sold-comps' `breaker_tripped`
    flag onto every return path — the field comic-fmv's own --out/stdout
    surface to distinguish 'outage' from 'priced' (BUI-535)."""

    def test_defaults_false_when_absent(self, server_url):
        """Back-compat: an older ebay-sold-comps that never sends the field
        must not crash and must default to False, not None/missing."""
        result = {"input": {"title": "X", "issue": "1", "year": 1990,
                            "grade": 8.0},
                  "comps": [_make_comp(p, 8.0) for p in [10, 11, 12, 13, 14]]}
        with patch("fmv_runner._upsert_fmv", return_value={"id": 1}):
            out = fmv_runner._compute_and_upsert_one(
                result, {"title": "X", "issue": "1", "grade": 8.0},
                server_url=server_url)
        assert out["breaker_tripped"] is False

    def test_true_on_fresh_priced_row(self, server_url):
        comps = [_make_comp(p, 8.0) for p in [10, 11, 12, 13, 14]]
        result = {"input": {"title": "X", "issue": "1", "year": 1990,
                            "grade": 8.0},
                  "comps": comps, "breaker_tripped": True}
        with patch("fmv_runner._upsert_fmv", return_value={"id": 1}):
            out = fmv_runner._compute_and_upsert_one(
                result, {"title": "X", "issue": "1", "grade": 8.0},
                server_url=server_url)
        assert out["breaker_tripped"] is True
        assert out["source"] == "fresh"

    def test_true_on_no_grade_error_row(self, server_url):
        result = {"input": {"title": "X", "issue": "1"}, "comps": [],
                  "breaker_tripped": True}
        out = fmv_runner._compute_and_upsert_one(
            result, {"title": "X", "issue": "1"}, server_url=server_url)
        assert out["breaker_tripped"] is True
        assert out["source"] == "error"

    def test_true_on_fetch_err_row(self, server_url):
        """The fetch-err path (BUI-536) and the breaker-tripped flag (BUI-535)
        are independent signals that both need to survive on the same row —
        a breaker-skipped book IS a fetch-err (BUI-536's guard) AND carries
        breaker_tripped=True (this ticket) simultaneously."""
        result = {
            "input": {"title": "X", "issue": "1", "year": 1990, "grade": 8.0},
            "comps": [],
            "queries_used": [{"tier": "base", "page": 1,
                              "outcome": "error:BreakerTrippedError",
                              "error": "breaker tripped"}],
            "breaker_tripped": True,
        }
        with patch("fmv_runner._upsert_fmv") as upsert_mock:
            out = fmv_runner._compute_and_upsert_one(
                result, {"title": "X", "issue": "1", "grade": 8.0},
                server_url=server_url)
            upsert_mock.assert_not_called()
        assert out["breaker_tripped"] is True
        assert out["source"] == "error"
        assert "fetch-err" in out["error"]

    def test_stitch_defaults_false_for_cached_and_error_rows(self):
        book = _make_book("a", "A", "1", 1990, 9.0)
        cached = {0: {"fmv_low": 5, "fmv_high": 10, "fmv_comps": 5,
                      "fmv_confidence": "low",
                      "title": "A", "issue": "1", "year": 1990, "grade": 9.0}}
        out = fmv_runner._stitch([book], cached, {})
        assert out[0]["breaker_tripped"] is False

        out_err = fmv_runner._stitch([book], {}, {})
        assert out_err[0]["breaker_tripped"] is False

    def test_print_table_surfaces_breaker_tripped_summary(self, capsys):
        rows = [
            {"input": {"title": "X", "issue": "1", "grade": 9.0},
             "fmv": {"fmv_low": None}, "comp_count_total": 0,
             "queries_used": [{"tier": "base", "error": "breaker tripped"}],
             "source": "error", "breaker_tripped": True},
        ]
        fmv_runner._print_table(rows)
        out = capsys.readouterr()
        combined = out.out + out.err
        assert "circuit breaker tripped" in combined.lower()


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

    def test_upsert_fmv_still_fails_loud_on_5xx(self, server_url):
        """BUI-639: only 422 is carved out of the BUI-186 fail-loud default —
        a 5xx (infrastructure failure) is exactly the transient case BUI-186
        exists for, and must keep aborting the run."""
        import requests
        inp = {"title": "X", "issue": "1", "year": 1990, "grade": 9.0}
        fmv = {"fmv_low": 100, "fmv_high": 150, "n": 8, "confidence": "HIGH",
               "window": 0.5, "cv_pct": "20%"}
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        http_err = requests.HTTPError("500 Server Error")
        http_err.response = mock_resp
        mock_resp.raise_for_status.side_effect = http_err
        with patch("fmv_runner.requests.post", return_value=mock_resp):
            with pytest.raises(SystemExit):
                fmv_runner._upsert_fmv(server_url, inp, fmv)

    def test_upsert_fmv_raises_permanent_rejection_on_422_instead_of_exiting(
            self, server_url):
        """BUI-639: a 422 is the comics-server write boundary's PERMANENT,
        per-item rejection (e.g. BUI-625's multi-issue-lot refusal) — never
        transient, so it must not take the whole run down via sys.exit. It
        raises `_UpsertRejected` instead, carrying the server's detail, so the
        caller can skip just this one book."""
        import requests
        inp = {"title": "X", "issue": "1", "year": 1990, "grade": 9.0}
        fmv = {"fmv_low": 100, "fmv_high": 150, "n": 8, "confidence": "HIGH",
               "window": 0.5, "cv_pct": "20%"}
        mock_resp = MagicMock()
        mock_resp.status_code = 422
        http_err = requests.HTTPError("422 Client Error")
        http_err.response = mock_resp
        mock_resp.raise_for_status.side_effect = http_err
        mock_resp.json.return_value = {
            "detail": "multi-issue lot listing cannot be its first issue"}
        with patch("fmv_runner.requests.post", return_value=mock_resp):
            with pytest.raises(fmv_runner._UpsertRejected) as exc_info:
                fmv_runner._upsert_fmv(server_url, inp, fmv)
        assert "multi-issue lot listing" in str(exc_info.value)

    def test_upsert_fmv_422_soft_fails_under_hard_fail_false(self, server_url):
        """The BUI-348/BUI-529 best-effort re-upsert sites pass hard_fail=False
        and already treat ANY failure (422 included) as a soft None return —
        that pre-existing behavior must be untouched by the BUI-639 carve-out,
        which only changes the hard_fail=True (default) path."""
        import requests
        inp = {"title": "X", "issue": "1", "year": 1990, "grade": 9.0}
        fmv = {"fmv_low": 100, "fmv_high": 150, "n": 8, "confidence": "HIGH",
               "window": 0.5, "cv_pct": "20%"}
        mock_resp = MagicMock()
        mock_resp.status_code = 422
        http_err = requests.HTTPError("422 Client Error")
        http_err.response = mock_resp
        mock_resp.raise_for_status.side_effect = http_err
        mock_resp.json.return_value = {"detail": "rejected"}
        with patch("fmv_runner.requests.post", return_value=mock_resp):
            result = fmv_runner._upsert_fmv(server_url, inp, fmv, hard_fail=False)
        assert result is None

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

    def test_skipped_hand_row_reused_verbatim_with_distinct_source(self):
        """BUI-533: a hand-priced skip is stitched exactly like a cache hit
        (the row is reused, never recomputed) but tagged with a distinct
        `source` so the table/summary can tell it apart from an ordinary
        cache hit."""
        book = _make_book("a", "A", "1", 1990, 9.0)
        row = {"fmv_low": 250, "fmv_high": 300, "fmv_comps": 1,
               "fmv_confidence": "low",
               "fmv_notes": "hand § anchored on the lone 4.0 sale",
               "title": "A", "issue": "1", "year": 1990, "grade": 9.0}
        out = fmv_runner._stitch([book], {}, {}, {0: row})
        assert out[0]["source"] == "skipped_hand_priced"
        assert out[0]["fmv"]["fmv_low"] == 250
        assert out[0]["fmv"]["fmv_high"] == 300
        assert out[0]["db_row"] == row

    def test_skipped_hand_defaults_to_empty_when_omitted(self):
        """Back-compat: existing callers that don't pass skipped_hand still work."""
        books = [_make_book("a", "A", "1", 1990, 9.0)]
        out = fmv_runner._stitch(books, {}, {})
        assert out[0]["source"] == "error"

    def test_lookup_error_skip_gets_its_own_source_and_no_price(self):
        """BUI-544: a fail-closed skip has no row to reuse, so it carries no
        price — and it must NOT borrow the hand-priced skip's source, or an
        outage would render as successful protection."""
        book = _make_book("a", "A", "1", 1990, 9.0)
        out = fmv_runner._stitch([book], {}, {}, {},
                                 {0: "comics-server FMV lookup failed"})
        assert out[0]["source"] == "skipped_lookup_error"
        assert out[0]["fmv"] is None
        assert out[0]["db_row"] is None
        assert "BUI-544" in out[0]["error"]
        assert "comics-server FMV lookup failed" in out[0]["error"]

    def test_both_skip_kinds_stay_distinct_in_one_batch(self):
        """The two skips must remain separable per-row, not just in aggregate."""
        books = [_make_book("a", "A", "1", 1990, 9.0),
                 _make_book("b", "B", "2", 1990, 9.0)]
        hand_row = {"fmv_low": 250, "fmv_high": 300, "fmv_comps": 1,
                    "fmv_confidence": "low",
                    "fmv_notes": "hand § anchored on the lone 4.0 sale"}
        out = fmv_runner._stitch(books, {}, {}, {0: hand_row}, {1: "boom"})
        assert out[0]["source"] == "skipped_hand_priced"
        assert out[0]["fmv"]["fmv_low"] == 250
        assert out[1]["source"] == "skipped_lookup_error"
        assert out[1]["fmv"] is None

    def test_lookup_error_defaults_to_empty_when_omitted(self):
        """Back-compat for the 4-arg callers that predate BUI-544."""
        books = [_make_book("a", "A", "1", 1990, 9.0)]
        out = fmv_runner._stitch(books, {}, {}, {})
        assert out[0]["source"] == "error"

    def test_rejected_skip_gets_its_own_source_and_no_price(self):
        """BUI-639: a permanent (422) write rejection has no persisted row to
        reuse, so — like skipped_lookup_error — it carries no price. It must
        NOT borrow either existing skip source, or a permanent per-book
        rejection would misread as an outage or as hand-priced protection."""
        book = _make_book("a", "A", "1", 1990, 9.0)
        out = fmv_runner._stitch([book], {}, {}, {}, {},
                                 {0: "multi-issue lot listing"})
        assert out[0]["source"] == "skipped_rejected"
        assert out[0]["fmv"] is None
        assert out[0]["db_row"] is None
        assert "BUI-639" in out[0]["error"]
        assert "multi-issue lot listing" in out[0]["error"]

    def test_all_three_skip_kinds_stay_distinct_in_one_batch(self):
        """All three skip classes must remain separable per-row, not just in
        aggregate — a batch can plausibly hit all three in one run."""
        books = [_make_book("a", "A", "1", 1990, 9.0),
                 _make_book("b", "B", "2", 1990, 9.0),
                 _make_book("c", "C", "3", 1990, 9.0)]
        hand_row = {"fmv_low": 250, "fmv_high": 300, "fmv_comps": 1,
                    "fmv_confidence": "low",
                    "fmv_notes": "hand § anchored on the lone 4.0 sale"}
        out = fmv_runner._stitch(books, {}, {}, {0: hand_row}, {1: "boom"},
                                 {2: "lot listing"})
        assert out[0]["source"] == "skipped_hand_priced"
        assert out[1]["source"] == "skipped_lookup_error"
        assert out[2]["source"] == "skipped_rejected"
        assert out[2]["fmv"] is None


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


# ─── BUI-565: string `year` at the handoff + surfacing the per-book error ────

class TestYearCoercionHelpers:
    def test_coerce_year_accepts_the_shapes_identify_emits(self):
        assert fmv_runner._coerce_year("1976") == 1976
        assert fmv_runner._coerce_year("  1976 ") == 1976
        assert fmv_runner._coerce_year("1976.0") == 1976
        assert fmv_runner._coerce_year(1976) == 1976
        assert fmv_runner._coerce_year(1976.0) == 1976

    def test_coerce_year_returns_none_for_unusable_values(self):
        for bad in (None, "", "   ", "n/a", "c. 1976", [], {}, True, False):
            assert fmv_runner._coerce_year(bad) is None, bad

    def test_normalize_book_year_coerces_in_place(self):
        book = {"title": "X-Men", "issue": "101", "year": "1976"}
        fmv_runner._normalize_book_year(book)
        assert book["year"] == 1976

    def test_normalize_book_year_leaves_unparseable_values_alone(self):
        """Deliberate: a garbage year must reach ebay-sold-comps so it raises
        and becomes a VISIBLE per-book error. Silently dropping it would price
        the book off a different (year-less) search with no announcement."""
        book = {"title": "X-Men", "issue": "101", "year": "sometime in the 70s"}
        fmv_runner._normalize_book_year(book)
        assert book["year"] == "sometime in the 70s"

    def test_normalize_book_year_is_a_noop_without_a_year(self):
        book = {"title": "X-Men", "issue": "101"}
        fmv_runner._normalize_book_year(book)
        assert "year" not in book

    def test_string_year_reaches_is_vintage_gate(self):
        """`_is_vintage` tests `isinstance(year, (int, float))`, so a string
        year silently read as "not vintage" and skipped the CGC-proxy rescue.
        Normalizing at the handoff is what closes that."""
        book = {"title": "X-Men", "issue": "101", "year": "1976"}
        fmv_runner._normalize_book_year(book)
        assert fmv_runner._is_vintage({"input": book}) is True


class TestRunNormalizesYearAtHandoff:
    def test_run_sends_an_int_year_to_fetch_comps(self, tmp_path, server_url):
        """BUI-565 repro at the handoff: `/comic:identify` emits year as a
        string, and the string is what crashed ebay-sold-comps' first tier."""
        batch = [{"item_id": "800411934143", "title": "X-Men", "issue": "101",
                  "year": "1976", "grade": 7.5}]
        batch_path = tmp_path / "batch.json"
        batch_path.write_text(json.dumps(batch))
        out_path = tmp_path / "out.json"

        fake_comps = [_make_comp(p, 7.5) for p in [400, 450, 500, 550, 600]]
        fake_result = [{
            "input": {"_req_id": 0, "title": "X-Men", "issue": "101",
                      "year": 1976, "grade": 7.5, "item_id": "800411934143"},
            "comps": fake_comps,
            "queries_used": [{"tier": "base", "cached": False}],
        }]
        with patch("fmv_runner._fetch_comps", return_value=fake_result) as fetch_mock, \
             patch("fmv_runner._upsert_fmv", return_value={"id": 1}) as upsert:
            fmv_runner.run(batch_path=str(batch_path), out_path=str(out_path),
                           max_age_days=7, force=False, quiet=True,
                           server_url=server_url)

        assert fetch_mock.call_args[0][0][0]["year"] == 1976
        # ...and the same int reaches the DB upsert body, so a re-run's
        # cache/identity lookup can't split on "1976" vs 1976.
        assert upsert.call_args[0][1]["year"] == 1976


class TestSoldCompsErrorIsSurfaced:
    """BUI-565's second half. `fetch_book_comps` already tags a per-book
    `error` when its fetch RAISED, but when the raise beat every tier the
    `queries_used` trail is EMPTY — and `_is_fetch_error`'s "no queries" guard
    reads empty as a genuine no-comps book. So the row fell through to BUI-44's
    unconditional upsert and came back as a clean n=0 with a real `comic_id`
    and a null `flag_reason`: invisible to BOTH of /comic:buy Step 3's guards.
    """

    def _errored_result(self, **over):
        result = {
            "input": {"title": "X-Men", "issue": "101", "year": "1976",
                      "grade": 7.5},
            "comps": [],
            "queries_used": [],
            "error": "'<' not supported between instances of 'str' and 'int'",
        }
        result.update(over)
        return result

    def test_errored_result_does_not_upsert(self, server_url):
        with patch("fmv_runner._upsert_fmv") as upsert_mock:
            out = fmv_runner._compute_and_upsert_one(
                self._errored_result(),
                {"title": "X-Men", "issue": "101", "grade": 7.5},
                server_url=server_url)
        upsert_mock.assert_not_called()
        assert out["source"] == "error"
        assert out["fmv"] is None

    def test_errored_result_yields_a_null_comic_id(self, server_url):
        """The load-bearing assertion: /comic:buy Step 3's `comic_id: null`
        guard is what catches this book, so the row must NOT carry an id."""
        with patch("fmv_runner._upsert_fmv") as upsert_mock:
            out = fmv_runner._compute_and_upsert_one(
                self._errored_result(),
                {"title": "X-Men", "issue": "101", "grade": 7.5},
                server_url=server_url)
        upsert_mock.assert_not_called()
        assert out["comic_id"] is None
        assert out["fmv_id"] is None

    def test_errored_result_propagates_the_upstream_message(self, server_url):
        with patch("fmv_runner._upsert_fmv"):
            out = fmv_runner._compute_and_upsert_one(
                self._errored_result(),
                {"title": "X-Men", "issue": "101", "grade": 7.5},
                server_url=server_url)
        assert out["error"].startswith("fetch-err")
        assert "'str' and 'int'" in out["error"]

    def test_errored_result_classifies_as_fetch_err(self, server_url):
        """It must RENDER as 'fetch-err' and join the run's fetch-err warning
        count — not as a bland 'n/a' that reads as an illiquid book."""
        with patch("fmv_runner._upsert_fmv"):
            out = fmv_runner._compute_and_upsert_one(
                self._errored_result(),
                {"title": "X-Men", "issue": "101", "grade": 7.5},
                server_url=server_url)
        assert fmv_runner._is_fetch_error(out) is True

    def test_partial_comps_alongside_an_error_still_bail(self, server_url):
        """Adversarial: a raise PART WAY through the tiers leaves a truncated
        pool of unknown size. Pricing off it is the same wrong answer, just
        quieter — and `_is_fetch_error`'s `comp_count_total` short-circuit
        would otherwise wave it straight through to the upsert."""
        comps = [_make_comp(p, 7.5) for p in [10, 11, 12, 13, 14]]
        with patch("fmv_runner._upsert_fmv") as upsert_mock:
            out = fmv_runner._compute_and_upsert_one(
                self._errored_result(
                    comps=comps,
                    queries_used=[{"tier": "base", "nkw": 5}]),
                {"title": "X-Men", "issue": "101", "grade": 7.5},
                server_url=server_url)
        upsert_mock.assert_not_called()
        assert out["comic_id"] is None
        assert out["source"] == "error"
        assert fmv_runner._is_fetch_error(out) is True

    def test_clean_result_is_untouched(self, server_url):
        """Acceptance: a result WITHOUT an `error` key keeps the BUI-44
        unconditional-upsert behavior byte-for-byte, n=0 included."""
        with patch("fmv_runner._upsert_fmv",
                   return_value={"comic_id": 3, "fmv_id": 4}) as upsert_mock:
            out = fmv_runner._compute_and_upsert_one(
                {"input": {"title": "X-Men", "issue": "101", "year": 1976,
                           "grade": 7.5},
                 "comps": [], "queries_used": [{"tier": "base", "nkw": 0}]},
                {"title": "X-Men", "issue": "101", "grade": 7.5},
                server_url=server_url)
        upsert_mock.assert_called_once()
        assert out["source"] == "fresh"
        assert out["comic_id"] == 3

    def test_empty_error_message_still_bails(self, server_url):
        """Adversarial: sold_comps tags the key with `str(e)`, and an exception
        raised with no message stringifies to "". A truthiness test on that key
        would drop this book straight back into the upsert path the guard
        exists to prevent."""
        with patch("fmv_runner._upsert_fmv") as upsert_mock:
            out = fmv_runner._compute_and_upsert_one(
                self._errored_result(error=""),
                {"title": "X-Men", "issue": "101", "grade": 7.5},
                server_url=server_url)
        upsert_mock.assert_not_called()
        assert out["comic_id"] is None
        assert fmv_runner._is_fetch_error(out) is True

    def test_end_to_end_errored_book_never_gets_a_comic_id(
            self, tmp_path, server_url):
        """The whole chain, on the ticket's repro: `_fetch_comps` returns the
        subprocess JSON verbatim, so a per-book `error` reaches
        `_compute_and_upsert_one` — and must land in the written out.json as a
        row /comic:buy Step 3's `comic_id: null` guard will catch."""
        batch = [{"item_id": "800411934143", "title": "X-Men", "issue": "101",
                  "year": "1976", "grade": 7.5}]
        batch_path = tmp_path / "batch.json"
        batch_path.write_text(json.dumps(batch))
        out_path = tmp_path / "out.json"

        fake_result = [{
            "input": {"_req_id": 0, "title": "X-Men", "issue": "101",
                      "year": "1976", "grade": 7.5, "item_id": "800411934143"},
            "comps": [], "queries_used": [], "slab_comps": [],
            "breaker_tripped": False,
            "error": "'<' not supported between instances of 'str' and 'int'",
        }]
        with patch("fmv_runner._fetch_comps", return_value=fake_result), \
             patch("fmv_runner._upsert_fmv") as upsert_mock:
            fmv_runner.run(batch_path=str(batch_path), out_path=str(out_path),
                           max_age_days=7, force=False, quiet=True,
                           server_url=server_url)

        upsert_mock.assert_not_called()
        rows = json.loads(out_path.read_text())
        assert len(rows) == 1
        assert rows[0]["comic_id"] is None
        assert rows[0]["source"] == "error"
        assert rows[0]["error"].startswith("fetch-err")
        # Not the pre-fix shape: a clean n=0 with a real comic_id.
        assert rows[0]["fmv"] is None

    def test_other_error_sources_are_not_mislabelled_fetch_err(self, server_url):
        """The `fetch_error` flag is deliberately narrower than
        `source == "error"`: a book with no usable grade never attempted a
        fetch, so labelling it 'fetch-err' would send an operator chasing an
        API outage that never happened."""
        out = fmv_runner._compute_and_upsert_one(
            {"input": {"title": "X-Men", "issue": "101"}, "comps": [],
             "queries_used": []},
            {"title": "X-Men", "issue": "101"}, server_url=server_url)
        assert out["source"] == "error"
        assert "no target grade" in out["error"]
        assert fmv_runner._is_fetch_error(out) is False


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


class TestFmvRefreshHeartbeat:
    """BUI-624: the `fmv-refresh` heartbeat (contract: BUI-602).

    The success definition is deliberately NOT "comic-fmv exited 0" — BUI-593
    is exactly a run that exited 0 while the write 422'd and the book was
    stored nowhere. So the ping is gated on a PERSISTED row: at least one
    `/api/comics` upsert that came back carrying an fmv_id.

    Both directions are pinned, because the two failures cost differently.
    Pinging when nothing persisted certifies the BUI-593 shape as health — the
    expensive, silent error. Failing to ping when something did persist makes a
    working pipeline look dead — loud, and cheap to fix.
    """

    def _batch(self):
        return [
            {"item_id": "1", "title": "Lot", "issue": "1", "year": 1990,
             "grade": 9.0},
            {"item_id": "2", "title": "Solo", "issue": "1", "year": 1990,
             "grade": 9.0},
        ]

    def _results(self):
        comps = [_make_comp(p, 9.0) for p in [50, 55, 60, 65, 70]]
        return [
            {"input": {"_req_id": i, "title": title, "issue": "1",
                       "year": 1990, "grade": 9.0, "item_id": str(i + 1)},
             "comps": comps,
             "queries_used": [{"tier": "base", "cached": False}]}
            for i, title in enumerate(["Lot", "Solo"])
        ]

    def _run(self, tmp_path, server_url, results, upsert):
        batch_path = tmp_path / "batch.json"
        batch_path.write_text(json.dumps(self._batch()))
        with patch("fmv_runner._fetch_comps", return_value=results), \
             patch("fmv_runner._upsert_fmv", side_effect=upsert), \
             patch("fmv_runner.requests.post") as post:
            fmv_runner.run(batch_path=str(batch_path), out_path=None,
                           max_age_days=7, force=False, quiet=True,
                           server_url=server_url)
        return post

    @staticmethod
    def _ok_upsert(server_url, inp, fmv, hard_fail=True):
        return {"comic_id": 99, "fmv_id": 5}

    @staticmethod
    def _all_rejected(server_url, inp, fmv, hard_fail=True):
        raise fmv_runner._UpsertRejected("multi-issue lot listing")

    def test_persisted_batch_pings_once(self, tmp_path, server_url):
        post = self._run(tmp_path, server_url, self._results(), self._ok_upsert)

        assert post.call_count == 1, "one ping per BATCH, never one per book"
        assert post.call_args[0][0] == f"{server_url}/api/heartbeat/fmv-refresh"
        assert "2" in post.call_args.kwargs["params"]["detail"]

    def test_all_writes_rejected_does_not_ping(self, tmp_path, server_url):
        """The BUI-593 shape itself: every fetch succeeded, every write 422'd,
        every book stored nowhere — and `comic-fmv` still exits 0. Silence is
        mandatory here, or the heartbeat certifies the very failure it exists
        to expose."""
        post = self._run(tmp_path, server_url, self._results(),
                         self._all_rejected)

        assert post.call_count == 0

    def test_fetch_errors_do_not_ping(self, tmp_path, server_url):
        """A book whose fetch raised bails before the upsert (BUI-565), so it
        persists nothing and contributes nothing to the heartbeat."""
        results = self._results()
        for r in results:
            r["comps"] = []
            r["error"] = "provider timeout"

        post = self._run(tmp_path, server_url, results, self._ok_upsert)

        assert post.call_count == 0

    def test_one_survivor_is_enough_to_ping(self, tmp_path, server_url):
        """The refresh DID run and DID store something. A partially-rejected
        batch reports its skips loudly on stderr; the heartbeat answers the
        narrower question "is this pipeline alive at all", and it is."""
        def upsert(server_url_, inp, fmv, hard_fail=True):
            if inp.get("title") == "Lot":
                raise fmv_runner._UpsertRejected("multi-issue lot listing")
            return {"comic_id": 99, "fmv_id": 5}

        post = self._run(tmp_path, server_url, self._results(), upsert)

        assert post.call_count == 1
        assert "1" in post.call_args.kwargs["params"]["detail"]

    def test_nothing_persisted_is_silence_not_a_zero_ping(self, server_url):
        with patch("fmv_runner.requests.post") as post:
            fmv_runner._ping_fmv_heartbeat(server_url, persisted=0)
        post.assert_not_called()

    def test_a_failed_ping_never_fails_the_run(self, server_url, capsys):
        """The heartbeat is a backstop under comic-fmv's reporting, not a gate
        in front of it. A watchdog able to fail a real FMV batch would cost
        more than the silence it exists to break."""
        with patch("fmv_runner.requests.post",
                   side_effect=fmv_runner.requests.ConnectionError("down")):
            fmv_runner._ping_fmv_heartbeat(server_url, persisted=3)

        assert "heartbeat ping failed" in capsys.readouterr().err

    def test_a_non_2xx_ping_is_reported_not_swallowed(self, server_url, capsys):
        """A 404 (job missing from a stale server's JOB_CONTRACTS) must surface
        — otherwise the operator sees a green run and a watchdog going stale
        with nothing connecting the two."""
        resp = MagicMock()
        resp.raise_for_status.side_effect = fmv_runner.requests.HTTPError("404")
        with patch("fmv_runner.requests.post", return_value=resp):
            fmv_runner._ping_fmv_heartbeat(server_url, persisted=1)

        assert "heartbeat ping failed" in capsys.readouterr().err

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


class TestAnchorDivergesNotes:
    def test_notes_carry_token_when_diverges(self):
        fmv = {"cv_pct": "20%", "confidence": "MEDIUM-LOW",
               "ungraded_anchor": {"median": 224.8, "n": 36},
               "anchor_diverges": True}
        notes = fmv_runner._build_notes(fmv)
        assert "ungraded_anchor=$224.8 (n=36 raw)" in notes
        assert "anchor_diverges=1" in notes

    def test_notes_omit_token_when_not_diverging(self):
        fmv = {"cv_pct": "20%", "confidence": "HIGH",
               "ungraded_anchor": {"median": 100.0, "n": 10},
               "anchor_diverges": False}
        notes = fmv_runner._build_notes(fmv)
        assert "ungraded_anchor=$100 (n=10 raw)" in notes
        assert "anchor_diverges" not in notes

    def test_notes_omit_token_when_key_absent(self):
        fmv = {"cv_pct": "20%", "confidence": "HIGH"}
        notes = fmv_runner._build_notes(fmv)
        assert "anchor_diverges" not in notes

    def test_cached_row_recovers_flag_from_notes(self):
        row = {"fmv_low": 400, "fmv_high": 425, "fmv_comps": 6,
               "fmv_confidence": "medium-low",
               "fmv_notes": ("window=±0.5 | cv=5% | label=MEDIUM-LOW | "
                             "ungraded_anchor=$224.8 (n=36 raw) | "
                             "anchor_diverges=1")}
        out = fmv_runner._fmv_from_db_row(row)
        assert out["anchor_diverges"] is True

    def test_cached_row_without_token_recovers_false(self):
        row = {"fmv_low": 100, "fmv_high": 150, "fmv_comps": 8,
               "fmv_confidence": "high", "fmv_notes": "window=±0.5 | cv=20%"}
        out = fmv_runner._fmv_from_db_row(row)
        assert out["anchor_diverges"] is False

    def test_db_row_shape_parity_carries_anchor_diverges(self):
        """Unlike `ungraded_anchor` (genuinely unreconstructible on a cache
        hit), `anchor_diverges` recovers from notes and so IS carried by
        `_fmv_from_db_row` — the shared shape-parity fixture
        (test_db_row_shape_parity_with_compute_fmv) already pins this since
        it doesn't exempt this key."""
        computed = fmv_math.compute_fmv([{"price": 100, "grade": 9.2}],
                                        target_grade=9.2)
        assert "anchor_diverges" in computed
        row = {"fmv_low": 100, "fmv_high": 150, "fmv_comps": 8,
               "fmv_confidence": "high", "fmv_notes": "window=±0.5 | cv=20%"}
        projected = fmv_runner._fmv_from_db_row(row)
        assert "anchor_diverges" in projected


# ─── --brief projection (BUI-362) ─────────────────────────────────────────────

class TestBriefProjection:
    """`_brief_row` must project the ten linkage/pricing fields under exactly
    the names /comic:buy's Step 3 documents — item_id, comic_id, fmv_id,
    max_bid, flag_reason, confidence, fmv_low, fmv_high, fmv_notes (BUI-505),
    source (BUI-549) — across all row sources."""

    BRIEF_KEYS = {"item_id", "comic_id", "fmv_id", "max_bid",
                  "flag_reason", "confidence", "fmv_low", "fmv_high",
                  "fmv_notes", "source"}

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
                         "fmv_notes": "window=n/a | cv=10% | label=HIGH",
                         "source": "fresh"}

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
        assert brief["source"] == "cached"
        # Cached path reads the persisted fmv_notes verbatim off db_row rather
        # than recomputing it (the reconstructed cached fmv dict is a lossy
        # projection missing fields like first_party_count — see
        # _fmv_from_db_row — so recomputing could drop tokens the original had).
        assert brief["fmv_notes"] == "window=±0.5 | cv=20% | label=MEDIUM"

    def test_error_row_projects_nulls_not_missing_keys(self):
        # A _stitch error row (no comps, no cache) has neither top-level ids
        # nor a db_row nor an fmv dict — every pricing/linkage field is null,
        # but every key must still exist for a uniform downstream reader.
        # `source` is the deliberate exception (BUI-549): it carries the
        # row's real source ("error") rather than null, precisely so a
        # --brief consumer can tell rows like this apart from each other.
        row = {
            "input": {"item_id": "333", "title": "X", "issue": "1"},
            "fmv": None, "db_row": None, "source": "error",
            "error": "no comps fetched and no cache",
        }
        brief = fmv_runner._brief_row(row)
        assert set(brief) == self.BRIEF_KEYS
        assert brief["item_id"] == "333"
        assert brief["source"] == "error"
        assert all(brief[k] is None
                   for k in self.BRIEF_KEYS - {"item_id", "source"})

    def test_skipped_lookup_error_row_has_distinct_source(self):
        # BUI-549: the whole point of adding `source` — a skipped_lookup_error
        # row (comics-server lookup FAILED) must be distinguishable in
        # --brief from an ordinary unpriced/error row, even though every
        # pricing field is null in both cases.
        row = {
            "input": {"item_id": "555", "title": "X", "issue": "1"},
            "fmv": None, "db_row": None, "source": "skipped_lookup_error",
            "error": "BUI-544: skipped, hand-priced provenance unverifiable",
        }
        brief = fmv_runner._brief_row(row)
        assert set(brief) == self.BRIEF_KEYS
        assert brief["source"] == "skipped_lookup_error"
        assert all(brief[k] is None
                   for k in self.BRIEF_KEYS - {"item_id", "source"})

    def test_skipped_hand_priced_row_has_distinct_source(self):
        # A hand-priced skip (BUI-533) is NOT the same source as a
        # lookup-error skip — the row is untouched in both cases, but for
        # opposite reasons (protection vs. a failed check).
        row = {
            "input": {"item_id": "666", "title": "X", "issue": "1"},
            "fmv": {"max_bid": 48, "flag_reason": None, "confidence": "LOW",
                    "fmv_low": 40, "fmv_high": 50},
            "db_row": {"id": 2, "fmv_id": 4,
                       "fmv_notes": "hand § anchored"},
            "source": "skipped_hand_priced",
        }
        brief = fmv_runner._brief_row(row)
        assert brief["source"] == "skipped_hand_priced"

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
        assert brief["source"] == "fresh"

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


# ─── BUI-588 / BUI-581: query-rewrite signals from ebay-sold-comps ────────────

class TestVariantDroppedSignal:
    """A pool that only exists because the book's variant term was dropped
    prices the BASE cover. That trade must reach the caller, not be buried in a
    clean-looking row (BUI-588)."""

    def _priced_result(self, **extra):
        result = {
            "input": {"title": "Uncanny X-Men", "issue": "281", "year": 1991,
                      "grade": 9.2},
            "comps": [_make_comp(p, 9.2) for p in [20, 22, 24, 26, 28]],
        }
        result.update(extra)
        return result

    def _book(self):
        return {"title": "Uncanny X-Men", "issue": "281", "grade": 9.2,
                "variant": "White Logo 1st Print"}

    def test_dropped_variant_becomes_a_needs_manual_flag(self, server_url):
        with patch("fmv_runner._upsert_fmv", return_value={"id": 1}):
            out = fmv_runner._compute_and_upsert_one(
                self._priced_result(variant_dropped="White Logo 1st Print"),
                self._book(), server_url=server_url)
        assert out["fmv"]["flag_reason"] == "variant_dropped"
        # /comic:buy Step 3 gates on flag_reason AND on the absent number.
        assert out["fmv"]["max_bid"] is None
        assert out["fmv"]["fmv_low"] is None

    def test_absent_signal_prices_normally(self, server_url):
        """The overwhelmingly common path must be untouched."""
        with patch("fmv_runner._upsert_fmv", return_value={"id": 1}):
            out = fmv_runner._compute_and_upsert_one(
                self._priced_result(), self._book(), server_url=server_url)
        assert out["fmv"]["flag_reason"] is None
        assert out["fmv"]["max_bid"] is not None

    def test_flag_reaches_the_db_row(self, server_url):
        """`fmv_flag_reason` is the structured column /comic:verify and the
        upsert's stale-price clearing both read — the notes token alone is not
        enough."""
        upsert = MagicMock(return_value={"id": 1})
        with patch("fmv_runner._upsert_fmv", upsert):
            fmv_runner._compute_and_upsert_one(
                self._priced_result(variant_dropped="White Logo 1st Print"),
                self._book(), server_url=server_url)
        fmv_arg = upsert.call_args.args[2]
        assert fmv_arg["flag_reason"] == "variant_dropped"

    def test_notes_name_the_dropped_term(self, server_url):
        with patch("fmv_runner._upsert_fmv", return_value={"id": 1}):
            out = fmv_runner._compute_and_upsert_one(
                self._priced_result(variant_dropped="White Logo 1st Print"),
                self._book(), server_url=server_url)
        notes = fmv_runner._build_notes(out["fmv"])
        assert "variant_dropped=White Logo 1st Print" in notes
        assert "manual_review=variant_dropped" in notes

    def test_notes_keep_the_term_when_a_pool_reason_wins_the_flag(self, server_url):
        """A shape reason takes the flag slot, but WHAT was traded away to get a
        pool at all is still the thing a human needs in order to judge the
        row — it must not be lost."""
        one_sided = {
            "input": {"title": "FF", "issue": "63", "year": 1967, "grade": 9.6},
            "comps": [_make_comp(p, 9.0) for p in [40, 42, 44, 45, 41]],
            "variant_dropped": "2nd Printing Gold Cover",
        }
        with patch("fmv_runner._upsert_fmv", return_value={"id": 1}):
            out = fmv_runner._compute_and_upsert_one(
                one_sided, {"title": "FF", "issue": "63", "grade": 9.6},
                server_url=server_url)
        assert out["fmv"]["flag_reason"] == "one_sided"
        notes = fmv_runner._build_notes(out["fmv"])
        assert "variant_dropped=2nd Printing Gold Cover" in notes

    def test_notes_omit_the_token_when_nothing_was_dropped(self, server_url):
        with patch("fmv_runner._upsert_fmv", return_value={"id": 1}):
            out = fmv_runner._compute_and_upsert_one(
                self._priced_result(), self._book(), server_url=server_url)
        assert "variant_dropped" not in fmv_runner._build_notes(out["fmv"])


class TestMastheadSwapSignal:
    def _result(self, **extra):
        result = {
            "input": {"title": "Uncanny X-Men", "issue": "69", "year": 1970,
                      "grade": 5.0},
            "comps": [_make_comp(p, 5.0) for p in [12, 14, 15, 16, 18]],
        }
        result.update(extra)
        return result

    def test_swapped_masthead_is_named_in_the_notes(self, server_url):
        """BUI-581: the comps came from the other name this series carried.
        Saying so keeps the number auditable — a reader comparing the row's
        title to the pool would otherwise have no way to know."""
        with patch("fmv_runner._upsert_fmv", return_value={"id": 1}):
            out = fmv_runner._compute_and_upsert_one(
                self._result(masthead_swapped_to="X-Men"),
                {"title": "Uncanny X-Men", "issue": "69", "grade": 5.0},
                server_url=server_url)
        assert "masthead=X-Men" in fmv_runner._build_notes(out["fmv"])

    def test_a_swap_does_not_withhold_the_price(self, server_url):
        """Unlike a dropped variant, the alias pool is the SAME book under its
        other name — comparable, so it prices normally."""
        with patch("fmv_runner._upsert_fmv", return_value={"id": 1}):
            out = fmv_runner._compute_and_upsert_one(
                self._result(masthead_swapped_to="X-Men"),
                {"title": "Uncanny X-Men", "issue": "69", "grade": 5.0},
                server_url=server_url)
        assert out["fmv"]["flag_reason"] is None
        assert out["fmv"]["max_bid"] is not None

    def test_no_token_when_the_callers_masthead_was_used(self, server_url):
        with patch("fmv_runner._upsert_fmv", return_value={"id": 1}):
            out = fmv_runner._compute_and_upsert_one(
                self._result(),
                {"title": "Uncanny X-Men", "issue": "69", "grade": 5.0},
                server_url=server_url)
        assert "masthead=" not in fmv_runner._build_notes(out["fmv"])


# ─── Cross-grade inversion sweep (BUI-583) ────────────────────────────────────

def _crow(comic_id, grade, low, high, title="X-Men", issue="83", year=None):
    """One `GET /api/comics` row (comic joined to one of its fmv rows)."""
    return {"id": comic_id, "title": title, "issue": issue, "year": year,
            "grade": grade, "fmv_low": low, "fmv_high": high}


class TestFetchInversions:
    def _get(self, rows):
        return patch("fmv_runner._get_json_or_warn", return_value=rows)

    def test_reports_the_motivating_pair(self, server_url):
        with self._get([_crow(812, 4.0, 35.0, 70.0), _crow(812, 7.0, 5.0, 45.0)]):
            got = fmv_runner.fetch_inversions(server_url)
        assert len(got) == 1
        f = got[0]
        assert (f["comic_id"], f["lower_grade"], f["higher_grade"]) == (812, 4.0, 7.0)
        assert (f["lower_low"], f["lower_high"]) == (35.0, 70.0)
        assert (f["higher_low"], f["higher_high"]) == (5.0, 45.0)

    def test_clean_table_reports_nothing(self, server_url):
        with self._get([_crow(1, 4.0, 30.0, 40.0), _crow(1, 7.0, 80.0, 100.0)]):
            assert fmv_runner.fetch_inversions(server_url) == []

    def test_groups_by_comic_id_not_title_issue_year(self, server_url):
        """A base cover and its Newsstand variant are separate comics rows
        sharing title/issue/year. They price differently and legitimately so —
        grouping on those fields would manufacture an inversion between them."""
        rows = [_crow(10, 9.0, 200.0, 240.0, title="Iron Man", issue="124", year=1979),
                _crow(11, 4.0, 20.0, 30.0, title="Iron Man", issue="124", year=1979)]
        with self._get(rows):
            assert fmv_runner.fetch_inversions(server_url) == []

    def test_unpriced_stub_rows_do_not_invert(self, server_url):
        with self._get([_crow(5, 4.0, 35.0, 70.0), _crow(5, 9.0, None, None)]):
            assert fmv_runner.fetch_inversions(server_url) == []

    def test_rows_without_a_grade_are_ignored(self, server_url):
        """A comics row with no fmv row at all LEFT JOINs to grade=None."""
        with self._get([_crow(7, None, None, None), _crow(7, 6.0, 10.0, 20.0)]):
            assert fmv_runner.fetch_inversions(server_url) == []

    def test_failed_read_returns_none_not_empty(self, server_url):
        """R11-shaped: a failed call must never render as 'no inversions'."""
        with patch("fmv_runner._get_json_or_warn",
                   return_value=fmv_runner._LOOKUP_FAILED):
            assert fmv_runner.fetch_inversions(server_url) is None

    def test_non_list_body_returns_none(self, server_url):
        with self._get({"unexpected": "shape"}):
            assert fmv_runner.fetch_inversions(server_url) is None


class TestRunInversionSweep:
    def test_missing_server_url_exits_1(self):
        with pytest.raises(SystemExit) as e:
            fmv_runner.run_inversion_sweep(server_url=None)
        assert e.value.code == 1

    def test_failed_read_exits_1_without_a_verdict(self, server_url, capsys):
        with patch("fmv_runner.fetch_inversions", return_value=None):
            with pytest.raises(SystemExit) as e:
                fmv_runner.run_inversion_sweep(server_url=server_url)
        assert e.value.code == 1
        assert "no verdict rendered" in capsys.readouterr().err

    def test_clean_sweep_reports_and_returns(self, server_url, capsys):
        with patch("fmv_runner.fetch_inversions", return_value=[]):
            fmv_runner.run_inversion_sweep(server_url=server_url)
        assert "No cross-grade FMV inversions found." in capsys.readouterr().out

    def test_findings_are_printed_with_both_bands(self, server_url, capsys):
        finding = {"comic_id": 812, "title": "X-Men", "issue": "83", "year": None,
                   "lower_grade": 4.0, "lower_low": 35.0, "lower_high": 70.0,
                   "higher_grade": 7.0, "higher_low": 5.0, "higher_high": 45.0}
        with patch("fmv_runner.fetch_inversions", return_value=[finding]):
            fmv_runner.run_inversion_sweep(server_url=server_url)
        out = capsys.readouterr().out
        assert "1 cross-grade FMV inversion(s)" in out
        assert "X-Men #83" in out
        assert "$35-70" in out and "$5-45" in out
        # The advisory contract must be stated wherever findings are shown.
        assert "no price was changed" in out
