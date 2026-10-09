"""eBay OAuth *user* token store (BUI-1209).

Every other eBay call in this app uses an app-level client_credentials token,
which reaches public data only. Member data (the watchlist, via the Trading
API) needs a user token: a long-lived refresh token, obtained once through the
browser consent flow (`ebay-auth login`), from which short-lived access tokens
are minted on demand.

Public API for callers (BUI-1210 `ebay-watchlist`):

    get_user_access_token() -> str      # raises UserTokenError
    UserTokenError                      # .exit_code: 3 re-login needed, 5 network

Nothing here ever prints a token or the client secret.
"""

import base64
import json
import sys
import time
import urllib.parse
from datetime import datetime, timezone

import requests

from ebay_fetch import CONFIG_DIR, CONFIG_FILE, atomic_write_json, load_config

TOKEN_FILE = CONFIG_DIR / "user_token_production.json"
ACCESS_CACHE_FILE = CONFIG_DIR / "user_access_cache_production.json"
SCOPE = "https://api.ebay.com/oauth/api_scope"
TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
AUTH_URL = "https://auth.ebay.com/oauth2/authorize"
RELOGIN_HINT = "run `ebay-auth login`"
CACHE_MARGIN_S = 300  # reuse a cached access token until 5 minutes before expiry
WARN_WITHIN_DAYS = 30
EXIT_RELOGIN = 3
EXIT_NETWORK = 5


class UserTokenError(Exception):
    """The user token is unusable. `exit_code` follows the plan's error contract."""

    def __init__(self, message, exit_code=EXIT_RELOGIN):
        super().__init__(message)
        self.exit_code = exit_code


def _relogin(reason):
    return UserTokenError(f"{reason}; {RELOGIN_HINT}", EXIT_RELOGIN)


def load_token_file():
    """Return the saved token dict, or raise UserTokenError if missing/unreadable."""
    try:
        data = json.loads(TOKEN_FILE.read_text())
    except FileNotFoundError:
        raise _relogin(f"eBay user token not found at {TOKEN_FILE}") from None
    except (OSError, ValueError) as exc:
        raise _relogin(f"eBay user token file unreadable ({type(exc).__name__})") from None
    if not isinstance(data, dict) or not data.get("refresh_token"):
        raise _relogin("eBay user token file has no refresh_token")
    return data


def refresh_expiry(data):
    """Return (expires_at_epoch, obtained_source) for the refresh token.

    obtained_at (epoch seconds, written by `ebay-auth login`) is preferred. The
    file eBay's raw response produced during the 2026-10-09 spike has none, so
    fall back to the file's mtime. Returns (None, ...) if the lifetime is unknown.
    """
    lifetime = data.get("refresh_token_expires_in")
    if not isinstance(lifetime, (int, float)):
        return None, "unknown"
    obtained = data.get("obtained_at")
    if isinstance(obtained, (int, float)):
        return obtained + lifetime, "obtained_at"
    try:
        return TOKEN_FILE.stat().st_mtime + lifetime, "file mtime"
    except OSError:
        return None, "unknown"


def _fmt(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%d")


def warn_if_expiring(data, now=None):
    """Print a stderr warning when the refresh token expires within 30 days."""
    now = time.time() if now is None else now
    expires_at, _ = refresh_expiry(data)
    if expires_at is None:
        return
    days = (expires_at - now) / 86400
    if 0 < days <= WARN_WITHIN_DAYS:
        print(
            f"Warning: eBay user token expires in {days:.0f} days "
            f"({_fmt(expires_at)}); {RELOGIN_HINT} before then.",
            file=sys.stderr,
        )


def _basic_auth(client_id, client_secret):
    raw = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    return f"Basic {raw}"


def token_request(form):
    """POST a grant to the token endpoint. Returns the JSON dict.

    Raises UserTokenError: exit 3 on invalid_grant, exit 5 on network/HTTP errors.
    The response body is never echoed except eBay's `error` code.
    """
    client_id, client_secret, _ = load_config()
    try:
        resp = requests.post(
            TOKEN_URL,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Authorization": _basic_auth(client_id, client_secret),
            },
            data=form,
            timeout=30,
        )
    except requests.RequestException as exc:
        raise UserTokenError(
            f"network error calling {TOKEN_URL}: {type(exc).__name__}", EXIT_NETWORK
        ) from None
    if resp.status_code != 200:
        try:
            error = resp.json().get("error")
        except (ValueError, AttributeError):
            error = None
        if error == "invalid_grant":
            raise _relogin("eBay rejected the grant (invalid_grant: expired, revoked, or bad code)")
        raise UserTokenError(
            f"token endpoint returned HTTP {resp.status_code} ({error or 'no error code'}) "
            f"at {TOKEN_URL}",
            EXIT_NETWORK,
        )
    try:
        body = resp.json()
        body["access_token"]
    except (ValueError, KeyError, TypeError):
        raise UserTokenError(f"malformed token response from {TOKEN_URL}", EXIT_NETWORK) from None
    return body


