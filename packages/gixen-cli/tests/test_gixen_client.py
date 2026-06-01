"""Unit tests for gixen_client.py and cli.py — all network calls are mocked."""

import json
import pytest
from decimal import Decimal
from unittest.mock import patch, MagicMock, PropertyMock

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from click.testing import CliRunner

from gixen_client import (
    GixenClient,
    GixenError,
    GixenLoginError,
    GixenSessionExpiredError,
    GixenItemError,
    GixenSnipeNotFoundError,
    GixenParseError,
    GixenAddNotConfirmedError,
    find_sibling_cleanup_targets,
)


# ---------------------------------------------------------------------------
# Fixtures & helpers
# ---------------------------------------------------------------------------

LOGIN_REDIRECT_HTML = (
    '<html><head>'
    '<META http-equiv="REFRESH" content="1; '
    'url=https://www.gixen.com/main/home_2.php?sessionid=99887766">'
    '</head><body>Please wait...</body></html>'
)

LOGIN_FAILED_HTML = (
    '<html><body>'
    '<form name="login"><input name="username"><input name="password">'
    '<input name="signin" type="submit">'
    '</form></body></html>'
)


def _make_snipe_row(item_id, dbidid, max_bid="10", title="Test Item",
                    current_bid="5.00 USD", status="SCHEDULED",
                    seller="testseller", time_to_end="1 h, 2 m, 3 s",
                    offset="6", group="0"):
    """Build HTML for one snipe row in the desktop table."""
    return (
        f'<tr class=d1>\n'
        f'<td rowspan="2"><input type="checkbox" name="dbidid_{dbidid}" value="{dbidid}" /></td>'
        f'<td><img src="thumb.jpg" height="50px" width="50px"><br />'
        f'<a target="_blank" href="http://www.ebay.com/itm/{item_id}">{item_id}</a></td>\n'
        f'<td colspan="4">{title} <i>(by <a target=_blank href="http://www.ebay.com/usr/{seller}/">{seller}</a>)</i>'
        f'<table><tr><td></td></tr></table></td>\n'
        f'<td class="fix">'
        f'<input name="edititemid_{item_id}" type="hidden" id="edititemid" value="{item_id}" size="14" />\n'
        f'<input name="editbidoffset_{item_id}" type="hidden" id="editbidoffset" value="{offset}" size="14" />\n'
        f'<input name="editbidoffsetmirror_{item_id}" type="hidden" id="editbidoffsetmirror" value="{offset}" size="14" />\n'
        f'<input name="editsnipegroup_{item_id}" type="hidden" id="editsnipegroup" value="{group}" size="14" />\n'
        f'<input name="editmaxbid_{item_id}" type="hidden" id="editmaxbid" value="{max_bid}" size="14" />\n'
        f'<input name="editcomment_{item_id}" type="hidden" id="editcomment" value="" size="128" />\n'
        f'<input name="username" type="hidden" id="username" value="testuser" size="14" />'
        f'<input name="edit_{item_id}" type="submit" value="Edit" onclick="document.pressed=this.value"/>\n'
        f'</td>\n'
        f'<td>\n'
        f'<input name="delete_{dbidid}" type="submit" value="Delete" onclick="document.pressed=this.value"/>\n'
        f'</td>\n'
        f'</tr>\n'
        f'<tr class=d1>\n'
        f'<td></td>\n'
        f'<td>{time_to_end}</td>\n'
        f'<td>{max_bid}</td>\n'
        f'<td>{current_bid}</td>\n'
        f'<td>{status}</td>\n'
        f'</tr>\n'
    )


def _wrap_table(*rows):
    """Wrap snipe rows in the desktop table/form structure."""
    return (
        '<html><body>'
        '<form name="bids" method="post" onsubmit="return onsubmitform();">'
        '<input name="username" type="hidden" value="testuser" />'
        '<table class="main-desktop test">'
        '<tr class=dhead><td></td><td>eBay Item Number</td></tr>\n'
        + "\n".join(rows)
        + '</table></form>'
        '<form name="addsnipe" method="post" action="home_2.php?sessionid=99887766">'
        '<input name="newitemid" type="text" id="newitemid" />'
        '</form>'
        '</body></html>'
    )


@pytest.fixture(autouse=True)
def _reset_throttle_state():
    """The throttle's last-post timestamp is keyed by username on the class
    (so multiple GixenClient instances sharing an account serialize against
    Gixen's per-account rate limit). Tests share `testuser`, so this state
    leaks between tests if not reset."""
    GixenClient._last_post_at_by_user.clear()
    yield
    GixenClient._last_post_at_by_user.clear()


def _client():
    return GixenClient(username="testuser", password="testpass")


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------

class TestLogin:
    @patch("gixen_client.requests.Session")
    def test_login_success(self, MockSession):
        session = MockSession.return_value
        resp = MagicMock()
        resp.text = LOGIN_REDIRECT_HTML
        session.post.return_value = resp

        client = GixenClient(username="user", password="pass")
        client.session = session
        sid = client.login()

        assert sid == "99887766"
        assert client.session_id == "99887766"
        session.post.assert_called_once()
        call_kwargs = session.post.call_args
        assert call_kwargs[1]["data"]["username"] == "user"
        assert call_kwargs[1]["data"]["password"] == "pass"
        assert call_kwargs[1]["data"]["signin"] == "signin"

    @patch("gixen_client.requests.Session")
    def test_login_bad_credentials(self, MockSession):
        session = MockSession.return_value
        resp = MagicMock()
        resp.text = LOGIN_FAILED_HTML
        session.post.return_value = resp

        client = GixenClient(username="bad", password="wrong")
        client.session = session

        with pytest.raises(GixenLoginError, match="Login failed"):
            client.login()


# ---------------------------------------------------------------------------
# List snipes
# ---------------------------------------------------------------------------

class TestListSnipes:
    def test_parse_single_snipe(self):
        client = _client()
        client.session_id = "99887766"

        html = _wrap_table(
            _make_snipe_row("111222333", "5001", max_bid="25", title="Cool Widget",
                            seller="widgetseller", status="SCHEDULED")
        )

        with patch.object(client, "_get_home_page", return_value=html):
            snipes = client.list_snipes()

        assert len(snipes) == 1
        assert snipes[0]["item_id"] == "111222333"
        assert snipes[0]["max_bid"] == "25"
        assert snipes[0]["dbidid"] == "5001"
        assert snipes[0]["bid_offset"] == "6"
        assert snipes[0]["snipe_group"] == "0"
        assert snipes[0]["seller"] == "widgetseller"

    def test_parse_multiple_snipes(self):
        client = _client()
        client.session_id = "99887766"

        html = _wrap_table(
            _make_snipe_row("111", "5001", max_bid="10"),
            _make_snipe_row("222", "5002", max_bid="20"),
            _make_snipe_row("333", "5003", max_bid="30"),
        )

        with patch.object(client, "_get_home_page", return_value=html):
            snipes = client.list_snipes()

        assert len(snipes) == 3
        assert [s["item_id"] for s in snipes] == ["111", "222", "333"]
        assert [s["max_bid"] for s in snipes] == ["10", "20", "30"]

    def test_parse_empty_list(self):
        client = _client()
        client.session_id = "99887766"

        html = _wrap_table()  # No rows

        with patch.object(client, "_get_home_page", return_value=html):
            snipes = client.list_snipes()

        assert snipes == []

    def test_parse_error_no_table(self):
        client = _client()
        client.session_id = "99887766"

        html = "<html><body>Maintenance in progress</body></html>"

        with patch.object(client, "_get_home_page", return_value=html):
            with pytest.raises(GixenParseError, match="Could not find snipe table"):
                client.list_snipes()

    def test_parse_error_non_numeric_bid(self):
        client = _client()
        client.session_id = "99887766"

        html = _wrap_table(
            _make_snipe_row("111", "5001", max_bid="abc")
        )

        with patch.object(client, "_get_home_page", return_value=html):
            with pytest.raises(GixenParseError, match="Non-numeric max bid"):
                client.list_snipes()


