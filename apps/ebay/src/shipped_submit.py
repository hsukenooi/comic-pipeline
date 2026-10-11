"""ebay-shipped --auto-submit: submit shipped eBay orders to EZShip (BUI-1238).

`ebay-shipped` (shipped_orders.py) reads Gmail and only reports. This module is
the unattended path behind its `--auto-submit` flag: it reads the buyer's own
orders from the eBay Trading API (GetOrders, OrderRole=Buyer, which carries
`ShipmentTrackingDetails`; the WonList has no tracking), groups them into one
EZShip order per tracking number, and calls `ezship new` for each one the
EZShip ledger has not seen.

Rules, each pinned by a test:

* One EZShip order per tracking number. A seller combining 20 lots into 4
  parcels yields 4 submissions, with the lot titles joined into one product name.
* Product name comes from `gixen list --json` titles matched by item id (the
  identified titles); a miss falls back to the eBay listing title.
* Declared value is never computed: `-d` is omitted so the CLI default of $10
  applies.
* HOLD (notify, never submit) when the item ships from outside the US, when its
  location cannot be read as a US place, or when the carrier is not one of the
  seven EZShip carriers.
* `ezship new` exit 3 (expired cookie) stops the whole batch and notifies.
* `--dry-run` never invokes ezship and never writes any state.
* The submission ledger is `~/.config/ezship/submitted-orders.json`, written by
  `ezship new` itself on success. This module re-reads it after every call: a
  tracking number missing from it after exit 0 stops the batch, because the
  next run would submit it again.
* In-flight marker (`auto-submit-inflight.json`, atomic + fsync'd) is written for
  a tracking number BEFORE `ezship new` runs. It is cleared only on a confirmed
  ledger entry, or on an outcome that provably submitted nothing (exit 3 expired
  cookie, or ezship failing to launch). Every other outcome is ambiguous (exit 1
  also covers a connection dropped after EZShip accepted the order, BUI-180) and
  leaves the marker: later runs HOLD that tracking number until a human checks
  EZShip and clears it with `ebay-shipped --clear-inflight TRACKING`. An
  unreadable marker file fails the run closed. A lock file stops concurrent runs.
* An item with two or more distinct tracking numbers is held, not submitted.
* The heartbeat is pinged only when the run finished with no failure. Holds are
  a correct outcome and do not withhold it.

Exit codes: 0 run finished (submits and holds both fine); 1 hard failure (eBay,
ledger, or an unconfirmable submission); 3 EZShip cookie expired; 4 one or more
orders were rejected by EZShip (the rest were still attempted).
"""

from __future__ import annotations

import datetime as dt
import fcntl
import json
import os
import re
import shlex
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import requests

import ebay_user_token as eut

ENDPOINT = "https://api.ebay.com/ws/api.dll"
NS = {"e": "urn:ebay:apis:eBLBaseComponents"}
COMPAT_LEVEL = "1349"
PER_PAGE = 100
MAX_PAGES = 20
WINDOW_DAYS = 30
HTTP_TIMEOUT_S = 60
EZSHIP_TIMEOUT_S = 120
GIXEN_TIMEOUT_S = 60

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_SESSION_EXPIRED = 3   # mirrors apps/ezship exit-codes.ts EXIT_SESSION_EXPIRED
EXIT_SOME_REJECTED = 4

DEFAULT_EZSHIP_DIR = Path.home() / "Projects" / "comic-pipeline" / "apps" / "ezship"
EZSHIP_DIR_ENV = "EZSHIP_DIR"
EZSHIP_CMD_ENV = "EZSHIP_CMD"  # override of the whole launcher, shlex-split
DEFAULT_EZSHIP_CMD = ["npx", "tsx", "src/cli.ts"]
DEFAULT_HOLDS_STATE = Path.home() / ".config" / "ezship" / "auto-submit-holds.json"
DEFAULT_INFLIGHT_STATE = Path.home() / ".config" / "ezship" / "auto-submit-inflight.json"
HEARTBEAT_JOB = "ebay-shipped-submit"
HEARTBEAT_PATH = "/api/heartbeat/ebay-shipped-submit"
PRODUCT_NAME_MAX = 200

