"""Tests for gixen_overlay.db — all use in-memory SQLite, no disk side effects."""
from __future__ import annotations

import sqlite3
import pytest

from gixen_overlay.db import (
    create_tables,
    upsert_comic,
    upsert_fmv,
    set_bid_fmv,
    get_fmv_for_bid,
    link_fmv_to_bid,
    get_primary_fmv_for_bid,
    list_comics,
    sweep_orphan_yearless_comics,
    get_seen_item_ids,
    mark_items_seen,
)


def _make_db(*, with_fmv_id: bool = True) -> sqlite3.Connection:
    """Create an in-memory DB with the minimal bids stub the plugin expects."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    cols = "id INTEGER PRIMARY KEY, item_id TEXT NOT NULL, max_bid REAL NOT NULL"
    if with_fmv_id:
        cols += ", fmv_id INTEGER"
    conn.execute(f"CREATE TABLE bids ({cols})")
    conn.commit()
    return conn


@pytest.fixture
def db():
    conn = _make_db()
    create_tables(conn)
    yield conn
    conn.close()


def _insert_bid(conn, item_id="100000001", max_bid=50.0) -> int:
    cur = conn.execute(
        "INSERT INTO bids (item_id, max_bid) VALUES (?, ?)", (item_id, max_bid)
    )
    conn.commit()
    return cur.lastrowid


def _insert_comic(conn, title="X-Men", issue="1", year=1963) -> int:
    return upsert_comic(conn, title, issue, year)


# ---------------------------------------------------------------------------
# create_tables
# ---------------------------------------------------------------------------


def test_create_tables_creates_fmv_and_bid_fmvs():
    conn = _make_db()
    create_tables(conn)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "fmv" in tables
    assert "bid_fmvs" in tables
    conn.close()


def test_create_tables_does_not_create_bid_comics():
    conn = _make_db()
    create_tables(conn)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "bid_comics" not in tables
    conn.close()


def test_create_tables_is_idempotent(db):
    create_tables(db)
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "fmv" in tables
    assert "bid_fmvs" in tables


# ---------------------------------------------------------------------------
# upsert_comic (identity-only)
# ---------------------------------------------------------------------------


def test_upsert_comic_inserts_new_record(db):
    cid = upsert_comic(db, "Amazing Spider-Man", "300", 1988)
    assert isinstance(cid, int) and cid > 0
    row = db.execute("SELECT * FROM comics WHERE id=?", (cid,)).fetchone()
    assert row["title"] == "Amazing Spider-Man"
    assert row["issue"] == "300"
    assert row["year"] == 1988


def test_upsert_comic_returns_same_id_on_conflict(db):
    id1 = upsert_comic(db, "X-Men", "1", 1963)
    id2 = upsert_comic(db, "X-Men", "1", 1963)
    assert id1 == id2


# --- BUI-28: variant is part of comic identity ---

def test_upsert_comic_variant_gets_distinct_id(db):
    """Base cover and Newsstand variant of the same (title, issue, year) split."""
    base = upsert_comic(db, "Hulk", "332", 1986)
    news = upsert_comic(db, "Hulk", "332", 1986, variant="Newsstand")
    assert base != news
    rows = db.execute(
        "SELECT variant FROM comics WHERE LOWER(title)='hulk' AND issue='332' ORDER BY id"
    ).fetchall()
    assert [r["variant"] for r in rows] == [None, "Newsstand"]


def test_upsert_comic_same_variant_is_stable(db):
    a = upsert_comic(db, "Hulk", "332", 1986, variant="Newsstand")
    b = upsert_comic(db, "Hulk", "332", 1986, variant="Newsstand")
    assert a == b


def test_upsert_comic_blank_variant_is_base(db):
    """Empty/whitespace variant normalizes to NULL (the base edition)."""
    base = upsert_comic(db, "Hulk", "332", 1986)
    blank = upsert_comic(db, "Hulk", "332", 1986, variant="   ")
    assert base == blank
    assert db.execute(
        "SELECT variant FROM comics WHERE id=?", (base,)
    ).fetchone()["variant"] is None


def test_upsert_comic_variant_distinct_for_yearless(db):
    base = upsert_comic(db, "Spawn", "300")
    direct = upsert_comic(db, "Spawn", "300", variant="Direct")
    assert base != direct


def test_upsert_comic_variant_promotes_within_variant_only(db):
    """A yearless variant placeholder is promoted by a yeared insert of the same
    variant — and does not absorb a different variant."""
    yearless_news = upsert_comic(db, "Hulk", "332", variant="Newsstand")
    yeared_news = upsert_comic(db, "Hulk", "332", 1986, variant="Newsstand")
    assert yearless_news == yeared_news  # promoted in place
    base = upsert_comic(db, "Hulk", "332", 1986)  # base must be its own row
    assert base != yeared_news


def test_upsert_comic_updates_locg_id_via_coalesce(db):
    id1 = upsert_comic(db, "X-Men", "1", 1963, locg_id=12345)
    id2 = upsert_comic(db, "X-Men", "1", 1963, locg_id=None)
    assert id1 == id2
    row = db.execute("SELECT locg_id FROM comics WHERE id=?", (id1,)).fetchone()
    assert row["locg_id"] == 12345


def test_upsert_comic_no_grade_or_fmv_columns(db):
    cid = upsert_comic(db, "Hulk", "181", 1974)
    cols = {row[1] for row in db.execute("PRAGMA table_info(comics)")}
    assert "grade" not in cols
    assert "fmv_low" not in cols


def test_upsert_comic_case_insensitive_yeared(db):
    id1 = upsert_comic(db, "The Mighty Thor", "154", 1968)
    id2 = upsert_comic(db, "THE MIGHTY THOR", "154", 1968)
    assert id1 == id2
    assert db.execute("SELECT COUNT(*) FROM comics WHERE issue='154'").fetchone()[0] == 1


def test_upsert_comic_case_insensitive_yearless(db):
    id1 = upsert_comic(db, "Batman", "375")
    id2 = upsert_comic(db, "BATMAN", "375")
    assert id1 == id2
    assert db.execute("SELECT COUNT(*) FROM comics WHERE issue='375'").fetchone()[0] == 1


def test_upsert_comic_caps_insert_finds_canonical_yeared(db):
    id1 = upsert_comic(db, "Batman", "375", 1984)
    id2 = upsert_comic(db, "BATMAN", "375", 1984)
    assert id1 == id2


# ---------------------------------------------------------------------------
# upsert_comic — case-insensitive title matching (PER-123)
# ---------------------------------------------------------------------------


def test_upsert_comic_allcaps_yeared_hits_existing_yeared_row(db):
    id1 = upsert_comic(db, "The Mighty Thor", "154", 1968)
    id2 = upsert_comic(db, "THE MIGHTY THOR", "154", 1968)
    assert id1 == id2
    assert db.execute("SELECT COUNT(*) FROM comics").fetchone()[0] == 1


def test_upsert_comic_allcaps_yeared_promotes_existing_yearless(db):
    id_yearless = upsert_comic(db, "THE MIGHTY THOR", "154")
    id_yeared = upsert_comic(db, "The Mighty Thor", "154", 1968)
    assert id_yearless == id_yeared
    row = db.execute("SELECT year FROM comics WHERE id=?", (id_yeared,)).fetchone()
    assert row["year"] == 1968
    assert db.execute("SELECT COUNT(*) FROM comics").fetchone()[0] == 1


def test_upsert_comic_allcaps_yearless_defers_to_existing_yeared(db):
    id_yeared = upsert_comic(db, "The Mighty Thor", "154", 1968)
    id_yearless = upsert_comic(db, "THE MIGHTY THOR", "154")
    assert id_yearless == id_yeared
    assert db.execute("SELECT COUNT(*) FROM comics").fetchone()[0] == 1


def test_upsert_comic_allcaps_yearless_hits_existing_yearless(db):
    id1 = upsert_comic(db, "The Mighty Thor", "154")
    id2 = upsert_comic(db, "THE MIGHTY THOR", "154")
    assert id1 == id2
    assert db.execute("SELECT COUNT(*) FROM comics").fetchone()[0] == 1


def test_upsert_comic_allcaps_skips_yearless_promotion_on_yeared_sibling_conflict(db):
    # Set up: yearless row + yeared row at 1968 (bypassing upsert_comic to avoid
    # auto-promotion — simulates pre-existing split state in the DB).
    cur = db.execute(
        "INSERT INTO comics (title, issue, year) VALUES (?, ?, NULL)",
        ("The Mighty Thor", "154"),
    )
    db.commit()
    id_yearless = cur.lastrowid
    db.execute(
        "INSERT INTO comics (title, issue, year) VALUES (?, ?, ?)",
        ("The Mighty Thor", "154", 1968),
    )
    db.commit()

    # ALL-CAPS yeared insert at year=1999 — LOWER() finds 1968 as a conflicting
    # yeared sibling, so PER-104 guard fires and returns the yearless row unchanged.
    id_returned = upsert_comic(db, "THE MIGHTY THOR", "154", 1999)

    assert id_returned == id_yearless
    row = db.execute("SELECT year FROM comics WHERE id=?", (id_yearless,)).fetchone()
    assert row["year"] is None
    assert db.execute("SELECT COUNT(*) FROM comics").fetchone()[0] == 2


def test_sweep_orphan_yearless_comics_merges_allcaps_stubs(db):
    yeared_id = upsert_comic(db, "The Mighty Thor", "154", 1968)
    # Manually insert an ALL-CAPS yearless stub (bypassing upsert_comic which
    # now deduplicates — this simulates pre-PER-123 data in the DB).
    cur = db.execute(
        "INSERT INTO comics (title, issue, year) VALUES (?, ?, NULL)",
        ("THE MIGHTY THOR", "154"),
    )
    db.commit()
    stub_id = cur.lastrowid

    result = sweep_orphan_yearless_comics(db)

    assert result["dry_run"] is False
    assert result["merged"] == 1
    assert result["details"][0]["yearless_id"] == stub_id
    assert result["details"][0]["yeared_id"] == yeared_id
    assert db.execute("SELECT COUNT(*) FROM comics").fetchone()[0] == 1


def test_sweep_orphan_yearless_comics_dry_run_does_not_delete(db):
    upsert_comic(db, "The Mighty Thor", "154", 1968)
    db.execute(
        "INSERT INTO comics (title, issue, year) VALUES (?, ?, NULL)",
        ("THE MIGHTY THOR", "154"),
    )
    db.commit()

    result = sweep_orphan_yearless_comics(db, dry_run=True)

    assert result["dry_run"] is True
    assert result["would_merge"] == 1
    assert db.execute("SELECT COUNT(*) FROM comics").fetchone()[0] == 2


# ---------------------------------------------------------------------------
# upsert_fmv
# ---------------------------------------------------------------------------


def test_upsert_fmv_inserts_and_returns_id(db):
    cid = _insert_comic(db)
    fid = upsert_fmv(db, cid, 9.2, low=800.0, high=1000.0, comps=12, confidence="high")
    assert isinstance(fid, int) and fid > 0


def test_upsert_fmv_on_same_comic_grade_updates_nonnull_fields(db):
    cid = _insert_comic(db)
    fid1 = upsert_fmv(db, cid, 9.2, low=800.0)
    fid2 = upsert_fmv(db, cid, 9.2, low=850.0)
    assert fid1 == fid2
    row = db.execute("SELECT low FROM fmv WHERE id=?", (fid1,)).fetchone()
    assert row["low"] == 850.0


def test_upsert_fmv_coalesce_does_not_overwrite_with_none(db):
    cid = _insert_comic(db)
    fid = upsert_fmv(db, cid, 9.2, low=800.0, high=1000.0)
    upsert_fmv(db, cid, 9.2, low=None)
    row = db.execute("SELECT low, high FROM fmv WHERE id=?", (fid,)).fetchone()
    assert row["low"] == 800.0
    assert row["high"] == 1000.0


def test_upsert_fmv_grade_none_raises(db):
    cid = _insert_comic(db)
    with pytest.raises(ValueError, match="grade is required"):
        upsert_fmv(db, cid, None)


def test_upsert_fmv_grade_only_stub_has_null_updated_at(db):
    cid = _insert_comic(db)
    fid = upsert_fmv(db, cid, 9.2)
    row = db.execute("SELECT updated_at FROM fmv WHERE id=?", (fid,)).fetchone()
    assert row["updated_at"] is None


def test_upsert_fmv_subsequent_call_with_low_sets_updated_at(db):
    cid = _insert_comic(db)
    fid = upsert_fmv(db, cid, 9.2)
    upsert_fmv(db, cid, 9.2, low=500.0)
    row = db.execute("SELECT updated_at FROM fmv WHERE id=?", (fid,)).fetchone()
    assert row["updated_at"] is not None


# ---------------------------------------------------------------------------
# link_fmv_to_bid / set_bid_fmv / get_fmv_for_bid
# ---------------------------------------------------------------------------


def test_link_fmv_to_bid_sole_junction_is_promoted_to_primary(db):
    """BUI-82: a sole junction is always primary so the grade/FMV aggregates
    (which key off is_primary=1) don't blank, even when linked non-primary."""
    bid_id = _insert_bid(db)
    cid = _insert_comic(db)
    fid = upsert_fmv(db, cid, 9.2, low=800.0)
    link_fmv_to_bid(db, bid_id, fid, is_primary=False)
    rows = db.execute("SELECT * FROM bid_fmvs WHERE bid_id=?", (bid_id,)).fetchall()
    assert len(rows) == 1
    assert rows[0]["fmv_id"] == fid
    assert rows[0]["is_primary"] == 1
    assert db.execute(
        "SELECT fmv_id FROM bids WHERE id=?", (bid_id,)
    ).fetchone()["fmv_id"] == fid


