"""BUI-114: validate the uvicorn --log-config shipped at server/log_config.json.

The config is consumed by uvicorn via logging.config.dictConfig at server
startup. These tests guard that it stays well-formed, timestamps every line,
and keeps stamping the app loggers — so a stray edit can't silently revert us
to timestamp-less logs (the exact gap that made BUI-115 hard to diagnose).
"""

import json
import logging.config
from pathlib import Path

import pytest

CONFIG_PATH = Path(__file__).resolve().parent.parent / "server" / "log_config.json"


@pytest.fixture
def config():
    return json.loads(CONFIG_PATH.read_text())


def test_log_config_is_valid_json():
    assert CONFIG_PATH.exists()
    json.loads(CONFIG_PATH.read_text())  # raises on malformed JSON


def test_every_formatter_includes_a_timestamp(config):
    for name, fmt in config["formatters"].items():
        spec = fmt.get("fmt") or fmt.get("format") or ""
        assert "%(asctime)s" in spec, f"formatter {name!r} has no timestamp"


def test_app_loggers_are_stamped(config):
    # The app loggers whose tracebacks/warnings we need stamped (BUI-114).
    for name in ("gixen_client", "server"):
        assert name in config["loggers"], f"{name} logger not configured"
        assert "app" in config["loggers"][name]["handlers"]


def test_uvicorn_access_and_default_present(config):
    for name in ("uvicorn", "uvicorn.access"):
        assert name in config["loggers"]


def test_handlers_and_loggers_cross_reference(config):
    """Every handler points at a defined formatter, and every logger points at
    defined handlers — the cross-references dictConfig would reject at runtime.
    (Checked structurally rather than by applying dictConfig, so the test never
    mutates the process-wide logging state that caplog tests depend on.)"""
    formatters = set(config["formatters"])
    handlers = config["handlers"]
    for hname, h in handlers.items():
        assert h["formatter"] in formatters, f"handler {hname!r} → unknown formatter"
    for lname, l in config["loggers"].items():
        for h in l.get("handlers", []):
            assert h in handlers, f"logger {lname!r} → unknown handler {h!r}"


def test_dictconfig_applies_cleanly():
    """uvicorn applies this via logging.config.dictConfig at startup — confirm
    it loads without raising (catches bad formatter factories / handler classes),
    restoring the touched loggers afterward so no global state leaks to caplog."""
    config = json.loads(CONFIG_PATH.read_text())
    touched = ("gixen_client", "server")
    saved = {n: (logging.getLogger(n).propagate,
                 list(logging.getLogger(n).handlers)) for n in touched}
    try:
        logging.config.dictConfig(config)
    finally:
        for n, (propagate, handlers) in saved.items():
            lg = logging.getLogger(n)
            lg.propagate = propagate
            lg.handlers = handlers
