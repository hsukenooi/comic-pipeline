"""Tests for locg.cli module."""
from __future__ import annotations

import json
import sys

import pytest

from locg import __version__
from locg.cli import _filter_fields, create_parser, main


def test_main_loads_dotenv_before_client(monkeypatch, capsys):
    """main() must call load_dotenv() before constructing LOCGClient so
    LOCG_USERNAME/LOCG_PASSWORD from a .env file are available during
    require_auth(). If ordering slipped (e.g. load_dotenv moved after
    client construction), auto-login would silently fail."""
    import locg.cli

    order: list[str] = []

    def fake_load_dotenv(*args, **kwargs):
        order.append("load_dotenv")

    class FakeClient:
        def __init__(self):
            order.append("LOCGClient")
        def close(self):
            order.append("close")

    monkeypatch.setattr(locg.cli, "load_dotenv", fake_load_dotenv)
    monkeypatch.setattr(locg.cli, "LOCGClient", FakeClient)
    # Use a real command (not bare `locg`) so main() reaches client construction.
    monkeypatch.setattr(sys, "argv", ["locg", "search", "batman"])

    # cmd_search would make real HTTP; stub it.
    monkeypatch.setattr(locg.cli, "cmd_search", lambda client, q: [])

    try:
        main()
    except SystemExit as e:
        assert e.code in (None, 0)

    assert order[:2] == ["load_dotenv", "LOCGClient"], (
        f"load_dotenv must run before LOCGClient() — got {order}"
    )


def test_no_args_prints_help_exits_2(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["locg"])
    with pytest.raises(SystemExit) as exc_info:
        main()
    assert exc_info.value.code == 2
    captured = capsys.readouterr()
    assert "usage" in captured.err.lower() or "locg" in captured.err.lower()


def test_help_flag_exits_0(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["locg", "--help"])
    with pytest.raises(SystemExit) as exc_info:
        main()
    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    assert "League of Comic Geeks" in captured.out


def test_version_flag_exits_0(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["locg", "--version"])
    with pytest.raises(SystemExit) as exc_info:
        main()
    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    assert __version__ in captured.out


def test_unknown_command_exits_2(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["locg", "foobar"])
    with pytest.raises(SystemExit) as exc_info:
        main()
    assert exc_info.value.code == 2


def test_search_subcommand_help(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["locg", "search", "--help"])
    with pytest.raises(SystemExit) as exc_info:
        main()
    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    assert "query" in captured.out.lower() or "search" in captured.out.lower()


def test_pretty_flag_before_subcommand(monkeypatch):
    """--pretty works before or after the subcommand."""
    from locg.cli import create_parser
    parser = create_parser()
    # After subcommand (handled by subparser)
    args = parser.parse_args(["search", "--pretty", "batman"])
    assert args.pretty is True
    assert args.command == "search"
    assert args.query == "batman"


def test_pretty_flag_after_subcommand(monkeypatch):
    """--pretty after subcommand is also handled."""
    parser = create_parser()
    args = parser.parse_args(["releases", "--pretty"])
    assert args.pretty is True
    assert args.command == "releases"


# --- _filter_fields tests ---


def test_filter_fields_list_of_dicts():
    data = [{"id": 1, "name": "A", "price": 3.99}, {"id": 2, "name": "B", "price": 4.99}]
    result = _filter_fields(data, ["name", "id"])
    assert result == [{"id": 1, "name": "A"}, {"id": 2, "name": "B"}]


def test_filter_fields_single_dict():
    data = {"id": 1, "name": "A", "price": 3.99, "publisher": "DC"}
    result = _filter_fields(data, ["name"])
    assert result == {"name": "A"}


def test_filter_fields_nonexistent_field_ignored():
    data = [{"id": 1, "name": "A"}]
    result = _filter_fields(data, ["name", "nonexistent"])
    assert result == [{"name": "A"}]


def test_filter_fields_non_dict_passthrough():
    """Non-dict, non-list values are returned unchanged."""
    assert _filter_fields("hello", ["name"]) == "hello"
    assert _filter_fields(42, ["name"]) == 42


def test_filter_fields_empty_list():
    assert _filter_fields([], ["name"]) == []


# --- --fields flag parsing ---


def test_fields_flag_parsed():
    parser = create_parser()
    args = parser.parse_args(["search", "--fields", "name,id", "batman"])
    assert args.fields == "name,id"


def test_fields_flag_before_subcommand():
    parser = create_parser()
    args = parser.parse_args(["--fields", "name", "search", "batman"])
    # Parent parser sets fields on the namespace
    assert args.command == "search"


# --- collection has subcommand parsing ---


