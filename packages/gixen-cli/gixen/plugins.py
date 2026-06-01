"""Plugin entry-point system for gixen-cli.

External packages register against the ``gixen.plugins`` entry-point group.
At FastAPI startup, the loader (``load_plugins``) discovers installed
plugins and registers them with a ``pluggy.PluginManager``. The host then
invokes three hooks during the lifespan:

    register_db_tables(conn)        — plugin creates its own SQLite tables
    register_routes(app)            — plugin mounts FastAPI routes on the app
    register_dashboard_tabs() -> list[dict]
                                    — plugin returns dashboard tab specs

Plugin authors import the ``hookimpl`` marker from this module to decorate
their hook implementations::

    from gixen.plugins import hookimpl

    @hookimpl
    def register_routes(app):
        app.include_router(...)
"""
from __future__ import annotations

import logging
import re
import sqlite3
from importlib.metadata import entry_points as _stdlib_entry_points


def entry_points(group: str):
    # entry_points(group=...) requires Python 3.12+; fall back to dict API on 3.9-3.11
    try:
        return _stdlib_entry_points(group=group)
    except TypeError:
        return _stdlib_entry_points().get(group, [])
from typing import TYPE_CHECKING

import pluggy

if TYPE_CHECKING:
    from fastapi import FastAPI

# Plugin authors only need `hookimpl`. `hookspec`, `GixenPluginSpec`, and
# `make_plugin_manager` are host-side primitives — re-exported in this module
# for the host and tests but intentionally not in __all__.
__all__ = ["hookimpl", "load_plugins"]

_logger = logging.getLogger("gixen.plugins")

_GROUP = "gixen.plugins"


hookspec = pluggy.HookspecMarker("gixen")
hookimpl = pluggy.HookimplMarker("gixen")


class GixenPluginSpec:
    """The contract every gixen plugin can implement.

    A plugin does not have to implement all three hooks — pluggy will only
    fire the hooks the plugin has decorated. Hookspecs document the
    signature and ordering contract; hook ordering is by entry-point name
    (alphabetical) unless a plugin uses ``@hookimpl(tryfirst=True)`` or
    ``trylast=True`` to override.

    Error handling: per-plugin isolation is applied at hook-invocation time
    by the host's lifespan. A plugin whose hook raises will not prevent
    other plugins from registering (for ``register_db_tables`` — see
    ``load_plugins`` and the lifespan in ``server/main.py``).
    """

    @hookspec
    def register_routes(self, app: "FastAPI"):
        """Register FastAPI routes on the host application.

        :param app: the host FastAPI instance. Plugins typically build an
            ``APIRouter`` and call ``app.include_router(router, prefix=...)``.
        """

    @hookspec
    def register_db_tables(self, conn: sqlite3.Connection):
        """Create plugin-owned SQLite tables.

        :param conn: the host's open ``sqlite3.Connection``. Plugins should
            use ``CREATE TABLE IF NOT EXISTS`` so re-runs are idempotent.
            Tables must be namespaced (e.g. ``myplugin_data``, not ``data``) to
            avoid collisions with the core ``bids`` table or other plugins.

        DDL executed in this hook is wrapped in a SQLite savepoint by the
        host; a failure rolls back this plugin's DDL only.

        ``app.state.db`` is guaranteed to be set to the same connection by the
        host before this hook fires. Plugins can read it via the FastAPI
        request lifecycle later (e.g. ``request.app.state.db``).

        **Important:** call ``conn.execute(...)`` per statement, NOT
        ``conn.executescript(...)``. Python's sqlite3 ``executescript``
        implicitly commits any pending transaction before running, which
        releases the host's savepoint and breaks per-plugin isolation. If
        you need multiple statements, call ``conn.execute`` for each one.
        """

    @hookspec
    def register_dashboard_tabs(self) -> list[dict]:
        """Return a list of dashboard tab specifications.

        Each spec is a plain ``dict`` with the following shape (TabSpec):

            {"label": str, "path": str}

        ``label`` — display text shown in the nav (e.g. ``"Comics"``).
        ``path``  — href for the nav link (e.g. ``"/v2/comics"``).

        The host collects all plugin tab lists, flattens them, stores the
        result on ``app.state.dashboard_tabs``, and exposes it via
        ``GET /api/dashboard-tabs``. The dashboard JS fetches that endpoint
        at page load and injects the tabs into the nav after the hardcoded
        core tabs (snipes, bids).
        """


