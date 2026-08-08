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
    STATUS_BLOCKED,
    STATUS_FAILED,
    STATUS_INDETERMINATE,
    STATUS_NOT_ATTEMPTED,
    STATUS_UPDATED,
    BatchOutcome,
    RowResult,
    add_one_row,
    reconcile_indeterminate_rows,
    advisories_from_response,
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
    assert payload == {
        "item_id": "1", "max_bid": 100.0, "bid_offset": 6, "snipe_group": 0,
        "comic_identities": [],
    }


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
        "comic_identities": [],
    }


# ---------------------------------------------------------------------------
# build_bid_payload — comic identity (BUI-619/U5)
# ---------------------------------------------------------------------------


def test_build_bid_payload_comic_id_and_grade_builds_one_identity():
    payload = build_bid_payload("1", 100, 6, 0, comic_id=187, grade=9.2)
    assert payload["comic_identities"] == [{"comic_id": 187, "grade": 9.2}]


def test_build_bid_payload_locg_id_and_grade_builds_one_identity():
    payload = build_bid_payload("1", 100, 6, 0, locg_id=555, grade=9.2)
    assert payload["comic_identities"] == [{"locg_id": 555, "grade": 9.2}]


def test_build_bid_payload_comic_id_wins_over_locg_id_when_both_given():
    """Mirrors cli.py's `add` --comic-id/--catalog-id precedence."""
    payload = build_bid_payload("1", 100, 6, 0, comic_id=187, locg_id=555, grade=9.2)
    assert payload["comic_identities"] == [{"comic_id": 187, "grade": 9.2}]


def test_build_bid_payload_no_identity_without_grade():
    """comic_id/locg_id alone (no grade) never fires the link resolution —
    matches the pre-existing link_attempted = grade is not None AND
    comic_id is not None gate."""
    payload = build_bid_payload("1", 100, 6, 0, comic_id=187)
    assert payload["comic_identities"] == []


def test_build_bid_payload_no_identity_without_comic_id_or_locg_id():
    payload = build_bid_payload("1", 100, 6, 0, grade=9.2)
    assert payload["comic_identities"] == []


# ---------------------------------------------------------------------------
# build_bid_payload — source provenance tag (BUI-621/U7)
# ---------------------------------------------------------------------------


def test_build_bid_payload_omits_source_by_default():
    payload = build_bid_payload("1", 100, 6, 0)
    assert "source" not in payload


def test_build_bid_payload_includes_source_when_given():
    payload = build_bid_payload("1", 100, 6, 0, source="cli")
    assert payload["source"] == "cli"


def test_build_bid_payload_omits_policy_bypass_by_default():
    """BUI-623 (U9): default False is left OUT of the payload entirely, same
    'only send what was given' convention as `source` above — an old server
    that has never heard of `policy_bypass` sees byte-identical requests."""
    payload = build_bid_payload("1", 100, 6, 0)
    assert "policy_bypass" not in payload


def test_build_bid_payload_includes_policy_bypass_when_true():
    payload = build_bid_payload("1", 100, 6, 0, policy_bypass=True)
    assert payload["policy_bypass"] is True


def test_created_from_response_defaults_true_when_key_missing():
    assert created_from_response({}) is True


def test_created_from_response_respects_explicit_false():
    assert created_from_response({"created": False}) is False


def test_created_from_response_true_for_non_dict():
    assert created_from_response(None) is True


# ---------------------------------------------------------------------------
# advisories_from_response — KTD4 envelope extraction (BUI-621/U7)
# ---------------------------------------------------------------------------


def test_advisories_from_response_extracts_list():
    advisories = [{"code": "x", "severity": "warning", "message": "m", "data": {}}]
    assert advisories_from_response({"item_id": "1", "advisories": advisories}) == advisories


def test_advisories_from_response_missing_key_is_empty():
    """An old server whose 2xx response predates the KTD4 envelope must not
    crash the CLI — absent key normalizes to []."""
    assert advisories_from_response({"item_id": "1", "created": True}) == []


