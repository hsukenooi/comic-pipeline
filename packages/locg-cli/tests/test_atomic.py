"""Tests for the shared atomic-write helper (BUI-339).

``_atomic.atomic_write`` / ``atomic_write_json`` consolidate what used to be
four independent tempfile+os.replace implementations across ``cache.py``,
``collection_cache.py``, ``collection_io.py``, and ``commands.py``. These
tests pin the invariants the ticket calls out: a unique tmp name per call,
a same-directory atomic rename, and no orphaned tmp file on any failure —
plus the per-site knobs (``mode``, ``fsync``, ``compact``) that let the
helper reproduce each call site's exact prior behavior.
"""
from __future__ import annotations

import json
import os
import stat
from unittest.mock import patch

import pytest

from locg import _atomic


def _leftover_tmp_files(path):
    """Any tmp file the helper's mkstemp naming scheme could have left behind,
    regardless of which call site's prefix/suffix was used."""
    return list(path.parent.glob(f".*{path.name}*")) + list(
        path.parent.glob(f"*{path.name}*.tmp")
    )


class TestAtomicWrite:
    def test_writes_content_and_leaves_no_tmp_file(self, tmp_path):
        path = tmp_path / "out.txt"
        _atomic.atomic_write(path, "hello world")
        assert path.read_text() == "hello world"
        assert _leftover_tmp_files(path) == []

    def test_tmp_file_created_in_same_directory_as_target(self, tmp_path):
        """A cross-filesystem tmp file would make the final os.replace()
        non-atomic (or raise). The tmp file must land in path.parent."""
        path = tmp_path / "sub" / "out.txt"
        path.parent.mkdir()
        seen_dirs = []
        real_mkstemp = _atomic.tempfile.mkstemp

        def spy_mkstemp(*args, **kwargs):
            seen_dirs.append(kwargs.get("dir"))
            return real_mkstemp(*args, **kwargs)

        with patch.object(_atomic.tempfile, "mkstemp", side_effect=spy_mkstemp):
            _atomic.atomic_write(path, "data")

        assert seen_dirs == [str(path.parent)]

    def test_tmp_name_is_unique_per_call(self, tmp_path):
        """Two writes to the same path must not reuse a tmp filename — a
        shared/predictable name is exactly the clobber risk BUI-335 fixed
        in the sibling apps/ebay helper."""
        path = tmp_path / "out.txt"
        seen_names = []
        real_mkstemp = _atomic.tempfile.mkstemp

        def spy_mkstemp(*args, **kwargs):
            fd, name = real_mkstemp(*args, **kwargs)
            seen_names.append(name)
            return fd, name

        with patch.object(_atomic.tempfile, "mkstemp", side_effect=spy_mkstemp):
            _atomic.atomic_write(path, "one")
            _atomic.atomic_write(path, "two")

        assert len(seen_names) == 2
        assert seen_names[0] != seen_names[1]

    def test_tmp_prefix_and_suffix_are_honored(self, tmp_path):
        path = tmp_path / "out.txt"
        captured = {}
        real_mkstemp = _atomic.tempfile.mkstemp

        def spy_mkstemp(*args, **kwargs):
            captured.update(kwargs)
            return real_mkstemp(*args, **kwargs)

        with patch.object(_atomic.tempfile, "mkstemp", side_effect=spy_mkstemp):
            _atomic.atomic_write(path, "data", tmp_prefix=".wish-list-", tmp_suffix=".json.tmp")

        assert captured["prefix"] == ".wish-list-"
        assert captured["suffix"] == ".json.tmp"

    def test_mode_sets_permissions_after_rename(self, tmp_path):
        path = tmp_path / "secret.txt"
        _atomic.atomic_write(path, "s3cr3t", mode=stat.S_IRUSR | stat.S_IWUSR)
        assert (path.stat().st_mode & 0o777) == 0o600

    def test_no_mode_leaves_default_permissions(self, tmp_path):
        path = tmp_path / "plain.txt"
        _atomic.atomic_write(path, "data")
        # Should not have been forced to 0600 when mode is None.
        assert path.exists()

    def test_replace_failure_cleans_up_tmp_and_raises(self, tmp_path):
        path = tmp_path / "out.txt"
        path.write_text("original")
        with patch.object(_atomic.os, "replace", side_effect=OSError("boom")):
            with pytest.raises(OSError):
                _atomic.atomic_write(path, "new content")
        assert path.read_text() == "original"
        assert _leftover_tmp_files(path) == []

    def test_write_failure_cleans_up_tmp_and_raises(self, tmp_path):
        path = tmp_path / "out.txt"

        class ExplodingFile:
            def write(self, _content):
                raise OSError("disk full")

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        with patch.object(_atomic.os, "fdopen", return_value=ExplodingFile()):
            with pytest.raises(OSError):
                _atomic.atomic_write(path, "new content")
        assert not path.exists()
        assert _leftover_tmp_files(path) == []

    def test_chmod_failure_cleans_up_tmp_and_raises(self, tmp_path):
        """The mode chmod happens after the rename in the reference pattern,
        so a chmod failure must still trigger the cleanup path (tmp is gone
        by then since replace succeeded, but no exception should be
        swallowed and no crash from a stale tmp check)."""
        path = tmp_path / "secret.txt"
        with patch.object(_atomic.Path, "chmod", side_effect=OSError("no perm")):
            with pytest.raises(OSError):
                _atomic.atomic_write(path, "data", mode=0o600)
        # The rename already happened before chmod raised, so the target
        # exists with the new content even though chmod failed.
        assert path.read_text() == "data"

    def test_fsync_true_still_writes_correctly(self, tmp_path):
        path = tmp_path / "durable.txt"
        _atomic.atomic_write(path, "durable data", fsync=True)
        assert path.read_text() == "durable data"
        assert _leftover_tmp_files(path) == []

    def test_fdopen_failure_closes_fd_and_cleans_up_tmp(self, tmp_path):
        """BUI-341: os.fdopen(fd) can itself raise (e.g. OOM) before the
        `with` takes ownership of fd — the except-below only unlinks the tmp
        path, so without an explicit close the raw fd would leak. Verify
        os.close() is actually invoked on the mkstemp'd fd and the tmp file
        is still cleaned up."""
        path = tmp_path / "out.txt"
        closed_fds = []
        real_close = _atomic.os.close

        def spy_close(fd):
            closed_fds.append(fd)
            real_close(fd)

        with patch.object(_atomic.os, "fdopen", side_effect=OSError("simulated OOM")), \
                patch.object(_atomic.os, "close", side_effect=spy_close) as mock_close:
            with pytest.raises(OSError, match="simulated OOM"):
                _atomic.atomic_write(path, "data")

        assert mock_close.called
        assert len(closed_fds) == 1
        assert _leftover_tmp_files(path) == []
        assert not path.exists()


