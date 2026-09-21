"""Canary for the apps/fmv -> overlay `fmv_flag_reason` contract (BUI-593).

`comic-fmv` (apps/fmv) is the sole producer of `fmv_flag_reason`; this plugin
validates it at `POST /api/comics`. The two are joined by HTTP, not by an
import — apps/fmv is not a workspace member — so nothing at build time can
notice when the producer learns a new reason the validator still rejects.

The failure mode is not graceful: a rejected value 422s and the SERVER
DISCARDS THE WHOLE UPSERT, so a book with a real, expensively-fetched comp
pool is written nowhere. BUI-588 shipped exactly that by adding
`variant_dropped` on the producer side (and to every doc) while leaving this
side untouched, which made every variant-dropped book unpriceable.

This test reads fmv_runner's source rather than importing it (again: no import
edge) and asserts the validator accepts everything the producer can emit.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from gixen_overlay.models import FMV_FLAG_REASONS, UpsertComicRequest


def _fmv_runner_source() -> str:
    """The producer's source, or skip if apps/fmv isn't checked out beside us.

    Located relative to this file so the test works from any CWD. apps/fmv is
    not installed into this environment, so there is no importable path to it.
    """
    repo_root = Path(__file__).resolve().parents[3]
    runner = repo_root / "apps" / "fmv" / "src" / "fmv_runner.py"
    if not runner.is_file():
        pytest.skip(f"apps/fmv not present at {runner}; cross-package canary skipped")
    return runner.read_text(encoding="utf-8")


# BUI-925: fmv_runner emits a flag reason two ways, and scanning only the
# first left a blind spot. `forced_flag_reason="..."` is the pooled-pricing
# route; a `"flag_reason": "..."` dict literal is the SHORT-CIRCUIT route,
# which BUI-928 introduced for `graded_mode_unavailable` — a book refused
# before any lookup, fetch, or upsert. That one does not reach the server
# today, which is exactly why it needs the canary rather than exempting it:
# the moment any path posts it, an unlisted reason 422s the whole upsert.
_EMISSION_PATTERNS = (
    r'forced_flag_reason\s*=\s*["\']([a-z_]+)["\']',
    r'"flag_reason"\s*:\s*"([a-z_]+)"',
)


def _emitted_reasons(source: str) -> set[str]:
    reasons: set[str] = set()
    for pattern in _EMISSION_PATTERNS:
        reasons |= set(re.findall(pattern, source))
    return reasons


def test_each_emission_pattern_still_matches_something():
    """A canary whose extractor silently stopped matching passes forever.

    Both producer shapes must still be found in the real source — if a
    refactor moves one, this fails loudly instead of quietly covering less.
    """
    source = _fmv_runner_source()
    for pattern in _EMISSION_PATTERNS:
        assert re.findall(pattern, source), (
            f"pattern {pattern!r} matched nothing in fmv_runner — the "
            f"producer moved, and this canary is now covering less than it "
            f"claims"
        )


def test_validator_accepts_every_reason_fmv_runner_can_emit():
    """Every flag reason literal in fmv_runner must validate.

    If this fails, a `comic-fmv` run will 422 and silently drop a priced book.
    Fix by adding the new reason to `FMV_FLAG_REASONS` in the SAME commit that
    introduces it upstream.
    """
    emitted = _emitted_reasons(_fmv_runner_source())
    assert emitted, "found no flag reason literals — did the producer move?"

    missing = sorted(emitted - set(FMV_FLAG_REASONS))
    assert not missing, (
        f"fmv_runner emits {missing} but the validator rejects it. Every such "
        f"POST /api/comics returns 422 and the entire upsert is discarded, so "
        f"the book is priced nowhere. Add it to FMV_FLAG_REASONS."
    )


@pytest.mark.parametrize("reason", FMV_FLAG_REASONS)
def test_each_declared_reason_round_trips(reason: str):
    """The declared vocabulary must actually pass its own validator."""
    assert UpsertComicRequest(
        title="X-Men", issue="1", fmv_flag_reason=reason
    ).fmv_flag_reason == reason


def test_variant_dropped_is_accepted():
    """BUI-593 regression test, named explicitly.

    BUI-588 added this reason to route variant-dropped books into the
    needs-manual channel; the validator rejected it, so the mark meant to
    surface those books is what blocked them from being written at all.
    """
    req = UpsertComicRequest(
        title="Uncanny X-Men", issue="281", fmv_flag_reason="variant_dropped"
    )
    assert req.fmv_flag_reason == "variant_dropped"


def test_unknown_reason_still_rejected():
    """Widening the allow-list must not turn the field into a free-text column."""
    with pytest.raises(ValueError, match="fmv_flag_reason must be one of"):
        UpsertComicRequest(title="X-Men", issue="1", fmv_flag_reason="bogus_reason")


def test_graded_mode_unavailable_is_accepted():
    """BUI-925/928 regression test, named explicitly.

    `comic-fmv` short-circuits every certified book to this reason until the
    graded pricing mode ships, so a slab is never priced off the raw market
    in the interim. It reaches no upsert TODAY, which is precisely the
    condition under which a missing vocabulary entry goes unnoticed until the
    first path that does post it 422s the whole write.
    """
    req = UpsertComicRequest(
        title="Amazing Spider-Man", issue="50",
        fmv_flag_reason="graded_mode_unavailable",
    )
    assert req.fmv_flag_reason == "graded_mode_unavailable"


def test_graded_refusal_reasons_are_all_in_the_vocabulary():
    """The nine refusals the graded pricing mode can return (plan U3/U7)."""
    expected = {
        "label_signature_series", "label_qualified", "label_restored",
        "label_conserved", "certifier_other", "no_certifier_pool",
        "ladder_too_thin", "ladder_non_monotone", "outside_ladder",
    }
    assert expected <= set(FMV_FLAG_REASONS)
