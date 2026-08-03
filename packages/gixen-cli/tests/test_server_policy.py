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
    notify_bid_write_committed, config_snapshot,
    BlockDecision, evaluate_block, build_block_detail,
    _block_flag_on, _policy_block_env_var,
)
from gixen.plugins import make_plugin_manager, hookimpl


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


# ---------------------------------------------------------------------------
# BUI-617 (U3) — check_bid_write / on_bid_write_committed hookspec invocation
# ---------------------------------------------------------------------------

class _FakeCheckPlugin:
    """Registers check_bid_write returning a fixed list of result dicts."""

    def __init__(self, results):
        self._results = results

    @hookimpl
    def check_bid_write(self, conn, intent):
        return self._results


class _FakeRaisingCheckPlugin:
    @hookimpl
    def check_bid_write(self, conn, intent):
        raise RuntimeError("plugin check_bid_write kaboom")


class _FakeCommitPlugin:
    """Registers on_bid_write_committed, recording every call it receives."""

    def __init__(self):
        self.calls: list[tuple] = []

    @hookimpl
    def on_bid_write_committed(self, conn, intent, bid_row_id, check_results):
        self.calls.append((bid_row_id, check_results))


class _FakeRaisingCommitPlugin:
    @hookimpl
    def on_bid_write_committed(self, conn, intent, bid_row_id, check_results):
        raise RuntimeError("plugin on_bid_write_committed kaboom")


def test_run_checks_merges_plugin_advisory(db, monkeypatch):
    """A fake plugin's check_bid_write contribution lands in both the
    envelope (advisories) and the full tri-state record (check_results)."""
    monkeypatch.delenv("POLICY_EXPOSURE_CEILING", raising=False)
    pm = make_plugin_manager()
    pm.register(_FakeCheckPlugin([
        {"code": "fmv_over", "outcome": "advise", "message": "over FMV", "data": {"x": 1}},
    ]))
    advisories, results = run_checks(db, _intent(), pm)
    assert len(advisories) == 1
    assert advisories[0] == {
        "code": "fmv_over", "severity": "warning", "message": "over FMV", "data": {"x": 1},
    }
    assert len(results) == 1
    assert results[0]["outcome"] == "advise"
    assert results[0]["code"] == "fmv_over"


def test_run_checks_plugin_pass_does_not_surface_as_advisory(db, monkeypatch):
    monkeypatch.delenv("POLICY_EXPOSURE_CEILING", raising=False)
    pm = make_plugin_manager()
    pm.register(_FakeCheckPlugin([
        {"code": "fine", "outcome": "pass", "message": "ok", "data": {}},
    ]))
    advisories, results = run_checks(db, _intent(), pm)
    assert advisories == []
    assert len(results) == 1  # pass still recorded in the full tri-state list
    assert results[0]["outcome"] == "pass"


def test_run_checks_plugin_check_bid_write_raising_downgrades_to_unevaluable(
    db, monkeypatch, caplog,
):
    """Covers AE8/KTD1: the overlay hook raises -> the write must be able to
    proceed (this call never raises), one unevaluable result is recorded,
    and a loud log is emitted."""
    monkeypatch.delenv("POLICY_EXPOSURE_CEILING", raising=False)
    pm = make_plugin_manager()
    pm.register(_FakeRaisingCheckPlugin())
    with caplog.at_level("ERROR", logger="server.policy"):
        advisories, results = run_checks(db, _intent(), pm)

    assert len(advisories) == 1
    assert advisories[0]["severity"] == "unevaluable"
    assert len(results) == 1
    assert results[0]["outcome"] == "unevaluable"
    assert any(rec.levelname == "ERROR" for rec in caplog.records)


def test_run_checks_plugin_malformed_outcome_downgrades_to_unevaluable(db, monkeypatch):
    """A plugin returning an outcome outside the tri-state vocabulary is
    itself downgraded — a malformed contribution must never read as a clean
    pass or as a normal advisory."""
    monkeypatch.delenv("POLICY_EXPOSURE_CEILING", raising=False)
    pm = make_plugin_manager()
    pm.register(_FakeCheckPlugin([
        {"code": "weird", "outcome": "not-a-real-outcome", "message": "??", "data": {}},
    ]))
    _advisories, results = run_checks(db, _intent(), pm)
    assert results[0]["outcome"] == "unevaluable"