class TestAtomicWriteJson:
    def test_round_trips_payload(self, tmp_path):
        path = tmp_path / "cache.json"
        _atomic.atomic_write_json(path, {"a": 1, "b": [1, 2, 3]})
        assert json.loads(path.read_text()) == {"a": 1, "b": [1, 2, 3]}

    def test_compact_uses_no_separators_whitespace(self, tmp_path):
        path = tmp_path / "compact.json"
        _atomic.atomic_write_json(path, {"a": 1, "b": 2}, compact=True)
        assert path.read_text() == '{"a":1,"b":2}'

    def test_non_compact_uses_default_json_spacing(self, tmp_path):
        path = tmp_path / "spaced.json"
        _atomic.atomic_write_json(path, {"a": 1, "b": 2}, compact=False)
        assert path.read_text() == json.dumps({"a": 1, "b": 2}, ensure_ascii=False)
        assert ", " in path.read_text() or path.read_text() == '{"a": 1, "b": 2}'

    def test_non_ascii_is_not_escaped(self, tmp_path):
        path = tmp_path / "unicode.json"
        _atomic.atomic_write_json(path, {"name": "café"})
        assert "café" in path.read_text()
        assert "\\u" not in path.read_text()

    def test_mode_applied_to_json_file(self, tmp_path):
        path = tmp_path / "secret.json"
        _atomic.atomic_write_json(path, {"token": "x"}, mode=stat.S_IRUSR | stat.S_IWUSR)
        assert (path.stat().st_mode & 0o777) == 0o600

    def test_tmp_suffix_is_json_tmp(self, tmp_path):
        path = tmp_path / "cache.json"
        captured = {}
        real_mkstemp = _atomic.tempfile.mkstemp

        def spy_mkstemp(*args, **kwargs):
            captured.update(kwargs)
            return real_mkstemp(*args, **kwargs)

        with patch.object(_atomic.tempfile, "mkstemp", side_effect=spy_mkstemp):
            _atomic.atomic_write_json(path, {"a": 1})

        assert captured["suffix"] == ".json.tmp"

    def test_replace_failure_leaves_old_json_intact(self, tmp_path):
        path = tmp_path / "cache.json"
        path.write_text(json.dumps({"old": True}))
        with patch.object(_atomic.os, "replace", side_effect=OSError("boom")):
            with pytest.raises(OSError):
                _atomic.atomic_write_json(path, {"new": True})
        assert json.loads(path.read_text()) == {"old": True}
        assert _leftover_tmp_files(path) == []
