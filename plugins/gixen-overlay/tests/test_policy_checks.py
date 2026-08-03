"""Overlay FMV-aware pre-trade checks (BUI-620, plan unit U4).

Two layers of coverage:

- Most tests call `gixen_overlay.policy.check_bid_write(conn, intent)`
  directly against a raw connection (gixen-cli's `bids` schema via
  `server.db.init_db` + the overlay's comics/fmv/bid_fmvs schema via
  `gixen_overlay.db.create_tables`, on the SAME connection — exactly the
  shared-DB shape the real server uses). This is fast and lets each test
  pin exact boundary values without going through the FastAPI layer.
- A handful of tests use the `api` fixture (conftest.py's real
  `server.main.app` with the real overlay plugin registered) to prove the
  whole path — hookspec dispatch, the host's `advisories` envelope, and the
  `bid_decisions` ledger — end to end, not just the policy module in
  isolation.

`REPO_ROOT`/rung-canary pattern follows `tests/test_skill_contracts.py`
(KTD5's cited precedent) — that file is out of scope for this ticket, so
the canary lives here instead, per the ticket's own instruction.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from gixen_overlay import policy
from gixen_overlay.db import create_tables
from server.db import init_db
from server.policy import PolicyIntent

REPO_ROOT = Path(__file__).resolve().parents[3]


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def conn(tmp_path):
    """A real sqlite connection carrying BOTH gixen-cli's `bids` schema and
    the overlay's comics/fmv/bid_fmvs schema — the same shared-DB shape
    `server.main`'s `app.state.db` has in production."""
    c = init_db(tmp_path / "policy_checks_test.db")
    create_tables(c)
    yield c
    c.close()


def _intent(
    *, item_id: str, max_bid: float, trigger: str = "create",
    snipe_group: int = 0, prior_row=None, comic_identities=None,
) -> PolicyIntent:
    return PolicyIntent(
        item_id=item_id, target_max_bid=max_bid, snipe_group=snipe_group,
        trigger=trigger, prior_row=prior_row,
        comic_identities=comic_identities or [],
    )


def _insert_comic_fmv(
    conn, *, title="Test Comic", issue="1", year=2000, grade=9.0,
    low=80.0, high=100.0, comps=5, confidence=None, updated_at=None,
    comic_id=None,
):
    """Insert an fmv row at `grade`, creating the comic unless `comic_id` is
    given — pass an existing `comic_id` to add a SECOND grade to the same
    comic (a fresh title/issue/year insert would collide with the comics
    table's unique index)."""
    if comic_id is None:
        conn.execute(
            "INSERT INTO comics (title, issue, year) VALUES (?, ?, ?)",
            (title, issue, year),
        )
        comic_id = conn.execute(
            "SELECT id FROM comics WHERE title=? AND issue=? AND year=?",
            (title, issue, year),
        ).fetchone()["id"]
    if updated_at is None:
        updated_at = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO fmv (comic_id, grade, low, high, comps, confidence, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (comic_id, grade, low, high, comps, confidence, updated_at),
    )
    conn.commit()
    fmv_id = conn.execute(
        "SELECT id FROM fmv WHERE comic_id=? AND grade=?", (comic_id, grade),
    ).fetchone()["id"]
    return comic_id, fmv_id


def _insert_bid(conn, item_id, max_bid, *, snipe_group=0, status="PENDING"):
    conn.execute(
        "INSERT INTO bids (item_id, max_bid, snipe_group, status) VALUES (?, ?, ?, ?)",
        (item_id, max_bid, snipe_group, status),
    )
    conn.commit()
    return conn.execute(
        "SELECT * FROM bids WHERE item_id=? ORDER BY id DESC LIMIT 1", (item_id,),
    ).fetchone()


def _link(conn, bid_id, fmv_id, is_primary=True):
    conn.execute(
        "INSERT INTO bid_fmvs (bid_id, fmv_id, is_primary) VALUES (?, ?, ?)",
        (bid_id, fmv_id, int(is_primary)),
    )
    conn.commit()


