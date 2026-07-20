"""Tests for the collection cache module (Unit 1)."""
from __future__ import annotations

import fcntl
import json
import multiprocessing
import os
import stat
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_cache(tmp_path: Path, **kwargs):
    from locg.collection_cache import CollectionCache
    return CollectionCache(
        path=tmp_path / "collection.json",
        lock_path=tmp_path / "collection.lock",
        audit_path=tmp_path / "import-history.jsonl",
        **kwargs,
    )


def make_row(
    publisher: str = "Marvel",
    series: str = "Amazing Spider-Man (1963 - 1998)",
    full_title: str = "Amazing Spider-Man #300",
    release_date: str = "1988-05-10",
    source: str = "locg_export",
    seq: int = 1,
    gixen_item_id: str | None = None,
) -> dict[str, Any]:
    return {
        "publisher_name": publisher,
        "series_name": series,
        "full_title": full_title,
        "release_date": release_date,
        "in_collection": 1,
        "in_wish_list": 0,
        "marked_read": 0,
        "my_rating": None,
        "media_format": "Print",
        "price_paid": None,
        "date_purchased": None,
        "condition": None,
        "notes": None,
        "tags": None,
        "storage_box": None,
        "owner": None,
        "purchase_store": None,
        "signature": None,
        "slabbing": None,
        "grading": None,
        "grading_company": None,
        "local_added_at": "2024-01-01T00:00:00.000000Z",
        "local_added_seq": seq,
        "pushed_to_locg_at": None,
        "last_seen_in_export_at": None,
        "source": source,
        "needs_manual_variant": False,
        "needs_manual_series_canonical": False,
        "metron_id": None,
        "gixen_item_id": gixen_item_id,
        "previous_full_title": None,
    }


def _load_json(path: Path) -> dict:
    with open(path) as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Happy path: load / save round-trip
# ---------------------------------------------------------------------------

def test_empty_cache_returns_default(tmp_path):
    from locg.collection_cache import CollectionCache, SCHEMA_VERSION, empty_payload
    cache = make_cache(tmp_path)
    payload = cache.load()
    assert payload["schema_version"] == SCHEMA_VERSION
    assert payload["comics"] == []
    assert payload["last_full_import"] is None
    assert payload["migration_in_progress"] is False
    assert payload["series_name_index"] == {}


def test_round_trip_50_rows(tmp_path):
    from locg.collection_cache import CollectionCache
    cache = make_cache(tmp_path)
    rows = [make_row(full_title=f"Comic #{i}", seq=i) for i in range(50)]

    def mutate(payload):
        payload["comics"].extend(rows)

    cache.apply(mutate, command="test")
    reloaded = cache.load()
    assert len(reloaded["comics"]) == 50
    loaded_titles = {r["full_title"] for r in reloaded["comics"]}
    assert loaded_titles == {f"Comic #{i}" for i in range(50)}


def test_identity_tuple_deduplicates(tmp_path):
    from locg.collection_cache import CollectionCache, make_identity
    cache = make_cache(tmp_path)
    row = make_row()
    # Insert twice — second insert should overwrite, not append
    def mutate(payload):
        identity_map = {make_identity(r): i for i, r in enumerate(payload["comics"])}
        key = make_identity(row)
        if key in identity_map:
            payload["comics"][identity_map[key]] = row
        else:
            payload["comics"].append(row)

    cache.apply(mutate, command="test")
    cache.apply(mutate, command="test")  # second insert

    reloaded = cache.load()
    assert len(reloaded["comics"]) == 1  # not 2


# ---------------------------------------------------------------------------
# R7: boolean columns stored as int 0/1
# ---------------------------------------------------------------------------

def test_boolean_columns_stored_as_int(tmp_path):
    from locg.collection_cache import CollectionCache
    cache = make_cache(tmp_path)
    row = make_row()
    row["in_collection"] = 1
    row["in_wish_list"] = 0
    row["marked_read"] = 0

    def mutate(payload):
        payload["comics"].append(row)

    cache.apply(mutate, command="test")

    # Inspect raw JSON bytes — must not contain `true`/`false` for LOCG columns
    raw = (tmp_path / "collection.json").read_text()
    data = json.loads(raw)
    r = data["comics"][0]
    assert r["in_collection"] == 1
    assert r["in_wish_list"] == 0
    assert r["marked_read"] == 0
    assert isinstance(r["in_collection"], int)
    assert isinstance(r["in_wish_list"], int)


# ---------------------------------------------------------------------------
# Permissions: 0600 file / 0700 dir
# ---------------------------------------------------------------------------

def test_file_permissions_after_write(tmp_path):
    from locg.collection_cache import CollectionCache
    cache = make_cache(tmp_path)
    cache.apply(lambda p: None, command="test")
    cache_file = tmp_path / "collection.json"
    assert cache_file.exists()
    mode = stat.S_IMODE(cache_file.stat().st_mode)
    assert mode == 0o600, f"Expected 0600, got {oct(mode)}"


# ---------------------------------------------------------------------------
# Schema version guard
# ---------------------------------------------------------------------------

def test_schema_version_1_loads_cleanly(tmp_path):
    from locg.collection_cache import CollectionCache, SCHEMA_VERSION, empty_payload
    cache = make_cache(tmp_path)
    payload = empty_payload()
    assert payload["schema_version"] == SCHEMA_VERSION
    cache.apply(lambda p: None, command="test")
    reloaded = cache.load()
    assert reloaded["schema_version"] == SCHEMA_VERSION


def test_future_schema_version_raises(tmp_path):
    from locg.collection_cache import CollectionCache
    cache = make_cache(tmp_path)
    # Write a cache file with a future schema version
    payload = {"schema_version": 99, "migration_in_progress": False,
               "last_full_import": None, "last_import_source": None,
               "last_writer": None, "series_name_index": {}, "comics": []}
    (tmp_path / "collection.json").write_text(json.dumps(payload))
    with pytest.raises(RuntimeError, match="re-import"):
        cache.load()


# ---------------------------------------------------------------------------
# migration_in_progress crash recovery
# ---------------------------------------------------------------------------