def test_advisories_from_response_non_dict_is_empty():
    assert advisories_from_response(None) == []
    assert advisories_from_response([1, 2, 3]) == []


def test_advisories_from_response_non_list_value_is_empty():
    """A malformed (non-list) advisories value degrades to empty rather than
    propagating a bad shape into the CLI's rendering."""
    assert advisories_from_response({"advisories": "not-a-list"}) == []


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
        "comic_identities": [], "source": "batch",
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


# ---------------------------------------------------------------------------
# add_one_row — advisories (BUI-621/U7)
# ---------------------------------------------------------------------------


def test_add_one_row_carries_advisories_from_response():
    advisories = [{"code": "exposure_ceiling", "severity": "warning",
                   "message": "over the group exposure ceiling", "data": {}}]
    server = _FakeServer({("post", "/api/bids"): (
        True, {"item_id": "1", "created": True, "advisories": advisories}, None,
    )})
    result = add_one_row(_row("1"), server_request=server)
    assert result.advisories == advisories
    assert result.to_dict()["advisories"] == advisories


def test_add_one_row_absent_advisories_key_defaults_empty():
    """An old server whose 2xx response predates the KTD4 envelope must not
    crash add-batch — the row just carries no advisories."""
    server = _FakeServer({("post", "/api/bids"): (True, {"item_id": "1", "created": True}, None)})
    result = add_one_row(_row("1"), server_request=server)
    assert result.advisories == []


def test_add_one_row_failed_row_has_no_advisories():
    server = _FakeServer({("post", "/api/bids"): (False, None, "Server returned 503: gixen down")})
    result = add_one_row(_row("1"), server_request=server)
    assert result.status == STATUS_FAILED
    assert result.advisories == []


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
        "comic_identities": [], "source": "batch",
    }


def test_add_one_row_comic_id_and_grade_reach_the_add_payload():
    """BUI-619 (U5): identity travels in the POST /api/bids payload itself
    now, not only the post-add link-fmv call below — so pre-trade FMV
    checks (U4) can see it before the Gixen call."""
    server = _FakeServer({
        ("post", "/api/bids"): (True, {"item_id": "1", "created": True}, None),
        ("post", "/api/bids/1/link-fmv"): (True, {}, None),
    })
    add_one_row(_row("1", comic_id=187, grade=9.2), server_request=server)
    add_calls = [c for c in server.calls if c[1] == "/api/bids"]
    assert add_calls[0][2]["comic_identities"] == [{"comic_id": 187, "grade": 9.2}]


def test_add_one_row_no_comic_id_sends_empty_identity_list():
    server = _FakeServer({("post", "/api/bids"): (True, {"item_id": "1", "created": True}, None)})
    add_one_row(_row("1"), server_request=server)
    assert server.calls[0][2]["comic_identities"] == []


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
# add_one_row — BUI-623 (U9): policy block (409) is BLOCKED, not FAILED
# ---------------------------------------------------------------------------

_BLOCK_DETAIL = {
    "blocked": True,
    "message": "Blocked by policy check(s): over_fmv.",
    "blocking_codes": ["over_fmv"],
    "unevaluable_while_blocking": [],
    "advisories": [
        {"code": "over_fmv", "severity": "warning", "message": "over FMV", "data": {}},
    ],
    "surviving_snipe": None,
}


def test_add_one_row_blocked_response_marks_status_blocked_not_failed():
    """The server's 409 policy block surfaces through server_request as
    (False, <structured detail dict with blocked=True>, <error string>) —
    add_one_row must read the STRUCTURED shape (not just `ok`) to tell a
    policy block apart from a genuine add failure."""
    server = _FakeServer({
        ("post", "/api/bids"): (False, _BLOCK_DETAIL, "Server returned 409: ..."),
    })
    result = add_one_row(_row("1"), server_request=server)
    assert result.status == STATUS_BLOCKED
    assert result.status != STATUS_FAILED


