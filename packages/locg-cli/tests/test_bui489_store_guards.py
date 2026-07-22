"""Regression tests for BUI-489: extend the BUI-476/BUI-471 wrong-store
guard (`_needs_explicit_store`) to the remaining collection mutators
(`remediate-delete`, `remediate-set-copies`, the wish-list writers), and give
`cmd_collection_export` a distinct not-imported signal instead of a silent
zero-row "success".

Same wrong-store trap as BUI-476/BUI-471: a mutating command that falls back
bare to `locg.config._cache_dir()`'s resolution (`LOCG_DATA_DIR` env ->
`<repo>/data/locg` -> `~/.cache/locg`) can land on a store DIFFERENT from the
server-owned one. An empty local store hard-fails loudly (R11's not_imported
gate); a NON-empty one silently mutates the wrong data. BUI-424 flagged
`remediate-delete` specifically as the highest-risk case in this module —
alias/cross-volume ambiguity makes a wrong-store delete indistinguishable
from a correct one without an independent check.

Every "refused" test below unsets LOCG_DATA_DIR and installs the matching
`_no_default_*_store` stub: the autouse `_isolate_cache_dir` fixture
(conftest.py) is what normally keeps a test off the REAL repo store, and
`delenv` removes exactly that protection — if a guard ever regresses, the
resolver falls through to `<repo>/data/locg` and the write would land there
BEFORE the assertion on the refusal gets a chance to run. The stub turns
that into a loud `AssertionError` instead.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from locg.collection_cache import CollectionCache


def make_cache(tmp_path: Path) -> CollectionCache:
    return CollectionCache(
        path=tmp_path / "collection.json",
        lock_path=tmp_path / "collection.lock",
        audit_path=tmp_path / "import-history.jsonl",
    )


def _row(
    full_title: str = "Amazing Spider-Man #300",
    gixen_item_id: str = "99",
    in_collection: int = 1,
) -> dict[str, Any]:
    return {
        "publisher_name": "Marvel Comics",
        "series_name": "Amazing Spider-Man",
        "full_title": full_title,
        "release_date": "1988-05-01",
        "in_collection": in_collection,
        "in_wish_list": 0,
        "marked_read": 0,
        "my_rating": None,
        "media_format": None,
        "price_paid": None,
        "date_purchased": None,
        "condition": None,
        "notes": None,
        "tags": None,
        "storage_box": None,
        "owner": None,
        "purchase_store": None,
        "signature": 0,
        "slabbing": 0,
        "grading": None,
        "grading_company": None,
        "local_added_at": None,
        "local_added_seq": None,
        "pushed_to_locg_at": None,
        "last_seen_in_export_at": None,
        "source": "agent_win",
        "needs_manual_variant": False,
        "needs_manual_series_canonical": False,
        "metron_id": None,
        "gixen_item_id": gixen_item_id,
        "previous_full_title": None,
    }


def _seed(cache: CollectionCache, rows: list[dict[str, Any]], imported: bool = True) -> None:
    def mutate(payload: dict[str, Any]) -> None:
        payload["comics"] = list(rows)
        if imported:
            payload["last_full_import"] = "2026-06-01T00:00:00.000000Z"
            payload["last_import_source"] = "seed.xlsx"

    cache.apply(mutate, command="seed")


def _no_default_collection_store(monkeypatch):
    """Make constructing the DEFAULT CollectionCache a hard failure — mirrors
    test_collection_commands.py's `_no_default_store`."""

    def _boom(*args, **kwargs):
        raise AssertionError("BUI-489 guard regressed: the default store was constructed")

    monkeypatch.setattr("locg.commands.CollectionCache", _boom)
    monkeypatch.setattr("locg.collection_cache.CollectionCache", _boom)


def _no_default_wish_list_store(monkeypatch):
    """Wish-list writers don't go through CollectionCache — they resolve
    `wish_list_cache_path()` directly — so they need their OWN stub."""

    def _boom(*args, **kwargs):
        raise AssertionError("BUI-489 guard regressed: the default wish-list path was resolved")

    monkeypatch.setattr("locg.commands.wish_list_cache_path", _boom)


# ---------------------------------------------------------------------------
# cmd_collection_remediate_delete
# ---------------------------------------------------------------------------

