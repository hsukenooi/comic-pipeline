---
title: "OAuth Token Refresh + Retry Pattern in ebay_fetch"
module: apps/ebay
date: 2026-07-11
problem_type: design_pattern
component: authentication
severity: high
related_components:
  - ebay_fetch.py
  - grade_photos.py
tags:
  - oauth
  - token-cache
  - retry
  - backward-compat
  - error-handling
  - batch
applies_when:
  - "Adding a new function that calls an OAuth-protected endpoint and needs failure detail beyond None"
  - "Retrying after a 401 inside a multi-item batch where the token cache could hand back the same rejected token"
  - "Adding defensive error handling to a network function that has defensive siblings already in the file"
---

# OAuth Token Refresh + Retry Pattern in ebay_fetch

## Context

Six tickets — BUI-184, BUI-299, BUI-300, BUI-310, BUI-311, BUI-312 — hardened the same underlying primitive from different angles: the OAuth token lifecycle and the network-call retry/error-handling shape shared by `apps/ebay/src/ebay_fetch.py`'s functions and their downstream caller `grade_photos.py`. Each ticket individually looked like a local fix (a malformed-JSON guard here, a timeout there), but they kept converging on the same four idioms — because the two files' network functions had drifted from each other: some had retry loops with backoff, some didn't; some distinguished failure reasons, some collapsed everything to `None`; only one atomically wrote its cache. The fixes that stuck were the ones that made a function look like its most-defensive sibling, not the ones that added something novel.

This doc distills the four idioms so the *seventh* time is a lookup, not a rediscovery.

## Guidance

### 1. Status-aware / status-blind wrapper pair (backward-compatible status surfacing)

`fetch_item()` originally collapsed every non-200 response — 401, 404, 429, network error — to a bare `return None`. Fine for a CLI printing "skip this one," useless for a caller that needs to distinguish "token expired, retry with a fresh one" from "item genuinely doesn't exist." BUI-310's fix was **not** to change `fetch_item()`'s contract (that would ripple to every caller) — it was to add a lower-level function that returns the status too, and make the old function a thin wrapper:

```python
# ebay_fetch.py (fetch_item_with_status / fetch_item)
def fetch_item_with_status(item_id, token, base_url, retries=3):
    """Returns (data, status_code). data is None on any failure; status_code
    is the HTTP status of the terminal response (401/404/429...), or None on a
    network error with no response. On success, status_code is always 200."""
    ...

def fetch_item(item_id, token, base_url, retries=3):
    """Thin wrapper over fetch_item_with_status() that discards the status,
    preserving the original None-on-failure contract for existing callers."""
    data, _status = fetch_item_with_status(item_id, token, base_url, retries=retries)
    return data
```