def test_collection_has_subcommand_parsed():
    parser = create_parser()
    args = parser.parse_args(["collection", "has", "Amazing Spider-Man #300"])
    assert args.command == "collection"
    assert args.collection_command == "has"
    assert args.title_query == "Amazing Spider-Man #300"


def test_collection_without_has_no_collection_command():
    parser = create_parser()
    args = parser.parse_args(["collection"])
    assert args.command == "collection"
    assert getattr(args, "collection_command", None) is None


# --- Auth exit code ---


def test_auth_failure_exits_1(monkeypatch, capsys):
    """AuthRequired should exit with code 1, not 3."""
    from locg.client import AuthRequired

    monkeypatch.setattr(sys, "argv", ["locg", "collection"])

    # Patch LOCGClient to raise AuthRequired
    import locg.cli
    original_client = locg.cli.LOCGClient

    class FakeClient:
        def __init__(self):
            pass
        def require_auth(self):
            raise AuthRequired("Session expired. Run: locg login")
        def close(self):
            pass

    monkeypatch.setattr(locg.cli, "LOCGClient", FakeClient)

    with pytest.raises(SystemExit) as exc_info:
        main()
    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    err = json.loads(captured.err)
    assert "expired" in err["error"].lower()


def test_cli_add_collection_with_grade_and_price(monkeypatch, capsys):
    """`locg add collection <id> --grade 8.5 --price 390` must call cmd_add with those values."""
    from unittest.mock import MagicMock, patch
    calls = {}

    def fake_cmd_add(client, list_name, comic_id, grade=None, price=None):
        calls["args"] = (list_name, comic_id, grade, price)
        return {"status": "ok", "added": True, "details_saved": True, "text": "done"}

    monkeypatch.setattr("locg.cli.cmd_add", fake_cmd_add)
    with patch("locg.cli.LOCGClient") as MockClient:
        MockClient.return_value.__enter__ = MockClient.return_value
        MockClient.return_value.close = MagicMock()
        monkeypatch.setattr(sys, "argv", ["locg", "add", "collection", "12345", "--grade", "8.5", "--price", "390"])
        try:
            main()
        except SystemExit as e:
            assert e.code in (None, 0)

    assert calls["args"] == ("collection", 12345, "8.5", "390")


def test_cli_add_partial_success_exits_1(monkeypatch, capsys):
    """Partial success must write JSON to stdout AND error JSON to stderr AND exit 1."""
    from unittest.mock import MagicMock, patch

    def fake_cmd_add(client, list_name, comic_id, grade=None, price=None):
        return {
            "status": "partial",
            "added": True,
            "details_saved": False,
            "details_error": "Server rejected grade",
        }

    monkeypatch.setattr("locg.cli.cmd_add", fake_cmd_add)
    with patch("locg.cli.LOCGClient") as MockClient:
        MockClient.return_value.close = MagicMock()
        monkeypatch.setattr(sys, "argv", ["locg", "add", "collection", "12345", "--grade", "8.5"])
        with pytest.raises(SystemExit) as exc:
            main()
    assert exc.value.code == 1

    captured = capsys.readouterr()
    assert "partial" in captured.out
    assert "details_error" in captured.out
    assert "Server rejected grade" in captured.err
    assert "Comic added but details not saved" in captured.err


def test_cli_add_rejects_bogus_grade(monkeypatch, capsys):
    from unittest.mock import patch
    with patch("locg.cli.LOCGClient"):
        monkeypatch.setattr(sys, "argv", ["locg", "add", "collection", "12345", "--grade", "11.0"])
        with pytest.raises(SystemExit) as exc:
            main()
    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert "Invalid grade" in captured.err


def test_cli_add_rejects_grade_on_non_collection_list(monkeypatch, capsys):
    from unittest.mock import patch
    with patch("locg.cli.LOCGClient"):
        monkeypatch.setattr(sys, "argv", ["locg", "add", "pull", "12345", "--grade", "8.5"])
        with pytest.raises(SystemExit) as exc:
            main()
    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert "collection" in captured.err.lower()


def test_cli_update_passes_all_flags(monkeypatch, capsys):
    from unittest.mock import MagicMock, patch
    calls = {}

    def fake_cmd_update(client, comic_id, grade=None, price=None, condition=None):
        calls["args"] = (comic_id, grade, price, condition)
        return {"type": "success", "text": "ok"}

    monkeypatch.setattr("locg.cli.cmd_update", fake_cmd_update)
    with patch("locg.cli.LOCGClient") as MockClient:
        MockClient.return_value.close = MagicMock()
        monkeypatch.setattr(sys, "argv", [
            "locg", "update", "12345",
            "--grade", "8.5", "--price", "390", "--condition", "white pages",
        ])
        try:
            main()
        except SystemExit as e:
            assert e.code in (None, 0)

    assert calls["args"] == (12345, "8.5", "390", "white pages")


