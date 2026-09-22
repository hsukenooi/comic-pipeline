"""Tests for the BUI-947 comps exclusion stamp.

BUI-946 made a graded run's guards drop a stored ledger row from THAT run's
pool, in memory. It covers only a listing the live fetch still sees: both
sold-comps providers serve a ~90-day window, so a stored `pool='slab'` row
older than that is never re-fetched, nothing reports it, and it keeps entering
the pool at 0.5 weight for up to 365 days. BUI-947 makes the drop durable by
STAMPING the row (`excluded_code`/`excluded_at`) instead of deleting it.

Three things are pinned here, in order:

  1. The MIGRATION, rehearsed on a `sqlite3 .backup` copy of a pre-migration
     DB — the ticket's own done-when, and the only rehearsal that resembles
     what runs on the Mac Mini.
  2. `get_comps` skipping stamped rows BY DEFAULT. That default is what makes
     a stamp reach every reader, including the pricing path's one sanctioned
     ledger read, without any of them opting in.
  3. `POST /api/comics/comps/exclude` — pool scoping, idempotency, and the
     404/422 boundary.
"""
from __future__ import annotations

import os
import sqlite3

import pytest

from gixen_overlay.db import (
    COMPS_EXCLUSION_CODES,
    create_tables,
    get_comps,
    stamp_comps_excluded,
    unstamp_comps_excluded,
    upsert_comic,
    upsert_comps,
)


# ---------------------------------------------------------------------------
# DB-layer fixtures
# ---------------------------------------------------------------------------


