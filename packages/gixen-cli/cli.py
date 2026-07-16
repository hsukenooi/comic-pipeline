"""Gixen CLI — manage eBay snipes from the command line."""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

import click
from dotenv import load_dotenv

import asyncio

import requests

from gixen_client import (
    GixenClient,
    GixenError,
    GixenLoginError,
    GixenSnipeNotFoundError,
    find_sibling_cleanup_targets,
)
import ebay_bidder
from record_win_prep import RecordWinPrepError, build_payload
from add_batch import (
    AddBatchError,
    BatchOutcome,
    RowResult,
    STATUS_ADDED,
    STATUS_FAILED,
    STATUS_NOT_ATTEMPTED,
    STATUS_UPDATED,
    apply_verify_results,
    build_bid_payload,
    created_from_response,
    parse_rows,
    run_batch,
    verify_items,
)

load_dotenv()


def _make_client() -> GixenClient:
    username = os.getenv("GIXEN_USERNAME", "")
    password = os.getenv("GIXEN_PASSWORD", "")
    if not username or not password:
        click.echo(
            "Error: GIXEN_USERNAME and GIXEN_PASSWORD must be set. "
            "Add them to .env or export them.",
            err=True,
        )
        sys.exit(1)
    return GixenClient(username=username, password=password)


# BUI-220: the canonical env var is COMICS_SERVER_URL (this is the comics
# server, not the Gixen bidding service). GIXEN_SERVER_URL is a deprecated alias
# still read as a fallback; using it emits a one-line deprecation warning (once).
_DEPRECATION_WARNED = False


def _server_url() -> str | None:
    global _DEPRECATION_WARNED
    url = os.getenv("COMICS_SERVER_URL", "")
    if not url:
        legacy = os.getenv("GIXEN_SERVER_URL", "")
        if legacy and not _DEPRECATION_WARNED:
            click.echo(
                "warning: GIXEN_SERVER_URL is deprecated; use COMICS_SERVER_URL",
                err=True,
            )
            _DEPRECATION_WARNED = True
        url = legacy
    return url.rstrip("/") or None


_DEFAULT_SERVER_TIMEOUT = 15  # seconds


def _server_request_result(method: str, path: str, **kwargs) -> tuple[bool, dict | list | None, str | None]:
    """Make a request to the comics server, returning (ok, data, error)
    instead of printing + sys.exit'ing on failure. `_server_request` below is
    a thin wrapper that preserves the original sys.exit behavior for every
    pre-existing command; `add-batch` (BUI-360) calls this directly so a
    single row's failure doesn't abort the whole batch process — including a
    sequential batch loop, where a hang or a malformed response must degrade
    to a per-row failure rather than block/crash the whole run."""
    kwargs.setdefault("timeout", _DEFAULT_SERVER_TIMEOUT)
    url = f"{_server_url()}{path}"
    try:
        resp = getattr(requests, method)(url, **kwargs)
        resp.raise_for_status()
        try:
            return True, resp.json(), None
        except ValueError:
            # Covers json.JSONDecodeError / requests' JSONDecodeError — a 2xx
            # response with a non-JSON or truncated body. Treated as a
            # request failure (not raised) so a batch loop degrades to a
            # per-row failure instead of crashing with an unhandled traceback
            # and losing the report for rows already processed.
            return False, None, f"Server returned {resp.status_code} but the response body was not valid JSON"
    except requests.ConnectionError:
        return False, None, "Server unreachable. Is the comics server running?"
    except requests.Timeout:
        return False, None, "Server timed out."
    except requests.HTTPError as e:
        status_code = "unknown"
        detail = ""
        if e.response is not None:
            status_code = e.response.status_code
            try:
                detail = e.response.json().get("detail", "")
            except (ValueError, AttributeError):
                pass
        return False, None, f"Server returned {status_code}: {detail}"