def test_add_one_row_blocked_carries_message_and_advisories():
    server = _FakeServer({
        ("post", "/api/bids"): (False, _BLOCK_DETAIL, "Server returned 409: ..."),
    })
    result = add_one_row(_row("1"), server_request=server)
    assert result.error == "Blocked by policy check(s): over_fmv."
    assert result.advisories == _BLOCK_DETAIL["advisories"]


def test_add_one_row_blocked_preserves_max_bid_and_grade_for_the_table():
    server = _FakeServer({
        ("post", "/api/bids"): (False, _BLOCK_DETAIL, "Server returned 409: ..."),
    })
    result = add_one_row(_row("1", max_bid=250, grade=9.4), server_request=server)
    assert result.max_bid == 250.0
    assert result.grade == 9.4


def test_add_one_row_non_409_failure_still_marks_failed_not_blocked():
    """Adversarial: a genuine failure whose response happens to be a dict
    must NOT be mistaken for a block — only `resp.get("blocked")` truthy
    routes to STATUS_BLOCKED."""
    server = _FakeServer({
        ("post", "/api/bids"): (False, {"detail": "internal error"}, "Server returned 500: boom"),
    })
    result = add_one_row(_row("1"), server_request=server)
    assert result.status == STATUS_FAILED


def test_add_one_row_sends_policy_bypass_when_row_flag_true():
    server = _FakeServer({("post", "/api/bids"): (True, {"item_id": "1", "created": True}, None)})
    add_one_row(_row("1", policy_bypass=True), server_request=server)
    assert server.calls[0][2]["policy_bypass"] is True


def test_add_one_row_omits_policy_bypass_by_default():
    """Matches the 'only send what was given' convention every other
    optional add_one_row field follows — an absent/false row flag must not
    even add the key, for byte-identical behavior against an old server."""
    server = _FakeServer({("post", "/api/bids"): (True, {"item_id": "1", "created": True}, None)})
    add_one_row(_row("1"), server_request=server)
    assert "policy_bypass" not in server.calls[0][2]


# ---------------------------------------------------------------------------
# add_one_row — stale-listing guard (BUI-567)
# ---------------------------------------------------------------------------


def test_add_one_row_rejects_already_ended_auction_without_network():
    server = _FakeServer({})
    result = add_one_row(_row("1", end_date_iso="2020-01-01T00:00:00Z"), server_request=server)
    assert result.status == STATUS_FAILED
    assert "end_date_iso" in result.error
    assert "already ended" in result.error
    assert server.calls == []


def test_add_one_row_rejects_already_ended_auction_naive_timestamp():
    """A naive (no explicit offset) ISO timestamp is treated as UTC — same
    convention `server/main.py`'s `iso_to_relative` effectively assumes for
    every real end_date_iso value this codebase produces (always UTC, `Z`
    suffix or `+00:00`)."""
    server = _FakeServer({})
    result = add_one_row(_row("1", end_date_iso="2020-01-01T00:00:00"), server_request=server)
    assert result.status == STATUS_FAILED
    assert "already ended" in result.error
    assert server.calls == []


def test_add_one_row_accepts_future_end_date_and_still_calls_server():
    server = _FakeServer({("post", "/api/bids"): (True, {"item_id": "1", "created": True}, None)})
    result = add_one_row(_row("1", end_date_iso="2099-01-01T00:00:00Z"), server_request=server)
    assert result.status == STATUS_ADDED
    assert server.calls != []


def test_add_one_row_absent_end_date_iso_skips_guard_backward_compatible():
    """No `end_date_iso` on the row at all — the pre-BUI-567 shape — must
    behave exactly as before: no validation, straight to the network."""
    server = _FakeServer({("post", "/api/bids"): (True, {"item_id": "1", "created": True}, None)})
    result = add_one_row(_row("1"), server_request=server)
    assert result.status == STATUS_ADDED
    assert server.calls != []


