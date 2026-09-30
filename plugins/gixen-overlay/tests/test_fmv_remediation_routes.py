"""Tests for the BUI-1023/BUI-1030 remediation routes.

  * POST /api/bids/{item_id}/unlink-fmv          (BUI-1023a)
  * POST /api/comics/comps/exclude-by-id         (BUI-1023b)
  * POST /api/comics/fmv/{fmv_id}/retire         (BUI-1030)

Seeding goes through raw sqlite on the test DB (as the neighbouring route
tests do) because the stubs under test sit on `comics` rows titled "The X-Men"
that `POST /api/comics` would never create, and because the dangling
`bids.fmv_id` case (no bid_fmvs row) has no API path at all.
"""
from __future__ import annotations

import os
import sqlite3

import pytest


def _raw() -> sqlite3.Connection:
    conn = sqlite3.connect(os.environ["DB_PATH"])
    conn.row_factory = sqlite3.Row
    return conn


def _comic(title="X-Men", issue="49", year=1968, variant=None) -> int:
    c = _raw()
    cur = c.execute(
        "INSERT INTO comics (title, issue, year, variant) VALUES (?,?,?,?)",
        (title, issue, year, variant),
    )
    c.commit()
    cid = cur.lastrowid
    c.close()
    return cid


def _fmv(comic_id, grade=6.0, low=None, high=None, flag=None) -> int:
    c = _raw()
    cur = c.execute(
        "INSERT INTO fmv (comic_id, grade, low, high, flag_reason) VALUES (?,?,?,?,?)",
        (comic_id, grade, low, high, flag),
    )
    c.commit()
    fid = cur.lastrowid
    c.close()
    return fid


def _bid(api, item_id="700000001") -> int:
    assert api.post("/api/bids", json={"item_id": item_id, "max_bid": 20.0}).status_code == 200
    c = _raw()
    bid_id = c.execute("SELECT id FROM bids WHERE item_id=?", (item_id,)).fetchone()[0]
    c.close()
    return bid_id


def _link(bid_id, fmv_id, primary=0, legacy=False) -> None:
    c = _raw()
    c.execute("INSERT INTO bid_fmvs (bid_id, fmv_id, is_primary) VALUES (?,?,?)",
              (bid_id, fmv_id, primary))
    if legacy:
        c.execute("UPDATE bids SET fmv_id=? WHERE id=?", (fmv_id, bid_id))
    c.commit()
    c.close()


def _set_legacy(bid_id, fmv_id) -> None:
    c = _raw()
    c.execute("UPDATE bids SET fmv_id=? WHERE id=?", (fmv_id, bid_id))
    c.commit()
    c.close()


def _state(bid_id):
    c = _raw()
    links = [tuple(r) for r in c.execute(
        "SELECT fmv_id, is_primary FROM bid_fmvs WHERE bid_id=? ORDER BY fmv_id", (bid_id,))]
    legacy = c.execute("SELECT fmv_id FROM bids WHERE id=?", (bid_id,)).fetchone()[0]
    c.close()
    return links, legacy


# ---------------------------------------------------------------------------
# unlink-fmv
# ---------------------------------------------------------------------------


def test_unlink_dry_run_is_the_default_and_writes_nothing(api):
    bid = _bid(api)
    wrong = _fmv(_comic("Wrong", "1"))
    good = _fmv(_comic("Right", "1"))
    _link(bid, wrong, primary=1, legacy=True)
    _link(bid, good, primary=0)
    before = _state(bid)

    r = api.post("/api/bids/700000001/unlink-fmv", json={"fmv_id": wrong})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["dry_run"] is True
    assert body["removed_link"]["fmv_id"] == wrong
    assert body["removed_link"]["was_primary"] is True
    assert body["removed_link"]["fmv"]["comic"]["title"] == "Wrong"
    assert body["legacy_fmv_id_before"] == wrong
    assert body["legacy_fmv_id_after"] == good
    assert body["promoted_fmv_id"] == good
    assert body["remaining_links"] == [{"fmv_id": good, "is_primary": 1}]
    assert _state(bid) == before