def test_run_checks_no_plugin_registered_no_unevaluable_noise(db, monkeypatch):
    """An empty PluginManager (no overlay registered — R5's standalone
    gixen-cli case) contributes nothing; only host checks run."""
    monkeypatch.delenv("POLICY_EXPOSURE_CEILING", raising=False)
    pm = make_plugin_manager()
    advisories, results = run_checks(db, _intent(), pm)
    assert advisories == []
    assert results == []


def test_run_checks_host_and_plugin_checks_both_contribute(db, monkeypatch):
    """Host-owned + plugin-contributed checks merge into one list — neither
    displaces the other."""
    monkeypatch.setenv("POLICY_EXPOSURE_CEILING", "10")
    pm = make_plugin_manager()
    pm.register(_FakeCheckPlugin([
        {"code": "plugin_check", "outcome": "advise", "message": "hmm", "data": {}},
    ]))
    intent = _intent(item_id="343434399", target_max_bid=50.0, snipe_group=0, trigger="create")
    advisories, results = run_checks(db, intent, pm)
    codes = {a["code"] for a in advisories}
    assert codes == {"exposure_ceiling", "plugin_check"}
    assert len(results) == 2


def test_notify_bid_write_committed_invokes_hook(db):
    pm = make_plugin_manager()
    fake = _FakeCommitPlugin()
    pm.register(fake)
    notify_bid_write_committed(pm, db, _intent(), 42, [{"code": "x"}])
    assert fake.calls == [(42, [{"code": "x"}])]


def test_notify_bid_write_committed_raising_hook_is_logged_not_raised(db, caplog):
    """Notification-only: the write already committed by the time this
    fires, so a raising plugin can only be logged, never surfaced."""
    pm = make_plugin_manager()
    pm.register(_FakeRaisingCommitPlugin())
    with caplog.at_level("ERROR", logger="server.policy"):
        notify_bid_write_committed(pm, db, _intent(), 42, [])
    assert any(rec.levelname == "ERROR" for rec in caplog.records)


def test_notify_bid_write_committed_pm_none_is_noop(db):
    notify_bid_write_committed(None, db, _intent(), 42, [])  # must not raise


def test_notify_bid_write_committed_no_plugin_registered_is_noop(db):
    pm = make_plugin_manager()  # empty
    notify_bid_write_committed(pm, db, _intent(), 42, [])  # must not raise


# ---------------------------------------------------------------------------
# BUI-618 (U6) — config_snapshot
# ---------------------------------------------------------------------------

def test_config_snapshot_reads_current_env(monkeypatch):
    monkeypatch.setenv("POLICY_EXPOSURE_CEILING", "123")
    assert config_snapshot() == {"POLICY_EXPOSURE_CEILING": "123"}


def test_config_snapshot_unset_is_none(monkeypatch):
    monkeypatch.delenv("POLICY_EXPOSURE_CEILING", raising=False)
    assert config_snapshot() == {"POLICY_EXPOSURE_CEILING": None}


def test_config_snapshot_reads_per_call_not_cached(monkeypatch):
    """KTD2 discipline extends to the ledger's config snapshot too — two
    calls with the env changed in between must see the new value."""
    monkeypatch.setenv("POLICY_EXPOSURE_CEILING", "1")
    first = config_snapshot()
    monkeypatch.setenv("POLICY_EXPOSURE_CEILING", "2")
    second = config_snapshot()
    assert first != second
    assert second["POLICY_EXPOSURE_CEILING"] == "2"


def test_config_snapshot_with_check_results_adds_block_flags(monkeypatch):
    """BUI-623 (U9): passing check_results additionally records the raw
    POLICY_BLOCK_<CODE> value for every code that ran this request."""
    monkeypatch.setenv("POLICY_EXPOSURE_CEILING", "10")
    monkeypatch.setenv("POLICY_BLOCK_EXPOSURE_CEILING", "true")
    check_results = [{"code": "exposure_ceiling", "outcome": "advise"}]
    snapshot = config_snapshot(check_results)
    assert snapshot == {
        "POLICY_EXPOSURE_CEILING": "10",
        "POLICY_BLOCK_EXPOSURE_CEILING": "true",
    }


def test_config_snapshot_with_check_results_records_unset_flag_as_none(monkeypatch):
    monkeypatch.delenv("POLICY_BLOCK_OVER_FMV", raising=False)
    check_results = [{"code": "over_fmv", "outcome": "pass"}]
    snapshot = config_snapshot(check_results)
    assert snapshot["POLICY_BLOCK_OVER_FMV"] is None


def test_config_snapshot_empty_check_results_matches_bare_call(monkeypatch):
    """An empty list must not add any POLICY_BLOCK_* keys — same shape as
    omitting the argument entirely."""
    monkeypatch.setenv("POLICY_EXPOSURE_CEILING", "5")
    assert config_snapshot([]) == config_snapshot()