def _by_code(results: list[dict], code: str) -> dict | None:
    return next((r for r in results if r["code"] == code), None)


# ---------------------------------------------------------------------------
# KTD5 — rung canary. Source-parses apps/fmv/src/fmv_math.py and compares
# against policy.py's duplicated constants.
# ---------------------------------------------------------------------------


def _extract_float(pattern: str, source: str, flags: int = 0) -> float:
    match = re.search(pattern, source, flags)
    assert match is not None, f"pattern {pattern!r} matched nothing in the source"
    return float(match.group(1))


def test_rung_extraction_helper_is_provably_able_to_fail():
    """KTD5: "assert the extraction is non-empty (proven able to fail)" — a
    canary whose extraction helper can never fail can't prove anything about
    the real file below. Feed it source text that does NOT contain the
    pattern and assert it raises."""
    with pytest.raises(AssertionError):
        _extract_float(r"THIS_CONSTANT_DOES_NOT_EXIST\s*=\s*([\d.]+)", "unrelated source text")


def test_rung_constants_match_fmv_math_source():
    """KTD5: policy.py duplicates fmv_math's rung ladder (0.80/0.70/0.60)
    because apps/fmv is not a workspace member — no import edge exists or
    should be created. If the real file's constants ever drift, this fails
    instead of the overlay's recomputed-cap check silently going stale."""
    math_src = (REPO_ROOT / "apps" / "fmv" / "src" / "fmv_math.py").read_text()

    base = _extract_float(r"BASE_BID_FACTOR\s*=\s*([\d.]+)", math_src)
    interpolated = _extract_float(r"INTERPOLATED_BID_FACTOR\s*=\s*([\d.]+)", math_src)
    medium_low = _extract_float(
        r'_CONF_RANK\["MEDIUM-LOW"\]:.*?return\s+([\d.]+)', math_src, re.DOTALL,
    )

    assert base == policy.RUNG_HIGH_CONFIDENCE == 0.80
    assert medium_low == policy.RUNG_MEDIUM_CONFIDENCE == 0.70
    assert interpolated == policy.RUNG_LOW_CONFIDENCE == 0.60


# ---------------------------------------------------------------------------
# Check 1 — unpriced entry (R5)
# ---------------------------------------------------------------------------


def test_unpriced_entry_advisory_when_no_identity_supplied(conn):
    intent = _intent(item_id="900100030", max_bid=50.0)
    results = policy.check_bid_write(conn, intent)
    r = _by_code(results, "unpriced_entry")
    assert r is not None and r["outcome"] == "advise"
    assert "no comic identity supplied" in r["data"]["attempted"][0]


def test_unpriced_entry_advisory_when_identity_unresolvable_ae4(conn):
    """AE4: identity supplied, FMV lookup finds no row."""
    intent = _intent(
        item_id="900100031", max_bid=50.0,
        comic_identities=[{"comic_id": 999999, "grade": 9.9}],
    )
    results = policy.check_bid_write(conn, intent)
    r = _by_code(results, "unpriced_entry")
    assert r is not None and r["outcome"] == "advise"
    assert any("comic_id=999999" in a for a in r["data"]["attempted"])


def test_unpriced_entry_pass_when_resolved_and_priced(conn):
    comic_id, _fmv_id = _insert_comic_fmv(conn, high=100.0)
    intent = _intent(
        item_id="900100032", max_bid=50.0,
        comic_identities=[{"comic_id": comic_id, "grade": 9.0}],
    )
    results = policy.check_bid_write(conn, intent)
    r = _by_code(results, "unpriced_entry")
    assert r is not None and r["outcome"] == "pass"


def test_unpriced_entry_patch_on_never_linked_bid(conn):
    """Adversarial: PATCH edit on a bid that was never linked to any comic
    (no payload identity ever supplied, no explicit link-fmv call either).
    Must not crash — degrades to the same unpriced advisory R5 describes,
    and every OTHER FMV-aware check stays silent (nothing to compute
    against; duplicate_comic doesn't fire on PATCH at all)."""
    bid = _insert_bid(conn, "900100033", 50.0)
    intent = _intent(item_id="900100033", max_bid=50.0, trigger="edit", prior_row=bid)
    results = policy.check_bid_write(conn, intent)
    r = _by_code(results, "unpriced_entry")
    assert r is not None and r["outcome"] == "advise"
    assert _by_code(results, "over_fmv") is None
    assert _by_code(results, "recomputed_cap") is None
    assert _by_code(results, "fmv_staleness") is None
    assert _by_code(results, "duplicate_comic") is None


