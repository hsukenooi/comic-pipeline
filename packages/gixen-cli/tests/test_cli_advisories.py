"""BUI-621 (U7): `gixen add`/`edit` render the KTD4 policy advisory envelope
(server/policy.py) to stderr, after the existing success line, never
changing the exit code and never touching the direct-Gixen path (there is
no server response to render there). See tests/test_add_batch.py and
tests/test_cli_add_batch.py for the equivalent add-batch coverage (per-row
table marker, JSON summary, end-of-run count).
"""
import os
import sys
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from cli import cli  # noqa: E402

_ADVISORY = {
    "code": "exposure_ceiling",
    "severity": "warning",
    "message": "over the group exposure ceiling",
    "data": {"limit": 500},
}


# ---------------------------------------------------------------------------
# `gixen add` — server mode
# ---------------------------------------------------------------------------


def test_add_prints_advisory_after_success_line():
    runner = CliRunner()
    with patch("cli._server_url", return_value="http://srv"), \
         patch("cli._server_request",
               return_value={"item_id": "444", "created": True, "advisories": [_ADVISORY]}), \
         patch("cli._record_add"):
        result = runner.invoke(cli, ["add", "444", "20.00"])

    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    added_idx = next(i for i, line in enumerate(lines) if "Added snipe" in line)
    advisory_idx = next(i for i, line in enumerate(lines) if "exposure_ceiling" in line)
    assert advisory_idx > added_idx, "advisory must print AFTER the success line"
    assert "over the group exposure ceiling" in result.output


def test_add_clean_response_prints_no_advisory_noise():
    runner = CliRunner()
    with patch("cli._server_url", return_value="http://srv"), \
         patch("cli._server_request",
               return_value={"item_id": "444", "created": True, "advisories": []}), \
         patch("cli._record_add"):
        result = runner.invoke(cli, ["add", "444", "20.00"])

    assert result.exit_code == 0, result.output
    assert "advisor" not in result.output.lower()


def test_add_old_server_response_without_advisories_key_does_not_crash():
    """An old server's 2xx response predates the KTD4 envelope entirely (no
    `advisories` key at all) — must degrade to zero advisories, not crash."""
    runner = CliRunner()
    with patch("cli._server_url", return_value="http://srv"), \
         patch("cli._server_request", return_value={"item_id": "444", "created": True}), \
         patch("cli._record_add"):
        result = runner.invoke(cli, ["add", "444", "20.00"])

    assert result.exit_code == 0, result.output
    assert result.exception is None
    assert "Added snipe" in result.output


def test_add_malformed_advisory_entry_does_not_crash():
    """A non-dict entry inside an otherwise-list `advisories` value (a
    genuine server bug, distinct from the envelope-absent case) must still
    degrade gracefully rather than raising out of the CLI."""
    runner = CliRunner()
    with patch("cli._server_url", return_value="http://srv"), \
         patch("cli._server_request",
               return_value={"item_id": "444", "created": True, "advisories": ["not-a-dict"]}), \
         patch("cli._record_add"):
        result = runner.invoke(cli, ["add", "444", "20.00"])

    assert result.exit_code == 0, result.output
    assert result.exception is None


def test_add_advisory_does_not_change_exit_code():
    """Advisories are advisory: never fatal, never a nonzero exit."""
    runner = CliRunner()
    with patch("cli._server_url", return_value="http://srv"), \
         patch("cli._server_request",
               return_value={"item_id": "444", "created": True, "advisories": [_ADVISORY]}), \
         patch("cli._record_add"):
        result = runner.invoke(cli, ["add", "444", "20.00"])

    assert result.exit_code == 0, result.output


def test_add_source_cli_reaches_the_add_payload():
    """BUI-621 (U7): `add` tags its own POST /api/bids payload source="cli"
    (add-batch's add_one_row tags "batch" instead — see test_add_batch.py)."""
    runner = CliRunner()
    captured = {}

    def fake_request(method, path, **kwargs):
        if path == "/api/bids":
            captured["json"] = kwargs.get("json")
            return {"item_id": "444", "created": True}
        return {}

    with patch("cli._server_url", return_value="http://srv"), \
         patch("cli._server_request", side_effect=fake_request), \
         patch("cli._record_add"):
        result = runner.invoke(cli, ["add", "444", "20.00"])

    assert result.exit_code == 0, result.output
    assert captured["json"]["source"] == "cli"