def test_remediate_delete_no_cache_no_env_is_refused(monkeypatch):
    from locg.commands import cmd_collection_remediate_delete

    monkeypatch.delenv("LOCG_DATA_DIR", raising=False)
    _no_default_collection_store(monkeypatch)

    result = cmd_collection_remediate_delete(gixen_item_id="99")

    assert result["status"] == "explicit_store_required"
    assert "LOCG_DATA_DIR" in result["error"]


def test_remediate_delete_guard_fires_even_for_dry_run(monkeypatch):
    """The refusal must come before `dry_run` is even consulted — a preview
    against the wrong store is exactly as misleading as a real write, since
    it confirms a delete that, run for real, could land on a different
    collection entirely."""
    from locg.commands import cmd_collection_remediate_delete

    monkeypatch.delenv("LOCG_DATA_DIR", raising=False)
    _no_default_collection_store(monkeypatch)

    result = cmd_collection_remediate_delete(gixen_item_id="99", dry_run=True)

    assert result["status"] == "explicit_store_required"


def test_remediate_delete_explicit_cache_bypasses_guard(tmp_path, monkeypatch):
    from locg.commands import cmd_collection_remediate_delete

    monkeypatch.delenv("LOCG_DATA_DIR", raising=False)
    cache = make_cache(tmp_path)
    _seed(cache, [_row(gixen_item_id="99")])
    _no_default_collection_store(monkeypatch)

    result = cmd_collection_remediate_delete(gixen_item_id="99", cache=cache)

    assert result["status"] == "ok"
    assert result["action"] == "removed"
    # Re-read from disk (not just the returned dict) — confirms the delete
    # actually persisted at the redirected store, not merely that `cache.apply`
    # was invoked with an outcome that happened not to be written.
    assert cache.load().get("comics", []) == []


def test_remediate_delete_locg_data_dir_set_still_runs(tmp_path, monkeypatch):
    """The SERVER-shaped call: no cache= is passed, so the store is resolved
    SOLELY from LOCG_DATA_DIR — exactly what routes._ensure_collection_store()
    guarantees before every collection call."""
    from locg.commands import cmd_collection_remediate_delete

    store = tmp_path / "server-owned"
    store.mkdir()
    monkeypatch.setenv("LOCG_DATA_DIR", str(store))
    cache = CollectionCache(
        path=store / "collection.json",
        lock_path=store / "collection.lock",
        audit_path=store / "import-history.jsonl",
    )
    _seed(cache, [_row(gixen_item_id="99")])

    result = cmd_collection_remediate_delete(gixen_item_id="99")

    assert result["status"] == "ok"
    # Re-read the SAME store dir from disk — proves the write landed at the
    # LOCG_DATA_DIR-resolved path, not merely that the call returned "ok".
    assert cache.load().get("comics", []) == []


# ---------------------------------------------------------------------------
# cmd_collection_remediate_set_copies
# ---------------------------------------------------------------------------

def test_remediate_set_copies_no_cache_no_env_is_refused(monkeypatch):
    from locg.commands import cmd_collection_remediate_set_copies

    monkeypatch.delenv("LOCG_DATA_DIR", raising=False)
    _no_default_collection_store(monkeypatch)

    result = cmd_collection_remediate_set_copies(gixen_item_id="99", in_collection=2)

    assert result["status"] == "explicit_store_required"


def test_remediate_set_copies_guard_fires_even_for_dry_run(monkeypatch):
    from locg.commands import cmd_collection_remediate_set_copies

    monkeypatch.delenv("LOCG_DATA_DIR", raising=False)
    _no_default_collection_store(monkeypatch)

    result = cmd_collection_remediate_set_copies(
        gixen_item_id="99", in_collection=2, dry_run=True
    )

    assert result["status"] == "explicit_store_required"


def test_remediate_set_copies_explicit_cache_bypasses_guard(tmp_path, monkeypatch):
    from locg.commands import cmd_collection_remediate_set_copies

    monkeypatch.delenv("LOCG_DATA_DIR", raising=False)
    cache = make_cache(tmp_path)
    _seed(cache, [_row(gixen_item_id="99", in_collection=1)])
    _no_default_collection_store(monkeypatch)

    result = cmd_collection_remediate_set_copies(
        gixen_item_id="99", in_collection=3, cache=cache
    )

    assert result["status"] == "ok"
    assert result["new_in_collection"] == 3
    # Re-read from disk — confirms the copy count actually persisted at the
    # redirected store, not just that the returned dict looked right.
    rows = {r["gixen_item_id"]: r for r in cache.load()["comics"]}
    assert rows["99"]["in_collection"] == 3


