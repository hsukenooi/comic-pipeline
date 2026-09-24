"""Tests for the paired FMV band-set comparison (BUI-979)."""
from __future__ import annotations

import json

import pytest

from gixen_overlay.band_compare import (
    band_comparison,
    main,
    successive_snapshot_band_sets,
    winkler_score,
)

from .test_fmv_accuracy_report import _bid, _comic_and_fmv, _db, _history, _link

# ---------------------------------------------------------------------------
# Winkler math
# ---------------------------------------------------------------------------


def test_winkler_hand_computed_below_inside_above():
    # Band [40, 60], alpha 0.5 -> penalty multiplier 2/0.5 = 4.
    assert winkler_score(40, 60, 30, 0.5) == 20 + 4 * 10  # below: 60
    assert winkler_score(40, 60, 50, 0.5) == 20  # inside: width
    assert winkler_score(40, 60, 65, 0.5) == 20 + 4 * 5  # above: 40
    # alpha 0.2 -> multiplier 10.
    assert winkler_score(40, 60, 65, 0.2) == pytest.approx(20 + 10 * 5)


@pytest.mark.parametrize("y", [40, 47.5, 60])
def test_covered_outcome_scores_exactly_the_width(y):
    assert winkler_score(40, 60, y) == 20


def test_widening_cannot_beat_a_band_that_already_covers():
    # y covered by [45, 55]; any wider band scores worse.
    assert winkler_score(40, 60, 50) > winkler_score(45, 55, 50)


def test_widening_to_cover_an_outside_outcome_never_pays_more_than_the_miss():
    # Extending high to just cover y costs the extension once; staying short
    # costs 2/alpha times it. Widening PAST y only adds width.
    assert winkler_score(40, 70, 70) < winkler_score(40, 60, 70)
    assert winkler_score(40, 90, 70) > winkler_score(40, 70, 70)


def test_winkler_rejects_bad_alpha_and_inverted_band():
    with pytest.raises(ValueError):
        winkler_score(40, 60, 50, alpha=1.0)
    with pytest.raises(ValueError):
        winkler_score(60, 40, 50)


# ---------------------------------------------------------------------------
# Paired comparison on the BUI-977 selection
# ---------------------------------------------------------------------------


def _resolved(conn, item_id, price, *, status="LOST", lot=False):
    _comic, fmv_id = _comic_and_fmv(conn, title=f"T{item_id}", low=10, high=20)
    bid_id = _bid(conn, item_id, status=status, winning_bid=price)
    _link(conn, bid_id, fmv_id)
    if lot:
        _c2, fmv2 = _comic_and_fmv(conn, title=f"T{item_id}b", low=10, high=20)
        _link(conn, bid_id, fmv2, is_primary=False)
    return bid_id


def test_pairs_only_auctions_present_in_both_sets():
    conn = _db()
    b1 = _resolved(conn, "1", 50)
    b2 = _resolved(conn, "2", 100)
    b3 = _resolved(conn, "3", 80)
    set_a = {b1: (40, 60), b2: (90, 110), b3: (70, 90)}
    set_b = {b1: (45, 55), b2: (None, 110), 999: (1, 2)}  # b2 invalid, b3 absent
    report = band_comparison(conn, set_a, set_b, labels=("old", "new"))
    assert report["n_resolved"] == 3
    assert report["n_a"] == 3
    assert report["n_b"] == 1
    assert report["n_paired"] == 1
    assert report["old"]["n"] == report["new"]["n"] == 1
    assert report["old"]["winkler_median"] == 20
    assert report["new"]["winkler_median"] == 10
    assert report["new"]["winkler_scaled_median"] == pytest.approx(10 / 50)
    assert report["paired"] == {
        "old_better": 0, "new_better": 1, "ties": 0,
        "median_scaled_diff": pytest.approx((10 - 20) / 50),
    }


def test_unresolved_and_lot_auctions_are_never_scored():
    conn = _db()
    good = _resolved(conn, "1", 50)
    pending = _resolved(conn, "2", 50, status="PENDING")
    lot = _resolved(conn, "3", 50, lot=True)
    bands = {good: (40, 60), pending: (40, 60), lot: (40, 60)}
    report = band_comparison(conn, bands, bands)
    assert report["n_resolved"] == 1
    assert report["n_paired"] == 1


