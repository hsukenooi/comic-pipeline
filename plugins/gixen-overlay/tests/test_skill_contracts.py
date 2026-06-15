"""Skill <-> endpoint/script contract harness (BUI-173).

The /comic:* skills document contracts that actually live in code — endpoint
paths, response field sets, status/confidence enums, score thresholds, units,
command names. Nothing kept prose and code in sync, and 16 of the 36 seam-audit
findings were `contract-mismatch` born exactly that way. This harness asserts
the contracts hold against the real code so a future rename/field/enum change
that breaks a skill's documented contract fails CI instead of biting at runtime.

HOW TO EXTEND (the runbook's instruction): as each documented-contract drift is
fixed — BUI-142 (dollars vs cents), BUI-148 (grade_from_description), BUI-150
(status mapping), BUI-161/162/163 (fmv.md fields / 4th confidence level / a
command that doesn't exist), BUI-167 (score thresholds) — add a test HERE
asserting the now-consistent contract, so the same drift fails CI next time.
Add the assertion in the per-skill batch that fixes the drift, not before (the
contract is still violated until then, so the assertion would fail early).
"""
from __future__ import annotations

import re
from pathlib import Path

import gixen_overlay.routes as overlay_routes

REPO_ROOT = Path(__file__).resolve().parents[3]
SKILLS_DIR = REPO_ROOT / ".claude" / "commands" / "comic"


def _registered_comics_paths() -> set[str]:
    """Every /api/comics* path the overlay actually registers."""
    return {
        rt.path
        for rt in overlay_routes.router.routes
        if getattr(rt, "path", "").startswith("/api/comics")
    }


def _referenced_comics_paths() -> dict[str, set[str]]:
    """Concrete /api/comics* paths each skill doc references -> set of doc names.

    Strips query strings and trailing punctuation; skips glob/wildcard (`*`) and
    path-param (`{...}`) forms that aren't a single concrete endpoint.
    """
    refs: dict[str, set[str]] = {}
    for md in sorted(SKILLS_DIR.glob("*.md")):
        text = md.read_text()
        for raw in re.findall(r"/api/comics/[^\s)`\"'>]*", text):
            path = raw.split("?")[0]
            # Skip glob/wildcard summaries and path-param forms BEFORE stripping
            # punctuation (else a trailing `*` gets silently removed first).
            if "*" in path or "{" in path:
                continue
            path = path.rstrip("/.,;:")
            if path:
                refs.setdefault(path, set()).add(md.name)
    return refs


def test_every_skill_referenced_endpoint_is_registered():
    """Endpoint-existence contract: a skill cannot reference an /api/comics/*
    path the overlay doesn't serve. Catches a route rename/removal that would
    silently 404 a skill (the BUI-150-class drift, generalised)."""
    registered = _registered_comics_paths()
    referenced = _referenced_comics_paths()

    missing = {
        path: docs for path, docs in referenced.items() if path not in registered
    }
    assert not missing, (
        "skill(s) reference /api/comics endpoints the overlay does not register: "
        + "; ".join(f"{p} (in {sorted(d)})" for p, d in sorted(missing.items()))
    )


def test_harness_actually_found_endpoints():
    """Guard the guard: if the extraction silently matched nothing, the contract
    test above would pass vacuously. Pin a known seam so a broken regex fails."""
    referenced = _referenced_comics_paths()
    assert "/api/comics/collection/check" in referenced, (
        "contract harness extracted no endpoint refs — extraction is broken"
    )


def test_identify_documents_grade_from_description():
    """BUI-148: ebay_fetch.py emits a third grade signal, grade_from_description
    (a grade found only in the listing body), but identify.md's field table
    omitted it — so the skill mislabelled description-graded listings as having
    no grade anywhere. Assert the field the script emits is documented, and the
    skill keys its strong-warning rule on it (grade_from_description null too)."""
    fetch_src = (REPO_ROOT / "apps" / "ebay" / "src" / "ebay_fetch.py").read_text()
    assert '"grade_from_description"' in fetch_src, (
        "ebay_fetch.py no longer emits grade_from_description — update this contract"
    )
    identify = (SKILLS_DIR / "identify.md").read_text()
    assert "grade_from_description" in identify, (
        "identify.md must document grade_from_description (the script emits it)"
    )


def test_endpoint_names_are_provider_neutral():
    """CLAUDE.md invariant: comics endpoints are provider-neutral — never
    /api/comics/locg/*. A drift here would leak the provider into the URL the
    skills hard-code."""
    leaky = {p for p in _registered_comics_paths() if "/locg" in p}
    assert not leaky, f"non-neutral comics endpoint(s) registered: {sorted(leaky)}"
