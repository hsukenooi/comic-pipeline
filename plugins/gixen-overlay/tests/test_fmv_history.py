"""Tests for fmv_history (BUI-659) and its two read endpoints (BUI-662).

BUI-659: `fmv` stores exactly one row per (comic_id, grade) — a recompute
overwrites it, so there is no way to ask what a book was worth last month.
`fmv_history` is the append-only fix: every `POST /api/comics` upsert that
carries a grade appends an immutable snapshot, and a one-time migration seeds
one row per pre-existing `fmv` row. These tests pin the two invariants that
matter most: the append happens on EVERY upsert including a no-op one (never
conditional on "did anything change"), and the seeding migration cannot
double-seed on a restart.

BUI-662 adds `GET /api/comics/comps` and `GET /api/comics/fmv-history` — the
read side of both this table and the BUI-656 comps ledger. Both endpoints
share one identity-resolution contract: an unresolvable book 400s, a known
book with nothing on file 200s with an empty list — "no comps" must never be
confusable with "wrong book."
"""
from __future__ import annotations

import ast
import os
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from gixen_overlay.db import (
    append_fmv_history,
    create_tables,
    upsert_comic,
    upsert_fmv,
)

# `api` fixture: see conftest.py (BUI-630 de-duplicated the three hand-copies).


# ---------------------------------------------------------------------------
# DB-layer fixtures
# ---------------------------------------------------------------------------