def test_unlink_apply_removes_link_and_repoints_legacy_to_promoted(api):
    bid = _bid(api)
    wrong = _fmv(_comic("Wrong", "1"))
    good = _fmv(_comic("Right", "1"))
    _link(bid, wrong, primary=1, legacy=True)
    _link(bid, good, primary=0)

    r = api.post("/api/bids/700000001/unlink-fmv",
                 json={"fmv_id": wrong, "dry_run": False})
    assert r.status_code == 200, r.text
    assert _state(bid) == ([(good, 1)], good)


def test_unlink_last_link_nulls_the_legacy_column(api):
    bid = _bid(api)
    wrong = _fmv(_comic("Wrong", "1"))
    _link(bid, wrong, primary=1, legacy=True)
    r = api.post("/api/bids/700000001/unlink-fmv",
                 json={"fmv_id": wrong, "dry_run": False})
    assert r.status_code == 200
    assert _state(bid) == ([], None)


def test_unlink_non_primary_leaves_legacy_and_primary_alone(api):
    bid = _bid(api)
    keep = _fmv(_comic("Keep", "1"))
    member = _fmv(_comic("Member", "2"))
    _link(bid, keep, primary=1, legacy=True)
    _link(bid, member, primary=0)
    r = api.post("/api/bids/700000001/unlink-fmv",
                 json={"fmv_id": member, "dry_run": False})
    assert r.status_code == 200
    assert _state(bid) == ([(keep, 1)], keep)


def test_unlink_dangling_legacy_pointer_alone_is_cleared(api):
    """bids.fmv_id set with NO bid_fmvs row: still a link the route removes."""
    bid = _bid(api)
    ghost = _fmv(_comic("Ghost", "1"))
    _set_legacy(bid, ghost)
    r = api.post("/api/bids/700000001/unlink-fmv",
                 json={"fmv_id": ghost, "dry_run": False})
    assert r.status_code == 200, r.text
    assert r.json()["removed_link"]["bid_fmvs_row_existed"] is False
    assert _state(bid) == ([], None)


def test_unlink_missing_link_is_404_and_lists_current_links(api):
    bid = _bid(api)
    real = _fmv(_comic("Real", "1"))
    _link(bid, real, primary=1, legacy=True)
    r = api.post("/api/bids/700000001/unlink-fmv", json={"fmv_id": 999999})
    assert r.status_code == 404
    detail = r.json()["detail"]
    assert detail["bid_rows"][0]["links"][0]["fmv_id"] == real
    assert detail["bid_rows"][0]["links"][0]["comic_id"] is not None
    assert _state(bid) == ([(real, 1)], real)


def test_unlink_unknown_item_is_404_and_non_numeric_is_422(api):
    assert api.post("/api/bids/999999999/unlink-fmv", json={"fmv_id": 1}).status_code == 404
    assert api.post("/api/bids/abc/unlink-fmv", json={"fmv_id": 1}).status_code == 422


def test_unlink_several_bid_rows_needs_bid_id(api):
    first = _bid(api)
    c = _raw()
    # A second row for the same item_id (a re-add): item_id is not unique.
    c.execute("DROP INDEX IF EXISTS idx_bids_item_id_active")
    c.commit()
    try:
        c.execute(
            "INSERT INTO bids (item_id, max_bid, status) VALUES ('700000001', 5, 'ENDED')"
        )
        c.commit()
    except sqlite3.IntegrityError:
        pytest.skip("schema forbids a second bid row for one item_id")
    second = c.execute("SELECT MAX(id) FROM bids").fetchone()[0]
    c.close()
    f = _fmv(_comic("Dup", "1"))
    _link(first, f, primary=1)
    _link(second, f, primary=1)

    r = api.post("/api/bids/700000001/unlink-fmv", json={"fmv_id": f, "dry_run": False})
    assert r.status_code == 409
    assert sorted(r.json()["detail"]["candidate_bid_ids"]) == sorted([first, second])
    assert _state(first)[0] and _state(second)[0]

    r = api.post("/api/bids/700000001/unlink-fmv",
                 json={"fmv_id": f, "bid_id": second, "dry_run": False})
    assert r.status_code == 200
    assert _state(second)[0] == [] and _state(first)[0] == [(f, 1)]