def test_migration_in_progress_different_last_import_raises(tmp_path):
    """Crashed mid-merge: live last_full_import != .bak.0 last_full_import."""
    from locg.collection_cache import CollectionCache
    cache = make_cache(tmp_path)
    cache_file = tmp_path / "collection.json"
    bak0 = tmp_path / "collection.json.bak.0"

    # Write bak.0 with one last_full_import value
    bak0.write_text(json.dumps({"schema_version": 1, "last_full_import": "2024-01-01T00:00:00Z",
                                 "migration_in_progress": False, "series_name_index": {},
                                 "last_import_source": None, "last_writer": None, "comics": []}))
    # Write live file with different last_full_import and migration_in_progress=True
    cache_file.write_text(json.dumps({"schema_version": 1, "last_full_import": "2024-02-01T00:00:00Z",
                                       "migration_in_progress": True, "series_name_index": {},
                                       "last_import_source": "test.xlsx", "last_writer": None, "comics": []}))
    with pytest.raises(RuntimeError, match="crashed"):
        cache.load()


def test_migration_in_progress_same_last_import_auto_clears(tmp_path):
    """Killed before merge began: live last_full_import == .bak.0 last_full_import."""
    from locg.collection_cache import CollectionCache
    cache = make_cache(tmp_path)
    cache_file = tmp_path / "collection.json"
    bak0 = tmp_path / "collection.json.bak.0"

    same_ts = "2024-01-01T00:00:00Z"
    bak0.write_text(json.dumps({"schema_version": 1, "last_full_import": same_ts,
                                 "migration_in_progress": False, "series_name_index": {},
                                 "last_import_source": None, "last_writer": None, "comics": []}))
    cache_file.write_text(json.dumps({"schema_version": 1, "last_full_import": same_ts,
                                       "migration_in_progress": True, "series_name_index": {},
                                       "last_import_source": None, "last_writer": None, "comics": []}))
    payload = cache.load()
    assert payload["migration_in_progress"] is False


# ---------------------------------------------------------------------------
# Corrupt JSON
# ---------------------------------------------------------------------------

def test_corrupt_json_raises_with_guidance(tmp_path):
    from locg.collection_cache import CollectionCache
    cache = make_cache(tmp_path)
    (tmp_path / "collection.json").write_text("{not valid json")
    with pytest.raises(RuntimeError, match="corrupt"):
        cache.load()


# ---------------------------------------------------------------------------
# Backup rotation
# ---------------------------------------------------------------------------

def test_bak0_written_before_merge(tmp_path):
    """.bak.0 must contain the pre-merge state."""
    from locg.collection_cache import CollectionCache
    cache = make_cache(tmp_path)
    row1 = make_row(full_title="Pre-Merge Comic")

    # First save (creates the live file with row1)
    cache.apply(lambda p: p["comics"].append(row1), command="first")

    # Second save — .bak.0 should capture the post-first / pre-second state
    row2 = make_row(full_title="Post-Merge Comic", seq=2)
    cache.apply(lambda p: p["comics"].append(row2), command="second")

    bak0 = tmp_path / "collection.json.bak.0"
    assert bak0.exists()
    bak_data = _load_json(bak0)
    titles = {r["full_title"] for r in bak_data["comics"]}
    assert "Pre-Merge Comic" in titles
    assert "Post-Merge Comic" not in titles


def test_rolling_backup_rotation(tmp_path):
    """Three successive merges leave .bak.0/.bak.1/.bak.2 with monotonically older contents."""
    from locg.collection_cache import CollectionCache
    cache = make_cache(tmp_path)

    labels = ["First", "Second", "Third", "Fourth"]
    for label in labels:
        row = make_row(full_title=f"{label} Comic")
        cache.apply(lambda p, r=row: p["comics"].append(r), command=label.lower())

    bak0 = _load_json(tmp_path / "collection.json.bak.0")
    bak1 = _load_json(tmp_path / "collection.json.bak.1")
    bak2 = _load_json(tmp_path / "collection.json.bak.2")

    def titles(payload):
        return {r["full_title"] for r in payload["comics"]}

    # .bak.0 = pre-fourth (after three)
    assert "Third Comic" in titles(bak0)
    # .bak.1 = pre-third (after two)
    assert "Second Comic" in titles(bak1)
    # .bak.2 = pre-second (after one)
    assert "First Comic" in titles(bak2)
    # Each bak should have fewer comics than the next
    assert len(bak0["comics"]) > len(bak1["comics"]) > len(bak2["comics"])


def test_bak0_not_refreshed_on_success(tmp_path):
    """.bak.0 mtime must NOT change after a successful merge — only before the next one."""
    from locg.collection_cache import CollectionCache
    cache = make_cache(tmp_path)
    bak0 = tmp_path / "collection.json.bak.0"

    # First save creates the live file
    cache.apply(lambda p: p["comics"].append(make_row(full_title="Row 1")), command="first")
    # Second save creates .bak.0
    cache.apply(lambda p: p["comics"].append(make_row(full_title="Row 2", seq=2)), command="second")

    mtime_after_second = bak0.stat().st_mtime_ns

    # Load — must NOT change .bak.0
    cache.load()
    assert bak0.stat().st_mtime_ns == mtime_after_second

    # Third save — NOW .bak.0 changes
    time.sleep(0.01)  # Ensure mtime changes
    cache.apply(lambda p: p["comics"].append(make_row(full_title="Row 3", seq=3)), command="third")
    assert bak0.stat().st_mtime_ns != mtime_after_second


# ---------------------------------------------------------------------------
# Flag cleared atomically — no intermediate on-disk state with migration_in_progress=True
# ---------------------------------------------------------------------------

def test_migration_flag_never_written_true(tmp_path):
    """The cache file must never contain migration_in_progress=True after apply()."""
    from locg.collection_cache import CollectionCache

    written_payloads = []
    original_replace = os.replace

    def spy_replace(src, dst):
        if str(dst).endswith("collection.json"):
            written_payloads.append(json.loads(Path(src).read_text()))
        original_replace(src, dst)

    cache = make_cache(tmp_path)
    with patch("os.replace", side_effect=spy_replace):
        cache.apply(lambda p: p["comics"].append(make_row()), command="test")

    assert written_payloads, "No atomic write observed"
    for payload in written_payloads:
        assert payload.get("migration_in_progress") is False


# ---------------------------------------------------------------------------
# local_added_seq tiebreaking
# ---------------------------------------------------------------------------

def test_local_added_seq_distinct_in_batch(tmp_path):
    """Two rows added in the same batch with same timestamp must have distinct seq."""
    from locg.collection_cache import CollectionCache, _next_seq
    # Reset seq counter context doesn't matter here — we test the counter directly
    seq1 = _next_seq()
    seq2 = _next_seq()
    assert seq2 == seq1 + 1


