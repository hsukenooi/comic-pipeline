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
            raise requests.ReadTimeout(f"curl timed out for {url}") from None

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
    """Base exception for Gixen client errors.

    BUI-558: redacts any live Gixen ``sessionid`` out of the message at the
    source. The session id rides in the request URL (``_home_url``), so it
    can land in any subclass's message that embeds a URL or a wrapped
    lower-level exception — today that's ``GixenConnectionError`` via
    ``_connection_error``, but nothing stops a future subclass from doing the
    same. Redacting here, once, means every current and future ``str(e)``
    sink — an HTTP 503 ``detail``, a ``logger.warning("%s", e)`` call — is
    safe by construction; no call site has to remember to apply the regex
    itself. Mirrors the redaction ``gixen_client._response_snippet`` already
    applies to logged response bodies.

    Overriding only ``__str__`` (not ``args``/``repr``) keeps exception
    chaining, pickling, and ``repr()`` untouched — this changes what the
    message renders as, not what the exception carries.
    """

    def __str__(self) -> str:
        return re.sub(r"sessionid=\d+", "sessionid=REDACTED", super().__str__())


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


def parse_listed_max_bid(value: str | int | float | None) -> float | None:
    """Parse a scraped snipe's max_bid to a POSITIVE float, or None when the
    value is absent, blank, unparseable, or non-positive (BUI-555).

    "None means unknown" — the same contract as server.fallback's
    _parse_snipe_group, and for a sharper reason: the consumer
    (server.db.mirror_gixen_max_bid) writes bids.max_bid, which _sniper_loop
    fires real money from. Coercing a scrape quirk to 0 — or to any fallback
    number — would durably rewrite the user's cap off a parse failure. A
    blank/garbage cell means "we did not read a cap this cycle", so the DB
    keeps whatever it already had.

    Lives here, beside GixenClient._max_bid_matches, because the two must agree
    on what counts as the same number: the same character strip tolerates the
    currency suffix ("25.00 USD") and thousands separators Gixen renders.
    """
    if value is None:
        return None
    cleaned = re.sub(r"[^0-9.\-]", "", str(value).strip())
    if cleaned in ("", ".", "-"):
        return None
    try:
        parsed = Decimal(cleaned)
    except InvalidOperation:
        return None
    # <= 0 is never a real cap — Gixen has no zero-bid snipe — so this is a
    # parse artifact (e.g. a stray "-" surviving the strip), not a value to
    # write. Zeroing a live row's cap would silently disarm the local sniper.
    if parsed <= 0:
        return None
    return float(parsed)


