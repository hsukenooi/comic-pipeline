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

    # Bucket by status when the server returned a terminal one (server-mode
    # /api/snipes returns WON/LOST/FAILED/ENDED rows for the dashboard's
    # "Recently Ended" section). Fall back to time_to_end for rows that lack
    # a terminal status — matches direct-Gixen-mode where we only get the
    # relative time string.
    _TERMINAL = {"WON", "LOST", "FAILED", "ENDED"}
    def _is_ended(s: dict) -> bool:
        if (s.get("status") or "").upper() in _TERMINAL:
            return True
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
            mirror = s.get("status_mirror", "")
            status_display = s.get("status", "")
            if mirror and mirror not in ("N/A", status_display):
                status_display += f" / mirror: {mirror}"
            click.echo(
                f"  {s['item_id']:<17} "
                f"{s.get('title', '')[:38]:<40} "
                f"{_format_bid(winning):>10} "
                f"{_format_bid(max_bid):>10} "
                f"{_format_group(s.get('snipe_group', '')):>3} "
                f"{status_display}"
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
@click.option("--comic", default=None, help="Comic title (e.g. 'Amazing Spider-Man')")
@click.option("--issue", default=None, help="Issue number (e.g. '300')")
@click.option("--year", default=None, type=int, help="Publication year")
@click.option("--grade", default=None, type=float, help="CGC grade (e.g. 9.2)")
@click.option("--fmv-low", default=None, type=float, help="FMV range low end")
@click.option("--fmv-high", default=None, type=float, help="FMV range high end")
@click.option("--fmv-comps", default=None, type=int, help="Number of comps used")
@click.option("--fmv-confidence", default=None, help="FMV confidence: high/medium/low")
@click.option("--fmv-notes", default=None, help="FMV notes")
@click.option("--locg-id", default=None, type=int, help="LOCG canonical comic ID")
@click.option("--locg-variant-id", default=None, type=int, help="LOCG variant comic ID (if different from --locg-id)")
def add(item_id: str, max_bid: str, offset: int, group: int,
        comic: str | None, issue: str | None, year: int | None, grade: float | None,
        fmv_low: float | None, fmv_high: float | None,
        fmv_comps: int | None, fmv_confidence: str | None, fmv_notes: str | None,
        locg_id: int | None, locg_variant_id: int | None):
    """Add a snipe for an eBay item."""
    try:
        bid = Decimal(max_bid)
    except InvalidOperation:
        click.echo(f"Error: Invalid bid amount: {max_bid}", err=True)
        sys.exit(1)

    if _server_url():
        payload = {
            "item_id": item_id,
            "max_bid": float(bid),
            "bid_offset": offset,
            "snipe_group": group,
        }
        if comic:
            payload.update({
                "comic": comic, "issue": issue, "year": year,
                "grade": grade, "fmv_low": fmv_low, "fmv_high": fmv_high,
                "fmv_comps": fmv_comps, "fmv_confidence": fmv_confidence,
                "fmv_notes": fmv_notes,
                "locg_id": locg_id, "locg_variant_id": locg_variant_id,
            })
        _server_request("post", "/api/bids", json=payload)
        _record_add(item_id)
        click.echo(f"Added snipe for {item_id} with max bid {bid}")
        return

    # Existing direct-Gixen path
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
@click.option("--locg-id", default=None, type=int, help="LOCG canonical comic ID")
@click.option("--locg-variant-id", default=None, type=int, help="LOCG variant comic ID (if different from --locg-id)")
def edit(item_id: str, max_bid: str, offset: int, group: int,
         locg_id: int | None, locg_variant_id: int | None):
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
        if locg_id is not None:
            payload["locg_id"] = locg_id
        if locg_variant_id is not None:
            payload["locg_variant_id"] = locg_variant_id
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


@cli.group("locg")
def locg_cmd():
    """Manage LOCG (League of Comic Geeks) ID linking on existing snipes."""


@locg_cmd.command("link")
@click.argument("item_id")
@click.argument("locg_id", type=int)
@click.option(
    "--issue",
    default=None,
    help="Specific issue within a lot (e.g. '2' for the 2nd issue of a 5-issue lot). "
    "Without this flag the bid's primary comic is updated.",
)
@click.option(
    "--variant-id",
    default=None,
    type=int,
    help="LOCG variant comic ID (if different from locg-id)",
)
def locg_link(item_id: str, locg_id: int, issue: str | None, variant_id: int | None):
    """Persist a resolved LOCG ID against a comic in a bid.

    Without --issue: targets the bid's primary comic. With --issue N: targets
    that issue within the bid; auto-creates the comic row + junction entry if
    the parser hadn't expanded the lot to that issue yet.
    """
    if not _server_url():
        click.echo(
            "Error: GIXEN_SERVER_URL must be set — locg link is server-only.",
            err=True,
        )
        sys.exit(1)

    payload: dict = {"locg_id": locg_id}
    if issue is not None:
        payload["issue"] = issue
    if variant_id is not None:
        payload["locg_variant_id"] = variant_id

    resp = _server_request(
        "post", f"/api/bids/{item_id}/comics/locg", json=payload
    )
    if not isinstance(resp, dict):  # safety: server should return a dict
        click.echo(f"Unexpected server response: {resp}", err=True)
        sys.exit(1)
    issue_str = resp.get("issue") or "?"
    is_primary = "primary" if resp.get("is_primary") else "secondary"
    click.echo(
        f"Linked LOCG {resp['locg_id']} to {item_id} #{issue_str} ({is_primary})"
    )


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


@cli.command("extract-comics")
def extract_comics_cmd():
    """Auto-link bids to comics by parsing cached eBay listing titles."""
    if not _server_url():
        click.echo(
            "Error: GIXEN_SERVER_URL not set — extract-comics only applies to server mode.",
            err=True,
        )
        sys.exit(1)
    result = _server_request("post", "/api/extract-comics")
    processed = result.get("processed", 0)
    linked = result.get("linked", 0)
    skipped = result.get("skipped", []) or []
    errors = result.get("errors", []) or []
    click.echo(f"Processed {processed}, linked {linked}, skipped {len(skipped)}, errors {len(errors)}.")
    for s in skipped:
        click.echo(f"  skip {s.get('item_id', '?')}: {s.get('reason', 'unknown')}")
    for e in errors:
        click.echo(f"  err  {e}", err=True)


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
