from __future__ import annotations

import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

def resolve_server_dir() -> Path:
    """Resolve the comics-server data dir with a safe fallback (BUI-220).

    The canonical default is ``~/.comics-server`` (this is the comics server,
    not the Gixen bidding service). But the live Mac Mini still boots from the
    legacy ``~/.gixen-server`` until that data is physically moved, so:

      1. ``~/.comics-server`` if it exists (post-migration / fresh installs), else
      2. ``~/.gixen-server`` if it exists (the live server keeps working), else
      3. ``~/.comics-server`` (the canonical default for a clean machine).

    This makes the rename safe to merge without Mac Mini access — nothing boots
    from an empty dir.
    """
    new = Path.home() / ".comics-server"
    legacy = Path.home() / ".gixen-server"
    if new.exists():
        return new
    if legacy.exists():
        return legacy
    return new


DB_PATH = resolve_server_dir() / "db.sqlite"

# Soft-delete tombstone status values (BUI-49 renamed PURGED -> REMOVED). Both
# are tolerated in queries so gixen-cli and gixen-overlay stay correct across
# package version skew (BUI-272: centralizes the ~13 hand-typed occurrences).
# This is a bare SQL value list, not a parenthesized tuple, so callers compose
# it into whatever IN/NOT IN clause shape they need, e.g.
# f"status NOT IN ({TOMBSTONE_STATUSES_SQL})" or, alongside other values,
# f"status NOT IN ('PENDING', {TOMBSTONE_STATUSES_SQL})".
TOMBSTONE_STATUSES_SQL = "'PURGED', 'REMOVED'"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS bids (
    id              INTEGER PRIMARY KEY,
    item_id         TEXT NOT NULL,
    comic_id        INTEGER,
    max_bid         REAL NOT NULL,
    bid_offset      INTEGER DEFAULT 6,
    snipe_group     INTEGER DEFAULT 0,
    status          TEXT DEFAULT 'PENDING' CHECK(status IN ('PENDING','WON','LOST','FAILED','ENDED','PURGED','REMOVED')),
    winning_bid     REAL,
    seller          TEXT,
    auction_end_at      TEXT,
    local_snipe_at      TEXT,
    local_snipe_result  TEXT,
    notes               TEXT,
    added_at            TEXT DEFAULT (datetime('now')),
    resolved_at         TEXT
);

CREATE INDEX IF NOT EXISTS idx_bids_item_id ON bids(item_id);

-- BUI-381: append-only ledger of bid-group wins, written the moment a WON is
-- classified. The BUI-371 group-cancel evidence used to live only in the WON
-- bids row, which is destructible — a completed-bids sweep (mark_bids_purged)
-- tombstones it to REMOVED, and a winner first seen already-terminal via the
-- web-add path never gets a row at all. Either way _group_won_before's
-- live-row query found nothing and the cancelled siblings fell through to the
-- eBay fallback's phantom-WON window. Nothing tombstones or deletes rows
-- here; the classifier applies the same lifetime/margin bounds to this ledger
-- as to live WON rows. won_end_at is NOT NULL by design: end-less evidence
-- cannot be bounded against a sibling's lifetime (recording an
-- observation-time proxy could falsely group-cancel a sibling added after
-- the real win — the recycled-group hazard from the BUI-371 review).
--
-- BUI-385: `source` (added via _COLUMN_MIGRATIONS) tags which writer recorded
-- a row — a closed vocabulary (GROUP_WIN_SOURCES) exposed over /api/group-wins
-- for forensics ("which win classified this row REMOVED"). The unique index
-- keys on (snipe_group, item_id, won_end_at), NOT (snipe_group, item_id): a
-- genuine re-listed re-win of the same eBay id in the same group ends at a
-- DISTINCT time and records a second, equally-genuine entry, where the old
-- 2-col key collapsed it to the first win (a WON-permissive evidence miss for
-- recycled group numbers). Every stored end is still a genuine auction end
-- (only record_group_win's guards write here, never an observation-time
-- proxy), so a distinct-end second row is real cancel evidence, not a
-- false-REMOVED double-count (_group_won_before is a boolean over ends,
-- dup-insensitive). The index itself is created/re-keyed in _apply_migrations
-- (single-sourced there so a fresh DB and a legacy 2-col DB converge on one
-- definition — the _BIDS_TABLE_SQL precedent).
CREATE TABLE IF NOT EXISTS group_wins (
    id          INTEGER PRIMARY KEY,
    snipe_group INTEGER NOT NULL,
    item_id     TEXT NOT NULL,
    won_end_at  TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);
