---
title: Cloudflare TLS Fingerprint Mismatch Blocks All Requests (curl_cffi → Playwright)
date: 2026-05-19
category: docs/solutions/integration-issues/
module: locg-cli
problem_type: integration_issue
component: tooling
severity: critical
symptoms:
  - All LOCG CLI commands fail with HTTP 403 on every request
  - cf_clearance cookie is present and not expired but requests are still rejected
  - No change in user credentials or behavior — breakage is external (Cloudflare tightened fingerprint verification)
root_cause: wrong_api
resolution_type: dependency_update
related_components:
  - authentication
tags:
  - cloudflare
  - tls-fingerprint
  - playwright
  - curl-cffi
  - web-scraping
  - cookie-persistence
  - resource-leak
  - ja3-ja4
---

# Cloudflare TLS Fingerprint Mismatch Blocks All Requests (curl_cffi → Playwright)

## Problem

Cloudflare began returning HTTP 403 on all LOCG requests after tightening `cf_clearance` verification to bind the cookie to the JA3/JA4 TLS fingerprint of the browser that originally solved the CAPTCHA challenge. The `curl_cffi` Chrome impersonation library synthesizes a Chrome TLS fingerprint but it never matches the fingerprint of a real challenge-solving session, so every request is rejected — even with a valid, unexpired cookie.

## Symptoms

- All LOCG CLI commands (search, releases, collection, add, etc.) fail with HTTP 403
- `cf_clearance` cookie is present in the session and not expired, but requests are still rejected
- No change in user credentials or behavior triggered the failure; it is an external change (Cloudflare policy)

## What Didn't Work

**`curl_cffi` with `impersonate="chrome"`** was the established approach. It synthesizes Chrome's TLS ClientHello (JA3/JA4 fingerprint) without running a real browser. This worked until Cloudflare began verifying that every subsequent request's TLS fingerprint matches the fingerprint of the session that originally obtained `cf_clearance`. Since `curl_cffi`'s synthesized fingerprint doesn't match any real browser session, the cookie is rejected regardless of its validity.

Refreshing or rotating `cf_clearance` does not help — the verification is fingerprint-bound, not time-bound. (Session history confirms `curl_cffi` was working as of April 2026 and broke when Cloudflare tightened fingerprint enforcement.)

## Solution

Replace `curl_cffi` with Playwright using `channel="chrome"` to drive the real system Chrome binary. Requests use `page.request.get/post` (Playwright's `APIRequestContext`), which shares the browser context's cookie jar without page navigation. A persistent browser profile at `~/.config/locg/playwright-profile/` preserves `cf_clearance` and `ci_session` across CLI invocations.

**`pyproject.toml` — swap the dependency:**
```toml
# Before
"curl-cffi>=0.13.0"

# After
"playwright>=1.40.0"
```

**`config.py` — new path helper:**
```python
def playwright_profile_dir() -> Path:
    return _config_dir() / "playwright-profile"
```

**`client.py` — launch persistent context with real Chrome:**
```python
from playwright.sync_api import APIResponse, sync_playwright

def __init__(self) -> None:
    self._playwright_instance = sync_playwright().start()
    try:
        self._context = self._playwright_instance.chromium.launch_persistent_context(
            user_data_dir=str(playwright_profile_dir()),
            channel="chrome",          # real system Chrome binary — authentic TLS fingerprint
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],  # plus --no-first-run, --no-default-browser-check
        )
        self._page = self._context.new_page()
    except Exception:
        self._playwright_instance.stop()  # prevent subprocess leak
        raise
```

**`client.py` — `_PlaywrightResponse` adapter (callers need `.status_code`, `.text`, `.content`, `.headers`, `.json()`):**
```python
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
        raw = api_response.body()      # Playwright's .text() calls .body() internally — read once and decode
        return _PlaywrightResponse(
            status_code=api_response.status,
            text=raw.decode("utf-8", errors="replace"),
            content=raw,
            headers=dict(api_response.headers),
        )
    finally:
        api_response.dispose()         # always release, even if body() raises
```

**`client.py` — HTTP calls via `APIRequestContext`:**
```python
# GET — pattern is the same for POST (use form= instead of params)
api_resp = self._page.request.get(url, timeout=30000)
return _wrap_response(api_resp)
```

**`client.py` — `close()` must use `try/finally`:**
```python
def close(self) -> None:
    try:
        self._context.close()
    finally:
        self._playwright_instance.stop()  # runs even if context.close() raises
```

**`cli.py` — construct client inside the `try` block:**
```python
# Before — NameError in finally if __init__ raised
client = LOCGClient()
try:
    ...
finally:
    client.close()

# After
client: Optional[LOCGClient] = None
try:
    client = LOCGClient()
    ...
finally:
    if client is not None:
        client.close()
```

## Why This Works

Cloudflare's `cf_clearance` verification checks that the TLS ClientHello fingerprint (JA3/JA4) on each request matches the fingerprint recorded when the CAPTCHA challenge was solved. Using `channel="chrome"` launches the actual system Chrome binary, so every TLS handshake produces an authentic Chrome fingerprint. The persistent context stores the `cf_clearance` cookie obtained during the first run (or a manual challenge-solving session) and replays it from the same real-Chrome TLS stack on subsequent requests — satisfying both the cookie and fingerprint checks simultaneously.

`page.request.*` (Playwright's `APIRequestContext`) sends HTTP requests through the browser engine's network stack rather than navigating pages, keeping calls fast while sharing the context's cookie jar.

## Prevention

**1. Read `body()` exactly once in response adapters.** Playwright's `.text()` calls `.body()` internally — calling both means two IPC round-trips and double memory. Always derive text via `body().decode(...)` in `_wrap_response()`. Never call Playwright response methods directly in command code.

**2. Implement `.json()` on any response wrapper.** The missing `.json()` caused a hard crash (`AttributeError`) in `commands.py::_post_json_with_retry()` on all add/update/remove commands. Every response-wrapper class passed to callers must implement `.json()`.

**3. Use `try/finally` for all multi-step cleanup.** Sequential `context.close(); pw.stop()` silently skips `pw.stop()` if `context.close()` raises. Always:
   ```python
   try:
       self._context.close()
   finally:
       self._playwright_instance.stop()
   ```

**4. Construct external-resource objects inside the `try` block.** If `LOCGClient.__init__` raises before `client` is bound, the `finally: client.close()` line raises `NameError` and the original error is lost. Use `client = None` before the `try`, then `if client is not None: client.close()` in `finally`.

**5. Test the adapter layer explicitly.** Mock `sync_playwright` at the module level and assert the wrapper's contract:
   ```python
   from locg.client import _wrap_response, _PlaywrightResponse

   def test_wrap_response_maps_fields():
       api_resp = MagicMock()
       api_resp.status = 200
       api_resp.body.return_value = b'{"ok": true}'
       api_resp.headers = {"content-type": "application/json"}
       result = _wrap_response(api_resp)
       assert result.status_code == 200
       assert result.json() == {"ok": True}
       api_resp.dispose.assert_called_once()
   ```

**6. If Cloudflare tightens further,** run Chrome headed once to solve the CAPTCHA manually (writing `cf_clearance` into the persistent profile at `~/.config/locg/playwright-profile/`), then resume headless. The persistent profile architecture already supports this without code changes.

## Related Issues

- Implementation plan: `docs/plans/2026-05-19-001-fix-playwright-http-client-plan.md`
- Linear issue: PER-38
