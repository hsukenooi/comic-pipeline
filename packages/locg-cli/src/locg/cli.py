"""CLI entry point for locg."""
from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Any, Optional

from dotenv import load_dotenv

from locg import __version__
from locg.client import AuthRequired, LOCGClient
from locg.config import env_path, wish_list_cache_path
from locg.commands import (
    VALID_LISTS,
    _validate_grade,
    _validate_price,
    cmd_add,
    cmd_cache_clear,
    cmd_cache_stats,
    cmd_check_lists,
    cmd_collection,
    cmd_collection_check,
    cmd_collection_doctor,
    cmd_collection_export,
    cmd_collection_has,
    cmd_collection_import,
    cmd_collection_record_win,
    cmd_collection_status,
    cmd_comic,
    cmd_find,
    cmd_login,
    cmd_lookup,
    cmd_pull_list,
    cmd_read_list,
    cmd_releases,
    cmd_remove,
    cmd_search,
    cmd_series,
    cmd_update,
    cmd_wish_list,
    cmd_wish_list_add,
    cmd_wish_list_from_cache,
    cmd_wish_list_remove,
    parse_lookup_spec,
)


def die(msg: str, code: int = 1) -> None:
    """Print structured JSON error to stderr and exit."""
    json.dump({"error": msg}, sys.stderr)
    print(file=sys.stderr)
    sys.exit(code)


def _filter_fields(data: Any, fields: list[str]) -> Any:
    """Keep only the specified fields in dicts (or lists of dicts)."""
    if isinstance(data, list):
        return [_filter_fields(item, fields) for item in data]
    if isinstance(data, dict):
        return {k: v for k, v in data.items() if k in fields}
    return data


def output(data: Any, pretty: bool = False, fields: list[str] | None = None) -> None:
    """Print JSON data to stdout."""
    if fields:
        data = _filter_fields(data, fields)
    if pretty:
        print(json.dumps(data, indent=2, ensure_ascii=False))
    else:
        print(json.dumps(data, separators=(",", ":"), ensure_ascii=False))


