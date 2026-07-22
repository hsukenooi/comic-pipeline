"""Unit tests for add_batch.py (BUI-360) — the BUI-168 mid-batch failure
semantics as pure logic, independent of the CLI wiring (see
tests/test_cli_add_batch.py for that). No network is ever touched: every
`server_request` here is a hand-rolled fake honoring the same
(ok, data, error) contract as cli._server_request_result.
"""

import json
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
    build_batch_rows,
    build_bid_payload,
    created_from_response,
    parse_brief_rows,
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


def test_add_one_row_carries_title_through_to_result(): # BUI-506
    server = _FakeServer({("post", "/api/bids"): (True, {"item_id": "1", "created": True}, None)})
    result = add_one_row(_row("1", title="Invincible #1"), server_request=server)
    assert result.title == "Invincible #1"
    assert result.to_dict()["title"] == "Invincible #1"
    # display-only: never part of the POST /api/bids payload
    assert "title" not in server.calls[0][2]


def test_add_one_row_absent_title_defaults_to_none_backward_compatible():
    server = _FakeServer({("post", "/api/bids"): (True, {"item_id": "1", "created": True}, None)})
    result = add_one_row(_row("1"), server_request=server)
    assert result.title is None


def test_add_one_row_validation_failure_still_carries_title():
    # A row that fails validation before any network call must still surface
    # its title, so a failed-row table entry is legible.
    result = add_one_row({"item_id": "1", "title": "Invincible #1"}, server_request=_FakeServer({}))
    assert result.status == STATUS_FAILED
    assert result.title == "Invincible #1"


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


def test_run_batch_not_attempted_rows_still_carry_title():
    server = _FakeServer({
        ("post", "/api/bids"): [
            (True, {"item_id": "1", "created": True}, None),
            (False, None, "Server unreachable. Is the comics server running?"),
        ],
    })
    rows = [_row("1", title="First"), _row("2", title="Second"), _row("3", title="Third")]
    outcome = run_batch(rows, server_request=server, health_check=lambda: False)
    assert [r.title for r in outcome.rows] == ["First", "Second", "Third"]
    assert outcome.rows[2].status == STATUS_NOT_ATTEMPTED


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


# ---------------------------------------------------------------------------
# parse_brief_rows (BUI-435)
# ---------------------------------------------------------------------------


def _brief(item_id="1", comic_id=42, max_bid=100, flag_reason=None):
    return {
        "item_id": item_id,
        "comic_id": comic_id,
        "fmv_id": 7,
        "max_bid": max_bid,
        "flag_reason": flag_reason,
        "confidence": "HIGH",
    }


def test_parse_brief_rows_bare_list():
    rows = [_brief("1"), _brief("2")]
    assert parse_brief_rows(rows) == rows


def test_parse_brief_rows_object_with_rows_key():
    rows = [_brief("1")]
    assert parse_brief_rows({"rows": rows}) == rows


def test_parse_brief_rows_object_with_brief_key():
    rows = [_brief("1")]
    assert parse_brief_rows({"brief": rows}) == rows


def test_parse_brief_rows_object_missing_rows_key_errors():
    with pytest.raises(AddBatchError):
        parse_brief_rows({"other": []})


def test_parse_brief_rows_clean_json_string():
    rows = [_brief("1"), _brief("2")]
    raw = json.dumps(rows)
    assert parse_brief_rows(raw) == rows


def test_parse_brief_rows_extracts_json_lines_from_mixed_stdout():
    raw = (
        "#   Comic                FMV Range      Max Bid\n"
        "1   Amazing Spider-Man   $800-1000      $800\n"
        + json.dumps(_brief("111")) + "\n"
        + json.dumps(_brief("222")) + "\n"
    )
    rows = parse_brief_rows(raw)
    assert [r["item_id"] for r in rows] == ["111", "222"]


def test_parse_brief_rows_truncated_json_line_is_hard_error():
    raw = '{"item_id": "111", "comic_id": 42, "max_bid": 800\n'  # missing closing brace
    with pytest.raises(AddBatchError):
        parse_brief_rows(raw)