def test_null_fmv_high_row_excluded_from_priced_sum(conn):
    """A resolved fmv row can still be an unpriced stub (high IS NULL —
    /comic:fmv never ran). Must not be summed as priced data, and must not
    crash on the arithmetic."""
    _comic_id, fmv_id = _insert_comic_fmv(conn, high=None, low=None, confidence=None)
    bid = _insert_bid(conn, "900100034", 50.0)
    _link(conn, bid["id"], fmv_id)
    intent = _intent(item_id="900100034", max_bid=50.0, trigger="edit", prior_row=bid)
    results = policy.check_bid_write(conn, intent)
    r = _by_code(results, "unpriced_entry")
    assert r is not None and r["outcome"] == "advise"
    assert r["data"]["resolved_count"] == 1
    assert r["data"]["priced_count"] == 0
    assert _by_code(results, "over_fmv") is None


def test_malformed_identity_entries_degrade_gracefully(conn):
    """Adversarial: junk identity dicts (per AddBidRequest.comic_identities'
    own docstring — unvalidated past "is a list of dicts"). None of these
    may crash the check; every one is recorded in `attempted`."""
    identities = [
        "not-a-dict",
        {"grade": 9.0},                                      # missing comic_id/locg_id
        {"comic_id": "not-an-int", "grade": 9.0},             # fails LinkFmvRequest validation
        {"comic_id": None, "locg_id": None, "grade": 9.0},    # both None
    ]
    intent = _intent(item_id="900100035", max_bid=50.0, comic_identities=identities)
    results = policy.check_bid_write(conn, intent)
    r = _by_code(results, "unpriced_entry")
    assert r is not None and r["outcome"] == "advise"
    assert len(r["data"]["attempted"]) == len(identities)


# ---------------------------------------------------------------------------
# Check 2 — over-FMV
# ---------------------------------------------------------------------------


def test_over_fmv_advisory_when_max_bid_exceeds_multiple(conn, monkeypatch):
    monkeypatch.delenv("POLICY_FMV_MULTIPLE", raising=False)
    _comic_id, fmv_id = _insert_comic_fmv(conn, high=100.0)
    bid = _insert_bid(conn, "900100040", 150.0)
    _link(conn, bid["id"], fmv_id)
    intent = _intent(item_id="900100040", max_bid=150.0, trigger="edit", prior_row=bid)
    results = policy.check_bid_write(conn, intent)
    r = _by_code(results, "over_fmv")
    assert r is not None and r["outcome"] == "advise"
    assert r["data"]["summed_high"] == 100.0
    assert r["data"]["ratio"] == pytest.approx(1.5)


def test_over_fmv_no_advisory_with_higher_multiple_env(conn, monkeypatch):
    monkeypatch.setenv("POLICY_FMV_MULTIPLE", "2.0")
    _comic_id, fmv_id = _insert_comic_fmv(conn, high=100.0)
    bid = _insert_bid(conn, "900100041", 150.0)
    _link(conn, bid["id"], fmv_id)
    intent = _intent(item_id="900100041", max_bid=150.0, trigger="edit", prior_row=bid)
    results = policy.check_bid_write(conn, intent)
    r = _by_code(results, "over_fmv")
    assert r is not None and r["outcome"] == "pass"


