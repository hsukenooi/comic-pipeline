"""Guard for BUI-172: the one shared comics-server call convention.

scripts/comics-server.sh is the single definition every /comic:* skill routes
through to resolve GIXEN_SERVER_URL, health-gate, and make hard-failing calls.
These tests pin its behaviour so the BUI-151/154/157/169/170-class divergences
(missing fallback, swallowed failures, empty-on-error) cannot silently return.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "comics-server.sh"


def _run(snippet: str, *, fake_hostname: str | None = None) -> subprocess.CompletedProcess:
    """Source the helper and run a bash snippet; optionally shadow `hostname`."""
    prelude = ""
    if fake_hostname is not None:
        prelude = f"hostname() {{ echo '{fake_hostname}'; }}\n"
    body = f"{prelude}source '{SCRIPT}'\n{snippet}\n"
    return subprocess.run(
        ["bash", "-c", body], capture_output=True, text=True
    )


def test_script_exists():
    assert SCRIPT.is_file(), f"shared convention missing at {SCRIPT}"


def test_preset_url_is_respected():
    r = _run('export GIXEN_SERVER_URL=http://preset:9; comics_resolve_server && echo "$GIXEN_SERVER_URL"')
    assert r.returncode == 0
    assert r.stdout.strip() == "http://preset:9"


def test_macbook_hostname_infers_tailscale():
    r = _run("unset GIXEN_SERVER_URL; comics_resolve_server && echo \"$GIXEN_SERVER_URL\"",
             fake_hostname="Hsus-MacBook-Air.local")
    assert r.returncode == 0
    assert r.stdout.strip() == "http://mac-mini.tail9b7fa5.ts.net:8080"


def test_macmini_hostname_infers_localhost():
    r = _run("unset GIXEN_SERVER_URL; comics_resolve_server && echo \"$GIXEN_SERVER_URL\"",
             fake_hostname="Hsus-Mac-mini.local")
    assert r.returncode == 0
    assert r.stdout.strip() == "http://localhost:8080"


def test_unrecognised_hostname_hard_fails():
    r = _run("unset GIXEN_SERVER_URL; comics_resolve_server",
             fake_hostname="some-random-box")
    assert r.returncode != 0
    assert "unrecognised" in r.stderr.lower()


def test_comics_curl_hard_fails_loudly_on_unreachable():
    # Port 9 (discard) refuses fast — a failed call must exit non-zero AND
    # print a loud diagnostic, never an empty success.
    r = _run("comics_curl http://127.0.0.1:9/nope")
    assert r.returncode != 0
    assert "FAILED" in r.stderr
    assert r.stdout == ""


def test_health_gate_requires_resolved_url():
    r = _run("unset GIXEN_SERVER_URL; comics_health_gate")
    assert r.returncode != 0
    assert "GIXEN_SERVER_URL" in r.stderr


@pytest.mark.parametrize("func", ["comics_resolve_server", "comics_health_gate", "comics_curl"])
def test_all_three_functions_are_defined(func: str):
    r = _run(f"type {func}")
    assert r.returncode == 0, f"{func} is not defined by the shared convention"
