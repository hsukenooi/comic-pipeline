---
title: SQLite FK-Follows-RENAME and SAVEPOINT Constraints in Schema Migrations
date: 2026-05-19
category: database-issues
module: gixen-overlay/db
problem_type: database_issue
component: database
severity: high
symptoms:
  - "DROP TABLE comics_old fails with FK constraint error after ALTER TABLE RENAME when FK-child rows exist"
  - "PRAGMA foreign_keys=OFF is silently ignored inside an active SAVEPOINT or transaction"
  - "conn.commit() inside CRUD helpers destroys the host SAVEPOINT, bypassing rollback"
root_cause: logic_error
resolution_type: migration
related_components:
  - tooling
tags:
  - sqlite
  - foreign-keys
  - savepoint
  - migration
  - schema-split
  - alter-table-rename
  - pragma
---

# SQLite FK-Follows-RENAME and SAVEPOINT Constraints in Schema Migrations

## Problem

Three non-obvious SQLite behaviors interact to make the standard rename-and-rebuild migration pattern fail when running inside a plugin architecture with host-managed SAVEPOINTs. In SQLite 3.26+, renaming a table silently rewrites FK references in child tables to follow the rename, which causes the subsequent `DROP TABLE old` to fail. Attempting to work around this with `PRAGMA foreign_keys=OFF` fails silently inside a SAVEPOINT, and calling any CRUD helper that issues `conn.commit()` destroys the host SAVEPOINT and bypasses transactional safety.

## Symptoms

- `DROP TABLE comics_old` raises `sqlite3.OperationalError: FOREIGN KEY constraint failed` after `ALTER TABLE comics RENAME TO comics_old`, even though all FK children pointed to `comics` before the rename
- `PRAGMA foreign_keys=OFF` executes without error but has no effect — FKs remain enforced
- `conn.commit()` called inside a migration helper silently commits and collapses the enclosing SAVEPOINT, making subsequent `ROLLBACK TO SAVEPOINT` a no-op

## What Didn't Work

- **`PRAGMA foreign_keys=OFF` before `DROP TABLE`**: SQLite does not allow changing `PRAGMA foreign_keys` inside an active transaction. The gixen-overlay plugin runs each plugin's `register_db_tables` hook inside a `SAVEPOINT plugin_<name>` — that counts as an active transaction. The pragma is silently ignored and FKs stay enforced.
- **`PRAGMA foreign_keys=OFF` with `RELEASE SAVEPOINT` first**: Releasing the SAVEPOINT commits the work so far, removing the ability to roll back the migration on failure.
- **Calling `upsert_comic` / `upsert_fmv` inside `_migrate_fmv_split`**: These helpers call `conn.commit()` internally, which destroys the host SAVEPOINT at first invocation, making the host's error recovery impossible.

## Solution

Use the **Python-memory approach**: save all FK-child rows to Python lists before touching the parent table, drop the FK children, do the rename-and-rebuild, then recreate the FK children and restore data from memory.

```python
def _migrate_fmv_split(conn):
    # 1. Save FK-child rows to Python memory BEFORE touching the parent
    saved_fmv = conn.execute(
        "SELECT id, comic_id, grade, low, high, comps, confidence, notes, updated_at FROM fmv"
    ).fetchall()
    saved_bid_fmvs = conn.execute(
        "SELECT bid_id, fmv_id, is_primary FROM bid_fmvs"
    ).fetchall()

    # 2. Drop FK children — must happen before renaming the parent
    conn.execute("DROP TABLE bid_fmvs")
    conn.execute("DROP TABLE fmv")

    # 3. Rename parent and rebuild with new schema
    conn.execute("ALTER TABLE comics RENAME TO comics_old")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS comics (
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
    conn.execute("DROP TABLE comics_old")  # safe — no FK children remain

    # 4. Recreate FK children with IF NOT EXISTS (re-entrant on crash/retry)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS fmv (
            id         INTEGER PRIMARY KEY,
            comic_id   INTEGER NOT NULL REFERENCES comics(id) ON DELETE CASCADE,
            grade      REAL NOT NULL,
            ...
            UNIQUE(comic_id, grade)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS bid_fmvs (
            bid_id     INTEGER NOT NULL REFERENCES bids(id) ON DELETE CASCADE,
            fmv_id     INTEGER NOT NULL REFERENCES fmv(id) ON DELETE CASCADE,
            is_primary INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (bid_id, fmv_id)
        )
    """)

    # 5. Restore FK-child rows from Python memory
    for f in saved_fmv:
        conn.execute(
            "INSERT INTO fmv (id, comic_id, grade, low, high, comps, confidence, notes, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (f["id"], f["comic_id"], f["grade"], f["low"], f["high"],
             f["comps"], f["confidence"], f["notes"], f["updated_at"]),
        )
    for bf in saved_bid_fmvs:
        conn.execute(
            "INSERT OR IGNORE INTO bid_fmvs (bid_id, fmv_id, is_primary) VALUES (?, ?, ?)",
            (bf["bid_id"], bf["fmv_id"], bf["is_primary"]),
        )
```

