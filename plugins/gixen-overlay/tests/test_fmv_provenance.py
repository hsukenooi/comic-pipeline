"""`fmv.provenance` — the column that replaced the notes-prefix marker (BUI-769).

`comic-fmv` refuses to recompute over a hand-priced row. Until this ticket the
row's provenance was claimed by a PREFIX in `fmv.notes`, which fails open to
any reword — and the failure direction is the expensive one: a human's priced
band silently replaced by the pooled answer they had already rejected. Twice
the marker set turned out to be a guess at a human convention (BUI-533 sampled
half of it; BUI-759 found the other half seven rows in, one of them the ASM #50
1st-Kraven row at $600-680).

These tests cover the SERVER half — the column, its migration, the one-time
backfill, the upsert's non-demotion rule, and the fact that `GET /api/comics`
actually serves the field the guard reads. The guard's own bucketing lives in
`apps/fmv/tests/test_fmv_runner.py`; a green suite here is not evidence that
the guard fires (that is exactly the BUI-775 trap).
"""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest

from gixen_overlay.db import (
    FMV_PROVENANCE_HAND,
    FMV_PROVENANCES,
    HAND_PRICE_NOTES_MARKERS,
    HAND_PRICE_NOTES_PREFIX_RE,
    _migrate_add_fmv_provenance_column,
    _migrate_backfill_fmv_provenance,
    create_tables,
    list_comics,
    upsert_comic,
    upsert_fmv,
)
from gixen_overlay.models import UpsertComicRequest


def _make_db() -> sqlite3.Connection:
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


def _provenance(conn: sqlite3.Connection, fmv_id: int) -> str | None:
    return conn.execute(
        "SELECT provenance FROM fmv WHERE id=?", (fmv_id,)
    ).fetchone()["provenance"]


# The 12 operator-priced rows as they stood on the live Mini 2026-08-12 — the
# same census `apps/fmv/tests/test_fmv_runner.py` pins its matcher against.
# Duplicated deliberately: apps/fmv is not a workspace member, so the two
# copies of the matcher have no import edge, and this list is what makes a
# widening on one side that never lands on the other fail loudly.
_LIVE_OPERATOR_NOTES = [
    "Manual: CGC-proxy + genuine raw comps (ASM #50",   # fmv 767, $600-680
    "manual: narrowed to +/-1.0 grade window",          # fmv 732
    "manual: narrowed to +/-1.0 grade window",          # fmv 739
    "manual: cross-variant proxy from Newsstand",       # fmv 745
    "manual: one-sided extrapolation below VG-",        # fmv 757
    "manual: exact grade match at VG/FN 5.0",           # fmv 759
    "manual: excluded $59.99 suspect outlier",          # fmv 763
    "hand § anchored on the lone 4.0 sale",             # fmv 733
    "hand § direct comps at this grade",                # fmv 810
    "hand § operator band, CLI pool rejected",          # fmv 862
    "hand § cross-checked against slab ladder",         # fmv 913
    "hand § re-priced after the 2026-08 sale",          # fmv 917
]

_MACHINE_NOTES = [
    "window=±0.5 | cv=20% | label=HIGH",
    "window=n/a | cv=12% | label=MEDIUM | manual_review=one_sided",
    "manual_review=one_sided | window=±0.5 | cv=31%",
    "LEDGER-ADVISORY (stale comps, no bid cap) | window=±0.5",
    "handled by the CGC ladder, no raw comps",
    "handoff from the calibration report",
]


# ─── Schema + migration ───────────────────────────────────────────────────────

