"""Configuration and credential management for locg."""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any


def _config_dir() -> Path:
    """Return the config directory, respecting XDG_CONFIG_HOME."""
    base = os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config"))
    return Path(base) / "locg"


def ensure_config_dir() -> Path:
    """Create the config directory if it doesn't exist. Returns the path."""
    d = _config_dir()
    if not d.exists():
        d.mkdir(parents=True)
        d.chmod(stat.S_IRWXU)  # 700
    return d


def config_path() -> Path:
    return _config_dir() / "config.json"


def cookie_path() -> Path:
    return _config_dir() / "cookies.json"


def playwright_profile_dir() -> Path:
    return _config_dir() / "playwright-profile"


def env_path() -> Path:
    return _config_dir() / ".env"


def _find_repo_root() -> Path | None:
    """Walk up from this file to the comic-pipeline repo root.

    `locg` is installed ``--editable`` (see ``scripts/install.sh``), so this
    module's source stays in the repo tree and ``__file__`` resolves inside the
    checkout regardless of the caller's cwd. The root is the first ancestor that
    has both a ``.git`` entry (a directory in a normal clone, a *file* in a
    Conductor worktree — ``exists()`` covers both) and a top-level
    ``pyproject.toml``. Requiring both avoids stopping at a nested package
    ``pyproject.toml`` (e.g. ``packages/locg-cli/``). Returns ``None`` for a
    non-editable / wheel install whose source lives outside any repo.
    """
    for parent in Path(__file__).resolve().parents:
        if (parent / ".git").exists() and (parent / "pyproject.toml").exists():
            return parent
    return None


def _cache_dir() -> Path:
    """Return the LOCG cache directory.

    Precedence:
      1. ``LOCG_DATA_DIR`` env override — used verbatim (whitespace-stripped;
         blank/whitespace-only is ignored, not treated as ``.``).
      2. ``<repo_root>/data/locg`` when running from an editable repo checkout.
      3. ``~/.cache/locg`` fallback for non-editable / wheel installs.
    """
    override = os.environ.get("LOCG_DATA_DIR", "").strip()
    if override:
        return Path(override)
    root = _find_repo_root()
    if root is not None:
        return root / "data" / "locg"
    return Path(os.path.expanduser("~/.cache")) / "locg"


def collection_cache_path() -> Path:
    return _cache_dir() / "collection.json"


def wish_list_cache_path() -> Path:
    return _cache_dir() / "wish-list.json"


def import_history_path() -> Path:
    return _cache_dir() / "import-history.jsonl"


def load_config() -> dict[str, Any]:
    p = config_path()
    if p.exists():
        with open(p) as f:
            return json.load(f)
    return {}


def save_config(data: dict[str, Any]) -> None:
    ensure_config_dir()
    p = config_path()
    with open(p, "w") as f:
        json.dump(data, f, indent=2)
    p.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 600