"""

# BUI-385: the group_wins unique index, defined once. _apply_migrations creates
# it (and drops the legacy 2-col idx_group_wins_group_item) on every startup, so
# both a fresh DB and an already-populated pre-BUI-385 DB converge here.
_GROUP_WINS_UNIQUE_INDEX_SQL = (
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_group_wins_group_item_end "
    "ON group_wins(snipe_group, item_id, won_end_at)"
)


# BUI-385: closed vocabulary of group_wins.source provenance tags. Keeping it a
# frozenset (not free-form strings) is what makes the /api/group-wins forensics
# surface trustworthy — a typo'd tag would be caught by the writers' tests, and
# no writer may land a NULL source (record_group_win defaults to the primary
# writer; the startup backfill tags its own rows; pre-column rows are stamped
# LEGACY). Values are hyphenated to read cleanly in the JSON endpoint.
GROUP_WIN_SOURCE_STATUS_TRANSITION = "status-transition"  # update_bid_status WON
GROUP_WIN_SOURCE_STARTUP_BACKFILL = "startup-backfill"    # _apply_migrations seed
GROUP_WIN_SOURCE_LISTED_WIN = "listed-win"                # _apply_listed_win_evidence (BUI-410; was _record_listed_win_evidence)
GROUP_WIN_SOURCE_LEGACY = "legacy"                        # pre-BUI-385 rows
GROUP_WIN_SOURCES = frozenset({
    GROUP_WIN_SOURCE_STATUS_TRANSITION,
    GROUP_WIN_SOURCE_STARTUP_BACKFILL,
    GROUP_WIN_SOURCE_LISTED_WIN,
    GROUP_WIN_SOURCE_LEGACY,
})

# BUI-385 — retraction (WON reversal) deliberately NOT implemented. The ticket
# paired re-win collapse with "a WON reversal has no signal into the ledger",
# but no code path reverses a WON: the eBay fallback selects only
# PENDING/ENDED/tombstone rows (never WON — see _ebay_fallback_rows), and Gixen
# keeps a completed auction WON, so a re-sync only re-classifies WON→WON
# (idempotent under the unique index). A retraction mechanism would guard a
# transition the system never makes (YAGNI), and eagerly DELETING a genuine win
# on a spurious re-classification would itself weaken real cancel evidence. The
# `source` + `recorded_at` provenance above makes any future retraction need
# diagnosable — the actual gap the BUI-381 P3 review was reaching for. If a
# real WON→non-WON path is ever introduced, revisit with a superseded flag
# (append-only, forensics-preserving) rather than a DELETE.


_COLUMN_MIGRATIONS = [
    # bids columns added since the original schema
    "ALTER TABLE bids ADD COLUMN ebay_title TEXT",
    "ALTER TABLE bids ADD COLUMN status_mirror TEXT",
    "ALTER TABLE bids ADD COLUMN cached_current_bid TEXT",
    "ALTER TABLE bids ADD COLUMN cached_at TEXT",
    "ALTER TABLE bids ADD COLUMN local_snipe_at TEXT",
    "ALTER TABLE bids ADD COLUMN local_snipe_result TEXT",
    # Plain INTEGER (no FK) so gixen-cli starts cleanly without the plugin.
    # The plugin reads/writes this column when present.
    "ALTER TABLE bids ADD COLUMN fmv_id INTEGER",
    # BUI-78: seller-stated and photo-assessed grades per snipe, for seller
    # reliability analytics. Both nullable CGC floats; written by the buy flow.
    "ALTER TABLE bids ADD COLUMN seller_grade REAL",
    "ALTER TABLE bids ADD COLUMN photo_grade REAL",
    # BUI-116: Gixen's internal row id for the snipe. Cached during sync so
    # modify/remove can POST directly without a list_snipes() lookup. Nullable —
    # existing/web-added rows start NULL (cache miss -> list fallback) until the
    # next sync fills them.
    "ALTER TABLE bids ADD COLUMN dbidid TEXT",
    # BUI-371: when a PENDING snipe was first observed missing from a healthy
    # (non-empty) Gixen list. Cleared if the snipe reappears. A vanish stamped
    # well before auction_end_at is positive evidence the snipe was cancelled
    # (user removal or bid-group auto-cancel) rather than executed — the
    # vanish-time disambiguation BUI-146 sanctioned instead of gating the eBay
    # WON inference.
    "ALTER TABLE bids ADD COLUMN gixen_vanished_at TEXT",
    # BUI-382: stamped by _run_ebay_fallback when eBay returned data for this
    # row but no usable positive price (reserve not met / unsold) — a
    # definitive answer that will not change on a later check. Excludes the
    # row from _ebay_fallback_rows' re-scan set so an already-answered
    # auction doesn't keep burning an eBay call every sync: forever for a
    # non-purged ENDED row (that branch has no time bound at all), or on
    # every sync within the 7-day window for a REMOVED/PURGED tombstone.
    "ALTER TABLE bids ADD COLUMN ebay_no_price_at TEXT",
    # BUI-384: when this row's snipe_group last CHANGED (the edit path via
    # update_bid, or the BUI-381 sync mirror via refresh_snipe_group). NULL
    # while the group has never changed since insert — added_at already
    # bounds membership then. _group_won_before bounds its evidence window
    # by max(added_at, group_changed_at): a snipe joined to a group AFTER
    # that group's win must not be classified REMOVED off a win that
    # predates its membership (the win postdates added_at but predates the
    # join — the late-join backdating false-REMOVED).
    "ALTER TABLE bids ADD COLUMN group_changed_at TEXT",
    # BUI-385: provenance tag on the group_wins ledger (which writer recorded
    # a row) — a GROUP_WIN_SOURCES value, exposed over /api/group-wins for
    # forensics. Nullable on ADD; _apply_migrations stamps pre-column rows
    # LEGACY and every writer tags its own rows so no NULL source persists.
    "ALTER TABLE group_wins ADD COLUMN source TEXT",
]


# The full current bids schema. Both table rebuilds in _apply_migrations (FK
# removal and the PURGED->REMOVED CHECK widen) converge on this exact shape, so
# it lives in one place rather than being duplicated per rebuild.
_BIDS_TABLE_SQL = """
    CREATE TABLE bids (
        id              INTEGER PRIMARY KEY,
        item_id         TEXT NOT NULL,
        comic_id        INTEGER,
        max_bid         REAL NOT NULL,
        bid_offset      INTEGER DEFAULT 6,
        snipe_group     INTEGER DEFAULT 0,
        status          TEXT DEFAULT 'PENDING' CHECK(status IN ('PENDING','WON','LOST','FAILED','ENDED','PURGED','REMOVED')),
        winning_bid     REAL,
        seller          TEXT,
        auction_end_at      TEXT,
        local_snipe_at      TEXT,
        local_snipe_result  TEXT,
        notes               TEXT,
        added_at            TEXT DEFAULT (datetime('now')),
        resolved_at         TEXT,
        ebay_title          TEXT,
        status_mirror       TEXT,
        cached_current_bid  TEXT,
        cached_at           TEXT,
        fmv_id              INTEGER,
        seller_grade        REAL,
        photo_grade         REAL,
        dbidid              TEXT,
        gixen_vanished_at   TEXT,
        ebay_no_price_at    TEXT,
        group_changed_at    TEXT
    )
