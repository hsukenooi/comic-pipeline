"""Gixen CLI — manage eBay snipes from the command line."""

import json
import os
import sys
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

import click
from dotenv import load_dotenv

from gixen_client import (
    GixenClient,
    GixenError,
    GixenLoginError,
    GixenSnipeNotFoundError,
    find_sibling_cleanup_targets,
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

    active = [s for s in snipes if s.get("time_to_end", "").upper() != "ENDED"]
    ended = [s for s in snipes if s.get("time_to_end", "").upper() == "ENDED"]

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
            f"{'Grp':>3} {'Diff':>10}"
        )
        click.echo("  " + "-" * 99)
        for s in ended:
            winning = s.get("current_bid", "")
            max_bid = s.get("max_bid", "")
            diff = _calc_diff(max_bid, winning)
            click.echo(
                f"  {s['item_id']:<17} "
                f"{s.get('title', '')[:38]:<40} "
                f"{_format_bid(winning):>10} "
                f"{_format_bid(max_bid):>10} "
                f"{_format_group(s.get('snipe_group', '')):>3} "
                f"{diff:>10}"
            )
        click.echo()

    click.echo(f"{len(snipes)} snipe(s) total")


def _format_group(group_str: str) -> str:
    """Display a snipe_group: blank for '0' / missing, else the number."""
    if not group_str or group_str == "0":
        return ""
    return group_str


def _format_bid(bid_str: str) -> str:
    """Format a bid string like '41.00 USD' to '$41.00'."""
    if not bid_str:
        return ""
    parts = bid_str.strip().split()
    try:
        amount = Decimal(parts[0])
        return f"${amount:.2f}"
    except (InvalidOperation, IndexError):
        return bid_str


def _calc_diff(max_bid: str, winning_bid: str) -> str:
    """Calculate difference between max bid and winning bid."""
    try:
        max_val = Decimal(max_bid.split()[0]) if " " in max_bid else Decimal(max_bid)
        win_val = Decimal(winning_bid.split()[0]) if " " in winning_bid else Decimal(winning_bid)
        diff = max_val - win_val
        if diff >= 0:
            return click.style(f"+${diff:.2f}", fg="green")
        else:
            return click.style(f"-${abs(diff):.2f}", fg="red")
    except (InvalidOperation, ValueError):
        return ""


@cli.command()
@click.argument("item_id")
@click.argument("max_bid")
@click.option("--offset", default=6, help="Seconds before end to place bid (1-15)")
@click.option("--group", default=0, help="Snipe group (0=none, 1-10)")
def add(item_id: str, max_bid: str, offset: int, group: int):
    """Add a snipe for an eBay item."""
    try:
        bid = Decimal(max_bid)
    except InvalidOperation:
        click.echo(f"Error: Invalid bid amount: {max_bid}", err=True)
        sys.exit(1)

    client = _make_client()
    try:
        # Check for existing snipe first
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
    client = _make_client()
    try:
        snipes = client.list_snipes()
    except GixenError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)

    siblings = find_sibling_cleanup_targets(snipes)

    # Fast path: no group siblings to clean. Preserve the original
    # no-prompt, single-line behaviour (including no trailing period).
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


if __name__ == "__main__":
    cli()
