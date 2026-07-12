---
title: Detecting a Metron 5xx from mokkari to trip the batch breaker
date: 2026-07-12
category: design-patterns
module: locg-cli
problem_type: design_pattern
component: service_object
severity: high
applies_when:
  - "Wrapping an external API library that flattens HTTP 5xx into the same exception type as no-match/validation errors"
  - "A batch caller must distinguish a transient outage (stop calling) from a genuine empty result (keep going)"
  - "The client runs synchronously inside a single-worker async route where a long in-request sleep would wedge the server"
tags:
  - metron
  - mokkari
  - circuit-breaker
  - error-classification
  - capped-retry
  - external-api
  - record-win
  - bui-342
---

# Detecting a Metron 5xx from mokkari to trip the batch breaker

## Context

`MetronClient` (`packages/locg-cli/src/locg/metron.py`) wraps the Metron API via the `mokkari` library. `cmd_collection_record_win` calls it per row in a batch and polls a `degraded` flag (`_check_metron_degraded`) to decide whether to keep hitting Metron or stop. That breaker was built incrementally across the **BUI-247 → BUI-260 (429 / rate-limit) → BUI-255 (connection error) → BUI-342 (HTTP 5xx)** lineage.

BUI-342 closed the last hole. Before it, a Metron **5xx** surfaced from mokkari as a plain `ApiError`, hit each lookup's blanket `except Exception`, and returned `None` with `degraded` left `False` — **indistinguishable from a genuine "no match."** On a sustained Metron outage the record-win batch kept hammering the down server and silently recorded every win as "not in Metron," with only routine DEBUG "lookup failed" lines in the logs (no WARNING that Metron was erroring).

The durable, reusable lesson is a technique plus a deliberate design constraint, which is why this is filed as a design pattern rather than a bug post-mortem.

## Guidance

**Classify a flat library exception into transient-vs-permanent by walking its `__cause__` chain — don't collapse the whole exception type to one verdict, and don't scrape the message string.**

mokkari raises the *same* `ApiError` type for ordinary no-matches (pydantic data-shape errors), `detail`-based 404s, connection errors, and real 5xx responses. So:

- Tripping `degraded` on **any** `ApiError` would halt a perfectly healthy batch the first time a book genuinely isn't in Metron — a false "Metron is down" that disables enrichment for every remaining row.
- Scraping the message string for a status code is brittle across mokkari/requests versions.

Instead, recover the real HTTP status from the chained cause and trip only on 500–599:

```python
def _http_status_from_cause(exc: BaseException) -> Optional[int]:
    cause = getattr(exc, "__cause__", None)
    response = getattr(cause, "response", None)
    status = getattr(response, "status_code", None)
    return status if isinstance(status, int) else None

def _is_server_error(exc: BaseException) -> bool:
    if not isinstance(exc, ApiError):
        return False
    status = _http_status_from_cause(exc)
    return status is not None and 500 <= status < 600
```

Each lookup's blanket handler re-raises **only** a real 5xx (into the retry decorator), leaving every other `ApiError` a silent `None` exactly as before:

```python
except Exception as exc:  # noqa: BLE001
    if _is_server_error(exc):
        raise                     # -> @_retry_once_on_rate_limit
    if _is_connection_error(exc):
        self.degraded = True
    logger.debug("Metron lookup failed for %r #%s: %s", series_query, issue_number, exc)
    return None
```

**Retry once, capped — never escalating backoff.** `MetronClient` runs synchronously inside the single-worker async route; a long in-route sleep would wedge the server. This is the deliberate BUI-255/BUI-260 constraint. So the fix reuses the existing `@_retry_once_on_rate_limit` decorator, adding a symmetric 5xx arm of ONE capped 1.0s retry, then trips:

```python
except ApiError as exc:
    if not _is_server_error(exc):
        return None               # non-5xx that somehow propagated: fail, don't trip
    time.sleep(_SERVER_ERROR_RETRY_SLEEP)  # 1.0s, not the shell path's 1s->60s escalation
    try:
        return func(self, *args, **kwargs)
    except (RateLimitError, ApiError):
        self.degraded = True
        return None
```

The batch caller needs no new logic — `_check_metron_degraded` already stops the batch once `degraded` is `True`; the fix just makes a 5xx flip that flag.