def test_add_one_row_rejects_unparseable_end_date_iso_without_network():
    """A present-but-garbage end_date_iso must fail loudly, not be silently
    treated as 'no end time known' — that would quietly defeat the guard
    for any malformed upstream value."""
    server = _FakeServer({})
    result = add_one_row(_row("1", end_date_iso="not-a-date"), server_request=server)
    assert result.status == STATUS_FAILED
    assert "end_date_iso" in result.error
    assert server.calls == []


def test_add_one_row_rejects_non_string_end_date_iso_without_network():
    server = _FakeServer({})
    result = add_one_row(_row("1", end_date_iso=12345), server_request=server)
    assert result.status == STATUS_FAILED
    assert "end_date_iso" in result.error
    assert server.calls == []


def test_add_one_row_ended_auction_still_carries_title_for_failed_table():
    result = add_one_row(
        _row("1", end_date_iso="2020-01-01T00:00:00Z", title="Watchmen #12"),
        server_request=_FakeServer({}),
    )
    assert result.status == STATUS_FAILED
    assert result.title == "Watchmen #12"


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


def test_run_batch_ae9_blocked_row_continues_no_halt():
    """AE9: an add-batch with one over-FMV (blocked) row and two clean rows
    ends with 2 added + 1 BLOCKED, and the batch does not halt — a policy
    block is not a server fault, so BUI-168 halt semantics don't apply."""
    server = _FakeServer({
        ("post", "/api/bids"): [
            (True, {"item_id": "1", "created": True}, None),
            (False, _BLOCK_DETAIL, "Server returned 409: ..."),
            (True, {"item_id": "3", "created": True}, None),
        ],
    })
    health_calls = []

    def health_check():
        health_calls.append(1)
        return True

    rows = [_row("1"), _row("2"), _row("3")]
    outcome = run_batch(rows, server_request=server, health_check=health_check)

    statuses = [r.status for r in outcome.rows]
    assert statuses == [STATUS_ADDED, STATUS_BLOCKED, STATUS_ADDED]
    assert outcome.halted is False
    # every row was attempted — the BLOCKED row never triggered a health
    # re-check the way a STATUS_FAILED row does (a policy block is not the
    # BUI-168 "is the server still up" signal).
    assert health_calls == []
    summary = outcome.summary()
    assert summary["added"] == 2
    assert summary["blocked"] == 1
    assert summary["failed"] == 0
    assert summary["not_attempted"] == 0


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
    # BUI-623 (U9): STATUS_BLOCKED is now a fixed key in every summary dict
    # (present at 0 even when no row was blocked) — see summary()'s own
    # comment for why it's listed unconditionally like every other status.
    # BUI-697: STATUS_INDETERMINATE joins it on the same rule.
    assert summary == {
        "total": 4, STATUS_ADDED: 1, STATUS_UPDATED: 1,
        STATUS_FAILED: 1, STATUS_NOT_ATTEMPTED: 1, STATUS_BLOCKED: 0,
        STATUS_INDETERMINATE: 0,
        "advisories": 0,
    }


def test_batch_outcome_summary_counts_advisories_across_rows():
    """BUI-621 (U7): the summary's own advisory count is what lets a reader
    of just the summary dict (not every row) see that a batch carried
    challenges — sums across every row, not just the first that has any."""
    outcome = BatchOutcome(rows=[
        RowResult(item_id="1", status=STATUS_ADDED, advisories=[
            {"code": "a", "severity": "warning", "message": "m1", "data": {}},
        ]),
        RowResult(item_id="2", status=STATUS_UPDATED, advisories=[
            {"code": "b", "severity": "warning", "message": "m2", "data": {}},
            {"code": "c", "severity": "unevaluable", "message": "m3", "data": {}},
        ]),
        RowResult(item_id="3", status=STATUS_FAILED, error="boom"),
    ])
    assert outcome.summary()["advisories"] == 3


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


