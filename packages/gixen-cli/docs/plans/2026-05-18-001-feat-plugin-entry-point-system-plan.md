---
title: PER-25 — Add Plugin Entry-Point System to gixen-cli
type: feat
status: active
date: 2026-05-18
deepened: 2026-05-18
origin: docs/refactor-split-handoff.md
linear: https://linear.app/hk-iterative/issue/PER-25
---

# PER-25 — Add Plugin Entry-Point System to gixen-cli

## Overview

Introduce a Python entry-point plugin system (`gixen.plugins` group) so external packages can extend the gixen-cli FastAPI server. This is the foundational issue of the larger refactor that splits comic-specific code out of `gixen-cli` into a new `comic-pipeline` repo (see origin: `docs/refactor-split-handoff.md`).

PER-25 ships the minimum viable contract — three hooks plus a loader — and bootstraps `pyproject.toml` from scratch since none exists today. PER-26/27/28 then refactor core code to call into the hook surface. PER-30 extracts the comic overlay against this contract and is explicitly designed to surface protocol gaps; additional hooks (CLI commands, model extension, response enrichment) are deferred to PER-30 discovery rather than designed speculatively here.

## Problem Frame

The gixen-cli repo today is a generic-Gixen-sniping-tool *with comic-specific code grafted into it* (see origin for current state inventory). To extract the comic code cleanly into a separate repo, we need a sanctioned extension point. The chosen mechanism is a Python entry-point plugin API (see origin: "Key Architectural Decisions"):

- In-process Python entry points under the `gixen.plugins` group (not HTTP/RPC/subprocess)
- Plugins register via `pyproject.toml` and are loaded at FastAPI startup
- Shared SQLite file; plugins own and namespace their own tables

The repo has zero existing plugin infrastructure and no `pyproject.toml`. PER-25 is therefore three things: (1) introduce packaging, (2) introduce the plugin protocol, (3) wire the loader into the existing FastAPI lifespan.

## Requirements Trace

From the Linear issue (PER-25) and origin document:

- **R1.** A `gixen.plugins` entry-point group is defined and discoverable.
- **R2.** A loader runs at FastAPI startup that discovers all installed plugins.
- **R3.** The plugin protocol exposes three hooks: `register_routes(app: FastAPI)`, `register_db_tables(conn: sqlite3.Connection)`, `register_dashboard_tabs() -> list[dict]`.
- **R4.** Tests verify plugin discovery works (no-plugins case, single-plugin case, multiple-plugin case, plugin import failure case).
- **R5.** The repo is pip-installable (new `pyproject.toml`); the existing `python cli.py` and `uvicorn server.main:app` workflows continue to work unchanged for users who don't pip-install.
- **R6.** The existing in-repo comic code (which will be extracted in PER-30) continues to function during the transition — PER-25 does not break the running server.

## Scope Boundaries

In scope:
- `pyproject.toml` bootstrap with hatchling backend
- `gixen.plugins` entry-point group declaration
- Plugin protocol via pluggy hookspec (3 hooks only)
- Loader inside `server/main.py` lifespan
- Tests for discovery, no-plugins, error isolation, ordering
- Minimal docstring-level documentation for plugin authors

Out of scope (these are PER-26/27/28/30):
- Refactoring existing routes to use `APIRouter` (PER-26)
- Moving the `comics`/`bid_comics` tables out of core `_SCHEMA` (PER-27)
- Building a dashboard tab framework that renders `register_dashboard_tabs()` output (PER-28)
- Extracting the comic overlay code (PER-30)

### Deferred to Separate Tasks

- **CLI command registration hook** (`register_cli_commands(group: click.Group)`): cli.py has comic surface today; PER-30 will determine whether to add this hook or split the CLI binary. Not in PER-25's 3-hook contract.
- **Pydantic model extension hook**: AddBidRequest/EditBidRequest have comic fields; PER-30 surfaces this gap.
- **Response enrichment hook** (`enrich_snipes(rows)` or similar): `/api/snipes`, `/api/history`, `/api/bids` join comics today; PER-30 surfaces this gap.
- **Dashboard tab framework / topbar nav rendering**: PER-28. PER-25 only defines the `register_dashboard_tabs()` hook signature; nothing consumes the returned data yet.

## Context & Research

### Relevant Code and Patterns