def _server_request(method: str, path: str, **kwargs) -> dict | list:
    """Make a request to the comics server. Raises SystemExit on failure."""
    ok, data, error = _server_request_result(method, path, **kwargs)
    if not ok:
        click.echo(f"Error: {error}", err=True)
        sys.exit(1)
    return data


@click.group()
def cli():
    """Manage Gixen eBay snipes."""


@cli.command("list")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
@click.option(
    "--added-since",
    type=click.DateTime(formats=["%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"]),
    help="Only show snipes added via this CLI since the given time (ISO format)",
)
def list_snipes(as_json: bool, added_since: datetime | None):
    """Show all current snipes."""
    if _server_url():
        snipes = _server_request("get", "/api/snipes")
    else:
        client = _make_client()
        try:
            snipes = client.list_snipes()
        except GixenError as e:
            click.echo(f"Error: {e}", err=True)
            sys.exit(1)

    # Filter by --added-since using local add history
    if added_since:
        history = _load_add_history()
        since_ts = added_since.replace(tzinfo=timezone.utc).timestamp()
        added_ids = {
            item_id
            for item_id, ts in history.items()
            if ts >= since_ts
        }
        snipes = [s for s in snipes if s["item_id"] in added_ids]

    if as_json:
        click.echo(json.dumps(snipes, indent=2))
        return

    if not snipes:
        click.echo("No snipes found.")
        return

    def _is_ended(s: dict) -> bool:
        return (s.get("time_to_end") or "").upper() == "ENDED"

    active = [s for s in snipes if not _is_ended(s)]
    ended = [s for s in snipes if _is_ended(s)]

    if active:
        click.echo(click.style(f"Active Listings ({len(active)})", bold=True))
        click.echo(
            f"  {'Item':<17} {'Title':<40} {'Current':>10} {'Max Bid':>10} "
            f"{'Grp':>3} {'Time Left'}"
        )
        click.echo("  " + "-" * 99)
        for s in active:
            click.echo(
                f"  {s['item_id']:<17} "
                f"{s.get('title', '')[:38]:<40} "
                f"{_format_bid(s.get('current_bid', '')):>10} "
                f"{_format_bid(s.get('max_bid', '')):>10} "
                f"{_format_group(s.get('snipe_group', '')):>3} "
                f"{s.get('time_to_end', '')}"
            )
        click.echo()

    if ended:
        click.echo(click.style(f"Recently Ended ({len(ended)})", bold=True))
        click.echo(
            f"  {'Item':<17} {'Title':<40} {'Winning':>10} {'Max Bid':>10} "
            f"{'Grp':>3} {'Status'}"
        )
        click.echo("  " + "-" * 99)
        for s in ended:
            winning = s.get("current_bid", "")
            max_bid = s.get("max_bid", "")
            click.echo(
                f"  {s['item_id']:<17} "
                f"{s.get('title', '')[:38]:<40} "
                f"{_format_bid(winning):>10} "
                f"{_format_bid(max_bid):>10} "
                f"{_format_group(s.get('snipe_group', '')):>3} "
            )
        click.echo()

    click.echo(f"{len(snipes)} snipe(s) total")


def _format_group(group_str: str | int | None) -> str:
    """Display a snipe_group: blank for '0' / missing, else the number."""
    if not group_str or str(group_str) == "0":
        return ""
    return str(group_str)


def _format_bid(bid_str: str | float | None) -> str:
    """Format a bid string like '41.00 USD' or float 41.0 to '$41.00'."""
    if bid_str is None:
        return ""
    bid_str = str(bid_str)
    if not bid_str:
        return ""
    parts = bid_str.strip().split()
    try:
        amount = Decimal(parts[0])
        return f"${amount:.2f}"
    except (InvalidOperation, IndexError):
        return bid_str