def test_batch_outcome_exit_code_nonzero_on_blocked_alone():
    """BUI-623 (U9): a BLOCKED row's write did not land any more than a
    FAILED or NOT_ATTEMPTED row's did — same 'any row failed to land'
    rationale exit_code()'s own docstring already applies to those two, even
    though (unlike NOT_ATTEMPTED) the batch never halts for it."""
    outcome = BatchOutcome(rows=[
        RowResult(item_id="1", status=STATUS_ADDED),
        RowResult(item_id="2", status=STATUS_BLOCKED, error="Blocked by policy check(s): over_fmv."),
    ])
    assert outcome.halted is False
    assert outcome.exit_code() == 1


def test_batch_outcome_summary_never_looks_all_clean_with_a_blocked_row():
    """AE9's summary claim, at the summary-dict level: 2 added + 1 blocked
    must be readable as NOT a clean batch from the summary alone."""
    outcome = BatchOutcome(rows=[
        RowResult(item_id="1", status=STATUS_ADDED),
        RowResult(item_id="2", status=STATUS_ADDED),
        RowResult(item_id="3", status=STATUS_BLOCKED, error="blocked"),
    ])
    summary = outcome.summary()
    assert summary["added"] == 2
    assert summary["blocked"] == 1
    assert not (summary["failed"] == 0 and summary["blocked"] == 0 and summary["not_attempted"] == 0)


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


# ---------------------------------------------------------------------------
# build_batch_rows — end_date_iso threading (BUI-567)
# ---------------------------------------------------------------------------


def test_build_batch_rows_carries_end_date_iso_from_working_list():
    brief = [_brief("1", comic_id=42, max_bid=800)]
    working_list = [_wl_row("1", grade=9.2, end_date_iso="2026-08-01T00:00:00Z")]
    result = build_batch_rows(brief, working_list)
    assert result.rows[0]["end_date_iso"] == "2026-08-01T00:00:00Z"


def test_build_batch_rows_absent_end_date_iso_omits_key_backward_compatible():
    brief = [_brief("1", comic_id=None, max_bid=50)]
    working_list = [_wl_row("1")]
    result = build_batch_rows(brief, working_list)
    assert "end_date_iso" not in result.rows[0]


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


# ---------------------------------------------------------------------------
# BUI-697: a client timeout is INDETERMINATE, not failed — and the
# end-of-batch reconcile is what turns "unknown" into knowledge.
# ---------------------------------------------------------------------------

_TIMEOUT = (
    False,
    {"indeterminate": True, "reason": "timeout"},
    "Server timed out — the write may still have committed.",
)


def _snipe(item_id, max_bid):
    """One /api/comics/snipes row, trimmed to the fields the reconcile reads."""
    return {"item_id": item_id, "max_bid": f"{max_bid:.2f} USD", "max_bid_numeric": max_bid}


def _no_sleep(_seconds):
    return None


def test_add_one_row_timeout_is_indeterminate_not_failed():
    """The whole bug in one assertion: `_server_request_result`'s timeout
    marker must never produce STATUS_FAILED, because a timeout is the CLI
    giving up on the read — not evidence the write did not commit."""
    server = _FakeServer({("post", "/api/bids"): _TIMEOUT})
    result = add_one_row(_row("111", max_bid=20), server_request=server)

    assert result.status == STATUS_INDETERMINATE
    assert result.status != STATUS_FAILED
    assert result.max_bid == 20.0
    assert "may still have committed" in result.error


def test_add_one_row_non_timeout_failure_is_still_failed():
    """The indeterminate branch is keyed on the structured marker, not on
    "any failure" — an ordinary 500 must stay FAILED."""
    server = _FakeServer({("post", "/api/bids"): (False, None, "Server returned 500: boom")})
    result = add_one_row(_row("111"), server_request=server)

    assert result.status == STATUS_FAILED