def _make_db(path: str = ":memory:") -> sqlite3.Connection:
    """Mirrors test_comps_ledger.py's `_make_db`: `comps` has no FK to `bids`,
    but `bid_fmvs` (same `create_tables` call) does, so the stub keeps this
    consistent with the rest of the suite."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS bids (id INTEGER PRIMARY KEY, "
        "item_id TEXT NOT NULL, max_bid REAL NOT NULL, fmv_id INTEGER)"
    )
    conn.commit()
    return conn


@pytest.fixture
def db():
    conn = _make_db()
    create_tables(conn)
    yield conn
    conn.close()


def _comp(**overrides) -> dict:
    base = {
        "pool": "slab",
        "provider": "sold-comps.com",
        "product_id": "366561460527",
        "title": "Invincible #1 (Limited Edition) Larry's Wonderful World "
                 "Image Comic 2003 CGC 9.8",
        "price": 629.0,
        "sold_date": "2026-08-02",
        "grade": 9.8,
        "buying_format": "auction",
        "link": "https://ebay.com/itm/366561460527",
        "query": "Invincible 1",
        "tier": "1",
        "from_cache": False,
        "observed_at": "2026-08-02T00:00:00Z",
        "provenance": "live",
        "certifier": "cgc",
        "label": "universal",
        "page_quality": "unknown",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# 1. The migration, rehearsed on a backup copy
# ---------------------------------------------------------------------------


def _pre_migration_db(path: str) -> None:
    """Build a DB file at `path` in exactly today's schema MINUS the two
    BUI-947 columns, with real comps rows in it.

    Built by DROPping the columns off a fully migrated DB rather than by
    hand-writing a frozen `CREATE TABLE comps` literal. That is deliberate,
    and it is the opposite choice from `test_fmv_certifier_migration.py`'s
    `_legacy_db()`: there the point was a table REBUILD, so the pre-state had
    to be pinned exactly. Here the migration is two additive ALTERs, and what
    it has to survive is "the Mac Mini's current schema, without these two
    columns" — a hand-written literal would freeze at today's column set and
    quietly stop resembling the deployed DB after the next additive column.

    WAL is set explicitly: the deployed DB is WAL-mode, and that is what makes
    the `.backup` rehearsal below meaningful rather than decorative.
    """
    conn = _make_db(path)
    conn.execute("PRAGMA journal_mode=WAL")
    create_tables(conn)
    comic_id = upsert_comic(conn, "Invincible", "1", 2003)
    upsert_comps(conn, comic_id, [
        _comp(product_id="15719"),
        _comp(product_id="15712", price=786.0),
        _comp(product_id="raw-1", pool="raw", certifier="none"),
    ])
    conn.execute("ALTER TABLE comps DROP COLUMN excluded_code")
    conn.execute("ALTER TABLE comps DROP COLUMN excluded_at")
    conn.commit()
    conn.close()


def _comps_columns(conn) -> set[str]:
    return {row[1] for row in conn.execute("PRAGMA table_info(comps)")}


def _comps_snapshot(conn) -> list[tuple]:
    return conn.execute(
        "SELECT id, comic_id, pool, provider, product_id, title, price, "
        "sold_date, grade, certifier, label, seen_count, conflict_count "
        "FROM comps ORDER BY id"
    ).fetchall()


def test_migration_rehearsal_on_a_backup_copy(tmp_path):
    """The ticket's done-when: rehearse on a backup copy before the real DB.

    `sqlite3 .backup` (here `conn.backup`, the same API) — never `cp`, which
    copies a stale snapshot of a WAL-mode DB. The copy RETAINS
    `journal_mode=wal`, so it is opened read-WRITE: a `mode=ro` URI would fail
    to take the WAL locks and the rehearsal would report a problem the real
    migration will never have.
    """
    live = tmp_path / "live.db"
    _pre_migration_db(str(live))

    backup = tmp_path / "backup.db"
    src = sqlite3.connect(str(live))
    dst = sqlite3.connect(str(backup))
    with dst:
        src.backup(dst)
    src.close()
    dst.close()

    conn = _make_db(str(backup))
    assert "excluded_code" not in _comps_columns(conn)
    before = _comps_snapshot(conn)
    assert len(before) == 3

    create_tables(conn)

    cols = _comps_columns(conn)
    assert "excluded_code" in cols and "excluded_at" in cols
    # Every pre-existing row is unchanged and un-stamped: NULL already means
    # "never stamped", so this migration has no backfill and must have
    # rewritten nothing.
    assert _comps_snapshot(conn) == before
    assert conn.execute(
        "SELECT COUNT(*) FROM comps WHERE excluded_code IS NOT NULL"
    ).fetchone()[0] == 0
    conn.close()


def test_migration_is_idempotent_run_twice_on_the_backup(tmp_path):
    """A DB that already HAS the columns must not error — SQLite refuses a
    duplicate ADD COLUMN, so the PRAGMA guard is the whole migration."""
    live = tmp_path / "live.db"
    _pre_migration_db(str(live))
    conn = _make_db(str(live))
    create_tables(conn)
    snapshot = _comps_snapshot(conn)
    stamp_comps_excluded(conn, 1, ["15719"], "store_variant")

    create_tables(conn)  # second run: no-op
    create_tables(conn)  # and a third, for good measure

    assert _comps_snapshot(conn) == snapshot
    # The second and third runs must not have cleared the stamp either.
    assert conn.execute(
        "SELECT excluded_code FROM comps WHERE product_id='15719'"
    ).fetchone()[0] == "store_variant"
    conn.close()


def test_migration_converges_a_db_that_has_only_one_of_the_two_columns(tmp_path):
    """Each column is checked independently, not as a pair — a DB left
    half-migrated by a crash still converges rather than erroring forever."""
    live = tmp_path / "live.db"
    _pre_migration_db(str(live))
    conn = _make_db(str(live))
    conn.execute("ALTER TABLE comps ADD COLUMN excluded_code TEXT")
    conn.commit()

    create_tables(conn)

    assert {"excluded_code", "excluded_at"} <= _comps_columns(conn)
    conn.close()


def test_a_fresh_install_has_both_columns_and_no_stamped_rows(db):
    assert {"excluded_code", "excluded_at"} <= _comps_columns(db)
    comic_id = upsert_comic(db, "Invincible", "1", 2003)
    upsert_comps(db, comic_id, [_comp()])
    row = db.execute("SELECT excluded_code, excluded_at FROM comps").fetchone()
    assert row["excluded_code"] is None
    assert row["excluded_at"] is None


# ---------------------------------------------------------------------------
# 2. get_comps skips stamped rows by default
# ---------------------------------------------------------------------------


def test_get_comps_skips_a_stamped_row_by_default(db):
    comic_id = upsert_comic(db, "Invincible", "1", 2003)
    upsert_comps(db, comic_id, [_comp(product_id="15719"),
                                _comp(product_id="15712")])
    stamp_comps_excluded(db, comic_id, ["15719"], "store_variant")

    rows = get_comps(db, comic_id=comic_id)
    assert [r["product_id"] for r in rows] == ["15712"]


def test_get_comps_include_excluded_shows_the_stamp(db):
    """The audit read. The rows are kept precisely so this question is
    answerable — the stamp names which guard judged the row and when."""
    comic_id = upsert_comic(db, "Invincible", "1", 2003)
    upsert_comps(db, comic_id, [_comp(product_id="15719")])
    stamp_comps_excluded(db, comic_id, ["15719"], "store_variant")

    rows = get_comps(db, comic_id=comic_id, include_excluded=True)
    assert len(rows) == 1
    assert rows[0]["excluded_code"] == "store_variant"
    assert rows[0]["excluded_at"] is not None


def test_get_comps_stamp_filter_composes_with_the_other_filters(db):
    """The stamp narrows; it never replaces or reorders. A stamped slab row
    is gone from a `pool='slab'` read, and an un-stamped one still arrives."""
    comic_id = upsert_comic(db, "Invincible", "1", 2003)
    upsert_comps(db, comic_id, [
        _comp(product_id="15719"),
        _comp(product_id="15712"),
        _comp(product_id="raw-1", pool="raw", certifier="none"),
    ])
    stamp_comps_excluded(db, comic_id, ["15719"], "store_variant")

    slab = get_comps(db, comic_id=comic_id, pool="slab")
    assert [r["product_id"] for r in slab] == ["15712"]
    assert len(get_comps(db, comic_id=comic_id, pool="raw")) == 1


def test_re_observing_a_stamped_comp_does_not_clear_the_stamp(db):
    """`upsert_comps`' conflict branch touches only the bookkeeping columns.
    If it ever widened to rewrite the row, a re-fetch of the same listing
    would silently un-stamp it and put it straight back in the pool."""
    comic_id = upsert_comic(db, "Invincible", "1", 2003)
    upsert_comps(db, comic_id, [_comp(product_id="15719")])
    stamp_comps_excluded(db, comic_id, ["15719"], "store_variant")

    upsert_comps(db, comic_id, [_comp(product_id="15719")])

    row = db.execute(
        "SELECT excluded_code, seen_count FROM comps WHERE product_id='15719'"
    ).fetchone()
    assert row["excluded_code"] == "store_variant"
    assert row["seen_count"] == 2
    assert get_comps(db, comic_id=comic_id) == []


def test_the_pricing_paths_own_ledger_read_never_sees_a_stamped_row(db):
    """`_merge_slab_pool`'s ledger input, from the server's side.

    `apps/fmv`'s `_fetch_ledger_comps` — the one sanctioned read of this
    ledger from the pricing path, and the source of BOTH the live graded
    merge's `ledger_slab` and `_graded_ledger_advisory`'s pool — reads by
    `(title, issue, year)` + `pool` + `limit` and passes nothing else. This
    replays exactly that parameter set: the stamp has to land through the
    DEFAULT, because that caller never opts in.
    """
    comic_id = upsert_comic(db, "Invincible", "1", 2003)
    upsert_comps(db, comic_id, [_comp(product_id="15719"),
                                _comp(product_id="15712")])
    stamp_comps_excluded(db, comic_id, ["15719"], "store_variant")

    rows = get_comps(db, title="Invincible", issue="1", year=2003,
                     pool="slab", limit=200)
    assert [r["product_id"] for r in rows] == ["15712"]


def test_get_comps_ordering_is_unchanged_by_the_new_clause(db):
    comic_id = upsert_comic(db, "Invincible", "1", 2003)
    upsert_comps(db, comic_id, [
        _comp(product_id="old", observed_at="2026-01-01T00:00:00Z"),
        _comp(product_id="new", observed_at="2026-09-01T00:00:00Z"),
    ])
    rows = get_comps(db, comic_id=comic_id)
    assert [r["product_id"] for r in rows] == ["new", "old"]


# ---------------------------------------------------------------------------
# 3. stamp_comps_excluded / POST /api/comics/comps/exclude
# ---------------------------------------------------------------------------


def test_stamp_only_touches_slab_rows(db):
    """A raw row is NEVER stamped by this path, whatever the caller sends —
    every code comes from a guard that only ever runs in graded mode, and
    silently thinning the raw pool is the expensive direction."""
    comic_id = upsert_comic(db, "Invincible", "1", 2003)
    upsert_comps(db, comic_id, [_comp(product_id="raw-1", pool="raw",
                                      certifier="none")])

    result = stamp_comps_excluded(db, comic_id, ["raw-1"], "manual")

    assert result["stamped"] == 0
    assert result["not_found"] == ["raw-1"]
    assert db.execute(
        "SELECT excluded_code FROM comps WHERE product_id='raw-1'"
    ).fetchone()[0] is None


def test_stamp_never_reaches_another_books_pool(db):
    """A product_id is unique per (provider, pool), not across books."""
    invincible = upsert_comic(db, "Invincible", "1", 2003)
    batman = upsert_comic(db, "Batman", "227", 1974)
    upsert_comps(db, batman, [_comp(product_id="shared")])
    upsert_comps(db, invincible, [_comp(product_id="shared")])

    result = stamp_comps_excluded(db, invincible, ["shared"], "cross_title")

    assert result["stamped"] == 1
    stamped = db.execute(
        "SELECT comic_id FROM comps WHERE excluded_code IS NOT NULL"
    ).fetchall()
    assert [r["comic_id"] for r in stamped] == [invincible]


def test_restamping_keeps_the_first_stamps_time_and_code(db):
    """Idempotency, and the reason for it: a second sweep over a swept
    archive must write nothing, and must not restate WHEN a row was judged."""
    comic_id = upsert_comic(db, "Invincible", "1", 2003)
    upsert_comps(db, comic_id, [_comp(product_id="15719")])
    stamp_comps_excluded(db, comic_id, ["15719"], "store_variant")
    first = db.execute(
        "SELECT excluded_code, excluded_at FROM comps WHERE product_id='15719'"
    ).fetchone()

    again = stamp_comps_excluded(db, comic_id, ["15719"], "manual")

    assert again == {"comic_id": comic_id, "code": "manual", "stamped": 0,
                     "already_stamped": 1, "not_found": []}
    now = db.execute(
        "SELECT excluded_code, excluded_at FROM comps WHERE product_id='15719'"
    ).fetchone()
    assert tuple(now) == tuple(first)


def test_stamp_reports_ids_it_could_not_find(db):
    comic_id = upsert_comic(db, "Invincible", "1", 2003)
    upsert_comps(db, comic_id, [_comp(product_id="15719")])

    result = stamp_comps_excluded(
        db, comic_id, ["15719", "never-stored"], "store_variant")

    assert result["stamped"] == 1
    assert result["not_found"] == ["never-stored"]


def test_stamp_matches_an_int_product_id(db):
    """The column is TEXT and a JSON body can carry an int; comparing without
    normalizing would silently stamp nothing and report `not_found`."""
    comic_id = upsert_comic(db, "Invincible", "1", 2003)
    upsert_comps(db, comic_id, [_comp(product_id="15719")])

    result = stamp_comps_excluded(db, comic_id, [15719], "store_variant")

    assert result["stamped"] == 1


def test_stamp_on_an_unknown_comic_returns_none(db):
    assert stamp_comps_excluded(db, 999999, ["15719"], "manual") is None


def test_stamp_refuses_an_unknown_code(db):
    """The column carries no CHECK (a new guard code must not need a table
    rebuild), so this is the only thing between a direct-Python caller and an
    unrecognized code on a row."""
    comic_id = upsert_comic(db, "Invincible", "1", 2003)
    upsert_comps(db, comic_id, [_comp(product_id="15719")])
    with pytest.raises(ValueError):
        stamp_comps_excluded(db, comic_id, ["15719"], "not-a-real-code")
    assert db.execute(
        "SELECT excluded_code FROM comps WHERE product_id='15719'"
    ).fetchone()[0] is None


def test_the_code_vocabulary_covers_every_live_guard_code():
    """The five codes `ebay-sold-comps` emits in `graded_identity_dropped_ids`
    (BUI-946/962), plus the operator's own. `comic-fmv` forwards those codes
    verbatim across an HTTP-only package boundary, so a code missing here is
    a 422 that discards the whole stamp at runtime."""
    assert set(COMPS_EXCLUSION_CODES) == {
        "multibook_lot", "cross_title", "store_variant", "hard_exclude",
        "printing", "manual",
    }


# ---------------------------------------------------------------------------
# 4. unstamp_comps_excluded / POST /api/comics/comps/unstamp (BUI-962)
# ---------------------------------------------------------------------------


def test_unstamp_clears_the_stamp(db):
    comic_id = upsert_comic(db, "Invincible", "1", 2003)
    upsert_comps(db, comic_id, [_comp(product_id="15719")])
    stamp_comps_excluded(db, comic_id, ["15719"], "store_variant")
    row_id = db.execute(
        "SELECT id FROM comps WHERE product_id='15719'"
    ).fetchone()[0]

    result = unstamp_comps_excluded(db, [row_id])

    assert result == {"dry_run": False, "unstamped": [row_id],
                      "not_stamped": [], "not_found": []}
    row = db.execute(
        "SELECT excluded_code, excluded_at FROM comps WHERE id=?", (row_id,)
    ).fetchone()
    assert row["excluded_code"] is None
    assert row["excluded_at"] is None
    # The corrected row re-enters the default (un-audited) read.
    assert [r["product_id"] for r in get_comps(db, comic_id=comic_id)] == ["15719"]


def test_unstamp_dry_run_writes_nothing(db):
    comic_id = upsert_comic(db, "Invincible", "1", 2003)
    upsert_comps(db, comic_id, [_comp(product_id="15719")])
    stamp_comps_excluded(db, comic_id, ["15719"], "store_variant")
    row_id = db.execute(
        "SELECT id FROM comps WHERE product_id='15719'"
    ).fetchone()[0]

    result = unstamp_comps_excluded(db, [row_id], dry_run=True)

    assert result == {"dry_run": True, "unstamped": [row_id],
                      "not_stamped": [], "not_found": []}
    # Nothing was written: the row is still stamped, same code and time.
    row = db.execute(
        "SELECT excluded_code, excluded_at FROM comps WHERE id=?", (row_id,)
    ).fetchone()
    assert row["excluded_code"] == "store_variant"
    assert row["excluded_at"] is not None
    assert get_comps(db, comic_id=comic_id) == []


def test_unstamp_leaves_other_stamped_rows_alone(db):
    """Only the listed ids are touched — a correction must never widen into a
    bulk un-stamp of rows the caller never named."""
    comic_id = upsert_comic(db, "Invincible", "1", 2003)
    upsert_comps(db, comic_id, [
        _comp(product_id="15719"),
        _comp(product_id="15712", price=786.0),
    ])
    stamp_comps_excluded(db, comic_id, ["15719"], "store_variant")
    stamp_comps_excluded(db, comic_id, ["15712"], "hard_exclude")
    target_id = db.execute(
        "SELECT id FROM comps WHERE product_id='15719'"
    ).fetchone()[0]
    other_id = db.execute(
        "SELECT id FROM comps WHERE product_id='15712'"
    ).fetchone()[0]

    result = unstamp_comps_excluded(db, [target_id])

    assert result["unstamped"] == [target_id]
    rows = {r["product_id"]: r["excluded_code"]
           for r in get_comps(db, comic_id=comic_id, include_excluded=True)}
    assert rows == {"15719": None, "15712": "hard_exclude"}
    other_row = db.execute(
        "SELECT excluded_code FROM comps WHERE id=?", (other_id,)
    ).fetchone()
    assert other_row["excluded_code"] == "hard_exclude"


def test_unstamp_reports_a_never_stamped_id_without_touching_it(db):
    comic_id = upsert_comic(db, "Invincible", "1", 2003)
    upsert_comps(db, comic_id, [_comp(product_id="15719")])
    row_id = db.execute(
        "SELECT id FROM comps WHERE product_id='15719'"
    ).fetchone()[0]

    result = unstamp_comps_excluded(db, [row_id])

    assert result == {"dry_run": False, "unstamped": [],
                      "not_stamped": [row_id], "not_found": []}


def test_unstamp_reports_an_unknown_id(db):
    result = unstamp_comps_excluded(db, [999999])
    assert result == {"dry_run": False, "unstamped": [],
                      "not_stamped": [], "not_found": [999999]}


def test_unstamp_deduplicates_a_repeated_id(db):
    """A caller repeating an id in the list must see it classified once, not
    once per repetition."""
    comic_id = upsert_comic(db, "Invincible", "1", 2003)
    upsert_comps(db, comic_id, [_comp(product_id="15719")])
    stamp_comps_excluded(db, comic_id, ["15719"], "store_variant")
    row_id = db.execute(
        "SELECT id FROM comps WHERE product_id='15719'"
    ).fetchone()[0]

    result = unstamp_comps_excluded(db, [row_id, row_id])

    assert result == {"dry_run": False, "unstamped": [row_id],
                      "not_stamped": [], "not_found": []}


def test_unstamp_is_idempotent(db):
    """Re-running a correction over the same id is a genuine no-op the
    second time — the same idempotence `stamp_comps_excluded` guarantees in
    the other direction."""
    comic_id = upsert_comic(db, "Invincible", "1", 2003)
    upsert_comps(db, comic_id, [_comp(product_id="15719")])
    stamp_comps_excluded(db, comic_id, ["15719"], "store_variant")
    row_id = db.execute(
        "SELECT id FROM comps WHERE product_id='15719'"
    ).fetchone()[0]

    first = unstamp_comps_excluded(db, [row_id])
    second = unstamp_comps_excluded(db, [row_id])

    assert first == {"dry_run": False, "unstamped": [row_id],
                     "not_stamped": [], "not_found": []}
    assert second == {"dry_run": False, "unstamped": [],
                      "not_stamped": [row_id], "not_found": []}


# ---------------------------------------------------------------------------
# The HTTP surface
# ---------------------------------------------------------------------------


def _create_comic(api, **overrides) -> int:
    body = {"title": "Invincible", "issue": "1", "year": 2003}
    body.update(overrides)
    return api.post("/api/comics", json=body).json()["comic_id"]


def _ingest(api, comic_id, *comps) -> None:
    r = api.post("/api/comics/comps",
                 json={"comic_id": comic_id, "comps": list(comps)})
    assert r.status_code == 200, r.text


def test_exclude_endpoint_stamps_and_the_read_stops_serving_the_row(api):
    comic_id = _create_comic(api)
    _ingest(api, comic_id, _comp(product_id="15719"), _comp(product_id="15712"))

    r = api.post("/api/comics/comps/exclude", json={
        "comic_id": comic_id, "product_ids": ["15719"],
        "code": "store_variant",
    })
    assert r.status_code == 200, r.text
    assert r.json() == {"comic_id": comic_id, "code": "store_variant",
                        "stamped": 1, "already_stamped": 0, "not_found": []}

    served = api.get("/api/comics/comps", params={"comic_id": comic_id}).json()
    assert [row["product_id"] for row in served] == ["15712"]


def test_the_stamp_is_persisted_to_the_real_db(api):
    comic_id = _create_comic(api)
    _ingest(api, comic_id, _comp(product_id="15719"))
    api.post("/api/comics/comps/exclude", json={
        "comic_id": comic_id, "product_ids": ["15719"], "code": "printing",
    })
    conn = sqlite3.connect(os.environ["DB_PATH"])
    try:
        code, at = conn.execute(
            "SELECT excluded_code, excluded_at FROM comps "
            "WHERE product_id='15719'"
        ).fetchone()
    finally:
        conn.close()
    assert code == "printing"
    assert at is not None


def test_the_get_route_can_show_excluded_rows_for_an_audit(api):
    comic_id = _create_comic(api)
    _ingest(api, comic_id, _comp(product_id="15719"))
    api.post("/api/comics/comps/exclude", json={
        "comic_id": comic_id, "product_ids": ["15719"],
        "code": "store_variant",
    })
    rows = api.get("/api/comics/comps", params={
        "comic_id": comic_id, "include_excluded": "true",
    }).json()
    assert [row["excluded_code"] for row in rows] == ["store_variant"]


def test_exclude_endpoint_404s_an_unknown_comic(api):
    r = api.post("/api/comics/comps/exclude", json={
        "comic_id": 999999, "product_ids": ["15719"], "code": "manual",
    })
    assert r.status_code == 404


def test_exclude_endpoint_422s_an_unknown_code(api):
    comic_id = _create_comic(api)
    _ingest(api, comic_id, _comp(product_id="15719"))
    r = api.post("/api/comics/comps/exclude", json={
        "comic_id": comic_id, "product_ids": ["15719"], "code": "autograph",
    })
    assert r.status_code == 422
    # Nothing was stamped: the whole call is refused before any write.
    rows = api.get("/api/comics/comps", params={"comic_id": comic_id}).json()
    assert [row["product_id"] for row in rows] == ["15719"]


def test_a_refused_stamp_lands_in_the_rejections_ledger(api):
    """`LedgerRoute` covers this route like every other overlay write, so a
    new guard code the server has never heard of is visible rather than a
    silent 422 in a client's stderr."""
    comic_id = _create_comic(api)
    _ingest(api, comic_id, _comp(product_id="15719"))
    api.post("/api/comics/comps/exclude", json={
        "comic_id": comic_id, "product_ids": ["15719"], "code": "autograph",
    })
    rejections = api.get(
        "/api/comics/health/rejections?hours=24").json()["rejections"]
    paths = [row["path"] for row in rejections]
    assert "/api/comics/comps/exclude" in paths


