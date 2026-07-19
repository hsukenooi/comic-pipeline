"""Canary for the overlay -> gixen-cli cross-package coupling (U5/BUI-56).

The overlay imports private helpers from gixen-cli's server.* modules
(routes.py:21-22). In the monorepo these resolve via the uv workspace install,
NOT the old hardcoded pythonpath. This test fails loudly if an upstream rename
in packages/gixen-cli breaks that surface — the coupling is now atomically
changeable and CI-guarded rather than silently fragile.

Deliberately imports by string-free direct reference so a rename can't be masked.
"""
from __future__ import annotations

import inspect


def test_overlay_routes_importable_via_workspace():
    """Loading the overlay's routes module must succeed purely via the
    workspace-resolved gixen-cli install (no sys.path injection)."""
    import gixen_overlay.routes  # noqa: F401


def test_gixen_cli_private_helper_surface_resolves():
    """The exact private helpers the overlay depends on must be importable
    from gixen-cli. If any is renamed upstream, this is the canary."""
    from server.db import TOMBSTONE_STATUSES_SQL, get_bid_by_item_id, write_transaction
    from server.main import (
        _ensure_fresh_sync,
        _spawn_fallback_task,
        iso_to_relative,
        _get_db_path,
        _write_locked,
    )

    assert all(
        callable(fn)
        for fn in (
            _ensure_fresh_sync,
            _spawn_fallback_task,
            iso_to_relative,
            get_bid_by_item_id,
            write_transaction,
            _get_db_path,
            _write_locked,
        )
    )
    # BUI-272: routes.py also imports this tombstone-filter constant from
    # server.db; pin its resolvability + the PURGED/REMOVED dual-tolerance (BUI-49).
    assert "'PURGED'" in TOMBSTONE_STATUSES_SQL
    assert "'REMOVED'" in TOMBSTONE_STATUSES_SQL


def test_plugin_hook_entrypoint_importable():
    """plugin.py's hookimpl import + the registered entry-point target resolve."""
    from gixen_overlay.plugin import plugin

    assert plugin is not None


def test_locg_command_surface_resolves():
    """BUI-91/92: the overlay wraps locg-cli's collection + wish-list functions
    behind /api/comics/*. These resolve via the `locg` workspace dependency. If
    any is renamed in packages/locg-cli, this canary fails loudly (same role as
    the gixen-cli helper canary above)."""
    from locg.commands import (
        _split_wish_list_name,
        cmd_collection_check,
        cmd_collection_export,
        cmd_collection_import,
        cmd_collection_record_win,
        cmd_collection_remediate_delete,
        cmd_collection_remediate_set_copies,
        cmd_collection_status,
        cmd_wish_list_add,
        cmd_wish_list_conflicts,
        cmd_wish_list_from_cache,
        cmd_wish_list_remove,
        cmd_wish_list_remove_conflicts,
    )

    assert all(
        callable(fn)
        for fn in (
            cmd_collection_check,
            cmd_collection_export,
            cmd_collection_import,
            cmd_collection_record_win,
            cmd_collection_remediate_delete,
            cmd_collection_remediate_set_copies,
            cmd_collection_status,
            cmd_wish_list_add,
            cmd_wish_list_from_cache,
            cmd_wish_list_conflicts,
            cmd_wish_list_remove,
            cmd_wish_list_remove_conflicts,
            _split_wish_list_name,
        )
    )


def _required_positional_count(fn) -> int:
    """Number of required positional parameters (no default, positional-kind)."""
    params = inspect.signature(fn).parameters.values()
    return sum(
        1
        for p in params
        if p.default is p.empty
        and p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
    )


def test_gixen_cli_private_helper_signatures_pinned():
    """BUI-155: `callable()` is a near-meaningless contract for the cross-package
    coupling surface — a rename's 'evil twin' (same name, changed arity) passes
    the importability canary while breaking routes.py at runtime. Pin the exact
    call shapes the overlay's call sites depend on (routes.py:189, 297, 593,
    634-635) so an upstream arity change fails CI loudly instead of in prod.

    These four `server.main`/`server.db` private helpers have NO overlay
    integration test exercising them through the real symbols (route tests mock
    them), so signature pinning is the only behavioral guard on their contract.
    """
    from server.db import get_bid_by_item_id
    from server.main import (
        _ensure_fresh_sync,
        _spawn_fallback_task,
        iso_to_relative,
    )

    # routes.py:593 — `iso_to_relative(end_date_iso)`: exactly one positional.
    assert _required_positional_count(iso_to_relative) == 1

    # routes.py:189/297 — `get_bid_by_item_id(db, item_id)`: exactly two.
    assert _required_positional_count(get_bid_by_item_id) == 2

    # routes.py:634-635 — both called with no args; `_ensure_fresh_sync` is
    # awaited, so it must stay a coroutine function.
    assert _required_positional_count(_ensure_fresh_sync) == 0
    assert _required_positional_count(_spawn_fallback_task) == 0
    assert inspect.iscoroutinefunction(_ensure_fresh_sync), (
        "routes.py:634 awaits _ensure_fresh_sync() — it must stay async"
    )


def test_write_lock_helper_signatures_pinned():
    """BUI-408 (Stage 1 of BUI-400's shared-connection isolation rollout):
    same rationale as test_gixen_cli_private_helper_signatures_pinned above,
    for the write-lock coupling api_link_locg's write now depends on —
    ``async with _write_locked(): with write_transaction(_get_db_path()) as
    wconn:`` (routes.py's ``/api/bids/{item_id}/comics/locg`` handler).
    """
    from pathlib import Path

    from server.db import write_transaction
    from server.main import _get_db_path, _write_locked

    # write_transaction(_get_db_path()) — must accept exactly one positional
    # path argument (it also has a default, so _required_positional_count
    # alone can't see this — bind() proves the call shape directly).
    inspect.signature(write_transaction).bind(Path("/tmp/pinned.db"))

    # _get_db_path() — no arguments, called synchronously.
    assert _required_positional_count(_get_db_path) == 0
    assert not inspect.iscoroutinefunction(_get_db_path)

    # _write_locked() — no arguments, used as `async with _write_locked():`.
    assert _required_positional_count(_write_locked) == 0
    cm = _write_locked()
    assert hasattr(cm, "__aenter__") and hasattr(cm, "__aexit__"), (
        "routes.py awaits `async with _write_locked():` — it must stay an "
        "async context manager"
    )