"""


def _rebuild_bids_table(
    conn: sqlite3.Connection, temp_name: str, savepoint_name: str
) -> None:
    """Rebuild the bids table (RENAME -> CREATE -> copy rows -> DROP) while
    preserving the overlay's bid_fmvs FK child across the rename.

    SQLite 3.26+ rewrites FK references on RENAME, so bid_fmvs.bid_id REFERENCES
    bids(id) would silently become REFERENCES <temp_name>(id) and then dangle
    once the temp table is dropped — every later INSERT INTO bid_fmvs then fails
    with "no such table" (BUI-79). The fix mirrors the overlay's
    _migrate_year_nullable: save bid_fmvs (its CREATE SQL + rows) to Python
    memory and drop it *before* the rename, so there is no FK for SQLite to
    rewrite, then recreate it from the saved SQL (which still references bids)
    and restore the rows.

    bid_fmvs is owned by the gixen-overlay plugin, so it is absent when
    gixen-cli runs standalone — preserved only when present. The bids INSERT
    copies EVERY column (introspected from the renamed table) verbatim, so no
    column is silently dropped (the BUI-64 fmv_id-drop trap). Raw conn.execute
    only — no CRUD helpers (they commit() and would collapse the savepoint).
    PRAGMA foreign_keys must change outside any transaction, so it brackets the
    SAVEPOINT.
    """
    conn.execute(f"DROP TABLE IF EXISTS {temp_name}")
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute(f"SAVEPOINT {savepoint_name}")
    try:
        bid_fmvs_sql_row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='bid_fmvs'"
        ).fetchone()
        saved_bid_fmvs = None
        if bid_fmvs_sql_row:
            saved_bid_fmvs = conn.execute(
                "SELECT bid_id, fmv_id, is_primary FROM bid_fmvs"
            ).fetchall()
            conn.execute("DROP TABLE bid_fmvs")

        conn.execute(f"ALTER TABLE bids RENAME TO {temp_name}")
        conn.execute(_BIDS_TABLE_SQL)
        cols = ", ".join(
            row[1] for row in conn.execute(f"PRAGMA table_info({temp_name})")
        )
        conn.execute(f"INSERT INTO bids ({cols}) SELECT {cols} FROM {temp_name}")
        conn.execute(f"DROP TABLE {temp_name}")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_bids_item_id ON bids(item_id)")

        if bid_fmvs_sql_row:
            conn.execute(bid_fmvs_sql_row["sql"])
            for bf in saved_bid_fmvs:
                conn.execute(
                    "INSERT OR IGNORE INTO bid_fmvs (bid_id, fmv_id, is_primary) "
                    "VALUES (?, ?, ?)",
                    (bf["bid_id"], bf["fmv_id"], bf["is_primary"]),
                )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_bid_fmvs_bid ON bid_fmvs(bid_id)"
            )
        conn.execute(f"RELEASE {savepoint_name}")
    except Exception:  # noqa: BLE001  # migration failure — rollback savepoint, then re-raise
        try:
            conn.execute(f"ROLLBACK TO {savepoint_name}")
        except Exception:  # noqa: BLE001  # rollback itself may fail; suppress, re-raise original
            pass
        raise
    finally:
        conn.execute("PRAGMA foreign_keys=ON")


def _repair_bid_fmvs_fk(conn: sqlite3.Connection) -> None:
    """Heal a bid_fmvs table whose bids FK was left dangling by a pre-fix bids
    rename (BUI-79).

    The BUI-49 PURGED->REMOVED rebuild renamed bids without preserving bid_fmvs,
    so SQLite 3.26+ rewrote bid_fmvs.bid_id to REFERENCES the temp table; when
    that temp table was dropped the FK pointed at a missing table and every
    INSERT INTO bid_fmvs failed with "no such table". This rebuilds bid_fmvs
    from its own CREATE SQL with the dangling table name rewritten back to bids,
    preserving all rows.

    No-op when bid_fmvs is absent (gixen-cli standalone) or all its FK targets
    resolve (healthy DB), so it is safe to run on every startup. Raw
    conn.execute only; PRAGMA foreign_keys brackets the SAVEPOINT.
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='bid_fmvs'"
    ).fetchone()
    if row is None:
        return
    existing = {
        r["name"]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    dangling = [
        fk["table"]
        for fk in conn.execute("PRAGMA foreign_key_list(bid_fmvs)")
        if fk["table"] not in existing
    ]
    if not dangling:
        return

    fixed_sql = row["sql"]
    for bad in dangling:
        # Rewrite the dangling reference (SQLite quotes the rewritten name, e.g.
        # REFERENCES "bids_status_rename_old"(id)) back to bids. Match the
        # optionally double-quoted whole identifier so a column merely prefixed
        # with the bad name is never touched.
        fixed_sql = re.sub(rf'"?{re.escape(bad)}"?', "bids", fixed_sql)

    saved = conn.execute(
        "SELECT bid_id, fmv_id, is_primary FROM bid_fmvs"
    ).fetchall()
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("SAVEPOINT bui79_repair")
    try:
        conn.execute("DROP TABLE bid_fmvs")
        conn.execute(fixed_sql)
        for bf in saved:
            conn.execute(
                "INSERT OR IGNORE INTO bid_fmvs (bid_id, fmv_id, is_primary) "
                "VALUES (?, ?, ?)",
                (bf["bid_id"], bf["fmv_id"], bf["is_primary"]),
            )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_bid_fmvs_bid ON bid_fmvs(bid_id)"
        )
        conn.execute("RELEASE bui79_repair")
    except Exception:  # noqa: BLE001  # migration failure — rollback savepoint, then re-raise
        try:
            conn.execute("ROLLBACK TO bui79_repair")
        except Exception:  # noqa: BLE001  # rollback itself may fail; suppress, re-raise original
            pass
        raise
    finally:
        conn.execute("PRAGMA foreign_keys=ON")
    conn.commit()