# ---------------------------------------------------------------------------
# BUI-623 (U9) — blocking mode with audited bypass
# ---------------------------------------------------------------------------

def test_policy_block_env_var_uppercases_code():
    assert _policy_block_env_var("over_fmv") == "POLICY_BLOCK_OVER_FMV"
    assert _policy_block_env_var("exposure_ceiling") == "POLICY_BLOCK_EXPOSURE_CEILING"


def test_block_flag_unset_is_off(monkeypatch):
    monkeypatch.delenv("POLICY_BLOCK_OVER_FMV", raising=False)
    assert _block_flag_on("over_fmv") is False


def test_block_flag_truthy_values_are_on(monkeypatch):
    for value in ("1", "true", "TRUE", "yes", "on"):
        monkeypatch.setenv("POLICY_BLOCK_OVER_FMV", value)
        assert _block_flag_on("over_fmv") is True, value


def test_block_flag_falsy_values_are_off(monkeypatch):
    for value in ("0", "false", "no", "off", ""):
        monkeypatch.setenv("POLICY_BLOCK_OVER_FMV", value)
        assert _block_flag_on("over_fmv") is False, value


def test_block_flag_malformed_value_is_off_not_a_crash(monkeypatch, caplog):
    """Adversarial: a malformed flag value must never be silently
    interpreted as 'on' — a boolean flag that starts blocking real-money
    writes must fail in the 'stays off' direction, not the 'starts
    blocking' one."""
    monkeypatch.setenv("POLICY_BLOCK_OVER_FMV", "banana")
    with caplog.at_level("WARNING", logger="server.policy"):
        result = _block_flag_on("over_fmv")
    assert result is False
    assert any("POLICY_BLOCK_OVER_FMV" in rec.message for rec in caplog.records)


def test_block_flag_whitespace_and_case_insensitive(monkeypatch):
    monkeypatch.setenv("POLICY_BLOCK_OVER_FMV", "  True  ")
    assert _block_flag_on("over_fmv") is True


def test_evaluate_block_no_blocking_flags_never_blocks(monkeypatch):
    monkeypatch.delenv("POLICY_BLOCK_EXPOSURE_CEILING", raising=False)
    check_results = [{"code": "exposure_ceiling", "outcome": "advise", "data": {}}]
    advisories = [{"code": "exposure_ceiling", "severity": "warning", "message": "m", "data": {}}]
    decision = evaluate_block(check_results, advisories, bypass=False)
    assert decision.blocked is False
    assert decision.blocking_codes == []


def test_evaluate_block_advise_plus_flag_no_bypass_blocks(monkeypatch):
    monkeypatch.setenv("POLICY_BLOCK_EXPOSURE_CEILING", "true")
    check_results = [{"code": "exposure_ceiling", "outcome": "advise", "data": {}}]
    advisories = [{"code": "exposure_ceiling", "severity": "warning", "message": "m", "data": {}}]
    decision = evaluate_block(check_results, advisories, bypass=False)
    assert decision.blocked is True
    assert decision.blocking_codes == ["exposure_ceiling"]


def test_evaluate_block_advise_plus_flag_with_bypass_does_not_block(monkeypatch):
    """The audited bypass suppresses the block, but blocking_codes still
    names what WOULD have blocked — the ledger/response can still record
    what was acknowledged."""
    monkeypatch.setenv("POLICY_BLOCK_EXPOSURE_CEILING", "true")
    check_results = [{"code": "exposure_ceiling", "outcome": "advise", "data": {}}]
    advisories = [{"code": "exposure_ceiling", "severity": "warning", "message": "m", "data": {}}]
    decision = evaluate_block(check_results, advisories, bypass=True)
    assert decision.blocked is False
    assert decision.blocking_codes == ["exposure_ceiling"]


def test_evaluate_block_pass_outcome_with_flag_on_never_blocks(monkeypatch):
    monkeypatch.setenv("POLICY_BLOCK_EXPOSURE_CEILING", "true")
    check_results = [{"code": "exposure_ceiling", "outcome": "pass", "data": {}}]
    decision = evaluate_block(check_results, [], bypass=False)
    assert decision.blocked is False
    assert decision.blocking_codes == []


