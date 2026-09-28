"""Tests for the BUI-998/BUI-1008 generic comps restamp path.

BUI-993/BUI-997 fixed `certifier` for slab comps (a writer fix plus a one-off
migration, both keyed on a one-line regex the server itself could re-run).
Two follow-ups needed the same shape but couldn't reuse BUI-997's migration:

  * BUI-998 — `label`/`page_quality` were never stamped at all for a slab
    comp that survives an `include_graded`-only fetch (see
    `apps/fmv/src/fmv_runner.py`'s `_slab_comps_only` and
    `apps/ebay/src/sold_comps.py`'s `_run`).
  * BUI-1008 — BUI-1003 fixed `sold_comps.parse_grade` for split-grade
    phrasings, but every raw `comps` row fetched before that fix keeps its
    old, wrong `grade` until the ledger re-observes the same sale.

Both rules live in `apps/ebay` (parse_grade, parse_slab_fields), which this
server cannot import — so the fix is generic: a compare-and-set endpoint the
`apps/ebay` restamp script posts computed diffs to, covering both tickets
with ONE endpoint rather than two near-identical migrations.

Three things are pinned here, in order:

  1. `list_all_comps` — the enumeration read `GET /api/comics/comps` cannot
     do (it always resolves one book first).
  2. `restamp_comps` — compare-and-set semantics, the id/not_found/matched
     partition, and that `certifier` is out of scope (BUI-997 already fixed
     it).
  3. `POST /api/comics/comps/restamp` / `GET /api/comics/comps/all` — the
     HTTP surface: dry-run default, 422 vocabulary boundary, batch
     atomicity on a refused item.
"""
from __future__ import annotations

import sqlite3

import pytest

from gixen_overlay.db import (
    COMPS_RESTAMP_FIELDS,
    create_tables,
    list_all_comps,
    restamp_comps,
    upsert_comic,
    upsert_comps,
)


# ---------------------------------------------------------------------------
# DB-layer fixtures — mirrors test_comps_exclusion.py's `_make_db`/`_comp`
# ---------------------------------------------------------------------------


def _make_db(path: str = ":memory:") -> sqlite3.Connection:
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
        "pool": "raw",
        "provider": "sold-comps.com",
        "product_id": "366561460527",
        "title": "Amazing Spider-Man #50 F/VF",
        "price": 250.0,
        "sold_date": "2026-08-02",
        "grade": 6.0,
        "buying_format": "auction",
        "link": "https://ebay.com/itm/366561460527",
        "query": "Amazing Spider-Man 50",
        "tier": "1",
        "from_cache": False,
        "observed_at": "2026-08-02T00:00:00Z",
        "provenance": "live",
        "certifier": "none",
        "label": "universal",
        "page_quality": "unknown",
    }
    base.update(overrides)
    return base


def _slab_comp(**overrides) -> dict:
    base = _comp(
        pool="slab", product_id="16410", title="Batman #227 CGC 6.5",
        certifier="cgc", label="universal", page_quality="unknown",
    )
    base.update(overrides)
    return base


def _row_id(conn, product_id: str) -> int:
    return conn.execute(
        "SELECT id FROM comps WHERE product_id=?", (product_id,)
    ).fetchone()[0]


# ---------------------------------------------------------------------------
# 1. list_all_comps
# ---------------------------------------------------------------------------


def test_list_all_comps_returns_every_row_with_no_filter(db):
    comic_id = upsert_comic(db, "Invincible", "1", 2003)
    upsert_comps(db, comic_id, [
        _comp(product_id="a"), _comp(product_id="b"),
        _slab_comp(product_id="c"),
    ])
    rows = list_all_comps(db)
    assert {r["product_id"] for r in rows} == {"a", "b", "c"}


def test_list_all_comps_pages_by_id_cursor(db):
    comic_id = upsert_comic(db, "Invincible", "1", 2003)
    upsert_comps(db, comic_id, [_comp(product_id=str(i)) for i in range(5)])
    first_page = list_all_comps(db, limit=2)
    assert len(first_page) == 2
    second_page = list_all_comps(db, after_id=first_page[-1]["id"], limit=2)
    assert [r["id"] for r in second_page] != [r["id"] for r in first_page]
    assert all(r["id"] > first_page[-1]["id"] for r in second_page)


def test_list_all_comps_filters_by_pool(db):
    comic_id = upsert_comic(db, "Invincible", "1", 2003)
    upsert_comps(db, comic_id, [_comp(product_id="raw1"), _slab_comp(product_id="slab1")])
    rows = list_all_comps(db, pool="slab")
    assert [r["product_id"] for r in rows] == ["slab1"]