def make_plugin_manager() -> pluggy.PluginManager:
    """Construct a fresh ``PluginManager`` with the gixen hookspecs loaded.

    Used by ``load_plugins`` and by tests that want to register fake
    plugins directly via ``pm.register(...)`` without going through the
    entry-point discovery path.
    """
    pm = pluggy.PluginManager("gixen")
    pm.add_hookspecs(GixenPluginSpec)
    return pm


def load_plugins() -> pluggy.PluginManager:
    """Discover and register all plugins declared under ``gixen.plugins``.

    Plugins are registered in deterministic order — sorted by entry-point
    name — so that hook invocation order is reproducible across machines
    (the default order from ``entry_points()`` is sys.path order, which is
    not stable). Plugins needing explicit ordering can use
    ``@hookimpl(tryfirst=True)`` or ``trylast=True``.

    Per-plugin error isolation: a plugin whose ``ep.load()`` raises, whose
    ``pm.register()`` raises (e.g. duplicate name), or whose registered
    hookimpls reference a misspelled hookspec, is logged at ERROR and
    skipped. The loader always returns a usable ``PluginManager`` — never
    raises on plugin failure.
    """
    pm = make_plugin_manager()
    registered: list[str] = []
    for ep in sorted(entry_points(_GROUP), key=lambda e: e.name):
        try:
            plugin = ep.load()
        except Exception:
            _logger.exception(
                "Plugin %s failed to load (from %s)", ep.name, ep.value
            )
            continue
        try:
            pm.register(plugin, name=ep.name)
            registered.append(ep.name)
            _logger.info("Plugin %s registered from %s", ep.name, ep.value)
        except Exception:
            _logger.exception("Plugin %s failed to register", ep.name)

    # Validate that every @hookimpl in registered plugins matches an existing
    # hookspec. Misspelled hook names (e.g. ``register_route`` vs
    # ``register_routes``) raise PluginValidationError here. The error message
    # from pluggy includes the offending plugin name.
    try:
        pm.check_pending()
    except Exception as exc:
        _logger.error("Plugin validation failed: %s", exc)

    if registered:
        _logger.info(
            "Loaded %d plugin(s) from %s: %s",
            len(registered), _GROUP, ", ".join(registered),
        )
    else:
        _logger.info("No plugins discovered in %s", _GROUP)
    return pm


# ---------------------------------------------------------------------------
# Host-side helpers — invoked by the FastAPI lifespan in server/main.py.
#
# These are underscore-prefixed because they are NOT part of the plugin
# author's API. ``__all__`` lists only what plugin authors should import;
# these helpers are the host's machinery for firing the hooks correctly,
# with per-plugin isolation for DDL and defensive error handling for the
# bulk hooks. PER-25 review's M-01 finding wanted this plumbing out of the
# server's lifespan; PER-26 Unit 3 delivers that.
#
# Each helper accepts a keyword-only ``logger`` so the lifespan can pass
# ``logging.getLogger("server.main")`` and existing PER-25 regression tests
# that assert on ``caplog.set_level(..., logger="server.main")`` continue to
# capture the cleanup-failure records.
# ---------------------------------------------------------------------------