def test_over_fmv_malformed_env_is_unevaluable(conn, monkeypatch):
    monkeypatch.setenv("POLICY_FMV_MULTIPLE", "not-a-number")
    _comic_id, fmv_id = _insert_comic_fmv(conn, high=100.0)
    bid = _insert_bid(conn, "900100042", 50.0)
    _link(conn, bid["id"], fmv_id)
    intent = _intent(item_id="900100042", max_bid=50.0, trigger="edit", prior_row=bid)
    results = policy.check_bid_write(conn, intent)
    r = _by_code(results, "over_fmv")
    assert r is not None and r["outcome"] == "unevaluable"
    assert r["data"]["raw_config"] == "not-a-number"


# ---------------------------------------------------------------------------
# Check 3 — recomputed cap
# ---------------------------------------------------------------------------


def test_recomputed_cap_stored_low_caps_at_070(conn):
    """A stored 'low' collapses MEDIUM-LOW and LOW (fmv_runner's
    _confidence_to_db_label), so the cap is the LAXEST rung in that band
    (0.70) — a CGC-proxy bid at its legitimate 0.70x high must not advise,
    while a bid above 0.70x (which no rung would have produced) must."""
    _comic_id, fmv_id = _insert_comic_fmv(conn, high=100.0, confidence="low")
    bid = _insert_bid(conn, "900100050", 75.0)
    _link(conn, bid["id"], fmv_id)
    intent = _intent(item_id="900100050", max_bid=75.0, trigger="edit", prior_row=bid)
    results = policy.check_bid_write(conn, intent)
    r = _by_code(results, "recomputed_cap")
    assert r is not None and r["outcome"] == "advise"
    assert r["data"]["recomputed_cap"] == pytest.approx(70.0)


def test_recomputed_cap_stored_medium_passes_at_standard_080_bid(conn):
    """A stored 'medium' row was bid at 0.80 by the brief path (bid_factor
    pays BASE for MEDIUM and above; _confidence_to_db_label stores both
    MEDIUM-HIGH and MEDIUM as 'medium') — the standard /comic:buy bid must
    NOT draw a false advisory. Guards the mapping bug where 'medium' -> 0.70
    would have flagged every ordinary medium-confidence add."""
    _comic_id, fmv_id = _insert_comic_fmv(conn, high=100.0, confidence="medium")
    bid = _insert_bid(conn, "900100053", 80.0)
    _link(conn, bid["id"], fmv_id)
    intent = _intent(item_id="900100053", max_bid=80.0, trigger="edit", prior_row=bid)
    results = policy.check_bid_write(conn, intent)
    r = _by_code(results, "recomputed_cap")
    assert r is not None and r["outcome"] == "pass"
    assert r["data"]["recomputed_cap"] == pytest.approx(80.0)


def test_recomputed_cap_high_confidence_passes_within_080(conn):
    _comic_id, fmv_id = _insert_comic_fmv(conn, high=100.0, confidence="high")
    bid = _insert_bid(conn, "900100051", 75.0)
    _link(conn, bid["id"], fmv_id)
    intent = _intent(item_id="900100051", max_bid=75.0, trigger="edit", prior_row=bid)
    results = policy.check_bid_write(conn, intent)
    r = _by_code(results, "recomputed_cap")
    assert r is not None and r["outcome"] == "pass"
    assert r["data"]["recomputed_cap"] == pytest.approx(80.0)


# ---------------------------------------------------------------------------
# Check 4 — staleness
# ---------------------------------------------------------------------------


def test_staleness_advisory_when_older_than_threshold(conn, monkeypatch):
    monkeypatch.delenv("POLICY_FMV_STALE_DAYS", raising=False)
    old_ts = (datetime.now(timezone.utc) - timedelta(days=45)).isoformat()
    _comic_id, fmv_id = _insert_comic_fmv(conn, high=100.0, updated_at=old_ts)
    bid = _insert_bid(conn, "900100060", 50.0)
    _link(conn, bid["id"], fmv_id)
    intent = _intent(item_id="900100060", max_bid=50.0, trigger="edit", prior_row=bid)
    results = policy.check_bid_write(conn, intent)
    r = _by_code(results, "fmv_staleness")
    assert r is not None and r["outcome"] == "advise"
    assert r["data"]["stale"][0]["age_days"] >= 44.0