def test_exclude_endpoint_422s_an_empty_product_id_list(api):
    comic_id = _create_comic(api)
    r = api.post("/api/comics/comps/exclude", json={
        "comic_id": comic_id, "product_ids": [], "code": "manual",
    })
    assert r.status_code == 422


def test_exclude_endpoint_422s_a_missing_comic_id(api):
    """`comic_id` is required here, unlike the ingest route where it is
    nullable — an exclusion is always a judgement about ONE book's pool."""
    r = api.post("/api/comics/comps/exclude", json={
        "product_ids": ["15719"], "code": "manual",
    })
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# POST /api/comics/comps/unstamp (BUI-962)
# ---------------------------------------------------------------------------


def _stamped_row_id(api, comic_id, product_id, code="store_variant") -> int:
    r = api.post("/api/comics/comps/exclude", json={
        "comic_id": comic_id, "product_ids": [product_id], "code": code,
    })
    assert r.status_code == 200, r.text
    rows = api.get("/api/comics/comps", params={
        "comic_id": comic_id, "include_excluded": "true",
    }).json()
    return next(row["id"] for row in rows if row["product_id"] == product_id)


def test_unstamp_endpoint_dry_run_default_writes_nothing(api):
    comic_id = _create_comic(api)
    _ingest(api, comic_id, _comp(product_id="15719"))
    row_id = _stamped_row_id(api, comic_id, "15719")

    r = api.post("/api/comics/comps/unstamp", json={"ids": [row_id]})

    assert r.status_code == 200, r.text
    assert r.json() == {"dry_run": True, "unstamped": [row_id],
                        "not_stamped": [], "not_found": []}
    # Still stamped — the audit read still shows it, and the default read
    # still skips it.
    served = api.get("/api/comics/comps", params={"comic_id": comic_id}).json()
    assert served == []
    audited = api.get("/api/comics/comps", params={
        "comic_id": comic_id, "include_excluded": "true",
    }).json()
    assert audited[0]["excluded_code"] == "store_variant"


