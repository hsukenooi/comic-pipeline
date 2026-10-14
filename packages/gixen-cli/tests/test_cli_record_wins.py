"""CLI tests for `gixen record-wins` (BUI-1267): the unattended half of
/comic:collection-add. Prep logic is covered by test_record_win_prep.py;
these cover the commit call, the heartbeat verdict, and the notification."""
import json
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from click.testing import CliRunner

from record_win_prep import RecordWinPrepError

COMMIT = "/api/comics/collection/record-win/commit"


def _payload(wins=(), needs_review=()):
    return {
        "wins": list(wins),
        "needs_review": list(needs_review),
        "total_ended_won": len(wins) + len(needs_review),
        "new_win_count": len(wins) + len(needs_review),
    }


def _run(payload, commit_result=(True, {"rows_written": 1, "marked_seen": 1}, None), snipes_ok=True):
    from cli import cli

    calls = []

    def fake_request(method, path, **kwargs):
        calls.append((method, path, kwargs))
        if path == "/api/snipes":
            return (True, [], None) if snipes_ok else (False, None, "Server unreachable.")
        assert path == COMMIT
        return commit_result

    with patch("cli._server_url", return_value="http://srv"), \
         patch("cli._server_request_result", side_effect=fake_request), \
         patch("cli.build_payload", return_value=payload) if not isinstance(payload, Exception) \
            else patch("cli.build_payload", side_effect=payload), \
         patch("cli._ping_record_wins") as ping, \
         patch("cli._notify") as notify:
        result = CliRunner().invoke(cli, ["record-wins"])
    return result, calls, ping, notify


def test_commits_ready_wins_without_resolved_reviews_and_pings_ok():
    result, calls, ping, notify = _run(_payload(wins=[{"item_id": "1"}]))

    assert result.exit_code == 0, result.output
    method, path, kwargs = calls[-1]
    assert (method, path) == ("post", COMMIT)
    assert kwargs["json"] == {"wins": [{"item_id": "1"}], "resolved_reviews": []}
    assert kwargs["timeout"] >= 600
    assert json.loads(result.output.splitlines()[-1])["rows_written"] == 1
    ping.assert_called_once()
    assert ping.call_args.args[1] is True
    notify.assert_not_called()


def test_needs_review_only_notifies_skips_commit_and_still_pings_ok():
    result, calls, ping, notify = _run(_payload(needs_review=[{"item_id": "2"}]))

    assert result.exit_code == 0, result.output
    assert all(path != COMMIT for _, path, _ in calls)
    notify.assert_called_once()
    assert "1 won auction(s) need review" in notify.call_args.args[0]
    assert ping.call_args.args[1] is True


def test_nothing_new_pings_ok():
    result, calls, ping, notify = _run(_payload())

    assert result.exit_code == 0, result.output
    assert ping.call_args.args[1] is True
    notify.assert_not_called()


def test_commit_http_error_pings_failure():
    result, _, ping, _ = _run(
        _payload(wins=[{"item_id": "1"}]),
        commit_result=(False, None, "Server returned 500: partial_failure"),
    )

    assert result.exit_code == 1
    assert ping.call_args.args[1] is False
    assert "500" in ping.call_args.args[2]


def test_commit_timeout_is_reported_as_indeterminate_failure():
    result, _, ping, _ = _run(
        _payload(wins=[{"item_id": "1"}]),
        commit_result=(False, {"indeterminate": True, "reason": "timeout"}, "timed out"),
    )

    assert result.exit_code == 1
    assert ping.call_args.args[1] is False
    assert "may still have landed" in ping.call_args.args[2]


def test_prep_error_pings_failure_and_never_commits():
    result, calls, ping, _ = _run(RecordWinPrepError("comic-identify not found"))

    assert result.exit_code == 1
    assert all(path != COMMIT for _, path, _ in calls)
    assert ping.call_args.args[1] is False


def test_snipe_list_failure_pings_failure():
    result, _, ping, _ = _run(_payload(), snipes_ok=False)

    assert result.exit_code == 1
    assert ping.call_args.args[1] is False


def test_requires_comics_server_url():
    from cli import cli

    with patch("cli._server_url", return_value=None):
        result = CliRunner().invoke(cli, ["record-wins"])

    assert result.exit_code == 1
    assert "COMICS_SERVER_URL is not set" in result.output
