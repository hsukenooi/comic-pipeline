"""Unit tests for gixen_client.py — all network calls are mocked."""

import pytest
from decimal import Decimal
from unittest.mock import patch, MagicMock, PropertyMock

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from gixen_client import (
    GixenClient,
    GixenError,
    GixenLoginError,
    GixenSessionExpiredError,
    GixenItemError,
    GixenSnipeNotFoundError,
    GixenParseError,
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

        with patch.object(client, "_post_home", return_value="<html>OK</html>") as mock_post:
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

        with patch.object(client, "_post_home", return_value="<html>OK</html>") as mock_post:
            client.add_snipe("444", Decimal("5"), bid_offset=3, snipe_group=2)

        data = mock_post.call_args[0][0]
        assert data["newbidoffset"] == "3"
        assert data["newsnipegroup"] == "2"

    def test_add_item_not_found(self):
        client = _client()
        client.session_id = "99887766"

        with patch.object(client, "_post_home", side_effect=GixenItemError(299, "The specified item Id was not found.")):
            with pytest.raises(GixenItemError) as exc_info:
                client.add_snipe("999", Decimal("1.00"))
            assert exc_info.value.code == 299

    def test_add_duplicate(self):
        client = _client()
        client.session_id = "99887766"

        with patch.object(client, "_post_home", side_effect=GixenItemError(202, "ITEM ALREADY PRESENT")):
            with pytest.raises(GixenItemError) as exc_info:
                client.add_snipe("111", Decimal("5"))
            assert exc_info.value.code == 202


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

        with patch.object(client, "list_snipes", return_value=snipes):
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

    def test_item_error_has_code(self):
        err = GixenItemError(299, "Not found")
        assert err.code == 299
        assert err.message == "Not found"
        assert "299" in str(err)
