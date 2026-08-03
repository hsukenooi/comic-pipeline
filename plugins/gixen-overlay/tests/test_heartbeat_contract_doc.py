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
# The module that DEFINES JOB_CONTRACTS. Never counts as a ping call site —
# see `_ping_call_sites`.
_CONTRACT_MODULE = (
    REPO_ROOT / "plugins" / "gixen-overlay" / "src" / "gixen_overlay" / "db.py"
)


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
    """Every file in the repo that records `job`'s heartbeat on success.

    Two shapes count, because BUI-624 produced two:

    * ``/api/heartbeat/<job>`` — the HTTP ping. Four of the five jobs use it
      (the two /comic:* skills via `comics-api`, apps/fmv's runner and sentinel
      probe via `requests`).
    * ``record_heartbeat(conn, "<job>"`` — the in-process write. `gixen-sync`
      cannot use HTTP: `packages/gixen-cli` has no import edge to this package
      and the ping has to land inside `_sync_gixen`'s own transaction, so it
      arrives via the `on_sync_observed` hookspec and this plugin stores it
      directly. Requiring an HTTP literal would have forced the flag to lie
      about a job that genuinely does ping.

    Three exclusions, each closing a way this check could pass vacuously:

    * `tests` directories — a fixture calling `record_heartbeat` is not a call
      site, and counting one would let a job pass on the strength of its own
      test;
    * `__pycache__` — a stale `.pyc` outlives the source it was compiled from,
      so deleting a ping and leaving the bytecode behind would keep this green;
    * `db.py` itself (`_CONTRACT_MODULE`) — the contract's own `ping` field
      names the endpoint in prose. That is a DESCRIPTION of a call site, not
      one, and counting it would make this test vacuous in the worst possible
      way: every job would look wired the moment someone wrote down where its
      ping *belongs*, which is exactly the hand-maintained claim being checked.
    """
    roots = [
        REPO_ROOT / ".claude" / "commands",
        REPO_ROOT / "apps",
        REPO_ROOT / "packages",
        REPO_ROOT / "plugins",
        REPO_ROOT / "scripts",
    ]
    needles = (f"/api/heartbeat/{job}", f'record_heartbeat(conn, "{job}"')
    hits = []
    for root in roots:
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if not path.is_file() or path == _CONTRACT_MODULE:
                continue
            if {".venv", "node_modules", "tests", "__pycache__"} & set(path.parts):
                continue
            try:
                text = path.read_text(errors="ignore")
            except OSError:  # pragma: no cover - unreadable file
                continue
            if any(n in text for n in needles):
                hits.append(path)
    return hits


@pytest.mark.parametrize("job", sorted(JOB_CONTRACTS))
def test_wired_flag_matches_reality(job):
    """The contract's own fails-green hole, closed.

    `wired` is a hand-maintained boolean claiming the caller actually pings,
    and four of the five call sites live outside this plugin (gixen-cli, the
    /comic:* skills, apps/fmv twice) where nothing in this suite would notice
    it going stale. A watchdog whose own bookkeeping can drift silently is the
    bug class one level up — so derive the truth from the repo instead:
    `wired` is true if and only if something actually records the heartbeat.

    When you wire a job, this test tells you to flip the flag (and
    test_row_cadence_matches_the_constant tells you to update the doc).

    The detector matches literal strings, so reformatting a call site across
    lines will fail this test. That failure direction is the safe one — it
    shouts "a job you believe is wired may not be" and a human confirms — and
    it is why `_ping_call_sites` matches the precise call form rather than
    `record_heartbeat(` plus the job name anywhere in a file (which db.py's own
    JOB_CONTRACTS keys would satisfy vacuously).
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


def test_the_contract_table_is_not_its_own_call_site():
    """Guard the guard (BUI-624).

    `_ping_call_sites` gained `plugins/` as a scan root so it could see the
    in-process `gixen-sync` ping. That root also contains db.py, whose `ping`
    field spells out `/api/heartbeat/<job>` for four of the five jobs — so
    without the `_CONTRACT_MODULE` exclusion, `test_wired_flag_matches_reality`
    would pass for any job merely because someone documented where its ping
    should go. A test that green-lights a wiring claim from the claim itself is
    the fails-green bug this whole file exists to prevent, one level up.
    """
    contract_text = _CONTRACT_MODULE.read_text()
    documented_in_prose = [
        job for job in JOB_CONTRACTS if f"/api/heartbeat/{job}" in contract_text
    ]
    assert documented_in_prose, (
        "harness assumption broken: the contract no longer names any endpoint "
        "in prose, so this test proves nothing — re-check the exclusion"
    )
    for job in documented_in_prose:
        assert _CONTRACT_MODULE not in _ping_call_sites(job), (
            f"{job}: db.py is being counted as its own ping call site"
        )


def test_call_site_detector_finds_both_ping_shapes():
    """Guard the guard, other half: the detector must actually match.

    A typo in either needle would silently return no hits, which
    `test_wired_flag_matches_reality` would report as "wired=True but nothing
    pings" — a confusing failure, or worse, a green one if someone then
    'fixed' it by flipping the flag. Pin one job of each shape.
    """
    http_job = _ping_call_sites("fmv-refresh")
    assert any(p.name == "fmv_runner.py" for p in http_job), http_job

    in_process = _ping_call_sites("gixen-sync")
    assert any(p.name == "plugin.py" for p in in_process), in_process


_SKILL_PING_GUARDS = {
    "wishlist-sellers.md": ("exit 0", "exit 3"),
    "collection-sync.md": ("aborted", "BUI-122"),
}


@pytest.mark.parametrize("skill, guards", sorted(_SKILL_PING_GUARDS.items()))
def test_skill_pings_stay_guarded(skill, guards):
    """The two skill-hosted pings must state their failure condition (BUI-624).

    These call sites are prose an agent follows, not code a runtime enforces,
    so this is the only mechanization available — and the thing worth
    mechanizing is not that the ping exists (test_wired_flag_matches_reality
    covers that) but that its GUARD does. An unguarded ping is strictly worse
    than no ping: `wishlist-sellers` exiting 3 means candidates were never
    verified, and a `collection-sync` abort means the BUI-122 data-loss probe
    tripped. Certifying either as a healthy run turns the watchdog into a
    rubber stamp on exactly the runs a human most needs to see.
    """
    body = (REPO_ROOT / ".claude" / "commands" / "comic" / skill).read_text()
    job = skill.removesuffix(".md")
    ping = f"/api/heartbeat/{job}"
    assert ping in body, f"{skill} no longer pings {job}"

    # The guard has to live in the ping's own `##` section, not merely
    # somewhere in a 500-line skill — bounded on BOTH sides, or a guard three
    # sections away would keep this green.
    at = body.index(ping)
    start = body.rindex("\n## ", 0, at)
    end = body.find("\n## ", at)
    section = body[start:] if end == -1 else body[start:end]
    for guard in guards:
        assert guard in section, (
            f"{skill}: the section containing the ping no longer mentions "
            f"{guard!r} — the ping may have lost its guard"
        )


def test_doc_records_why_middleware_was_not_used(text):
    """FastAPI middleware cannot be added from a plugin whose register_routes
    hook fires inside the host lifespan. Keep the reason written down so the
    next person does not re-try it."""
    assert "add_middleware" in text
    assert "lifespan" in text
