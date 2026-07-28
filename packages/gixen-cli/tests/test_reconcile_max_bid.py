"""BUI-555: tests for scripts/reconcile_max_bid.py, the one-time repair.

This script writes production data (the EM runs it against the live Mac Mini DB
under the BUI-514 ritual), so its two riskiest properties get direct coverage:
the dry run really writes nothing, and an ambiguous item_id is never guessed at.
"""
import importlib.util
import os
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from server.db import init_db  # noqa: E402

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "reconcile_max_bid.py"


@pytest.fixture
def recon():
    """Load the script by path — scripts/ is not a package (it is deliberately
    not shipped in the wheel; the script runs from source out of the workspace
    .venv, see its module docstring)."""
    spec = importlib.util.spec_from_file_location("_reconcile_max_bid", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "recon.db"
    conn = init_db(path)
    conn.close()
    return path


def _seed(db_path, item_id, max_bid, status="PENDING"):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO bids (item_id, max_bid, status) VALUES (?, ?, ?)",
        (item_id, max_bid, status),
    )
    conn.commit()
    conn.close()


def _max_bids(db_path, item_id):
    conn = sqlite3.connect(db_path)
    try:
        return [r[0] for r in conn.execute(
            "SELECT max_bid FROM bids WHERE item_id=? ORDER BY id", (item_id,))]
    finally:
        conn.close()


def _listing(item_id, max_bid):
    return {"item_id": item_id, "max_bid": max_bid, "status": "SCHEDULED"}


def _run(recon, monkeypatch, db_path, snipes, *, apply=False):
    class _FakeClient:
        def list_snipes(self):
            return snipes
    monkeypatch.setattr(recon, "GixenClient", lambda *a, **k: _FakeClient())
    argv = ["--db-path", str(db_path)] + (["--apply"] if apply else [])
    return recon.main(argv)


def test_dry_run_is_the_default_and_writes_nothing(recon, monkeypatch, db_path, capsys):
    """The whole safety posture of this script. A divergence must be REPORTED
    without touching the row unless --apply was passed explicitly."""
    _seed(db_path, "137539988403", 3.90, status="WON")
    rc = _run(recon, monkeypatch, db_path, [_listing("137539988403", "12.00 USD")])
    assert rc == 0
    assert _max_bids(db_path, "137539988403") == [3.90]
    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert "137539988403" in out


def test_apply_repairs_a_resolved_row(recon, monkeypatch, db_path):
    """The rows the per-sync mirror deliberately will not touch: already
    resolved, still listed by Gixen, carrying the stale cap that makes a WON
    show a winning_bid above max_bid."""
    _seed(db_path, "147447825929", 60.0, status="WON")
    rc = _run(recon, monkeypatch, db_path,
              [_listing("147447825929", "335.00 USD")], apply=True)
    assert rc == 0
    assert _max_bids(db_path, "147447825929") == [335.0]


def test_apply_repairs_in_the_downward_direction_too(recon, monkeypatch, db_path):
    _seed(db_path, "147447605357", 80.0, status="LOST")
    _run(recon, monkeypatch, db_path,
         [_listing("147447605357", "30.00 USD")], apply=True)
    assert _max_bids(db_path, "147447605357") == [30.0]


def test_apply_stamps_provenance(recon, monkeypatch, db_path):
    _seed(db_path, "800373497879", 110.0, status="WON")
    _run(recon, monkeypatch, db_path,
         [_listing("800373497879", "50.00 USD")], apply=True)
    conn = sqlite3.connect(db_path)
    try:
        stamp = conn.execute(
            "SELECT max_bid_changed_at FROM bids WHERE item_id='800373497879'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert stamp is not None


def test_ambiguous_item_with_two_live_rows_is_never_guessed(recon, monkeypatch,
                                                            db_path, capsys):
    """A re-listed eBay id has two non-tombstoned rows whose caps are
    LEGITIMATELY different — one per auction. Gixen's single listing cannot say
    which it refers to, so the script must refuse rather than rewrite a real
    historical bid (the BUI-500 'assert exactly one match' lesson)."""
    _seed(db_path, "111222333", 20.0, status="LOST")
    _seed(db_path, "111222333", 45.0, status="PENDING")
    rc = _run(recon, monkeypatch, db_path,
              [_listing("111222333", "99.00 USD")], apply=True)
    assert rc == 0
    assert _max_bids(db_path, "111222333") == [20.0, 45.0]   # both untouched
    assert "AMBIGUOUS" in capsys.readouterr().out


def test_tombstoned_rows_are_invisible_to_the_repair(recon, monkeypatch, db_path):
    """A REMOVED row is a soft-delete, not an auction result. It must neither be
    repaired nor make its item_id look ambiguous — otherwise every purged item
    would block the repair of its live twin."""
    _seed(db_path, "444555666", 5.0, status="REMOVED")
    _seed(db_path, "444555666", 20.0, status="PENDING")
    _run(recon, monkeypatch, db_path,
         [_listing("444555666", "35.00 USD")], apply=True)
    assert _max_bids(db_path, "444555666") == [5.0, 35.0]


def test_unreadable_gixen_max_bid_is_skipped_not_zeroed(recon, monkeypatch, db_path):
    _seed(db_path, "777888999", 25.0, status="WON")
    _run(recon, monkeypatch, db_path,
         [_listing("777888999", "N/A")], apply=True)
    assert _max_bids(db_path, "777888999") == [25.0]


def test_matching_values_are_left_alone(recon, monkeypatch, db_path, capsys):
    _seed(db_path, "123123123", 25.0, status="PENDING")
    rc = _run(recon, monkeypatch, db_path,
              [_listing("123123123", "25.00 USD")], apply=True)
    assert rc == 0
    assert "Nothing to do" in capsys.readouterr().out


def test_empty_gixen_list_refuses_to_conclude_anything(recon, monkeypatch,
                                                       db_path, capsys):
    """An empty scrape is far likelier a session/anti-bot glitch than an empty
    account. Exiting non-zero keeps it from reading as a clean 'all reconciled'."""
    _seed(db_path, "123123123", 25.0)
    rc = _run(recon, monkeypatch, db_path, [], apply=True)
    assert rc == 1
    assert "EMPTY" in capsys.readouterr().out


def test_missing_db_exits_nonzero(recon, monkeypatch, tmp_path):
    rc = _run(recon, monkeypatch, tmp_path / "nope.db", [_listing("1", "2.00")])
    assert rc == 2