def test_link_fmv_to_bid_nonprimary_lot_member_stays_nonprimary(db):
    """Once a primary exists, a non-primary link to a *different* comic stays
    a non-primary lot member — genuine lots are preserved."""
    bid_id = _insert_bid(db)
    cid1 = _insert_comic(db, issue="1")
    cid2 = _insert_comic(db, issue="2")
    primary = upsert_fmv(db, cid1, 9.2, low=800.0)
    member = upsert_fmv(db, cid2, 9.0, low=400.0)
    link_fmv_to_bid(db, bid_id, primary, is_primary=True)
    link_fmv_to_bid(db, bid_id, member, is_primary=False)
    rows = {r["fmv_id"]: r["is_primary"]
            for r in db.execute(
                "SELECT fmv_id, is_primary FROM bid_fmvs WHERE bid_id=?", (bid_id,))}
    assert rows[primary] == 1
    assert rows[member] == 0


def test_link_fmv_to_bid_primary_mirrors_to_bids_fmv_id(db):
    bid_id = _insert_bid(db)
    cid = _insert_comic(db)
    fid = upsert_fmv(db, cid, 9.2, low=800.0)
    link_fmv_to_bid(db, bid_id, fid, is_primary=True)
    row = db.execute("SELECT fmv_id FROM bids WHERE id=?", (bid_id,)).fetchone()
    assert row["fmv_id"] == fid