# ---------------------------------------------------------------------------
# Add snipe
# ---------------------------------------------------------------------------

class TestAddSnipe:
    def test_add_success(self):
        client = _client()
        client.session_id = "99887766"
        client._min_post_gap = 0  # disable throttle for test speed

        with patch.object(client, "_post_home", return_value="<html>OK</html>") as mock_post, \
             patch.object(client, "list_snipes", return_value=[{"item_id": "444555666", "dbidid": "5001"}]):
            result = client.add_snipe("444555666", Decimal("15.00"))

        assert result is True
        data = mock_post.call_args[0][0]
        assert data["newitemid"] == "444555666"
        assert data["newmaxbid"] == "15.00"
        assert data["newbidoffset"] == "6"
        assert data["newsnipegroup"] == "0"
        assert data["username"] == "testuser"

    def test_add_with_options(self):
        client = _client()
        client.session_id = "99887766"
        client._min_post_gap = 0

        with patch.object(client, "_post_home", return_value="<html>OK</html>") as mock_post, \
             patch.object(client, "list_snipes", return_value=[{"item_id": "444", "dbidid": "5001"}]):
            client.add_snipe("444", Decimal("5"), bid_offset=3, snipe_group=2)

        data = mock_post.call_args[0][0]
        assert data["newbidoffset"] == "3"
        assert data["newsnipegroup"] == "2"

    def test_add_item_not_found(self):
        client = _client()
        client.session_id = "99887766"
        client._min_post_gap = 0

        with patch.object(client, "_post_home", side_effect=GixenItemError(299, "The specified item Id was not found.")):
            with pytest.raises(GixenItemError) as exc_info:
                client.add_snipe("999", Decimal("1.00"))
            assert exc_info.value.code == 299

    def test_add_duplicate(self):
        client = _client()
        client.session_id = "99887766"
        client._min_post_gap = 0

        with patch.object(client, "_post_home", side_effect=GixenItemError(202, "ITEM ALREADY PRESENT")):
            with pytest.raises(GixenItemError) as exc_info:
                client.add_snipe("111", Decimal("5"))
            assert exc_info.value.code == 202

    def test_add_silent_failure_retries_then_succeeds(self):
        """Gixen accepts the POST but drops the snipe. First verify fails,
        retry POST + verify succeeds, no exception raised."""
        client = _client()
        client.session_id = "99887766"
        client._min_post_gap = 0
        client._add_retry_backoff = 0  # skip sleep in test

        # list_snipes: first verify (empty -> absent), second verify after retry (present)
        list_calls = [[], [{"item_id": "777", "dbidid": "5099"}]]

        with patch.object(client, "_post_home", return_value="<html>OK</html>") as mock_post, \
             patch.object(client, "list_snipes", side_effect=list_calls):
            result = client.add_snipe("777", Decimal("10.00"))

        assert result is True
        # Two POSTs: original + retry
        assert mock_post.call_count == 2

    def test_add_silent_failure_persists_raises(self):
        """Snipe absent after both initial and retry verify -> raises GixenAddNotConfirmedError."""
        client = _client()
        client.session_id = "99887766"
        client._min_post_gap = 0
        client._add_retry_backoff = 0

        with patch.object(client, "_post_home", return_value="<html>OK</html>") as mock_post, \
             patch.object(client, "list_snipes", return_value=[]):
            with pytest.raises(GixenAddNotConfirmedError) as exc_info:
                client.add_snipe("888", Decimal("12.00"))

        assert "888" in str(exc_info.value)
        # Initial POST + one retry POST
        assert mock_post.call_count == 2

    def test_add_not_confirmed_error_is_gixen_error(self):
        assert issubclass(GixenAddNotConfirmedError, GixenError)

    def test_add_handles_202_duplicate_on_retry_as_success(self):
        """Race: first POST landed but list_snipes verify missed it. Retry
        POST raises GixenItemError(202) (already present). We re-check the
        list and find the item — return True instead of bubbling the 202."""
        client = _client()
        client.session_id = "99887766"
        client._min_post_gap = 0
        client._add_retry_backoff = 0

        # POST 1 succeeds, POST 2 raises 202 (item already present).
        post_calls = ["<html>OK</html>", GixenItemError(202, "ITEM ALREADY PRESENT")]
        # list_snipes: first verify empty (missed it), retry list shows item.
        list_calls = [[], [{"item_id": "777", "dbidid": "5099"}]]

        def post_side_effect(*args, **kwargs):
            result = post_calls.pop(0)
            if isinstance(result, Exception):
                raise result
            return result

        with patch.object(client, "_post_home", side_effect=post_side_effect), \
             patch.object(client, "list_snipes", side_effect=list_calls):
            result = client.add_snipe("777", Decimal("10.00"))

        assert result is True

    def test_add_raises_add_not_confirmed_when_list_snipes_parse_fails(self):
        """If list_snipes raises GixenParseError between POST and verify,
        treat as unconfirmed and raise GixenAddNotConfirmedError immediately
        rather than double-POSTing."""
        client = _client()
        client.session_id = "99887766"
        client._min_post_gap = 0
        client._add_retry_backoff = 0

        with patch.object(client, "_post_home", return_value="<html>OK</html>") as mock_post, \
             patch.object(client, "list_snipes", side_effect=GixenParseError("HTML drift")):
            with pytest.raises(GixenAddNotConfirmedError):
                client.add_snipe("999", Decimal("10.00"))

        # Critical: only one POST. We must NOT retry when verify failed —
        # that's the double-POST risk the safety guard exists to prevent.
        assert mock_post.call_count == 1

    def test_add_raises_add_not_confirmed_when_list_snipes_http_fails(self):
        """Network error on verify: treat as unconfirmed, no double-POST."""
        import requests as _req
        client = _client()
        client.session_id = "99887766"
        client._min_post_gap = 0
        client._add_retry_backoff = 0

        with patch.object(client, "_post_home", return_value="<html>OK</html>") as mock_post, \
             patch.object(client, "list_snipes", side_effect=_req.HTTPError("502")):
            with pytest.raises(GixenAddNotConfirmedError):
                client.add_snipe("999", Decimal("10.00"))
        assert mock_post.call_count == 1

    def test_post_home_throttles_consecutive_posts(self):
        """Two _post_home calls in quick succession sleep to enforce minimum gap."""
        client = _client()
        client.session_id = "99887766"
        client._min_post_gap = 1.5

        sleep_calls: list = []

        def fake_sleep(seconds):
            sleep_calls.append(seconds)

        # monotonic returns: first call captures last_post_at after POST 1,
        # second call computes elapsed at start of POST 2, then captures last_post_at.
        monotonic_values = iter([0.0, 0.1, 0.1])

        ok_resp = MagicMock()
        ok_resp.status_code = 200
        ok_resp.text = "<html>OK</html>"
        ok_resp.raise_for_status = MagicMock()
        client.session.post = MagicMock(return_value=ok_resp)

        with patch("gixen_client.time.sleep", side_effect=fake_sleep), \
             patch("gixen_client.time.monotonic", side_effect=lambda: next(monotonic_values)):
            client._post_home({"a": "1"})
            client._post_home({"b": "2"})

        # Elapsed = 0.1 - 0.0 = 0.1, remaining = 1.5 - 0.1 = 1.4
        assert sleep_calls, "Expected at least one throttle sleep"
        assert any(s >= 1.0 for s in sleep_calls), (
            f"Expected a throttle sleep >= 1.0s but got {sleep_calls}"
        )

    def test_post_home_throttle_shared_across_instances_for_same_user(self):
        """Two GixenClient instances sharing a username (e.g. _api_client +
        _sync_client) must serialize against Gixen's account rate limit, not
        each maintain its own throttle clock."""
        c1 = GixenClient(username="testuser", password="x")
        c2 = GixenClient(username="testuser", password="x")
        c1.session_id = "99887766"
        c2.session_id = "99887766"
        c1._min_post_gap = 1.5
        c2._min_post_gap = 1.5

        sleeps: list = []

        def fake_sleep(seconds):
            sleeps.append(seconds)

        # c1 posts at t=0; c2 posts at t=0.1 — should sleep for the gap.
        monotonic_values = iter([0.0, 0.1, 0.1])

        ok_resp = MagicMock()
        ok_resp.status_code = 200
        ok_resp.text = "<html>OK</html>"
        ok_resp.raise_for_status = MagicMock()
        c1.session.post = MagicMock(return_value=ok_resp)
        c2.session.post = MagicMock(return_value=ok_resp)

        with patch("gixen_client.time.sleep", side_effect=fake_sleep), \
             patch("gixen_client.time.monotonic", side_effect=lambda: next(monotonic_values)):
            c1._post_home({"a": "1"})
            c2._post_home({"b": "2"})

        assert sleeps, (
            "expected c2's _post_home to throttle off c1's post; throttle "
            "state must be account-level not instance-level"
        )

    def test_post_home_throttle_cleared_after_login(self):
        """Login already takes seconds (HTTP round trip); the recursion path
        in _post_home (500 → relogin → retry) should not double-sleep on top
        of that. Verify the throttle clock is cleared by login()."""
        client = _client()
        client._min_post_gap = 1.5

        # Seed a recent post timestamp so the next throttle check would fire.
        with patch("gixen_client.time.monotonic", return_value=100.0):
            client._last_post_at = 100.0

        # Login resp returns valid session HTML.
        login_resp = MagicMock()
        login_resp.text = LOGIN_REDIRECT_HTML
        client.session.post = MagicMock(return_value=login_resp)

        client.login()
        assert client._last_post_at is None, (
            "login() must clear _last_post_at so a subsequent _post_home "
            "doesn't stack throttle on top of login latency"
        )

    def test_post_home_no_throttle_when_gap_exceeded(self):
        """If enough time has passed since last POST, no throttle sleep occurs."""
        client = _client()
        client.session_id = "99887766"
        client._min_post_gap = 1.5

        sleep_calls: list = []

        def fake_sleep(seconds):
            sleep_calls.append(seconds)

        monotonic_values = iter([0.0, 10.0, 10.0])

        ok_resp = MagicMock()
        ok_resp.status_code = 200
        ok_resp.text = "<html>OK</html>"
        ok_resp.raise_for_status = MagicMock()
        client.session.post = MagicMock(return_value=ok_resp)

        with patch("gixen_client.time.sleep", side_effect=fake_sleep), \
             patch("gixen_client.time.monotonic", side_effect=lambda: next(monotonic_values)):
            client._post_home({"a": "1"})
            client._post_home({"b": "2"})

        assert sleep_calls == [], f"Expected no sleeps but got {sleep_calls}"