# The seven carriers `ezship new -c` knows (apps/ezship/src/types.ts CARRIER_MAP).
# Keys are lowercased aliases eBay is seen to send; values are the exact EZShip
# spellings. Anything not here is held, never guessed.
EZSHIP_CARRIERS = ("DHL", "FedEx", "Ontrac", "UPS", "USPS", "Other", "Amazon")
_CARRIER_ALIASES = {
    "usps": "USPS",
    "ups": "UPS",
    "fedex": "FedEx",
    "fed ex": "FedEx",
    "dhl": "DHL",
    "ontrac": "Ontrac",
    "amazon": "Amazon",
    "other": "Other",
}

_US_STATES = {s.lower() for s in (
    "Alabama Alaska Arizona Arkansas California Colorado Connecticut Delaware "
    "Florida Georgia Hawaii Idaho Illinois Indiana Iowa Kansas Kentucky "
    "Louisiana Maine Maryland Massachusetts Michigan Minnesota Mississippi "
    "Missouri Montana Nebraska Nevada Ohio Oklahoma Oregon Pennsylvania "
    "Tennessee Texas Utah Vermont Virginia Washington Wisconsin Wyoming"
).split()} | {
    "new hampshire", "new jersey", "new mexico", "new york", "north carolina",
    "north dakota", "rhode island", "south carolina", "south dakota",
    "west virginia", "district of columbia",
}


class SubmitError(Exception):
    def __init__(self, message, exit_code=EXIT_FAILURE):
        super().__init__(message)
        self.exit_code = exit_code


# ─── eBay GetOrders ───────────────────────────────────────────────────────────

AUTH_ERROR_CODES = {"931", "932", "21916984", "21917053"}


def _request_body(page, now, days):
    f = lambda d: d.strftime("%Y-%m-%dT%H:%M:%S.000Z")  # noqa: E731
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<GetOrdersRequest xmlns="urn:ebay:apis:eBLBaseComponents">'
        f"<CreateTimeFrom>{f(now - dt.timedelta(days=days))}</CreateTimeFrom>"
        f"<CreateTimeTo>{f(now)}</CreateTimeTo>"
        "<OrderRole>Buyer</OrderRole><OrderStatus>All</OrderStatus>"
        "<DetailLevel>ReturnAll</DetailLevel>"
        f"<Pagination><EntriesPerPage>{PER_PAGE}</EntriesPerPage>"
        f"<PageNumber>{page}</PageNumber></Pagination></GetOrdersRequest>"
    )


def _fetch_page(token, page, now, days):
    try:
        resp = requests.post(
            ENDPOINT,
            data=_request_body(page, now, days).encode(),
            headers={
                "X-EBAY-API-CALL-NAME": "GetOrders",
                "X-EBAY-API-SITEID": "0",
                "X-EBAY-API-COMPATIBILITY-LEVEL": COMPAT_LEVEL,
                "X-EBAY-API-IAF-TOKEN": token,
                "Content-Type": "text/xml",
            },
            timeout=HTTP_TIMEOUT_S,
        )
    except requests.RequestException as exc:
        raise SubmitError(f"network error calling GetOrders: {type(exc).__name__}") from None
    if resp.status_code != 200:
        raise SubmitError(f"HTTP {resp.status_code} from GetOrders")
    try:
        root = ET.fromstring(resp.content)
    except ET.ParseError:
        raise SubmitError("unparseable XML from GetOrders") from None
    ack = root.findtext("e:Ack", namespaces=NS)
    errors = [
        (er.findtext("e:ErrorCode", namespaces=NS), er.findtext("e:LongMessage", namespaces=NS))
        for er in root.findall("e:Errors", NS)
    ]
    if ack not in ("Success", "Warning"):
        detail = "; ".join(f"{c}: {m}" for c, m in errors) or "no error detail"
        hint = f"; {eut.RELOGIN_HINT}" if any(c in AUTH_ERROR_CODES for c, _ in errors) else ""
        raise SubmitError(f"eBay GetOrders Ack={ack}: {detail}{hint}")
    if root.find("e:OrderArray", NS) is None:
        # A flap (Ack=Success, no OrderArray) must never read as "no orders".
        raise SubmitError("eBay GetOrders returned Ack=Success with no OrderArray")
    has_more = root.findtext("e:HasMoreOrders", namespaces=NS) == "true"
    return root, has_more