def test_remediate_set_copies_locg_data_dir_set_still_runs(tmp_path, monkeypatch):
    from locg.commands import cmd_collection_remediate_set_copies

    store = tmp_path / "server-owned"
    store.mkdir()
    monkeypatch.setenv("LOCG_DATA_DIR", str(store))
    cache = CollectionCache(
        path=store / "collection.json",
        lock_path=store / "collection.lock",
        audit_path=store / "import-history.jsonl",
    )
    _seed(cache, [_row(gixen_item_id="99", in_collection=1)])

    result = cmd_collection_remediate_set_copies(gixen_item_id="99", in_collection=5)

    assert result["status"] == "ok"
    assert result["new_in_collection"] == 5
    # Re-read the SAME store dir from disk — proves the write landed at the
    # LOCG_DATA_DIR-resolved path, not merely that the call returned "ok".
    rows = {r["gixen_item_id"]: r for r in cache.load()["comics"]}
    assert rows["99"]["in_collection"] == 5


# ---------------------------------------------------------------------------
# wish-list writers: cmd_wish_list_add / cmd_wish_list_remove /
# cmd_wish_list_set_year — all three now accept `cache`, which
# `_resolve_wish_list_path` derives a WISH-LIST path from
# (`cache.path.parent / "wish-list.json"`), so passing one both bypasses the
# guard AND redirects the write (not just a bare signal).
# ---------------------------------------------------------------------------

def test_wish_list_add_no_cache_no_env_is_refused(monkeypatch):
    from locg.commands import cmd_wish_list_add

    monkeypatch.delenv("LOCG_DATA_DIR", raising=False)
    _no_default_wish_list_store(monkeypatch)

    result = cmd_wish_list_add("Amazing Spider-Man #300")

    assert result["status"] == "explicit_store_required"
    assert "LOCG_DATA_DIR" in result["error"]


def test_wish_list_add_explicit_cache_redirects_and_bypasses_guard(tmp_path, monkeypatch):
    from locg.commands import cmd_wish_list_add

    monkeypatch.delenv("LOCG_DATA_DIR", raising=False)
    cache = make_cache(tmp_path)
    _no_default_wish_list_store(monkeypatch)

    result = cmd_wish_list_add("Amazing Spider-Man #300", cache=cache)

    assert result["status"] == "ok"
    written = json.loads((tmp_path / "wish-list.json").read_text())
    assert written["items"][0]["name"] == "Amazing Spider-Man #300"


def test_wish_list_add_locg_data_dir_set_still_runs(tmp_path, monkeypatch):
    from locg.commands import cmd_wish_list_add

    store = tmp_path / "server-owned"
    store.mkdir()
    monkeypatch.setenv("LOCG_DATA_DIR", str(store))

    result = cmd_wish_list_add("Amazing Spider-Man #300")

    assert result["status"] == "ok"
    assert (store / "wish-list.json").exists()


def test_wish_list_remove_no_cache_no_env_is_refused(monkeypatch):
    from locg.commands import cmd_wish_list_remove

    monkeypatch.delenv("LOCG_DATA_DIR", raising=False)
    _no_default_wish_list_store(monkeypatch)

    result = cmd_wish_list_remove("Amazing Spider-Man #300")

    assert result["status"] == "explicit_store_required"


def test_wish_list_remove_explicit_cache_redirects_and_bypasses_guard(tmp_path, monkeypatch):
    from locg.commands import cmd_wish_list_add, cmd_wish_list_remove

    cache = make_cache(tmp_path)
    cmd_wish_list_add("Amazing Spider-Man #300", cache=cache)  # seed via the SAME redirected store

    monkeypatch.delenv("LOCG_DATA_DIR", raising=False)
    _no_default_wish_list_store(monkeypatch)

    result = cmd_wish_list_remove("Amazing Spider-Man #300", cache=cache)

    assert result["status"] == "ok"
    written = json.loads((tmp_path / "wish-list.json").read_text())
    assert written["items"] == []


def test_wish_list_remove_locg_data_dir_set_still_runs(tmp_path, monkeypatch):
    from locg.commands import cmd_wish_list_add, cmd_wish_list_remove

    store = tmp_path / "server-owned"
    store.mkdir()
    monkeypatch.setenv("LOCG_DATA_DIR", str(store))
    cmd_wish_list_add("Amazing Spider-Man #300")

    result = cmd_wish_list_remove("Amazing Spider-Man #300")

    assert result["status"] == "ok"