def _read_cached_access_token(now):
    try:
        cache = json.loads(ACCESS_CACHE_FILE.read_text())
        if now < cache["expires_at"] - CACHE_MARGIN_S:
            return cache["access_token"]
    except Exception:  # noqa: BLE001  # missing/malformed cache is a cache miss
        pass
    return None


def get_user_access_token():
    """Return a valid user access token, minting and caching one if needed.

    Raises UserTokenError (exit_code 3: re-login needed; 5: network). Emits the
    30-day expiry warning on every call, cache hit or not.
    """
    data = load_token_file()
    warn_if_expiring(data)
    now = time.time()
    expires_at, _ = refresh_expiry(data)
    if expires_at is not None and expires_at <= now:
        raise _relogin(f"eBay user refresh token expired {_fmt(expires_at)}")

    cached = _read_cached_access_token(now)
    if cached:
        return cached

    body = token_request(
        {"grant_type": "refresh_token", "refresh_token": data["refresh_token"], "scope": SCOPE}
    )
    access_token = body["access_token"]
    try:
        atomic_write_json(
            ACCESS_CACHE_FILE,
            {"access_token": access_token, "expires_at": now + body.get("expires_in", 7200)},
            mode=0o600,
        )
    except OSError as exc:
        print(f"Warning: could not write user token cache: {exc}", file=sys.stderr)
    return access_token


def consent_url(client_id, runame):
    query = urllib.parse.urlencode(
        {"client_id": client_id, "redirect_uri": runame, "response_type": "code", "scope": SCOPE}
    )
    return f"{AUTH_URL}?{query}"


def extract_code(pasted):
    """Pull the authorization code out of a pasted redirect URL (or a bare code)."""
    pasted = pasted.strip()
    qs = urllib.parse.parse_qs(urllib.parse.urlparse(pasted).query)
    return (qs.get("code") or [pasted])[0]


def load_runame():
    try:
        runame = json.loads(CONFIG_FILE.read_text()).get("runame")
    except (OSError, ValueError, AttributeError):
        runame = None
    if not runame:
        raise UserTokenError(f'no RuName: add a "runame" key to {CONFIG_FILE}', EXIT_RELOGIN)
    return runame


def exchange_code(code, runame):
    """Exchange an authorization code and write the token file (0600, obtained_at)."""
    body = token_request(
        {"grant_type": "authorization_code", "code": code, "redirect_uri": runame}
    )
    if not body.get("refresh_token"):
        raise UserTokenError("token response had no refresh_token", EXIT_NETWORK)
    body["obtained_at"] = time.time()
    atomic_write_json(TOKEN_FILE, body, mode=0o600)
    try:
        ACCESS_CACHE_FILE.unlink()  # drop the cache minted from the old refresh token
    except OSError:
        pass
    return body


def describe_state(now=None):
    """Return (state, details dict) for `ebay-auth status`. Never includes secrets."""
    now = time.time() if now is None else now
    try:
        data = load_token_file()
    except UserTokenError as exc:
        return "missing", {"detail": str(exc)}
    expires_at, source = refresh_expiry(data)
    details = {"file": str(TOKEN_FILE), "obtained from": source}
    if expires_at is None:
        return "unknown", details
    days = (expires_at - now) / 86400
    details["refresh token expires"] = f"{_fmt(expires_at)} ({days:.0f} days)"
    if days <= 0:
        return "expired", details
    return ("expiring" if days <= WARN_WITHIN_DAYS else "valid"), details