# ---------------------------------------------------------------------------
# comps/exclude-by-id
# ---------------------------------------------------------------------------


def _comp_row(comic_id=None, pool="raw", product_id="p1", excluded_code=None) -> int:
    c = _raw()
    cur = c.execute(
        "INSERT INTO comps (comic_id, pool, provider, product_id, title, price, "
        "sold_date, provenance, excluded_code, first_seen_at, last_seen_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (comic_id, pool, "sold-comps.com", product_id, "t", 10.0, "2026-09-01",
         "live", excluded_code, "2026-09-01T00:00:00Z", "2026-09-01T00:00:00Z"),
    )
    c.commit()
    rid = cur.lastrowid
    c.close()
    return rid


def _comp_state(rid):
    c = _raw()
    row = c.execute("SELECT excluded_code, excluded_at FROM comps WHERE id=?", (rid,)).fetchone()
    c.close()
    return tuple(row)


def test_exclude_by_id_reaches_a_null_comic_row_the_old_route_cannot(api):
    rid = _comp_row(comic_id=None)
    # The comic_id-keyed route cannot address it at all.
    old = api.post("/api/comics/comps/exclude", json={
        "comic_id": 1, "product_ids": ["p1"], "code": "manual", "pool": "raw"})
    assert old.status_code == 404

    dry = api.post("/api/comics/comps/exclude-by-id",
                   json={"ids": [rid], "code": "manual"})
    assert dry.status_code == 200, dry.text
    assert dry.json()["dry_run"] is True
    assert [r["id"] for r in dry.json()["stamped"]] == [rid]
    assert dry.json()["stamped"][0]["comic_id"] is None
    assert _comp_state(rid) == (None, None)

    applied = api.post("/api/comics/comps/exclude-by-id",
                       json={"ids": [rid], "code": "manual", "dry_run": False})
    assert applied.status_code == 200
    code, at = _comp_state(rid)
    assert code == "manual" and at is not None


def test_exclude_by_id_is_first_stamp_wins_and_buckets_the_rest(api):
    done = _comp_row(product_id="a", excluded_code="printing")
    slab = _comp_row(pool="slab", product_id="b")
    fresh = _comp_row(product_id="c")
    before = _comp_state(done)

    r = api.post("/api/comics/comps/exclude-by-id", json={
        "ids": [done, slab, fresh, 424242, fresh], "code": "manual",
        "pool": "raw", "dry_run": False})
    assert r.status_code == 200, r.text
    body = r.json()
    assert [x["id"] for x in body["stamped"]] == [fresh]
    assert [x["id"] for x in body["already_stamped"]] == [done]
    assert [x["id"] for x in body["wrong_pool"]] == [slab]
    assert body["not_found"] == [424242]
    assert _comp_state(done) == before          # original code/time kept
    assert _comp_state(slab) == (None, None)    # other pool untouched
    assert _comp_state(fresh)[0] == "manual"


def test_exclude_by_id_refuses_unknown_code_and_empty_ids(api):
    rid = _comp_row()
    assert api.post("/api/comics/comps/exclude-by-id",
                    json={"ids": [rid], "code": "nope", "dry_run": False}).status_code == 422
    assert api.post("/api/comics/comps/exclude-by-id",
                    json={"ids": [], "code": "manual"}).status_code == 422
    assert api.post("/api/comics/comps/exclude-by-id",
                    json={"ids": [rid], "code": "manual", "pool": "x"}).status_code == 422
    assert _comp_state(rid) == (None, None)


# ---------------------------------------------------------------------------
# fmv/{id}/retire
# ---------------------------------------------------------------------------


def _stub_and_replacement(**repl_kw):
    stub = _fmv(_comic("The X-Men", "49", 1968), grade=6.0)
    repl = _fmv(_comic("X-Men", "49", 1968), grade=6.0, **repl_kw)
    return stub, repl


def _fmv_row(fid):
    c = _raw()
    row = dict(c.execute("SELECT * FROM fmv WHERE id=?", (fid,)).fetchone())
    c.close()
    return row


