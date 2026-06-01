---
title: "fix: Replace curl_cffi with Playwright HTTP Client"
type: fix
status: active
date: 2026-05-19
---

# fix: Replace curl_cffi with Playwright HTTP Client

## Overview

Replace the `curl_cffi` Chrome-impersonation session in `LOCGClient` with Playwright's `APIRequestContext`, launched against the real system Chrome binary and a dedicated persistent profile. Cloudflare now binds `cf_clearance` to the JA3/JA4 TLS fingerprint of the solving browser, so `curl_cffi` impersonation fails even with a valid cookie. The real Chrome binary produces the correct fingerprint; `cf_clearance` cookies stored in the dedicated profile persist across CLI invocations.

## Problem Frame

`curl_cffi` with `impersonate="chrome"` synthesizes a Chrome TLS fingerprint but is rejected by Cloudflare because the fingerprint differs from the one that originally solved the challenge. All LOCG requests return 403. Using the real Chrome binary eliminates the fingerprint mismatch.

## Requirements Trace

- R1. LOCGClient HTTP calls bypass the Cloudflare 403 block.
- R2. `cf_clearance` and `ci_session` cookies persist across CLI invocations without requiring the user to re-solve Cloudflare challenges each run.
- R3. The public interface — `get()`, `post()`, `login()`, `verify_session()`, `close()`, `is_authenticated`, `require_auth()` — is preserved unchanged for callers in `commands.py`.
- R4. All existing LOCG CLI commands work without modification to `cli.py` or `commands.py`.
- R5. The test suite passes after the migration.

## Scope Boundaries

- No changes to `cli.py`, `commands.py`, `models.py`, `parser.py`, or `cache.py`.
- No migration of existing `~/.config/locg/cookies.json` to the new profile; users may need to run `locg login` once after upgrading.
- The new profile lives at `~/.config/locg/playwright-profile/`; it is separate from the user's main Chrome profile, avoiding SingletonLock conflicts.

### Deferred to Separate Tasks

- Headed-fallback flow when `cf_clearance` is stale and Cloudflare presents a JS challenge: out of scope; user should run `locg login` (which could open headed Chrome) to refresh.

## Context & Research

### Relevant Code and Patterns

- `src/locg/client.py` — `LOCGClient` class; the only file that imports `curl_cffi`. All HTTP logic is here.
- `src/locg/config.py` — `_config_dir()`, `ensure_config_dir()`, `cookie_path()` for XDG-aware path management.
- `tests/test_client.py` — mocks `_load_cookies` during construction and replaces `_session` directly.
- `pyproject.toml` — project dependencies.

### Institutional Learnings

- None in `docs/solutions/` for this area yet.

### External References

- `playwright.sync_api.BrowserType.launch_persistent_context` — accepts `channel="chrome"` (auto-locates system Chrome), `user_data_dir`, `headless`, `args`.
- `playwright.sync_api.APIRequestContext` — `page.request.get(url, params=…)` and `page.request.post(url, form=…)` share the context's cookie jar; no page navigation needed.
- `playwright.sync_api.BrowserContext.cookies(urls=[…])` — returns list of cookie dicts for cookie inspection.
- `playwright.sync_api.APIResponse` — `.status` (int), `.text()` (str), `.body()` (bytes), `.headers` (dict, lowercase keys), `.dispose()`.

## Key Technical Decisions

- **`channel="chrome"` instead of `executable_path`:** Auto-locates system Chrome across OS installs; Playwright-supported; produces correct TLS fingerprint.
- **Dedicated profile at `~/.config/locg/playwright-profile/`:** Avoids SingletonLock conflicts with user's running Chrome. Playwright creates the dir on first launch and persists cookies across runs.
- **`page.request.get/post` instead of `page.goto` or `page.evaluate(fetch)`:** `APIRequestContext` shares the context's cookie jar, returns structured response with `.status`, `.text()`, `.body()`, `.headers`; no page navigation overhead; synchronous API.
- **`_PlaywrightResponse` wrapper:** Adapts Playwright's `APIResponse` to the `.status_code`, `.text`, `.content`, `.headers` shape callers expect, so `commands.py` and `parser.py` require no changes.
- **`_load_cookies` / `_save_cookies` removed:** Cookie persistence is handled by the Playwright persistent context. The `cookie_path()` JSON file is no longer written.
- **Headless mode (deferred):** Defaulting to `headless=True` is correct when a valid `cf_clearance` is present in the profile (same Chrome binary = same TLS fingerprint = cookie accepted). If challenges recur in practice, adding `--disable-blink-features=AutomationControlled` or a headed fallback in `login()` can be addressed separately.

