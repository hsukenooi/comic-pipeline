"""BUI-933: a raising ``register_db_tables`` hook must abort startup.

Before this fix, ``_invoke_db_tables_isolated`` (``gixen/plugins.py``) logged
a plugin's ``register_db_tables`` exception, rolled back that plugin's
savepoint, and returned normally — so a failed overlay migration (e.g.
``_migrate_year_nullable`` or the BUI-924 fmv rebuild) left the comics server
running with ``/api/comics/*`` broken instead of refusing to boot. This is
the loud-failure fix: the function now raises ``PluginDBTablesError`` after
every plugin has had a chance to migrate, and the ``lifespan`` in
``server/main.py`` does not catch it, so FastAPI/Uvicorn abort startup.

Two levels, mirroring how the rest of this suite tests plugin hooks
(test_server_policy.py's ``make_plugin_manager()`` pattern for the unit
level, test_server_api.py's ``_install_plugins``/entry-points pattern for
the integration level):

  - Unit: ``_invoke_db_tables_isolated`` itself, against a real sqlite3
    connection, with fake ``@hookimpl`` plugins registered directly.
  - Integration: booting the real ``server.main.app`` through
    ``TestClient``'s lifespan (``__enter__``/``with``) with a raising
    plugin wired in via ``gixen.plugins.entry_points`` — the same path
    production startup takes.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import sys
import types
from importlib.metadata import EntryPoint
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from gixen.plugins import (  # noqa: E402
    PluginDBTablesError,
    hookimpl,
    make_plugin_manager,
    _invoke_db_tables_isolated,
)


class _GoodTablePlugin:
    """Creates one namespaced table successfully."""

    @hookimpl
    def register_db_tables(self, conn):
        conn.execute("CREATE TABLE good_plugin_table (id INTEGER PRIMARY KEY)")


class _RaisingTablePlugin:
    """Simulates a failed migration, e.g. an ALTER TABLE against a schema
    the plugin's own migration helper expected but that isn't there."""

    @hookimpl
    def register_db_tables(self, conn):
        conn.execute("CREATE TABLE bad_plugin_partial (id INTEGER PRIMARY KEY)")
        raise RuntimeError("simulated failed overlay migration (BUI-933)")


def _tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    return {r[0] for r in rows}


# ---------------------------------------------------------------------------
# Unit: _invoke_db_tables_isolated
# ---------------------------------------------------------------------------

def test_raising_register_db_tables_raises_plugin_db_tables_error():
    conn = sqlite3.connect(":memory:")
    pm = make_plugin_manager()
    pm.register(_RaisingTablePlugin(), name="bad-plugin")

    with pytest.raises(PluginDBTablesError) as excinfo:
        _invoke_db_tables_isolated(pm, conn, logger=logging.getLogger("server.main"))

    assert "bad-plugin" in str(excinfo.value)


def test_raising_plugin_own_ddl_is_rolled_back():
    """The failing plugin's own savepoint is still rolled back to — its
    partial DDL must not survive even though the whole call now raises."""
    conn = sqlite3.connect(":memory:")
    pm = make_plugin_manager()
    pm.register(_RaisingTablePlugin(), name="bad-plugin")

    with pytest.raises(PluginDBTablesError):
        _invoke_db_tables_isolated(pm, conn, logger=logging.getLogger("server.main"))

    assert "bad_plugin_partial" not in _tables(conn)


def test_good_sibling_ddl_survives_a_later_bad_plugin():
    """A GOOD plugin's already-committed (RELEASEd) DDL must not be undone
    just because a later sibling's register_db_tables raised — the raise
    changes what happens AFTER the loop, not the per-plugin isolation."""
    conn = sqlite3.connect(":memory:")
    pm = make_plugin_manager()
    # Registration/hook-firing order is by entry-point name (alphabetical) in
    # load_plugins(), but _invoke_db_tables_isolated itself just iterates
    # pm.list_name_plugin() in registration order — register the good one
    # first so it runs, and lands, before the bad one raises.
    pm.register(_GoodTablePlugin(), name="a-good-plugin")
    pm.register(_RaisingTablePlugin(), name="b-bad-plugin")

    with pytest.raises(PluginDBTablesError) as excinfo:
        _invoke_db_tables_isolated(conn=conn, pm=pm, logger=logging.getLogger("server.main"))

    assert "good_plugin_table" in _tables(conn)
    assert "b-bad-plugin" in str(excinfo.value)
    assert "a-good-plugin" not in str(excinfo.value)


def test_all_plugins_succeeding_returns_names_and_does_not_raise():
    conn = sqlite3.connect(":memory:")
    pm = make_plugin_manager()
    pm.register(_GoodTablePlugin(), name="a-good-plugin")

    succeeded = _invoke_db_tables_isolated(pm, conn, logger=logging.getLogger("server.main"))

    assert succeeded == ["a-good-plugin"]
    assert "good_plugin_table" in _tables(conn)


# ---------------------------------------------------------------------------
# Integration: real lifespan startup through TestClient
# ---------------------------------------------------------------------------

def _install_plugins(monkeypatch, plugins: dict):
    """Mirrors test_server_api.py's helper: wire fake modules in as
    gixen.plugins entry points so load_plugins() (called from the real
    lifespan) discovers them exactly like a pip-installed plugin."""
    eps = []
    for name, mod in plugins.items():
        module_name = f"_test_dbtables_{name.replace('-', '_')}"
        monkeypatch.setitem(sys.modules, module_name, mod)
        eps.append(EntryPoint(name=name, value=module_name, group="gixen.plugins"))
    monkeypatch.setattr(
        "gixen.plugins.entry_points",
        lambda group: eps if group == "gixen.plugins" else [],
    )


def _make_mock_gixen():
    m = MagicMock()
    m.list_snipes.return_value = []
    m.add_snipe.return_value = None
    m.modify_snipe.return_value = None
    m.remove_snipe.return_value = True
    m.purge_completed.return_value = None
    return m


def test_lifespan_aborts_startup_when_a_plugin_register_db_tables_raises(tmp_path, monkeypatch):
    """The acceptance criterion: a raising register_db_tables hook aborts
    server startup rather than the server coming up with a broken plugin
    schema. Entering TestClient's context runs the real lifespan — the same
    startup path Uvicorn drives in production."""
    raising_mod = types.ModuleType("_raising_db_tables_stub")

    @hookimpl
    def register_db_tables(conn):
        raise RuntimeError("simulated failed overlay migration (BUI-933)")

    raising_mod.register_db_tables = register_db_tables
    _install_plugins(monkeypatch, {"raising-db-tables-plugin": raising_mod})

    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("GIXEN_USERNAME", "u")
    monkeypatch.setenv("GIXEN_PASSWORD", "p")
    monkeypatch.setenv("GIXEN_SYNC_ENABLED", "false")
    monkeypatch.setenv("LOCAL_SNIPER_ENABLED", "false")

    mock = _make_mock_gixen()
    with patch("server.main.GixenClient", return_value=mock):
        # Import fresh so module-level state (e.g. _SERVER_GIT_SHA) isn't an
        # issue; server.main.app is a module-level FastAPI instance whose
        # lifespan is what we're driving here.
        from server.main import app
        from fastapi.testclient import TestClient

        with pytest.raises(PluginDBTablesError) as excinfo:
            with TestClient(app):
                pytest.fail("startup should have aborted before yielding a usable client")

    assert "raising-db-tables-plugin" in str(excinfo.value)


def test_lifespan_starts_normally_when_no_plugin_raises(tmp_path, monkeypatch):
    """Control: a well-behaved plugin's DDL does not prevent startup — this
    guards against the fix being overzealous (e.g. raising unconditionally)."""
    good_mod = types.ModuleType("_good_db_tables_stub")

    @hookimpl
    def register_db_tables(conn):
        conn.execute("CREATE TABLE fine_plugin_table (id INTEGER PRIMARY KEY)")

    good_mod.register_db_tables = register_db_tables
    _install_plugins(monkeypatch, {"good-db-tables-plugin": good_mod})

    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("GIXEN_USERNAME", "u")
    monkeypatch.setenv("GIXEN_PASSWORD", "p")
    monkeypatch.setenv("GIXEN_SYNC_ENABLED", "false")
    monkeypatch.setenv("LOCAL_SNIPER_ENABLED", "false")

    mock = _make_mock_gixen()
    with patch("server.main.GixenClient", return_value=mock):
        from server.main import app
        from fastapi.testclient import TestClient

        with TestClient(app) as client:
            r = client.get("/health")
            assert r.status_code == 200
            assert r.json()["status"] == "ok"

    conn = sqlite3.connect(str(tmp_path / "test.db"))
    assert "fine_plugin_table" in _tables(conn)
    conn.close()
