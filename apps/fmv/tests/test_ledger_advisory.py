"""BUI-663 — degraded-mode ADVISORY pricing from the comps ledger.

When both sold-comps providers fail for a book, `_compute_and_upsert_one`'s
BUI-536 guard returns a `fetch-err` row that touches nothing. BUI-663 adds one
thing on top of that row: an ADVISORY band priced from the comps ledger, so an
operator at an auction deadline gets a number to hand-check instead of nothing.

The whole safety argument is that the advisory number cannot overpay BY
CONSTRUCTION, which is what most of this file pins:

  * `max_bid` is None on every advisory row (`TestNoBidCapEverEscapes`)
  * nothing is upserted, so the number reaches no `fmv` row — and therefore no
    `snipe-add`, no policy check, no dashboard, and no later cache re-read
    (`TestAdvisoryIsNeverPersisted`)
  * the two tiers that RE-upsert skip advisory rows entirely — without that,
    `_apply_cgc_cross_check` would mint a comics row and write the band
  * every surface labels it (`TestSurfacesLabelTheRow`)

Transport is mocked at `requests.get`; the ledger→pool projection,
`_get_json_or_warn`'s failure handling, `compute_fmv`, the depth floor and the
whole result assembly all run for real. Mocking `_get_json_or_warn` or
`_fetch_ledger_comps` instead would test none of the contract.
"""

import json

from unittest.mock import MagicMock, patch

import pytest

import fmv_runner


# ─── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def server_url():
    return "http://test-server:8080"


# Every column of the `comps` table, in declaration order, as
# `GET /api/comics/comps` returns them: `get_comps` does `SELECT * FROM comps`
# and the route projects each row with `dict(r)`, so the wire shape IS the
# table. Source of truth: `plugins/gixen-overlay/src/gixen_overlay/db.py`'s
# `CREATE TABLE IF NOT EXISTS comps (...)`. Kept complete rather than trimmed
# to the three fields the pool needs, so a row that is missing a field the
# server really sends can't quietly pass here.
_COMPS_ROW_COLUMNS = (
    "id", "comic_id", "pool", "provider", "product_id", "title", "price",
    "sold_date", "grade", "buying_format", "link", "query", "tier",
    "from_cache", "observed_at", "provenance", "first_seen_at",
    "last_seen_at", "seen_count", "conflict_count",
)


def _ledger_row(row_id, *, price, grade, sold_date, comic_id=77):
    """One `GET /api/comics/comps` row, full width."""
    row = {
        "id": row_id,
        "comic_id": comic_id,
        "pool": "raw",
        "provider": "sold-comps.com",
        "product_id": f"p{row_id}",
        "title": f"Fantastic Four #46 CGC {grade}",
        "price": price,
        "sold_date": sold_date,
        "grade": grade,
        "buying_format": "Auction",
        "link": f"https://example.test/{row_id}",
        "query": "Fantastic Four 46",
        "tier": "base",
        "from_cache": 0,
        "observed_at": "2026-07-30T12:00:00+00:00",
        "provenance": "live",
        "first_seen_at": "2026-07-30T12:00:00+00:00",
        "last_seen_at": "2026-07-30T12:00:00+00:00",
        "seen_count": 1,
        "conflict_count": 0,
    }
    assert set(row) == set(_COMPS_ROW_COLUMNS), "fixture drifted from the comps table"
    return row


# A healthy raw pool around grade 8.0: eight comps, grades 7.5-8.5 (span 1.0,
# inside MAX_GRADE_SPAN and bracketing the target, so no pool-shape flag), and
# prices tight enough to survive the IQR trim.
_HEALTHY_POOL = [
    _ledger_row(1, price=40.0, grade=7.5, sold_date="2026-06-01"),
    _ledger_row(2, price=42.0, grade=7.5, sold_date="2026-06-08"),
    _ledger_row(3, price=45.0, grade=8.0, sold_date="2026-06-15"),
    _ledger_row(4, price=48.0, grade=8.0, sold_date="2026-06-22"),
    _ledger_row(5, price=50.0, grade=8.0, sold_date="2026-06-29"),
    _ledger_row(6, price=55.0, grade=8.0, sold_date="2026-07-06"),
    _ledger_row(7, price=58.0, grade=8.5, sold_date="2026-07-13"),
    _ledger_row(8, price=60.0, grade=8.5, sold_date="2026-07-20"),
]


