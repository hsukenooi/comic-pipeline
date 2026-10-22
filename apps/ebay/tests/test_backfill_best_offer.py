"""Tests for backfill_best_offer.py (BUI-1280). HTTP is faked."""
from __future__ import annotations

import gzip
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import backfill_best_offer as bb


def _write_capture(d: Path) -> None:
    rec = {"response": {"items": [
        {"itemId": "1", "bestOfferAccepted": True},
        {"itemId": "2", "bestOfferAccepted": False},
        {"itemId": "9", "bestOfferAccepted": True},
        {"itemId": "3"},  # no flag: ignored
    ]}}
    with gzip.open(d / "raw_responses.a.jsonl.gz", "wt") as fh:
        fh.write(json.dumps(rec) + "\nnot json\n")
    with open(d / "raw_responses.b.jsonl", "w") as fh:
        fh.write(json.dumps({"response": {"items": [
            {"itemId": "4", "bestOfferAccepted": False}]}}) + "\n")


def test_load_capture_flags_reads_gz_and_plain(tmp_path):
    _write_capture(tmp_path)
    assert bb.load_capture_flags(tmp_path) == {
        "1": True, "2": False, "9": True, "4": False}


ROWS = [
    {"id": 10, "pool": "slab", "product_id": "1", "best_offer": None},
    {"id": 11, "pool": "slab", "product_id": "2", "best_offer": None},
    {"id": 12, "pool": "slab", "product_id": "1", "best_offer": 0},  # stored
    {"id": 13, "pool": "raw", "product_id": "4", "best_offer": None},
    {"id": 14, "pool": "raw", "product_id": "77", "best_offer": None},
]
FLAGS = {"1": True, "2": False, "9": True, "4": False}


def test_plan_counts_per_pool_and_never_touches_stored():
    items, counts, not_in_ledger = bb.plan(ROWS, FLAGS)
    assert {i["id"]: i["new"] for i in items} == {10: 1, 11: 0, 13: 0}
    assert all(i["expected"] is None for i in items)  # fill-only
    assert counts["slab"]["would_set_true"] == 1
    assert counts["slab"]["would_set_false"] == 1
    assert counts["slab"]["already_set"] == 1
    assert counts["raw"]["no_capture_match"] == 1
    assert not_in_ledger == 1  # "9"


def test_plan_pool_filter():
    items, counts, _ = bb.plan(ROWS, FLAGS, pool="slab")
    assert set(counts) == {"slab"} and len(items) == 2


def _run(argv, tmp_path):
    _write_capture(tmp_path)
    post = MagicMock()
    post.return_value.json.return_value = {
        "matched": 3, "changed": 3, "skipped_stale": 0, "not_found": 0}
    with patch.object(bb, "_server_base", return_value="http://x"), \
            patch.object(bb, "_health_gate"), \
            patch.object(bb, "_iter_all_comps", return_value=iter(ROWS)), \
            patch.object(bb.requests, "post", post):
        bb.main(["--capture-dir", str(tmp_path), *argv])
    return post


def test_default_is_dry_run(tmp_path):
    post = _run([], tmp_path)
    body = post.call_args.kwargs["json"]
    assert body["dry_run"] is True
    assert all("_pool" not in i for i in body["items"])


def test_apply_posts_dry_run_false(tmp_path):
    post = _run(["--apply"], tmp_path)
    assert post.call_args.kwargs["json"]["dry_run"] is False
