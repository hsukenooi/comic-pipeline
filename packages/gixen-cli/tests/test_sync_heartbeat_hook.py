"""BUI-624: the `on_sync_observed` hookspec and its firing site in _sync_gixen.

`gixen-sync` is the one heartbeat job whose ping cannot be an HTTP call — this
package has no import edge to the overlay that owns the contract table, and the
ping has to land at exactly the moment BUI-604's `_stamp_sync_observed` marks.
It leaves through a hook fired INSIDE the apply phase's write_transaction().

That placement is what these tests defend, and there are two independent things
to defend:

* the ping is honest — it fires on a completed pass (including a pass that saw
  zero snipes) and never on a cycle that bailed before the write phase;
* the ping is harmless — a plugin that raises, or writes garbage, cannot turn a
  healthy sync into a _sync_loop backoff or an api_sync 500. `_sync_gixen`'s own
  comment states that invariant absolutely; a heartbeat is I/O, so it has to be
  proven rather than assumed.
"""

import asyncio
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import server.main as m
from gixen.plugins import (
    _invoke_sync_observed,
    hookimpl,
    make_plugin_manager,
)
from gixen_client import GixenConnectionError
from server.db import init_db, write_transaction


class _RecordingPlugin:
    """Stands in for gixen_overlay's hookimpl: writes a row, records the args."""

    def __init__(self, *, raise_on_call: bool = False):
        self.calls: list[int] = []
        self.raise_on_call = raise_on_call

    @hookimpl
    def on_sync_observed(self, conn: sqlite3.Connection, snipe_count: int) -> None:
        self.calls.append(snipe_count)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS fake_heartbeats (job TEXT, n INTEGER)"
        )
        conn.execute(
            "INSERT INTO fake_heartbeats (job, n) VALUES ('gixen-sync', ?)",
            (snipe_count,),
        )
        if self.raise_on_call:
            raise RuntimeError("plugin exploded after writing")


def _pm(plugin) -> object:
    pm = make_plugin_manager()
    pm.register(plugin, name="recorder")
    return pm


def _heartbeat_rows(path: Path) -> list[tuple]:
    conn = sqlite3.connect(path)
    try:
        return conn.execute(
            "SELECT job, n FROM fake_heartbeats ORDER BY rowid"
        ).fetchall()
    except sqlite3.OperationalError:
        return []  # table never created — the hook never wrote
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# _invoke_sync_observed in isolation
# ---------------------------------------------------------------------------


def test_hook_receives_the_transaction_and_the_count(tmp_path):
    path = tmp_path / "hook.db"
    init_db(path).close()
    plugin = _RecordingPlugin()
    with write_transaction(path) as wconn:
        _invoke_sync_observed(_pm(plugin), wconn, 3, logger=m.logger)
    assert plugin.calls == [3]
    assert _heartbeat_rows(path) == [("gixen-sync", 3)]


def test_pm_none_is_a_noop(tmp_path):
    """A server started without a plugin manager (or a unit test calling
    _sync_gixen directly) must not blow up on the heartbeat."""
    path = tmp_path / "nopm.db"
    init_db(path).close()
    with write_transaction(path) as wconn:
        _invoke_sync_observed(None, wconn, 1, logger=m.logger)  # must not raise


def test_a_raising_hook_does_not_escape_and_rolls_back_only_itself(tmp_path):
    """The invariant _sync_gixen's comment states, proven at the helper level.

    The plugin writes, THEN raises — so a savepoint that did not roll back
    would leave a heartbeat behind for a hook that failed, and an unguarded
    raise would abort the caller's own DML. Neither may happen: the caller's
    insert commits, the plugin's does not.
    """
    path = tmp_path / "raise.db"
    init_db(path).close()
    plugin = _RecordingPlugin(raise_on_call=True)

    with write_transaction(path) as wconn:
        wconn.execute(
            "INSERT INTO bids (item_id, max_bid, status) "
            "VALUES ('624000001', 10.0, 'PENDING')"
        )
        _invoke_sync_observed(_pm(plugin), wconn, 7, logger=m.logger)

    assert plugin.calls == [7], "the hook should have been called"
    assert _heartbeat_rows(path) == [], (
        "a hook that raised must not leave its own write behind"
    )
    conn = sqlite3.connect(path)
    assert conn.execute(
        "SELECT COUNT(*) FROM bids WHERE item_id='624000001'"
    ).fetchone()[0] == 1, "the caller's DML must survive a raising hook"
    conn.close()


def test_heartbeat_dies_with_a_rolled_back_transaction(tmp_path):
    """The other direction, and the reason the hook fires INSIDE the write.

    A heartbeat that outlived a rolled-back apply phase would assert a sync
    that did not happen — the fails-green shape, re-created inside the very
    mechanism built to detect it.

    The caller writes first, as a real apply phase does: with DML pending the
    savepoint NESTS, so the plugin's write is bound to the caller's fate. See
    the next test for what happens when it does not.
    """
    path = tmp_path / "rollback.db"
    init_db(path).close()
    plugin = _RecordingPlugin()

    with pytest.raises(RuntimeError, match="apply phase blew up"):
        with write_transaction(path) as wconn:
            wconn.execute(
                "INSERT INTO bids (item_id, max_bid, status) "
                "VALUES ('624000002', 10.0, 'PENDING')"
            )
            _invoke_sync_observed(_pm(plugin), wconn, 2, logger=m.logger)
            raise RuntimeError("apply phase blew up")

    assert plugin.calls == [2]
    assert _heartbeat_rows(path) == []


