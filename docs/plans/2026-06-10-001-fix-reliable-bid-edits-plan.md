---
title: "fix: Reliable Active-Listing Bid Edits"
type: fix
status: active
date: 2026-06-10
issue: BUI-115
depends_on: BUI-114
---

# fix: Reliable Active-Listing Bid Edits (BUI-115)

## Summary

Updating an active snipe's max bid via `PATCH /api/bids/{item_id}` fails ~17% of the time (live Mac Mini log: 97× 200, 20× 503). The root cause is an **asymmetry between the GET and POST paths** in `GixenClient`: `modify_snipe`/`remove_snipe` must call `list_snipes()` (a GET) first to resolve Gixen's internal `dbidid`, but the GET path never recovers a stale Gixen session, while the POST path (used by `add_snipe`) does. When Gixen returns its stale-session response, the edit surfaces as a `503` instead of transparently re-authenticating and retrying.

This plan makes the GET path recover sessions the way the POST path already does, broadens stale-session detection so a 200-with-non-table body also triggers recovery, adds verify-after-modify so a silently-dropped change can't leave the DB lying, and makes the dashboard surface (and auto-retry once on) a transient failure.

Scope is the four fixes in BUI-115. No change to the `bids.status` lifecycle or the `REMOVED`/`PURGED` tombstone handling.

---

## Problem Frame

`api_edit_bid` (`packages/gixen-cli/server/main.py`) calls `_api_client.modify_snipe(...)` under `_api_lock`. `modify_snipe` (`packages/gixen-cli/gixen_client.py`) first calls `list_snipes()` → `_get_home_page()` (a GET) to find the snipe's `dbidid`, then POSTs the modification.

Two GET-path failure modes, both observed in the live logs, currently surface as `503`:

1. **HTTP 500 from Gixen** (137×). Gixen returns 500 for a stale/invalid session. `_post_home` catches 500 and transparently re-logins; `_get_home_page` calls `resp.raise_for_status()` and throws raw `requests.HTTPError` with no re-login. `api_edit_bid` maps that to `503`.
2. **200 with a non-table body** (parse error "Could not find snipe table", 743×). `_get_home_page` returns the body; `_parse_snipe_table` raises `GixenParseError`. `_is_session_expired()` did **not** match whatever Gixen actually returns (log shows "Session expired, re-logging in" = 0 and "forcing re-login" = 0 across 880 failures), so no re-login fires.

Secondary: `modify_snipe` POSTs and unconditionally returns `True` + logs "Modified snipe" without verifying the change landed. `add_snipe` has a verify-and-retry loop; `modify` has none — so a silent drop leaves the local DB showing the new `max_bid` while Gixen keeps the old.

**Why it's *inconsistent*:** `add` (POST-only) self-heals a stale session; `modify`/`remove` (mandatory GET first) do not. Success depends purely on whether the session is fresh at that instant.

---

## Requirements

- **R1** — A stale Gixen session encountered on the GET path recovers transparently (one re-login + one retry), instead of surfacing as a `503`, for both the HTTP-500 and the 200-non-table shapes.
- **R2** — `modify_snipe` confirms the new `max_bid` is live in Gixen before the server writes the local DB; a silent drop is retried once and, if still unconfirmed, raised rather than reported as success.
- **R3** — The dashboard inline-edit makes a failed save unmissable and recovers transparently from a single transient `503`.
- **R4** — No regression to the `bids.status` lifecycle, the `REMOVED`/`PURGED` tombstone handling, the `add_snipe` confirmation path, or the `_min_post_gap` throttle behavior.

---

## Key Technical Decisions

### KTD1 — Treat HTTP 500 on the GET path as session-expiry, mirroring `_post_home`

`_get_home_page` already takes a `retry_on_expired` flag and already re-logins on the HTML-detected expiry case. Add a 500 branch **before** `raise_for_status()` that does exactly what `_post_home` does: when `resp.status_code == 500 and retry_on_expired`, clear `session_id`, `login()`, and re-issue the GET with `retry_on_expired=False`. One attempt only — a second 500 falls through to `raise_for_status()` and propagates, so a genuinely-down Gixen still fails loudly. The BUI-114 ≥400 body-snippet log stays; it now precedes the recovery attempt.

