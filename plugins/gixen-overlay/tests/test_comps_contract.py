"""Canary for the apps/fmv -> overlay comps-ledger contract (BUI-673).

Sibling of `test_flag_reason_contract.py`, for the same producer/validator
pair and the same reason: `comic-fmv` (apps/fmv) is the sole producer of
`POST /api/comics/comps` payloads, this plugin validates them, and the two are
joined by HTTP rather than by an import — apps/fmv is not a workspace member,
so nothing at build time can notice when the producer and the validator
disagree.

BUI-673 is what that costs. BUI-658 forwarded `observed_at` verbatim from
BUI-657's stamp, which is a raw epoch **float**; `CompItem.observed_at` is
`str | None` and pydantic v2 does not coerce float to str, so every post 422'd
and the server discarded every batch. Thirty tests passed on the producer side
because they mocked `_post_json` — the payload never reached this model — and
the fixture they mocked with used an ISO string the producer never emits.

The lesson those tests encode: a mock at the contract boundary cannot test the
contract. So these assert against the real model, from the validator's side.
"""
from __future__ import annotations

import ast
import time
from pathlib import Path

import pytest
from pydantic import ValidationError

from gixen_overlay.models import CompItem


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


def _comp_ledger_fields() -> set[str]:
    """`_COMP_LEDGER_FIELDS` from fmv_runner, read via AST rather than regex.

    It is a module-level tuple of string literals; parsing it structurally
    means a reformat (line wrapping, trailing comma) can't silently turn this
    canary into a no-op the way a brittle pattern match would.
    """
    for node in ast.parse(_fmv_runner_source()).body:
        if not isinstance(node, ast.Assign):
            continue
        targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
        if "_COMP_LEDGER_FIELDS" not in targets:
            continue
        if not isinstance(node.value, ast.Tuple):
            pytest.fail("_COMP_LEDGER_FIELDS is no longer a tuple literal")
        return {
            e.value for e in node.value.elts
            if isinstance(e, ast.Constant) and isinstance(e.value, str)
        }
    pytest.fail("_COMP_LEDGER_FIELDS not found in fmv_runner — did it move?")


# Set by `_comp_to_ledger_item` itself rather than projected off the comp,
# because they depend on which list a comp came from, not on the comp.
_SET_BY_PRODUCER = {"pool", "provenance"}


def test_producer_projects_only_fields_this_model_declares():
    """An unknown key is not itself fatal — pydantic ignores extras — but it
    means the producer believes it is sending something this model will store,
    and it isn't. That silent drop is the BUI-588 shape."""
    unknown = sorted(_comp_ledger_fields() - set(CompItem.model_fields))
    assert not unknown, (
        f"fmv_runner projects {unknown}, which CompItem does not declare. "
        f"Those values are silently dropped on ingest — the comp lands in the "
        f"ledger missing data the producer thinks it recorded."
    )


def test_producer_covers_every_required_field():
    """A missing required field 422s the WHOLE batch, not just one comp."""
    required = {
        name for name, f in CompItem.model_fields.items() if f.is_required()
    }
    covered = _comp_ledger_fields() | _SET_BY_PRODUCER
    missing = sorted(required - covered)
    assert not missing, (
        f"CompItem requires {missing} but fmv_runner neither projects nor sets "
        f"them. Every POST /api/comics/comps would 422 and the server would "
        f"discard the entire batch."
    )


def test_observed_at_rejects_a_raw_epoch_stamp():
    """BUI-673's actual bug, pinned from this side.

    If someone 'fixes' a future recurrence by widening this field to
    `float | str | None`, that will make the 422 go away and leave the column
    holding two encodings that neither sort nor parse as a unit — it sits
    under `idx_comps_observed`, beside ISO `first_seen_at`/`last_seen_at`, and
    BUI-661's backfill writes ISO. Fix the producer, not this model.
    """
    with pytest.raises(ValidationError):
        CompItem(
            pool="raw", provider="serpapi", product_id="x",
            provenance="live", observed_at=time.time(),
        )


def test_observed_at_accepts_the_iso_string_the_producer_now_sends():
    item = CompItem(
        pool="raw", provider="serpapi", product_id="x",
        provenance="live", observed_at="2026-08-01T12:30:00+00:00",
    )
    assert item.observed_at == "2026-08-01T12:30:00+00:00"
