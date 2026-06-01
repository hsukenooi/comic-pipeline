"""Tests for locg.config module."""
from __future__ import annotations

import json
from pathlib import Path

from locg.config import (
    cookie_path,
    ensure_config_dir,
    load_config,
    save_config,
    wish_list_cache_path,
)


def test_ensure_config_dir_creates_and_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    d = ensure_config_dir()
    assert d.exists()
    assert d == tmp_path / "locg"
    # Second call is idempotent
    d2 = ensure_config_dir()
    assert d2 == d
    assert d2.exists()


def test_config_load_save_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    data = {"username": "testuser", "theme": "dark"}
    save_config(data)
    loaded = load_config()
    assert loaded == data


def test_missing_config_returns_empty_dict(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    result = load_config()
    assert result == {}


def test_cookie_path_resolves(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    p = cookie_path()
    assert p == tmp_path / "locg" / "cookies.json"
    assert p.name == "cookies.json"


def test_wish_list_cache_path_default(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    p = wish_list_cache_path()
    assert p == tmp_path / "locg" / "wish-list.json"
    assert p.name == "wish-list.json"


def test_wish_list_cache_path_respects_xdg_cache_home(tmp_path, monkeypatch):
    custom_cache = tmp_path / "custom-cache"
    monkeypatch.setenv("XDG_CACHE_HOME", str(custom_cache))
    p = wish_list_cache_path()
    assert p == custom_cache / "locg" / "wish-list.json"