def test_add_advisories_render_even_when_link_fmv_also_ran():
    """The advisory line prints after whichever success message the
    link-fmv branch chose — it must not be skipped just because a link
    attempt also happened."""
    runner = CliRunner()

    def fake_request(method, path, **kwargs):
        if path == "/api/bids":
            return {"item_id": "444", "created": True, "advisories": [_ADVISORY]}
        return {}  # link-fmv ok

    with patch("cli._server_url", return_value="http://srv"), \
         patch("cli._server_request", side_effect=fake_request), \
         patch("cli._record_add"):
        result = runner.invoke(
            cli, ["add", "444", "10.00", "--comic-id", "187", "--grade", "5.0"]
        )

    assert result.exit_code == 0, result.output
    assert "✅ Added + linked" in result.output
    assert "exposure_ceiling" in result.output


# ---------------------------------------------------------------------------
# `gixen add` — direct-Gixen mode (no COMICS_SERVER_URL): no advisory path
# ---------------------------------------------------------------------------


def test_add_direct_mode_has_no_advisory_rendering_path_error():
    """No server, no response to render advisories from — the rendering
    code must simply not run, never error."""
    runner = CliRunner()
    with patch("cli._make_client") as mock_make, \
         patch("cli._record_add"), \
         patch("cli._server_url", return_value=None):
        mock_client = MagicMock()
        mock_client.list_snipes.return_value = []
        mock_make.return_value = mock_client

        result = runner.invoke(cli, ["add", "444555666", "15.00"])

    assert result.exit_code == 0, result.output
    assert result.exception is None
    assert "advisor" not in result.output.lower()


# ---------------------------------------------------------------------------
# `gixen edit` — server mode
# ---------------------------------------------------------------------------


def test_edit_prints_advisory_after_success_line():
    runner = CliRunner()
    with patch("cli._server_url", return_value="http://srv"), \
         patch("cli._server_request",
               return_value={"item_id": "200000001", "advisories": [_ADVISORY]}):
        result = runner.invoke(cli, ["edit", "200000001", "75.0"])

    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    updated_idx = next(i for i, line in enumerate(lines) if "Updated snipe" in line)
    advisory_idx = next(i for i, line in enumerate(lines) if "exposure_ceiling" in line)
    assert advisory_idx > updated_idx, "advisory must print AFTER the success line"


def test_edit_clean_response_prints_no_advisory_noise():
    runner = CliRunner()
    with patch("cli._server_url", return_value="http://srv"), \
         patch("cli._server_request",
               return_value={"item_id": "200000001", "advisories": []}):
        result = runner.invoke(cli, ["edit", "200000001", "75.0"])

    assert result.exit_code == 0, result.output
    assert "advisor" not in result.output.lower()


def test_edit_old_server_response_without_advisories_key_does_not_crash():
    runner = CliRunner()
    with patch("cli._server_url", return_value="http://srv"), \
         patch("cli._server_request", return_value={"item_id": "200000001"}):
        result = runner.invoke(cli, ["edit", "200000001", "75.0"])

    assert result.exit_code == 0, result.output
    assert result.exception is None
    assert "Updated snipe" in result.output


def test_edit_source_cli_reaches_the_patch_payload():
    runner = CliRunner()
    captured = {}

    def fake_request(method, path, **kwargs):
        captured["json"] = kwargs.get("json")
        return {"item_id": "200000001"}

    with patch("cli._server_url", return_value="http://srv"), \
         patch("cli._server_request", side_effect=fake_request):
        result = runner.invoke(cli, ["edit", "200000001", "75.0"])

    assert result.exit_code == 0, result.output
    assert captured["json"]["source"] == "cli"


def test_edit_direct_mode_has_no_advisory_rendering_path_error():
    runner = CliRunner()
    mock_client = MagicMock()
    mock_client.list_snipes.return_value = [{
        "item_id": "200000001", "bid_offset": "9", "snipe_group": "3", "dbidid": "555",
    }]
    with patch("cli._server_url", return_value=None), \
         patch("cli._make_client", return_value=mock_client):
        result = runner.invoke(cli, ["edit", "200000001", "75.0"])

    assert result.exit_code == 0, result.output
    assert result.exception is None
    assert "advisor" not in result.output.lower()