def _get_ebay_bid_count(item_id: str) -> int | None:
    """Return current bid count for an eBay listing, or None if unavailable."""
    try:
        resp = requests.get(
            f"https://www.ebay.com/itm/{item_id}",
            headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"},
            timeout=10,
        )
        resp.raise_for_status()
        m = re.search(r'x-bid-count.*?<span[^>]*>(\d+)\s*bid', resp.text, re.IGNORECASE | re.DOTALL)
        if m:
            return int(m.group(1))
    except Exception:  # noqa: BLE001  # best-effort bid-count scrape; any failure → None
        pass
    return None


@cli.command()
@click.argument("item_id")
@click.argument("max_bid")
@click.option("--offset", default=6, help="Seconds before end to place bid (1-15)")
@click.option("--group", default=0, help="Snipe group (0=none, 1-10)")
@click.option(
    "--catalog-id",
    type=int,
    default=None,
    help="External LOCG catalog id (locg_id) for post-bid FMV linking. "
         "Use --comic-id if you have the internal comics.id from gixen-overlay.",
)
@click.option(
    "--comic-id",
    type=int,
    default=None,
    help="Internal gixen-overlay comics.id for post-bid FMV linking. "
         "Takes precedence over --catalog-id if both are given.",
)
@click.option("--grade", type=float, default=None, help="Numeric condition grade for post-bid FMV linking")
@click.option("--seller", default=None, help="eBay seller username (BUI-78 seller-reliability)")
@click.option("--seller-grade", type=float, default=None, help="Seller's stated grade (CGC float, BUI-78)")
@click.option("--photo-grade", type=float, default=None, help="Photo-assessed consensus grade (CGC float, BUI-78)")
def add(
    item_id: str,
    max_bid: str,
    offset: int,
    group: int,
    catalog_id: int | None,
    comic_id: int | None,
    grade: float | None,
    seller: str | None,
    seller_grade: float | None,
    photo_grade: float | None,
):
    """Add a snipe for an eBay item."""
    try:
        bid = Decimal(max_bid)
    except InvalidOperation:
        click.echo(f"Error: Invalid bid amount: {max_bid}", err=True)
        sys.exit(1)

    if comic_id is not None and catalog_id is not None:
        click.echo(
            f"⚠️  Both --comic-id and --catalog-id provided; using --comic-id "
            f"({comic_id}) and ignoring --catalog-id ({catalog_id}).",
            err=True,
        )
        catalog_id = None

    if _server_url():
        # BUI-78: pass seller + grades when supplied (server stores + lowercases
        # seller); BUI-360 factors the payload shape into add_batch.py so
        # `add` and `add-batch` can't silently drift on a future field.
        payload = build_bid_payload(
            item_id, bid, offset, group,
            seller=seller, seller_grade=seller_grade, photo_grade=photo_grade,
        )
        resp = _server_request("post", "/api/bids", json=payload)
        # BUI-67: the server upserts. created=False means an existing live snipe
        # was updated in place — don't reset the add-history timestamp (that drives
        # the --added-since window), and tell the user it was an update so an
        # accidental re-add (e.g. a lowered max bid) is visible.
        created = created_from_response(resp)
        if created:
            _record_add(item_id)
        verb = "Added" if created else "Updated existing snipe"

        link_attempted = grade is not None and (comic_id is not None or catalog_id is not None)
        link_ok = True
        if link_attempted:
            if comic_id is not None:
                link_body = {"comic_id": comic_id, "grade": grade}
                link_desc = f"comic_id={comic_id}, grade={grade}"
            else:
                link_body = {"locg_id": catalog_id, "grade": grade}
                link_desc = f"locg_id={catalog_id}, grade={grade}"
            try:
                _server_request(
                    "post",
                    f"/api/bids/{item_id}/link-fmv",
                    json=link_body,
                )
            except SystemExit:
                link_ok = False
                click.echo(
                    f"⚠️  Snipe {'added' if created else 'updated'} but FMV link failed "
                    f"for {item_id} ({link_desc})",
                    err=True,
                )

        if link_attempted and link_ok:
            click.echo(f"✅ {verb} + linked: {item_id} (max bid {bid})")
        elif link_attempted and not link_ok:
            click.echo(f"⚠️  {verb} (FMV link failed): {item_id} (max bid {bid})")
        elif created:
            click.echo(f"Added snipe for {item_id} with max bid {bid}")
        else:
            click.echo(f"Updated existing snipe for {item_id} with max bid {bid}")
        return

    # Existing direct-Gixen path
    if seller is not None or seller_grade is not None or photo_grade is not None:
        click.echo(
            "⚠️  --seller/--seller-grade/--photo-grade require COMICS_SERVER_URL "
            "(server mode); ignored in direct-Gixen mode.",
            err=True,
        )
    client = _make_client()
    try:
        existing = client.list_snipes()
        for s in existing:
            if s["item_id"] == item_id:
                existing_bid = s.get("max_bid", "?")
                click.echo(
                    f"Error: Snipe already exists for {item_id} "
                    f"with max bid {existing_bid}. "
                    f"Use `edit {item_id} {max_bid}` to change it.",
                    err=True,
                )
                sys.exit(1)
        client.add_snipe(item_id, bid, bid_offset=offset, snipe_group=group)
        _record_add(item_id)
        click.echo(f"Added snipe for {item_id} with max bid {bid}")

        # Warn if there are no bids — sellers can only end auctions early when
        # no bids have been placed.
        bid_count = _get_ebay_bid_count(item_id)
        if bid_count == 0:
            click.echo(
                f"\n0 bids on this listing — place a minimum bid now to prevent "
                f"the seller from ending early:\n"
                f"  https://www.ebay.com/itm/{item_id}"
            )
        elif bid_count is None:
            # Scrape failed — fall back to price heuristic
            snipes = client.list_snipes()
            for s in snipes:
                if s["item_id"] == item_id:
                    current_bid_str = s.get("current_bid", "")
                    try:
                        current_val = Decimal(current_bid_str.split()[0])
                        if current_val < Decimal("2.00") or str(current_val).endswith(".99"):
                            click.echo(
                                f"\nCurrent bid is {_format_bid(current_bid_str)} — couldn't verify bid count, "
                                f"but this looks like it may have no bids yet.\n"
                                f"Consider placing a minimum bid to prevent early ending:\n"
                                f"  https://www.ebay.com/itm/{item_id}"
                            )
                    except (InvalidOperation, IndexError):
                        pass
                    break

    except GixenError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