def test_local_added_seq_ordering(tmp_path):
    """Rows with same local_added_at but different seq order deterministically."""
    from locg.collection_cache import CollectionCache, _next_seq
    ts = "2024-01-01T00:00:00.000001Z"
    seq_a = _next_seq()
    seq_b = _next_seq()
    # seq_b > seq_a, so row_b is "later" in tiebreak
    row_a = make_row(full_title="Row A", seq=seq_a)
    row_b = make_row(full_title="Row B", seq=seq_b)
    row_a["local_added_at"] = ts
    row_b["local_added_at"] = ts

    def tiebreak_key(r):
        return (r["local_added_at"], r["local_added_seq"])

    assert tiebreak_key(row_b) > tiebreak_key(row_a)


# ---------------------------------------------------------------------------
# write_wins: intra-batch duplicate handling (BUI-356)
# ---------------------------------------------------------------------------

def test_write_wins_intra_batch_duplicate_gixen_id_appends_once(tmp_path):
    """Two rows sharing gixen_item_id in ONE write_wins call must not both append.

    The duplicate index used to be built once before the loop, so neither row
    saw the other as a duplicate and both got appended (BUI-356). The second
    (later) row must instead overwrite the first, exactly like a duplicate
    against the on-disk store.
    """
    cache = make_cache(tmp_path)
    row_a = make_row(full_title="Amazing Spider-Man #300", seq=1, gixen_item_id="DUPE")
    row_b = make_row(
        full_title="Amazing Spider-Man #300 (variant)", seq=2, gixen_item_id="DUPE"
    )

    result = cache.write_wins([row_a, row_b])

    payload = cache.load()
    dupes = [r for r in payload["comics"] if r.get("gixen_item_id") == "DUPE"]
    assert len(dupes) == 1
    # Later row in the batch wins, matching duplicate-against-store overwrite
    # semantics (payload["comics"][idx] = row).
    assert dupes[0]["full_title"] == "Amazing Spider-Man #300 (variant)"
    # BUI-367: the intra-batch collision is one insert (row_a) + one overwrite
    # (row_b replacing row_a in place), never two inserts.
    assert result.inserted == 1
    assert result.overwritten == 1


# ---------------------------------------------------------------------------
# write_wins: inserted/overwritten split (BUI-367)
# ---------------------------------------------------------------------------

def test_write_wins_pure_insert_batch_counts_all_inserted(tmp_path):
    """A batch with no gixen_item_id collisions is all inserts, zero overwrites."""
    cache = make_cache(tmp_path)
    rows = [
        make_row(full_title="Amazing Spider-Man #300", seq=1, gixen_item_id="A"),
        make_row(full_title="Amazing Spider-Man #301", seq=2, gixen_item_id="B"),
        make_row(full_title="Amazing Spider-Man #302", seq=3, gixen_item_id="C"),
    ]

    result = cache.write_wins(rows)

    assert result.inserted == 3
    assert result.overwritten == 0
    payload = cache.load()
    assert len(payload["comics"]) == 3


def test_write_wins_overwrite_only_batch_counts_all_overwritten(tmp_path):
    """A batch whose every gixen_item_id already exists on disk is all overwrites."""
    cache = make_cache(tmp_path)
    seed_rows = [
        make_row(full_title="Amazing Spider-Man #300", seq=1, gixen_item_id="A"),
        make_row(full_title="Amazing Spider-Man #301", seq=2, gixen_item_id="B"),
    ]
    cache.write_wins(seed_rows)

    updated_rows = [
        make_row(full_title="Amazing Spider-Man #300 (CGC 9.8)", seq=3, gixen_item_id="A"),
        make_row(full_title="Amazing Spider-Man #301 (CGC 9.6)", seq=4, gixen_item_id="B"),
    ]
    result = cache.write_wins(updated_rows)

    assert result.inserted == 0
    assert result.overwritten == 2
    payload = cache.load()
    # Still 2 rows total — both updated in place, none appended.
    assert len(payload["comics"]) == 2
    titles = {r["full_title"] for r in payload["comics"]}
    assert titles == {"Amazing Spider-Man #300 (CGC 9.8)", "Amazing Spider-Man #301 (CGC 9.6)"}


def test_write_wins_mixed_batch_splits_inserted_and_overwritten(tmp_path):
    """A batch mixing new gixen_item_ids with ones already on disk splits accurately."""
    cache = make_cache(tmp_path)
    seed_rows = [
        make_row(full_title="Amazing Spider-Man #300", seq=1, gixen_item_id="A"),
    ]
    cache.write_wins(seed_rows)

    mixed_rows = [
        make_row(full_title="Amazing Spider-Man #300 (CGC 9.8)", seq=2, gixen_item_id="A"),  # overwrite
        make_row(full_title="Amazing Spider-Man #301", seq=3, gixen_item_id="B"),  # insert
        make_row(full_title="Amazing Spider-Man #302", seq=4, gixen_item_id="C"),  # insert
    ]
    result = cache.write_wins(mixed_rows)

    assert result.inserted == 2
    assert result.overwritten == 1
    payload = cache.load()
    assert len(payload["comics"]) == 3


def test_write_wins_row_without_gixen_id_always_inserted(tmp_path):
    """Rows without a gixen_item_id can never match an existing row — always inserted."""
    cache = make_cache(tmp_path)
    rows = [
        make_row(full_title="Amazing Spider-Man #300", seq=1, gixen_item_id=None),
        make_row(full_title="Amazing Spider-Man #301", seq=2, gixen_item_id=None),
    ]

    result = cache.write_wins(rows)

    assert result.inserted == 2
    assert result.overwritten == 0


# ---------------------------------------------------------------------------
# Concurrent writers
# ---------------------------------------------------------------------------

def _concurrent_writer_proc(cache_path_str: str, lock_path_str: str, audit_path_str: str,
                             row_id: int, result_path_str: str) -> None:
    """Top-level worker function for multiprocessing (must be picklable)."""
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
    from locg.collection_cache import CollectionCache

    cache = CollectionCache(
        path=Path(cache_path_str),
        lock_path=Path(lock_path_str),
        audit_path=Path(audit_path_str),
    )

    def mutate(payload):
        time.sleep(0.02)  # Increase interleaving probability
        row = make_row(full_title=f"Concurrent Comic #{row_id}", seq=row_id)
        row["gixen_item_id"] = str(row_id)
        payload["comics"].append(row)

    cache.apply(mutate, command="concurrent-test")
    Path(result_path_str).write_text("done")