def test_indeterminate_row_counts_in_summary_and_exit_code():
    """Exit-code membership: "unknown" is not success. A caller checking $?
    must be told it has follow-up to do."""
    outcome = BatchOutcome(rows=[
        RowResult(item_id="1", status=STATUS_ADDED),
        RowResult(item_id="2", status=STATUS_INDETERMINATE, error="Server timed out."),
    ])

    assert outcome.summary()[STATUS_INDETERMINATE] == 1
    assert outcome.summary()[STATUS_FAILED] == 0
    assert outcome.exit_code() == 1


def test_batch_outcome_exit_code_nonzero_on_indeterminate_alone():
    outcome = BatchOutcome(rows=[RowResult(item_id="1", status=STATUS_INDETERMINATE)])
    assert outcome.exit_code() == 1


def test_run_batch_indeterminate_row_rechecks_health_and_halts_when_down():
    """Halt semantics: a timeout takes FAILED's rule, not BLOCKED's. The
    server may genuinely be sick, so re-check health before the next row —
    but only a DOWN server halts the batch."""
    server = _FakeServer({
        ("post", "/api/bids"): _TIMEOUT,
        ("get", "/api/comics/snipes"): (False, None, "Server timed out."),
    })
    health_calls = []

    def health_check():
        health_calls.append(1)
        return False

    outcome = run_batch(
        [_row("1"), _row("2")],
        server_request=server, health_check=health_check,
        sleep=_no_sleep, settle_seconds=0,
    )

    assert len(health_calls) == 1
    assert outcome.halted is True
    assert [r.status for r in outcome.rows] == [STATUS_INDETERMINATE, STATUS_NOT_ATTEMPTED]


def test_run_batch_indeterminate_row_continues_when_server_healthy():
    server = _FakeServer({
        ("post", "/api/bids"): [_TIMEOUT, (True, {"created": True}, None)],
        ("get", "/api/comics/snipes"): (True, [], None),
    })
    outcome = run_batch(
        [_row("1"), _row("2")],
        server_request=server, health_check=lambda: True,
        sleep=_no_sleep, settle_seconds=0,
    )

    assert outcome.halted is False
    assert [r.status for r in outcome.rows] == [STATUS_INDETERMINATE, STATUS_ADDED]


def test_reconcile_found_live_upgrades_to_landed_and_attempts_fmv_link():
    """The load-bearing behaviour: a row found live is landed, and it gets
    the SAME post-add FMV link every other landed row gets. The BUI-697
    incident left 25 landed rows permanently unlinked (`link_attempted:
    false`) precisely because this path did not exist."""
    server = _FakeServer({
        ("post", "/api/bids"): _TIMEOUT,
        ("get", "/api/comics/snipes"): (True, [_snipe("111", 20.0)], None),
        ("post", "/api/bids/111/link-fmv"): (True, {"ok": True}, None),
    })

    outcome = run_batch(
        [_row("111", max_bid=20, comic_id=42, grade=9.0)],
        server_request=server, health_check=lambda: True,
        sleep=_no_sleep, settle_seconds=0,
    )

    row = outcome.rows[0]
    assert row.status == STATUS_UPDATED
    assert row.error is None
    assert row.reconcile == {
        "checked": True, "found": True, "error": None, "live_max_bid": 20.0,
    }
    assert row.link_attempted is True
    assert row.link_ok is True
    assert ("post", "/api/bids/111/link-fmv", {"comic_id": 42, "grade": 9.0}) in server.calls
    assert outcome.exit_code() == 0


def test_reconcile_found_live_without_identity_does_not_fake_a_link():
    """A row with no comic_id/grade never had a link to attempt — the
    reconcile must not invent one just because it upgraded the status."""
    server = _FakeServer({
        ("post", "/api/bids"): _TIMEOUT,
        ("get", "/api/comics/snipes"): (True, [_snipe("111", 100.0)], None),
    })
    outcome = run_batch(
        [_row("111", max_bid=100)],
        server_request=server, health_check=lambda: True,
        sleep=_no_sleep, settle_seconds=0,
    )

    row = outcome.rows[0]
    assert row.status == STATUS_UPDATED
    assert row.link_attempted is False
    assert not any(path.endswith("/link-fmv") for _m, path, _j in server.calls)