**Key rule:** Inside any migration that runs under a host-managed SAVEPOINT, use only raw `conn.execute()` SQL. Never call CRUD helpers that issue `conn.commit()`.

### Bonus: shadow-row collapse survivor selection

When collapsing rows that share the same logical key (e.g., `UNIQUE(title, issue, year, grade)` → `UNIQUE(title, issue, year)`), use this SQL pattern to deterministically pick one survivor per group:

```sql
SELECT c.id, c.title, c.issue, c.year, c.locg_id, c.locg_variant_id, c.created_at
FROM comics c
INNER JOIN (
    SELECT title, issue, year,
        COALESCE(
            MIN(CASE WHEN locg_id IS NOT NULL THEN id END),
            MIN(CASE WHEN fmv_low IS NOT NULL THEN id END),
            MIN(CASE WHEN fmv_updated_at IS NOT NULL THEN id END),
            MIN(id)
        ) AS survivor_id
    FROM comics
    GROUP BY title, issue, year
) grp ON c.id = grp.survivor_id
```

Priority: `locg_id NOT NULL` > `fmv_low NOT NULL` > `newest fmv_updated_at` > `lowest id`. The `MIN(id)` final fallback guarantees a deterministic result even when all other columns are null.

## Why This Works

SQLite 3.26 introduced a correctness improvement: when you rename a table, any FK in a child table that referenced the old name is rewritten to reference the new name. This is generally correct behavior — it prevents dangling FK references — but it means that `fmv.comic_id REFERENCES comics(id)` becomes `fmv.comic_id REFERENCES comics_old(id)` after the rename. The child table now enforces referential integrity against the old table, so dropping the old table fails.

The Python-memory approach sidesteps this entirely by ensuring no FK children exist when the parent rename happens. There is nothing for SQLite to rewrite, and `DROP TABLE comics_old` succeeds unconditionally.

`PRAGMA foreign_keys` is a connection-level setting that SQLite refuses to change inside an active transaction (the docs say "no-op" — it just does nothing and returns no error). A SAVEPOINT is an active transaction, so any migration running inside one cannot disable FKs this way.

`conn.commit()` in Python's sqlite3 module commits all pending work on the connection, which releases all active SAVEPOINTs. The host's try/except around its SAVEPOINT will catch the subsequent exception correctly, but `ROLLBACK TO SAVEPOINT` is now a no-op because the savepoint no longer exists — the partial migration is committed.

## Prevention

- **Never call CRUD helpers inside migration functions** that run under a host-managed SAVEPOINT. Write raw `conn.execute()` SQL. Add a lint comment or docstring warning at the top of the migration function.
- **Always use `IF NOT EXISTS` on `CREATE TABLE` statements inside migration bodies** — makes the migration re-entrant if it crashes mid-run and is retried.
- **Add a crash-detection gate** at the start of the migration: if `comics` has no `grade` column (migration complete) but `comics_old` exists (crash between RENAME and DROP), raise `RuntimeError` with a clear message before the server starts on corrupted state.
- **Test idempotency explicitly**: call `create_tables(conn)` twice on a migrated DB and assert both row counts and schema structure are unchanged.
- **Test crash recovery**: assert that `RuntimeError` is raised when `comics_old` exists alongside a post-migration `comics` schema.

```python
def test_migration_crash_recovery_raises_runtime_error():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    # Simulate: post-migration comics schema + leftover comics_old
    conn.execute("CREATE TABLE comics (id INTEGER PRIMARY KEY, title TEXT, ...)")
    conn.execute("CREATE TABLE comics_old (id INTEGER PRIMARY KEY, title TEXT, grade REAL)")
    conn.commit()
    with pytest.raises(RuntimeError, match="crashed mid-migration state"):
        create_tables(conn)
```

## Related Issues

- Origin design document: `docs/2026-05-13-comic-fmv-split.md` — full schema rationale and conflict-resolution rules
- Implementation plan: `docs/plans/2026-05-19-001-feat-fmv-schema-split-plan.md` — §"Institutional Learnings" and Unit 3 for the technical decisions behind this migration
- SQLite docs: [ALTER TABLE](https://www.sqlite.org/lang_altertable.html) — the FK-follows-RENAME behavior is noted under "ALTER TABLE RENAME"