## Open Questions

### Resolved During Planning

- **Can `page.request` share cookies with `page.goto`?** Yes — `APIRequestContext` and page navigation share the same `BrowserContext` cookie jar.
- **Does `launch_persistent_context` create the `user_data_dir` if missing?** Yes — Playwright creates the directory on first launch.
- **Can we use `channel="chrome"` without knowing the macOS binary path?** Yes — `channel="chrome"` is Playwright's supported mechanism for locating the system Chrome installation across platforms.

### Deferred to Implementation

- Whether `headless=True` reliably passes Cloudflare once a valid `cf_clearance` is stored in the dedicated profile. Test empirically; switch to `headless=False` (or add `--start-minimized`) if blocks recur.
- Exact Playwright `args` needed (e.g., `--disable-blink-features=AutomationControlled`, `--no-first-run`, `--no-default-browser-check`). Determine at implementation time by testing against leagueofcomicgeeks.com.
- Whether `.dispose()` must be called on every `APIResponse`. Caller pattern should call it after reading `.text()` and `.body()` to free memory.

## High-Level Technical Design

> *This illustrates the intended approach and is directional guidance for review, not implementation specification. The implementing agent should treat it as context, not code to reproduce.*

```
LOCGClient.__init__
  └─ sync_playwright().start() → _playwright_instance
  └─ chromium.launch_persistent_context(
         user_data_dir=~/.config/locg/playwright-profile/,
         channel="chrome", headless=True, args=[…]
     ) → _context
  └─ _context.new_page() → _page

LOCGClient.get(path, params)
  └─ _page.request.get(url, params=params) → APIResponse
  └─ _wrap_response(api_response) → _PlaywrightResponse

LOCGClient.post(path, data)
  └─ _page.request.post(url, form=data) → APIResponse
  └─ _wrap_response(api_response) → _PlaywrightResponse

LOCGClient.is_authenticated (property)
  └─ _context.cookies(["https://leagueofcomicgeeks.com"])
  └─ any(c["name"] == "ci_session" for c in cookies)

LOCGClient.close()
  └─ _context.close()
  └─ _playwright_instance.stop()

_PlaywrightResponse (dataclass)
  status_code: int      ← api_response.status
  text: str             ← api_response.text()
  content: bytes        ← api_response.body()
  headers: dict         ← dict(api_response.headers)
```

## Implementation Units

- [x] **Unit 1: Update Dependencies**

**Goal:** Swap `curl-cffi` for `playwright` in project metadata.

**Requirements:** R1, R2

**Dependencies:** None

**Files:**
- Modify: `pyproject.toml`

**Approach:**
- Remove `curl-cffi>=0.13.0` from `[project].dependencies`.
- Add `playwright>=1.40.0` to `[project].dependencies`.
- Note in `CLAUDE.md` or `README.md` that after `pip install`, users must run `playwright install chrome` to ensure Playwright can locate the channel — OR document that `channel="chrome"` relies on the system Chrome installation being present (no Playwright browser download needed).

**Test expectation: none** — pure config, no behavioral change.

**Verification:**
- `pip install -e .` succeeds.
- `python -c "from playwright.sync_api import sync_playwright"` succeeds.

---

- [x] **Unit 2: Response Wrapper**

**Goal:** Introduce `_PlaywrightResponse` so `get()` and `post()` return an object with the `.status_code`, `.text`, `.content`, `.headers` attributes that callers in `commands.py` and the existing tests depend on.

**Requirements:** R3, R4

**Dependencies:** Unit 1

**Files:**
- Modify: `src/locg/client.py`
- Test: `tests/test_client.py`