## Why This Matters

mokkari's `_handle_http_response` does `raise ApiError(msg) from err`, where `err` is the `requests.HTTPError` from `raise_for_status()`, which still carries `.response.status_code`. So a real 5xx has `__cause__.response.status_code ∈ [500, 600)`. Data-shape (pydantic), `detail`-based 404, and connection `ApiError`s are raised **without** a chained HTTP response (`__cause__` is `None` or non-HTTP), so `_is_server_error` returns `False` and a genuine no-match never trips the breaker. Verified against **mokkari 3.27.0**.

Both retry arms catch `(RateLimitError, ApiError)`, so a cross-class retry (5xx-after-429 or 429-after-5xx) trips `degraded` and returns `None` rather than escaping the decorator into `cmd_collection_record_win` (which only catches `MetronCredentialError`).

Without this, a Metron outage is a **silent data-quality event**: an entire record-win batch completes "successfully" while stripping Metron enrichment from every row and paying N sequential calls to a down server — the exact repeated-work cost the breaker exists to prevent.

The general rule: **detect the specific failure signal, never trip a breaker on the generic shared exception type.** When one broad exception carries both transient outages and ordinary empty results, a breaker keyed on the type alone either misses real outages or falsely halts healthy work.

## When to Apply

- Wrapping an external API library that flattens HTTP 5xx into the same exception type as no-match / validation errors.
- A batch caller must tell a transient outage (stop calling) apart from a genuine empty result (keep going).
- The client runs synchronously inside a single-worker async route, where a long in-request sleep would wedge the server — favor one short capped retry over escalating backoff.

## Examples

**Guard the library coupling with a contract test that drives the library's own raise path** — not a hand-built exception — so a future upgrade that stops chaining the HTTPError fails loudly here instead of silently regressing the breaker to never-trips (the pre-BUI-342 bug):

```python
session = mokkari.api("u", "p", user_agent="locg-cli-test")  # offline
for status, expected in ((500, True), (503, True), (404, False)):
    resp = requests.Response(); resp.status_code = status; resp._content = b"body"
    resp.url = "https://metron.cloud/api/issue/"
    try:
        session._handle_http_response(resp)
        raise AssertionError(f"mokkari did not raise on {status}")
    except AssertionError:
        raise
    except Exception as exc:  # pin whatever mokkari raises
        assert _is_server_error(exc) is expected, \
            f"mokkari 5xx-detection contract broke for {status}: {exc!r}"
```

Plus two narrower guards:

- **Detection matrix** — 500/599 → `True`; 404, 429, `"Connection error:"`, a bare-message `ApiError`, and a non-`ApiError` → `False`.
- **Batch-breaker test** (`test_record_win_metron_5xx_disables_metron`) — after a `None` + `degraded=True` from the first row, `metron.lookup_issue.call_count == 1`: Metron is not called again for the rest of the batch.

Files: `packages/locg-cli/src/locg/metron.py` (detection + decorator), `packages/locg-cli/src/locg/commands.py` (`_check_metron_degraded` docstring only), tests in `packages/locg-cli/tests/test_metron.py` and `tests/test_collection_commands.py`.

## Related

- [oauth-token-refresh-retry-pattern](../design-patterns/oauth-token-refresh-retry-pattern.md) — sibling pattern in `apps/ebay` (BUI-184/299/300/310/311/312): a network client hardened across many tickets around typed-failure-detection-instead-of-collapsing-to-`None` + a bounded retry + "make a function look like its most-defensive sibling." Same philosophy, different module and mechanism (OAuth token-refresh vs Metron 5xx → shared `degraded` breaker); note the backoff shape diverges (that path retries with backoff; this one uses one capped 1.0s retry because it is on the single-worker async route).
- Breaker lineage: BUI-247 → BUI-260 (429 / rate-limit) → BUI-255 (connection error) → **BUI-342** (5xx).
- Follow-up: **BUI-344** — `resolve_creator_run`'s per-candidate loop has no `degraded` short-circuit, so a creator-run lookup against a down Metron still pays one capped retry per candidate instead of bailing early (latency-only, CLI-only).
- Protects the record-win correctness family (`cmd_collection_record_win`) from silently recording wins as "not in Metron" during an outage.
