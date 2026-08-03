"""Unit tests for server/policy.py — BUI-615 (U1: check point + advisory
envelope) and BUI-616 (U2: group-aware PENDING exposure ceiling check).

DB-backed tests use tmp_path + init_db/insert_bid, mirroring
tests/test_server_db.py. Config tests use monkeypatch.setenv/delenv scoped
per test, mirroring test_server_api.py's BUI-573 env-fixture pattern — KTD2
requires every policy env var to be read fresh on each call, never cached at
import time, so these tests exercise that by changing env BETWEEN calls in
the same test.

Endpoint-level envelope coverage (the `advisories` key on every 2xx branch of
api_add_bid/api_edit_bid) lives in tests/test_server_api.py alongside the
existing tests for those branches.
"""
import sys
import os
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from server.db import init_db, insert_bid
from server.policy import (
    PolicyIntent, CheckResult, run_checks,
    _sum_grouped_pending, _project_exposure, _check_exposure,
)


@pytest.fixture
def db(tmp_path):
    conn = init_db(tmp_path / "policy_test.db")
    yield conn
    conn.close()


def _intent(
    item_id="900000001", target_max_bid=100.0, snipe_group=0,
    trigger="create", prior_row=None,
):
    return PolicyIntent(
        item_id=item_id, target_max_bid=target_max_bid,
        snipe_group=snipe_group, trigger=trigger, prior_row=prior_row,
    )


# ---------------------------------------------------------------------------
# _sum_grouped_pending — pure projection formula (execution note: implement
# test-first, the group math is the bug surface).
# ---------------------------------------------------------------------------

def test_sum_grouped_pending_ae3():
    """AE3: ungrouped $100 + $50 plus a group of $200/$180 -> $350."""
    rows = [
        {"snipe_group": 0, "max_bid": 100.0},
        {"snipe_group": 0, "max_bid": 50.0},
        {"snipe_group": 7, "max_bid": 200.0},
        {"snipe_group": 7, "max_bid": 180.0},
    ]
    assert _sum_grouped_pending(rows) == 350.0


def test_sum_grouped_pending_empty():
    assert _sum_grouped_pending([]) == 0.0


def test_sum_grouped_pending_all_ungrouped_sums():
    rows = [{"snipe_group": 0, "max_bid": 10.0}, {"snipe_group": 0, "max_bid": 20.0}]
    assert _sum_grouped_pending(rows) == 30.0


def test_sum_grouped_pending_multiple_groups_each_count_once():
    rows = [
        {"snipe_group": 1, "max_bid": 50.0},
        {"snipe_group": 1, "max_bid": 40.0},
        {"snipe_group": 2, "max_bid": 30.0},
        {"snipe_group": 2, "max_bid": 90.0},
    ]
    assert _sum_grouped_pending(rows) == 50.0 + 90.0


def test_sum_grouped_pending_single_member_group_counts_once():
    rows = [{"snipe_group": 3, "max_bid": 25.0}]
    assert _sum_grouped_pending(rows) == 25.0


# ---------------------------------------------------------------------------
# _project_exposure — DB-backed replace semantics (U2 acceptance)
# ---------------------------------------------------------------------------

def test_project_exposure_create_adds(db):
    insert_bid(db, "111111111", 100.0, 6, 0, None)
    intent = _intent(item_id="222222222", target_max_bid=50.0, snipe_group=0, trigger="create")
    assert _project_exposure(db, intent) == 150.0


def test_project_exposure_upsert_replaces_not_doubles(db):
    """Upsert raising a live item's bid -> old value replaced, not
    double-counted."""
    insert_bid(db, "333333333", 100.0, 6, 0, None)
    intent = _intent(item_id="333333333", target_max_bid=250.0, snipe_group=0, trigger="upsert")
    assert _project_exposure(db, intent) == 250.0