def test_link_fmv_to_bid_primary_demotes_prior_different_comic(db):
    """A primary re-link to a *different* comic demotes (but keeps) the prior
    junction — multi-comic lots are preserved."""
    bid_id = _insert_bid(db)
    cid1 = _insert_comic(db, issue="1")
    cid2 = _insert_comic(db, issue="2")
    fid1 = upsert_fmv(db, cid1, 9.0, low=700.0)
    fid2 = upsert_fmv(db, cid2, 9.2, low=800.0)
    link_fmv_to_bid(db, bid_id, fid1, is_primary=True)
    link_fmv_to_bid(db, bid_id, fid2, is_primary=True)
    rows = {r["fmv_id"]: r["is_primary"]
            for r in db.execute("SELECT fmv_id, is_primary FROM bid_fmvs WHERE bid_id=?", (bid_id,))}
    assert rows[fid1] == 0
    assert rows[fid2] == 1


def test_link_fmv_to_bid_primary_replaces_same_comic_grade_only_stub(db):
    """BUI-82: re-linking the *same comic* to a valued FMV must replace the
    prior grade-only junction, not leave a demoted null-valued duplicate.

    The duplicate inflates the dashboard's lot_count to 2 and trips the
    "unpriced lot member" guard, blanking the FMV of a single priced comic.
    """
    bid_id = _insert_bid(db)
    cid = _insert_comic(db)
    stub = upsert_fmv(db, cid, 8.0)  # grade-only stub, low IS NULL
    link_fmv_to_bid(db, bid_id, stub, is_primary=True)
    valued = upsert_fmv(db, cid, 9.2, low=800.0, high=1000.0)  # same comic, real FMV
    link_fmv_to_bid(db, bid_id, valued, is_primary=True)
    rows = db.execute(
        "SELECT fmv_id, is_primary FROM bid_fmvs WHERE bid_id=?", (bid_id,)
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["fmv_id"] == valued
    assert rows[0]["is_primary"] == 1
    assert db.execute(
        "SELECT fmv_id FROM bids WHERE id=?", (bid_id,)
    ).fetchone()["fmv_id"] == valued


def test_link_fmv_to_bid_idempotent(db):
    bid_id = _insert_bid(db)
    cid = _insert_comic(db)
    fid = upsert_fmv(db, cid, 9.2, low=800.0)
    link_fmv_to_bid(db, bid_id, fid)
    link_fmv_to_bid(db, bid_id, fid)
    count = db.execute(
        "SELECT COUNT(*) FROM bid_fmvs WHERE bid_id=?", (bid_id,)
    ).fetchone()[0]
    assert count == 1


def test_link_fmv_to_bid_nonexistent_fmv_raises_fk(db):
    bid_id = _insert_bid(db)
    with pytest.raises(sqlite3.IntegrityError):
        link_fmv_to_bid(db, bid_id, 9999, is_primary=False)


def test_set_bid_fmv_sets_and_clears(db):
    bid_id = _insert_bid(db)
    cid = _insert_comic(db)
    fid = upsert_fmv(db, cid, 9.2, low=800.0)
    set_bid_fmv(db, bid_id, fid)
    assert db.execute("SELECT fmv_id FROM bids WHERE id=?", (bid_id,)).fetchone()["fmv_id"] == fid
    set_bid_fmv(db, bid_id, None)
    assert db.execute("SELECT fmv_id FROM bids WHERE id=?", (bid_id,)).fetchone()["fmv_id"] is None


def test_get_fmv_for_bid_returns_fmv_row(db):
    bid_id = _insert_bid(db)
    cid = _insert_comic(db)
    fid = upsert_fmv(db, cid, 9.2, low=800.0)
    set_bid_fmv(db, bid_id, fid)
    row = get_fmv_for_bid(db, bid_id)
    assert row is not None
    assert row["id"] == fid
    assert row["low"] == 800.0


def test_get_fmv_for_bid_returns_none_when_unlinked(db):
    bid_id = _insert_bid(db)
    assert get_fmv_for_bid(db, bid_id) is None


def test_get_primary_fmv_for_bid_integration(db):
    bid_id = _insert_bid(db)
    cid = upsert_comic(db, "Daredevil", "1", 1964, locg_id=12345)
    fid = upsert_fmv(db, cid, 9.4, low=600.0)
    link_fmv_to_bid(db, bid_id, fid, is_primary=True)
    row = get_primary_fmv_for_bid(db, bid_id)
    assert row is not None
    assert row["grade"] == 9.4
    assert row["low"] == 600.0
    assert row["title"] == "Daredevil"
    assert row["locg_id"] == 12345


# ---------------------------------------------------------------------------
# list_comics (joined read path)
# ---------------------------------------------------------------------------


def test_list_comics_returns_all(db):
    upsert_comic(db, "X-Men", "1", 1963)
    upsert_comic(db, "Hulk", "181", 1974)
    rows = list_comics(db)
    assert len(rows) == 2


def test_list_comics_one_row_per_fmv_grade(db):
    cid = upsert_comic(db, "X-Men", "1", 1963)
    upsert_fmv(db, cid, 9.0, low=700.0)
    upsert_fmv(db, cid, 9.2, low=800.0)
    rows = list_comics(db)
    assert len(rows) == 2
    grades = {r["grade"] for r in rows}
    assert grades == {9.0, 9.2}


def test_list_comics_comic_without_fmv_returns_one_null_row(db):
    upsert_comic(db, "X-Men", "1", 1963)
    rows = list_comics(db)
    assert len(rows) == 1
    assert rows[0]["grade"] is None
    assert rows[0]["fmv_low"] is None


def test_list_comics_filter_by_grade(db):
    cid = upsert_comic(db, "X-Men", "1", 1963)
    upsert_fmv(db, cid, 9.0, low=700.0)
    upsert_fmv(db, cid, 9.2, low=800.0)
    rows = list_comics(db, grade=9.2)
    assert len(rows) == 1
    assert rows[0]["fmv_low"] == 800.0


def test_list_comics_filter_by_title(db):
    upsert_comic(db, "X-Men", "1", 1963)
    upsert_comic(db, "Hulk", "181", 1974)
    rows = list_comics(db, title="X-Men")
    assert len(rows) == 1
    assert rows[0]["title"] == "X-Men"


def test_list_comics_empty_db(db):
    assert list_comics(db) == []


# ---------------------------------------------------------------------------
# list_comics — locg_id and max_age_days filters (FMV cache lookup path)
# ---------------------------------------------------------------------------


def test_list_comics_filters_by_locg_id(db):
    """A locg_id lookup returns rows for that canonical issue, regardless of
    title spelling. This is the lookup comic-fmv uses for cache reuse."""
    cid_asm = upsert_comic(db, "Amazing Spider-Man", "300", 1988, locg_id=6977652)
    upsert_fmv(db, cid_asm, 9.2, low=800.0, high=1000.0)
    cid_hulk = upsert_comic(db, "Hulk", "181", 1974, locg_id=12345)
    upsert_fmv(db, cid_hulk, 9.0, low=50.0, high=70.0)

    rows = list_comics(db, locg_id=6977652)
    assert len(rows) == 1
    assert rows[0]["title"] == "Amazing Spider-Man"


def test_list_comics_locg_id_plus_grade(db):
    """The fmv-cache lookup pattern: locg_id + grade pinpoints one row."""
    cid = upsert_comic(db, "Hulk", "181", 1974, locg_id=12345)
    upsert_fmv(db, cid, 9.0, low=50.0, high=70.0)
    upsert_fmv(db, cid, 9.2, low=100.0, high=130.0)

    rows = list_comics(db, locg_id=12345, grade=9.0)
    assert len(rows) == 1
    assert rows[0]["grade"] == 9.0


def test_list_comics_max_age_excludes_stale(db):
    """A row whose fmv updated_at is older than the cutoff is excluded."""
    from datetime import datetime, timedelta, timezone

    cid = upsert_comic(db, "Hulk", "181", 1974)
    upsert_fmv(db, cid, 9.0, low=50.0, high=70.0)
    # Backdate updated_at to 30 days ago
    old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    db.execute("UPDATE fmv SET updated_at = ?", (old,))
    db.commit()

    assert list_comics(db, max_age_days=7) == []   # 30d > 7d cutoff
    assert len(list_comics(db, max_age_days=60)) == 1  # 30d < 60d cutoff


def test_list_comics_max_age_keeps_fresh(db):
    """A row whose updated_at is within the cutoff is included."""
    cid = upsert_comic(db, "Hulk", "181", 1974)
    upsert_fmv(db, cid, 9.0, low=50.0, high=70.0)

    rows = list_comics(db, max_age_days=7)
    assert len(rows) == 1


def test_list_comics_max_age_excludes_null_updated_at(db):
    """A comic with no FMV value (updated_at IS NULL) doesn't satisfy the
    freshness predicate. Without this guard, callers would treat grade-only
    stub rows as cache hits and skip the real compute."""
    cid = upsert_comic(db, "Hulk", "181", 1974)
    upsert_fmv(db, cid, 9.0)  # grade-only stub, no FMV values → updated_at stays NULL

    # Confirm the stub really did leave updated_at NULL
    row = db.execute("SELECT updated_at FROM fmv WHERE comic_id=?",
                     (cid,)).fetchone()
    assert row["updated_at"] is None

    assert list_comics(db, max_age_days=365) == []


def test_list_comics_combines_locg_grade_and_freshness(db):
    """The end-to-end FMV-cache lookup: locg_id + grade + max_age_days."""
    from datetime import datetime, timedelta, timezone

    cid = upsert_comic(db, "ASM", "300", 1988, locg_id=6977652)
    upsert_fmv(db, cid, 9.2, low=800.0, high=1000.0)

    rows = list_comics(db, locg_id=6977652, grade=9.2, max_age_days=7)
    assert len(rows) == 1

    # Same lookup with a tighter freshness window after backdating
    old = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    db.execute("UPDATE fmv SET updated_at = ?", (old,))
    db.commit()

    assert list_comics(db, locg_id=6977652, grade=9.2, max_age_days=7) == []


# ---------------------------------------------------------------------------
# Migration tests
# ---------------------------------------------------------------------------


def _make_legacy_db() -> sqlite3.Connection:
    """Build a pre-migration DB with the old comics schema."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("""
        CREATE TABLE bids (
            id INTEGER PRIMARY KEY,
            item_id TEXT NOT NULL,
            comic_id INTEGER,
            fmv_id INTEGER,
            max_bid REAL NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE comics (
            id              INTEGER PRIMARY KEY,
            title           TEXT NOT NULL,
            issue           TEXT NOT NULL,
            year            INTEGER NOT NULL,
            grade           REAL,
            fmv_low         REAL,
            fmv_high        REAL,
            fmv_comps       INTEGER,
            fmv_confidence  TEXT,
            fmv_notes       TEXT,
            fmv_updated_at  TEXT,
            locg_id         INTEGER,
            locg_variant_id INTEGER,
            created_at      TEXT DEFAULT (datetime('now')),
            UNIQUE(title, issue, year, grade)
        )
    """)
    conn.execute("""
        CREATE TABLE bid_comics (
            bid_id     INTEGER NOT NULL REFERENCES bids(id),
            comic_id   INTEGER NOT NULL REFERENCES comics(id),
            is_primary INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (bid_id, comic_id)
        )
    """)
    conn.commit()
    return conn


def test_migration_collapses_shadow_rows_to_one_comic():
    conn = _make_legacy_db()
    # Two comics rows: same title/issue/year, different grade (the classic shadow bug)
    conn.execute("INSERT INTO comics (id, title, issue, year, grade, fmv_low) VALUES (1, 'ASM', '300', 1988, 9.2, 800.0)")
    conn.execute("INSERT INTO comics (id, title, issue, year, grade, fmv_low) VALUES (2, 'ASM', '300', 1988, 9.0, 600.0)")
    conn.commit()
    create_tables(conn)
    comics = conn.execute("SELECT * FROM comics").fetchall()
    assert len(comics) == 1
    fmv_rows = conn.execute("SELECT * FROM fmv ORDER BY grade").fetchall()
    assert len(fmv_rows) == 2
    grades = {r["grade"] for r in fmv_rows}
    assert grades == {9.0, 9.2}


def test_migration_sets_bids_fmv_id():
    conn = _make_legacy_db()
    conn.execute("INSERT INTO bids (id, item_id, comic_id, max_bid) VALUES (1, 'item1', 1, 50.0)")
    conn.execute("INSERT INTO comics (id, title, issue, year, grade, fmv_low) VALUES (1, 'ASM', '300', 1988, 9.2, 800.0)")
    conn.commit()
    create_tables(conn)
    bid = conn.execute("SELECT fmv_id FROM bids WHERE id=1").fetchone()
    assert bid["fmv_id"] is not None
    fmv = conn.execute("SELECT * FROM fmv WHERE id=?", (bid["fmv_id"],)).fetchone()
    assert fmv["grade"] == 9.2


def test_migration_migrates_bid_comics_to_bid_fmvs():
    conn = _make_legacy_db()
    conn.execute("INSERT INTO bids (id, item_id, comic_id, max_bid) VALUES (1, 'item1', 1, 50.0)")
    conn.execute("INSERT INTO comics (id, title, issue, year, grade, fmv_low) VALUES (1, 'ASM', '300', 1988, 9.2, 800.0)")
    conn.execute("INSERT INTO bid_comics (bid_id, comic_id, is_primary) VALUES (1, 1, 1)")
    conn.commit()
    create_tables(conn)
    bf = conn.execute("SELECT * FROM bid_fmvs WHERE bid_id=1").fetchall()
    assert len(bf) == 1
    assert bf[0]["is_primary"] == 1
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "bid_comics" not in tables


def test_migration_fmv_bid_fmvs_survive_python_memory_roundtrip():
    """Verifies the Python-memory table rebuild preserves all field values."""
    conn = _make_legacy_db()
    conn.execute("INSERT INTO bids (id, item_id, comic_id, max_bid) VALUES (1, 'item1', 1, 50.0)")
    conn.execute("""
        INSERT INTO comics (id, title, issue, year, grade, fmv_low, fmv_high, fmv_comps, fmv_confidence, fmv_notes)
        VALUES (1, 'ASM', '300', 1988, 9.2, 800.0, 1000.0, 12, 'high', 'Key issue')
    """)
    conn.execute("INSERT INTO bid_comics (bid_id, comic_id, is_primary) VALUES (1, 1, 1)")
    conn.commit()
    create_tables(conn)
    fmv = conn.execute("SELECT * FROM fmv").fetchone()
    assert fmv["low"] == 800.0
    assert fmv["high"] == 1000.0
    assert fmv["comps"] == 12
    assert fmv["confidence"] == "high"
    assert fmv["notes"] == "Key issue"


def test_migration_bid_with_null_comic_id_stays_unlinked():
    conn = _make_legacy_db()
    conn.execute("INSERT INTO bids (id, item_id, comic_id, max_bid) VALUES (1, 'item1', NULL, 50.0)")
    conn.commit()
    create_tables(conn)
    bid = conn.execute("SELECT fmv_id FROM bids WHERE id=1").fetchone()
    assert bid["fmv_id"] is None


def test_migration_is_idempotent():
    conn = _make_legacy_db()
    conn.execute("INSERT INTO comics (id, title, issue, year, grade, fmv_low) VALUES (1, 'ASM', '300', 1988, 9.2, 800.0)")
    conn.commit()
    create_tables(conn)
    create_tables(conn)
    comics = conn.execute("SELECT * FROM comics").fetchall()
    assert len(comics) == 1
    fmv_rows = conn.execute("SELECT * FROM fmv").fetchall()
    assert len(fmv_rows) == 1


def test_migration_crash_recovery_raises_runtime_error():
    """If comics has no grade column but comics_old exists, raise RuntimeError."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE bids (id INTEGER PRIMARY KEY, item_id TEXT, fmv_id INTEGER, max_bid REAL)")
    conn.execute("""
        CREATE TABLE comics (
            id INTEGER PRIMARY KEY,
            title TEXT NOT NULL,
            issue TEXT NOT NULL,
            year INTEGER NOT NULL,
            locg_id INTEGER,
            locg_variant_id INTEGER,
            created_at TEXT,
            UNIQUE(title, issue, year)
        )
    """)
    # Simulate mid-rebuild crash: comics_old still present
    conn.execute("""
        CREATE TABLE comics_old (
            id INTEGER PRIMARY KEY,
            title TEXT NOT NULL,
            issue TEXT NOT NULL,
            year INTEGER NOT NULL,
            grade REAL
        )
    """)
    conn.commit()
    with pytest.raises(RuntimeError, match="crashed mid-migration state"):
        create_tables(conn)


def test_migration_fmv_split_crash_after_drop_old_raises_runtime_error():
    """A crash after DROP TABLE comics_old in fmv_split leaves the schema looking
    already-migrated. The marker guard must raise RuntimeError on next startup."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE bids (id INTEGER PRIMARY KEY, item_id TEXT, fmv_id INTEGER, max_bid REAL)")
    # Post-drop schema: no grade col, no comics_old — gate would return early without marker.
    conn.execute("""
        CREATE TABLE comics (
            id INTEGER PRIMARY KEY, title TEXT NOT NULL, issue TEXT NOT NULL,
            year INTEGER NOT NULL, locg_id INTEGER, locg_variant_id INTEGER,
            created_at TEXT, UNIQUE(title, issue, year)
        )
    """)
    conn.execute("""
        CREATE TABLE fmv (
            id INTEGER PRIMARY KEY,
            comic_id INTEGER NOT NULL REFERENCES comics(id) ON DELETE CASCADE,
            grade REAL NOT NULL, low REAL, high REAL, comps INTEGER,
            confidence TEXT, notes TEXT, updated_at TEXT, UNIQUE(comic_id, grade)
        )
    """)
    conn.execute("""
        CREATE TABLE bid_fmvs (
            bid_id INTEGER NOT NULL REFERENCES bids(id) ON DELETE CASCADE,
            fmv_id INTEGER NOT NULL REFERENCES fmv(id) ON DELETE CASCADE,
            is_primary INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (bid_id, fmv_id)
        )
    """)
    conn.execute("CREATE TABLE migration_state (migration TEXT PRIMARY KEY)")
    conn.execute("INSERT INTO migration_state (migration) VALUES ('fmv_split')")
    conn.commit()
    with pytest.raises(RuntimeError, match="fmv_split"):
        create_tables(conn)


def test_migration_year_nullable_crash_after_drop_old_raises_runtime_error():
    """A crash after DROP TABLE comics_old in year_nullable leaves year already
    nullable. The marker guard must raise RuntimeError on next startup."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE bids (id INTEGER PRIMARY KEY, item_id TEXT, fmv_id INTEGER, max_bid REAL)")
    # Post-drop schema: year is nullable — gate would return early without marker.
    conn.execute("""
        CREATE TABLE comics (
            id INTEGER PRIMARY KEY, title TEXT NOT NULL, issue TEXT NOT NULL,
            year INTEGER, locg_id INTEGER, locg_variant_id INTEGER, created_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE fmv (
            id INTEGER PRIMARY KEY,
            comic_id INTEGER NOT NULL REFERENCES comics(id) ON DELETE CASCADE,
            grade REAL NOT NULL, low REAL, UNIQUE(comic_id, grade)
        )
    """)
    conn.execute("""
        CREATE TABLE bid_fmvs (
            bid_id INTEGER NOT NULL REFERENCES bids(id) ON DELETE CASCADE,
            fmv_id INTEGER NOT NULL REFERENCES fmv(id) ON DELETE CASCADE,
            is_primary INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (bid_id, fmv_id)
        )
    """)
    conn.execute("CREATE TABLE migration_state (migration TEXT PRIMARY KEY)")
    conn.execute("INSERT INTO migration_state (migration) VALUES ('year_nullable')")
    conn.commit()
    with pytest.raises(RuntimeError, match="year_nullable"):
        create_tables(conn)


