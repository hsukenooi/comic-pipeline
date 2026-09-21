"""BUI-922/938 — replay: the saved graded pools, priced with the guards on.

Both tickets' Done-when is a REPLAY, not a unit assertion: take the pool the
2026-09-21 `/comic:buy` run actually saw, apply the new graded-only exclusion
guards, and show the refusal turn into a price. That spans the package
boundary — the guards live in `apps/ebay/src/sold_comps.py`, the ladder math
in `apps/fmv/src/fmv_math.py` — so this file imports the LIVE guard by path,
exactly the way `scripts/backfill_comps_ledger.py` imports the live parser
(see its module docstring's import-boundary section for why a path load and
not a vendored copy; a copy of an exclusion rule drifts, and a replay proved
against a stale copy proves nothing). The load fails loudly if the source
tree is not there; it never falls back.

The fixtures are apps/ebay's own comp data, so they live beside apps/ebay's
tests and are read from there rather than duplicated here.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

import fmv_math

_REPO = Path(__file__).resolve().parents[3]
_EBAY_SRC = _REPO / "apps" / "ebay" / "src"
_FIXTURES = _REPO / "apps" / "ebay" / "tests" / "fixtures"


def _load_live_sold_comps():
    target = _EBAY_SRC / "sold_comps.py"
    if not target.is_file():
        raise SystemExit(
            f"test_graded_pool_guards: cannot find the live guard at {target}. "
            "This replay must run against apps/ebay's real exclusion rules, "
            "never a copy."
        )
    sys.path.insert(0, str(_EBAY_SRC))
    spec = importlib.util.spec_from_file_location("sold_comps", target)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


sc = _load_live_sold_comps()


def _pool(name: str) -> dict:
    return json.loads((_FIXTURES / name).read_text())


def _guarded(data: dict) -> list[dict]:
    """`data["comps"]` with the BUI-922/938 graded guards applied — the same
    two calls, in the same order, `fetch_book_comps` makes per comp."""
    target = data["target"]
    kept = []
    for comp in data["comps"]:
        if sc.hard_exclude(comp["title"], graded_target=target["certifier"]):
            continue
        if sc.graded_identity_exclude(comp["title"], issue=target["issue"],
                                      target_is_variant=bool(target.get("variant"))):
            continue
        kept.append(comp)
    return kept


def _price(comps, target, *, page_quality):
    return fmv_math.graded_fmv(list(comps), target["grade"],
                               certifier=target["certifier"],
                               label=target["label"], page_quality=page_quality)


class TestBatman227Replay:
    """BUI-922: the ampersand lot + the cross-title comp, both excluded."""

    NAME = "graded_pool_bui922_batman227.json"

    def test_refusal_becomes_a_price(self):
        data = _pool(self.NAME)
        target = data["target"]
        assert target["grade"] == 4.5 and target["certifier"] == "cgc"

        before = _price(data["comps"], target, page_quality=None)
        # The refusal the 2026-09-21 run recorded for this exact pool.
        assert before["flag_reason"] == "ladder_non_monotone"
        assert before["fmv_high"] is None

        after = _price(_guarded(data), target, page_quality=None)
        assert after["flag_reason"] is None
        assert after["fmv_high"] == 775
        assert after["fmv_low"] == 775 and after["median"] == 775
        assert after["pool_n"] == len(data["comps"]) - 2

    def test_the_bogus_4_0_rung_no_longer_outranks_the_genuine_6_0(self):
        # The mechanism, not just the outcome: the $1,399.99 "4.0 & 6.0" lot
        # was the whole 4.0 rung, sitting above the genuine 5.5 ($899) and
        # 6.0 ($900) sales.
        data = _pool(self.NAME)
        ladder_before = fmv_math.bucket_weighted_medians(
            fmv_math.graded_pool(data["comps"])[0])
        assert ladder_before[4.0] > ladder_before[6.0]
        ladder_after = fmv_math.bucket_weighted_medians(
            fmv_math.graded_pool(_guarded(data))[0])
        assert 4.0 not in ladder_after
        assert ladder_after[5.5] < ladder_after[6.0] < ladder_after[6.5]


class TestInvincible1Replay:
    """BUI-938: the Larry's Comics store variants, excluded."""

    LIVE = "graded_pool_bui938_invincible1_live.json"
    SERVER = "graded_pool_bui938_invincible1_server.json"

    def test_server_held_pool_prices_once_the_store_variants_are_out(self):
        data = _pool(self.SERVER)
        target = data["target"]
        assert target["grade"] == 9.4 and target["certifier"] == "cgc"

        before = _price(data["comps"], target, page_quality=None)
        assert before["flag_reason"] == "ladder_non_monotone"
        assert before["fmv_high"] is None

        after = _price(_guarded(data), target, page_quality=None)
        assert after["flag_reason"] is None
        assert after["fmv_high"] == 3650
        assert after["pool_n"] == len(data["comps"]) - 5

    def test_the_target_s_own_neighbour_rung_stops_inverting(self):
        # The mechanism, not just the outcome. A $750 Larry's sale is one of
        # only two comps in the 9.6 rung (the other is a $4,500 first print),
        # which drags 9.6 to $2,625 — BELOW the $3,609 at 9.4. Those are
        # exactly the two rungs a 9.4 target interpolates between, which is
        # what `ladder_non_monotone` was reporting.
        data = _pool(self.SERVER)
        before = fmv_math.bucket_weighted_medians(
            fmv_math.graded_pool(data["comps"])[0])
        assert before[9.6] < before[9.4]
        after = fmv_math.bucket_weighted_medians(
            fmv_math.graded_pool(_guarded(data))[0])
        grades = sorted(after)
        assert all(after[a] < after[b] for a, b in zip(grades, grades[1:]))

    def test_saved_live_slice_alone_stays_thin_and_still_refuses(self):
        # Honest bound on what this ticket fixes: the 9 LIVE comps the run
        # saved are 4 store variants plus 5 first prints across 3 rungs, and
        # 3 rungs minus the dropped target rung is 2 anchors — under the
        # ladder's minimum either way. The guard is necessary, not sufficient;
        # the server-held pool above is what the fix actually prices.
        data = _pool(self.LIVE)
        target = data["target"]
        after = _price(_guarded(data), target, page_quality=None)
        assert after["flag_reason"] == "ladder_too_thin"

    def test_page_quality_scoping_still_refuses_both_ways(self):
        # BUI-939, NOT this ticket: the target reads page_quality "white", and
        # `_graded_page_quality_filter` cuts the server pool to the 2 comps
        # whose own titles happen to say "white". That refusal is unchanged by
        # the guards, and pinning it here keeps the two tickets' effects apart.
        data = _pool(self.SERVER)
        target = data["target"]
        assert target["page_quality"] == "white"
        for comps in (data["comps"], _guarded(data)):
            out = _price(comps, target, page_quality=target["page_quality"])
            assert out["flag_reason"] == "ladder_too_thin"


class TestGuardsAreGradedOnly:
    """The raw path must be able to see everything it saw before."""

    @pytest.mark.parametrize("name", [
        "graded_pool_bui922_batman227.json",
        "graded_pool_bui938_invincible1_server.json",
    ])
    def test_no_comp_in_either_pool_is_newly_excluded_from_a_raw_pool(self, name):
        for comp in _pool(name)["comps"]:
            title = comp["title"]
            if sc.comic_identity.is_comp_excluded(title):
                continue  # already excluded on both paths, before this ticket
            assert sc.hard_exclude(title) == bool(
                sc.LOCAL_EXCLUDE_RE.search(title)), title