def test_project_exposure_ae7_in_group_edit_below_max_unchanged(db):
    """AE7: group holding $200 and $180; editing the $180 member to $190 ->
    projection unchanged ($200 is still the group max), no advisory."""
    insert_bid(db, "444444444", 200.0, 6, 9, None)
    insert_bid(db, "555555555", 180.0, 6, 9, None)
    intent = _intent(item_id="555555555", target_max_bid=190.0, snipe_group=9, trigger="edit")
    assert _project_exposure(db, intent) == 200.0


def test_project_exposure_ae7_editing_the_group_max_down_reprojects(db):
    """The inverse of the above: editing the CURRENT max member down folds
    correctly to the new second-highest member, not a stale cached max."""
    insert_bid(db, "444444445", 200.0, 6, 9, None)
    insert_bid(db, "555555556", 180.0, 6, 9, None)
    intent = _intent(item_id="444444445", target_max_bid=150.0, snipe_group=9, trigger="edit")
    assert _project_exposure(db, intent) == 180.0


def test_project_exposure_patch_fallback_no_prior_row(db):
    """PATCH fallback on a not-yet-ingested row: prior contribution 0 (the
    `no_prior_row` case the U6 ledger will mark explicitly, a later wave)."""
    insert_bid(db, "666666666", 40.0, 6, 0, None)
    intent = _intent(item_id="777777777", target_max_bid=60.0, snipe_group=0, trigger="edit")
    assert _project_exposure(db, intent) == 100.0


def test_project_exposure_excludes_tombstones(db):
    removed_id = insert_bid(db, "888888888", 500.0, 6, 0, None)
    db.execute("UPDATE bids SET status='REMOVED' WHERE id=?", (removed_id,))
    purged_id = insert_bid(db, "888888889", 500.0, 6, 0, None)
    db.execute("UPDATE bids SET status='PURGED' WHERE id=?", (purged_id,))
    insert_bid(db, "999999999", 20.0, 6, 0, None)
    intent = _intent(item_id="000000001", target_max_bid=10.0, snipe_group=0, trigger="create")
    # 20 (live) + 10 (target) — neither tombstoned row may count.
    assert _project_exposure(db, intent) == 30.0


def test_project_exposure_ignores_non_pending_terminal_rows(db):
    """A WON/LOST/ENDED/FAILED row is not PENDING and must not inflate the
    projection even though it isn't a tombstone either."""
    won_id = insert_bid(db, "121212120", 900.0, 6, 0, None)
    db.execute("UPDATE bids SET status='WON' WHERE id=?", (won_id,))
    intent = _intent(item_id="343434344", target_max_bid=10.0, snipe_group=0, trigger="create")
    assert _project_exposure(db, intent) == 10.0


def test_project_exposure_seeded_web_added_row_inflates(db):
    """Seeded web-added PENDING row (no policy-evaluation history) still
    inflates the projection — R1 refinement: web-added/mirrored rows count
    in the sum even though they never trigger checks themselves."""
    insert_bid(db, "121212121", 300.0, 6, 0, None)  # simulates a web-added row
    intent = _intent(item_id="343434343", target_max_bid=10.0, snipe_group=0, trigger="create")
    assert _project_exposure(db, intent) == 310.0


def test_project_exposure_group_with_mixed_statuses_only_counts_live_member(db):
    """Adversarial: a group where one sibling already resolved (WON) and
    another was cancelled (REMOVED) — only the surviving PENDING member may
    contribute to the group's max. The terminal/tombstoned siblings are
    excluded by the base query, not by any group-aware special-casing, so
    this also guards against a future query change that stops filtering by
    status='PENDING'."""
    won_id = insert_bid(db, "232300001", 500.0, 6, 55, None)
    db.execute("UPDATE bids SET status='WON' WHERE id=?", (won_id,))
    removed_id = insert_bid(db, "232300002", 400.0, 6, 55, None)
    db.execute("UPDATE bids SET status='REMOVED' WHERE id=?", (removed_id,))
    insert_bid(db, "232300003", 90.0, 6, 55, None)  # the only live member

    intent = _intent(item_id="232300004", target_max_bid=5.0, snipe_group=0, trigger="create")
    # group max is 90 (the only PENDING sibling), not 500 or 400.
    assert _project_exposure(db, intent) == 95.0


