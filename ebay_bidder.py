"""Local eBay bid placer — Playwright-based, bypasses third-party snipe services.

Architecture:
- EbayBidder keeps one browser process alive for the lifetime of the server.
- setup_session() uses a persistent context (locks the profile dir) only during
  the one-time login flow; afterwards it extracts cookies to a JSON file and
  closes the persistent context.
- place_bid() opens a fresh lightweight context from that JSON file — no profile
  lock, so many bids can run concurrently.
- place_bids_concurrent() fires a batch in parallel via asyncio.gather().
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from playwright.async_api import async_playwright, Browser, Playwright

logger = logging.getLogger(__name__)

CONTEXT_PATH = Path.home() / ".gixen-server" / "ebay_browser"
COOKIES_FILE = Path.home() / ".gixen-server" / "ebay_cookies.json"

# Selectors for eBay's bid confirmation flow (as of 2025).
# eBay occasionally renames these; update if bids stop working.
_CONFIRM_SELECTORS = [
    '[data-testid="ux-call-to-action"]',
    '#bidBtn_btn',
    'button[type="submit"][id*="bid"]',
    'input[type="submit"][id*="bid"]',
]
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


class EbayBidder:
    """Long-lived bid placer. Call start() at server startup, stop() at shutdown."""

    def __init__(self) -> None:
        self._pw: Playwright | None = None
        self._browser: Browser | None = None

    async def start(self) -> None:
        """Launch the shared browser process."""
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        logger.info("EbayBidder: browser started")

    async def stop(self) -> None:
        """Shut down the shared browser process."""
        if self._browser:
            await self._browser.close()
        if self._pw:
            await self._pw.stop()
        logger.info("EbayBidder: browser stopped")

    async def place_bid(self, item_id: str, max_bid: float, dry_run: bool = False) -> dict:
        """Place one eBay bid. Safe to call concurrently for different items."""
        if self._browser is None:
            return {"success": False, "message": "Bidder not started", "dry_run": dry_run}
        if not COOKIES_FILE.exists():
            return {"success": False, "message": "No eBay session — run 'cli.py ebay-auth'", "dry_run": dry_run}

        result: dict[str, Any] = {"success": False, "message": "", "dry_run": dry_run}
        storage_state = json.loads(COOKIES_FILE.read_text())
        context = await self._browser.new_context(storage_state=storage_state)
        try:
            page = await context.new_page()
            url = f"https://offer.ebay.com/ws/eBayISAPI.dll?MakeBid&item={item_id}&maxbid={max_bid:.2f}"
            logger.info("place_bid: item=%s max_bid=%.2f dry_run=%s", item_id, max_bid, dry_run)

            response = await page.goto(url, timeout=30_000, wait_until="domcontentloaded")
            if response and response.status >= 400:
                result["message"] = f"HTTP {response.status} loading bid page"
                return result

            await page.wait_for_load_state("networkidle", timeout=15_000)

            if "signin" in page.url.lower():
                result["message"] = "Session expired — run 'cli.py ebay-auth' to refresh"
                return result

            page_text = (await page.inner_text("body")).lower()

            if "item not found" in page_text or "listing has ended" in page_text:
                result["message"] = "Item not found or listing has ended"
                return result

            if dry_run:
                result["success"] = True
                result["message"] = f"Dry run — bid page loaded OK for item {item_id}"
                return result

            confirm_btn = None
            for selector in _CONFIRM_SELECTORS:
                btn = await page.query_selector(selector)
                if btn and await btn.is_visible():
                    confirm_btn = btn
                    break

            if confirm_btn is None:
                snippet = page_text[:300].replace("\n", " ")
                result["message"] = f"Confirm button not found. Page snippet: {snippet}"
                return result

            await confirm_btn.click()
            await page.wait_for_load_state("networkidle", timeout=15_000)

            final_text = (await page.inner_text("body")).lower()

            if any(s in final_text for s in _SUCCESS_SIGNALS):
                result["success"] = True
                result["message"] = "Bid placed successfully"
            elif any(s in final_text for s in _OUTBID_SIGNALS):
                result["success"] = False
                result["message"] = "Bid placed but immediately outbid (max bid too low)"
            else:
                snippet = final_text[:300].replace("\n", " ")
                result["message"] = f"Bid submitted — unknown result: {snippet}"

        except asyncio.TimeoutError:
            result["message"] = "Timed out waiting for eBay bid page"
        except Exception as exc:
            result["message"] = f"Unexpected error: {exc}"
            logger.exception("place_bid: unhandled error for item %s", item_id)
        finally:
            await context.close()

        logger.info("place_bid result for %s: %s", item_id, result)
        return result

    async def place_bids_concurrent(self, bids: list[dict]) -> list[dict]:
        """Fire multiple bids in parallel. bids = [{"item_id": ..., "max_bid": ...}]."""
        if not bids:
            return []
        tasks = [self.place_bid(b["item_id"], b["max_bid"]) for b in bids]
        return await asyncio.gather(*tasks)


# ---------------------------------------------------------------------------
# Standalone helpers (used by CLI commands, not the server)
# ---------------------------------------------------------------------------

async def setup_session() -> None:
    """Open a visible browser for the user to log in to eBay.
    Saves cookies to COOKIES_FILE for use by place_bid()."""
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

    # Extract cookies and save for concurrent use
    storage = await context.storage_state()
    COOKIES_FILE.parent.mkdir(parents=True, exist_ok=True)
    COOKIES_FILE.write_text(json.dumps(storage))
    COOKIES_FILE.chmod(0o600)

    await context.close()
    await pw.stop()
    print(f"Session saved to {COOKIES_FILE}")


async def place_bid(item_id: str, max_bid: float, dry_run: bool = False) -> dict:
    """Standalone place_bid for CLI use — starts and stops its own browser."""
    bidder = EbayBidder()
    await bidder.start()
    try:
        return await bidder.place_bid(item_id, max_bid, dry_run=dry_run)
    finally:
        await bidder.stop()
