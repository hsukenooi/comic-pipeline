"""Gixen CLI — manage eBay snipes from the command line."""

import os
import sys
import click
from decimal import Decimal, InvalidOperation
from dotenv import load_dotenv

from gixen_client import (
    GixenClient,
    GixenError,
    GixenLoginError,
    GixenSnipeNotFoundError,
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
def list_snipes():
    """Show all current snipes."""
    client = _make_client()
    try:
        snipes = client.list_snipes()
    except GixenError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)

    if not snipes:
        click.echo("No snipes found.")
        return

    active = [s for s in snipes if s.get("time_to_end", "").upper() != "ENDED"]
    ended = [s for s in snipes if s.get("time_to_end", "").upper() == "ENDED"]

    if active:
        click.echo(click.style(f"Active Listings ({len(active)})", bold=True))
        click.echo(
            f"  {'Item':<17} {'Title':<40} {'Current':>10} {'Max Bid':>10} {'Time Left'}"
        )
        click.echo("  " + "-" * 95)
        for s in active:
            click.echo(
                f"  {s['item_id']:<17} "
                f"{s.get('title', '')[:38]:<40} "
                f"{_format_bid(s.get('current_bid', '')):>10} "
                f"{_format_bid(s.get('max_bid', '')):>10} "
                f"{s.get('time_to_end', '')}"
            )
        click.echo()

    if ended:
        click.echo(click.style(f"Recently Ended ({len(ended)})", bold=True))
        click.echo(
            f"  {'Item':<17} {'Title':<40} {'Winning':>10} {'Max Bid':>10} {'Diff':>10}"
        )
        click.echo("  " + "-" * 95)
        for s in ended:
            winning = s.get("current_bid", "")
            max_bid = s.get("max_bid", "")
            diff = _calc_diff(max_bid, winning)
            click.echo(
                f"  {s['item_id']:<17} "
                f"{s.get('title', '')[:38]:<40} "
                f"{_format_bid(winning):>10} "
                f"{_format_bid(max_bid):>10} "
                f"{diff:>10}"
            )
        click.echo()

    click.echo(f"{len(snipes)} snipe(s) total")


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
        client.add_snipe(item_id, bid, bid_offset=offset, snipe_group=group)
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
def purge():
    """Remove completed/ended snipes."""
    client = _make_client()
    try:
        client.purge_completed()
        click.echo("Purged completed snipes")
    except GixenError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


if __name__ == "__main__":
    cli()