def test_migration_fresh_db_no_legacy_data_is_noop():
    """Fresh DB (no comics rows, no bid_comics) migration gate returns immediately."""
    conn = _make_db()
    create_tables(conn)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "comics_old" not in tables
    assert "bid_comics" not in tables
    assert "fmv" in tables
    assert "bid_fmvs" in tables


def test_migrate_sweep_allcaps_orphans_merges_on_startup():
    """create_tables() sweeps ALL-CAPS yearless stubs into their yeared siblings."""
    conn = _make_db()
    create_tables(conn)
    # Manually insert a yeared canonical row and an ALL-CAPS yearless stub
    # (bypasses upsert_comic which now deduplicates — simulates pre-PER-123 data).
    yeared_id = upsert_comic(conn, "The Mighty Thor", "154", 1968)
    cur = conn.execute(
        "INSERT INTO comics (title, issue, year) VALUES (?, ?, NULL)",
        ("THE MIGHTY THOR", "154"),
    )
    conn.commit()
    stub_id = cur.lastrowid
    # Clear the migration marker so the sweep runs again on next create_tables call.
    conn.execute("DELETE FROM migration_state WHERE migration='sweep_allcaps_orphans'")
    conn.commit()

    create_tables(conn)

    assert conn.execute("SELECT COUNT(*) FROM comics").fetchone()[0] == 1
    row = conn.execute("SELECT id FROM comics WHERE year=1968").fetchone()
    assert row["id"] == yeared_id
    assert conn.execute(
        "SELECT COUNT(*) FROM migration_state WHERE migration='sweep_allcaps_orphans'"
    ).fetchone()[0] == 1