def parse_listed_snipe_group(value: str | int | None) -> int | None:
    """Parse a Gixen-reported snipe_group to an int, or None when the value
    is absent, blank, or unparseable (a scrape quirk, or the BUI-383 regex-
    miss case). "None means unknown" — the same contract as
    `parse_listed_max_bid` above and `server.fallback._parse_snipe_group` —
    never coerce a miss to 0: group 0 is a positive "no group" claim, and a
    caller (BUI-709's write confirms below) that collapsed "unknown" to 0
    would falsely report a mismatch for every parser-drift case, or worse,
    falsely confirm a real un-group that never actually landed.

    Duplicated in spirit (not import) from `server.fallback._parse_snipe_group`:
    this module carries no dependency on `server/`, the same reasoning
    `parse_listed_max_bid` already documents for `_max_bid_matches`.
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Terminal-status vocabulary (BUI-595)
# ---------------------------------------------------------------------------

# Gixen reports many ended-auction states beyond the 4 "primary" statuses.
# Map every raw Gixen status observed in production (as scraped by
# list_snipes() — upper-cased, stripped) to the internal terminal status
# {WON, LOST, FAILED, ENDED}.
#
# OUTBID and BID UNDER ASKING PRICE are both losses: in OUTBID Gixen placed
# our bid but eBay's proxy revealed a higher standing max; in BID UNDER ASKING
# PRICE the current price already exceeded our max at snipe time so Gixen
# skipped the submission. Different mechanics, same outcome — we lost, and
# current_bid is the price that beat us.
#
# This lives here (not server/main.py, where it originated) because
# gixen_client.py owns the scrape/parse layer and carries no FastAPI
# dependency — both server/main.py (the live WON/LOST/FAILED/ENDED
# classification path) and cli.py's direct-mode `purge` command (a cosmetic
# dry-run count of completed snipes) already import this module, so both can
# share one copy of the vocabulary instead of each re-declaring it (BUI-595:
# cli.py's purge count had drifted to a 4-status tuple missing OUTBID/BID
# UNDER ASKING PRICE). server/main.py re-exports this under its original
# name (`_GIXEN_TERMINAL_MAP`) for backward compatibility.
GIXEN_TERMINAL_MAP: dict[str, str] = {
    "WON": "WON",
    "LOST": "LOST",
    "OUTBID": "LOST",
    "BID UNDER ASKING PRICE": "LOST",
    "FAILED": "FAILED",
    "ENDED": "ENDED",
}

# Raw Gixen status strings (as scraped by list_snipes(), before any mapping)
# that represent a completed/terminal snipe — the key set of
# GIXEN_TERMINAL_MAP. Consumers that only need "is this snipe done?" over the
# raw scraped statuses (e.g. cli.py's purge dry-run count) should use this
# rather than the internal 4-status set, since list_snipes() never maps
# statuses itself.
GIXEN_RAW_TERMINAL_STATUSES: frozenset[str] = frozenset(GIXEN_TERMINAL_MAP)


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

def _default_client_timeout() -> float:
    """Resolve GixenClient's default timeout (BUI-699).

    A *good-case* fetch of home_2.php (the snipe table GET/POST target used
    by login/_get_home_page/_post_home below) measured 12.95s for 399,838
    bytes / 91 snipe rows in a 2026-08-07 probe, and the page only grows as
    more snipes are added — the old 15.0s default left ~2s of margin on a
    real-money write path. 60s gives real headroom over that measured good
    case. 322/328 all-time curl-28 (operation timeout) failures were on this
    same endpoint, zero on login across 1,358 logins, so home_2.php is
    specifically where the margin matters. This does not rescue a genuinely
    stalled transfer (one probe still failed mid-body at 60s) — that class is
    BUI-697's reconcile job, not a timeout value. Override with
    GIXEN_CLIENT_TIMEOUT (seconds) for local tuning without a code change.
    """
    return float(os.getenv("GIXEN_CLIENT_TIMEOUT", "60.0"))


# Read once at import time (module-level default args are bound at def-time,
# same pattern as server/main.py's SYNC_INTERVAL) rather than per instance.
_DEFAULT_TIMEOUT = _default_client_timeout()

_LOGIN_COOLDOWN = 300  # seconds to wait after a failed login before retrying
# BUI-118: a *transient* login failure (connectivity blip — ConnectionError /
# Timeout / empty body, the BUI-77 classification) gets a short cooldown for the
# first few consecutive occurrences, so one momentary blip mid-edit does not lock
# all writes out for the full 300s. Only once transient failures pile up
# (sustained outage) does the full _LOGIN_COOLDOWN arm. A genuine credentials
# rejection (a real login page with no session id) still arms the full cooldown
# on the first failure — that is the IP-rate-limit / real-auth case the cooldown
# exists to protect.
_TRANSIENT_LOGIN_COOLDOWN = 5  # seconds for an isolated connectivity blip
_TRANSIENT_FAILURE_THRESHOLD = 3  # consecutive transient failures → full cooldown


class GixenClient:
    """Web-scraping client for Gixen.com."""

    # Minimum seconds between Gixen write POSTs — prevents silent drops during bursts.
    _min_post_gap: float = 1.5
    # Backoff before retrying an add_snipe that wasn't confirmed by list_snipes.
    _add_retry_backoff: float = 5.0
    # BUI-117: within this window, a second+ login() reuses the last good session
    # instead of re-authenticating. Sized to span one edit's lifetime (which
    # includes the 5s _add_retry_backoff + 1.5s post gaps) so an edit's burst of
    # recovery re-logins collapses to a single real login.
    _login_throttle: float = 12.0
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
        timeout: float = _DEFAULT_TIMEOUT,
    ):
        self.username = username or os.getenv("GIXEN_USERNAME", "")
        self.password = password or os.getenv("GIXEN_PASSWORD", "")
        self.timeout = timeout
        self.session = _CurlSession()
        self.session_id: Optional[str] = None
        self._login_failed_at: Optional[float] = None  # monotonic timestamp
        # BUI-118: how long the most recent failure armed the cooldown for. A
        # transient blip arms only _TRANSIENT_LOGIN_COOLDOWN until enough
        # consecutive transient failures accumulate; a credentials rejection
        # arms the full _LOGIN_COOLDOWN immediately.
        self._login_cooldown_secs: float = _LOGIN_COOLDOWN
        # BUI-118: consecutive *transient* login failures since the last success.
        # Reset to 0 on any successful login.
        self._transient_login_failures: int = 0
        # BUI-117: instance-level successful-login throttle. A single edit's
        # recovery path can clear session_id and call login() up to ~6 times
        # during a Gixen flap (3 list_snipes × GET-500 + table-missing re-logins).
        # These collapse to one real login if a successful login is recent enough.
        # Instance-level (not class-level) on purpose: each client has its own
        # _CurlSession/cookie jar, so a session id is not portable across the
        # _api_client/_sync_client pair even though they share a username.
        self._last_login_at: Optional[float] = None  # monotonic, set on success
        self._last_session_id: Optional[str] = None  # last good session id

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

    def _arm_login_cooldown(self, *, transient: bool) -> None:
        """Record a failed login and arm the appropriate cooldown (BUI-118).

        A *transient* failure (connectivity blip — ConnectionError/Timeout or an
        empty body, the BUI-77 classification) only arms the short
        ``_TRANSIENT_LOGIN_COOLDOWN`` until ``_TRANSIENT_FAILURE_THRESHOLD``
        consecutive transient failures have piled up, at which point a sustained
        outage is assumed and the full ``_LOGIN_COOLDOWN`` arms. A
        non-transient failure (genuine credentials rejection) arms the full
        cooldown immediately — that is the real-auth / IP-rate-limit case the
        cooldown exists to protect against.
        """
        self._login_failed_at = time.monotonic()
        if transient:
            self._transient_login_failures += 1
            if self._transient_login_failures >= _TRANSIENT_FAILURE_THRESHOLD:
                self._login_cooldown_secs = _LOGIN_COOLDOWN
            else:
                self._login_cooldown_secs = _TRANSIENT_LOGIN_COOLDOWN
        else:
            # A real credentials rejection is not a blip — back off fully and
            # immediately. Reset the transient counter: this failure is a
            # different class of problem.
            self._transient_login_failures = 0
            self._login_cooldown_secs = _LOGIN_COOLDOWN

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
            remaining = self._login_cooldown_secs - elapsed
            if remaining > 0:
                raise GixenLoginError(
                    f"Login cooldown active — retry in {int(remaining)}s. "
                    "Backing off to avoid IP rate-limiting."
                )

        # BUI-117: collapse a burst of recovery re-logins within one edit to a
        # single real login. This only fires on the 2nd+ login() inside the
        # window — the first one always authenticates for real, so a legitimate
        # single stale-session recovery still does its one login and heals (the
        # recovery callers null session_id first, so we restore the last good id
        # here). During a *persistent* flap the reused session is already dead;
        # the bounded retry then 500s and surfaces 503 — the edit fails after one
        # login instead of six, which is the intended bound, not a regression.
        if (
            self._last_login_at is not None
            and self._last_session_id is not None
            and time.monotonic() - self._last_login_at < self._login_throttle
        ):
            logger.info("Login throttle active — reusing recent session")
            self.session_id = self._last_session_id
            return self.session_id

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
            # Classify as connectivity, not credentials (BUI-77). Transient:
            # one blip must not lock writes for the full cooldown (BUI-118).
            self._arm_login_cooldown(transient=True)
            raise self._connection_error(LOGIN_URL, e) from e
        except Exception:
            # An unexpected error mid-login is treated as transient too — it is
            # not a confirmed credentials rejection, so it should not arm the
            # full cooldown on the first occurrence (BUI-118).
            self._arm_login_cooldown(transient=True)
            raise

        # Gixen returns HTML with a meta-refresh containing the sessionid
        match = re.search(r'sessionid=(\d+)', resp.text)
        if not match:
            # An empty/blank body with no sessionid is the signature of a
            # truncated connection or a flapping host, not an auth rejection —
            # a real rejection returns the login HTML (form + error). Only the
            # latter is a credentials problem (BUI-77), and only it arms the
            # full cooldown on the first failure (BUI-118).
            if not (resp.text or "").strip():
                self._arm_login_cooldown(transient=True)
                raise GixenConnectionError(
                    f"Gixen returned an empty response from {LOGIN_URL} — the "
                    "host is likely unreachable or flapping. This is a "
                    "connectivity problem, not a credentials problem."
                )
            self._arm_login_cooldown(transient=False)
            raise GixenLoginError(
                "Login failed — Gixen returned a page with no session ID. "
                "If credentials are correct, Gixen's login page may have "
                "changed. Check your GIXEN_USERNAME and GIXEN_PASSWORD."
            )

        self._login_failed_at = None
        # BUI-118: a clean login clears the transient-failure streak and resets
        # the armed cooldown duration back to the full default for the next
        # genuine failure.
        self._transient_login_failures = 0
        self._login_cooldown_secs = _LOGIN_COOLDOWN
        self.session_id = match.group(1)
        # BUI-117: remember this good login so a same-edit burst of recovery
        # re-logins within _login_throttle reuses it instead of re-authenticating.
        self._last_login_at = time.monotonic()
        self._last_session_id = self.session_id
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

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def list_snipes(self) -> List[Dict[str, Optional[str]]]:
        """Fetch and parse the current snipe list.

        Returns:
            List of dicts with keys: item_id, title, max_bid, current_bid,
            status, status_mirror, time_to_end, bid_offset, bid_offset_mirror,
            dbidid, snipe_group, seller.

            snipe_group is the scraped string ("0" = genuinely ungrouped) or
            None when the field couldn't be parsed at all — "unknown", which
            consumers must never coerce to 0 (BUI-383; see _parse_snipe_table).
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

    @staticmethod
    def _group_matches(listed: str | int | None, expected: int) -> bool | None:
        """Compare a listed snipe_group against the sent value.

        BUI-709 (EM-decided policy, following BUI-383's group semantics):
        returns True on a match, False when the listed value parses to a
        DIFFERENT int than `expected`, and None when the listed value is
        unparseable/missing — a parser-drift regex miss, not evidence of a
        mismatch. Callers treat None as "confirm on cap/presence alone" +
        `logger.warning`: failing closed on parser drift would take down
        every grouped write, and warn-and-pass only degrades verification
        back to the pre-BUI-709 status quo rather than blocking real money.
        """
        parsed = parse_listed_snipe_group(listed)
        if parsed is None:
            return None
        return parsed == expected

    def _verify_present(self, target: str, snipe_group: int = 0) -> bool:
        """True if `target` (an item_id) appears in the current snipe list.

        BUI-709: when this add carried a group (`snipe_group` nonzero),
        presence alone does not prove the write landed correctly — the
        listed group must also match the sent one (same mismatch/unknown
        policy as `modify_snipe`'s `_confirmed`, via `_group_matches`). A
        `snipe_group=0` add has nothing extra to verify: 0 here means "no
        group was requested" for a fresh create, not a deliberate un-group
        claim to confirm (contrast `modify_snipe`, where an explicit 0 IS a
        deliberate write and is always checked).

        Raises whatever list_snipes() raises (GixenParseError,
        requests.HTTPError, GixenSessionExpiredError) — callers decide how to
        log and chain the resulting GixenAddNotConfirmedError.
        """
        for s in self.list_snipes():
            if s["item_id"] != target:
                continue
            if not snipe_group:
                return True
            group_match = self._group_matches(s.get("snipe_group"), snipe_group)
            if group_match is None:
                logger.warning(
                    "add_snipe verify for item=%s: listed snipe_group "
                    "unparseable/missing (%r) for sent group=%s; confirming "
                    "presence only (BUI-709 parser-drift carve-out)",
                    target, s.get("snipe_group"), snipe_group,
                )
                return True
            return group_match
        return False

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
                BUI-709: also raised when the add carried a group
                (snipe_group != 0) and the item is present but listed under a
                DIFFERENT group than sent — a wrong-group add is not a
                confirmed add.
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
            present = self._verify_present(target, snipe_group)
        except (GixenParseError, requests.HTTPError, GixenSessionExpiredError) as e:
            logger.warning(
                "add_snipe for item=%s: verify list_snipes failed (%s); "
                "refusing to double-POST",
                item_id, e,
            )
            raise GixenAddNotConfirmedError(item_id) from e

        if present:
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
                # BUI-709: the 202-retry arm — a wrong-group add would sneak
                # through exactly here if this verify only checked presence
                # (the retry POST hitting ITEM ALREADY PRESENT proves *an*
                # item is there, not that it carries the group we sent).
                try:
                    present = self._verify_present(target, snipe_group)
                except (GixenParseError, requests.HTTPError, GixenSessionExpiredError):
                    raise GixenAddNotConfirmedError(item_id) from e
                if present:
                    logger.info(
                        "add_snipe for item=%s: first POST landed, retry hit "
                        "202; treating as success", item_id,
                    )
                    return True
                # 202 but verify still doesn't see it (or the group doesn't
                # match) → genuinely confused.
                raise GixenAddNotConfirmedError(item_id) from e
            raise

        try:
            present = self._verify_present(target, snipe_group)
        except (GixenParseError, requests.HTTPError, GixenSessionExpiredError) as e:
            raise GixenAddNotConfirmedError(item_id) from e

        if present:
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

        BUI-709: the confirm also compares the LISTED snipe_group against the
        sent one — unconditionally, unlike add_snipe's `_verify_present`
        (which only checks group when the add carried a nonzero one). Every
        modify's `snipe_group` is already a deliberate, resolved value by
        the time it reaches this client (the server resolves any None-
        passthrough intent before calling here — see AddBidRequest/
        EditBidRequest's snipe_group docs in server/main.py), so an explicit
        0 here IS a deliberate un-group write and must be confirmed like any
        other value, not skipped the way a fresh create's incidental 0 is.
        A DIFFERENT listed group is NOT confirmed (EM-decided policy); an
        unparseable/missing listed group (BUI-383 parser drift) confirms on
        the cap alone plus a warning — failing closed there would take down
        every grouped modify during ordinary scrape noise.

        Raises:
            GixenSnipeNotFoundError: If item_id is not in the snipe list (only
                when dbidid is not supplied — the lookup path).
            GixenModifyNotConfirmedError: If the modify POST returned no error but
                the new max_bid never appeared in the list, or (BUI-709) appeared
                at the right cap but under a different listed group, even after
                one retry.
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
                if s["item_id"] != target:
                    continue
                if not self._max_bid_matches(s.get("max_bid", ""), max_bid):
                    return False
                # BUI-709: cap matched — now confirm the group actually
                # landed too. A True return here used to mean only "the cap
                # is live"; a caller reading it as "the group landed too"
                # was exactly the gap this closes.
                group_match = self._group_matches(s.get("snipe_group"), snipe_group)
                if group_match is None:
                    logger.warning(
                        "modify_snipe for item=%s: listed snipe_group "
                        "unparseable/missing (%r) for sent group=%s; "
                        "confirming cap only (BUI-709 parser-drift "
                        "carve-out)",
                        item_id, s.get("snipe_group"), snipe_group,
                    )
                    return True
                return group_match
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

    def _parse_snipe_table(self, html: str) -> List[Dict[str, Optional[str]]]:
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

        # snipe_group may be None on a regex miss (BUI-383); every other
        # value is a str, but the dict is typed loosely to admit it.
        snipes: List[Dict[str, Optional[str]]] = []

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
            snipe: Dict[str, Optional[str]] = {"item_id": item_id}

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

            # Snipe group. A regex MISS is encoded as None ("unknown"), never
            # "0" (BUI-383): "0" is a positive "no group" claim, and the
            # server's per-sync mirror (refresh_snipe_group) trusts it in
            # both directions — a miss collapsed to "0" would durably CLEAR
            # real group membership in the DB (N → 0), weakening the BUI-371
            # group-cancel evidence. The server skips None/blank/unparseable
            # values (server.main._parse_snipe_group), so an unknown here
            # preserves whatever membership the DB already knows.
            m = re.search(
                rf'name="editsnipegroup_{re.escape(suffix)}" type="hidden" '
                rf'id="editsnipegroup" value="([^"]*)"',
                html,
            )
            snipe["snipe_group"] = m.group(1) if m else None

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
                    raise GixenParseError(f"Non-numeric max bid: {snipe['max_bid']}") from None

            snipes.append(snipe)

        # Now enrich with data from the table rows (title, current bid, status, etc.)
        # These appear in the table cells around each item.
        #
        # BUI-580: every scan below must be bounded to the item's OWN markup.
        # Gixen renders each snipe twice — a mobile block (labelled cells:
        # "Group: 1", "Time to end: ...", "Status (main): ...") and a desktop
        # table (positional cells) — and an unbounded `.*?` under DOTALL does
        # not fail when a row's shape is unexpected: it scans on and captures
        # the NEXT snipe's cells instead. That is silent and wrong, which is
        # strictly worse than an empty value. Two spans per item:
        #   mobile  — its first anchor up to the next snipe's first anchor
        #   desktop — its edit submit up to the next row's dbidid checkbox
        # A row whose shape drifts now yields "" (loud) rather than a
        # neighbour's data (silent).
        #
        # anchor_order holds EVERY item anchor on the page, both blocks — not
        # just each item's first. That is what stops the last mobile row's span
        # from running on through the whole desktop table below it.
        mobile_start = {}
        anchor_order = []
        for s in snipes:
            hits = [
                m.start() for m in
                re.finditer(rf'<a[^>]*>{re.escape(s["item_id"])}</a>', html)
            ]
            anchor_order.extend(hits)
            mobile_start[s["item_id"]] = hits[0] if hits else html.find(s["item_id"])
        anchor_order.sort()

        for snipe in snipes:
            iid = snipe["item_id"]

            # This item's mobile block: up to the next item's anchor.
            start = mobile_start[iid]
            nxt = next((p for p in anchor_order if p > start), len(html))
            mobile_span = html[start:nxt] if start >= 0 else ""

            # This item's desktop rows: up to the next row's checkbox.
            m_edit = re.search(rf'name="edit_{re.escape(iid)}"', html)
            desktop_span = ""
            if m_edit:
                rest = html[m_edit.end():]
                m_next_row = re.search(r'name="dbidid_', rest)
                desktop_span = rest[:m_next_row.start()] if m_next_row else rest

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
            m = re.search(r'ebay\.com/usr/([^/"]+)', mobile_span)
            snipe["seller"] = m.group(1) if m else ""

            # Current bid — "X.XX USD" pattern after max bid display
            # In the desktop table: <td>X.XX</td>\n<td>Y.YY USD
            #
            # BUI-580: the leading cell is empty ("<td></td>") only for an
            # UNGROUPED snipe. A grouped one renders "<td align="right">Group:
            # 1</td>", so a literal <td></td> here missed the item's own row
            # and — before the span bound above — ran on into the next snipe's
            # row, reporting its bid and its clock. Live: X-Men #101 (group 1)
            # showed $9.65 / "2d 12h" borrowed from the #127 row beneath it
            # while the real auction sat at $291 with 38 minutes less to run.
            # Both fields are load-bearing (current_bid becomes winning_bid on
            # WON/LOST; time_to_end sets auction_end_at, which the local sniper
            # fires on), so match either shape rather than counting on the cell
            # being empty.
            m = re.search(
                r'</tr>\s*<tr[^>]*>\s*<td[^>]*>[^<]*</td>\s*'
                r'<td>([^<]*)</td>\s*<td>([^<]*)</td>\s*<td>([\d.]+ \w+)',
                desktop_span, re.DOTALL,
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
            # Read the labelled cell directly. (Took main's structural
            # extraction over our keyword whitelist — both fix the original
            # "175" bug, but main's reads the labelled cell directly which is
            # more durable.) BUI-580 replaced the ~900-char scan window with
            # the item's own span: a fixed character count is the same
            # unbounded-scan hazard as the current-bid bug, just spelled with
            # a magic number.
            m = re.search(r'Status \(main\):\s*</td><td>([^<]+)', mobile_span)
            snipe["status"] = m.group(1).strip() if m else ""
            m = re.search(r'Status \(mirror\):\s*</td><td>([^<]+)', mobile_span)
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
    def _find_snipe(
        snipes: List[Dict[str, Optional[str]]], item_id: str
    ) -> Dict[str, Optional[str]]:
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

def _canonical_snipe_group(value: object) -> Optional[str]:
    """Canonical non-zero snipe_group for sibling matching, or None.

    None is returned for every value that is not a positive group claim:
    None itself (a parse miss — "unknown", BUI-383), blank/whitespace, "0"
    (genuinely ungrouped), and anything non-numeric (a scrape quirk like
    "N/A"). Digits are normalized through int() so "01" and "1" match.
    Matching on anything weaker would be dangerous: these targets get
    REMOVED from Gixen, so two snipes sharing an empty-string or unparseable
    group must never be treated as won-group siblings."""
    if value is None:
        return None
    v = str(value).strip()
    if not v.isdigit():
        return None
    n = int(v)
    return str(n) if n else None


def find_sibling_cleanup_targets(
    snipes: List[Dict[str, Optional[str]]],
) -> List[Dict[str, Optional[str]]]:
    """Return snipes that should be removed because a sibling in their snipe
    group has already won.

    A "sibling" is any snipe sharing a non-zero ``snipe_group`` value with a
    snipe whose ``status`` is ``"WON"``. The winning snipe(s) themselves are
    never returned. Group ``"0"`` (no group) is ignored entirely, as are
    unknown/blank/unparseable group values (see _canonical_snipe_group) —
    an empty-string group must not register as a won group (BUI-383).

    Pure function — no I/O. Input order is preserved in the result.
    """
    won_groups = set()
    for s in snipes:
        if s.get("status") == "WON":
            group = _canonical_snipe_group(s.get("snipe_group"))
            if group is not None:
                won_groups.add(group)
    targets = []
    for s in snipes:
        if s.get("status") == "WON":
            continue
        group = _canonical_snipe_group(s.get("snipe_group"))
        if group is not None and group in won_groups:
            targets.append(s)
    return targets