class TestProvenanceColumn:
    def test_fresh_db_has_the_column(self, db):
        cols = {r[1] for r in db.execute("PRAGMA table_info(fmv)")}
        assert "provenance" in cols

    def test_existing_rows_migrate_to_null_not_machine(self):
        """NULL means "never claimed", which is NOT the same as 'machine':
        a pre-column row must keep falling back to the notes matcher, or the
        migration itself would strip protection from every row it touches."""
        conn = _make_db()
        conn.execute("""
            CREATE TABLE fmv (
                id INTEGER PRIMARY KEY, comic_id INTEGER NOT NULL,
                grade REAL NOT NULL, low REAL, high REAL, comps INTEGER,
                confidence TEXT, notes TEXT, updated_at TEXT,
                UNIQUE(comic_id, grade))
        """)
        conn.execute(
            "INSERT INTO fmv (comic_id, grade, low, high, notes) "
            "VALUES (1, 6.5, 600, 680, 'Manual: CGC-proxy (ASM #50)')"
        )
        _migrate_add_fmv_provenance_column(conn)
        row = conn.execute("SELECT provenance, low FROM fmv").fetchone()
        assert row["provenance"] is None
        assert row["low"] == 600      # ADD COLUMN rewrites no data
        conn.close()

    def test_migration_is_idempotent(self, db):
        """Runs on every server startup — a second pass must not raise
        (SQLite errors on a duplicate ADD COLUMN)."""
        for _ in range(3):
            _migrate_add_fmv_provenance_column(db)
        cols = [r[1] for r in db.execute("PRAGMA table_info(fmv)")]
        assert cols.count("provenance") == 1

    def test_check_constraint_rejects_a_value_outside_the_vocabulary(self, db):
        comic_id = upsert_comic(db, title="X", issue="1", year=1990)
        fmv_id = upsert_fmv(db, comic_id=comic_id, grade=9.0, low=10, high=20)
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("UPDATE fmv SET provenance='OVERRIDE' WHERE id=?",
                       (fmv_id,))

    def test_check_constraint_admits_null(self, db):
        comic_id = upsert_comic(db, title="X", issue="1", year=1990)
        fmv_id = upsert_fmv(db, comic_id=comic_id, grade=9.0, low=10, high=20)
        db.execute("UPDATE fmv SET provenance=NULL WHERE id=?", (fmv_id,))
        assert _provenance(db, fmv_id) is None

    @pytest.mark.parametrize("value", FMV_PROVENANCES)
    def test_check_constraint_admits_every_vocabulary_value(self, db, value):
        comic_id = upsert_comic(db, title="X", issue="1", year=1990)
        fmv_id = upsert_fmv(db, comic_id=comic_id, grade=9.0, low=10, high=20)
        db.execute("UPDATE fmv SET provenance=? WHERE id=?", (value, fmv_id))
        assert _provenance(db, fmv_id) == value


# ─── The one-time backfill ────────────────────────────────────────────────────

