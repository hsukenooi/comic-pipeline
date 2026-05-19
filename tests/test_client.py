"""Tests for locg.client module."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from locg.client import AuthRequired, LOCGClient


def _make_client_with_session(ci_session: str | None = "abc123") -> LOCGClient:
    """Build an LOCGClient without touching the filesystem or launching Chrome."""
    cookies = [{"name": "ci_session", "value": ci_session}] if ci_session else []

    mock_context = MagicMock()
    mock_context.cookies.return_value = cookies
    mock_context.new_page.return_value = MagicMock()

    with patch("locg.client.sync_playwright") as mock_sync_pw, \
         patch("locg.client.playwright_profile_dir", return_value="/tmp/fake-profile"):
        mock_sync_pw.return_value.start.return_value.chromium.launch_persistent_context.return_value = mock_context
        client = LOCGClient()

    return client


def test_require_auth_verifies_once():
    """require_auth should call verify_session at most once per LOCGClient instance."""
    client = _make_client_with_session()
    client.verify_session = MagicMock(return_value=True)

    client.require_auth()
    client.require_auth()
    client.require_auth()

    assert client.verify_session.call_count == 1


def test_require_auth_expired_session_raises(monkeypatch):
    """If verify_session returns False, require_auth should raise AuthRequired."""
    monkeypatch.delenv("LOCG_USERNAME", raising=False)
    monkeypatch.delenv("LOCG_PASSWORD", raising=False)
    client = _make_client_with_session()
    client.verify_session = MagicMock(return_value=False)

    with pytest.raises(AuthRequired, match="Session expired"):
        client.require_auth()


def test_require_auth_no_cookie_raises(monkeypatch):
    """Without a ci_session cookie, require_auth raises before verify_session."""
    monkeypatch.delenv("LOCG_USERNAME", raising=False)
    monkeypatch.delenv("LOCG_PASSWORD", raising=False)
    client = _make_client_with_session(ci_session=None)
    client.verify_session = MagicMock(return_value=True)

    with pytest.raises(AuthRequired, match="Not logged in"):
        client.require_auth()

    client.verify_session.assert_not_called()


def test_require_auth_does_not_cache_on_transient_error():
    """If verify_session raises, the next require_auth should retry (not cache the failure)."""
    client = _make_client_with_session()
    call_count = {"n": 0}

    def side_effect():
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("simulated 429")
        return True

    client.verify_session = MagicMock(side_effect=side_effect)

    # First call — the exception propagates; we should NOT catch AuthRequired,
    # we should catch RuntimeError specifically.
    with pytest.raises(RuntimeError, match="simulated 429"):
        client.require_auth()

    # _server_auth_verified should still be None (not cached)
    assert client._server_auth_verified is None

    # Second call — verify_session is called again, returns True, no raise
    client.require_auth()
    assert call_count["n"] == 2
    assert client._server_auth_verified is True


def test_require_auth_expired_triggers_env_auto_login(monkeypatch):
    """Expired session with LOCG_USERNAME/LOCG_PASSWORD in env should auto-login."""
    client = _make_client_with_session()
    client.verify_session = MagicMock(return_value=False)
    client.login = MagicMock(return_value=True)

    monkeypatch.setenv("LOCG_USERNAME", "user")
    monkeypatch.setenv("LOCG_PASSWORD", "pass")

    client.require_auth()  # should NOT raise
    client.login.assert_called_once_with("user", "pass")


def test_require_auth_expired_no_env_still_raises(monkeypatch):
    """Without env creds, expired session still raises AuthRequired."""
    client = _make_client_with_session()
    client.verify_session = MagicMock(return_value=False)
    client.login = MagicMock(return_value=True)

    monkeypatch.delenv("LOCG_USERNAME", raising=False)
    monkeypatch.delenv("LOCG_PASSWORD", raising=False)

    with pytest.raises(AuthRequired, match="Session expired"):
        client.require_auth()
    client.login.assert_not_called()


def test_require_auth_auto_login_failure_raises(monkeypatch):
    """If env creds are set but login fails, still raise AuthRequired."""
    client = _make_client_with_session()
    client.verify_session = MagicMock(return_value=False)
    client.login = MagicMock(return_value=False)  # login fails

    monkeypatch.setenv("LOCG_USERNAME", "user")
    monkeypatch.setenv("LOCG_PASSWORD", "wrong")

    with pytest.raises(AuthRequired, match="Session expired"):
        client.require_auth()
    client.login.assert_called_once_with("user", "wrong")


def test_require_auth_no_cookie_triggers_env_auto_login(monkeypatch):
    """Missing ci_session cookie should also attempt auto-login when env creds set."""
    client = _make_client_with_session(ci_session=None)
    client.login = MagicMock(return_value=True)

    monkeypatch.setenv("LOCG_USERNAME", "user")
    monkeypatch.setenv("LOCG_PASSWORD", "pass")

    client.require_auth()
    client.login.assert_called_once_with("user", "pass")


def test_require_auth_env_login_swallows_exception(monkeypatch):
    """A rate-limit or network error during auto-login must surface as
    AuthRequired — never as a raw exception — so the CLI produces a clean
    exit-1 auth error instead of an exit-4 unexpected error."""
    client = _make_client_with_session()
    client.verify_session = MagicMock(return_value=False)
    client.login = MagicMock(side_effect=Exception("Rate limited. Retry after 60s"))

    monkeypatch.setenv("LOCG_USERNAME", "user")
    monkeypatch.setenv("LOCG_PASSWORD", "pass")

    with pytest.raises(AuthRequired, match="Session expired"):
        client.require_auth()


def test_require_auth_env_login_sets_verified_flag(monkeypatch):
    """After a successful env auto-login, _server_auth_verified must be True
    so the next require_auth() skips verify_session. The invariant cannot
    rely on login()'s internal side effects because tests (and future
    refactors) may bypass them."""
    client = _make_client_with_session(ci_session=None)
    client.login = MagicMock(return_value=True)

    monkeypatch.setenv("LOCG_USERNAME", "user")
    monkeypatch.setenv("LOCG_PASSWORD", "pass")

    client.require_auth()
    assert client._server_auth_verified is True


def test_require_auth_partial_env_does_not_login(monkeypatch):
    """Only one of LOCG_USERNAME/LOCG_PASSWORD set — no auto-login attempt."""
    client = _make_client_with_session()
    client.verify_session = MagicMock(return_value=False)
    client.login = MagicMock(return_value=True)

    monkeypatch.setenv("LOCG_USERNAME", "user")
    monkeypatch.delenv("LOCG_PASSWORD", raising=False)

    with pytest.raises(AuthRequired, match="Session expired"):
        client.require_auth()
    client.login.assert_not_called()


def test_login_success_primes_verified_cache():
    """On successful login, _server_auth_verified should be True so
    the next require_auth call doesn't re-verify."""
    client = _make_client_with_session(ci_session=None)  # start with no cookie

    # After a successful POST /login the server sets ci_session;
    # simulate this by having post() return a 200 and then updating
    # the mock context to report the cookie as present.
    def fake_post(path, data=None):
        client._context.cookies.return_value = [{"name": "ci_session", "value": "xyz"}]
        resp = MagicMock()
        resp.status_code = 200
        return resp

    client.post = MagicMock(side_effect=fake_post)
    client.verify_session = MagicMock(return_value=True)

    assert client.login("user", "pass") is True
    assert client._server_auth_verified is True

    # Now require_auth should NOT call verify_session again
    client.require_auth()
    assert client.verify_session.call_count == 1  # only the one from login()


