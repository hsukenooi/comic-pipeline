---
title: Plugin-owned read endpoints that mirror a host API without polluting the host
date: 2026-05-19
category: docs/solutions/best-practices
module: gixen-overlay
problem_type: best_practice
component: service_object
severity: medium
applies_when:
  - Building a plugin endpoint that joins plugin-owned tables with host-owned tables
  - Mirroring an existing host endpoint's response shape while adding domain-specific fields
  - Host endpoint depends on a freshness/sync contract (pull-on-visit) that the plugin must inherit
  - Aggregating SQLite columns where NULL components would silently understate a SUM
related_components:
  - database
  - tooling
  - development_workflow
tags:
  - fastapi
  - plugin-architecture
  - sqlite
  - conditional-aggregates
  - null-handling
  - cross-repo
  - api-mirroring
  - gixen-overlay
---

# Plugin-owned read endpoints that mirror a host API without polluting the host

## Context

PER-40 added a `/comics` dashboard to the `gixen-overlay` plugin. The dashboard needed two new endpoints, `/api/comics/snipes` and `/api/comics/history`, that mirror the shape of gixen-cli's existing `/api/snipes` and `/api/history` while joining the host's `bids` table with the plugin's `comics`, `fmv`, and `bid_fmvs` tables.

The constraint: gixen-cli is being prepped for OSS release and must stay comic-free, so all comic-aware columns, queries, and rendering live in the plugin. The plugin attaches to the host at boot via entry-point discovery; the host knows nothing about comics. But the plugin endpoints must preserve the host's freshness contract (background Gixen sync) and JSON shape (so JS render helpers are shared, not forked).

Several patterns and gotchas surfaced moving from review to a working implementation. This document captures them so the next plugin-owned read endpoint doesn't re-derive them.

## Guidance

### a. Plugin install + entry-point discovery

The plugin lives in its own subtree but installs into the host venv via `pip install -e` and registers under the `gixen.plugins` group. The host enumerates that group at boot, instantiates each plugin, and calls its hookimpls.

`plugins/gixen-overlay/pyproject.toml`:

```toml
[project.entry-points."gixen.plugins"]
gixen-overlay = "gixen_overlay.plugin:plugin"

[tool.hatch.build.targets.wheel]
packages = ["src/gixen_overlay"]

[tool.pytest.ini_options]
pythonpath = ["src", "/Users/hsukenooi/Projects/gixen-cli"]
```

`plugins/gixen-overlay/src/gixen_overlay/plugin.py`:

```python
class GixenOverlayPlugin:
    @hookimpl
    def register_db_tables(self, conn: sqlite3.Connection) -> None:
        create_tables(conn)

    @hookimpl
    def register_routes(self, app: "FastAPI") -> None:
        from gixen_overlay.routes import router
        app.include_router(router)

    @hookimpl
    def register_dashboard_tabs(self) -> list[dict]:
        return [{"label": "comics", "path": "/comics"}]

plugin = GixenOverlayPlugin()
```

Because the install is editable, code changes to the plugin take effect on host restart — no `pip install` step in CI/deploy beyond the initial wiring.

### b. Importing private host helpers for contract parity

`/api/comics/snipes` must trigger the same background Gixen sync as `/api/snipes`. The plugin imports the host's private helpers directly:

`plugins/gixen-overlay/src/gixen_overlay/routes.py`:

```python
from server.db import get_bid_by_item_id
from server.main import _ensure_fresh_sync, _iso_to_relative, _spawn_fallback_task

@router.get("/api/comics/snipes")
async def api_comics_snipes(request: Request):
    """Same pull-on-visit + fallback semantics as gixen-cli's /api/snipes."""
    await _ensure_fresh_sync()
    _spawn_fallback_task()
    ...
```

This is a deliberate, named cross-repo coupling. The alternative — calling `/api/snipes` from JS for its side-effect, then `/api/comics/snipes` for enrichment, and merging client-side — was rejected because it doubles network calls and complicates JS merging logic.

The coupling is made explicit by the host helpers being underscore-prefixed: importing them is a knowing "I am consuming a host internal" act, not an accident. But knowing isn't safe by itself. Two cheap guardrails are worth adding when the coupling matters:

- **Pin a known-good host commit** in the plugin's deploy notes so a host refactor doesn't silently break the plugin between checkouts.
- **Add an import-time smoke test** in the plugin's test suite that exercises the imported symbols. A rename or signature change on the host then surfaces as a CI failure on the plugin side, not as a 500 in production.

Better still, push the host to expose a small public surface for the contract (e.g., a `gixen.sync.ensure_fresh()` API that plugins are explicitly invited to depend on) and switch off the underscore imports once available.

### c. Numeric companion fields for formatted-string mirror responses

