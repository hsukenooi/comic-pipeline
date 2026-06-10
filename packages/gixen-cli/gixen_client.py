"""Gixen web client — automates the Gixen.com web interface via HTTP requests.

Gixen's official API (api.php/xmlapi.php) is disabled for some accounts.
This client logs into the web UI and performs operations by submitting the
same HTML forms a browser would.
"""

import os
import re
import subprocess
import logging
import tempfile
import time
from decimal import Decimal, InvalidOperation
from typing import Dict, List, Optional

import requests

logger = logging.getLogger(__name__)


def _response_snippet(text: str, username: Optional[str] = None, limit: int = 200) -> str:
    """Truncated, secret-redacted slice of a Gixen response body, for logging.

    BUI-114: when Gixen returns an unexpected body (HTTP 5xx, or a 200 page that
    isn't the snipe table), we log the first ``limit`` chars so the failure mode
    is diagnosable instead of opaque. Redacts the session id and username so a
    captured snippet never leaks credentials into the log file, and collapses
    whitespace so the snippet stays on a single log line.
    """
    snippet = (text or "")[:limit]
    snippet = re.sub(r"sessionid=\d+", "sessionid=REDACTED", snippet)
    if username:
        snippet = snippet.replace(username, "REDACTED_USER")
    return re.sub(r"\s+", " ", snippet).strip()


# ---------------------------------------------------------------------------
# Curl-based HTTP session (bypasses LibreSSL 2.8.3 TLS compatibility issues)
# ---------------------------------------------------------------------------

class _CurlResponse:
    def __init__(self, status_code: int, text: str):
        self.status_code = status_code
        self.text = text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)


class _CurlSession:
    """Drop-in replacement for requests.Session using curl subprocess.

    macOS system Python uses LibreSSL 2.8.3, which has TLS data-transfer
    bugs with some servers despite completing the handshake. Curl ships with
    LibreSSL 3.3.6 + SecureTransport and works reliably.
    """

    def __init__(self):
        self.headers: Dict[str, str] = {}
        self._cookie_jar = tempfile.NamedTemporaryFile(suffix=".txt", delete=False).name

    def _run(self, method: str, url: str, data: Optional[Dict] = None,
             timeout: float = 15.0, allow_redirects: bool = True) -> _CurlResponse:
        cmd = ["curl", "-s", "-D", "-", "--max-time", str(int(timeout)),
               "-b", self._cookie_jar, "-c", self._cookie_jar]
        if not allow_redirects:
            cmd += ["--max-redirs", "0"]
        for k, v in self.headers.items():
            cmd += ["-H", f"{k}: {v}"]
        if method == "POST":
            for k, v in (data or {}).items():
                cmd += ["--data-urlencode", f"{k}={v}"]
        cmd += [url]

        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True,
                timeout=timeout + 10,
            )
        except subprocess.TimeoutExpired:
            raise requests.ReadTimeout(f"curl timed out for {url}")

        # A non-zero curl exit is a transport-level failure (DNS, connect, TLS,
        # timeout) — stdout is typically empty. Parsing it would yield a
        # misleading "200 + empty body", which login() then misattributes to
        # bad credentials (BUI-77). Surface it as a requests-style connection
        # error instead so callers can classify it as connectivity. Curl exit
        # 28 == operation timeout; everything else maps to a connection error.
        if result.returncode != 0:
            detail = (result.stderr or "").strip() or f"curl exit {result.returncode}"
            if result.returncode == 28:
                raise requests.ConnectTimeout(f"{url}: {detail}")
            raise requests.ConnectionError(f"{url}: {detail}")

        raw = result.stdout
        sep = raw.find("\r\n\r\n")
        if sep < 0:
            sep = raw.find("\n\n")
            body = raw[sep + 2:] if sep >= 0 else raw
            header_block = raw[:sep] if sep >= 0 else ""
        else:
            header_block = raw[:sep]
            body = raw[sep + 4:]

        status_code = 200
        m = re.match(r"HTTP/\S+ (\d{3})", header_block)
        if m:
            status_code = int(m.group(1))

        return _CurlResponse(status_code, body)

    def get(self, url: str, timeout: float = 15.0, **_) -> _CurlResponse:
        return self._run("GET", url, timeout=timeout)

    def post(self, url: str, data: Optional[Dict] = None, timeout: float = 15.0,
             allow_redirects: bool = True, **_) -> _CurlResponse:
        return self._run("POST", url, data=data, timeout=timeout,
                         allow_redirects=allow_redirects)