def test_evaluate_block_unevaluable_never_blocks_even_with_flag_on(monkeypatch):
    """Guard-strictness: fail-closed is reserved for a check that
    AFFIRMATIVELY fired — an unevaluable check never blocks by itself, no
    matter how its flag is set."""
    monkeypatch.setenv("POLICY_BLOCK_EXPOSURE_CEILING", "true")
    check_results = [{"code": "exposure_ceiling", "outcome": "unevaluable", "data": {}}]
    advisories = [{"code": "exposure_ceiling", "severity": "unevaluable", "message": "m", "data": {}}]
    decision = evaluate_block(check_results, advisories, bypass=False)
    assert decision.blocked is False
    assert decision.blocking_codes == []


def test_evaluate_block_unevaluable_with_flag_on_marks_both_response_and_result(monkeypatch):
    """A blocking check gone blind must be visible in BOTH check_results
    (the ledger's checks_json) and advisories (the response/ledger's
    advisories_json) — not silently permissive."""
    monkeypatch.setenv("POLICY_BLOCK_EXPOSURE_CEILING", "true")
    check_results = [{"code": "exposure_ceiling", "outcome": "unevaluable", "data": {}}]
    advisories = [{"code": "exposure_ceiling", "severity": "unevaluable", "message": "m", "data": {}}]
    decision = evaluate_block(check_results, advisories, bypass=False)
    assert decision.unevaluable_while_blocking_codes == ["exposure_ceiling"]
    assert check_results[0]["data"]["unevaluable_while_blocking"] is True
    assert advisories[0]["data"]["unevaluable_while_blocking"] is True


def test_evaluate_block_unevaluable_flag_off_no_marker(monkeypatch):
    monkeypatch.delenv("POLICY_BLOCK_EXPOSURE_CEILING", raising=False)
    check_results = [{"code": "exposure_ceiling", "outcome": "unevaluable", "data": {}}]
    advisories = [{"code": "exposure_ceiling", "severity": "unevaluable", "message": "m", "data": {}}]
    decision = evaluate_block(check_results, advisories, bypass=False)
    assert decision.unevaluable_while_blocking_codes == []
    assert "unevaluable_while_blocking" not in check_results[0]["data"]
    assert "unevaluable_while_blocking" not in advisories[0]["data"]


def test_evaluate_block_flag_set_for_a_code_that_never_ran(monkeypatch):
    """Adversarial: a flag set for a check code that isn't in check_results
    at all (a typo'd env var, or a plugin check that never registered) must
    not raise and must not block anything — there is nothing to act on."""
    monkeypatch.setenv("POLICY_BLOCK_SOME_CHECK_THAT_DOES_NOT_EXIST", "true")
    check_results = [{"code": "exposure_ceiling", "outcome": "advise", "data": {}}]
    decision = evaluate_block(check_results, [], bypass=False)
    assert decision.blocked is False


def test_evaluate_block_multiple_blocking_checks_all_named(monkeypatch):
    monkeypatch.setenv("POLICY_BLOCK_EXPOSURE_CEILING", "true")
    monkeypatch.setenv("POLICY_BLOCK_OVER_FMV", "true")
    check_results = [
        {"code": "exposure_ceiling", "outcome": "advise", "data": {}},
        {"code": "over_fmv", "outcome": "advise", "data": {}},
        {"code": "staleness", "outcome": "pass", "data": {}},
    ]
    decision = evaluate_block(check_results, [], bypass=False)
    assert decision.blocked is True
    assert set(decision.blocking_codes) == {"exposure_ceiling", "over_fmv"}


def test_build_block_detail_shape():
    decision = BlockDecision(blocked=True, blocking_codes=["over_fmv"])
    advisories = [{"code": "over_fmv", "severity": "warning", "message": "m", "data": {}}]
    detail = build_block_detail(decision, advisories, surviving_snipe=None)
    assert detail["blocked"] is True
    assert "over_fmv" in detail["message"]
    assert detail["blocking_codes"] == ["over_fmv"]
    assert detail["advisories"] == advisories
    assert detail["surviving_snipe"] is None
    assert detail["unevaluable_while_blocking"] == []


def test_build_block_detail_names_surviving_snipe():
    """R14/U9 acceptance: a blocked upsert/edit of a live row must name the
    surviving snipe and its current max_bid in the message text, not just
    bury it in structured data."""
    decision = BlockDecision(blocked=True, blocking_codes=["over_fmv"])
    detail = build_block_detail(
        decision, [], surviving_snipe={"item_id": "123", "max_bid": 100.0},
    )
    assert "100.00" in detail["message"]
    assert "remains active" in detail["message"]
    assert detail["surviving_snipe"] == {"item_id": "123", "max_bid": 100.0}