class TestBackfill:
    def _seed(self, db, notes_list):
        ids = []
        for i, notes in enumerate(notes_list):
            comic_id = upsert_comic(db, title=f"Book {i}", issue=str(i),
                                    year=1970)
            ids.append(upsert_fmv(db, comic_id=comic_id, grade=6.5, low=100,
                                  high=200, notes=notes))
        # Undo the marker create_tables already set, and clear any claim the
        # upserts stored, so the backfill runs against pre-column-shaped rows.
        db.execute("DELETE FROM migration_state "
                   "WHERE migration='backfill_fmv_provenance'")
        db.execute("UPDATE fmv SET provenance=NULL")
        return ids

    def test_claims_all_twelve_live_operator_rows(self, db):
        """Acceptance criterion 2. A partial backfill must NAME the rows it
        missed — "11 of 12" is precisely the finding BUI-775 exists for."""
        ids = self._seed(db, _LIVE_OPERATOR_NOTES)
        _migrate_backfill_fmv_provenance(db)
        missed = [_LIVE_OPERATOR_NOTES[i] for i, fid in enumerate(ids)
                  if _provenance(db, fid) != "hand"]
        assert not missed, (
            f"{len(ids) - len(missed)} of {len(ids)} live operator rows "
            f"claimed; UNCLAIMED: {missed}")

    def test_census_is_never_shrunk(self):
        """Growing this list is fine (a new convention appears); shrinking it
        is how the backfill would silently narrow."""
        assert len(_LIVE_OPERATOR_NOTES) >= 12

    def test_leaves_machine_rows_unclaimed(self, db):
        """The over-match direction is cheap but not free — a wrongly-claimed
        machine row stops auto-refreshing. Pin the lookalikes the apps/fmv
        matcher also pins (`manual_review=`, `handled by`, `handoff`)."""
        ids = self._seed(db, _MACHINE_NOTES)
        _migrate_backfill_fmv_provenance(db)
        wrongly = [_MACHINE_NOTES[i] for i, fid in enumerate(ids)
                   if _provenance(db, fid) is not None]
        assert not wrongly, f"machine rows wrongly claimed 'hand': {wrongly}"

    def test_touches_nothing_but_provenance(self, db):
        """A money-adjacent write: it must add a claim and change no number.
        Compared field-by-field rather than on low/high alone — the BUI-775
        post-mortem's own point about comparing too little."""
        ids = self._seed(db, _LIVE_OPERATOR_NOTES[:3])
        cols = "id, comic_id, grade, low, high, comps, confidence, notes, updated_at"
        before = {r["id"]: dict(r) for r in
                  db.execute(f"SELECT {cols} FROM fmv")}
        _migrate_backfill_fmv_provenance(db)
        after = {r["id"]: dict(r) for r in db.execute(f"SELECT {cols} FROM fmv")}
        assert after == before
        assert all(_provenance(db, fid) == "hand" for fid in ids)

    def test_does_not_overwrite_an_existing_claim(self, db):
        """An operator who deliberately marked a `hand §`-noted row 'machine'
        (e.g. after re-pricing it from the pool) must not have that reversed."""
        ids = self._seed(db, ["hand § anchored on the lone 4.0 sale"])
        db.execute("UPDATE fmv SET provenance='machine' WHERE id=?", (ids[0],))
        _migrate_backfill_fmv_provenance(db)
        assert _provenance(db, ids[0]) == "machine"

    def test_runs_once_and_only_once(self, db):
        """Marker-gated (mirroring _migrate_seed_fmv_history) so the notes
        matcher can genuinely be retired after its grace period, instead of
        being re-armed by every server restart."""
        ids = self._seed(db, ["hand § anchored on the lone 4.0 sale"])
        _migrate_backfill_fmv_provenance(db)
        assert _provenance(db, ids[0]) == "hand"

        # A new operator row appears AFTER the backfill, claimed only in its
        # notes. The second pass must not touch it — the marker gate is what
        # keeps this a one-time translation of the old convention.
        comic_id = upsert_comic(db, title="Later", issue="1", year=1970)
        later = upsert_fmv(db, comic_id=comic_id, grade=6.5, low=1, high=2,
                           notes="hand § written after the backfill")
        _migrate_backfill_fmv_provenance(db)
        assert _provenance(db, later) is None

    def test_survives_a_null_notes_row(self, db):
        comic_id = upsert_comic(db, title="Stub", issue="1", year=1970)
        fmv_id = upsert_fmv(db, comic_id=comic_id, grade=6.5)
        db.execute("DELETE FROM migration_state "
                   "WHERE migration='backfill_fmv_provenance'")
        _migrate_backfill_fmv_provenance(db)
        assert _provenance(db, fmv_id) is None


