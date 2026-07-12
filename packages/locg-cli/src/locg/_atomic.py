"""Shared atomic-write helper for locg-cli's on-disk caches (BUI-339).

Before this module existed, ``cache.py``, ``collection_cache.py``,
``collection_io.py``, and ``commands.py`` each hand-rolled their own
``tempfile.mkstemp()`` + ``os.replace()`` dance. They agreed on the shape
(unique tmp name in the same directory, atomic rename, best-effort cleanup
of the tmp file on failure) but nothing enforced that agreement, so the
four copies could drift independently — the failure mode a sibling package
hit in BUI-335/BUI-336 (``apps/ebay``'s ``atomic_write_json()``, which this
module mirrors).

Every site's exact existing behavior (JSON compactness, ``fsync``, file
mode, tmp-file naming) is preserved via keyword arguments rather than
homogenized — see each call site for which knobs it passes.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Optional


def atomic_write(
    path: Path,
    content: str,
    *,
    mode: Optional[int] = None,
    fsync: bool = False,
    tmp_prefix: str = ".tmp-",
    tmp_suffix: str = ".tmp",
) -> None:
    """Atomically replace ``path`` with ``content``.

    Writes to a uniquely-named temp file in ``path``'s own directory (via
    ``tempfile.mkstemp``, so the name can't collide across concurrent
    callers), then ``os.replace()``s it into place — a same-filesystem
    rename is atomic, so readers never observe a partially-written file.

    If anything fails — the write, the rename, the optional fsync, or the
    optional chmod — the temp file is best-effort unlinked before the
    exception propagates, so a failed write never leaves an orphaned
    ``.tmp`` file behind.

    :param mode: if given, ``path.chmod(mode)`` after the rename (e.g.
        ``stat.S_IRUSR | stat.S_IWUSR`` for 0600 on files holding session
        state or backups).
    :param fsync: if True, fsync the file descriptor before the rename and
        fsync the parent directory after it, so the write is durable
        against a crash immediately following. Off by default — most
        callers here are best-effort caches, not commit logs.
    :param tmp_prefix: temp-file prefix, kept distinct per call site so an
        orphaned leftover (should the cleanup itself ever fail) is
        traceable to its origin.
    :param tmp_suffix: temp-file suffix; callers writing JSON pass
        ``.json.tmp`` for a more descriptive leftover name.
    """
    fd, tmp_name = tempfile.mkstemp(prefix=tmp_prefix, suffix=tmp_suffix, dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
            if fsync:
                f.flush()
                os.fsync(f.fileno())
        os.replace(tmp_name, path)
        if fsync:
            dir_fd = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        if mode is not None:
            path.chmod(mode)
    except Exception:
        # Best-effort cleanup so a failed write leaves no orphaned tmp file.
        # unlink directly rather than pre-checking existence: after a
        # successful os.replace() the tmp is already gone, and the missing
        # file just raises FileNotFoundError (an OSError) which we swallow.
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def atomic_write_json(
    path: Path,
    payload: Any,
    *,
    mode: Optional[int] = None,
    fsync: bool = False,
    compact: bool = False,
    tmp_prefix: str = ".tmp-",
) -> None:
    """:func:`atomic_write` for a JSON-serializable ``payload``.

    :param compact: if True, serialize with ``separators=(",", ":")``
        (no spaces) instead of ``json``'s default ``", "``/``": "``
        separators. Some callers here compact (small cache files written
        often); others keep the default spacing (human-readable diffs on
        the wish-list/collection caches). Preserve whichever each existing
        site used.
    """
    content = json.dumps(
        payload,
        separators=(",", ":") if compact else None,
        ensure_ascii=False,
    )
    atomic_write(
        path,
        content,
        mode=mode,
        fsync=fsync,
        tmp_prefix=tmp_prefix,
        tmp_suffix=".json.tmp",
    )