def _make_db() -> sqlite3.Connection:
    """In-memory DB with the minimal bids stub the plugin's FK chain expects.

    Mirrors test_comps_ledger.py's `_make_db`.
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(
        "CREATE TABLE bids (id INTEGER PRIMARY KEY, item_id TEXT NOT NULL, "
        "max_bid REAL NOT NULL, fmv_id INTEGER)"
    )
    conn.commit()
    return conn


@pytest.fixture
def db():
    conn = _make_db()
    create_tables(conn)
    yield conn
    conn.close()


def _comic(conn, title="X-Men", issue="1", year=1963) -> int:
    return upsert_comic(conn, title, issue, year)


# ---------------------------------------------------------------------------
# create_tables: schema shape
# ---------------------------------------------------------------------------


def test_create_tables_creates_fmv_history():
    conn = _make_db()
    create_tables(conn)
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    assert "fmv_history" in tables
    conn.close()


def test_create_tables_fmv_history_is_idempotent(db):
    create_tables(db)  # second call must not raise
    assert db.execute("SELECT COUNT(*) FROM fmv_history").fetchone()[0] == 0


def test_fmv_history_check_rejects_unknown_source(db):
    comic_id = _comic(db)
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO fmv_history (comic_id, grade, source) "
            "VALUES (?, 9.4, 'bogus')",
            (comic_id,),
        )


def test_fmv_history_accepts_every_documented_source(db):
    comic_id = _comic(db)
    for source in ("upsert", "backfill"):
        db.execute(
            "INSERT INTO fmv_history (comic_id, grade, source) VALUES (?, 9.4, ?)",
            (comic_id, source),
        )
    db.commit()
    assert db.execute("SELECT COUNT(*) FROM fmv_history").fetchone()[0] == 2


def test_fmv_history_comic_id_is_not_null(db):
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO fmv_history (comic_id, grade, source) VALUES (NULL, 9.4, 'upsert')"
        )


def test_fmv_history_cascades_on_comics_delete(db):
    """Unlike comps (ON DELETE SET NULL), fmv_history is *about* the comics
    row, so deleting the book deletes its price history with it."""
    comic_id = _comic(db)
    fmv_id = upsert_fmv(db, comic_id, grade=9.4, low=100.0, high=150.0)
    append_fmv_history(db, fmv_id)
    assert db.execute(
        "SELECT COUNT(*) FROM fmv_history WHERE comic_id=?", (comic_id,)
    ).fetchone()[0] == 1

    db.execute("DELETE FROM comics WHERE id=?", (comic_id,))
    db.commit()
    assert db.execute(
        "SELECT COUNT(*) FROM fmv_history WHERE comic_id=?", (comic_id,)
    ).fetchone()[0] == 0


# ---------------------------------------------------------------------------
# append_fmv_history
# ---------------------------------------------------------------------------


def test_append_fmv_history_snapshots_current_fmv_row(db):
    comic_id = _comic(db)
    fmv_id = upsert_fmv(
        db, comic_id, grade=9.4, low=100.0, high=150.0, comps=5,
        confidence="high", notes="a note",
    )
    append_fmv_history(db, fmv_id)
    row = db.execute("SELECT * FROM fmv_history").fetchone()
    assert row["comic_id"] == comic_id
    assert row["grade"] == 9.4
    assert row["low"] == 100.0
    assert row["high"] == 150.0
    assert row["comps"] == 5
    assert row["confidence"] == "high"
    assert row["notes"] == "a note"
    assert row["flag_reason"] is None
    assert row["source"] == "upsert"


def test_append_fmv_history_recorded_at_matches_fmv_updated_at(db):
    comic_id = _comic(db)
    fmv_id = upsert_fmv(db, comic_id, grade=9.4, low=100.0, high=150.0)
    append_fmv_history(db, fmv_id)
    fmv_updated_at = db.execute(
        "SELECT updated_at FROM fmv WHERE id=?", (fmv_id,)
    ).fetchone()["updated_at"]
    assert fmv_updated_at is not None
    history_recorded_at = db.execute(
        "SELECT recorded_at FROM fmv_history WHERE comic_id=?", (comic_id,)
    ).fetchone()["recorded_at"]
    assert history_recorded_at == fmv_updated_at


def test_append_fmv_history_raises_for_unknown_fmv_id(db):
    with pytest.raises(ValueError, match="no fmv row with id"):
        append_fmv_history(db, 999999)


def test_append_fmv_history_always_appends_two_calls_two_rows(db):
    """R11 refinement: identical values, called twice, produce two rows —
    'we re-measured and nothing moved' is a fact worth a row."""
    comic_id = _comic(db)
    fmv_id = upsert_fmv(db, comic_id, grade=9.4, low=100.0, high=150.0)
    append_fmv_history(db, fmv_id)
    append_fmv_history(db, fmv_id)
    assert db.execute(
        "SELECT COUNT(*) FROM fmv_history WHERE comic_id=?", (comic_id,)
    ).fetchone()[0] == 2


def test_append_fmv_history_accepts_explicit_backfill_source(db):
    comic_id = _comic(db)
    fmv_id = upsert_fmv(db, comic_id, grade=9.4, low=100.0, high=150.0)
    append_fmv_history(db, fmv_id, source="backfill")
    row = db.execute("SELECT source FROM fmv_history").fetchone()
    assert row["source"] == "backfill"


# ---------------------------------------------------------------------------
# The one-time seeding migration
# ---------------------------------------------------------------------------
#
# `create_tables` seeds fmv_history from pre-existing `fmv` rows exactly
# once, gated via migration_state (never re-runs on a restart). To exercise
# that "pre-existing" scenario without hand-rolling a whole legacy schema
# (the fmv-split/year-nullable precedent), these tests build fmv rows through
# the normal API on an already-migrated DB, then roll back exactly the two
# artifacts BUI-659 adds (drop fmv_history, clear its migration_state marker)
# to simulate "a DB that had fmv rows before this migration shipped," and
# call create_tables again — the "deploy" moment.


def _simulate_pre_bui659_state(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM migration_state WHERE migration='seed_fmv_history'")
    conn.execute("DROP TABLE fmv_history")
    conn.commit()


def test_seed_migration_seeds_one_row_per_existing_fmv_row(db):
    comic1 = _comic(db, title="X-Men", issue="1", year=1963)
    upsert_fmv(db, comic1, grade=9.4, low=100.0, high=150.0, comps=5,
               confidence="high", notes="priced")
    comic2 = _comic(db, title="Fantastic Four", issue="1", year=1961)
    upsert_fmv(db, comic2, grade=8.0)  # bare stub: no price, updated_at NULL
    comic3 = _comic(db, title="Daredevil", issue="1", year=1964)
    upsert_fmv(db, comic3, grade=9.0, flag_reason="one_sided")

    _simulate_pre_bui659_state(db)
    create_tables(db)  # the "deploy" moment

    fmv_rows = {
        (r["comic_id"], r["grade"]): r
        for r in db.execute("SELECT * FROM fmv").fetchall()
    }
    history_rows = db.execute("SELECT * FROM fmv_history").fetchall()
    assert len(history_rows) == len(fmv_rows) == 3

    for h in history_rows:
        assert h["source"] == "backfill"
        src = fmv_rows[(h["comic_id"], h["grade"])]
        assert h["low"] == src["low"]
        assert h["high"] == src["high"]
        assert h["comps"] == src["comps"]
        assert h["confidence"] == src["confidence"]
        assert h["flag_reason"] == src["flag_reason"]
        assert h["notes"] == src["notes"]
        assert h["recorded_at"] == src["updated_at"]

    # The bare stub (never priced) carries its NULL updated_at through too —
    # no fallback to "now" is fabricated for it.
    stub_history = db.execute(
        "SELECT recorded_at FROM fmv_history WHERE comic_id=?", (comic2,)
    ).fetchone()
    assert stub_history["recorded_at"] is None


def test_seed_migration_is_idempotent_no_double_seed(db):
    comic_id = _comic(db)
    upsert_fmv(db, comic_id, grade=9.4, low=100.0, high=150.0)
    _simulate_pre_bui659_state(db)

    create_tables(db)
    first_count = db.execute("SELECT COUNT(*) FROM fmv_history").fetchone()[0]
    create_tables(db)  # simulate a restart — must not double-seed
    second_count = db.execute("SELECT COUNT(*) FROM fmv_history").fetchone()[0]

    assert first_count == 1
    assert second_count == first_count


def test_seed_migration_uses_fmv_updated_at_not_the_migrations_clock(db):
    comic_id = _comic(db)
    upsert_fmv(db, comic_id, grade=9.4, low=100.0, high=150.0)
    # Backdate updated_at so a fresh "now" computed by the migration would be
    # trivially distinguishable from it.
    db.execute(
        "UPDATE fmv SET updated_at=? WHERE comic_id=?",
        ("2020-01-01T00:00:00+00:00", comic_id),
    )
    db.commit()

    _simulate_pre_bui659_state(db)
    create_tables(db)

    row = db.execute(
        "SELECT recorded_at FROM fmv_history WHERE comic_id=?", (comic_id,)
    ).fetchone()
    assert row["recorded_at"] == "2020-01-01T00:00:00+00:00"


def test_fresh_and_migrated_db_end_in_the_same_fmv_history_schema():
    """(c) A fresh DB (fmv_history created empty) and an existing DB (created
    via the seeding migration finding pre-existing fmv rows) must define the
    identical fmv_history schema."""
    fresh = _make_db()
    create_tables(fresh)

    migrated = _make_db()
    create_tables(migrated)
    comic_id = _comic(migrated)
    upsert_fmv(migrated, comic_id, grade=9.4, low=100.0)
    _simulate_pre_bui659_state(migrated)
    create_tables(migrated)

    fresh_cols = [tuple(r) for r in fresh.execute("PRAGMA table_info(fmv_history)")]
    migrated_cols = [
        tuple(r) for r in migrated.execute("PRAGMA table_info(fmv_history)")
    ]
    assert fresh_cols == migrated_cols
    assert db_index_names(fresh) == db_index_names(migrated)
    fresh.close()
    migrated.close()


def db_index_names(conn: sqlite3.Connection) -> set[str]:
    return {
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND tbl_name='fmv_history'"
        )
    }


# ---------------------------------------------------------------------------
# POST /api/comics — the live append (route layer)
# ---------------------------------------------------------------------------


def _create_comic(api, **overrides) -> dict:
    body = {"title": "Amazing Spider-Man", "issue": "1", "year": 1963}
    body.update(overrides)
    return api.post("/api/comics", json=body).json()


def _history_rows_from_db(comic_id: int) -> list[sqlite3.Row]:
    db_path = os.environ["DB_PATH"]
    raw = sqlite3.connect(db_path)
    raw.row_factory = sqlite3.Row
    rows = raw.execute(
        "SELECT * FROM fmv_history WHERE comic_id=? ORDER BY id", (comic_id,)
    ).fetchall()
    raw.close()
    return rows


def test_upsert_comic_with_grade_appends_one_history_row(api):
    data = _create_comic(api, grade=9.4, fmv_low=100.0, fmv_high=150.0)
    rows = _history_rows_from_db(data["comic_id"])
    assert len(rows) == 1
    assert rows[0]["grade"] == 9.4
    assert rows[0]["low"] == 100.0
    assert rows[0]["source"] == "upsert"


def test_upsert_comic_without_grade_appends_no_history_row(api):
    data = _create_comic(api)
    rows = _history_rows_from_db(data["comic_id"])
    assert rows == []


def test_upsert_comic_two_identical_upserts_append_two_history_rows(api):
    payload = {"title": "Hulk", "issue": "181", "year": 1974,
               "grade": 9.8, "fmv_low": 5000.0, "fmv_high": 7000.0}
    r1 = api.post("/api/comics", json=payload).json()
    r2 = api.post("/api/comics", json=payload).json()
    assert r1["comic_id"] == r2["comic_id"]
    rows = _history_rows_from_db(r1["comic_id"])
    assert len(rows) == 2


def test_upsert_comic_multi_issue_lot_422_appends_no_history_row(api):
    r = api.post("/api/comics", json={
        "title": "Amazing Spider-man #18,19,20,21,22 lot of 5 NM Gems Wow",
        "issue": "18",
        "grade": 9.0,
    })
    assert r.status_code == 422
    # No comic was even created, so there is nothing to look up by comic_id —
    # assert the table is empty outright.
    db_path = os.environ["DB_PATH"]
    raw = sqlite3.connect(db_path)
    count = raw.execute("SELECT COUNT(*) FROM fmv_history").fetchone()[0]
    raw.close()
    assert count == 0


def test_append_failure_does_not_break_the_upsert_response(api, caplog):
    """A broken append degrades to a log line, never a broken write — the fmv
    row itself is already durable by the time append_fmv_history runs."""
    def _explode(*a, **kw):
        raise sqlite3.OperationalError("no such table: fmv_history")

    with patch("gixen_overlay.routes.append_fmv_history", side_effect=_explode):
        r = api.post("/api/comics", json={
            "title": "Iron Man", "issue": "1", "year": 1968,
            "grade": 9.0, "fmv_low": 300.0, "fmv_high": 400.0,
        })
    assert r.status_code == 200
    data = r.json()
    assert data["fmv_id"] is not None

    # The fmv row itself landed even though the history append blew up.
    gr = api.get("/api/comics", params={"grade": 9.0})
    assert gr.json()[0]["fmv_low"] == 300.0

    assert _history_rows_from_db(data["comic_id"]) == []
    assert any(
        "append_fmv_history failed" in r.message for r in caplog.records
    )

# ---------------------------------------------------------------------------
# GET /api/comics/fmv-history (BUI-662)
# ---------------------------------------------------------------------------


def test_get_fmv_history_by_comic_id(api):
    data = _create_comic(api, grade=9.4, fmv_low=100.0, fmv_high=150.0)
    r = api.get("/api/comics/fmv-history", params={"comic_id": data["comic_id"]})
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 1
    assert rows[0]["low"] == 100.0


def test_get_fmv_history_by_title_issue_year(api):
    _create_comic(api, title="Daredevil", issue="1", year=1964,
                  grade=9.0, fmv_low=200.0)
    r = api.get("/api/comics/fmv-history", params={
        "title": "Daredevil", "issue": "1", "year": 1964,
    })
    assert r.status_code == 200
    assert len(r.json()) == 1


def test_get_fmv_history_unresolvable_comic_id_400s(api):
    r = api.get("/api/comics/fmv-history", params={"comic_id": 999999})
    assert r.status_code == 400


def test_get_fmv_history_unresolvable_title_issue_400s(api):
    r = api.get("/api/comics/fmv-history", params={
        "title": "Nonexistent Series", "issue": "1",
    })
    assert r.status_code == 400


def test_get_fmv_history_no_identity_supplied_400s(api):
    r = api.get("/api/comics/fmv-history")
    assert r.status_code == 400


def test_get_fmv_history_resolves_yearless_placeholder_when_year_supplied(api):
    """BUI-698: `_resolve_comic_id` is shared with `get_comps` — see that
    endpoint's own tests in test_comps_ledger.py for the full rationale and
    the confirmed live incident. Pinned here too so this endpoint cannot
    silently regress if the shared helper ever gets split."""
    data = _create_comic(api, title="Invincible", issue="77", year=None,
                         grade=9.2, fmv_low=60.0, fmv_high=80.0)
    r = api.get("/api/comics/fmv-history", params={
        "title": "Invincible", "issue": "77", "year": 2011,
    })
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 1
    assert rows[0]["comic_id"] == data["comic_id"]


def test_get_fmv_history_known_book_with_no_history_returns_empty_list(api):
    """A known book with no history rows must 200 with [] — never 400. 'No
    history' and 'wrong book' are distinguishable states."""
    data = _create_comic(api)  # no grade => no fmv/history row at all
    r = api.get("/api/comics/fmv-history", params={"comic_id": data["comic_id"]})
    assert r.status_code == 200
    assert r.json() == []