def _apply_migrations(conn: sqlite3.Connection) -> None:
    for stmt in _COLUMN_MIGRATIONS:
        try:
            conn.execute(stmt)
            conn.commit()
        except sqlite3.OperationalError as e:
            # Idempotent column adds: ignore "duplicate column name". Anything
            # else (disk full, locked DB, syntax error in a future migration)
            # should not be silently swallowed.
            if "duplicate column" not in str(e).lower():
                raise

    # Heal any bid_fmvs whose bids FK was left dangling by a pre-fix bids rename
    # (BUI-79). Must run before the rename rebuilds below, which preserve
    # bid_fmvs by saving its CREATE SQL verbatim — repairing first ensures that
    # saved SQL references bids, not a dropped temp table.
    _repair_bid_fmvs_fk(conn)

    # Remove the FK on bids.comic_id for existing databases that were created
    # before this refactor. SQLite has no ALTER TABLE DROP CONSTRAINT, so we
    # must rebuild the table. PRAGMA foreign_keys cannot be changed inside an
    # active transaction — it must precede any BEGIN/SAVEPOINT.
    fk_rows = conn.execute("PRAGMA foreign_key_list(bids)").fetchall()
    if any(row["table"] == "comics" for row in fk_rows):
        _rebuild_bids_table(conn, "bids_old", "fk_rebuild")

    # Rename the soft-delete tombstone status PURGED -> REMOVED (BUI-49): widen
    # the CHECK to *allow* REMOVED, then (below) remap existing data. SQLite
    # can't ALTER a CHECK constraint, so widening the allowed-status set requires
    # a table rebuild (same pattern as the FK removal above). Idempotency is by
    # feature detection: only rebuild while the live CHECK still lacks REMOVED.
    table_sql_row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='bids'"
    ).fetchone()
    if table_sql_row and "REMOVED" not in (table_sql_row["sql"] or ""):
        _rebuild_bids_table(conn, "bids_status_rename_old", "status_rename")

    # (2) Remap the tombstone value. Runs in all cases — whether the CHECK was
    # just widened above, or already allowed REMOVED on a fresh / FK-rebuilt DB
    # that still held legacy PURGED rows. Idempotent: matches 0 rows once done.
    conn.execute("UPDATE bids SET status='REMOVED' WHERE status='PURGED'")
    conn.commit()

    # BUI-83: backfill auction_end_at from resolved_at for legacy resolved rows.
    # update_bid_status now does `auction_end_at=COALESCE(auction_end_at, ?)` at
    # resolution time (the 2b7484a fix), but rows that resolved *before* that
    # landed kept auction_end_at NULL. With no end date they fall out of both
    # /api/comics/snipes (filtered as terminal) and the 7-day history window once
    # resolved_at ages past the fallback branch — rendering in neither table. A
    # resolved auction's end time is its resolved_at, so backfill it. Excludes the
    # soft-delete tombstone (PURGED/REMOVED): its resolved_at is the removal time,
    # not an auction end. Idempotent: matches 0 rows once every resolved row has
    # an end date.
    conn.execute(
        "UPDATE bids SET auction_end_at = resolved_at "
        "WHERE auction_end_at IS NULL AND resolved_at IS NOT NULL "
        f"AND status NOT IN ({TOMBSTONE_STATUSES_SQL})"
    )
    conn.commit()

    # BUI-385: re-key the ledger's unique index from (snipe_group, item_id) to
    # (snipe_group, item_id, won_end_at), so a genuine re-listed re-win at a
    # distinct end can record a second entry instead of being collapsed to the
    # first (see the group_wins schema comment). This is the sole creation site
    # for both fresh and existing DBs. DROP IF EXISTS + CREATE IF NOT EXISTS is
    # idempotent: the explicit DROP of the OLD name is what forces the re-key
    # (a bare `CREATE UNIQUE INDEX IF NOT EXISTS` under a changed column list
    # would silently no-op — the SQLite idempotent-by-NAME trap). The old 2-col
    # index guaranteed (group, item) uniqueness, so (group, item, won_end_at)
    # is trivially unique over existing rows — the new index can never fail to
    # build. On a fresh DB the DROP is a no-op.
    #
    # Forward-only, like this file's other migrations (the PURGED->REMOVED
    # rename is equally non-reversible). Rollback caveat: once a genuine re-win
    # has recorded two rows sharing (group, item), reverting to pre-BUI-385
    # code — whose _SCHEMA rebuilds the strict 2-col index under the now-absent
    # old name — fails to start until an operator collapses the duplicates:
    #   DELETE FROM group_wins WHERE id NOT IN
    #     (SELECT MIN(id) FROM group_wins GROUP BY snipe_group, item_id);
    conn.execute("DROP INDEX IF EXISTS idx_group_wins_group_item")
    conn.execute(_GROUP_WINS_UNIQUE_INDEX_SQL)
    conn.commit()

    # BUI-381: seed the durable group-win ledger from WON rows that predate
    # recording-at-classification-time (or were written by an older package
    # version — the usual version-skew tolerance). Runs every startup; the
    # (snipe_group, item_id, won_end_at) unique index + INSERT OR IGNORE make
    # it a no-op once seeded. Only genuine auction ends are seeded — the ledger
    # never stores an observation-time proxy (see the group_wins schema
    # comment). `auction_end_at != resolved_at` excludes the two identifiable
    # proxy shapes: update_bid_status's COALESCE fill at resolution time and
    # the BUI-83 legacy backfill, both of which set auction_end_at :=
    # resolved_at verbatim. Excluded rows keep serving proxy evidence via the
    # live-row arm of _group_won_before until purged (shipped BUI-371
    # behavior); after a purge their evidence is lost — WON-permissive. BUI-385
    # tags these rows GROUP_WIN_SOURCE_STARTUP_BACKFILL for the forensics
    # surface.
    conn.execute(
        "INSERT OR IGNORE INTO group_wins "
        "(snipe_group, item_id, won_end_at, recorded_at, source) "
        "SELECT snipe_group, item_id, auction_end_at, ?, ? "
        "FROM bids WHERE status='WON' AND snipe_group != 0 "
        "AND auction_end_at IS NOT NULL "
        "AND (resolved_at IS NULL OR auction_end_at != resolved_at)",
        (datetime.now(timezone.utc).isoformat(), GROUP_WIN_SOURCE_STARTUP_BACKFILL),
    )
    conn.commit()

    # BUI-385: stamp any ledger row still missing a provenance tag — rows
    # written by update_bid_status / the listed-win evidence path before the
    # source column existed — as LEGACY. They can't be attributed to a specific
    # writer retroactively. Idempotent: matches 0 rows once every row is tagged
    # (the backfill INSERT above always sets source, so this only ever catches
    # pre-column rows). Runs after the backfill so a WON row that both predates
    # the column and matches a current WON row stays LEGACY, not re-tagged.
    conn.execute(
        "UPDATE group_wins SET source=? WHERE source IS NULL",
        (GROUP_WIN_SOURCE_LEGACY,),
    )
    conn.commit()

    # Enforce at most one live (PENDING) snipe per item_id (BUI-67). Runs last,
    # after the PURGED->REMOVED remap so the CHECK already permits the REMOVED
    # tombstone this writes on dedup losers.
    _dedup_pending_and_index(conn)


# Fields carried forward from a collapsed duplicate onto its survivor, so no
# auction-tracking data or cached state is lost (BUI-67 KTD4). max_bid is merged
# separately (MAX, not freshest) so a stale clone can never lower the ceiling.
_DEDUP_FILL_FIELDS = (
    "auction_end_at", "fmv_id", "local_snipe_at", "local_snipe_result",
    "seller", "cached_current_bid", "cached_at",
)

# Marker on a dedup-loser tombstone, distinguishing it from user-cancel /
# completed-sweep tombstones (BUI-67). Written here and read by the server's
# eBay-fallback exclusion — one constant so the writer and filter can't drift.
DEDUP_TOMBSTONE_NOTE = "deduped BUI-67"

# Marker on a tombstone written by the BUI-371 cancelled-before-end
# classification (vanish-time / group-win evidence), so those REMOVED rows can
# be told apart from user-cancel (delete_bid) and completed-sweep
# (mark_bids_purged) tombstones in a post-hoc audit — same convention as
# DEDUP_TOMBSTONE_NOTE. Unlike the dedup note, these rows are NOT excluded
# from the eBay-fallback tombstone branch: their auctions really ended, so the
# final price is still worth backfilling for history.
CANCELLED_TOMBSTONE_NOTE = "cancelled before end BUI-371"
_PENDING_UNIQUE_INDEX = "idx_bids_pending_item_id"