def test_hit_rates_reuse_fixed_window_metrics():
    conn = _db()
    b1 = _resolved(conn, "1", 50)   # mid 50: within 10%, in band
    b2 = _resolved(conn, "2", 130)  # mid 100: +30%, above band
    report = band_comparison(conn, {b1: (40, 60), b2: (90, 110)},
                             {b1: (40, 60), b2: (90, 110)}, include_rows=True)
    a = report["a"]
    assert a["share_within_10pct"] == 50.0
    assert a["share_within_20pct"] == 50.0
    assert a["in_band_pct"] == 50.0
    assert a["above_band_pct"] == 50.0
    assert a["mdape_pct"] == pytest.approx(15.0)
    # Winkler: 20 (covered) and 20 + 4*20 = 100.
    assert sorted(r["a_winkler"] for r in report["rows"]) == [20, 100]
    assert report["paired"]["ties"] == 2


def test_empty_pairing_reports_none_not_zero():
    conn = _db()
    _resolved(conn, "1", 50)
    report = band_comparison(conn, {}, {})
    assert report["n_paired"] == 0
    assert report["a"]["winkler_scaled_median"] is None
    assert report["paired"]["median_scaled_diff"] is None


def test_labels_must_differ():
    with pytest.raises(ValueError):
        band_comparison(_db(), {}, {}, labels=("x", "x"))


# ---------------------------------------------------------------------------
# Leakage-free successive snapshots
# ---------------------------------------------------------------------------


def test_successive_snapshots_use_only_pre_bid_snapshots():
    conn = _db()
    comic, fmv_id = _comic_and_fmv(conn, low=10, high=20)
    bid = _bid(conn, "1", status="LOST", winning_bid=55,
               added_at="2026-03-01 00:00:00")
    _link(conn, bid, fmv_id)
    _history(conn, comic, 9.0, low=30, high=40, recorded_at="2026-02-01T00:00:00+00:00")
    _history(conn, comic, 9.0, low=45, high=60, recorded_at="2026-02-20T00:00:00+00:00")
    # After the bid (and after the auction): must never be used.
    _history(conn, comic, 9.0, low=54, high=56, recorded_at="2026-03-20T00:00:00+00:00")
    prior, in_force = successive_snapshot_band_sets(conn)
    assert prior == {bid: (30, 40)}
    assert in_force == {bid: (45, 60)}


def test_successive_snapshots_skip_single_snapshot_and_current_fmv_rows():
    conn = _db()
    c1, f1 = _comic_and_fmv(conn, title="A", low=10, high=20)
    b1 = _bid(conn, "1", status="LOST", winning_bid=55)
    _link(conn, b1, f1)
    _history(conn, c1, 9.0, low=30, high=40, recorded_at="2026-02-01T00:00:00+00:00")
    _c2, f2 = _comic_and_fmv(conn, title="B", low=10, high=20)  # no history
    b2 = _bid(conn, "2", status="LOST", winning_bid=55)
    _link(conn, b2, f2)
    assert successive_snapshot_band_sets(conn) == ({}, {})


# ---------------------------------------------------------------------------
# CLI (read-only)
# ---------------------------------------------------------------------------


def test_cli_reads_band_files_from_a_read_only_db(tmp_path, capsys):
    import sqlite3

    from gixen_overlay.db import create_tables

    from .test_fmv_accuracy_report import _make_db

    path = tmp_path / "db.sqlite"
    mem = _make_db()
    create_tables(mem)
    bid = _resolved(mem, "1", 50)
    disk = sqlite3.connect(path)
    mem.backup(disk)
    disk.close()
    (tmp_path / "a.json").write_text(json.dumps({str(bid): [40, 60]}))
    (tmp_path / "b.json").write_text(json.dumps({str(bid): {"low": 45, "high": 55}}))
    assert main(["--db", str(path), "--a", str(tmp_path / "a.json"),
                 "--b", str(tmp_path / "b.json")]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["n_paired"] == 1
    assert out["b"]["winkler_median"] == 10
