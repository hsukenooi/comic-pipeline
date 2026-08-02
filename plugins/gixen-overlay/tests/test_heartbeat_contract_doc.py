"""BUI-602: pin docs/reference/job-heartbeat-contract.md to JOB_CONTRACTS.

The contract table IS the design doc for the Silent-Failure Observability
project, and a design doc that drifts from the code is worse than none — it
tells you a cadence the watchdog is not actually enforcing. So the prose table
and the constant are checked against each other here, same pattern as
test_collection_sync_doc.py / test_skill_contracts.py.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from gixen_overlay.db import (
    HEARTBEAT_OUTER_PING_STATE,
    HEARTBEAT_STALE_FACTOR,
    JOB_CONTRACTS,
    REJECTED_WRITES_RETENTION_DAYS,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
DOC = REPO_ROOT / "docs" / "reference" / "job-heartbeat-contract.md"


@pytest.fixture(scope="module")
def text() -> str:
    return DOC.read_text()


def _table_rows(text: str) -> dict[str, list[str]]:
    """Parse the `| job | cadence | ... |` table into {job: [cells]}."""
    rows: dict[str, list[str]] = {}
    for line in text.splitlines():
        m = re.match(r"^\|\s*`([a-z-]+)`\s*\|(.*)\|\s*$", line)
        if m:
            rows[m.group(1)] = [c.strip() for c in m.group(2).split("|")]
    return rows


def test_doc_exists(text):
    assert "Silent-Failure Observability" in text


def test_every_job_has_a_row(text):
    assert set(_table_rows(text)) == set(JOB_CONTRACTS)


@pytest.mark.parametrize("job", sorted(JOB_CONTRACTS))
def test_row_cadence_matches_the_constant(text, job):
    cadence, stale, _success, wired = _table_rows(text)[job]
    assert cadence == f"{JOB_CONTRACTS[job]['cadence_hours']:g}h"
    assert stale == (
        f"{JOB_CONTRACTS[job]['cadence_hours'] * HEARTBEAT_STALE_FACTOR:g}h"
    )
    assert wired == ("yes" if JOB_CONTRACTS[job]["wired"] else "no")


def test_outer_ping_gap_is_documented_prominently(text):
    """BUI-602's stated gotcha. A watchdog that can die unnoticed must not ship
    with the gap buried or unmentioned."""
    assert "## The outer layer — NOT WIRED" in text
    assert HEARTBEAT_OUTER_PING_STATE in text
    assert "healthchecks.io" in text
    # And a concrete, runnable recipe rather than a vague intention.
    assert "/api/comics/health/heartbeats" in text
    assert 'd["healthy"]' in text


def test_doc_states_the_retention_window(text):
    assert f"({REJECTED_WRITES_RETENTION_DAYS})" in text


def test_doc_points_at_the_machine_readable_twin(text):
    assert "JOB_CONTRACTS" in text
    assert "gixen_overlay/db.py" in text


def _ping_call_sites(job: str) -> list[Path]:
    """Every file in the repo that pings `job`'s heartbeat endpoint."""
    roots = [
        REPO_ROOT / ".claude" / "commands",
        REPO_ROOT / "apps",
        REPO_ROOT / "packages",
        REPO_ROOT / "scripts",
    ]
    needle = f"/api/heartbeat/{job}"
    hits = []
    for root in roots:
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if not path.is_file() or ".venv" in path.parts or "node_modules" in path.parts:
                continue
            try:
                if needle in path.read_text(errors="ignore"):
                    hits.append(path)
            except OSError:  # pragma: no cover - unreadable file
                continue
    return hits


@pytest.mark.parametrize("job", sorted(JOB_CONTRACTS))
def test_wired_flag_matches_reality(job):
    """The contract's own fails-green hole, closed.

    `wired` is a hand-maintained boolean claiming the caller actually pings,
    and three of the four call sites live outside this plugin (gixen-cli, the
    /comic:* skills, apps/fmv) where nothing in this suite would notice it
    going stale. A watchdog whose own bookkeeping can drift silently is the
    bug class one level up — so derive the truth from the repo instead:
    `wired` is true if and only if something actually calls the endpoint.

    When you wire a job, this test tells you to flip the flag (and
    test_row_cadence_matches_the_constant tells you to update the doc).
    """
    sites = _ping_call_sites(job)
    if JOB_CONTRACTS[job]["wired"]:
        assert sites, (
            f"{job} is marked wired=True but nothing in the repo pings "
            f"/api/heartbeat/{job} — the watchdog will report 'never' forever"
        )
    else:
        assert not sites, (
            f"{job} pings /api/heartbeat/{job} from "
            f"{[str(p.relative_to(REPO_ROOT)) for p in sites]} but is still "
            f"marked wired=False — flip it in JOB_CONTRACTS and in "
            f"{DOC.relative_to(REPO_ROOT)}, or its silence will read as "
            f"'not instrumented' instead of as an alarm"
        )


def test_doc_records_why_middleware_was_not_used(text):
    """FastAPI middleware cannot be added from a plugin whose register_routes
    hook fires inside the host lifespan. Keep the reason written down so the
    next person does not re-try it."""
    assert "add_middleware" in text
    assert "lifespan" in text