def _all_tiers_failed(title="Fantastic Four", issue="46", year=1963, grade=8.0):
    """The BUI-536 provider-outage result shape: zero comps, every query
    tier carrying an `error`."""
    return {
        "input": {"title": title, "issue": issue, "year": year, "grade": grade},
        "comps": [],
        "queries_used": [{"tier": "base", "error": "provider 503"},
                         {"tier": "wide", "error": "provider 503"}],
    }


def _book(title="Fantastic Four", issue="46", year=1963, grade=8.0):
    return {"title": title, "issue": issue, "year": year, "grade": grade,
            "item_id": "12345"}


def _ledger_get(payload, *, status_ok=True):
    """A `requests.get` double returning one JSON payload — the transport
    boundary, and the only thing mocked. Everything above it is real."""
    resp = MagicMock()
    resp.json.return_value = payload
    if not status_ok:
        import requests
        resp.raise_for_status.side_effect = requests.exceptions.HTTPError("400")
    return MagicMock(return_value=resp)


def _price_with_ledger(payload, *, server_url, book=None, status_ok=True,
                       result=None):
    """Run the real degraded-mode path with the ledger returning `payload`."""
    book = book or _book()
    with patch("fmv_runner.requests.get", _ledger_get(payload,
                                                      status_ok=status_ok)), \
            patch("fmv_runner._upsert_fmv") as upsert_mock, \
            patch("fmv_runner._post_comps", return_value=None):
        out = fmv_runner._compute_and_upsert_one(
            result or _all_tiers_failed(), book, server_url=server_url)
    return out, upsert_mock


# ─── The happy path ───────────────────────────────────────────────────────────

class TestAdvisoryBandReplacesNothing:
    def test_a_fetch_err_book_gains_an_advisory_band(self, server_url):
        out, _ = _price_with_ledger(_HEALTHY_POOL, server_url=server_url)
        assert out["source"] == fmv_runner.SOURCE_LEDGER_ADVISORY
        assert out["source"] == "ledger-advisory"
        assert out["fmv"]["fmv_low"] is not None
        assert out["fmv"]["fmv_high"] is not None
        assert out["fmv"]["ledger_advisory"] is True
        assert out["ledger_rows"] == 8
        assert out["ledger_comic_id"] == 77

    def test_the_error_string_still_says_the_fetch_failed(self, server_url):
        """The advisory is an ADDITION to the fetch-err report, never a
        replacement: the fetch really did fail and the run must keep saying so.
        """
        out, _ = _price_with_ledger(_HEALTHY_POOL, server_url=server_url)
        assert "fetch-err" in out["error"]
        assert "ADVISORY" in out["error"]
        assert "NO max_bid" in out["error"]

    def test_the_row_still_counts_as_a_fetch_error(self, server_url):
        """`_is_fetch_error` must still claim it, so the book stays inside the
        loud BUI-143 warning and count rather than disappearing into a
        'priced' bucket."""
        out, _ = _price_with_ledger(_HEALTHY_POOL, server_url=server_url)
        assert fmv_runner._is_fetch_error(out) is True

    def test_queries_used_and_comp_count_are_the_failed_fetch_s(self, server_url):
        """`comp_count_total` counts the PROVIDER pool (zero — they failed),
        never the ledger rows; BUI-143's fetch-error signal keys off it."""
        out, _ = _price_with_ledger(_HEALTHY_POOL, server_url=server_url)
        assert out["comp_count_total"] == 0
        assert len(out["queries_used"]) == 2


# ─── The invariant: no bid cap, ever ──────────────────────────────────────────

