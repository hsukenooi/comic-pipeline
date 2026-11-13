"""Tests for ezship_bound (BUI-1376). Fixtures are the real emails, scrubbed."""

import base64
import copy
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

import ezship_bound as eb
import shipped_orders as so

FIX = Path(__file__).parent / "fixtures" / "ezship_bound"
NOW = datetime(2026, 11, 12, tzinfo=timezone.utc)
HART_TRACKING = "92001902673388000127757663"


def load(name):
    return json.loads((FIX / name).read_text())


def make(mid, sender, subject, text, date="Wed, 11 Nov 2026 05:48:14 +0000"):
    return {
        "id": mid,
        "payload": {
            "mimeType": "multipart/alternative",
            "headers": [{"name": "From", "value": sender},
                        {"name": "Subject", "value": subject},
                        {"name": "Date", "value": date}],
            "body": {},
            "parts": [{"mimeType": "text/plain", "body": {
                "data": base64.urlsafe_b64encode(text.encode()).decode()}}],
        },
    }


def with_id(msg, mid):
    return {**msg, "id": mid}


@pytest.fixture
def hart_order():
    return load("hart_order.json")


@pytest.fixture
def found_order():
    return load("foundobjects_order.json")


@pytest.fixture
def near_order():
    return load("nearearth_order.json")


@pytest.fixture
def search(monkeypatch):
    """Route per-order Gmail searches to a dict keyed by a substring of the query."""
    store, queries = {}, []

    def fake_fetch(query, max_results, max_workers=8):
        queries.append(query)
        for key, msgs in store.items():
            if key in query:
                return so.Fetched(list(msgs), False, len(msgs))
        return so.Fetched([], False, 0)

    monkeypatch.setattr(eb, "fetch_messages", fake_fetch)
    handle = type("Search", (), {})()
    handle.map = store
    handle.queries = queries
    return handle


def report(messages, ledger=frozenset(), **kw):
    kw.setdefault("days", 90)
    return eb.build_report(messages, set(ledger), order_days=180, now=NOW, **kw)


def row_for(rep, number):
    return next(r for r in rep["orders"] if r["order_number"] == number)


def test_hart_order_with_shipping_email(hart_order, search):
    search.map["75151"] = [hart_order, load("hart_shipping.json")]
    rep = report([hart_order])
    row = row_for(rep, "75151")
    assert row["status"] == eb.READY
    prop = row["proposal"]
    assert prop["tracking"] == HART_TRACKING
    assert prop["carrier"] == "USPS"
    assert (prop["category"], prop["category_id"]) == ("Cables", "1013")
    assert prop["declared_value"] == "$54.50"
    assert prop["declared_value_cents"] == 5450
    assert prop["remark"] == "Hart Audio Cables 75151"
    assert 'from:orders@hartaudiocables.com' in search.queries[0]
    assert "newer_than:180d" in search.queries[0]


def test_found_objects_waiting_for_tracking(found_order, search):
    rep = report([found_order])
    row = row_for(rep, "1702")
    assert row["status"] == eb.WAITING
    assert row["trackings"] == []
    # The proposal fields are still derivable from the parsed order.
    order = eb.parse_order(found_order)
    cid, cname, _ = eb.pick_category([i["name"] for i in order["items"]])
    assert (cid, cname) == ("1063", "Books")
    assert eb.declared_value_cents(order["total_cents"], 200) == 3444
    assert len(order["items"]) == 2
    assert order["items"][1]["name"].endswith("PRESALE 11/04")


def test_near_earth_waiting_for_tracking(near_order, search):
    rep = report([near_order])
    row = row_for(rep, "US-3108")
    assert row["status"] == eb.WAITING
    order = eb.parse_order(near_order)
    assert order["order_number"] == "US-3108"
    cid, cname, _ = eb.pick_category([i["name"] for i in order["items"]])
    assert (cid, cname) == ("875", "Socks")
    assert eb.declared_value_cents(order["total_cents"], 200) == 2550


def test_newsletter_with_portland_is_skipped(search):
    news = make("n1", "Image Comics <news@image.test>", "New releases",
                "Order #5 now\nImage Comics\nPortland, OR 97214\n")
    rep = report([news])
    assert rep["orders"] == [] and rep["unparsed"] == []
    assert rep["skipped_not_warehouse"] == 1


def test_non_shopify_order_is_unparsed(search):
    odd = make("o1", "Some Shop <hi@shop.test>", "Order #55 received",
               "Order #55\nShip to 12826 NE Airport Way Portland OR\nTotal $5.00 USD\n")
    rep = report([odd])
    assert rep["orders"] == []
    assert rep["unparsed"][0]["status"] == eb.UNPARSED
    assert "Shopify" in rep["unparsed"][0]["reason"]