def test_list_all_comps_caps_limit_server_side(db):
    from gixen_overlay.db import MAX_COMPS_ALL_LIMIT
    comic_id = upsert_comic(db, "Invincible", "1", 2003)
    upsert_comps(db, comic_id, [_comp(product_id="only")])
    # A limit far past the cap must not raise or silently do something else —
    # it just clamps.
    rows = list_all_comps(db, limit=MAX_COMPS_ALL_LIMIT * 10)
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# 2. restamp_comps — compare-and-set semantics
# ---------------------------------------------------------------------------


def test_dry_run_previews_without_writing(db):
    comic_id = upsert_comic(db, "Invincible", "1", 2003)
    upsert_comps(db, comic_id, [_comp(product_id="a", grade=6.0)])
    row_id = _row_id(db, "a")

    result = restamp_comps(
        db, [{"id": row_id, "field": "grade", "expected": 6.0, "new": 6.5}],
        dry_run=True,
    )

    assert result["dry_run"] is True
    assert result["matched"] == 1
    assert result["changed"] == 1
    assert result["skipped_stale"] == 0
    assert result["not_found"] == 0
    stored = db.execute("SELECT grade FROM comps WHERE id=?", (row_id,)).fetchone()
    assert stored["grade"] == 6.0  # untouched


def test_apply_writes_the_new_grade(db):
    comic_id = upsert_comic(db, "Invincible", "1", 2003)
    upsert_comps(db, comic_id, [_comp(product_id="a", grade=6.0)])
    row_id = _row_id(db, "a")

    result = restamp_comps(
        db, [{"id": row_id, "field": "grade", "expected": 6.0, "new": 6.5}],
        dry_run=False,
    )

    assert result["changed"] == 1
    stored = db.execute("SELECT grade FROM comps WHERE id=?", (row_id,)).fetchone()
    assert stored["grade"] == 6.5


def test_apply_writes_label_and_page_quality(db):
    """BUI-998's own shape: a slab row missing label/page_quality entirely
    (the pre-fix writer's output) restamped to what the title actually
    names."""
    comic_id = upsert_comic(db, "Invincible", "1", 2003)
    upsert_comps(db, comic_id, [
        _slab_comp(product_id="c", label="universal", page_quality="unknown"),
    ])
    row_id = _row_id(db, "c")

    result = restamp_comps(db, [
        {"id": row_id, "field": "label", "expected": "universal",
         "new": "signature_series"},
        {"id": row_id, "field": "page_quality", "expected": "unknown",
         "new": "white"},
    ], dry_run=False)

    assert result["changed"] == 2
    stored = db.execute(
        "SELECT label, page_quality FROM comps WHERE id=?", (row_id,)
    ).fetchone()
    assert stored["label"] == "signature_series"
    assert stored["page_quality"] == "white"


def test_stale_expected_is_skipped_and_never_written(db):
    """Compare-and-set: if the CURRENT stored value no longer matches
    `expected` (some other writer already changed it since the caller's
    read), the row is reported skipped_stale and left untouched — never
    overwritten blind."""
    comic_id = upsert_comic(db, "Invincible", "1", 2003)
    upsert_comps(db, comic_id, [_comp(product_id="a", grade=7.0)])
    row_id = _row_id(db, "a")

    result = restamp_comps(
        db, [{"id": row_id, "field": "grade", "expected": 6.0, "new": 6.5}],
        dry_run=False,
    )

    assert result["changed"] == 0
    assert result["skipped_stale"] == 1
    assert result["matched"] == 1
    stored = db.execute("SELECT grade FROM comps WHERE id=?", (row_id,)).fetchone()
    assert stored["grade"] == 7.0


def test_unknown_id_is_reported_not_found_not_raised(db):
    result = restamp_comps(
        db, [{"id": 999999, "field": "grade", "expected": 6.0, "new": 6.5}],
        dry_run=False,
    )
    assert result["not_found"] == 1
    assert result["matched"] == 0
    assert result["results"][0]["status"] == "not_found"


def test_matched_partitions_into_changed_and_skipped_stale(db):
    comic_id = upsert_comic(db, "Invincible", "1", 2003)
    upsert_comps(db, comic_id, [
        _comp(product_id="a", grade=6.0), _comp(product_id="b", grade=7.0),
    ])
    id_a, id_b = _row_id(db, "a"), _row_id(db, "b")

    result = restamp_comps(db, [
        {"id": id_a, "field": "grade", "expected": 6.0, "new": 6.5},  # changes
        {"id": id_b, "field": "grade", "expected": 999.0, "new": 6.5},  # stale
        {"id": 424242, "field": "grade", "expected": 6.0, "new": 6.5},  # missing
    ], dry_run=False)

    assert result["matched"] == 2
    assert result["changed"] == 1
    assert result["skipped_stale"] == 1
    assert result["not_found"] == 1