# ---------------------------------------------------------------------------
# Modify snipe
# ---------------------------------------------------------------------------

class TestModifySnipe:
    def test_modify_success(self):
        client = _client()
        client.session_id = "99887766"

        snipes = [{"item_id": "111", "dbidid": "5001", "max_bid": "10"}]

        with patch.object(client, "list_snipes", return_value=snipes):
            with patch.object(client, "_post_home", return_value="<html>OK</html>") as mock_post:
                result = client.modify_snipe("111", Decimal("20.00"))

        assert result is True
        data = mock_post.call_args[0][0]
        assert data["newitemid"] == "111"
        assert data["newmaxbid"] == "20.00"
        assert data["dbidid"] == "5001"
        assert data["ismodified"] == "1"

    def test_modify_item_not_found(self):
        client = _client()
        client.session_id = "99887766"

        with patch.object(client, "list_snipes", return_value=[]):
            with pytest.raises(GixenSnipeNotFoundError, match="111"):
                client.modify_snipe("111", Decimal("20"))


# ---------------------------------------------------------------------------
# Remove snipe
# ---------------------------------------------------------------------------

class TestRemoveSnipe:
    def test_remove_success(self):
        client = _client()
        client.session_id = "99887766"

        snipes = [{"item_id": "111", "dbidid": "5001"}]

        # First call returns the snipe list; second call (post-delete verification) returns empty.
        with patch.object(client, "list_snipes", side_effect=[snipes, []]):
            with patch.object(client, "_post_home", return_value="<html>OK</html>") as mock_post:
                result = client.remove_snipe("111")

        assert result is True
        data = mock_post.call_args[0][0]
        assert data["delete_5001"] == "Delete"

    def test_remove_item_not_found(self):
        client = _client()
        client.session_id = "99887766"

        with patch.object(client, "list_snipes", return_value=[]):
            with pytest.raises(GixenSnipeNotFoundError, match="999"):
                client.remove_snipe("999")


# ---------------------------------------------------------------------------
# Purge
# ---------------------------------------------------------------------------