class TestNoBidCapEverEscapes:
    def test_max_bid_is_none_on_the_band(self, server_url):
        out, _ = _price_with_ledger(_HEALTHY_POOL, server_url=server_url)
        assert out["fmv"]["max_bid"] is None

    def test_max_bid_is_none_even_though_the_math_computed_one(self, server_url):
        """Non-vacuity: prove the pool WOULD have produced a cap, so the None
        above is this feature withholding it rather than the math declining to
        price the pool at all."""
        import fmv_math
        comps = [{"price": r["price"], "grade": r["grade"],
                  "sold_date": r["sold_date"], "title": r["title"]}
                 for r in _HEALTHY_POOL]
        live = fmv_math.compute_fmv(comps, target_grade=8.0)
        assert live["flag_reason"] is None
        assert live["max_bid"] is not None and live["max_bid"] > 0

        out, _ = _price_with_ledger(_HEALTHY_POOL, server_url=server_url)
        assert out["fmv"]["fmv_high"] == live["fmv_high"]  # same math
        assert out["fmv"]["max_bid"] is None                # cap withheld

    def test_brief_projects_a_null_max_bid_and_the_advisory_source(self,
                                                                   server_url):
        """The `--brief` line is what `/comic:buy` Step 4 reads to propose a
        bid. `flag_reason` stays null here, so a consumer gating only on
        `flag_reason` would miss this row — `source` is the gate."""
        out, _ = _price_with_ledger(_HEALTHY_POOL, server_url=server_url)
        brief = fmv_runner._brief_row(out)
        assert brief["max_bid"] is None
        assert brief["flag_reason"] is None
        assert brief["source"] == "ledger-advisory"
        assert brief["comic_id"] is None
        assert brief["fmv_low"] is not None
        assert brief["fmv_notes"].startswith("LEDGER-ADVISORY")


# ─── The invariant: nothing is written ────────────────────────────────────────

class TestAdvisoryIsNeverPersisted:
    def test_no_upsert_is_attempted(self, server_url):
        out, upsert_mock = _price_with_ledger(_HEALTHY_POOL,
                                              server_url=server_url)
        upsert_mock.assert_not_called()
        assert out["db_row"] is None
        assert out["comic_id"] is None
        assert out["fmv_id"] is None

    def test_no_http_post_of_any_kind_is_attempted(self, server_url):
        """Broader than the mock above: no POST at all leaves the pricing
        path, so there is no route by which this number reaches the server —
        which is what makes it unable to be re-served later as `cached`."""
        with patch("fmv_runner.requests.get", _ledger_get(_HEALTHY_POOL)), \
                patch("fmv_runner.requests.post") as post_mock:
            out = fmv_runner._compute_and_upsert_one(
                _all_tiers_failed(), _book(), server_url=server_url)
        post_mock.assert_not_called()
        assert out["source"] == "ledger-advisory"

    def test_the_cgc_proxy_rescue_skips_an_advisory_row(self, server_url):
        out, _ = _price_with_ledger(_HEALTHY_POOL, server_url=server_url)
        assert fmv_runner._is_ledger_advisory(out) is True
        assert fmv_runner._is_unpriced_raw(out) is False
        # An advisory row always HAS an fmv_high today, so the line above
        # would hold even without the explicit source exclusion. Pin the
        # exclusion itself against a hypothetical unpriced advisory row: the
        # rescue's second fetch ends in `_upsert_fmv`, so "no advisory row,
        # ever" must not depend on how the band happened to come out.
        unpriced = {**out, "fmv": {**out["fmv"], "fmv_high": None}}
        assert fmv_runner._is_unpriced_raw(unpriced) is False
        assert fmv_runner._is_unpriced_raw(
            {**unpriced, "source": "fresh"}) is True  # control

    def test_the_cgc_cross_check_skips_an_advisory_row(self, server_url):
        """The real leak this guard closes: an advisory band is often exactly
        the thin/LOW shape the cross-check hunts for, and the cross-check ends
        in `_upsert_fmv(..., hard_fail=False)` — which, with `comic_id: None`,
        would MINT a comics row and persist the ledger-derived band."""
        out, _ = _price_with_ledger(
            _HEALTHY_POOL, server_url=server_url,
            book=_book(year=1963),  # vintage → passes _is_vintage
        )
        # Force the OTHER two predicates to claim it, so only the BUI-663
        # exclusion can keep it out of the candidate set.
        out["fmv"]["confidence"] = "LOW"
        assert fmv_runner._is_vintage(out) is True
        assert fmv_runner._is_thin_or_low_confidence_priced(out) is True

        fresh = {0: out}
        with patch("fmv_runner._upsert_fmv") as upsert_mock, \
                patch("fmv_runner._fetch_comps") as fetch_mock:
            fmv_runner._apply_cgc_cross_check(
                fresh, [_book()], server_url=server_url, force=False)
        upsert_mock.assert_not_called()
        fetch_mock.assert_not_called()
        assert fresh[0]["source"] == "ledger-advisory"