def test_html_only_order_is_unparsed(search):
    html = "<p>Order #9</p><p>12826 Northeast Airport Way</p>"
    msg = make("h1", "Shop <a@shop.test>", "Order #9 confirmed", html)
    msg["payload"]["parts"][0]["mimeType"] = "text/html"
    rep = report([msg])
    assert rep["unparsed"][0]["reason"] == "no text/plain part"


def test_two_tracking_numbers_hold(hart_order, search):
    second = make("s2", "Hart Audio Cables <orders@hartaudiocables.com>",
                  "A shipment from order #75151 is on the way",
                  "Order #75151\nUPS tracking number: 1Z999AA10123456784\n")
    search.map["75151"] = [load("hart_shipping.json"), second]
    row = row_for(report([hart_order]), "75151")
    assert row["status"] == eb.HOLD
    assert set(row["trackings"]) == {HART_TRACKING, "1Z999AA10123456784"}
    assert "proposal" not in row


def test_other_store_same_digits_does_not_link(found_order, search):
    other = make("x1", "Other Store <info@foundobjects.store>",
                 "Your receipt", "Paid $1702.00 for stuff.\nTracking number: "
                 + HART_TRACKING + "\n")
    search.map["1702"] = [other]
    row = row_for(report([found_order]), "1702")
    assert row["status"] == eb.WAITING


def test_order_token_must_be_whole(found_order, search):
    longer = make("x2", "found / objects <info@foundobjects.store>",
                  "A shipment from order #17021", "Order #17021\nUSPS tracking number: "
                  + HART_TRACKING + "\n")
    search.map["1702"] = [longer]
    assert row_for(report([found_order]), "1702")["status"] == eb.WAITING


def test_orphan_shipment(search):
    ship = load("hart_shipping.json")
    ship["payload"]["parts"][0]["body"]["data"] = base64.urlsafe_b64encode(
        ("Order #75151\n12826 Northeast Airport Way\nUSPS tracking number: "
         + HART_TRACKING + "\n").encode()).decode()
    ship = with_id(ship, "ship1")
    # Dated inside the window.
    ship["internalDate"] = str(int(datetime(2026, 11, 1, tzinfo=timezone.utc).timestamp() * 1000))
    rep = report([ship])
    assert rep["orders"] == []
    assert rep["orphans"][0]["status"] == eb.ORPHAN
    assert rep["orphans"][0]["trackings"] == [HART_TRACKING]


def test_sock_and_comic_get_no_category(search):
    cid, cname, why = eb.pick_category(["Wool Socks", "Batman #1 comic"])
    assert cid is None and "several categories" in why
    cid, _n, why = eb.pick_category(["Wool Socks", "Mystery Gadget"])
    assert cid is None and "Mystery Gadget" in why


def test_notebook_is_not_a_book():
    assert eb.pick_category(["Spiral notebook"])[0] is None


def test_sgd_total_is_unparsed(near_order, search):
    text = eb.plain_text_part(near_order["payload"]).replace(
        "Total\n\n$51.00 USD", "Total\n\n$51.00 SGD")
    sgd = make("sg", "Near Earth US <store+1@t.shopifyemail.com>",
               "Order #US-3108 confirmed", text)
    rep = report([sgd])
    assert rep["unparsed"][0]["reason"] == "total currency is SGD, not USD"
    missing = make("sg2", "Near Earth US <store+1@t.shopifyemail.com>",
                   "Order #US-3108 confirmed",
                   text.replace("Total\n\n$51.00 SGD", "Total\n\n$51.00"))
    assert "missing" in report([missing])["unparsed"][0]["reason"]


def test_total_ignores_subtotal_and_savings():
    text = ("Order #7\n\nOrder summary\n-------------\n\nBook × 1\n\n$10.00\n\n"
            "Subtotal\n\n$10.00\n\nTotal savings\n\n$1.00 USD\n\nShipping\n\n$2.00\n\n"
            "Total\n\n$11.00 USD\n12826 NE Airport Way")
    msg = make("t", "Shop <a@shop.test>", "Order #7 confirmed", text)
    assert eb.parse_order(msg)["total_cents"] == 1100


def test_ledger_marks_already_submitted(hart_order, search):
    search.map["75151"] = [load("hart_shipping.json")]
    rep = report([hart_order], ledger={HART_TRACKING})
    row = row_for(rep, "75151")
    assert row["status"] == eb.ALREADY
    assert "proposal" not in row