class TestPurge:
    def test_purge_success(self):
        client = _client()
        client.session_id = "99887766"

        with patch.object(client, "_post_home", return_value="<html>OK</html>") as mock_post:
            result = client.purge_completed()

        assert result is True
        data = mock_post.call_args[0][0]
        assert data["purgecompleted"] == "1"
        assert data["gixenlinkcontinue"] == "1"


# ---------------------------------------------------------------------------
# Session expiration
# ---------------------------------------------------------------------------

class TestSessionExpiration:
    def test_auto_relogin_on_expired_session(self):
        client = _client()
        client.session_id = "old_session"

        expired_resp = MagicMock()
        expired_resp.text = LOGIN_FAILED_HTML  # looks like login form
        expired_resp.raise_for_status = MagicMock()

        fresh_html = _wrap_table(_make_snipe_row("111", "5001"))
        fresh_resp = MagicMock()
        fresh_resp.text = fresh_html
        fresh_resp.raise_for_status = MagicMock()

        # First GET returns expired session, second returns fresh page
        client.session.get = MagicMock(side_effect=[expired_resp, fresh_resp])

        login_resp = MagicMock()
        login_resp.text = LOGIN_REDIRECT_HTML
        client.session.post = MagicMock(return_value=login_resp)

        snipes = client.list_snipes()
        assert len(snipes) == 1
        assert client.session_id == "99887766"

    def test_auto_relogin_on_invalidated_session_id(self):
        # Regression: when Gixen invalidates the session_id server-side, the
        # response is the homepage with a "Could not log you in. (33)"
        # wrong-alert div — no <form name="signin">. Without explicit
        # detection, the parser raises GixenParseError and re-login never
        # fires, leaving the persistent client wedged. Observed in production:
        # 145+ consecutive _sync_loop failures and PATCH /api/bids returning
        # 503.
        client = _client()
        client.session_id = "dead_session"

        invalidated_html = (
            '<html><body>'
            '<div class="wrong-alert">'
            'Password is not a match. Could not log you in. (33)'
            '</div>'
            '</body></html>'
        )
        invalidated_resp = MagicMock()
        invalidated_resp.text = invalidated_html
        invalidated_resp.raise_for_status = MagicMock()

        fresh_resp = MagicMock()
        fresh_resp.text = _wrap_table(_make_snipe_row("222", "5002"))
        fresh_resp.raise_for_status = MagicMock()

        client.session.get = MagicMock(side_effect=[invalidated_resp, fresh_resp])

        login_resp = MagicMock()
        login_resp.text = LOGIN_REDIRECT_HTML
        client.session.post = MagicMock(return_value=login_resp)

        snipes = client.list_snipes()
        assert len(snipes) == 1
        assert client.session_id == "99887766"

    def test_relogin_fails_raises_error(self):
        client = _client()
        client.session_id = "old_session"

        expired_resp = MagicMock()
        expired_resp.text = LOGIN_FAILED_HTML
        expired_resp.raise_for_status = MagicMock()

        # Both GETs return expired
        client.session.get = MagicMock(return_value=expired_resp)

        # Login also fails
        login_fail_resp = MagicMock()
        login_fail_resp.text = LOGIN_FAILED_HTML
        client.session.post = MagicMock(return_value=login_fail_resp)

        with pytest.raises((GixenLoginError, GixenSessionExpiredError)):
            client.list_snipes()


# ---------------------------------------------------------------------------
# Error detection from HTML
# ---------------------------------------------------------------------------

class TestErrorDetection:
    def test_item_error_in_html(self):
        html = (
            '<html><body>'
            '<b><font color="red">Error (299): \'The specified item Id was not found.\'</font></b>'
            '</body></html>'
        )
        with pytest.raises(GixenItemError) as exc_info:
            GixenClient._check_html_error(html)
        assert exc_info.value.code == 299

    def test_duplicate_error(self):
        html = '<font color="red">Error (202): \'Item already present\'</font>'
        with pytest.raises(GixenItemError) as exc_info:
            GixenClient._check_html_error(html)
        assert exc_info.value.code == 202

    def test_suspended_account(self):
        html = '<font color="red">Error (115): \'Account suspended\'</font>'
        with pytest.raises(GixenLoginError, match="suspended"):
            GixenClient._check_html_error(html)

    def test_no_error(self):
        html = "<html><body>Normal page</body></html>"
        GixenClient._check_html_error(html)  # Should not raise


# ---------------------------------------------------------------------------
# Exception hierarchy
# ---------------------------------------------------------------------------

class TestExceptionHierarchy:
    def test_all_inherit_from_gixen_error(self):
        assert issubclass(GixenLoginError, GixenError)
        assert issubclass(GixenSessionExpiredError, GixenError)
        assert issubclass(GixenItemError, GixenError)
        assert issubclass(GixenSnipeNotFoundError, GixenError)
        assert issubclass(GixenParseError, GixenError)
        assert issubclass(GixenAddNotConfirmedError, GixenError)

    def test_item_error_has_code(self):
        err = GixenItemError(299, "Not found")
        assert err.code == 299
        assert err.message == "Not found"
        assert "299" in str(err)


# ---------------------------------------------------------------------------
# CLI: add duplicate detection
# ---------------------------------------------------------------------------

class TestCliAddDuplicate:
    def test_add_warns_on_existing_snipe(self):
        from cli import cli

        runner = CliRunner()
        existing_snipes = [{"item_id": "111222333", "max_bid": "465", "dbidid": "5001"}]

        with patch("cli._make_client") as mock_make, \
             patch("cli._server_url", return_value=None):
            mock_client = MagicMock()
            mock_client.list_snipes.return_value = existing_snipes
            mock_make.return_value = mock_client

            result = runner.invoke(cli, ["add", "111222333", "440"])

        assert result.exit_code == 1
        assert "already exists" in result.output
        assert "465" in result.output
        assert "edit" in result.output
        mock_client.add_snipe.assert_not_called()

    def test_add_succeeds_when_no_duplicate(self):
        from cli import cli

        runner = CliRunner()

        with patch("cli._make_client") as mock_make, \
             patch("cli._record_add") as mock_record, \
             patch("cli._server_url", return_value=None):
            mock_client = MagicMock()
            mock_client.list_snipes.return_value = []
            mock_make.return_value = mock_client

            result = runner.invoke(cli, ["add", "444555666", "15.00"])

        assert result.exit_code == 0
        assert "Added snipe" in result.output
        mock_client.add_snipe.assert_called_once()
        mock_record.assert_called_once_with("444555666")


# ---------------------------------------------------------------------------
# CLI: server-mode add created-vs-updated signal (BUI-67)
# ---------------------------------------------------------------------------

