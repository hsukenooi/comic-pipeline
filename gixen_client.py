"""Gixen web client — automates the Gixen.com web interface via HTTP requests.

Gixen's official API (api.php/xmlapi.php) is disabled for some accounts.
This client logs into the web UI and performs operations by submitting the
same HTML forms a browser would.
"""

import os
import re
import subprocess
import logging
import time
from decimal import Decimal, InvalidOperation
from typing import Dict, List, Optional

import requests

logger = logging.getLogger(__name__)


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

    def _run(self, method: str, url: str, data: Optional[Dict] = None,
             timeout: float = 15.0, allow_redirects: bool = True) -> _CurlResponse:
        cmd = ["curl", "-s", "-D", "-", "--max-time", str(int(timeout))]
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


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

_LOGIN_COOLDOWN = 300  # seconds to wait after a failed login before retrying


class GixenClient:
    """Web-scraping client for Gixen.com."""

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

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    def login(self) -> str:
        """Log in to Gixen and return the session ID.

        Raises:
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
        except Exception:
            self._login_failed_at = time.monotonic()
            raise

        # Gixen returns HTML with a meta-refresh containing the sessionid
        match = re.search(r'sessionid=(\d+)', resp.text)
        if not match:
            self._login_failed_at = time.monotonic()
            raise GixenLoginError(
                "Login failed — could not extract session ID. "
                "Check your GIXEN_USERNAME and GIXEN_PASSWORD."
            )

        self._login_failed_at = None
        self.session_id = match.group(1)
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
        return (
            'name="signin"' in html
            and 'name="username"' in html
            and 'sessionid=' not in html
        )

    def _get_home_page(self, retry_on_expired: bool = True) -> str:
        """Fetch the main snipe page. Auto-re-login on session expiration."""
        resp = self.session.get(self._home_url(), timeout=self.timeout)
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
        resp = self.session.post(
            self._home_url(), data=data, timeout=self.timeout
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
            status, time_to_end, bid_offset, bid_offset_mirror, dbidid,
            snipe_group, seller.
        """
        html = self._get_home_page()
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
        """
        data = {
            "newitemid": str(item_id),
            "newmaxbid": str(max_bid),
            "newbidoffset": str(bid_offset),
            "newbidoffsetmirror": str(bid_offset),
            "newsnipegroup": str(snipe_group),
            "username": self.username,
        }
        self._post_home(data)
        logger.info("Added snipe: item=%s, max_bid=%s", item_id, max_bid)
        return True

    def modify_snipe(
        self,
        item_id: str,
        max_bid: Decimal,
        bid_offset: int = 6,
        snipe_group: int = 0,
    ) -> bool:
        """Modify an existing snipe's bid.

        Raises:
            GixenSnipeNotFoundError: If item_id is not in the snipe list.
        """
        snipes = self.list_snipes()
        snipe = self._find_snipe(snipes, str(item_id))

        data = {
            "newitemid": str(item_id),
            "newmaxbid": str(max_bid),
            "newbidoffset": str(bid_offset),
            "newbidoffsetmirror": str(bid_offset),
            "newsnipegroup": str(snipe_group),
            "username": self.username,
            "dbidid": snipe["dbidid"],
            "ismodified": "1",
        }
        self._post_home(data)
        logger.info("Modified snipe: item=%s, new_max_bid=%s", item_id, max_bid)
        return True

    def remove_snipe(self, item_id: str) -> bool:
        """Remove a snipe.

        Raises:
            GixenSnipeNotFoundError: If item_id is not in the snipe list.
        """
        snipes = self.list_snipes()
        snipe = self._find_snipe(snipes, str(item_id))
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
            raise GixenParseError(
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

            # Status
            m = re.search(
                rf'name="edit_{re.escape(iid)}".*?</tr>\s*<tr[^>]*>.*?'
                rf'<td>([\d.]+ \w+[^<]*)</td>\s*<td>(\w+)',
                html, re.DOTALL,
            )
            if m:
                snipe["status"] = m.group(2).strip()
            else:
                # Try simpler pattern
                m = re.search(
                    rf'{re.escape(iid)}.*?(SCHEDULED|WON|LOST|FAILED|ENDED)',
                    html,
                )
                snipe["status"] = m.group(1) if m else ""

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
