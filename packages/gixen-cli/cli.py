"""Gixen CLI — manage eBay snipes from the command line."""

from __future__ import annotations

import importlib.metadata
import json
import math
import os
import re
import subprocess
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
    GIXEN_RAW_TERMINAL_STATUSES,
)
import ebay_bidder
from record_win_prep import RecordWinPrepError, build_payload
from add_batch import (
    AddBatchError,
    BatchOutcome,
    RECONCILE_SETTLE_SECONDS,
    RowResult,
    STATUS_ADDED,
    STATUS_BLOCKED,
    STATUS_FAILED,
    STATUS_INDETERMINATE,
    STATUS_NOT_ATTEMPTED,
    STATUS_UPDATED,
    advisories_from_response,
    apply_verify_results,
    build_batch_rows,
    build_bid_payload,
    created_from_response,
    parse_brief_rows,
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


def _parse_optional_int(value: object) -> int | None:
    """Parse a Gixen-scraped bid_offset/snipe_group to int, or None when
    unparseable/blank.

    Mirrors server/fallback.py's `_parse_snipe_group` contract (BUI-383) for
    snipe_group: None must never be coerced to 0 by a caller — group 0 is a
    positive "no group" claim, so treating an unparseable/blank value as 0
    could silently un-group a snipe whose real group we simply failed to
    read. `edit`'s direct-mode passthrough (BUI-404) applies the identical
    fail-closed contract to bid_offset: silently substituting a hardcoded
    default (6) for an unreadable current value is the same "silent reset"
    bug this fix exists to prevent, just on the other field.
    """
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    try:
        return int(s)
    except ValueError:
        return None


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


# BUI-697: raised from 15s. 15 was below the comics server's own worst-case
# CLI→server→Gixen chain: `GixenClient(timeout=15.0)` is a SEPARATE, inner
# timeout, so a slow Gixen could consume this entire outer budget and fire it
# while the write was still in flight. A bigger budget makes that rarer — it
# does NOT make it impossible (a stalled transfer can exceed any bound), which
# is why `add_batch.reconcile_indeterminate_rows` exists and is the actual fix.
_DEFAULT_SERVER_TIMEOUT = 60  # seconds
_SERVER_TIMEOUT_ENV = "COMICS_SERVER_TIMEOUT"
_RECONCILE_SETTLE_ENV = "ADD_BATCH_SETTLE_SECONDS"


def _env_float(name: str, default: float, *, minimum: float) -> float:
    """Read a float from the environment, falling back to `default` for an
    unset, unparseable, non-finite, or below-`minimum` value. Deliberately
    fail-safe rather than fail-loud, matching this system's existing
    `POLICY_BLOCK_*` parse convention: a typo must land on the safe default,
    never on a degenerate one (an accidental `0` timeout must not mean "wait
    forever"). Read per call, never cached at import (KTD2), so an operator
    can change it without a reinstall."""
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    if not math.isfinite(value) or value < minimum:
        return default
    return value


def _server_timeout() -> float:
    """The CLI→comics-server read timeout, overridable via
    `COMICS_SERVER_TIMEOUT` (seconds)."""
    # minimum is a positive epsilon, not 0: a 0/negative timeout would be
    # "block forever", which is strictly worse than the value it replaced.
    return _env_float(_SERVER_TIMEOUT_ENV, _DEFAULT_SERVER_TIMEOUT, minimum=0.001)


def _reconcile_settle_seconds() -> float:
    """How long `add-batch` lets an in-flight write settle before its BUI-697
    reconcile read, overridable via `ADD_BATCH_SETTLE_SECONDS`. Unlike the
    timeout above, 0 IS a legitimate value (read back immediately) — the
    reconcile itself, not the wait, is what makes the report honest."""
    return _env_float(_RECONCILE_SETTLE_ENV, RECONCILE_SETTLE_SECONDS, minimum=0.0)


def _server_request_result(method: str, path: str, **kwargs) -> tuple[bool, dict | list | None, str | None]:
    """Make a request to the comics server, returning (ok, data, error)
    instead of printing + sys.exit'ing on failure. `_server_request` below is
    a thin wrapper that preserves the original sys.exit behavior for every
    pre-existing command; `add-batch` (BUI-360) calls this directly so a
    single row's failure doesn't abort the whole batch process — including a
    sequential batch loop, where a hang or a malformed response must degrade
    to a per-row failure rather than block/crash the whole run."""
    kwargs.setdefault("timeout", _server_timeout())
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
        # BUI-697: a timeout is INDETERMINATE, not a failure. We stopped
        # waiting; the comics server did not stop working, and it frequently
        # commits the write afterwards (2026-08-07: 36 rows reported "failed",
        # 11 had landed; the 25-row retry reported all-failed and all 25
        # landed). The structured `{"indeterminate": True}` marker rides in
        # `data` — the same channel BUI-623 uses for `{"blocked": ...}` — so
        # callers branch on a flag instead of string-matching this message.
        return (
            False,
            {"indeterminate": True, "reason": "timeout"},
            "Server timed out — the write may still have committed.",
        )
    except requests.HTTPError as e:
        status_code = "unknown"
        detail = ""
        if e.response is not None:
            status_code = e.response.status_code
            try:
                detail = e.response.json().get("detail", "")
            except (ValueError, AttributeError):
                pass
        # BUI-623 (U9): a policy block's 409 `detail` is a structured KTD4
        # envelope (`{blocked, message, advisories, blocking_codes,
        # surviving_snipe, ...}`), not a plain string — surface it as `data`
        # too (distinct from `error`'s flattened-to-string message below) so
        # a caller that wants the structured shape (add_batch.py's BLOCKED
        # detection, `_server_request`'s nicer rendering below) doesn't have
        # to re-parse it out of the error string. Every OTHER failure keeps
        # returning `data=None` exactly as before — this is additive, not a
        # contract change for any existing caller that ignores `data` on
        # failure (every add_batch.py test that seeds `(False, None, ...)`
        # by hand is unaffected).
        data = detail if isinstance(detail, dict) and detail.get("blocked") else None
        return False, data, f"Server returned {status_code}: {detail}"


def _server_request(method: str, path: str, **kwargs) -> dict | list:
    """Make a request to the comics server. Raises SystemExit on failure."""
    ok, data, error = _server_request_result(method, path, **kwargs)
    if not ok:
        if isinstance(data, dict) and data.get("blocked"):
            # BUI-623 (U9): render the structured 409 policy-block envelope
            # clearly instead of the generic f"Error: Server returned 409:
            # {dict}" the branch below would otherwise print — the message
            # already names the blocking check(s) and (for a blocked
            # upsert/edit of a live row) the surviving snipe; advisories
            # render via the same helper the 2xx path uses.
            click.echo(f"Error: {data.get('message', 'Blocked by policy.')}", err=True)
            _print_advisories(data.get("advisories") or [])
            sys.exit(1)
        click.echo(f"Error: {error}", err=True)
        if isinstance(data, dict) and data.get("indeterminate"):
            # BUI-697: single-item commands still exit 1 here (the caller got
            # no confirmation), but they must not leave the operator believing
            # nothing happened — that belief is what let a stale write commit
            # against an already-edited queue.
            click.echo(
                "       This is INDETERMINATE, not a failure: the write may "
                "have committed anyway. Re-read live state (gixen list) "
                "before re-adding, editing, or removing this item.",
                err=True,
            )
        sys.exit(1)
    return data


def _print_advisories(advisories: list[dict]) -> None:
    """Render each KTD4 policy advisory (BUI-621/U7) to stderr, after the
    command's success line — visible but never fatal: advisories never
    change the exit code, and this is only ever called from the server-mode
    branch of `add`/`edit` after that branch's own success echo, never from
    the direct-Gixen path (there is no server response to render there).
    `advisories` has already been normalized to `[]` by
    `add_batch.advisories_from_response` for an old server whose 2xx
    response predates the envelope, so this never needs to re-check that —
    but a malformed *entry* within an otherwise-list `advisories` value
    (server bug, not the envelope-absent case) is still tolerated here
    rather than raising, per the same never-crash-the-CLI spirit."""
    for advisory in advisories:
        if not isinstance(advisory, dict):
            continue
        code = advisory.get("code", "?")
        message = advisory.get("message", "")
        click.echo(f"⚠️  [{code}] {message}", err=True)


def _git(*args: str) -> str:
    try:
        output = subprocess.check_output(
            ["git", *args],
            cwd=Path(__file__).resolve().parent,
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        return output or "unknown"
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def _version_string() -> str:
    """BUI-612: staleness signal for a `uv tool install`ed binary.

    Reads git HEAD at runtime from this file's own location, rather than
    stamping it in at build time (contrast BUI-305's comic-fmv and
    BUI-314's ebay-fetch): gixen-cli is only ever `uv tool install
    --editable`ed (scripts/install.sh, scripts/deploy.sh — needed for
    cli.py's own file-relative load_dotenv() to find packages/gixen-cli/.env
    regardless of caller cwd), which makes it source-backed like the comics
    server, not a frozen copy — an editable install's console script really
    does run this exact file out of the git checkout. A build-time stamp
    would not even be reachable here: setuptools' default editable-install
    strategy redirects every module straight back to its source-tree path
    via an import finder, with no mechanism (unlike hatchling's
    `force_include`) to also drop a separately-generated file into
    site-packages — so a `_build_info`-style module would never resolve.
    Falls back to "unknown" (never raises) for a non-editable/frozen
    install, where `__file__` no longer sits inside a git checkout.
    """
    try:
        pkg_version = importlib.metadata.version("gixen-cli")
    except importlib.metadata.PackageNotFoundError:
        pkg_version = "unknown"
    sha = _git("rev-parse", "--short", "HEAD")
    date = _git("log", "-1", "--format=%cd", "--date=short")
    return f"gixen {pkg_version} (git {sha}, {date})"


def _print_version(ctx: click.Context, param: click.Parameter, value: bool) -> None:
    if not value or ctx.resilient_parsing:
        return
    click.echo(_version_string())
    ctx.exit()


@click.group()
@click.option("--version", is_flag=True, expose_value=False, is_eager=True,
              callback=_print_version,
              help="Print the installed version and the git SHA/date it was built "
                   "from, then exit. Use this to check for a stale `uv tool install` "
                   "(see scripts/deploy.sh).")
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
    help="External LOCG catalog id (locg_id) for FMV linking (BUI-619: "
         "carried in the add payload for pre-trade checks, plus the "
         "existing post-bid link-fmv call for old-server compatibility). "
         "Use --comic-id if you have the internal comics.id from gixen-overlay.",
)
@click.option(
    "--comic-id",
    type=int,
    default=None,
    help="Internal gixen-overlay comics.id for FMV linking (BUI-619: "
         "carried in the add payload for pre-trade checks, plus the "
         "existing post-bid link-fmv call for old-server compatibility). "
         "Takes precedence over --catalog-id if both are given.",
)
@click.option("--grade", type=float, default=None, help="Numeric condition grade for post-bid FMV linking")
@click.option("--seller", default=None, help="eBay seller username (BUI-78 seller-reliability)")
@click.option("--seller-grade", type=float, default=None, help="Seller's stated grade (CGC float, BUI-78)")
@click.option("--photo-grade", type=float, default=None, help="Photo-assessed consensus grade (CGC float, BUI-78)")
@click.option(
    "--ack-policy",
    is_flag=True,
    help="Audited bypass (BUI-623): commit past a blocking policy check "
         "(POLICY_BLOCK_<CHECK>) instead of getting a 409. Per-invocation "
         "and blanket — it acknowledges every advisory that fired, not one "
         "check by name — and the full advisory set is recorded on the "
         "server's decisions ledger with bypass=1. Server mode only; has "
         "no effect in direct-Gixen mode (there is no policy layer there).",
)
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
    ack_policy: bool,
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
            # BUI-619 (U5): comic_id/catalog_id are already mutually exclusive
            # by this point (the both-given branch above nulls out catalog_id).
            comic_id=comic_id, locg_id=catalog_id, grade=grade,
            # BUI-621 (U7): tag this row's provenance "cli" — add-batch's
            # add_one_row tags its own rows "batch".
            source="cli",
            # BUI-623 (U9): --ack-policy -> policy_bypass=true in the payload.
            policy_bypass=ack_policy,
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
        # BUI-621 (U7): render policy advisories from the POST /api/bids
        # response (KTD4) after the success line above — visible, non-fatal,
        # never reorders/replaces the pre-existing success/failure lines.
        _print_advisories(advisories_from_response(resp))
        return

    # Existing direct-Gixen path
    if seller is not None or seller_grade is not None or photo_grade is not None:
        click.echo(
            "⚠️  --seller/--seller-grade/--photo-grade require COMICS_SERVER_URL "
            "(server mode); ignored in direct-Gixen mode.",
            err=True,
        )
    if ack_policy:
        # BUI-623 (U9): the policy layer only exists server-side — there is
        # nothing to bypass when talking to Gixen directly.
        click.echo(
            "⚠️  --ack-policy requires COMICS_SERVER_URL (server mode); "
            "ignored in direct-Gixen mode.",
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
    # BUI-623 (U9): distinct icon from Failed — a policy block is not a
    # server/Gixen fault (see add_batch.py's STATUS_BLOCKED comment).
    STATUS_BLOCKED: "🚫 Blocked",
    # BUI-697: deliberately NOT an ❌ — this row makes no claim either way.
    STATUS_INDETERMINATE: "❓ Indeterminate",
}


def _add_batch_status_cell(row: dict) -> str:
    label = _STATUS_ICON.get(row["status"], row["status"])
    if row["status"] in (STATUS_FAILED, STATUS_BLOCKED, STATUS_INDETERMINATE) and row.get("error"):
        label = f"{label} ({row['error']})"
    elif row["status"] in (STATUS_ADDED, STATUS_UPDATED) and row.get("link_attempted"):
        if row.get("link_ok"):
            label = f"{label} + linked"
        else:
            detail = row.get("link_error")
            label = f"{label} (FMV link failed: {detail})" if detail else f"{label} (FMV link failed)"
    # BUI-621 (U7): a landed row's KTD4 advisories get their own marker —
    # appended after the link-status suffix above so a row can show both
    # "+ linked" AND an advisory count without either clobbering the other.
    advisories = row.get("advisories") or []
    if advisories:
        n = len(advisories)
        label = f"{label} ⚠️  {n} {'advisory' if n == 1 else 'advisories'}"
    return label


_ADD_BATCH_TITLE_WIDTH = 28


def _add_batch_title_cell(title: str | None) -> str:
    """BUI-506: truncate a long title to keep the table's columns aligned
    rather than blowing out the row width; a missing title renders as the
    same '—' placeholder every other absent field in this table uses."""
    if not title:
        return "—"
    if len(title) <= _ADD_BATCH_TITLE_WIDTH:
        return title
    return title[: _ADD_BATCH_TITLE_WIDTH - 1] + "…"


def _print_add_batch_table(rows: list[dict]) -> None:
    click.echo(
        f"{'#':<4}{'Item ID':<16}{'Title':<{_ADD_BATCH_TITLE_WIDTH + 1}}"
        f"{'Grade':<8}{'Max Bid':<12}{'Status'}"
    )
    click.echo("-" * 100)
    for i, row in enumerate(rows, start=1):
        grade = row.get("grade")
        max_bid = row.get("max_bid")
        click.echo(
            f"{i:<4}"
            f"{(row.get('item_id') or '—'):<16}"
            f"{_add_batch_title_cell(row.get('title')):<{_ADD_BATCH_TITLE_WIDTH + 1}}"
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
    photo_grade (number), group (int, default 0), offset (int, default 6),
    title (str, BUI-506 — display-only, echoed back in the human table and
    JSON summary; never sent to the server), end_date_iso (str, BUI-567 —
    ISO 8601 auction end time; a row whose end_date_iso has already elapsed
    is rejected before the network call, surfaced as a failed row with the
    reason in its error field; omitting it skips the check entirely, so
    existing rows.json files without it behave exactly as before).
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

    On a row whose add call TIMED OUT (BUI-697): mark it indeterminate, not
    failed — the CLI stopped waiting, the comics server did not stop working,
    and the write often commits anyway. A timeout re-checks server health the
    same way a failure does. After the batch, every indeterminate row is
    reconciled against GET /api/comics/snipes (after a short settle wait): a
    row found live at its own max_bid becomes `updated` and gets its FMV link
    attempted; anything else stays `indeterminate`, never demoted to failed.

    Exits non-zero if any row failed, was blocked, was left not-attempted, or
    is still indeterminate after the reconcile.
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

    outcome = run_batch(
        rows,
        server_request=_server_request_result,
        health_check=_health_check,
        settle_seconds=_reconcile_settle_seconds(),
    )

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

    # BUI-697: an unresolved-after-reconcile row is the one outcome a reader
    # is most likely to misread as "it didn't land" — say the opposite out
    # loud, and name the dependent action (editing the queue) that turned
    # this misreading into a real-money error on 2026-08-07.
    summary = outcome.summary()
    unresolved = summary[STATUS_INDETERMINATE]
    if unresolved:
        noun = "row" if unresolved == 1 else "rows"
        click.echo(
            f"❓ {unresolved} INDETERMINATE {noun} — the write may have "
            "committed even though this CLI stopped waiting. Do NOT treat "
            "these as failed, and do NOT edit/re-add/remove them until you "
            "have re-read live state (gixen list).",
            err=True,
        )

    # BUI-621 (U7): an explicit end-of-run count, on every exit path that
    # goes through this function — a batch whose rows all landed but carried
    # advisories must never read as clean just because nothing failed.
    advisory_count = summary["advisories"]
    if advisory_count:
        noun = "advisory" if advisory_count == 1 else "advisories"
        click.echo(
            f"⚠️  {advisory_count} {noun} across this batch — review before "
            "treating it as clean.",
            err=True,
        )


def _load_json_file(path: str, expected_type: type, type_description: str):
    """Read + json.loads(path), exiting 1 with a clear message on any I/O or
    parse failure, or if the parsed value isn't `expected_type`. Shared by
    `build_batch_cmd`'s working-list and overrides file reads (below)."""
    try:
        value = json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError) as e:
        click.echo(f"Error: could not read/parse {path}: {e}", err=True)
        sys.exit(1)
    if not isinstance(value, expected_type):
        click.echo(f"Error: {path} must be {type_description}", err=True)
        sys.exit(1)
    return value


@cli.command("build-batch")
@click.argument("brief_file", type=click.Path(exists=True, dir_okay=False))
@click.argument("working_list_file", type=click.Path(exists=True, dir_okay=False))
@click.option(
    "--overrides",
    "overrides_file",
    type=click.Path(exists=True, dir_okay=False),
    default=None,
    help='JSON object {item_id: {"max_bid"?, "group"?, "skip"?}} — the '
         "Step 4 user-approval gate's per-row overrides.",
)
@click.option(
    "--out",
    "out_file",
    type=click.Path(dir_okay=False, writable=True),
    default=None,
    help="Also write the built rows JSON (ready for `gixen add-batch`) to this file.",
)
def build_batch_cmd(
    brief_file: str, working_list_file: str, overrides_file: str | None, out_file: str | None
):
    """Build `gixen add-batch` ROWS_FILE input deterministically (BUI-435),
    instead of hand-merging comic-fmv --brief output with the working list
    in context.

    BRIEF_FILE is comic-fmv --brief's captured output: either a clean JSON
    list/`{"rows": [...]}`, or the raw stdout (human table + one JSON object
    per line — non-JSON lines are ignored, but a line that looks like JSON
    and fails to parse is a hard error). WORKING_LIST_FILE is a JSON list of
    working-list rows: {item_id, title?, grade?, listing_type?/type?,
    seller?, seller_grade?, photo_grade?, group?, end_date_iso?}. `title`
    (BUI-506), when present, is carried straight into the built row and on
    through `add-batch`'s human table and JSON rows — display-only, never
    sent to the server. `end_date_iso` (BUI-567), when present, is likewise
    carried straight through unchanged so `add-batch`'s stale-listing guard
    can see it. Absent `title`/`end_date_iso` is fully backward-compatible.

    Prints the resulting rows JSON (feed straight into `gixen add-batch`),
    and reports skipped (BIN / user-skip) and unlinked (null comic_id) rows
    to stderr. Never drops comic_id silently and never fabricates a max_bid
    for a needs-manual row — see add_batch.build_batch_rows for the full
    per-row resolution rules. Exits non-zero on any structurally invalid
    input (AddBatchError) rather than emitting a partial/guessed batch.
    """
    try:
        brief_raw = Path(brief_file).read_text()
    except OSError as e:
        click.echo(f"Error: could not read {brief_file}: {e}", err=True)
        sys.exit(1)

    working_list = _load_json_file(
        working_list_file, list, "a JSON list of working-list rows"
    )

    overrides: dict | None = None
    if overrides_file:
        overrides = _load_json_file(
            overrides_file, dict, "a JSON object keyed by item_id"
        )

    try:
        brief_rows = parse_brief_rows(brief_raw)
        result = build_batch_rows(brief_rows, working_list, overrides)
    except AddBatchError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)

    text = json.dumps(result.rows, indent=2)
    click.echo(text)
    if out_file:
        try:
            Path(out_file).write_text(text)
        except OSError as e:
            click.echo(f"warning: could not write --out ({out_file}): {e}", err=True)

    if result.skipped:
        click.echo(f"Skipped {len(result.skipped)} row(s): {result.skipped}", err=True)
    if result.unlinked:
        click.echo(
            f"{len(result.unlinked)} row(s) will add without FMV linkage "
            f"(comic_id null): {[r['item_id'] for r in result.unlinked]}",
            err=True,
        )


