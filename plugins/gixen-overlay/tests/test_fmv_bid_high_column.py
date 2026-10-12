"""BUI-1242: the `fmv.bid_high` column.

A slab row's published `high` is widened (+/-15%); `bid_high` keeps the
pre-widening top its max_bid rides. Covers the migration (a production-shaped
pre-BUI-1242 DB, including the old five-value pricing_basis CHECK that forces
the BUI-952 rebuild), the upsert's ON CONFLICT treatment, validation, and the
POST/GET round trip.
"""
from __future__ import annotations

import sqlite3

import pytest

from gixen_overlay.db import FMV_PRICING_BASES, create_tables, upsert_fmv


def _fresh_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(
        "CREATE TABLE bids ("
        "id INTEGER PRIMARY KEY, item_id TEXT NOT NULL, max_bid REAL NOT NULL, "
        "fmv_id INTEGER REFERENCES fmv(id) ON DELETE SET NULL)"
    )
    create_tables(conn)
    conn.commit()
    return conn


def _rewind_to_pre_bui1242(conn: sqlite3.Connection, *,
                           old_check: bool = False) -> None:
    """The table the Mac Mini runs before this deploy: no `bid_high`. With
    `old_check`, also the pre-BUI-1028 shape (no `ceiling_cap`, five-value
    pricing_basis CHECK), so the CHECK-widen REBUILD runs after the ALTERs
    and must carry `bid_high` across."""
    sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='fmv'"
    ).fetchone()[0]
    drop: tuple[str, ...] = ("bid_high ",)
    if old_check:
        drop += ("ceiling_cap ",)
    lines = [ln for ln in sql.splitlines()
             if not ln.strip().startswith(drop) and not ln.strip().startswith("--")]
    sql = "\n".join(lines)
    if old_check:
        current = ", ".join(f"'{b}'" for b in FMV_PRICING_BASES)
        assert current in sql
        sql = sql.replace(current, ", ".join(
            f"'{b}'" for b in ("direct", "interpolated", "ladder", "proxy",
                               "lone_sale")))
    assert "bid_high" not in sql
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("DROP TABLE fmv")
    conn.execute(sql)
    conn.execute("PRAGMA foreign_keys=ON")


def _seed_comic(conn):
    conn.execute(
        "INSERT INTO comics (id, title, issue, year) VALUES (1, 'X', '1', 1963)")


@pytest.mark.parametrize("old_check", [False, True])
def test_a_pre_bui1242_db_gains_bid_high_on_migration(old_check):
    conn = _fresh_db()
    _rewind_to_pre_bui1242(conn, old_check=old_check)
    assert "bid_high" not in {r[1] for r in conn.execute("PRAGMA table_info(fmv)")}
    _seed_comic(conn)
    conn.execute(
        "INSERT INTO fmv (id, comic_id, grade, low, high, comps, confidence, "
        "certifier, label, pricing_basis) "
        "VALUES (11, 1, 9.4, 1000, 1200, 3, 'medium', 'cgc', 'universal', 'direct')")
    conn.execute("INSERT INTO bids (id, item_id, max_bid, fmv_id) "
                 "VALUES (5, 'i5', 960, 11)")
    conn.execute("INSERT INTO bid_fmvs (bid_id, fmv_id, is_primary) "
                 "VALUES (5, 11, 1)")
    conn.commit()

    create_tables(conn)
    conn.commit()

    assert "bid_high" in {r[1] for r in conn.execute("PRAGMA table_info(fmv)")}
    # The existing slab row keeps its price and link and gets NULL, which
    # every reader takes as "bid off high" (correct: it was never widened).
    old = conn.execute("SELECT * FROM fmv WHERE id=11").fetchone()
    assert (old["low"], old["high"], old["bid_high"]) == (1000, 1200, None)
    assert conn.execute("SELECT fmv_id FROM bids WHERE id=5").fetchone()[0] == 11
    upsert_fmv(conn, 1, 9.4, low=850, high=1375, comps=3, confidence="medium",
               certifier="cgc", pricing_basis="direct", bid_high=1200)
    create_tables(conn)  # idempotent second pass
    row = conn.execute("SELECT high, bid_high FROM fmv WHERE id=11").fetchone()
    assert (row["high"], row["bid_high"]) == (1375, 1200)


def _slab(conn, **kw):
    args = {"low": 850, "high": 1150, "comps": 1, "confidence": "low",
            "certifier": "cgc", "pricing_basis": "lone_sale", "bid_high": 1000}
    args.update(kw)
    return upsert_fmv(conn, 1, 9.4, **args)