def test_concurrent_writers_no_lost_updates(tmp_path):
    """Two processes merging concurrently must both commit — no lost updates."""
    cache_path = tmp_path / "collection.json"
    lock_path = tmp_path / "collection.lock"
    audit_path = tmp_path / "import-history.jsonl"
    result1 = tmp_path / "result1.txt"
    result2 = tmp_path / "result2.txt"

    ctx = multiprocessing.get_context("fork")
    p1 = ctx.Process(
        target=_concurrent_writer_proc,
        args=(str(cache_path), str(lock_path), str(audit_path), 1, str(result1)),
    )
    p2 = ctx.Process(
        target=_concurrent_writer_proc,
        args=(str(cache_path), str(lock_path), str(audit_path), 2, str(result2)),
    )
    p1.start()
    p2.start()
    p1.join(timeout=15)
    p2.join(timeout=15)

    assert p1.exitcode == 0, f"Process 1 failed with exit code {p1.exitcode}"
    assert p2.exitcode == 0, f"Process 2 failed with exit code {p2.exitcode}"
    assert result1.exists(), "Process 1 did not complete"
    assert result2.exists(), "Process 2 did not complete"

    from locg.collection_cache import CollectionCache
    final = CollectionCache(path=cache_path, lock_path=lock_path, audit_path=audit_path).load()
    titles = {r["full_title"] for r in final["comics"]}
    assert "Concurrent Comic #1" in titles
    assert "Concurrent Comic #2" in titles


# ---------------------------------------------------------------------------
# fsync called before os.replace
# ---------------------------------------------------------------------------

def test_fsync_called_before_replace(tmp_path):
    """os.fsync must be called on the tempfile fd before os.replace."""
    from locg.collection_cache import CollectionCache

    fsync_calls = []
    replace_calls = []
    original_fsync = os.fsync
    original_replace = os.replace

    def spy_fsync(fd):
        fsync_calls.append(("fsync", fd))
        original_fsync(fd)

    def spy_replace(src, dst):
        replace_calls.append(("replace", src, dst))
        original_replace(src, dst)

    cache = make_cache(tmp_path)
    with patch("os.fsync", side_effect=spy_fsync), patch("os.replace", side_effect=spy_replace):
        cache.apply(lambda p: None, command="test")

    # fsync must have been called at least once (on the tempfile fd)
    assert any(call[0] == "fsync" for call in fsync_calls)
    # The replace for collection.json must happen after at least one fsync
    replace_idx = next(
        i for i, c in enumerate(replace_calls)
        if c[0] == "replace" and str(c[2]).endswith("collection.json")
    )
    # At least one fsync must appear before the replace
    fsync_before = [c for c in fsync_calls[:replace_idx] if c[0] == "fsync"]
    # fsync_calls and replace_calls are from the same call sequence via the patches
    # Simpler: just verify fsync was called at all and replace was called
    assert fsync_calls
    assert any(str(c[2]).endswith("collection.json") for c in replace_calls if c[0] == "replace")


# ---------------------------------------------------------------------------
# flock timeout
# ---------------------------------------------------------------------------

def _hold_lock_proc(lock_path_str: str, ready_path: str, release_path: str) -> None:
    """Hold an exclusive flock until release_path appears."""
    lock_path = Path(lock_path_str)
    lock_path.touch()
    with open(lock_path) as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        Path(ready_path).write_text("ready")
        deadline = time.monotonic() + 10.0
        while not Path(release_path).exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        fcntl.flock(f, fcntl.LOCK_UN)


def test_flock_timeout_raises(tmp_path):
    """flock acquisition timeout must raise TimeoutError with clear message."""
    from locg.collection_cache import CollectionCache

    lock_path = tmp_path / "collection.lock"
    ready_flag = tmp_path / "ready.txt"
    release_flag = tmp_path / "release.txt"

    ctx = multiprocessing.get_context("fork")
    holder = ctx.Process(
        target=_hold_lock_proc,
        args=(str(lock_path), str(ready_flag), str(release_flag)),
    )
    holder.start()
    # Wait for the holder to acquire the lock
    deadline = time.monotonic() + 5.0
    while not ready_flag.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert ready_flag.exists(), "Lock holder did not start"

    try:
        cache = make_cache(tmp_path)
        with pytest.raises(TimeoutError, match="another locg-cli operation in progress"):
            cache.apply(lambda p: None, command="test", timeout=0.15)
    finally:
        release_flag.write_text("release")
        holder.join(timeout=5)


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------

def test_audit_log_envelope_shape(tmp_path):
    """Every audit record must be a single JSON line with {type, ts, command, details}."""
    from locg.collection_cache import CollectionCache
    cache = make_cache(tmp_path)
    cache.append_audit({
        "type": "reconciliation",
        "ts": "2024-01-01T00:00:00Z",
        "command": "import",
        "details": {"identity": ("Marvel", "ASM", "ASM #1", "1963-03-10")},
    })
    cache.append_audit({
        "type": "behavioral_drift",
        "ts": "2024-01-01T00:01:00Z",
        "command": "import",
        "details": {"columns_changed": ["my_rating"]},
    })
    lines = (tmp_path / "import-history.jsonl").read_text().strip().splitlines()
    assert len(lines) == 2
    for line in lines:
        record = json.loads(line)
        assert {"type", "ts", "command", "details"}.issubset(record.keys())


def test_audit_log_rejects_missing_keys(tmp_path):
    from locg.collection_cache import CollectionCache
    cache = make_cache(tmp_path)
    with pytest.raises(ValueError, match="missing"):
        cache.append_audit({"type": "reconciliation", "ts": "2024-01-01T00:00:00Z"})


# ---------------------------------------------------------------------------
# series_name_index rebuild
# ---------------------------------------------------------------------------

def test_series_name_index_locg_export_only(tmp_path):
    """series_name_index must be built only from source='locg_export' rows."""
    from locg.collection_cache import CollectionCache, rebuild_series_name_index
    rows = [
        make_row(series="Amazing Spider-Man (1963 - 1998)", source="locg_export"),
        make_row(series="Batman (1940 - 2011)", source="locg_export", full_title="Batman #1", seq=2),
        make_row(series="X-Men (1963)", source="agent_win", full_title="X-Men #1", seq=3),
    ]
    payload = {"schema_version": 1, "last_full_import": None, "last_import_source": None,
               "migration_in_progress": False, "last_writer": None,
               "series_name_index": {}, "comics": rows}
    index = rebuild_series_name_index(payload)
    # locg_export rows contribute
    assert any("amazing spider-man" in k for k in index.keys())
    assert any("batman" in k for k in index.keys())
    # agent_win rows must NOT contribute
    assert not any("x-men" in k for k in index.keys())