# ─── The gate: pool depth, not age ────────────────────────────────────────────

class TestPoolDepthFloor:
    def test_a_two_comp_ledger_pool_stays_a_plain_fetch_err(self, server_url):
        """BUI-663's measured gate: thinness, not age, is what makes a ledger
        price wrong. Two comps clears fmv_math's own `too_sparse` floor (2) but
        not this one (3)."""
        pool = [
            _ledger_row(1, price=45.0, grade=8.0, sold_date="2026-06-15"),
            _ledger_row(2, price=50.0, grade=8.0, sold_date="2026-06-29"),
        ]
        out, upsert_mock = _price_with_ledger(pool, server_url=server_url)
        assert out["source"] == "error"
        assert out["fmv"] is None
        upsert_mock.assert_not_called()

    def test_three_comps_is_enough(self, server_url):
        """The floor is a floor, not a wall — pin the boundary from both
        sides so a silent off-by-one can't pass."""
        pool = [
            _ledger_row(1, price=45.0, grade=8.0, sold_date="2026-06-15"),
            _ledger_row(2, price=50.0, grade=8.0, sold_date="2026-06-29"),
            _ledger_row(3, price=55.0, grade=8.0, sold_date="2026-07-06"),
        ]
        out, _ = _price_with_ledger(pool, server_url=server_url)
        assert out["source"] == "ledger-advisory"
        assert out["fmv"]["n"] >= fmv_runner.LEDGER_ADVISORY_MIN_POOL

    def test_a_pool_shape_flag_stays_a_plain_fetch_err(self, server_url):
        """A ledger pool that `compute_fmv` flags needs-manual emits no band:
        the ticket's 'what happens when the ledger is also thin' answered
        through the pipeline's existing guard, not a new one. Here the comps
        are all ABOVE the target grade → `one_sided`."""
        pool = [
            _ledger_row(i, price=40.0 + i, grade=9.5, sold_date="2026-06-15")
            for i in range(1, 7)
        ]
        out, _ = _price_with_ledger(
            pool, server_url=server_url, book=_book(grade=6.0),
            result=_all_tiers_failed(grade=6.0))
        assert out["source"] == "error"
        assert out["fmv"] is None

    def test_rows_with_null_price_or_grade_do_not_count_toward_the_floor(
            self, server_url):
        """The ledger's `price`/`grade` columns are nullable. Three rows on the
        wire, one usable pair — must not price."""
        pool = [
            _ledger_row(1, price=45.0, grade=8.0, sold_date="2026-06-15"),
            _ledger_row(2, price=None, grade=8.0, sold_date="2026-06-29"),
            _ledger_row(3, price=50.0, grade=None, sold_date="2026-07-06"),
        ]
        out, _ = _price_with_ledger(pool, server_url=server_url)
        assert out["source"] == "error"


# ─── Failure modes of the ledger read itself ──────────────────────────────────

class TestLedgerReadFailuresDegradeToFetchErr:
    def test_transport_failure(self, server_url):
        import requests
        with patch("fmv_runner.requests.get",
                   side_effect=requests.exceptions.ConnectionError("down")), \
                patch("fmv_runner._upsert_fmv") as upsert_mock:
            out = fmv_runner._compute_and_upsert_one(
                _all_tiers_failed(), _book(), server_url=server_url)
        upsert_mock.assert_not_called()
        assert out["source"] == "error"
        assert out["fmv"] is None

    def test_http_400_unresolvable_identity(self, server_url):
        """The endpoint 400s a book it cannot resolve. That must degrade to the
        honest fetch-err, never to a band priced off somebody else's comps."""
        out, _ = _price_with_ledger([], server_url=server_url, status_ok=False)
        assert out["source"] == "error"
        assert out["fmv"] is None

    def test_empty_ledger(self, server_url):
        out, _ = _price_with_ledger([], server_url=server_url)
        assert out["source"] == "error"

    def test_non_list_body(self, server_url):
        """A JSON object where a list was promised (a server-shape change)
        must not be indexed into — degrade instead."""
        out, _ = _price_with_ledger({"detail": "nope"}, server_url=server_url)
        assert out["source"] == "error"

    def test_a_book_with_no_title_never_even_reads_the_ledger(self, server_url):
        with patch("fmv_runner.requests.get") as get_mock:
            fmv_runner._fetch_ledger_comps(
                server_url, title=None, issue="46", year=1963)
            fmv_runner._fetch_ledger_comps(
                server_url, title="X-Men", issue=None, year=1963)
        get_mock.assert_not_called()