def _bid_high_row(conn):
    return tuple(conn.execute(
        "SELECT low, high, bid_high FROM fmv WHERE comic_id=1 AND grade=9.4 "
        "AND certifier='cgc'").fetchone())


def test_upsert_bid_high_is_replaced_not_coalesced():
    """A re-price that omits `bid_high` (an older comic-fmv, whose high is
    unwidened) CLEARS it, so a stale basis never sits beside a new high. A
    flagged write clears it; a bare n=0 stub leaves it."""
    conn = _fresh_db()
    _seed_comic(conn)
    _slab(conn)
    assert _bid_high_row(conn) == (850, 1150, 1000)
    upsert_fmv(conn, 1, 9.4, comps=0, confidence="low", certifier="cgc")  # stub
    assert _bid_high_row(conn) == (850, 1150, 1000)
    _slab(conn, low=1000, high=1000, bid_high=None)
    assert _bid_high_row(conn) == (1000, 1000, None)
    _slab(conn)
    upsert_fmv(conn, 1, 9.4, flag_reason="ladder_too_thin", certifier="cgc")
    assert _bid_high_row(conn) == (None, None, None)


@pytest.mark.parametrize("kw", [
    {"bid_high": -1},
    {"bid_high": 1200},                                  # above high
    {"certifier": "none", "pricing_basis": "direct"},    # raw row
    {"low": None, "high": None, "flag_reason": "too_sparse"},
])
def test_upsert_rejects_a_malformed_bid_high(kw):
    conn = _fresh_db()
    _seed_comic(conn)
    with pytest.raises(ValueError):
        _slab(conn, **kw)


def test_yearless_merge_carries_bid_high_with_the_high():
    """`_merge_yearless_into_yeared`'s COALESCE: the bid basis travels with the high
    it belongs to, and never lands beside a high it does not."""
    from gixen_overlay import db as overlay_db

    conn = _fresh_db()
    conn.execute("INSERT INTO comics (id, title, issue, year) VALUES (1, 'X', '1', 1963)")
    conn.execute("INSERT INTO comics (id, title, issue, year) VALUES (2, 'X', '1', NULL)")
    upsert_fmv(conn, 2, 9.4, low=850, high=1150, comps=1, confidence="low",
               certifier="cgc", pricing_basis="lone_sale", bid_high=1000)
    upsert_fmv(conn, 1, 9.4, comps=0, confidence="low", certifier="cgc")  # stub
    conn.commit()
    overlay_db._merge_yearless_into_yeared(conn, 2, 1)
    assert _bid_high_row(conn) == (850, 1150, 1000)


# ─── API round trip ──────────────────────────────────────────────────────────

def _body(**over):
    body = {"title": "Slab Book", "issue": "1", "year": 1970, "grade": 9.4,
            "fmv_low": 850.0, "fmv_high": 1150.0, "fmv_comps": 1,
            "fmv_confidence": "low", "certifier": "cgc", "label": "universal",
            "pricing_basis": "lone_sale", "fmv_bid_high": 1000.0}
    body.update(over)
    return body


def test_upsert_comic_stores_and_serves_bid_high(api):
    r = api.post("/api/comics", json=_body())
    assert r.status_code == 200, r.text
    rows = api.get("/api/comics", params={"title": "Slab Book",
                                          "certifier": "cgc"}).json()
    (row,) = rows
    assert (row["fmv_high"], row["fmv_bid_high"]) == (1150.0, 1000.0)


def test_every_row_serves_the_bid_high_key_as_null_when_absent(api):
    """The KEY's presence is comic-fmv's deploy-order probe."""
    api.post("/api/comics", json={
        "title": "Plain Book", "issue": "1", "year": 1970, "grade": 6.0,
        "fmv_low": 10.0, "fmv_high": 20.0})
    (row,) = api.get("/api/comics", params={"title": "Plain Book"}).json()
    assert "fmv_bid_high" in row and row["fmv_bid_high"] is None


@pytest.mark.parametrize("over", [
    {"fmv_bid_high": -5.0},
    {"fmv_bid_high": 1200.0},                          # above fmv_high
    {"certifier": "none", "pricing_basis": None},       # raw row
    {"fmv_flag_reason": "too_sparse", "fmv_low": None, "fmv_high": None},
])
def test_upsert_comic_rejects_a_malformed_bid_high(api, over):
    r = api.post("/api/comics", json=_body(**over))
    assert r.status_code == 422, r.text
