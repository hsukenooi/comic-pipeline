"""Tests for the BUI-924 price-identity migration (plan unit U2).

`fmv` was unique on `(comic_id, grade)`, so a CGC 9.6 slab and a raw 9.6 copy
of the same book collided on ONE row — the second write silently overwrote the
first's price. Widening the key to `(comic_id, grade, certifier, label)` is a
table REBUILD, not an index swap, because the constraint is inline in the
`CREATE TABLE fmv` literal.

That rebuild is the dangerous part and it is what most of this file pins.
`fmv` is the price store; `bids.fmv_id` points at it with **no foreign key**,
so a dangling link is invisible to `PRAGMA foreign_key_check` (the BUI-626
lesson). And `bids.fmv_id` IS declared `REFERENCES fmv(id) ON DELETE SET NULL`
on the host side, so `DROP TABLE fmv` silently nulls every bid's primary price
link. Every inbound path is therefore asserted with a DIRECT query against the
path's own table — never a JOIN, which returns empty for exactly the dangling
case it is supposed to catch.

Mirrors `test_year_nullable.py`, whose migration this one copies.
"""
from __future__ import annotations

import sqlite3

import pytest

from gixen_overlay.db import (
    COMP_PAGE_QUALITIES,
    FMV_CERTIFIERS,
    FMV_LABELS,
    FMV_PRICING_BASES,
    append_fmv_history,
    create_tables,
    list_comics,
    upsert_comic,
    upsert_comps,
    upsert_fmv,
)

# ---------------------------------------------------------------------------
# Fixtures: a legacy-shaped DB (pre-BUI-924) with linked bids
# ---------------------------------------------------------------------------


