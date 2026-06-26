"""Local eBay bid placer — direct HTTP, no browser automation at bid time.

Flow per bid:
  1. GET the bid review page (offer.ebay.com) with saved session cookies
  2. Parse hidden form fields (including the per-request stok CSRF token)
  3. POST the bid form — same bytes a real browser would send

setup_session() is the only place Playwright is used: one-time login that
saves cookies to COOKIES_FILE. Everything else is plain httpx.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path
from typing import Any

import httpx

from server.db import resolve_server_dir

logger = logging.getLogger(__name__)

# BUI-220: these live in the comics-server data dir (only the directory path is
# the comics server — the bidding itself is genuinely Gixen). resolve_server_dir
# tracks ~/.comics-server with a ~/.gixen-server fallback, so the path stays
# correct after the physical migration.
COOKIES_FILE = resolve_server_dir() / "ebay_cookies.json"

_BID_REVIEW_URL = "https://offer.ebay.com/ws/eBayISAPI.dll?MakeBid&item={item_id}&maxbid={max_bid:.2f}"
_BID_POST_URL   = "https://offer.ebay.com/ws/eBayISAPI.dll?MakeBid"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
}

_SUCCESS_SIGNALS = [
    "you are the current high bidder",
    "you're the current high bidder",
    "bid confirmed",
    "bid placed",
]
_OUTBID_SIGNALS = [
    "you've been outbid",
    "you have been outbid",
    "a higher maximum bid",
]


def _load_cookies() -> dict[str, str]:
    """Load eBay cookies from Playwright storage state, filtered to eBay domains."""
    data = json.loads(COOKIES_FILE.read_text())
    return {
        c["name"]: c["value"]
        for c in data.get("cookies", [])
        if "ebay.com" in c.get("domain", "")
    }


def _parse_hidden_fields(html: str) -> dict[str, str]:
    """Extract all hidden input fields from the bid form."""
    fields: dict[str, str] = {}
    for tag in re.finditer(r'<input[^>]+>', html, re.IGNORECASE):
        t = tag.group(0)
        if 'type="hidden"' not in t.lower() and "type='hidden'" not in t.lower():
            continue
        name  = re.search(r'name=["\']([^"\']+)["\']', t)
        value = re.search(r'value=["\']([^"\']*)["\']', t)
        if name:
            fields[name.group(1)] = value.group(1) if value else ""
    return fields


async def place_bid(item_id: str, max_bid: float, dry_run: bool = False) -> dict[str, Any]:
    """Place one eBay bid via direct HTTP. Safe to call concurrently."""
    result: dict[str, Any] = {"success": False, "message": "", "dry_run": dry_run, "item_id": item_id}

    if not COOKIES_FILE.exists():
        result["message"] = "No eBay session — run 'cli.py ebay-auth' first"
        return result

    cookies = _load_cookies()
    if not cookies:
        result["message"] = "Cookie file exists but contains no eBay cookies — re-run 'cli.py ebay-auth'"
        return result

    review_url = _BID_REVIEW_URL.format(item_id=item_id, max_bid=max_bid)

    async with httpx.AsyncClient(
        cookies=cookies,
        headers=_HEADERS,
        follow_redirects=True,
        timeout=20.0,
    ) as client:
        # ── Step 1: GET the bid review page ──────────────────────────────
        try:
            resp = await client.get(
                review_url,
                headers={"Referer": f"https://www.ebay.com/itm/{item_id}"},
            )
        except httpx.RequestError as exc:
            result["message"] = f"Network error fetching bid page: {exc}"
            return result

        if resp.status_code >= 400:
            result["message"] = f"HTTP {resp.status_code} on bid review page"
            return result

        if "signin" in str(resp.url).lower():
            result["message"] = "Session expired — run 'cli.py ebay-auth' to refresh"
            return result

        html = resp.text
        page_text = html.lower()

        if "listing has ended" in page_text or "item not found" in page_text:
            result["message"] = "Listing has ended or item not found"
            return result

        if dry_run:
            has_form = "stok" in html or "maxbid" in page_text
            if has_form:
                result["success"] = True
                result["message"] = f"Dry run OK — bid review page loaded for item {item_id}"
            else:
                snippet = page_text[:250].replace("\n", " ")
                result["message"] = f"Dry run — unexpected page content: {snippet}"
            return result

        # ── Step 2: Parse hidden fields (includes stok CSRF token) ───────
        hidden = _parse_hidden_fields(html)

        if "stok" not in hidden:
            snippet = page_text[:300].replace("\n", " ")
            result["message"] = f"stok token not found — page may have changed: {snippet}"
            return result

        # ── Step 3: POST the bid ──────────────────────────────────────────
        post_data = {
            **hidden,
            "maxbid": f"{max_bid:.2f}",
            "quant": "1",
            "mode": "1",
            "MakeBidPop": "1",
            "UsingCSS": "0",
        }

        try:
            post_resp = await client.post(
                _BID_POST_URL,
                data=post_data,
                headers={
                    "Referer": review_url,
                    "Origin": "https://offer.ebay.com",
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Sec-Fetch-Site": "same-origin",
                },
            )
        except httpx.RequestError as exc:
            result["message"] = f"Network error posting bid: {exc}"
            return result

        final_text = post_resp.text.lower()

        if any(s in final_text for s in _SUCCESS_SIGNALS):
            result["success"] = True
            result["message"] = "Bid placed — you are the high bidder"
        elif any(s in final_text for s in _OUTBID_SIGNALS):
            result["success"] = True  # bid was accepted by eBay, just outbid
            result["message"] = "Bid placed but outbid immediately (max bid too low)"
        elif "enter" in final_text and "or more" in final_text:
            m = re.search(r'enter \$([\d,.]+) or more', final_text)
            min_bid = f"${m.group(1)}" if m else "more"
            result["message"] = f"Bid rejected — minimum is {min_bid}"
        elif "signin" in str(post_resp.url).lower():
            result["message"] = "Session expired mid-bid — run 'cli.py ebay-auth' to refresh"
        else:
            snippet = final_text[:300].replace("\n", " ")
            result["message"] = f"Unknown result — page snippet: {snippet}"

    logger.info("place_bid %s max=%.2f → %s", item_id, max_bid, result["message"])
    return result


async def place_bids_concurrent(bids: list[dict]) -> list[dict]:
    """Fire multiple bids in parallel. bids = [{"item_id": ..., "max_bid": ...}]."""
    if not bids:
        return []
    return await asyncio.gather(*[
        place_bid(b["item_id"], b["max_bid"]) for b in bids
    ])


# ---------------------------------------------------------------------------
# EbayBidder class — used by the server (thin wrapper, no browser process)
# ---------------------------------------------------------------------------

class EbayBidder:
    """Server-lifetime bid placer. start()/stop() are no-ops (no browser to manage)."""

    async def start(self) -> None:
        logger.info("EbayBidder: ready (HTTP mode)")

    async def stop(self) -> None:
        logger.info("EbayBidder: stopped")

    async def place_bid(self, item_id: str, max_bid: float, dry_run: bool = False) -> dict:
        return await place_bid(item_id, max_bid, dry_run=dry_run)

    async def place_bids_concurrent(self, bids: list[dict]) -> list[dict]:
        return await place_bids_concurrent(bids)


# ---------------------------------------------------------------------------
# One-time session setup (the only place Playwright is used)
# ---------------------------------------------------------------------------

async def setup_session() -> None:
    """Open a visible browser for the user to log in to eBay.
    Saves cookies to COOKIES_FILE for use by place_bid()."""
    from playwright.async_api import async_playwright

    CONTEXT_PATH = resolve_server_dir() / "ebay_browser"
    CONTEXT_PATH.mkdir(parents=True, exist_ok=True)

    pw = await async_playwright().start()
    context = await pw.chromium.launch_persistent_context(
        str(CONTEXT_PATH),
        headless=False,
        args=["--disable-blink-features=AutomationControlled"],
    )
    page = await context.new_page()
    await page.goto("https://www.ebay.com/signin/")
    print("\nLog in to eBay in the browser window, then press Enter here to save the session.")
    await asyncio.get_event_loop().run_in_executor(None, input)

    storage = await context.storage_state()
    COOKIES_FILE.parent.mkdir(parents=True, exist_ok=True)
    COOKIES_FILE.write_text(json.dumps(storage))
    COOKIES_FILE.chmod(0o600)

    await context.close()
    await pw.stop()
    print(f"Session saved → {COOKIES_FILE}")