@cli.command()
@click.argument("item_id")
@click.argument("max_bid")
@click.option("--offset", type=int, default=None,
              help="Seconds before end to place bid (1-15); omit to keep the current offset")
@click.option("--group", type=int, default=None,
              help="Snipe group (0=none, 1-10); omit to keep the current group")
@click.option(
    "--ack-policy",
    is_flag=True,
    help="Audited bypass (BUI-623): commit past a blocking policy check "
         "instead of getting a 409 — same semantics as `gixen add "
         "--ack-policy`. Server mode only.",
)
def edit(item_id: str, max_bid: str, offset: int | None, group: int | None, ack_policy: bool):
    """Change the bid on an existing snipe.

    BUI-401/BUI-404: --offset / --group default to None so a bare
    `edit <id> <bid>` changes only the max bid — the field is omitted from the
    PATCH body (server mode) so the server's DB-backed passthrough preserves
    the current value, or resolved from a `list_snipes` lookup (direct mode,
    which has no local store) before calling `modify_snipe` (which has no
    passthrough concept of its own and would otherwise reset to its 6 / 0
    defaults).
    """
    try:
        bid = Decimal(max_bid)
    except InvalidOperation:
        click.echo(f"Error: Invalid bid amount: {max_bid}", err=True)
        sys.exit(1)

    if _server_url():
        payload: dict = {"max_bid": float(bid)}
        if offset is not None:
            payload["bid_offset"] = offset
        if group is not None:
            payload["snipe_group"] = group
        # BUI-621 (U7): tag this edit's provenance "cli" — mirrors `add`'s
        # own source tag (EditBidRequest.source accepts the same optional
        # field U6 added).
        payload["source"] = "cli"
        # BUI-623 (U9): --ack-policy -> policy_bypass=true in the payload.
        if ack_policy:
            payload["policy_bypass"] = True
        resp = _server_request("patch", f"/api/bids/{item_id}", json=payload)
        click.echo(f"Updated snipe for {item_id} to max bid {bid}")
        # BUI-621 (U7): render policy advisories from the PATCH response
        # (KTD4) after the success line above — visible, non-fatal.
        _print_advisories(advisories_from_response(resp))
        return

    if ack_policy:
        # BUI-623 (U9): the policy layer only exists server-side — there is
        # nothing to bypass when talking to Gixen directly.
        click.echo(
            "⚠️  --ack-policy requires COMICS_SERVER_URL (server mode); "
            "ignored in direct-Gixen mode.",
            err=True,
        )

    # BUI-414: this whole direct-mode branch (the list_snipes resolve below
    # plus the modify_snipe call at the end) has NO mutual exclusion against
    # another concurrent `gixen edit` invocation on the same item_id — unlike
    # server mode, which serializes under _api_lock (BUI-402). An in-process
    # lock can't fix that here: each CLI invocation is its own short-lived
    # process, so there's no shared process for a lock to live in. This is
    # deliberately left unguarded — direct mode is a single-human-terminal
    # fallback path (used when the comics server is unreachable), not a
    # concurrent service, and building an OS-level file lock to close this
    # window would trade a low-likelihood race for real complexity (stale
    # lock cleanup, etc). Don't run concurrent direct-mode edits against the
    # same item_id. See packages/gixen-cli/CLAUDE.md "Key Details".
    client = _make_client()
    modify_kwargs: dict = {}
    if offset is not None:
        modify_kwargs["bid_offset"] = offset
    if group is not None:
        modify_kwargs["snipe_group"] = group

    if offset is None or group is None:
        # BUI-404: direct mode has no local store to resolve "keep current"
        # from the way server-mode's api_edit_bid does from the DB — resolve
        # the omitted field(s) from Gixen's own live snipe list instead, so a
        # max_bid-only edit doesn't fall through to modify_snipe's own
        # defaults (offset 6 / group 0) and silently reset a tuned fire-offset
        # or un-group the snipe. A list_snipes failure or a not-found item
        # aborts the edit here rather than proceeding with guessed defaults.
        try:
            snipes = client.list_snipes()
        except GixenError as e:
            click.echo(f"Error: {e}", err=True)
            sys.exit(1)
        snipe = next(
            (s for s in snipes if s.get("item_id") == str(item_id)), None
        )
        if snipe is None:
            click.echo(
                f"Error: Item {item_id} not found in your snipe list", err=True
            )
            sys.exit(1)
        if offset is None:
            current_offset = _parse_optional_int(snipe.get("bid_offset"))
            if current_offset is None:
                # An unparseable/blank bid_offset is "unknown" — silently
                # substituting the hardcoded default (6) would be the exact
                # silent-reset bug this fix exists to prevent, just on this
                # field instead of snipe_group. Fail closed instead of
                # guessing, same as the snipe_group branch below.
                click.echo(
                    f"Error: current bid_offset for {item_id} could not be "
                    "read from Gixen (unknown/unparseable) — pass --offset "
                    "explicitly to avoid an unintended change.",
                    err=True,
                )
                sys.exit(1)
            modify_kwargs["bid_offset"] = current_offset
        if group is None:
            current_group = _parse_optional_int(snipe.get("snipe_group"))
            if current_group is None:
                # BUI-383: an unparseable/blank snipe_group is "unknown", not
                # "0" — coercing it to 0 would silently un-group a snipe whose
                # real group we simply failed to read. Fail closed instead of
                # guessing.
                click.echo(
                    f"Error: current snipe_group for {item_id} could not be "
                    "read from Gixen (unknown/unparseable) — pass --group "
                    "explicitly to avoid an unintended change.",
                    err=True,
                )
                sys.exit(1)
            modify_kwargs["snipe_group"] = current_group
        # Deliberately NOT passing the dbidid resolved above through to
        # modify_snipe: that would let it skip its own fresh list_snipes()
        # lookup right before the POST, reusing a resolution that's now one
        # extra round trip older — a stale-dbidid window this code didn't
        # have before (pre-fix, modify_snipe always resolved dbidid fresh
        # immediately pre-POST, since cli.py never passed one). Eating one
        # extra list_snipes() call keeps that same-call freshness guarantee;
        # the ticket already treats one extra round trip as an acceptable
        # cost for the "keep current" resolution.

    try:
        client.modify_snipe(item_id, bid, **modify_kwargs)
        click.echo(f"Updated snipe for {item_id} to max bid {bid}")
    except GixenSnipeNotFoundError:
        click.echo(f"Error: Item {item_id} not found in your snipe list", err=True)
        sys.exit(1)
    except GixenError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


