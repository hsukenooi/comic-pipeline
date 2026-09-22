---
title: "A fail-soft helper that returns partial results erases the error for any caller whose whole output is a count"
date: 2026-09-22
category: integration-issues
module: "apps/ebay (ebay_fetch.py search_by_keyword / search_active_asks) + apps/fmv (fmv_runner.py _fetch_active_asks) (BUI-971)"
problem_type: integration_issue
component: service_object
severity: medium
mechanized_by: test
enforced_by_test:
  - "apps/ebay/tests/test_ebay_fetch.py::test_browse_http_error_returns_an_error_key_and_never_n_zero"
  - "apps/ebay/tests/test_ebay_fetch.py::test_a_genuine_zero_carries_no_error_key"
  - "apps/fmv/tests/test_fmv_runner.py::test_fetch_active_asks_warns_on_a_browse_search_error"
  - "apps/fmv/tests/test_fmv_runner.py::test_fetch_active_asks_returns_none_when_n_is_zero"
related_components:
  - "apps/ebay"
  - "apps/fmv"
  - "ebay-fetch"
  - "comic-fmv"
symptoms:
  - "ebay-fetch --active-asks printed {\"low\": null, \"n\": 0} and exited 0 whether the Browse search 401'd or the book genuinely had no live Buy It Now listings"
  - "A refused row showed no ask ceiling in either case, with no warning — an eBay outage was indistinguishable from a quiet market"
  - "The stderr line from the failing search was printed by the subprocess and then discarded by the parent, which only reads stdout"
root_cause: error_handling
resolution_type: code_fix
tags:
  - "fail-soft"
  - "partial-results"
  - "silent-zero"
  - "subprocess-boundary"
  - "active-ask-ceiling"
  - "error-in-payload-not-exit-code"
  - "bui-971"
  - "bui-565"
---

# A fail-soft partial return erases the error for a count-only caller

## Problem

`search_by_keyword` fails soft: on a Browse HTTP status, an exhausted rate-limit
budget, or a transport error it prints one line to stderr and returns whatever
items it had. That is right for `wishlist-sellers`, which wants partial results.

`search_active_asks` (BUI-954) wraps it and reduces the items to `{"low", "n"}`.
For a single-page search, "whatever it had" is `[]`, so a failed search produced
`{"low": null, "n": 0}` — byte-for-byte what a book with no live asks produces.
The process still exited 0. `comic-fmv` therefore could not tell the two apart,
and the refused row showed no ceiling either way, with no warning. This is the
BUI-565 shape (an errored fetch reading as a clean zero) on a display-only path:
it costs no money, but it hides an outage.

The stderr line was not a mitigation. The subprocess wrote it; the parent reads
only stdout, and on exit 0 discards `result.stderr` entirely.

## Symptoms

- `ebay-fetch --active-asks "Fantastic Four #93" --grade 6.0` against an invalid
  token: `{"low": null, "n": 0}`, exit 0.
- The same command against a valid token on an ask-less book: identical output.
- A brief with no `asks from` token, which is also what a correct quiet market
  looks like.

## What Didn't Work

- **Reading the exit code.** The subprocess genuinely succeeded — it ran, it
  produced valid JSON. Exiting non-zero would also have collapsed the failure
  into `_fetch_active_asks`'s existing generic `exit N` branch, losing which
  failure it was.
- **Reading stderr on exit 0.** Possible, but it makes the parent parse another
  process's log prose. The diagnosis belongs in the payload.
- **Changing `search_by_keyword`'s return type.** Two callers and twenty-odd
  tests depend on "a list of items, partial on failure". The partial return is
  correct for the other caller; only this one needed more.

## Solution

An optional `on_error` hook on the shared helper, and an `error` key in the
caller's payload.

```python
# search_by_keyword — an added channel, not a changed contract
def search_by_keyword(..., on_error=None):
    ...
    msg = f"Error searching by keyword: HTTP {resp.status_code}: {resp.text[:200]}"
    print(msg, file=sys.stderr)
    if on_error is not None:
        on_error(msg)
    return all_items[:max_results]

# search_active_asks — the count is the whole output, so any failure voids it
errors: list[str] = []
items = search_by_keyword(..., on_error=errors.append)
if errors:
    return {"low": None, "n": None, "error": errors[0]}
```

`n` is **null, not 0**, so a consumer that ignores the `error` key still cannot
read an outage as a genuine zero. `_fetch_active_asks` checks the key before its
`n`/`low` guard and prints one collapsed warning line; a genuine `n: 0` returns
None silently, as before.

Verified live, 2026-09-22: a seeded invalid token against the real Browse API
returns the `error` payload and prints
`Warning: ebay-fetch --active-asks search failed for Fantastic Four #93: ... HTTP 401 ...`,
while the same book on a valid token still returns `{"low": 21.0, "n": 4}`.

## Why This Works

**Partial results are only safe when the caller can see the shape of what is
missing.** A caller that forwards items can — a short list is visibly short. A
caller that reduces them to a count cannot: every failure collapses onto the same
number a success can legitimately produce. Fail-soft is a property of the pair,
not of the helper alone.

**The error belongs where the caller already looks.** The parent reads stdout, so
stdout carries the diagnosis. An exit code says "something failed"; a payload key
says which thing, in the parent's own vocabulary, and costs the parent nothing to
ignore when absent.

**Silence is reserved for the genuine zero.** Warning on every ask-less book
would drown the one line that means the search is down, which is the same
reasoning BUI-698 used to quiet the ledger's by-design 400.

## Related

- `docs/solutions/integration-issues/subprocess-boundary-between-comic-fmv-and-ebay-fetch-untested.md`
  — the same boundary, the previous failure: no test had ever crossed it.
- `docs/conventions/fmv-math-spec.md` §7c — the ceiling's display-only contract
  and this error shape.
