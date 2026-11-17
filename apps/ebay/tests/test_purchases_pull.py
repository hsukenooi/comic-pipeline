"""BUI-1415: ebay-purchases-pull and ebay-offer-log.

GetOrders XML here mirrors a live response read 2026-11-17: per-line
TransactionPrice, ActualShippingCost, Taxes/TotalTaxAmount, OrderLineItemID;
order-level ShippingServiceSelected/ShippingServiceCost and Total. No field
names the listing type or a Best Offer.
"""
from __future__ import annotations

import json
import xml.etree.ElementTree as ET

import pytest
import requests

import ebay_user_token as eut
import offer_log
import purchases_pull as pp

NSDECL = 'xmlns="urn:ebay:apis:eBLBaseComponents"'


def txn(item_id, txn_id="10", price="100.00", ship="5.00", tax="8.00", qty=1, line_id=True):
    ship_el = f"<ActualShippingCost>{ship}</ActualShippingCost>" if ship is not None else ""
    tax_el = f"<Taxes><TotalTaxAmount>{tax}</TotalTaxAmount></Taxes>" if tax is not None else ""
    line = f"<OrderLineItemID>{item_id}-{txn_id}</OrderLineItemID>" if line_id else ""
    return (
        f"<Transaction><Item><ItemID>{item_id}</ItemID><Title>Book {item_id}</Title></Item>"
        f"<QuantityPurchased>{qty}</QuantityPurchased><CreatedDate>2026-11-03T09:14:26.000Z</CreatedDate>"
        f'<TransactionID>{txn_id}</TransactionID><TransactionPrice currencyID="USD">{price}</TransactionPrice>'
        f"{ship_el}{tax_el}{line}</Transaction>"
    )


def order(order_id, txns, total=None, ship="12.40", status="Completed", cancel="NotApplicable", refunds=""):
    total_el = f"<Total>{total}</Total>" if total is not None else ""
    return (
        f"<Order><OrderID>{order_id}</OrderID><OrderStatus>{status}</OrderStatus>"
        f"<CancelStatus>{cancel}</CancelStatus><SellerUserID>seller_a</SellerUserID>"
        f"<CreatedTime>2026-11-02T00:00:00.000Z</CreatedTime>"
        f"<ShippingServiceSelected><ShippingServiceCost>{ship}</ShippingServiceCost></ShippingServiceSelected>"
        f"{total_el}{refunds}<TransactionArray>{''.join(txns)}</TransactionArray></Order>"
    )


def response(orders, more=False):
    return (
        f'<?xml version="1.0"?><GetOrdersResponse {NSDECL}><Ack>Success</Ack>'
        f"<HasMoreOrders>{'true' if more else 'false'}</HasMoreOrders>"
        f"<OrderArray>{''.join(orders)}</OrderArray></GetOrdersResponse>"
    ).encode()


def parse(*orders):
    return pp.parse_purchases(ET.fromstring(response(list(orders))))


def test_one_row_per_line_item_with_totals():
    rows, warnings = parse(order("O1", [txn("111", qty=2), txn("222", "20", ship=None, tax=None)]))
    assert [r["order_line_item_id"] for r in rows] == ["111-10", "222-20"]
    a, b = rows
    assert (a["price"], a["shipping"], a["tax"], a["total"]) == (200.0, 5.0, 8.0, 213.0)
    assert a["purchase_type"] == "unknown" and a["seller"] == "seller_a"
    assert a["purchased_at"] == "2026-11-03T09:14:26.000Z"
    # Multi-line order: the part of the order shipping the other line does not
    # account for (12.40 - 5.00) lands on the line with no ActualShippingCost.
    assert (b["shipping"], b["tax"], b["total"]) == (7.4, None, 107.4)
    assert (a["shipping_estimated"], b["shipping_estimated"]) == (False, True)
    assert (a["refunded"], b["refunded"]) == (0.0, 0.0)
    assert warnings == []


def test_multi_line_shipping_split_by_price_and_sums_exactly():
    rows, _ = parse(order("O5", [txn("1", "1", price="300.00", ship=None, tax=None),
                                 txn("2", "2", price="100.00", ship=None, tax=None),
                                 txn("3", "3", price="100.00", ship=None, tax=None)],
                          ship="10.00", total="510.00"))
    assert [r["shipping"] for r in rows] == [6.0, 2.0, 2.0]
    assert all(r["shipping_estimated"] for r in rows)