def test_get_fmv_history_newest_first(api):
    payload = {"title": "Hulk", "issue": "181", "year": 1974, "grade": 9.8}
    data = api.post("/api/comics", json={**payload, "fmv_low": 100.0}).json()
    api.post("/api/comics", json={**payload, "fmv_low": 200.0})
    api.post("/api/comics", json={**payload, "fmv_low": 300.0})
    r = api.get("/api/comics/fmv-history", params={"comic_id": data["comic_id"]})
    rows = r.json()
    assert len(rows) == 3
    assert [row["low"] for row in rows] == [300.0, 200.0, 100.0]


def test_get_fmv_history_grade_filter_narrows(api):
    data = _create_comic(api, grade=9.4, fmv_low=100.0)
    api.post("/api/comics", json={
        "title": "Amazing Spider-Man", "issue": "1", "year": 1963,
        "grade": 9.6, "fmv_low": 200.0,
    })
    r = api.get("/api/comics/fmv-history", params={
        "comic_id": data["comic_id"], "grade": 9.4,
    })
    rows = r.json()
    assert len(rows) == 1
    assert rows[0]["grade"] == 9.4


# ---------------------------------------------------------------------------
# Scope-boundary contract: never read into a price (BUI-662)
#
# GET /api/comics/comps' own tests live in test_comps_ledger.py, alongside
# its POST sibling (BUI-656) — see that file's "GET /api/comics/comps" section.
# ---------------------------------------------------------------------------


