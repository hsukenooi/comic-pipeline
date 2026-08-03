"""Plugin entry-point system for gixen-cli.

External packages register against the ``gixen.plugins`` entry-point group.
At FastAPI startup, the loader (``load_plugins``) discovers installed
plugins and registers them with a ``pluggy.PluginManager``. The host then
invokes three hooks during the lifespan:

    register_db_tables(conn)        — plugin creates its own SQLite tables
    register_routes(app)            — plugin mounts FastAPI routes on the app
    register_dashboard_tabs() -> list[dict]
                                    — plugin returns dashboard tab specs

Plus two **request-time** hooks (BUI-617/U3 — the first hooks that fire per
request rather than once at lifespan), invoked from ``server/policy.py``'s
check point on every ``POST /api/bids`` / ``PATCH /api/bids/{item_id}``:

    check_bid_write(conn, intent) -> list[dict]
                                    — pre-write, read-only policy checks
    on_bid_write_committed(conn, intent, bid_row_id, check_results)
                                    — post-write notification

Plus one **background-loop** hook (BUI-624), invoked from ``_sync_gixen``
inside its apply-phase transaction:

    on_sync_observed(conn, snipe_count)
                                    — a full snipe-sync pass is landing

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
    # Type-only: server/policy.py never imports this module, so importing
    # PolicyIntent here under TYPE_CHECKING (never at runtime) documents the
    # hookspec signatures below without creating a real import edge — this
    # module stays a leaf of the plugin subsystem with zero dependency on
    # server/* at runtime (server/main.py imports FROM here, not the other
    # way around).
    from server.policy import PolicyIntent

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

    @hookspec
    def check_bid_write(
        self, conn: sqlite3.Connection, intent: "PolicyIntent"
    ) -> list[dict]:
        """Contribute pre-write, read-only policy checks (BUI-617/U3).

        Fired once per write, from ``server/policy.py``'s ``run_checks`` —
        the host's single check point, called from both ``api_add_bid`` and
        ``api_edit_bid`` inside their existing ``_api_lock`` acquisition,
        BEFORE the Gixen call. This is the first **request-time** hook (the
        three above fire once, at lifespan startup) — see KTD1 in
        ``docs/plans/2026-08-02-001-feat-capital-commitment-layer-plan.md``.

        :param conn: the host's open ``sqlite3.Connection``. Gather-phase
            reads only — no writes. A plugin that writes here breaks the
            check point's read-only contract (checks run inside a lock held
            across the eventual Gixen call; a write here has no transaction
            of its own to land in safely).
        :param intent: a ``server.policy.PolicyIntent`` snapshot of the write
            being evaluated (``item_id``, ``target_max_bid``, ``snipe_group``,
            ``trigger``, ``prior_row``, ``comic_identities``). Imported only
            under ``TYPE_CHECKING`` here — this module has no runtime
            dependency on ``server.*``; plugins receive the real object at
            call time and can import the real type themselves.
        :return: a list of plain ``dict``s, each shaped like
            ``server.policy.CheckResult`` — ``{"code": str, "outcome":
            "pass" | "advise" | "unevaluable", "message": str, "data":
            dict}``. An empty list (or ``None``) contributes nothing.

        **Must not raise.** The host wraps this call defensively (the
        ``_collect_dashboard_tabs`` pattern below): an exception anywhere in
        this hook — from any registered plugin, since pluggy halts a bulk
        hook call's impl chain on the first raise (see
        ``_invoke_register_routes``'s docstring) — is caught by the host,
        logged loudly, and downgraded to a single ``unevaluable`` check
        result. It never becomes an exception into the money path, and never
        blocks, fails, or delays the bid write (v1 is advisory-only). A
        returned ``outcome`` outside the tri-state vocabulary above is
        likewise downgraded to ``unevaluable`` by the host — a cooperative
        plugin should still avoid raising, since one plugin's raise loses
        every OTHER registered plugin's contribution for this call too
        (bulk-call semantics, not per-plugin isolation).
        """

    @hookspec
    def on_bid_write_committed(
        self,
        conn: sqlite3.Connection,
        intent: "PolicyIntent",
        bid_row_id: int | None,
        check_results: list[dict],
    ) -> None:
        """Notify plugins after a bid write has committed (BUI-617/U3).

        Fired by the host, in the same request, AFTER the bid row has been
        written (created or modified) — never for a write that only
        evaluated checks without landing a row (an unconfirmed upsert/edit,
        or a Gixen failure). Lets a plugin persist state it resolved during
        its own ``check_bid_write`` call — the overlay's motivating use case
        is linking the FMV row(s) its checks resolved to the now-existing
        bid row (a later wave, U4/U5) — something the host cannot do itself:
        it has no import edge into overlay code.

        :param conn: the host's open ``sqlite3.Connection``.
        :param intent: the same ``PolicyIntent`` passed to ``check_bid_write``
            for this write.
        :param bid_row_id: the committed ``bids.id`` this write produced.
        :param check_results: the full tri-state ``check_results`` list this
            request's ``run_checks`` produced (host + every plugin's
            contribution), the same shape ``check_bid_write`` returns.
        :return: ignored — this hook is notification-only.

        **Must not raise.** The write has already committed by the time this
        fires, so there is nothing left to roll back or degrade — the host
        wraps this call in a single try/except (the ``_collect_dashboard_tabs``
        pattern) and only logs loudly on failure.
        """

    @hookspec
    def on_sync_observed(self, conn: sqlite3.Connection, snipe_count: int) -> None:
        """Notify plugins that a full ``_sync_gixen`` pass is landing (BUI-624).

        Fired from ``server/main.py``'s ``_sync_gixen``, at the same point
        BUI-604's ``_stamp_sync_observed`` marks — a scrape that reached the
        apply phase without raising — but from *inside* that apply phase's
        ``write_transaction()``, immediately before it commits. Not fired on
        any of the ``return []`` paths above it: a ``GixenConnectionError`` /
        ``GixenError`` is exactly the case a watchdog must see as "I am
        blind", never as a healthy observation.

        :param conn: the apply phase's open ``write_transaction()``
            connection, with the app-wide ``_write_lock`` held. A plugin
            writing here joins the sync's own transaction — which is the
            point: its write commits if and only if the sync's writes do, so
            a "the sync ran" record can never outlive a cycle whose DML
            rolled back.
        :param snipe_count: how many snipes the scrape returned. **Zero is a
            success**, not a failure — "reached Gixen, nothing live right now"
            is a completed pass, and conflating it with "did not run" is the
            fails-green bug this hook exists to let a plugin close.
        :return: ignored — this hook is notification-only.

        **Must not raise, and cannot break the sync if it does.** The host
        fires this through ``_invoke_sync_observed`` below, which brackets the
        call in a SQLite savepoint: a raising hookimpl is rolled back to the
        savepoint and logged, and the sync's own DML commits untouched. Do NOT
        do network I/O here — the write lock is held and the event loop is
        blocked for the duration.
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
    except Exception as exc:  # noqa: BLE001  # pluggy validation — log all plugin errors, then continue
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