New callers that need to react to *why* a fetch failed call `fetch_item_with_status()` directly; every old caller (this module's own CLI `main()`, pre-BUI-310 `grade_photos.py`) keeps working unmodified because `fetch_item()`'s signature and return contract never changed.

### 2. Refresh-on-401 self-heal: cache-bypassing `get_token(force_refresh=True)` + one retry

`get_token()` normally trusts its 5-minute-buffered disk cache. BUI-310 added a keyword-only `force_refresh` param because a 401 mid-batch isn't provably a cache-TTL problem (could be server-side revocation, clock skew) — a caller retrying after a 401 needs a token *guaranteed different* from the one just rejected, not "whatever the cache currently says is still valid":

```python
def get_token(client_id, client_secret, base_url, *, force_refresh=False):
    """force_refresh=True skips the cache-freshness check and always requests
    a new token from eBay."""
```

The caller pattern in `grade_photos.py:main()` is a **bounded (max-2-attempt) retry loop**: try with the current token, and only on a `TokenExpiredError` (raised from a 401) force-refresh once and retry that same item, keeping the fresh token for the rest of the sequential batch:

```python
# grade_photos.py:main()
result = None
for attempt in range(2):
    try:
        result = download_listing(token, item_id, f"{args.workdir}/{label}", base_url)
        break
    except TokenExpiredError as e:
        if attempt == 0:
            try:
                token = get_token(client_id, client_secret, base_url, force_refresh=True)
            except SystemExit:
                print(f"{label}: FETCH FAILED — token refresh failed after 401 (see stderr)")
                break
            continue
        print(f"{label}: FETCH FAILED — {e}")
        break
    except RuntimeError as e:
        print(f"{label}: FETCH FAILED — {e}")
        break
```

### 3. The `SystemExit`-past-except trap

`get_token()` calls `sys.exit(1)` on several hard-auth-failure paths (bad credentials, retry budget exhausted on 429/5xx, malformed token response). `sys.exit()` raises `SystemExit`, which subclasses **`BaseException`, not `Exception`** — so a bare `except Exception` (or `except RuntimeError`) around the refresh call does **not** catch it. Without a guard, a hard auth failure during the mid-batch refresh-on-401 retry propagates all the way out of `main()`'s loop and kills the whole process, silently dropping the `FETCH FAILED` lines for every remaining item — exactly the failure mode BUI-300's "one item's failure never aborts the batch" invariant exists to prevent.

The fix is the explicit `except SystemExit:` block in the loop above, which degrades a hard refresh failure to that one item's `FETCH FAILED` line and lets the batch continue. **Two independent reviewers flagged this on BUI-310** — it is easy to miss precisely because `except Exception` *looks* like it covers everything.

### 4. Mirror-the-most-defensive-sibling idiom (now centralized via shared helpers)

Rather than inventing new error handling per function, each hardening pass copied the shape of whichever sibling already handled that failure mode best. The `RequestException` guard in `fetch_item_with_status()` —

```python
for attempt in range(retries):
    try:
        resp = requests.get(url, headers=headers, params=params, timeout=10)
    except requests.exceptions.RequestException as exc:
        print(f"Network error fetching item {item_id}: {exc}", file=sys.stderr)
        return None, None
```

— has the same shape as the guards in `get_token()`, `search_seller_listings()`, and `search_by_keyword()`. Likewise the atomic tmp→`.replace()` cache write in `get_token()`'s token-cache write (commented "mirrors `_aspects_cache_put`") copies the pattern already established by `_aspects_cache_put()`.

This was a **temporary** idiom before BUI-323. The consolidation (a shared retry/backoff + atomic-write helper) shipped in BUI-323 (PR #160): see `_retry_request()` and `_atomic_write_json()` in `apps/ebay/src/ebay_fetch.py`. These helpers are now the canonical implementations; when adding new network calls or cache writes to `ebay_fetch.py`, use them rather than hand-duplicating the pattern.

## Why This Matters

- **No status wrapper (pattern 1):** a 401 looks identical to a 404 or a network blip to any caller. A caller reacting to "not found" by treating a listing as unavailable-forever, when it was really just an expired token, risks a stale decision — skipping a still-live auction and later re-fetching/re-bidding it, a **duplicate-buy risk** in this repo's comic-buying pipeline.
- **No refresh-on-401 (pattern 2):** a token that expires partway through a long sequential batch (e.g., grading 15 comics) turns *every remaining item* into `FETCH FAILED` instead of self-healing on the first expired one — losing real work for no reason.
- **No `SystemExit` guard (pattern 3):** a hard auth failure during that one refresh attempt crashes the entire batch process instead of degrading to a single item's failure line — silently violating BUI-300's per-item-isolation invariant via an exception type most people forget `except Exception` doesn't catch.
- **No sibling mirroring (pattern 4):** a fix applied to one function doesn't propagate to structurally identical functions until each independently hits the same bug in production — and an un-atomic cache write risks a crash mid-write leaving a corrupted, half-written JSON cache that silently breaks every subsequent read.

## When to Apply

- **Adding any new network call to `ebay_fetch.py`:** check what `get_token()` / `fetch_item_with_status()` already do (retry/backoff shape, `RequestException` guard, malformed-JSON guard) and match it rather than writing a fresh ad hoc version.
- **Any long sequential batch that holds a cached token across many iterations** (like `grade_photos.py:main()`): assume the token can expire mid-batch and needs a bounded refresh-and-retry, with an explicit `SystemExit` guard around the refresh call.
- **Any new caller of `fetch_item()`:** if it only needs "did this work," keep using `fetch_item()`. If it needs to distinguish *why* it failed (auth vs. not-found vs. rate-limited), call `fetch_item_with_status()` directly instead of inferring status from `None`.
- **Any new disk-cache write:** use the tmp-file-then-`.replace()` pattern from `_aspects_cache_put()` / `get_token()`'s cache write, not a direct `open(..., "w")`.

## Examples

Backward-compatible status surfacing (BUI-310):

```python
# Before: every failure reason collapsed to None
def fetch_item(item_id, token, base_url, retries=3):
    ...
    else:
        print(f"Error fetching item {item_id}: HTTP {resp.status_code}...")
        return None            # 401, 404, 429-exhausted all look the same

# After: status-aware function + thin backward-compatible wrapper
def fetch_item_with_status(item_id, token, base_url, retries=3):
    ...
    return None, resp.status_code   # caller can branch on 401 specifically

def fetch_item(item_id, token, base_url, retries=3):
    data, _status = fetch_item_with_status(item_id, token, base_url, retries=retries)
    return data                     # old callers see no change
```

The `SystemExit` guard (BUI-310):

```python
# Naive version — looks safe, isn't: get_token() can sys.exit(1) -> SystemExit
# (a BaseException) unwinds the whole batch, dropping every remaining item.
except TokenExpiredError as e:
    if attempt == 0:
        token = get_token(client_id, client_secret, base_url, force_refresh=True)
        continue

# Guarded version — SystemExit caught explicitly, batch continues.
except TokenExpiredError as e:
    if attempt == 0:
        try:
            token = get_token(client_id, client_secret, base_url, force_refresh=True)
        except SystemExit:
            print(f"{label}: FETCH FAILED — token refresh failed after 401 (see stderr)")
            break
        continue
```

## Related

- `docs/solutions/developer-experience/cross-package-regressions-escape-per-package-test-runs.md` — background on the `ebay_fetch.py`↔`grade_photos.py` coupling (its BUI-283 section describes the *move* of retry/OAuth logic into `ebay_fetch.py`; this doc describes that logic's current defensive shape).
- `docs/solutions/best-practices/mypy-bool-return-type-and-non-required-typecheck-ci.md` and `docs/solutions/best-practices/fmv-self-referential-feedback-deflation-guard.md` — a related `requests.exceptions.JSONDecodeError`-subclasses-both-`ValueError`-and-`RequestException` exception-ordering gotcha, in `apps/fmv` (same theme, different primitive).
- **BUI-323** (shipped, PR #160) — extracted `_retry_request()` and `_atomic_write_json()` into `apps/ebay/src/ebay_fetch.py` to consolidate the retry/backoff and atomic-write patterns from pattern 4.
