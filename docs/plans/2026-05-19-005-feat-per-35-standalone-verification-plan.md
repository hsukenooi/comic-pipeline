---
title: "feat: PER-35 Standalone gixen-cli Verification"
type: feat
status: active
date: 2026-05-19
---

# feat: PER-35 Standalone gixen-cli Verification

## Overview

Add automated tests that verify gixen-cli behaves correctly when installed with no comic plugin present — the baseline experience for a generic Gixen user who has never heard of comics.

## Problem Frame

After extracting the comic overlay into comic-pipeline, gixen-cli must still work as a standalone tool for non-comic users. The risk: residual imports, route registrations, or dashboard tab references may depend on comic-specific code, causing the server to fail or expose comic UI to users who did not install the plugin.

## Requirements Trace

- R1. Generic snipe CRUD works when no plugin is installed
- R2. Dashboard renders exactly the `Bids` tab and no comic-specific tabs
- R3. No routes under `/api/comics`, `/api/extract-comics`, or comic-specific paths are accessible
- R4. Server starts without error when `gixen.plugins` entry-point group is empty

## Scope Boundaries

- Does not test the comic plugin itself (covered by PER-34)
- Does not test with an active Gixen session (integration tests handle that)
- No live server start required — tests use FastAPI TestClient

## Implementation Units

- [ ] **Unit 1: No-plugin server behavior tests**

**Goal:** Automated assertions that the server starts clean and comic endpoints are absent without any plugin

**Requirements:** R1, R2, R3, R4

**Dependencies:** None — runs against existing server code with no plugin installed

**Files:**
- Create: `tests/test_standalone_server.py`

**Approach:**
- Use FastAPI TestClient with an in-memory SQLite DB (matching pattern in test_server_api.py)
- Patch `gixen.plugins.load_plugins` to return an empty list (simulates no plugin installed)
- Assert `GET /api/dashboard-tabs` returns `[]` (only the built-in Bids tab, but that is rendered by the dashboard HTML, not this endpoint — the endpoint returns plugin tabs, so empty is correct)
- Assert `GET /api/snipes`, `POST /api/snipes`, `DELETE /api/snipes/{id}` work (snipe CRUD)
- Assert `GET /api/comics` returns 404 (not registered without plugin)
- Assert `POST /api/extract-comics` returns 404
- Assert server startup lifespan runs without error when plugin list is empty

**Patterns to follow:**
- `tests/test_server_api.py` — TestClient setup, in-memory DB fixture pattern
- `tests/test_plugin_integration.py` — plugin mock pattern using `patch`

**Test scenarios:**
- Happy path: `GET /api/dashboard-tabs` → 200 with `[]` (no plugin tabs registered)
- Happy path: `GET /api/snipes` → 200 (snipe routes still work without plugin)
- Error path: `GET /api/comics` → 404 (comic routes not registered without plugin)
- Error path: `POST /api/extract-comics` → 404
- Integration: server lifespan completes without exception when `load_plugins()` returns `[]`

**Verification:**
- `pytest tests/test_standalone_server.py` passes with zero failures
- Full suite `pytest tests/ --ignore=tests/test_integration.py` still passes

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| Comic routes still present in server/main.py from partial PER-30 work | Test will reveal this — failing test is the correct outcome; fix is to complete PER-30 |
| TestClient doesn't invoke lifespan by default in some FastAPI versions | Use `with TestClient(app) as client:` context manager which triggers lifespan |

## Sources & References

- Related plan: `docs/plans/2026-05-19-004-feat-per-34-e2e-test-plan.md`
- Existing patterns: `tests/test_server_api.py`, `tests/test_plugin_integration.py`