def test_multi_line_shipping_with_no_order_cost_is_flagged_missing():
    xml = order("O6", [txn("1", "1", ship=None), txn("2", "2", ship=None)]).replace(
        "<ShippingServiceSelected><ShippingServiceCost>12.40</ShippingServiceCost>"
        "</ShippingServiceSelected>", "")
    rows, warnings = parse(xml)
    assert [r["shipping"] for r in rows] == [None, None]
    assert all(r["shipping_estimated"] for r in rows)
    assert any("not derivable" in w for w in warnings)


def refund(amount, status="Succeeded", cur="USD"):
    return (f"<MonetaryDetails><Refunds><Refund><RefundStatus>{status}</RefundStatus>"
            f'<RefundAmount currencyID="{cur}">{amount}</RefundAmount></Refund></Refunds></MonetaryDetails>')


def test_refund_is_spread_over_lines_and_never_exceeds_a_line():
    rows, _ = parse(order("O7", [txn("1", "1", price="100.00", ship="0", tax="0"),
                                 txn("2", "2", price="300.00", ship="0", tax="0")],
                          refunds=refund("100.00")))
    assert [r["refunded"] for r in rows] == [25.0, 75.0]
    rows, _ = parse(order("O8", [txn("3", "3", price="50.00", ship="0", tax="0")],
                          refunds=refund("80.00")))
    assert rows[0]["refunded"] == 50.0  # capped at the line total


def test_full_refund_keeps_the_line_with_net_zero():
    rows, _ = parse(order("O9", [txn("4", "4")], refunds=refund("113.00")))
    assert rows[0]["total"] == 113.0 and rows[0]["refunded"] == 113.0
    assert rows[0]["cancelled"] is False


def test_unsettled_and_foreign_currency_refunds_are_not_netted():
    rows, _ = parse(order("O10", [txn("5", "5")], refunds=refund("50.00", status="Pending")))
    assert rows[0]["refunded"] == 0.0
    rows, warnings = parse(order("O11", [txn("6", "6")], refunds=refund("50.00", cur="GBP")))
    assert rows[0]["refunded"] == 0.0 and any("currency" in w for w in warnings)


def test_single_line_order_falls_back_to_order_shipping_and_flags_mismatch():
    rows, warnings = parse(order("O2", [txn("333", ship=None, tax="0.0", line_id=False)],
                                 total="150.00"))
    assert rows[0]["order_line_item_id"] == "333-10"
    assert rows[0]["shipping"] == 12.4 and rows[0]["total"] == 112.4
    assert len(warnings) == 1 and "112.40 != order total 150.00" in warnings[0]


@pytest.mark.parametrize("status,cancel,expected", [
    ("Completed", "NotApplicable", False),
    ("Completed", "CancelRequested", False),  # not cancelled until it completes
    ("Cancelled", "NotApplicable", True),
    ("Completed", "CancelComplete", True),
])
def test_cancelled(status, cancel, expected):
    rows, _ = parse(order("O3", [txn("444")], status=status, cancel=cancel))
    assert rows[0]["cancelled"] is expected


class FakeResp:
    def __init__(self, content=b"", status=200, body=None):
        self.content, self.status_code, self._body = content, status, body
        self.text = json.dumps(body) if body is not None else ""

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))


@pytest.fixture
def ebay(monkeypatch):
    """GetOrders returns two pages; server POSTs are captured."""
    monkeypatch.setattr(eut, "get_user_access_token", lambda: "tok")
    pages = [response([order("O1", [txn("111")])], more=True),
             response([order("O2", [txn("222")])])]
    posted = []

    def fake_post(url, data=None, json=None, **kw):
        if "api.ebay.com" in url:
            return FakeResp(pages.pop(0))
        posted.append((url, json))
        if json is None:  # a heartbeat ping
            return FakeResp()
        return FakeResp(body={"inserted": len(json["purchases"]), "updated": 0})

    monkeypatch.setattr(requests, "post", fake_post)
    return posted


def test_dry_run_posts_nothing(ebay, monkeypatch, capsys):
    monkeypatch.setenv("COMICS_SERVER_URL", "http://srv")
    assert pp.main([]) == 0
    assert ebay == []
    out = capsys.readouterr().out
    assert "DRY RUN: 2 purchase line item(s)" in out and "spend 2026-11: $226.00" in out


def test_apply_posts_every_page(ebay, monkeypatch):
    monkeypatch.setenv("COMICS_SERVER_URL", "http://srv/")
    assert pp.main(["--apply"]) == 0
    assert [u for u, _ in ebay] == ["http://srv/api/comics/purchases",
                                    "http://srv/api/heartbeat/ebay-purchases-pull"]
    assert [p["item_id"] for p in ebay[0][1]["purchases"]] == ["111", "222"]