def create_parser() -> argparse.ArgumentParser:
    # Shared flags available on all subcommands
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--pretty", action="store_true", help="Pretty-print JSON output")
    common.add_argument("-v", "--verbose", action="store_true", help="Verbose output (INFO level)")
    common.add_argument("--debug", action="store_true", help="Debug output (DEBUG level, includes HTTP details)")
    common.add_argument("--fields", help="Comma-separated list of fields to include in output (e.g. --fields name,id)")

    parser = argparse.ArgumentParser(
        prog="locg",
        description="CLI for League of Comic Geeks",
        parents=[common],
    )
    parser.add_argument("--version", action="version", version=f"locg {__version__}")

    sub = parser.add_subparsers(dest="command", help="Available commands")

    # search
    p = sub.add_parser("search", parents=[common], help="Search for comic series")
    p.add_argument("query", help="Search term")

    # releases
    p = sub.add_parser("releases", parents=[common], help="New releases for a given week")
    p.add_argument("--date", help="Week date (YYYY-MM-DD), default: this week")

    # comic
    p = sub.add_parser("comic", parents=[common], help="Get comic details")
    p.add_argument("id", type=int, help="Comic ID")

    # series
    p = sub.add_parser("series", parents=[common], help="Get series details and issue list")
    p.add_argument("id", type=int, help="Series ID")

    # find — locate specific issue(s) within a series without manual pagination
    p = sub.add_parser(
        "find",
        parents=[common],
        help="Find issues by number within a series (paginates automatically)",
    )
    p.add_argument("--series-id", type=int, required=True, help="Series ID to search within")
    p.add_argument("--issue", required=True, help="Issue number, e.g. 229")
    p.add_argument(
        "--variant",
        help="Case-insensitive substring filter on title (e.g. 'newsstand', 'homage')",
    )
    p.add_argument(
        "--exact",
        action="store_true",
        help="Only return titles ending in #<issue> with no variant suffix",
    )

    # collection — LOCG view (default) + cache-based subcommands
    p = sub.add_parser("collection", parents=[common], help="View your collection or manage the local cache")
    p.add_argument("--title", help="Filter results by title (case-insensitive substring match, LOCG fetch only)")
    coll_sub = p.add_subparsers(dest="collection_command")

    # collection has — live LOCG check
    p_has = coll_sub.add_parser("has", parents=[common], help="Check if a title is in your collection via LOCG (requires login)")
    p_has.add_argument("title_query", help="Title to search for (case-insensitive substring match)")

    # collection import — local cache
    p_import = coll_sub.add_parser("import", parents=[common], help="Import a LOCG Excel export into the local collection cache")
    p_import.add_argument("path", help="Path to a LOCG Excel export (.xlsx)")

    # collection export — local cache
    p_export = coll_sub.add_parser("export", parents=[common], help="Export pending-push rows to a LOCG-compatible CSV")
    p_export.add_argument("--out", dest="out_path", help="Output CSV path (default: ~/Downloads/locg-bulk-import-<timestamp>.csv)")

    # collection status — local cache (--verbose inherited from common parent)
    coll_sub.add_parser("status", parents=[common], help="Show local collection cache status (use --verbose for extended metrics)")

    # collection check — local cache
    p_check = coll_sub.add_parser("check", parents=[common], help="Check if a specific comic is in the local collection cache")
    p_check.add_argument("--series", required=True, help="Series name (normalized match)")
    p_check.add_argument("--issue", required=True, help="Issue number (e.g. 300)")
    p_check.add_argument("--variant", help="Optional variant text to match in title")
    p_check.add_argument("--year", help="Optional publication year filter (e.g. 1988)")

    # collection doctor — local cache
    coll_sub.add_parser("doctor", parents=[common], help="Print first-run setup walkthrough and cache status")

    # collection record-win — agent win recording
    p_rw = coll_sub.add_parser(
        "record-win",
        parents=[common],
        help=(
            "Record Gixen auction wins into the local collection cache. "
            "Reads a JSON list from stdin or --from-gixen-json. "
            "Commits in batches of 25; large batches scale with Metron rate limit."
        ),
    )
    p_rw.add_argument(
        "--from-gixen-json",
        dest="gixen_json_path",
        metavar="PATH",
        help="Path to a JSON file containing wins (use '-' to read from stdin)",
    )

    # pull-list
    p = sub.add_parser("pull-list", parents=[common], help="View your pull list (requires login)")
    p.add_argument("--title", help="Filter results by title (case-insensitive substring match)")

    # wish-list
    p = sub.add_parser("wish-list", parents=[common], help="View your wish list (requires login)")
    p.add_argument("--title", help="Filter results by title (case-insensitive substring match)")
    wish_sub = p.add_subparsers(dest="wish_list_command")
    p_wish_add = wish_sub.add_parser(
        "add",
        parents=[common],
        help="Append a manual entry to the local wish-list cache",
        epilog=(
            "Writes {name: <title>, id: null} to ~/.cache/locg/wish-list.json. "
            "A subsequent `locg collection import` overwrites the cache from "
            "the LOCG XLSX export, so manually-added entries are not preserved "
            "across imports."
        ),
    )
    p_wish_add.add_argument("title", help="Title to record (e.g. 'Amazing Spider-Man #300')")
    p_wish_remove = wish_sub.add_parser(
        "remove",
        parents=[common],
        help="Remove a title from the local wish-list cache",
    )
    p_wish_remove.add_argument("title", help="Exact title to remove (e.g. 'Amazing Spider-Man #300')")

    # read-list
    p = sub.add_parser("read-list", parents=[common], help="View your read list (requires login)")
    p.add_argument("--title", help="Filter results by title (case-insensitive substring match)")

    # add
    p = sub.add_parser("add", parents=[common], help="Add a comic to a list")
    p.add_argument("list", choices=VALID_LISTS, help="Target list")
    p.add_argument("comic_id", type=int, help="Comic ID")
    p.add_argument("--grade", help="LOCG CGC grade (collection only, e.g. 8.5, 9.2, 9.8)")
    p.add_argument("--price", help="Purchase price (collection only, numeric)")

    # remove
    p = sub.add_parser("remove", parents=[common], help="Remove a comic from a list")
    p.add_argument("list", choices=VALID_LISTS, help="Target list")
    p.add_argument("comic_id", type=int, help="Comic ID")

    # update
    p = sub.add_parser("update", parents=[common], help="Update grade/price/condition on a comic in your collection")
    p.add_argument("id", type=int, help="Comic ID")
    p.add_argument("--grade", help="LOCG CGC grade (e.g. 8.5, 9.2, 9.8)")
    p.add_argument("--price", help="Purchase price (numeric)")
    p.add_argument("--condition", help="Free-text condition notes")

    # check
    p = sub.add_parser("check", parents=[common], help="Check which lists comics belong to (requires login)")
    p.add_argument("comic_ids", type=int, nargs="+", help="One or more comic IDs")

    # lookup
    p = sub.add_parser(
        "lookup",
        parents=[common],
        help="Resolve LOCG IDs for a batch of 'Series:Issue[:Variant]' specs",
        epilog=(
            "Series names may contain colons; the trailing token is treated as a "
            "variant only when the second-to-last token looks like an issue number "
            "(e.g. 'Batman: The Long Halloween:9' is parsed as series + issue)."
        ),
    )
    p.add_argument(
        "specs",
        nargs="+",
        help="One or more 'Series:Issue[:Variant]' specs (e.g. 'Uncanny X-Men:185')",
    )
    p.add_argument(
        "--no-collection",
        action="store_true",
        help="Skip collection-membership check (no auth needed)",
    )
    p.add_argument(
        "--no-cache",
        action="store_true",
        help="Bypass the on-disk ID cache (always hit the API; do not write back)",
    )

    # cache (with stats / clear / path subcommands)
    p = sub.add_parser(
        "cache",
        parents=[common],
        help="Inspect or clear the on-disk LOCG ID cache",
    )
    cache_sub = p.add_subparsers(dest="cache_command")
    cache_sub.add_parser("stats", parents=[common], help="Show cache file path, entry count, size")
    cache_sub.add_parser("clear", parents=[common], help="Delete every cached ID entry")

    # login
    p = sub.add_parser(
        "login",
        parents=[common],
        help="Log in to League of Comic Geeks",
        epilog=(
            "Env vars LOCG_USERNAME and LOCG_PASSWORD (or a .env file at "
            "~/.config/locg/.env) enable automatic re-authentication when "
            "a session expires, so commands do not require a manual login."
        ),
    )
    p.add_argument("-u", "--username", help="Username (prompts if not provided)")
    p.add_argument("-p", "--password", help="Password (prompts if not provided)")

    return parser


