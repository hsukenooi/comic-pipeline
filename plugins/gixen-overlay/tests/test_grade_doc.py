"""Doc-lint regressions for the grade skill (BUI-147/165).

grade.md's Step 1 used to embed the download script as an inline ```python```
block an agent ran verbatim. BUI-279 extracted it to
apps/ebay/src/grade_photos.py (grade.md now just invokes it), so these tests
target the extracted script instead of regex-pulling a code block out of the
doc. A missing HTTP status check turned an API hiccup into a silent
image_count=0 (BUI-147), and a stale comment contradicted the code on BIN
pricing (BUI-165). Pin both.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SKILL = REPO_ROOT / ".claude" / "commands" / "comic" / "grade.md"
SCRIPT = REPO_ROOT / "apps" / "ebay" / "src" / "grade_photos.py"


@pytest.fixture(scope="module")
def text() -> str:
    return SKILL.read_text()


@pytest.fixture(scope="module")
def step1_script() -> str:
    # BUI-279: the doc no longer embeds this — read the extracted script the
    # doc invokes.
    return SCRIPT.read_text()


def test_step1_script_parses(step1_script):
    ast.parse(step1_script)  # the agent invokes this — must be valid python


def test_download_checks_http_status_and_retries_429(step1_script):
    """BUI-147: the fetch must check the HTTP status and retry 429 before
    calling .json(), so a down/429/404 API aborts loudly instead of silently
    yielding image_count=0 (which triage would DROP as un-gradeable)."""
    assert "status_code" in step1_script, "no HTTP status check before parsing"
    assert "429" in step1_script, "no 429 retry handling"
    # A persistent non-200 must raise, not fall through to an empty image list.
    assert "raise RuntimeError" in step1_script
    # And the surrounding loop must surface the failure, not swallow it.
    assert "FETCH FAILED" in step1_script


def test_no_contradictory_none_for_bin_claim(text, step1_script):
    """BUI-165: the code populates current_price from the BIN price, so neither
    the doc nor the extracted downloader script (BUI-279) may claim it is None
    for BIN. The contradicted phrasing is gone from both."""
    combined = text + step1_script
    assert "None for BIN" not in combined
    # The corrected semantics are documented: BIN price counts toward the gate.
    assert "BIN price" in combined