def _dedup_pending_and_index(conn: sqlite3.Connection) -> None:
    """Collapse pre-existing same-item PENDING duplicates, then add the partial
    unique index that prevents new ones (BUI-67).

    Collapse keeps the MAX(id) row as survivor (the row consumers treat as
    "live": get_bid_by_item_id, the overlay history MAX(id) dedup, link-fmv),
    forward-filling each live-snipe field from the freshest (highest cached_at)
    contributing row — auction_end_at can diverge across rows by sync drift, so
    a blind keep-survivor could fire the sniper at the wrong second. Losers are
    tombstoned REMOVED with the DEDUP_TOMBSTONE_NOTE marker.

    Raw conn.execute only — no CRUD helpers (they commit() and would collapse the
    savepoint). Collapse strictly precedes CREATE UNIQUE INDEX: building it over
    un-collapsed dups fails, and DDL may implicitly commit the collapse first, so
    the order is load-bearing (KTD5).
    """
    # Once the index exists the migration is provably complete — it makes new
    # PENDING duplicates impossible, so there is nothing left to collapse. Skip
    # the table scan + write transaction on every subsequent server start.
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?",
        (_PENDING_UNIQUE_INDEX,),
    ).fetchone():
        return

    dup_item_ids = [
        r["item_id"] for r in conn.execute(
            "SELECT item_id FROM bids WHERE status='PENDING' "
            "GROUP BY item_id HAVING COUNT(*) > 1"
        )
    ]

    set_clause = ", ".join(f"{f}=?" for f in _DEDUP_FILL_FIELDS)
    conn.execute("SAVEPOINT bui67_dedup")
    try:
        if dup_item_ids:
            now = datetime.now(timezone.utc).isoformat()
            for item_id in dup_item_ids:
                rows = conn.execute(
                    "SELECT * FROM bids WHERE item_id=? AND status='PENDING'",
                    (item_id,),
                ).fetchall()
                survivor_id = max(r["id"] for r in rows)
                # Freshest first: non-NULL cached_at outranks NULL, then later
                # cached_at (ISO strings sort chronologically) outranks earlier.
                ordered = sorted(
                    rows,
                    key=lambda r: (r["cached_at"] is not None, r["cached_at"] or ""),
                    reverse=True,
                )
                merged = {
                    field: next(
                        (r[field] for r in ordered if r[field] is not None), None
                    )
                    for field in _DEDUP_FILL_FIELDS
                }
                merged_max_bid = max(r["max_bid"] for r in rows)
                conn.execute(
                    f"UPDATE bids SET {set_clause}, max_bid=? WHERE id=?",
                    [merged[f] for f in _DEDUP_FILL_FIELDS] + [merged_max_bid, survivor_id],
                )
                conn.execute(
                    "UPDATE bids SET status='REMOVED', resolved_at=?, notes=? "
                    "WHERE item_id=? AND status='PENDING' AND id<>?",
                    (now, DEDUP_TOMBSTONE_NOTE, item_id, survivor_id),
                )

            remaining = conn.execute(
                "SELECT COUNT(*) FROM ("
                "SELECT 1 FROM bids WHERE status='PENDING' "
                "GROUP BY item_id HAVING COUNT(*) > 1)"
            ).fetchone()[0]
            if remaining:
                raise RuntimeError(
                    f"BUI-67 dedup left {remaining} duplicate PENDING item_id(s)"
                )

        conn.execute(
            f"CREATE UNIQUE INDEX IF NOT EXISTS {_PENDING_UNIQUE_INDEX} "
            "ON bids(item_id) WHERE status='PENDING'"
        )
        conn.execute("RELEASE bui67_dedup")
    except Exception:  # noqa: BLE001  # migration failure — rollback savepoint, then re-raise
        try:
            conn.execute("ROLLBACK TO bui67_dedup")
        except Exception:  # noqa: BLE001  # rollback itself may fail; suppress, re-raise original
            pass
        raise
    conn.commit()


def _connect(path: Path) -> sqlite3.Connection:
    """Open a raw connection with this server's standard pragmas (BUI-407):
    WAL, ``foreign_keys=ON``, and a 5s ``busy_timeout`` as a safety margin
    against WAL-checkpoint / external-process contention — not a per-write
    retry loop, see ``write_transaction``'s docstring for why concurrent BUSY
    between our own writers isn't the failure mode this guards against
    (that's Stage 1+ of BUI-400's staged rollout). Shared by both the
    long-lived read connection (``init_db``) and the ephemeral per-transaction
    write connection (``write_transaction``) so the pragma set can't drift
    between them.
    """
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    return conn


def init_db(path: Path = DB_PATH) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = _connect(path)
    try:
        conn.executescript(_SCHEMA)
        conn.commit()
    except Exception:
        conn.close()
        raise
    _apply_migrations(conn)
    os.chmod(path, 0o600)
    return conn


@contextmanager
def write_transaction(path: Path = DB_PATH):
    """Open a fresh, short-lived write connection scoped to one transaction.

    BUI-407 (Stage 0 of BUI-400's shared-connection isolation rollout): this
    is the write-side counterpart to the long-lived read connection
    (``server.main._db``, opened by ``init_db`` above). It opens its own
    connection (WAL, ``foreign_keys=ON``, ``busy_timeout=5000``), yields it
    to the caller, commits exactly once on a clean exit, and rolls back +
    closes on any exception raised inside the ``with`` block — so a caller's
    mid-transaction failure can never leave a stray uncommitted write parked
    on a connection some *other* code path later commits (the fragmentation
    ``insert_bid``/``set_auction_end_time`` used to cause by self-committing
    mid-cycle — see the design doc's §2 finding 2).

    Stage 0 only adds this factory — no caller is wired to it yet. The
    existing shared ``_db`` singleton in ``server.main`` still owns every
    write; routing writers through here (under a short-held ``_write_lock``
    so two ephemeral write connections never overlap) is Stage 1+.
    See ``docs/plans/2026-07-18-001-design-shared-connection-isolation-plan.md``
    §3/§5 for the full design and staged rollout.
    """
    conn = _connect(path)
    try:
        yield conn
        conn.commit()
    except Exception:
        # rollback itself may fail (e.g. the connection is already broken by
        # whatever raised); suppress that secondary failure so the ORIGINAL
        # exception from the `with` block is what propagates, not a masking
        # error from rollback() — same idiom as _rebuild_bids_table /
        # _repair_bid_fmvs_fk / _dedup_pending_and_index above.
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass
        raise
    finally:
        conn.close()