_STATUS_ICON = {
    STATUS_ADDED: "✅ Added",
    STATUS_UPDATED: "🔄 Updated",
    STATUS_FAILED: "❌ Failed",
    STATUS_NOT_ATTEMPTED: "⏸️  Not attempted",
}


def _add_batch_status_cell(row: dict) -> str:
    label = _STATUS_ICON.get(row["status"], row["status"])
    if row["status"] == STATUS_FAILED and row.get("error"):
        return f"{label} ({row['error']})"
    if row["status"] in (STATUS_ADDED, STATUS_UPDATED) and row.get("link_attempted"):
        if row.get("link_ok"):
            return f"{label} + linked"
        detail = row.get("link_error")
        return f"{label} (FMV link failed: {detail})" if detail else f"{label} (FMV link failed)"
    return label


def _print_add_batch_table(rows: list[dict]) -> None:
    click.echo(
        f"{'#':<4}{'Item ID':<16}{'Grade':<8}{'Max Bid':<12}{'Status'}"
    )
    click.echo("-" * 80)
    for i, row in enumerate(rows, start=1):
        grade = row.get("grade")
        max_bid = row.get("max_bid")
        click.echo(
            f"{i:<4}"
            f"{(row.get('item_id') or '—'):<16}"
            f"{(f'{grade:g}' if grade is not None else '—'):<8}"
            f"{(_format_bid(max_bid) if max_bid is not None else '—'):<12}"
            f"{_add_batch_status_cell(row)}"
        )


