"""Hatch build hook (BUI-612): stamp the wheel with the git SHA/date it was
built from — mirrors the git-stamp hook used by apps/ebay's hatch_build.py
(BUI-314) and apps/fmv's hatch_build.py (BUI-305).

Verified this resolves the real repo HEAD under `uv tool install --editable
"$REPO_ROOT/packages/locg-cli"` (what scripts/install.sh and
scripts/deploy.sh run): that local-path install calls `build_wheel()`
directly against the working tree, with no intermediate sdist copy — unlike
a registry-style `uv build`, which round-trips through an isolated sdist
extraction with no `.git`. `_git()` degrades to "unknown" for that case
(and for a missing `git` binary) rather than trusting a `.git` that may not
exist.

Deliberately a bare top-level `_build_info.py`, NOT nested inside the
`locg` package (e.g. `locg/_build_info.py`), even though locg ships as a
real package (`src/locg`) unlike apps/ebay|fmv's flat top-level modules.
Verified by hand: under `uv tool install --editable` (what scripts/install.sh
and scripts/deploy.sh actually run for this package), setuptools-style
editable redirects aren't in play here — hatchling's own editable install
still materializes `force_include`d files as real copies in site-packages —
but a file force_included *inside* the `locg/` package directory is
invisible: the editable `.pth` points `sys.path` straight at `src/locg`
(a real package with `__init__.py`), which wins package resolution over the
namespace-only `locg/` stub hatchling drops in site-packages, so anything
force_included under `locg/` in that stub never becomes reachable as
`locg.<name>`. A bare top-level module has no such competing package to
lose to, so it stays reachable either way. Written via `force_include` from
a tempdir, so nothing generated lands in `src/`.
"""
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class GitStampBuildHook(BuildHookInterface):
    PLUGIN_NAME = "git-stamp"

    def initialize(self, version: str, build_data: dict) -> None:
        sha = self._git("rev-parse", "--short", "HEAD")
        date = self._git("log", "-1", "--format=%cd", "--date=short")

        stamp_dir = Path(tempfile.mkdtemp(prefix="locg-build-stamp-"))
        stamp_file = stamp_dir / "_build_info.py"
        stamp_file.write_text(f"GIT_SHA = {sha!r}\nGIT_DATE = {date!r}\n")

        build_data.setdefault("force_include", {})[str(stamp_file)] = "_build_info.py"

    def _git(self, *args: str) -> str:
        try:
            output = subprocess.check_output(
                ["git", *args],
                cwd=self.root,
                text=True,
                stderr=subprocess.DEVNULL,
            ).strip()
            return output or "unknown"
        except (OSError, subprocess.CalledProcessError):
            return "unknown"