Rationale: smallest change that closes the dominant failure mode, and it reuses a recovery pattern already proven on the POST path.

### KTD2 — Recover from a 200 non-table body via a re-login + retry in `list_snipes`, not by guessing the exact HTML

I cannot observe a real production failing body this session (BUI-114's capture needs a live event). Rather than hard-code a new `_is_session_expired` substring that may not match, make `list_snipes` resilient structurally: if `_parse_snipe_table` raises `GixenParseError` and we haven't retried yet, force a re-login and re-fetch once; if it still can't parse, raise. This covers every stale-session shape that yields a parseable-as-non-table 200 (login page, "could not log you in" wrong-alert, anti-bot interstitial) without depending on a brittle string. Keep the existing `_is_session_expired` fast-path (it short-circuits before the parse attempt when it *does* match).

Rationale: robustness to unknown body shapes (explicit constraint from the issue). The retry is bounded and only fires on the already-failing path, so it adds no cost to the success path.

### KTD3 — Verify-after-modify by reusing the post-POST list, parity with `add_snipe`

After the modify POST, re-list and confirm the snipe's `max_bid` matches the requested value (to Gixen's rounding). On mismatch/absence, back off (respecting `_min_post_gap`) and retry the POST once, then re-verify; if still unconfirmed, raise `GixenModifyNotConfirmedError` (new, parallel to `GixenAddNotConfirmedError`). `api_edit_bid` maps it to `503` (it already maps `GixenError` → 503), so the DB write is skipped and the client/dashboard retries — never a false success.

Rationale: closes the silent-divergence gap. Mirrors the established `add_snipe` pattern so the codebase stays consistent.

### KTD4 — Dashboard: persistent error state + one silent retry on 503

In `saveMaxBid` (`server/static/index.html`), on a `503` specifically, retry the PATCH once after a short delay before surfacing failure. If it still fails (or fails with a non-503), replace the transient 3.5s flash with a state that persists until the user acts (keep the cell visibly errored + a visible reason, not hover-only), so a failed edit in a batch can't be missed.

Rationale: addresses the *perceived* reliability directly, and the single client retry complements the server-side recovery for the residual race window.

---

## Implementation Units

### U1. GET-path 500 recovery in `_get_home_page`

**Goal:** A stale-session HTTP 500 on the GET path re-logins once and retries, instead of throwing `HTTPError` → 503. Advances R1.

**Dependencies:** none (builds on the BUI-114 ≥400 logging already at this site).

**Files:**
- `packages/gixen-cli/gixen_client.py` — `_get_home_page`
- `packages/gixen-cli/tests/test_gixen_client.py` — `TestSessionExpiration`

**Approach:** Add a `if resp.status_code == 500 and retry_on_expired:` branch before `raise_for_status()` that clears `session_id`, calls `login()`, and returns `self._get_home_page(retry_on_expired=False)`. Mirror the existing `_post_home` 500 handler (same log line intent: "Gixen returned 500 on GET, forcing re-login"). The second pass, with `retry_on_expired=False`, lets a persistent 500 propagate. Preserve the BUI-114 ≥400 body-snippet warning (it should log before the recovery attempt so a recovered case still leaves a breadcrumb).

**Patterns to follow:** `_post_home` 500 branch (the existing 500→re-login it already implements).

**Test scenarios:**
- GET returns 500 then a valid snipe table → `list_snipes()` returns parsed snipes; `session_id` refreshed; exactly one `login()` call. (Covers R1.)
- GET returns 500 twice → propagates `requests.HTTPError` (no infinite loop).
- GET returns 500 with `retry_on_expired=False` → raises immediately, no `login()` call.
- Mocks set `resp.status_code` as an int (200 / 500) — required by the new comparison.

**Verification:** New 500-recovery tests pass; existing `TestSessionExpiration` tests still pass.