def _fmv_runner_source() -> str:
    """apps/fmv's pricing-path source, or skip if it isn't checked out beside
    us. Located relative to this file so the test works from any CWD —
    apps/fmv is not a workspace member, so there is no importable path.
    Mirrors test_flag_reason_contract.py's precedent for this cross-package
    canary shape.
    """
    repo_root = Path(__file__).resolve().parents[3]
    runner = repo_root / "apps" / "fmv" / "src" / "fmv_runner.py"
    if not runner.is_file():
        pytest.skip(f"apps/fmv not present at {runner}; cross-package canary skipped")
    return runner.read_text(encoding="utf-8")


def test_fmv_history_endpoint_never_referenced_in_pricing_path():
    """This project writes the archive and never reads it back into a
    price — degraded-mode pricing from stored comps is a deliberately
    separate follow-up (BUI-663) with its own staleness rules and soak.
    `/api/comics/fmv-history` has no legitimate caller anywhere in the
    pricing path, unlike `/api/comics/comps`, which apps/fmv legitimately
    POSTs to (BUI-658/U3, the ledger write) — see the next test, which is
    scoped to GET-only usage for exactly that reason."""
    source = _fmv_runner_source()
    assert "fmv-history" not in source, (
        "apps/fmv references /api/comics/fmv-history somewhere — the archive "
        "read endpoint must never be wired into the pricing path (BUI-662 "
        "scope boundary)."
    )