def test_staleness_pass_when_fresh(conn):
    _comic_id, fmv_id = _insert_comic_fmv(conn, high=100.0)  # updated_at defaults to now
    bid = _insert_bid(conn, "900100061", 50.0)
    _link(conn, bid["id"], fmv_id)
    intent = _intent(item_id="900100061", max_bid=50.0, trigger="edit", prior_row=bid)
    results = policy.check_bid_write(conn, intent)
    r = _by_code(results, "fmv_staleness")
    assert r is not None and r["outcome"] == "pass"


def test_staleness_malformed_env_is_unevaluable(conn, monkeypatch):
    monkeypatch.setenv("POLICY_FMV_STALE_DAYS", "soon")
    _comic_id, fmv_id = _insert_comic_fmv(conn, high=100.0)
    bid = _insert_bid(conn, "900100062", 50.0)
    _link(conn, bid["id"], fmv_id)
    intent = _intent(item_id="900100062", max_bid=50.0, trigger="edit", prior_row=bid)
    results = policy.check_bid_write(conn, intent)
    r = _by_code(results, "fmv_staleness")
    assert r is not None and r["outcome"] == "unevaluable"


# ---------------------------------------------------------------------------
# Check 5 — duplicate comic (R6/KTD7). AE1/AE2 + adversarial group-0 +
# tombstone-filter + POST-only.
# ---------------------------------------------------------------------------


def test_duplicate_comic_same_group_silent_ae1(conn):
    comic_id, fmv_id = _insert_comic_fmv(conn, high=100.0)
    existing = _insert_bid(conn, "900100070", 90.0, snipe_group=2)
    _link(conn, existing["id"], fmv_id)

    intent = _intent(
        item_id="900100071", max_bid=90.0, snipe_group=2,
        comic_identities=[{"comic_id": comic_id, "grade": 9.0}],
    )
    results = policy.check_bid_write(conn, intent)
    assert _by_code(results, "duplicate_comic")["outcome"] == "pass"


def test_duplicate_comic_ungrouped_second_copy_advisory_ae2(conn):
    comic_id, fmv_id = _insert_comic_fmv(conn, high=100.0)
    existing = _insert_bid(conn, "900100072", 90.0, snipe_group=2)
    _link(conn, existing["id"], fmv_id)

    intent = _intent(
        item_id="900100073", max_bid=90.0, snipe_group=0,
        comic_identities=[{"comic_id": comic_id, "grade": 9.0}],
    )
    results = policy.check_bid_write(conn, intent)
    r = _by_code(results, "duplicate_comic")
    assert r["outcome"] == "advise"
    dup = r["data"]["duplicates"][0]
    assert dup["existing_item_id"] == "900100072"
    assert dup["existing_snipe_group"] == 2
    assert "900100072" in r["message"]


def test_duplicate_comic_group_zero_never_exempts(conn):
    """Adversarial (binding constraint): both the existing snipe and the new
    write are ungrouped (snipe_group=0). Group 0 must never be treated as a
    shared exemption group, even though the values are literally equal."""
    comic_id, fmv_id = _insert_comic_fmv(conn, high=100.0)
    existing = _insert_bid(conn, "900100074", 90.0, snipe_group=0)
    _link(conn, existing["id"], fmv_id)

    intent = _intent(
        item_id="900100075", max_bid=90.0, snipe_group=0,
        comic_identities=[{"comic_id": comic_id, "grade": 9.0}],
    )
    results = policy.check_bid_write(conn, intent)
    assert _by_code(results, "duplicate_comic")["outcome"] == "advise"


def test_duplicate_comic_tombstoned_sibling_excluded(conn):
    comic_id, fmv_id = _insert_comic_fmv(conn, high=100.0)
    removed = _insert_bid(conn, "900100076", 90.0, snipe_group=0, status="REMOVED")
    _link(conn, removed["id"], fmv_id)

    intent = _intent(
        item_id="900100077", max_bid=90.0, snipe_group=0,
        comic_identities=[{"comic_id": comic_id, "grade": 9.0}],
    )
    results = policy.check_bid_write(conn, intent)
    assert _by_code(results, "duplicate_comic")["outcome"] == "pass"