# ---------------------------------------------------------------------------
# _check_exposure — tri-state outcome + KTD2 config handling
# ---------------------------------------------------------------------------

def test_check_exposure_ceiling_unset_disabled(db, monkeypatch):
    monkeypatch.delenv("POLICY_EXPOSURE_CEILING", raising=False)
    assert _check_exposure(db, _intent()) is None


def test_check_exposure_ceiling_empty_string_treated_as_unset(db, monkeypatch):
    monkeypatch.setenv("POLICY_EXPOSURE_CEILING", "   ")
    assert _check_exposure(db, _intent()) is None


def test_check_exposure_ceiling_malformed_is_unevaluable(db, monkeypatch, caplog):
    monkeypatch.setenv("POLICY_EXPOSURE_CEILING", "not-a-number")
    with caplog.at_level("WARNING", logger="server.policy"):
        result = _check_exposure(db, _intent())
    assert result.outcome == "unevaluable"
    assert result.code == "exposure_ceiling"
    assert result.data["raw_config"] == "not-a-number"
    assert any("POLICY_EXPOSURE_CEILING" in rec.message for rec in caplog.records)


def test_check_exposure_crossing_ceiling_advises(db, monkeypatch):
    monkeypatch.setenv("POLICY_EXPOSURE_CEILING", "100")
    insert_bid(db, "232323232", 80.0, 6, 0, None)
    intent = _intent(item_id="454545454", target_max_bid=50.0, snipe_group=0, trigger="create")
    result = _check_exposure(db, intent)
    assert result.outcome == "advise"
    assert result.data["projected"] == 130.0
    assert result.data["ceiling"] == 100.0


def test_check_exposure_within_ceiling_passes(db, monkeypatch):
    monkeypatch.setenv("POLICY_EXPOSURE_CEILING", "1000")
    intent = _intent(item_id="656565656", target_max_bid=50.0, snipe_group=0, trigger="create")
    result = _check_exposure(db, intent)
    assert result.outcome == "pass"


def test_check_exposure_exactly_at_ceiling_is_not_an_advisory(db, monkeypatch):
    """Boundary: the check fires on exceeding the ceiling, not merely
    reaching it."""
    monkeypatch.setenv("POLICY_EXPOSURE_CEILING", "50")
    intent = _intent(item_id="454545455", target_max_bid=50.0, snipe_group=0, trigger="create")
    result = _check_exposure(db, intent)
    assert result.outcome == "pass"


# ---------------------------------------------------------------------------
# CheckResult.to_advisory — envelope projection
# ---------------------------------------------------------------------------

def test_check_result_to_advisory_pass_is_none():
    assert CheckResult(code="x", outcome="pass", message="ok").to_advisory() is None


def test_check_result_to_advisory_advise_shape():
    result = CheckResult(code="x", outcome="advise", message="hmm", data={"a": 1})
    assert result.to_advisory() == {
        "code": "x", "severity": "warning", "message": "hmm", "data": {"a": 1},
    }


def test_check_result_to_advisory_unevaluable_severity_distinct():
    result = CheckResult(code="x", outcome="unevaluable", message="broken")
    adv = result.to_advisory()
    assert adv["severity"] == "unevaluable"
    assert adv["severity"] != "warning"  # never collapses into "advise" (KTD6)


# ---------------------------------------------------------------------------
# run_checks — the U1 check point itself
# ---------------------------------------------------------------------------

