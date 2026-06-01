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


def _cache_dir() -> Path:
    """Return the cache directory, respecting XDG_CACHE_HOME."""
    base = os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache"))
    return Path(base) / "locg"


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