def test_duplicate_comic_never_fires_on_patch(conn):
    comic_id, fmv_id = _insert_comic_fmv(conn, high=100.0)
    existing = _insert_bid(conn, "900100078", 90.0, snipe_group=0)
    _link(conn, existing["id"], fmv_id)
    other = _insert_bid(conn, "900100079", 90.0, snipe_group=0)
    _link(conn, other["id"], fmv_id)

    intent = _intent(item_id="900100079", max_bid=95.0, trigger="edit", prior_row=other)
    results = policy.check_bid_write(conn, intent)
    assert _by_code(results, "duplicate_comic") is None


# ---------------------------------------------------------------------------
# KTD8 — both arms sum resolved FMV highs, not one issue's high.
# ---------------------------------------------------------------------------


def test_post_two_identity_lot_compares_against_summed_high(conn, monkeypatch):
    monkeypatch.delenv("POLICY_FMV_MULTIPLE", raising=False)
    comic_a, _fmv_a = _insert_comic_fmv(conn, title="Lot Book A", issue="1", grade=9.0, high=80.0)
    comic_b, _fmv_b = _insert_comic_fmv(conn, title="Lot Book B", issue="1", grade=9.0, high=80.0)
    identities = [
        {"comic_id": comic_a, "grade": 9.0},
        {"comic_id": comic_b, "grade": 9.0},
    ]

    # $120 exceeds ONE issue's high ($80) but is within the SUMMED high
    # ($160) — a single-issue comparison would false-advise here.
    intent = _intent(item_id="900100080", max_bid=120.0, comic_identities=identities)
    results = policy.check_bid_write(conn, intent)
    r = _by_code(results, "over_fmv")
    assert r["outcome"] == "pass"
    assert r["data"]["summed_high"] == 160.0

    # Push above the summed high to prove it still fires for a genuinely
    # over-FMV lot.
    intent2 = _intent(item_id="900100081", max_bid=170.0, comic_identities=identities)
    results2 = policy.check_bid_write(conn, intent2)
    r2 = _by_code(results2, "over_fmv")
    assert r2["outcome"] == "advise"
    assert r2["data"]["summed_high"] == 160.0


def test_patch_multi_linked_lot_compares_against_summed_high(conn, monkeypatch):
    monkeypatch.delenv("POLICY_FMV_MULTIPLE", raising=False)
    _comic_a, fmv_a = _insert_comic_fmv(conn, title="Lot Book C", issue="1", grade=9.0, high=80.0)
    _comic_b, fmv_b = _insert_comic_fmv(conn, title="Lot Book D", issue="1", grade=9.0, high=70.0)
    bid = _insert_bid(conn, "900100082", 100.0)
    _link(conn, bid["id"], fmv_a, is_primary=True)
    _link(conn, bid["id"], fmv_b, is_primary=False)

    intent = _intent(item_id="900100082", max_bid=100.0, trigger="edit", prior_row=bid)
    results = policy.check_bid_write(conn, intent)
    r = _by_code(results, "over_fmv")
    assert r["outcome"] == "pass"  # 100 <= 150 (80 + 70)
    assert r["data"]["summed_high"] == 150.0
    assert r["data"]["link_count"] == 2


# ---------------------------------------------------------------------------
# Cross-cutting verification: tri-state everywhere, no check writes, one
# raising check doesn't lose the others' contributions.
# ---------------------------------------------------------------------------


def test_all_five_checks_return_tri_state_outcomes(conn):
    comic_id, fmv_id = _insert_comic_fmv(conn, high=50.0, confidence="medium")
    existing = _insert_bid(conn, "900100090", 40.0, snipe_group=0)
    _link(conn, existing["id"], fmv_id)

    intent = _intent(
        item_id="900100091", max_bid=150.0,
        comic_identities=[{"comic_id": comic_id, "grade": 9.0}],
    )
    results = policy.check_bid_write(conn, intent)
    assert len(results) == 5
    assert {r["code"] for r in results} == {
        "unpriced_entry", "over_fmv", "recomputed_cap", "fmv_staleness", "duplicate_comic",
    }
    assert all(r["outcome"] in {"pass", "advise", "unevaluable"} for r in results)