class TestBackfillMatcherMirrorsTheGuards:
    """The backfill's regex is a deliberate verbatim twin of
    `apps/fmv/src/fmv_runner.py`'s (no import edge — apps/fmv is not a
    workspace member). A twin that drifts silently under-claims rows, which
    is how "the guard is correct but never reaches the data" happens."""

    def _fmv_runner_source(self) -> str:
        runner = (Path(__file__).resolve().parents[3]
                  / "apps" / "fmv" / "src" / "fmv_runner.py")
        if not runner.is_file():
            pytest.skip(f"apps/fmv not present at {runner}")
        return runner.read_text(encoding="utf-8")

    def test_vocabulary_matches_the_consumer(self):
        """`fmv.provenance`'s closed vocabulary is a cross-package contract:
        this side owns the CHECK constraint and the pydantic validator,
        `apps/fmv` owns the READER. A rename here that never lands there is
        not a loud failure — the reader treats an unrecognized claim as
        "don't know" and fails CLOSED, so EVERY row would start bucketing into
        `skipped_hand` and the whole table would silently stop refreshing.
        Pin both spellings against each other."""
        src = self._fmv_runner_source()
        theirs = {
            re.search(rf'_PROVENANCE_{name}\s*=\s*"([^"]+)"', src).group(1)
            for name in ("HAND", "MACHINE")
        }
        assert theirs == set(FMV_PROVENANCES), (
            f"apps/fmv reads {sorted(theirs)} but this side stores "
            f"{sorted(FMV_PROVENANCES)}; the vocabularies have drifted")

    def test_hand_constant_matches_the_consumer(self):
        """The backfill writes FMV_PROVENANCE_HAND; the guard tests for
        `_PROVENANCE_HAND`. If those two ever differ, the backfill claims 12
        rows the guard cannot read — protection that looks applied and is
        not, which is the exact BUI-775 failure shape."""
        src = self._fmv_runner_source()
        theirs = re.search(r'_PROVENANCE_HAND\s*=\s*"([^"]+)"', src).group(1)
        assert theirs == FMV_PROVENANCE_HAND

    def test_marker_tuple_matches_the_producer(self):
        m = re.search(r"_HAND_PRICE_MARKERS\s*=\s*\(([^)]*)\)",
                      self._fmv_runner_source())
        assert m, "could not find _HAND_PRICE_MARKERS — did the producer move?"
        theirs = tuple(re.findall(r'"([^"]+)"', m.group(1)))
        assert theirs == HAND_PRICE_NOTES_MARKERS, (
            f"apps/fmv matches {theirs} but this backfill matches "
            f"{HAND_PRICE_NOTES_MARKERS}; the twin has drifted")

    def test_markers_are_bare_words(self):
        """`\\b` after a punctuated marker inverts the match instead of
        widening it — fail here, not in a backfill run."""
        for marker in HAND_PRICE_NOTES_MARKERS:
            assert marker.isalpha(), f"marker {marker!r} is not a bare word"

    @pytest.mark.parametrize("notes", _LIVE_OPERATOR_NOTES)
    def test_matches_every_live_operator_note(self, notes):
        assert HAND_PRICE_NOTES_PREFIX_RE.match(notes) is not None

    @pytest.mark.parametrize("notes", _MACHINE_NOTES)
    def test_matches_no_machine_note(self, notes):
        assert HAND_PRICE_NOTES_PREFIX_RE.match(notes) is None


# ─── upsert_fmv's non-demotion rule ───────────────────────────────────────────