def test_close_tears_down_playwright():
    """close() must shut down the context and the Playwright driver."""
    client = _make_client_with_session()
    client.close()

    client._context.close.assert_called_once()
    client._playwright_instance.stop.assert_called_once()


def test_wrap_response_maps_fields():
    """_wrap_playwright_response maps APIResponse fields to _PlaywrightResponse."""
    from locg.client import _PlaywrightResponse, _wrap_response

    api_resp = MagicMock()
    api_resp.status = 200
    api_resp.text.return_value = "hello"
    api_resp.body.return_value = b"hello"
    api_resp.headers = {"content-type": "text/html", "retry-after": "30"}

    result = _wrap_response(api_resp)

    assert isinstance(result, _PlaywrightResponse)
    assert result.status_code == 200
    assert result.text == "hello"
    assert result.content == b"hello"
    assert result.headers["retry-after"] == "30"
    api_resp.dispose.assert_called_once()


def test_wrap_response_empty_body():
    """Empty body produces content=b'' and text='', not None."""
    from locg.client import _wrap_response

    api_resp = MagicMock()
    api_resp.status = 204
    api_resp.text.return_value = ""
    api_resp.body.return_value = b""
    api_resp.headers = {}

    result = _wrap_response(api_resp)

    assert result.content == b""
    assert result.text == ""