def test_no_check_writes_to_the_db(conn):
    _comic_id, fmv_id = _insert_comic_fmv(conn, high=100.0, confidence="low")
    bid = _insert_bid(conn, "900100092", 500.0)  # deliberately over every threshold
    _link(conn, bid["id"], fmv_id)
    before = {
        table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("bids", "comics", "fmv", "bid_fmvs")
    }

    intent = _intent(item_id="900100092", max_bid=500.0, trigger="edit", prior_row=bid)
    results = policy.check_bid_write(conn, intent)
    assert any(r["outcome"] == "advise" for r in results)  # sanity: checks actually engaged

    after = {
        table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("bids", "comics", "fmv", "bid_fmvs")
    }
    assert before == after


def test_one_check_raising_does_not_lose_other_checks(conn, monkeypatch):
    """Binding constraint: the host's own _invoke_plugin_checks wraps the
    WHOLE hook call in one try/except (losing every plugin's contribution on
    a raise) — this module must not lean on that alone. Simulate a check bug
    and assert the other checks in the SAME call still return real results."""
    def _boom(conn, intent):
        raise RuntimeError("simulated check bug")

    monkeypatch.setattr(policy, "_CHECKS", (
        ("unpriced_entry", policy._check_unpriced_entry),
        ("boom_check", _boom),
        ("over_fmv", policy._check_over_fmv),
    ))
    intent = _intent(item_id="900100093", max_bid=50.0)
    results = policy.check_bid_write(conn, intent)
    codes = {r["code"] for r in results}
    assert "unpriced_entry" in codes
    boom_result = _by_code(results, "boom_check")
    assert boom_result is not None
    assert boom_result["outcome"] == "unevaluable"


def test_crash_fallback_code_matches_the_checks_own_success_code(conn, monkeypatch):
    """A crashed check must report under the SAME `code` its normal
    pass/advise results use (review finding) — otherwise a ledger consumer
    filtering by e.g. code="over_fmv" would silently miss the crashed
    evaluations, quietly defeating KTD6's "errored must never collapse into
    found nothing" for anyone who groups by code."""
    def _boom(conn, intent):
        raise RuntimeError("simulated over_fmv bug")

    patched = tuple(
        (code, _boom if code == "over_fmv" else fn) for code, fn in policy._CHECKS
    )
    monkeypatch.setattr(policy, "_CHECKS", patched)

    intent = _intent(item_id="900100094", max_bid=50.0)
    results = policy.check_bid_write(conn, intent)

    r = _by_code(results, "over_fmv")
    assert r is not None
    assert r["outcome"] == "unevaluable"


def test_duplicate_comic_matches_across_grades_ktd7(conn):
    """KTD7: duplicate-comic matches on comics.id ACROSS grades, not just the
    same grade — a raw copy already sniped elsewhere still flags a graded
    copy of the same book (and vice versa)."""
    comic_id, fmv_low_grade = _insert_comic_fmv(conn, grade=4.0, high=40.0)
    _comic_id2, fmv_high_grade = _insert_comic_fmv(conn, grade=9.4, high=300.0, comic_id=comic_id)
    assert comic_id == _comic_id2  # same comic, two grades' worth of fmv rows

    existing = _insert_bid(conn, "900100095", 35.0, snipe_group=0)
    _link(conn, existing["id"], fmv_low_grade)

    # New write targets the SAME comic at a DIFFERENT grade.
    intent = _intent(
        item_id="900100096", max_bid=250.0, snipe_group=0,
        comic_identities=[{"comic_id": comic_id, "grade": 9.4}],
    )
    results = policy.check_bid_write(conn, intent)
    r = _by_code(results, "duplicate_comic")
    assert r["outcome"] == "advise"
    dup = r["data"]["duplicates"][0]
    assert dup["existing_item_id"] == "900100095"
    assert dup["existing_grade"] == 4.0
    assert dup["new_grade"] == 9.4