**Approach:**
- Add a `_PlaywrightResponse` dataclass (or named tuple) with `status_code: int`, `text: str`, `content: bytes`, `headers: dict`.
- Add `_wrap_playwright_response(api_response) -> _PlaywrightResponse` that reads `.status`, `.text()`, `.body()`, `dict(.headers)` and calls `.dispose()`.
- Update `get()` and `post()` return type annotations from `cffi_requests.Response` to `_PlaywrightResponse`.
- Remove the `cffi_requests` import.

**Test scenarios:**
- Happy path: `_wrap_playwright_response` with a mock `APIResponse` returns correct `.status_code`, `.text`, `.content`, `.headers`.
- Edge case: empty body produces `content=b""` and `text=""`, not `None`.
- Happy path: `resp.headers.get("Retry-After", "60")` works (dict `.get()` is available).

**Verification:**
- `grep -r "cffi_requests" src/` returns nothing.
- All callers that read `.text`, `.status_code`, `.content`, `.headers` on the response continue to work (verified via passing tests in Unit 5).

---

- [x] **Unit 3: Replace LOCGClient HTTP Core**

**Goal:** Replace the `curl_cffi` session with a Playwright persistent browser context. Implement `get()`, `post()`, `is_authenticated`, and `close()` against the new backend.

**Requirements:** R1, R2, R3, R4

**Dependencies:** Unit 2

**Files:**
- Modify: `src/locg/client.py`
- Modify: `src/locg/config.py` (add `playwright_profile_dir()` helper)

**Approach:**
- In `config.py`, add `playwright_profile_dir() -> Path` returning `_config_dir() / "playwright-profile"`. `launch_persistent_context` creates it on first run.
- In `client.py`:
  - Remove `curl_cffi` imports; add `from playwright.sync_api import sync_playwright`.
  - In `__init__`: call `sync_playwright().start()` → `self._playwright_instance`; call `self._playwright_instance.chromium.launch_persistent_context(user_data_dir=str(playwright_profile_dir()), channel="chrome", headless=True, args=[…])` → `self._context`; call `self._context.new_page()` → `self._page`.
  - Remove `_load_cookies`, `_save_cookies`, and `_cookies_loaded` field.
  - `get()`: build URL, call `self._page.request.get(url, params=params or {}, timeout=30000)`, wrap and return. Preserve rate-limit check on status 429.
  - `post()`: call `self._page.request.post(url, form=data or {}, timeout=30000)`, wrap and return. Remove the `_save_cookies()` call (cookies are persisted by Playwright).
  - `is_authenticated` property: use `self._context.cookies(["https://leagueofcomicgeeks.com"])` and check for `ci_session`.
  - `close()`: `self._context.close(); self._playwright_instance.stop()`.

**Patterns to follow:**
- `src/locg/config.py` — existing `_config_dir()`, `ensure_config_dir()` pattern for path helpers.

**Test scenarios:**
- See Unit 5 for full test coverage; unit-level verification is via passing tests.

**Verification:**
- `from locg.client import LOCGClient` imports without error.
- `LOCGClient()` can be instantiated (manual smoke test — requires system Chrome).
- No `curl_cffi` references remain in `src/locg/`.

---

- [x] **Unit 4: Remove `cookie_path()` Usage**

**Goal:** Remove the JSON cookie file read/write that `_load_cookies` / `_save_cookies` performed. `cookie_path()` in `config.py` can stay (backward compat, deletion is out of scope) but nothing should call it from `client.py`.

**Requirements:** R2, R3

**Dependencies:** Unit 3

**Files:**
- Modify: `src/locg/client.py`
- Modify: `src/locg/__init__.py` (if it re-exports cookie helpers — check first)