def test_retire_dry_run_reports_inbound_incl_dangling_and_writes_nothing(api):
    stub, repl = _stub_and_replacement(low=10.0, high=20.0)
    linked = _bid(api, "700000001")
    dangling = _bid(api, "700000002")
    _link(linked, stub, primary=1, legacy=True)
    _set_legacy(dangling, stub)   # bids.fmv_id only: a JOIN through bid_fmvs misses it
    before = (_state(linked), _state(dangling), _fmv_row(stub))

    r = api.post(f"/api/comics/fmv/{stub}/retire", json={"replacement_fmv_id": repl})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["dry_run"] is True and body["retired"] is False
    assert body["identity_check"]["ok"] is True
    assert [x["bid_id"] for x in body["inbound"]["bid_fmvs"]] == [linked]
    ptrs = {x["id"]: x["dangling"] for x in body["inbound"]["bids_fmv_id"]}
    assert ptrs == {linked: False, dangling: True}
    assert {"table": "bids", "column": "fmv_id"} in body["scanned_reference_columns"]
    assert (_state(linked), _state(dangling), _fmv_row(stub)) == before


def test_retire_apply_repoints_everything_and_flags_the_stub(api):
    stub, repl = _stub_and_replacement(low=10.0, high=20.0)
    linked = _bid(api, "700000001")
    dangling = _bid(api, "700000002")
    both = _bid(api, "700000003")
    _link(linked, stub, primary=1, legacy=True)
    _set_legacy(dangling, stub)
    _link(both, stub, primary=1, legacy=True)
    _link(both, repl, primary=0)        # already links the replacement

    r = api.post(f"/api/comics/fmv/{stub}/retire",
                 json={"replacement_fmv_id": repl, "dry_run": False})
    assert r.status_code == 200, r.text
    assert r.json()["retired"] is True
    assert _state(linked) == ([(repl, 1)], repl)
    assert _state(dangling) == ([], repl)           # pointer repointed, no junction invented
    assert _state(both) == ([(repl, 1)], repl)      # dedup-safe, primary carried over
    row = _fmv_row(stub)
    assert row["flag_reason"] == "superseded"
    assert f"superseded_by={repl}" in row["notes"]
    assert row["low"] is None and row["high"] is None
    # Nothing still references the stub, and the row was not deleted.
    c = _raw()
    assert c.execute("SELECT COUNT(*) FROM bid_fmvs WHERE fmv_id=?", (stub,)).fetchone()[0] == 0
    assert c.execute("SELECT COUNT(*) FROM bids WHERE fmv_id=?", (stub,)).fetchone()[0] == 0
    c.close()
    # Re-running is refused only for real reasons: a retired stub with no
    # inbound refs retires again as a no-op.
    again = api.post(f"/api/comics/fmv/{stub}/retire",
                     json={"replacement_fmv_id": repl, "dry_run": False})
    assert again.status_code == 200


def test_retire_refuses_a_priced_stub(api):
    stub = _fmv(_comic("The X-Men", "49"), grade=6.0, low=1.0, high=2.0)
    repl = _fmv(_comic("X-Men", "49"), grade=6.0)
    r = api.post(f"/api/comics/fmv/{stub}/retire",
                 json={"replacement_fmv_id": repl, "dry_run": False})
    assert r.status_code == 409
    assert any("priced" in x for x in r.json()["detail"]["reasons"])
    assert _fmv_row(stub)["flag_reason"] is None


