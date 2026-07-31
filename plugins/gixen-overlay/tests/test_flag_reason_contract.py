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


def test_validator_accepts_every_reason_fmv_runner_can_emit():
    """Every `forced_flag_reason="..."` literal in fmv_runner must validate.

    If this fails, a `comic-fmv` run will 422 and silently drop a priced book.
    Fix by adding the new reason to `FMV_FLAG_REASONS` in the SAME commit that
    introduces it upstream.
    """
    emitted = set(re.findall(
        r'forced_flag_reason\s*=\s*["\']([a-z_]+)["\']', _fmv_runner_source()
    ))
    assert emitted, "found no forced_flag_reason literals — did the producer move?"

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