def test_wish_list_set_year_no_cache_no_env_is_refused(monkeypatch):
    from locg.commands import cmd_wish_list_set_year

    monkeypatch.delenv("LOCG_DATA_DIR", raising=False)
    _no_default_wish_list_store(monkeypatch)

    result = cmd_wish_list_set_year("The X-Men #1", "1963")

    assert result["status"] == "explicit_store_required"


def test_wish_list_set_year_explicit_cache_redirects_and_bypasses_guard(tmp_path, monkeypatch):
    from locg.commands import cmd_wish_list_add, cmd_wish_list_set_year

    cache = make_cache(tmp_path)
    cmd_wish_list_add("The X-Men #1", cache=cache)

    monkeypatch.delenv("LOCG_DATA_DIR", raising=False)
    _no_default_wish_list_store(monkeypatch)

    result = cmd_wish_list_set_year("The X-Men #1", "1963", cache=cache)

    assert result["status"] == "ok"
    written = json.loads((tmp_path / "wish-list.json").read_text())
    assert written["items"][0]["year"] == "1963"


def test_wish_list_set_year_locg_data_dir_set_still_runs(tmp_path, monkeypatch):
    from locg.commands import cmd_wish_list_add, cmd_wish_list_set_year

    store = tmp_path / "server-owned"
    store.mkdir()
    monkeypatch.setenv("LOCG_DATA_DIR", str(store))
    cmd_wish_list_add("The X-Men #1")

    result = cmd_wish_list_set_year("The X-Men #1", "1963")

    assert result["status"] == "ok"


# ---------------------------------------------------------------------------
# cmd_wish_list_add_creator_run (BUI-497): the one wish-list writer BUI-489
# deliberately left unguarded. Metron is stubbed out entirely (mirrors
# test_commands.py's own `_patch_metron_run`) since this suite exercises only
# the store guard + write redirect, not creator-run resolution logic —
# that's already covered in test_commands.py.
# ---------------------------------------------------------------------------

def _patch_metron_run(monkeypatch, *, creator, run):
    """Patch MetronClient so resolve_creator/resolve_creator_run return canned data."""
    import locg.metron as metron_mod
    from unittest.mock import MagicMock

    inst = MagicMock()
    inst.resolve_creator.return_value = creator
    inst.resolve_creator_run.return_value = run
    monkeypatch.setattr(metron_mod, "MetronClient", lambda: inst)
    return inst


def _stub_collection_check_not_owned(monkeypatch):
    """cmd_collection_check is a READ command (out of BUI-497's scope, per
    _needs_explicit_store's docstring) — it always resolves via the bare/env
    store, with no `cache` override. Stubbing it decouples these guard tests
    from that unrelated resolution path, matching how the Metron calls are
    stubbed out above.

    NOTE: this stub scopes OUT a real (if currently dormant) gap, not proves
    it safe — when an explicit `cache` is passed (as in
    test_creator_run_explicit_cache_bypasses_guard_and_writes_redirected_store
    below), the REAL cmd_collection_check would still check the bare/env
    store rather than `cache`'s store, which could silently reintroduce the
    BUI-122 owned-book-gets-wishlisted trap for a future caller. See the
    BUI-497 docstring on cmd_wish_list_add_creator_run for the full
    explanation. Left unfixed here deliberately — out of this ticket's named
    scope (the R11 load and the wish-list read/write only)."""
    import locg.commands as cmds

    monkeypatch.setattr(
        cmds,
        "cmd_collection_check",
        lambda **kwargs: {"match_status": "not_in_cache", "in_wish_list": False},
    )


def test_creator_run_no_cache_no_env_is_refused(monkeypatch):
    from locg.commands import cmd_wish_list_add_creator_run

    monkeypatch.delenv("LOCG_DATA_DIR", raising=False)
    _no_default_collection_store(monkeypatch)
    _no_default_wish_list_store(monkeypatch)

    def _boom(*args, **kwargs):
        raise AssertionError("Metron was called despite the store refusal")

    monkeypatch.setattr("locg.metron.MetronClient", _boom)

    result = cmd_wish_list_add_creator_run(
        series="Uncanny X-Men", creator="John Romita Jr.", series_id=99,
    )

    assert result["status"] == "explicit_store_required"
    assert "LOCG_DATA_DIR" in result["error"]