GIXEN_BASE = "https://www.gixen.com/main"
LOGIN_URL = f"{GIXEN_BASE}/home_1.php"
HOME_URL = f"{GIXEN_BASE}/home_2.php"


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class GixenError(Exception):
    """Base exception for Gixen client errors."""


class GixenLoginError(GixenError):
    """Bad credentials or account suspended."""


class GixenConnectionError(GixenError):
    """Could not reach Gixen at the network layer (DNS, connect, TLS, timeout).

    Distinct from GixenLoginError: this means we never got a usable response
    from the host, so it is a connectivity problem, not a credentials problem.
    A black-holed/unreachable host (BUI-77) lands here instead of being
    misattributed to bad credentials.
    """


class GixenSessionExpiredError(GixenError):
    """Session timed out and re-login also failed."""


class GixenItemError(GixenError):
    """Gixen returned an item-level error (e.g. item not found, duplicate)."""

    def __init__(self, code: int, message: str):
        self.code = code
        self.message = message
        super().__init__(f"Gixen error {code}: {message}")


class GixenSnipeNotFoundError(GixenError):
    """Item not found in the current snipe list (for modify/remove)."""


class GixenParseError(GixenError):
    """HTML response didn't match expected structure."""


class GixenSnipeTableMissingError(GixenParseError):
    """The snipe table/form was absent from the response entirely.

    BUI-115: this specific shape is, in practice, almost always a stale-session
    response (login page, "could not log you in" wrong-alert, anti-bot page) that
    _is_session_expired didn't match — so list_snipes recovers from it with one
    re-login + retry. Distinct from a generic GixenParseError (e.g. a malformed
    field inside an otherwise-valid table), where re-login would not help.
    """


class GixenAddNotConfirmedError(GixenError):
    """add_snipe POST returned no error but the item never appeared in the list."""

    def __init__(self, item_id: str):
        self.item_id = str(item_id)
        super().__init__(
            f"Gixen accepted add for item {item_id} but it never appeared in the "
            f"snipe list — likely silently rate-limited or dropped."
        )


class GixenModifyNotConfirmedError(GixenError):
    """modify_snipe POST returned no error but the new max_bid never went live.

    BUI-115 parity with GixenAddNotConfirmedError: a silently-dropped modify
    must not be reported as success, or the local DB would show the new bid
    while Gixen kept the old one.
    """

    def __init__(self, item_id: str, max_bid):
        self.item_id = str(item_id)
        self.max_bid = max_bid
        super().__init__(
            f"Gixen accepted modify for item {item_id} (new max_bid {max_bid}) but "
            f"the change never appeared in the snipe list — likely silently dropped."
        )


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

_LOGIN_COOLDOWN = 300  # seconds to wait after a failed login before retrying