def test_parcel_shared_by_two_orders_is_held(hart_order, search):
    twin = with_id(copy.deepcopy(hart_order), "twin")
    twin["payload"]["headers"] = [
        {"name": "From", "value": "Hart Audio Cables <orders@hartaudiocables.com>"},
        {"name": "Subject", "value": "Order #75152 confirmed"},
        {"name": "Date", "value": "Tue, 25 Aug 2026 09:00:00 +0000"}]
    text = eb.plain_text_part(hart_order["payload"]).replace("#75151", "#75152")
    twin["payload"]["parts"][0]["body"]["data"] = base64.urlsafe_b64encode(
        text.encode()).decode()
    ship = make("sh", "Hart Audio Cables <orders@hartaudiocables.com>",
                "A shipment is on the way",
                "Order #75151\nOrder #75152\nUSPS tracking number: " + HART_TRACKING + "\n")
    search.map["75151"] = [ship]
    search.map["75152"] = [ship]
    rep = report([hart_order, twin])
    assert {r["status"] for r in rep["orders"]} == {eb.HOLD}
    assert all("proposal" not in r for r in rep["orders"])


def test_unsupported_carrier(hart_order, search):
    spx = make("sp", "Hart Audio Cables <orders@hartaudiocables.com>",
               "A shipment from order #75151 is on the way",
               "Order #75151\nTracking number: SPXSG1234567890\n")
    search.map["75151"] = [spx]
    row = row_for(report([hart_order]), "75151")
    assert row["status"] == eb.UNSUPPORTED
    assert "proposal" not in row


def test_declared_value_rounds_half_up_and_floors_to_minimum():
    assert eb.declared_value_cents(1001, 200) == 501  # 500.5 rounds up
    assert eb.declared_value_cents(100, 200) == 200   # raised to the category minimum


def test_every_rule_id_is_in_the_saved_category_list():
    cats = eb.load_categories()
    for cid, name, _pat in eb.CATEGORY_RULES:
        assert cats[cid]["name"] == name
    assert cats["1063"]["declared_min"] == 200


def test_category_file_is_in_the_wheel_include():
    pyproject = (Path(__file__).parent.parent / "pyproject.toml").read_text()
    assert "src/ezship_categories.json" in pyproject
    assert 'ezship-bound = "ezship_bound:main"' in pyproject


def test_truncated_order_search_exits_2(monkeypatch, hart_order, tmp_path, capsys):
    def fake_fetch(query, max_results, max_workers=8):
        if "12826" not in query:
            return so.Fetched([], True, 25)
        return so.Fetched([hart_order], False, 1)

    monkeypatch.setattr(eb, "fetch_messages", fake_fetch)
    assert eb.main(["--ledger", str(tmp_path / "none.json")]) == 2


def test_gmail_failure_exits_1(monkeypatch, tmp_path, capsys):
    def boom(*a, **k):
        raise so.ShippedOrdersError("gws down")

    monkeypatch.setattr(eb, "fetch_messages", boom)
    assert eb.main(["--ledger", str(tmp_path / "none.json")]) == 1


def test_order_search_failure_exits_1(monkeypatch, hart_order, tmp_path):
    def fake_fetch(query, max_results, max_workers=8):
        if "12826" not in query:
            raise so.ShippedOrdersError("denied")
        return so.Fetched([hart_order], False, 1)

    monkeypatch.setattr(eb, "fetch_messages", fake_fetch)
    assert eb.main(["--ledger", str(tmp_path / "none.json")]) == 1


def test_unparsed_exits_3_and_json_payload(monkeypatch, tmp_path, capsys):
    odd = make("o1", "Some Shop <hi@shop.test>", "Order #55 received",
               "Order #55\n12826 NE Airport Way\n")
    monkeypatch.setattr(eb, "fetch_messages",
                        lambda q, m, w=8: so.Fetched([odd], False, 1))
    assert eb.main(["--json", "--ledger", str(tmp_path / "none.json")]) == 3
    payload = json.loads(capsys.readouterr().out)
    assert payload["counts"]["unparsed"] == 1


def test_main_uses_both_windows(monkeypatch, tmp_path):
    seen = []

    def fake_fetch(q, m, w=8):
        seen.append(q)
        return so.Fetched([], False, 0)

    monkeypatch.setattr(eb, "fetch_messages", fake_fetch)
    assert eb.main(["--days", "90", "--ledger", str(tmp_path / "n.json")]) == 0
    assert seen[0].startswith("newer_than:180d ")
    assert '"12826"' in seen[0] and "-from:ezbuy.sg" in seen[0]


