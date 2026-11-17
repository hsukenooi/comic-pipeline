#!/usr/bin/env python3
"""ebay-slab-alerts: Telegram alerts for underpriced CGC slabs (BUI-1419).

One scan, run every 12 hours by `com.comics.slab-alerts`:

  1. Ratings. Read Act/Pass button presses with Telegram `getUpdates` (from a
     stored offset), store them on the comics server, then answer them.
  2. Hunt list. `GET /api/comics/slab-watch` items with both `min_grade` and
     `ceiling` set.
  3. Search. One eBay Browse search per book, screened by
     `slab_deals.screen_listings` (CGC blue label, the right book).
  4. Price. Every copy is priced all-in: item + shipping, plus tax at
     SLAB_ALERT_TAX_RATE on both. A Best Offer copy's item price is 0.89 x
     the ask (`slab_deals.BEST_OFFER_ACCEPTED_RATIO`). An auction uses its
     current bid; its max bid keeps the all-in price at min(threshold,
     ceiling).
  5. Seen-set. A copy alerted before comes back only when its expected price
     dropped, a Best Offer appeared, or an auction entered its last day.
  6. Market. `comic-fmv --batch` graded mode prices each remaining copy at its
     grade. The market price is the band median; only MEDIUM, MEDIUM-HIGH, or
     HIGH confidence can alert. A failed or missing price never alerts.
  7. Gate. grade >= min_grade, expected <= ceiling, and expected <=
     (1 - SLAB_ALERT_MARGIN) x market.
  8. Checks. `slab_checks.check_listing` on the full item. A weak seller
     drops the copy; no returns is shown in the alert, never dropped.
  9. Send. One Telegram message, largest discount first, with Act and Pass
     buttons per copy. No message when nothing qualifies. This month's spend
     (`/api/comics/purchases/spend`) is shown for information only.

Every copy seen is logged on the server (`POST /api/comics/slab-alerts`):
first and last seen, and ended when a complete search no longer finds it.
The alert state is written only after Telegram accepts the message, so a
failed send retries next scan rather than going silent.

`--dry-run` prints the message and writes nothing: no server writes, no
Telegram calls, and `comic-fmv --probe`.

Env:
    SLAB_ALERT_MARGIN            default 0.10 (alert at <= 90% of market)
    SLAB_ALERT_TAX_RATE          default 0.08 (no tax estimate existed before)
    SLAB_ALERT_DEFAULT_SHIPPING  default 15.0 (used when the listing shows none)
    SLAB_ALERT_MAX_BOOKS         default 0 = every hunt-list book
    SLAB_ALERT_MAX_ITEM_FETCHES  default 40 (Browse getItem calls per scan)
    SLAB_ALERT_STATE_FILE        default ~/.comics-server/slab-alerts-state.json
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID  (else read from
                                 ~/.config/tasks-to-linear.env, those two lines only)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

import slab_checks
import slab_deals
from seller_scan import _server_base

DEFAULT_MARGIN = 0.10
DEFAULT_TAX_RATE = 0.08
DEFAULT_SHIPPING = 15.0
DEFAULT_MAX_ITEM_FETCHES = 40
MAX_IN_MESSAGE = 15
MESSAGE_BUDGET = 3800  # Telegram caps a message at 4096 characters
LAST_DAY = timedelta(hours=24)
ALERT_CONFIDENCE = {"MEDIUM", "MEDIUM-HIGH", "HIGH"}
CALLBACK_RE = re.compile(r"^sa:(act|pass):(\d{6,20})$")
TELEGRAM_ENV_FILE = Path.home() / ".config" / "tasks-to-linear.env"
FMV_TIMEOUT = 3600
_READ_TIMEOUT = 30


class SlabAlertError(Exception):
    """A hard failure: the scan stops and sends nothing."""


# --- Config -----------------------------------------------------------------


def _env_float(name: str, default: float, lo: float, hi: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        val = float(raw)
    except ValueError:
        raise SlabAlertError(f"{name}={raw!r} is not a number") from None
    if not lo <= val <= hi:
        raise SlabAlertError(f"{name}={raw!r} is outside {lo}-{hi}")
    return val


def config() -> dict[str, float]:
    return {
        "margin": _env_float("SLAB_ALERT_MARGIN", DEFAULT_MARGIN, 0.0, 0.9),
        "tax_rate": _env_float("SLAB_ALERT_TAX_RATE", DEFAULT_TAX_RATE, 0.0, 0.3),
        "default_shipping": _env_float("SLAB_ALERT_DEFAULT_SHIPPING", DEFAULT_SHIPPING,
                                       0.0, 500.0),
        "max_books": int(_env_float("SLAB_ALERT_MAX_BOOKS", 0, 0, 10000)),
        "max_item_fetches": int(_env_float("SLAB_ALERT_MAX_ITEM_FETCHES",
                                           DEFAULT_MAX_ITEM_FETCHES, 0, 10000)),
    }


def _state_file() -> Path:
    raw = os.environ.get("SLAB_ALERT_STATE_FILE")
    return Path(raw) if raw else Path.home() / ".comics-server" / "slab-alerts-state.json"


def load_state(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        raise SlabAlertError(f"state file {path} is unreadable: {e}") from e
    return data if isinstance(data, dict) else {}


def save_state(path: Path, state: dict) -> None:
    """Atomic: a crash mid-write leaves the old file, never a torn one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".slab-alerts-")
    with os.fdopen(fd, "w") as f:
        json.dump(state, f)
    os.replace(tmp, path)