- `server/main.py:550-597` — existing `@asynccontextmanager lifespan(app)` is the canonical mount point for the loader. Plugin hooks fire after `init_db()` and before `yield`.
- `server/main.py:600` — `app = FastAPI(lifespan=lifespan)` construction.
- `server/db.py:97-111` — `init_db()` opens the SQLite connection, runs `_SCHEMA`, then `_apply_migrations`. The connection passed to `register_db_tables(conn)` is the same one used by core routes.
- `tests/test_server_api.py:21-31` — TestClient fixture pattern with `monkeypatch.setenv("DB_PATH", ...)` before importing `server.main`. PER-25 tests follow this pattern.
- `tests/test_server_db.py:18-22` — direct `init_db(tmp_path / "test.db")` fixture pattern for DB-level unit tests.
- `tests/conftest.py` — minimal (only the `integration` marker). No shared fixtures today; PER-25 adds a `make_plugin_manager` fixture.

### Institutional Learnings

None — no `docs/solutions/` directory exists in this repo. Worth capturing the pluggy/entry-points patterns we learn here after PER-25/PER-30 land.

### External References

- [Python 3.14 `importlib.metadata.entry_points()`](https://docs.python.org/3.14/library/importlib.metadata.html) — selectable API: `entry_points(group="gixen.plugins")`. Returns `EntryPoints` collection. Dict-style return is gone since Python 3.12.
- [`pluggy` 1.6.0 documentation](https://pluggy.readthedocs.io/en/stable/) — hookspec/hookimpl markers, `PluginManager.load_setuptools_entrypoints()`, LIFO ordering, `tryfirst`/`trylast` escape hatches.
- [Datasette plugin hooks](https://docs.datasette.io/en/stable/plugin_hooks.html) — closest prior art (long-running ASGI web service with pluggy-based plugin system, ~50 hooks).
- [FastAPI lifespan events](https://fastapi.tiangolo.com/advanced/events/) — official lifecycle. Note: `include_router()` / `app.mount()` inside lifespan works in practice but isn't documented as a supported pattern.
- [PEP 621 `pyproject.toml`](https://packaging.python.org/en/latest/specifications/pyproject-toml/) — `[project.entry-points."gixen.plugins"]` syntax. Group names with dots require TOML quoting.
- [Hatchling docs](https://hatch.pypa.io/latest/config/build/) — `[tool.hatch.build.targets.wheel]` packages + include syntax for mixed flat layout.

### Slack Context

Not gathered — user did not request Slack search for this task. Slack tools are available; ask in a follow-up turn if organizational context becomes relevant.

## Key Technical Decisions

- **Use `pluggy`, not a roll-your-own loader.** Rationale: Datasette (the closest prior art) uses pluggy; pytest plugins everyone has transitively installed; gives us `tryfirst`/`trylast`/`historic`/`firstresult`/tracing for free. Costs one dependency. Avoids re-implementing features we'd inevitably need within 6 months. **One-way decision:** once plugins (like `gixen-overlay` in PER-30) start importing `from gixen.plugins import hookimpl`, swapping pluggy out becomes a breaking change for every external plugin. If we wanted to evaluate alternatives, now is the time.
- **Build backend: `hatchling`.** Rationale: cleanest handling of the mixed flat layout (`cli.py` + `server/` + new `gixen/`). Used by FastAPI/httpx/Starlette/Rich. Setuptools auto-discovery refuses this layout; flit-core can't do multi-package; poetry-core drags Poetry conventions.
- **New top-level package: `gixen/`.** Hosts the hookspec and loader (`gixen/plugins.py`, `gixen/__init__.py`). Keeps the plugin API surface namespaced and importable by plugin authors as `from gixen.plugins import hookimpl`. Avoids polluting `server/` with packaging-level concerns.
- **Per-plugin error isolation, log + continue.** Rationale: pytest, Datasette, MkDocs all do this. Fail-fast on a single bad plugin makes the host unusable in production after any plugin regression. Each `ep.load()` and `pm.register()` is wrapped in try/except; failures log and skip.
- **Deterministic ordering by entry-point name.** Default `entry_points()` order is `sys.path` order — not stable across machines. Sort by `ep.name` before registering. Plugins needing explicit ordering use `@hookimpl(tryfirst=True)` / `trylast=True`.
- **Versioning via host SemVer.** Plugins declare `dependencies = ["gixen-cli>=0.X,<0.Y"]` in their `pyproject.toml`. No separate `__plugin_api_version__` symbol — `pyproject.toml` already does the job, and plugin authors won't reliably update a bespoke version field. This resolves the origin doc's open question on plugin-boundary versioning (see origin: `docs/refactor-split-handoff.md` "Open Questions / Things Not Yet Decided" → "Versioning strategy across the plugin boundary").
- **Re-export `pluggy` markers from `gixen.plugins`.** Plugin authors `from gixen.plugins import hookimpl` rather than depending on pluggy directly. Mirrors what Datasette does. Lets us swap or wrap the framework later without breaking plugins.
- **Loader runs inside the existing lifespan, ordered: load → `register_db_tables(conn)` (per-plugin with savepoints) → `register_routes(app)` (bulk) → `register_dashboard_tabs()` (bulk, flatten results) → invalidate `app.openapi_schema`.** Per-plugin dispatch is reserved for `register_db_tables` only (savepoints are the only thing that genuinely needs it). Routes and tabs use bulk `pm.hook.X(...)` calls — if one plugin fails, pluggy halts the chain and the host logs at ERROR. This is a loud-failure design: it's easier to debug than partial-registration silence.
- **Plugin name convention: `gixen-<plugin>`.** Per pluggy's documented convention. First plugin will be `gixen-overlay` (the comic plugin in PER-30).
- **Test plugins via direct `pm.register(FakePlugin())`, not via installed entry points.** Avoids the slow/fragile route of pip-installing fake distributions in test fixtures. Add one monkeypatched-`entry_points` test for the discovery path specifically.

## Open Questions

### Resolved During Planning

- **pluggy vs roll-your-own** → pluggy (see Key Technical Decisions).
- **Build backend** → hatchling.
- **Where does the loader live** → new `gixen/plugins.py` module.
- **What happens if a plugin's `register_routes` conflicts with a core route** → no special handling. FastAPI accepts duplicate path registrations (last write wins). Silent rollback would hide configuration bugs that the operator should notice. If conflict becomes a real problem in practice (post-PER-30), add detection then; don't speculate now.
- **Error posture for `register_db_tables` failures** → each plugin's DDL runs inside a SQLite savepoint (`SAVEPOINT plugin_<name>` / `RELEASE` or `ROLLBACK TO`). Failure rolls back that plugin's DDL only, doesn't poison the connection for other plugins or core.
- **Plugin name uniqueness** → enforce uniqueness via pluggy's `pm.register(plugin, name=ep.name)` which raises on duplicate names. The loader catches and skips duplicates with a warning.

### Deferred to Implementation

- **Exact tab dict shape.** `register_dashboard_tabs() -> list[dict]` returns a list of plain dicts in PER-25. PER-28 builds the consumer (topbar renderer) and will define the exact field set then — likely something like `{"slug", "label", "key", "route"}` but that's PER-28's call, not PER-25's. PER-25 deliberately doesn't define a `TabSpec` type to avoid baking in a shape no one consumes yet.
- **Whether `register_routes` should pass `app` directly or a scoped registration helper.** The handoff says `register_routes(app: FastAPI)`. We will follow that signature, but if PER-30 reveals plugins need richer registration (e.g. mounting static files, adding dependencies), PER-30 surfaces it as a protocol change.

## High-Level Technical Design

> *This illustrates the intended approach and is directional guidance for review, not implementation specification. The implementing agent should treat it as context, not code to reproduce.*

```
                        ┌─────────────────────────────────┐
                        │ pyproject.toml                  │
                        │   [project.entry-points         │
                        │     ."gixen.plugins"]           │
                        │   # external plugins declare    │
                        │   # entries here                │
                        └────────────────┬────────────────┘
                                         │
                                         ▼
   server/main.py                  gixen/plugins.py
   @asynccontextmanager            ┌───────────────────────────────┐
   async def lifespan(app):        │ hookspec = HookspecMarker("gixen")│
       conn = init_db(...)         │ hookimpl = HookimplMarker("gixen")│
       pm = load_plugins() ──────► │                               │
       # per-plugin w/ savepoints  │ class GixenPluginSpec:        │
       for name, p in pm.list_     │   @hookspec register_routes   │
            name_plugin():         │   @hookspec register_db_tables│
         SAVEPOINT sp_<name>       │   @hookspec register_dashboard│
         subset_hook_caller(       │             _tabs             │
           "register_db_tables")(  │                               │
             conn=conn)            │ def make_plugin_manager():    │
         RELEASE or ROLLBACK       │   pm = PluginManager("gixen") │
       # bulk                      │   pm.add_hookspecs(...)       │
       pm.hook.register_routes(    │   return pm                   │
            app=app)               │                               │
       app.state.dashboard_tabs =  │ def load_plugins():           │
         flat(pm.hook              │   pm = make_plugin_manager()  │
           .register_dashboard_    │   for ep in sorted(           │
           tabs())                 │       entry_points(           │
       app.openapi_schema = None   │         group="gixen.plugins")│
       yield                       │       , key=lambda e: e.name):│
                                   │     try: pm.register(...)     │
                                   │     except: log + continue    │
                                   │   pm.check_pending()          │
                                   │   return pm                   │
                                   └───────────────────────────────┘

   External plugin package (e.g. comic-pipeline/plugins/gixen-overlay/)
   ┌───────────────────────────────────────────────────────────┐
   │ pyproject.toml                                            │
   │   [project.entry-points."gixen.plugins"]                  │
   │   overlay = "gixen_overlay.plugin"                        │
   │                                                           │
   │ gixen_overlay/plugin.py                                   │
   │   from gixen.plugins import hookimpl                      │
   │   @hookimpl                                               │
   │   def register_routes(app): app.include_router(...)       │
   │   @hookimpl                                               │
   │   def register_db_tables(conn): conn.executescript(...)   │
   │   @hookimpl                                               │
   │   def register_dashboard_tabs(): return [{...}]           │
   └───────────────────────────────────────────────────────────┘
```

## Output Structure

```
gixen-cli/
├── pyproject.toml                 NEW — hatchling, project metadata, dependencies
├── requirements.txt               DELETE — superseded by pyproject.toml
├── gixen/                         NEW — top-level plugin API package
│   ├── __init__.py                NEW — empty namespace marker
│   └── plugins.py                 NEW — hookspec class, loader, plugin manager factory
├── server/
│   ├── install.sh                 MODIFY — pip command swapped to `pip install -e .`
│   └── main.py                    MODIFY — wire load_plugins into lifespan
└── tests/
    ├── conftest.py                MODIFY — add make_plugin_manager / fake_entry_points fixtures
    └── test_plugins.py            NEW — discovery, error isolation, ordering tests
```

## Implementation Units

- [ ] **Unit 1: Bootstrap `pyproject.toml` for the host package**

**Goal:** Make `gixen-cli` a pip-installable package so external plugin packages can declare it as a dependency and `importlib.metadata.entry_points(group="gixen.plugins")` returns selectable results.

**Requirements:** R1, R5

**Dependencies:** None — this is the foundation for everything else.

**Files:**
- Create: `pyproject.toml`
- Delete: `requirements.txt`
- Modify: `server/install.sh` (pip command only — replace `pip install -r requirements.txt` with `pip install -e .`)

**Approach:**
- Backend: `hatchling`. `[build-system] requires = ["hatchling"]`, `build-backend = "hatchling.build"`.
- `[project]` metadata: `name = "gixen-cli"`, `version = "0.3.0"` (next minor after current state), `requires-python = ">=3.14"`, description from CLAUDE.md, readme = "README.md".
- `dependencies`: copy from existing `requirements.txt` — `click`, `requests`, `python-dotenv`, `fastapi`, `uvicorn[standard]`, `httpx`, `pydantic`, `playwright` — plus the new `pluggy>=1.6`.
- `[project.scripts]`: `gixen = "cli:cli"` so `gixen` is on PATH after install. The existing `python cli.py` invocation continues to work.
- No host `[project.entry-points."gixen.plugins"]` stanza — the host registers no plugins into itself. The group is implicit; external plugins (like `gixen-overlay` in PER-30) are where the stanza appears.
- `[tool.hatch.build.targets.wheel]`: `packages = ["server", "gixen"]`, `include = ["cli.py", "gixen_client.py", "ebay_bidder.py"]`. Mixed flat layout: top-level modules listed explicitly because hatchling won't auto-discover them.
- `[tool.hatch.build.targets.sdist]`: include the same plus `tests/`, README, CLAUDE.md.
- **Delete `requirements.txt`** and update `server/install.sh` to `pip install -e .` so the two install paths can't diverge. This is the minimum change to keep `install.sh` working; broader polish of the script (PyPI install, version pinning) is deferred to PER-37. The `dev` extras for `pytest` etc. are also deferred to PER-37 — developers can `pip install pytest` directly in the meantime.

**Patterns to follow:**
- Reference the FastAPI repo's `pyproject.toml` (hatchling-based, similar flat-vs-package mix).

**Test scenarios:**
- Test expectation: none — pure packaging configuration. Verification covers correctness.

**Verification:**
- `python -m pip install -e .` succeeds inside the repo's venv.
- `python -c "from importlib.metadata import entry_points; print(list(entry_points(group='gixen.plugins')))"` returns `[]` (empty selectable, but the call succeeds).
- `gixen --help` produces the same output as `python cli.py --help`.
- `python cli.py --help` continues to work (existing workflow unchanged).
- `uvicorn server.main:app` continues to start (existing workflow unchanged).

---

- [ ] **Unit 2: Define hookspec and plugin manager factory**

**Goal:** Establish the public plugin contract. Plugin authors import from `gixen.plugins`; the host gets a factory that returns a configured `PluginManager`.

**Requirements:** R3

**Dependencies:** Unit 1 (the `gixen/` package needs to exist as a real importable package via `pyproject.toml`).

**Files:**
- Create: `gixen/__init__.py`
- Create: `gixen/plugins.py`
- Create: `tests/test_plugins.py`

**Approach:**
- `gixen/__init__.py`: empty namespace marker. Version metadata can be added later via `importlib.metadata.version("gixen-cli")` if needed; not required for PER-25.
- `gixen/plugins.py`:
  - `hookspec = pluggy.HookspecMarker("gixen")`
  - `hookimpl = pluggy.HookimplMarker("gixen")`
  - `class GixenPluginSpec` with three `@hookspec`-decorated methods. Signatures match Linear: `register_routes(self, app)`, `register_db_tables(self, conn)`, `register_dashboard_tabs(self) -> list[dict]`. The tab shape is a plain `dict` for PER-25; PER-28 will define the exact field set when the consumer (topbar renderer) is built. Hookspec docstrings explain the contract, error posture, and ordering note.
  - `def make_plugin_manager() -> pluggy.PluginManager`: constructs `PluginManager("gixen")`, calls `add_hookspecs(GixenPluginSpec)`, returns it.
- Re-export from `gixen/plugins.py`: `__all__ = ["hookimpl", "hookspec", "GixenPluginSpec", "make_plugin_manager", "load_plugins"]` so `from gixen.plugins import hookimpl` works for plugin authors.

**Execution note:** Test-first — define the hookspec class via failing tests asserting the manager exposes the three hooks before writing the implementation.

**Patterns to follow:**
- Datasette's `datasette/plugins.py` and `datasette/hookspecs.py` (same pluggy-based pattern).

**Test scenarios:**
- Happy path: `make_plugin_manager()` returns a `pluggy.PluginManager` instance with `project_name == "gixen"`.
- Happy path: the returned manager has all three hooks registered as hookspecs (`pm.hook.register_routes._hookexec` exists, ditto for `register_db_tables` and `register_dashboard_tabs`).
- Happy path: a plugin module that defines `@hookimpl def register_routes(app): ...` can be registered with `pm.register(plugin)` without raising; `pm.is_registered(plugin)` returns True.
- Edge case: a plugin that defines a `@hookimpl` for a hookspec name that doesn't exist raises `PluginValidationError` at registration time (via `pm.check_pending()`).

**Verification:**
- `from gixen.plugins import hookimpl, hookspec, make_plugin_manager` imports cleanly.
- All Unit 2 tests pass.

---

- [ ] **Unit 3: Implement entry-point discovery and per-plugin error isolation**

**Goal:** Discover all installed plugins via the `gixen.plugins` entry-point group, register them in deterministic order, and isolate per-plugin failures.

**Requirements:** R1, R2, R4

**Dependencies:** Unit 2 (needs `make_plugin_manager`).

**Files:**
- Modify: `gixen/plugins.py` (add `load_plugins()` function)
- Modify: `tests/test_plugins.py`
- Modify: `tests/conftest.py` (add `fake_entry_points` fixture)

**Approach:**
- `def load_plugins(group: str = "gixen.plugins") -> pluggy.PluginManager`:
  ```
  pm = make_plugin_manager()
  for ep in sorted(entry_points(group=group), key=lambda e: e.name):
      try:
          plugin = ep.load()
      except Exception:
          logger.exception("Plugin %s failed to load (from %s)", ep.name, ep.value)
          continue
      try:
          pm.register(plugin, name=ep.name)
      except Exception:
          logger.exception("Plugin %s failed to register", ep.name)
  # Validate that every @hookimpl in registered plugins matches an existing hookspec.
  # Misspelled hook names (e.g. `register_route` vs `register_routes`) raise here.
  try:
      pm.check_pending()
  except Exception:
      logger.exception("Plugin validation failed (misspelled or unknown hookspec)")
  return pm
  ```
- Use `logging.getLogger("gixen.plugins")`; do not configure handlers (the host configures logging).
- The loader does NOT call any hooks — that's the lifespan's job (Unit 4). `load_plugins()` only does discovery + registration.
- `fake_entry_points` fixture in `tests/conftest.py`: monkeypatches `gixen.plugins.entry_points` to return a caller-supplied list of `importlib.metadata.EntryPoint` objects pointing at in-process modules. Use the 3-arg constructor `EntryPoint(name=..., value=..., group=...)` (positional or keyword both work in Python 3.14). `value` is the standard `"module:attr"` string. Example:
  ```
  ep = EntryPoint(name="overlay", value="tests.fake_plugins.simple:plugin", group="gixen.plugins")
  ```
  The fixture lets tests exercise the discovery path without pip-installing fake distributions.

**Execution note:** Test-first. The loader's error paths are the entire reason it exists — write the failure-isolation tests before the implementation.

**Patterns to follow:**
- pytest's `_pytest/config/__init__.py:_setup_cli_plugins` for the loop-and-isolate pattern.
- Datasette's `datasette/app.py` `_plugins_discovered` for the entry-point sort + try/except shape.

**Test scenarios:**
- Happy path: no plugins installed → `load_plugins()` returns a `PluginManager` with `pm.list_name_plugin()` empty. No log output.
- Happy path: single plugin installed → returned `pm` has one registered plugin matching the entry-point name.
- Happy path: three plugins with names `b`, `a`, `c` → registration order is `a`, `b`, `c` (sorted by name). Verify by inspecting `pm.list_name_plugin()` in order.
- Error path: plugin's `ep.load()` raises `ImportError` → loader logs at ERROR level, skips the plugin, continues with the rest. The returned `pm` does not contain the failing plugin.
- Error path: plugin's `pm.register()` raises (e.g. duplicate name) → loader logs at ERROR level, skips, continues. Other plugins still register.
- Error path: a plugin module with no `@hookimpl` functions → registers silently; pluggy doesn't require any hooks to be implemented.
- Edge case: two plugins with the same entry-point name → first one registers; second raises during `pm.register` (pluggy enforces name uniqueness); loader logs and skips. The returned pm has the first plugin.
- Edge case: entry-point name with hyphens (`gixen-overlay`) → registers correctly. (Hyphens are valid in entry-point names per PEP 621.)
- Error path: a plugin defines `@hookimpl def register_route(app):` (misspelled — missing the `s`). `pm.check_pending()` raises `PluginValidationError`; the loader catches and logs at ERROR. The returned `pm` still contains the misspelled plugin (pluggy registered it before `check_pending` ran), but the misspelled hook will never fire. The error message identifies the plugin and the bogus hook name.
- Integration: a fake plugin module declaring `@hookimpl def register_routes(app): app.state.touched = True` is loaded, then `pm.hook.register_routes(app=fake_app)` fires the hook and sets `touched`. Verifies end-to-end discovery → registration → hook invocation flow.

**Verification:**
- All Unit 3 tests pass.
- Manual: pip-install a tiny test plugin in the dev venv, restart, observe log line confirming discovery.

---

- [ ] **Unit 4: Wire loader into FastAPI lifespan**

**Goal:** Make the existing FastAPI app discover and invoke plugins during startup. After this unit, an installed plugin can register routes, tables, and tabs that show up in the running server.

**Requirements:** R2, R3, R6

**Dependencies:** Unit 3.

**Files:**
- Modify: `server/main.py` (extend `lifespan`)
- Modify: `tests/test_server_api.py` or new `tests/test_plugin_integration.py`

**Approach:**
- Inside `lifespan` (server/main.py:550-597), after `init_db()` returns and before background tasks spawn:
  ```
  pm = load_plugins()
  app.state.plugin_manager = pm

  # Tables first (plugin routes may query plugin tables).
  # Per-plugin loop with savepoints — required because SQLite needs per-plugin
  # rollback granularity if a plugin's DDL fails partway through.
  for plugin_name, plugin in pm.list_name_plugin():
      sp_name = "sp_" + re.sub(r'[^a-z0-9_]', '_', plugin_name.lower())
      try:
          _db.execute(f"SAVEPOINT {sp_name}")
          # subset_hook_caller fires only this plugin's register_db_tables impl.
          others = [p for n, p in pm.list_name_plugin() if n != plugin_name]
          pm.subset_hook_caller("register_db_tables", remove_plugins=others)(conn=_db)
          _db.execute(f"RELEASE {sp_name}")
      except Exception:
          _db.execute(f"ROLLBACK TO {sp_name}")
          _db.execute(f"RELEASE {sp_name}")
          logger.exception("register_db_tables failed for plugin %s", plugin_name)

  # Routes — bulk call. If any plugin's register_routes raises, pluggy aborts
  # the remaining plugins' route registration. This is acceptable: a plugin
  # whose route registration fails at startup is a deployment bug the operator
  # should fix, not something the host should silently work around.
  try:
      pm.hook.register_routes(app=app)
  except Exception:
      logger.exception("register_routes failed; some plugin routes may be missing")

  # Tabs — bulk call. pm.hook returns a list of results (one per registered
  # plugin), so flatten before storing.
  try:
      tab_lists = pm.hook.register_dashboard_tabs()
      app.state.dashboard_tabs = [t for lst in tab_lists for t in (lst or [])]
  except Exception:
      logger.exception("register_dashboard_tabs failed; tab list may be incomplete")
      app.state.dashboard_tabs = []

  # Force OpenAPI schema regeneration so plugin routes show up in /docs.
  app.openapi_schema = None
  ```
- Per-plugin isolation is kept only for `register_db_tables` because savepoints are the only piece that genuinely needs it. For `register_routes` and `register_dashboard_tabs`, bulk calls are simpler and the failure mode (one bad plugin halts the chain) is loud rather than silent — easier to debug than partial registration.
- Route-conflict detection is intentionally not implemented. FastAPI's `app.routes` accepts duplicates (last-write wins per path); if a plugin shadows a core route, the operator will notice via integration tests or production behavior. Adding silent rollback would hide configuration bugs.
- Lifespan shutdown does not need to do anything plugin-specific in PER-25. PER-30 may surface cleanup needs.

**Execution note:** Add a fake plugin integration test in `tests/test_plugin_integration.py` using the `fake_entry_points` fixture from Unit 3. Start `TestClient(app)` as a context manager (triggers lifespan), then assert the plugin's route is reachable and the plugin's table exists.

**Patterns to follow:**
- Existing `server/main.py` lifespan structure (`init_db` → background tasks → yield → cleanup) — slot plugin loading between `init_db` and background tasks.
- `tests/test_server_api.py:21-31` for TestClient + monkeypatched env fixture.

**Test scenarios:**
- Happy path: no plugins installed → server starts, all existing routes work, `app.state.plugin_manager` exists with zero registered plugins, `app.state.dashboard_tabs == []`.
- Happy path: a fake plugin registering one route (`GET /api/fake/ping → "pong"`) → after `TestClient(app).__enter__()`, the route is reachable and returns `"pong"`.
- Happy path: a fake plugin registering a table (`CREATE TABLE fake_t (id INTEGER PRIMARY KEY)`) → after lifespan, the table exists in the SQLite DB (`SELECT name FROM sqlite_master WHERE type='table' AND name='fake_t'` returns one row).
- Happy path: a fake plugin returning `[{"slug": "fake", "label": "Fake", "key": "f", "route": "/v2/fake"}]` from `register_dashboard_tabs` → `app.state.dashboard_tabs` contains the spec.
- Error path: a fake plugin's `register_db_tables` raises after creating one table → savepoint rolls back; the table does NOT exist in the DB; other plugins' tables still exist; server starts successfully.
- Error path: a fake plugin's `register_routes` raises → caught by the lifespan's outer try/except, logged at ERROR; server still starts; existing core routes still work; plugins registered later in the chain may not have had their routes registered (acceptable per design — see Approach).
- Error path: a fake plugin's `register_dashboard_tabs` raises → caught at the lifespan level; `app.state.dashboard_tabs` is set to `[]`; server still starts.
- Integration: ordering — two plugins, the second depends on the first's table. Verify `register_db_tables` for `a` runs before `b` (sorted by name), so `b`'s DDL referencing `a`'s table succeeds.
- Integration: OpenAPI reflects plugin routes — after lifespan, `GET /openapi.json` includes the fake plugin's `GET /api/fake/ping`.

**Verification:**
- All Unit 4 tests pass.
- Manual: start `uvicorn server.main:app --reload` against the empty plugin set; confirm server starts and existing dashboard works unchanged.
- Manual: pip-install a tiny test plugin into the dev venv; restart server; confirm log line announcing plugin discovery + test plugin's route is reachable.

## System-Wide Impact

- **Interaction graph:** Plugin hooks fire inside the existing `lifespan` between `init_db()` and background-task spawn. No callbacks added to background loops, route handlers, or shutdown.
- **Error propagation:** Plugin failures are logged but do NOT propagate up to the lifespan — the host always starts successfully even if every plugin fails to load. This is the most user-hostile failure case to debug, which is why error logging must include the plugin name, entry-point value, and full traceback at ERROR level.
- **State lifecycle risks:** SQLite savepoints around `register_db_tables`. No partial-write concern because each plugin's DDL is wrapped. Caveat: a plugin that creates a table AND inserts seed data in the same hook will have both rolled back together — acceptable, since plugins should run DDL only and seed via runtime calls if needed.
- **API surface parity:** None of the existing 17 routes are modified in PER-25. New surface: `app.state.plugin_manager` and `app.state.dashboard_tabs` are introduced. Nothing reads them yet in core — PER-28 will.
- **Integration coverage:** TestClient-based integration tests in Unit 4 cover the full discovery → register → invoke → serve path. Unit tests in Unit 3 cover discovery in isolation.
- **Unchanged invariants:**
  - `python cli.py` and `uvicorn server.main:app` continue to work without `pip install -e .`.
  - All 17 existing routes return the same responses (verified by running existing `tests/test_server_api.py`).
  - SQLite schema in `server/db.py` is unchanged; comic tables stay in core for now (PER-27 moves them).
  - The LaunchAgent install at `server/install.sh` is unchanged (PER-37 may polish it).

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| Hatchling's flat-layout package discovery rejects the `cli.py` + `server/` + `gixen/` mix. | Explicit `[tool.hatch.build.targets.wheel] packages = [...]` + `include = [...]` declarations cover this exact case; verified in research. Fallback: move top-level modules into `gixen/` (would change `cli.py` invocations — large blast radius, avoided). |
| Calling `app.include_router()` / `app.mount()` inside lifespan is not documented as supported by FastAPI. | It works in practice because lifespan completes before the first request. Risk is silent breakage on a future FastAPI upgrade. Mitigation: pin FastAPI version range in `pyproject.toml` (`fastapi>=0.136,<0.150`); add an integration test that asserts `/openapi.json` includes plugin routes; revisit if FastAPI changes behavior. |
| `pluggy` adds a new dependency; the transitive dep graph may pull in surprises. | pluggy is pure-Python, ~1500 lines, zero runtime deps. Risk is essentially zero. Used by pytest, which is already in the dependency set. |
| Plugin authors mis-spell a hookspec name (e.g. `register_route` vs `register_routes`) and the hook silently never fires. | `pm.check_pending()` is called after the registration loop in `load_plugins()` (Unit 3). It raises `PluginValidationError` for any `@hookimpl` that references an unknown hookspec. The loader logs the error at ERROR and continues — the plugin author gets a clear log message identifying their typo. |
| Existing comic routes will continue to work during PER-25 (since extraction is deferred to PER-30), but the plugin loader runs an empty discovery cycle on every startup — wasted work. | Negligible cost (entry_points scan is ~ms). Not worth optimizing. |
| `_db` is opened in `init_db()` and held as a module global. Plugin DDL uses the same connection. SQLite is single-writer; plugin DDL during startup can't conflict with background writers because lifespan startup runs before background tasks spawn. | Verified by reading `server/main.py:550-597` — background tasks spawn after `_db` init. Plugin loading is correctly ordered. |
| **Plugin code runs in-process inside the server.** A malicious or buggy plugin can do anything the server process can: read `~/.gixen-server/.env`, exfiltrate session cookies, drop tables. This is the same trust model as any pip dependency — `pip install gixen-overlay` already gives `gixen-overlay` arbitrary code execution. | Document the trust model in the plugin author docs (deferred to PER-30 alongside the first real plugin). PER-25 ships no sandbox; sandboxing plugins would require the HTTP/RPC architecture we explicitly rejected (see origin: "Key Architectural Decisions"). Mitigation for now: only install plugins the user trusts; the entry-point group name (`gixen.plugins`) is namespaced enough to avoid accidental collision with unrelated packages. |

## Documentation / Operational Notes

- README update is **out of scope** for PER-25 — deferred to PER-37 (Publish v1.0).
- Plugin author docs deferred to PER-30 once we have a real plugin to use as a worked example. Hookspec docstrings in `gixen/plugins.py` are the only documentation PER-25 ships.
- No operational change for current users — server install via `server/install.sh` continues to work unchanged.
- Logging: plugin discovery and failures log to `gixen.plugins` logger at ERROR/INFO. No changes to existing logger configuration.

## Sources & References

- **Origin document:** [docs/refactor-split-handoff.md](../refactor-split-handoff.md)
- **Linear issue:** PER-25 — Add Plugin Entry-Point System to gixen-cli
- **External docs:**
  - https://docs.python.org/3.14/library/importlib.metadata.html
  - https://pluggy.readthedocs.io/en/stable/
  - https://docs.datasette.io/en/stable/plugin_hooks.html
  - https://fastapi.tiangolo.com/advanced/events/
  - https://packaging.python.org/en/latest/specifications/pyproject-toml/
  - https://hatch.pypa.io/latest/config/build/
- **Related code in this repo:**
  - `server/main.py:550-597` (lifespan)
  - `server/db.py:97-111` (init_db)
  - `tests/test_server_api.py:21-31` (TestClient fixture pattern)
  - `tests/conftest.py` (current minimal fixtures)
- **Review pass:** This plan was deepened on 2026-05-18 via feasibility, scope-guardian, and coherence reviewers. Key adjustments: simplified hook dispatch (bulk for routes/tabs, per-plugin only for db_tables), dropped route-conflict-detection rollback, dropped `TabSpec` TypedDict in favor of plain `dict`, dropped empty host entry-points stanza, replaced `requirements.txt` rather than running both, added `pm.check_pending()` to loader, spelled out `EntryPoint` constructor for test fixture.