def test_migrate_sweep_allcaps_orphans_is_idempotent():
    """create_tables() called twice does not double-count or re-run the sweep."""
    conn = _make_db()
    create_tables(conn)
    upsert_comic(conn, "X-Men", "1", 1963)
    cur = conn.execute(
        "INSERT INTO comics (title, issue, year) VALUES (?, ?, NULL)",
        ("X-MEN", "1"),
    )
    conn.commit()
    conn.execute("DELETE FROM migration_state WHERE migration='sweep_allcaps_orphans'")
    conn.commit()

    create_tables(conn)
    assert conn.execute("SELECT COUNT(*) FROM comics").fetchone()[0] == 1

    # Second call — stub is gone; no new merges.
    create_tables(conn)
    assert conn.execute("SELECT COUNT(*) FROM comics").fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM migration_state WHERE migration='sweep_allcaps_orphans'"
    ).fetchone()[0] == 1


def test_migrate_lowercase_title_indexes_creates_lower_expression_indexes(db):
    idx = db.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND name='idx_comics_tiyv'"
    ).fetchone()
    assert idx is not None
    assert "lower(" in idx["sql"].lower()
    # BUI-28: variant is part of the unique key.
    assert "variant" in idx["sql"].lower()


def test_migrate_lowercase_title_indexes_blocks_case_variant_yeared_duplicate(db):
    db.execute(
        "INSERT INTO comics (title, issue, year) VALUES (?, ?, ?)",
        ("The Mighty Thor", "154", 1968),
    )
    db.commit()
    with pytest.raises(Exception):
        db.execute(
            "INSERT INTO comics (title, issue, year) VALUES (?, ?, ?)",
            ("THE MIGHTY THOR", "154", 1968),
        )
        db.commit()


