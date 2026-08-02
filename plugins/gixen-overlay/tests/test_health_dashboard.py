"""BUI-601/602: the dashboard health strip.

"A nonzero count is visible on the dashboard without asking" is BUI-601's
acceptance criterion, and the whole strip has one rule that is easy to get
wrong: it may render "fine" ONLY when the server has affirmatively said so.
A failed health fetch, or a job nobody has instrumented yet, must render as
its own visible state — never as green and never as silence. That is exactly
the fails-green mistake both tickets exist to correct, so it is pinned here.

Runs the page's real JS in node, same technique as test_gixen_overlay_routes.py.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile

import pytest

_HTML_PATH = os.path.join(
    os.path.dirname(__file__), "..", "src", "gixen_overlay", "static", "v2-comics.html"
)


@pytest.fixture(scope="module")
def html() -> str:
    with open(_HTML_PATH) as fh:
        return fh.read()


@pytest.fixture(scope="module")
def node() -> str:
    exe = shutil.which("node")
    if exe is None:  # pragma: no cover
        pytest.skip("node not installed")
    return exe


def _extract_function(source: str, name: str) -> str:
    """Brace-match a top-level `function <name>(...) {...}` out of JS source.

    Same helper as `_extract_js_function` in test_gixen_overlay_routes.py; the
    overlay suite has no conftest, and every test module here carries its own
    copy of the fixtures it needs.
    """
    start = source.index(f"function {name}(")
    depth = 0
    i = source.index("{", start)
    while i < len(source):
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
            if depth == 0:
                return source[start : i + 1]
        i += 1
    raise AssertionError(f"unterminated function {name}")  # pragma: no cover


def _run_health_chips(node: str, html: str, health: dict) -> str:
    script = (
        f"{_extract_function(html, 'escapeHtml')}\n"
        f"{_extract_function(html, 'healthChips')}\n"
        "let _health = JSON.parse(process.argv[1]);\n"
        "process.stdout.write(healthChips());\n"
    )
    out = subprocess.run(
        [node, "-e", script, json.dumps(health)],
        capture_output=True, text=True, check=True,
    )
    return out.stdout


def _healthy_payload(**overrides) -> dict:
    payload = {
        "rejections": {"count": 0, "window_hours": 24.0},
        "heartbeats": {
            "stale_jobs": [],
            "never_seen_jobs": [],
            "pending_instrumentation_jobs": [],
        },
        "error": None,
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# Page wiring
# ---------------------------------------------------------------------------


def test_page_script_is_syntactically_valid(node, html):
    """The page has no other syntax guard; a broken script silently blanks the
    whole dashboard."""
    src = re.search(r"<script>(.*?)</script>", html, re.S).group(1)
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
        fh.write(src)
        path = fh.name
    result = subprocess.run([node, "--check", path], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_page_polls_both_health_endpoints(html):
    assert '"/api/comics/health/rejections"' in html
    assert '"/api/comics/health/heartbeats"' in html


def test_health_has_its_own_refresh_loop(html):
    """Kept independent of load() so a failing snipes fetch can't take the
    health strip down with it."""
    assert "setInterval(loadHealth, HEALTH_REFRESH_MS)" in html


# ---------------------------------------------------------------------------
# The one rule: green only when the server said so
# ---------------------------------------------------------------------------


def test_failed_health_fetch_renders_unknown_not_green(node, html):
    out = _run_health_chips(
        node, html, {"rejections": None, "heartbeats": None, "error": "HTTP 500"}
    )
    assert "health unknown" in out
    assert "health-chip unknown" in out
    assert "good" not in out
    assert "HTTP 500" in out


def test_nonzero_rejections_are_visible_without_asking(node, html):
    out = _run_health_chips(
        node, html, _healthy_payload(rejections={"count": 3, "window_hours": 24.0})
    )
    assert "3 rejected writes" in out
    assert "health-chip bad" in out


def test_single_rejection_is_not_pluralised(node, html):
    out = _run_health_chips(
        node, html, _healthy_payload(rejections={"count": 1, "window_hours": 24.0})
    )
    assert "1 rejected write<" in out


def test_stale_job_shows_as_bad(node, html):
    out = _run_health_chips(
        node,
        html,
        _healthy_payload(
            heartbeats={
                "stale_jobs": ["gixen-sync"],
                "never_seen_jobs": ["fmv-refresh"],
                "pending_instrumentation_jobs": [],
            }
        ),
    )
    assert "2 jobs stale" in out
    assert "health-chip bad" in out


def test_pending_instrumentation_never_shows_as_ok(node, html):
    """A declared-but-unwired job means health is UNKNOWN, not good. Rendering
    it green would be the exact fails-green bug BUI-602 exists to kill."""
    out = _run_health_chips(
        node,
        html,
        _healthy_payload(
            heartbeats={
                "stale_jobs": [],
                "never_seen_jobs": [],
                "pending_instrumentation_jobs": ["gixen-sync", "fmv-refresh"],
            }
        ),
    )
    assert "2 jobs unmonitored" in out
    assert "health-chip warn" in out
    assert "jobs ok" not in out


def test_all_clear_shows_a_quiet_green_chip_and_no_rejection_chip(node, html):
    out = _run_health_chips(node, html, _healthy_payload())
    assert "jobs ok" in out
    assert "health-chip good" in out
    assert "rejected" not in out