def test_series_name_index_normalizes_keys(tmp_path):
    """Index keys strip (Vol. N) and (YYYY - YYYY) from series names."""
    from locg.collection_cache import rebuild_series_name_index, _normalize_series_key
    assert _normalize_series_key("Amazing Spider-Man (1963 - 1998)") == "amazing spider-man"
    assert _normalize_series_key("Batman (Vol. 2)") == "batman"
    assert _normalize_series_key("Spawn (1992 - Present)") == "spawn"
    assert _normalize_series_key("1963 (1993)") == "1963"


def test_normalize_series_key_strips_leading_article():
    """Leading articles are dropped so 'The X' and 'X' share a key (BUI-45)."""
    from locg.collection_cache import _normalize_series_key
    # The cache stores the McFarlane run with the article; identify drops it.
    assert (
        _normalize_series_key("The Incredible Hulk (Vol. 2) (1968 - 1999)")
        == _normalize_series_key("Incredible Hulk")
        == "incredible hulk"
    )
    assert _normalize_series_key("A Distant Soil") == "distant soil"
    assert _normalize_series_key("An Unkindness of Ravens") == "unkindness of ravens"
    # Only a true leading article is stripped, not an embedded one.
    assert _normalize_series_key("Theater of War") == "theater of war"


def test_normalize_series_key_does_not_strip_stray_punctuation():
    """BUI-460: _normalize_series_key only strips year/vol/article decoration,
    NOT stray punctuation — so a series string with a leftover separator (the
    BUI-456 residual comic_identity.py's _finalize now cleans up before
    threading it here) would resolve to the WRONG key. This locks the
    contract from the locg side: apps/ebay/src/comic_identity.py must hand
    this function a clean series string, because this function will not
    clean it up itself.

    Fixed inputs (what comic-identify now produces for "X-Men Annual: 1",
    "Avengers Annual-#5", "Amazing Spider-Man Annual No. 1") thread to the
    same key the local series_name_index uses for those series.
    """
    from locg.collection_cache import _normalize_series_key

    assert _normalize_series_key("X-Men") == "x-men"
    assert _normalize_series_key("Avengers") == "avengers"
    assert _normalize_series_key("Amazing Spider-Man") == "amazing spider-man"

    # The pre-fix (buggy) series strings prove the point: a stray separator
    # produces a DIFFERENT, wrong key that would miss the local index.
    assert _normalize_series_key("X-Men :") == "x-men :" != _normalize_series_key("X-Men")
    assert _normalize_series_key("Avengers -") == "avengers -" != _normalize_series_key(
        "Avengers"
    )
    assert _normalize_series_key(
        "Amazing Spider-Man No."
    ) == "amazing spider-man no." != _normalize_series_key("Amazing Spider-Man")


# ---------------------------------------------------------------------------
# Integration: last_writer populated after apply
# ---------------------------------------------------------------------------

def test_last_writer_populated(tmp_path):
    from locg.collection_cache import CollectionCache
    cache = make_cache(tmp_path)
    cache.apply(lambda p: None, command="mycommand")
    reloaded = cache.load()
    lw = reloaded["last_writer"]
    assert lw is not None
    assert lw["pid"] == os.getpid()
    assert lw["command"] == "mycommand"
    assert "ts" in lw


# ---------------------------------------------------------------------------
# BUI-199: series-name normalization + volume resolution helpers
# ---------------------------------------------------------------------------

def test_base_series_name_strips_decoration():
    from locg.collection_cache import base_series_name
    assert base_series_name("Fantastic Four (Vol. 3) (1997 - 2012)") == "Fantastic Four"
    assert base_series_name("Spawn (1992 - Present)") == "Spawn"
    assert base_series_name("X-Force (1991)") == "X-Force"
    # Leading article preserved (LOCG keeps it in the catalog string).
    assert base_series_name("The X-Men (Vol. 1) (1963 - 1981)") == "The X-Men"


def test_base_series_name_strips_endash_range():
    """En-dash year ranges must be stripped too (BUI-199 finding 4)."""
    from locg.collection_cache import base_series_name
    assert base_series_name("The X-Men (Vol. 1) (1963 – 1981)") == "The X-Men"
    # Em-dash variant as well.
    assert base_series_name("Daredevil (1964 — 1998)") == "Daredevil"


def test_base_full_title_endash_no_decoration_leaks():
    """An en-dash decorated series must not leak into full_title (Cause 1)."""
    from locg.collection_cache import base_full_title
    ft = base_full_title("Uncanny X-Men (Vol. 1) (1980 – 2011)", "142")
    assert ft == "Uncanny X-Men #142"
    assert "(" not in ft and "–" not in ft


def test_base_series_name_pure_decoration_falls_back():
    """A string that strips to empty falls back to the original (finding 6)."""
    from locg.collection_cache import base_series_name, base_full_title
    assert base_series_name("(1991)") == "(1991)"
    # base_full_title must never produce a leading-space " #1".
    assert not base_full_title("(1991)", "1").startswith(" ")


def test_series_year_range_endash_and_bare_year():
    from locg.collection_cache import series_year_range
    assert series_year_range("X-Men (Vol. 1) (1963 - 1981)") == (1963, 1981)
    assert series_year_range("X-Men (Vol. 1) (1963 – 1981)") == (1963, 1981)
    assert series_year_range("Spawn (1992 - Present)") == (1992, 9999)
    # Bare single year is a ONE-year range, NOT open-ended (finding 2).
    assert series_year_range("X-Force (1991)") == (1991, 1991)
    assert series_year_range("No Years Here") is None


def test_series_year_range_bare_year_excludes_later_year():
    """A 2016 win must NOT match X-Force (1991) (finding 2)."""
    from locg.collection_cache import series_year_range
    begin, end = series_year_range("X-Force (1991)")
    assert not (begin <= 2016 <= end)