def test_a_row_already_matching_new_is_a_no_op_change(db):
    """expected == current == new is still reported `changed` (it passes
    compare-and-set) — the caller's diff logic is expected to have already
    excluded a genuinely no-op row before posting it; this endpoint's own
    contract is compare-and-set, not "only send me real diffs"."""
    comic_id = upsert_comic(db, "Invincible", "1", 2003)
    upsert_comps(db, comic_id, [_comp(product_id="a", grade=6.0)])
    row_id = _row_id(db, "a")
    result = restamp_comps(
        db, [{"id": row_id, "field": "grade", "expected": 6.0, "new": 6.0}],
        dry_run=False,
    )
    assert result["changed"] == 1


def test_db_layer_refuses_an_unknown_field(db):
    """Defense in depth behind `models.CompsRestampItem` — the same posture
    `stamp_comps_excluded` takes on `code`."""
    comic_id = upsert_comic(db, "Invincible", "1", 2003)
    upsert_comps(db, comic_id, [_comp(product_id="a")])
    row_id = _row_id(db, "a")
    with pytest.raises(ValueError):
        restamp_comps(
            db, [{"id": row_id, "field": "certifier",
                  "expected": "none", "new": "cgc"}],
            dry_run=False,
        )


def test_certifier_is_not_a_restampable_field():
    assert "certifier" not in COMPS_RESTAMP_FIELDS
    assert set(COMPS_RESTAMP_FIELDS) == {"grade", "label", "page_quality"}


def test_one_transaction_a_mid_batch_db_layer_error_leaves_earlier_writes(db):
    """The loop commits once, after every item — an earlier item's UPDATE is
    still visible mid-call (same connection), so a later item in the SAME
    batch correctly sees it as the new current value rather than racing its
    own transaction."""
    comic_id = upsert_comic(db, "Invincible", "1", 2003)
    upsert_comps(db, comic_id, [_comp(product_id="a", grade=6.0)])
    row_id = _row_id(db, "a")

    result = restamp_comps(db, [
        {"id": row_id, "field": "grade", "expected": 6.0, "new": 6.5},
        # Same id again, in the same call: by the time this item runs the
        # row's grade is already 6.5 (the first item's write), so an
        # `expected` of 6.0 is now stale.
        {"id": row_id, "field": "grade", "expected": 6.0, "new": 7.0},
    ], dry_run=False)

    assert result["changed"] == 1
    assert result["skipped_stale"] == 1
    stored = db.execute("SELECT grade FROM comps WHERE id=?", (row_id,)).fetchone()
    assert stored["grade"] == 6.5


# ---------------------------------------------------------------------------
# 3. The HTTP surface
# ---------------------------------------------------------------------------


def _create_comic(api, **overrides) -> int:
    body = {"title": "Invincible", "issue": "1", "year": 2003}
    body.update(overrides)
    return api.post("/api/comics", json=body).json()["comic_id"]


def _ingest(api, comic_id, *comps) -> None:
    r = api.post("/api/comics/comps",
                 json={"comic_id": comic_id, "comps": list(comps)})
    assert r.status_code == 200, r.text


def _comps_all_row_id(api, product_id: str) -> int:
    rows = api.get("/api/comics/comps/all").json()
    return next(r["id"] for r in rows if r["product_id"] == product_id)


def test_comps_all_endpoint_enumerates_without_a_book_identity(api):
    comic_id = _create_comic(api)
    _ingest(api, comic_id, _comp(product_id="a"), _slab_comp(product_id="c"))

    rows = api.get("/api/comics/comps/all").json()

    assert {r["product_id"] for r in rows} == {"a", "c"}


def test_comps_all_endpoint_filters_by_pool_and_pages(api):
    comic_id = _create_comic(api)
    _ingest(api, comic_id, _comp(product_id="a"), _slab_comp(product_id="c"))

    raw_only = api.get("/api/comics/comps/all", params={"pool": "raw"}).json()
    assert [r["product_id"] for r in raw_only] == ["a"]

    page1 = api.get("/api/comics/comps/all", params={"limit": 1}).json()
    assert len(page1) == 1
    page2 = api.get(
        "/api/comics/comps/all",
        params={"after_id": page1[-1]["id"], "limit": 1},
    ).json()
    assert len(page2) == 1
    assert page2[0]["id"] != page1[0]["id"]


def test_restamp_endpoint_dry_run_default_writes_nothing(api):
    comic_id = _create_comic(api)
    _ingest(api, comic_id, _comp(product_id="a", grade=6.0))
    row_id = _comps_all_row_id(api, "a")

    r = api.post("/api/comics/comps/restamp", json={"items": [
        {"id": row_id, "field": "grade", "expected": 6.0, "new": 6.5},
    ]})

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["dry_run"] is True
    assert body["changed"] == 1
    served = api.get("/api/comics/comps", params={"comic_id": comic_id}).json()
    assert served[0]["grade"] == 6.0