def insert_bid(
    conn: sqlite3.Connection,
    item_id: str,
    max_bid: float,
    bid_offset: int,
    snipe_group: int,
    seller: str | None,
    seller_grade: float | None = None,
    photo_grade: float | None = None,
) -> int:
    # seller_grade/photo_grade are trailing defaults (BUI-78) so existing
    # positional callers (e.g. _sync_gixen) keep working unchanged.
    #
    # Caller must conn.commit() (BUI-407) — this used to self-commit, which
    # fragmented _sync_gixen's intended single end-of-cycle commit (see the
    # design doc's §2 finding 2). Every caller now commits explicitly.
    cur = conn.execute(
        """
        INSERT INTO bids (item_id, max_bid, bid_offset, snipe_group, seller,
                          seller_grade, photo_grade)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (item_id, max_bid, bid_offset, snipe_group, seller, seller_grade, photo_grade),
    )
    return cur.lastrowid


def update_bid_grades(
    conn: sqlite3.Connection,
    item_id: str,
    seller: str | None = None,
    seller_grade: float | None = None,
    photo_grade: float | None = None,
) -> None:
    """Update the live (PENDING) row's seller + grades from a buy-flow re-add.

    - `seller` is the canonical key, so the supplied (lowercased username) value
      is **authoritative** and overwrites whatever was there — e.g. a mixed-case
      store name a prior sync wrote — `COALESCE(?, seller)` (supplied wins, else
      keep existing). This keeps one canonical key per seller (BUI-78 A1).
    - Grades are observations, so they are **fill-NULL only** —
      `COALESCE(<col>, ?)` — completing an incomplete insert without editing an
      already-set grade (BUI-78 C2; re-grading is a deferred follow-up).

    No-op when all inputs are None.

    Caller must conn.commit() (BUI-407) — see insert_bid's docstring."""
    if seller is None and seller_grade is None and photo_grade is None:
        return
    conn.execute(
        "UPDATE bids SET "
        "seller=COALESCE(?, seller), "
        "seller_grade=COALESCE(seller_grade, ?), "
        "photo_grade=COALESCE(photo_grade, ?) "
        "WHERE item_id=? AND status='PENDING'",
        (seller, seller_grade, photo_grade, item_id),
    )


def get_bid_by_item_id(conn: sqlite3.Connection, item_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM bids WHERE item_id=? ORDER BY id DESC LIMIT 1",
        (item_id,),
    ).fetchone()


def get_bid_by_id(conn: sqlite3.Connection, row_id: int) -> sqlite3.Row | None:
    """Return the bids row with this id, or None if it no longer exists.

    The by-id counterpart to get_bid_by_item_id. Its reason to exist is the
    BUI-417 TOCTOU guard: both _run_ebay_fallback (server/fallback.py) and
    _sync_gixen's vanished-null-end apply (server/main.py) decide a terminal
    write from a status snapshot read LOCK-FREE at gather time, then apply it
    under _write_lock. A concurrent writer can commit a genuine terminal
    transition (or re-add the snipe) into the gather->apply window; because the
    terminal writes guard on status CLASS (`status NOT IN tombstones`), not
    equality vs the snapshot, a non-tombstone outcome like WON is not caught.
    Re-reading the row by its id INSIDE the apply transaction lets each apply
    path re-validate every precondition its gather-time decision rested on
    before committing — see both call sites (they share this guard's rationale
    and reference each other)."""
    return conn.execute(
        "SELECT * FROM bids WHERE id=?", (row_id,)
    ).fetchone()


def get_pending_bid_by_item_id(
    conn: sqlite3.Connection, item_id: str
) -> sqlite3.Row | None:
    """Return the live (PENDING) snipe for an item_id, or None.

    Unlike get_bid_by_item_id, this filters to status='PENDING' so a newer
    terminal/tombstone row can't shadow the live snipe. This is the lookup the
    add-upsert path keys on — deciding insert-vs-update by the *live* row, not
    the latest row of any status (BUI-67).
    """
    return conn.execute(
        "SELECT * FROM bids WHERE item_id=? AND status='PENDING' ORDER BY id DESC LIMIT 1",
        (item_id,),
    ).fetchone()


def update_bid(
    conn: sqlite3.Connection,
    item_id: str,
    max_bid: float,
    bid_offset: int | None,
    snipe_group: int | None,
) -> None:
    # gixen_vanished_at=NULL: every caller runs right after a successful Gixen
    # add/modify — first-party confirmation the snipe is live on Gixen, which
    # invalidates any earlier vanish observation exactly like reappearing on
    # the list does (BUI-371). Without this, a stale pre-end vanish stamp on a
    # re-added snipe could later misclassify its genuine result as REMOVED.
    #
    # bid_offset / snipe_group passthrough (BUI-401 / BUI-392): None means
    # "leave this field unchanged" — a max_bid-only PATCH must not silently
    # reset a tuned fire-offset back to 6 (BUI-401) or un-group the snipe
    # (BUI-392). Each field joins the SET list only when non-None, so a
    # max_bid-only edit (both None) touches neither. An explicit value writes
    # through the normal branch; an explicit snipe_group=0 still un-groups.
    #
    # group_changed_at (BUI-384): stamped only when snipe_group actually
    # changes — every SET expression sees the pre-UPDATE row, so the CASE
    # compares against the old value. An edit that keeps the group must NOT
    # re-stamp: that would narrow _group_won_before's evidence window and
    # weaken legitimate group-cancel evidence for no reason. A None snipe_group
    # skips its SET entirely, so group_changed_at is never touched then.
    now = datetime.now(timezone.utc).isoformat()
    # Every fragment below is a static literal — only the bound values are
    # caller-supplied — so this dynamic assembly carries no injection surface.
    set_clauses = ["max_bid=?"]
    params: list = [max_bid]
    if bid_offset is not None:
        set_clauses.append("bid_offset=?")
        params.append(bid_offset)
    if snipe_group is not None:
        set_clauses.append(
            "group_changed_at=CASE WHEN snipe_group != ? THEN ? "
            "ELSE group_changed_at END"
        )
        params.extend([snipe_group, now])
        set_clauses.append("snipe_group=?")
        params.append(snipe_group)
    set_clauses.append("gixen_vanished_at=NULL")
    params.append(item_id)
    # Caller must conn.commit() (BUI-407) — see insert_bid's docstring.
    conn.execute(
        f"UPDATE bids SET {', '.join(set_clauses)} "
        "WHERE item_id=? AND status='PENDING'",
        params,
    )


# Sanity allowance for record_group_win's future-end check. Mirrors
# server.fallback._CANCEL_EVIDENCE_MARGIN (moved from server.main in BUI-389;
# auction_end_at is estimated from Gixen's minute-granular countdown, plus
# clock skew) — a WON whose stored end is slightly in the future is normal
# estimation error, but one further out than this is self-contradictory input.
_WON_END_FUTURE_ALLOWANCE = timedelta(minutes=10)


def record_group_win(
    conn: sqlite3.Connection,
    item_id: str,
    snipe_group: int,
    won_end_at: str | None,
    recorded_at: str | None = None,
    source: str = GROUP_WIN_SOURCE_STATUS_TRANSITION,
) -> None:
    """Append BUI-381 group-win evidence to the durable ledger (see the
    group_wins schema comment). INSERT OR IGNORE against the
    (snipe_group, item_id, won_end_at) unique index makes re-recording the
    same win a no-op, while a genuine re-listed re-win at a DISTINCT end
    records a second entry (BUI-385 — the old 2-col key collapsed it).

    `source` is the provenance tag (a GROUP_WIN_SOURCES value) surfaced by
    /api/group-wins; it defaults to the primary writer (update_bid_status's
    WON transition) so no caller can silently land a NULL source. The
    _apply_listed_win_evidence path (BUI-410) passes GROUP_WIN_SOURCE_LISTED_WIN;
    the startup backfill writes its rows directly (not through here) tagged
    GROUP_WIN_SOURCE_STARTUP_BACKFILL.

    The ledger is permanent (nothing tombstones it), so it holds itself to a
    stricter evidence standard than the live-row query and stores only sound
    entries — anything else is skipped, WON-permissive:
    - group 0 (no group), or a missing end time: an end-less win cannot be
      bounded against a sibling's lifetime, and an observation-time proxy
      could falsely group-cancel a sibling added after the real win (the
      recycled-group hazard).
    - an unparseable end: useless to the classifier, never stored.
    - an end beyond the future allowance: a "win" that has not ended yet is
      self-contradictory input (e.g. eBay describing a re-listed same-ID
      auction) — not evidence.
    Caller must conn.commit()."""
    if source not in GROUP_WIN_SOURCES:
        # Enforce the closed vocabulary at the write boundary so a typo'd tag
        # can never land in the permanent ledger and surface, uncaught, over
        # /api/group-wins. Every production call site passes a
        # GROUP_WIN_SOURCE_* constant, so this only fires on a programming
        # error (caught by tests), never in the classification path.
        raise ValueError(
            f"record_group_win: unknown source {source!r} "
            f"(expected one of {sorted(GROUP_WIN_SOURCES)})"
        )
    if not snipe_group or not won_end_at:
        return
    try:
        won_end = datetime.fromisoformat(won_end_at.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return
    if won_end.tzinfo is None:
        won_end = won_end.replace(tzinfo=timezone.utc)
    if won_end > datetime.now(timezone.utc) + _WON_END_FUTURE_ALLOWANCE:
        return
    conn.execute(
        "INSERT OR IGNORE INTO group_wins "
        "(snipe_group, item_id, won_end_at, recorded_at, source) "
        "VALUES (?, ?, ?, ?, ?)",
        (snipe_group, item_id, won_end_at,
         recorded_at or datetime.now(timezone.utc).isoformat(), source),
    )


def update_bid_status(
    conn: sqlite3.Connection,
    item_id: str,
    status: str,
    winning_bid: float | None = None,
    resolved_at: str | None = None,
    status_mirror: str | None = None,
    *,
    only_id: int | None = None,
) -> None:
    # COALESCE on status_mirror so callers that don't have a fresh mirror value
    # (e.g. the eBay fallback path) don't clobber the last-known mirror status.
    # Caller must conn.commit() — this helper is hot-path inside loops where
    # the caller batches the commit at the end of the cycle.
    #
    # only_id narrows the write to one row. Every _sync_gixen and fallback
    # caller now passes it (the BUI-371 REMOVED classification first, then the
    # item_id-wide legacy writes narrowed by BUI-382/BUI-388/BUI-390): a
    # terminal/tombstone write must land only on the row it is resolving, never
    # on a *live* PENDING row or an older resolved-but-unpurged sibling that
    # shares the item_id (a re-listed auction re-added after the original
    # resolved — the BUI-178 class of collateral damage). No in-tree caller
    # omits only_id; the transition loop passes only_id=None only for the
    # row-less-winner no-op (no rows share the item_id), so the item_id-wide
    # branch is retained for that boundary and the legacy contract, never for a
    # deliberate multi-row write. Prefer only_id whenever the site holds a row id.
    id_clause = " AND id=?" if only_id is not None else ""
    win_rows: list[sqlite3.Row] = []
    if status == "WON":
        # BUI-381: capture group-win evidence at classification time, for
        # every WON writer (Gixen sync transitions, the eBay fallback
        # inference). The WON row itself is destructible — mark_bids_purged
        # sweeps it to REMOVED — and _group_won_before's live-row query would
        # then find nothing, reopening the phantom-WON window for exactly the
        # cancelled siblings this win should classify.
        #
        # Captured BEFORE the UPDATE, and only for rows with a genuine
        # auction_end_at: the UPDATE below COALESCE-fills a NULL end with
        # resolved_at (an observation-time proxy), and the permanent ledger
        # must never store a proxy — it could falsely group-cancel a sibling
        # added after the real win. Skipping is WON-permissive: the live WON
        # row still serves its (shipped BUI-371) proxy evidence until purged.
        # Same predicate as the UPDATE, so every captured row is one the
        # UPDATE flips to WON.
        params_won: list = [item_id]
        if only_id is not None:
            params_won.append(only_id)
        win_rows = conn.execute(
            "SELECT snipe_group, auction_end_at FROM bids "
            "WHERE item_id=? AND snipe_group != 0 AND auction_end_at IS NOT NULL "
            f"AND status NOT IN ({TOMBSTONE_STATUSES_SQL}){id_clause}",
            params_won,
        ).fetchall()
    params: list = [status, winning_bid, resolved_at, resolved_at, status_mirror, item_id]
    if only_id is not None:
        params.append(only_id)
    conn.execute(
        "UPDATE bids SET status=?, winning_bid=?, resolved_at=?, "
        "auction_end_at=COALESCE(auction_end_at, ?), "
        "status_mirror=COALESCE(?, status_mirror) "
        f"WHERE item_id=? AND status NOT IN ({TOMBSTONE_STATUSES_SQL}){id_clause}",
        params,
    )
    for row in win_rows:
        record_group_win(
            conn, item_id, row["snipe_group"], row["auction_end_at"],
            recorded_at=resolved_at,
            source=GROUP_WIN_SOURCE_STATUS_TRANSITION,
        )


def cache_gixen_data(
    conn: sqlite3.Connection,
    item_id: str,
    title: str | None,
    seller: str | None,
    current_bid: str | None,
    dbidid: str | None = None,
) -> None:
    """Cache Gixen-sourced fields. Does not touch auction_end_at — that's
    eBay's domain (Gixen only provides relative time-to-end). COALESCE keeps
    the existing value when the caller passes None.

    cached_at is only refreshed when at least one input field is non-NULL,
    so all-NULL writes (common for SCHEDULED snipes whose Gixen row hasn't
    populated current_bid yet) don't make the freshness indicator lie about
    when we last got real data.

    Caller must conn.commit() — this helper is hot-path inside the
    _sync_gixen loop where commits are batched at the end of the cycle.
    """
    # BUI-116: dbidid is Gixen's internal row id, always present on a live snipe
    # and needed by modify/remove. Write it unconditionally (its own statement,
    # before the has_data guard) so a SCHEDULED snipe with no current_bid still
    # gets its dbidid cached — otherwise the all-NULL early-return below would
    # skip it and the edit fast-path could never warm up.
    if dbidid:
        conn.execute(
            "UPDATE bids SET dbidid=? "
            f"WHERE item_id=? AND status NOT IN ({TOMBSTONE_STATUSES_SQL})",
            (dbidid, item_id),
        )

    has_data = any(v is not None for v in (title, seller, current_bid))
    if not has_data:
        return  # nothing to write, don't bump cached_at
    now = datetime.now(timezone.utc).isoformat()
    # BUI-78 A1: seller uses COALESCE(seller, ?) — keep an already-set seller
    # rather than overwriting it. The buy flow writes the canonical lowercased
    # eBay username at INSERT; Gixen's scrape returns the store display name, so
    # without this guard the sync would clobber the username and split a seller's
    # grade history. Seller-per-item is immutable, so never overwriting is safe;
    # a row that started NULL (web-added snipe) still gets filled.
    conn.execute(
        "UPDATE bids SET "
        "ebay_title=COALESCE(?, ebay_title), "
        "seller=COALESCE(seller, ?), "
        "cached_current_bid=COALESCE(?, cached_current_bid), "
        "cached_at=? "
        f"WHERE item_id=? AND status NOT IN ({TOMBSTONE_STATUSES_SQL})",
        (title, seller, current_bid, now, item_id),
    )


def delete_bid(conn: sqlite3.Connection, item_id: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    # Soft-delete tombstone. Renamed PURGED -> REMOVED in BUI-49; skip rows that
    # already carry either tombstone value so we don't re-stamp resolved_at.
    #
    # Caller must conn.commit() (BUI-407) — see insert_bid's docstring.
    conn.execute(
        f"UPDATE bids SET status='REMOVED', resolved_at=? WHERE item_id=? AND status NOT IN ({TOMBSTONE_STATUSES_SQL})",
        (now, item_id),
    )


def get_all_bids(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM bids ORDER BY added_at DESC").fetchall()


def get_pending_bids(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM bids WHERE status='PENDING'").fetchall()


def mark_bids_purged(conn: sqlite3.Connection, item_ids: list[str]) -> None:
    if not item_ids:
        return
    now = datetime.now(timezone.utc).isoformat()
    # placeholders contains only '?' chars — no user data is interpolated
    placeholders = ",".join("?" * len(item_ids))
    # Tombstone completed bids. Renamed PURGED -> REMOVED in BUI-49.
    #
    # BUI-178: guard on status, like delete_bid/update_bid_status. The partial
    # unique index only forbids two PENDING rows, so a re-listed/re-added item
    # can have a live PENDING row alongside an old WON/LOST row sharing the
    # item_id. Without this filter the completed-sweep tombstones BOTH and the
    # live snipe silently vanishes. Only tombstone resolved (completed) rows.
    #
    # Caller must conn.commit() (BUI-407) — see insert_bid's docstring.
    conn.execute(
        f"UPDATE bids SET status='REMOVED', resolved_at=? "
        f"WHERE item_id IN ({placeholders}) "
        f"AND status NOT IN ('PENDING', {TOMBSTONE_STATUSES_SQL})",
        [now, *item_ids],
    )


def refresh_snipe_group(
    conn: sqlite3.Connection, item_id: str, snipe_group: int,
    changed_at: str | None = None,
) -> None:
    """Mirror Gixen's listed snipe_group onto the live (PENDING) row (BUI-381).

    _sync_gixen used to never refresh snipe_group on existing rows, so a
    retroactive `gixen group N` applied via Gixen's web UI strengthened
    nothing — the winner's row kept group 0 and its group win classified no
    siblings. Gixen's list is the same authority the BUI-371 classifier
    already trusts for group evidence, in both directions: 0→N arms winner
    evidence, N→0 (user un-grouped) clears stale membership that could
    otherwise false-classify a genuine result as REMOVED.

    group_changed_at (BUI-384) is stamped with `changed_at` (defaulting to
    now) whenever the mirror actually changes the group — the WHERE's
    `snipe_group != ?` guarantees the stamp only lands on a real change. The
    observation time is an upper bound on the true change time, so the stamp
    can only NARROW _group_won_before's evidence window (WON-permissive) —
    never widen it. This is what stops a retroactive group join from being
    backdated to added_at and swallowing a pre-join win as cancel evidence.

    Caller must conn.commit() — hot-path inside the _sync_gixen loop where
    commits are batched at the end of the cycle."""
    conn.execute(
        "UPDATE bids SET snipe_group=?, group_changed_at=? "
        "WHERE item_id=? AND status='PENDING' AND snipe_group != ?",
        (snipe_group, changed_at or datetime.now(timezone.utc).isoformat(),
         item_id, snipe_group),
    )


def set_auction_end_time(conn: sqlite3.Connection, item_id: str, end_time_iso: str) -> None:
    """Caller must conn.commit() (BUI-407) — see insert_bid's docstring."""
    conn.execute(
        "UPDATE bids SET auction_end_at=? WHERE item_id=? AND status='PENDING'",
        (end_time_iso, item_id),
    )


def get_bids_ready_to_snipe(conn: sqlite3.Connection, now_iso: str) -> list[sqlite3.Row]:
    """Return PENDING bids whose fire time (auction_end_at - bid_offset) has arrived."""
    return conn.execute(
        """
        SELECT * FROM bids
        WHERE status = 'PENDING'
          AND local_snipe_at IS NULL
          AND auction_end_at IS NOT NULL
          AND datetime(auction_end_at, '-' || bid_offset || ' seconds') <= datetime(?)
        """,
        (now_iso,),
    ).fetchall()


def set_local_snipe_result(
    conn: sqlite3.Connection,
    item_id: str,
    fired_at: str,
    result: str,
) -> None:
    """Caller must conn.commit() (BUI-407) — see insert_bid's docstring."""
    conn.execute(
        "UPDATE bids SET local_snipe_at=?, local_snipe_result=? WHERE item_id=? AND status='PENDING'",
        (fired_at, result, item_id),
    )