# ─── The wire contract of the read ────────────────────────────────────────────

class TestLedgerReadWireContract:
    def _captured_params(self, server_url, **kwargs):
        get_mock = _ledger_get(_HEALTHY_POOL)
        with patch("fmv_runner.requests.get", get_mock):
            fmv_runner._fetch_ledger_comps(server_url, **kwargs)
        return get_mock.call_args

    def test_reads_the_raw_pool_only(self, server_url):
        """Not optional: the ledger also stores the graded/slab pool, whose
        prices are multiples of raw ones. An unfiltered read would overstate a
        raw book's value by an order of magnitude, silently."""
        args = self._captured_params(server_url, title="Fantastic Four",
                                     issue="46", year=1963)
        assert args.kwargs["params"]["pool"] == "raw"

    def test_sends_no_days_cutoff(self, server_url):
        """BUI-663's measured conclusion, pinned as a test: no staleness rule.
        A 365d/730d cutoff moved 0 of 483 pools and a 90d cutoff moved 3, all
        UPWARD — a rule that costs money to be right about identity. If someone
        adds `days` here, this fails and they read the ticket."""
        args = self._captured_params(server_url, title="Fantastic Four",
                                     issue="46", year=1963)
        assert "days" not in args.kwargs["params"]

    def test_sends_no_grade_filter(self, server_url):
        """`build_pool` does its own progressive ±window widening and needs the
        neighbouring grades to do it."""
        args = self._captured_params(server_url, title="Fantastic Four",
                                     issue="46", year=1963)
        assert "grade" not in args.kwargs["params"]

    def test_hits_the_comps_endpoint_with_wire_typed_params(self, server_url):
        """Assert the wire TYPES, not an equality between two mocks: `issue` is
        a string on this endpoint (the `comics.issue` column is TEXT), `year`
        an int, `limit` an int."""
        args = self._captured_params(server_url, title="Fantastic Four",
                                     issue=46, year=1963)
        assert args.args[0] == f"{server_url}/api/comics/comps"
        params = args.kwargs["params"]
        assert isinstance(params["title"], str)
        assert isinstance(params["issue"], str) and params["issue"] == "46"
        assert isinstance(params["year"], int)
        assert isinstance(params["limit"], int)
        assert params["limit"] == fmv_runner.LEDGER_ADVISORY_READ_LIMIT
        # Round-trips as JSON — no non-serializable object rides along.
        json.dumps(params)

    def test_uses_a_short_timeout(self, server_url):
        """In a DUAL outage (providers down AND the comics server unreachable)
        this read happens once per failed book, so the 15s default would add
        15s x N of dead wait to a batch already returning nothing."""
        args = self._captured_params(server_url, title="Fantastic Four",
                                     issue="46", year=1963)
        assert args.kwargs["timeout"] == fmv_runner.LEDGER_ADVISORY_TIMEOUT_SECONDS
        assert fmv_runner.LEDGER_ADVISORY_TIMEOUT_SECONDS < 15

    def test_a_book_with_no_year_sends_no_year(self, server_url):
        """`requests` drops a None-valued param, so the server falls back to a
        title+issue match. Pinned because it is the identity-precision
        trade-off this feature knowingly accepts (advisory only)."""
        args = self._captured_params(server_url, title="Fantastic Four",
                                     issue="46", year=None)
        assert args.kwargs["params"]["year"] is None


# ─── Labelling, at every surface ──────────────────────────────────────────────