### U2. Parse-failure recovery in `list_snipes`

**Goal:** A 200 response whose body isn't the snipe table triggers one re-login + re-fetch before giving up, covering stale-session shapes `_is_session_expired` misses. Advances R1.

**Dependencies:** U1 (shares `_get_home_page`/`list_snipes`; sequence to keep the diff coherent).

**Files:**
- `packages/gixen-cli/gixen_client.py` — `list_snipes` (and a small retry seam; do not change `_parse_snipe_table`'s parsing logic)
- `packages/gixen-cli/tests/test_gixen_client.py`

**Approach:** Wrap the parse in `list_snipes` so that on `GixenParseError`, if a re-login hasn't been attempted for this call, clear `session_id`, `login()`, re-fetch via `_get_home_page`, and parse once more; a second `GixenParseError` propagates. Keep the existing `_is_session_expired` fast-path in `_get_home_page` intact (it still short-circuits when it matches). Keep the BUI-114 "snipe table not found" body-snippet log. Do not let this retry stack on top of U1's GET retry into more than one extra login per `list_snipes` call.

**Patterns to follow:** `add_snipe`'s bounded "retry once then raise" structure; `_get_home_page`'s `retry_on_expired` one-shot discipline.

**Test scenarios:**
- First GET returns a non-table 200 (e.g., login page that `_is_session_expired` does NOT match), second GET returns a valid table → `list_snipes()` succeeds after one re-login.
- Both GETs return non-table bodies → raises `GixenParseError` (bounded, no loop).
- A valid table on the first GET → no re-login, single fetch (success path unchanged).
- Interaction with U1: a 500 then a non-table 200 then a valid table stays within the one-extra-login bound or fails cleanly (assert call counts).

**Verification:** New parse-recovery tests pass; success-path `list_snipes` still issues a single GET.

### U3. Verify-after-modify in `modify_snipe` + `GixenModifyNotConfirmedError`

**Goal:** `modify_snipe` confirms the change landed in Gixen before returning success; a silent drop is retried once then raised. Advances R2.

**Dependencies:** U1, U2 (verification re-lists, so it benefits from the recovered GET path).

**Files:**
- `packages/gixen-cli/gixen_client.py` — `modify_snipe`, new `GixenModifyNotConfirmedError(GixenError)` in the exception hierarchy
- `packages/gixen-cli/server/main.py` — confirm `api_edit_bid` maps the new error to 503 (it already catches `GixenError`; add an explicit branch only if a distinct message/log is wanted)
- `packages/gixen-cli/tests/test_gixen_client.py`, `packages/gixen-cli/tests/test_server_api.py`

**Approach:** After the modify POST, re-list and find the snipe; compare its `max_bid` against the requested value using Gixen's numeric formatting (compare as `Decimal`, tolerant of trailing-zero/format differences — mirror how `add_snipe` confirms presence). On mismatch/absence: respect `_min_post_gap`, re-POST once, re-verify; if still unconfirmed, raise `GixenModifyNotConfirmedError`. Reuse the snipe list already fetched at the top of `modify_snipe` only if it is still fresh enough to be meaningful — the *confirming* read must be **after** the POST, so it is a new list call. Keep the existing "Modified snipe" success log but move it to fire only after confirmation.

**Execution note:** Test-first for the confirmation logic — write the silent-drop test (POST "succeeds" but list still shows the old `max_bid`) before implementing, so the new control flow is pinned by a failing test.

**Patterns to follow:** `add_snipe` verify-and-retry loop (`gixen_client.py:445-506`) and `GixenAddNotConfirmedError`.

**Test scenarios:**
- POST then list shows the new `max_bid` → returns True, "Modified snipe" logged once. (Covers R2 happy path.)
- POST then list still shows the old `max_bid`, retry POST then list shows new → returns True after one retry.
- POST then list never shows the new value → raises `GixenModifyNotConfirmedError`; DB is not written.
- Snipe absent from the post-POST list → raises `GixenModifyNotConfirmedError` (not a generic crash).
- `max_bid` equality is format-tolerant (e.g., `40` vs `40.00`).
- `api_edit_bid`: when `modify_snipe` raises `GixenModifyNotConfirmedError`, response is 503 and `update_bid` is NOT called (DB unchanged). (Integration via TestClient.)

**Verification:** New confirmation tests pass; `test_edit_bid_gixen_error_returns_503` and `test_edit_bid_not_in_db_self_heals_via_sync` still pass; on a confirmed modify the DB write still happens exactly once.

### U4. Dashboard inline-edit: one silent 503 retry + persistent error state

**Goal:** A failed bid save is unmissable, and a single transient 503 recovers without user action. Advances R3.

**Dependencies:** none (frontend-only; independent of U1–U3 but logically lands after them).

**Files:**
- `packages/gixen-cli/server/static/index.html` — `saveMaxBid` (~531-584)

**Approach:** On a non-OK response with status 503, await a short delay and retry the PATCH once before entering the error branch. If the retry also fails, or the failure is non-503, render a persistent error state on the cell (revert the value to `dataset.currentMax`, keep a visible error affordance that stays until the user re-interacts, and keep the reason visible rather than hover-only). Preserve the existing `suppressRefresh` handling and the 30s `AbortController` timeout. Keep the success path (`saved` flash → `load()`) unchanged.

**Patterns to follow:** the existing `saveMaxBid` try/catch and class-toggling; existing `AbortController` timeout handling.

**Test scenarios:** `Test expectation: none — static dashboard JS with no JS test harness in this package (the dashboard has no existing unit tests).` Manual verification only: simulate a 503 (e.g., temporarily point the fetch at a failing item) and confirm (a) one automatic retry fires, (b) a persistent, visible error state remains after a hard failure, (c) the value reverts and no false "saved" state shows.

**Verification:** Manual: a forced 503 retries once then shows a persistent error; a forced success still flashes `saved` and refreshes. No console errors.

---

## Scope Boundaries

**In scope:** the four fixes above (GET-path 500 recovery, parse-failure recovery, verify-after-modify, dashboard resilience) and their tests.

**Out of scope (non-goals):**
- The `bids.status` lifecycle and `REMOVED`/`PURGED` tombstone handling — explicitly untouched (R4).
- The `add_snipe` confirmation path — already correct; only referenced as the pattern to mirror.

### Deferred to Follow-Up Work
- **Caching `dbidid` to remove the `list_snipes` GET from the edit hot path** — tracked as **BUI-116**. That is the structural fix; this plan makes the existing GET path resilient. BUI-116 depends on U3's verification as its safety net.
- **Hard-coding a specific stale-session HTML signature into `_is_session_expired`** once BUI-114's body capture records a real production failing body. KTD2 is robust without it; tightening detection later is an optimization, not a requirement.

---

## Risks & Dependencies

- **Risk: re-login loops / login amplification.** Each recovery path is bounded to one extra `login()` per call (`retry_on_expired=False` second pass; single parse-retry guard). Tests assert call counts to prevent regressions. `_min_post_gap` still throttles writes.
- **Risk: verify read adds Gixen load + latency to every edit.** Accepted — one extra GET per edit buys correctness, and the GET path is now resilient (U1/U2). The throttle keeps bursts safe.
- **Risk: `max_bid` comparison false-negatives** from formatting (`40` vs `40.00`) causing needless retries/failures. Mitigated by comparing as `Decimal` (KTD3 test scenario).
- **Dependency:** stacked on BUI-114 (body-capture logging at the same call sites — build on it, don't duplicate). Sequenced before BUI-116.

---

## Verification Strategy

Run from `packages/gixen-cli`: `uv run pytest -m "not integration"`. The full suite must stay green (currently 230 passed on the BUI-114 branch). New unit tests live in `test_gixen_client.py` (U1–U3) and `test_server_api.py` (U3 integration). U4 is verified manually against the running dashboard. Optional end-to-end smoke against the live Mac Mini server after deploy: issue a real edit and confirm a 200 with the new `max_bid` reflected in both the DB and Gixen.