def test_reconcile_absent_stays_indeterminate_and_is_reported_not_landed():
    """Absence at T+settle is evidence, not proof (the incident's 25-row
    retry reported all-failed and all 25 appeared live later) — so the row
    is reported as not-landed WITHOUT being demoted to FAILED."""
    server = _FakeServer({
        ("post", "/api/bids"): _TIMEOUT,
        ("get", "/api/comics/snipes"): (True, [_snipe("999", 5.0)], None),
    })

    outcome = run_batch(
        [_row("111", max_bid=20, comic_id=42, grade=9.0)],
        server_request=server, health_check=lambda: True,
        sleep=_no_sleep, settle_seconds=0,
    )

    row = outcome.rows[0]
    assert row.status == STATUS_INDETERMINATE
    assert row.status != STATUS_FAILED
    assert row.reconcile == {
        "checked": True, "found": False, "error": None, "live_max_bid": None,
    }
    assert "NOT live" in row.error
    # Not landed: excluded from the verify pass and from a clean exit code.
    assert verify_items(outcome) == []
    assert outcome.exit_code() == 1
    # And no link was attempted for a row we cannot confirm landed.
    assert row.link_attempted is False


def test_reconcile_found_live_at_a_different_max_bid_stays_indeterminate():
    """The ASM #61 shape: a snipe IS live for this item but at a different
    amount, so this row's write is not confirmed. Claiming "landed" there
    would tell the operator their $20 cap is in when $1.09 is."""
    server = _FakeServer({
        ("post", "/api/bids"): _TIMEOUT,
        ("get", "/api/comics/snipes"): (True, [_snipe("111", 1.09)], None),
    })

    outcome = run_batch(
        [_row("111", max_bid=20, comic_id=42, grade=9.0)],
        server_request=server, health_check=lambda: True,
        sleep=_no_sleep, settle_seconds=0,
    )

    row = outcome.rows[0]
    assert row.status == STATUS_INDETERMINATE
    assert row.reconcile["found"] is True
    assert row.reconcile["live_max_bid"] == 1.09
    assert "1.09" in row.error and "20.00" in row.error
    assert row.link_attempted is False


def test_reconcile_tolerates_float_noise_in_max_bid():
    server = _FakeServer({
        ("post", "/api/bids"): _TIMEOUT,
        ("get", "/api/comics/snipes"): (
            True, [{"item_id": "111", "max_bid_numeric": 20.000000001}], None,
        ),
    })
    outcome = run_batch(
        [_row("111", max_bid=20)],
        server_request=server, health_check=lambda: True,
        sleep=_no_sleep, settle_seconds=0,
    )
    assert outcome.rows[0].status == STATUS_UPDATED


def test_reconcile_falls_back_to_the_display_max_bid_string():
    """An older server without `max_bid_numeric` must still reconcile."""
    server = _FakeServer({
        ("post", "/api/bids"): _TIMEOUT,
        ("get", "/api/comics/snipes"): (True, [{"item_id": "111", "max_bid": "20.00 USD"}], None),
    })
    outcome = run_batch(
        [_row("111", max_bid=20)],
        server_request=server, health_check=lambda: True,
        sleep=_no_sleep, settle_seconds=0,
    )
    assert outcome.rows[0].status == STATUS_UPDATED


def test_reconcile_call_failure_never_upgrades_a_row():
    """A server that cannot answer tells us nothing. The row must stay
    indeterminate — never upgraded to landed, never demoted to failed."""
    server = _FakeServer({
        ("post", "/api/bids"): _TIMEOUT,
        ("get", "/api/comics/snipes"): (
            False, None, "Server unreachable. Is the comics server running?",
        ),
    })

    outcome = run_batch(
        [_row("111", max_bid=20, comic_id=42, grade=9.0)],
        server_request=server, health_check=lambda: True,
        sleep=_no_sleep, settle_seconds=0,
    )

    row = outcome.rows[0]
    assert row.status == STATUS_INDETERMINATE
    assert row.reconcile["checked"] is False
    assert row.reconcile["found"] is None
    assert "Server unreachable" in row.reconcile["error"]
    assert row.link_attempted is False
    assert outcome.exit_code() == 1


