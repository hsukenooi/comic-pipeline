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


def test_the_four_named_jobs_are_declared():
    assert set(JOB_CONTRACTS) == {
        "gixen-sync",
        "wishlist-sellers",
        "collection-sync",
        "fmv-refresh",
    }


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


def test_never_pinged_unwired_job_is_pending_not_ok(conn):
    report = heartbeat_report(conn, now=NOW)
    statuses = {j["job"]: j["status"] for j in report["jobs"]}
    assert set(statuses.values()) == {"pending_instrumentation"}
    assert "ok" not in statuses.values()
    assert sorted(report["pending_instrumentation_jobs"]) == sorted(JOB_CONTRACTS)


def test_never_pinged_wired_job_is_an_alarm(conn, monkeypatch):
    """Once a caller IS wired, silence becomes the alarm — not a shrug."""
    monkeypatch.setitem(
        JOB_CONTRACTS["fmv-refresh"], "wired", True
    )
    report = heartbeat_report(conn, now=NOW)
    entry = next(j for j in report["jobs"] if j["job"] == "fmv-refresh")
    assert entry["status"] == "never"
    assert report["never_seen_jobs"] == ["fmv-refresh"]
    assert report["healthy"] is False


def test_todays_report_is_not_healthy(api):
    """Nothing pings yet, so the deployed answer today is an honest 'no'.

    Pinned deliberately: if this ever flips to True without a job being wired,
    the watchdog has started lying."""
    report = api.get("/api/comics/health/heartbeats").json()
    assert report["healthy"] is False
    assert sorted(report["pending_instrumentation_jobs"]) == sorted(JOB_CONTRACTS)


def test_report_iterates_the_contract_not_the_table(conn):
    """A job with no row must still appear. Iterating stored heartbeats would
    render "never ran" as absence — i.e. as nothing wrong at all."""
    record_heartbeat(conn, "fmv-refresh", at=NOW.isoformat())
    report = heartbeat_report(conn, now=NOW)
    assert {j["job"] for j in report["jobs"]} == set(JOB_CONTRACTS)


def test_pending_jobs_keep_the_report_unhealthy(conn, monkeypatch):
    """Three uninstrumented jobs plus one fresh one is NOT a clean bill of
    health, and `healthy` must not say it is.

    This is the report's own fails-green trap: the outer-ping recipe in
    docs/reference/job-heartbeat-contract.md alarms on `healthy == false`, so
    a version that counted only wired jobs would hand an external monitor a
    green light for a system observing almost nothing.
    """
    monkeypatch.setitem(JOB_CONTRACTS["fmv-refresh"], "wired", True)
    record_heartbeat(conn, "fmv-refresh", at=NOW.isoformat())
    report = heartbeat_report(conn, now=NOW)
    assert report["healthy"] is False
    assert report["stale_jobs"] == []
    assert report["never_seen_jobs"] == []
    assert len(report["pending_instrumentation_jobs"]) == 3


def test_healthy_only_when_every_job_is_wired_and_fresh(conn, monkeypatch):
    for job in JOB_CONTRACTS:
        monkeypatch.setitem(JOB_CONTRACTS[job], "wired", True)
        record_heartbeat(conn, job, at=NOW.isoformat())
    report = heartbeat_report(conn, now=NOW)
    assert report["healthy"] is True
    assert report["pending_instrumentation_jobs"] == []


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


def test_report_lists_the_wiring_instruction_for_pending_jobs(api):
    report = api.get("/api/comics/health/heartbeats").json()
    pending = [j for j in report["jobs"] if j["status"] == "pending_instrumentation"]
    assert pending
    for j in pending:
        assert j["ping"], f"{j['job']} has no wiring instruction"
