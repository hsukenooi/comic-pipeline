"""BUI-1415: the purchase record and the offer log.

Two additive tables on the comics server's DB:

* `purchases` holds one row per eBay order line item, keyed by eBay's
  `OrderLineItemID` (`<item id>-<transaction id>`), so a re-pull of an
  overlapping GetOrders window is an idempotent upsert. `ebay-purchases-pull`
  (apps/ebay) writes it. Every purchase type lands here: auction wins, Buy It
  Now, and accepted Best Offers.
* `offers` holds one row per offer you make, entered by hand through
  `ebay-offer-log` (`source='manual'`). Nothing here places an offer or buys.

GetOrders does not say how an item was bought (no listing type, no Best Offer
flag; checked live 2026-11-17). So `purchase_type` is stored as the client
sent it (normally `unknown`) and refined at read time: an item id with a
Gixen `WON` bid reads as `auction`, and one with an `accepted` offer in the
offer log reads as `best_offer`. A Buy It Now purchase stays `unknown` unless
someone records it as such.

`collection_status` is likewise stored (`pending` / `recorded` / `skipped`)
and refined at read time: an item id in `collection_wins_seen` (written by
`POST /api/comics/collection/record-win/commit`) reads as `recorded`, and an
unrecorded Gixen `WON` reads as `snipe_win` because `/comic:collection-add`'s
snipe flow already picks it up. Nothing here writes to the collection: an
eBay listing title is not an identity, and a wrong identity is the BUI-122
data-loss trigger.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any

PURCHASE_TYPES = ("auction", "buy_it_now", "best_offer", "unknown")
COLLECTION_STATUSES = ("pending", "recorded", "skipped")
OFFER_OUTCOMES = ("pending", "accepted", "declined", "countered", "expired", "withdrawn")
# "ebay" is reserved for a future automatic offer pull, so adding it needs
# no table rebuild; today every row is "manual".
OFFER_SOURCES = ("manual", "ebay")

_sql_list = lambda vals: ", ".join(f"'{v}'" for v in vals)  # noqa: E731

# Columns the client owns. A re-pull overwrites these; `collection_status`,
# `created_at`, and `notes` are server/user state a re-pull never touches.
_PURCHASE_FIELDS = (
    "order_line_item_id", "order_id", "item_id", "transaction_id", "title",
    "seller", "purchased_at", "quantity", "price", "shipping", "tax", "total",
    "currency", "purchase_type", "cancelled",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_purchase_tables(conn: sqlite3.Connection) -> None:
    """Idempotent: CREATE ... IF NOT EXISTS only, safe on an existing DB."""
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS purchases (
            id                 INTEGER PRIMARY KEY,
            order_line_item_id TEXT NOT NULL UNIQUE,
            order_id           TEXT,
            item_id            TEXT NOT NULL,
            transaction_id     TEXT,
            title              TEXT,
            seller             TEXT,
            purchased_at       TEXT NOT NULL,
            quantity           INTEGER NOT NULL DEFAULT 1,
            price              REAL NOT NULL,
            shipping           REAL,
            tax                REAL,
            total              REAL NOT NULL,
            currency           TEXT NOT NULL DEFAULT 'USD',
            purchase_type      TEXT NOT NULL DEFAULT 'unknown'
                               CHECK(purchase_type IN ({_sql_list(PURCHASE_TYPES)})),
            cancelled          INTEGER NOT NULL DEFAULT 0,
            collection_status  TEXT NOT NULL DEFAULT 'pending'
                               CHECK(collection_status IN ({_sql_list(COLLECTION_STATUSES)})),
            notes              TEXT,
            created_at         TEXT NOT NULL,
            updated_at         TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_purchases_item ON purchases(item_id)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_purchases_purchased_at ON purchases(purchased_at)"
    )
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS offers (
            id            INTEGER PRIMARY KEY,
            item_id       TEXT NOT NULL,
            title         TEXT,
            seller        TEXT,
            ask           REAL,
            offer         REAL NOT NULL,
            counteroffer  REAL,
            final_price   REAL,
            outcome       TEXT NOT NULL DEFAULT 'pending'
                          CHECK(outcome IN ({_sql_list(OFFER_OUTCOMES)})),
            source        TEXT NOT NULL DEFAULT 'manual'
                          CHECK(source IN ({_sql_list(OFFER_SOURCES)})),
            notes         TEXT,
            offered_at    TEXT NOT NULL,
            created_at    TEXT NOT NULL,
            updated_at    TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_offers_item ON offers(item_id)")


# ---------------------------------------------------------------------------
# Purchases
# ---------------------------------------------------------------------------


def upsert_purchases(conn: sqlite3.Connection, rows: list[dict[str, Any]]) -> dict[str, int]:
    """Insert or refresh each row by `order_line_item_id`. Commit-free.

    A known `purchase_type` already stored is never downgraded to `unknown`
    by a re-pull (GetOrders cannot tell the types apart, so the client always
    sends `unknown`).
    """
    inserted = updated = 0
    now = _now()
    for r in rows:
        values = {f: r.get(f) for f in _PURCHASE_FIELDS}
        values["purchase_type"] = values["purchase_type"] or "unknown"
        values["cancelled"] = 1 if values["cancelled"] else 0
        values["quantity"] = values["quantity"] or 1
        values["currency"] = values["currency"] or "USD"
        existing = conn.execute(
            "SELECT purchase_type FROM purchases WHERE order_line_item_id = ?",
            (values["order_line_item_id"],),
        ).fetchone()
        if existing is None:
            cols = ", ".join(_PURCHASE_FIELDS)
            marks = ", ".join("?" for _ in _PURCHASE_FIELDS)
            conn.execute(
                f"INSERT INTO purchases ({cols}, created_at, updated_at) "
                f"VALUES ({marks}, ?, ?)",
                (*[values[f] for f in _PURCHASE_FIELDS], now, now),
            )
            inserted += 1
            continue
        if values["purchase_type"] == "unknown":
            values["purchase_type"] = existing[0]
        sets = ", ".join(f"{f} = ?" for f in _PURCHASE_FIELDS if f != "order_line_item_id")
        conn.execute(
            f"UPDATE purchases SET {sets}, updated_at = ? WHERE order_line_item_id = ?",
            (
                *[values[f] for f in _PURCHASE_FIELDS if f != "order_line_item_id"],
                now, values["order_line_item_id"],
            ),
        )
        updated += 1
    return {"inserted": inserted, "updated": updated}


def _table_has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return any(r[1] == column for r in conn.execute(f"PRAGMA table_info({table})"))


def _won_item_ids(conn: sqlite3.Connection) -> set[str]:
    """Item ids with a Gixen WON bid. Empty when `bids` lacks a status column
    (test stubs), so the refinement degrades to the stored value."""
    if not _table_has_column(conn, "bids", "status"):
        return set()
    return {r[0] for r in conn.execute("SELECT item_id FROM bids WHERE status = 'WON'")}


def _recorded_item_ids(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT item_id FROM collection_wins_seen")}


def _accepted_offer_item_ids(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT item_id FROM offers WHERE outcome = 'accepted'")}


def _refine(row: dict[str, Any], won: set[str], recorded: set[str], accepted: set[str]) -> dict[str, Any]:
    item = row["item_id"]
    if row["purchase_type"] == "unknown":
        if item in won:
            row["purchase_type"] = "auction"
        elif item in accepted:
            row["purchase_type"] = "best_offer"
    if row["collection_status"] == "pending":
        if item in recorded:
            row["collection_status"] = "recorded"
        elif item in won:
            row["collection_status"] = "snipe_win"
    row["cancelled"] = bool(row["cancelled"])
    return row


def list_purchases(
    conn: sqlite3.Connection,
    *,
    month: str | None = None,
    collection_status: str | None = None,
) -> list[dict[str, Any]]:
    """Purchases newest first, with `purchase_type` and `collection_status`
    refined (see the module docstring). `month` is `YYYY-MM` of
    `purchased_at` (UTC). `collection_status` filters on the refined value."""
    sql = "SELECT * FROM purchases"
    params: list[Any] = []
    if month:
        sql += " WHERE substr(purchased_at, 1, 7) = ?"
        params.append(month)
    sql += " ORDER BY purchased_at DESC, id DESC"
    won, recorded, accepted = (
        _won_item_ids(conn), _recorded_item_ids(conn), _accepted_offer_item_ids(conn)
    )
    out = [_refine(dict(r), won, recorded, accepted) for r in _rows(conn, sql, params)]
    if collection_status:
        out = [r for r in out if r["collection_status"] == collection_status]
    return out


def monthly_spend(conn: sqlite3.Connection, *, months: int | None = None) -> list[dict[str, Any]]:
    """Spend per `YYYY-MM` (UTC) and currency, newest first. Cancelled line
    items are excluded. `total` is price + shipping + tax as the client
    computed it. Currencies are never summed together: a month with a non-USD
    purchase gets a second entry. `months=N` keeps the newest N months."""
    rows = _rows(
        conn,
        "SELECT substr(purchased_at, 1, 7) AS month, currency, COUNT(*) AS count, "
        "ROUND(SUM(total), 2) AS total, ROUND(SUM(price), 2) AS price, "
        "ROUND(SUM(COALESCE(shipping, 0)), 2) AS shipping, "
        "ROUND(SUM(COALESCE(tax, 0)), 2) AS tax "
        "FROM purchases WHERE cancelled = 0 GROUP BY month, currency "
        "ORDER BY month DESC, currency",
        [],
    )
    out = [dict(r) for r in rows]
    if months:
        keep = sorted({r["month"] for r in out}, reverse=True)[:months]
        out = [r for r in out if r["month"] in keep]
    return out


def set_purchase_collection_status(
    conn: sqlite3.Connection, order_line_item_id: str, status: str
) -> dict[str, Any] | None:
    """Store a collection status by hand. None when the line item is unknown.
    Commit-free."""
    cur = conn.execute(
        "UPDATE purchases SET collection_status = ?, updated_at = ? "
        "WHERE order_line_item_id = ?",
        (status, _now(), order_line_item_id),
    )
    if cur.rowcount == 0:
        return None
    return {"order_line_item_id": order_line_item_id, "collection_status": status}


# ---------------------------------------------------------------------------
# Offers
# ---------------------------------------------------------------------------

_OFFER_EDITABLE = (
    "title", "seller", "ask", "offer", "counteroffer", "final_price", "outcome", "notes",
)


def insert_offer(conn: sqlite3.Connection, offer: dict[str, Any]) -> dict[str, Any]:
    """Log one offer. Commit-free. Returns the stored row."""
    now = _now()
    cur = conn.execute(
        "INSERT INTO offers (item_id, title, seller, ask, offer, counteroffer, "
        "final_price, outcome, source, notes, offered_at, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            offer["item_id"], offer.get("title"), offer.get("seller"), offer.get("ask"),
            offer["offer"], offer.get("counteroffer"), offer.get("final_price"),
            offer.get("outcome") or "pending", offer.get("source") or "manual",
            offer.get("notes"), offer.get("offered_at") or now, now, now,
        ),
    )
    return get_offer(conn, cur.lastrowid)


def update_offer(
    conn: sqlite3.Connection, offer_id: int, changes: dict[str, Any]
) -> dict[str, Any] | None:
    """Apply only the given fields. None when the offer id is unknown.
    Commit-free."""
    if get_offer(conn, offer_id) is None:
        return None
    fields = [f for f in _OFFER_EDITABLE if f in changes]
    if fields:
        sets = ", ".join(f"{f} = ?" for f in fields)
        conn.execute(
            f"UPDATE offers SET {sets}, updated_at = ? WHERE id = ?",
            (*[changes[f] for f in fields], _now(), offer_id),
        )
    return get_offer(conn, offer_id)


def get_offer(conn: sqlite3.Connection, offer_id: int) -> dict[str, Any] | None:
    rows = _rows(conn, "SELECT * FROM offers WHERE id = ?", [offer_id])
    return dict(rows[0]) if rows else None


def list_offers(
    conn: sqlite3.Connection, *, item_id: str | None = None, seller: str | None = None
) -> list[dict[str, Any]]:
    sql = "SELECT * FROM offers"
    clauses, params = [], []
    if item_id:
        clauses.append("item_id = ?")
        params.append(item_id)
    if seller:
        clauses.append("seller = ?")
        params.append(seller)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY offered_at DESC, id DESC"
    return [dict(r) for r in _rows(conn, sql, params)]


def _rows(conn: sqlite3.Connection, sql: str, params: list[Any]) -> list[sqlite3.Row]:
    """Run a read with sqlite3.Row rows whatever the connection's factory."""
    cur = conn.cursor()
    cur.row_factory = sqlite3.Row
    return cur.execute(sql, params).fetchall()