def test_reconcile_rejects_a_non_list_snipes_response():
    """A 2xx body of the wrong shape is as uninformative as a failed call —
    it must not be read as "no snipes are live" (which would falsely confirm
    every row as absent)."""
    server = _FakeServer({
        ("post", "/api/bids"): _TIMEOUT,
        ("get", "/api/comics/snipes"): (True, {"unexpected": "shape"}, None),
    })
    outcome = run_batch(
        [_row("111")],
        server_request=server, health_check=lambda: True,
        sleep=_no_sleep, settle_seconds=0,
    )
    assert outcome.rows[0].status == STATUS_INDETERMINATE
    assert outcome.rows[0].reconcile["checked"] is False


def test_reconcile_is_skipped_entirely_when_no_row_is_indeterminate():
    """An ordinary batch pays nothing for this: no settle wait, no extra
    round trip."""
    server = _FakeServer({("post", "/api/bids"): (True, {"created": True}, None)})
    slept = []

    outcome = run_batch(
        [_row("1"), _row("2")],
        server_request=server, health_check=lambda: True,
        sleep=slept.append,
    )

    assert slept == []
    assert not any(path == "/api/comics/snipes" for _m, path, _j in server.calls)
    assert outcome.exit_code() == 0


def test_reconcile_waits_the_settle_interval_before_reading():
    """The settle wait is a courtesy to the common case, not the fix — but
    it must actually happen, and before the read."""
    order = []
    server = _FakeServer({
        ("post", "/api/bids"): _TIMEOUT,
        ("get", "/api/comics/snipes"): (True, [], None),
    })

    def tracking(method, path, **kwargs):
        order.append(("request", path))
        return server(method, path, **kwargs)

    run_batch(
        [_row("111")],
        server_request=tracking, health_check=lambda: True,
        sleep=lambda s: order.append(("sleep", s)), settle_seconds=7.5,
    )

    assert order == [
        ("request", "/api/bids"),
        ("sleep", 7.5),
        ("request", "/api/comics/snipes"),
    ]


def test_reconcile_resolves_each_row_independently():
    """A mixed batch: one landed, one absent — the found row must not carry
    the absent row's verdict or vice versa."""
    server = _FakeServer({
        ("post", "/api/bids"): [_TIMEOUT, _TIMEOUT],
        ("get", "/api/comics/snipes"): (True, [_snipe("222", 30.0)], None),
    })

    outcome = run_batch(
        [_row("111", max_bid=20), _row("222", max_bid=30)],
        server_request=server, health_check=lambda: True,
        sleep=_no_sleep, settle_seconds=0,
    )

    assert [r.status for r in outcome.rows] == [STATUS_INDETERMINATE, STATUS_UPDATED]
    assert outcome.rows[0].reconcile["found"] is False
    assert outcome.rows[1].reconcile["found"] is True


def test_reconcile_indeterminate_rows_is_a_noop_without_indeterminate_rows():
    """Direct call, no run_batch: the guard is in the function itself."""
    server = _FakeServer({})
    outcome = BatchOutcome(rows=[RowResult(item_id="1", status=STATUS_ADDED)])
    reconcile_indeterminate_rows(outcome, [_row("1")], server_request=server, sleep=_no_sleep)
    assert server.calls == []
    assert outcome.rows[0].reconcile is None


def test_row_result_to_dict_carries_the_reconcile_verdict():
    row = RowResult(item_id="1", status=STATUS_INDETERMINATE)
    row.reconcile = {"checked": True, "found": False, "error": None, "live_max_bid": None}
    assert row.to_dict()["reconcile"] == row.reconcile
    assert RowResult(item_id="2", status=STATUS_ADDED).to_dict()["reconcile"] is None