def test_parse_brief_rows_no_json_lines_found_errors():
    raw = "just a human table\nwith no json in it\n"
    with pytest.raises(AddBatchError):
        parse_brief_rows(raw)


def test_parse_brief_rows_duplicate_item_id_errors():
    with pytest.raises(AddBatchError):
        parse_brief_rows([_brief("1"), _brief("1")])


def test_parse_brief_rows_missing_item_id_errors():
    with pytest.raises(AddBatchError):
        parse_brief_rows([{"comic_id": 1, "max_bid": 10}])


def test_parse_brief_rows_row_not_a_dict_errors():
    with pytest.raises(AddBatchError):
        parse_brief_rows(["not a dict"])


def test_parse_brief_rows_rejects_unusable_input_type():
    with pytest.raises(AddBatchError):
        parse_brief_rows(12345)


# ---------------------------------------------------------------------------
# build_batch_rows (BUI-435)
# ---------------------------------------------------------------------------


def _wl_row(item_id="1", **kwargs):
    d = {"item_id": item_id}
    d.update(kwargs)
    return d


def test_build_batch_rows_happy_path_merges_all_three_sources():
    brief = [_brief("1", comic_id=42, max_bid=800)]
    working_list = [_wl_row("1", grade=9.2, seller="tuners36", seller_grade=9.0, photo_grade=8.5)]
    result = build_batch_rows(brief, working_list)
    assert result.rows == [{
        "item_id": "1",
        "max_bid": 800.0,
        "comic_id": 42,
        "grade": 9.2,
        "seller": "tuners36",
        "seller_grade": 9.0,
        "photo_grade": 8.5,
    }]
    assert result.skipped == []
    assert result.unlinked == []


# ---------------------------------------------------------------------------
# build_batch_rows — title threading (BUI-506)
# ---------------------------------------------------------------------------


def test_build_batch_rows_carries_title_from_working_list():
    brief = [_brief("1", comic_id=42, max_bid=800)]
    working_list = [_wl_row("1", grade=9.2, title="Invincible #1")]
    result = build_batch_rows(brief, working_list)
    assert result.rows[0]["title"] == "Invincible #1"


def test_build_batch_rows_absent_title_omits_key_backward_compatible():
    """No `title` on the working-list row must produce exactly the same
    output row as before this field existed — no null placeholder key."""
    brief = [_brief("1", comic_id=None, max_bid=50)]
    working_list = [_wl_row("1")]
    result = build_batch_rows(brief, working_list)
    assert "title" not in result.rows[0]
    assert result.rows[0] == {"item_id": "1", "max_bid": 50.0}


def test_build_batch_rows_never_drops_comic_id_when_present():
    brief = [_brief("1", comic_id=999, max_bid=50)]
    working_list = [_wl_row("1", grade=9.4)]
    result = build_batch_rows(brief, working_list)
    assert result.rows[0]["comic_id"] == 999


def test_build_batch_rows_letter_grade_is_coerced_to_cgc_float():
    brief = [_brief("1", max_bid=50)]
    working_list = [_wl_row("1", grade="NM-")]
    result = build_batch_rows(brief, working_list)
    assert result.rows[0]["grade"] == 9.2


def test_build_batch_rows_override_max_bid_wins_over_brief():
    brief = [_brief("1", max_bid=800)]
    working_list = [_wl_row("1")]
    result = build_batch_rows(brief, working_list, overrides={"1": {"max_bid": 650}})
    assert result.rows[0]["max_bid"] == 650.0


def test_build_batch_rows_override_group_wins_over_working_list_default():
    brief = [_brief("1", max_bid=50)]
    working_list = [_wl_row("1", group=3)]
    result = build_batch_rows(brief, working_list, overrides={"1": {"group": 7}})
    assert result.rows[0]["group"] == 7


def test_build_batch_rows_working_list_group_default_passes_through():
    brief = [_brief("1", max_bid=50)]
    working_list = [_wl_row("1", group=2)]
    result = build_batch_rows(brief, working_list)
    assert result.rows[0]["group"] == 2