gixen-cli's `/api/snipes` serializes `max_bid` as a pre-formatted string:

```python
"max_bid": f"{item['max_bid']:.2f} USD",  # "475.00 USD"
```

The host's JS works around the string with a regex helper (`parseAmt`) every time it needs to do arithmetic. A plugin endpoint that emits the same shape inherits that papercut: `max_bid - winning_bid` in JS is `NaN`.

Fix: emit numeric companion fields alongside the formatted ones, and mirror the parse logic server-side so server-side math (e.g., `value_pct`) sees the same numbers the client does.

```python
_NUMERIC_RE = re.compile(r"[^0-9.]")

def _parse_current_bid(value: str | None) -> float | None:
    """Mirrors the JS `parseAmt` in server/static/index.html.

    Strips everything except digits and dots — same behavior as the client
    helper. Non-negative inputs only: a minus sign would be stripped along
    with the rest, so "−$10" parses as 10.0. That's fine for eBay current
    bids (always non-negative) but unsafe for any field that can be negative
    (refunds, credits) — use a more deliberate parser there.
    """
    if value is None:
        return None
    cleaned = _NUMERIC_RE.sub("", str(value))
    if not cleaned or cleaned == ".":
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None

# In the row builder:
return {
    "max_bid": f"{item['max_bid']:.2f} USD",     # kept for shape parity
    "current_bid": item.get("cached_current_bid"),
    "max_bid_numeric": item["max_bid"],          # raw float
    "current_bid_numeric": _parse_current_bid(item.get("cached_current_bid")),
    ...
}
```

Tests should cover both shapes — the formatted string for render parity, the numeric for math.

### d. Conditional aggregates for primary-flagged + lot-wide values in one pass

The bid → fmv junction (`bid_fmvs`) has an `is_primary` flag picking out one row of the lot. The dashboard needs both the primary's grade AND lot-wide FMV sums. A naive plan reaches for `MAX(fmv.grade) WHERE bid_fmvs.is_primary=1` inside a `GROUP BY` — that's invalid SQL.

The right pattern is a conditional aggregate inside `MAX(CASE WHEN ... THEN ... END)`, which lets one GROUP BY query emit both the primary-flagged scalar and the lot-wide sums:

```python
_COMICS_AGGREGATES = """
    MAX(CASE WHEN bf.is_primary = 1 THEN f.grade END) AS primary_grade,
    SUM(f.low) AS fmv_low_sum,
    SUM(f.high) AS fmv_high_sum,
    COUNT(bf.fmv_id) AS lot_count,
    SUM(CASE WHEN bf.fmv_id IS NOT NULL AND f.low IS NULL THEN 1 ELSE 0 END) AS fmv_low_null_count,
    SUM(CASE WHEN bf.fmv_id IS NOT NULL AND f.high IS NULL THEN 1 ELSE 0 END) AS fmv_high_null_count
"""

rows = db.execute(f"""
    SELECT b.*, {_COMICS_AGGREGATES}
    FROM bids b
    LEFT JOIN bid_fmvs bf ON bf.bid_id = b.id
    LEFT JOIN fmv f ON f.id = bf.fmv_id
    WHERE b.status != 'PURGED'
    GROUP BY b.id
    ORDER BY b.added_at DESC
""").fetchall()
```

This pattern replaces a correlated subquery or a second roundtrip. Factor the aggregate clause into a constant so the two endpoints (`/api/comics/snipes` and `/api/comics/history`) reuse it verbatim — drift between the two would silently produce inconsistent enrichment.

### e. SUM-NULL gotcha + detection + null-the-aggregate decision

SQLite's `SUM()` silently skips NULL components. For a lot of 5 comics where one has `fmv.low IS NULL`, `SUM(fmv.low)` returns the sum of the other 4 — no signal that the aggregate is incomplete. A downstream "% of FMV" indicator would render green ("screaming deal!") when really one comic is unpriced.

Detection rides alongside the SUM as a conditional count:

```sql
SUM(CASE WHEN bf.fmv_id IS NOT NULL AND f.low IS NULL THEN 1 ELSE 0 END) AS fmv_low_null_count
```

The row builder then nulls the entire aggregate when any lot component is unpriced:

```python
# FMV aggregation rules:
# - unlinked: both null
# - lot (N>=2): null both when any component is unpriced — avoids silent
#   understatement (SQLite SUM ignores NULLs and would produce a partial sum)
# - single comic (N==1): keep whatever bound exists; value_pct guards
#   against the partial-bound case separately
if needs_linking:
    fmv_low = None
    fmv_high = None
elif lot_count >= 2 and (item["fmv_low_null_count"] or item["fmv_high_null_count"]):
    fmv_low = None
    fmv_high = None
else:
    fmv_low = item["fmv_low_sum"]
    fmv_high = item["fmv_high_sum"]
```