def test_creator_run_explicit_cache_bypasses_guard_and_writes_redirected_store(
    tmp_path, monkeypatch
):
    from locg.commands import cmd_wish_list_add_creator_run

    monkeypatch.delenv("LOCG_DATA_DIR", raising=False)
    cache = make_cache(tmp_path)
    _seed(cache, [], imported=True)  # stamp last_full_import; nothing owned
    _no_default_collection_store(monkeypatch)
    _no_default_wish_list_store(monkeypatch)
    _stub_collection_check_not_owned(monkeypatch)
    _patch_metron_run(
        monkeypatch,
        creator={"id": 355, "name": "John Romita Jr."},
        run={
            "issues": [{"number": "175", "metron_id": 1, "cover_date": "1983-11-01"}],
            "warnings": [],
        },
    )

    result = cmd_wish_list_add_creator_run(
        series="Uncanny X-Men", creator="John Romita Jr.", series_id=99, cache=cache,
    )

    assert result["status"] == "ok"
    assert result["added"] == ["Uncanny X-Men #175"]
    # Re-read from disk at the redirected store — confirms the write actually
    # persisted there, not merely that the returned dict looked right.
    written = json.loads((tmp_path / "wish-list.json").read_text())
    assert written["items"][0]["name"] == "Uncanny X-Men #175"


def test_creator_run_locg_data_dir_set_still_runs(tmp_path, monkeypatch):
    """The SERVER-shaped call: no cache= is passed, so the store is resolved
    SOLELY from LOCG_DATA_DIR — exactly what routes._ensure_collection_store()
    guarantees before every collection call."""
    from locg.collection_cache import CollectionCache
    from locg.commands import cmd_wish_list_add_creator_run

    store = tmp_path / "server-owned"
    store.mkdir()
    monkeypatch.setenv("LOCG_DATA_DIR", str(store))

    default_cache = CollectionCache()
    default_cache.load()

    def mutate(payload):
        payload["last_full_import"] = "2026-01-01T00:00:00Z"

    default_cache.apply(mutate, command="seed")

    _stub_collection_check_not_owned(monkeypatch)
    _patch_metron_run(
        monkeypatch,
        creator={"id": 355, "name": "John Romita Jr."},
        run={
            "issues": [{"number": "175", "metron_id": 1, "cover_date": "1983-11-01"}],
            "warnings": [],
        },
    )

    result = cmd_wish_list_add_creator_run(
        series="Uncanny X-Men", creator="John Romita Jr.", series_id=99,
    )

    assert result["status"] == "ok"
    assert (store / "wish-list.json").exists()


# ---------------------------------------------------------------------------
# cmd_wish_list_remove_conflicts: env-var-only escape (see its own docstring
# for why it does NOT accept a `cache` override — its audit half has none
# either, and a partial override would let the audit and the removal it
# drives silently disagree on which store they mean).
# ---------------------------------------------------------------------------

def test_wish_list_remove_conflicts_no_env_is_refused(monkeypatch):
    from locg.commands import cmd_wish_list_remove_conflicts

    monkeypatch.delenv("LOCG_DATA_DIR", raising=False)
    _no_default_collection_store(monkeypatch)
    _no_default_wish_list_store(monkeypatch)

    result = cmd_wish_list_remove_conflicts()

    assert result["status"] == "explicit_store_required"


def test_wish_list_remove_conflicts_refuses_before_the_audit_runs(monkeypatch):
    """Mirrors record-win's "refuses before spending any Metron call" test:
    the refusal must come before cmd_wish_list_conflicts() (and the
    per-item cmd_collection_check calls it makes) ever runs."""
    import locg.commands as cmds

    monkeypatch.delenv("LOCG_DATA_DIR", raising=False)
    _no_default_collection_store(monkeypatch)
    _no_default_wish_list_store(monkeypatch)

    def _boom():
        raise AssertionError("the conflicts audit ran despite the refusal")

    monkeypatch.setattr(cmds, "cmd_wish_list_conflicts", _boom)

    result = cmds.cmd_wish_list_remove_conflicts()

    assert result["status"] == "explicit_store_required"


def test_wish_list_remove_conflicts_locg_data_dir_set_still_runs(tmp_path, monkeypatch):
    """The autouse _isolate_cache_dir fixture already sets LOCG_DATA_DIR, so a
    bare call must keep working exactly as it did before BUI-489."""
    from locg.commands import cmd_wish_list_remove_conflicts, wish_list_cache_path

    wish_list_cache_path().parent.mkdir(parents=True, exist_ok=True)
    wish_list_cache_path().write_text(
        json.dumps({"updated_at": "2026-01-01T00:00:00Z", "items": []})
    )

    result = cmd_wish_list_remove_conflicts()

    assert result.get("status") != "explicit_store_required"
    assert result["removed_count"] == 0