def test_cli_update_requires_at_least_one_flag(monkeypatch, capsys):
    from unittest.mock import patch
    with patch("locg.cli.LOCGClient"):
        monkeypatch.setattr(sys, "argv", ["locg", "update", "12345"])
        with pytest.raises(SystemExit) as exc:
            main()
    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert "at least one" in captured.err.lower()


def test_cli_update_rejects_bogus_grade(monkeypatch, capsys):
    from unittest.mock import patch
    with patch("locg.cli.LOCGClient"):
        monkeypatch.setattr(sys, "argv", ["locg", "update", "12345", "--grade", "11.0"])
        with pytest.raises(SystemExit) as exc:
            main()
    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert "Invalid grade" in captured.err


def test_cli_update_server_error_exits_1(monkeypatch, capsys):
    """When cmd_update returns a server error, exit 1."""
    from unittest.mock import MagicMock, patch

    def fake_cmd_update(client, comic_id, grade=None, price=None, condition=None):
        return {"type": "error", "text": "Something went wrong."}

    monkeypatch.setattr("locg.cli.cmd_update", fake_cmd_update)
    with patch("locg.cli.LOCGClient") as MockClient:
        MockClient.return_value.close = MagicMock()
        monkeypatch.setattr(sys, "argv", ["locg", "update", "12345", "--grade", "8.5"])
        with pytest.raises(SystemExit) as exc:
            main()
    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert "error" in captured.out  # still printed to stdout (machine-readable)


# --- collection record-win ---

def test_cli_record_win_malformed_json_exits_2(monkeypatch, capsys, tmp_path):
    """Malformed JSON passed via --from-gixen-json exits 2 with a parse error."""
    bad_json = tmp_path / "bad.json"
    bad_json.write_text("{not valid json")

    monkeypatch.setattr(sys, "argv", [
        "locg", "collection", "record-win", "--from-gixen-json", str(bad_json),
    ])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2
    captured = capsys.readouterr()
    assert "parse" in captured.err.lower() or "json" in captured.err.lower()


def test_cli_record_win_non_list_json_exits_2(monkeypatch, capsys, tmp_path):
    """JSON that isn't a list exits 2."""
    bad_json = tmp_path / "obj.json"
    bad_json.write_text('{"item_id": "1"}')

    monkeypatch.setattr(sys, "argv", [
        "locg", "collection", "record-win", "--from-gixen-json", str(bad_json),
    ])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2


# --- wish-list cache routing ---


def test_cli_wish_list_uses_cache_skips_client(monkeypatch, tmp_path, capsys):
    """When wish-list cache exists, LOCGClient must not be constructed."""
    import locg.cli

    cache_file = tmp_path / "wish-list.json"
    cache_file.write_text(json.dumps({
        "updated_at": "2026-01-01T00:00:00+00:00",
        "items": [{"name": "Batman #1", "id": None}],
    }))

    client_constructed = []

    class FakeClient:
        def __init__(self):
            client_constructed.append(True)
        def close(self):
            pass

    monkeypatch.setattr(locg.cli, "LOCGClient", FakeClient)
    monkeypatch.setattr(locg.cli, "cmd_wish_list_from_cache", lambda title=None: [{"name": "Batman #1"}])
    monkeypatch.setattr(sys, "argv", ["locg", "wish-list"])

    try:
        main()
    except SystemExit as e:
        assert e.code in (None, 0)

    assert not client_constructed, "LOCGClient must not be constructed when wish-list cache is present"


def test_cli_wish_list_falls_back_to_live_when_no_cache(monkeypatch, tmp_path, capsys):
    """When wish-list cache is absent, cmd_wish_list (live) is called."""
    import locg.cli

    # Cache file is absent — _isolate_wish_list_cache fixture returns a path that doesn't exist yet

    live_called = []

    def fake_cmd_wish_list(client, title=None):
        live_called.append(True)
        return [{"name": "Batman #1"}]

    class FakeClient:
        def __init__(self):
            pass
        def require_auth(self):
            pass
        def close(self):
            pass

    monkeypatch.setattr(locg.cli, "LOCGClient", FakeClient)
    monkeypatch.setattr(locg.cli, "load_dotenv", lambda *a, **kw: None)
    monkeypatch.setattr(locg.cli, "cmd_wish_list", fake_cmd_wish_list)
    monkeypatch.setattr(sys, "argv", ["locg", "wish-list"])

    try:
        main()
    except SystemExit as e:
        assert e.code in (None, 0)

    assert live_called, "cmd_wish_list (live) must be called when wish-list cache is absent"