def _invoke_db_tables_isolated(
    pm: pluggy.PluginManager,
    conn: sqlite3.Connection,
    *,
    logger: logging.Logger,
) -> list[str]:
    """Fire ``register_db_tables`` per plugin inside a SQLite savepoint.

    Each plugin's DDL runs in its own savepoint; failure rolls back this
    plugin only and leaves the connection usable for the next plugin and
    for core. A plugin that violates the hookspec by calling
    ``conn.executescript(...)`` implicitly COMMITs the transaction and
    destroys the savepoint — the inner ``ROLLBACK TO`` would then raise
    ``OperationalError``. We guard that secondary failure so the lifespan
    keeps going. (PER-25 ADV-001 / REL-01 / COR-01.)

    Returns the list of plugin names whose DDL succeeded, for caller logging.
    """
    succeeded: list[str] = []
    for plugin_name, _plugin in pm.list_name_plugin():
        sp_name = "sp_" + re.sub(r"[^a-z0-9_]", "_", plugin_name.lower())
        try:
            conn.execute(f"SAVEPOINT {sp_name}")
            others = [p for n, p in pm.list_name_plugin() if n != plugin_name]
            pm.subset_hook_caller(
                "register_db_tables", remove_plugins=others
            )(conn=conn)
            conn.execute(f"RELEASE {sp_name}")
            succeeded.append(plugin_name)
        except Exception:
            try:
                conn.execute(f"ROLLBACK TO {sp_name}")
                conn.execute(f"RELEASE {sp_name}")
            except Exception:
                logger.exception(
                    "Savepoint cleanup failed for plugin %s; the plugin likely "
                    "used conn.executescript() which is forbidden — see the "
                    "register_db_tables hookspec docstring. Connection state "
                    "may be inconsistent.",
                    plugin_name,
                )
            logger.exception(
                "register_db_tables failed for plugin %s", plugin_name
            )
    return succeeded


def _invoke_register_routes(
    pm: pluggy.PluginManager,
    app: "FastAPI",
    *,
    logger: logging.Logger,
) -> None:
    """Fire the bulk ``register_routes`` hook and force OpenAPI regen.

    Pluggy halts the impl chain on the first raise within one hook call.
    PER-25 chose this loud-failure posture deliberately: operators see the
    failure in logs rather than getting silently partial registration. The
    outer try/except logs and lets the server continue to start.

    ``app.openapi_schema = None`` runs in a finally block so the schema is
    consistent regardless of whether plugins succeeded — cheap when no
    plugins ran, essential when they did.
    """
    try:
        pm.hook.register_routes(app=app)
    except Exception:
        logger.exception(
            "register_routes failed; some plugin routes may be missing"
        )
    finally:
        app.openapi_schema = None


def _collect_dashboard_tabs(
    pm: pluggy.PluginManager,
    *,
    logger: logging.Logger,
) -> list[dict]:
    """Fire the bulk ``register_dashboard_tabs`` hook and flatten results.

    Each plugin's contribution must be a ``list[dict]``. A plugin that
    returns a bare dict would iterate as keys and silently corrupt the
    flattened output; guard with ``isinstance(x, list)`` and skip with a
    clear log message. (PER-25 ADV-002.)

    On a top-level failure of the bulk hook call, returns an empty list.
    """
    try:
        tab_lists = pm.hook.register_dashboard_tabs()
        flat: list[dict] = []
        for lst in tab_lists:
            if lst is None:
                continue
            if not isinstance(lst, list):
                logger.error(
                    "register_dashboard_tabs returned %s, expected list[dict]; "
                    "skipping this plugin's tabs",
                    type(lst).__name__,
                )
                continue
            valid = [item for item in lst if isinstance(item, dict)]
            dropped = len(lst) - len(valid)
            if dropped:
                logger.error(
                    "register_dashboard_tabs: %d non-dict element(s) dropped from "
                    "plugin contribution; expected list[dict] elements",
                    dropped,
                )
            flat.extend(valid)
        return flat
    except Exception:
        logger.exception(
            "register_dashboard_tabs failed; tab list may be incomplete"
        )
        return []