def test_savepoint_is_outermost_when_the_caller_wrote_nothing(tmp_path):
    """Pins the caveat rather than pretending it away.

    A SAVEPOINT taken with no DML pending is the outermost one, so its RELEASE
    commits — the plugin's write survives a caller that later rolls back. That
    is why `_invoke_sync_observed` must stay the LAST statement in the caller's
    transaction, and why both its docstring and _sync_gixen's call site say so.

    If this test ever starts failing because the write vanished, someone made
    the guarantee unconditional — good; delete this test and simplify those two
    comments. If it fails because a NEW statement runs after the hook in
    _sync_gixen, that statement is now able to fail with the heartbeat already
    committed, which is the bug this pin exists to make visible.
    """
    path = tmp_path / "outermost.db"
    init_db(path).close()
    plugin = _RecordingPlugin()

    with pytest.raises(RuntimeError, match="nothing was written first"):
        with write_transaction(path) as wconn:
            _invoke_sync_observed(_pm(plugin), wconn, 0, logger=m.logger)
            raise RuntimeError("nothing was written first")

    assert _heartbeat_rows(path) == [("gixen-sync", 0)]


# ---------------------------------------------------------------------------
# The firing site inside _sync_gixen
# ---------------------------------------------------------------------------


@pytest.fixture
def sync_env(tmp_path, monkeypatch):
    """Minimal _sync_gixen environment: a real DB, a mocked Gixen client, and
    a plugin manager reachable the way _sync_gixen reaches it."""
    path = tmp_path / "sync.db"
    conn = init_db(path)
    monkeypatch.setattr(m, "_db", conn)
    monkeypatch.setattr(m, "_db_path", path)
    monkeypatch.setattr(m, "_ebay_fetch_bin", lambda: None)
    plugin = _RecordingPlugin()
    m.app.state.plugin_manager = _pm(plugin)
    yield conn, path, plugin
    m.app.state.plugin_manager = None
    conn.close()


def _run_sync(conn, client):
    async def go():
        m._write_lock = asyncio.Lock()
        return await m._sync_gixen(conn, client)
    return asyncio.run(go())


def test_completed_pass_pings_even_with_zero_snipes(sync_env):
    """"Reached Gixen, nothing live right now" is a SUCCESS.

    This is the whole distinction the heartbeat exists to draw. A quiet week
    with no active snipes must not look like a dead sync loop.
    """
    conn, path, plugin = sync_env
    client = MagicMock()
    client.list_snipes.return_value = []

    _run_sync(conn, client)

    assert plugin.calls == [0]
    assert _heartbeat_rows(path) == [("gixen-sync", 0)]


def test_pass_with_snipes_reports_the_count(sync_env):
    conn, path, plugin = sync_env
    client = MagicMock()
    client.list_snipes.return_value = [
        {"item_id": "624000010", "max_bid": "12.00", "title": "ASM #300"},
    ]

    _run_sync(conn, client)

    assert plugin.calls == [1]


def test_unreachable_gixen_does_not_ping(sync_env):
    """A GixenConnectionError is "I am blind", not a healthy observation.

    _sync_gixen returns [] on this path without entering the apply phase, and
    the heartbeat must stay silent so the watchdog can go stale — the outage IS
    the thing being watched for.
    """
    conn, path, plugin = sync_env
    client = MagicMock()
    client.list_snipes.side_effect = GixenConnectionError("network down")

    assert _run_sync(conn, client) == []

    assert plugin.calls == []
    assert _heartbeat_rows(path) == []


def test_a_raising_plugin_cannot_fail_the_sync(sync_env, monkeypatch):
    """The load-bearing safety property, end to end.

    A heartbeat that could raise past the apply phase would convert a healthy
    cycle into a _sync_loop backoff (or an api_sync 500) — a watchdog capable
    of blacking out the loop it reports on. The sync must return its snipes and
    still stamp BUI-604's observer state.
    """
    conn, path, _plugin = sync_env
    m.app.state.plugin_manager = _pm(_RecordingPlugin(raise_on_call=True))
    monkeypatch.setattr(m, "_last_sync_ok_at", None)
    client = MagicMock()
    client.list_snipes.return_value = [
        {"item_id": "624000020", "max_bid": "9.00", "title": "FF #48"},
    ]

    snipes = _run_sync(conn, client)

    assert len(snipes) == 1, "the sync's own return value is unaffected"
    assert m._last_sync_ok_at is not None, (
        "_stamp_sync_observed must still run — the cycle WAS healthy"
    )
    assert _heartbeat_rows(path) == [], "the failed hook left nothing behind"
    # And the cycle's own write landed.
    assert conn.execute(
        "SELECT COUNT(*) FROM bids WHERE item_id='624000020'"
    ).fetchone()[0] == 1
