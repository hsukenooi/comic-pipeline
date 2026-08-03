"""CLI wiring tests for `gixen add-batch` (BUI-360).

add_batch.py's own logic (sequential ordering, failure/halt semantics, exit
codes, verify wiring) is covered by tests/test_add_batch.py; these tests only
cover cli.py's plumbing — reading/parsing the rows file, resolving
COMICS_SERVER_URL, wiring `_server_request_result` through, printing the
human table + JSON summary, and the process exit code. No network is ever
touched — every comics-server call is patched at `cli._server_request_result`.
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from unittest.mock import patch

from click.testing import CliRunner


def _write_rows(tmp_path, rows):
    path = tmp_path / "rows.json"
    path.write_text(json.dumps(rows))
    return str(path)


def _fake_request_from(responses):
    """responses: dict of (method, path) -> (ok, data, err) or a list of
    such tuples consumed in order for repeat calls to the same endpoint."""
    queues = {k: (list(v) if isinstance(v, list) else [v]) for k, v in responses.items()}

    def fake(method, path, **kwargs):
        key = (method, path)
        queue = queues.get(key)
        if not queue:
            raise AssertionError(f"no fake response queued for {key}")
        return queue[0] if len(queue) == 1 else queue.pop(0)

    return fake


# ---------------------------------------------------------------------------
# Pre-flight: COMICS_SERVER_URL required
# ---------------------------------------------------------------------------


def test_add_batch_requires_comics_server_url(tmp_path):
    from cli import cli

    rows_file = _write_rows(tmp_path, [{"item_id": "1", "max_bid": 10}])
    runner = CliRunner()
    with patch("cli._server_url", return_value=None):
        result = runner.invoke(cli, ["add-batch", rows_file])

    assert result.exit_code == 1
    assert "COMICS_SERVER_URL is not set" in result.output


# ---------------------------------------------------------------------------
# Input file handling
# ---------------------------------------------------------------------------


def test_add_batch_rejects_malformed_json(tmp_path):
    from cli import cli

    bad_file = tmp_path / "rows.json"
    bad_file.write_text("{not json")
    runner = CliRunner()
    with patch("cli._server_url", return_value="http://srv"):
        result = runner.invoke(cli, ["add-batch", str(bad_file)])

    assert result.exit_code == 1
    assert "could not read/parse" in result.output


def test_add_batch_rejects_non_list_shape(tmp_path):
    from cli import cli

    rows_file = _write_rows(tmp_path, {"oops": "not rows"})
    runner = CliRunner()
    with patch("cli._server_url", return_value="http://srv"):
        result = runner.invoke(cli, ["add-batch", rows_file])

    assert result.exit_code == 1
    assert "JSON list" in result.output


def test_add_batch_empty_rows_list_is_a_clean_noop(tmp_path):
    from cli import cli

    rows_file = _write_rows(tmp_path, [])
    runner = CliRunner()
    with patch("cli._server_url", return_value="http://srv"):
        result = runner.invoke(cli, ["add-batch", rows_file])

    assert result.exit_code == 0
    assert "No rows to add" in result.output


def test_add_batch_empty_rows_list_still_honors_json_out(tmp_path):
    """Every exit path must honor --json-out consistently — a caller reading
    Step 6's results exclusively from the --json-out file (per buy.md) must
    not find a missing/stale file just because the approved batch was empty."""
    from cli import cli

    rows_file = _write_rows(tmp_path, [])
    out_path = tmp_path / "out.json"
    runner = CliRunner()
    with patch("cli._server_url", return_value="http://srv"):
        result = runner.invoke(cli, ["add-batch", rows_file, "--json-out", str(out_path)])

    assert result.exit_code == 0, result.output
    written = json.loads(out_path.read_text())
    assert written["summary"]["total"] == 0
    assert written["rows"] == []


def test_add_batch_rejects_json_out_same_path_as_rows_file(tmp_path):
    """--json-out writing over ROWS_FILE would destroy the original batch
    input the instant the run completes — exactly when a BUI-168 halt
    (not_attempted rows) needs it most for a retry."""
    from cli import cli

    rows_file = _write_rows(tmp_path, [{"item_id": "1", "max_bid": 10}])
    runner = CliRunner()
    with patch("cli._server_url", return_value="http://srv"):
        result = runner.invoke(cli, ["add-batch", rows_file, "--json-out", rows_file])

    assert result.exit_code == 1
    assert "same file as ROWS_FILE" in result.output
    # The original rows file must survive untouched.
    assert json.loads(open(rows_file).read()) == [{"item_id": "1", "max_bid": 10}]


def test_add_batch_json_out_write_failure_warns_but_does_not_crash(tmp_path):
    """A --json-out write failure must not raise past the point where the
    batch's own (already-computed) success/failure exit code is set — stdout
    already has the correct JSON even if persisting a copy to disk fails.

    click's own `click.Path(writable=True)` type already rejects a directory
    or a genuinely-unwritable path at argument-parsing time (before
    add_batch_cmd's body even runs), so a real filesystem condition can't
    reach the try/except this test targets — patch `Path.write_text` itself
    to simulate a failure that occurs after click's upfront check passes
    (e.g. disk fills up between validation and the actual write)."""
    from cli import cli
    from pathlib import Path as RealPath

    rows_file = _write_rows(tmp_path, [{"item_id": "1", "max_bid": 10}])
    json_out = tmp_path / "out.json"
    fake = _fake_request_from({
        ("get", "/health"): (True, {}, None),
        ("post", "/api/bids"): (True, {"item_id": "1", "created": True}, None),
    })
    runner = CliRunner()
    with patch("cli._server_url", return_value="http://srv"), \
         patch("cli._server_request_result", side_effect=fake), \
         patch("cli._record_adds"), \
         patch.object(RealPath, "write_text", side_effect=OSError("disk full")):
        result = runner.invoke(cli, ["add-batch", rows_file, "--json-out", str(json_out)])

    # The batch itself fully succeeded -- the exit code must reflect that,
    # not the unrelated --json-out write failure.
    assert result.exit_code == 0, result.output
    assert "could not write --json-out" in result.output
    payload = _extract_json(result.output)
    assert payload["summary"]["added"] == 1


def test_add_batch_malformed_server_response_degrades_to_row_failure_not_crash(tmp_path):
    """A 200 response with a non-JSON body (a real 'flaky server' failure
    mode) must degrade to a per-row FAILED result, not propagate as an
    unhandled exception that crashes the batch with zero output after
    earlier rows may have already placed real bids."""
    from cli import cli

    rows_file = _write_rows(tmp_path, [
        {"item_id": "1", "max_bid": 10}, {"item_id": "2", "max_bid": 20},
    ])

    def fake(method, path, **kwargs):
        if (method, path) == ("post", "/api/bids") and kwargs["json"]["item_id"] == "2":
            return (False, None, "Server returned 200 but the response body was not valid JSON")
        if (method, path) == ("get", "/health"):
            return (True, {}, None)
        return (True, {"item_id": kwargs["json"]["item_id"], "created": True}, None)

    runner = CliRunner()
    with patch("cli._server_url", return_value="http://srv"), \
         patch("cli._server_request_result", side_effect=fake), \
         patch("cli._record_adds"):
        result = runner.invoke(cli, ["add-batch", rows_file])

    assert result.exit_code == 1, result.output
    payload = _extract_json(result.output)
    assert [r["status"] for r in payload["rows"]] == ["added", "failed"]
    assert "not valid JSON" in payload["rows"][1]["error"]


# ---------------------------------------------------------------------------
# Initial health gate
# ---------------------------------------------------------------------------


def test_add_batch_halts_before_any_add_when_server_down(tmp_path):
    from cli import cli

    rows_file = _write_rows(tmp_path, [
        {"item_id": "1", "max_bid": 10}, {"item_id": "2", "max_bid": 20},
    ])
    fake = _fake_request_from({("get", "/health"): (False, None, "Server unreachable.")})
    runner = CliRunner()
    with patch("cli._server_url", return_value="http://srv"), \
         patch("cli._server_request_result", side_effect=fake):
        result = runner.invoke(cli, ["add-batch", rows_file])

    assert result.exit_code == 1
    assert "not responding" in result.output
    payload = _extract_json(result.output)
    assert payload["halted"] is True
    assert all(r["status"] == "not_attempted" for r in payload["rows"])


def _extract_json(output: str) -> dict:
    """Test helper: the CLI prints a human table, then a JSON blob, and
    (CliRunner mixes stdout/stderr) possibly a trailing warning line after
    that. Find the JSON object by locating the first '{' and decoding only
    that one balanced value — trailing text after it is ignored rather than
    tripping json.loads' "Extra data" on a full-string parse."""
    start = output.index("{")
    obj, _end = json.JSONDecoder().raw_decode(output[start:])
    return obj


# ---------------------------------------------------------------------------
# Happy path: all rows succeed
# ---------------------------------------------------------------------------


def test_add_batch_all_success_exit_zero_and_records_add_history(tmp_path):
    from cli import cli

    rows_file = _write_rows(tmp_path, [
        {"item_id": "1", "max_bid": 100}, {"item_id": "2", "max_bid": 200},
    ])
    fake = _fake_request_from({
        ("get", "/health"): (True, {"status": "ok"}, None),
        ("post", "/api/bids"): [
            (True, {"item_id": "1", "created": True}, None),
            (True, {"item_id": "2", "created": True}, None),
        ],
    })
    runner = CliRunner()
    with patch("cli._server_url", return_value="http://srv"), \
         patch("cli._server_request_result", side_effect=fake), \
         patch("cli._record_adds") as mock_record:
        result = runner.invoke(cli, ["add-batch", rows_file])

    assert result.exit_code == 0, result.output
    payload = _extract_json(result.output)
    assert payload["summary"]["total"] == 2
    assert payload["summary"]["added"] == 2
    assert payload["halted"] is False
    # One bulk call for the whole batch (not one read-modify-write per row).
    mock_record.assert_called_once_with(["1", "2"])


def test_add_batch_update_does_not_record_add_history(tmp_path):
    from cli import cli

    rows_file = _write_rows(tmp_path, [{"item_id": "1", "max_bid": 100}])
    fake = _fake_request_from({
        ("get", "/health"): (True, {"status": "ok"}, None),
        ("post", "/api/bids"): (True, {"item_id": "1", "created": False}, None),
    })
    runner = CliRunner()
    with patch("cli._server_url", return_value="http://srv"), \
         patch("cli._server_request_result", side_effect=fake), \
         patch("cli._record_adds") as mock_record:
        result = runner.invoke(cli, ["add-batch", rows_file])

    assert result.exit_code == 0, result.output
    payload = _extract_json(result.output)
    assert payload["rows"][0]["status"] == "updated"
    mock_record.assert_not_called()


# ---------------------------------------------------------------------------
# Mid-batch failure: server stays up -> continue; goes down -> halt
# ---------------------------------------------------------------------------


def test_add_batch_mid_batch_failure_continues_when_server_healthy(tmp_path):
    from cli import cli

    rows_file = _write_rows(tmp_path, [
        {"item_id": "1", "max_bid": 10}, {"item_id": "2", "max_bid": 20},
        {"item_id": "3", "max_bid": 30},
    ])
    fake = _fake_request_from({
        ("get", "/health"): [
            (True, {"status": "ok"}, None),   # pre-flight
            (True, {"status": "ok"}, None),   # re-check after row 2 fails
        ],
        ("post", "/api/bids"): [
            (True, {"item_id": "1", "created": True}, None),
            (False, None, "Server returned 500: boom"),
            (True, {"item_id": "3", "created": True}, None),
        ],
    })
    runner = CliRunner()
    with patch("cli._server_url", return_value="http://srv"), \
         patch("cli._server_request_result", side_effect=fake), \
         patch("cli._record_adds"):
        result = runner.invoke(cli, ["add-batch", rows_file])

    assert result.exit_code == 1, result.output
    payload = _extract_json(result.output)
    statuses = [r["status"] for r in payload["rows"]]
    assert statuses == ["added", "failed", "added"]
    assert payload["rows"][1]["error"] == "Server returned 500: boom"
    assert payload["halted"] is False
    assert "Failed" in result.output  # human table shows it


def test_add_batch_halts_remaining_rows_when_server_goes_down_mid_batch(tmp_path):
    from cli import cli

    rows_file = _write_rows(tmp_path, [
        {"item_id": "1", "max_bid": 10}, {"item_id": "2", "max_bid": 20},
        {"item_id": "3", "max_bid": 30},
    ])
    fake = _fake_request_from({
        ("get", "/health"): [
            (True, {"status": "ok"}, None),    # pre-flight
            (False, None, "Server unreachable."),  # re-check after row 2 fails
        ],
        ("post", "/api/bids"): [
            (True, {"item_id": "1", "created": True}, None),
            (False, None, "Server unreachable. Is the comics server running?"),
        ],
    })
    runner = CliRunner()
    with patch("cli._server_url", return_value="http://srv"), \
         patch("cli._server_request_result", side_effect=fake), \
         patch("cli._record_adds"):
        result = runner.invoke(cli, ["add-batch", rows_file])

    assert result.exit_code == 1, result.output
    payload = _extract_json(result.output)
    statuses = [r["status"] for r in payload["rows"]]
    assert statuses == ["added", "failed", "not_attempted"]
    assert payload["halted"] is True


def test_add_batch_never_all_success_table_after_a_failure(tmp_path):
    """A partial batch (any failure/not-attempted row) must never render as
    an all-added summary — the core BUI-168 guarantee."""
    from cli import cli

    rows_file = _write_rows(tmp_path, [{"item_id": "1", "max_bid": 10}, {"item_id": "2", "max_bid": 20}])
    fake = _fake_request_from({
        ("get", "/health"): [(True, {}, None), (False, None, "down")],
        ("post", "/api/bids"): [(False, None, "Server returned 503: down")],
    })
    runner = CliRunner()
    with patch("cli._server_url", return_value="http://srv"), \
         patch("cli._server_request_result", side_effect=fake), \
         patch("cli._record_adds"):
        result = runner.invoke(cli, ["add-batch", rows_file])

    assert result.exit_code != 0
    payload = _extract_json(result.output)
    assert payload["summary"]["added"] == 0
    assert payload["summary"]["failed"] == 1
    assert payload["summary"]["not_attempted"] == 1


# ---------------------------------------------------------------------------
# --json-out
# ---------------------------------------------------------------------------


def test_add_batch_writes_json_out_file(tmp_path):
    from cli import cli

    rows_file = _write_rows(tmp_path, [{"item_id": "1", "max_bid": 10}])
    out_path = tmp_path / "out.json"
    fake = _fake_request_from({
        ("get", "/health"): (True, {}, None),
        ("post", "/api/bids"): (True, {"item_id": "1", "created": True}, None),
    })
    runner = CliRunner()
    with patch("cli._server_url", return_value="http://srv"), \
         patch("cli._server_request_result", side_effect=fake), \
         patch("cli._record_adds"):
        result = runner.invoke(cli, ["add-batch", rows_file, "--json-out", str(out_path)])

    assert result.exit_code == 0, result.output
    written = json.loads(out_path.read_text())
    assert written["summary"]["added"] == 1


# ---------------------------------------------------------------------------
# --verify
# ---------------------------------------------------------------------------


def test_add_batch_verify_appends_verdicts(tmp_path):
    from cli import cli

    rows_file = _write_rows(tmp_path, [{"item_id": "1", "max_bid": 10, "grade": 9.2}])
    fake = _fake_request_from({
        ("get", "/health"): (True, {}, None),
        ("post", "/api/bids"): (True, {"item_id": "1", "created": True}, None),
        ("post", "/api/comics/verify"): (True, {
            "summary": {"total": 1, "fully_linked": 1, "issues": 0},
            "results": [{"item_id": "1", "verdict": "fully_linked"}],
        }, None),
    })
    runner = CliRunner()
    with patch("cli._server_url", return_value="http://srv"), \
         patch("cli._server_request_result", side_effect=fake), \
         patch("cli._record_adds"):
        result = runner.invoke(cli, ["add-batch", rows_file, "--verify"])

    assert result.exit_code == 0, result.output
    payload = _extract_json(result.output)
    assert payload["rows"][0]["verify"]["verdict"] == "fully_linked"


def test_add_batch_verify_skips_rows_without_grade(tmp_path):
    """A batch of entirely gradeless rows must not even attempt the verify
    POST (nothing to verify) — this exercises cli.py's `if items:` gate
    end-to-end: no fake response is queued for /api/comics/verify, so if the
    CLI incorrectly attempted that call the fake would raise
    AssertionError("no fake response queued...") and this test would fail."""
    from cli import cli

    rows_file = _write_rows(tmp_path, [{"item_id": "1", "max_bid": 10}])  # no grade
    fake = _fake_request_from({
        ("get", "/health"): (True, {}, None),
        ("post", "/api/bids"): (True, {"item_id": "1", "created": True}, None),
        # deliberately no ("post", "/api/comics/verify") entry
    })
    runner = CliRunner()
    with patch("cli._server_url", return_value="http://srv"), \
         patch("cli._server_request_result", side_effect=fake), \
         patch("cli._record_adds"):
        result = runner.invoke(cli, ["add-batch", rows_file, "--verify"])

    assert result.exit_code == 0, result.output
    payload = _extract_json(result.output)
    assert payload["rows"][0]["verify"] is None
    assert payload["verify_error"] is None


def test_add_batch_human_table_shows_link_error_detail(tmp_path):
    """A landed row whose link-fmv call failed must still show up as a
    success in the table (status not demoted), with the link error surfaced
    as detail rather than folded into the row's `error` field."""
    from cli import cli

    rows_file = _write_rows(tmp_path, [{"item_id": "1", "max_bid": 10, "comic_id": 187, "grade": 9.2}])
    fake = _fake_request_from({
        ("get", "/health"): (True, {}, None),
        ("post", "/api/bids"): (True, {"item_id": "1", "created": True}, None),
        ("post", "/api/bids/1/link-fmv"): (False, None, "Server returned 500: boom"),
    })
    runner = CliRunner()
    with patch("cli._server_url", return_value="http://srv"), \
         patch("cli._server_request_result", side_effect=fake), \
         patch("cli._record_adds"):
        result = runner.invoke(cli, ["add-batch", rows_file])

    assert result.exit_code == 0, result.output  # a link failure alone doesn't fail the batch
    payload = _extract_json(result.output)
    assert payload["rows"][0]["status"] == "added"
    assert payload["rows"][0]["error"] is None
    assert payload["rows"][0]["link_error"] == "Server returned 500: boom"
    assert "FMV link failed: Server returned 500: boom" in result.output


def test_add_batch_prints_title_in_human_table_and_json_rows(tmp_path):
    """BUI-506: a row carrying an optional `title` (e.g. from `gixen
    build-batch`'s rows.json) must show the comic's name in both the human
    table and the JSON summary rows, instead of a bare item_id."""
    from cli import cli

    rows_file = _write_rows(
        tmp_path, [{"item_id": "1", "max_bid": 10, "title": "Invincible #1"}]
    )
    fake = _fake_request_from({
        ("get", "/health"): (True, {}, None),
        ("post", "/api/bids"): (True, {"item_id": "1", "created": True}, None),
    })
    runner = CliRunner()
    with patch("cli._server_url", return_value="http://srv"), \
         patch("cli._server_request_result", side_effect=fake), \
         patch("cli._record_adds"):
        result = runner.invoke(cli, ["add-batch", rows_file])

    assert result.exit_code == 0, result.output
    assert "Invincible #1" in result.output
    payload = _extract_json(result.output)
    assert payload["rows"][0]["title"] == "Invincible #1"


def test_add_batch_absent_title_renders_placeholder_not_none(tmp_path):
    """A row with no `title` (every pre-BUI-506 rows.json) must render the
    same '—' placeholder the table already uses for other absent fields,
    not the literal string "None"."""
    from cli import cli

    rows_file = _write_rows(tmp_path, [{"item_id": "1", "max_bid": 10}])
    fake = _fake_request_from({
        ("get", "/health"): (True, {}, None),
        ("post", "/api/bids"): (True, {"item_id": "1", "created": True}, None),
    })
    runner = CliRunner()
    with patch("cli._server_url", return_value="http://srv"), \
         patch("cli._server_request_result", side_effect=fake), \
         patch("cli._record_adds"):
        result = runner.invoke(cli, ["add-batch", rows_file])

    assert result.exit_code == 0, result.output
    assert "None" not in result.output
    payload = _extract_json(result.output)
    assert payload["rows"][0]["title"] is None


def test_add_batch_verify_failure_recorded_but_does_not_change_exit_code(tmp_path):
    """--verify is a warn-only wrap step (mirrors verify.md) — a failed
    verify call must not flip an otherwise-successful add-batch to a
    failing exit code; it surfaces as verify_error instead."""
    from cli import cli

    rows_file = _write_rows(tmp_path, [{"item_id": "1", "max_bid": 10, "grade": 9.2}])
    fake = _fake_request_from({
        ("get", "/health"): (True, {}, None),
        ("post", "/api/bids"): (True, {"item_id": "1", "created": True}, None),
        ("post", "/api/comics/verify"): (False, None, "Server returned 500: verify boom"),
    })
    runner = CliRunner()
    with patch("cli._server_url", return_value="http://srv"), \
         patch("cli._server_request_result", side_effect=fake), \
         patch("cli._record_adds"):
        result = runner.invoke(cli, ["add-batch", rows_file, "--verify"])

    assert result.exit_code == 0, result.output
    payload = _extract_json(result.output)
    assert payload["verify_error"] == "Server returned 500: verify boom"
    assert "--verify" in result.output  # warning surfaced to the user


# ---------------------------------------------------------------------------
# Policy advisories (BUI-621/U7) — per-row marker, JSON summary, end-of-run count
# ---------------------------------------------------------------------------


def test_add_batch_row_with_advisories_marks_table_and_json(tmp_path):
    """A row whose POST /api/bids response carries KTD4 advisories must show
    a marker in the human table AND carry the full advisories verbatim in
    the JSON summary — the batch must never end looking clean."""
    from cli import cli

    rows_file = _write_rows(tmp_path, [{"item_id": "1", "max_bid": 10}])
    advisories = [{
        "code": "exposure_ceiling", "severity": "warning",
        "message": "over the group exposure ceiling", "data": {"limit": 500},
    }]
    fake = _fake_request_from({
        ("get", "/health"): (True, {}, None),
        ("post", "/api/bids"): (
            True, {"item_id": "1", "created": True, "advisories": advisories}, None,
        ),
    })
    runner = CliRunner()
    with patch("cli._server_url", return_value="http://srv"), \
         patch("cli._server_request_result", side_effect=fake), \
         patch("cli._record_adds"):
        result = runner.invoke(cli, ["add-batch", rows_file])

    assert result.exit_code == 0, result.output
    assert "advisor" in result.output.lower()  # per-row table marker
    payload = _extract_json(result.output)
    assert payload["rows"][0]["advisories"] == advisories
    assert payload["summary"]["advisories"] == 1
    # end-of-run count, distinct from the per-row table marker
    assert "1 advisory across this batch" in result.output


def test_add_batch_all_clean_batch_has_no_advisory_noise(tmp_path):
    """The inverse of the above: when no row carries an advisory, the table
    and end-of-run output must stay silent about advisories entirely."""
    from cli import cli

    rows_file = _write_rows(tmp_path, [{"item_id": "1", "max_bid": 10}])
    fake = _fake_request_from({
        ("get", "/health"): (True, {}, None),
        ("post", "/api/bids"): (True, {"item_id": "1", "created": True}, None),
    })
    runner = CliRunner()
    with patch("cli._server_url", return_value="http://srv"), \
         patch("cli._server_request_result", side_effect=fake), \
         patch("cli._record_adds"):
        result = runner.invoke(cli, ["add-batch", rows_file])

    assert result.exit_code == 0, result.output
    # "no advisory noise" means no human-visible marker (per-row table
    # suffix, end-of-run count line) — the JSON summary legitimately always
    # carries a neutral "advisories": 0 field, which is not noise.
    human_text = result.output.split("{", 1)[0]  # everything before the JSON blob
    assert "advisor" not in human_text.lower()
    assert "review before treating it as clean" not in result.output
    payload = _extract_json(result.output)
    assert payload["rows"][0]["advisories"] == []
    assert payload["summary"]["advisories"] == 0


def test_add_batch_advisory_count_survives_a_multi_row_batch(tmp_path):
    """Advisories from multiple rows all sum into the one end-of-run count,
    and each row's own advisories still land verbatim in the JSON summary."""
    from cli import cli

    rows_file = _write_rows(tmp_path, [
        {"item_id": "1", "max_bid": 10}, {"item_id": "2", "max_bid": 20},
    ])
    advisory_1 = {"code": "a", "severity": "warning", "message": "m1", "data": {}}
    advisory_2 = {"code": "b", "severity": "unevaluable", "message": "m2", "data": {}}
    fake = _fake_request_from({
        ("get", "/health"): (True, {}, None),
        ("post", "/api/bids"): [
            (True, {"item_id": "1", "created": True, "advisories": [advisory_1]}, None),
            (True, {"item_id": "2", "created": True, "advisories": [advisory_2]}, None),
        ],
    })
    runner = CliRunner()
    with patch("cli._server_url", return_value="http://srv"), \
         patch("cli._server_request_result", side_effect=fake), \
         patch("cli._record_adds"):
        result = runner.invoke(cli, ["add-batch", rows_file])

    assert result.exit_code == 0, result.output
    payload = _extract_json(result.output)
    assert payload["rows"][0]["advisories"] == [advisory_1]
    assert payload["rows"][1]["advisories"] == [advisory_2]
    assert payload["summary"]["advisories"] == 2
    assert "2 advisories across this batch" in result.output


def test_add_batch_old_server_response_without_advisories_key_does_not_crash(tmp_path):
    """An old server's 2xx response predates the KTD4 envelope entirely (no
    `advisories` key) — add-batch must degrade to zero advisories, not
    crash."""
    from cli import cli

    rows_file = _write_rows(tmp_path, [{"item_id": "1", "max_bid": 10}])
    fake = _fake_request_from({
        ("get", "/health"): (True, {}, None),
        # No "advisories" key at all — the shape a pre-KTD4 server returns.
        ("post", "/api/bids"): (True, {"item_id": "1", "created": True}, None),
    })
    runner = CliRunner()
    with patch("cli._server_url", return_value="http://srv"), \
         patch("cli._server_request_result", side_effect=fake), \
         patch("cli._record_adds"):
        result = runner.invoke(cli, ["add-batch", rows_file])

    assert result.exit_code == 0, result.output
    payload = _extract_json(result.output)
    assert payload["rows"][0]["advisories"] == []
    assert payload["summary"]["advisories"] == 0


def test_add_batch_source_batch_reaches_the_add_payload(tmp_path):
    """Every add-batch row is provenance-tagged "batch" in the POST
    /api/bids payload (BUI-621/U7)."""
    from cli import cli

    rows_file = _write_rows(tmp_path, [{"item_id": "1", "max_bid": 10}])
    captured = {}

    def fake(method, path, **kwargs):
        if (method, path) == ("post", "/api/bids"):
            captured["json"] = kwargs.get("json")
            return (True, {"item_id": "1", "created": True}, None)
        return (True, {}, None)

    runner = CliRunner()
    with patch("cli._server_url", return_value="http://srv"), \
         patch("cli._server_request_result", side_effect=fake), \
         patch("cli._record_adds"):
        result = runner.invoke(cli, ["add-batch", rows_file])

    assert result.exit_code == 0, result.output
    assert captured["json"]["source"] == "batch"


# ---------------------------------------------------------------------------
# BUI-623 (U9): a 409 policy block is BLOCKED, distinct from FAILED, and the
# batch continues past it (no health re-check, no halt).
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


def test_add_batch_ae9_blocked_row_continues_batch_with_two_added(tmp_path):
    """AE9: one over-FMV (blocked) row between two clean rows ends with 2
    added + 1 BLOCKED, and the batch does not halt."""
    from cli import cli

    rows_file = _write_rows(tmp_path, [
        {"item_id": "1", "max_bid": 10}, {"item_id": "2", "max_bid": 999},
        {"item_id": "3", "max_bid": 10},
    ])
    fake = _fake_request_from({
        ("get", "/health"): (True, {}, None),
        ("post", "/api/bids"): [
            (True, {"item_id": "1", "created": True}, None),
            (False, _BLOCK_DETAIL, "Server returned 409: ..."),
            (True, {"item_id": "3", "created": True}, None),
        ],
    })
    runner = CliRunner()
    with patch("cli._server_url", return_value="http://srv"), \
         patch("cli._server_request_result", side_effect=fake), \
         patch("cli._record_adds"):
        result = runner.invoke(cli, ["add-batch", rows_file])

    payload = _extract_json(result.output)
    assert [r["status"] for r in payload["rows"]] == ["added", "blocked", "added"]
    assert payload["summary"]["added"] == 2
    assert payload["summary"]["blocked"] == 1
    assert payload["halted"] is False
    # BLOCKED is not a health-check trigger — /health was never called
    # beyond the pre-flight gate (which "get"/"/health" above always
    # satisfies, so the fake never runs dry).
    assert "🚫 Blocked" in result.output
    assert "Blocked by policy check(s): over_fmv." in result.output


def test_add_batch_blocked_row_trips_nonzero_exit_code(tmp_path):
    """A batch that lands 2 added + 1 blocked must not exit 0 — see
    add_batch.py's exit_code() docstring for the 'row did not land' logic
    this extends from FAILED/NOT_ATTEMPTED to BLOCKED."""
    from cli import cli

    rows_file = _write_rows(tmp_path, [{"item_id": "1", "max_bid": 999}])
    fake = _fake_request_from({
        ("get", "/health"): (True, {}, None),
        ("post", "/api/bids"): (False, _BLOCK_DETAIL, "Server returned 409: ..."),
    })
    runner = CliRunner()
    with patch("cli._server_url", return_value="http://srv"), \
         patch("cli._server_request_result", side_effect=fake), \
         patch("cli._record_adds"):
        result = runner.invoke(cli, ["add-batch", rows_file])

    assert result.exit_code == 1, result.output


def test_add_batch_blocked_row_never_records_add_history(tmp_path):
    """A BLOCKED row never landed — it must not be treated as a create for
    the --added-since history the way an ADDED row is."""
    from cli import cli

    rows_file = _write_rows(tmp_path, [{"item_id": "1", "max_bid": 999}])
    fake = _fake_request_from({
        ("get", "/health"): (True, {}, None),
        ("post", "/api/bids"): (False, _BLOCK_DETAIL, "Server returned 409: ..."),
    })
    runner = CliRunner()
    with patch("cli._server_url", return_value="http://srv"), \
         patch("cli._server_request_result", side_effect=fake), \
         patch("cli._record_adds") as mock_record:
        runner.invoke(cli, ["add-batch", rows_file])

    mock_record.assert_not_called()


def test_add_batch_per_row_policy_bypass_reaches_the_payload(tmp_path):
    """A ROWS_FILE row's own "policy_bypass": true reaches the POST
    /api/bids payload verbatim."""
    from cli import cli

    rows_file = _write_rows(tmp_path, [
        {"item_id": "1", "max_bid": 999, "policy_bypass": True},
    ])
    captured = {}

    def fake(method, path, **kwargs):
        if (method, path) == ("post", "/api/bids"):
            captured["json"] = kwargs.get("json")
            return (True, {"item_id": "1", "created": True}, None)
        return (True, {}, None)

    runner = CliRunner()
    with patch("cli._server_url", return_value="http://srv"), \
         patch("cli._server_request_result", side_effect=fake), \
         patch("cli._record_adds"):
        result = runner.invoke(cli, ["add-batch", rows_file])

    assert result.exit_code == 0, result.output
    assert captured["json"]["policy_bypass"] is True
