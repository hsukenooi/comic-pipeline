"""HTTP client for League of Comic Geeks using Playwright with real Chrome."""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import urlencode

from playwright.sync_api import APIResponse, sync_playwright

from locg.config import cookie_path, playwright_profile_dir

BASE_URL = "https://leagueofcomicgeeks.com"

logger = logging.getLogger("locg")


class AuthRequired(Exception):
    """Raised when a command requires authentication."""
    pass


@dataclass
class _PlaywrightResponse:
    status_code: int
    text: str
    content: bytes
    headers: dict[str, str]

    def json(self) -> Any:
        return json.loads(self.text)


def _wrap_response(api_response: APIResponse) -> _PlaywrightResponse:
    try:
        raw = api_response.body()
        return _PlaywrightResponse(
            status_code=api_response.status,
            text=raw.decode("utf-8", errors="replace"),
            content=raw,
            headers=dict(api_response.headers),
        )
    finally:
        api_response.dispose()


class LOCGClient:
    """HTTP client that uses real Chrome via Playwright to bypass Cloudflare."""

    def __init__(self) -> None:
        self._playwright_instance = sync_playwright().start()
        try:
            profile_dir = str(playwright_profile_dir())
            self._context = self._playwright_instance.chromium.launch_persistent_context(
                user_data_dir=profile_dir,
                channel="chrome",
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-first-run",
                    "--no-default-browser-check",
                ],
            )
            self._page = self._context.new_page()
        except Exception:
            self._playwright_instance.stop()
            raise
        self._server_auth_verified: Optional[bool] = None
        self._cf_warmed_up = False
        # Remove stale cookies.json left by the previous curl_cffi implementation.
        p = cookie_path()
        if p.exists():
            try:
                p.unlink()
                logger.debug("Removed legacy cookies.json (cookies now in Playwright profile)")
            except OSError:
                pass

    def _warm_up_cloudflare(self) -> None:
        """Navigate to homepage so Cloudflare's Turnstile challenge can run and set cf_clearance."""
        if self._cf_warmed_up:
            return
        logger.debug("Warming up Cloudflare clearance via homepage navigation")
        self._page.goto(BASE_URL, wait_until="networkidle", timeout=30000)
        self._cf_warmed_up = True
        logger.debug("Cloudflare warm-up complete")

    @property
    def is_authenticated(self) -> bool:
        cookies = self._context.cookies([BASE_URL])
        return any(c["name"] == "ci_session" for c in cookies)

    def require_auth(self) -> None:
        if not self.is_authenticated:
            if self._try_env_login():
                return
            raise AuthRequired("Not logged in. Run: locg login")
        if self._server_auth_verified is None:
            # verify_session may raise (429, network, malformed response).
            # Do NOT cache the result on failure — let the exception
            # propagate so the next invocation retries.
            self._server_auth_verified = self.verify_session()
        if self._server_auth_verified is False:
            if self._try_env_login():
                return
            raise AuthRequired("Session expired. Run: locg login")

    def _try_env_login(self) -> bool:
        """Attempt auto-login using LOCG_USERNAME/LOCG_PASSWORD from the environment.

        Returns True if login succeeded, False if credentials are missing or
        the login failed. Callers treat False as "fall back to raising AuthRequired".

        Exceptions from login() (rate limits, network errors) are caught and
        logged so callers see a clean AuthRequired rather than a raw traceback
        propagating to exit code 4.
        """
        username = os.environ.get("LOCG_USERNAME")
        password = os.environ.get("LOCG_PASSWORD")
        if not username or not password:
            return False
        logger.debug("Attempting auto-login from LOCG_USERNAME/LOCG_PASSWORD")
        try:
            ok = self.login(username, password)
        except Exception as e:
            logger.warning("Auto-login failed: %s", e)
            return False
        if ok:
            # login() sets _server_auth_verified internally, but make the
            # post-condition explicit so require_auth's early-return paths
            # don't depend on that side effect.
            self._server_auth_verified = True
        else:
            logger.warning(
                "Auto-login rejected by server. "
                "Check LOCG_USERNAME / LOCG_PASSWORD."
            )
        return ok

    def get(self, path: str, params: Optional[dict[str, Any]] = None) -> _PlaywrightResponse:
        url = f"{BASE_URL}{path}"
        if params:
            url = f"{url}?{urlencode(params, doseq=True)}"
        logger.debug(f"GET {url}")
        start = time.monotonic()
        api_resp = self._page.request.get(url, timeout=30000)
        resp = _wrap_response(api_resp)
        elapsed = time.monotonic() - start
        logger.debug(f"  -> {resp.status_code} ({elapsed:.2f}s, {len(resp.content)} bytes)")
        if resp.status_code == 403:
            # Cloudflare clearance either not yet obtained or has expired mid-session.
            # Reset the warm-up flag so _warm_up_cloudflare re-navigates to the homepage,
            # then retry once.  This handles both the first-time case and the case where
            # cf_clearance expires after a long batch of sequential requests.
            self._cf_warmed_up = False
            self._warm_up_cloudflare()
            logger.debug(f"Retrying GET {url} after Cloudflare warm-up")
            start = time.monotonic()
            api_resp = self._page.request.get(url, timeout=30000)
            resp = _wrap_response(api_resp)
            elapsed = time.monotonic() - start
            logger.debug(f"  -> {resp.status_code} ({elapsed:.2f}s, {len(resp.content)} bytes)")
        if resp.status_code == 429:
            retry_after = resp.headers.get("retry-after", "60")
            logger.warning(f"Rate limited on GET {url}, retry after {retry_after}s")
            raise Exception(f"Rate limited. Retry after {retry_after}s")
        return resp

    def post(self, path: str, data: Optional[dict[str, Any]] = None) -> _PlaywrightResponse:
        url = f"{BASE_URL}{path}"
        logger.debug(f"POST {url}")
        if not self._cf_warmed_up:
            self._warm_up_cloudflare()
        start = time.monotonic()
        api_resp = self._page.request.post(url, form=data or {}, timeout=30000)
        resp = _wrap_response(api_resp)
        elapsed = time.monotonic() - start
        logger.debug(f"  -> {resp.status_code} ({elapsed:.2f}s)")
        return resp

    def verify_session(self) -> bool:
        """Confirm session validity from local cookie state. No HTTP call.

        The previous implementation fetched the entire user collection
        (~545 KB) on every authenticated command just to read ``data-user``.
        A second attempt used a small probe URL, but Cloudflare flags
        unusual probe titles, making any explicit GET unreliable.

        Server-side staleness (cookie present but invalidated) surfaces
        downstream: list commands trip ``_check_session_valid``, and any
        command's response that contains ``data-user="0"`` raises
        AuthRequired with the standard "Session expired" message.
        """
        return self.is_authenticated

    def login(self, username: str, password: str) -> bool:
        """Log in and persist the session cookie. Returns True on success."""
        resp = self.post("/login", data={
            "username": username,
            "password": password,
        })
        if not self.is_authenticated:
            logger.debug(f"Login failed: no ci_session cookie (status {resp.status_code})")
            return False

        # Verify the session is actually valid server-side
        if not self.verify_session():
            logger.debug("Login appeared to succeed but session is not valid server-side")
            return False

        self._server_auth_verified = True
        logger.debug("Login successful (verified)")
        return True

    def close(self) -> None:
        try:
            self._context.close()
        finally:
            self._playwright_instance.stop()