def parse_orders(root):
    """Flatten a GetOrders root into one record per transaction (line item)."""
    rows = []
    for order in root.findall("e:OrderArray/e:Order", NS):
        o = lambda p: order.findtext(p, namespaces=NS)  # noqa: E731
        cancelled = (o("e:CancelStatus") or "NotApplicable") not in ("NotApplicable", "")
        for txn in order.findall("e:TransactionArray/e:Transaction", NS):
            t = lambda p: txn.findtext(p, namespaces=NS)  # noqa: E731
            trackings = []
            for d in txn.findall("e:ShippingDetails/e:ShipmentTrackingDetails", NS):
                num = (d.findtext("e:ShipmentTrackingNumber", namespaces=NS) or "").strip()
                carrier = (d.findtext("e:ShippingCarrierUsed", namespaces=NS) or "").strip()
                if num:
                    trackings.append((num, carrier))
            rows.append({
                "order_id": o("e:OrderID"),
                "seller": o("e:SellerUserID"),
                "cancelled": cancelled,
                "item_id": t("e:Item/e:ItemID"),
                "ebay_title": t("e:Item/e:Title"),
                "location": t("e:Item/e:Location"),
                "country": t("e:Item/e:Country"),
                "shipped_time": t("e:ShippedTime") or o("e:ShippedTime"),
                "trackings": trackings,
            })
    return rows


def fetch_transactions(token, now=None, days=WINDOW_DAYS):
    now = now or dt.datetime.now(dt.timezone.utc)
    rows = []
    for page in range(1, MAX_PAGES + 1):
        root, has_more = _fetch_page(token, page, now, days)
        rows.extend(parse_orders(root))
        if not has_more:
            return rows
    raise SubmitError(f"GetOrders still had more pages after {MAX_PAGES}; refusing a partial window")


# ─── Grouping and rules ───────────────────────────────────────────────────────

def normalize_carrier(raw):
    """Map eBay's free-text carrier to an EZShip carrier name, or None."""
    key = re.sub(r"\s+", " ", (raw or "").strip().lower())
    return _CARRIER_ALIASES.get(key)


def ships_from_us(location, country=None):
    """True only on positive evidence. eBay prints a US location as
    "City, State Name" with no country (verified live), so a trailing US state
    name is the proof; anything else (a foreign country, blank, a bare
    abbreviation we cannot place) is not US and gets held."""
    if country:
        return country.strip().upper() in ("US", "USA", "UNITED STATES")
    if not location or "," not in location:
        return False
    return location.rsplit(",", 1)[1].strip().lower() in _US_STATES


def _clean_title(title):
    return re.sub(r"\s+", " ", title or "").strip()


def group_by_tracking(rows, titles_by_item):
    """One group per tracking number. Returns (groups, skipped) where skipped
    are transactions that cannot be submitted at all (no tracking, cancelled)."""
    groups, skipped = {}, []
    for r in rows:
        if r["cancelled"]:
            skipped.append({**r, "reason": "cancelled"})
            continue
        if not r["trackings"]:
            skipped.append({**r, "reason": "no_tracking"})
            continue
        multi = len({n.upper() for n, _ in r["trackings"]}) > 1
        for number, carrier in r["trackings"]:
            key = number.upper()
            g = groups.setdefault(key, {
                "tracking": number, "carriers": set(), "item_ids": [],
                "titles": [], "title_misses": [], "locations": set(),
                "countries": set(), "order_ids": [], "sellers": [], "multi_tracking_items": [],
            })
            if multi and r["item_id"] not in g["multi_tracking_items"]:
                g["multi_tracking_items"].append(r["item_id"])
            g["carriers"].add(carrier)
            if r["item_id"] in g["item_ids"]:
                continue
            g["item_ids"].append(r["item_id"])
            title = titles_by_item.get(r["item_id"])
            if title:
                g["titles"].append(_clean_title(title))
            else:
                g["title_misses"].append(r["item_id"])
                g["titles"].append(_clean_title(r["ebay_title"]) or f"eBay item {r['item_id']}")
            g["locations"].add(r["location"] or "")
            if r["country"]:
                g["countries"].add(r["country"])
            for field, val in (("order_ids", r["order_id"]), ("sellers", r["seller"])):
                if val and val not in g[field]:
                    g[field].append(val)
    return list(groups.values()), skipped