def main() -> None:
    # Load ~/.config/locg/.env so LOCG_USERNAME/LOCG_PASSWORD are
    # resolved from a deterministic path, not wherever the user happens
    # to be running locg from.
    load_dotenv(dotenv_path=env_path())

    # Pre-scan for global flags before argparse, since parent parser
    # defaults can overwrite values when flags appear before subcommand
    raw = sys.argv[1:]
    pretty = "--pretty" in raw
    debug = "--debug" in raw
    verbose = "--verbose" in raw or "-v" in raw

    parser = create_parser()
    args = parser.parse_args()

    if not args.command:
        parser.print_help(sys.stderr)
        sys.exit(2)

    # Use pre-scanned values (handles both `locg --pretty releases` and `locg releases --pretty`)
    args.pretty = pretty
    args.debug = debug
    args.verbose = verbose

    # Pre-scan --fields (same reason as other flags above)
    fields: list[str] | None = None
    for i, arg in enumerate(raw):
        if arg == "--fields" and i + 1 < len(raw):
            fields = [f.strip() for f in raw[i + 1].split(",")]
            break
        elif arg.startswith("--fields="):
            fields = [f.strip() for f in arg.split("=", 1)[1].split(",")]
            break

    # Configure logging to stderr (keeps stdout clean for JSON)
    if args.debug:
        level = logging.DEBUG
    elif args.verbose:
        level = logging.INFO
    else:
        level = logging.WARNING
    logging.basicConfig(
        level=level,
        format="%(levelname)s: %(message)s",
        stream=sys.stderr,
    )

    logger = logging.getLogger("locg")

    # Collection cache subcommands are purely local — skip Playwright browser launch.
    _LOCAL_COLLECTION_SUBCMDS = {"import", "export", "status", "check", "doctor", "record-win"}
    _collection_sub = (
        getattr(args, "collection_command", None)
        if args.command == "collection"
        else None
    )
    # wish-list skips Playwright when the local cache exists; it still needs a
    # client when no cache is present (live fallback, R5).
    _wish_list_cached = args.command == "wish-list" and wish_list_cache_path().exists()
    # wish-list add/remove are pure local-cache writes — never need a client.
    _wish_list_add = (
        args.command == "wish-list"
        and getattr(args, "wish_list_command", None) in ("add", "remove")
    )
    _needs_client = not (
        args.command == "cache"
        or (_collection_sub in _LOCAL_COLLECTION_SUBCMDS)
        or _wish_list_cached
        or _wish_list_add
    )

    client: Optional[LOCGClient] = None

    try:
        if _needs_client:
            client = LOCGClient()
        result: Any = None
        logger.info(f"Running command: {args.command}")

        if args.command == "search":
            result = cmd_search(client, args.query)
        elif args.command == "releases":
            result = cmd_releases(client, args.date)
        elif args.command == "comic":
            result = cmd_comic(client, args.id)
        elif args.command == "series":
            result = cmd_series(client, args.id)
        elif args.command == "find":
            result = cmd_find(
                client,
                series_id=args.series_id,
                issue=args.issue,
                variant=getattr(args, "variant", None),
                exact=getattr(args, "exact", False),
            )
        elif args.command == "collection":
            sub_cmd = getattr(args, "collection_command", None)
            if sub_cmd == "has":
                result = cmd_collection_has(client, args.title_query)
            elif sub_cmd == "import":
                result = cmd_collection_import(args.path)
            elif sub_cmd == "export":
                result = cmd_collection_export(getattr(args, "out_path", None))
            elif sub_cmd == "status":
                result = cmd_collection_status(verbose=args.verbose)
            elif sub_cmd == "check":
                result = cmd_collection_check(
                    series=args.series,
                    issue=args.issue,
                    variant=getattr(args, "variant", None),
                    year=getattr(args, "year", None),
                )
            elif sub_cmd == "doctor":
                result = cmd_collection_doctor()
            elif sub_cmd == "record-win":
                import json as _json
                import sys as _sys
                path = getattr(args, "gixen_json_path", None)
                if path is None or path == "-":
                    raw = _sys.stdin.read()
                else:
                    import pathlib as _pathlib
                    raw = _pathlib.Path(path).read_text()
                try:
                    wins = _json.loads(raw)
                except _json.JSONDecodeError as exc:
                    die(f"Failed to parse JSON input: {exc}", code=2)
                if not isinstance(wins, list):
                    die("JSON input must be a list of win objects", code=2)
                result = cmd_collection_record_win(wins)
            else:
                result = cmd_collection(client, title=args.title)
        elif args.command == "pull-list":
            result = cmd_pull_list(client, title=args.title)
        elif args.command == "wish-list":
            if getattr(args, "wish_list_command", None) == "add":
                # The subparser positional `title` shadows the parent's
                # --title flag, so args.title is the value to append.
                result = cmd_wish_list_add(args.title)
            elif getattr(args, "wish_list_command", None) == "remove":
                result = cmd_wish_list_remove(args.title)
            elif _wish_list_cached:
                try:
                    result = cmd_wish_list_from_cache(title=args.title)
                except (FileNotFoundError, json.JSONDecodeError):
                    # Cache disappeared or is corrupt between the exists() check
                    # and the read — fall through to live fetch.
                    result = cmd_wish_list(client, title=args.title)
            else:
                result = cmd_wish_list(client, title=args.title)
        elif args.command == "read-list":
            result = cmd_read_list(client, title=args.title)
        elif args.command == "add":
            grade = getattr(args, "grade", None)
            price = getattr(args, "price", None)
            if (grade is not None or price is not None) and args.list != "collection":
                die("--grade and --price are only valid when adding to collection")
            if grade is not None:
                try:
                    grade = _validate_grade(grade)
                except ValueError as e:
                    die(str(e))
            if price is not None:
                try:
                    price = _validate_price(price)
                except ValueError as e:
                    die(str(e))
            result = cmd_add(client, args.list, args.comic_id, grade=grade, price=price)
            if isinstance(result, dict) and result.get("status") == "partial":
                output(result, pretty=args.pretty, fields=fields)
                json.dump(
                    {"error": f"Comic added but details not saved: {result.get('details_error', 'unknown')}"},
                    sys.stderr,
                )
                print(file=sys.stderr)
                sys.exit(1)
        elif args.command == "remove":
            result = cmd_remove(client, args.list, args.comic_id)
        elif args.command == "update":
            grade = getattr(args, "grade", None)
            price = getattr(args, "price", None)
            condition = getattr(args, "condition", None)
            if grade is None and price is None and condition is None:
                die("update: at least one of --grade, --price, --condition is required")
            if grade is not None:
                try:
                    grade = _validate_grade(grade)
                except ValueError as e:
                    die(str(e))
            if price is not None:
                try:
                    price = _validate_price(price)
                except ValueError as e:
                    die(str(e))
            result = cmd_update(client, args.id, grade=grade, price=price, condition=condition)
            if isinstance(result, dict) and (
                result.get("type") == "error"
                or "error" in result
            ):
                output(result, pretty=args.pretty, fields=fields)
                sys.exit(1)
        elif args.command == "check":
            result = cmd_check_lists(client, args.comic_ids)
        elif args.command == "lookup":
            try:
                requests = [parse_lookup_spec(s) for s in args.specs]
            except ValueError as e:
                die(str(e))
            result = cmd_lookup(
                client,
                requests,
                check_collection=not args.no_collection,
                use_cache=not args.no_cache,
            )
        elif args.command == "cache":
            sub_cmd = getattr(args, "cache_command", None)
            if sub_cmd == "clear":
                result = cmd_cache_clear()
            else:
                # default to stats (also handles explicit "stats")
                result = cmd_cache_stats()
        elif args.command == "login":
            result = cmd_login(client, username=args.username, password=args.password)

        if result is not None:
            output(result, pretty=args.pretty, fields=fields)

    except AuthRequired as e:
        die(str(e), code=1)
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as e:
        die(str(e), code=4)
    finally:
        if client is not None:
            client.close()
