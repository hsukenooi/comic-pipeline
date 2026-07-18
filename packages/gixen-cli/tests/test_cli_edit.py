"""BUI-401: `gixen edit` server-mode PATCH-body composition.

A max_bid-only edit must OMIT bid_offset/snipe_group from the PATCH body so the
server's None-passthrough preserves the stored fire-offset and group, instead of
the pre-BUI-401 defaults (--offset 6 / --group 0) resetting them. Explicit
options are still sent through.

BUI-404: the direct-mode branch (COMICS_SERVER_URL unset) has no local store to
resolve "keep current" from, so it resolves omitted fields from a `list_snipes`
lookup before calling `modify_snipe` instead of falling through to
`modify_snipe`'s own 6 / 0 defaults.
"""
import os
import sys
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from cli import cli  # noqa: E402
from gixen_client import GixenError, GixenSnipeNotFoundError  # noqa: E402


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


# ---------------------------------------------------------------------------
# BUI-404: direct-mode `edit` resolves omitted fields via list_snipes
# ---------------------------------------------------------------------------

def _make_mock_client(snipes):
    mock_client = MagicMock()
    mock_client.list_snipes.return_value = snipes
    return mock_client


def _run_direct_edit(args, mock_client):
    with patch("cli._server_url", return_value=None), \
         patch("cli._make_client", return_value=mock_client):
        result = CliRunner().invoke(cli, ["edit", *args])
    return result