_LOCK_HANDLE = None


def _lock(state_path: Path) -> None:
    """One live scan at a time (launchd plus a manual run would double-alert).
    The lock is held until the process exits."""
    global _LOCK_HANDLE
    import fcntl
    state_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(state_path.with_suffix(".lock"), "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        raise SlabAlertError("another ebay-slab-alerts scan is running") from None
    _LOCK_HANDLE = fh


def telegram_creds(env_file: Path = TELEGRAM_ENV_FILE) -> tuple[str, str]:
    """TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID from the environment, else the
    two matching lines of the env file (nothing else in it is read)."""
    vals = {k: os.environ.get(k) for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")}
    if not all(vals.values()) and env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line.startswith("export "):
                line = line[7:]
            key, sep, val = line.partition("=")
            if sep and key in vals and not vals[key]:
                vals[key] = val.strip().strip('"').strip("'")
    if not all(vals.values()):
        raise SlabAlertError("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set")
    return vals["TELEGRAM_BOT_TOKEN"], vals["TELEGRAM_CHAT_ID"]


# --- Telegram ---------------------------------------------------------------


def telegram_call(token: str, method: str, payload: dict, *, retries: int = 3,
                  sleep=time.sleep) -> Any:
    """POST one Bot API method. Honors a 429 `retry_after` up to 60 s, then
    gives up. Errors never carry the token (it is in the URL)."""
    url = f"https://api.telegram.org/bot{token}/{method}"
    for attempt in range(retries):
        try:
            resp = requests.post(url, json=payload, timeout=_READ_TIMEOUT)
        except requests.exceptions.RequestException as e:
            raise SlabAlertError(
                f"telegram {method} failed: {str(e).replace(token, '<token>')}") from None
        try:
            data = resp.json()
        except ValueError:
            data = {}
        if resp.status_code == 429 and attempt < retries - 1:
            params = data.get("parameters") if isinstance(data, dict) else None
            wait = (params or {}).get("retry_after", 5)
            if not isinstance(wait, (int, float)) or wait > 60:
                raise SlabAlertError(f"telegram {method} rate-limited ({wait}s)")
            sleep(wait)
            continue
        if not isinstance(data, dict) or not data.get("ok"):
            desc = data.get("description") if isinstance(data, dict) else None
            raise SlabAlertError(
                f"telegram {method} HTTP {resp.status_code}: {desc or 'no description'}")
        return data.get("result")
    raise SlabAlertError(f"telegram {method} still rate-limited after {retries} tries")


def parse_ratings(updates: list, chat_id: str) -> tuple[list[dict], list[str], int | None]:
    """(ratings, callback ids to answer, next offset) from getUpdates. Only a
    `sa:act|pass:<item id>` press from our chat counts; everything else is
    skipped but still confirmed past."""
    ratings: list[dict] = []
    answers: list[str] = []
    last_id = None
    for u in updates if isinstance(updates, list) else []:
        if not isinstance(u, dict):
            continue
        if isinstance(u.get("update_id"), int):
            last_id = max(last_id or u["update_id"], u["update_id"])
        cq = u.get("callback_query")
        if not isinstance(cq, dict):
            continue
        if cq.get("id"):
            answers.append(str(cq["id"]))
        chat = ((cq.get("message") or {}).get("chat") or {}).get("id")
        m = CALLBACK_RE.match(str(cq.get("data") or ""))
        if m is None or str(chat) != str(chat_id):
            continue
        ratings.append({"item_id": m.group(2), "rating": m.group(1)})
    return ratings, answers, (last_id + 1 if last_id is not None else None)


def process_ratings(base: str, token: str, chat_id: str, state_path: Path) -> int:
    """Store button presses, then advance the offset, then answer them.
    Storing first means a crash replays the presses next scan (idempotent
    on the server) instead of losing them."""
    state = load_state(state_path)
    payload: dict[str, Any] = {"timeout": 0}
    if isinstance(state.get("telegram_offset"), int):
        payload["offset"] = state["telegram_offset"]
    try:
        updates = telegram_call(token, "getUpdates", payload)
    except SlabAlertError as e:  # ratings wait for the next scan; alerts still go
        print(f"warning: ratings not read: {e}", file=sys.stderr)
        return 0
    ratings, answers, offset = parse_ratings(updates, chat_id)
    if ratings:
        _post(base, "/api/comics/slab-alerts/ratings", {"ratings": ratings})
    if offset is not None:
        state["telegram_offset"] = offset
        save_state(state_path, state)
    for cq_id in answers:
        try:  # best effort: a press older than ~15 min can no longer be answered
            telegram_call(token, "answerCallbackQuery",
                          {"callback_query_id": cq_id, "text": "Recorded"}, retries=1)
        except SlabAlertError:
            pass
    return len(ratings)


# --- Comics server ----------------------------------------------------------


def _get(base: str, path: str, params: dict | None = None) -> Any:
    try:
        resp = requests.get(f"{base}{path}", params=params, timeout=_READ_TIMEOUT)
    except requests.exceptions.RequestException as e:
        raise SlabAlertError(f"GET {path} failed: {e}") from e
    if resp.status_code != 200:
        raise SlabAlertError(f"GET {path} HTTP {resp.status_code}: {resp.text[:160]}")
    return resp.json()


def _post(base: str, path: str, body: dict) -> Any:
    try:
        resp = requests.post(f"{base}{path}", json=body, timeout=_READ_TIMEOUT)
    except requests.exceptions.RequestException as e:
        raise SlabAlertError(f"POST {path} failed: {e}") from e
    if resp.status_code != 200:
        raise SlabAlertError(f"POST {path} HTTP {resp.status_code}: {resp.text[:160]}")
    return resp.json()


def hunt_list(base: str) -> list[dict]:
    data = _get(base, "/api/comics/slab-watch")
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise SlabAlertError("slab-watch returned no items list")
    return [i for i in items if isinstance(i, dict) and i.get("comic_id")
            and i.get("title") and i.get("issue") is not None
            and i.get("min_grade") is not None and i.get("ceiling") is not None]


def month_spend(base: str, now: datetime) -> str:
    """Informational only: a failure reads 'unavailable', never blocks."""
    try:
        months = _get(base, "/api/comics/purchases/spend", {"months": 1}).get("months") or []
    except (SlabAlertError, AttributeError, ValueError):
        return "This month's spend: unavailable"
    month = now.strftime("%Y-%m")
    total = sum(float(m.get("total") or 0) for m in months
                if isinstance(m, dict) and m.get("month") == month
                and (m.get("currency") or "USD") == "USD")
    return f"This month's spend: ${total:,.0f} (no cap)"


# --- Pricing and gates ------------------------------------------------------


def expected_price(copy: dict, cfg: dict) -> tuple[float, float]:
    """(expected all-in, shipping used). Best Offer item price is 0.89 x ask."""
    ship = copy.get("shipping")
    ship = cfg["default_shipping"] if ship is None else float(ship)
    item = copy["price"]
    if copy.get("best_offer") and copy.get("listing_type") != "Auction":
        item = item * slab_deals.BEST_OFFER_ACCEPTED_RATIO
    return round((item + ship) * (1 + cfg["tax_rate"]), 2), ship


def item_cap(all_in_cap: float, ship: float, cfg: dict) -> float:
    """Item price (bid or offer) that keeps the all-in price at `all_in_cap`."""
    return max(round(all_in_cap / (1 + cfg["tax_rate"]) - ship, 2), 0.0)


def _end(copy: dict) -> datetime | None:
    raw = copy.get("end_date")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def in_last_day(copy: dict, now: datetime) -> bool:
    end = _end(copy)
    return copy.get("listing_type") == "Auction" and end is not None and end - now <= LAST_DAY


def needs_alert(copy: dict, prev: dict | None, now: datetime) -> bool:
    """The seen-set rule: alert a copy once, then again only on a price drop,
    a new Best Offer, or an auction entering its last day."""
    if not prev or not prev.get("last_alerted_at"):
        return True
    alerted = prev.get("alerted_expected")
    # A drop must be real money (1% or $1, whichever is more), so a shuffled
    # shipping option or rounding never re-sends the same copy.
    if alerted is None or copy["expected"] <= float(alerted) - max(1.0, 0.01 * float(alerted)):
        return True
    if copy.get("best_offer") and not prev.get("alerted_best_offer"):
        return True
    return in_last_day(copy, now) and not prev.get("alerted_last_day")


def run_fmv(rows: list[dict], *, probe: bool) -> dict[str, dict]:
    """comic-fmv --batch graded mode -> {item_id: fmv dict}. Hard-fails on any
    error: a failed price must never read as a cheap copy."""
    if not rows:
        return {}
    with tempfile.TemporaryDirectory(prefix="slab-alerts-") as d:
        batch, out = Path(d) / "batch.json", Path(d) / "out.json"
        batch.write_text(json.dumps(rows))
        cmd = ["comic-fmv", "--batch", str(batch), "--out", str(out), "--quiet"]
        if probe:
            cmd.append("--probe")
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=FMV_TIMEOUT)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise SlabAlertError(f"comic-fmv failed to run: {e}") from e
        if proc.returncode != 0:
            raise SlabAlertError(
                f"comic-fmv exited {proc.returncode}: {proc.stderr.strip()[-300:]}")
        try:
            results = json.loads(out.read_text())
        except (OSError, ValueError) as e:
            raise SlabAlertError(f"comic-fmv output unreadable: {e}") from e
    by_item: dict[str, dict] = {}
    for r in results if isinstance(results, list) else []:
        if not isinstance(r, dict):
            continue
        item_id = (r.get("input") or {}).get("item_id")
        if item_id:
            by_item[str(item_id)] = r.get("fmv") or {}
    return by_item


def market_of(fmv: dict) -> tuple[float | None, str | None, str | None]:
    """(market, confidence, skip reason). Market is the band median; a
    refused, unpriced, or below-MEDIUM row has no usable market."""
    conf = str(fmv.get("confidence") or "").upper() or None
    if fmv.get("flag_reason"):
        return None, conf, f"fmv refused ({fmv['flag_reason']})"
    if conf not in ALERT_CONFIDENCE:
        return None, conf, f"confidence {conf or 'none'}"
    med = fmv.get("median")
    if not isinstance(med, (int, float)) or isinstance(med, bool) or med <= 0:
        return None, conf, "no median"
    return float(med), conf, None


# --- Scan -------------------------------------------------------------------


def _keyword(title: str, issue: str) -> str:
    return f"{slab_deals._LEADING_ARTICLE_RE.sub('', title)} #{issue} CGC"


def scan_book(book: dict, cfg: dict, prev_rows: dict[str, dict], now: datetime,
              search=slab_deals.search_listings) -> tuple[list[dict], list[dict], list[dict]]:
    """(observations, eligible copies, ended observations) for one book."""
    title, issue = book["title"], str(book["issue"])
    try:
        year = int(book["year"]) if book.get("year") not in (None, "") else None
    except (TypeError, ValueError):
        year = None
    raw = search(_keyword(title, issue), max_results=slab_deals.DEFAULT_MAX_RESULTS)
    by_id = {str(i.get("item_id")): i for i in raw if isinstance(i, dict)}
    kept, _dropped = slab_deals.screen_listings(raw, title=title, issue=issue, year=year)
    stamp = now.isoformat()
    observations, eligible = [], []
    seen: set[str] = set()
    for c in kept:
        item_id = str(c.get("item_id") or "")
        if not item_id or c.get("grade") is None:
            continue
        seen.add(item_id)
        src = by_id.get(item_id, {})
        copy = {
            "item_id": item_id, "comic_id": book["comic_id"], "book": f"{title} #{issue}",
            "title": (c.get("title") or "")[:300], "grade": float(c["grade"]),
            "listing_type": c.get("listing_type"), "best_offer": bool(src.get("best_offer")),
            "price": float(c["price"]), "shipping": src.get("shipping"),
            "end_date": c.get("end_date_iso"), "url": c.get("url"),
            "page_quality": c.get("page_quality") or "unknown",
            "min_grade": float(book["min_grade"]), "ceiling": float(book["ceiling"]),
            "year": year, "issue": issue, "series": title,
        }
        copy["expected"], copy["ship_used"] = expected_price(copy, cfg)
        obs = {k: copy[k] for k in ("item_id", "comic_id", "title", "grade", "listing_type",
                                    "best_offer", "price", "shipping", "expected", "end_date")}
        obs["last_seen_at"] = stamp
        prev = prev_rows.get(item_id)
        if prev is None:
            obs["first_seen_at"] = stamp
        elif prev.get("ended_at"):
            obs["ended_at"] = None  # seen again: a search missed it once
        observations.append(obs)
        if (copy["grade"] >= copy["min_grade"] and copy["expected"] <= copy["ceiling"]
                and needs_alert(copy, prev, now)):
            eligible.append(copy)
    ended = []
    if len(raw) < slab_deals.DEFAULT_MAX_RESULTS:  # a capped search proves nothing
        for item_id, prev in prev_rows.items():
            if (prev.get("comic_id") == book["comic_id"] and not prev.get("ended_at")
                    and item_id not in seen):
                ended.append({"item_id": item_id, "ended_at": stamp})
    return observations, eligible, ended


def fmv_rows(copies: list[dict]) -> list[dict]:
    rows = []
    for c in copies:
        (row,) = slab_deals.fmv_batch([c], title=c["series"], issue=c["issue"],
                                      year=c["year"], publisher=None)
        rows.append(row)
    return rows


def gate(copies: list[dict], fmvs: dict[str, dict], cfg: dict) -> list[dict]:
    """Copies that clear the confidence and margin gates, with market added."""
    out = []
    for c in copies:
        market, conf, _skip = market_of(fmvs.get(c["item_id"]) or {})
        c["confidence"] = conf
        if market is None:
            continue
        c["market"] = market
        c["threshold"] = round((1 - cfg["margin"]) * market, 2)
        if c["expected"] <= c["threshold"] and c["expected"] <= c["ceiling"]:
            c["discount"] = round(1 - c["expected"] / market, 4)
            out.append(c)
    return out


def check_copies(copies: list[dict], cfg: dict, fetch,
                 prev_rows: dict[str, dict] | None = None,
                 now: datetime | None = None) -> list[dict]:
    """Pre-alert checks on the full item, largest discount first, up to
    SLAB_ALERT_MAX_ITEM_FETCHES. A failed fetch never alerts. When the item
    shows shipping the summary did not, the copy is re-priced and re-gated,
    including the seen-set rule (else a default-shipping estimate below the
    real cost would read as a price drop every scan)."""
    prev_rows = prev_rows or {}
    now = now or datetime.now(timezone.utc)
    out = []
    for c in sorted(copies, key=lambda c: -c["discount"])[:cfg["max_item_fetches"]]:
        item = fetch(c["item_id"])
        if not isinstance(item, dict):
            continue
        verdict = slab_checks.check_listing(item)
        if verdict["verdict"] == "drop":
            continue
        if c.get("shipping") is None:
            from ebay_fetch import summary_shipping_usd
            ship = summary_shipping_usd(item)
            if ship is not None:
                c["shipping"] = ship
                c["expected"], c["ship_used"] = expected_price(c, cfg)
                if (c["expected"] > min(c["threshold"], c["ceiling"])
                        or not needs_alert(c, prev_rows.get(c["item_id"]), now)):
                    continue
                c["discount"] = round(1 - c["expected"] / c["market"], 4)
        c["checks"] = verdict
        out.append(c)
    out.sort(key=lambda c: -c["discount"])
    return out


def _copy_lines(i: int, c: dict, cfg: dict, now: datetime) -> list[str]:
    cap = item_cap(min(c["threshold"], c["ceiling"]), c["ship_used"], cfg)
    kind = c["listing_type"] or "?"
    if c["listing_type"] == "Auction":
        end = _end(c)
        left = f", ends in {max((end - now).total_seconds(), 0) / 3600:.0f}h" if end else ""
        price = f"bid ${c['price']:,.0f}{left}. Max bid ${cap:,.0f}"
    elif c.get("best_offer"):
        kind = "Best Offer"
        price = (f"ask ${c['price']:,.0f}, likely ${c['price'] * slab_deals.BEST_OFFER_ACCEPTED_RATIO:,.0f}"
                 f". Walk away above ${cap:,.0f}")
    else:
        price = f"BIN ${c['price']:,.0f}. Walk away above ${cap:,.0f}"
    ship = "ship est." if c.get("shipping") is None else f"ship ${c['ship_used']:,.0f}"
    notes = list(c["checks"].get("flags") or [])
    if c["checks"].get("verdict") == "pass_unverified":
        notes.append("no cert number")
    notes = [n.replace("returns not accepted", "no returns") for n in notes]
    return [
        f"{i}. {c['book']} CGC {c['grade']:g} ({kind}), {c['discount']:.0%} under market",
        f"   {price}",
        f"   all-in ${c['expected']:,.0f} ({ship}, tax) vs market ${c['market']:,.0f} "
        f"({c['confidence']}), ceiling ${c['ceiling']:,.0f}",
        *( [f"   Note: {', '.join(notes)}"] if notes else [] ),
        f"   {c['url']}",
    ]


def render(alerts: list[dict], spend_line: str, cfg: dict,
           now: datetime) -> tuple[str, dict, list[dict]]:
    """(text, reply_markup, copies shown). Copies past the size budget are
    not shown and so not marked alerted: they alert next scan."""
    head = [f"Slab alerts: {len(alerts)} copies at or under "
            f"{1 - cfg['margin']:.0%} of market", spend_line, ""]
    text, shown, kb = "\n".join(head), [], []
    for c in alerts[:MAX_IN_MESSAGE]:
        block = "\n".join(_copy_lines(len(shown) + 1, c, cfg, now)) + "\n"
        if len(text) + len(block) > MESSAGE_BUDGET:
            break
        text += block
        shown.append(c)
        n = len(shown)
        kb.append([{"text": f"Act {n}", "callback_data": f"sa:act:{c['item_id']}"},
                   {"text": f"Pass {n}", "callback_data": f"sa:pass:{c['item_id']}"}])
    if len(shown) < len(alerts):
        text += f"\n+{len(alerts) - len(shown)} more next scan"
    return text.rstrip(), {"inline_keyboard": kb}, shown


def _default_fetch():
    from ebay_fetch import fetch_item, get_token, load_config
    client_id, client_secret, base_url = load_config()
    token = get_token(client_id, client_secret, base_url)
    return lambda item_id: fetch_item(item_id, token, base_url)


def scan(*, dry_run: bool, max_books: int | None = None, now: datetime | None = None,
         search=slab_deals.search_listings, fetch=None, fmv=run_fmv,
         out=print) -> dict:
    now = now or datetime.now(timezone.utc)
    cfg = config()
    if max_books is not None:
        cfg["max_books"] = max_books
    base = _server_base()
    if not base:
        raise SlabAlertError("COMICS_SERVER_URL is not set")
    try:
        slab_deals._health_gate(base)
    except slab_deals.SlabDealsError as e:
        raise SlabAlertError(str(e)) from e
    state_path = _state_file()
    rated = 0
    if not dry_run:
        _lock(state_path)
        token, chat_id = telegram_creds()
        rated = process_ratings(base, token, chat_id, state_path)
    books = hunt_list(base)
    if cfg["max_books"]:
        books = books[:cfg["max_books"]]
    listed = _get(base, "/api/comics/slab-alerts")
    prev_rows = {str(r["item_id"]): r for r in (listed.get("copies") or [])
                 if isinstance(r, dict) and r.get("item_id")}
    observations, eligible, errors = [], [], []
    for book in books:
        try:
            obs, elig, ended = scan_book(book, cfg, prev_rows, now, search=search)
        except slab_deals.SlabDealsError as e:
            errors.append(f"{book.get('title')} #{book.get('issue')}: {e}")
            continue
        observations += obs + ended
        have = {e["item_id"] for e in eligible}
        eligible += [e for e in elig if e["item_id"] not in have]
    fmvs = fmv(fmv_rows(eligible), probe=dry_run)
    passed = gate(eligible, fmvs, cfg)
    for o in observations:  # log the market seen, alerted or not
        c = next((e for e in eligible if e["item_id"] == o["item_id"]), None)
        if c and c.get("market"):
            o["market"], o["confidence"] = c["market"], c["confidence"]
    alerts = (check_copies(passed, cfg, fetch or _default_fetch(), prev_rows, now)
              if passed else [])
    text, markup, shown = render(alerts, month_spend(base, now), cfg, now) if alerts \
        else ("", {}, [])
    summary = {"books": len(books), "copies": len(observations), "eligible": len(eligible),
               "priced_pass": len(passed), "alerts": len(shown), "ratings": rated,
               "errors": errors}
    if dry_run:
        out(text or "No copies qualify; no message would be sent.")
        out(json.dumps(summary))
        return summary
    if observations:
        _post(base, "/api/comics/slab-alerts", {"copies": observations})
    if shown:
        token, chat_id = telegram_creds()
        telegram_call(token, "sendMessage", {"chat_id": chat_id, "text": text,
                                             "reply_markup": markup,
                                             "disable_web_page_preview": True})
        stamp = now.isoformat()
        _post(base, "/api/comics/slab-alerts", {"copies": [
            {"item_id": c["item_id"], "last_alerted_at": stamp,
             "alerted_expected": c["expected"], "alerted_best_offer": c["best_offer"],
             "alerted_last_day": in_last_day(c, now)} for c in shown]})
    out(json.dumps(summary))
    return summary


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(
        prog="ebay-slab-alerts",
        description="Scan the slab hunt list and send one Telegram alert for CGC copies "
                    "priced at or under the margin below market. Never bids or buys.",
        epilog="Env: SLAB_ALERT_MARGIN (0.10), SLAB_ALERT_TAX_RATE (0.08), "
               "SLAB_ALERT_DEFAULT_SHIPPING (15), SLAB_ALERT_MAX_BOOKS (0 = all), "
               "SLAB_ALERT_MAX_ITEM_FETCHES (40), SLAB_ALERT_STATE_FILE, "
               "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID.")
    p.add_argument("--dry-run", action="store_true",
                   help="Print the message; write nothing and call no Telegram API "
                        "(comic-fmv runs with --probe)")
    p.add_argument("--max-books", type=int, default=None,
                   help="Scan only the first N hunt-list books (overrides SLAB_ALERT_MAX_BOOKS)")
    args = p.parse_args(argv)
    try:
        summary = scan(dry_run=args.dry_run, max_books=args.max_books)
    except SlabAlertError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    if summary["errors"]:
        print("search errors: " + "; ".join(summary["errors"]), file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