@pytest.mark.parametrize("subject", ["Re: Order #5 confirmed", "FWD: Order #5 confirmed",
                                     "fw: Order #5 confirmed"])
def test_reply_and_forward_mail_is_skipped(subject, search):
    msg = make("r1", "Shop <a@shop.test>", subject,
               "Order #5\n12826 NE Airport Way\n")
    rep = report([msg])
    assert rep["unparsed"] == [] and rep["orders"] == []


def test_sent_label_mail_is_skipped(search):
    msg = make("r2", "Me <me@example.test>", "Order #5 confirmed",
               "Order #5\n12826 NE Airport Way\n")
    msg["labelIds"] = ["SENT"]
    assert report([msg])["unparsed"] == []


def test_shipping_mail_without_tracking_is_not_waiting(hart_order, search):
    vague = make("v1", "Hart Audio Cables <orders@hartaudiocables.com>",
                 "A shipment from order #75151 is on the way",
                 "Order #75151\nYour parcel is on its way. Track it with the carrier.\n")
    search.map["75151"] = [vague]
    row = row_for(report([hart_order]), "75151")
    assert row["status"] == eb.SHIPPED_NO_TRACKING
    assert "proposal" not in row


def test_shipped_no_tracking_exits_3(monkeypatch, hart_order, tmp_path):
    vague = make("v1", "Hart Audio Cables <orders@hartaudiocables.com>",
                 "A shipment from order #75151 is on the way", "Order #75151\nOn its way.\n")

    def fake_fetch(query, max_results, max_workers=8):
        if "12826" not in query:
            return so.Fetched([vague], False, 1)
        return so.Fetched([hart_order], False, 1)

    monkeypatch.setattr(eb, "fetch_messages", fake_fetch)
    assert eb.main(["--ledger", str(tmp_path / "none.json")]) == 3


@pytest.mark.parametrize("line", ["order #123", "ORDER #123", "Order No. 123",
                                  "Order Number: 123"])
def test_order_number_spellings_are_parsed_as_orders(line, search):
    msg = make("p1", "Shop <a@shop.test>", "Receipt",
               line + "\n12826 NE Airport Way\n")
    rep = report([msg])
    assert rep["skipped_not_warehouse"] == 0
    assert rep["unparsed"][0]["order_number"] == "123"


def test_warehouse_mail_without_order_number_is_unparsed(search):
    msg = make("p2", "Shop <a@shop.test>", "Hello", "Ship to 12826 NE Airport Way\n")
    rep = report([msg])
    assert rep["skipped_not_warehouse"] == 0
    assert rep["unparsed"][0]["reason"].endswith("no order number")


def test_ezship_mail_is_still_skipped(search):
    msg = make("p3", "EZShip <noreply@ezbuy.sg>", "Hello", "12826 NE Airport Way\n")
    assert report([msg])["unparsed"] == []


def test_item_number_is_not_an_order_number():
    assert eb._ORDER_NO.search("Batman #2 comic") is None


@pytest.mark.parametrize("name", ["Comic bags & boards", "Batman T-shirt",
                                  "Comic book box", "Batman hoodie"])
def test_supplies_and_apparel_are_not_books(name):
    assert eb.pick_category([name])[0] is None


def test_old_mail_without_order_number_is_skipped(search):
    old = make("p9", "Shop <a@shop.test>", "Hello", "Ship to 12826 NE Airport Way\n",
               date="Mon, 01 Jun 2026 00:00:00 +0000")
    old["internalDate"] = str(int(datetime(2026, 6, 1, tzinfo=timezone.utc).timestamp() * 1000))
    rep = report([old])
    assert rep["unparsed"] == [] and rep["skipped_not_warehouse"] == 1


def test_order_shipped_before_the_window_is_named(hart_order, search):
    search.map["75151"] = [load("hart_shipping.json")]
    rep = report([hart_order], ledger={HART_TRACKING}, days=45)
    assert rep["orders"] == []
    assert rep["outside_window"] == [{
        "merchant": "Hart Audio Cables", "order_number": "75151",
        "last_email": "2026-08-26", "trackings": [HART_TRACKING],
        "in_ledger": [HART_TRACKING]}]
    text = eb.render_table(rep)
    assert "Shipped before the --days window" in text
    assert (f"Hart Audio Cables #75151  last email 2026-08-26  "
            f"{HART_TRACKING} (in CLI ledger)") in text