def test_failed_apply_pings_failure_heartbeat(ebay, monkeypatch):
    monkeypatch.setenv("COMICS_SERVER_URL", "http://srv")
    real = requests.post

    def post(url, **kw):
        if url.endswith("/api/comics/purchases"):
            return FakeResp(status=500)
        return real(url, **kw)

    monkeypatch.setattr(requests, "post", post)
    assert pp.main(["--apply"]) == 1
    assert [u for u, _ in ebay] == ["http://srv/api/heartbeat/ebay-purchases-pull/failure"]


def test_apply_without_server_is_usage_error(ebay, monkeypatch):
    monkeypatch.delenv("COMICS_SERVER_URL", raising=False)
    monkeypatch.delenv("GIXEN_SERVER_URL", raising=False)
    assert pp.main(["--apply"]) == 2
    assert pp.main(["--days", "91"]) == 2


def test_ebay_failure_exits_1_and_posts_nothing(monkeypatch):
    monkeypatch.setattr(eut, "get_user_access_token", lambda: "tok")
    monkeypatch.setattr(requests, "post", lambda *a, **k: FakeResp(b"", 503))
    monkeypatch.setenv("COMICS_SERVER_URL", "http://srv")
    assert pp.main(["--apply"]) == 1


def test_offer_log_add_and_update(monkeypatch, capsys):
    monkeypatch.setenv("COMICS_SERVER_URL", "http://srv")
    calls = []
    row = {"id": 1, "offered_at": "2026-11-17T00:00:00", "item_id": "123456789012",
           "ask": 300.0, "offer": 240.0, "counteroffer": None, "final_price": None,
           "outcome": "pending", "seller": "s1"}

    def fake(method):
        def _call(url, json=None, **kw):
            calls.append((method, url, json))
            return FakeResp(status=201 if method == "post" else 200, body=row)
        return _call

    monkeypatch.setattr(requests, "post", fake("post"))
    monkeypatch.setattr(requests, "patch", fake("patch"))
    assert offer_log.main(["add", "123456789012", "--offer", "240", "--ask", "300",
                           "--seller", "s1"]) == 0
    assert calls[-1] == ("post", "http://srv/api/comics/offers",
                         {"item_id": "123456789012", "offer": 240.0, "ask": 300.0, "seller": "s1"})
    assert offer_log.main(["update", "1", "--counter", "270", "--outcome", "countered"]) == 0
    assert calls[-1] == ("patch", "http://srv/api/comics/offers/1",
                         {"counteroffer": 270.0, "outcome": "countered"})
    assert offer_log.main(["update", "1"]) == 2
    assert "#1" in capsys.readouterr().out


def test_single_line_with_no_shipping_anywhere_is_flagged():
    xml = order("O7", [txn("1", "1", ship=None)]).replace(
        "<ShippingServiceSelected><ShippingServiceCost>12.40</ShippingServiceCost>"
        "</ShippingServiceSelected>", "")
    rows, warnings = parse(xml)
    assert rows[0]["shipping"] is None and rows[0]["shipping_estimated"]
    assert any("no shipping cost" in w for w in warnings)


def test_refund_over_line_totals_warns_and_caps():
    rows, warnings = parse(order("O8", [txn("1", "1")], refunds=refund("999.00")))
    assert rows[0]["refunded"] == rows[0]["total"]
    assert any("exceeds the line totals" in w for w in warnings)


def test_pull_windows_on_mod_time_but_shipped_submit_keeps_create_time(monkeypatch):
    import shipped_submit as ss
    bodies = []

    def fake_post(url, data=None, **kw):
        bodies.append(data.decode())
        return FakeResp(response([]))

    monkeypatch.setattr(requests, "post", fake_post)
    pp.fetch_purchases("tok")
    ss.fetch_transactions("tok")
    assert "<ModTimeFrom>" in bodies[0] and "CreateTimeFrom" not in bodies[0]
    assert "<CreateTimeFrom>" in bodies[1] and "ModTimeFrom" not in bodies[1]


def test_late_refund_on_an_older_order_flows_through_parse_and_post(monkeypatch):
    """A re-pull returns the same order (created before the window) with a
    refund now settled; the posted row keeps its id and carries the refund."""
    monkeypatch.setattr(requests, "post", lambda url, data=None, **kw: FakeResp(response(
        [order("O9", [txn("9")], refunds=refund("50.00"))])))
    rows, _ = pp.fetch_purchases("tok")
    assert rows[0]["order_line_item_id"] == "9-10"
    assert rows[0]["purchased_at"] == "2026-11-03T09:14:26.000Z"
    assert rows[0]["refunded"] == 50.0