def test_build_batch_rows_zero_group_omitted_from_output():
    brief = [_brief("1", max_bid=50)]
    working_list = [_wl_row("1")]
    result = build_batch_rows(brief, working_list)
    assert "group" not in result.rows[0]


def test_build_batch_rows_skips_bin_listing_type_field():
    brief = [_brief("1", max_bid=50)]
    working_list = [_wl_row("1", listing_type="BIN")]
    result = build_batch_rows(brief, working_list)
    assert result.rows == []
    assert result.skipped == [{"item_id": "1", "reason": "bin"}]


def test_build_batch_rows_skips_bin_type_field_alias_case_insensitive():
    brief = [_brief("1", max_bid=50)]
    working_list = [_wl_row("1", type="bin")]
    result = build_batch_rows(brief, working_list)
    assert result.rows == []
    assert result.skipped == [{"item_id": "1", "reason": "bin"}]


def test_build_batch_rows_user_skip_override_excludes_row():
    brief = [_brief("1", max_bid=50)]
    working_list = [_wl_row("1")]
    result = build_batch_rows(brief, working_list, overrides={"1": {"skip": True}})
    assert result.rows == []
    assert result.skipped == [{"item_id": "1", "reason": "user_skip"}]


def test_build_batch_rows_user_skip_bypasses_missing_brief_row_check():
    """skip=true is valid even for an item_id that was never priced (no
    brief row at all) — it must not be required to also appear in brief."""
    working_list = [_wl_row("1")]
    result = build_batch_rows([], working_list, overrides={"1": {"skip": True}})
    assert result.rows == []
    assert result.skipped == [{"item_id": "1", "reason": "user_skip"}]


def test_build_batch_rows_null_comic_id_omits_comic_id_and_grade_but_still_adds():
    brief = [_brief("1", comic_id=None, max_bid=50)]
    working_list = [_wl_row("1", grade=9.4)]
    result = build_batch_rows(brief, working_list)
    assert result.rows == [{"item_id": "1", "max_bid": 50.0}]
    assert "comic_id" not in result.rows[0]
    assert "grade" not in result.rows[0]
    assert result.unlinked == [{"item_id": "1", "reason": "comic_id_null"}]


def test_build_batch_rows_needs_manual_without_override_is_hard_error():
    brief = [_brief("1", comic_id=42, max_bid=None, flag_reason="one_sided")]
    working_list = [_wl_row("1")]
    with pytest.raises(AddBatchError, match="needs-manual"):
        build_batch_rows(brief, working_list)


def test_build_batch_rows_needs_manual_with_override_max_bid_succeeds():
    brief = [_brief("1", comic_id=42, max_bid=None, flag_reason="one_sided")]
    working_list = [_wl_row("1", grade=9.0)]
    result = build_batch_rows(brief, working_list, overrides={"1": {"max_bid": 120}})
    assert result.rows[0]["max_bid"] == 120.0
    assert result.rows[0]["comic_id"] == 42


def test_build_batch_rows_needs_manual_with_skip_override_is_skipped_not_error():
    brief = [_brief("1", comic_id=42, max_bid=None, flag_reason="too_sparse")]
    working_list = [_wl_row("1")]
    result = build_batch_rows(brief, working_list, overrides={"1": {"skip": True}})
    assert result.rows == []
    assert result.skipped == [{"item_id": "1", "reason": "user_skip"}]


def test_build_batch_rows_missing_brief_row_is_hard_error_not_silent_drop():
    working_list = [_wl_row("1"), _wl_row("2")]
    brief = [_brief("1", max_bid=50)]  # "2" never priced, no override
    with pytest.raises(AddBatchError, match="no matching comic-fmv --brief row"):
        build_batch_rows(brief, working_list)


def test_build_batch_rows_duplicate_item_id_in_working_list_errors():
    brief = [_brief("1", max_bid=50)]
    working_list = [_wl_row("1"), _wl_row("1")]
    with pytest.raises(AddBatchError, match="duplicate item_id"):
        build_batch_rows(brief, working_list)


def test_build_batch_rows_override_for_unknown_item_id_errors():
    brief = [_brief("1", max_bid=50)]
    working_list = [_wl_row("1")]
    with pytest.raises(AddBatchError, match="not present in the working list"):
        build_batch_rows(brief, working_list, overrides={"999": {"skip": True}})