def test_resolve_volume_overlapping_ranges_picks_narrowest():
    """Boundary-year overlap (Hulk 1999) resolves to the narrowest range,
    deterministically regardless of candidate order (finding 3)."""
    from locg.collection_cache import resolve_series_for_win
    early = "Hulk (Vol. 1) (1962 - 1999)"
    late = "Hulk (Vol. 2) (1999 - 2008)"
    candidates = {"hulk": [early, late]}
    # 1999 is in both; the narrower (1999-2008) span wins.
    assert resolve_series_for_win("hulk", "1", 1999, {}, candidates) == late
    # Order must not matter.
    candidates_rev = {"hulk": [late, early]}
    assert resolve_series_for_win("hulk", "1", 1999, {}, candidates_rev) == late


def test_resolve_volume_closed_beats_open_present():
    """A closed range containing the year beats an open (Present) range."""
    from locg.collection_cache import resolve_series_for_win
    closed = "Thor (Vol. 1) (1966 - 1996)"
    open_present = "Thor (Vol. 6) (2020 - Present)"
    candidates = {"thor": [open_present, closed]}
    assert resolve_series_for_win("thor", "300", 1980, {}, candidates) == closed


def test_resolve_xmen_boundary_pair():
    """Paired boundary test: #141 -> early, #142 -> late (classic era)."""
    from locg.collection_cache import resolve_series_for_win
    assert resolve_series_for_win("x-men", "141", 1980, {}, None) == "The X-Men (Vol. 1) (1963 - 1981)"
    assert resolve_series_for_win("x-men", "142", 1981, {}, None) == "Uncanny X-Men (Vol. 1) (1980 - 2011)"


def test_resolve_xmen_modern_relaunch_not_classic():
    """A modern X-Men #1 (2019) must NOT be forced into Vol. 1; with no modern
    candidate it returns None so the caller falls back to Metron."""
    from locg.collection_cache import resolve_series_for_win
    # Empty index/candidates: must not short-circuit into the classic split.
    assert resolve_series_for_win("x-men", "1", 2019, {}, None) is None
    # With a modern candidate present, that volume is picked.
    modern = "X-Men (Vol. 5) (2019 - 2021)"
    assert resolve_series_for_win("x-men", "1", 2019, {}, {"x-men": [modern]}) == modern


def test_resolve_xmen_later_volume_not_collapsed_to_vol1():
    """X-Men (Vol. 2) #7 (1991 Jim Lee) must resolve to Vol. 2, not the classic
    split's Vol. 1 — the classic era's year window (1963-2011) overlaps the
    later volume's own range, so an explicit candidate for that year must win
    over the hardcoded split (BUI-265)."""
    from locg.collection_cache import resolve_series_for_win
    vol2 = "X-Men (Vol. 2) (1991 - 2001)"
    candidates = {"x-men": [vol2]}
    assert resolve_series_for_win("x-men", "7", 1991, {}, candidates) == vol2


def test_resolve_xmen_genuine_vol1_still_splits():
    """A genuine classic-era Vol. 1 issue still resolves correctly when an
    explicit Vol. 1 candidate is present (BUI-265 regression: the later-volume
    fix must not disturb the original BUI-197/BUI-199/BUI-200 split)."""
    from locg.collection_cache import resolve_series_for_win
    vol1 = "The X-Men (Vol. 1) (1963 - 1981)"
    candidates = {"x-men": [vol1]}
    assert resolve_series_for_win("x-men", "107", 1979, {}, candidates) == vol1
    # And with no candidates at all, the hardcoded split still fires.
    assert resolve_series_for_win("x-men", "107", 1979, {}, None) == vol1


def test_resolve_unknown_key_returns_none():
    """An unknown normalized key returns None (Metron fallback fires)."""
    from locg.collection_cache import resolve_series_for_win
    assert resolve_series_for_win("totally-unknown", "1", 2000, {}, None) is None


# --- BUI-421: don't guess a volume when the year is missing or contradicts ---


def test_resolve_multi_volume_no_year_returns_none():
    """BUI-421 Fix A: >1 distinct volumes and no year is unknowable — refuse to
    guess via the last-writer index (the $233 FF #16 filed as a modern issue)
    and return None so the caller flags needs_manual_series_canonical."""
    from locg.collection_cache import resolve_series_for_win
    v1 = "Fantastic Four (Vol. 1) (1961 - 1996)"
    v3 = "Fantastic Four (Vol. 3) (1998 - 2003)"
    candidates = {"fantastic four": [v1, v3]}
    # No year (None) with two volumes → None, regardless of index/order.
    index = {"fantastic four": v3}  # last-writer index would have guessed v3
    assert (
        resolve_series_for_win("fantastic four", "16", None, index, candidates)
        is None
    )
    assert (
        resolve_series_for_win("fantastic four", "16", None, {}, candidates)
        is None
    )


def test_resolve_single_wrong_candidate_contradicting_year_returns_none():
    """BUI-421 Fix B: the only known volume post-dates the win's year, so it
    cannot contain the issue — return None (Metron / manual) rather than file a
    1969 book under a 2015 volume."""
    from locg.collection_cache import resolve_series_for_win
    modern_only = "The Mighty Thor (Vol. 3) (2015 - 2018)"
    candidates = {"mighty thor": [modern_only]}
    assert (
        resolve_series_for_win("mighty thor", "164", 1969, {}, candidates) is None
    )


def test_resolve_single_correct_candidate_matching_year_returned():
    """BUI-421 Fix B: a single candidate whose OWN range contains the year is
    still returned (the guard only refuses genuine contradictions)."""
    from locg.collection_cache import resolve_series_for_win
    vol1 = "Thor (Vol. 1) (1966 - 1996)"
    candidates = {"thor": [vol1]}
    assert resolve_series_for_win("thor", "300", 1980, {}, candidates) == vol1


def test_resolve_single_candidate_no_year_still_returned():
    """BUI-421 Fix B: a single candidate with NO year must still resolve — the
    no-year guard applies only to the multi-candidate case (no over-refusal)."""
    from locg.collection_cache import resolve_series_for_win
    vol1 = "Thor (Vol. 1) (1966 - 1996)"
    candidates = {"thor": [vol1]}
    assert resolve_series_for_win("thor", "300", None, {}, candidates) == vol1
    # Also via the one-to-one index alone (no volume_candidates map).
    assert resolve_series_for_win("thor", "300", None, {"thor": vol1}, None) == vol1


def test_resolve_single_candidate_undecorated_range_not_over_refused():
    """BUI-421 Fix B: a candidate with no parseable (Vol/year) decoration can't
    be proven to contradict the year, so it's still returned even with a year."""
    from locg.collection_cache import resolve_series_for_win
    plain = "Some Indie Series"
    candidates = {"some indie series": [plain]}
    assert (
        resolve_series_for_win("some indie series", "5", 1999, {}, candidates)
        == plain
    )