# ---------------------------------------------------------------------------
# cmd_collection_export: distinct not-imported signal (BUI-489 Part 2)
# ---------------------------------------------------------------------------

def test_export_genuinely_untouched_store_returns_not_imported(tmp_path, monkeypatch):
    """Zero comics, zero wish-list entries, never imported — the actual
    wrong/fresh-store trap. Must NOT silently write a zero-row CSV."""
    import locg.commands as cmds

    cache = make_cache(tmp_path)
    monkeypatch.setattr(cmds, "CollectionCache", lambda: cache)

    out_csv = tmp_path / "out.csv"
    result = cmds.cmd_collection_export(str(out_csv))

    assert result["status"] == "not_imported"
    assert not out_csv.exists()


def test_export_record_win_only_store_exports_normally(tmp_path, monkeypatch):
    """The real /comic:collection-add flow (BUI-432's
    test_audit_integrates_with_real_export_headers calls this "the real
    export pipeline"): record-win populates real rows with
    last_full_import still None, since no `collection import` has ever run.
    Must export normally, never be mistaken for 'never touched'."""
    import locg.commands as cmds

    cache = make_cache(tmp_path)
    monkeypatch.setattr(cmds, "CollectionCache", lambda: cache)
    _seed(cache, [_row()], imported=False)

    out_csv = tmp_path / "out.csv"
    result = cmds.cmd_collection_export(str(out_csv))

    assert result.get("status") != "not_imported"
    assert result["ready_count"] == 1
    assert out_csv.exists()


def test_export_wish_only_store_exports_normally(tmp_path, monkeypatch):
    """A local-only wish add on a never-imported collection is also
    legitimate (push_wishes=True is the owned-safe wish mirror)."""
    import locg.commands as cmds
    from locg.commands import cmd_wish_list_add

    cache = make_cache(tmp_path)
    monkeypatch.setattr(cmds, "CollectionCache", lambda: cache)
    monkeypatch.setenv("LOCG_DATA_DIR", str(tmp_path))
    cmd_wish_list_add("Saga #1")

    out_csv = tmp_path / "out.csv"
    result = cmds.cmd_collection_export(str(out_csv), push_wishes=True)

    assert result.get("status") != "not_imported"
    assert result["wish_list_count"] == 1


def test_export_legitimately_imported_empty_store_exports_normally(tmp_path, monkeypatch):
    """A real import that produced zero pending rows must stay a normal 'ok'
    zero-count result, not get relabeled not_imported (a wrong reading of
    `last_full_import is None` alone would have broken this)."""
    import locg.commands as cmds

    cache = make_cache(tmp_path)
    monkeypatch.setattr(cmds, "CollectionCache", lambda: cache)
    _seed(cache, [], imported=True)

    out_csv = tmp_path / "out.csv"
    result = cmds.cmd_collection_export(str(out_csv))

    assert result.get("status") != "not_imported"
    assert result["ready_count"] == 0


def test_export_corrupt_wish_list_with_empty_never_imported_collection_does_not_crash(
    tmp_path, monkeypatch
):
    """Found in BUI-489 code review: the not-imported check's presence probe
    of wish-list.json (via `_read_wish_list_cache_items`, which deliberately
    does NOT catch JSONDecodeError — it's built for its other, write-side
    callers) is a NEW read-path caller of that function. Export must not
    propagate a raw json.JSONDecodeError for a corrupt wish-list.json; a
    corrupt file is itself evidence of real (if unreadable) prior activity,
    so this falls through to a normal export rather than crashing OR
    misreporting `not_imported`."""
    import locg.commands as cmds

    cache = make_cache(tmp_path)
    monkeypatch.setattr(cmds, "CollectionCache", lambda: cache)
    monkeypatch.setenv("LOCG_DATA_DIR", str(tmp_path))
    (tmp_path / "wish-list.json").write_text("{not valid json")

    out_csv = tmp_path / "out.csv"
    result = cmds.cmd_collection_export(str(out_csv))  # must not raise

    assert result.get("status") != "not_imported"
    assert result["ready_count"] == 0