class TestCliServerAddCreatedFlag:
    def test_records_add_and_says_added_on_create(self):
        from cli import cli

        runner = CliRunner()
        with patch("cli._server_url", return_value="http://srv"), \
             patch("cli._server_request",
                   return_value={"item_id": "444", "max_bid": 20.0, "created": True}), \
             patch("cli._record_add") as mock_record:
            result = runner.invoke(cli, ["add", "444", "20.00"])

        assert result.exit_code == 0, result.output
        assert "Added snipe" in result.output
        mock_record.assert_called_once_with("444")

    def test_skips_record_and_says_updated_on_in_place_update(self):
        from cli import cli

        runner = CliRunner()
        with patch("cli._server_url", return_value="http://srv"), \
             patch("cli._server_request",
                   return_value={"item_id": "444", "max_bid": 20.0, "created": False}), \
             patch("cli._record_add") as mock_record:
            result = runner.invoke(cli, ["add", "444", "20.00"])

        assert result.exit_code == 0, result.output
        assert "updated existing snipe" in result.output.lower()
        # A re-add (in-place update) must NOT reset the add-history timestamp.
        mock_record.assert_not_called()


# ---------------------------------------------------------------------------
# CLI: add --comic-id / --catalog-id link-fmv routing (server mode)
# ---------------------------------------------------------------------------

class TestCliAddLinkFmv:
    def test_comic_id_sends_comic_id_in_link_body(self):
        from cli import cli

        runner = CliRunner()
        calls = []

        def fake_request(method, path, **kwargs):
            calls.append((method, path, kwargs.get("json")))
            return {}

        with patch("cli._server_url", return_value="http://srv"), \
             patch("cli._server_request", side_effect=fake_request), \
             patch("cli._record_add"):
            result = runner.invoke(
                cli, ["add", "444", "10.00", "--comic-id", "187", "--grade", "5.0"]
            )

        assert result.exit_code == 0, result.output
        link_calls = [c for c in calls if c[1].endswith("/link-fmv")]
        assert len(link_calls) == 1
        assert link_calls[0] == ("post", "/api/bids/444/link-fmv",
                                 {"comic_id": 187, "grade": 5.0})
        assert "✅ Added + linked" in result.output

    def test_catalog_id_still_sends_locg_id(self):
        from cli import cli

        runner = CliRunner()
        calls = []

        def fake_request(method, path, **kwargs):
            calls.append((method, path, kwargs.get("json")))
            return {}

        with patch("cli._server_url", return_value="http://srv"), \
             patch("cli._server_request", side_effect=fake_request), \
             patch("cli._record_add"):
            result = runner.invoke(
                cli, ["add", "444", "10.00", "--catalog-id", "9001", "--grade", "8.5"]
            )

        assert result.exit_code == 0, result.output
        link_calls = [c for c in calls if c[1].endswith("/link-fmv")]
        assert len(link_calls) == 1
        assert link_calls[0] == ("post", "/api/bids/444/link-fmv",
                                 {"locg_id": 9001, "grade": 8.5})
        assert "✅ Added + linked" in result.output

    def test_comic_id_takes_precedence_over_catalog_id(self):
        from cli import cli

        runner = CliRunner()
        calls = []

        def fake_request(method, path, **kwargs):
            calls.append((method, path, kwargs.get("json")))
            return {}

        with patch("cli._server_url", return_value="http://srv"), \
             patch("cli._server_request", side_effect=fake_request), \
             patch("cli._record_add"):
            result = runner.invoke(cli, [
                "add", "444", "10.00",
                "--comic-id", "187",
                "--catalog-id", "9001",
                "--grade", "5.0",
            ])

        assert result.exit_code == 0, result.output
        link_calls = [c for c in calls if c[1].endswith("/link-fmv")]
        assert len(link_calls) == 1
        assert link_calls[0] == ("post", "/api/bids/444/link-fmv",
                                 {"comic_id": 187, "grade": 5.0})
        assert "ignoring --catalog-id" in result.output
        assert "9001" in result.output

    def test_no_link_call_when_grade_missing(self):
        from cli import cli

        runner = CliRunner()
        calls = []

        def fake_request(method, path, **kwargs):
            calls.append((method, path, kwargs.get("json")))
            return {}

        with patch("cli._server_url", return_value="http://srv"), \
             patch("cli._server_request", side_effect=fake_request), \
             patch("cli._record_add"):
            result = runner.invoke(
                cli, ["add", "444", "10.00", "--comic-id", "187"]
            )

        assert result.exit_code == 0, result.output
        assert not any(c[1].endswith("/link-fmv") for c in calls)
        assert "Added snipe" in result.output

    def test_help_disambiguates_flags(self):
        from cli import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["add", "--help"])

        assert result.exit_code == 0
        assert "--comic-id" in result.output
        assert "--catalog-id" in result.output
        assert "locg_id" in result.output
        assert "comics.id" in result.output


# ---------------------------------------------------------------------------
# CLI: list --json
# ---------------------------------------------------------------------------

class TestCliListJson:
    def test_list_json_output(self):
        from cli import cli

        runner = CliRunner()
        snipes = [
            {"item_id": "111", "max_bid": "10", "title": "Widget",
             "current_bid": "5.00 USD", "time_to_end": "1 h", "status": "SCHEDULED"},
        ]

        with patch("cli._make_client") as mock_make, \
             patch("cli._server_request", return_value=snipes):
            mock_client = MagicMock()
            mock_client.list_snipes.return_value = snipes
            mock_make.return_value = mock_client

            result = runner.invoke(cli, ["list", "--json"])

        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert len(parsed) == 1
        assert parsed[0]["item_id"] == "111"

    def test_list_json_empty(self):
        from cli import cli

        runner = CliRunner()

        with patch("cli._make_client") as mock_make, \
             patch("cli._server_request", return_value=[]):
            mock_client = MagicMock()
            mock_client.list_snipes.return_value = []
            mock_make.return_value = mock_client

            result = runner.invoke(cli, ["list", "--json"])

        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert parsed == []


# ---------------------------------------------------------------------------
# CLI: list --added-since
# ---------------------------------------------------------------------------

class TestCliListAddedSince:
    def test_filters_by_add_history(self):
        from cli import cli
        from datetime import datetime, timezone

        runner = CliRunner()
        now = datetime.now(timezone.utc).timestamp()
        history = {"111": now, "222": now - 7200}  # 222 added 2 hours ago

        snipes = [
            {"item_id": "111", "max_bid": "10", "time_to_end": "1 h"},
            {"item_id": "222", "max_bid": "20", "time_to_end": "2 h"},
            {"item_id": "333", "max_bid": "30", "time_to_end": "3 h"},
        ]

        with patch("cli._make_client") as mock_make, \
             patch("cli._load_add_history", return_value=history), \
             patch("cli._server_request", return_value=snipes):
            mock_client = MagicMock()
            mock_client.list_snipes.return_value = snipes
            mock_make.return_value = mock_client

            # Only show items added since 1 hour ago
            since = datetime.fromtimestamp(now - 3600, tz=timezone.utc)
            since_str = since.strftime("%Y-%m-%dT%H:%M:%S")
            result = runner.invoke(cli, ["list", "--json", "--added-since", since_str])

        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert len(parsed) == 1
        assert parsed[0]["item_id"] == "111"

    def test_added_since_no_matches_shows_empty(self):
        from cli import cli
        from datetime import datetime, timezone

        runner = CliRunner()
        now = datetime.now(timezone.utc).timestamp()
        # All adds are old
        history = {"111": now - 7200}

        snipes = [
            {"item_id": "111", "max_bid": "10", "time_to_end": "1 h"},
        ]

        with patch("cli._make_client") as mock_make, \
             patch("cli._server_request", return_value=snipes), \
             patch("cli._load_add_history", return_value=history):
            mock_client = MagicMock()
            mock_client.list_snipes.return_value = snipes
            mock_make.return_value = mock_client

            since = datetime.fromtimestamp(now - 3600, tz=timezone.utc)
            since_str = since.strftime("%Y-%m-%dT%H:%M:%S")
            result = runner.invoke(cli, ["list", "--added-since", since_str])

        assert result.exit_code == 0
        assert "No snipes found" in result.output