@cli.command("add-batch")
@click.argument("rows_file", type=click.Path(exists=True, dir_okay=False))
@click.option(
    "--verify",
    is_flag=True,
    help="POST every landed (added/updated) row to /api/comics/verify and "
         "append the verdict to its result.",
)
@click.option(
    "--json-out",
    type=click.Path(dir_okay=False, writable=True),
    default=None,
    help="Also write the JSON result summary to this file.",
)
def add_batch_cmd(rows_file: str, verify: bool, json_out: str | None):
    """Add a batch of snipes strictly sequentially, with BUI-168 failure
    semantics built in (BUI-360).

    ROWS_FILE is a JSON list of rows (or an object with a top-level "rows"
    list). Required per row: item_id (str), max_bid (number). Optional:
    comic_id (int), grade (number), seller (str), seller_grade (number),
    photo_grade (number), group (int, default 0), offset (int, default 6).
    item_id must be unique across rows in one file (the server upserts on
    item_id, so a duplicate would collapse into one bid). Reuses the same
    server-mode request path as `gixen add` (POST /api/bids, then POST
    .../link-fmv when grade+comic_id are both given) rather than re-deriving
    it — --comic-id/--catalog-id ambiguity doesn't apply here: this row
    schema only has comic_id.

    On a failed row: mark it failed with the error, then re-check server
    health before the next row. If the server is down, halt the batch and
    report every remaining row as not-attempted — never keep firing adds at
    a dead server, and never print an all-success summary after a failure.
    Exits non-zero if any row failed or was left not-attempted.
    """
    server_url = _server_url()
    if not server_url:
        click.echo(
            "Error: COMICS_SERVER_URL is not set. add-batch requires the "
            "comics server (BUI-360/BUI-168) — set the variable and confirm "
            "the server is running before continuing.",
            err=True,
        )
        sys.exit(1)

    if json_out and Path(json_out).resolve() == Path(rows_file).resolve():
        click.echo(
            f"Error: --json-out ({json_out}) is the same file as ROWS_FILE — "
            "refusing, since writing the result would destroy the original "
            "batch input before a halted/failed run could be retried from it.",
            err=True,
        )
        sys.exit(1)

    try:
        raw = json.loads(Path(rows_file).read_text())
    except (OSError, json.JSONDecodeError) as e:
        click.echo(f"Error: could not read/parse {rows_file}: {e}", err=True)
        sys.exit(1)

    try:
        rows = parse_rows(raw)
    except AddBatchError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)

    if not rows:
        click.echo("No rows to add.")
        _emit_add_batch_result(BatchOutcome(), json_out)
        sys.exit(0)

    def _health_check() -> bool:
        return _server_request_result("get", "/health")[0]

    if not _health_check():
        click.echo(
            f"Error: The comics server at {server_url} is not responding. "
            "Halting before any adds — no rows attempted.",
            err=True,
        )
        outcome = BatchOutcome(
            rows=[
                RowResult(item_id=r.get("item_id"), status=STATUS_NOT_ATTEMPTED)
                for r in rows
            ],
            halted=True,
        )
        _emit_add_batch_result(outcome, json_out)
        sys.exit(1)

    outcome = run_batch(rows, server_request=_server_request_result, health_check=_health_check)

    # Record-add history only for genuine new creates — mirrors `add`'s own
    # BUI-67 rule (an in-place update must not reset the --added-since
    # window). Derived from `outcome.rows` (already computed by run_batch)
    # rather than re-detecting "created" via a second, independent check —
    # `RowResult.status == STATUS_ADDED` *is* the created signal.
    created_item_ids = [r.item_id for r in outcome.rows if r.status == STATUS_ADDED and r.item_id]
    if created_item_ids:
        _record_adds(created_item_ids)

    if verify:
        items = verify_items(outcome)
        if items:
            ok, resp, err = _server_request_result(
                "post", "/api/comics/verify", json={"items": items}
            )
            if ok and isinstance(resp, dict):
                apply_verify_results(outcome, resp)
            else:
                outcome.verify_error = err or "verify call returned no parseable JSON"

    _emit_add_batch_result(outcome, json_out)

    if outcome.verify_error:
        click.echo(f"⚠️  --verify: {outcome.verify_error}", err=True)

    sys.exit(outcome.exit_code())


