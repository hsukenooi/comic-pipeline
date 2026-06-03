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


def _server_url() -> str | None:
    return os.getenv("GIXEN_SERVER_URL", "").rstrip("/") or None


def _server_request(method: str, path: str, **kwargs) -> dict | list:
    """Make a request to the gixen server. Raises SystemExit on failure."""
    url = f"{_server_url()}{path}"
    try:
        resp = getattr(requests, method)(url, **kwargs)
        resp.raise_for_status()
        return resp.json()
    except requests.ConnectionError:
        click.echo("Error: Server unreachable. Is the gixen server running?", err=True)
        sys.exit(1)
    except requests.Timeout:
        click.echo("Error: Server timed out.", err=True)
        sys.exit(1)
    except requests.HTTPError as e:
        status_code = "unknown"
        detail = ""
        if e.response is not None:
            status_code = e.response.status_code
            try:
                detail = e.response.json().get("detail", "")
            except (ValueError, AttributeError):
                pass
        click.echo(f"Error: Server returned {status_code}: {detail}", err=True)
        sys.exit(1)


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


def _calc_diff(max_bid: str | float | None, winning_bid: str | float | None) -> str:
    """Calculate difference between max bid and winning bid."""
    try:
        max_str = str(max_bid)
        win_str = str(winning_bid) if winning_bid is not None else ""
        max_val = Decimal(max_str.split()[0]) if " " in max_str else Decimal(max_str)
        win_val = Decimal(win_str.split()[0]) if " " in win_str else Decimal(win_str)
        diff = max_val - win_val
        if diff >= 0:
            return click.style(f"+${diff:.2f}", fg="green")
        else:
            return click.style(f"-${abs(diff):.2f}", fg="red")
    except (InvalidOperation, ValueError):
        return ""


def _get_ebay_bid_count(item_id: str) -> int | None:
    """Return current bid count for an eBay listing, or None if unavailable."""
    try:
        import requests
        resp = requests.get(
            f"https://www.ebay.com/itm/{item_id}",
            headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"},
            timeout=10,
        )
        resp.raise_for_status()
        m = re.search(r'x-bid-count.*?<span[^>]*>(\d+)\s*bid', resp.text, re.IGNORECASE | re.DOTALL)
        if m:
            return int(m.group(1))
    except Exception:
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
        payload = {
            "item_id": item_id,
            "max_bid": float(bid),
            "bid_offset": offset,
            "snipe_group": group,
        }
        # BUI-78: pass seller + grades when supplied (server stores + lowercases
        # seller). Omit unset keys so the payload stays minimal.
        if seller is not None:
            payload["seller"] = seller
        if seller_grade is not None:
            payload["seller_grade"] = seller_grade
        if photo_grade is not None:
            payload["photo_grade"] = photo_grade
        resp = _server_request("post", "/api/bids", json=payload)
        # BUI-67: the server upserts. created=False means an existing live snipe
        # was updated in place — don't reset the add-history timestamp (that drives
        # the --added-since window), and tell the user it was an update so an
        # accidental re-add (e.g. a lowered max bid) is visible.
        created = resp.get("created", True) if isinstance(resp, dict) else True
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
            "⚠️  --seller/--seller-grade/--photo-grade require GIXEN_SERVER_URL "
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
        click.echo("Error: GIXEN_SERVER_URL not set — sync only applies to server mode.", err=True)
        sys.exit(1)
    result = _server_request("post", "/api/sync")
    click.echo(f"Synced {result.get('synced', '?')} snipes from Gixen.")


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