# ---------------------------------------------------------------------------
# CLI: add history helpers
# ---------------------------------------------------------------------------

class TestAddHistory:
    def test_load_missing_file(self, tmp_path):
        from cli import _load_add_history
        with patch("cli.HISTORY_FILE", tmp_path / "nonexistent.json"):
            assert _load_add_history() == {}

    def test_load_corrupt_file(self, tmp_path):
        from cli import _load_add_history
        bad_file = tmp_path / "bad.json"
        bad_file.write_text("not json{{{")
        with patch("cli.HISTORY_FILE", bad_file):
            assert _load_add_history() == {}

    def test_record_and_load_roundtrip(self, tmp_path):
        from cli import _record_add, _load_add_history
        hist_file = tmp_path / "history.json"
        with patch("cli.HISTORY_FILE", hist_file):
            _record_add("111")
            _record_add("222")
            history = _load_add_history()

        assert "111" in history
        assert "222" in history
        assert isinstance(history["111"], float)
        assert history["222"] >= history["111"]


# ---------------------------------------------------------------------------
# find_sibling_cleanup_targets
# ---------------------------------------------------------------------------

def _snipe(item_id, status="SCHEDULED", group="0", title=""):
    """Build a minimal snipe dict for cleanup-target tests."""
    return {
        "item_id": item_id,
        "status": status,
        "snipe_group": group,
        "title": title,
    }


class TestFindSiblingCleanupTargets:
    def test_empty_input(self):
        assert find_sibling_cleanup_targets([]) == []

    def test_no_groups_even_with_won(self):
        # WON snipe in group "0" — group "0" is "no group" and is ignored.
        snipes = [
            _snipe("111", status="WON", group="0"),
            _snipe("222", status="SCHEDULED", group="0"),
        ]
        assert find_sibling_cleanup_targets(snipes) == []

    def test_won_with_two_scheduled_siblings(self):
        snipes = [
            _snipe("111", status="WON", group="1"),
            _snipe("222", status="SCHEDULED", group="1"),
            _snipe("333", status="SCHEDULED", group="1"),
        ]
        result = find_sibling_cleanup_targets(snipes)
        assert [s["item_id"] for s in result] == ["222", "333"]

    def test_won_with_mixed_non_won_siblings(self):
        snipes = [
            _snipe("111", status="WON", group="1"),
            _snipe("222", status="LOST", group="1"),
            _snipe("333", status="SCHEDULED", group="1"),
        ]
        result = find_sibling_cleanup_targets(snipes)
        assert [s["item_id"] for s in result] == ["222", "333"]

    def test_group_without_a_winner_is_left_alone(self):
        snipes = [
            _snipe("111", status="SCHEDULED", group="2"),
            _snipe("222", status="SCHEDULED", group="2"),
        ]
        assert find_sibling_cleanup_targets(snipes) == []

    def test_only_winning_groups_siblings_returned(self):
        snipes = [
            _snipe("111", status="WON", group="1"),
            _snipe("222", status="SCHEDULED", group="1"),
            _snipe("333", status="SCHEDULED", group="2"),  # group 2 has no win
            _snipe("444", status="SCHEDULED", group="2"),
        ]
        result = find_sibling_cleanup_targets(snipes)
        assert [s["item_id"] for s in result] == ["222"]

    def test_two_groups_each_with_a_win(self):
        snipes = [
            _snipe("111", status="WON", group="1"),
            _snipe("222", status="SCHEDULED", group="1"),
            _snipe("333", status="WON", group="2"),
            _snipe("444", status="SCHEDULED", group="2"),
        ]
        result = find_sibling_cleanup_targets(snipes)
        # Input order is preserved.
        assert [s["item_id"] for s in result] == ["222", "444"]

    def test_two_wins_in_same_group_neither_is_a_target(self):
        snipes = [
            _snipe("111", status="WON", group="1"),
            _snipe("222", status="WON", group="1"),
            _snipe("333", status="SCHEDULED", group="1"),
        ]
        result = find_sibling_cleanup_targets(snipes)
        assert [s["item_id"] for s in result] == ["333"]

    def test_input_order_is_preserved(self):
        snipes = [
            _snipe("999", status="SCHEDULED", group="1"),
            _snipe("111", status="WON", group="1"),
            _snipe("555", status="LOST", group="1"),
        ]
        result = find_sibling_cleanup_targets(snipes)
        assert [s["item_id"] for s in result] == ["999", "555"]


# ---------------------------------------------------------------------------
# CLI: purge with sibling cleanup
# ---------------------------------------------------------------------------