def test_unstamp_endpoint_apply_clears_exactly_the_listed_ids(api):
    comic_id = _create_comic(api)
    _ingest(api, comic_id, _comp(product_id="15719"), _comp(product_id="15712"))
    target_id = _stamped_row_id(api, comic_id, "15719", code="store_variant")
    other_id = _stamped_row_id(api, comic_id, "15712", code="hard_exclude")

    r = api.post("/api/comics/comps/unstamp",
                 json={"ids": [target_id], "dry_run": False})

    assert r.status_code == 200, r.text
    assert r.json() == {"dry_run": False, "unstamped": [target_id],
                        "not_stamped": [], "not_found": []}
    served = {row["product_id"]: row["excluded_code"]
             for row in api.get("/api/comics/comps", params={
                 "comic_id": comic_id, "include_excluded": "true",
             }).json()}
    assert served == {"15719": None, "15712": "hard_exclude"}
    # The corrected row is back in the default (un-audited) read; the other
    # stamped row is still held out.
    assert [row["product_id"] for row in api.get(
        "/api/comics/comps", params={"comic_id": comic_id}).json()] == ["15719"]
    other_row = next(row for row in api.get("/api/comics/comps", params={
        "comic_id": comic_id, "include_excluded": "true",
    }).json() if row["id"] == other_id)
    assert other_row["excluded_code"] == "hard_exclude"


def test_unstamp_endpoint_422s_an_empty_ids_list(api):
    r = api.post("/api/comics/comps/unstamp", json={"ids": []})
    assert r.status_code == 422


def test_unstamp_endpoint_422s_a_missing_ids_field(api):
    r = api.post("/api/comics/comps/unstamp", json={"dry_run": False})
    assert r.status_code == 422