Refusing to show a partial number is the right move for a money signal. "I don't know" is honest; an understated number is misleading.

### f. Filter parity with host endpoints

The first plan for `/api/comics/snipes` filtered on `status != 'PURGED' AND auction_end_at > now`. But gixen-cli's `/api/snipes` filters only on `status` — the active/ended split happens client-side via `isEnded(r)` in JS, so a snipe that ages past T=0 mid-session stays in the same payload and gets re-bucketed by the next render.

If the plugin filters by end-date server-side, a row aging past T=0 disappears entirely: it's no longer in `/api/comics/snipes`, and `/api/comics/history` only catches up on the next 30s refresh. The user sees a row blink out and come back.

Mirror the host's filter exactly so the JS partitioning continues to work:

```python
WHERE b.status != 'PURGED'
```

And for history, mirror the host's `MAX(id) per item_id` dedup so a re-added snipe appears once. **Apply the `status != 'PURGED'` filter here too** — see the caution below:

```python
INNER JOIN (
    SELECT item_id, MAX(id) AS max_id
    FROM bids
    WHERE status != 'PURGED'
      AND (
        (
          auction_end_at IS NOT NULL
          AND datetime(auction_end_at) <= datetime('now')
          AND datetime(auction_end_at) >= datetime('now', '-7 days')
        ) OR (
          auction_end_at IS NULL
          AND resolved_at IS NOT NULL
          AND datetime(resolved_at) >= datetime('now', '-7 days')
        )
      )
    GROUP BY item_id
) latest ON b.id = latest.max_id
```

Rule of thumb: when mirroring a host endpoint, copy the host's `WHERE`/dedup verbatim. Bucketing belongs on the client because the client is what sees time tick past T=0.

> **Caution (added 2026-06-01, BUI-50):** earlier revisions of this doc showed the
> history dedup subquery *without* `WHERE status != 'PURGED'`. That omission is the
> exact bug behind BUI-50 — soft-deleted (removed) snipes leaked into "recently
> ended" and the client painted them "won". PURGED is a soft-delete tombstone, not a
> terminal auction outcome; it must be filtered on **both** the active and history
> endpoints. See `../ui-bugs/purged-snipes-shown-as-won-2026-06-01.md`.

### g. Clamp `lot_count - 1` to non-negative

`cond_extra_count = lot_count - 1` is meant to be "extras beyond the primary." But when the bid is unlinked, `lot_count == 0`, so the raw expression returns `-1`. The UI then renders "−1 others" or worse.

```python
"cond_extra_count": max(0, lot_count - 1),
```

Cheap defensive arithmetic. Always clamp `n - 1` style derivations when `n` comes from a `COUNT()` that can legitimately be zero.

## Why This Matters