class TestCliPurge:
    def test_no_siblings_preserves_original_behavior(self):
        from cli import cli

        runner = CliRunner()
        snipes = [
            _snipe("111", status="WON", group="0"),
            _snipe("222", status="SCHEDULED", group="0"),
        ]

        with patch("cli._make_client") as mock_make, \
             patch("cli._server_url", return_value=None):
            mock_client = MagicMock()
            mock_client.list_snipes.return_value = snipes
            mock_make.return_value = mock_client

            result = runner.invoke(cli, ["purge"])

        assert result.exit_code == 0
        assert result.output == "Purged completed snipes\n"
        mock_client.purge_completed.assert_called_once()
        mock_client.remove_snipe.assert_not_called()

    def test_siblings_with_no_prompt_aborts(self):
        from cli import cli

        runner = CliRunner()
        snipes = [
            _snipe("111", status="WON", group="1"),
            _snipe("222", status="SCHEDULED", group="1"),
        ]

        with patch("cli._make_client") as mock_make, \
             patch("cli._server_url", return_value=None):
            mock_client = MagicMock()
            mock_client.list_snipes.return_value = snipes
            mock_make.return_value = mock_client

            # Answer "n" to the prompt
            result = runner.invoke(cli, ["purge"], input="n\n")

        assert result.exit_code == 0
        assert "Aborted" in result.output
        mock_client.purge_completed.assert_not_called()
        mock_client.remove_snipe.assert_not_called()

    def test_siblings_with_yes_skips_prompt_and_removes(self):
        from cli import cli

        runner = CliRunner()
        snipes = [
            _snipe("111", status="WON", group="1"),
            _snipe("222", status="SCHEDULED", group="1"),
            _snipe("333", status="SCHEDULED", group="1"),
        ]

        with patch("cli._make_client") as mock_make, \
             patch("cli._server_url", return_value=None):
            mock_client = MagicMock()
            mock_client.list_snipes.return_value = snipes
            mock_make.return_value = mock_client

            result = runner.invoke(cli, ["purge", "--yes"])

        assert result.exit_code == 0
        mock_client.purge_completed.assert_called_once()
        removed_ids = [
            call.args[0] for call in mock_client.remove_snipe.call_args_list
        ]
        assert removed_ids == ["222", "333"]
        assert "Purged completed snipes" in result.output
        assert "Removed 2 sibling snipe(s)" in result.output

    def test_dry_run_does_nothing(self):
        from cli import cli

        runner = CliRunner()
        snipes = [
            _snipe("111", status="WON", group="1", title="ASM #300 CGC 9.8"),
            _snipe("222", status="SCHEDULED", group="1", title="ASM #300 raw"),
        ]

        with patch("cli._make_client") as mock_make, \
             patch("cli._server_url", return_value=None):
            mock_client = MagicMock()
            mock_client.list_snipes.return_value = snipes
            mock_make.return_value = mock_client

            result = runner.invoke(cli, ["purge", "--dry-run"])

        assert result.exit_code == 0
        assert "Dry run" in result.output
        # Sibling is mentioned in the planned-removal listing
        assert "222" in result.output
        assert "ASM #300 raw" in result.output
        mock_client.purge_completed.assert_not_called()
        mock_client.remove_snipe.assert_not_called()

    def test_dry_run_with_no_siblings(self):
        from cli import cli

        runner = CliRunner()
        snipes = [_snipe("111", status="WON", group="0")]

        with patch("cli._make_client") as mock_make, \
             patch("cli._server_url", return_value=None):
            mock_client = MagicMock()
            mock_client.list_snipes.return_value = snipes
            mock_make.return_value = mock_client

            result = runner.invoke(cli, ["purge", "--dry-run"])

        assert result.exit_code == 0
        assert "Would purge" in result.output
        mock_client.purge_completed.assert_not_called()
        mock_client.remove_snipe.assert_not_called()

    def test_remove_failure_continues_and_exits_nonzero(self):
        from cli import cli

        runner = CliRunner()
        snipes = [
            _snipe("111", status="WON", group="1"),
            _snipe("222", status="SCHEDULED", group="1"),
            _snipe("333", status="SCHEDULED", group="1"),
        ]

        def remove_side_effect(item_id):
            if item_id == "222":
                raise GixenError("network blew up")
            return True

        with patch("cli._make_client") as mock_make, \
             patch("cli._server_url", return_value=None):
            mock_client = MagicMock()
            mock_client.list_snipes.return_value = snipes
            mock_client.remove_snipe.side_effect = remove_side_effect
            mock_make.return_value = mock_client

            result = runner.invoke(cli, ["purge", "--yes"])

        assert result.exit_code == 1
        # Both removals were attempted despite the first one failing.
        attempted_ids = [
            call.args[0] for call in mock_client.remove_snipe.call_args_list
        ]
        assert attempted_ids == ["222", "333"]
        assert "failed to remove 222" in result.output
        # The successful one is reported.
        assert "Removed 1 sibling snipe(s)" in result.output


# ---------------------------------------------------------------------------
# CLI: list shows snipe_group column
# ---------------------------------------------------------------------------

class TestCliListShowsGroup:
    def test_format_group_blank_for_zero_or_missing(self):
        from cli import _format_group
        assert _format_group("0") == ""
        assert _format_group("") == ""
        assert _format_group("1") == "1"
        assert _format_group("10") == "10"

    def test_active_list_shows_group_column(self):
        from cli import cli

        runner = CliRunner()
        snipes = [
            {
                "item_id": "111",
                "title": "ASM #300 CGC 9.8",
                "current_bid": "5.00 USD",
                "max_bid": "75.00",
                "snipe_group": "1",
                "time_to_end": "2 h",
                "status": "SCHEDULED",
            },
            {
                "item_id": "222",
                "title": "Random other thing",
                "current_bid": "1.00 USD",
                "max_bid": "10.00",
                "snipe_group": "0",
                "time_to_end": "5 h",
                "status": "SCHEDULED",
            },
        ]

        with patch("cli._make_client") as mock_make, \
             patch("cli._server_request", return_value=snipes):
            mock_client = MagicMock()
            mock_client.list_snipes.return_value = snipes
            mock_make.return_value = mock_client

            result = runner.invoke(cli, ["list"])

        assert result.exit_code == 0
        assert "Grp" in result.output
        # Grouped item shows its group; ungrouped item leaves the column blank.
        lines = result.output.splitlines()
        line_111 = next(l for l in lines if "111" in l and "ASM" in l)
        line_222 = next(l for l in lines if "222" in l and "Random" in l)
        # The "1" appears only in the Grp column for the grouped item.
        assert " 1 " in line_111
        # For the ungrouped item, between the max bid and time-left there are
        # only spaces where the group column lives.
        assert " 0 " not in line_222


# ---------------------------------------------------------------------------
# CLI: group command
# ---------------------------------------------------------------------------