def product_name(titles):
    name = "; ".join(titles)
    if len(name) > PRODUCT_NAME_MAX:
        name = name[: PRODUCT_NAME_MAX - 1].rstrip() + "…"
    return name


def classify(group, submitted):
    """Return the group with `action` (submit|hold|skip), `reason`, `ezship_carrier`."""
    out = dict(group)
    carriers = {c for c in group["carriers"]}
    mapped = {normalize_carrier(c) for c in carriers}
    out["ezship_carrier"] = next(iter(mapped)) if len(mapped) == 1 else None
    out["product"] = product_name(group["titles"])
    if group["tracking"].upper() in submitted:
        return {**out, "action": "skip", "reason": "already_submitted"}
    if group["multi_tracking_items"]:
        return {**out, "action": "hold",
                "reason": f"item(s) {group['multi_tracking_items']!r} carry several tracking "
                          "numbers; cannot tell which parcel holds what"}
    if out["ezship_carrier"] is None:
        return {**out, "action": "hold",
                "reason": f"unsupported carrier {sorted(carriers)!r} (EZShip: {', '.join(EZSHIP_CARRIERS)})"}
    country = next(iter(group["countries"])) if len(group["countries"]) == 1 else None
    foreign = [loc for loc in group["locations"] if not ships_from_us(loc, country)]
    if len(group["countries"]) > 1:
        foreign.append("conflicting countries")
    if foreign:
        return {**out, "action": "hold",
                "reason": f"ships from non-US or unreadable location {sorted(foreign)!r}"}
    return {**out, "action": "submit", "reason": "new"}


# ─── Inputs: gixen titles, ledger, holds state ────────────────────────────────

def load_gixen_titles():
    """{item_id: title} from `gixen list --json`. A failure degrades to {} (the
    eBay titles are used) and is reported, never fatal: a product name is cosmetic."""
    try:
        proc = subprocess.run(["gixen", "list", "--json"], capture_output=True,
                              text=True, timeout=GIXEN_TIMEOUT_S)
        if proc.returncode != 0:
            return {}, f"gixen list exited {proc.returncode}"
        data = json.loads(proc.stdout)
        return {str(x["item_id"]): x["title"] for x in data if x.get("title")}, None
    except (OSError, subprocess.TimeoutExpired, ValueError, KeyError, TypeError) as exc:
        return {}, f"gixen list failed: {type(exc).__name__}"


def load_holds_state(path):
    try:
        data = json.loads(path.read_text())
        return set(data) if isinstance(data, list) else set()
    except FileNotFoundError:
        return set()
    except (OSError, ValueError):
        return set()


def save_holds_state(path, notified):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(sorted(notified)) + "\n")
        os.replace(tmp, path)
    except OSError as exc:
        print(f"warning: could not save holds state: {exc}", file=sys.stderr)


class InflightError(Exception):
    pass


def clear_command(tracking):
    return f"ebay-shipped --clear-inflight {shlex.quote(tracking)}"


def load_inflight(path):
    """{TRACKING: {since, state}}. Missing file = {}. Any unreadable or malformed
    file raises InflightError: unknown quarantine state must stop the run (fail
    closed), never read as "nothing is quarantined"."""
    try:
        text = Path(path).read_text()
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise InflightError(f"cannot read {path}: {exc}") from None
    try:
        data = json.loads(text)
    except ValueError:
        raise InflightError(f"{path} is not valid JSON") from None
    if not isinstance(data, dict) or not all(isinstance(v, dict) for v in data.values()):
        raise InflightError(f"{path} has an unexpected shape")
    return data