def test_migrate_lowercase_title_indexes_is_idempotent():
    conn = _make_db()
    create_tables(conn)
    create_tables(conn)
    idx = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND name='idx_comics_tiyv'"
    ).fetchone()
    assert idx is not None
    assert "lower(" in idx["sql"].lower()
    assert conn.execute(
        "SELECT COUNT(*) FROM migration_state WHERE migration='lowercase_title_indexes'"
    ).fetchone()[0] == 1


def test_migration_regression_second_grade_revision_upserts_fmv_not_new_comic():
    """After migration, a grade revision updates the existing fmv row, not inserts a new comic."""
    conn = _make_legacy_db()
    conn.execute("INSERT INTO comics (id, title, issue, year, grade, fmv_low) VALUES (1, 'ASM', '300', 1988, 9.2, 800.0)")
    conn.commit()
    create_tables(conn)
    # Now use new API: upsert same identity, different grade — should create another fmv row
    cid = upsert_comic(conn, "ASM", "300", 1988)
    upsert_fmv(conn, cid, 9.4, low=900.0)
    comics = conn.execute("SELECT * FROM comics").fetchall()
    assert len(comics) == 1, "Must not create a second comics row"
    fmv_rows = conn.execute("SELECT * FROM fmv WHERE comic_id=?", (cid,)).fetchall()
    assert len(fmv_rows) == 2