def _format_cap(value) -> str | None:
    """Render a server-reported max_bid for the per-item group line, or None if
    it isn't a usable number. Never raises — this is a display path on a
    command whose write has already committed."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        return f"${float(value):.2f}"
    except (TypeError, ValueError):
        return None


def _group_via_server(group_n: int, item_ids: tuple[str, ...]) -> None:
    """BUI-710: `gixen group` in server mode — one PATCH per item, sequentially.

    Sequential on purpose, like every other multi-write path in this CLI: the
    server serializes Gixen calls under `_api_lock`, so concurrent PATCHes buy
    nothing and only widen the window in which a half-formed group exists.

    **The residual real-money risk, stated plainly.** Gixen's modify form has
    no passthrough — `newmaxbid` is mandatory — so *something* must supply a
    concrete cap; BUI-710 moves that job to the server rather than removing it.
    The cap the server resolves is the DB mirror, which can lag Gixen only if
    someone edits the cap on gixen.com's own web UI while our list fetches are
    failing. Four things bound it, and there is no fifth:

    1. the opening `GET /api/snipes` re-mirrors every live cap from Gixen
       (`_ensure_fresh_sync` -> `mirror_gixen_max_bid`) whenever Gixen's read
       path works at all;
    2. the resolve happens under the server's `_api_lock`, so no concurrent
       write *through the server* can be reverted;
    3. an unusable cap (no live row, missing, non-positive, unparseable) is a
       422 — never a default;
    4. the resolved cap is printed per item, so a wrong one is visible rather
       than silent.

    In the one window that escapes (1) — Gixen's GET path stalling while POSTs
    land, the exact 2026-08-07 signature — every other route to a group change
    is *staler*: `edit --group` takes a cap a human read off the same DB (plus
    typo risk), and an `add-batch` re-run takes one off a rows.json. This is
    the least-stale option available then, and the only one that works at all.
    """
    # The opening read is GET /api/snipes, NOT GET /api/bids, and the
    # difference is the ticket's stale-cap guard. Both would list the rows,
    # but only /api/snipes runs `_ensure_fresh_sync()` first, which re-mirrors
    # each live row's max_bid from Gixen's own list (`mirror_gixen_max_bid`).
    # So the caps the server is about to resolve from have just been
    # reconciled against Gixen — for the cost of the single Gixen round trip
    # the direct-Gixen branch already spends on its opening `list_snipes()`.
    # When Gixen is unreachable `_ensure_fresh_sync` logs and serves
    # last-known rows instead of failing: deliberate, because grouping while
    # Gixen flaps is the entire reason BUI-710 exists, and the mirrored cap is
    # still the best value anyone has. A failure to reach the COMICS SERVER,
    # by contrast, aborts here — we must never guess our way past a failed
    # read (`_server_request` exits nonzero).
    snipes = _server_request("get", "/api/snipes")
    if not isinstance(snipes, list):
        click.echo(
            "Error: unexpected /api/snipes response (expected a list)", err=True
        )
        sys.exit(1)

    # PENDING-only, matching the server's own resolution source
    # (`get_pending_bid_by_item_id`): a terminal or never-ingested row has no
    # cap for the server to resolve and would come back 422 mid-loop. Refusing
    # up front — all-or-nothing, exactly like the direct-Gixen branch's
    # missing-id check — keeps a typo from leaving a half-formed group.
    live = {
        str(s.get("item_id")): s
        for s in snipes
        if isinstance(s, dict) and s.get("status") == "PENDING"
    }
    missing = [iid for iid in item_ids if iid not in live]
    if missing:
        click.echo(
            f"Error: no live (PENDING) snipe on the comics server for: "
            f"{', '.join(missing)}",
            err=True,
        )
        sys.exit(1)

    failures: list[str] = []
    for iid in item_ids:
        # No max_bid in the payload — see group_cmd's docstring. `source`
        # mirrors `edit`'s BUI-621 provenance tag so /api/decisions can tell a
        # group write from a cap edit.
        ok, data, error = _server_request_result(
            "patch", f"/api/bids/{iid}",
            json={"snipe_group": group_n, "source": "cli"},
        )
        if not ok:
            failures.append(iid)
            if isinstance(data, dict) and data.get("blocked"):
                click.echo(
                    f"  {iid}: blocked — "
                    f"{data.get('message', 'blocked by policy.')}",
                    err=True,
                )
                _print_advisories(data.get("advisories") or [])
            elif isinstance(data, dict) and data.get("indeterminate"):
                # BUI-697's lesson, applied here: a client timeout is not
                # evidence the write failed. Say so, or the operator re-runs
                # against a queue that already changed.
                click.echo(
                    f"  {iid}: INDETERMINATE — {error} The group change may "
                    f"have landed; re-read `gixen list` before retrying.",
                    err=True,
                )
            else:
                click.echo(f"  {iid}: failed — {error}", err=True)
            continue

        row = data if isinstance(data, dict) else {}
        cap = _format_cap(row.get("max_bid"))
        # Print the cap the server actually holds after the write. The command
        # promises not to move money; showing the number is what lets an
        # operator verify that instead of trusting it.
        suffix = f" (max bid unchanged at {cap})" if cap else ""
        click.echo(f"  {iid}: group -> {group_n}{suffix}")
        landed = row.get("snipe_group")
        if isinstance(landed, int) and landed != group_n:
            # Belt-and-braces on top of BUI-709's Gixen-side confirm (which
            # already refuses to report success when the LISTED group differs
            # from the sent one). Warn, don't fail: the read-back row is
            # `get_bid_by_item_id`, which is not status-filtered, so a
            # mismatch is likelier to be that quirk than a lost write.
            click.echo(
                f"  {iid}: warning — server reports group {landed}, not "
                f"{group_n}; re-read `gixen list`.",
                err=True,
            )
        _print_advisories(advisories_from_response(row))

    ok_count = len(item_ids) - len(failures)
    click.echo(f"Updated {ok_count} of {len(item_ids)} snipe(s).")
    if failures:
        sys.exit(1)


@cli.command("group")
@click.argument("group_n", type=click.IntRange(0, 10))
@click.argument("item_ids", nargs=-1, required=True)
def group_cmd(group_n: int, item_ids: tuple[str, ...]):
    """Assign one or more existing snipes to a group (0=ungroup, 1-10).

    Preserves each snipe's existing max bid and offset.

    BUI-710: server mode (COMICS_SERVER_URL set) routes each item through
    `PATCH /api/bids/{item_id}` — the same retrying, dbidid-fast-path,
    policy-checked write `edit` uses — instead of the direct-Gixen loop below,
    which needs 1+3N `home_2.php` round trips with no retry and failed for
    ~18h straight during the 2026-08-07 outage. The PATCH body carries NO
    max_bid: BUI-710 made `EditBidRequest.max_bid` a None-passthrough, so the
    server resolves the current cap from the live row inside the same
    `_api_lock` acquisition that does the Gixen modify. That is the whole
    safety argument for putting a group change on a money endpoint — this
    command never states, computes, or transmits a bid amount, so it cannot
    transmit a wrong one, and no concurrent edit can slip into a read->PATCH
    window that doesn't exist. The direct-Gixen branch stays as the fallback
    when no server URL is configured.
    """
    if _server_url():
        _group_via_server(group_n, item_ids)
        return

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
        if s.get("status") in GIXEN_RAW_TERMINAL_STATUSES
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
