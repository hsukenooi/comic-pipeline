"""Unit tests for add_batch.py (BUI-360) — the BUI-168 mid-batch failure
semantics as pure logic, independent of the CLI wiring (see
tests/test_cli_add_batch.py for that). No network is ever touched: every
`server_request` here is a hand-rolled fake honoring the same
(ok, data, error) contract as cli._server_request_result.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from add_batch import (
    AddBatchError,
    STATUS_ADDED,
    STATUS_FAILED,
    STATUS_NOT_ATTEMPTED,
    STATUS_UPDATED,
    BatchOutcome,
    RowResult,
    add_one_row,
    apply_verify_results,
    build_bid_payload,
    created_from_response,
    parse_rows,
    run_batch,
    verify_items,
)


def _row(item_id="111", max_bid=100, **kwargs):
    d = {"item_id": item_id, "max_bid": max_bid}
    d.update(kwargs)
    return d


class _FakeServer:
    """Scriptable fake for the `server_request` callable. `responses` is a
    dict keyed by (method, path) -> either a fixed (ok, data, err) tuple or
    a list of such tuples consumed in order (for repeat calls to the same
    endpoint, e.g. /health across a multi-row batch)."""

    def __init__(self, responses: dict):
        self._responses = {k: list(v) if isinstance(v, list) else [v] for k, v in responses.items()}
        self.calls = []

    def __call__(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs.get("json")))
        key = (method, path)
        queue = self._responses.get(key)
        if not queue:
            raise AssertionError(f"no fake response queued for {key}")
        return queue[0] if len(queue) == 1 else queue.pop(0)


# ---------------------------------------------------------------------------
# parse_rows
# ---------------------------------------------------------------------------


def test_parse_rows_bare_list():
    rows = parse_rows([_row("1"), _row("2")])
    assert [r["item_id"] for r in rows] == ["1", "2"]


def test_parse_rows_object_with_rows_key():
    rows = parse_rows({"rows": [_row("1")]})
    assert len(rows) == 1


def test_parse_rows_rejects_non_list():
    with pytest.raises(AddBatchError, match="JSON list"):
        parse_rows({"not_rows": []})


def test_parse_rows_rejects_non_object_row():
    with pytest.raises(AddBatchError, match="row 1"):
        parse_rows([_row("1"), "not-an-object"])


def test_parse_rows_rejects_duplicate_item_id():
    """The server upserts on item_id (BUI-67) — two rows for the same
    item_id would silently collapse into one bid while both rows are
    reported as independently landed. Must hard-stop before any row is
    attempted, not just warn."""
    with pytest.raises(AddBatchError, match=r"duplicate item_id.*111"):
        parse_rows([_row("111", max_bid=10), _row("111", max_bid=20)])


def test_parse_rows_allows_repeated_missing_item_id():
    """A row missing item_id entirely is a per-row validation failure in
    add_one_row, not a parse_rows structural error — multiple such rows
    must not be mistaken for "duplicate item_id"."""
    rows = parse_rows([{"max_bid": 10}, {"max_bid": 20}])
    assert len(rows) == 2


def test_parse_rows_distinct_item_ids_ok():
    rows = parse_rows([_row("1"), _row("2"), _row("3")])
    assert len(rows) == 3


# ---------------------------------------------------------------------------
# build_bid_payload / created_from_response — shared with cli.py's `add`
# ---------------------------------------------------------------------------


def test_build_bid_payload_minimal():
    payload = build_bid_payload("1", 100, 6, 0)
    assert payload == {"item_id": "1", "max_bid": 100.0, "bid_offset": 6, "snipe_group": 0}


def test_build_bid_payload_omits_unset_optional_fields():
    payload = build_bid_payload("1", 100, 6, 0, seller="X")
    assert "seller_grade" not in payload
    assert "photo_grade" not in payload
    assert payload["seller"] == "X"


def test_build_bid_payload_includes_all_optional_fields_when_given():
    payload = build_bid_payload("1", 100, 6, 0, seller="X", seller_grade=9.0, photo_grade=8.5)
    assert payload == {
        "item_id": "1", "max_bid": 100.0, "bid_offset": 6, "snipe_group": 0,
        "seller": "X", "seller_grade": 9.0, "photo_grade": 8.5,
    }


def test_created_from_response_defaults_true_when_key_missing():
    assert created_from_response({}) is True


def test_created_from_response_respects_explicit_false():
    assert created_from_response({"created": False}) is False


def test_created_from_response_true_for_non_dict():
    assert created_from_response(None) is True


# ---------------------------------------------------------------------------
# add_one_row — validation (no network)
# ---------------------------------------------------------------------------


def test_add_one_row_missing_item_id_fails_without_network():
    server = _FakeServer({})
    result = add_one_row({"max_bid": 10}, server_request=server)
    assert result.status == STATUS_FAILED
    assert "item_id" in result.error
    assert server.calls == []


def test_add_one_row_missing_max_bid_fails_without_network():
    server = _FakeServer({})
    result = add_one_row({"item_id": "1"}, server_request=server)
    assert result.status == STATUS_FAILED
    assert "max_bid" in result.error
    assert server.calls == []


def test_add_one_row_invalid_max_bid_fails_without_network():
    server = _FakeServer({})
    result = add_one_row(_row(max_bid="not-a-number"), server_request=server)
    assert result.status == STATUS_FAILED
    assert "max_bid" in result.error
    assert server.calls == []


def test_add_one_row_invalid_grade_fails_without_network():
    server = _FakeServer({})
    result = add_one_row(_row(grade="NM"), server_request=server)
    assert result.status == STATUS_FAILED
    assert "grade" in result.error
    assert server.calls == []


@pytest.mark.parametrize("bad_max_bid", [float("nan"), float("inf"), float("-inf")])
def test_add_one_row_rejects_non_finite_max_bid_without_network(bad_max_bid):
    """NaN/Infinity pass Decimal()/float() conversion without error and can
    bypass a naive server-side `v <= 0` positivity check (NaN compares False
    either way under IEEE-754) — must be rejected client-side before
    reaching a real-money bid field."""
    server = _FakeServer({})
    result = add_one_row(_row(max_bid=bad_max_bid), server_request=server)
    assert result.status == STATUS_FAILED
    assert "max_bid" in result.error
    assert "not finite" in result.error
    assert server.calls == []


@pytest.mark.parametrize("field", ["grade", "seller_grade", "photo_grade"])
@pytest.mark.parametrize("bad_value", [float("nan"), float("inf"), float("-inf")])
def test_add_one_row_rejects_non_finite_optional_floats_without_network(field, bad_value):
    server = _FakeServer({})
    result = add_one_row(_row(**{field: bad_value}), server_request=server)
    assert result.status == STATUS_FAILED
    assert field in result.error
    assert server.calls == []


@pytest.mark.parametrize("field", ["offset", "group", "comic_id"])
def test_add_one_row_invalid_int_fields_fail_without_network(field):
    """Sibling coverage to the max_bid/grade validation tests above — the
    int-coerced fields (offset, group, comic_id) get the same treatment."""
    server = _FakeServer({})
    result = add_one_row(_row(**{field: "not-a-number"}), server_request=server)
    assert result.status == STATUS_FAILED
    assert field in result.error
    assert server.calls == []


# ---------------------------------------------------------------------------
# add_one_row — happy paths
# ---------------------------------------------------------------------------


def test_add_one_row_minimal_success():
    server = _FakeServer({("post", "/api/bids"): (True, {"item_id": "1", "created": True}, None)})
    result = add_one_row(_row("1"), server_request=server)
    assert result.status == STATUS_ADDED
    assert result.max_bid == 100.0
    assert result.link_attempted is False
    assert server.calls == [("post", "/api/bids", {
        "item_id": "1", "max_bid": 100.0, "bid_offset": 6, "snipe_group": 0,
    })]


def test_add_one_row_updated_when_created_false():
    server = _FakeServer({("post", "/api/bids"): (True, {"item_id": "1", "created": False}, None)})
    result = add_one_row(_row("1"), server_request=server)
    assert result.status == STATUS_UPDATED


def test_add_one_row_passes_seller_and_grade_fields():
    server = _FakeServer({("post", "/api/bids"): (True, {"item_id": "1", "created": True}, None)})
    result = add_one_row(
        _row("1", seller="SomeSeller", seller_grade=9.0, photo_grade=8.5, offset=10, group=2),
        server_request=server,
    )
    assert result.status == STATUS_ADDED
    _, _, payload = server.calls[0]
    assert payload == {
        "item_id": "1", "max_bid": 100.0, "bid_offset": 10, "snipe_group": 2,
        "seller": "SomeSeller", "seller_grade": 9.0, "photo_grade": 8.5,
    }


def test_add_one_row_links_fmv_when_grade_and_comic_id_present():
    server = _FakeServer({
        ("post", "/api/bids"): (True, {"item_id": "1", "created": True}, None),
        ("post", "/api/bids/1/link-fmv"): (True, {}, None),
    })
    result = add_one_row(_row("1", comic_id=187, grade=9.2), server_request=server)
    assert result.status == STATUS_ADDED
    assert result.link_attempted is True
    assert result.link_ok is True
    link_calls = [c for c in server.calls if c[1].endswith("link-fmv")]
    assert link_calls == [("post", "/api/bids/1/link-fmv", {"comic_id": 187, "grade": 9.2})]


def test_add_one_row_no_link_when_grade_missing():
    server = _FakeServer({("post", "/api/bids"): (True, {"item_id": "1", "created": True}, None)})
    result = add_one_row(_row("1", comic_id=187), server_request=server)
    assert result.link_attempted is False
    assert not any(c[1].endswith("link-fmv") for c in server.calls)


def test_add_one_row_no_link_when_comic_id_missing():
    server = _FakeServer({("post", "/api/bids"): (True, {"item_id": "1", "created": True}, None)})
    result = add_one_row(_row("1", grade=9.2), server_request=server)
    assert result.link_attempted is False


def test_add_one_row_link_failure_does_not_demote_status():
    """A link-fmv failure must not turn an otherwise-successful add into a
    FAILED row — matches `gixen add`'s single-item behavior of still
    exiting 0 when only the link call fails."""
    server = _FakeServer({
        ("post", "/api/bids"): (True, {"item_id": "1", "created": True}, None),
        ("post", "/api/bids/1/link-fmv"): (False, None, "Server returned 500: boom"),
    })
    result = add_one_row(_row("1", comic_id=187, grade=9.2), server_request=server)
    assert result.status == STATUS_ADDED
    assert result.link_ok is False
    assert result.error is None  # add-failure field must stay clean
    assert "Server returned 500: boom" in result.link_error


# ---------------------------------------------------------------------------
# add_one_row — add failure
# ---------------------------------------------------------------------------


def test_add_one_row_server_failure_marks_failed_with_error_text():
    server = _FakeServer({("post", "/api/bids"): (False, None, "Server returned 503: gixen down")})
    result = add_one_row(_row("1"), server_request=server)
    assert result.status == STATUS_FAILED
    assert result.error == "Server returned 503: gixen down"
    assert result.max_bid == 100.0  # preserved even on failure, for the human table


# ---------------------------------------------------------------------------
# run_batch — sequential ordering + BUI-168 halt semantics
# ---------------------------------------------------------------------------


def test_run_batch_all_success_runs_in_order():
    server = _FakeServer({
        ("post", "/api/bids"): [
            (True, {"item_id": "1", "created": True}, None),
            (True, {"item_id": "2", "created": True}, None),
            (True, {"item_id": "3", "created": True}, None),
        ],
    })
    rows = [_row("1"), _row("2"), _row("3")]
    outcome = run_batch(rows, server_request=server, health_check=lambda: True)

    assert [r.item_id for r in outcome.rows] == ["1", "2", "3"]
    assert all(r.status == STATUS_ADDED for r in outcome.rows)
    assert outcome.halted is False
    assert outcome.exit_code() == 0
    # add calls happened strictly in row order
    add_calls = [c for c in server.calls if c[1] == "/api/bids"]
    assert [c[2]["item_id"] for c in add_calls] == ["1", "2", "3"]


def test_run_batch_marks_failed_row_and_continues_when_server_healthy():
    server = _FakeServer({
        ("post", "/api/bids"): [
            (True, {"item_id": "1", "created": True}, None),
            (False, None, "Server returned 500: boom"),
            (True, {"item_id": "3", "created": True}, None),
        ],
    })
    health_calls = []

    def health_check():
        health_calls.append(1)
        return True  # server still up after the mid-batch failure

    rows = [_row("1"), _row("2"), _row("3")]
    outcome = run_batch(rows, server_request=server, health_check=health_check)

    statuses = [r.status for r in outcome.rows]
    assert statuses == [STATUS_ADDED, STATUS_FAILED, STATUS_ADDED]
    assert outcome.rows[1].error == "Server returned 500: boom"
    assert outcome.halted is False
    assert outcome.exit_code() == 1  # any failure -> non-zero, even though batch continued
    # health was (re-)checked exactly once, after the failure
    assert len(health_calls) == 1


def test_run_batch_halts_and_reports_not_attempted_when_server_down():
    server = _FakeServer({
        ("post", "/api/bids"): [
            (True, {"item_id": "1", "created": True}, None),
            (False, None, "Server unreachable. Is the comics server running?"),
        ],
    })
    rows = [_row("1"), _row("2"), _row("3"), _row("4")]
    outcome = run_batch(rows, server_request=server, health_check=lambda: False)

    statuses = [r.status for r in outcome.rows]
    assert statuses == [STATUS_ADDED, STATUS_FAILED, STATUS_NOT_ATTEMPTED, STATUS_NOT_ATTEMPTED]
    assert outcome.halted is True
    assert outcome.exit_code() == 1
    # rows 3 and 4 never triggered a network call at all
    add_calls = [c for c in server.calls if c[1] == "/api/bids"]
    assert len(add_calls) == 2


def test_run_batch_never_calls_health_check_when_nothing_fails():
    server = _FakeServer({("post", "/api/bids"): (True, {"item_id": "1", "created": True}, None)})
    health_calls = []
    outcome = run_batch([_row("1")], server_request=server, health_check=lambda: health_calls.append(1) or True)
    assert health_calls == []
    assert outcome.exit_code() == 0


# ---------------------------------------------------------------------------
# BatchOutcome.summary / exit_code / to_dict
# ---------------------------------------------------------------------------


def test_batch_outcome_summary_counts_each_status():
    outcome = BatchOutcome(rows=[
        RowResult(item_id="1", status=STATUS_ADDED),
        RowResult(item_id="2", status=STATUS_UPDATED),
        RowResult(item_id="3", status=STATUS_FAILED, error="boom"),
        RowResult(item_id="4", status=STATUS_NOT_ATTEMPTED),
    ])
    summary = outcome.summary()
    assert summary == {
        "total": 4, STATUS_ADDED: 1, STATUS_UPDATED: 1,
        STATUS_FAILED: 1, STATUS_NOT_ATTEMPTED: 1,
    }


def test_batch_outcome_exit_code_zero_when_all_landed():
    outcome = BatchOutcome(rows=[
        RowResult(item_id="1", status=STATUS_ADDED),
        RowResult(item_id="2", status=STATUS_UPDATED),
    ])
    assert outcome.exit_code() == 0


def test_batch_outcome_exit_code_nonzero_on_any_failure():
    outcome = BatchOutcome(rows=[
        RowResult(item_id="1", status=STATUS_ADDED),
        RowResult(item_id="2", status=STATUS_FAILED, error="boom"),
    ])
    assert outcome.exit_code() == 1


def test_batch_outcome_exit_code_nonzero_on_not_attempted_alone():
    """A halted batch's not-attempted rows are just as much a non-success as
    a failed row — must not report exit 0 just because nothing errored."""
    outcome = BatchOutcome(rows=[
        RowResult(item_id="1", status=STATUS_ADDED),
        RowResult(item_id="2", status=STATUS_NOT_ATTEMPTED),
    ], halted=True)
    assert outcome.exit_code() == 1


def test_batch_outcome_to_dict_shape():
    outcome = BatchOutcome(rows=[RowResult(item_id="1", status=STATUS_ADDED, max_bid=10.0, grade=9.0)])
    d = outcome.to_dict()
    assert set(d.keys()) == {"summary", "halted", "verify_error", "rows"}
    assert d["rows"][0]["item_id"] == "1"
    assert d["rows"][0]["status"] == STATUS_ADDED


# ---------------------------------------------------------------------------
# verify_items / apply_verify_results — --verify wiring
# ---------------------------------------------------------------------------


def test_verify_items_only_includes_landed_rows_with_a_grade():
    outcome = BatchOutcome(rows=[
        RowResult(item_id="1", status=STATUS_ADDED, grade=9.2),
        RowResult(item_id="2", status=STATUS_ADDED, grade=None),  # no grade -> excluded
        RowResult(item_id="3", status=STATUS_FAILED, grade=9.0),  # not landed -> excluded
        RowResult(item_id="4", status=STATUS_UPDATED, grade=8.0),
    ])
    items = verify_items(outcome)
    assert items == [
        {"item_id": "1", "grade": 9.2},
        {"item_id": "4", "grade": 8.0},
    ]


def test_apply_verify_results_splices_verdict_onto_matching_row():
    outcome = BatchOutcome(rows=[
        RowResult(item_id="1", status=STATUS_ADDED, grade=9.2),
        RowResult(item_id="2", status=STATUS_ADDED, grade=8.0),
    ])
    verify_response = {
        "summary": {"total": 2, "fully_linked": 1, "issues": 1},
        "results": [
            {"item_id": "1", "verdict": "fully_linked"},
            {"item_id": "2", "verdict": "fmv_stub", "missing": ["fmv.low", "fmv.high"]},
        ],
    }
    apply_verify_results(outcome, verify_response)
    assert outcome.rows[0].verify == {"item_id": "1", "verdict": "fully_linked"}
    assert outcome.rows[1].verify["verdict"] == "fmv_stub"


def test_apply_verify_results_leaves_unmatched_row_verify_none():
    outcome = BatchOutcome(rows=[RowResult(item_id="1", status=STATUS_ADDED, grade=9.2)])
    apply_verify_results(outcome, {"summary": {}, "results": []})
    assert outcome.rows[0].verify is None