def _emit_add_batch_result(outcome: BatchOutcome, json_out: str | None) -> None:
    """Print the human table + JSON summary (serialized once, reused for
    both the stdout echo and --json-out) — shared by every exit path of
    `add_batch_cmd` (empty-rows, pre-flight-halted, and normal completion)
    so `--json-out` is honored consistently regardless of how the run ended."""
    _print_add_batch_table([r.to_dict() for r in outcome.rows])
    text = json.dumps(outcome.to_dict(), indent=2)
    click.echo(text)
    if json_out:
        try:
            Path(json_out).write_text(text)
        except OSError as e:
            # The batch's own success/failure already happened and is fully
            # represented in `text` above (stdout has it) — a failure to
            # ALSO persist it to --json-out is reported, but must not raise
            # past this point and turn an otherwise-successful batch's exit
            # code into an unrelated traceback (cli.py's caller still exits
            # via `outcome.exit_code()`, not this write).
            click.echo(f"warning: could not write --json-out ({json_out}): {e}", err=True)


@cli.command()
@click.argument("item_id")
@click.argument("max_bid")
@click.option("--offset", default=6, help="Seconds before end to place bid (1-15)")
@click.option("--group", default=0, help="Snipe group (0=none, 1-10)")
def edit(item_id: str, max_bid: str, offset: int, group: int):
    """Change the bid on an existing snipe."""
    try:
        bid = Decimal(max_bid)
    except InvalidOperation:
        click.echo(f"Error: Invalid bid amount: {max_bid}", err=True)
        sys.exit(1)

    if _server_url():
        payload = {
            "max_bid": float(bid),
            "bid_offset": offset,
            "snipe_group": group,
        }
        _server_request("patch", f"/api/bids/{item_id}", json=payload)
        click.echo(f"Updated snipe for {item_id} to max bid {bid}")
        return

    client = _make_client()
    try:
        client.modify_snipe(item_id, bid, bid_offset=offset, snipe_group=group)
        click.echo(f"Updated snipe for {item_id} to max bid {bid}")
    except GixenSnipeNotFoundError:
        click.echo(f"Error: Item {item_id} not found in your snipe list", err=True)
        sys.exit(1)
    except GixenError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@cli.command("group")
@click.argument("group_n", type=click.IntRange(0, 10))
@click.argument("item_ids", nargs=-1, required=True)
def group_cmd(group_n: int, item_ids: tuple[str, ...]):
    """Assign one or more existing snipes to a group (0=ungroup, 1-10).

    Preserves each snipe's existing max bid and offset.
    """
    client = _make_client()
    try:
        snipes = client.list_snipes()
    except GixenError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)

    by_id = {s["item_id"]: s for s in snipes}

    missing = [iid for iid in item_ids if iid not in by_id]
    if missing:
        click.echo(
            f"Error: not in snipe list: {', '.join(missing)}", err=True
        )
        sys.exit(1)

    failures: list[tuple[str, str]] = []
    for iid in item_ids:
        s = by_id[iid]
        try:
            current_bid = Decimal(s["max_bid"])
        except (InvalidOperation, KeyError):
            failures.append((iid, f"unparseable max bid {s.get('max_bid')!r}"))
            click.echo(
                f"  {iid}: skipped — unparseable max bid "
                f"{s.get('max_bid')!r}",
                err=True,
            )
            continue
        try:
            offset = int(s.get("bid_offset", "6"))
        except ValueError:
            offset = 6
        try:
            client.modify_snipe(
                iid, current_bid, bid_offset=offset, snipe_group=group_n
            )
            click.echo(f"  {iid}: group -> {group_n}")
        except GixenError as e:
            failures.append((iid, str(e)))
            click.echo(f"  {iid}: failed — {e}", err=True)

    ok = len(item_ids) - len(failures)
    click.echo(f"Updated {ok} of {len(item_ids)} snipe(s).")
    if failures:
        sys.exit(1)


