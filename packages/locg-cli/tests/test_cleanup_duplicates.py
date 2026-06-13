"""Tests for the one-time BUI-125 duplicate-cleanup script.

The script lives in ``scripts/`` (not the ``locg`` package), so it's added to
``sys.path`` here. It reuses locg's own identity/normalization helpers, so these
tests double as a guard that the cleanup's matching stays aligned with the live
dedup logic.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import cleanup_duplicates as cd  # noqa: E402


def _row(full_title, *, source, pushed=None, seq=1, in_collection=1, publisher=None, release_date=None, series=None):
    return {
        "full_title": full_title,
        "series_name": series or full_title.split(" #")[0],
        "publisher_name": publisher,
        "release_date": release_date,
        "in_collection": in_collection,
        "source": source,
        "pushed_to_locg_at": pushed,
        "local_added_seq": seq,
    }


def test_exact_identity_dup_keeps_established():
    comics = [
        _row("Uncanny X-Men #210", source="locg_export", pushed="2026-01-01", seq=3, publisher="", release_date="1986-10-01"),
        _row("Uncanny X-Men #210", source="agent_win", seq=7, publisher="", release_date="1986-10-01"),
    ]
    drops = cd.find_exact_identity_dups(comics)
    assert len(drops) == 1
    drop_i, dropped, keep_i = drops[0]
    assert dropped["source"] == "agent_win"          # the redundant twin
    assert comics[keep_i]["source"] == "locg_export"  # established row kept


def test_exact_identity_dup_keeps_earliest_when_neither_established():
    # Both pending wins (the real Thor #137 / Uncanny #210 case in the store).
    comics = [
        _row("Thor #137", source="agent_win", seq=2, publisher="", release_date="1967-02-01"),
        _row("Thor #137", source="agent_win", seq=9, publisher="", release_date="1967-02-01"),
    ]
    drops = cd.find_exact_identity_dups(comics)
    assert len(drops) == 1
    _, dropped, keep_i = drops[0]
    assert dropped["local_added_seq"] == 9       # later add dropped
    assert comics[keep_i]["local_added_seq"] == 2  # earliest kept


def test_pending_win_owned_by_established_is_dropped_incl_leading_article():
    comics = [
        _row("The Incredible Hulk #181", source="locg_export", pushed="2026-01-01", seq=1, publisher="Marvel"),
        _row("Incredible Hulk #181", source="agent_win", seq=5),  # blank pub, article dropped
        _row("Daredevil #1", source="agent_win", seq=6),          # genuine, not owned
    ]
    drops = cd.find_owned_pending_wins(comics, exclude=set())
    assert len(drops) == 1
    _, dropped, owner = drops[0]
    assert dropped["full_title"] == "Incredible Hulk #181"
    assert owner["source"] == "locg_export"


def test_pending_win_not_dropped_when_owner_is_only_another_pending_win():
    # Two pending wins for the same book but no established owner -> not class 1
    # (one is removed by class 2 / exact-identity instead).
    comics = [
        _row("Thor #137", source="agent_win", seq=2, publisher="", release_date="1967-02-01"),
        _row("Thor #137", source="agent_win", seq=9, publisher="", release_date="1967-02-01"),
    ]
    assert cd.find_owned_pending_wins(comics, exclude=set()) == []


def test_owned_wish_adds_only_local_and_owned():
    owned = {cd._normalize_title("Thor #137")}
    items = [
        {"name": "Thor #137", "id": None},                                  # local + owned -> drop
        {"name": "Amazing Spider-Man #300", "id": None},                    # local, not owned -> keep
        {"name": "Uncanny X-Men #210", "series_name": "Uncanny X-Men"},     # derived -> keep
    ]
    drops = cd.find_owned_wish_adds(items, owned)
    assert [i for i, _ in drops] == [0]


def test_main_apply_drops_and_backs_up(tmp_path: Path, capsys):
    comics = [
        _row("Thor #137", source="agent_win", seq=2, publisher="", release_date="1967-02-01"),
        _row("Thor #137", source="agent_win", seq=9, publisher="", release_date="1967-02-01"),  # exact twin
        _row("Daredevil #1", source="agent_win", seq=6),  # genuine pending win survives
    ]
    wish = [
        {"name": "Thor #137", "id": None},               # owned -> drop
        {"name": "New Mutants #98", "id": None},          # keep
    ]
    (tmp_path / "collection.json").write_text(json.dumps({"comics": comics}))
    (tmp_path / "wish-list.json").write_text(json.dumps({"items": wish}))

    rc = cd.main(["--store-dir", str(tmp_path), "--apply"])
    assert rc == 0

    out_comics = json.loads((tmp_path / "collection.json").read_text())["comics"]
    titles = sorted(r["full_title"] for r in out_comics)
    assert titles == ["Daredevil #1", "Thor #137"]  # one Thor twin dropped, genuine win kept

    out_wish = [it["name"] for it in json.loads((tmp_path / "wish-list.json").read_text())["items"]]
    assert out_wish == ["New Mutants #98"]

    backups = list(tmp_path.glob("*.dedup-bak.*"))
    assert {b.name.split(".dedup-bak.")[0] for b in backups} == {"collection.json", "wish-list.json"}


def test_main_dry_run_changes_nothing(tmp_path: Path):
    comics = [
        _row("Thor #137", source="agent_win", seq=2, publisher="", release_date="1967-02-01"),
        _row("Thor #137", source="agent_win", seq=9, publisher="", release_date="1967-02-01"),
    ]
    (tmp_path / "collection.json").write_text(json.dumps({"comics": comics}))
    before = (tmp_path / "collection.json").read_text()

    rc = cd.main(["--store-dir", str(tmp_path)])  # no --apply
    assert rc == 0
    assert (tmp_path / "collection.json").read_text() == before
    assert not list(tmp_path.glob("*.dedup-bak.*"))
