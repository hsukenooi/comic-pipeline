"""BUI-602: job heartbeats + the cadence watchdog.

The repo's dominant trap class is "fails green" — a job that dies or silently
no-ops looks identical to a healthy one, and error-based alerting structurally
cannot see it. These tests pin the properties that make the heartbeat a real
signal rather than a second thing that fails green:

* silence is never rendered as health (`never` / `pending_instrumentation`);
* a job absent from the heartbeats table is still reported, because iterating
  stored rows would render "never ran" as nothing at all;
* an unparseable or unknown-job ping fails loudly instead of storing quietly;
* the watchdog declares its own missing outer ping.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from gixen_overlay.db import (
    HEARTBEAT_STALE_FACTOR,
    JOB_CONTRACTS,
    create_tables,
    heartbeat_report,
    record_heartbeat,
)

NOW = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    create_tables(c)
    return c


# `api` fixture: see conftest.py (BUI-630 de-duplicated the three hand-copies).


# ---------------------------------------------------------------------------
# The contract table
# ---------------------------------------------------------------------------


def test_the_five_named_jobs_are_declared():
    """BUI-624 added `sentinel-probe` (BUI-603's probe, which pings but had no
    contract entry, so every ping 404'd). The set is still pinned rather than
    derived: a job silently disappearing from the contract removes it from the
    watchdog's iteration entirely, and `heartbeat_report` iterates the contract
    precisely so a missing job is visible instead of absent."""
    assert set(JOB_CONTRACTS) == {
        "gixen-sync",
        "wishlist-sellers",
        "collection-sync",
        "fmv-refresh",
        "sentinel-probe",
    }


def test_every_declared_job_is_wired():
    """BUI-624's acceptance criterion, as an assertion.

    Not the same claim as `test_wired_flag_matches_reality` in
    test_heartbeat_contract_doc.py: that one checks the flag is not a *lie*
    (flag true iff a call site exists, in either direction). This one checks
    that the flag is not *false* — that the project's whole point, `healthy:
    true` being reachable at all, has not silently regressed by someone
    flipping a job back to uninstrumented rather than fixing its ping.

    A genuinely new, not-yet-wired job is allowed to break this test. Declare
    it, watch this fail, and wire it — that is the intended workflow, not a
    reason to relax the assertion to `>= 4`."""
    unwired = sorted(j for j, c in JOB_CONTRACTS.items() if not c["wired"])
    assert unwired == [], (
        f"{unwired} are declared but not instrumented, so the watchdog can "
        f"never report healthy. Wire the ping or remove the contract entry — "
        f"a permanently-pending job trains its reader to ignore the report."
    )


@pytest.mark.parametrize("job", sorted(JOB_CONTRACTS))
def test_every_contract_is_complete(job):
    """The contract table IS the design doc; an entry missing its cadence or
    its success definition is a hole in the doc, not just in the code."""
    entry = JOB_CONTRACTS[job]
    assert set(entry) == {"cadence_hours", "success", "wired", "ping"}
    assert isinstance(entry["cadence_hours"], float) and entry["cadence_hours"] > 0
    assert isinstance(entry["wired"], bool)
    # A success definition has to actually say something.
    assert len(entry["success"]) > 60
    assert len(entry["ping"]) > 20


# ---------------------------------------------------------------------------
# Silence is never health
# ---------------------------------------------------------------------------


def test_never_pinged_job_is_an_alarm_not_a_shrug(conn):
    """Every job is wired since BUI-624, so an empty heartbeats table means
    every one of them is `never` — the alarm — and none is `ok`.

    Before BUI-624 this same silence read as `pending_instrumentation`, which
    was honest then (nothing was instrumented) and would be a lie now: a wired
    job that has not pinged is a job that has stopped running."""
    report = heartbeat_report(conn, now=NOW)
    statuses = {j["job"]: j["status"] for j in report["jobs"]}
    assert set(statuses.values()) == {"never"}
    assert sorted(report["never_seen_jobs"]) == sorted(JOB_CONTRACTS)
    assert report["pending_instrumentation_jobs"] == []
    assert report["healthy"] is False


def test_a_newly_declared_unwired_job_is_pending_not_ok(conn, monkeypatch):
    """The `pending_instrumentation` branch is still live and still not health.

    BUI-624 wired every job that exists today, which would leave this branch
    untested and free to rot — and the next job someone declares will land in
    it. A contract declared but not instrumented reports health as UNKNOWN; it
    must never report `ok`, and must never be quietly excluded from `healthy`.
    """
    monkeypatch.setitem(JOB_CONTRACTS["fmv-refresh"], "wired", False)
    report = heartbeat_report(conn, now=NOW)
    entry = next(j for j in report["jobs"] if j["job"] == "fmv-refresh")
    assert entry["status"] == "pending_instrumentation"
    assert report["pending_instrumentation_jobs"] == ["fmv-refresh"]
    assert report["healthy"] is False
    # And it still tells you how to close the gap.
    assert entry["ping"]


def test_todays_report_is_not_healthy_until_something_pings(api):
    """A fresh store is an honest 'no', now for a stronger reason than before.

    Pinned deliberately, and the pin has been re-aimed rather than removed. It
    used to guard "healthy must not flip True while jobs are uninstrumented".
    Every job IS instrumented now, so it guards the surviving half: `healthy`
    must not be True on the strength of the wiring alone. Nothing has pinged
    this server, so nothing is verified to be running, so the answer is no.
    If this ever passes with `healthy is True`, the report has started
    inferring health from code that exists rather than from jobs that ran."""
    report = api.get("/api/comics/health/heartbeats").json()
    assert report["healthy"] is False
    assert sorted(report["never_seen_jobs"]) == sorted(JOB_CONTRACTS)


def test_report_iterates_the_contract_not_the_table(conn):
    """A job with no row must still appear. Iterating stored heartbeats would
    render "never ran" as absence — i.e. as nothing wrong at all."""
    record_heartbeat(conn, "fmv-refresh", at=NOW.isoformat())
    report = heartbeat_report(conn, now=NOW)
    assert {j["job"] for j in report["jobs"]} == set(JOB_CONTRACTS)


def test_one_pending_job_keeps_the_whole_report_unhealthy(conn, monkeypatch):
    """Every job fresh EXCEPT one uninstrumented straggler is NOT a clean bill
    of health, and `healthy` must not say it is.

    This is the report's own fails-green trap: the outer-ping recipe in
    docs/reference/job-heartbeat-contract.md alarms on `healthy == false`, so
    a version that counted only wired jobs would hand an external monitor a
    green light while a declared job was observing nothing.
    """
    monkeypatch.setitem(JOB_CONTRACTS["fmv-refresh"], "wired", False)
    for job in JOB_CONTRACTS:
        if job != "fmv-refresh":
            record_heartbeat(conn, job, at=NOW.isoformat())
    report = heartbeat_report(conn, now=NOW)
    assert report["healthy"] is False
    assert report["stale_jobs"] == []
    assert report["never_seen_jobs"] == []
    assert report["pending_instrumentation_jobs"] == ["fmv-refresh"]


def test_healthy_when_every_declared_job_has_pinged(conn):
    """BUI-624's acceptance criterion: `healthy: true` is REACHABLE.

    No monkeypatching of `wired` — that is the point. Before BUI-624 this
    could only be demonstrated by forcing four flags to a value they did not
    have in the deployed system, which proved the arithmetic and nothing about
    the product. Now the real contract, pinged, reports healthy."""
    for job in JOB_CONTRACTS:
        record_heartbeat(conn, job, at=NOW.isoformat())
    report = heartbeat_report(conn, now=NOW)
    assert report["healthy"] is True
    assert report["pending_instrumentation_jobs"] == []
    assert report["never_seen_jobs"] == []
    assert report["stale_jobs"] == []


def test_one_stale_job_still_sinks_a_fully_wired_report(conn):
    """The other half of reachability: reachable must not mean automatic.

    A report that can say True has to keep being able to say False for the
    ordinary reason — one job stopped running — or wiring the jobs would have
    traded a permanently-red watchdog for a permanently-green one."""
    cadence = JOB_CONTRACTS["gixen-sync"]["cadence_hours"]
    for job in JOB_CONTRACTS:
        record_heartbeat(conn, job, at=NOW.isoformat())
    record_heartbeat(
        conn,
        "gixen-sync",
        at=(NOW - timedelta(hours=cadence * HEARTBEAT_STALE_FACTOR + 1)).isoformat(),
    )
    report = heartbeat_report(conn, now=NOW)
    assert report["healthy"] is False
    assert report["stale_jobs"] == ["gixen-sync"]


# ---------------------------------------------------------------------------
# Cadence
# ---------------------------------------------------------------------------


def test_fresh_ping_is_ok(conn):
    record_heartbeat(conn, "gixen-sync", at=(NOW - timedelta(minutes=5)).isoformat())
    entry = next(
        j for j in heartbeat_report(conn, now=NOW)["jobs"] if j["job"] == "gixen-sync"
    )
    assert entry["status"] == "ok"
    assert entry["age_hours"] == pytest.approx(5 / 60, abs=1e-3)


def test_job_is_flagged_only_past_the_stale_factor(conn):
    """Ordinary jitter (a scan 20 minutes behind, a laptop asleep past its cron
    slot) must not cry wolf — a muted watchdog is another fails-green
    instance."""
    cadence = JOB_CONTRACTS["gixen-sync"]["cadence_hours"]
    late_but_tolerated = NOW - timedelta(hours=cadence * HEARTBEAT_STALE_FACTOR * 0.9)
    record_heartbeat(conn, "gixen-sync", at=late_but_tolerated.isoformat())
    entry = next(
        j for j in heartbeat_report(conn, now=NOW)["jobs"] if j["job"] == "gixen-sync"
    )
    assert entry["status"] == "ok"


def test_job_past_its_cadence_is_stale(conn):
    cadence = JOB_CONTRACTS["gixen-sync"]["cadence_hours"]
    stale_at = NOW - timedelta(hours=cadence * HEARTBEAT_STALE_FACTOR + 1)
    record_heartbeat(conn, "gixen-sync", at=stale_at.isoformat())
    report = heartbeat_report(conn, now=NOW)
    assert report["stale_jobs"] == ["gixen-sync"]
    assert report["healthy"] is False


def test_unparseable_timestamp_is_stale_not_fresh(conn):
    """A corrupt stored timestamp must never read as recent."""
    conn.execute(
        "INSERT INTO heartbeats (job, last_success_at) VALUES ('gixen-sync', 'garbage')"
    )
    entry = next(
        j for j in heartbeat_report(conn, now=NOW)["jobs"] if j["job"] == "gixen-sync"
    )
    assert entry["status"] == "stale"
    assert entry["age_hours"] is None


def test_naive_timestamp_is_treated_as_utc(conn):
    conn.execute(
        "INSERT INTO heartbeats (job, last_success_at) VALUES (?, ?)",
        ("gixen-sync", (NOW - timedelta(minutes=10)).replace(tzinfo=None).isoformat()),
    )
    entry = next(
        j for j in heartbeat_report(conn, now=NOW)["jobs"] if j["job"] == "gixen-sync"
    )
    assert entry["status"] == "ok"


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


def test_repeat_pings_overwrite_and_count(conn):
    record_heartbeat(conn, "gixen-sync", at=(NOW - timedelta(hours=2)).isoformat())
    row = record_heartbeat(conn, "gixen-sync", detail="pass 2", at=NOW.isoformat())
    assert row["success_count"] == 2
    assert row["last_success_at"] == NOW.isoformat()
    assert row["detail"] == "pass 2"
    assert conn.execute("SELECT COUNT(*) FROM heartbeats").fetchone()[0] == 1


def test_unknown_stored_job_is_surfaced_not_dropped(conn):
    """A typo'd ping that stored fine would leave the real job looking dead
    while the watchdog stayed green. Surface it instead."""
    record_heartbeat(conn, "fmv-refesh", at=NOW.isoformat())
    report = heartbeat_report(conn, now=NOW)
    assert report["unknown_jobs"] == ["fmv-refesh"]


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


def test_ping_records_and_reports(api):
    r = api.post("/api/heartbeat/fmv-refresh")
    assert r.status_code == 200
    body = r.json()
    assert body["job"] == "fmv-refresh"
    assert body["success_count"] == 1
    assert body["cadence_hours"] == JOB_CONTRACTS["fmv-refresh"]["cadence_hours"]

    report = api.get("/api/comics/health/heartbeats").json()
    entry = next(j for j in report["jobs"] if j["job"] == "fmv-refresh")
    assert entry["status"] == "ok"
    assert entry["success_count"] == 1


def test_ping_accepts_a_detail(api):
    api.post("/api/heartbeat/fmv-refresh?detail=42%20books")
    report = api.get("/api/comics/health/heartbeats").json()
    entry = next(j for j in report["jobs"] if j["job"] == "fmv-refresh")
    assert entry["last_success_at"] is not None


def test_on_sync_observed_hookimpl_records_the_gixen_sync_heartbeat(conn):
    """BUI-624: the overlay half of the one job that cannot ping over HTTP.

    gixen-cli fires `on_sync_observed` from inside `_sync_gixen`'s apply-phase
    transaction (it has no import edge to this package, and the ping must land
    in that transaction). This hookimpl is where it becomes a heartbeat row —
    the same row an HTTP ping would have produced, so the report cannot tell
    the two mechanisms apart.
    """
    from gixen_overlay.plugin import plugin

    plugin.on_sync_observed(conn, 4)

    entry = next(
        j for j in heartbeat_report(conn, now=NOW)["jobs"] if j["job"] == "gixen-sync"
    )
    assert entry["status"] == "ok"
    assert entry["success_count"] == 1
    row = conn.execute(
        "SELECT detail FROM heartbeats WHERE job='gixen-sync'"
    ).fetchone()
    assert "4" in row["detail"]


def test_on_sync_observed_pings_on_a_zero_snipe_cycle(conn):
    """"Reached Gixen, nothing live right now" is a completed pass.

    Suppressing the ping here would make a quiet week indistinguishable from a
    dead sync loop — the exact confusion the contract table exists to end."""
    from gixen_overlay.plugin import plugin

    plugin.on_sync_observed(conn, 0)

    entry = next(
        j for j in heartbeat_report(conn, now=NOW)["jobs"] if j["job"] == "gixen-sync"
    )
    assert entry["status"] == "ok"


def test_unknown_job_is_404(api):
    """A silent accept would let a typo'd ping mask a genuinely dead job."""
    r = api.post("/api/heartbeat/not-a-real-job")
    assert r.status_code == 404
    assert "JOB_CONTRACTS" in r.json()["detail"]
    # And the refusal itself is ledgered (BUI-601 covers this route too).
    rejections = api.get("/api/comics/health/rejections").json()
    assert rejections["count"] == 1
    assert rejections["rejections"][0]["path"] == "/api/heartbeat/not-a-real-job"


def test_watchdog_endpoint_declares_its_own_blind_spot(api):
    """The gotcha BUI-602 names: nothing outside this machine polls the
    watchdog, so it can still fail green if the server is down. The endpoint
    must say so rather than imply a health it cannot vouch for."""
    report = api.get("/api/comics/health/heartbeats").json()
    assert report["outer_ping"] == "unwired"


def test_report_names_the_ping_site_for_every_silent_job(api):
    """A job with no heartbeat row must say WHERE its ping lives.

    Pre-BUI-624 the silent jobs were all `pending_instrumentation` and the
    instruction read as "go build this". Now they are `never` — wired, but not
    heard from — and the same field answers the more urgent question: which
    call site stopped firing, so the reader knows what to go look at.
    """
    report = api.get("/api/comics/health/heartbeats").json()
    silent = [
        j for j in report["jobs"]
        if j["status"] in ("never", "pending_instrumentation")
    ]
    assert silent, "fixture store should have no heartbeat rows"
    for j in silent:
        assert j["ping"], f"{j['job']} has no ping site recorded"