@cli.command()
@click.argument("item_id")
def remove(item_id: str):
    """Remove a snipe."""
    if _server_url():
        _server_request("delete", f"/api/bids/{item_id}")
        click.echo(f"Removed snipe for {item_id}")
        return

    client = _make_client()
    try:
        client.remove_snipe(item_id)
        click.echo(f"Removed snipe for {item_id}")
    except GixenSnipeNotFoundError:
        click.echo(f"Error: Item {item_id} not found in your snipe list", err=True)
        sys.exit(1)
    except GixenError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@cli.command()
def sync():
    """Sync server DB with live Gixen (picks up snipes added via the web UI)."""
    if not _server_url():
        click.echo("Error: COMICS_SERVER_URL not set — sync only applies to server mode.", err=True)
        sys.exit(1)
    result = _server_request("post", "/api/sync")
    click.echo(f"Synced {result.get('synced', '?')} snipes from Gixen.")


@cli.command("record-win-prep")
@click.option(
    "--output",
    type=click.Path(dir_okay=False, writable=True),
    default=None,
    help="Write the JSON result to this file instead of stdout.",
)
def record_win_prep_cmd(output: str | None):
    """Build the /comic:collection-add record-win payload in one call — see
    record_win_prep.py for what it does and why (BUI-352/353/354).

    Requires COMICS_SERVER_URL: this command fetches the seen-set from the
    comics server, so a missing/unset URL here is a hard stop.

    Prints the JSON result to stdout, or writes it to --output if given.
    """
    server_url = _server_url()
    if not server_url:
        click.echo(
            "Error: COMICS_SERVER_URL is not set. record-win-prep needs the "
            "comics server to fetch the seen-set and cannot safely proceed "
            "without it (BUI-352) — source scripts/comics-server.sh and run "
            "comics_resolve_server first.",
            err=True,
        )
        sys.exit(1)

    snipes = _server_request("get", "/api/snipes")

    try:
        payload = build_payload(snipes, server_url)
    except RecordWinPrepError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)

    text = json.dumps(payload, indent=2)
    if output:
        Path(output).write_text(text)
        click.echo(
            f"Wrote {output}: {payload['new_win_count']} new win(s) "
            f"({len(payload['wins'])} ready to record, "
            f"{len(payload['needs_review'])} need review)."
        )
    else:
        click.echo(text)