@pytest.mark.parametrize("stub_comic,repl_comic,grade,field", [
    (("The X-Men", "49", 1968, None), ("X-Men", "50", 1968, None), 6.0, "issue"),
    (("The X-Men", "49", 1968, None), ("Uncanny X-Men", "49", 1968, None), 6.0, "title"),
    (("The X-Men", "49", 1968, None), ("X-Men", "49", 1991, None), 6.0, "year"),
    (("The X-Men", "49", 1968, None), ("X-Men", "49", 1968, "Newsstand"), 6.0, "variant"),
    (("The X-Men", "49", 1968, None), ("X-Men", "49", 1968, None), 7.0, "grade"),
])
def test_retire_refuses_identity_mismatch(api, stub_comic, repl_comic, grade, field):
    bid = _bid(api)
    stub = _fmv(_comic(*stub_comic), grade=6.0)
    repl = _fmv(_comic(*repl_comic), grade=grade)
    _link(bid, stub, primary=1, legacy=True)
    r = api.post(f"/api/comics/fmv/{stub}/retire",
                 json={"replacement_fmv_id": repl, "dry_run": False})
    assert r.status_code == 409, r.text
    assert field in r.json()["detail"]["reasons"][0]
    assert _state(bid) == ([(stub, 1)], stub)       # nothing moved
    assert _fmv_row(stub)["flag_reason"] is None


def test_retire_article_normalization_matches_the_fmv_runner(api):
    for stub_t, repl_t in (("The Mighty Thor", "Mighty Thor"),
                           ("A Distant Soil", "Distant Soil"),
                           ("the x-men", "X-Men")):
        stub = _fmv(_comic(stub_t, "127"), grade=4.0)
        repl = _fmv(_comic(repl_t, "127"), grade=4.0)
        r = api.post(f"/api/comics/fmv/{stub}/retire", json={"replacement_fmv_id": repl})
        assert r.status_code == 200, (stub_t, r.text)


def test_retire_missing_rows_and_self_replacement(api):
    stub, repl = _stub_and_replacement()
    assert api.post("/api/comics/fmv/999999/retire",
                    json={"replacement_fmv_id": repl}).status_code == 404
    assert api.post(f"/api/comics/fmv/{stub}/retire",
                    json={"replacement_fmv_id": 999999}).status_code == 404
    assert api.post(f"/api/comics/fmv/{stub}/retire",
                    json={"replacement_fmv_id": stub}).status_code == 409


def test_retire_refuses_a_stub_flagged_for_another_reason_and_a_retired_replacement(api):
    flagged = _fmv(_comic("The X-Men", "49"), grade=6.0, flag="too_sparse")
    repl = _fmv(_comic("X-Men", "49"), grade=6.0)
    assert api.post(f"/api/comics/fmv/{flagged}/retire",
                    json={"replacement_fmv_id": repl}).status_code == 409
    stub = _fmv(_comic("The X-Men", "2"), grade=6.0)
    gone = _fmv(_comic("X-Men", "2"), grade=6.0, flag="superseded")
    assert api.post(f"/api/comics/fmv/{stub}/retire",
                    json={"replacement_fmv_id": gone}).status_code == 409


def test_retire_allows_a_replacement_flagged_too_sparse(api):
    """BUI-1026's fifth replacement is too_sparse (flagged, unpriced)."""
    stub = _fmv(_comic("The X-Men", "15"), grade=6.0)
    repl = _fmv(_comic("X-Men", "15"), grade=6.0, flag="too_sparse")
    r = api.post(f"/api/comics/fmv/{stub}/retire",
                 json={"replacement_fmv_id": repl, "dry_run": False})
    assert r.status_code == 200, r.text
    assert _fmv_row(repl)["flag_reason"] == "too_sparse"


def test_retire_failure_midway_rolls_everything_back(api, monkeypatch):
    """One transaction: a failure after the repoint leaves no partial state."""
    import gixen_overlay.db as dbm

    stub, repl = _stub_and_replacement()
    bid = _bid(api)
    _link(bid, stub, primary=1, legacy=True)
    real = dbm._fmv_referencing_columns
    calls = {"n": 0}

    def boom(conn):
        calls["n"] += 1
        if calls["n"] >= 3:          # the post-apply re-scan
            raise RuntimeError("boom")
        return real(conn)

    monkeypatch.setattr(dbm, "_fmv_referencing_columns", boom)
    with pytest.raises(RuntimeError):
        api.post(f"/api/comics/fmv/{stub}/retire",
                 json={"replacement_fmv_id": repl, "dry_run": False})
    assert _state(bid) == ([(stub, 1)], stub)
    assert _fmv_row(stub)["flag_reason"] is None