def test_build_batch_rows_unrecognized_grade_string_errors():
    brief = [_brief("1", max_bid=50)]
    working_list = [_wl_row("1", grade="MINT CONDITION")]
    with pytest.raises(AddBatchError, match="grade"):
        build_batch_rows(brief, working_list)


def test_build_batch_rows_negative_max_bid_errors():
    brief = [_brief("1", max_bid=-10)]
    working_list = [_wl_row("1")]
    with pytest.raises(AddBatchError, match="max_bid"):
        build_batch_rows(brief, working_list)


def test_build_batch_rows_zero_max_bid_errors():
    brief = [_brief("1", max_bid=0)]
    working_list = [_wl_row("1")]
    with pytest.raises(AddBatchError, match="max_bid"):
        build_batch_rows(brief, working_list)


def test_build_batch_rows_group_out_of_range_errors():
    brief = [_brief("1", max_bid=50)]
    working_list = [_wl_row("1", group=11)]
    with pytest.raises(AddBatchError, match="group"):
        build_batch_rows(brief, working_list)


def test_build_batch_rows_nan_max_bid_override_rejected():
    """NaN passes float()/Decimal() without error and can slip past a naive
    server-side "v <= 0" positivity check — reject it client-side too,
    mirroring add_one_row's own NaN/Infinity guard."""
    brief = [_brief("1", max_bid=50)]
    working_list = [_wl_row("1")]
    with pytest.raises(AddBatchError, match="max_bid"):
        build_batch_rows(brief, working_list, overrides={"1": {"max_bid": float("nan")}})


def test_build_batch_rows_working_list_row_missing_item_id_errors():
    brief = []
    with pytest.raises(AddBatchError, match="missing item_id"):
        build_batch_rows(brief, [{"grade": 9.0}])


def test_build_batch_rows_seller_fields_omitted_when_absent():
    brief = [_brief("1", max_bid=50)]
    working_list = [_wl_row("1")]
    result = build_batch_rows(brief, working_list)
    row = result.rows[0]
    assert "seller" not in row
    assert "seller_grade" not in row
    assert "photo_grade" not in row


def test_build_batch_rows_rejects_non_dict_override_entry():
    brief = [_brief("1", max_bid=50)]
    working_list = [_wl_row("1")]
    with pytest.raises(AddBatchError, match="must be a JSON object"):
        build_batch_rows(brief, working_list, overrides={"1": "skip"})


def test_build_batch_rows_rejects_non_dict_overrides_top_level():
    brief = [_brief("1", max_bid=50)]
    working_list = [_wl_row("1")]
    with pytest.raises(AddBatchError):
        build_batch_rows(brief, working_list, overrides=["not", "a", "dict"])


def test_build_batch_rows_rejects_non_list_brief_rows():
    with pytest.raises(AddBatchError, match="brief_rows"):
        build_batch_rows({"not": "a list"}, [_wl_row("1")])


def test_build_batch_rows_rejects_non_list_working_list():
    with pytest.raises(AddBatchError, match="working_list"):
        build_batch_rows([_brief("1", max_bid=50)], {"not": "a list"})


def test_build_batch_rows_rejects_non_dict_brief_row_entry():
    with pytest.raises(AddBatchError):
        build_batch_rows(["not a dict"], [_wl_row("1")])


def test_build_batch_rows_multiple_copies_same_group_bid_group_scenario():
    """BUI-363 bid-group scenario: two copies of the same book, same group,
    different max bids by grade — both must land with the group intact."""
    brief = [_brief("1", comic_id=10, max_bid=800), _brief("2", comic_id=10, max_bid=900)]
    working_list = [
        _wl_row("1", grade=9.0, group=4),
        _wl_row("2", grade=9.2, group=4),
    ]
    result = build_batch_rows(brief, working_list)
    assert len(result.rows) == 2
    assert all(r["group"] == 4 for r in result.rows)
    assert result.rows[0]["max_bid"] != result.rows[1]["max_bid"]