class GixenClient:
    """Web-scraping client for Gixen.com."""

    # Minimum seconds between Gixen write POSTs — prevents silent drops during bursts.
    _min_post_gap: float = 1.5
    # Backoff before retrying an add_snipe that wasn't confirmed by list_snipes.
    _add_retry_backoff: float = 5.0
    # Account-keyed monotonic timestamp of the last _post_home call. Class-
    # level so two GixenClient instances sharing the same username (e.g.
    # _api_client + _sync_client in the server) actually serialize against
    # Gixen-side rate limits. Without this, the two clients each carry their
    # own _last_post_at and the throttle is per-instance, defeating the
    # bursts-protection intent.
    _last_post_at_by_user: dict[str, float] = {}

    def __init__(
        self,
        username: Optional[str] = None,
        password: Optional[str] = None,
        timeout: float = 15.0,
    ):
        self.username = username or os.getenv("GIXEN_USERNAME", "")
        self.password = password or os.getenv("GIXEN_PASSWORD", "")
        self.timeout = timeout
        self.session = _CurlSession()
        self.session_id: Optional[str] = None
        self._login_failed_at: Optional[float] = None  # monotonic timestamp

    @property
    def _last_post_at(self) -> Optional[float]:
        return type(self)._last_post_at_by_user.get(self.username)

    @_last_post_at.setter
    def _last_post_at(self, value: Optional[float]) -> None:
        if value is None:
            type(self)._last_post_at_by_user.pop(self.username, None)
        else:
            type(self)._last_post_at_by_user[self.username] = value

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    @staticmethod
    def _connection_error(url: str, exc: Exception) -> "GixenConnectionError":
        """Wrap a transport-level failure in a GixenError-class connectivity error."""
        return GixenConnectionError(
            f"Could not reach Gixen at {url}: {exc}. The host may be down or "
            "unreachable from this network — a connectivity problem, not a "
            "credentials problem."
        )

    def login(self) -> str:
        """Log in to Gixen and return the session ID.

        Raises:
            GixenConnectionError: If Gixen is unreachable (DNS/connect/TLS/timeout)
                or returns an empty response.
            GixenLoginError: If credentials are wrong or account is suspended.
            GixenLoginError: If called within the cooldown window after a failure.
        """
        if self._login_failed_at is not None:
            elapsed = time.monotonic() - self._login_failed_at
            remaining = _LOGIN_COOLDOWN - elapsed
            if remaining > 0:
                raise GixenLoginError(
                    f"Login cooldown active — retry in {int(remaining)}s. "
                    "Backing off to avoid IP rate-limiting."
                )

        try:
            resp = self.session.post(
                LOGIN_URL,
                data={
                    "username": self.username,
                    "password": self.password,
                    "signin": "signin",
                },
                timeout=self.timeout,
                allow_redirects=False,
            )
        except (requests.ConnectionError, requests.Timeout) as e:
            # Host unreachable / black-holed / timed out — never got a response.
            # Classify as connectivity, not credentials (BUI-77).
            self._login_failed_at = time.monotonic()
            raise self._connection_error(LOGIN_URL, e) from e
        except Exception:
            self._login_failed_at = time.monotonic()
            raise

        # Gixen returns HTML with a meta-refresh containing the sessionid
        match = re.search(r'sessionid=(\d+)', resp.text)
        if not match:
            self._login_failed_at = time.monotonic()
            # An empty/blank body with no sessionid is the signature of a
            # truncated connection or a flapping host, not an auth rejection —
            # a real rejection returns the login HTML (form + error). Only the
            # latter is a credentials problem (BUI-77).
            if not (resp.text or "").strip():
                raise GixenConnectionError(
                    f"Gixen returned an empty response from {LOGIN_URL} — the "
                    "host is likely unreachable or flapping. This is a "
                    "connectivity problem, not a credentials problem."
                )
            raise GixenLoginError(
                "Login failed — Gixen returned a page with no session ID. "
                "If credentials are correct, Gixen's login page may have "
                "changed. Check your GIXEN_USERNAME and GIXEN_PASSWORD."
            )

        self._login_failed_at = None
        self.session_id = match.group(1)
        # Clear the post-throttle: re-login already takes seconds and has
        # effectively spaced the requests. Without this, the recursion path
        # in _post_home (500 → relogin → retry) stacks throttle on top of
        # login latency.
        self._last_post_at = None
        logger.info("Logged in to Gixen (session_id=%s...)", self.session_id[:8])
        return self.session_id

    def _ensure_session(self) -> str:
        """Return current session ID, logging in if needed."""
        if not self.session_id:
            self.login()
        return self.session_id

    def _home_url(self) -> str:
        return f"{HOME_URL}?sessionid={self._ensure_session()}"

    def _is_session_expired(self, html: str) -> bool:
        """Detect if the response indicates an expired session."""
        # Expired sessions redirect to login or show the login form
        if (
            'name="signin"' in html
            and 'name="username"' in html
            and 'sessionid=' not in html
        ):
            return True
        # Server-invalidated session_id: Gixen serves the homepage with a
        # "Could not log you in. (33)" wrong-alert div instead of the snipe
        # table. Without this, the parser raises GixenParseError and the
        # auto-relogin path never fires.
        if 'wrong-alert' in html and 'Could not log you in' in html:
            return True
        return False

    def _get_home_page(self, retry_on_expired: bool = True) -> str:
        """Fetch the main snipe page. Auto-re-login on session expiration."""
        url = self._home_url()
        try:
            resp = self.session.get(url, timeout=self.timeout)
        except (requests.ConnectionError, requests.Timeout) as e:
            raise self._connection_error(url, e) from e
        if resp.status_code >= 400:
            # BUI-114: Gixen serves HTTP 500 for a stale session (among other
            # causes). Capture what it returned before raising so the failure is
            # diagnosable — BUI-115 uses this to broaden session-expiry detection.
            logger.warning(
                "GET home page returned HTTP %s; body snippet: %s",
                resp.status_code, _response_snippet(resp.text, self.username),
            )
        # BUI-115: Gixen returns HTTP 500 for a stale/invalid session. The POST
        # path (_post_home) already recovers from this; the GET path did not,
        # which is why modify/remove (which list_snipes first) failed ~17% of
        # the time while add (POST-only) self-healed. Mirror the POST recovery:
        # re-login once and retry. A second 500 falls through to raise_for_status
        # so a genuinely-down Gixen still fails loudly.
        if resp.status_code == 500 and retry_on_expired:
            logger.info("Gixen returned 500 on GET, forcing re-login")
            self.session_id = None
            self.login()
            return self._get_home_page(retry_on_expired=False)
        resp.raise_for_status()
        html = resp.text

        if self._is_session_expired(html):
            if retry_on_expired:
                logger.info("Session expired, re-logging in")
                self.session_id = None
                self.login()
                return self._get_home_page(retry_on_expired=False)
            raise GixenSessionExpiredError("Session expired and re-login failed")

        return html

    def _post_home(self, data: dict, retry_on_expired: bool = True, check_errors: bool = True) -> str:
        """POST to the home page. Auto-re-login on session expiration."""
        if self._min_post_gap and self._last_post_at is not None:
            elapsed = time.monotonic() - self._last_post_at
            remaining = self._min_post_gap - elapsed
            if remaining > 0:
                time.sleep(remaining)

        url = self._home_url()
        try:
            resp = self.session.post(url, data=data, timeout=self.timeout)
        except (requests.ConnectionError, requests.Timeout) as e:
            raise self._connection_error(url, e) from e
        self._last_post_at = time.monotonic()

        # Gixen returns HTTP 500 for requests with a stale/invalid session.
        # Treat it as session expiry and retry after re-login.
        if resp.status_code == 500 and retry_on_expired:
            logger.info("Gixen returned 500 on POST, forcing re-login")
            self.session_id = None
            self.login()
            return self._post_home(data, retry_on_expired=False, check_errors=check_errors)

        if resp.status_code >= 400:
            # BUI-114: capture the body for any non-500 HTTP error reaching here
            # (the 500-on-stale-session case is handled by the re-login above).
            logger.warning(
                "POST home page returned HTTP %s; body snippet: %s",
                resp.status_code, _response_snippet(resp.text, self.username),
            )
        resp.raise_for_status()
        html = resp.text

        if self._is_session_expired(html):
            if retry_on_expired:
                logger.info("Session expired, re-logging in")
                self.session_id = None
                self.login()
                return self._post_home(data, retry_on_expired=False, check_errors=check_errors)
            raise GixenSessionExpiredError("Session expired and re-login failed")

        if check_errors:
            self._check_html_error(html)
        return html

    # ------------------------------------------------------------------
    # Error detection
    # ------------------------------------------------------------------

    @staticmethod
    def _check_html_error(html: str) -> None:
        """Check for Gixen error messages in the HTML response."""
        match = re.search(
            r'<font color="red">Error \((\d+)\):\s*[\'"]?(.+?)[\'"]?</font>',
            html, re.IGNORECASE,
        )
        if match:
            code = int(match.group(1))
            message = match.group(2).strip()
            if code == 115:
                raise GixenLoginError(f"Account suspended (error {code})")
            raise GixenItemError(code, message)

        # Also check for the "Could not add" message that follows some errors
        match = re.search(
            r'<font color="red">Could not add this item \((\d+)\)',
            html, re.IGNORECASE,
        )
        # This is already caught by the Error pattern above; only here as fallback

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def list_snipes(self) -> List[Dict[str, str]]:
        """Fetch and parse the current snipe list.

        Returns:
            List of dicts with keys: item_id, title, max_bid, current_bid,
            status, status_mirror, time_to_end, bid_offset, bid_offset_mirror,
            dbidid, snipe_group, seller.
        """
        html = self._get_home_page()
        try:
            return self._parse_snipe_table(html)
        except GixenSnipeTableMissingError:
            # BUI-115: a 200 body that isn't the snipe table is, in practice,
            # almost always a stale-session response that _is_session_expired
            # didn't match (login page, "could not log you in" wrong-alert,
            # anti-bot interstitial). Production saw 743 of these surface as 503
            # with the auto-relogin never firing. Rather than hard-code a brittle
            # body signature, recover structurally: force one re-login + re-fetch
            # and parse again. A second parse failure is a real drift/outage and
            # propagates. Bounded to one extra login per call.
            logger.info("snipe table missing, forcing re-login and retrying once")
            self.session_id = None
            self.login()
            html = self._get_home_page(retry_on_expired=False)
            return self._parse_snipe_table(html)

    def add_snipe(
        self,
        item_id: str,
        max_bid: Decimal,
        bid_offset: int = 6,
        snipe_group: int = 0,
    ) -> bool:
        """Add a new snipe.

        Returns True on success.

        Raises:
            GixenItemError: If the item can't be added (not found, duplicate, etc.)
            GixenAddNotConfirmedError: If the POST returned no error but the snipe
                never appeared in the snipe list (Gixen silently dropped it),
                even after one retry. Also raised when the verify list_snipes
                itself fails (parse error, HTTP error) — in that case we cannot
                tell whether the POST landed, so we refuse to double-POST.
        """
        data = {
            "newitemid": str(item_id),
            "newmaxbid": str(max_bid),
            "newbidoffset": str(bid_offset),
            "newbidoffsetmirror": str(bid_offset),
            "newsnipegroup": str(snipe_group),
            "username": self.username,
        }
        target = str(item_id)

        self._post_home(data)

        # Verify the POST landed. If list_snipes itself fails (parser drift,
        # network blip), we can't know whether the POST succeeded — and
        # double-POSTing in that uncertain state risks duplicate snipes. Bail
        # with AddNotConfirmedError so the caller can investigate.
        try:
            snipes = self.list_snipes()
        except (GixenParseError, requests.HTTPError, GixenSessionExpiredError) as e:
            logger.warning(
                "add_snipe for item=%s: verify list_snipes failed (%s); "
                "refusing to double-POST",
                item_id, e,
            )
            raise GixenAddNotConfirmedError(item_id) from e

        if any(s["item_id"] == target for s in snipes):
            logger.info("Added snipe: item=%s, max_bid=%s", item_id, max_bid)
            return True

        # Silent drop: Gixen returned 200 with no error banner, but the snipe
        # never landed. Back off and retry once before giving up.
        logger.warning(
            "add_snipe for item=%s not confirmed in list; retrying after %.1fs",
            item_id, self._add_retry_backoff,
        )
        if self._add_retry_backoff:
            time.sleep(self._add_retry_backoff)

        # Retry POST. Catch the eventual-consistency race: Gixen accepted the
        # original POST but the verify GET was served from a stale view; the
        # retry POST then trips ITEM ALREADY PRESENT (code 202). Treat 202 +
        # subsequent verify-shows-item as success (the first POST really
        # landed). Any other GixenItemError bubbles up.
        try:
            self._post_home(data)
        except GixenItemError as e:
            if e.code == 202:
                try:
                    snipes = self.list_snipes()
                except (GixenParseError, requests.HTTPError, GixenSessionExpiredError):
                    raise GixenAddNotConfirmedError(item_id) from e
                if any(s["item_id"] == target for s in snipes):
                    logger.info(
                        "add_snipe for item=%s: first POST landed, retry hit "
                        "202; treating as success", item_id,
                    )
                    return True
                # 202 but verify still doesn't see it → genuinely confused.
                raise GixenAddNotConfirmedError(item_id) from e
            raise

        try:
            snipes = self.list_snipes()
        except (GixenParseError, requests.HTTPError, GixenSessionExpiredError) as e:
            raise GixenAddNotConfirmedError(item_id) from e

        if any(s["item_id"] == target for s in snipes):
            logger.info("Added snipe on retry: item=%s, max_bid=%s", item_id, max_bid)
            return True

        raise GixenAddNotConfirmedError(item_id)

    @staticmethod
    def _max_bid_matches(actual: str, expected: Decimal) -> bool:
        """Compare a snipe-list max_bid against the requested value as Decimals.

        Tolerates Gixen formatting drift: strips anything that isn't part of a
        decimal number (a currency suffix like " USD", stray whitespace, thousands
        separators) before comparing, so 40, 40.00, and "40.00 USD" all match
        40.00. A false mismatch here would raise GixenModifyNotConfirmedError for
        a modify that actually landed (503 + the DB left showing the old bid), so
        the comparison errs toward recognizing equivalent values.
        """
        try:
            cleaned = re.sub(r"[^0-9.\-]", "", str(actual))
            if cleaned in ("", ".", "-"):
                return False
            return Decimal(cleaned) == Decimal(str(expected))
        except (InvalidOperation, TypeError):
            return False

    def modify_snipe(
        self,
        item_id: str,
        max_bid: Decimal,
        bid_offset: int = 6,
        snipe_group: int = 0,
        dbidid: Optional[str] = None,
    ) -> bool:
        """Modify an existing snipe's bid.

        BUI-115: verifies the new max_bid actually went live in Gixen before
        returning success, mirroring add_snipe's confirmation. A silently-dropped
        modify is retried once, then raised as GixenModifyNotConfirmedError rather
        than reported as success (which would leave the DB lying about the bid).

        BUI-116: when ``dbidid`` (Gixen's internal row id) is supplied, the
        pre-POST list_snipes() lookup is skipped — this is the edit fast-path.
        A stale cached dbidid is caught by the post-POST verify below; the caller
        (the server, which owns the cache) handles re-resolving and retrying.

        Raises:
            GixenSnipeNotFoundError: If item_id is not in the snipe list (only
                when dbidid is not supplied — the lookup path).
            GixenModifyNotConfirmedError: If the modify POST returned no error but
                the new max_bid never appeared in the list, even after one retry.
        """
        target = str(item_id)
        if dbidid is None:
            snipe = self._find_snipe(self.list_snipes(), target)
            dbidid = snipe["dbidid"]

        data = {
            "newitemid": str(item_id),
            "newmaxbid": str(max_bid),
            "newbidoffset": str(bid_offset),
            "newbidoffsetmirror": str(bid_offset),
            "newsnipegroup": str(snipe_group),
            "username": self.username,
            "dbidid": dbidid,
            "ismodified": "1",
        }

        def _confirmed() -> bool:
            # Re-read AFTER the POST and confirm the new max_bid is live. A list
            # failure here propagates (GixenError -> 503) rather than confirming.
            for s in self.list_snipes():
                if s["item_id"] == target:
                    return self._max_bid_matches(s.get("max_bid", ""), max_bid)
            return False

        self._post_home(data)
        if _confirmed():
            logger.info("Modified snipe: item=%s, new_max_bid=%s", item_id, max_bid)
            return True

        # Silent drop: Gixen returned 200 with no error banner but the new bid
        # never went live. Back off and retry the POST once before giving up.
        logger.warning(
            "modify_snipe for item=%s not confirmed; retrying after %.1fs",
            item_id, self._add_retry_backoff,
        )
        if self._add_retry_backoff:
            time.sleep(self._add_retry_backoff)
        self._post_home(data)
        if _confirmed():
            logger.info("Modified snipe on retry: item=%s, new_max_bid=%s", item_id, max_bid)
            return True

        raise GixenModifyNotConfirmedError(item_id, max_bid)

    def remove_snipe(self, item_id: str, dbidid: Optional[str] = None) -> bool:
        """Remove a snipe.

        BUI-116: when ``dbidid`` is supplied, the pre-POST list_snipes() lookup
        is skipped. The post-delete verify below still confirms the item is gone,
        so a stale cached dbidid (delete hits a wrong/absent row) surfaces as the
        "still in list" error, which the server turns into a list-based retry.

        Raises:
            GixenSnipeNotFoundError: If item_id is not in the snipe list (only
                when dbidid is not supplied — the lookup path).
        """
        if dbidid is None:
            snipe = self._find_snipe(self.list_snipes(), str(item_id))
            dbidid = snipe["dbidid"]

        data = {
            f"delete_{dbidid}": "Delete",
            "username": self.username,
        }
        # Skip global error check — Gixen may show stale red-font errors for
        # other items on the page even when this delete succeeded.
        self._post_home(data, check_errors=False)

        # Verify the item is actually gone.
        remaining = self.list_snipes()
        still_there = any(s["item_id"] == str(item_id) for s in remaining)
        if still_there:
            raise GixenError(f"Delete POST succeeded but item {item_id} is still in snipe list")

        logger.info("Removed snipe: item=%s", item_id)
        return True

    def purge_completed(self) -> bool:
        """Remove completed/ended snipes from the list."""
        data = {
            "purgecompleted": "1",
            "gixenlinkcontinue": "1",
        }
        self._post_home(data)
        logger.info("Purged completed snipes")
        return True

    # ------------------------------------------------------------------
    # HTML parsing
    # ------------------------------------------------------------------

    def _parse_snipe_table(self, html: str) -> List[Dict[str, str]]:
        """Parse the desktop snipe table from the home page HTML."""
        # Check that the expected form exists
        if '<form name="bids"' not in html and '<form name="addsnipe"' not in html:
            # BUI-114: log what Gixen actually returned instead of the snipe
            # table — this is the single most common failure mode (anti-bot
            # interstitial, login page, error body) and was previously opaque.
            logger.warning(
                "snipe table not found in response; body snippet: %s",
                _response_snippet(html, getattr(self, "username", None)),
            )
            raise GixenSnipeTableMissingError(
                "Could not find snipe table in response. "
                "Gixen may be down or the page structure has changed."
            )

        snipes: List[Dict[str, str]] = []

        # Each snipe in the desktop table has hidden inputs with names like
        # edititemid_<ITEMID>, editmaxbid_<ITEMID>, etc., plus a
        # delete_<DBIDID> button and a checkbox dbidid_<DBIDID>.
        #
        # We extract snipes by finding all edititemid_* hidden inputs,
        # then gathering related fields for each.

        # Find all item IDs from edit hidden inputs
        edit_items = re.findall(
            r'<input name="edititemid_(\d+)" type="hidden" '
            r'id="edititemid" value="(\d+)"',
            html,
        )

        for suffix, item_id in edit_items:
            snipe: Dict[str, str] = {"item_id": item_id}

            # Max bid
            m = re.search(
                rf'name="editmaxbid_{re.escape(suffix)}" type="hidden" '
                rf'id="editmaxbid" value="([^"]*)"',
                html,
            )
            snipe["max_bid"] = m.group(1) if m else ""

            # Bid offset
            m = re.search(
                rf'name="editbidoffset_{re.escape(suffix)}" type="hidden" '
                rf'id="editbidoffset" value="([^"]*)"',
                html,
            )
            snipe["bid_offset"] = m.group(1) if m else "6"

            # Bid offset mirror
            m = re.search(
                rf'name="editbidoffsetmirror_{re.escape(suffix)}" type="hidden" '
                rf'id="editbidoffsetmirror" value="([^"]*)"',
                html,
            )
            snipe["bid_offset_mirror"] = m.group(1) if m else "6"

            # Snipe group
            m = re.search(
                rf'name="editsnipegroup_{re.escape(suffix)}" type="hidden" '
                rf'id="editsnipegroup" value="([^"]*)"',
                html,
            )
            snipe["snipe_group"] = m.group(1) if m else "0"

            # Comment
            m = re.search(
                rf'name="editcomment_{re.escape(suffix)}" type="hidden" '
                rf'id="editcomment" value="([^"]*)"',
                html,
            )
            snipe["comment"] = m.group(1) if m else ""

            # DBIDID — from the delete button near this item
            m = re.search(
                rf'name="delete_(\d+)" type="submit" value="Delete"',
                html[html.index(f'edititemid_{suffix}'):],
            )
            snipe["dbidid"] = m.group(1) if m else ""

            # Validate required fields
            if not snipe["item_id"].isdigit():
                raise GixenParseError(f"Non-numeric item ID: {snipe['item_id']}")
            if snipe["max_bid"]:
                try:
                    Decimal(snipe["max_bid"])
                except InvalidOperation:
                    raise GixenParseError(f"Non-numeric max bid: {snipe['max_bid']}")

            snipes.append(snipe)

        # Now enrich with data from the table rows (title, current bid, status, etc.)
        # These appear in the table cells around each item
        for snipe in snipes:
            iid = snipe["item_id"]

            # Title — appears after the item link, before </td> or <i>
            # Pattern: item link followed by title text
            m = re.search(
                rf'>{re.escape(iid)}</a></td>\s*<td colspan="4">(.*?)(?:<i>|\s*<table)',
                html, re.DOTALL,
            )
            if m:
                snipe["title"] = m.group(1).strip()
            else:
                snipe["title"] = ""

            # Seller — appears in <a> tag linking to ebay.com/usr/
            m = re.search(
                rf'{re.escape(iid)}.*?ebay\.com/usr/([^/"]+)',
                html, re.DOTALL,
            )
            snipe["seller"] = m.group(1) if m else ""

            # Current bid — "X.XX USD" pattern after max bid display
            # In the desktop table: <td>X.XX</td>\n<td>Y.YY USD
            m = re.search(
                rf'name="edit_{re.escape(iid)}".*?</tr>\s*<tr[^>]*>\s*<td></td>\s*'
                rf'<td>([^<]*)</td>\s*<td>([^<]*)</td>\s*<td>([\d.]+ \w+)',
                html, re.DOTALL,
            )
            if m:
                snipe["time_to_end"] = m.group(1).strip()
                # m.group(2) is the max bid display (redundant)
                snipe["current_bid"] = m.group(3).strip()
            else:
                snipe["time_to_end"] = ""
                snipe["current_bid"] = ""

            # Status (main) and Status (mirror)
            # Gixen renders: <td>Status (main): </td><td>SCHEDULED</td>
            # Find the item's anchor tag, then scan the next ~900 chars for
            # both rows. (Took main's structural extraction over our keyword
            # whitelist — both fix the original "175" bug, but main's reads
            # the labelled cell directly which is more durable.)
            m_anchor = re.search(rf'<a[^>]*>{re.escape(iid)}</a>', html)
            anchor_pos = m_anchor.start() if m_anchor else html.find(iid)
            chunk = html[anchor_pos:anchor_pos + 900] if anchor_pos >= 0 else ""
            m = re.search(r'Status \(main\):\s*</td><td>([^<]+)', chunk)
            snipe["status"] = m.group(1).strip() if m else ""
            m = re.search(r'Status \(mirror\):\s*</td><td>([^<]+)', chunk)
            snipe["status_mirror"] = m.group(1).strip() if m else ""
            if not snipe["status"]:
                # Status row absent — Gixen may have changed its desktop-table
                # layout. Loud signal so we can diagnose before phantom-PENDING
                # rows accumulate.
                logger.warning(
                    "_parse_snipe_table: no Status (main) row near edititemid_%s",
                    iid,
                )

        # Deduplicate — the mobile table has separate forms too,
        # but we only parsed desktop table inputs (edititemid_<ID> pattern)
        seen = set()
        unique_snipes = []
        for s in snipes:
            if s["item_id"] not in seen:
                seen.add(s["item_id"])
                unique_snipes.append(s)

        return unique_snipes

    @staticmethod
    def _find_snipe(snipes: List[Dict[str, str]], item_id: str) -> Dict[str, str]:
        """Find a snipe by item_id in the list."""
        for snipe in snipes:
            if snipe["item_id"] == item_id:
                return snipe
        raise GixenSnipeNotFoundError(
            f"Item {item_id} not found in your Gixen snipe list"
        )


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def find_sibling_cleanup_targets(
    snipes: List[Dict[str, str]],
) -> List[Dict[str, str]]:
    """Return snipes that should be removed because a sibling in their snipe
    group has already won.

    A "sibling" is any snipe sharing a non-zero ``snipe_group`` value with a
    snipe whose ``status`` is ``"WON"``. The winning snipe(s) themselves are
    never returned. Group ``"0"`` (no group) is ignored entirely.

    Pure function — no I/O. Input order is preserved in the result.
    """
    won_groups = {
        s.get("snipe_group", "0")
        for s in snipes
        if s.get("status") == "WON" and s.get("snipe_group", "0") != "0"
    }
    return [
        s
        for s in snipes
        if s.get("snipe_group", "0") in won_groups and s.get("status") != "WON"
    ]
