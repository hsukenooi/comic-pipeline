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
from datetime import date
from pathlib import Path

import pytest

import fmv_math
import fmv_runner

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


# The day the 2026-09-21 `/comic:buy` run these fixtures were captured from
# actually priced them. BUI-948 made the age reference an explicit calendar
# date, so a replay has to name the replayed day or it would re-age with the
# wall clock and stop being a replay. Every comp in both fixtures is within 90
# days of it (the oldest, Batman's 2026-06-23, is exactly 90), so all of them
# still carry full weight and both replays assert the same numbers they did
# before as_of existed.
_RUN_DATE = date(2026, 9, 21)


def _price(comps, target, *, page_quality):
    return fmv_math.graded_fmv(list(comps), target["grade"],
                               certifier=target["certifier"],
                               label=target["label"], page_quality=page_quality,
                               as_of=_RUN_DATE)


def _weighted(comps):
    return fmv_math.graded_pool(comps, as_of=_RUN_DATE)[0]


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
            _weighted(data["comps"]))
        assert ladder_before[4.0] > ladder_before[6.0]
        ladder_after = fmv_math.bucket_weighted_medians(
            _weighted(_guarded(data)))
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
            _weighted(data["comps"]))
        assert before[9.6] < before[9.4]
        after = fmv_math.bucket_weighted_medians(
            _weighted(_guarded(data)))
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

    def test_page_quality_scoping_widens_and_the_guards_then_price(self):
        # BUI-939 and this ticket, kept apart: the target reads page_quality
        # "white", and `_graded_page_quality_filter` cuts the server pool to
        # the 2 comps whose own titles say "white". Post-BUI-939 that scoped
        # pool is starved for the ladder and widens back to the whole pool
        # (`ladder_starved`), so the outcome is whatever the WHOLE pool says:
        # unguarded it still inverts (BUI-939 alone does not price this book),
        # guarded it prices — the same $3,650 as with no page quality at all.
        data = _pool(self.SERVER)
        target = data["target"]
        assert target["page_quality"] == "white"
        before = _price(data["comps"], target, page_quality="white")
        assert before["page_quality_fallback_reason"] == "ladder_starved"
        assert before["flag_reason"] == "ladder_non_monotone"
        after = _price(_guarded(data), target, page_quality="white")
        assert after["page_quality_fallback_reason"] == "ladder_starved"
        assert after["flag_reason"] is None
        assert after["fmv_high"] == 3650

def _dropped_ids(data: dict) -> set[str]:
    """The product_ids the BUI-922/938 guards excluded from `data["comps"]`
    — everything the live fetch saw that `_guarded` did not keep."""
    kept_ids = {c["product_id"] for c in _guarded(data)}
    return {c["product_id"] for c in data["comps"]} - kept_ids


class TestLedgerHonoursGuards:
    """BUI-946 — the Done-when: merge(live-guarded, ledger-unguarded,
    dropped_ids) prices both books.

    The ticket's defect, reproduced here rather than assumed: BUI-922/938
    fixed the LIVE fetch (`TestBatman227Replay`/`TestInvincible1Replay`
    above already pin that a guarded-only pool prices), but
    `_merge_slab_pool` re-admits the SAME excluded listings the moment they
    also exist as `pool='slab'` ledger rows from an earlier, pre-guard
    fetch — which is exactly what these two fixtures' `data["comps"]`
    represent when passed as the LEDGER side unfiltered. Both books
    refused in production after PR #523 deployed for precisely this
    reason.
    """

    @pytest.mark.parametrize("name", [
        "graded_pool_bui922_batman227.json",
        "graded_pool_bui938_invincible1_server.json",
    ])
    def test_merge_without_dropped_ids_still_refuses(self, name):
        # Pins the DEFECT: the live fetch is guarded (only the surviving
        # comps reach `live`), but the ledger is the unguarded pre-guard
        # snapshot — an old `_merge_slab_pool(live, ledger)` call with no
        # `dropped_ids` re-admits the excluded listings from the ledger side
        # and reproduces the exact production refusal.
        data = _pool(name)
        target = data["target"]
        pool = fmv_runner._merge_slab_pool(_guarded(data), data["comps"])
        priced = _price(pool, target, page_quality=None)
        assert priced["flag_reason"] == "ladder_non_monotone"
        assert priced["fmv_high"] is None

    @pytest.mark.parametrize("name,expected_fmv_high", [
        ("graded_pool_bui922_batman227.json", 775),
        ("graded_pool_bui938_invincible1_server.json", 3650),
    ])
    def test_merge_with_dropped_ids_prices(self, name, expected_fmv_high):
        # The FIX: passing dropped_ids (the same product_ids
        # graded_identity_dropped_ids would report for this fetch) makes the
        # merge skip those same listings on the ledger side too, so the
        # result matches the guarded-only pool's already-pinned price.
        data = _pool(name)
        target = data["target"]
        dropped_ids = _dropped_ids(data)
        pool = fmv_runner._merge_slab_pool(
            _guarded(data), data["comps"], dropped_ids=dropped_ids)
        priced = _price(pool, target, page_quality=None)
        assert priced["flag_reason"] is None
        assert priced["fmv_high"] == expected_fmv_high
        # The honoured merge must equal the guarded-only pool exactly — the
        # ledger contributes nothing this run wasn't already going to keep.
        guarded_only = _price(_guarded(data), target, page_quality=None)
        assert priced["pool_n"] == guarded_only["pool_n"]


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