def test_run_checks_ceiling_unset_empty_results(db, monkeypatch):
    monkeypatch.delenv("POLICY_EXPOSURE_CEILING", raising=False)
    advisories, results = run_checks(db, _intent(), None)
    assert advisories == []
    assert results == []


def test_run_checks_advisory_shape_matches_ktd4(db, monkeypatch):
    monkeypatch.setenv("POLICY_EXPOSURE_CEILING", "10")
    intent = _intent(item_id="787878787", target_max_bid=50.0, snipe_group=0, trigger="create")
    advisories, results = run_checks(db, intent, None)
    assert len(advisories) == 1
    adv = advisories[0]
    assert set(adv.keys()) == {"code", "severity", "message", "data"}
    assert adv["code"] == "exposure_ceiling"
    assert adv["severity"] == "warning"
    assert len(results) == 1
    assert results[0]["outcome"] == "advise"


def test_run_checks_unevaluable_surfaces_as_advisory(db, monkeypatch):
    """A config typo must never present as 'check found nothing' — it must
    be visible in the envelope, distinctly from a normal advisory."""
    monkeypatch.setenv("POLICY_EXPOSURE_CEILING", "abc")
    advisories, results = run_checks(db, _intent(), None)
    assert len(advisories) == 1
    assert advisories[0]["severity"] == "unevaluable"
    assert results[0]["outcome"] == "unevaluable"
    assert results[0]["data"]["raw_config"] == "abc"


def test_run_checks_reads_env_per_request_not_cached(db, monkeypatch):
    """KTD2: policy env is read PER REQUEST, never cached at import — two
    run_checks calls in the same process, with the env changed in between,
    must see the new value on the second call."""
    monkeypatch.setenv("POLICY_EXPOSURE_CEILING", "1000")
    intent = _intent(item_id="898989898", target_max_bid=50.0, snipe_group=0, trigger="create")
    advisories_1, _ = run_checks(db, intent, None)
    assert advisories_1 == []  # comfortably within the first ceiling

    monkeypatch.setenv("POLICY_EXPOSURE_CEILING", "10")
    advisories_2, _ = run_checks(db, intent, None)
    assert len(advisories_2) == 1
    assert advisories_2[0]["code"] == "exposure_ceiling"


def test_run_checks_pm_none_tolerated(db, monkeypatch):
    monkeypatch.delenv("POLICY_EXPOSURE_CEILING", raising=False)
    advisories, results = run_checks(db, _intent(), pm=None)
    assert advisories == []
    assert results == []


def test_run_checks_exception_in_check_downgrades_to_unevaluable(db, monkeypatch, caplog):
    """An exception raised INSIDE a check must never propagate out of
    run_checks — v1 is advisory-only and a check bug must degrade to a
    warning, never block/fail/delay the write (KTD6, guard-strictness)."""
    import server.policy as policy

    def boom(conn, intent):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(policy, "_CHECKS", (boom,))
    with caplog.at_level("ERROR", logger="server.policy"):
        advisories, results = run_checks(db, _intent(), None)

    assert len(advisories) == 1
    assert advisories[0]["severity"] == "unevaluable"
    assert advisories[0]["code"] == "boom"
    assert results[0]["outcome"] == "unevaluable"
    assert any(rec.levelname == "ERROR" for rec in caplog.records)


def test_run_checks_one_check_raising_does_not_stop_others(db, monkeypatch, caplog):
    """A raising check is isolated — it must not prevent a later check in the
    registry from running and contributing its own result."""
    import server.policy as policy

    def boom(conn, intent):
        raise RuntimeError("kaboom")

    def fine(conn, intent):
        return policy.CheckResult(code="fine", outcome="advise", message="ok", data={})

    monkeypatch.setattr(policy, "_CHECKS", (boom, fine))
    with caplog.at_level("ERROR", logger="server.policy"):
        advisories, results = run_checks(db, _intent(), None)

    codes = {a["code"] for a in advisories}
    assert codes == {"boom", "fine"}
    assert len(results) == 2
