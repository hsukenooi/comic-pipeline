"""Tests for the cache-directory resolver and its precedence order.

These exercise the resolver internals (``locg.config._cache_dir`` /
``_find_repo_root``) directly rather than the public path functions, so they are
independent of the autouse cache-isolation fixtures in ``conftest.py`` (which
patch the public functions). Precedence under test (see ``config._cache_dir``):

  1. ``LOCG_DATA_DIR`` env override
  2. ``<repo_root>/data/locg`` (repo root found via marker walk)
  3. ``~/.cache/locg`` fallback when no repo root is found
"""
from __future__ import annotations

from pathlib import Path

import locg.cache as cache_mod
import locg.config as config_mod
from locg.config import _cache_dir, _find_repo_root


def test_data_dir_override_wins_over_repo_root(monkeypatch, tmp_path):
    # Override is honored verbatim even when a real repo root exists up-tree.
    monkeypatch.setenv("LOCG_DATA_DIR", str(tmp_path / "override"))
    assert _cache_dir() == tmp_path / "override"


def test_whitespace_only_data_dir_is_ignored(monkeypatch, tmp_path):
    # A blank/whitespace LOCG_DATA_DIR (e.g. `export LOCG_DATA_DIR="$DIR "`
    # typo) must NOT resolve to a cwd-relative junk dir; it falls through.
    root = tmp_path / "repo"
    monkeypatch.setattr(config_mod, "_find_repo_root", lambda: root)
    monkeypatch.setenv("LOCG_DATA_DIR", "   ")
    assert _cache_dir() == root / "data" / "locg"


def test_repo_root_resolves_to_data_locg(monkeypatch, tmp_path):
    monkeypatch.delenv("LOCG_DATA_DIR", raising=False)
    root = tmp_path / "repo"
    (root / "data").mkdir(parents=True)
    (root / ".git").mkdir()
    (root / "pyproject.toml").write_text("[project]\nname='x'\n")
    monkeypatch.setattr(config_mod, "_find_repo_root", lambda: root)
    assert _cache_dir() == root / "data" / "locg"


def test_find_repo_root_skips_nested_pyproject(monkeypatch, tmp_path):
    # Walks ancestors to the true root AND does not stop at a nested
    # pyproject.toml that lacks a .git sibling (subsumes the plain-walk case).
    root = tmp_path / "repo"
    pkg = root / "packages" / "locg-cli"
    src = pkg / "src" / "locg"
    src.mkdir(parents=True)
    (root / ".git").mkdir()
    (root / "pyproject.toml").write_text("[project]\nname='root'\n")
    (pkg / "pyproject.toml").write_text("[project]\nname='locg'\n")  # no .git here
    fake_file = src / "config.py"
    fake_file.write_text("# fake")
    monkeypatch.setattr(config_mod, "__file__", str(fake_file))
    assert _find_repo_root() == root


def test_find_repo_root_git_as_file_worktree(monkeypatch, tmp_path):
    # Conductor worktrees have a .git *file*, not a directory — exists() covers it.
    root = tmp_path / "worktree"
    src = root / "packages" / "locg-cli" / "src" / "locg"
    src.mkdir(parents=True)
    (root / ".git").write_text("gitdir: /somewhere/.git/worktrees/wt\n")
    (root / "pyproject.toml").write_text("[project]\nname='root'\n")
    fake_file = src / "config.py"
    fake_file.write_text("# fake")
    monkeypatch.setattr(config_mod, "__file__", str(fake_file))
    assert _find_repo_root() == root


def test_falls_back_to_home_cache_when_no_repo(monkeypatch, tmp_path):
    monkeypatch.delenv("LOCG_DATA_DIR", raising=False)
    monkeypatch.setattr(config_mod, "_find_repo_root", lambda: None)
    monkeypatch.setattr("os.path.expanduser", lambda p: str(tmp_path / "home"))
    assert _cache_dir() == tmp_path / "home" / "locg"


def test_cache_and_config_share_one_resolver():
    # Single source of truth: cache.py resolves the directory via the very same
    # function object config.py defines — no second copy can drift.
    assert cache_mod._cache_dir is config_mod._cache_dir
