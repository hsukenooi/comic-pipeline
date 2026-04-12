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


@cli.command()
def list():
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

    # Header
    click.echo(
        f"{'Item ID':<15} {'Max Bid':>8} {'Current':>12} {'Time Left':<14} {'Status':<10} {'Title'}"
    )
    click.echo("-" * 95)

    for s in snipes:
        click.echo(
            f"{s['item_id']:<15} "
            f"{s['max_bid']:>8} "
            f"{s.get('current_bid', ''):>12} "
            f"{s.get('time_to_end', ''):.<14} "
            f"{s.get('status', ''):.<10} "
            f"{s.get('title', '')[:35]}"
        )

    click.echo(f"\n{len(snipes)} snipe(s)")


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