**Approach:**
- Confirm `cookie_path` is no longer imported in `client.py` after Unit 3.
- Confirm `ensure_config_dir` is no longer called from `client.py` (profile dir creation is Playwright's responsibility).
- Update `client.py` docstring from "Cloudflare bypass via curl_cffi" to reflect Playwright.

**Test expectation: none** — cleanup, no behavioral change beyond what Unit 3 covers.

**Verification:**
- `grep "cookie_path\|ensure_config_dir\|_load_cookies\|_save_cookies" src/locg/client.py` returns nothing.

---

- [x] **Unit 5: Update Tests**

**Goal:** Update `tests/test_client.py` to mock the Playwright context instead of `curl_cffi`, so all existing behavior tests pass with the new implementation.

**Requirements:** R5

**Dependencies:** Unit 3

**Files:**
- Modify: `tests/test_client.py`

**Approach:**
- Replace `_make_client_with_session(ci_session)` helper:
  - Patch `sync_playwright` during `LOCGClient.__init__` to avoid launching Chrome.
  - Install a mock `_context` with `.cookies()` returning a list containing a fake `ci_session` dict (or empty list when `ci_session=None`).
  - Install a mock `_page` with `.request.get` / `.request.post` as `MagicMock`.
  - Install a mock `_playwright_instance` with `.stop` as `MagicMock`.
- All tests that currently mock `client.verify_session` and `client.login` at the method level require no changes beyond the helper.
- Tests that exercise `get()` / `post()` directly (none currently — but new ones may be added) should mock `_page.request.get/post` to return a mock with `.status`, `.text()`, `.body()`, `.headers`, `.dispose()`.

**Test scenarios:**
- Happy path: `LOCGClient()` can be constructed without launching Chrome (mock applied).
- Happy path: `is_authenticated` returns `True` when `ci_session` is in the mock cookie list.
- Edge case: `is_authenticated` returns `False` when cookie list is empty.
- All existing `require_auth` behavior tests pass (R1–R10 in current file) — no logic changes, only mock plumbing changes.
- Happy path: `close()` calls `_context.close()` and `_playwright_instance.stop()`.

**Verification:**
- `PYTHONPATH=src python3 -m pytest tests/test_client.py -v` — all tests pass.
- `PYTHONPATH=src python3 -m pytest tests/ -v` — full suite passes.

## System-Wide Impact

- **Interaction graph:** `LOCGClient` is instantiated in `src/locg/cli.py` via `cmd_*` functions in `commands.py`. The `mock_client` fixture in `conftest.py` mocks the entire client, so command tests are unaffected.
- **Error propagation:** `get()` still raises on 429. Other HTTP errors (403, 500) are returned as `_PlaywrightResponse` with the appropriate `status_code`; callers check status codes as before.
- **State lifecycle risks:** Playwright keeps a browser process running for the duration of `LOCGClient`'s lifetime. `close()` must be called to release it. The existing `cli.py` pattern (instantiate client, run command, no explicit close) will leave Chrome running until process exit — acceptable for a short-lived CLI. If `close()` needs to be guaranteed, `commands.py` or `cli.py` would need a try/finally, but that's out of scope.
- **API surface parity:** No other entry points to `LOCGClient`; change is self-contained.
- **Integration coverage:** `verify_session()` and `login()` make real HTTP calls; covered by the existing mock-at-method-level pattern. A live integration test against leagueofcomicgeeks.com is not feasible in CI but should be done manually after landing.
- **Unchanged invariants:** `require_auth()`, `_try_env_login()`, `verify_session()`, and `login()` logic are untouched.

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| `headless=True` still blocked by Cloudflare despite correct TLS fingerprint | Test empirically; if blocked, switch to `headless=False` in `launch_persistent_context`. The dedicated profile means the browser window only appears on first run or after challenge expiry. |
| `launch_persistent_context` with `channel="chrome"` fails if system Chrome is not installed | Document Chrome as a runtime prerequisite. The error from Playwright is clear. |
| Playwright browser process lingers if `close()` is not called in error paths | Out of scope for this fix; CLI is short-lived. Add try/finally in a follow-up if needed. |
| Existing `cookies.json` not migrated | Users run `locg login` once after upgrade. Document in CLAUDE.md / release notes. |
| Test mocking strategy for `sync_playwright` is fragile if Playwright API changes | Keep the mock surface thin — patch at `sync_playwright` level, not internal Playwright internals. |

## Documentation / Operational Notes

- Update `CLAUDE.md` Dependencies section: replace `curl-cffi` with `playwright`; add note that system Chrome must be installed.
- Users with an existing `~/.config/locg/cookies.json` must re-run `locg login` once to populate the new Playwright profile.
- First invocation creates `~/.config/locg/playwright-profile/`; subsequent runs reuse it.

## Sources & References

- Related code: `src/locg/client.py`, `src/locg/config.py`, `tests/test_client.py`
- External docs: Playwright `launch_persistent_context` (Python), `APIRequestContext`
- Linear issue: PER-38