class TestUpsertProvenance:
    def _priced_hand_row(self, db):
        comic_id = upsert_comic(db, title="Amazing Spider-Man", issue="50",
                                year=1967)
        fmv_id = upsert_fmv(db, comic_id=comic_id, grade=6.5, low=600,
                            high=680, comps=4, confidence="medium",
                            notes="OVERRIDE: operator band", provenance="hand")
        return comic_id, fmv_id

    def test_stores_the_claim(self, db):
        _, fmv_id = self._priced_hand_row(db)
        assert _provenance(db, fmv_id) == "hand"

    def test_omitting_provenance_never_demotes_a_stored_claim(self, db):
        """THE fail-closed core. Every caller written before this column
        existed posts None here; forgetting the field must be safe, in the
        protecting direction."""
        comic_id, fmv_id = self._priced_hand_row(db)
        upsert_fmv(db, comic_id=comic_id, grade=6.5, low=120, high=150,
                   comps=9, confidence="high", notes="window=±0.5 | cv=20%")
        assert _provenance(db, fmv_id) == "hand"

    def test_a_bare_n0_stub_never_demotes_a_priced_row(self, db):
        """BUI-599's stub guard, applied to provenance: a failed re-lookup
        posts `fmv_comps: 0` with real-looking metadata and must not degrade a
        priced row — least of all its provenance."""
        comic_id, fmv_id = self._priced_hand_row(db)
        upsert_fmv(db, comic_id=comic_id, grade=6.5, comps=0,
                   confidence="low", notes="window=n/a | cv=n/a",
                   provenance="machine")
        assert _provenance(db, fmv_id) == "hand"
        assert db.execute("SELECT low FROM fmv WHERE id=?",
                          (fmv_id,)).fetchone()["low"] == 600

    def test_an_operator_can_claim_an_unpriced_row_without_inventing_a_price(
            self, db):
        """The deliberate boundary of the stub guard above, pinned from the
        side that motivates it: an operator marking an unpriced (n=0) book
        'hand' — "do not auto-price this" — posts no `low`, so the post IS
        stub-shaped. Scoping the stub guard wider would swallow that claim
        silently, which is the exact failure class this column exists to end.
        A default `comic-fmv` run cannot reach such a row anyway: the guard's
        lookup deliberately does not filter on `fmv_low`, so it buckets into
        `skipped_hand` before any write."""
        comic_id = upsert_comic(db, title="Illiquid", issue="1", year=1970)
        fmv_id = upsert_fmv(db, comic_id=comic_id, grade=6.5, comps=0,
                            notes="window=n/a")
        assert _provenance(db, fmv_id) is None
        upsert_fmv(db, comic_id=comic_id, grade=6.5, provenance="hand")
        assert _provenance(db, fmv_id) == "hand"

    def test_an_explicit_machine_claim_wins_on_a_real_reprice(self, db):
        """What keeps `--force` idempotent: a forced overwrite leaves a
        machine number behind, and a row still claiming 'hand' would be
        skipped by every later default run forever."""
        comic_id, fmv_id = self._priced_hand_row(db)
        upsert_fmv(db, comic_id=comic_id, grade=6.5, low=120, high=150,
                   comps=9, confidence="high", notes="window=±0.5",
                   provenance="machine")
        assert _provenance(db, fmv_id) == "machine"

    def test_a_claim_can_be_added_to_an_existing_machine_row(self, db):
        """An operator hand-pricing a book the pipeline already priced."""
        comic_id = upsert_comic(db, title="Batman", issue="251", year=1972)
        fmv_id = upsert_fmv(db, comic_id=comic_id, grade=5.5, low=400,
                            high=425, notes="window=±0.5 | cv=20%",
                            provenance="machine")
        upsert_fmv(db, comic_id=comic_id, grade=5.5, low=250, high=300,
                   notes="anchored on the lone 4.0 sale", provenance="hand")
        assert _provenance(db, fmv_id) == "hand"

    def test_empty_string_is_no_claim_not_a_claim_of_empty(self, db):
        comic_id, fmv_id = self._priced_hand_row(db)
        upsert_fmv(db, comic_id=comic_id, grade=6.5, low=120, high=150,
                   notes="window=±0.5", provenance="")
        assert _provenance(db, fmv_id) == "hand"

    def test_a_provenance_only_post_does_not_stamp_updated_at(self, db):
        """`updated_at` is what the freshness cache reads. Claiming an old
        row's provenance must not make it look freshly priced, or the claim
        would silently extend the cache window on a stale number."""
        comic_id, fmv_id = self._priced_hand_row(db)
        db.execute("UPDATE fmv SET updated_at='2020-01-01T00:00:00' WHERE id=?",
                   (fmv_id,))
        upsert_fmv(db, comic_id=comic_id, grade=6.5, provenance="hand")
        assert db.execute("SELECT updated_at FROM fmv WHERE id=?",
                          (fmv_id,)).fetchone()[0] == "2020-01-01T00:00:00"


# ─── The read path the guard actually consumes ────────────────────────────────