class TestDirectModeEditPassthrough:
    def test_max_bid_only_preserves_current_offset_and_group(self):
        """A bare `edit <id> <bid>` in direct mode must look up the snipe's
        live offset/group on Gixen and pass those through, instead of falling
        to modify_snipe's own 6/0 defaults (the BUI-404 bug)."""
        snipes = [{
            "item_id": "200000001", "bid_offset": "9", "snipe_group": "3",
            "dbidid": "555",
        }]
        mock_client = _make_mock_client(snipes)

        result = _run_direct_edit(["200000001", "75.0"], mock_client)

        assert result.exit_code == 0, result.output
        mock_client.list_snipes.assert_called_once()
        mock_client.modify_snipe.assert_called_once()
        args, kwargs = mock_client.modify_snipe.call_args
        assert args[0] == "200000001"
        assert kwargs["bid_offset"] == 9
        assert kwargs["snipe_group"] == 3
        # dbidid is deliberately NOT reused from the lookup — modify_snipe
        # resolves it fresh immediately before its own POST (no added
        # stale-dbidid window versus pre-fix behavior).
        assert "dbidid" not in kwargs

    def test_explicit_offset_overrides_looked_up_value(self):
        """An explicitly-passed --offset is honored over the current value on
        Gixen; --group (omitted) is still resolved from the lookup."""
        snipes = [{
            "item_id": "200000001", "bid_offset": "9", "snipe_group": "3",
            "dbidid": "555",
        }]
        mock_client = _make_mock_client(snipes)

        result = _run_direct_edit(
            ["200000001", "75.0", "--offset", "12"], mock_client
        )

        assert result.exit_code == 0, result.output
        args, kwargs = mock_client.modify_snipe.call_args
        assert kwargs["bid_offset"] == 12  # explicit value wins, not 9
        assert kwargs["snipe_group"] == 3  # still resolved (omitted by user)

    def test_explicit_group_overrides_looked_up_value(self):
        """An explicitly-passed --group is honored over the current value on
        Gixen; --offset (omitted) is still resolved from the lookup."""
        snipes = [{
            "item_id": "200000001", "bid_offset": "9", "snipe_group": "3",
            "dbidid": "555",
        }]
        mock_client = _make_mock_client(snipes)

        result = _run_direct_edit(
            ["200000001", "75.0", "--group", "0"], mock_client
        )

        assert result.exit_code == 0, result.output
        args, kwargs = mock_client.modify_snipe.call_args
        assert kwargs["snipe_group"] == 0  # explicit un-group wins, not 3
        assert kwargs["bid_offset"] == 9  # still resolved (omitted by user)

    def test_both_explicit_skips_list_snipes_lookup(self):
        """When the user supplies both fields there's nothing to resolve — no
        extra list_snipes round trip (matches pre-fix behavior/cost)."""
        mock_client = MagicMock()

        result = _run_direct_edit(
            ["200000001", "75.0", "--offset", "5", "--group", "2"],
            mock_client,
        )

        assert result.exit_code == 0, result.output
        mock_client.list_snipes.assert_not_called()
        args, kwargs = mock_client.modify_snipe.call_args
        assert kwargs == {"bid_offset": 5, "snipe_group": 2}

    def test_item_not_found_in_list_snipes_aborts(self):
        """A max_bid-only edit for an item not in the live list must fail
        loudly rather than proceed with guessed/default offset+group."""
        mock_client = _make_mock_client([])  # empty list -> not found

        result = _run_direct_edit(["999999999", "75.0"], mock_client)

        assert result.exit_code == 1
        assert "not found" in result.output
        mock_client.modify_snipe.assert_not_called()

    def test_list_snipes_failure_aborts_without_defaults(self):
        """If the resolve round-trip itself fails (network/parse error), the
        edit must abort — never silently fall back to modify_snipe's 6/0
        defaults, and never crash uncaught."""
        mock_client = MagicMock()
        mock_client.list_snipes.side_effect = GixenError("Gixen unreachable")

        result = _run_direct_edit(["200000001", "75.0"], mock_client)

        assert result.exit_code == 1
        assert "Gixen unreachable" in result.output
        mock_client.modify_snipe.assert_not_called()

    def test_unknown_snipe_group_aborts_instead_of_defaulting_to_zero(self):
        """BUI-383: an unparseable/blank snipe_group is "unknown", not "0".
        Coercing it to 0 would silently un-group the snipe, so the edit must
        fail closed and ask the caller to pass --group explicitly."""
        snipes = [{
            "item_id": "200000001", "bid_offset": "9", "snipe_group": None,
            "dbidid": "555",
        }]
        mock_client = _make_mock_client(snipes)

        result = _run_direct_edit(["200000001", "75.0"], mock_client)

        assert result.exit_code == 1
        assert "--group" in result.output
        mock_client.modify_snipe.assert_not_called()

    def test_unknown_bid_offset_aborts_instead_of_defaulting_to_six(self):
        """Symmetric to the snipe_group case above: a blank/unparseable
        bid_offset must not be silently coerced to the hardcoded default (6)
        — that's the exact silent-reset bug BUI-404 fixes, just on the other
        field. The edit must fail closed and ask for --offset explicitly."""
        snipes = [{
            "item_id": "200000001", "bid_offset": "", "snipe_group": "3",
            "dbidid": "555",
        }]
        mock_client = _make_mock_client(snipes)

        result = _run_direct_edit(["200000001", "75.0"], mock_client)

        assert result.exit_code == 1
        assert "--offset" in result.output
        mock_client.modify_snipe.assert_not_called()

    def test_snipe_not_found_exception_from_modify_snipe_still_handled(self):
        """The existing GixenSnipeNotFoundError handling around the final
        modify_snipe call is preserved (e.g. a TOCTOU where the snipe vanishes
        between the lookup and the modify POST)."""
        snipes = [{
            "item_id": "200000001", "bid_offset": "9", "snipe_group": "3",
            "dbidid": "555",
        }]
        mock_client = _make_mock_client(snipes)
        mock_client.modify_snipe.side_effect = GixenSnipeNotFoundError(
            "Item 200000001 not found in your Gixen snipe list"
        )

        result = _run_direct_edit(["200000001", "75.0"], mock_client)

        assert result.exit_code == 1
        assert "not found" in result.output
