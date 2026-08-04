"""Comic-specific database functions for the gixen-overlay plugin."""
from __future__ import annotations

import logging
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Any

logger = logging.getLogger(__name__)

# BUI-656: the comps ledger's closed vocabularies (KTD3). Single-sourced here
# — `models.CompItem` imports both so its pydantic validation and this
# module's CHECK constraints can never drift apart (the cross-layer drift
# BUI-588 warns about for FMV_FLAG_REASONS, avoided here because db.py and
# models.py share one package and an import edge is available).
COMPS_POOLS = ("raw", "slab")
COMPS_PROVENANCES = ("live", "backfill-cache", "backfill-capture")
_comps_pools_sql = ", ".join(f"'{p}'" for p in COMPS_POOLS)
_comps_provenances_sql = ", ".join(f"'{p}'" for p in COMPS_PROVENANCES)

# BUI-659: the fmv_history closed vocabulary. 'upsert' marks a row appended
# by the live POST /api/comics path (api_upsert_comic); 'backfill' marks a
# row seeded once by the one-time migration from pre-existing `fmv` rows.
# Single-sourced here for the same cross-layer-drift reason as COMPS_POOLS.
FMV_HISTORY_SOURCES = ("upsert", "backfill")
_fmv_history_sources_sql = ", ".join(f"'{s}'" for s in FMV_HISTORY_SOURCES)


# ---------------------------------------------------------------------------
# Table creation (called from register_db_tables hookimpl)
# ---------------------------------------------------------------------------


