"""CLI wiring tests for `gixen build-batch` (BUI-435).

add_batch.py's own merge logic (per-row resolution, comic_id/skip/override
rules) is covered by tests/test_add_batch.py; these tests only cover cli.py's
plumbing — reading/parsing the three input files, wiring them into
`build_batch_rows`, printing the rows JSON, writing --out, and the process
exit code. No network is ever touched (build-batch never calls the server).
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from click.testing import CliRunner


def _write_json(tmp_path, name, data):
    path = tmp_path / name
    path.write_text(json.dumps(data))
    return str(path)


def _brief_row(item_id="1", comic_id=42, max_bid=100):
    return {
        "item_id": item_id,
        "comic_id": comic_id,
        "fmv_id": 7,
        "max_bid": max_bid,
        "flag_reason": None,
        "confidence": "HIGH",
    }


def test_build_batch_happy_path_prints_rows_json(tmp_path):
    from cli import cli

    brief_file = _write_json(tmp_path, "brief.json", [_brief_row("1", max_bid=800)])
    wl_file = _write_json(tmp_path, "wl.json", [{"item_id": "1", "grade": 9.2}])

    runner = CliRunner()
    result = runner.invoke(cli, ["build-batch", brief_file, wl_file])

    assert result.exit_code == 0, result.output
    assert '"item_id": "1"' in result.output
    assert '"comic_id": 42' in result.output
    assert '"max_bid": 800.0' in result.output
    assert '"grade": 9.2' in result.output


def test_build_batch_writes_out_file_as_bare_rows_list(tmp_path):
    from cli import cli

    brief_file = _write_json(tmp_path, "brief.json", [_brief_row("1", max_bid=50)])
    wl_file = _write_json(tmp_path, "wl.json", [{"item_id": "1"}])
    out_file = tmp_path / "rows_out.json"

    runner = CliRunner()
    result = runner.invoke(cli, ["build-batch", brief_file, wl_file, "--out", str(out_file)])

    assert result.exit_code == 0, result.output
    written = json.loads(out_file.read_text())
    assert written == [{"item_id": "1", "max_bid": 50.0, "comic_id": 42}]


def test_build_batch_out_file_is_directly_consumable_by_add_batch_parse_rows(tmp_path):
    """The whole point of the builder is that its --out is a valid add-batch
    ROWS_FILE with no further hand-editing."""
    from add_batch import parse_rows
    from cli import cli

    brief_file = _write_json(tmp_path, "brief.json", [_brief_row("1", max_bid=50)])
    wl_file = _write_json(tmp_path, "wl.json", [{"item_id": "1", "grade": 9.0}])
    out_file = tmp_path / "rows_out.json"

    runner = CliRunner()
    result = runner.invoke(cli, ["build-batch", brief_file, wl_file, "--out", str(out_file)])

    assert result.exit_code == 0, result.output
    rows = parse_rows(json.loads(out_file.read_text()))
    assert rows == [{"item_id": "1", "max_bid": 50.0, "comic_id": 42, "grade": 9.0}]


def test_build_batch_applies_overrides_file(tmp_path):
    from cli import cli

    brief_file = _write_json(tmp_path, "brief.json", [_brief_row("1", max_bid=800)])
    wl_file = _write_json(tmp_path, "wl.json", [{"item_id": "1"}])
    overrides_file = _write_json(tmp_path, "overrides.json", {"1": {"max_bid": 650, "group": 3}})

    runner = CliRunner()
    result = runner.invoke(
        cli, ["build-batch", brief_file, wl_file, "--overrides", overrides_file]
    )

    assert result.exit_code == 0, result.output
    assert '"max_bid": 650.0' in result.output
    assert '"group": 3' in result.output


def test_build_batch_reports_skipped_and_unlinked_rows_to_stderr(tmp_path):
    from cli import cli

    brief_file = _write_json(
        tmp_path,
        "brief.json",
        [_brief_row("1", comic_id=None, max_bid=50), _brief_row("2", max_bid=60)],
    )
    wl_file = _write_json(
        tmp_path,
        "wl.json",
        [{"item_id": "1"}, {"item_id": "2", "listing_type": "BIN"}],
    )

    runner = CliRunner()
    result = runner.invoke(cli, ["build-batch", brief_file, wl_file])

    assert result.exit_code == 0, result.output
    assert "comic_id null" in result.output
    assert "Skipped" in result.output and "bin" in result.output.lower()


def test_build_batch_exits_nonzero_on_missing_brief_row(tmp_path):
    """A working-list item with no matching brief row and no skip override
    must hard-fail the CLI, not silently emit a partial batch."""
    from cli import cli

    brief_file = _write_json(tmp_path, "brief.json", [])
    wl_file = _write_json(tmp_path, "wl.json", [{"item_id": "1"}])

    runner = CliRunner()
    result = runner.invoke(cli, ["build-batch", brief_file, wl_file])

    assert result.exit_code == 1
    assert "no matching comic-fmv --brief row" in result.output


def test_build_batch_exits_nonzero_on_needs_manual_row_without_resolution(tmp_path):
    from cli import cli

    brief_file = _write_json(
        tmp_path, "brief.json", [{"item_id": "1", "comic_id": 42, "max_bid": None, "flag_reason": "one_sided"}]
    )
    wl_file = _write_json(tmp_path, "wl.json", [{"item_id": "1"}])

    runner = CliRunner()
    result = runner.invoke(cli, ["build-batch", brief_file, wl_file])

    assert result.exit_code == 1
    assert "needs-manual" in result.output


def test_build_batch_accepts_raw_comic_fmv_stdout_capture_for_brief_file(tmp_path):
    """BRIEF_FILE doesn't have to be pre-cleaned JSON — the raw captured
    stdout of `comic-fmv --brief` (human table + JSON lines) works too."""
    from cli import cli

    raw_stdout = (
        "# Comic FMV table header\n"
        "1  Amazing Spider-Man #300  $800-1000  $800\n"
        + json.dumps(_brief_row("1", max_bid=800)) + "\n"
    )
    brief_file = tmp_path / "brief_raw.txt"
    brief_file.write_text(raw_stdout)
    wl_file = _write_json(tmp_path, "wl.json", [{"item_id": "1"}])

    runner = CliRunner()
    result = runner.invoke(cli, ["build-batch", str(brief_file), wl_file])

    assert result.exit_code == 0, result.output
    assert '"item_id": "1"' in result.output
    assert '"comic_id": 42' in result.output


def test_build_batch_rejects_malformed_working_list_json(tmp_path):
    from cli import cli

    brief_file = _write_json(tmp_path, "brief.json", [_brief_row("1", max_bid=50)])
    wl_file = tmp_path / "wl.json"
    wl_file.write_text("{not json")

    runner = CliRunner()
    result = runner.invoke(cli, ["build-batch", brief_file, str(wl_file)])

    assert result.exit_code == 1
    assert "could not read/parse" in result.output


def test_build_batch_rejects_non_list_working_list_shape(tmp_path):
    from cli import cli

    brief_file = _write_json(tmp_path, "brief.json", [_brief_row("1", max_bid=50)])
    wl_file = _write_json(tmp_path, "wl.json", {"oops": "not a list"})

    runner = CliRunner()
    result = runner.invoke(cli, ["build-batch", brief_file, str(wl_file)])

    assert result.exit_code == 1
    assert "JSON list" in result.output


def test_build_batch_rejects_non_dict_overrides_shape(tmp_path):
    from cli import cli

    brief_file = _write_json(tmp_path, "brief.json", [_brief_row("1", max_bid=50)])
    wl_file = _write_json(tmp_path, "wl.json", [{"item_id": "1"}])
    overrides_file = _write_json(tmp_path, "overrides.json", ["not", "a", "dict"])

    runner = CliRunner()
    result = runner.invoke(
        cli, ["build-batch", brief_file, wl_file, "--overrides", overrides_file]
    )

    assert result.exit_code == 1
    assert "JSON object" in result.output