- **(a)** Wrong here means the host can't see the plugin and the dashboard 404s. Editable install means a forgotten `pip install -e` after a fresh checkout is a recurring papercut for new contributors — name it in the onboarding step.
- **(b)** Skip the host's `_ensure_fresh_sync` import and your endpoint returns stale data — the user sees a bid that ended 3 minutes ago still listed as active until they manually refresh `/api/snipes` from another tab.
- **(c)** Drop the numeric companions and every consumer downstream pays the parsing tax. JS `max - winning_bid` becomes `NaN`, the spread column shows "NaN", screenshots get posted in Slack, you debug for an hour, then add `parseAmt` to a fifth file.
- **(d)** Without conditional aggregates you do this as two queries or a subquery, and the two queries drift (one filters `is_primary=1`, the other doesn't). One pass with `MAX(CASE WHEN ...)` is one source of truth.
- **(e)** Worst-case outcome: a lot where 4 of 5 comics are priced at $100 (low) but the 5th is unpriced. `SUM(low)` returns $400. Current bid is $300. `value_pct` renders 75% — "great deal!" — when the real low bound is unknown and the bid could be 60% of true value. You buy bad lots because the signal lied.
- **(f)** A row aging mid-session disappears from the dashboard. The user thinks the system crashed or they imagined the snipe. They go look in the wrong place. Active/ended is a render-time concept; treat it that way.
- **(g)** "−1 others" is the kind of UI bug that erodes trust in everything else on the page.

## When to Apply

This pattern fits when:

- The host application supports a plugin model via Python entry points and exposes a hookspec for routes/DB/dashboards (gixen-cli's `gixen.plugins` group is the local instance; the same shape works for any pluggy- or stevedore-style host).
- The plugin owns auxiliary tables that join with host-owned tables, and the plugin needs to surface that joined view as its own endpoint.
- The plugin endpoint is intended to mirror the contract of a sibling host endpoint — same freshness semantics, same JSON shape modulo enrichment — so the host's frontend can share render helpers between the two.
- The host is being kept domain-agnostic (e.g., for OSS prep) and cannot grow comic/domain-specific columns.

Less applicable when the plugin is fully standalone (its own DB, own frontend) — at that point the cross-repo helper imports become unjustified coupling and the plugin should be a separate service.

## Examples

### SQL aggregation: before/after

**Before (invalid pseudo-SQL from the original plan):**

```sql
SELECT b.*,
       MAX(f.grade) WHERE bf.is_primary = 1 AS primary_grade,
       SUM(f.low) AS fmv_low_sum
FROM bids b
LEFT JOIN bid_fmvs bf ON bf.bid_id = b.id
LEFT JOIN fmv f ON f.id = bf.fmv_id
GROUP BY b.id
```

SQLite rejects this — `WHERE` cannot follow an aggregate.

**After (conditional aggregate, factored constant):**

```python
_COMICS_AGGREGATES = """
    MAX(CASE WHEN bf.is_primary = 1 THEN f.grade END) AS primary_grade,
    SUM(f.low) AS fmv_low_sum,
    SUM(f.high) AS fmv_high_sum,
    COUNT(bf.fmv_id) AS lot_count,
    SUM(CASE WHEN bf.fmv_id IS NOT NULL AND f.low IS NULL THEN 1 ELSE 0 END) AS fmv_low_null_count,
    SUM(CASE WHEN bf.fmv_id IS NOT NULL AND f.high IS NULL THEN 1 ELSE 0 END) AS fmv_high_null_count
"""
```

Used verbatim in both `/api/comics/snipes` and `/api/comics/history` so they cannot drift.

### Numeric companions: before/after

**Before (mirror the host shape directly):**

```python
return {
    "max_bid": f"{item['max_bid']:.2f} USD",
    "current_bid": item.get("cached_current_bid"),
    "fmv_low": fmv_low,
    "fmv_high": fmv_high,
}
```

Every JS consumer that wants `max_bid - current_bid` must regex-strip both first.

**After (add numerics alongside the formatted strings, server computes `value_pct` from the same parsed value):**

```python
max_bid_numeric = item["max_bid"]
current_bid_numeric = _parse_current_bid(item.get("cached_current_bid"))

value_pct = None
if (
    lot_count == 1
    and fmv_low is not None
    and fmv_high is not None
    and current_bid_numeric is not None
):
    midpoint = (fmv_low + fmv_high) / 2
    if midpoint > 0:
        value_pct = current_bid_numeric / midpoint * 100

return {
    "max_bid": f"{item['max_bid']:.2f} USD",
    "current_bid": item.get("cached_current_bid"),
    "max_bid_numeric": max_bid_numeric,
    "current_bid_numeric": current_bid_numeric,
    "value_pct": value_pct,
    ...
}
```

Server and client now agree on the numeric value because both paths run through `_parse_current_bid` / `parseAmt` with the same regex.

### Filter parity: before/after

**Before (server-side end-date filter — rows blink out mid-session):**

```sql
WHERE b.status != 'PURGED' AND datetime(b.auction_end_at) > datetime('now')
```

**After (mirror host's `/api/snipes`, let JS partition):**

```sql
WHERE b.status != 'PURGED'
```

The JS `isEnded(r)` helper handles the active/ended split on every render.

### `lot_count - 1` clamp

**Before:** `"cond_extra_count": lot_count - 1` → renders "−1 others" for unlinked bids.

**After:** `"cond_extra_count": max(0, lot_count - 1)`.

## Related

- General case of §(b)'s import-time smoke test: [docs/solutions/developer-experience/cross-package-regressions-escape-per-package-test-runs.md](../developer-experience/cross-package-regressions-escape-per-package-test-runs.md) — the repo-wide version of "a host-side contract change should fail on the consumer's side in CI, not in production": grep all consumers on a signature change, keep mocks mirroring the real return shape, grep source-text guard tests before relocating, and gate on the full CI matrix.
- Brainstorm: [docs/brainstorms/2026-05-19-comics-dashboard-requirements.md](../../brainstorms/2026-05-19-comics-dashboard-requirements.md)
- Plan: [docs/plans/2026-05-19-002-feat-comics-dashboard-plan.md](../../plans/2026-05-19-002-feat-comics-dashboard-plan.md)
- Sibling SQLite gotcha (DDL/migration scope, not DQL): [docs/solutions/database-issues/sqlite-fk-rename-savepoint-pragma-2026-05-19.md](../database-issues/sqlite-fk-rename-savepoint-pragma-2026-05-19.md)
- Linear: [PER-40](https://linear.app/hk-iterative/issue/PER-40)
- Squash-merged in [PR #5](https://github.com/hsukenooi/comic-pipeline/pull/5)
