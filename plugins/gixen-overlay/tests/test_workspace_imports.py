"""Canary for the overlay -> gixen-cli cross-package coupling (U5/BUI-56).

The overlay imports private helpers from gixen-cli's server.* modules
(routes.py:21-22). In the monorepo these resolve via the uv workspace install,
NOT the old hardcoded pythonpath. This test fails loudly if an upstream rename
in packages/gixen-cli breaks that surface — the coupling is now atomically
changeable and CI-guarded rather than silently fragile.

Deliberately imports by string-free direct reference so a rename can't be masked.
"""
from __future__ import annotations


def test_overlay_routes_importable_via_workspace():
    """Loading the overlay's routes module must succeed purely via the
    workspace-resolved gixen-cli install (no sys.path injection)."""
    import gixen_overlay.routes  # noqa: F401


def test_gixen_cli_private_helper_surface_resolves():
    """The exact private helpers the overlay depends on must be importable
    from gixen-cli. If any is renamed upstream, this is the canary."""
    from server.db import get_bid_by_item_id
    from server.main import (
        _ensure_fresh_sync,
        _iso_to_relative,
        _spawn_fallback_task,
    )

    assert all(
        callable(fn)
        for fn in (
            _ensure_fresh_sync,
            _spawn_fallback_task,
            _iso_to_relative,
            get_bid_by_item_id,
        )
    )


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
            cmd_collection_status,
            cmd_wish_list_add,
            cmd_wish_list_from_cache,
            cmd_wish_list_conflicts,
            cmd_wish_list_remove,
            cmd_wish_list_remove_conflicts,
            _split_wish_list_name,
        )
    )
