"""CLI wiring tests for `gixen record-win-prep` (BUI-352/353).

record_win_prep.py's own logic (filter/dedup/seen-fetch hardening/identify
mapping/gating) is covered by tests/test_record_win_prep.py; these tests only
cover cli.py's plumbing — resolving COMICS_SERVER_URL, fetching snipes, and
surfacing a RecordWinPrepError as a clean CLI failure.
"""
import json
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from click.testing import CliRunner

from record_win_prep import RecordWinPrepError


def test_record_win_prep_requires_comics_server_url():
    """BUI-352: no ambient fallback here — a missing URL is a hard stop, not
    the old inline `curl ... || echo ""` that silently produced an empty
    seen-set."""
    from cli import cli

    runner = CliRunner()
    with patch("cli._server_url", return_value=None):
        result = runner.invoke(cli, ["record-win-prep"])

    assert result.exit_code == 1
    assert "COMICS_SERVER_URL is not set" in result.output


def test_record_win_prep_prints_payload_to_stdout():
    from cli import cli

    runner = CliRunner()
    fake_payload = {"wins": [], "needs_review": [], "total_ended_won": 0, "new_win_count": 0}

    with patch("cli._server_url", return_value="http://srv"), \
         patch("cli._server_request", return_value=[]), \
         patch("cli.build_payload", return_value=fake_payload) as mock_build:
        result = runner.invoke(cli, ["record-win-prep"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == fake_payload
    mock_build.assert_called_once_with([], "http://srv")


def test_record_win_prep_writes_output_file(tmp_path):
    from cli import cli

    runner = CliRunner()
    fake_payload = {
        "wins": [{"item_id": "1"}],
        "needs_review": [{"item_id": "2"}],
        "total_ended_won": 2,
        "new_win_count": 2,
    }
    out_path = tmp_path / "prep.json"

    with patch("cli._server_url", return_value="http://srv"), \
         patch("cli._server_request", return_value=[{"item_id": "1"}, {"item_id": "2"}]), \
         patch("cli.build_payload", return_value=fake_payload):
        result = runner.invoke(cli, ["record-win-prep", "--output", str(out_path)])

    assert result.exit_code == 0, result.output
    assert json.loads(out_path.read_text()) == fake_payload
    assert "2 new win(s)" in result.output
    assert "1 need review" in result.output
    # The full needs_review detail lives in the file, not dumped into stdout.
    assert "item_id" not in result.output


def test_record_win_prep_surfaces_hard_stop_as_clean_failure():
    """A RecordWinPrepError (BUI-352 connectivity hard-stop, or BUI-353's
    identify line-count mismatch) must exit non-zero, not raise a traceback
    into the skill's bash block."""
    from cli import cli

    runner = CliRunner()
    with patch("cli._server_url", return_value="http://srv"), \
         patch("cli._server_request", return_value=[]), \
         patch("cli.build_payload", side_effect=RecordWinPrepError("cannot reach comics server")):
        result = runner.invoke(cli, ["record-win-prep"])

    assert result.exit_code == 1
    assert "cannot reach comics server" in result.output


def test_record_win_prep_snipes_fetch_failure_exits_cleanly():
    """`_server_request` (an existing cli.py helper, reused here to fetch
    /api/snipes) already converts a non-2xx/connection failure into a clean
    `sys.exit(1)` for every other command in this file — confirm this command
    inherits that behavior rather than crashing with an unhandled traceback."""
    from cli import cli

    def fake_server_request(method, path, **kwargs):
        # Mirrors _server_request's own real failure path (cli.py:73-89).
        import sys as _sys
        _sys.exit(1)

    runner = CliRunner()
    with patch("cli._server_url", return_value="http://srv"), \
         patch("cli._server_request", side_effect=fake_server_request):
        result = runner.invoke(cli, ["record-win-prep"])

    assert result.exit_code == 1
    assert result.exception is None or isinstance(result.exception, SystemExit)
