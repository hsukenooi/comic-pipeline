"""Doc-lint regressions for the collection-sync skill (BUI-138/157/158).

These are instruction-file contracts: the skill body is bash an agent runs
verbatim, so a stale-file reuse, an unguarded server call, or a dangling shell
variable is a real defect. Pin the fixes so they can't silently regress.
"""
from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SKILL = REPO_ROOT / ".claude" / "commands" / "comic" / "collection-sync.md"


@pytest.fixture(scope="module")
def text() -> str:
    return SKILL.read_text()


def test_export_does_not_reuse_fixed_stale_temp_file(text):
    """BUI-138: the fixed /tmp/sync-export.json was left untouched on a failed
    export and re-parsed into a CSV from stale data. The export must use a fresh
    per-run temp file (mktemp) instead."""
    assert "/tmp/sync-export.json" not in text, (
        "export still uses the fixed /tmp/sync-export.json — a stale file can be re-read"
    )
    assert "mktemp" in text, "export must use a fresh mktemp temp file per run"


def test_export_hard_fails_before_parsing(text):
    """BUI-138: the export call must hard-fail (and stop) before the python parse,
    so a failed fetch can't fall through to building a CSV."""
    assert "comics-api GET /api/comics/collection/export" in text
    # the export line chains a failure guard that exits before the parse
    assert "not generating a CSV from stale data" in text


def test_step0_routes_through_shared_server_convention(text):
    """BUI-157 (+ BUI-172/BUI-510 adoption): Step 0 resolves/health-gates and
    reads status through `comics-api` (which health-gates internally before
    every call) so a 500 hard-fails rather than slipping past the null-import
    gate."""
    assert "comics-api GET /api/comics/collection/status" in text


def test_no_dangling_csv_variable(text):
    """BUI-158: Step 3's split reads $CSV, which was never assigned. If the skill
    references $CSV it must also bind it."""
    if "$CSV" in text:
        assert "CSV=" in text, "$CSV is read but never assigned in the skill"


def test_step1_backup_has_no_hostname_ssh_branching(text):
    """BUI-433: Step 1 used to back up the store via client-orchestrated
    `cp -r` + `ssh` + `case "$(hostname)"` MacBook/Mac-Mini branching. The
    server now backs up its own store locally, so that branching must be
    gone — the ONLY remaining `hostname` reference in the doc is the
    unrelated `comics_resolve_server` convention note, not backup logic."""
    assert 'case "$(hostname)"' not in text
    assert "ssh mini" not in text
    assert "ssh " not in text
    assert "*MacBook*" not in text and "*macbook*" not in text


def test_step1_backup_is_one_server_call(text):
    """BUI-433: Step 1 becomes a single POST to the backup endpoint."""
    assert "comics-api POST /api/comics/collection/backup" in text
    assert "BACKUP_PATH=" in text
    assert "do not proceed without a backup" in text


def test_abort_paths_reference_restore_endpoint(text):
    """BUI-433: Steps 3/3b/6's abort instructions must reference the
    executable restore endpoint, not just prose ("restore from the Step 1
    backup" with no command)."""
    assert text.count("comics-api POST /api/comics/collection/restore") >= 3
    assert "backup_path" in text
