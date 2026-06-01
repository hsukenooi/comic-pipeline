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