# BUI-663 narrowed the tripwire below from "never" to "exactly once, here".
# `apps/fmv` may GET the comps ledger from this ONE function and nowhere else:
# it is the degraded-mode read that prices a book advisory-only after both
# sold-comps providers fail, and it withholds the bid cap. Any OTHER GET of
# that endpoint from the pricing path is the BUI-662 violation this test has
# always guarded, and still fails the build.
_SANCTIONED_COMPS_GET_FN = "_fetch_ledger_comps"


def _get_json_or_warn_call_sites(source: str) -> list[tuple[str, str]]:
    """[(enclosing function name, "/api/comics…" path)] for every
    `_get_json_or_warn(f"{server_url}/api/comics…", …)` call in the source.

    Parsed with `ast`, not regex, per the BUI-673 update to
    `docs/solutions/architecture-patterns/http-only-contracts-need-a-source-parsing-canary.md`:
    a reformat or a line-wrap must not be able to turn this contract test into
    a silent no-op. The path is rebuilt from the f-string's literal
    `ast.Constant` parts, so the interpolated `{server_url}` prefix drops out
    and `f"{server_url}/api/comics/comps"` yields exactly `/api/comics/comps`.
    """
    sites: list[tuple[str, str]] = []
    for fn in ast.walk(ast.parse(source)):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(fn):
            if not isinstance(node, ast.Call):
                continue
            if not (isinstance(node.func, ast.Name)
                    and node.func.id == "_get_json_or_warn"):
                continue
            if not (node.args and isinstance(node.args[0], ast.JoinedStr)):
                continue
            literal = "".join(
                part.value for part in node.args[0].values
                if isinstance(part, ast.Constant) and isinstance(part.value, str)
            )
            if literal.startswith("/api/comics"):
                sites.append((fn.name, literal))
    return sites


