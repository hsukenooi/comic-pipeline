"""BUI-401: `gixen edit` server-mode PATCH-body composition.

A max_bid-only edit must OMIT bid_offset/snipe_group from the PATCH body so the
server's None-passthrough preserves the stored fire-offset and group, instead of
the pre-BUI-401 defaults (--offset 6 / --group 0) resetting them. Explicit
options are still sent through.
"""
import os
import sys
from unittest.mock import patch

from click.testing import CliRunner

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from cli import cli  # noqa: E402


def _run_edit(args):
    """Invoke `gixen edit` in server mode, capturing the PATCH json payload."""
    captured = {}

    def fake_request(method, path, **kwargs):
        captured["method"] = method
        captured["path"] = path
        captured["json"] = kwargs.get("json")
        return {}

    with patch("cli._server_url", return_value="http://srv"), \
         patch("cli._server_request", side_effect=fake_request):
        result = CliRunner().invoke(cli, ["edit", *args])
    return result, captured


def test_edit_max_bid_only_omits_offset_and_group():
    """Bare `edit <id> <bid>` sends only max_bid — both passthrough fields are
    omitted so the server preserves them."""
    result, captured = _run_edit(["200000001", "75.0"])
    assert result.exit_code == 0, result.output
    assert captured["method"] == "patch"
    assert captured["path"] == "/api/bids/200000001"
    assert captured["json"] == {"max_bid": 75.0}


def test_edit_includes_offset_and_group_when_provided():
    """Explicit --offset/--group ARE sent (a real change)."""
    result, captured = _run_edit(
        ["200000001", "75.0", "--offset", "9", "--group", "3"]
    )
    assert result.exit_code == 0, result.output
    assert captured["json"] == {"max_bid": 75.0, "bid_offset": 9, "snipe_group": 3}


def test_edit_explicit_group_zero_is_sent():
    """--group 0 is an explicit un-group request — it must be sent even though
    it equals the pre-BUI-401 default, and bid_offset stays omitted."""
    result, captured = _run_edit(["200000001", "75.0", "--group", "0"])
    assert result.exit_code == 0, result.output
    assert captured["json"] == {"max_bid": 75.0, "snipe_group": 0}


def test_edit_only_offset_provided_omits_group():
    result, captured = _run_edit(["200000001", "75.0", "--offset", "3"])
    assert result.exit_code == 0, result.output
    assert captured["json"] == {"max_bid": 75.0, "bid_offset": 3}
