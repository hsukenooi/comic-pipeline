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
    cmd_collection_audit_pending,
    cmd_collection_backfill,
    cmd_collection_check,
    cmd_collection_doctor,
    cmd_collection_export,
    cmd_collection_has,
    cmd_collection_import,
    cmd_collection_record_win,
    cmd_collection_remediate_delete,
    cmd_collection_remediate_set_copies,
    cmd_collection_status,
    cmd_comic,
    cmd_creator_run_lookup,
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
    cmd_wish_list_add_creator_run,
    cmd_wish_list_from_cache,
    cmd_wish_list_migrate_source,
    cmd_wish_list_remove,
    cmd_wish_list_set_year,
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
    p_import = coll_sub.add_parser(
        "import",
        parents=[common],
        help=(
            "Import a LOCG Excel export into the local collection cache. "
            "IMPORTANT: requires LOCG_DATA_DIR to be set explicitly (BUI-476) — "
            "this command rewrites the WHOLE collection and refuses to guess "
            "which store you mean. On the Mac Mini the server-owned store is: "
            "LOCG_DATA_DIR=$HOME/.comics-server/collection-store locg collection "
            "import <path>"
        ),
    )
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

    # collection audit-pending — read-only pre-sync data-quality audit (BUI-432)
    p_audit = coll_sub.add_parser(
        "audit-pending",
        parents=[common],
        help="Audit an already-exported wins CSV for data-quality issues before uploading to LOCG (read-only)",
        epilog=(
            "Flags missing publisher/series/full_title, a decorated full_title "
            "((Vol.)/year), and a YYYY-01-01 date, plus a dateless summary "
            "(count, titles, all_dateless) and a ready-to-surface "
            "dateless_warning. BUI-466: a YYYY-01-01 date is cross-checked "
            "against the collection store (source=agent_win + no metron_id) "
            "before it hard-stops — a confirmed BUI-105 placeholder lands in "
            "flagged_rows/flagged_count, a confirmed-genuine or unconfirmable "
            "January cover date is demoted to advisory_rows/advisory_count and "
            "does not hard-stop. Read-only — never mutates the store or the CSV. "
            "Pass the path from a prior `collection export`; do not re-export "
            "before auditing (re-exporting re-blanks placeholder dates)."
        ),
    )
    p_audit.add_argument("csv_path", help="Path to the already-exported wins CSV (from `collection export`)")

    # collection record-win — agent win recording
    p_rw = coll_sub.add_parser(
        "record-win",
        parents=[common],
        help=(
            "Record Gixen auction wins into the local collection cache. "
            "Reads a JSON list from stdin or --from-gixen-json. "
            "Commits in batches of 25; large batches scale with Metron rate limit. "
            "IMPORTANT: requires LOCG_DATA_DIR to be set explicitly (BUI-476) — "
            "this command refuses to guess which collection store you mean rather "
            "than risk silently writing wins into the wrong one. On the Mac Mini "
            "the server-owned store is: LOCG_DATA_DIR=$HOME/.comics-server/"
            "collection-store locg collection record-win --from-gixen-json <path>"
        ),
    )
    p_rw.add_argument(
        "--from-gixen-json",
        dest="gixen_json_path",
        metavar="PATH",
        help="Path to a JSON file containing wins (use '-' to read from stdin)",
    )

    # collection backfill — publisher/date remediation for stored pending wins (BUI-461)
    p_backfill = coll_sub.add_parser(
        "backfill",
        parents=[common],
        help="Backfill publisher_name/release_date on ALREADY-STORED pending agent_win rows (dry-run by default)",
        epilog=(
            "Remediates rows the record-win producer wrote before BUI-458/BUI-210 "
            "(null publisher, YYYY-01-01 placeholder date), and legacy hand-"
            "remediated YYYY-01-02 dodge dates (BUI-471). Targets ONLY pending "
            "agent_win rows (source=agent_win, in_collection>=1, pending push — "
            "matches the export's own pending definition, BUI-471) — never an "
            "locg_export row or a wish twin — and writes ONLY "
            "publisher_name/release_date/metron_id, never identity or copy counts. "
            "Dry-run is the DEFAULT and writes nothing; --apply resolves and writes "
            "in chunks of 25, taking a durable backup once before the first chunk "
            "with anything to write (refusing if it captured zero rows) — a crash "
            "mid-run loses at most the in-flight chunk, never the whole backlog. A "
            "Metron miss leaves the field alone (never a fabricated publisher or "
            "date); a dateless row has no year to era-gate a lookup with and is "
            "reported for the documented web fallback. Re-running is a no-op. "
            "IMPORTANT: requires LOCG_DATA_DIR to be set explicitly (BUI-471) — "
            "this command refuses to guess which collection store you mean rather "
            "than risk silently remediating the wrong one. On the Mac Mini the "
            "server-owned store is: LOCG_DATA_DIR=$HOME/.comics-server/"
            "collection-store locg collection backfill [--apply]"
        ),
    )
    p_backfill.add_argument("--series", help="Only rows whose series_name contains this text (case-insensitive)")
    p_backfill.add_argument("--full-title", dest="full_title", help="Only rows whose full_title contains this text (case-insensitive)")
    p_backfill.add_argument("--apply", action="store_true", help="Write the changes (default is a read-only dry run)")
    p_backfill.add_argument(
        "--cadence",
        type=float,
        default=3.0,
        help="Seconds to sleep between rows that spend a Metron call (default: 3.0; Metron allows ~20 req/min). 0 disables.",
    )
    p_backfill.add_argument("--limit", type=int, help="Process at most N candidate rows (for consecutive runs against a large backlog)")
    p_backfill.add_argument("--backup-dir", dest="backup_dir", help="Where --apply writes its durable pre-write snapshot (default: <store>-backups/backfill-<ts>)")

    # collection remediate-delete — matcher-bypassing single-row delete (BUI-427)
    p_rem_del = coll_sub.add_parser(
        "remediate-delete",
        parents=[common],
        help="Delete/decrement ONE collection row by STABLE IDENTITY, bypassing the check matcher",
        epilog=(
            "For a volume-mis-filed row the ordinary `collection check` matcher "
            "can't disambiguate (masthead alias / X-Men split / leading-article "
            "normalization can target the wrong row, or refuse via "
            "ambiguous_cross_volume). Supply EXACTLY ONE identity: "
            "--gixen-item-id, OR --full-title (+ --release-date, --source). "
            "A row with in_collection > 1 is decremented; a single-copy row is "
            "removed outright. --dry-run previews without mutating."
        ),
    )
    p_rem_del.add_argument("--gixen-item-id", dest="gixen_item_id", help="The row's stable gixen_item_id")
    p_rem_del.add_argument("--full-title", dest="full_title", help="Exact full_title to match (with --release-date/--source)")
    p_rem_del.add_argument("--release-date", dest="release_date", help="Exact release_date to match alongside --full-title")
    p_rem_del.add_argument("--source", help="Exact source to match alongside --full-title (e.g. agent_win, locg_export)")
    p_rem_del.add_argument("--dry-run", dest="dry_run", action="store_true", help="Preview the op without mutating")

    # collection remediate-set-copies — matcher-bypassing copy-count set/adjust (BUI-427)
    p_rem_set = coll_sub.add_parser(
        "remediate-set-copies",
        parents=[common],
        help="Set or adjust in_collection on ONE collection row by STABLE IDENTITY, bypassing the check matcher",
        epilog=(
            "Same identity resolution as remediate-delete. Supply EXACTLY ONE "
            "of --in-collection (an explicit absolute value) or --delta (a "
            "signed adjustment); a delta that would go negative is refused, "
            "never clamped. Unlike remediate-delete, this never removes the "
            "row even at 0 copies — in_collection == 0 is itself a valid "
            "tracked-but-not-owned state."
        ),
    )
    p_rem_set.add_argument("--gixen-item-id", dest="gixen_item_id", help="The row's stable gixen_item_id")
    p_rem_set.add_argument("--full-title", dest="full_title", help="Exact full_title to match (with --release-date/--source)")
    p_rem_set.add_argument("--release-date", dest="release_date", help="Exact release_date to match alongside --full-title")
    p_rem_set.add_argument("--source", help="Exact source to match alongside --full-title (e.g. agent_win, locg_export)")
    p_rem_set_count = p_rem_set.add_mutually_exclusive_group(required=True)
    p_rem_set_count.add_argument("--in-collection", dest="in_collection", type=int, help="Set copies-owned to this exact value (>= 0)")
    p_rem_set_count.add_argument("--delta", type=int, help="Adjust copies-owned by this signed amount")
    p_rem_set.add_argument("--dry-run", dest="dry_run", action="store_true", help="Preview the op without mutating")

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
        help="Append a manual entry to the local wish-list cache, or a whole creator run",
        epilog=(
            "Writes {name: <title>, id: null, source: local[, year: <cover_year>]} "
            "to wish-list.json. As of BUI-208 a subsequent `locg collection import` "
            "no longer touches wish-list.json, so local adds — and any BUI-387 "
            "`year` (cover-year) stamp — survive imports (wish-list.json is the "
            "single source of truth; only a server-side removal or a manual re-seed "
            "changes it). With --creator + --series-id, resolves the creator's run "
            "on the series from Metron credits and adds the gap issues (owned + "
            "already-wishlisted issues are filtered out first)."
        ),
    )
    # `title` is the simple single-entry path; optional when --creator is used
    # (the run resolver builds "<series> #<N>" titles from `title` as the series).
    p_wish_add.add_argument(
        "title",
        help=(
            "Title to record (e.g. 'Amazing Spider-Man #300'). With --creator, "
            "this is the SERIES title used for the '<series> #<N>' wish entries."
        ),
    )
    p_wish_add.add_argument(
        "--creator",
        help=(
            "Resolve this creator's run on the series from Metron credits and add "
            "the gap issues (e.g. --creator 'John Romita Jr.'). Requires --series-id."
        ),
    )
    p_wish_add.add_argument(
        "--role",
        default="penciller",
        help="Credit role to filter the run by (default: penciller).",
    )
    p_wish_add.add_argument(
        "--series-id",
        dest="series_id",
        type=int,
        help="Metron series id (required with --creator).",
    )
    p_wish_add.add_argument(
        "--year",
        help=(
            "Per-issue COVER YEAR to stamp on the entry (BUI-387), e.g. 1963 for "
            "'The X-Men #1'. Year-scopes the conflicts audit so a vintage want no "
            "longer flags against an owned modern volume. Must be the issue's own "
            "cover year, NEVER a series start year (year_began, BUI-129). Ignored "
            "with --creator (that path stamps each issue's Metron cover year "
            "automatically)."
        ),
    )
    p_wish_remove = wish_sub.add_parser(
        "remove",
        parents=[common],
        help="Remove a title from the local wish-list cache",
    )
    p_wish_remove.add_argument("title", help="Exact title to remove (e.g. 'Amazing Spider-Man #300')")
    p_wish_set_year = wish_sub.add_parser(
        "set-year",
        parents=[common],
        help="Stamp a per-issue cover year on an existing wish-list entry (BUI-387 backfill)",
        epilog=(
            "Sets a `year` field on the exact-named wish entry so the conflicts "
            "audit year-scopes it. Pass the issue's OWN cover year, never a series "
            "start year (year_began, BUI-129). The one-time backfill for the "
            "cross-volume decoy holds — resolve each held issue's cover year from "
            "Metron first (see the wish-list-year backfill process doc)."
        ),
    )
    p_wish_set_year.add_argument("title", help="Exact wish-list entry name (e.g. 'The X-Men #1')")
    p_wish_set_year.add_argument("year", help="Per-issue cover year, 4 digits (e.g. 1963)")
    wish_sub.add_parser(
        "migrate-source",
        parents=[common],
        help="Backfill an explicit source ('local'/'export') field on every wish-list entry (BUI-208)",
        epilog=(
            "Writes a verified .bak copy of wish-list.json, then stamps "
            "source on each entry lacking one. Idempotent; a second run is a no-op."
        ),
    )

    # creator-run (BUI-340): read-only lookup, no wish-list/collection writes.
    p = sub.add_parser(
        "creator-run",
        parents=[common],
        help="Look up a creator's run on a series via Metron (read-only, no writes)",
        epilog=(
            "Resolves the creator's Metron id and the exact issue set they hold "
            "the given role on (per-issue credit confirmed, gaps and discontinuous "
            "stints included), and prints it. Does NOT touch the wish-list or "
            "collection cache. Use `wish-list add <series> --creator ... --series-id ...` "
            "instead when you actually want the gap issues added to the wish-list."
        ),
    )
    p.add_argument(
        "series",
        help="Series title, used only for the response's 'series' field (resolution is keyed off --series-id).",
    )
    p.add_argument(
        "--creator",
        required=True,
        help="Creator name to resolve on Metron (e.g. 'John Romita Jr.').",
    )
    p.add_argument(
        "--series-id",
        dest="series_id",
        type=int,
        required=True,
        help="Metron series id.",
    )
    p.add_argument(
        "--role",
        default="penciller",
        help="Credit role to filter the run by (default: penciller).",
    )

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
    _LOCAL_COLLECTION_SUBCMDS = {
        "import", "export", "status", "check", "doctor", "record-win", "audit-pending",
        "remediate-delete", "remediate-set-copies", "backfill",
    }
    _collection_sub = (
        getattr(args, "collection_command", None)
        if args.command == "collection"
        else None
    )
    # wish-list skips Playwright when the local cache exists; it still needs a
    # client when no cache is present (live fallback, R5).
    _wish_list_cached = args.command == "wish-list" and wish_list_cache_path().exists()
    # wish-list add/remove/migrate-source/set-year are pure local-cache writes — never need a client.
    _wish_list_add = (
        args.command == "wish-list"
        and getattr(args, "wish_list_command", None)
        in ("add", "remove", "migrate-source", "set-year")
    )
    # creator-run is a pure Metron lookup — never needs the Playwright/LOCG client.
    _needs_client = not (
        args.command == "cache"
        or (_collection_sub in _LOCAL_COLLECTION_SUBCMDS)
        or _wish_list_cached
        or _wish_list_add
        or args.command == "creator-run"
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
                # BUI-476: the store refusal must exit NON-ZERO. Every caller
                # that chains (`... && next-step`, `set -e`, a skill branching
                # on $?) reads exit 0 as "imported"; the refusal recorded
                # nothing, so a zero exit would recreate the exact
                # "operator believes it worked" failure the guard exists to
                # prevent. Mirrors backfill/remediate-* below.
                if result.get("status") == "explicit_store_required":
                    # No `fields=`: --fields would filter the payload down to
                    # `{}` (it has only status/error, never a key a caller
                    # narrows to), leaving an unexplained exit 1 with no
                    # remediation text anywhere.
                    output(result, pretty=args.pretty)
                    sys.exit(1)
            elif sub_cmd == "export":
                result = cmd_collection_export(getattr(args, "out_path", None))
                # BUI-489: a never-touched store must not exit 0 with a
                # silently empty CSV — same "a chained caller reads exit 0 as
                # success" rationale as the explicit_store_required refusals
                # above, applied to export's narrower not-imported signal.
                if result.get("status") == "not_imported":
                    output(result, pretty=args.pretty)
                    sys.exit(1)
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
            elif sub_cmd == "audit-pending":
                result = cmd_collection_audit_pending(args.csv_path)
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
                # BUI-476: see the identical note on `import` above — a refusal
                # recorded zero wins and must not exit 0.
                if result.get("status") == "explicit_store_required":
                    # No `fields=`: --fields would filter the payload down to
                    # `{}` (it has only status/error, never a key a caller
                    # narrows to), leaving an unexplained exit 1 with no
                    # remediation text anywhere.
                    output(result, pretty=args.pretty)
                    sys.exit(1)
            elif sub_cmd == "backfill":
                result = cmd_collection_backfill(
                    series=getattr(args, "series", None),
                    full_title=getattr(args, "full_title", None),
                    apply=getattr(args, "apply", False),
                    cadence=getattr(args, "cadence", 3.0),
                    limit=getattr(args, "limit", None),
                    backup_dir=getattr(args, "backup_dir", None),
                )
                if result.get("status") not in ("ok", "preview"):
                    output(result, pretty=args.pretty, fields=fields)
                    sys.exit(1)
            elif sub_cmd == "remediate-delete":
                result = cmd_collection_remediate_delete(
                    gixen_item_id=getattr(args, "gixen_item_id", None),
                    full_title=getattr(args, "full_title", None),
                    release_date=getattr(args, "release_date", None),
                    source=getattr(args, "source", None),
                    dry_run=getattr(args, "dry_run", False),
                )
                if result.get("status") not in ("ok", "preview"):
                    output(result, pretty=args.pretty, fields=fields)
                    sys.exit(1)
            elif sub_cmd == "remediate-set-copies":
                result = cmd_collection_remediate_set_copies(
                    gixen_item_id=getattr(args, "gixen_item_id", None),
                    full_title=getattr(args, "full_title", None),
                    release_date=getattr(args, "release_date", None),
                    source=getattr(args, "source", None),
                    in_collection=getattr(args, "in_collection", None),
                    delta=getattr(args, "delta", None),
                    dry_run=getattr(args, "dry_run", False),
                )
                if result.get("status") not in ("ok", "preview"):
                    output(result, pretty=args.pretty, fields=fields)
                    sys.exit(1)
            else:
                result = cmd_collection(client, title=args.title)
        elif args.command == "pull-list":
            result = cmd_pull_list(client, title=args.title)
        elif args.command == "wish-list":
            if getattr(args, "wish_list_command", None) == "add":
                # The subparser positional `title` shadows the parent's
                # --title flag, so args.title is the value to append.
                creator = getattr(args, "creator", None)
                if creator:
                    series_id = getattr(args, "series_id", None)
                    if series_id is None:
                        die("wish-list add --creator requires --series-id (the Metron series id)")
                    result = cmd_wish_list_add_creator_run(
                        series=args.title,
                        creator=creator,
                        series_id=series_id,
                        role=getattr(args, "role", "penciller") or "penciller",
                    )
                else:
                    # BUI-387: --year stamps the entry's per-issue cover year.
                    result = cmd_wish_list_add(args.title, year=getattr(args, "year", None))
                # BUI-489: the store refusal must exit NON-ZERO — same
                # rationale as import/record-win above (a chained caller
                # reads exit 0 as "added").
                if result.get("status") == "explicit_store_required":
                    output(result, pretty=args.pretty)
                    sys.exit(1)
            elif getattr(args, "wish_list_command", None) == "remove":
                result = cmd_wish_list_remove(args.title)
                if result.get("status") == "explicit_store_required":
                    output(result, pretty=args.pretty)
                    sys.exit(1)
            elif getattr(args, "wish_list_command", None) == "set-year":
                # BUI-387 backfill: stamp a per-issue cover year on an existing entry.
                result = cmd_wish_list_set_year(args.title, args.year)
                if result.get("status") == "explicit_store_required":
                    output(result, pretty=args.pretty)
                    sys.exit(1)
            elif getattr(args, "wish_list_command", None) == "migrate-source":
                result = cmd_wish_list_migrate_source()
            elif _wish_list_cached:
                try:
                    result = cmd_wish_list_from_cache(title=args.title)
                except (FileNotFoundError, json.JSONDecodeError):
                    # Cache disappeared or is corrupt between the exists() check
                    # and the read — fall through to live fetch.
                    result = cmd_wish_list(client, title=args.title)
            else:
                result = cmd_wish_list(client, title=args.title)
        elif args.command == "creator-run":
            result = cmd_creator_run_lookup(
                series=args.series,
                creator=args.creator,
                series_id=args.series_id,
                role=args.role or "penciller",
            )
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
    except Exception as e:  # noqa: BLE001  # CLI top-level handler — translate any unexpected error to exit 4
        die(str(e), code=4)
    finally:
        if client is not None:
            client.close()
