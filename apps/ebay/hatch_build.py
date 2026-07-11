"""Hatch build hook (BUI-314): stamp the wheel with the git SHA/date it was
built from.

Verified this resolves the real repo HEAD under `uv tool install
"$REPO_ROOT/apps/ebay"` (what scripts/install.sh runs): that local-path install
calls `build_wheel()` directly against the working tree, with no intermediate
sdist copy — unlike a registry-style `uv build`, which round-trips through an
isolated sdist extraction with no `.git`. `_git()` degrades to "unknown" for
that case (and for a missing `git` binary) rather than trusting a `.git` that
may not exist.

The stamp is written as a generated module and merged into the wheel via
`force_include` rather than into `src/`, so nothing generated lands in the
repo tree.
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

        stamp_dir = Path(tempfile.mkdtemp(prefix="ebay-build-stamp-"))
        stamp_file = stamp_dir / "_ebay_build_stamp.py"
        stamp_file.write_text(f"GIT_SHA = {sha!r}\nGIT_DATE = {date!r}\n")

        build_data.setdefault("force_include", {})[str(stamp_file)] = "_ebay_build_stamp.py"

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