def _invoke_sync_observed(
    pm: pluggy.PluginManager | None,
    conn: sqlite3.Connection,
    snipe_count: int,
    *,
    logger: logging.Logger,
) -> None:
    """Fire ``on_sync_observed`` inside the caller's transaction, savepointed.

    BUI-624. The caller is ``_sync_gixen``'s apply phase, and the invariant it
    documents at that site is absolute: **nothing here may turn a healthy sync
    cycle into a failed one.** A raise reaching ``_sync_gixen`` would leave the
    committed DML intact but convert the cycle into a ``_sync_loop`` backoff or
    an ``api_sync`` 500 — a watchdog ping that can black out the very loop it
    reports on is worse than no ping at all.

    Two mechanisms enforce that, not one:

    * the savepoint (``_invoke_db_tables_isolated``'s pattern) — a raising
      hookimpl's partial DML is rolled back and the sync's own writes survive;
    * the outer try/except — this function has no raising path of its own.

    Firing INSIDE the transaction rather than after the commit is deliberate,
    and it is what lets the ping obey that invariant by construction instead of
    by promise: there is simply no post-commit I/O left to go wrong.

    It also buys atomicity, with one honest caveat. When the caller's
    transaction already has pending DML the savepoint nests, so the plugin's
    write commits with the caller's and a later rollback takes it too — a "the
    job ran" record can never outlive writes that vanished. When the caller has
    written *nothing* yet, this savepoint is the outermost one and its
    ``RELEASE`` commits on its own. That is harmless where it happens (a Gixen
    cycle that found nothing to change has no writes for the record to
    contradict, and the hook is the last statement in the block) but it is a
    real property of SQLite savepoints, not a rounding error: **keep this call
    last inside the caller's transaction.** A statement added after it would be
    able to fail with the plugin's write already committed.

    ``pm=None`` is a no-op (a server started without a plugin manager, e.g. a
    unit test calling ``_sync_gixen`` directly).
    """
    if pm is None:
        return
    sp = "sp_sync_observed"
    try:
        conn.execute(f"SAVEPOINT {sp}")
        try:
            pm.hook.on_sync_observed(conn=conn, snipe_count=snipe_count)
        except Exception:
            logger.exception(
                "on_sync_observed hook raised; rolling back to %s. The sync's "
                "own writes are unaffected — only this cycle's plugin-side "
                "bookkeeping is lost",
                sp,
            )
            conn.execute(f"ROLLBACK TO {sp}")
        finally:
            conn.execute(f"RELEASE {sp}")
    except Exception:
        # Reached only if the savepoint machinery itself failed — e.g. a
        # hookimpl that violated the contract by committing the transaction
        # out from under us (the `executescript` trap register_db_tables
        # documents), which destroys the savepoint and makes ROLLBACK TO /
        # RELEASE raise in turn. Swallowed here so the caller still returns
        # normally; the connection may be inconsistent, which is why the log
        # is loud.
        logger.exception(
            "on_sync_observed savepoint bookkeeping failed; a plugin likely "
            "committed or closed the sync's transaction, which the hookspec "
            "forbids. Connection state may be inconsistent"
        )


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