class TestCliGroup:
    def test_assigns_group_preserving_bid_and_offset(self):
        from cli import cli

        runner = CliRunner()
        snipes = [
            {
                "item_id": "111",
                "max_bid": "75.00",
                "bid_offset": "4",
                "snipe_group": "0",
            },
            {
                "item_id": "222",
                "max_bid": "60.00",
                "bid_offset": "6",
                "snipe_group": "0",
            },
        ]

        with patch("cli._make_client") as mock_make:
            mock_client = MagicMock()
            mock_client.list_snipes.return_value = snipes
            mock_make.return_value = mock_client

            result = runner.invoke(cli, ["group", "1", "111", "222"])

        assert result.exit_code == 0
        # Two modify_snipe calls, each preserving the original bid + offset
        # and assigning the new group.
        calls = mock_client.modify_snipe.call_args_list
        assert len(calls) == 2

        # Call 1: item 111 keeps max_bid 75 and offset 4
        args, kwargs = calls[0]
        assert args[0] == "111"
        assert args[1] == Decimal("75.00")
        assert kwargs == {"bid_offset": 4, "snipe_group": 1}

        # Call 2: item 222 keeps max_bid 60 and offset 6
        args, kwargs = calls[1]
        assert args[0] == "222"
        assert args[1] == Decimal("60.00")
        assert kwargs == {"bid_offset": 6, "snipe_group": 1}

        assert "Updated 2 of 2" in result.output

    def test_ungroup_with_zero(self):
        from cli import cli

        runner = CliRunner()
        snipes = [
            {
                "item_id": "111",
                "max_bid": "10",
                "bid_offset": "6",
                "snipe_group": "1",
            }
        ]

        with patch("cli._make_client") as mock_make:
            mock_client = MagicMock()
            mock_client.list_snipes.return_value = snipes
            mock_make.return_value = mock_client

            result = runner.invoke(cli, ["group", "0", "111"])

        assert result.exit_code == 0
        kwargs = mock_client.modify_snipe.call_args.kwargs
        assert kwargs["snipe_group"] == 0

    def test_rejects_out_of_range_group(self):
        from cli import cli

        runner = CliRunner()

        with patch("cli._make_client") as mock_make:
            mock_make.return_value = MagicMock()
            result = runner.invoke(cli, ["group", "11", "111"])

        assert result.exit_code != 0
        # Click renders IntRange errors with the range in the message.
        assert "11" in result.output

    def test_aborts_when_any_item_id_missing(self):
        from cli import cli

        runner = CliRunner()
        snipes = [
            {
                "item_id": "111",
                "max_bid": "10",
                "bid_offset": "6",
                "snipe_group": "0",
            }
        ]

        with patch("cli._make_client") as mock_make:
            mock_client = MagicMock()
            mock_client.list_snipes.return_value = snipes
            mock_make.return_value = mock_client

            result = runner.invoke(cli, ["group", "1", "111", "999"])

        assert result.exit_code == 1
        assert "999" in result.output
        # No modifications attempted when validation fails.
        mock_client.modify_snipe.assert_not_called()

    def test_per_item_failure_continues_and_exits_nonzero(self):
        from cli import cli

        runner = CliRunner()
        snipes = [
            {
                "item_id": "111",
                "max_bid": "10",
                "bid_offset": "6",
                "snipe_group": "0",
            },
            {
                "item_id": "222",
                "max_bid": "20",
                "bid_offset": "6",
                "snipe_group": "0",
            },
        ]

        def modify_side_effect(item_id, *_, **__):
            if item_id == "111":
                raise GixenError("network blew up")
            return True

        with patch("cli._make_client") as mock_make:
            mock_client = MagicMock()
            mock_client.list_snipes.return_value = snipes
            mock_client.modify_snipe.side_effect = modify_side_effect
            mock_make.return_value = mock_client

            result = runner.invoke(cli, ["group", "1", "111", "222"])

        assert result.exit_code == 1
        # Both attempted despite the first failing.
        attempted = [c.args[0] for c in mock_client.modify_snipe.call_args_list]
        assert attempted == ["111", "222"]
        assert "Updated 1 of 2" in result.output

# ---------------------------------------------------------------------------
# CLI thin-client mode tests
# ---------------------------------------------------------------------------

from click.testing import CliRunner
from unittest.mock import MagicMock, patch
from cli import cli as cli_app


def test_cli_add_posts_to_server(monkeypatch):
    """When GIXEN_SERVER_URL is set, `add` POSTs to server."""
    monkeypatch.setenv("GIXEN_SERVER_URL", "http://localhost:8080")
    monkeypatch.setenv("GIXEN_USERNAME", "u")
    monkeypatch.setenv("GIXEN_PASSWORD", "p")

    runner = CliRunner()
    with runner.isolated_filesystem():
        with patch("cli.requests") as mock_req:
            mock_resp = MagicMock()
            mock_resp.raise_for_status.return_value = None
            mock_resp.json.return_value = {
                "item_id": "123456789", "status": "PENDING", "max_bid": 50.0
            }
            mock_req.post.return_value = mock_resp
            mock_req.get.return_value = mock_resp

            result = runner.invoke(cli_app, ["add", "123456789", "50.00"])
            assert result.exit_code == 0
            assert "Added snipe" in result.output
            mock_req.post.assert_called_once()
            call_url = mock_req.post.call_args[0][0]
            assert "/api/bids" in call_url


def test_cli_server_unreachable_shows_error(monkeypatch):
    """When server is unreachable, add fails with clear message."""
    import requests as req_lib
    monkeypatch.setenv("GIXEN_SERVER_URL", "http://localhost:8080")
    monkeypatch.setenv("GIXEN_USERNAME", "u")
    monkeypatch.setenv("GIXEN_PASSWORD", "p")

    runner = CliRunner()
    with runner.isolated_filesystem():
        with patch("cli.requests") as mock_req:
            # Make exception attributes real classes so except clauses work
            mock_req.ConnectionError = req_lib.ConnectionError
            mock_req.HTTPError = req_lib.HTTPError
            mock_req.post.side_effect = req_lib.ConnectionError("refused")
            result = runner.invoke(cli_app, ["add", "123456789", "50.00"])
            assert result.exit_code != 0
            assert "unreachable" in result.output.lower() or "error" in result.output.lower()


def test_cli_edit_patches_server(monkeypatch):
    """When GIXEN_SERVER_URL is set, `edit` sends PATCH to /api/bids/{item_id}."""
    monkeypatch.setenv("GIXEN_SERVER_URL", "http://localhost:8080")

    runner = CliRunner()
    with patch("cli.requests") as mock_req:
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = {"item_id": "123456789", "max_bid": 75.0, "status": "PENDING"}
        mock_req.patch.return_value = mock_resp

        result = runner.invoke(cli_app, ["edit", "123456789", "75.00"])
        assert result.exit_code == 0
        mock_req.patch.assert_called_once()
        call_url = mock_req.patch.call_args[0][0]
        assert "/api/bids/123456789" in call_url


def test_cli_remove_deletes_server(monkeypatch):
    """When GIXEN_SERVER_URL is set, `remove` sends DELETE to /api/bids/{item_id}."""
    monkeypatch.setenv("GIXEN_SERVER_URL", "http://localhost:8080")

    runner = CliRunner()
    with patch("cli.requests") as mock_req:
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = {"item_id": "123456789", "status": "REMOVED"}
        mock_req.delete.return_value = mock_resp

        result = runner.invoke(cli_app, ["remove", "123456789"])
        assert result.exit_code == 0
        mock_req.delete.assert_called_once()
        call_url = mock_req.delete.call_args[0][0]
        assert "/api/bids/123456789" in call_url


def test_cli_purge_posts_to_server(monkeypatch):
    """When GIXEN_SERVER_URL is set, `purge` sends POST to /api/purge."""
    monkeypatch.setenv("GIXEN_SERVER_URL", "http://localhost:8080")

    runner = CliRunner()
    with patch("cli.requests") as mock_req:
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = {"purged_completed": 2, "removed_siblings": 0}
        mock_req.post.return_value = mock_resp

        result = runner.invoke(cli_app, ["purge"])
        assert result.exit_code == 0
        mock_req.post.assert_called_once()
        call_url = mock_req.post.call_args[0][0]
        assert "/api/purge" in call_url


def test_cli_purge_dry_run_server_mode(monkeypatch):
    """purge --dry-run in server mode prints a message and makes no HTTP request."""
    monkeypatch.setenv("GIXEN_SERVER_URL", "http://localhost:8080")

    runner = CliRunner()
    with patch("cli.requests") as mock_req:
        result = runner.invoke(cli_app, ["purge", "--dry-run"])
        assert result.exit_code == 0
        assert "would purge" in result.output.lower()
        mock_req.post.assert_not_called()