class TestListComicsServesProvenance:
    def test_field_is_present_on_every_row(self, db):
        comic_id = upsert_comic(db, title="Amazing Spider-Man", issue="50",
                                year=1967)
        upsert_fmv(db, comic_id=comic_id, grade=6.5, low=600, high=680,
                   notes="OVERRIDE: operator band", provenance="hand")
        rows = [dict(r) for r in list_comics(db, title="Amazing Spider-Man",
                                             issue="50", grade=6.5)]
        assert len(rows) == 1
        assert rows[0]["fmv_provenance"] == "hand"

    def test_an_unclaimed_row_serves_null_not_a_missing_key(self, db):
        """The guard distinguishes "claimed machine" from "never claimed" —
        the key must exist and be null, not be absent."""
        comic_id = upsert_comic(db, title="X", issue="1", year=1990)
        upsert_fmv(db, comic_id=comic_id, grade=9.0, low=10, high=20)
        row = dict(list_comics(db, title="X", issue="1", grade=9.0)[0])
        assert "fmv_provenance" in row
        assert row["fmv_provenance"] is None

    def test_the_identity_lookup_the_guard_uses_carries_it(self, db):
        """`comic-fmv` looks a book up by `(title, issue, grade)` — the key the
        WRITE uses (BUI-775). The column has to reach the guard through THAT
        query, not just through the dashboard's."""
        comic_id = upsert_comic(db, title="Amazing Spider-Man", issue="50",
                                year=1967)
        upsert_fmv(db, comic_id=comic_id, grade=6.5, low=600, high=680,
                   notes="OVERRIDE: operator band", provenance="hand")
        rows = [dict(r) for r in list_comics(db, title="amazing spider-man",
                                             issue="50", grade=6.5)]
        assert [r["fmv_provenance"] for r in rows] == ["hand"]


# ─── The wire contract ────────────────────────────────────────────────────────

class TestUpsertComicRequestProvenance:
    @pytest.mark.parametrize("value", FMV_PROVENANCES)
    def test_accepts_every_vocabulary_value(self, value):
        req = UpsertComicRequest(title="X", issue="1", fmv_provenance=value)
        assert req.fmv_provenance == value

    def test_omitting_it_is_valid_and_means_no_claim(self):
        assert UpsertComicRequest(title="X", issue="1").fmv_provenance is None

    def test_empty_string_normalizes_to_no_claim(self):
        assert UpsertComicRequest(
            title="X", issue="1", fmv_provenance="").fmv_provenance is None

    @pytest.mark.parametrize("value", ["Hand", "hand-priced", "OVERRIDE",
                                       "operator", "HAND"])
    def test_a_misspelled_claim_is_rejected_loudly(self, value):
        """The whole defect being fixed is a claim that could be misspelled
        into invisibility. A wrong spelling must 422, not be stored as an
        unreadable claim — and the 422 discards the entire upsert, so nothing
        is overwritten while the operator retries."""
        with pytest.raises(ValueError):
            UpsertComicRequest(title="X", issue="1", fmv_provenance=value)


class TestPostApiComicsProvenance:
    def test_round_trips_through_post_then_get(self, api):
        resp = api.post("/api/comics", json={
            "title": "Amazing Spider-Man", "issue": "50", "year": 1967,
            "grade": 6.5, "fmv_low": 600, "fmv_high": 680,
            "fmv_notes": "OVERRIDE: operator band, CLI pool rejected",
            "fmv_provenance": "hand",
        })
        assert resp.status_code == 200
        rows = api.get("/api/comics", params={
            "title": "Amazing Spider-Man", "issue": "50", "grade": 6.5,
        }).json()
        assert [r["fmv_provenance"] for r in rows] == ["hand"]

    def test_a_later_machine_post_without_the_field_keeps_the_claim(self, api):
        """End to end over HTTP: `comic-fmv` posting a recompute WITHOUT the
        field (an older build) must not strip the operator's claim."""
        base = {"title": "Amazing Spider-Man", "issue": "50", "year": 1967,
                "grade": 6.5}
        api.post("/api/comics", json={**base, "fmv_low": 600, "fmv_high": 680,
                                      "fmv_provenance": "hand"})
        api.post("/api/comics", json={**base, "fmv_low": 120, "fmv_high": 150,
                                      "fmv_notes": "window=±0.5 | cv=20%"})
        rows = api.get("/api/comics", params={
            "title": "Amazing Spider-Man", "issue": "50", "grade": 6.5,
        }).json()
        assert rows[0]["fmv_provenance"] == "hand"

    def test_an_unreadable_claim_422s_and_writes_nothing(self, api):
        resp = api.post("/api/comics", json={
            "title": "Amazing Spider-Man", "issue": "50", "year": 1967,
            "grade": 6.5, "fmv_low": 600, "fmv_high": 680,
            "fmv_provenance": "OVERRIDE",
        })
        assert resp.status_code == 422
        assert api.get("/api/comics", params={
            "title": "Amazing Spider-Man", "issue": "50"}).json() == []