def test_comps_read_endpoint_get_fetched_only_from_the_degraded_mode_call_site():
    """`GET /api/comics/comps` (the BUI-662 read endpoint on this path) may be
    called from exactly ONE function in apps/fmv — the BUI-663 degraded-mode
    read — and from nowhere else.

    Formerly `test_comps_read_endpoint_never_get_fetched_in_pricing_path`; the
    boundary it guards is unchanged, the permitted set grew from zero to one.
    Still deliberately narrower than 'the string /api/comics/comps never
    appears': `POST /api/comics/comps` (BUI-658/U3's ledger write, same path)
    is legitimate and expected, and only a GET is the violation.

    Why the exception is safe, and why it must stay exactly this narrow: the
    sanctioned call site prices a book ADVISORY-only after both sold-comps
    providers have failed. It nulls `max_bid` and never upserts, so the number
    reaches no `fmv` row and therefore no bid cap. An unrestricted GET
    elsewhere in the pricing path carries none of those guarantees, which is
    exactly why the tripwire narrows rather than disappears.
    """
    source = _fmv_runner_source()
    sites = _get_json_or_warn_call_sites(source)
    assert sites, (
        "found no _get_json_or_warn(f\"{server_url}/api/comics…\") calls in "
        "fmv_runner.py — did the helper move or get renamed? This contract "
        "test is vacuous until that is fixed."
    )
    comps_gets = [fn for fn, path in sites if path == "/api/comics/comps"]
    unsanctioned = sorted({fn for fn in comps_gets
                           if fn != _SANCTIONED_COMPS_GET_FN})
    assert not unsanctioned, (
        f"{unsanctioned} GET-fetch /api/comics/comps — only "
        f"{_SANCTIONED_COMPS_GET_FN}() (BUI-663's degraded-mode read, which "
        "withholds max_bid and never upserts) may read the comps ledger from "
        "the pricing path. A POST to this same path (BUI-658/U3's ledger "
        "write) is fine; only a GET is the violation this test guards."
    )
    assert len(comps_gets) <= 1, (
        f"{_SANCTIONED_COMPS_GET_FN}() GET-fetches /api/comics/comps "
        f"{len(comps_gets)} times — the degraded-mode read is a single call "
        "site by design, so a second one means the guard conditions around it "
        "have been duplicated (or dropped). Consolidate them."
    )