def save_inflight(path, data):
    """Atomic, durable write. Raises InflightError on failure (callers must not
    submit if the marker could not be written)."""
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        with open(tmp, "w") as fh:
            fh.write(json.dumps(data, indent=1, sort_keys=True) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except OSError as exc:
        raise InflightError(f"cannot write {path}: {exc}") from None


class InflightLocked(InflightError):
    """The auto-submit run lock is held by a live run."""


def acquire_run_lock(inflight_path):
    """Take the non-blocking exclusive run lock shared by --auto-submit and
    --clear-inflight. Returns the open lock file (closing it releases the lock).
    Raises BlockingIOError if another process holds it, OSError on other failures.
    Non-blocking on purpose: a run holds the lock for minutes, so a waiter would
    appear hung; callers fail fast instead."""
    inflight_path = Path(inflight_path)
    inflight_path.parent.mkdir(parents=True, exist_ok=True)
    lock = open(inflight_path.with_name(inflight_path.name + ".lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BaseException:
        lock.close()
        raise
    return lock


def clear_inflight(path, tracking):
    """Human-invoked: drop one tracking number's marker. Returns True if present.
    Takes the run lock so it cannot race a live --auto-submit run over the marker
    file (BUI-1248); raises InflightLocked if a run holds it."""
    try:
        lock = acquire_run_lock(path)
    except BlockingIOError:
        raise InflightLocked("an ebay-shipped --auto-submit run is in progress; "
                             "marker left untouched. Retry when it finishes.") from None
    except OSError as exc:
        raise InflightError(f"cannot take run lock: {exc}") from None
    try:
        data = load_inflight(path)
        if data.pop(tracking.upper(), None) is None:
            return False
        save_inflight(path, data)
        return True
    finally:
        lock.close()


def _now_iso():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ─── Side effects: ezship, notify, heartbeat ──────────────────────────────────

def notify(message):
    """macOS notification (same mechanism as scripts/em-batch-nightly.sh). Best effort."""
    try:
        safe = message.replace("\\", " ").replace('"', "'")
        subprocess.run(["/usr/bin/osascript", "-e",
                        f'display notification "{safe}" with title "ebay-shipped"'],
                       capture_output=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        pass


def ezship_command(group):
    launcher = shlex.split(os.environ.get(EZSHIP_CMD_ENV, "")) or list(DEFAULT_EZSHIP_CMD)
    remark = f"eBay {', '.join(group['order_ids'])} from {', '.join(group['sellers'])}"[:200]
    # No -d: the CLI default ($10) applies; the declared value is never computed.
    return launcher + ["new", "-t", group["tracking"], "-c", group["ezship_carrier"],
                       "-p", group["product"], "--category", "Books", "-r", remark]


def run_ezship(group):
    """Run one `ezship new`. Returns the exit code, or None on timeout."""
    cwd = os.environ.get(EZSHIP_DIR_ENV) or str(DEFAULT_EZSHIP_DIR)
    try:
        proc = subprocess.run(ezship_command(group), cwd=cwd, capture_output=True,
                              text=True, timeout=EZSHIP_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return None, "timed out"
    except OSError as exc:
        return -1, f"could not launch ezship: {exc}"
    tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-1:] or [""]
    return proc.returncode, tail[0]


def ping_heartbeat(server_url, ok, detail):
    """Best-effort POST to the comics server's heartbeat endpoints."""
    if not server_url:
        print("warning: COMICS_SERVER_URL unset; heartbeat not recorded", file=sys.stderr)
        return
    path = HEARTBEAT_PATH + ("" if ok else "/failure")
    try:
        requests.post(f"{server_url}{path}", params={"detail": detail[:300]},
                      timeout=10).raise_for_status()
    except requests.RequestException as exc:
        print(f"warning: heartbeat ping failed (non-fatal): {exc}", file=sys.stderr)


# ─── Orchestration ────────────────────────────────────────────────────────────

def _render(decisions, skipped, warnings, dry_run):
    lines = [f"ebay-shipped --auto-submit {'(dry run) ' if dry_run else ''}"
             f"- {len(decisions)} tracking group(s)"]
    for d in decisions:
        lines.append(f"[{d['action'].upper():6}] {d['tracking']} {d['ezship_carrier'] or '?'} "
                     f"| {d['reason']} | items={len(d['item_ids'])} "
                     f"orders={','.join(d['order_ids'])} seller={','.join(d['sellers'])}")
        lines.append(f"         product: {d['product']}")
        if d["title_misses"]:
            lines.append(f"         title fallback (not in gixen): {', '.join(d['title_misses'])}")
    for s in skipped:
        lines.append(f"[SKIP  ] item {s['item_id']} order {s['order_id']} | {s['reason']}")
    lines.extend(f"warning: {w}" for w in warnings)
    return "\n".join(lines)


def run(*, ledger_path, load_ledger, dry_run, holds_path=DEFAULT_HOLDS_STATE,
        inflight_path=None, server_url=None, days=WINDOW_DAYS, json_output=False):
    """Run the whole job. `load_ledger` is shipped_orders.load_ledger (injected
    to avoid a circular import). Returns the process exit code."""
    inflight_path = Path(inflight_path) if inflight_path else Path(holds_path).with_name(
        DEFAULT_INFLIGHT_STATE.name)
    lock = None
    if not dry_run:
        try:
            lock = acquire_run_lock(inflight_path)
        except BlockingIOError:
            print("another ebay-shipped --auto-submit run holds the lock; nothing done",
                  file=sys.stderr)
            return EXIT_OK
        except OSError as exc:
            return _fail(f"cannot take run lock: {exc}", server_url, dry_run)
    try:
        return _run_locked(ledger_path, load_ledger, dry_run, holds_path, inflight_path,
                           server_url, days, json_output)
    finally:
        if lock:
            lock.close()


def _run_locked(ledger_path, load_ledger, dry_run, holds_path, inflight_path,
                server_url, days, json_output):
    try:
        submitted = load_ledger(ledger_path)
        inflight = load_inflight(inflight_path)
        token = eut.get_user_access_token()
        rows = fetch_transactions(token, days=days)
    except eut.UserTokenError as exc:
        return _fail(f"eBay user token: {exc}", server_url, dry_run)
    except Exception as exc:  # noqa: BLE001  # ledger errors included: never "no new orders"
        return _fail(str(exc), server_url, dry_run)

    titles, gixen_warning = load_gixen_titles()
    warnings = [gixen_warning] if gixen_warning else []
    groups, skipped = group_by_tracking(rows, titles)
    decisions = [classify(g, submitted) for g in groups]
    for i, d in enumerate(decisions):
        mark = inflight.get(d["tracking"].upper())
        if mark and d["action"] == "submit":
            decisions[i] = {**d, "action": "hold", "reason": (
                f"QUARANTINED since {mark.get('since', '?')} ({mark.get('state', '?')}): "
                f"check EZShip, then run: {clear_command(d['tracking'])}")}
    decisions.sort(key=lambda d: d["tracking"])

    if json_output:
        print(json.dumps({
            "dry_run": dry_run,
            "decisions": [{k: (sorted(v) if isinstance(v, set) else v) for k, v in d.items()}
                          for d in decisions],
            "skipped": skipped, "warnings": warnings}, indent=2))
    else:
        print(_render(decisions, skipped, warnings, dry_run))

    if dry_run:
        return EXIT_OK

    notified = load_holds_state(holds_path)
    new_holds = [d for d in decisions if d["action"] == "hold" and d["tracking"].upper() not in notified]
    if new_holds:
        notify("EZShip hold (not submitted): " + "; ".join(
            f"{d['tracking']} {d['reason']}" for d in new_holds)[:200])
        save_holds_state(holds_path, notified | {d["tracking"].upper() for d in new_holds})

    rejected, submitted_count = [], 0
    for d in decisions:
        if d["action"] != "submit":
            continue
        key = d["tracking"].upper()
        inflight[key] = {"since": _now_iso(), "state": "in-flight"}
        try:
            save_inflight(inflight_path, inflight)  # BEFORE the call: crash-safe
        except InflightError as exc:
            return _fail(f"cannot record in-flight marker, not submitting {d['tracking']}: {exc}",
                         server_url, dry_run)
        code, detail = run_ezship(d)
        if code in (EXIT_SESSION_EXPIRED, -1):
            # Treated as nothing submitted: exit 3 comes from the SubmitNewOrder
            # response (apps/ezship/src/api.ts), so this relies on EZShip rejecting
            # an unauthenticated request, not on proof; -1 means never launched.
            inflight.pop(key, None)
            _save_or_warn(inflight_path, inflight)
        if code == EXIT_SESSION_EXPIRED:
            notify("EZShip session expired. Run: ezship set-cookie. Batch stopped.")
            return _fail("EZShip session expired (exit 3); stopped before "
                         f"{d['tracking']}", server_url, dry_run, EXIT_SESSION_EXPIRED)
        if code == -1:  # ezship could not even launch: every remaining order would fail alike
            notify(f"EZShip could not be launched: {detail}"[:200])
            return _fail(f"ezship launch failure: {detail}", server_url, dry_run)
        # Re-read the ledger: it is the only proof a submission landed, and the
        # only thing standing between a rerun and a second real shipment.
        try:
            landed = d["tracking"].upper() in load_ledger(ledger_path)
        except Exception as exc:  # noqa: BLE001
            return _fail(f"ledger unreadable after submitting {d['tracking']} "
                         f"(left quarantined: {clear_command(d['tracking'])}): {exc}",
                         server_url, dry_run)
        if code == 0 and landed:
            inflight.pop(key, None)
            _save_or_warn(inflight_path, inflight)
            submitted_count += 1
            print(f"submitted {d['tracking']}")
        elif code == 0 or code is None or landed:
            # exit 0 without a ledger entry, a timeout, or an error that still
            # wrote the ledger: the outcome is unknown, so stop before a rerun
            # can double-submit.
            inflight[key] = {"since": inflight[key]["since"], "state": f"unconfirmed exit={code}"}
            _save_or_warn(inflight_path, inflight)
            notify(f"EZShip submission of {d['tracking']} is unconfirmed and quarantined; "
                   f"batch stopped. Check EZShip, then: {clear_command(d['tracking'])}"[:250])
            return _fail(f"unconfirmed submission of {d['tracking']} (exit={code}, "
                         f"ledger={'yes' if landed else 'no'}): {detail}", server_url, dry_run)
        else:
            # Exit 1 is ambiguous (a dropped connection can follow acceptance), so
            # the marker stays: this tracking number is quarantined, not retried.
            inflight[key] = {"since": inflight[key]["since"], "state": f"failed exit={code}"}
            _save_or_warn(inflight_path, inflight)
            rejected.append(d["tracking"])
            print(f"FAILED {d['tracking']} (exit {code}), quarantined; verify on EZShip then: "
                  f"{clear_command(d['tracking'])}: {detail}", file=sys.stderr)

    if rejected:
        notify(f"EZShip rejected {len(rejected)} order(s): {', '.join(rejected)}"[:200])
        return _fail(f"{len(rejected)} order(s) rejected: {', '.join(rejected)}",
                     server_url, dry_run, EXIT_SOME_REJECTED)

    detail = (f"submitted={submitted_count} held={sum(d['action'] == 'hold' for d in decisions)} "
              f"already={sum(d['action'] == 'skip' for d in decisions)}")
    ping_heartbeat(server_url, True, detail)
    print(detail)
    return EXIT_OK


def _save_or_warn(path, data):
    # The in-flight entry already exists on disk from before the call, so a failed
    # update here only loses detail or leaves a stale marker: both fail closed.
    try:
        save_inflight(path, data)
    except InflightError as exc:
        print(f"warning: {exc}", file=sys.stderr)


def _fail(message, server_url, dry_run, code=EXIT_FAILURE):
    print(f"Error: {message}", file=sys.stderr)
    if not dry_run:
        ping_heartbeat(server_url, False, message)
    return code