def create_tables(conn: sqlite3.Connection) -> None:
    """Create comics, fmv, and bid_fmvs tables. Idempotent (IF NOT EXISTS).

    `comics.year` is nullable. Uniqueness is enforced by two partial indexes
    so a comic exists at most once per (title, issue): either yeared or
    yearless, never both at the same time after reconciliation in
    upsert_comic.
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS comics (
            id              INTEGER PRIMARY KEY,
            title           TEXT NOT NULL,
            issue           TEXT NOT NULL,
            year            INTEGER,
            variant         TEXT,
            locg_id         INTEGER,
            locg_variant_id INTEGER,
            created_at      TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS fmv (
            id          INTEGER PRIMARY KEY,
            comic_id    INTEGER NOT NULL REFERENCES comics(id) ON DELETE CASCADE,
            grade       REAL NOT NULL,
            low         REAL,
            high        REAL,
            comps       INTEGER,
            confidence  TEXT CHECK(confidence IN ('high', 'medium', 'low') OR confidence IS NULL),
            notes       TEXT,
            flag_reason TEXT,
            updated_at  TEXT,
            UNIQUE(comic_id, grade)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS bid_fmvs (
            bid_id      INTEGER NOT NULL REFERENCES bids(id) ON DELETE CASCADE,
            fmv_id      INTEGER NOT NULL REFERENCES fmv(id) ON DELETE CASCADE,
            is_primary  INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (bid_id, fmv_id)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_fmv_comic ON fmv(comic_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_bid_fmvs_bid ON bid_fmvs(bid_id)")
    # BUI-113: remember which seller-scan wish-list matches have already been
    # surfaced, so repeat scans default to showing only new ones. Standalone —
    # no FK or JOIN to comics/bids — so it's a plain additive table that needs
    # none of the Python-memory rebuild machinery the comic tables require.
    # Only matches get recorded (a handful of item_ids per scan, not every
    # listing), so the table stays small and means exactly "matches I've shown".
    conn.execute("""
        CREATE TABLE IF NOT EXISTS seller_scan_seen (
            item_id       TEXT PRIMARY KEY,
            seller        TEXT,
            first_seen_at TEXT DEFAULT (datetime('now'))
        )
    """)
    # BUI-121: remember which WON snipes have already been recorded into the
    # collection, so a second run of /comic:collection-add skips them instead of
    # re-POSTing everything and relying on the server's already-owned dedup.
    # Standalone — no FK to bids/comics — same additive pattern as seller_scan_seen.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS collection_wins_seen (
            item_id        TEXT PRIMARY KEY,
            first_seen_at  TEXT DEFAULT (datetime('now'))
        )
    """)
    # BUI-601: the rejected-writes ledger. Every 4xx/5xx on a *mutating*
    # overlay request lands here (see routes.LedgerRoute), so a write that the
    # server refused is recorded somewhere instead of vanishing into the
    # caller's stderr. BUI-593 is the motivating incident: an FMV fetch
    # succeeded, the write 422'd, and the book was stored NOWHERE — BUI-585
    # then sat blocked for days on the wrong theory because nothing had
    # persisted the refusal. Standalone (no FK, no JOIN to comics/bids) — the
    # same plain-additive pattern as seller_scan_seen / collection_wins_seen,
    # so it needs none of the Python-memory rebuild machinery the comic tables
    # require.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS rejected_writes (
            id          INTEGER PRIMARY KEY,
            created_at  TEXT NOT NULL,
            method      TEXT NOT NULL,
            path        TEXT NOT NULL,
            query       TEXT,
            status      INTEGER NOT NULL,
            detail      TEXT,
            payload     TEXT
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_rejected_writes_created "
        "ON rejected_writes(created_at)"
    )
    # BUI-602: job heartbeats. One row per job, overwritten on each successful
    # run — this is a "when did this last WORK" table, not an event log, so it
    # stays at exactly len(JOB_CONTRACTS) rows and never needs pruning. The
    # cadence contract lives in JOB_CONTRACTS below; `heartbeat_report` joins
    # the two into the watchdog verdict.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS heartbeats (
            job              TEXT PRIMARY KEY,
            last_success_at  TEXT NOT NULL,
            detail           TEXT,
            success_count    INTEGER NOT NULL DEFAULT 1
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS migration_state (
            migration TEXT PRIMARY KEY
        )
    """)
    # BUI-656: the comps ledger. Durable, identity-scoped storage for every
    # comp the pipeline treats as a comp (tier 1 — see the comps-data-flywheel
    # plan's KTD1), so a `comic-fmv` recompute no longer destroys the comps
    # behind the price it replaces (`fmv.comps` is a bare INTEGER count under
    # UNIQUE(comic_id, grade) — a recompute overwrites it with no history).
    # comic_id is nullable (KTD3) and never inferred: a backfilled response
    # whose book could not be resolved lands with comic_id NULL rather than
    # attached to a guess. ON DELETE SET NULL (not CASCADE): deleting a bad
    # comics row must not delete market facts, only orphan them.
    #
    # Uniqueness is (provider, product_id, COALESCE(comic_id, -1), pool) — the
    # COALESCE is load-bearing for the same reason idx_comics_tiyv's
    # COALESCE(variant,'') is above: SQLite treats bare NULLs as distinct in a
    # unique index, so without it every re-observation of an identity-free
    # (comic_id IS NULL) comp would insert a new duplicate row instead of
    # updating the one already on file.
    #
    # Both `pool` and `provenance` are closed vocabularies, CHECK-enforced here
    # as defense-in-depth alongside the route's pydantic validation (the same
    # belt-and-suspenders pattern `fmv.confidence`'s CHECK already applies) —
    # `models.CompItem` validates the identical two vocabularies against these
    # same constants, imported from here so the two enforcement points can
    # never drift apart.
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS comps (
            id             INTEGER PRIMARY KEY,
            comic_id       INTEGER REFERENCES comics(id) ON DELETE SET NULL,
            pool           TEXT NOT NULL CHECK(pool IN ({_comps_pools_sql})),
            provider       TEXT NOT NULL,
            product_id     TEXT NOT NULL,
            title          TEXT,
            price          REAL,
            sold_date      TEXT,
            grade          REAL,
            buying_format  TEXT,
            link           TEXT,
            query          TEXT,
            tier           TEXT,
            from_cache     INTEGER,
            observed_at    TEXT,
            provenance     TEXT NOT NULL CHECK(provenance IN ({_comps_provenances_sql})),
            first_seen_at  TEXT NOT NULL,
            last_seen_at   TEXT NOT NULL,
            seen_count     INTEGER NOT NULL DEFAULT 1,
            conflict_count INTEGER NOT NULL DEFAULT 0
        )
    """)
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_comps_identity "
        "ON comps(provider, product_id, COALESCE(comic_id, -1), pool)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_comps_comic ON comps(comic_id)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_comps_observed ON comps(observed_at)"
    )
    _migrate_fmv_split(conn)
    _migrate_year_nullable(conn)
    _migrate_sweep_allcaps_orphans(conn)
    # variant column must exist before the unique-index migration references it.
    _migrate_add_variant_column(conn)
    # flag_reason column must be added AFTER the fmv-split / year-nullable rebuilds
    # above (those recreate `fmv` from the pre-BUI-132 schema), so it survives them.
    _migrate_add_fmv_flag_reason_column(conn)
    _migrate_lowercase_title_indexes(conn)
    # Partial unique indexes go AFTER migrations so the legacy duplicate-row
    # cleanup (fmv-split collapses (title, issue, year, grade) duplicates into
    # one comic) has run before we try to enforce uniqueness on the cleaned set.
    # LOWER(title) expression indexes enforce uniqueness case-insensitively so
    # direct SQL writes also can't create case-variant duplicates.
    # BUI-28: variant is part of the identity, so a base cover and its Newsstand
    # (etc.) variant are distinct rows. COALESCE(variant,'') folds NULL→'' so two
    # base rows still collide (SQLite treats bare NULLs as distinct in indexes).
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_comics_tiyv "
        "ON comics(LOWER(title), issue, year, COALESCE(variant,'')) WHERE year IS NOT NULL"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_comics_tiv_nullyear "
        "ON comics(LOWER(title), issue, COALESCE(variant,'')) WHERE year IS NULL"
    )
    # Drop the pre-variant indexes (they'd wrongly reject a second variant row).
    conn.execute("DROP INDEX IF EXISTS idx_comics_tiy")
    conn.execute("DROP INDEX IF EXISTS idx_comics_ti_nullyear")
    # BUI-659: fmv_history — an append-only ledger of every fmv snapshot,
    # keyed by comic_id (not comic_id+grade), so a book's price at grade 9.4
    # today doesn't erase what it was worth last month. `fmv` itself stays a
    # bare UNIQUE(comic_id, grade) upsert target (unchanged) — this table is
    # purely additive alongside it.
    #
    # Created HERE — after _migrate_fmv_split/_migrate_year_nullable, not up
    # in the initial DDL block above — on purpose: both of those migrations
    # `ALTER TABLE comics RENAME TO comics_old` on a legacy DB, and SQLite
    # 3.26+ rewrites any FK in another table that pointed at `comics` to
    # follow the rename. A `fmv_history` created earlier would have its
    # `REFERENCES comics(id)` silently rewritten to `REFERENCES
    # comics_old(id)`, then left dangling once `comics_old` is dropped —
    # exactly the documented failure in
    # docs/solutions/database-issues/sqlite-fk-rename-savepoint-pragma-2026-05-19.md.
    # `fmv`/`bid_fmvs` avoid this by being dropped and rebuilt AROUND the
    # rename inside those migrations; `fmv_history` avoids it more simply by
    # not existing yet when the rename happens — comics is in its final,
    # stable shape (fresh or migrated) by this point in create_tables.
    #
    # comic_id is NOT NULL with ON DELETE CASCADE (unlike comps' nullable,
    # ON DELETE SET NULL comic_id): a comps row is a market fact about a
    # listing that outlives any one comics identity guess, but an fmv_history
    # row is *about* the comics row itself — deleting the book deletes its
    # price history with it, same as `fmv` already does.
    #
    # `source` is a closed vocabulary (defense-in-depth CHECK, mirroring
    # comps' pool/provenance pattern) — 'upsert' for a live append from
    # api_upsert_comic, 'backfill' for the one-time seeding migration below.
    # There is deliberately no UPDATE or DELETE statement anywhere in this
    # module that targets fmv_history — every row, once written, is
    # permanent for the life of its comic_id.
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS fmv_history (
            id          INTEGER PRIMARY KEY,
            comic_id    INTEGER NOT NULL REFERENCES comics(id) ON DELETE CASCADE,
            grade       REAL NOT NULL,
            low         REAL,
            high        REAL,
            comps       INTEGER,
            confidence  TEXT,
            flag_reason TEXT,
            notes       TEXT,
            recorded_at TEXT,
            source      TEXT NOT NULL CHECK(source IN ({_fmv_history_sources_sql}))
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_fmv_history_comic ON fmv_history(comic_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_fmv_history_recorded "
        "ON fmv_history(recorded_at)"
    )
    # Seed fmv_history from whatever `fmv` rows already exist. Also runs LAST
    # for the same reason the table creation just above does — `fmv` is in
    # its final, stable shape here on both a fresh DB (the legacy migrations
    # above are no-ops) and an existing one (they've already run).
    _migrate_seed_fmv_history(conn)


# ---------------------------------------------------------------------------
# Migration crash-guard helpers
# ---------------------------------------------------------------------------


def _assert_no_migration_marker(conn: sqlite3.Connection, name: str) -> None:
    """Raise RuntimeError if a crash marker for `name` is present in migration_state.

    Called before each migration's gate so a crash in the post-DROP window
    (where the schema looks already-migrated and the gate would return early)
    still surfaces instead of silently leaving fmv/bid_fmvs empty.
    """
    row = conn.execute(
        "SELECT 1 FROM migration_state WHERE migration=?", (name,)
    ).fetchone()
    if row is not None:
        raise RuntimeError(
            f"DB in crashed mid-migration state: '{name}' marker present — "
            "restore from pre-migration snapshot before restarting"
        )


def _set_migration_marker(conn: sqlite3.Connection, name: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO migration_state (migration) VALUES (?)", (name,)
    )


def _clear_migration_marker(conn: sqlite3.Connection, name: str) -> None:
    conn.execute("DELETE FROM migration_state WHERE migration=?", (name,))


def _migrate_add_variant_column(conn: sqlite3.Connection) -> None:
    """Add the nullable `variant` column to comics if absent (BUI-28).

    Additive and idempotent. Existing rows keep variant=NULL (treated as the
    base edition); variants split off only on the next encounter — no bulk
    backfill of historically conflated rows.
    """
    cols = {row[1] for row in conn.execute("PRAGMA table_info(comics)")}
    if "variant" not in cols:
        conn.execute("ALTER TABLE comics ADD COLUMN variant TEXT")


def _migrate_add_fmv_flag_reason_column(conn: sqlite3.Connection) -> None:
    """Add the nullable `flag_reason` column to fmv if absent (BUI-132).

    Promotes the BUI-86 needs_manual state from an in-process field + an
    `fmv_notes` `manual_review=<reason>` token to a first-class DB column, so
    `/comic:verify` can emit a distinct `needs_manual` verdict and the upsert can
    clear a now-stale price on a newly-flagged book (without weakening the n=0
    stub guard — see upsert_fmv).

    Additive and idempotent, mirroring _migrate_add_variant_column. A NULL
    flag_reason means "not flagged" (priced or a plain n=0 stub); a non-NULL
    value is one of one_sided / too_wide / too_sparse. Version-skew tolerant: a
    DB written by older overlay code simply lacks the column until this runs.
    """
    cols = {row[1] for row in conn.execute("PRAGMA table_info(fmv)")}
    if "flag_reason" not in cols:
        conn.execute("ALTER TABLE fmv ADD COLUMN flag_reason TEXT")


# ---------------------------------------------------------------------------
# One-time data migration: collapse legacy comics table into comics+fmv+bid_fmvs
# ---------------------------------------------------------------------------


def _migrate_fmv_split(conn: sqlite3.Connection) -> None:
    """Idempotent migration from the legacy monolithic comics schema.

    Gate: if comics.grade column is absent (already migrated or fresh DB),
    return immediately — with one exception: if comics_old also exists, that
    indicates a crash mid-rebuild and we raise to prevent silent data loss.

    IMPORTANT: Uses raw conn.execute() SQL only. Never calls CRUD helpers
    (upsert_fmv, upsert_comic, link_fmv_to_bid) — those call conn.commit()
    which would destroy the host's SAVEPOINT and make rollback impossible.
    """
    # Check marker before the gate: a crash after DROP TABLE comics_old leaves
    # the schema looking already-migrated (no grade col, no comics_old), so the
    # gate would return early and hide the incomplete fmv/bid_fmvs restore.
    _assert_no_migration_marker(conn, "fmv_split")

    cols = {row[1] for row in conn.execute("PRAGMA table_info(comics)")}
    if "grade" not in cols:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        if "comics_old" in tables:
            raise RuntimeError(
                "DB in crashed mid-migration state: comics_old exists but comics "
                "has no grade column — manual recovery required before server start"
            )
        return

    logger.info("fmv-split migration: starting")

    # Step 1: Select survivor comics per (title, issue, year) group.
    # Priority: locg_id NOT NULL > fmv_low NOT NULL > newest fmv_updated_at > lowest id.
    survivors = conn.execute("""
        SELECT c.id, c.title, c.issue, c.year, c.locg_id, c.locg_variant_id, c.created_at
        FROM comics c
        INNER JOIN (
            SELECT
                title, issue, year,
                COALESCE(
                    MIN(CASE WHEN locg_id IS NOT NULL THEN id END),
                    MIN(CASE WHEN fmv_low IS NOT NULL THEN id END),
                    MIN(CASE WHEN fmv_updated_at IS NOT NULL THEN id END),
                    MIN(id)
                ) AS survivor_id
            FROM comics
            GROUP BY title, issue, year
        ) grp ON c.id = grp.survivor_id
    """).fetchall()

    survivor_ids = [r["id"] for r in survivors]
    # Build O(n) mapping from legacy comic_id to survivor_id for the same (title, issue, year)
    survivor_key_map = {(s["title"], s["issue"], s["year"]): s["id"] for s in survivors}
    id_to_survivor: dict[int, int] = {s["id"]: s["id"] for s in survivors}

    all_comics = conn.execute(
        "SELECT id, title, issue, year FROM comics"
    ).fetchall()
    for c in all_comics:
        if c["id"] not in id_to_survivor:
            id_to_survivor[c["id"]] = survivor_key_map[(c["title"], c["issue"], c["year"])]

    # Step 2: Manufacture fmv rows for each legacy (survivor_id, grade) pair.
    legacy_rows = conn.execute(
        "SELECT id, grade, fmv_low, fmv_high, fmv_comps, fmv_confidence, fmv_notes "
        "FROM comics WHERE grade IS NOT NULL"
    ).fetchall()

    fmv_inserted = 0
    # (survivor_id, grade) -> fmv_id lookup built as we insert
    fmv_lookup: dict[tuple[int, float], int] = {}

    for row in legacy_rows:
        survivor_id = id_to_survivor.get(row["id"], row["id"])
        grade = row["grade"]
        key = (survivor_id, grade)

        # Check if fmv row already exists (shouldn't on first run, but be safe)
        existing = conn.execute(
            "SELECT id, low FROM fmv WHERE comic_id=? AND grade=?",
            (survivor_id, grade),
        ).fetchone()

        if existing is None:
            now = datetime.now(timezone.utc).isoformat()
            cur = conn.execute(
                """
                INSERT INTO fmv (comic_id, grade, low, high, comps, confidence, notes, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (survivor_id, grade, row["fmv_low"], row["fmv_high"],
                 row["fmv_comps"], row["fmv_confidence"], row["fmv_notes"],
                 now if row["fmv_low"] is not None else None),
            )
            fmv_lookup[key] = cur.lastrowid  # type: ignore[assignment]  # INSERT always yields int lastrowid
            fmv_inserted += 1
        else:
            fmv_id = existing["id"]
            if existing["low"] is None and row["fmv_low"] is not None:
                # Losing row has FMV data; take it and merge notes
                existing_notes = conn.execute(
                    "SELECT notes FROM fmv WHERE id=?", (fmv_id,)
                ).fetchone()["notes"]
                merged_notes = (
                    f"[merged from legacy comic_id={row['id']}] "
                    + (existing_notes or "")
                ).strip()
                conn.execute(
                    """
                    UPDATE fmv SET low=?, high=?, comps=?, confidence=?, notes=?
                    WHERE id=?
                    """,
                    (row["fmv_low"], row["fmv_high"], row["fmv_comps"],
                     row["fmv_confidence"], merged_notes, fmv_id),
                )
            fmv_lookup[key] = fmv_id

    # Step 3: Repoint bids.fmv_id from comic_id+grade to fmv_id.
    bids_linked = 0
    bid_rows = conn.execute(
        "SELECT b.id AS bid_id, b.comic_id, c.grade "
        "FROM bids b "
        "JOIN comics c ON c.id = b.comic_id "
        "WHERE b.comic_id IS NOT NULL"
    ).fetchall()

    for b in bid_rows:
        survivor_id = id_to_survivor.get(b["comic_id"], b["comic_id"])
        grade = b["grade"]
        if grade is None:
            continue
        fmv_id = fmv_lookup.get((survivor_id, grade))
        if fmv_id is not None:
            conn.execute("UPDATE bids SET fmv_id=? WHERE id=?", (fmv_id, b["bid_id"]))
            bids_linked += 1

    # Step 4: Migrate bid_comics -> bid_fmvs (only if bid_comics exists).
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    junction_inserted = 0
    junction_skipped = 0

    if "bid_comics" in tables:
        bc_rows = conn.execute(
            "SELECT bc.bid_id, bc.comic_id, bc.is_primary, c.grade "
            "FROM bid_comics bc "
            "JOIN comics c ON c.id = bc.comic_id"
        ).fetchall()

        for bc in bc_rows:
            survivor_id = id_to_survivor.get(bc["comic_id"], bc["comic_id"])
            grade = bc["grade"]
            if grade is None:
                junction_skipped += 1
                continue
            fmv_id = fmv_lookup.get((survivor_id, grade))
            if fmv_id is None:
                junction_skipped += 1
                continue
            cur = conn.execute(
                "INSERT OR IGNORE INTO bid_fmvs (bid_id, fmv_id, is_primary) VALUES (?, ?, ?)",
                (bc["bid_id"], fmv_id, bc["is_primary"]),
            )
            junction_inserted += cur.rowcount

        # Step 5: Drop bid_comics (removes FK blocking non-survivor delete).
        conn.execute("DROP TABLE bid_comics")

    # Step 6: Delete non-survivor comics rows.
    if any(sid is None for sid in survivor_ids):
        raise RuntimeError(
            "survivor_ids contains None — sqlite3.Row row_factory misconfiguration"
        )
    if survivor_ids:
        placeholders = ",".join("?" * len(survivor_ids))
        conn.execute(
            f"DELETE FROM comics WHERE id NOT IN ({placeholders})", survivor_ids
        )

    # Step 7: Rebuild comics table via Python-memory approach.
    # SQLite 3.26+ updates FK references on RENAME, so DROP TABLE comics_old
    # would fail if fmv rows exist (FK follows the rename). Solution: save
    # fmv and bid_fmvs to Python memory, drop them, rebuild comics, restore.
    # Write crash marker before first DROP so a crash mid-restore is detectable
    # on next startup (the gate would otherwise return early on the clean schema).
    _set_migration_marker(conn, "fmv_split")
    saved_fmv = conn.execute(
        "SELECT id, comic_id, grade, low, high, comps, confidence, notes, updated_at FROM fmv"
    ).fetchall()
    saved_bid_fmvs = conn.execute(
        "SELECT bid_id, fmv_id, is_primary FROM bid_fmvs"
    ).fetchall()

    conn.execute("DROP TABLE bid_fmvs")
    conn.execute("DROP TABLE fmv")
    conn.execute("ALTER TABLE comics RENAME TO comics_old")
    conn.execute("""
        CREATE TABLE comics (
            id              INTEGER PRIMARY KEY,
            title           TEXT NOT NULL,
            issue           TEXT NOT NULL,
            year            INTEGER NOT NULL,
            locg_id         INTEGER,
            locg_variant_id INTEGER,
            created_at      TEXT DEFAULT (datetime('now')),
            UNIQUE(title, issue, year)
        )
    """)
    conn.execute("""
        INSERT INTO comics (id, title, issue, year, locg_id, locg_variant_id, created_at)
        SELECT id, title, issue, year, locg_id, locg_variant_id, created_at
        FROM comics_old
    """)
    conn.execute("DROP TABLE comics_old")

    # Recreate fmv and bid_fmvs with full FK constraints.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS fmv (
            id          INTEGER PRIMARY KEY,
            comic_id    INTEGER NOT NULL REFERENCES comics(id) ON DELETE CASCADE,
            grade       REAL NOT NULL,
            low         REAL,
            high        REAL,
            comps       INTEGER,
            confidence  TEXT CHECK(confidence IN ('high', 'medium', 'low') OR confidence IS NULL),
            notes       TEXT,
            updated_at  TEXT,
            UNIQUE(comic_id, grade)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS bid_fmvs (
            bid_id      INTEGER NOT NULL REFERENCES bids(id) ON DELETE CASCADE,
            fmv_id      INTEGER NOT NULL REFERENCES fmv(id) ON DELETE CASCADE,
            is_primary  INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (bid_id, fmv_id)
        )
    """)

    # Restore fmv rows preserving original ids.
    for f in saved_fmv:
        conn.execute(
            """
            INSERT INTO fmv (id, comic_id, grade, low, high, comps, confidence, notes, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (f["id"], f["comic_id"], f["grade"], f["low"], f["high"],
             f["comps"], f["confidence"], f["notes"], f["updated_at"]),
        )

    # Restore bid_fmvs rows.
    for bf in saved_bid_fmvs:
        conn.execute(
            "INSERT OR IGNORE INTO bid_fmvs (bid_id, fmv_id, is_primary) VALUES (?, ?, ?)",
            (bf["bid_id"], bf["fmv_id"], bf["is_primary"]),
        )

    conn.execute("CREATE INDEX IF NOT EXISTS idx_fmv_comic ON fmv(comic_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_bid_fmvs_bid ON bid_fmvs(bid_id)")

    _clear_migration_marker(conn, "fmv_split")

    logger.info(
        "fmv-split migration complete: survivors=%d fmv_inserted=%d "
        "bids_linked=%d junction_inserted=%d junction_skipped=%d",
        len(survivor_ids), fmv_inserted, bids_linked, junction_inserted, junction_skipped,
    )


# ---------------------------------------------------------------------------
# One-time migration: drop comics.year NOT NULL (PER-98)
# ---------------------------------------------------------------------------


def _migrate_year_nullable(conn: sqlite3.Connection) -> None:
    """Make comics.year nullable + swap the UNIQUE(title, issue, year) constraint
    for two partial unique indexes (see create_tables).

    Gate: if comics.year is already nullable, return immediately. Detected via
    PRAGMA table_info — when notnull=0 on the year column.

    IMPORTANT: Uses raw conn.execute() SQL only. Never calls CRUD helpers
    (upsert_fmv, upsert_comic, link_fmv_to_bid) — those call conn.commit()
    which would destroy the host's SAVEPOINT and make rollback impossible.

    Pattern: Python-memory rebuild (per docs/solutions/database-issues/
    sqlite-fk-rename-savepoint-pragma-2026-05-19.md). SQLite 3.26+ rewrites
    FK references on RENAME, so fmv rows would block DROP TABLE comics_old.
    Save FK children to Python memory first, drop them, rebuild, restore.
    """
    # Check marker before the gate: a crash after DROP TABLE comics_old leaves
    # year already nullable, so the gate would return early and hide the
    # incomplete fmv/bid_fmvs restore.
    _assert_no_migration_marker(conn, "year_nullable")

    year_col = next(
        (row for row in conn.execute("PRAGMA table_info(comics)") if row[1] == "year"),
        None,
    )
    if year_col is None:
        # No comics table yet — create_tables made it from scratch with the
        # nullable schema. Nothing to migrate.
        return
    if year_col[3] == 0:
        # year is already nullable. Migration done (or fresh install). Bail.
        return

    logger.info("year-nullable migration: starting")

    # Only carry rows that survive a JOIN against the live parents. CASCADE
    # deletes can be bypassed by sqlite3 CLI sessions that didn't opt into
    # PRAGMA foreign_keys=ON (default OFF), leaving orphan junction rows that
    # would fail FK enforcement when re-inserted.
    saved_fmv = conn.execute(
        """
        SELECT f.id, f.comic_id, f.grade, f.low, f.high, f.comps, f.confidence, f.notes, f.updated_at
        FROM fmv f
        JOIN comics c ON c.id = f.comic_id
        """
    ).fetchall()
    saved_bid_fmvs = conn.execute(
        """
        SELECT bf.bid_id, bf.fmv_id, bf.is_primary
        FROM bid_fmvs bf
        JOIN fmv f ON f.id = bf.fmv_id
        JOIN bids b ON b.id = bf.bid_id
        """
    ).fetchall()
    # bids.fmv_id is declared REFERENCES fmv(id) ON DELETE SET NULL. Dropping
    # the fmv table fires that cascade and nulls every bid's primary fmv link.
    # Save current values so we can restore the column after the rebuild.
    saved_bid_fmv_id = conn.execute(
        "SELECT b.id, b.fmv_id FROM bids b "
        "JOIN fmv f ON f.id = b.fmv_id WHERE b.fmv_id IS NOT NULL"
    ).fetchall()

    # Write crash marker before first DROP so a crash mid-restore is detectable
    # on next startup (the gate would otherwise return early on the nullable schema).
    _set_migration_marker(conn, "year_nullable")

    conn.execute("DROP TABLE bid_fmvs")
    conn.execute("DROP TABLE fmv")
    conn.execute("ALTER TABLE comics RENAME TO comics_old")
    conn.execute("""
        CREATE TABLE comics (
            id              INTEGER PRIMARY KEY,
            title           TEXT NOT NULL,
            issue           TEXT NOT NULL,
            year            INTEGER,
            locg_id         INTEGER,
            locg_variant_id INTEGER,
            created_at      TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.execute("""
        INSERT INTO comics (id, title, issue, year, locg_id, locg_variant_id, created_at)
        SELECT id, title, issue, year, locg_id, locg_variant_id, created_at
        FROM comics_old
    """)
    conn.execute("DROP TABLE comics_old")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_comics_tiy "
        "ON comics(title, issue, year) WHERE year IS NOT NULL"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_comics_ti_nullyear "
        "ON comics(title, issue) WHERE year IS NULL"
    )

    # Recreate FK children with full constraints, restore from memory.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS fmv (
            id          INTEGER PRIMARY KEY,
            comic_id    INTEGER NOT NULL REFERENCES comics(id) ON DELETE CASCADE,
            grade       REAL NOT NULL,
            low         REAL,
            high        REAL,
            comps       INTEGER,
            confidence  TEXT CHECK(confidence IN ('high', 'medium', 'low') OR confidence IS NULL),
            notes       TEXT,
            updated_at  TEXT,
            UNIQUE(comic_id, grade)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS bid_fmvs (
            bid_id      INTEGER NOT NULL REFERENCES bids(id) ON DELETE CASCADE,
            fmv_id      INTEGER NOT NULL REFERENCES fmv(id) ON DELETE CASCADE,
            is_primary  INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (bid_id, fmv_id)
        )
    """)
    for f in saved_fmv:
        conn.execute(
            """
            INSERT INTO fmv (id, comic_id, grade, low, high, comps, confidence, notes, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (f["id"], f["comic_id"], f["grade"], f["low"], f["high"],
             f["comps"], f["confidence"], f["notes"], f["updated_at"]),
        )
    for bf in saved_bid_fmvs:
        conn.execute(
            "INSERT OR IGNORE INTO bid_fmvs (bid_id, fmv_id, is_primary) VALUES (?, ?, ?)",
            (bf["bid_id"], bf["fmv_id"], bf["is_primary"]),
        )
    # Restore bids.fmv_id values that the SET NULL cascade wiped when fmv dropped.
    for b in saved_bid_fmv_id:
        conn.execute("UPDATE bids SET fmv_id = ? WHERE id = ?", (b["fmv_id"], b["id"]))

    conn.execute("CREATE INDEX IF NOT EXISTS idx_fmv_comic ON fmv(comic_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_bid_fmvs_bid ON bid_fmvs(bid_id)")

    _clear_migration_marker(conn, "year_nullable")

    logger.info(
        "year-nullable migration complete: %d fmv, %d bid_fmvs, %d bids.fmv_id restored",
        len(saved_fmv), len(saved_bid_fmvs), len(saved_bid_fmv_id),
    )


# ---------------------------------------------------------------------------
# One-time data migration: sweep ALL-CAPS yearless orphans created pre-PER-123
# ---------------------------------------------------------------------------


def _find_yearless_orphans(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Yearless comics rows that have a yeared sibling (same title/issue).

    Shared SELECT for the one-time ALL-CAPS migration and the on-demand
    orphan sweep; both merge these rows into their yeared sibling and delete
    the yearless stub, but differ in dry-run/commit handling.
    """
    return conn.execute(
        """
        SELECT
            c.id   AS yearless_id,
            c.title,
            c.issue,
            (SELECT id FROM comics
             WHERE LOWER(title)=LOWER(c.title) AND issue=c.issue AND year IS NOT NULL
             ORDER BY (locg_id IS NULL), id LIMIT 1) AS yeared_id
        FROM comics c
        WHERE c.year IS NULL
          AND EXISTS (
              SELECT 1 FROM comics
              WHERE LOWER(title)=LOWER(c.title) AND issue=c.issue AND year IS NOT NULL
          )
        """
    ).fetchall()


def _migrate_sweep_allcaps_orphans(conn: sqlite3.Connection) -> None:
    """Merge ALL-CAPS yearless orphan comics into their yeared siblings exactly once.

    Cleans up stubs created before PER-123 added case-insensitive title matching.
    Gate: migration_state row 'sweep_allcaps_orphans' present → already ran.

    IMPORTANT: Uses raw conn.execute() only — no conn.commit(). Called from
    create_tables() which runs inside the host's per-plugin SAVEPOINT; calling
    conn.commit() here would destroy it (same constraint as _migrate_fmv_split
    and _migrate_year_nullable).
    """
    row = conn.execute(
        "SELECT 1 FROM migration_state WHERE migration='sweep_allcaps_orphans'"
    ).fetchone()
    if row is not None:
        return

    orphans = _find_yearless_orphans(conn)

    for orphan in orphans:
        _merge_yearless_into_yeared(conn, orphan["yearless_id"], orphan["yeared_id"])
        conn.execute("DELETE FROM comics WHERE id=?", (orphan["yearless_id"],))

    if orphans:
        logger.info(
            "_migrate_sweep_allcaps_orphans: merged %d orphan(s): %s",
            len(orphans),
            [(r["title"], r["issue"]) for r in orphans],
        )
    _set_migration_marker(conn, "sweep_allcaps_orphans")


# ---------------------------------------------------------------------------
# One-time schema migration: LOWER(title) expression unique indexes (PER-120)
# ---------------------------------------------------------------------------


def _migrate_lowercase_title_indexes(conn: sqlite3.Connection) -> None:
    """Replace case-sensitive partial unique indexes with LOWER(title), variant-aware ones.

    Old: ON comics(title, issue, year) / ON comics(title, issue)
    New: ON comics(LOWER(title), issue, year, COALESCE(variant,'')) /
         ON comics(LOWER(title), issue, COALESCE(variant,''))   (BUI-28)

    Gate: migration_state row 'lowercase_title_indexes' present → already ran.
    (DBs that ran the pre-variant version of this migration are upgraded to the
    variant-aware indexes by the unconditional create/drop in create_tables.)

    IMPORTANT: Uses raw conn.execute() only — no conn.commit(). Called from
    create_tables() which runs inside the host's per-plugin SAVEPOINT. The index
    creation here happens in create_tables' autocommit window (before any
    migration marker INSERT opens a transaction) so the indexes survive a caller
    rollback.
    """
    row = conn.execute(
        "SELECT 1 FROM migration_state WHERE migration='lowercase_title_indexes'"
    ).fetchone()
    if row is not None:
        return

    conn.execute("DROP INDEX IF EXISTS idx_comics_tiy")
    conn.execute("DROP INDEX IF EXISTS idx_comics_ti_nullyear")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_comics_tiyv "
        "ON comics(LOWER(title), issue, year, COALESCE(variant,'')) WHERE year IS NOT NULL"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_comics_tiv_nullyear "
        "ON comics(LOWER(title), issue, COALESCE(variant,'')) WHERE year IS NULL"
    )
    _set_migration_marker(conn, "lowercase_title_indexes")


# ---------------------------------------------------------------------------
# Comic CRUD (identity-only)
# ---------------------------------------------------------------------------


def _strip_embedded_issue(title: str, issue: str) -> str:
    """Strip an embedded ``#<issue>`` (or a bare trailing issue token) from
    *title* when it duplicates the separate `issue` field.

    Server-side twin of `_strip_embedded_issue` in
    `apps/fmv/src/fmv_runner.py` (BUI-346/BUI-591) — kept duplicated rather
    than shared, same rationale as fmv_runner's own duplication note:
    apps/fmv is not a workspace member and does not import this plugin. The
    `(?<!\\d)` guard on the trailing-token strip prevents chewing into an
    unrelated longer number (e.g. issue="99" must not touch the "2099" in
    "X-Men 2099")."""
    issue_str = str(issue).strip() if issue else ""
    if not title or not issue_str:
        return title
    cleaned = re.sub(rf'#\s*{re.escape(issue_str)}\b', '', title, flags=re.IGNORECASE)
    cleaned = re.sub(rf'(?<!\d){re.escape(issue_str)}\s*$', '', cleaned.strip())
    return re.sub(r'\s+', ' ', cleaned).strip()


# --- BUI-599: signals that make the class-B truncation below DECLINE. -------
#
# Every one of these is matched ONLY against the text that follows a confirmed
# `#<own issue>` token — never against the series name that precedes it. That
# anchoring is what makes a vocabulary test safe here: BUI-596's rule.md warns
# that a whole-title vocabulary scan false-positives on genuine series names
# (its scan flags `DC Comics Presents` purely for containing "DC Comics"), and
# no series name can reach these patterns, because the series name is exactly
# the part that is kept and never examined.

# A multi-issue lot: `18,19,20,21,22 ...` or `... lot of 5 ...` (class D).
#
# BUI-625 widened both tests from "the text after a `#<issue>` token" to the
# WHOLE title, because the `#` is not part of the shape. The same lot listings
# appear hashless in `bids.ebay_title` on the live Mac Mini
# (`"Uncanny X-men  5,6,7,8,9  Bronze age lot of 5 Fine to VF"`), and a
# hashless lot tells exactly the same lie as a hashed one.
#
# Measured before widening (live DB, 2026-08-03, read-only): across the 666
# `comics` rows BOTH tests match ZERO rows, so no legitimate stored identity is
# affected; across the 641 raw listing titles in `bids.ebay_title` the pair
# matches 7, and all 7 are genuine multi-issue lots (`Full Run`,
# `1,2,3 Limited Series`, `Akira 1,2,3,4,5,6,7,8,9`). Precision 7/7, and 3 of
# those 7 are reachable only by the run test — `lot of N` alone misses a
# "Full Run" listing.
#
# The run test is keyed on the row's OWN issue number followed by a separator
# and another digit, so it inherits the same "the caller's own `issue` is
# duplicated into the title" signal the rest of this module trusts rather than
# scanning for any comma-digit anywhere. `(?<!\d)` is BUI-591's guard, for the
# same reason it exists there: issue="99" must not match inside "2099".
# `-` IS DELIBERATELY NOT A SEPARATOR HERE, and the reason is counter-intuitive
# enough to be worth freezing: a dash range (`#18-22`) really is a lot shape,
# and `apps/ebay/src/comic_identity.py`'s `_LOT_RE` — the older, incident-driven
# lot detector for this same input class (BUI-135/221/226-245/261) — does match
# it. Measuring the widening against `comics` alone says it is free: adding `-`
# flags 0 additional rows of 666, and 0 additional rows of BUI-596's frozen 173.
# Measuring it against the INPUT corpus says the opposite. `bids.ebay_title`
# holds ~100 single-issue listings in one seller's house format —
# `"X-Men   96 - 1st Moira MacTaggert VG/Fine Cond"` — where the issue is
# followed by a dash and the digit of a "1st appearance" note. Every one of
# those would be refused as a lot. The `comics` table looked safe only because
# its rows are already-normalized series names with no tail left to match.
#
# So the separator set stays punctuation that enumerates rather than separates
# prose. `apps/ebay` is not a workspace member (same barrier documented on
# `_strip_embedded_issue` above), so `_LOT_RE` cannot be imported; the dash
# range is a known, accepted gap rather than an oversight.
_LOT_SEPARATORS = r'[,/&+]'
_LOT_PHRASE_RE = re.compile(r'\blot\s+of\s+\d', re.IGNORECASE)

# An edition designation that belongs in the `variant` COLUMN (class C). The
# printing/facsimile members are not present in BUI-596's measured 60 class-B
# rows — they are here because a printing marker is a documented data-loss
# class in this repo (BUI-364/372/373), and the cost of listing one that never
# fires is zero while the cost of omitting one is a silent merge.
#
# THREE RELATED VOCABULARIES EXIST. None is reusable here, but a spelling added
# to one is usually worth adding to the others, and BUI-373 exists precisely
# because two printing-marker lists drifted and silently missed "2nd Ptg":
#
#   - `locg.commands._PRINTING_MARKER_RE` (packages/locg-cli) — the ONE
#     printing-marker detector for that package, and ordinal-aware where this
#     list is not. Importable (locg is a workspace dep) but scoped to LOCG
#     collection names, not eBay listing tails.
#   - `title_parser._EDITION_TAGS` (same package, next door) — deliberately NOT
#     reused: it is a listing-NOISE vocabulary for cleaning a series name, so
#     it also matches `wow`, `gem`, `beauty`, `rare` and every grade token.
#     Feeding it to the extractor below would make `Gem` an edition designation.
#   - `comic_identity.py` (apps/ebay) — freeform-title parsing, not importable.
_EDITION_WORDS = (
    r'variants?', r'virgin', r'newsstand', r'foil', r'incentive', r'cover',
    r'facsimile', r'reprint', r'printing', r'ptg',
)
_EDITION_WORDS_ALT = '|'.join(_EDITION_WORDS)
_VARIANT_DESIGNATION_RE = re.compile(
    rf'\b(?:{_EDITION_WORDS_ALT}|1:\d+)\b', re.IGNORECASE
)

# --- BUI-625: the class-C EXTRACTION rule. ---------------------------------
#
# `_VARIANT_DESIGNATION_RE` above answers "is there an edition designation in
# this tail?" — which is all a DECLINE needs. Moving the designation into the
# `variant` column needs a strictly harder answer: "where does the designation
# start and end?" On BUI-596's measured corpus that second question is only
# reliably answerable for one shape — a tail that is NOTHING BUT the
# designation, ending in the designation word:
#
#     "Absolute Flash #10 Nick Robles Cover"   -> variant "Nick Robles Cover"
#
# That is exactly the 8 rows rows.tsv classifies `C-variant-designation`. The
# other 21 tails that reach the class-C decline are class-B listing titles with
# a designation word buried mid-prose, and their span is NOT decidable:
#
#   - `"... McFarlane Newsstand X-men Spider-man FVF Beauty Wow"` — is the
#     designation `Newsstand` or `McFarlane Newsstand`? McFarlane is the
#     interior artist, a selling point, not part of the edition.
#   - `"WORLD'S FINEST # 200 - (VG+) -SUPERMAN/ROBIN-NEAL ADAMS COVER-..."` —
#     a 1971 book with exactly ONE cover. `NEAL ADAMS COVER` is a cover-artist
#     CREDIT, not a variant; extracting it would mint a phantom variant beside
#     the real base edition.
#   - `"Iron Man #125 Newsstand Variant (Marvel Comics August 1979) ..."` —
#     would yield `Newsstand Variant`, while the live table already stores that
#     designation as `Newsstand` (comics id 578 is `Iron Man` #124 1979
#     `Newsstand`). Same book, two spellings, two rows.
#
# So the rule below is SYNTACTIC, not a vocabulary judgement, and every clause
# exists to keep it on the safe side of the BUI-28 identity asymmetry:
#
#   * merging two distinct books into one row is UNRECOVERABLE — the row no
#     longer records which book it was;
#   * minting an extra variant row is recoverable — the base row is untouched
#     and still reachable.
#
# Therefore the extracted value is taken VERBATIM and never canonicalized.
# Canonicalizing (folding `Kirkham virgin variant` -> `Kirkham variant`, say)
# is the one transformation that could merge, and BUI-596's corpus holds that
# exact pair at the same (series, issue): rows 444 and 460 are
# `Amazing Spider-man` #25 `Rare Kirkham virgin variant` and
# `Rare Kirkham variant` — two genuinely different books one token apart.

# One name token: letters, with internal punctuation a person's or masthead's
# name can carry. No digits — that alone excludes every grade token (`NM-`,
# `FN/VF`), every `1:100` ratio, every `2nd Ptg`, every parenthesised
# publisher/date, and every enumerated lot run.
_EDITION_NAME_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z.'\-]*")
_EDITION_TERMINAL_RE = re.compile(_EDITION_WORDS_ALT, re.IGNORECASE)
# Separators left dangling on the kept prefix once the tail is cut, e.g.
# `FANTASTIC FOUR # 31 - (VG-) ...` would otherwise keep a trailing hyphen.
# Also stripped off the FRONT of a tail before it is tokenized.
_TRAILING_SEPARATORS = " \t-–—:,;|"
# Total tokens allowed in an extractable designation, INCLUDING the terminal
# word. All 8 measured class-C rows are 3 (`<First> <Last> Cover`); 4 leaves
# room for a middle name without letting listing prose be swallowed whole —
# `"Silver age Neal Adams Cover"` (a credit on a single-cover book, 5 tokens)
# stays declined. `variant` is row identity, so an unbounded swallow is an
# unbounded number of ways to mint a distinct row for one book.
_EDITION_MAX_TOKENS = 4


def _find_issue_token(title: str, issue: str) -> re.Match[str] | None:
    """Locate *title*'s own ``#<issue>`` token — the signal every rule here is
    keyed on, and the one BUI-596 measured at zero false positives across the
    table (the `#<own issue>` rule and a loose `title LIKE '%#%'` select the
    same 173 rows of 808, so there are no legitimate `#`-in-title rows).

    Shared by `_extract_edition_designation` and `_strip_listing_tail` so the
    two cannot drift on what counts as the token; they run back to back on the
    same (title, issue) inside a single `upsert_comic` call.
    """
    if not title or not issue:
        return None
    return re.search(rf'#\s*{re.escape(issue)}\b', title, flags=re.IGNORECASE)


def multi_issue_lot_reason(title: str, issue: str) -> str | None:
    """Return why *title* reads as a multi-issue lot, or None if it does not.

    BUI-596 class D: one listing, several books, and `issue` holding only the
    first. No title rule repairs this — truncating
    `"Amazing Spider-man #18,19,20,21,22 lot of 5"` mints a clean-looking
    `Amazing Spider-man` #18 that silently asserts the lot IS issue 18, and
    then merges it with the real single issue. `POST /api/comics` refuses these
    outright (BUI-625); `_strip_listing_tail` declines to truncate them.

    Fails OPEN on a blank title/issue, matching every other rule in this module:
    a shape that cannot be evaluated is not a lot.
    """
    issue_str = str(issue).strip() if issue else ""
    if not title or not issue_str:
        return None
    run = rf'(?<!\d){re.escape(issue_str)}\s*{_LOT_SEPARATORS}\s*\d'
    if re.search(run, title):
        return (
            f"issue {issue_str} is followed by a separator and another issue "
            "number, which reads as an enumerated multi-issue run"
        )
    if _LOT_PHRASE_RE.search(title):
        return "the title says 'lot of N', which names several books"
    return None


def _extract_edition_designation(title: str, issue: str) -> str | None:
    """Pull an edition designation out of *title* into a `variant` value.

    Returns the designation verbatim (`"Nick Robles Cover"`), or None when the
    title carries none this rule can attribute unambiguously. See the block
    comment above for why "unambiguous" is defined so narrowly, and why the
    result is never canonicalized.

    Fires only when ALL of these hold, so that two distinct designations can
    never produce one string:

    1. The title duplicates its own `#<issue>` token — the same signal
       `_strip_listing_tail` is keyed on, whose false-positive rate BUI-596
       measured at zero across the table.
    2. Everything after that token is 2..4 whitespace-separated name tokens of
       letters only (no digits, parentheses, slashes or plus/minus).
    3. The LAST of those tokens is an edition word. Ending on it is what makes
       the span decidable: the designation runs to the end of the title, so
       there is no listing prose left over to guess about.
    4. At least one token precedes it. A bare `"#20 Variant"` names no
       distinguishing feature, so two different variants of one issue would
       both extract `"Variant"` and MERGE — the one outcome this must not have.

    Never fires on a lot: clause 2 forbids digits, and a lot tail is an
    enumerated run of them.

    Known, accepted over-split: `variant` is compared case-SENSITIVELY (the
    column has no `COLLATE NOCASE`, and both unique indexes key on
    `COALESCE(variant,'')` with no `LOWER` — unlike `title`, which they do
    lower). So `"Nick Robles Cover"` and `"Nick Robles cover"` would be two
    rows for one book. That asymmetry predates this rule (BUI-28), and folding
    case here would not fix it — only the index would. Splitting is the
    recoverable direction, so it is left alone rather than half-fixed.
    """
    issue_str = str(issue).strip() if issue else ""
    match = _find_issue_token(title, issue_str)
    if match is None:
        return None

    tail = title[match.end():].strip().lstrip(_TRAILING_SEPARATORS).strip()
    tokens = tail.split()
    if not 2 <= len(tokens) <= _EDITION_MAX_TOKENS:
        return None
    if not all(_EDITION_NAME_TOKEN_RE.fullmatch(t) for t in tokens):
        return None
    if not _EDITION_TERMINAL_RE.fullmatch(tokens[-1]):
        return None
    # Rejoin from the split rather than returning the raw slice so that the
    # value is whitespace-normalized — `"Nick  Robles Cover"` and
    # `"Nick Robles Cover"` are one book, and `variant` is matched exactly
    # (the identity index keys on `COALESCE(variant,'')`, with no LOWER and no
    # collation), so an unsquashed double space would be a second row.
    return " ".join(tokens)


def _strip_listing_tail(
    title: str, issue: str, variant: str | None = None
) -> str | None:
    """Truncate *title* at its own ``#<issue>`` token, keeping only the series
    text before it (BUI-599, the class-B "full listing title" shape).

    Returns the truncated title, or ``None`` when the rule declines to fire —
    the caller then falls back to BUI-591's issue-token strip.

    Keyed on exactly the signal BUI-591 already trusts (the row's own issue
    number, duplicated into its own title), so it inherits BUI-596's proof
    that the signal has zero false positives on the live table: the `#<own
    issue>` rule and the loose `title LIKE '%#%'` heuristic select the same
    173 rows out of 808, i.e. there are no legitimate `#`-in-title rows to
    damage. What changes is only what happens *after* the token is located:
    BUI-591 deleted the token and kept the rest, which left
    `"Iron Man #126 (Marvel Comics September 1979) VF Condition!"` as
    `"Iron Man (Marvel Comics September 1979) VF Condition!"` — still
    malformed, still unreachable. Truncating instead yields `"Iron Man"`,
    which is also exactly the `proposed_new_title` BUI-596's own remediation
    plan derives for these rows.

    Declines in three cases, each a fail-open:

    - **Nothing usable before the token** (``"#126 VF"``): there is no series
      name to keep, so the title is left for BUI-591's strip.
    - **A multi-issue lot** (class D, ``"Amazing Spider-man #18,19,20,21,22
      lot of 5"``): truncating would produce a clean-looking `Amazing
      Spider-man` #18 that silently asserts the lot *is* issue 18. Since
      BUI-625 `POST /api/comics` refuses these with a 422 before they ever
      reach here; this decline still covers the in-process callers, which pass
      an already-parsed series rather than a raw listing title.
    - **A variant designation with no `variant` supplied** (class C,
      ``"Iron Man #125 Newsstand Variant (Marvel Comics August 1979) ..."``):
      `variant` is part of row identity (BUI-28), so cutting a cover
      designation out of the title without moving it into that column would
      merge a variant into its base edition — and merge distinct variant
      siblings (`#20 Bermejo Variant` / `#20 Crain Variant`) into one row.
      That is precisely the "silently collide two distinct books" failure this
      write boundary must not have.

      BUI-625 narrowed this decline rather than removing it. `upsert_comic`
      now runs `_extract_edition_designation` FIRST, which moves the
      designation into `variant` for the one tail shape whose span is
      decidable (``"Absolute Flash #10 Nick Robles Cover"``) — and having
      supplied `variant`, truncation here is safe and fires normally. What
      still reaches this decline is the residue: a designation word buried in
      listing prose, where no rule can say which words belong to the edition.
      Those keep today's behaviour (BUI-591's strip, a malformed but
      recoverable title) instead of guessing.

      Note the asymmetry that decides class C vs class D: a lot has NO correct
      row, so refusing the write loses nothing; an unattributable designation
      HAS a correct row that simply cannot be named, so degrading beats
      refusing — hard-failing it would store the book nowhere (the BUI-593
      class), and the caller has no `variant` to supply on a retry.
    """
    issue_str = str(issue).strip() if issue else ""
    match = _find_issue_token(title, issue_str)
    if match is None:
        return None

    tail = title[match.end():]
    lot_reason = multi_issue_lot_reason(title, issue_str)
    if lot_reason is not None:
        logger.warning(
            "upsert_comic: declining listing-title truncation — %s, and one "
            "comics row cannot stand for several books (BUI-596 class D). "
            "title=%r",
            lot_reason,
            title,
        )
        return None
    # `not variant`, not `variant is None`: `upsert_comic` already folds a
    # blank variant to None before calling, but the helper must reach the same
    # verdict when called directly with "" — a blank variant means the
    # designation was NOT recorded, whichever spelling of blank arrives.
    if not variant and _VARIANT_DESIGNATION_RE.search(tail):
        logger.warning(
            "upsert_comic: declining listing-title truncation — text after '#%s' "
            "carries an edition designation that could not be attributed to a "
            "`variant` value, and truncating would merge this into the base "
            "edition (BUI-596 class C residue; variant is row identity per "
            "BUI-28). Pass `variant` explicitly to record it. title=%r",
            issue_str,
            title,
        )
        return None

    prefix = title[: match.start()].strip().rstrip(_TRAILING_SEPARATORS).strip()
    # Compose with BUI-591's strip so a prefix that itself ends in the bare
    # issue token lands on its final form in ONE pass — without this, a second
    # call would keep normalizing and the function would not be idempotent.
    return _strip_embedded_issue(prefix, issue_str) or None


def _normalize_comic_title(title: str, issue: str, variant: str | None = None) -> str:
    """Strip a duplicated issue number out of *title* before it becomes row
    identity (BUI-591), and — since BUI-599 — the listing junk that followed it.

    `POST /api/comics` used to store `title` verbatim, so any writer that
    isn't `comic-fmv` (which normalizes client-side per BUI-346) could persist
    a title with the issue number doubled into it, e.g. `"X-Men #123"` —
    unreachable by any later (title, issue) lookup, including comic-fmv's own.
    Moving the fix here — the single choke point every `upsert_comic` caller
    goes through (`POST /api/comics`, the extract-comics auto-link path in
    `_link_issue_to_bid`, and link-locg's auto-create) — means the server no
    longer depends on a client having normalized first.

    Fails OPEN: a title/issue that can't be normalized (blank/None) is
    returned unchanged, never dropped — matching `fmv_runner.py`'s
    `_normalize_book_title` behavior.

    Deliberately does **not** mirror the leading-article half of BUI-346's
    client-side normalizer (`_strip_leading_article`): many legitimate titles
    intentionally keep "The" (e.g. "The Amazing Spider-Man"), and folding that
    in here would change the identity key for a much wider, unmeasured set of
    existing rows than BUI-591's blast-radius measurement (`title LIKE
    '%#%'`) covered.

    Two rules, tried in order:

    1. **BUI-599** — `_strip_listing_tail` truncates at the `#<issue>` token,
       so a whole eBay listing title collapses to its series name
       (`"Iron Man #126 (Marvel Comics September 1979) VF Condition!"` ->
       `"Iron Man"`). It declines on the two shapes where truncation would
       destroy meaning rather than junk — a multi-issue lot and an unrecorded
       variant designation; see its docstring.
    2. **BUI-591** — otherwise, `_strip_embedded_issue` removes just the
       duplicated issue token, which also covers the bare-trailing-token form
       (`"Amazing Spider-Man 300"`) that carries no `#` for rule 1 to cut at.

    Idempotent: rule 1 keeps only text preceding the *first* `#<issue>` and
    composes BUI-591's strip into that prefix, so re-normalizing an
    already-normalized title is a no-op — which matters because this runs on
    every write to a durable identity column, not once at migration time.

    Note the *result* of normalizing can equal an existing row's title. That
    is the intended effect, not a collision: `upsert_comic`'s reconciliation
    then resolves the write onto that existing row instead of inserting a
    second, unreachable placeholder beside it. The unique indexes are never
    reached with a duplicate key."""
    truncated = _strip_listing_tail(title, issue, variant)
    if truncated is not None:
        return truncated
    # `or title` completes the fail-open contract above. A title that is
    # NOTHING but its own issue token ("#126") strips to the empty string, and
    # BUI-591 stored that — `comics.title` is NOT NULL but '' satisfies NOT
    # NULL, so a blank identity would have been written. Keep the original: a
    # malformed title is recoverable, an empty one is not.
    return _strip_embedded_issue(title, issue) or title


def upsert_comic(
    conn: sqlite3.Connection,
    title: str,
    issue: str,
    year: int | None = None,
    locg_id: int | None = None,
    locg_variant_id: int | None = None,
    variant: str | None = None,
) -> int:
    """Upsert a comic identity row. Returns the comic id.

    BUI-591/BUI-599: `title` is normalized before it becomes row identity — the
    duplicated issue number and any listing junk trailing it are removed; see
    `_normalize_comic_title`. A caller that posts a full eBay listing title
    therefore resolves onto the clean row for that book (creating it if it does
    not exist) rather than depositing a second, unreachable one beside it.

    `variant` (BUI-28) is part of the row identity: a base cover and its
    Newsstand/Direct/etc. variant of the same (title, issue, year) get distinct
    comic ids. An empty/blank variant normalizes to NULL (the base edition). All
    the reconciliation below is scoped to a single variant.

    BUI-625: when the caller supplies no `variant` and the title ends in an
    edition designation, `_extract_edition_designation` recovers it into that
    column, so `"Absolute Flash #10 Nick Robles Cover"` lands as
    `("Absolute Flash", "10", variant="Nick Robles Cover")` — distinct from the
    base edition rather than either merged into it or left malformed.

    Year is optional. Reconciliation rules keep at most one row per
    (title, issue, variant) logical comic:

    - Yeared insert finds an existing yeared row at the same year → updates
      locg metadata, returns it.
    - Yeared insert finds an existing yearless row for the same (title, issue)
      → promotes it (UPDATE comics SET year=?), returns it. Avoids creating a
      duplicate alongside the yearless placeholder. Exception: if a yeared row
      at a *different* year already exists, promotion is skipped (warning logged,
      yearless row returned unchanged) to prevent two yeared siblings (PER-104).
    - Yearless insert finds an existing yeared row for the same (title, issue)
      → prefers the yeared one (returns its id without creating a yearless
      duplicate). Locg metadata still gets merged in.
    - Yearless insert finds an existing yearless row → updates locg, returns.

    When multiple yeared rows exist for the same (title, issue) — pre-PER-98
    historical data — the one with locg_id set wins; ties broken by lowest id.
    """
    # BUI-28: normalize blank variant to NULL (base edition) and scope every
    # identity query to this variant so reconciliation never crosses variants.
    # BUI-599 moved this ABOVE the title normalization, which now needs to know
    # whether a variant was supplied before it will cut a cover designation out
    # of the title.
    variant = (variant or "").strip() or None

    # BUI-625: when the caller named no variant but the title ends in an
    # edition designation, move it into the column it belongs in. This runs
    # BEFORE the title normalization on purpose — having recovered the
    # designation, `_strip_listing_tail` will now truncate the title instead of
    # declining, and the variant stays distinct from the base edition. A
    # caller-supplied `variant` always wins; nothing is overwritten.
    if variant is None:
        variant = _extract_edition_designation(title, issue)

    # BUI-591/BUI-599: normalize before any identity query runs, so
    # reconciliation below (and the yearless-promotion logic) all operate on
    # the same cleaned title every caller would have seen.
    title = _normalize_comic_title(title, issue, variant)

    v_sql = "variant=?" if variant is not None else "variant IS NULL"
    v_param: tuple = (variant,) if variant is not None else ()

    if year is not None:
        # Yeared insert.
        existing_yeared = conn.execute(
            f"SELECT id FROM comics WHERE LOWER(title)=LOWER(?) AND issue=? AND year=? AND {v_sql}",
            (title, issue, year, *v_param),
        ).fetchone()
        if existing_yeared is not None:
            conn.execute(
                "UPDATE comics SET locg_id=COALESCE(?, locg_id), "
                "locg_variant_id=COALESCE(?, locg_variant_id) WHERE id=?",
                (locg_id, locg_variant_id, existing_yeared["id"]),
            )
            conn.commit()
            return existing_yeared["id"]
        # Look for a yearless placeholder to promote.
        existing_yearless = conn.execute(
            f"SELECT id FROM comics WHERE LOWER(title)=LOWER(?) AND issue=? AND year IS NULL AND {v_sql}",
            (title, issue, *v_param),
        ).fetchone()
        if existing_yearless is not None:
            # Guard (PER-104): if a yeared row at a *different* year already
            # exists, promoting would create two yeared siblings. Skip and warn.
            conflicting_yeared = conn.execute(
                "SELECT id FROM comics "
                f"WHERE LOWER(title)=LOWER(?) AND issue=? AND year IS NOT NULL AND year!=? AND {v_sql}",
                (title, issue, year, *v_param),
            ).fetchone()
            if conflicting_yeared is not None:
                logger.warning(
                    "upsert_comic: skipping yearless promotion — yeared sibling "
                    "conflict (title=%r issue=%r incoming_year=%r variant=%r); keeping "
                    "yearless row",
                    title,
                    issue,
                    year,
                    variant,
                )
                return existing_yearless["id"]
            conn.execute(
                "UPDATE comics SET year=?, "
                "locg_id=COALESCE(?, locg_id), "
                "locg_variant_id=COALESCE(?, locg_variant_id) WHERE id=?",
                (year, locg_id, locg_variant_id, existing_yearless["id"]),
            )
            conn.commit()
            return existing_yearless["id"]
        # No existing row — insert fresh.
        cur = conn.execute(
            "INSERT INTO comics (title, issue, year, variant, locg_id, locg_variant_id) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (title, issue, year, variant, locg_id, locg_variant_id),
        )
        conn.commit()
        return cur.lastrowid  # type: ignore[return-value]  # INSERT always yields int lastrowid

    # Yearless insert. Prefer an existing yeared row if one exists — never
    # create a yearless duplicate next to a yeared canonical row.
    canonical_yeared = conn.execute(
        f"SELECT id FROM comics WHERE LOWER(title)=LOWER(?) AND issue=? AND year IS NOT NULL AND {v_sql} "
        "ORDER BY (locg_id IS NULL), id LIMIT 1",
        (title, issue, *v_param),
    ).fetchone()
    if canonical_yeared is not None:
        # PER-103: clean up any pre-existing yearless orphan alongside the
        # canonical yeared row before returning.
        orphan = conn.execute(
            f"SELECT id FROM comics WHERE LOWER(title)=LOWER(?) AND issue=? AND year IS NULL AND {v_sql}",
            (title, issue, *v_param),
        ).fetchone()
        if orphan is not None:
            _merge_yearless_into_yeared(conn, orphan["id"], canonical_yeared["id"])
            conn.execute("DELETE FROM comics WHERE id=?", (orphan["id"],))
        conn.execute(
            "UPDATE comics SET locg_id=COALESCE(?, locg_id), "
            "locg_variant_id=COALESCE(?, locg_variant_id) WHERE id=?",
            (locg_id, locg_variant_id, canonical_yeared["id"]),
        )
        conn.commit()
        return canonical_yeared["id"]
    existing_yearless = conn.execute(
        f"SELECT id FROM comics WHERE LOWER(title)=LOWER(?) AND issue=? AND year IS NULL AND {v_sql}",
        (title, issue, *v_param),
    ).fetchone()
    if existing_yearless is not None:
        conn.execute(
            "UPDATE comics SET locg_id=COALESCE(?, locg_id), "
            "locg_variant_id=COALESCE(?, locg_variant_id) WHERE id=?",
            (locg_id, locg_variant_id, existing_yearless["id"]),
        )
        conn.commit()
        return existing_yearless["id"]
    cur = conn.execute(
        "INSERT INTO comics (title, issue, year, variant, locg_id, locg_variant_id) "
        "VALUES (?, ?, NULL, ?, ?, ?)",
        (title, issue, variant, locg_id, locg_variant_id),
    )
    conn.commit()
    return cur.lastrowid  # type: ignore[return-value]  # INSERT always yields int lastrowid


# ---------------------------------------------------------------------------
# Orphan yearless cleanup (PER-103)
# ---------------------------------------------------------------------------


def _merge_yearless_into_yeared(
    conn: sqlite3.Connection, yearless_id: int, yeared_id: int
) -> None:
    """Reparent all fmv children from a yearless orphan onto a yeared row.

    For each fmv grade on yearless_id:
    - No conflict (yeared has no fmv at that grade): reassign comic_id in-place.
    - Conflict (yeared already has fmv at that grade): COALESCE non-null fields
      into the yeared fmv, reparent bid_fmvs and bids.fmv_id, then delete the
      duplicate yearless fmv row.

    Does NOT delete the yearless comics row — caller's responsibility.
    """
    yearless_fmvs = conn.execute(
        "SELECT id, grade, low, high, comps, confidence, notes, updated_at "
        "FROM fmv WHERE comic_id=?",
        (yearless_id,),
    ).fetchall()

    for yfmv in yearless_fmvs:
        yeared_fmv = conn.execute(
            "SELECT id FROM fmv WHERE comic_id=? AND grade=?",
            (yeared_id, yfmv["grade"]),
        ).fetchone()

        if yeared_fmv is None:
            conn.execute("UPDATE fmv SET comic_id=? WHERE id=?", (yeared_id, yfmv["id"]))
        else:
            # Merge non-null fields from yearless into yeared (COALESCE keeps
            # existing non-null values; yearless fills gaps only).
            conn.execute(
                """
                UPDATE fmv SET
                    low        = COALESCE(low,        ?),
                    high       = COALESCE(high,       ?),
                    comps      = COALESCE(comps,      ?),
                    confidence = COALESCE(confidence, ?),
                    notes      = COALESCE(notes,      ?),
                    updated_at = COALESCE(updated_at, ?)
                WHERE id=?
                """,
                (
                    yfmv["low"], yfmv["high"], yfmv["comps"],
                    yfmv["confidence"], yfmv["notes"], yfmv["updated_at"],
                    yeared_fmv["id"],
                ),
            )
            # Reparent bid_fmvs; preserve the higher is_primary if both exist.
            for bf in conn.execute(
                "SELECT bid_id, is_primary FROM bid_fmvs WHERE fmv_id=?",
                (yfmv["id"],),
            ).fetchall():
                conn.execute(
                    """
                    INSERT INTO bid_fmvs (bid_id, fmv_id, is_primary) VALUES (?, ?, ?)
                    ON CONFLICT(bid_id, fmv_id) DO UPDATE
                        SET is_primary = MAX(is_primary, excluded.is_primary)
                    """,
                    (bf["bid_id"], yeared_fmv["id"], bf["is_primary"]),
                )
            # Reparent bids.fmv_id.
            conn.execute(
                "UPDATE bids SET fmv_id=? WHERE fmv_id=?",
                (yeared_fmv["id"], yfmv["id"]),
            )
            # Delete the now-redundant yearless fmv (cascade cleans its bid_fmvs).
            conn.execute("DELETE FROM fmv WHERE id=?", (yfmv["id"],))


def sweep_orphan_yearless_comics(
    conn: sqlite3.Connection, dry_run: bool = False
) -> dict:
    """Find yearless rows that have a yeared sibling and merge them in.

    When dry_run=True reports what would change without touching the DB.
    Returns a summary dict with 'merged' (or 'would_merge') count and details.
    """
    orphans = _find_yearless_orphans(conn)

    details = [
        {
            "title": row["title"],
            "issue": row["issue"],
            "yearless_id": row["yearless_id"],
            "yeared_id": row["yeared_id"],
        }
        for row in orphans
    ]

    if dry_run:
        return {"dry_run": True, "would_merge": len(details), "details": details}

    for row in orphans:
        _merge_yearless_into_yeared(conn, row["yearless_id"], row["yeared_id"])
        conn.execute("DELETE FROM comics WHERE id=?", (row["yearless_id"],))
    if orphans:
        conn.commit()

    logger.info("sweep_orphan_yearless_comics: merged %d orphan(s)", len(orphans))
    return {"dry_run": False, "merged": len(details), "details": details}


# ---------------------------------------------------------------------------
# FMV CRUD
# ---------------------------------------------------------------------------


def upsert_fmv(
    conn: sqlite3.Connection,
    comic_id: int,
    grade: float | None,
    low: float | None = None,
    high: float | None = None,
    comps: int | None = None,
    confidence: str | None = None,
    notes: str | None = None,
    flag_reason: str | None = None,
) -> int:
    """Upsert a per-grade FMV row. Returns the fmv id.

    `flag_reason` (BUI-132) carries the BUI-86 needs_manual state as a structured
    column (one_sided / too_wide / too_sparse, or NULL for not-flagged). The
    discriminator that lets the upsert distinguish three cases is whether the
    incoming row is a real FMV *decision* (it carries a price or a flag) vs. a
    bare n=0 stub (no price, no flag):

    - A newly-FLAGGED book (incoming flag_reason set) clears its stale
      auto-priced low/high/comps AND overwrites confidence/notes with the
      incoming (typically NULL) values, then stores the flag — a book that now
      needs manual pricing must not keep a stale number or stale auto-price
      metadata (confidence='high', notes) (BUI-86 residual #2). The automated
      fmv_runner path overwrites notes/forces confidence=low anyway, but a
      direct flag-only POST ({grade, fmv_flag_reason}) relies on this.
    - A freshly-PRICED book (incoming low/high set, no flag) stores the new price
      and CLEARS any prior flag — a book that used to be unpriceable but now
      prices cleanly is no longer needs_manual.
    - A bare n=0 STUB (no price, no flag) preserves the existing row — this is
      the n=0 stub guard, and it is NOT weakened here: the stub path is reached
      only when excluded.flag_reason IS NULL AND excluded.low IS NULL, so it can
      never wipe a real price. BUI-599 widened it from the price to the
      metadata beside it (comps/confidence/notes), which a stub used to
      overwrite because it carries them non-NULL (`fmv_comps: 0`, a confidence
      label, notes) — leaving the contradictory `low=15 high=20 comps=0`.
    """
    if grade is None:
        raise ValueError("grade is required for upsert_fmv")
    flag_reason = flag_reason or None
    has_value = any(
        v is not None for v in (low, high, comps, confidence, notes, flag_reason)
    )
    now = datetime.now(timezone.utc).isoformat() if has_value else None
    conn.execute(
        """
        INSERT INTO fmv (comic_id, grade, low, high, comps, confidence, notes, flag_reason, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(comic_id, grade) DO UPDATE SET
            -- A flagged incoming row clears the stale auto-priced number; an
            -- unflagged incoming row (a fresh price OR a bare n=0 stub)
            -- COALESCE-preserves it. The n=0 stub guard lives in the COALESCE:
            -- a stub's NULL low can't overwrite a real price.
            low         = CASE WHEN excluded.flag_reason IS NOT NULL THEN NULL
                               ELSE COALESCE(excluded.low,   low) END,
            high        = CASE WHEN excluded.flag_reason IS NOT NULL THEN NULL
                               ELSE COALESCE(excluded.high,  high) END,
            -- BUI-599: extend the stub guard from the price to the metadata
            -- beside it. A bare stub is not "empty" on the wire — fmv_runner
            -- posts n=0 as `fmv_comps: 0` (not NULL) with a confidence label
            -- and notes, so COALESCE alone let a failed lookup overwrite a
            -- priced row's comps/confidence/notes and leave `low=15 high=20
            -- comps=0` behind. When the incoming row is a stub (no flag, no
            -- price) AND the stored row holds a real price, keep the stored
            -- metadata: a failed lookup must not degrade a good answer.
            comps       = CASE WHEN excluded.flag_reason IS NOT NULL THEN excluded.comps
                               WHEN excluded.low IS NULL AND low IS NOT NULL THEN comps
                               ELSE COALESCE(excluded.comps, comps) END,
            -- A flagged incoming row also drops stale auto-price metadata: a
            -- flag-only POST ({grade, fmv_flag_reason}) carries NULL confidence
            -- and notes, so it must overwrite (not COALESCE-keep) the prior
            -- priced row's confidence/notes — else a needs_manual book would
            -- surface the old auto-price's confidence='high'/notes on /comics.
            confidence  = CASE WHEN excluded.flag_reason IS NOT NULL THEN excluded.confidence
                               WHEN excluded.low IS NULL AND low IS NOT NULL THEN confidence
                               ELSE COALESCE(excluded.confidence, confidence) END,
            notes       = CASE WHEN excluded.flag_reason IS NOT NULL THEN excluded.notes
                               WHEN excluded.low IS NULL AND low IS NOT NULL THEN notes
                               ELSE COALESCE(excluded.notes,      notes) END,
            -- A flagged row stores its flag; a freshly-priced row clears any
            -- prior flag (incoming low set ⇒ no longer needs_manual); a bare
            -- n=0 stub leaves the flag untouched.
            flag_reason = CASE WHEN excluded.flag_reason IS NOT NULL THEN excluded.flag_reason
                               WHEN excluded.low IS NOT NULL THEN NULL
                               ELSE flag_reason END,
            updated_at  = CASE WHEN excluded.low IS NOT NULL OR excluded.flag_reason IS NOT NULL
                               THEN excluded.updated_at
                               ELSE updated_at END
        """,
        (comic_id, grade, low, high, comps, confidence, notes, flag_reason, now),
    )
    conn.commit()
    row = conn.execute(
        "SELECT id FROM fmv WHERE comic_id=? AND grade=?", (comic_id, grade)
    ).fetchone()
    return row["id"]


# ---------------------------------------------------------------------------
# fmv_history (BUI-659)
# ---------------------------------------------------------------------------


def append_fmv_history(
    conn: sqlite3.Connection, fmv_id: int, source: str = "upsert"
) -> None:
    """Append an immutable snapshot of `fmv_id`'s CURRENT row to fmv_history.

    Re-SELECTs the fmv row by id rather than accepting the caller's posted
    values directly — `upsert_fmv`'s CASE/COALESCE logic can leave the stored
    row different from what was posted (an n=0 stub preserves the existing
    price; a flagged row clears it), so the row actually on file after the
    upsert is the only thing worth snapshotting. `recorded_at` is copied from
    the row's own `updated_at`, matching the seeding migration below (which
    uses `fmv.updated_at`, never its own clock) — both paths agree that
    `recorded_at` means "when this reading was established," not "when we
    happened to log it."

    Commits independently (its own transaction) so the caller can wrap this
    in a try/except without the fmv upsert's own commit (already durable by
    the time this runs) being affected either way.

    Raises `ValueError` if `fmv_id` doesn't exist — callers (api_upsert_comic)
    are expected to call this immediately after `upsert_fmv` returns a fresh
    id, so a missing row here means something upstream is already broken and
    should not be swallowed silently by this function itself; the caller's
    own try/except is where "never block the write" is enforced.
    """
    row = conn.execute(
        "SELECT comic_id, grade, low, high, comps, confidence, flag_reason, "
        "notes, updated_at FROM fmv WHERE id=?",
        (fmv_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"append_fmv_history: no fmv row with id={fmv_id}")
    conn.execute(
        """
        INSERT INTO fmv_history (
            comic_id, grade, low, high, comps, confidence, flag_reason, notes,
            recorded_at, source
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            row["comic_id"], row["grade"], row["low"], row["high"],
            row["comps"], row["confidence"], row["flag_reason"], row["notes"],
            row["updated_at"], source,
        ),
    )
    conn.commit()


def _migrate_seed_fmv_history(conn: sqlite3.Connection) -> None:
    """One-time seed: one fmv_history row per existing `fmv` row (BUI-659).

    Gate: migration_state row 'seed_fmv_history' present → already ran, so a
    restart cannot double-seed. Each seeded row carries `source='backfill'`
    and `recorded_at = fmv.updated_at` (the row's own historical timestamp,
    NOT this migration's clock — a row seeded today must not claim its price
    was set today).

    IMPORTANT: Uses raw conn.execute() only — no conn.commit(). Called from
    create_tables(), which runs inside the host's per-plugin SAVEPOINT;
    committing here would destroy it (same constraint as the older
    _migrate_* functions above).
    """
    row = conn.execute(
        "SELECT 1 FROM migration_state WHERE migration='seed_fmv_history'"
    ).fetchone()
    if row is not None:
        return

    fmv_rows = conn.execute(
        "SELECT comic_id, grade, low, high, comps, confidence, flag_reason, "
        "notes, updated_at FROM fmv"
    ).fetchall()
    for r in fmv_rows:
        conn.execute(
            """
            INSERT INTO fmv_history (
                comic_id, grade, low, high, comps, confidence, flag_reason,
                notes, recorded_at, source
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'backfill')
            """,
            (
                r["comic_id"], r["grade"], r["low"], r["high"], r["comps"],
                r["confidence"], r["flag_reason"], r["notes"], r["updated_at"],
            ),
        )
    if fmv_rows:
        logger.info(
            "_migrate_seed_fmv_history: seeded %d row(s)", len(fmv_rows)
        )
    _set_migration_marker(conn, "seed_fmv_history")


# ---------------------------------------------------------------------------
# Comps ledger (BUI-656)
# ---------------------------------------------------------------------------


def upsert_comps(
    conn: sqlite3.Connection,
    comic_id: int | None,
    comps: list[dict[str, Any]],
) -> dict[str, int]:
    """Upsert a batch of comps for one book (or an identity-free backfill
    response, `comic_id=None`). One `INSERT ... ON CONFLICT DO UPDATE` per
    comp, all inside the caller's transaction (a single commit at the end).

    KTD4: a sold listing is immutable — item X cleared at $Y on date D — so
    the conflict rule is *keep the first answer*. Re-observing an already-
    known comp only ever bumps bookkeeping (`last_seen_at`, `seen_count`);
    `price`/`sold_date`/`title`/`link` are never rewritten by any branch of
    this function. When the incoming `price` or `sold_date` disagrees with
    what's on file, `conflict_count` increments and one warning names both
    the stored and incoming values — silently overwriting would let a
    provider bug rewrite history with no trace, and silently ignoring would
    make the disagreement invisible.

    Each `comp` dict is expected to carry `provider`, `product_id`, `pool`,
    and `provenance` (all required — the route's pydantic model enforces the
    closed vocabularies before this ever runs); every other key is optional
    and defaults to NULL when absent.

    Returns `{"inserted": n, "updated": n, "conflicts": n}` — a running
    total across the batch, so the caller (the ingest endpoint) can report
    exactly what happened rather than a bare 200.
    """
    now = datetime.now(timezone.utc).isoformat()
    identity_comic_id = comic_id if comic_id is not None else -1
    inserted = updated = conflicts = 0
    for comp in comps:
        # A direct caller (bypassing the route's pydantic model — e.g. a
        # future backfill script calling this function in-process) gets a
        # named error instead of a bare KeyError, matching upsert_fmv's
        # house style for its own required-field guard (`grade is required
        # for upsert_fmv`).
        missing = [k for k in ("provider", "product_id", "pool", "provenance")
                   if k not in comp]
        if missing:
            raise ValueError(
                f"comp is missing required field(s): {', '.join(missing)}"
            )
        provider = comp["provider"]
        product_id = comp["product_id"]
        pool = comp["pool"]
        price = comp.get("price")
        sold_date = comp.get("sold_date")

        # Pre-check (rather than inferring insert-vs-update from rowcount,
        # which INSERT...ON CONFLICT DO UPDATE reports identically either
        # way) both decides the counters below AND drives the conflict
        # comparison — the same comparison the persisted conflict_count
        # increment uses, so the two can't disagree. Safe without extra
        # locking: this function runs synchronously with no `await` between
        # the SELECT and the INSERT, same as every other read-then-write
        # helper in this module (upsert_fmv, upsert_comic).
        existing = conn.execute(
            "SELECT price, sold_date FROM comps "
            "WHERE provider=? AND product_id=? AND COALESCE(comic_id,-1)=? AND pool=?",
            (provider, product_id, identity_comic_id, pool),
        ).fetchone()

        conflict = False
        if existing is not None:
            if (
                price is not None
                and existing["price"] is not None
                and price != existing["price"]
            ) or (
                sold_date is not None
                and existing["sold_date"] is not None
                and sold_date != existing["sold_date"]
            ):
                conflict = True
                logger.warning(
                    "comps conflict: provider=%s product_id=%s pool=%s "
                    "comic_id=%s stored(price=%s, sold_date=%s) "
                    "incoming(price=%s, sold_date=%s)",
                    provider, product_id, pool, comic_id,
                    existing["price"], existing["sold_date"], price, sold_date,
                )

        conn.execute(
            """
            INSERT INTO comps (
                comic_id, pool, provider, product_id, title, price, sold_date,
                grade, buying_format, link, query, tier, from_cache,
                observed_at, provenance, first_seen_at, last_seen_at,
                seen_count, conflict_count
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 0)
            ON CONFLICT(provider, product_id, COALESCE(comic_id, -1), pool)
            DO UPDATE SET
                last_seen_at   = excluded.last_seen_at,
                seen_count     = seen_count + 1,
                conflict_count = conflict_count + ?
            """,
            (
                comic_id, pool, provider, product_id,
                comp.get("title"), price, sold_date, comp.get("grade"),
                comp.get("buying_format"), comp.get("link"), comp.get("query"),
                comp.get("tier"), comp.get("from_cache"), comp.get("observed_at"),
                comp["provenance"], now, now,
                1 if conflict else 0,
            ),
        )
        if existing is None:
            inserted += 1
        else:
            updated += 1
            if conflict:
                conflicts += 1

    conn.commit()
    return {"inserted": inserted, "updated": updated, "conflicts": conflicts}


def link_fmv_to_bid(
    conn: sqlite3.Connection,
    bid_id: int,
    fmv_id: int,
    is_primary: bool = False,
) -> None:
    """Insert into bid_fmvs and keep one junction per comic per bid.

    Primary links replace any prior junction pointing at the *same comic*
    (so a grade-only stub re-linked to a valued FMV collapses to one row
    rather than leaving a demoted null-valued duplicate — BUI-82) and demote
    other-comic junctions to lot members. A sole junction is always primary
    so the dashboard's grade/FMV aggregates (which key off is_primary=1)
    never blank.
    """
    if is_primary:
        # Drop prior junctions for the same comic as the new FMV; genuine
        # other-comic lot members survive and are demoted just below.
        conn.execute(
            """
            DELETE FROM bid_fmvs
            WHERE bid_id = ?
              AND fmv_id != ?
              AND fmv_id IN (
                  SELECT other.id FROM fmv other
                  JOIN fmv target ON target.comic_id = other.comic_id
                  WHERE target.id = ?
              )
            """,
            (bid_id, fmv_id, fmv_id),
        )
        conn.execute(
            "UPDATE bid_fmvs SET is_primary=0 WHERE bid_id=? AND fmv_id != ?",
            (bid_id, fmv_id),
        )
        conn.execute(
            """
            INSERT INTO bid_fmvs (bid_id, fmv_id, is_primary)
            VALUES (?, ?, 1)
            ON CONFLICT(bid_id, fmv_id) DO UPDATE SET is_primary = 1
            """,
            (bid_id, fmv_id),
        )
        conn.execute("UPDATE bids SET fmv_id=? WHERE id=?", (fmv_id, bid_id))
    else:
        conn.execute(
            "INSERT OR IGNORE INTO bid_fmvs (bid_id, fmv_id, is_primary) VALUES (?, ?, 0)",
            (bid_id, fmv_id),
        )
        # A sole junction must be primary, else cond_grade/fmv blank out.
        sole = conn.execute(
            "SELECT COUNT(*) FROM bid_fmvs WHERE bid_id=?", (bid_id,)
        ).fetchone()[0] == 1
        if sole:
            conn.execute(
                "UPDATE bid_fmvs SET is_primary=1 WHERE bid_id=? AND fmv_id=?",
                (bid_id, fmv_id),
            )
            conn.execute("UPDATE bids SET fmv_id=? WHERE id=?", (fmv_id, bid_id))
    conn.commit()


def get_primary_fmv_for_bid(conn: sqlite3.Connection, bid_id: int) -> sqlite3.Row | None:
    """Return the primary fmv row (with comic fields) for a bid."""
    return conn.execute(
        """
        SELECT f.*, c.title, c.issue, c.year, c.locg_id, c.locg_variant_id
        FROM bid_fmvs bf
        JOIN fmv f ON f.id = bf.fmv_id
        JOIN comics c ON c.id = f.comic_id
        WHERE bf.bid_id = ? AND bf.is_primary = 1
        LIMIT 1
        """,
        (bid_id,),
    ).fetchone()


# ---------------------------------------------------------------------------
# Read path
# ---------------------------------------------------------------------------


def list_comics(
    conn: sqlite3.Connection,
    title: str | None = None,
    issue: str | None = None,
    year: int | None = None,
    grade: float | None = None,
    locg_id: int | None = None,
    locg_variant_id: int | None = None,
    max_age_days: float | None = None,
) -> list[sqlite3.Row]:
    """Return comics enriched with FMV data. One row per (comic, fmv) pair.

    locg_id: filter to one canonical issue (used by comic-fmv to look up a
        fresh FMV by LOCG ID + grade without juggling title spellings).
    locg_variant_id: BUI-139 — two variant rows of one issue share the same
        issue-level locg_id (only locg_variant_id differs), so a locg_id+grade
        lookup alone is variant-blind and can return a base cover's FMV for a
        Newsstand variant (a different price tier). When set, scope to that
        exact variant. (Base/NULL-variant disambiguation is done caller-side in
        comic-fmv's _db_lookup, since an absent query param can't express NULL.)
    max_age_days: if set, only return rows where the joined fmv.updated_at
        is within the last N days. Stale rows are excluded so callers can't
        accidentally reuse outdated FMVs.
    """
    clauses: list[str] = []
    params: list[Any] = []
    if title is not None:
        clauses.append("LOWER(c.title) = LOWER(?)")
        params.append(title)
    if issue is not None:
        clauses.append("c.issue = ?")
        params.append(issue)
    if year is not None:
        clauses.append("c.year = ?")
        params.append(year)
    if grade is not None:
        clauses.append("f.grade = ?")
        params.append(grade)
    if locg_id is not None:
        clauses.append("c.locg_id = ?")
        params.append(locg_id)
    if locg_variant_id is not None:
        clauses.append("c.locg_variant_id = ?")
        params.append(locg_variant_id)
    if max_age_days is not None:
        cutoff = (datetime.now(timezone.utc)
                  - timedelta(days=max_age_days)).isoformat()
        clauses.append("f.updated_at IS NOT NULL AND f.updated_at >= ?")
        params.append(cutoff)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    return conn.execute(
        f"""
        SELECT c.id, c.title, c.issue, c.year, c.locg_id, c.locg_variant_id,
               f.id AS fmv_id, f.grade,
               f.low AS fmv_low, f.high AS fmv_high, f.comps AS fmv_comps,
               f.confidence AS fmv_confidence, f.notes AS fmv_notes,
               f.flag_reason AS fmv_flag_reason,
               f.updated_at AS fmv_updated_at
        FROM comics c
        LEFT JOIN fmv f ON f.comic_id = c.id
        {where}
        ORDER BY c.id, f.grade
        """,
        params,
    ).fetchall()


# ---------------------------------------------------------------------------
# First-party auction outcomes (BUI-286)
# ---------------------------------------------------------------------------

# Default grade-window scanned for candidate outcomes. Deliberately generous
# (matches apps/fmv's fmv_math.MAX_GRADE_WINDOW ceiling) — the caller merges
# these rows into the same comp pool build_pool() progressively narrows with
# its own ±0.5→±2.0 walk, so the *effective* grade window a first-party comp
# survives at is whatever build_pool lands on, not this default. Kept as an
# independent constant (not imported from apps/fmv) because gixen-overlay does
# not depend on apps/fmv — apps/* are uv-tool-installed, not workspace members.
DEFAULT_OUTCOME_GRADE_WINDOW = 2.0

# Default recency window (days) for "resolved recently enough to be a current
# comp". 180 days is a starting default (BUI-286); not yet tied to any decay
# curve — that is U2's job.
DEFAULT_OUTCOME_RECENCY_DAYS = 180

# R2/KTD-3 (structural invariant): WON and LOST together, in one clause, with
# no parameter to narrow it to wins alone. A caller wanting "wins only" would
# reintroduce the truncated-from-above deflation spiral (a proxy-auction win's
# winning_bid is the underbidder's max; the wins we see are the auctions we
# *didn't* lose to a higher bidder, so a wins-only sample biases low). See the
# Problem Frame in docs/plans/2026-07-04-001-feat-fmv-auction-outcome-feedback-plan.md.
_STATUS_WON = "WON"
_STATUS_LOST = "LOST"
# Single source of truth: the SQL IN-list is built from the same constants the
# Python-side bucketing in `calibration_report` compares against, so the two
# can't drift if the resolved-status set ever changes.
_OUTCOME_STATUSES_SQL = f"'{_STATUS_WON}', '{_STATUS_LOST}'"

# BUI-660: mark_bids_purged (the completed-bids sweep) and update_bid_status
# (the three BUI-371 classification sites) both tombstone a resolved bid to
# REMOVED, erasing whether it was WON or LOST — 89 first-party comps were
# already destroyed this way before `bids.prior_status` existed (see
# packages/gixen-cli/server/db.py). `prior_status` records the status a
# tombstone replaced, so a purge-swept row is still admissible: "resolved
# now, or resolved before the tombstone."
#
# The status a caller should treat as "what this bid resolved as": the live
# status normally, or the pre-tombstone status for a purge-swept REMOVED row.
# A REMOVED row whose prior_status is NULL — written before this fix, or
# tombstoned from PENDING by a BUI-371 classification site — evaluates to
# NULL here, same as any other never-resolved row; no history is fabricated
# for either.
_EFFECTIVE_STATUS_SQL = (
    "CASE WHEN b.status = 'REMOVED' THEN b.prior_status ELSE b.status END"
)
# Defined as a single IN-check against _EFFECTIVE_STATUS_SQL (not an OR of
# two separately-worded status checks) so the two constants can never drift
# apart — SQL's three-valued logic already does the right thing for a
# tombstone with no recoverable prior status: NULL IN (...) evaluates to
# NULL, which WHERE treats as "no match," excluding the row exactly as
# intended.
_RESOLVED_STATUS_CLAUSE = f"{_EFFECTIVE_STATUS_SQL} IN ({_OUTCOME_STATUSES_SQL})"

# Shared "a resolved auction" predicate fragments (BUI-286/BUI-288). Both
# `get_first_party_outcomes` (Issue A, the comp-pool feed) and
# `calibration_report` (Issue C, the loss-vs-FMV audit) build their WHERE
# clause out of exactly these fragments plus `_OUTCOME_STATUSES_SQL` above —
# there is deliberately no second, divergently-worded definition of "a
# resolved auction" anywhere in this module.

# A multi-comic lot's `winning_bid` prices the whole lot, not one book, so a
# secondary lot member (`is_primary = 0`) is not a valid per-book comp.
_PRIMARY_LINK_CLAUSE = "bf.is_primary = 1"

# NULL-price `ENDED` rows carry no trustworthy comp/signal (R3).
_WINNING_BID_NOT_NULL_CLAUSE = "b.winning_bid IS NOT NULL"

# Recency is judged the same way /api/comics/history judges "when did this
# resolve": COALESCE(auction_end_at, resolved_at), bounded to the last `days`.
# SQLite's own datetime() normalizes both sides rather than comparing against
# a Python-formatted ISO string — see the note on `get_first_party_outcomes`.
_RESOLVED_RECENCY_CLAUSE = (
    "datetime(COALESCE(b.auction_end_at, b.resolved_at)) >= datetime('now', ?)"
)


def get_first_party_outcomes(
    conn: sqlite3.Connection,
    *,
    grade: float,
    title: str | None = None,
    issue: str | None = None,
    year: int | None = None,
    locg_id: int | None = None,
    locg_variant_id: int | None = None,
    window: float = DEFAULT_OUTCOME_GRADE_WINDOW,
    days: float = DEFAULT_OUTCOME_RECENCY_DAYS,
) -> list[sqlite3.Row]:
    """Return the user's own resolved auctions for a (comic, grade) window.

    BUI-286 (Issue A in the auction-outcome-feedback plan): feeds apps/fmv's
    first-party-comp merge via GET /api/comics/outcomes, and is designed to be
    reused as-is (not forked) by the later loss-vs-FMV calibration report
    (Issue C) — both need the identical definition of "a resolved auction".

    Comic resolution mirrors `list_comics`: locg_id (+ optional
    locg_variant_id) when given, else (title, issue[, year]) case-insensitive
    on title. Returns [] (never raises) when neither identity is supplied, or
    when nothing matches — a book with no resolved auctions must fall through
    to pricing exactly as it does today, not error.

    Trustworthiness filter (R3): `b.winning_bid IS NOT NULL` AND
    `_RESOLVED_STATUS_CLAUSE` — NULL-price ENDED rows are excluded, and a
    REMOVED tombstone is admitted only when `prior_status` shows it was
    purge-swept from WON/LOST (BUI-660); a tombstone with no prior_status
    (written from PENDING, or before this column existed) stays excluded.
    `winning_bid IS NOT NULL` is kept as an explicit belt-and-suspenders check
    per R3's wording. The returned `status` column reflects this: a
    purge-swept row reports its pre-tombstone status (WON/LOST), never the
    literal `REMOVED` stored on the row — see `_EFFECTIVE_STATUS_SQL`.

    `bf.is_primary = 1` restricts to the bid's primary linked comic. A
    multi-comic lot's `winning_bid` prices the whole lot, not one book, so a
    secondary lot member (`is_primary = 0`) is not a valid per-book comp and
    is excluded — the same reasoning `get_primary_fmv_for_bid` and the
    dashboard aggregates already apply to represent "the" comic for a bid.

    Recency is judged the same way /api/comics/history judges "when did this
    resolve": `COALESCE(auction_end_at, resolved_at)`, bounded to the last
    `days`.
    """
    if not locg_id and not (title and issue):
        return []

    clauses: list[str] = [_PRIMARY_LINK_CLAUSE]
    params: list[Any] = []
    if locg_id is not None:
        clauses.append("c.locg_id = ?")
        params.append(locg_id)
        if locg_variant_id is not None:
            clauses.append("c.locg_variant_id = ?")
            params.append(locg_variant_id)
    else:
        clauses.append("LOWER(c.title) = LOWER(?)")
        params.append(title)
        clauses.append("c.issue = ?")
        params.append(issue)
        if year is not None:
            clauses.append("c.year = ?")
            params.append(year)

    clauses.append("f.grade BETWEEN ? AND ?")
    params.extend([grade - window, grade + window])
    # BUI-660: admits a live WON/LOST row, or a REMOVED row purge-swept from
    # one (prior_status IN (WON, LOST)) — see _RESOLVED_STATUS_CLAUSE.
    clauses.append(_RESOLVED_STATUS_CLAUSE)
    clauses.append(_WINNING_BID_NOT_NULL_CLAUSE)
    # Normalize both sides through SQLite's own datetime() rather than
    # comparing against a Python-formatted ISO string: auction_end_at/
    # resolved_at are stored in SQLite's "YYYY-MM-DD HH:MM:SS" shape (space
    # separator, no offset), which does not compare correctly byte-for-byte
    # against `datetime.isoformat()`'s "T"-separated, offset-suffixed output
    # (same convention already used by /api/comics/history's own recency
    # filter, a few lines up the file in routes.py).
    clauses.append(_RESOLVED_RECENCY_CLAUSE)
    params.append(f"-{days} days")

    where = " AND ".join(clauses)
    return conn.execute(
        f"""
        SELECT b.winning_bid AS price,
               f.grade AS grade,
               COALESCE(b.auction_end_at, b.resolved_at) AS sold_date,
               {_EFFECTIVE_STATUS_SQL} AS status
        FROM bids b
        JOIN bid_fmvs bf ON bf.bid_id = b.id
        JOIN fmv f       ON f.id = bf.fmv_id
        JOIN comics c    ON c.id = f.comic_id
        WHERE {where}
        ORDER BY sold_date DESC
        """,
        params,
    ).fetchall()


# ---------------------------------------------------------------------------
# Loss-vs-FMV calibration report (BUI-288)
# ---------------------------------------------------------------------------


DEFAULT_CALIBRATION_MIN_LOSSES = 2


def calibration_report(
    conn: sqlite3.Connection,
    *,
    days: float = DEFAULT_OUTCOME_RECENCY_DAYS,
    min_losses: int = DEFAULT_CALIBRATION_MIN_LOSSES,
) -> list[dict[str, Any]]:
    """DIAGNOSTIC-ONLY audit: rank priced (comic, grade) books with evidence
    that `fmv.high` is set too low — the honest "learn from outcomes, without
    learning the wrong lesson" loop (Issue C in the auction-outcome-feedback
    plan; rebased BUI-532/BUI-543). Issues **zero writes** — this function
    contains no INSERT/UPDATE/upsert of any kind, only a single SELECT plus
    in-memory aggregation; it never touches the `fmv` table.

    **Two independent admit paths; either one surfaces a row (BUI-543):**

    1. **Win-based exceedance — the headline.** `contested_win_margin`
       (`median(winning_bid / fmv_high)` over WINS) is exact, uncensored
       evidence: a WON row's `winning_bid` is the literal price paid, no
       estimation or floor involved. `contested_win_margin > 1` admits a row
       **on its own, regardless of loss history — including a book with zero
       losses.** Before BUI-543, `contested_win_margin` was computed but
       never consulted for admission, so a book with strong confirmed
       win-based exceedance and fewer than `min_losses` losses (including
       none at all) never surfaced here no matter how strong its win
       evidence — that gap is what BUI-543 closes.
    2. **Loss-based overshoot — the labeled secondary, unchanged from
       pre-BUI-543.** See the `min_losses`/`overshoot` gates below.

    **`min_losses` governs only the loss-based signal — never a row's
    existence.** A row with a qualifying win margin surfaces however many
    losses it has, from zero up; the loss-based path still independently
    requires >= `min_losses` losses before *its own* signal counts. Raising
    or lowering `min_losses` only ever tightens or loosens the loss-based
    gate — it can never suppress a row a qualifying win margin already
    admitted, and it can never admit a row on loss count/rate alone.

    **The ranking key for the loss-based path is OVERSHOOT vs `fmv.high`,
    never raw win/loss rate.** Losing is the *intended* outcome of the 80%
    (or 60%, on low confidence) bid haircut — you are designed to lose most
    auctions by bargain-hunting below fair value, so a high loss *count* or
    *rate* is not a mispricing signal. A book that loses every auction it
    enters is working exactly as designed as long as those losses clear at or
    below `fmv.high`. The only sound loss-based signal is where the losing
    hammer price lands *relative to* `fmv.high`: persistently clearing above
    it means `fmv.high` itself is set too low. **Do not "also surface high
    loss-rate books" or add a win/loss RATE metric here** — that reintroduces
    the deflation/mispricing trap this report exists to avoid (R4 in the
    plan; see the Problem Frame in
    docs/plans/2026-07-04-001-feat-fmv-auction-outcome-feedback-plan.md). R4
    is about win/loss *rate*; it has never governed win-based *exceedance*
    (a `> 1` margin on an exact, uncensored price) — BUI-543 admits on the
    latter and still never ranks, filters, or promotes any row on a *low*
    margin, a loss count, or a loss rate.

    Reuses the exact same "a resolved auction" predicate as
    `get_first_party_outcomes` — `_RESOLVED_STATUS_CLAUSE`,
    `_PRIMARY_LINK_CLAUSE`, `_WINNING_BID_NOT_NULL_CLAUSE`, and
    `_RESOLVED_RECENCY_CLAUSE` — rather than forking a second definition
    (BUI-660 verified this report inherits the purge-durability fix rather
    than assuming it: `_RESOLVED_STATUS_CLAUSE` admits a purge-swept REMOVED
    row exactly as `get_first_party_outcomes` does, and the WON/LOST bucketing
    below reads `_EFFECTIVE_STATUS_SQL` — the pre-tombstone status — so a
    restored row still counts as its original win or loss rather than
    silently dropping out of both buckets). Unlike `get_first_party_outcomes`
    (which grade-windows around a *target* grade for a fresh pricing run),
    this report only considers a bid's own directly-linked `fmv` row
    (`bid_fmvs.fmv_id = fmv.id`, no window): calibration measures each priced
    (comic, grade) row against the auctions actually linked to it.

    Excludes: NULL `winning_bid`, `ENDED` bids and a REMOVED tombstone with no
    recoverable prior WON/LOST status (`_RESOLVED_STATUS_CLAUSE` excludes
    both), secondary lot members (`is_primary = 0`), and any `fmv` row with a
    NULL (or non-positive) `high` — an unpriced or flagged book has nothing to
    compare a hammer price against, so it is excluded rather than dividing by
    zero/NULL.

    A (comic, grade) with **fewer than `min_losses` losses** (default
    `DEFAULT_CALIBRATION_MIN_LOSSES` = 2) in-window does not get a loss-based
    admit. A single loss — however far above `fmv_high` it cleared — is
    indistinguishable from a one-off bidding war; the loss-based signal's
    "persistent" framing requires at least `min_losses` independent losses
    before the overshoot is trusted as a pattern rather than an outlier. A
    row like this can still surface via the win-based path above — the
    min_losses gate no longer decides whether the row exists at all (BUI-543).

    A (comic, grade) whose losses' median `winning_bid / fmv_high` is `<= 1`
    does not get a loss-based admit either — those losses cleared at or below
    `fmv_high`, which is the haircut doing its job, however many losses there
    are (R4: never surface on loss *count*). The gate drops the loss-based
    signal when the MEDIAN loss ratio is `<= 1`. For n >= 3 the median is
    outlier-robust — a single high-ratio loss (a bidding war) does not by
    itself drag it above 1 (e.g. ratios `[0.5, 0.6, 5.0]` have median `0.6`,
    not admitted, even though one ratio is `5.0`; note this is NOT the same
    as every individual ratio being `<= 1`). CAVEAT at the default
    `min_losses = 2`: median([a, b]) == (a + b) / 2, the mean — so a single
    blowout is only half-tempered. A pair like `[1.02, 8.0]` has median
    `4.51` and WILL admit at overshoot 4.51. The min_losses gate suppresses
    lone single-loss noise but is NOT fully outlier-robust at n = 2; the
    human reading the ranked list should weigh `loss_count` and the loss
    spread, not `overshoot` alone. The gate is written against the median
    `overshoot`, not the rate, on purpose — so a future change to the rate
    metric can't accidentally start surfacing sub-1 medians again.

    A (comic, grade) with **no wins and no losses in-window at all** has no
    calibration signal of any kind and is always omitted, regardless of
    `min_losses`.

    Returns one dict per (comic, grade) that clears either admit path above,
    each with:
      - `comic_id`, `title`, `issue`, `year`, `grade`, `fmv_high`
      - `loss_count`, `above_fmv_loss_count`, `above_fmv_loss_rate` (0-100,
        the % of losses where `winning_bid > fmv_high`; `above_fmv_loss_rate`
        is `None` when `loss_count` is 0)
      - `overshoot` — `median(winning_bid / fmv_high)` over losses, or `None`
        when there are no losses. A **censored, confounded upper bound** (see
        the `/comic:calibration-report` skill doc's "why it changed
        (BUI-532)" section) — reported as context on every row, and the
        ranking key only for rows whose `loss_backed` is `True`.
      - `win_count`, `contested_win_margin` — `median(winning_bid / fmv_high)`
        over wins, or `None` if there were no wins. Exact and uncensored —
        reported as context on every row, and the ranking key (and headline
        admit signal) for rows whose `win_backed` is `True`.
      - `win_backed` (bool) — `True` iff `contested_win_margin` is non-null
        and `> 1`: this row cleared the win-based admit gate by itself.
      - `loss_backed` (bool) — `True` iff `loss_count >= min_losses` and
        `overshoot` is non-null and `> 1`: this row independently clears the
        (unchanged) loss-based admit gate. At least one of `win_backed` /
        `loss_backed` is always `True` for a returned row; both can be `True`
        at once. These two booleans make a row self-describing on the
        payload alone — a caller never has to know the `min_losses` it
        requested to tell a win-backed row from a loss-backed one.

    Sorted with `win_backed` rows first (the headline — exact, uncensored
    evidence), each tier then ordered by its own metric descending
    (`contested_win_margin` within the win-backed tier, `overshoot` within
    the loss-only tier). A win-backed row always outranks a loss-only row,
    regardless of either metric's magnitude.
    """
    rows = conn.execute(
        f"""
        SELECT f.id AS fmv_id,
               c.id AS comic_id,
               c.title AS title,
               c.issue AS issue,
               c.year AS year,
               f.grade AS grade,
               f.high AS fmv_high,
               b.winning_bid AS winning_bid,
               {_EFFECTIVE_STATUS_SQL} AS status
        FROM bids b
        JOIN bid_fmvs bf ON bf.bid_id = b.id
        JOIN fmv f       ON f.id = bf.fmv_id
        JOIN comics c    ON c.id = f.comic_id
        WHERE {_PRIMARY_LINK_CLAUSE}
          AND {_RESOLVED_STATUS_CLAUSE}
          AND {_WINNING_BID_NOT_NULL_CLAUSE}
          AND f.high IS NOT NULL
          AND {_RESOLVED_RECENCY_CLAUSE}
        ORDER BY f.id
        """,
        (f"-{days} days",),
    ).fetchall()

    groups: dict[int, dict[str, Any]] = {}
    for row in rows:
        fmv_high = row["fmv_high"]
        if fmv_high is None or fmv_high <= 0:
            # Guard against divide-by-zero / a nonsensical zero-or-negative
            # fmv_high — belt-and-suspenders alongside the NOT NULL SQL filter.
            continue
        group = groups.setdefault(
            row["fmv_id"],
            {
                "comic_id": row["comic_id"],
                "title": row["title"],
                "issue": row["issue"],
                "year": row["year"],
                "grade": row["grade"],
                "fmv_high": fmv_high,
                "_losses": [],
                "_wins": [],
            },
        )
        ratio = row["winning_bid"] / fmv_high
        if row["status"] == _STATUS_LOST:
            group["_losses"].append(ratio)
        elif row["status"] == _STATUS_WON:
            group["_wins"].append(ratio)

    report: list[dict[str, Any]] = []
    for group in groups.values():
        losses = group.pop("_losses")
        wins = group.pop("_wins")

        loss_count = len(losses)
        overshoot = median(losses) if losses else None
        above_fmv_loss_count = sum(1 for ratio in losses if ratio > 1) if losses else 0
        above_fmv_loss_rate = (
            above_fmv_loss_count / loss_count * 100 if loss_count else None
        )

        win_count = len(wins)
        contested_win_margin = median(wins) if wins else None

        # BUI-543: two independent admit paths — either one surfaces the row.
        # Win-based: contested_win_margin is exact, uncensored evidence (a
        # WON row's winning_bid is the literal price paid) — admits on its
        # own, regardless of loss history, including a book with zero losses.
        win_backed = contested_win_margin is not None and contested_win_margin > 1
        # Loss-based: unchanged pre-BUI-543 gate (FIX 3 + R4) — requires
        # >= min_losses qualifying losses AND a median overshoot > 1.
        # min_losses governs ONLY this loss-based signal, never whether the
        # row exists at all — that governance leak onto row existence was
        # the BUI-543 bug (a zero-loss win-backed book never surfaced).
        loss_backed = (
            loss_count >= min_losses and overshoot is not None and overshoot > 1
        )
        if not (win_backed or loss_backed):
            continue  # neither admit signal clears its bar (R4)

        group["loss_count"] = loss_count
        group["above_fmv_loss_count"] = above_fmv_loss_count
        group["above_fmv_loss_rate"] = above_fmv_loss_rate
        group["overshoot"] = overshoot
        group["win_count"] = win_count
        group["contested_win_margin"] = contested_win_margin
        group["win_backed"] = win_backed
        group["loss_backed"] = loss_backed
        report.append(group)

    # BUI-543: win-backed rows headline (exact, uncensored evidence) — a
    # loss-only row never outranks one, however large its overshoot. Within
    # each tier, sort by that tier's own metric descending. Both branches of
    # the ternary are guaranteed non-None for the rows that reach them (a
    # win_backed row always has a numeric contested_win_margin; a row that
    # falls to the `else` is only here because loss_backed admitted it, which
    # requires a numeric overshoot).
    report.sort(
        key=lambda g: (
            g["win_backed"],
            g["contested_win_margin"] if g["win_backed"] else g["overshoot"],
        ),
        reverse=True,
    )
    return report


# ---------------------------------------------------------------------------
# Comps and fmv_history read endpoints (BUI-662)
# ---------------------------------------------------------------------------

# Neither read function below is ever called from the pricing path — this
# project writes the archive and never reads it back into a price (see the
# comps-data-flywheel plan's Scope Boundaries). See
# tests/test_fmv_history.py's contract test, which greps apps/fmv for exactly
# that.

DEFAULT_COMPS_READ_LIMIT = 100
DEFAULT_FMV_HISTORY_READ_LIMIT = 100


def _resolve_comic_id(
    conn: sqlite3.Connection,
    *,
    comic_id: int | None,
    title: str | None,
    issue: str | None,
    year: int | None,
) -> int | None:
    """Resolve a `comic_id` or `(title, issue[, year])` pair to a comics.id.

    Returns None when the identity is UNRESOLVABLE: neither `comic_id` nor
    `(title AND issue)` was supplied, or what was supplied matches no row in
    `comics`. Both `get_comps` and `get_fmv_history` below treat that as
    distinct from "resolved, but nothing on file for it" — the caller 400s
    on None (wrong/unknown book) versus 200 + [] on an empty result (a known
    book we simply have no rows for yet). Same resolution shape as
    `list_comics` (case-insensitive title match, optional year).
    """
    if comic_id is not None:
        row = conn.execute(
            "SELECT id FROM comics WHERE id=?", (comic_id,)
        ).fetchone()
        return row["id"] if row is not None else None
    if title is not None and issue is not None:
        clauses = ["LOWER(title) = LOWER(?)", "issue = ?"]
        params: list[Any] = [title, issue]
        if year is not None:
            clauses.append("year = ?")
            params.append(year)
        row = conn.execute(
            f"SELECT id FROM comics WHERE {' AND '.join(clauses)} LIMIT 1",
            params,
        ).fetchone()
        return row["id"] if row is not None else None
    return None


def get_comps(
    conn: sqlite3.Connection,
    *,
    comic_id: int | None = None,
    title: str | None = None,
    issue: str | None = None,
    year: int | None = None,
    grade: float | None = None,
    days: float | None = None,
    pool: str | None = None,
    provider: str | None = None,
    limit: int = DEFAULT_COMPS_READ_LIMIT,
) -> list[sqlite3.Row] | None:
    """Read path for `GET /api/comics/comps` (BUI-662). Newest-first by
    `observed_at`.

    Identity: `comic_id`, or `(title, issue[, year])` — see
    `_resolve_comic_id`. Returns None when the identity is unresolvable (the
    route 400s); returns [] when it resolves to a real book with no comps on
    file (the route 200s) — "no comps" must never be confusable with "wrong
    book."

    `days` filters on `observed_at` (when the comp was fetched), never
    `first_seen_at` (when this row was first written to the ledger) — a
    re-observed comp's `observed_at` advances even though `first_seen_at`
    doesn't, and staleness here is about the market data's age, not the
    ledger row's age. Comparison goes through SQLite's own `datetime()`
    (mirrors `_RESOLVED_RECENCY_CLAUSE`), not a Python-formatted string, since
    `observed_at` may carry a 'Z' suffix that doesn't compare correctly
    byte-for-byte against `datetime.isoformat()`'s output.
    """
    resolved_id = _resolve_comic_id(
        conn, comic_id=comic_id, title=title, issue=issue, year=year
    )
    if resolved_id is None:
        return None

    clauses = ["comic_id = ?"]
    params: list[Any] = [resolved_id]
    if grade is not None:
        clauses.append("grade = ?")
        params.append(grade)
    if pool is not None:
        clauses.append("pool = ?")
        params.append(pool)
    if provider is not None:
        clauses.append("provider = ?")
        params.append(provider)
    if days is not None:
        clauses.append(
            "observed_at IS NOT NULL AND datetime(observed_at) >= datetime('now', ?)"
        )
        params.append(f"-{days} days")
    where = " AND ".join(clauses)
    params.append(limit)
    return conn.execute(
        f"""
        SELECT * FROM comps
        WHERE {where}
        ORDER BY observed_at DESC
        LIMIT ?
        """,
        params,
    ).fetchall()


def get_fmv_history(
    conn: sqlite3.Connection,
    *,
    comic_id: int | None = None,
    title: str | None = None,
    issue: str | None = None,
    year: int | None = None,
    grade: float | None = None,
    limit: int = DEFAULT_FMV_HISTORY_READ_LIMIT,
) -> list[sqlite3.Row] | None:
    """Read path for `GET /api/comics/fmv-history` (BUI-662). Newest-first by
    `recorded_at`.

    Identity and the None-vs-[] contract are identical to `get_comps` above
    (see `_resolve_comic_id`) — an unresolvable book returns None (the route
    400s), a resolved book with no history rows returns [] (the route 200s).
    """
    resolved_id = _resolve_comic_id(
        conn, comic_id=comic_id, title=title, issue=issue, year=year
    )
    if resolved_id is None:
        return None

    clauses = ["comic_id = ?"]
    params: list[Any] = [resolved_id]
    if grade is not None:
        clauses.append("grade = ?")
        params.append(grade)
    where = " AND ".join(clauses)
    params.append(limit)
    return conn.execute(
        f"""
        SELECT * FROM fmv_history
        WHERE {where}
        ORDER BY recorded_at DESC
        LIMIT ?
        """,
        params,
    ).fetchall()


# ---------------------------------------------------------------------------
# Seller-scan seen-tracking (BUI-113)
# ---------------------------------------------------------------------------


def get_seen_item_ids(
    conn: sqlite3.Connection, seller: str | None = None
) -> set[str]:
    """Return the set of seller-scan item_ids already surfaced.

    `seller` is an optional filter; omitted (the default) returns every seen
    item_id, which is what seller_scan.py wants — item_ids are globally unique
    on eBay, so a match surfaced under any seller shouldn't re-appear.
    """
    if seller is not None:
        rows = conn.execute(
            "SELECT item_id FROM seller_scan_seen WHERE seller=?", (seller,)
        ).fetchall()
    else:
        rows = conn.execute("SELECT item_id FROM seller_scan_seen").fetchall()
    return {r["item_id"] for r in rows}


def mark_items_seen(
    conn: sqlite3.Connection, item_ids: list[str], seller: str | None = None
) -> int:
    """Record item_ids as surfaced. Returns the number of newly-inserted rows.

    INSERT OR IGNORE preserves the original first_seen_at (and seller) on a
    re-mark, so the timestamp reflects when a match was *first* shown.
    """
    inserted = 0
    for item_id in item_ids:
        cur = conn.execute(
            "INSERT OR IGNORE INTO seller_scan_seen (item_id, seller) VALUES (?, ?)",
            (item_id, seller),
        )
        inserted += cur.rowcount
    conn.commit()
    return inserted


def remove_seen_for_seller(conn: sqlite3.Connection, seller: str) -> int:
    """Remove every seller-scan seen entry for `seller` (BUI-542 `--forget`).

    Returns the number of rows removed. Deliberately scoped to one seller —
    there is no "forget everyone" call site; a targeted recovery for the
    seller whose output was lost shouldn't have a blast radius covering every
    other seller's seen-set too. Does not touch anything else: in particular
    the BUI-301 rejected-candidate cache is a separate, client-side (JSON
    file) system that this never sees.
    """
    cur = conn.execute(
        "DELETE FROM seller_scan_seen WHERE seller=?", (seller,)
    )
    conn.commit()
    return cur.rowcount


# ---------------------------------------------------------------------------
# Collection-wins seen-tracking (BUI-121)
# ---------------------------------------------------------------------------


def get_collection_wins_seen(conn: sqlite3.Connection) -> set[str]:
    """Return the set of item_ids already recorded into the collection.

    Used by /comic:collection-add to skip WON snipes that were processed in a
    prior run. No seller dimension — every recorded win is globally unique by
    item_id.
    """
    rows = conn.execute("SELECT item_id FROM collection_wins_seen").fetchall()
    return {r["item_id"] for r in rows}


def mark_collection_wins_seen(
    conn: sqlite3.Connection, item_ids: list[str]
) -> int:
    """Record item_ids as processed wins. Returns the number of newly-inserted rows.

    INSERT OR IGNORE preserves the original first_seen_at on a re-mark, so the
    timestamp reflects when a win was *first* processed.
    """
    inserted = 0
    for item_id in item_ids:
        cur = conn.execute(
            "INSERT OR IGNORE INTO collection_wins_seen (item_id) VALUES (?)",
            (item_id,),
        )
        inserted += cur.rowcount
    conn.commit()
    return inserted


# ---------------------------------------------------------------------------
# Rejected-writes ledger (BUI-601)
# ---------------------------------------------------------------------------

# How long a rejection stays queryable. Long enough to cover "I ran the FMV
# refresh some time last week and a book went missing" (the BUI-593 shape),
# short enough that the table stays a diagnostic surface rather than an
# archive. Pruned opportunistically on insert — there is no separate sweeper
# job to forget to run (which would itself be a fails-green instance).
REJECTED_WRITES_RETENTION_DAYS = 30

# A second, independent bound. Retention alone is time-based, so a caller stuck
# in a retry loop could still pile up unboundedly *within* the window — and a
# retry loop against a rejecting endpoint is exactly the traffic this ledger
# attracts. Capping on row id (an INTEGER PRIMARY KEY, so monotonic) keeps the
# newest N and makes the delete an indexed range scan rather than a sort. The
# cap is never allowed to be lossy for the recent past the dashboard reads:
# 5000 rows is orders of magnitude more than the 50 the badge shows or the
# handful a real incident produces.
REJECTED_WRITES_MAX_ROWS = 5000

# Default lookback for the /api/comics/health/rejections badge. A rejection
# older than this is still in the table (see retention above) and reachable by
# passing an explicit `hours`, it just doesn't light the dashboard up forever.
DEFAULT_REJECTIONS_WINDOW_HOURS = 24.0

_REJECTIONS_MAX_LIMIT = 500


def record_rejected_write(
    conn: sqlite3.Connection,
    *,
    method: str,
    path: str,
    status: int,
    query: str | None = None,
    detail: str | None = None,
    payload: str | None = None,
    at: str | None = None,
) -> int:
    """Append one refused mutating request to the ledger. Returns its row id.

    Commit-free by design: the caller owns the transaction (routes.py writes
    through `write_transaction()` under the app-wide write lock, per BUI-408).
    `at` is injectable so tests can pin the clock; production passes None and
    gets `datetime.now(timezone.utc).isoformat()`, matching every other
    timestamp this module writes.
    """
    created_at = at or datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        "INSERT INTO rejected_writes "
        "(created_at, method, path, query, status, detail, payload) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (created_at, method, path, query, int(status), detail, payload),
    )
    _prune_rejected_writes(conn, now=created_at)
    return int(cur.lastrowid or 0)


def _prune_rejected_writes(conn: sqlite3.Connection, *, now: str) -> int:
    """Enforce both ledger bounds — age and row count. Returns rows deleted.

    An unparseable `now` skips only the age sweep rather than raising: pruning
    is housekeeping, and letting it abort the INSERT it rides along with would
    trade a slightly oversized table for a lost rejection — the one thing this
    ledger must not do. The row cap still applies in that case.
    """
    deleted = 0
    try:
        parsed = datetime.fromisoformat(now)
    except (TypeError, ValueError):
        logger.warning("rejected_writes prune skipped: unparseable timestamp %r", now)
    else:
        cutoff = (parsed - timedelta(days=REJECTED_WRITES_RETENTION_DAYS)).isoformat()
        deleted += conn.execute(
            "DELETE FROM rejected_writes WHERE created_at < ?", (cutoff,)
        ).rowcount
    deleted += conn.execute(
        "DELETE FROM rejected_writes WHERE id <= "
        "(SELECT MAX(id) FROM rejected_writes) - ?",
        (REJECTED_WRITES_MAX_ROWS,),
    ).rowcount
    return deleted


def rejected_writes_report(
    conn: sqlite3.Connection,
    *,
    hours: float = DEFAULT_REJECTIONS_WINDOW_HOURS,
    limit: int = 50,
    now: datetime | None = None,
) -> dict[str, Any]:
    """The /api/comics/health/rejections payload: a count plus recent rows.

    `count` is the total in the window; `rejections` is the newest `limit` of
    them. Those are deliberately allowed to disagree — the dashboard badge
    reads `count` (it must not under-report because the row list is capped),
    while a human reads the rows.
    """
    ref = now or datetime.now(timezone.utc)
    since = (ref - timedelta(hours=hours)).isoformat()
    capped = max(1, min(int(limit), _REJECTIONS_MAX_LIMIT))
    count = conn.execute(
        "SELECT COUNT(*) FROM rejected_writes WHERE created_at >= ?", (since,)
    ).fetchone()[0]
    rows = conn.execute(
        "SELECT id, created_at, method, path, query, status, detail, payload "
        "FROM rejected_writes WHERE created_at >= ? "
        "ORDER BY created_at DESC, id DESC LIMIT ?",
        (since, capped),
    ).fetchall()
    return {
        "count": int(count),
        "window_hours": hours,
        "since": since,
        "limit": capped,
        "retention_days": REJECTED_WRITES_RETENTION_DAYS,
        "rejections": [dict(r) for r in rows],
    }


# ---------------------------------------------------------------------------
# Job heartbeats + cadence watchdog (BUI-602)
# ---------------------------------------------------------------------------

# ===========================================================================
# THE CONTRACT TABLE — job -> cadence -> success definition.
#
# This constant IS the design doc for the Silent-Failure Observability
# project (BUI-601/BUI-602); there is no separate plan. Its prose twin lives
# at docs/reference/job-heartbeat-contract.md and is pinned to this dict by
# tests/test_heartbeat_contract_doc.py, so the two cannot drift.
#
# WHY THIS EXISTS: the repo's dominant trap class is "fails green" — a job
# that dies, or silently no-ops, looks exactly like a healthy one. Error-based
# alerting structurally cannot see it, because there is no error. The only
# thing that distinguishes "ran and found nothing" from "did not run" is a
# positive signal emitted on success. That is the heartbeat.
#
# Fields:
#   cadence_hours  Expected interval between successes. A job whose last
#                  success is older than cadence_hours * STALE_FACTOR is
#                  flagged `stale`. Sized to the SLOWEST normal run, not the
#                  average — a watchdog that cries wolf gets muted, and a
#                  muted watchdog is another fails-green instance.
#   success        What counts as a success. Deliberately narrow: a run that
#                  completed but wrote nothing because its input was empty
#                  still pings (it worked); a run that crashed, timed out, or
#                  hard-failed its own exit-code gate must NOT ping.
#   wired          True once the caller actually pings on success. False means
#                  the contract is declared but NOT yet instrumented — such a
#                  job reports `pending_instrumentation`, never `ok`. It is
#                  never silently treated as healthy.
#   ping           The exact call the caller must add to become wired.
# ===========================================================================

# A job is flagged only once it is this many cadences late, so ordinary jitter
# (a scan that starts 20 minutes behind, a laptop asleep past its cron slot)
# does not produce a false alarm.
HEARTBEAT_STALE_FACTOR = 2.0

JOB_CONTRACTS: dict[str, dict[str, Any]] = {
    "gixen-sync": {
        "cadence_hours": 1.0,
        "success": (
            "One completed pass of the comics server's background Gixen "
            "snipe-sync loop (server.main._sync_gixen) that reached its write "
            "phase without raising. GIXEN_SYNC_INTERVAL defaults to 600s, so "
            "a healthy server pings ~6x/hour; the 1h cadence tolerates the "
            "documented flapping/backoff (BUI-562) without alarming."
        ),
        "wired": True,
        "ping": (
            "server.main._sync_gixen, as the last statement INSIDE its apply-"
            "phase write_transaction(), via the on_sync_observed hookspec "
            "(BUI-624) — gixen-cli cannot import this package, so the ping "
            "crosses the boundary outward as a hook, and gixen_overlay.plugin "
            "stores it with record_heartbeat(conn, \"gixen-sync\"). It is the "
            "one job whose ping is NOT an HTTP POST: firing inside the "
            "transaction leaves no post-commit I/O that could turn a healthy "
            "sync into a _sync_loop backoff, and binds the heartbeat to the "
            "fate of the cycle's own writes."
        ),
    },
    "wishlist-sellers": {
        "cadence_hours": 168.0,
        "success": (
            "A /comic:wishlist-sellers run that exited 0. Exit 3 (partial — "
            "some candidates never verified) must NOT ping: the un-verified "
            "books are exactly the ones that would silently stop surfacing. "
            "Zero matching sellers on a clean run IS a success — 'ran and "
            "found nothing' is the case this whole table exists to "
            "distinguish from 'did not run'."
        ),
        "wired": True,
        "ping": (
            ".claude/commands/comic/wishlist-sellers.md, final step, guarded "
            "on exit 0: comics-api POST /api/heartbeat/wishlist-sellers"
        ),
    },
    "collection-sync": {
        "cadence_hours": 336.0,
        "success": (
            "A /comic:collection-sync round-trip that completed its Step 5 "
            "re-import and its Step 6 post-import safety check. An aborted "
            "sync (the "
            "'Deleted from Collection.' probe tripping, the BUI-122 guard) "
            "must NOT ping — an abort is the sync working correctly but NOT "
            "having synced. Manual/user-invoked, hence the long cadence; the "
            "signal being watched for is 'I have not synced in a month and "
            "wins are piling up unpushed', not a missed tick."
        ),
        "wired": True,
        "ping": (
            ".claude/commands/comic/collection-sync.md, after the Step 5 "
            "re-import reconciles and the Step 6 post-import safety check "
            "passes: comics-api POST /api/heartbeat/collection-sync"
        ),
    },
    "fmv-refresh": {
        "cadence_hours": 168.0,
        "success": (
            "A comic-fmv batch that fetched sold comps AND persisted them — "
            "the write must be confirmed, not just attempted. BUI-593 is "
            "precisely a run where the fetch succeeded and the write 422'd, "
            "so 'comic-fmv exited 0' alone is NOT the success definition; the "
            "upsert response must have been accepted. Pairs with the BUI-601 "
            "ledger: the heartbeat says the refresh ran, the ledger says what "
            "it failed to store."
        ),
        "wired": True,
        "ping": (
            "apps/fmv/src/fmv_runner.py's run(), once per batch, gated on at "
            "least one /api/comics upsert having returned 2xx (a persisted row "
            "carries a non-null fmv_id): POST /api/heartbeat/fmv-refresh. A "
            "batch that fetch-erred, 422'd, or came entirely from cache "
            "refreshed nothing and does NOT ping."
        ),
    },
    "sentinel-probe": {
        "cadence_hours": 168.0,
        "success": (
            "A `comic-fmv --sentinel-probe` run (BUI-603) in which every "
            "sentinel book AND the negative control passed — exit 0. This one "
            "is deliberately STRICTER than the others: exit 1 means the probe "
            "ran fine and found the comp pipeline miscalibrated, and it "
            "already alarms through its own exit code, so re-reporting it as a "
            "healthy run would be the fails-green shape one layer up. The "
            "consequence is intended: a persistently failing probe eventually "
            "also goes stale here, which is a second, louder alarm about the "
            "same fact and never a quieter one. Exit 2 (the probe could not "
            "complete) must not ping either."
        ),
        "wired": True,
        "ping": (
            "apps/fmv/src/sentinel_probe.py's _ping_heartbeat, called from "
            "run_sentinel_probe only on the all-pass branch: POST "
            "/api/heartbeat/sentinel-probe. Best-effort — a failed ping never "
            "changes the probe's own exit code, which is the primary alert "
            "surface. Scheduling: docs/reference/sentinel-probe-scheduling.md "
            "(weekly — each run spends real provider request budget)."
        ),
    },
}

# THE OUTER LAYER — NOT WIRED. Read this before trusting the watchdog.
#
# Everything above is a PULL: something must ask
# GET /api/comics/health/heartbeats for a stale job to be noticed. If the
# comics server is down, or the Mac Mini is asleep, or launchd never restarted
# the process, nobody asks — and the watchdog fails green in exactly the way
# it was built to prevent. A watchdog with no outer ping is its own worst bug
# class.
#
# Closing it needs a check OUTSIDE this machine (healthchecks.io, an uptime
# pinger, a cloud /schedule agent) that polls the endpoint on a schedule and
# alarms on a non-200 OR on any job not `ok`. Until that exists, the endpoint
# reports this gap in its own response (`outer_ping: "unwired"`) rather than
# implying a health it cannot vouch for. See
# docs/reference/job-heartbeat-contract.md for the wiring recipe.
#
# BUI-624 wired all five jobs above and deliberately left this at "unwired".
# Creating the external monitor is an ops action, not a code change, and the
# flag describes the deployed world, not this repo's intent: flipping it to
# "wired" on a monitor that does not exist would be the exact lie the whole
# project exists to close. Flip it in the same change that creates the check —
# which is now unblocked, since `healthy` can finally reach True.
HEARTBEAT_OUTER_PING_STATE = "unwired"

# Watchdog verdicts, worst-first. `stale` and `never` are both actionable;
# `pending_instrumentation` is a declared-but-unbuilt contract and is NOT a
# claim of health.
HEARTBEAT_STATUS_OK = "ok"
HEARTBEAT_STATUS_STALE = "stale"
HEARTBEAT_STATUS_NEVER = "never"
HEARTBEAT_STATUS_PENDING = "pending_instrumentation"


def record_heartbeat(
    conn: sqlite3.Connection,
    job: str,
    *,
    detail: str | None = None,
    at: str | None = None,
) -> dict[str, Any]:
    """Record a successful run of `job`. Returns the stored row.

    Upsert on the job name: one row per job, so the table never grows. The
    ON CONFLICT branch keeps `success_count` monotonic, which is what tells
    "pinged once during setup and never again" apart from "pinging steadily".

    Commit-free — the caller owns the transaction (see record_rejected_write).
    Unknown job names are accepted at this layer on purpose; the endpoint is
    where the JOB_CONTRACTS allow-list is enforced, so this stays a plain
    storage primitive.
    """
    ts = at or datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO heartbeats (job, last_success_at, detail, success_count) "
        "VALUES (?, ?, ?, 1) "
        "ON CONFLICT(job) DO UPDATE SET "
        "  last_success_at = excluded.last_success_at, "
        "  detail = excluded.detail, "
        "  success_count = heartbeats.success_count + 1",
        (job, ts, detail),
    )
    row = conn.execute(
        "SELECT job, last_success_at, detail, success_count FROM heartbeats WHERE job=?",
        (job,),
    ).fetchone()
    return dict(row)


def _heartbeat_age_hours(last_success_at: str, ref: datetime) -> float | None:
    """Hours since `last_success_at`, or None if the stored value is unparseable.

    A corrupt timestamp must not read as "recent" — callers treat None as
    unknown-and-therefore-not-ok, never as fresh.
    """
    try:
        seen = datetime.fromisoformat(last_success_at)
    except (TypeError, ValueError):
        return None
    if seen.tzinfo is None:
        seen = seen.replace(tzinfo=timezone.utc)
    return (ref - seen).total_seconds() / 3600.0


def heartbeat_report(
    conn: sqlite3.Connection,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """The watchdog verdict for every job in the contract table.

    Iterates JOB_CONTRACTS, not the heartbeats table — a job that has NEVER
    pinged is the single most important thing this can report, and iterating
    stored rows would render it as absence (i.e. as nothing wrong at all).
    Heartbeat rows for jobs not in the contract are surfaced separately under
    `unknown_jobs` rather than dropped, so a typo'd ping is visible instead of
    looking like a job that never ran.
    """
    ref = now or datetime.now(timezone.utc)
    stored = {
        r["job"]: r
        for r in conn.execute(
            "SELECT job, last_success_at, detail, success_count FROM heartbeats"
        ).fetchall()
    }

    jobs: list[dict[str, Any]] = []
    for name, contract in JOB_CONTRACTS.items():
        row = stored.pop(name, None)
        cadence = float(contract["cadence_hours"])
        entry: dict[str, Any] = {
            "job": name,
            "cadence_hours": cadence,
            "stale_after_hours": cadence * HEARTBEAT_STALE_FACTOR,
            "success": contract["success"],
            "wired": bool(contract["wired"]),
            "last_success_at": row["last_success_at"] if row else None,
            "success_count": row["success_count"] if row else 0,
            "age_hours": None,
        }
        if row is None:
            # Never pinged. If nobody has wired the caller yet that is expected
            # and reported as such; if the caller IS wired, silence is the
            # alarm.
            entry["status"] = (
                HEARTBEAT_STATUS_NEVER
                if contract["wired"]
                else HEARTBEAT_STATUS_PENDING
            )
            entry["ping"] = contract["ping"]
        else:
            age = _heartbeat_age_hours(row["last_success_at"], ref)
            entry["age_hours"] = None if age is None else round(age, 3)
            if age is None or age > cadence * HEARTBEAT_STALE_FACTOR:
                entry["status"] = HEARTBEAT_STATUS_STALE
            else:
                entry["status"] = HEARTBEAT_STATUS_OK
        jobs.append(entry)

    stale = [j["job"] for j in jobs if j["status"] == HEARTBEAT_STATUS_STALE]
    never = [j["job"] for j in jobs if j["status"] == HEARTBEAT_STATUS_NEVER]
    pending = [j["job"] for j in jobs if j["status"] == HEARTBEAT_STATUS_PENDING]
    return {
        # `healthy` means "every job in the contract is verified to be
        # running" — so an uninstrumented job makes it False, exactly like a
        # stale one. That is the honest answer and it matters: the outer-ping
        # recipe in docs/reference/job-heartbeat-contract.md alarms on
        # `healthy == false`, and a version that ignored
        # pending_instrumentation would report a clean bill of health for a
        # system observing nothing at all — this project's own bug class,
        # reintroduced one layer up. Consumers wanting the narrower question
        # ("is anything I AM watching broken?") read stale_jobs +
        # never_seen_jobs directly.
        "healthy": not stale and not never and not pending,
        "stale_jobs": stale,
        "never_seen_jobs": never,
        "pending_instrumentation_jobs": pending,
        "checked_at": ref.isoformat(),
        "stale_factor": HEARTBEAT_STALE_FACTOR,
        # The endpoint declares its own blind spot — see
        # HEARTBEAT_OUTER_PING_STATE above.
        "outer_ping": HEARTBEAT_OUTER_PING_STATE,
        "jobs": jobs,
        "unknown_jobs": sorted(stored),
    }