class TestSurfacesLabelTheRow:
    def test_table_renders_a_distinct_band_and_no_bid_number(self, server_url,
                                                             capsys):
        out, _ = _price_with_ledger(_HEALTHY_POOL, server_url=server_url)
        fmv_runner._print_table([out])
        printed = capsys.readouterr().out
        assert "LEDGER" in printed
        assert "advisory" in printed
        assert "ledger-advisory" in printed  # the Source column
        assert "$?" not in printed  # never a blank-looking cap

    def test_notes_lead_with_the_disclaimer(self, server_url):
        out, _ = _price_with_ledger(_HEALTHY_POOL, server_url=server_url)
        notes = fmv_runner._build_notes(out["fmv"])
        assert notes.startswith("LEDGER-ADVISORY")
        assert "no bid cap" in notes

    def test_the_whole_row_survives_the_out_json_dump(self, server_url):
        """`--out` is `json.dump(final, ...)`. A non-serializable value on the
        advisory row would take down the write for the WHOLE batch, not just
        this book."""
        out, _ = _price_with_ledger(_HEALTHY_POOL, server_url=server_url)
        round_tripped = json.loads(json.dumps(out))
        assert round_tripped["source"] == "ledger-advisory"
        assert round_tripped["fmv"]["max_bid"] is None

    def test_a_live_row_is_never_labelled(self, server_url):
        """The label must not leak onto ordinary rows — otherwise it stops
        meaning anything."""
        import fmv_math
        comps = [{"price": r["price"], "grade": r["grade"],
                  "sold_date": r["sold_date"], "title": r["title"]}
                 for r in _HEALTHY_POOL]
        live = fmv_math.compute_fmv(comps, target_grade=8.0)
        live["first_party_count"] = 0
        assert not live.get("ledger_advisory")
        assert "LEDGER-ADVISORY" not in fmv_runner._build_notes(live)


class TestRunLevelReporting:
    def _run_one_advisory_book(self, tmp_path, server_url, quiet):
        batch = tmp_path / "batch.json"
        batch.write_text(json.dumps([_book()]))
        fetched = [{**_all_tiers_failed(), "input": {
            **_all_tiers_failed()["input"], "_req_id": 0}}]
        with patch("fmv_runner._fetch_comps", return_value=fetched), \
                patch("fmv_runner._split_by_db_cache",
                      return_value=({}, [{"_idx": 0, **_book()}], {}, {}, {})), \
                patch("fmv_runner.requests.get", _ledger_get(_HEALTHY_POOL)), \
                patch("fmv_runner._upsert_fmv") as upsert_mock:
            fmv_runner.run(batch_path=str(batch), out_path=None,
                           max_age_days=7, server_url=server_url, force=False,
                           quiet=quiet, brief=False, grade_window=None)
        return upsert_mock

    def test_advisory_count_is_reported_even_under_quiet(self, tmp_path,
                                                         server_url, capsys):
        """Unconditional, like the three BUI-533/544/639 skip counts and for
        the same reason: an operator reading only `--brief` must still learn
        this happened."""
        upsert_mock = self._run_one_advisory_book(tmp_path, server_url,
                                                  quiet=True)
        err = capsys.readouterr().err
        assert "LEDGER ADVISORY" in err
        assert "NO max_bid" in err
        upsert_mock.assert_not_called()

    def test_no_advisory_line_when_nothing_was_advisory(self, tmp_path,
                                                        server_url, capsys):
        batch = tmp_path / "batch.json"
        batch.write_text(json.dumps([_book()]))
        fetched = [{**_all_tiers_failed(), "input": {
            **_all_tiers_failed()["input"], "_req_id": 0}}]
        import requests
        with patch("fmv_runner._fetch_comps", return_value=fetched), \
                patch("fmv_runner._split_by_db_cache",
                      return_value=({}, [{"_idx": 0, **_book()}], {}, {}, {})), \
                patch("fmv_runner.requests.get",
                      side_effect=requests.exceptions.ConnectionError("down")):
            fmv_runner.run(batch_path=str(batch), out_path=None,
                           max_age_days=7, server_url=server_url, force=False,
                           quiet=True, brief=False, grade_window=None)
        assert "LEDGER ADVISORY" not in capsys.readouterr().err