def _legacy_db() -> sqlite3.Connection:
    """The exact pre-BUI-924 schema: `fmv` UNIQUE(comic_id, grade), no certifier.

    Includes every additive column the modern `fmv` carries (`flag_reason`,
    `ungraded_anchor`, `ungraded_anchor_n`, `provenance`) so the rebuild is
    exercised against the shape actually running on the Mac Mini, not a
    stripped-down one where a dropped column would go unnoticed.

    `bids.fmv_id` carries the real `ON DELETE SET NULL` reference, and
    `PRAGMA foreign_keys=ON` is set, so the cascade the rebuild has to survive
    actually fires here.
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(
        "CREATE TABLE bids ("
        "id INTEGER PRIMARY KEY, item_id TEXT NOT NULL, max_bid REAL NOT NULL, "
        "fmv_id INTEGER REFERENCES fmv(id) ON DELETE SET NULL)"
    )
    conn.execute(
        "CREATE TABLE comics ("
        "id INTEGER PRIMARY KEY, title TEXT NOT NULL, issue TEXT NOT NULL, "
        "year INTEGER, variant TEXT, locg_id INTEGER, locg_variant_id INTEGER, "
        "created_at TEXT DEFAULT (datetime('now')))"
    )
    conn.execute(
        "CREATE TABLE fmv ("
        "id INTEGER PRIMARY KEY, "
        "comic_id INTEGER NOT NULL REFERENCES comics(id) ON DELETE CASCADE, "
        "grade REAL NOT NULL, low REAL, high REAL, comps INTEGER, "
        "confidence TEXT CHECK(confidence IN ('high','medium','low') "
        "OR confidence IS NULL), "
        "notes TEXT, flag_reason TEXT, ungraded_anchor REAL, "
        "ungraded_anchor_n INTEGER, "
        "provenance TEXT CHECK(provenance IN ('machine','hand') "
        "OR provenance IS NULL), "
        "updated_at TEXT, UNIQUE(comic_id, grade))"
    )
    conn.execute(
        "CREATE TABLE bid_fmvs ("
        "bid_id INTEGER NOT NULL REFERENCES bids(id) ON DELETE CASCADE, "
        "fmv_id INTEGER NOT NULL REFERENCES fmv(id) ON DELETE CASCADE, "
        "is_primary INTEGER NOT NULL DEFAULT 0, "
        "PRIMARY KEY (bid_id, fmv_id))"
    )
    # Legacy comps + fmv_history, without the new columns, so the additive
    # ALTERs have work to do.
    conn.execute(
        "CREATE TABLE comps ("
        "id INTEGER PRIMARY KEY, "
        "comic_id INTEGER REFERENCES comics(id) ON DELETE SET NULL, "
        "pool TEXT NOT NULL CHECK(pool IN ('raw','slab')), "
        "provider TEXT NOT NULL, product_id TEXT NOT NULL, title TEXT, "
        "price REAL, sold_date TEXT, grade REAL, buying_format TEXT, "
        "link TEXT, query TEXT, tier TEXT, from_cache INTEGER, "
        "observed_at TEXT, "
        "provenance TEXT NOT NULL "
        "CHECK(provenance IN ('live','backfill-cache','backfill-capture')), "
        "first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL, "
        "seen_count INTEGER NOT NULL DEFAULT 1, "
        "conflict_count INTEGER NOT NULL DEFAULT 0)"
    )
    conn.execute(
        "CREATE TABLE fmv_history ("
        "id INTEGER PRIMARY KEY, "
        "comic_id INTEGER NOT NULL REFERENCES comics(id) ON DELETE CASCADE, "
        "grade REAL NOT NULL, low REAL, high REAL, comps INTEGER, "
        "confidence TEXT, flag_reason TEXT, notes TEXT, recorded_at TEXT, "
        "source TEXT NOT NULL CHECK(source IN ('upsert','backfill')))"
    )
    # Mark the older one-time DATA migrations as already-run so this legacy DB
    # exercises only the BUI-924 path. Deliberately NOT 'fmv_split' or
    # 'year_nullable': those two names are CRASH markers, not done-markers —
    # seeding them would (correctly) raise.
    conn.execute("CREATE TABLE migration_state (migration TEXT PRIMARY KEY)")
    for name in (
        "sweep_allcaps_orphans", "lowercase_title_indexes",
        "seed_fmv_history", "backfill_fmv_provenance",
    ):
        conn.execute("INSERT INTO migration_state (migration) VALUES (?)", (name,))
    return conn


def _seed_legacy_rows(conn: sqlite3.Connection) -> None:
    """3 fmv rows, 2 bid_fmvs rows, 2 bids carrying fmv_id — the U2 scenario."""
    conn.execute(
        "INSERT INTO comics (id, title, issue, year) VALUES (1, 'ASM', '50', 1967)"
    )
    conn.execute(
        "INSERT INTO comics (id, title, issue, year) VALUES (2, 'Hulk', '181', 1974)"
    )
    conn.execute(
        "INSERT INTO fmv (id, comic_id, grade, low, high, comps, confidence, "
        "notes, provenance, updated_at) VALUES "
        "(11, 1, 6.0, 600, 680, 7, 'high', 'hand § priced by me', 'hand', "
        "'2026-08-01T00:00:00+00:00')"
    )
    conn.execute(
        "INSERT INTO fmv (id, comic_id, grade, low, high, comps, confidence, "
        "notes, flag_reason, ungraded_anchor, ungraded_anchor_n, updated_at) "
        "VALUES (22, 1, 9.0, NULL, NULL, 0, NULL, 'sparse pool', 'too_sparse', "
        "125.5, 4, '2026-08-02T00:00:00+00:00')"
    )
    conn.execute(
        "INSERT INTO fmv (id, comic_id, grade, low, high, comps, notes, "
        "updated_at) VALUES (33, 2, 5.5, 2000, 2400, 11, "
        "'interpolated=grade 5.0→6.0', '2026-08-03T00:00:00+00:00')"
    )
    conn.execute(
        "INSERT INTO bids (id, item_id, max_bid, fmv_id) VALUES (101, 'A1', 500, 11)"
    )
    conn.execute(
        "INSERT INTO bids (id, item_id, max_bid, fmv_id) VALUES (102, 'B2', 900, 33)"
    )
    conn.execute(
        "INSERT INTO bid_fmvs (bid_id, fmv_id, is_primary) VALUES (101, 11, 1)"
    )
    conn.execute(
        "INSERT INTO bid_fmvs (bid_id, fmv_id, is_primary) VALUES (102, 33, 1)"
    )
    conn.commit()


def _fresh_db() -> sqlite3.Connection:
    """A post-migration DB built from scratch (the fresh-install path)."""
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


def _cols(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


# ---------------------------------------------------------------------------
# Vocabulary constants
# ---------------------------------------------------------------------------


def test_vocabularies_are_the_documented_closed_sets():
    assert FMV_CERTIFIERS == ("none", "cgc", "cbcs", "other")
    assert FMV_LABELS == (
        "universal", "signature_series", "qualified", "restored",
        "conserved", "other",
    )
    assert COMP_PAGE_QUALITIES == (
        "white", "ow_w", "ow", "c_ow", "cream", "unknown",
    )
    # BUI-952 appended 'lone_sale'. Appending to this tuple is never the
    # whole change: the CHECK constraint that enforces it is frozen into the
    # stored schema of every DB that already ran, so see
    # `test_the_pricing_basis_check_is_widened_on_an_existing_db` below.
    assert FMV_PRICING_BASES == (
        "direct", "interpolated", "ladder", "proxy", "lone_sale")


# ---------------------------------------------------------------------------
# The rebuild: nothing may be lost
# ---------------------------------------------------------------------------


def test_rebuild_preserves_counts_ids_and_every_inbound_bid_path():
    conn = _legacy_db()
    _seed_legacy_rows(conn)

    create_tables(conn)
    conn.commit()

    # Counts unchanged.
    assert conn.execute("SELECT COUNT(*) FROM fmv").fetchone()[0] == 3
    assert conn.execute("SELECT COUNT(*) FROM bid_fmvs").fetchone()[0] == 2

    # Every fmv.id preserved (ids, not just a count — a rebuild that
    # renumbered would keep the count and break every inbound link).
    assert [r[0] for r in conn.execute("SELECT id FROM fmv ORDER BY id")] == [11, 22, 33]

    # Inbound path 1: bid_fmvs.fmv_id. DIRECT query, no JOIN — a JOIN returns
    # empty for exactly the dangling case (BUI-626).
    assert sorted(
        r[0] for r in conn.execute("SELECT fmv_id FROM bid_fmvs")
    ) == [11, 33]

    # Inbound path 2: bids.fmv_id. This is the one the ON DELETE SET NULL
    # cascade nulls when `fmv` is dropped.
    assert [
        (r["id"], r["fmv_id"])
        for r in conn.execute("SELECT id, fmv_id FROM bids ORDER BY id")
    ] == [(101, 11), (102, 33)]

    # Inbound path 3: bids.fmv_id values must still resolve to a live fmv row.
    live_ids = {r[0] for r in conn.execute("SELECT id FROM fmv")}
    dangling = [
        r["fmv_id"]
        for r in conn.execute("SELECT fmv_id FROM bids WHERE fmv_id IS NOT NULL")
        if r["fmv_id"] not in live_ids
    ]
    assert dangling == []

    # Raw is an explicit sentinel on every migrated row — never NULL.
    rows = conn.execute("SELECT certifier, label FROM fmv").fetchall()
    assert all(r["certifier"] == "none" and r["label"] == "universal" for r in rows)


def test_rebuild_preserves_bid_links_with_foreign_keys_off_too():
    """The `sqlite3` CLI default is OFF; the server runs ON.

    With enforcement OFF the `ON DELETE SET NULL` cascade never fires, so
    `bids.fmv_id` is untouched by the DROP and the restore UPDATE is a no-op
    that writes back the same values. Pinned because "it worked when I tested
    it" must not depend on which of the two connections ran the migration.
    """
    conn = _legacy_db()
    conn.execute("PRAGMA foreign_keys=OFF")
    _seed_legacy_rows(conn)

    create_tables(conn)
    conn.commit()

    assert [
        (r["id"], r["fmv_id"])
        for r in conn.execute("SELECT id, fmv_id FROM bids ORDER BY id")
    ] == [(101, 11), (102, 33)]
    assert conn.execute("SELECT COUNT(*) FROM fmv").fetchone()[0] == 3


def test_rebuild_leaves_an_already_dangling_bid_link_exactly_as_it_found_it():
    """A pre-existing dangle is neither repaired nor re-pointed.

    `bids.fmv_id` has NO foreign key, so such a row is invisible to
    `PRAGMA foreign_key_check` (BUI-626) and can be left behind by a CLI
    session that never enabled enforcement. Two mechanisms meet here and the
    result is worth pinning rather than assuming: the `ON DELETE SET NULL`
    cascade fires only for ids that actually EXIST in `fmv`, so it never
    touches this one, and the save-side JOIN drops it, so the restore never
    writes it either. The value therefore survives untouched — which is safe
    only because every real `fmv.id` is preserved, so a stale id can never
    come to name a DIFFERENT row than it did before.
    """
    conn = _legacy_db()
    _seed_legacy_rows(conn)  # ends committed, so the PRAGMA below takes effect
    # `PRAGMA foreign_keys` is SILENTLY IGNORED inside a transaction, so each
    # toggle is bracketed by a commit — the same constraint `server/db.py`'s
    # own rebuilds document.
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("UPDATE bids SET fmv_id=999 WHERE id=101")
    conn.commit()
    conn.execute("PRAGMA foreign_keys=ON")

    create_tables(conn)
    conn.commit()

    assert conn.execute(
        "SELECT fmv_id FROM bids WHERE id=101"
    ).fetchone()["fmv_id"] == 999
    # The intact link is untouched, and the id it names still exists.
    assert conn.execute(
        "SELECT fmv_id FROM bids WHERE id=102"
    ).fetchone()["fmv_id"] == 33
    assert conn.execute("SELECT COUNT(*) FROM fmv WHERE id=999").fetchone()[0] == 0


def test_rebuild_fails_loudly_on_a_column_the_literal_does_not_know():
    """The save list comes from PRAGMA, so an unknown column cannot be dropped.

    It fails the restore INSERT instead — and the host rolls the whole
    `register_db_tables` savepoint back. That is the correct direction: the
    fix is to add the column to the `CREATE TABLE fmv` literal, never to lose
    the data it holds.
    """
    conn = _legacy_db()
    _seed_legacy_rows(conn)
    conn.execute("ALTER TABLE fmv ADD COLUMN some_future_column TEXT")
    conn.commit()

    with pytest.raises(sqlite3.OperationalError, match="some_future_column"):
        create_tables(conn)


def test_rebuild_preserves_provenance_flag_reason_and_anchor():
    """Every column is carried by the PRAGMA-derived list, not a literal."""
    conn = _legacy_db()
    _seed_legacy_rows(conn)

    create_tables(conn)
    conn.commit()

    hand = conn.execute("SELECT * FROM fmv WHERE id=11").fetchone()
    assert hand["provenance"] == "hand"
    assert hand["low"] == 600 and hand["high"] == 680
    assert hand["notes"] == "hand § priced by me"
    assert hand["confidence"] == "high"
    assert hand["updated_at"] == "2026-08-01T00:00:00+00:00"

    flagged = conn.execute("SELECT * FROM fmv WHERE id=22").fetchone()
    assert flagged["flag_reason"] == "too_sparse"
    assert flagged["ungraded_anchor"] == 125.5
    assert flagged["ungraded_anchor_n"] == 4


def test_rebuild_widens_the_unique_key():
    conn = _legacy_db()
    _seed_legacy_rows(conn)
    create_tables(conn)
    conn.commit()

    # Slab beside raw at the same grade: allowed.
    conn.execute(
        "INSERT INTO fmv (comic_id, grade, certifier, label, low) "
        "VALUES (1, 6.0, 'cgc', 'universal', 1400)"
    )
    # A second raw row at that key: still rejected.
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO fmv (comic_id, grade, certifier, label, low) "
            "VALUES (1, 6.0, 'none', 'universal', 700)"
        )
    conn.rollback()


def test_rebuild_enforces_the_certifier_and_label_vocabularies():
    conn = _fresh_db()
    comic_id = upsert_comic(conn, title="X", issue="1", year=1963)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO fmv (comic_id, grade, certifier) VALUES (?, 9.6, 'pgx')",
            (comic_id,),
        )
    conn.rollback()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO fmv (comic_id, grade, label) VALUES (?, 9.6, 'blue')",
            (comic_id,),
        )
    conn.rollback()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO fmv (comic_id, grade, pricing_basis) "
            "VALUES (?, 9.6, 'vibes')",
            (comic_id,),
        )
    conn.rollback()


def test_create_tables_is_idempotent_on_an_already_migrated_db():
    conn = _legacy_db()
    _seed_legacy_rows(conn)
    create_tables(conn)
    conn.commit()

    before = [tuple(r) for r in conn.execute("SELECT * FROM fmv ORDER BY id")]
    bids_before = [
        tuple(r) for r in conn.execute("SELECT id, fmv_id FROM bids ORDER BY id")
    ]

    create_tables(conn)
    conn.commit()

    after = [tuple(r) for r in conn.execute("SELECT * FROM fmv ORDER BY id")]
    assert after == before
    assert [
        tuple(r) for r in conn.execute("SELECT id, fmv_id FROM bids ORDER BY id")
    ] == bids_before


def test_stale_migration_marker_raises_before_any_write():
    """A crash in the post-DROP window leaves the schema looking migrated."""
    conn = _legacy_db()
    _seed_legacy_rows(conn)
    create_tables(conn)
    conn.commit()

    conn.execute(
        "INSERT INTO migration_state (migration) VALUES ('fmv_certifier_rebuild')"
    )
    conn.commit()

    with pytest.raises(RuntimeError, match="fmv_certifier_rebuild"):
        create_tables(conn)

    # Nothing was destroyed on the way to the raise.
    assert conn.execute("SELECT COUNT(*) FROM fmv").fetchone()[0] == 3


def test_fresh_install_has_the_new_columns_and_no_marker():
    conn = _fresh_db()
    assert {"certifier", "label", "pricing_basis"} <= _cols(conn, "fmv")
    assert {"certifier", "label"} <= _cols(conn, "fmv_history")
    assert {"certifier", "label", "page_quality"} <= _cols(conn, "comps")
    assert conn.execute(
        "SELECT 1 FROM migration_state WHERE migration='fmv_certifier_rebuild'"
    ).fetchone() is None


# ---------------------------------------------------------------------------
# upsert_fmv on the full key
# ---------------------------------------------------------------------------


def test_upsert_fmv_slab_beside_raw_returns_the_slab_row_id():
    """The trailing id lookup must use the FULL key.

    `SELECT id FROM fmv WHERE comic_id=? AND grade=?` returns *either* row once
    two coexist — and that id is what the API echoes and what `bids.fmv_id`
    ends up pointing at. Getting it wrong links a slab bid to the raw price.
    """
    conn = _fresh_db()
    comic_id = upsert_comic(conn, title="ASM", issue="300", year=1988)
    raw_id = upsert_fmv(conn, comic_id, 9.6, low=100, high=140)
    slab_id = upsert_fmv(
        conn, comic_id, 9.6, low=900, high=1100, certifier="cgc", label="universal"
    )

    assert slab_id != raw_id
    assert conn.execute(
        "SELECT low FROM fmv WHERE id=?", (raw_id,)
    ).fetchone()["low"] == 100
    assert conn.execute(
        "SELECT low FROM fmv WHERE id=?", (slab_id,)
    ).fetchone()["low"] == 900


def test_upsert_fmv_second_raw_write_upserts_in_place():
    conn = _fresh_db()
    comic_id = upsert_comic(conn, title="ASM", issue="300", year=1988)
    first = upsert_fmv(conn, comic_id, 9.6, low=100, high=140)
    second = upsert_fmv(conn, comic_id, 9.6, low=120, high=160)
    assert first == second
    assert conn.execute(
        "SELECT COUNT(*) FROM fmv WHERE comic_id=? AND grade=9.6", (comic_id,)
    ).fetchone()[0] == 1


def test_a_legacy_client_write_never_touches_the_slab_row():
    """An old `comic-fmv` sends no certifier — it must land on the raw row."""
    conn = _fresh_db()
    comic_id = upsert_comic(conn, title="ASM", issue="300", year=1988)
    slab_id = upsert_fmv(
        conn, comic_id, 9.6, low=900, high=1100, certifier="cgc"
    )
    raw_id = upsert_fmv(conn, comic_id, 9.6, low=100, high=140)

    assert raw_id != slab_id
    slab = conn.execute("SELECT * FROM fmv WHERE id=?", (slab_id,)).fetchone()
    assert slab["low"] == 900 and slab["high"] == 1100

    # And the reverse: a slab write must not disturb the raw row.
    upsert_fmv(conn, comic_id, 9.6, low=950, high=1200, certifier="cgc")
    raw = conn.execute("SELECT * FROM fmv WHERE id=?", (raw_id,)).fetchone()
    assert raw["low"] == 100 and raw["high"] == 140


def test_upsert_fmv_rejects_an_unknown_certifier_or_label():
    conn = _fresh_db()
    comic_id = upsert_comic(conn, title="X", issue="1", year=1963)
    with pytest.raises(ValueError, match="certifier"):
        upsert_fmv(conn, comic_id, 9.6, certifier="pgx")
    with pytest.raises(ValueError, match="label"):
        upsert_fmv(conn, comic_id, 9.6, label="blue")
    with pytest.raises(ValueError, match="pricing_basis"):
        upsert_fmv(conn, comic_id, 9.6, pricing_basis="vibes")


# ---------------------------------------------------------------------------
# pricing_basis
# ---------------------------------------------------------------------------


def test_upsert_derives_pricing_basis_from_notes_when_omitted():
    conn = _fresh_db()
    comic_id = upsert_comic(conn, title="X", issue="1", year=1963)

    interp = upsert_fmv(
        conn, comic_id, 9.0, low=50, high=50,
        notes="n=4 · interpolated=grade 8.5→9.4 · window ±0.5",
    )
    assert conn.execute(
        "SELECT pricing_basis FROM fmv WHERE id=?", (interp,)
    ).fetchone()[0] == "interpolated"

    proxy = upsert_fmv(
        conn, comic_id, 8.0, low=40, high=60, notes="CGC proxy: 0.5× of CGC 8.0",
    )
    assert conn.execute(
        "SELECT pricing_basis FROM fmv WHERE id=?", (proxy,)
    ).fetchone()[0] == "proxy"

    plain = upsert_fmv(conn, comic_id, 7.0, low=20, high=30, notes="n=9 · window ±0.5")
    assert conn.execute(
        "SELECT pricing_basis FROM fmv WHERE id=?", (plain,)
    ).fetchone()[0] == "direct"


def _narrow_the_pricing_basis_check(conn: sqlite3.Connection) -> None:
    """Rewind a fresh DB's `fmv.pricing_basis` CHECK to the pre-BUI-952
    vocabulary — i.e. the shape the Mac Mini is actually running.

    Built by rewriting the table's OWN stored DDL rather than by pasting a
    frozen `CREATE TABLE` literal here, so a column added to `fmv` next year
    cannot make this fixture quietly stop resembling production. The table is
    empty at this point (the caller seeds after), so nothing has to be moved.
    """
    sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='fmv'"
    ).fetchone()[0]
    current = ", ".join(f"'{b}'" for b in FMV_PRICING_BASES)
    old = ", ".join(f"'{b}'" for b in
                    ("direct", "interpolated", "ladder", "proxy"))
    assert current in sql, "the fmv DDL no longer spells the basis list this way"
    assert not conn.execute("SELECT 1 FROM fmv LIMIT 1").fetchone()
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("DROP TABLE fmv")
    conn.execute(sql.replace(current, old))
    conn.execute("PRAGMA foreign_keys=ON")


def test_a_pre_bui952_db_rejects_the_new_basis_before_the_migration():
    """The fixture is only worth anything if it reproduces the failure. A
    CHECK frozen at four values raises IntegrityError several frames below
    `upsert_fmv`'s own vocabulary check — the write dies and the server
    discards the whole row (the BUI-593 class)."""
    conn = _fresh_db()
    _narrow_the_pricing_basis_check(conn)
    conn.execute(
        "INSERT INTO comics (id, title, issue, year) VALUES (1, 'X', '1', 1963)"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO fmv (comic_id, grade, low, high, pricing_basis) "
            "VALUES (1, 4.5, 1000, 1000, 'lone_sale')"
        )


def test_the_pricing_basis_check_is_widened_on_an_existing_db():
    """BUI-952: `create_tables` widens the stale CHECK, and the rebuild that
    does it loses nothing — the same three inbound paths BUI-924's rebuild had
    to survive, each queried DIRECTLY rather than through a JOIN (a JOIN
    returns empty for exactly the dangling case it would be catching)."""
    conn = _fresh_db()
    _narrow_the_pricing_basis_check(conn)
    conn.execute(
        "INSERT INTO comics (id, title, issue, year) VALUES (1, 'X', '1', 1963)"
    )
    conn.execute(
        "INSERT INTO fmv (id, comic_id, grade, low, high, comps, certifier, "
        "label, pricing_basis) VALUES "
        "(11, 1, 4.5, 700, 900, 3, 'cgc', 'universal', 'ladder')"
    )
    conn.execute(
        "INSERT INTO bids (id, item_id, max_bid, fmv_id) "
        "VALUES (5, 'i5', 420, 11)"
    )
    conn.execute(
        "INSERT INTO bid_fmvs (bid_id, fmv_id, is_primary) VALUES (5, 11, 1)"
    )
    conn.commit()

    create_tables(conn)
    conn.commit()

    # The constraint now names every current basis...
    sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='fmv'"
    ).fetchone()[0]
    assert "'lone_sale'" in sql
    # ...and a lone_sale row actually lands.
    fmv_id = upsert_fmv(conn, 1, 5.5, low=1000, high=1000, certifier="cgc",
                        pricing_basis="lone_sale")
    assert conn.execute(
        "SELECT pricing_basis FROM fmv WHERE id=?", (fmv_id,)
    ).fetchone()[0] == "lone_sale"

    # Nothing was lost by the rebuild.
    row = conn.execute("SELECT * FROM fmv WHERE id=11").fetchone()
    assert row["low"] == 700 and row["high"] == 900
    assert row["certifier"] == "cgc" and row["pricing_basis"] == "ladder"
    assert conn.execute(
        "SELECT fmv_id FROM bids WHERE id=5"
    ).fetchone()[0] == 11
    assert conn.execute(
        "SELECT is_primary FROM bid_fmvs WHERE bid_id=5 AND fmv_id=11"
    ).fetchone()[0] == 1


def test_the_pricing_basis_check_widen_is_a_no_op_once_current():
    """Gated on the CONSTRAINT, not a marker, so a restart must not rebuild
    the money table again. Asserted by identity of the stored DDL and by the
    row ids surviving — a second rebuild would renumber nothing but would
    still be a DROP nobody asked for."""
    conn = _fresh_db()
    conn.execute(
        "INSERT INTO comics (id, title, issue, year) VALUES (1, 'X', '1', 1963)"
    )
    fmv_id = upsert_fmv(conn, 1, 4.5, low=1000, high=1000, certifier="cgc",
                        pricing_basis="lone_sale")
    conn.commit()
    before = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='fmv'"
    ).fetchone()[0]

    create_tables(conn)
    create_tables(conn)
    conn.commit()

    after = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='fmv'"
    ).fetchone()[0]
    assert after == before
    assert conn.execute(
        "SELECT pricing_basis FROM fmv WHERE id=?", (fmv_id,)
    ).fetchone()[0] == "lone_sale"


def test_upsert_accepts_the_lone_sale_basis_on_a_fresh_db():
    conn = _fresh_db()
    comic_id = upsert_comic(conn, title="X", issue="1", year=1963)
    fmv_id = upsert_fmv(
        conn, comic_id, 4.5, low=1000, high=1000, certifier="cgc",
        notes="certifier=cgc | label=universal | basis=lone_sale",
        pricing_basis="lone_sale",
    )
    assert conn.execute(
        "SELECT pricing_basis FROM fmv WHERE id=?", (fmv_id,)
    ).fetchone()[0] == "lone_sale"


def test_explicit_pricing_basis_wins_over_the_derived_one():
    conn = _fresh_db()
    comic_id = upsert_comic(conn, title="X", issue="1", year=1963)
    fmv_id = upsert_fmv(
        conn, comic_id, 9.6, low=900, high=1100, certifier="cgc",
        notes="n=1 exact · neighbours 9.4/9.8", pricing_basis="ladder",
    )
    assert conn.execute(
        "SELECT pricing_basis FROM fmv WHERE id=?", (fmv_id,)
    ).fetchone()[0] == "ladder"


def test_a_bare_stub_does_not_downgrade_a_priced_rows_pricing_basis():
    """The BUI-599 stub guard applies to this money-relevant column too."""
    conn = _fresh_db()
    comic_id = upsert_comic(conn, title="X", issue="1", year=1963)
    fmv_id = upsert_fmv(
        conn, comic_id, 9.6, low=900, high=1100, certifier="cgc",
        pricing_basis="ladder",
    )
    upsert_fmv(conn, comic_id, 9.6, comps=0, certifier="cgc")
    assert conn.execute(
        "SELECT pricing_basis FROM fmv WHERE id=?", (fmv_id,)
    ).fetchone()[0] == "ladder"


def test_pricing_basis_backfill_applies_the_same_rule_to_existing_rows():
    conn = _legacy_db()
    _seed_legacy_rows(conn)
    conn.execute(
        "INSERT INTO fmv (id, comic_id, grade, low, high, notes) "
        "VALUES (44, 2, 7.0, 500, 700, 'CGC proxy: 0.5× of CGC 7.0')"
    )
    conn.commit()

    create_tables(conn)
    conn.commit()

    basis = {
        r["id"]: r["pricing_basis"]
        for r in conn.execute("SELECT id, pricing_basis FROM fmv")
    }
    assert basis[33] == "interpolated"   # notes carry `interpolated=`
    assert basis[44] == "proxy"          # notes carry `CGC proxy`
    assert basis[11] == "direct"         # unmatched
    assert basis[22] == "direct"         # NULL notes-free flagged row


def test_pricing_basis_backfill_runs_once():
    conn = _legacy_db()
    _seed_legacy_rows(conn)
    create_tables(conn)
    conn.commit()

    # An operator corrects a row by hand; a restart must not re-derive it.
    conn.execute("UPDATE fmv SET pricing_basis='ladder' WHERE id=33")
    conn.commit()
    create_tables(conn)
    conn.commit()
    assert conn.execute(
        "SELECT pricing_basis FROM fmv WHERE id=33"
    ).fetchone()[0] == "ladder"


# ---------------------------------------------------------------------------
# The yearless-to-yeared merge
# ---------------------------------------------------------------------------


def _orphan_yearless(conn: sqlite3.Connection, title: str, issue: str) -> int:
    """Seed a yearless orphan beside an existing yeared row (PER-103 shape).

    Inserted by raw SQL because `upsert_comic` deliberately refuses to create
    one next to a yeared canonical row — the orphan is the legacy state the
    merge exists to clean up.
    """
    conn.execute(
        "INSERT INTO comics (title, issue, year) VALUES (?, ?, NULL)", (title, issue)
    )
    return conn.execute(
        "SELECT id FROM comics WHERE title=? AND issue=? AND year IS NULL",
        (title, issue),
    ).fetchone()[0]


def test_yearless_merge_keeps_both_a_raw_and_a_slab_row_at_one_grade():
    """The merge's fmv lookup keys on the full identity.

    Keyed on `(comic_id, grade)` alone it would treat the yearless slab row as
    a duplicate of the yeared raw row, COALESCE them into one, and DELETE the
    slab — a silent price merge across two different markets.
    """
    conn = _fresh_db()
    yeared = upsert_comic(conn, title="ASM", issue="300", year=1988)
    upsert_fmv(conn, yeared, 9.6, low=100, high=140)

    yearless = _orphan_yearless(conn, "ASM", "300")
    upsert_fmv(conn, yearless, 9.6, low=900, high=1100, certifier="cgc")

    upsert_comic(conn, title="ASM", issue="300")
    conn.commit()

    rows = conn.execute(
        "SELECT certifier, low, high FROM fmv WHERE comic_id=? AND grade=9.6 "
        "ORDER BY certifier",
        (yeared,),
    ).fetchall()
    assert [(r["certifier"], r["low"], r["high"]) for r in rows] == [
        ("cgc", 900, 1100),
        ("none", 100, 140),
    ]
    # And the orphan is gone, exactly as PER-103 requires.
    assert conn.execute(
        "SELECT COUNT(*) FROM comics WHERE year IS NULL AND title='ASM'"
    ).fetchone()[0] == 0


def test_yearless_merge_still_collapses_two_rows_at_the_same_full_key():
    conn = _fresh_db()
    yeared = upsert_comic(conn, title="ASM", issue="300", year=1988)
    yeared_fmv = upsert_fmv(conn, yeared, 9.6, low=900, certifier="cgc")

    yearless = _orphan_yearless(conn, "ASM", "300")
    yearless_fmv = upsert_fmv(
        conn, yearless, 9.6, low=800, high=1200, certifier="cgc"
    )

    upsert_comic(conn, title="ASM", issue="300")
    conn.commit()

    rows = conn.execute(
        "SELECT id, low, high FROM fmv WHERE comic_id=? AND grade=9.6", (yeared,)
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["id"] == yeared_fmv
    assert rows[0]["low"] == 900     # yeared non-NULL wins
    assert rows[0]["high"] == 1200   # yearless fills the gap
    assert conn.execute(
        "SELECT COUNT(*) FROM fmv WHERE id=?", (yearless_fmv,)
    ).fetchone()[0] == 0


def test_yearless_merge_carries_pricing_basis_with_the_price():
    conn = _fresh_db()
    yeared = upsert_comic(conn, title="ASM", issue="300", year=1988)
    yeared_fmv = upsert_fmv(conn, yeared, 9.6)
    conn.execute("UPDATE fmv SET pricing_basis=NULL WHERE id=?", (yeared_fmv,))

    yearless = _orphan_yearless(conn, "ASM", "300")
    upsert_fmv(conn, yearless, 9.6, low=100, high=140, pricing_basis="ladder")

    upsert_comic(conn, title="ASM", issue="300")
    conn.commit()

    row = conn.execute("SELECT * FROM fmv WHERE id=?", (yeared_fmv,)).fetchone()
    assert row["low"] == 100
    assert row["pricing_basis"] == "ladder"


# ---------------------------------------------------------------------------
# fmv_history
# ---------------------------------------------------------------------------


def test_fmv_history_rows_are_distinct_per_certifier():
    conn = _fresh_db()
    comic_id = upsert_comic(conn, title="ASM", issue="300", year=1988)
    raw_id = upsert_fmv(conn, comic_id, 9.6, low=100, high=140)
    slab_id = upsert_fmv(conn, comic_id, 9.6, low=900, high=1100, certifier="cgc")
    append_fmv_history(conn, raw_id)
    append_fmv_history(conn, slab_id)

    rows = conn.execute(
        "SELECT grade, certifier, label, low FROM fmv_history "
        "WHERE comic_id=? ORDER BY certifier",
        (comic_id,),
    ).fetchall()
    assert [(r["certifier"], r["label"], r["low"]) for r in rows] == [
        ("cgc", "universal", 900),
        ("none", "universal", 100),
    ]


def test_legacy_fmv_history_rows_get_the_raw_sentinel():
    conn = _legacy_db()
    _seed_legacy_rows(conn)
    conn.execute(
        "INSERT INTO fmv_history (comic_id, grade, low, high, recorded_at, source) "
        "VALUES (1, 6.0, 600, 680, '2026-08-01T00:00:00+00:00', 'backfill')"
    )
    conn.commit()

    create_tables(conn)
    conn.commit()

    row = conn.execute("SELECT * FROM fmv_history").fetchone()
    assert row["certifier"] == "none" and row["label"] == "universal"


# ---------------------------------------------------------------------------
# comps
# ---------------------------------------------------------------------------


def _comp(**over):
    base = {
        "provider": "sold-comps.com",
        "product_id": "p1",
        "pool": "raw",
        "provenance": "live",
        "title": "Amazing Spider-Man 300",
        "price": 100.0,
    }
    base.update(over)
    return base


def test_upsert_comps_carries_the_new_identity_fields():
    conn = _fresh_db()
    comic_id = upsert_comic(conn, title="ASM", issue="300", year=1988)
    upsert_comps(conn, comic_id, [
        _comp(),
        _comp(product_id="p2", pool="slab", certifier="cgc",
              label="signature_series", page_quality="ow_w"),
    ])

    rows = conn.execute(
        "SELECT product_id, certifier, label, page_quality FROM comps "
        "ORDER BY product_id"
    ).fetchall()
    assert [tuple(r) for r in rows] == [
        ("p1", "none", "universal", "unknown"),
        ("p2", "cgc", "signature_series", "ow_w"),
    ]


def test_comps_slab_backfill_reads_the_certifier_from_the_title():
    conn = _legacy_db()
    _seed_legacy_rows(conn)
    for pid, pool, title in (
        ("s1", "slab", "AMAZING SPIDER-MAN 300 CGC 9.8 White Pages"),
        ("s2", "slab", "Hulk 181 cbcs 6.5 Off-White"),
        ("s3", "slab", "ASM 300 PGX 9.4"),
        ("r1", "raw", "ASM 300 raw copy CGC-ready"),
    ):
        conn.execute(
            "INSERT INTO comps (comic_id, pool, provider, product_id, title, "
            "provenance, first_seen_at, last_seen_at) "
            "VALUES (1, ?, 'sold-comps.com', ?, ?, 'live', 'x', 'x')",
            (pool, pid, title),
        )
    conn.commit()

    create_tables(conn)
    conn.commit()

    got = {
        r["product_id"]: r["certifier"]
        for r in conn.execute("SELECT product_id, certifier FROM comps")
    }
    assert got == {"s1": "cgc", "s2": "cbcs", "s3": "other", "r1": "none"}


def test_comps_backfill_runs_once():
    conn = _legacy_db()
    _seed_legacy_rows(conn)
    conn.execute(
        "INSERT INTO comps (comic_id, pool, provider, product_id, title, "
        "provenance, first_seen_at, last_seen_at) "
        "VALUES (1, 'slab', 'sold-comps.com', 's1', 'ASM 300 PGX 9.4', "
        "'live', 'x', 'x')"
    )
    conn.commit()
    create_tables(conn)
    conn.commit()

    conn.execute("UPDATE comps SET certifier='cbcs' WHERE product_id='s1'")
    conn.commit()
    create_tables(conn)
    conn.commit()
    assert conn.execute(
        "SELECT certifier FROM comps WHERE product_id='s1'"
    ).fetchone()[0] == "cbcs"


# ---------------------------------------------------------------------------
# list_comics
# ---------------------------------------------------------------------------


def test_list_comics_returns_the_identity_columns():
    conn = _fresh_db()
    comic_id = upsert_comic(conn, title="ASM", issue="300", year=1988)
    upsert_fmv(
        conn, comic_id, 9.6, low=100, high=140,
        notes="n=5 · interpolated=grade 9.4→9.8",
    )
    rows = list_comics(conn, title="ASM", issue="300")
    assert len(rows) == 1
    row = dict(rows[0])
    assert row["certifier"] == "none"
    assert row["label"] == "universal"
    assert row["pricing_basis"] == "interpolated"


def test_list_comics_defaults_to_raw_rows_only():
    conn = _fresh_db()
    comic_id = upsert_comic(conn, title="ASM", issue="300", year=1988)
    upsert_fmv(conn, comic_id, 9.6, low=100, high=140)
    upsert_fmv(conn, comic_id, 9.6, low=900, high=1100, certifier="cgc")

    default_rows = list_comics(conn, title="ASM", issue="300")
    assert [(r["certifier"], r["fmv_low"]) for r in default_rows] == [("none", 100)]

    slab_rows = list_comics(conn, title="ASM", issue="300", certifier="cgc")
    assert [(r["certifier"], r["fmv_low"]) for r in slab_rows] == [("cgc", 900)]


def test_list_comics_still_returns_a_comic_with_no_fmv_rows():
    """The default certifier filter must not turn the LEFT JOIN into an inner one."""
    conn = _fresh_db()
    upsert_comic(conn, title="Daredevil", issue="1", year=1964)
    rows = list_comics(conn, title="Daredevil", issue="1")
    assert len(rows) == 1
    assert rows[0]["fmv_id"] is None
    assert rows[0]["certifier"] is None