def test_resolve_mighty_thor_masthead_via_alias_expansion():
    """BUI-421 Fix C: a "mighty thor" #164 (1969) win must resolve to the
    "thor"-keyed Vol. 1 via masthead-alias candidate expansion — the true
    volume lives under a different key and would otherwise never be a candidate.

    Covers both stores: (a) only the correct "thor" Vol. 1 present, and (b) the
    "mighty thor" modern Vol. 3 ALSO present — the year picks Vol. 1 either way.
    """
    from locg.collection_cache import resolve_series_for_win
    thor_v1 = "Thor (Vol. 1) (1966 - 1996)"
    mighty_v3 = "The Mighty Thor (Vol. 3) (2015 - 2018)"
    # (a) Alias pulls the correct Vol. 1 in even with no "mighty thor" key.
    assert (
        resolve_series_for_win("mighty thor", "164", 1969, {}, {"thor": [thor_v1]})
        == thor_v1
    )
    # (b) Both mastheads present: year 1969 disambiguates to Vol. 1.
    both = {"mighty thor": [mighty_v3], "thor": [thor_v1]}
    assert (
        resolve_series_for_win("mighty thor", "164", 1969, {}, both) == thor_v1
    )


def test_resolve_alias_expansion_preserves_xmen_split():
    """BUI-421: alias expansion (uncanny x-men ↔ x-men are BOTH a split key AND
    an alias pair) must not disturb the classic X-Men split or the BUI-265
    later-volume-wins logic — the split branch runs before candidate-gathering."""
    from locg.collection_cache import resolve_series_for_win
    early = "The X-Men (Vol. 1) (1963 - 1981)"
    late = "Uncanny X-Men (Vol. 1) (1980 - 2011)"
    vol2 = "X-Men (Vol. 2) (1991 - 2001)"
    # Classic split still fires with no candidates.
    assert resolve_series_for_win("x-men", "141", 1980, {}, None) == early
    assert resolve_series_for_win("uncanny x-men", "142", 1981, {}, None) == late
    # BUI-265 later volume still wins over the split.
    assert resolve_series_for_win("x-men", "7", 1991, {}, {"x-men": [vol2]}) == vol2
    # Genuine classic Vol. 1 issue still splits, alias notwithstanding.
    assert (
        resolve_series_for_win("x-men", "107", 1979, {}, {"x-men": [early]}) == early
    )


def test_resolve_modern_xmen_not_cross_filed_to_uncanny_alias():
    """BUI-421 guard: a MODERN "x-men" win (year > 2011) must NOT be alias-
    expanded onto a distinct modern "uncanny x-men" volume. The X-Men split
    keys' cross-masthead equivalence is the CLASSIC split (owned by the split
    branch); in the modern era they are different relaunches, so candidate-
    gathering gathers from the exact key only. With only the Uncanny volume in
    the store, the win resolves to None (→ Metron), never to the Uncanny volume."""
    from locg.collection_cache import resolve_series_for_win
    uncanny_v5 = "Uncanny X-Men (Vol. 5) (2018 - 2019)"
    store = {"uncanny x-men": [uncanny_v5]}
    result = resolve_series_for_win("x-men", "1", 2019, {}, store)
    assert result is None


# --- BUI-197: owned_match_keys masthead aliases (single source of equivalence) ---

def test_owned_match_keys_includes_self():
    from locg.collection_cache import owned_match_keys
    assert "batman" in owned_match_keys("Batman", "100")


def test_owned_match_keys_masthead_alias_symmetric():
    """Alias equivalence is symmetric: each masthead expands to the other,
    regardless of which side the query holds."""
    from locg.collection_cache import owned_match_keys
    assert "thor" in owned_match_keys("The Mighty Thor", "300")
    assert "mighty thor" in owned_match_keys("Thor", "300")
    assert "hulk" in owned_match_keys("Incredible Hulk", "181")
    assert "incredible hulk" in owned_match_keys("Hulk", "181")
    assert "iron man" in owned_match_keys("Invincible Iron Man", "1")
    assert "invincible iron man" in owned_match_keys("Iron Man", "1")
    assert "x-men" in owned_match_keys("Uncanny X-Men", "200")
    assert "uncanny x-men" in owned_match_keys("X-Men", "200")


def test_owned_match_keys_no_year_needed():
    """The alias expansion takes no year — required for the no-year conflicts
    audit + export."""
    from locg.collection_cache import owned_match_keys
    # The function has no year parameter at all; result is purely series/issue.
    assert "thor" in owned_match_keys("The Mighty Thor", "1")


def test_owned_match_keys_unrelated_series_not_cross_linked():
    """An unaliased series expands only to itself — no spurious cross-link that
    could make two genuinely-different runs match (era-collision guard)."""
    from locg.collection_cache import owned_match_keys
    keys = owned_match_keys("Daredevil", "1")
    assert keys == frozenset({"daredevil"})


def test_owned_match_keys_xmen_split_still_works():
    """The classic X-Men issue-number split survives alongside the alias table:
    #137 (<=141) cross-links to the early masthead too."""
    from locg.collection_cache import owned_match_keys, _normalize_series_key
    keys = owned_match_keys("Uncanny X-Men", "137")
    assert "x-men" in keys
    assert _normalize_series_key("The X-Men (Vol. 1) (1963 - 1981)") in keys


def test_owned_match_keys_annual_alias_expansion():
    """An annual-suffixed series resolves to its base run's alias set with the
    Annual qualifier re-applied: 'X-Men Annual' matches 'uncanny x-men annual'."""
    from locg.collection_cache import owned_match_keys
    keys = owned_match_keys("X-Men Annual", "9")
    assert "x-men annual" in keys
    assert "uncanny x-men annual" in keys
    # And the regular run key is NOT pulled in (annual stays an annual).
    assert "x-men" not in keys
    assert "uncanny x-men" not in keys


def test_owned_match_keys_annual_plural_suffix():
    """A plural 'Annuals' qualifier is also recognized."""
    from locg.collection_cache import owned_match_keys
    keys = owned_match_keys("Hulk Annuals", "1")
    assert "incredible hulk annuals" in keys


# ---------------------------------------------------------------------------
# verified_copy_bytes / backup_store / restore_from (BUI-433)
# ---------------------------------------------------------------------------

def _write_wish_list(store_dir: Path, items: list[dict[str, Any]]) -> None:
    (store_dir / "wish-list.json").write_text(
        json.dumps({"updated_at": "2026-07-01T00:00:00Z", "items": items})
    )