def test_restamp_endpoint_apply_writes_exactly_the_matched_items(api):
    comic_id = _create_comic(api)
    _ingest(api, comic_id, _comp(product_id="a", grade=6.0))
    row_id = _comps_all_row_id(api, "a")

    r = api.post("/api/comics/comps/restamp", json={
        "dry_run": False,
        "items": [{"id": row_id, "field": "grade", "expected": 6.0, "new": 6.5}],
    })

    assert r.status_code == 200, r.text
    assert r.json()["changed"] == 1
    served = api.get("/api/comics/comps", params={"comic_id": comic_id}).json()
    assert served[0]["grade"] == 6.5


def test_restamp_endpoint_stale_expected_is_skipped(api):
    comic_id = _create_comic(api)
    _ingest(api, comic_id, _comp(product_id="a", grade=7.0))
    row_id = _comps_all_row_id(api, "a")

    r = api.post("/api/comics/comps/restamp", json={
        "dry_run": False,
        "items": [{"id": row_id, "field": "grade", "expected": 6.0, "new": 6.5}],
    })

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["skipped_stale"] == 1
    assert body["changed"] == 0
    served = api.get("/api/comics/comps", params={"comic_id": comic_id}).json()
    assert served[0]["grade"] == 7.0


def test_restamp_endpoint_reports_unknown_id_as_not_found(api):
    r = api.post("/api/comics/comps/restamp", json={
        "dry_run": False,
        "items": [{"id": 999999, "field": "grade", "expected": 6.0, "new": 6.5}],
    })
    assert r.status_code == 200, r.text
    assert r.json()["not_found"] == 1


def test_restamp_endpoint_writes_label_and_page_quality(api):
    comic_id = _create_comic(api)
    _ingest(api, comic_id, _slab_comp(
        product_id="c", label="universal", page_quality="unknown"))
    row_id = _comps_all_row_id(api, "c")

    r = api.post("/api/comics/comps/restamp", json={
        "dry_run": False,
        "items": [
            {"id": row_id, "field": "label", "expected": "universal",
             "new": "signature_series"},
            {"id": row_id, "field": "page_quality", "expected": "unknown",
             "new": "white"},
        ],
    })

    assert r.status_code == 200, r.text
    assert r.json()["changed"] == 2
    served = api.get("/api/comics/comps", params={"comic_id": comic_id}).json()
    assert served[0]["label"] == "signature_series"
    assert served[0]["page_quality"] == "white"


def test_restamp_endpoint_422s_an_out_of_vocabulary_label(api):
    comic_id = _create_comic(api)
    _ingest(api, comic_id, _slab_comp(product_id="c"))
    row_id = _comps_all_row_id(api, "c")

    r = api.post("/api/comics/comps/restamp", json={
        "dry_run": False,
        "items": [{"id": row_id, "field": "label", "expected": "universal",
                    "new": "not_a_real_label"}],
    })

    assert r.status_code == 422
    served = api.get("/api/comics/comps", params={"comic_id": comic_id}).json()
    assert served[0]["label"] == "universal"  # nothing written


def test_restamp_endpoint_422s_an_unknown_field(api):
    comic_id = _create_comic(api)
    _ingest(api, comic_id, _comp(product_id="a"))
    row_id = _comps_all_row_id(api, "a")

    r = api.post("/api/comics/comps/restamp", json={
        "dry_run": False,
        "items": [{"id": row_id, "field": "certifier",
                    "expected": "none", "new": "cgc"}],
    })
    assert r.status_code == 422


def test_restamp_endpoint_422s_a_mixed_batch_refuses_the_whole_call(api):
    """One bad item in a larger batch must refuse the WHOLE call before
    anything is written — same all-or-nothing-at-validation-time posture
    CompsIngestRequest already takes."""
    comic_id = _create_comic(api)
    _ingest(api, comic_id, _comp(product_id="a", grade=6.0))
    row_id = _comps_all_row_id(api, "a")

    r = api.post("/api/comics/comps/restamp", json={
        "dry_run": False,
        "items": [
            {"id": row_id, "field": "grade", "expected": 6.0, "new": 6.5},
            {"id": row_id, "field": "label", "expected": "universal",
             "new": "not_a_real_label"},
        ],
    })

    assert r.status_code == 422
    served = api.get("/api/comics/comps", params={"comic_id": comic_id}).json()
    assert served[0]["grade"] == 6.0  # the valid item never landed either


def test_restamp_endpoint_422s_an_empty_items_list(api):
    r = api.post("/api/comics/comps/restamp", json={"items": []})
    assert r.status_code == 422