@cli.command()
@click.option(
    "--dry-run",
    is_flag=True,
    help="Show what would be purged/removed without making changes",
)
@click.option(
    "--yes",
    "-y",
    is_flag=True,
    help="Skip the confirmation prompt when sibling snipes will be removed",
)
def purge(dry_run: bool, yes: bool):
    """Remove completed snipes (and sibling snipes from groups with a win)."""
    if _server_url():
        if dry_run:
            click.echo("Would purge completed snipes.")
            return
        result = _server_request("post", "/api/purge", json={"sibling_ids": []})
        click.echo(f"Purged {result['purged_completed']} completed snipe(s)")
        if result["removed_siblings"]:
            click.echo(f"Removed {result['removed_siblings']} sibling snipe(s)")
        return

    # Existing direct-Gixen path (unchanged)
    client = _make_client()
    try:
        snipes = client.list_snipes()
    except GixenError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)

    siblings = find_sibling_cleanup_targets(snipes)

    if not siblings:
        if dry_run:
            click.echo("Would purge completed snipes")
            return
        try:
            client.purge_completed()
        except GixenError as e:
            click.echo(f"Error: {e}", err=True)
            sys.exit(1)
        click.echo("Purged completed snipes")
        return

    completed_count = sum(
        1
        for s in snipes
        if s.get("status") in ("WON", "LOST", "FAILED", "ENDED")
    )

    click.echo(
        f"This will purge {completed_count} completed snipe(s) and remove "
        f"{len(siblings)} sibling snipe(s) from groups with a win:"
    )
    for s in siblings:
        title = (s.get("title") or "")[:40]
        click.echo(
            f"  group {s.get('snipe_group', '?')}: "
            f"{s['item_id']} \"{title}\" (was {s.get('status') or '?'})"
        )

    if dry_run:
        click.echo("Dry run — no changes made.")
        return

    if not yes and not click.confirm("Continue?", default=False):
        click.echo("Aborted.")
        return

    try:
        client.purge_completed()
    except GixenError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)
    click.echo("Purged completed snipes")

    removed: list[dict] = []
    failures: list[tuple[str, str]] = []
    for s in siblings:
        try:
            client.remove_snipe(s["item_id"])
            removed.append(s)
        except GixenError as e:
            failures.append((s["item_id"], str(e)))
            click.echo(
                f"  failed to remove {s['item_id']}: {e}", err=True
            )

    if removed:
        click.echo(
            f"Removed {len(removed)} sibling snipe(s) from groups with a win."
        )

    if failures:
        sys.exit(1)


HISTORY_FILE = Path(__file__).parent / ".gixen_history.json"


def _load_add_history() -> dict[str, float]:
    """Load {item_id: unix_timestamp} from local history file."""
    if not HISTORY_FILE.exists():
        return {}
    try:
        return json.loads(HISTORY_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _record_add(item_id: str) -> None:
    """Record that a snipe was added now."""
    history = _load_add_history()
    history[item_id] = datetime.now(timezone.utc).timestamp()
    HISTORY_FILE.write_text(json.dumps(history))


def _record_adds(item_ids: list[str]) -> None:
    """Bulk form of `_record_add` — one read-modify-write for the whole list
    instead of one per item_id (add-batch's per-row equivalent).

    Called mid-batch, after real bids may already have landed on the live
    server and before add-batch has emitted its JSON/table summary — a local
    filesystem hiccup here (disk full, permissions) must not raise past this
    point and cost the caller the whole batch report over what is, at worst,
    a stale `--added-since` filter."""
    if not item_ids:
        return
    history = _load_add_history()
    now = datetime.now(timezone.utc).timestamp()
    for item_id in item_ids:
        history[item_id] = now
    try:
        HISTORY_FILE.write_text(json.dumps(history))
    except OSError as e:
        click.echo(f"warning: could not update add-history file: {e}", err=True)


@cli.command("ebay-auth")
def ebay_auth():
    """Open a browser window to log in to eBay and save the session locally."""
    click.echo("Opening eBay login — sign in, then press Enter in this terminal.")
    asyncio.run(ebay_bidder.setup_session())


@cli.command("bid")
@click.argument("item_id")
@click.argument("max_bid", type=float)
@click.option("--dry-run", is_flag=True, help="Load bid page but don't click confirm.")
def bid_now(item_id: str, max_bid: float, dry_run: bool):
    """Place an eBay bid immediately via local browser automation."""
    if not item_id.isdigit():
        click.echo("Error: item_id must be numeric", err=True)
        sys.exit(1)
    click.echo(f"Placing bid: item={item_id} max_bid=${max_bid:.2f}{' (dry run)' if dry_run else ''}")
    result = asyncio.run(ebay_bidder.place_bid(item_id, max_bid, dry_run=dry_run))
    if result["success"]:
        click.echo(click.style(f"✓ {result['message']}", fg="green"))
    else:
        click.echo(click.style(f"✗ {result['message']}", fg="red"))
        sys.exit(1)


if __name__ == "__main__":
    cli()