def test_patch_with_no_prior_row_at_all_is_unpriced_not_a_crash(conn):
    """Adversarial: PATCH against an item the server never ingested at all
    (intent.prior_row is None, not just an existing-but-unlinked bid row —
    the host's own "no_prior_row" case). Must degrade to the same unpriced
    advisory as any other unlinked write, never crash on a None prior_row."""
    intent = _intent(item_id="900100097", max_bid=50.0, trigger="edit", prior_row=None)
    results = policy.check_bid_write(conn, intent)
    r = _by_code(results, "unpriced_entry")
    assert r is not None and r["outcome"] == "advise"
    assert _by_code(results, "duplicate_comic") is None


# ---------------------------------------------------------------------------
# End-to-end via the real API (conftest's `api` fixture) — proves the
# hookspec dispatch, the host's `advisories` envelope, and the
# `bid_decisions` ledger all actually wire together, not just the policy
# module in isolation.
# ---------------------------------------------------------------------------


def test_ae1_grouped_second_copy_via_api_is_silent(api):
    comic_resp = api.post("/api/comics", json={
        "title": "AE1 API Comic", "issue": "1", "year": 2000,
        "grade": 9.0, "fmv_low": 80.0, "fmv_high": 100.0,
    })
    comic_id = comic_resp.json()["comic_id"]

    r1 = api.post("/api/bids", json={
        "item_id": "900200010", "max_bid": 90.0, "snipe_group": 4,
        "comic_identities": [{"comic_id": comic_id, "grade": 9.0}],
    })
    assert r1.status_code == 200

    r2 = api.post("/api/bids", json={
        "item_id": "900200011", "max_bid": 90.0, "snipe_group": 4,
        "comic_identities": [{"comic_id": comic_id, "grade": 9.0}],
    })
    assert r2.status_code == 200
    assert not any(a["code"] == "duplicate_comic" for a in r2.json()["advisories"])


def test_ae2_ungrouped_second_copy_via_api_names_item_and_group(api):
    comic_resp = api.post("/api/comics", json={
        "title": "AE2 API Comic", "issue": "1", "year": 2000,
        "grade": 9.0, "fmv_low": 80.0, "fmv_high": 100.0,
    })
    comic_id = comic_resp.json()["comic_id"]

    r1 = api.post("/api/bids", json={
        "item_id": "900200012", "max_bid": 90.0, "snipe_group": 4,
        "comic_identities": [{"comic_id": comic_id, "grade": 9.0}],
    })
    assert r1.status_code == 200

    r2 = api.post("/api/bids", json={
        "item_id": "900200013", "max_bid": 90.0, "snipe_group": 0,
        "comic_identities": [{"comic_id": comic_id, "grade": 9.0}],
    })
    assert r2.status_code == 200
    dup = next(a for a in r2.json()["advisories"] if a["code"] == "duplicate_comic")
    assert "900200012" in dup["message"]
    assert dup["data"]["duplicates"][0]["existing_item_id"] == "900200012"
    assert dup["data"]["duplicates"][0]["existing_snipe_group"] == 4


def test_ae4_unresolvable_identity_commits_and_ledger_records_unpriced(api):
    r = api.post("/api/bids", json={
        "item_id": "900200014", "max_bid": 50.0,
        "comic_identities": [{"comic_id": 999999, "grade": 9.9}],
    })
    assert r.status_code == 200
    assert r.json()["created"] is True
    advisories = r.json()["advisories"]
    unpriced = next(a for a in advisories if a["code"] == "unpriced_entry")
    assert unpriced["severity"] == "warning"
    assert any("comic_id=999999" in s for s in unpriced["data"]["attempted"])

    decisions = api.get("/api/decisions", params={"item_id": "900200014"}).json()
    assert decisions, "expected a bid_decisions ledger row for this write"
    checks = decisions[0]["checks"]
    unpriced_check = next(c for c in checks if c["code"] == "unpriced_entry")
    assert unpriced_check["outcome"] == "advise"
    assert any("comic_id=999999" in s for s in unpriced_check["data"]["attempted"])
