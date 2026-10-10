"""BUI-1219: the `fmv.median` + `fmv.bid_tier` columns.

The migration (old DB → new columns, NULL on existing rows, carried across
the BUI-952 CHECK-widen rebuild), the upsert's ON CONFLICT treatment (a
priced write REPLACES the pair, so a stale median never sits beside a new
high), its validation, and the POST/GET round trip.
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


def _drop_the_median_columns(conn: sqlite3.Connection, *,
                             also_ceiling: bool = False) -> None:
    """Rewind to the pre-BUI-1219 table (what the Mac Mini runs before this
    deploy): no `median`/`bid_tier`. With `also_ceiling`, also the
    pre-BUI-1028 shape (no `ceiling_cap`, five-value CHECK), so the CHECK
    widen REBUILD runs after the ALTERs and must carry the new columns."""
    sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='fmv'"
    ).fetchone()[0]
    drop: tuple[str, ...] = ("median ", "bid_tier ")
    if also_ceiling:
        drop += ("ceiling_cap ",)
    lines = [ln for ln in sql.splitlines()
             if not ln.strip().startswith(drop) and not ln.strip().startswith("--")]
    sql = "\n".join(lines)
    if also_ceiling:
        current = ", ".join(f"'{b}'" for b in FMV_PRICING_BASES)
        assert current in sql
        sql = sql.replace(current, ", ".join(
            f"'{b}'" for b in ("direct", "interpolated", "ladder", "proxy",
                               "lone_sale")))
    assert "bid_tier" not in sql
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("DROP TABLE fmv")
    conn.execute(sql)
    conn.execute("PRAGMA foreign_keys=ON")


def _seed_comic(conn):
    conn.execute(
        "INSERT INTO comics (id, title, issue, year) VALUES (1, 'X', '1', 1963)")


@pytest.mark.parametrize("also_ceiling", [False, True])
def test_a_pre_bui1219_db_gains_median_and_bid_tier_on_migration(also_ceiling):
    conn = _fresh_db()
    _drop_the_median_columns(conn, also_ceiling=also_ceiling)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(fmv)")}
    assert not {"median", "bid_tier"} & cols
    _seed_comic(conn)
    conn.execute(
        "INSERT INTO fmv (id, comic_id, grade, low, high, comps, confidence) "
        "VALUES (11, 1, 6.0, 100, 140, 7, 'high')")
    conn.execute("INSERT INTO bids (id, item_id, max_bid, fmv_id) "
                 "VALUES (5, 'i5', 110, 11)")
    conn.execute("INSERT INTO bid_fmvs (bid_id, fmv_id, is_primary) "
                 "VALUES (5, 11, 1)")
    conn.commit()

    create_tables(conn)
    conn.commit()

    cols = {r[1] for r in conn.execute("PRAGMA table_info(fmv)")}
    assert {"median", "bid_tier", "ceiling_cap"} <= cols
    # The existing row keeps its price and link, and gets NULLs (today's cap).
    old = conn.execute("SELECT * FROM fmv WHERE id=11").fetchone()
    assert (old["low"], old["high"], old["median"], old["bid_tier"]) == (
        100, 140, None, None)
    assert conn.execute("SELECT fmv_id FROM bids WHERE id=5").fetchone()[0] == 11
    # Writable straight away, and idempotent on a second pass.
    upsert_fmv(conn, 1, 6.0, low=100, high=140, comps=7, confidence="high",
               median=120, bid_tier="VERY HIGH")
    create_tables(conn)
    row = conn.execute("SELECT median, bid_tier FROM fmv WHERE id=11").fetchone()
    assert (row["median"], row["bid_tier"]) == (120, "VERY HIGH")


def _median_row(conn):
    return tuple(conn.execute(
        "SELECT low, high, median, bid_tier FROM fmv WHERE comic_id=1 AND grade=6.0"
    ).fetchone())


def test_upsert_median_pair_is_replaced_not_coalesced():
    """A re-price that omits the pair (an older comic-fmv, or a row that is
    now interpolated) must CLEAR a stale median, never keep it beside a new
    high. A flagged write clears it; a bare n=0 stub leaves it."""
    conn = _fresh_db()
    _seed_comic(conn)
    upsert_fmv(conn, 1, 6.0, low=100, high=140, comps=7, confidence="high",
               median=120, bid_tier="VERY HIGH")
    assert _median_row(conn) == (100, 140, 120, "VERY HIGH")
    upsert_fmv(conn, 1, 6.0, comps=0, confidence="low")           # bare stub
    assert _median_row(conn) == (100, 140, 120, "VERY HIGH")
    upsert_fmv(conn, 1, 6.0, low=200, high=300, comps=7, confidence="high")
    assert _median_row(conn) == (200, 300, None, None)
    upsert_fmv(conn, 1, 6.0, low=100, high=140, comps=7, confidence="medium",
               median=110, bid_tier="MEDIUM")
    upsert_fmv(conn, 1, 6.0, flag_reason="one_sided", confidence="low")
    assert _median_row(conn) == (None, None, None, None)


@pytest.mark.parametrize("kwargs", [
    {"median": 120},                                   # half a pair
    {"bid_tier": "HIGH"},
    {"median": 120, "bid_tier": "SUPER"},              # unknown tier
    {"median": -1, "bid_tier": "HIGH"},
])
def test_upsert_rejects_a_malformed_median_pair(kwargs):
    conn = _fresh_db()
    _seed_comic(conn)
    with pytest.raises(ValueError):
        upsert_fmv(conn, 1, 6.0, low=100, high=140, comps=7,
                   confidence="high", **kwargs)


def test_upsert_rejects_a_median_pair_on_an_unpriced_row():
    conn = _fresh_db()
    _seed_comic(conn)
    with pytest.raises(ValueError):
        upsert_fmv(conn, 1, 6.0, flag_reason="one_sided", median=120,
                   bid_tier="HIGH")


@pytest.mark.parametrize("kwargs", [
    {"certifier": "cgc"},
    {"notes": "CGC proxy (raw ~ 0.50-0.55x slab)"},     # derived basis 'proxy'
    {"pricing_basis": "interpolated"},
])
def test_upsert_drops_the_median_pair_off_a_non_raw_direct_row(kwargs):
    """Dropped (stored NULL, today's cap), never raised: the basis may be
    DERIVED from notes, and that should not fail the whole write."""
    conn = _fresh_db()
    _seed_comic(conn)
    fmv_id = upsert_fmv(conn, 1, 6.0, low=100, high=140, comps=7,
                        confidence="high", median=120, bid_tier="HIGH",
                        **kwargs)
    row = conn.execute("SELECT median, bid_tier, high FROM fmv WHERE id=?",
                       (fmv_id,)).fetchone()
    assert (row["median"], row["bid_tier"], row["high"]) == (None, None, 140)


# ─── API round trip ──────────────────────────────────────────────────────────

def _body(**over):
    body = {"title": "Median Book", "issue": "1", "year": 1970, "grade": 6.0,
            "fmv_low": 100.0, "fmv_high": 140.0, "fmv_comps": 7,
            "fmv_confidence": "high", "fmv_median": 120.0,
            "fmv_bid_tier": "VERY HIGH"}
    body.update(over)
    return body


def test_upsert_comic_stores_and_serves_the_median_pair(api):
    r = api.post("/api/comics", json=_body())
    assert r.status_code == 200, r.text
    (row,) = api.get("/api/comics", params={"title": "Median Book"}).json()
    assert (row["fmv_median"], row["fmv_bid_tier"]) == (120.0, "VERY HIGH")
    # An older client re-pricing without the pair clears it.
    body = _body(fmv_high=200.0)
    del body["fmv_median"], body["fmv_bid_tier"]
    assert api.post("/api/comics", json=body).status_code == 200
    (row,) = api.get("/api/comics", params={"title": "Median Book"}).json()
    assert (row["fmv_high"], row["fmv_median"], row["fmv_bid_tier"]) == (
        200.0, None, None)


def test_every_row_serves_the_median_keys_as_null_when_absent(api):
    api.post("/api/comics", json={
        "title": "Plain Book", "issue": "1", "year": 1970, "grade": 6.0,
        "fmv_low": 10.0, "fmv_high": 20.0})
    (row,) = api.get("/api/comics", params={"title": "Plain Book"}).json()
    assert row["fmv_median"] is None and row["fmv_bid_tier"] is None


@pytest.mark.parametrize("over", [
    {"fmv_median": None},                               # tier without median
    {"fmv_bid_tier": None},                             # median without tier
    {"fmv_bid_tier": "SUPER"},
    {"fmv_median": -5.0},
    {"fmv_flag_reason": "one_sided", "fmv_low": None, "fmv_high": None},
    {"certifier": "cgc"},
])
def test_upsert_comic_rejects_a_malformed_median_pair(api, over):
    r = api.post("/api/comics", json=_body(**over))
    assert r.status_code == 422, r.text
    assert api.get("/api/comics", params={"title": "Median Book"}).json() == []