# ---------------------------------------------------------------------------
# Integration: sqlite_master verification
# ---------------------------------------------------------------------------


def test_register_db_tables_creates_tables_via_sqlite_master():
    conn = _make_db()
    create_tables(conn)
    tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "comics" in tables
    assert "fmv" in tables
    assert "bid_fmvs" in tables
    assert "seller_scan_seen" in tables
    assert "bid_comics" not in tables
    conn.close()


# ---------------------------------------------------------------------------
# BUI-113: seller-scan seen-tracking helpers
# ---------------------------------------------------------------------------


def test_seller_scan_seen_table_is_idempotent():
    # create_tables runs on every server start; calling it twice must not error.
    conn = _make_db()
    create_tables(conn)
    create_tables(conn)
    assert get_seen_item_ids(conn) == set()
    conn.close()


def test_mark_and_get_seen_item_ids(db):
    assert mark_items_seen(db, ["111", "222"], "tuners36") == 2
    assert get_seen_item_ids(db) == {"111", "222"}


def test_mark_items_seen_is_idempotent(db):
    mark_items_seen(db, ["111"], "tuners36")
    # Re-marking inserts nothing new and preserves the original row.
    assert mark_items_seen(db, ["111", "333"], "tuners36") == 1
    assert get_seen_item_ids(db) == {"111", "333"}


def test_get_seen_item_ids_filters_by_seller(db):
    mark_items_seen(db, ["111"], "tuners36")
    mark_items_seen(db, ["222"], "beatlebluecat")
    assert get_seen_item_ids(db) == {"111", "222"}
    assert get_seen_item_ids(db, "tuners36") == {"111"}


def test_mark_items_seen_preserves_first_seen_at(db):
    mark_items_seen(db, ["111"], "tuners36")
    first = db.execute(
        "SELECT first_seen_at FROM seller_scan_seen WHERE item_id='111'"
    ).fetchone()["first_seen_at"]
    mark_items_seen(db, ["111"], "someoneelse")
    row = db.execute(
        "SELECT first_seen_at, seller FROM seller_scan_seen WHERE item_id='111'"
    ).fetchone()
    # INSERT OR IGNORE keeps the original timestamp and seller.
    assert row["first_seen_at"] == first
    assert row["seller"] == "tuners36"