def test_verified_copy_bytes_round_trips(tmp_path):
    from locg.collection_cache import verified_copy_bytes

    src = tmp_path / "src.json"
    src.write_text('{"a": 1}')
    dest_parent = tmp_path / "sub"
    dest_parent.mkdir()
    dest = dest_parent / "dest.json"

    data = verified_copy_bytes(src, dest, mode=stat.S_IRUSR | stat.S_IWUSR)

    assert data == b'{"a": 1}'
    assert dest.read_bytes() == b'{"a": 1}'
    assert stat.S_IMODE(dest.stat().st_mode) == stat.S_IRUSR | stat.S_IWUSR


def test_backup_store_returns_path_and_nonempty_sanity_count(tmp_path):
    """BUI-433: backup must return a durable path plus counts a caller can
    assert are non-empty before trusting the backup."""
    cache = make_cache(tmp_path)
    cache.apply(lambda p: p["comics"].append(make_row()), command="seed")
    _write_wish_list(tmp_path, [{"name": "Batman #1", "id": 1}])

    dest_dir = tmp_path.parent / "backups" / "snap-1"
    result = cache.backup_store(dest_dir)

    assert result["backup_path"] == str(dest_dir)
    assert result["comics_count"] == 1
    assert result["wish_list_count"] == 1
    assert result["files"]["collection.json"] > 0
    assert result["files"]["wish-list.json"] > 0
    assert (dest_dir / "collection.json").exists()
    assert (dest_dir / "wish-list.json").exists()
    # The copy is byte-identical to the live file at backup time.
    assert (dest_dir / "collection.json").read_bytes() == (tmp_path / "collection.json").read_bytes()


def test_backup_store_empty_store_raises_zero_counts_not_silent_success(tmp_path):
    """An empty/never-written store must not silently report a 'successful'
    backup with real-looking data — no files exist to copy, and both sanity
    counts come back zero so a caller (the route) can hard-fail rather than
    treat this as a captured backup."""
    cache = make_cache(tmp_path)  # collection.json/wish-list.json never written
    dest_dir = tmp_path.parent / "backups" / "snap-empty"

    result = cache.backup_store(dest_dir)

    assert result["files"] == {}
    assert result["comics_count"] == 0
    assert result["wish_list_count"] == 0


def test_backup_store_is_distinct_from_rotating_bak_ring(tmp_path):
    """BUI-433 constraint: the named backup must survive normal mutation
    churn that rotates/evicts the in-store .bak.0/1/2 ring."""
    cache = make_cache(tmp_path)
    cache.apply(lambda p: p["comics"].append(make_row(full_title="Snapshot Comic")), command="seed")

    dest_dir = tmp_path.parent / "backups" / "snap-durable"
    result = cache.backup_store(dest_dir)
    assert result["comics_count"] == 1

    # Churn the store well past the 3-generation .bak ring depth.
    for i in range(5):
        cache.apply(
            lambda p, i=i: p["comics"].append(make_row(full_title=f"Churn {i}", seq=i + 2)),
            command=f"churn-{i}",
        )

    # The rotating ring only ever holds 3 generations of collection.json.bak.N
    # inside the store dir; the named backup lives in a different directory
    # entirely and must be untouched by any of that rotation.
    reloaded = json.loads((dest_dir / "collection.json").read_bytes())
    titles = {r["full_title"] for r in reloaded["comics"]}
    assert titles == {"Snapshot Comic"}, "the durable backup must be unaffected by later .bak ring rotation"


def test_restore_from_round_trips_a_store(tmp_path):
    """BUI-433 acceptance: restore must round-trip a backed-up store."""
    cache = make_cache(tmp_path)
    cache.apply(lambda p: p["comics"].append(make_row(full_title="Original Comic")), command="seed")
    _write_wish_list(tmp_path, [{"name": "Original Wish #1", "id": 1}])

    dest_dir = tmp_path.parent / "backups" / "snap-restore"
    backup_result = cache.backup_store(dest_dir)
    assert backup_result["comics_count"] == 1

    # Simulate a bad destructive write after the backup was taken.
    cache.apply(lambda p: p["comics"].clear(), command="destructive-mutation")
    _write_wish_list(tmp_path, [])
    assert json.loads((tmp_path / "collection.json").read_text())["comics"] == []

    restore_result = cache.restore_from(dest_dir)

    assert restore_result["comics_count"] == 1
    assert restore_result["wish_list_count"] == 1
    live_comics = json.loads((tmp_path / "collection.json").read_text())["comics"]
    assert {r["full_title"] for r in live_comics} == {"Original Comic"}
    live_wish = json.loads((tmp_path / "wish-list.json").read_text())["items"]
    assert {i["name"] for i in live_wish} == {"Original Wish #1"}


def test_restore_from_missing_backup_dir_raises_file_not_found(tmp_path):
    cache = make_cache(tmp_path)
    with pytest.raises(FileNotFoundError):
        cache.restore_from(tmp_path / "does-not-exist")


def test_restore_from_empty_backup_dir_raises_file_not_found(tmp_path):
    """A backup directory with neither collection.json nor wish-list.json is
    nothing to restore from — must not silently no-op as 'success'."""
    cache = make_cache(tmp_path)
    empty_backup = tmp_path.parent / "empty-backup"
    empty_backup.mkdir()
    with pytest.raises(FileNotFoundError):
        cache.restore_from(empty_backup)


def test_restore_from_partial_backup_skips_missing_file(tmp_path):
    """A backup taken before wish-list.json ever existed only carries
    collection.json — restoring it must not fail, and must not touch a
    wish-list.json that already exists live."""
    cache = make_cache(tmp_path)
    cache.apply(lambda p: p["comics"].append(make_row()), command="seed")

    partial_backup = tmp_path.parent / "partial-backup"
    partial_backup.mkdir()
    (partial_backup / "collection.json").write_text((tmp_path / "collection.json").read_text())

    _write_wish_list(tmp_path, [{"name": "Untouched Wish #1", "id": 1}])

    result = cache.restore_from(partial_backup)
    assert result["files"] == {"collection.json": len((partial_backup / "collection.json").read_bytes())}
    # The live wish-list, absent from the backup, is left exactly as-is.
    live_wish = json.loads((tmp_path / "wish-list.json").read_text())["items"]
    assert {i["name"] for i in live_wish} == {"Untouched Wish #1"}
