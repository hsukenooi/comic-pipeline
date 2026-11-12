"""Tests for ezship-bound (BUI-1376).

The fixtures in tests/fixtures/ezship_bound/ are real Shopify emails with the
billing address, card digits, buyer name, and order-page links replaced. A
small fake Gmail stands in for `gws`: the address search returns every message
that prints the warehouse street, and a `from:<sender> "<number>"` search
returns that sender's messages holding the number anywhere, which is as loose
as Gmail's own matching once it drops the `#`.
"""
from __future__ import annotations

import base64
import json
import re
import tomllib
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

import ezship_bound as eb
from shipped_orders import Fetched, ShippedOrdersError, decode_body, headers_of

FIXTURES = Path(__file__).parent / "fixtures" / "ezship_bound"
NOW = datetime(2026, 11, 12, tzinfo=timezone.utc)
CATEGORIES = eb.load_categories()

HART_TRACKING = "92001902673388000127757663"
FOUND_TRACKING = "92001902673388001129785524"
BIGTIME_TRACKING = "92346902673388000021086519"


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text())


def plain_of(message: dict) -> str:
    return base64.urlsafe_b64decode(message["payload"]["parts"][0]["body"]["data"]).decode()


def message(msg_id, from_header, subject, plain, internal_date="1794268800000", html=True):
    enc = lambda s: base64.urlsafe_b64encode(s.encode()).decode()  # noqa: E731
    parts = [{"mimeType": "text/plain", "body": {"data": enc(plain)}}]
    if html:
        parts.append({"mimeType": "text/html", "body": {"data": enc(f"<pre>{plain}</pre>")}})
    return {
        "id": msg_id, "threadId": msg_id, "internalDate": internal_date,
        "payload": {"mimeType": "multipart/alternative", "body": {}, "parts": parts,
                    "headers": [{"name": "From", "value": from_header},
                                {"name": "Subject", "value": subject}]},
    }


def variant(base: dict, msg_id: str, *, plain=None, subject=None, from_header=None):
    hdrs = headers_of(base)
    return message(msg_id, from_header or hdrs["From"], subject or hdrs["Subject"],
                   plain if plain is not None else plain_of(base), base["internalDate"])


def fake_gmail(messages, truncate=False, fail=False):
    calls = []

    def fetch(query, max_results):
        calls.append(query)
        if fail:
            raise ShippedOrdersError("gws failed")
        link = re.search(r'from:(\S+) "([^"]+)"', query)
        hits = []
        for m in messages:
            hdrs = headers_of(m)
            text = hdrs["Subject"] + "\n" + decode_body(m["payload"])
            if link:
                if eb.sender_address(hdrs["From"]) == link.group(1) and link.group(2) in text:
                    hits.append(m)
            elif "12826" in text or "Airport Way" in text:
                hits.append(m)
        return Fetched(hits, truncate, len(hits))

    fetch.calls = calls
    return fetch


def run(messages, *, days=90, order_days=180, ledger=frozenset(), **gmail):
    return eb.collect(order_days, days, 500, set(ledger), CATEGORIES,
                      fetch=fake_gmail(messages, **gmail), now=NOW)


def only_row(payload):
    assert len(payload["rows"]) == 1, payload["rows"]
    return payload["rows"][0]


# ─── The three real orders ────────────────────────────────────────────────────

def test_hart_order_with_shipment_is_ready():
    row = only_row(run([fixture("hart_confirmation"), fixture("hart_shipment")]))
    assert row["status"] == "ready"
    assert row["tracking"] == [{"tracking_number": HART_TRACKING, "carrier": "USPS"}]
    assert row["ezship_carrier"] == "USPS"
    assert row["category"] == {"id": "1013", "name": "Cables"}
    assert row["declared_value_cents"] == 5450
    assert row["remark"] == "Hart Audio Cables 75151"
    assert row["product"] == "RPL-HC-15: Dual Lemo 2-pin Cable for Focal Utopia"
    assert "--category-id 1013" in row["command"]
    assert "-d 5450" in row["command"]


def test_found_objects_without_shipment_waits_for_tracking():
    row = only_row(run([fixture("foundobjects_confirmation")]))
    assert row["status"] == "waiting_for_tracking"
    assert row["category"] == {"id": "1063", "name": "Books"}
    assert row["declared_value_cents"] == 3444
    assert row["total"] == "68.88"
    # The second item name wraps across two lines in the email.
    assert row["items"][1]["name"] == (
        "SIGNED CLAY MANN ABSOLUTE BATMAN #25 CVR L FOIL - PRESALE 11/04")
    assert "command" not in row


def test_found_objects_with_shipment_is_ready():
    row = only_row(run([fixture("foundobjects_confirmation"), fixture("foundobjects_shipment")]))
    assert row["status"] == "ready"
    assert row["tracking"][0]["tracking_number"] == FOUND_TRACKING


def test_near_earth_without_shipment_waits_for_tracking():
    row = only_row(run([fixture("nearearth_confirmation")]))
    assert row["status"] == "waiting_for_tracking"
    assert row["order_number"] == "US-3108"
    assert row["category"] == {"id": "875", "name": "Socks"}
    assert row["declared_value_cents"] == 2550
    assert row["remark"] == "Near Earth US US-3108"


def test_near_earth_with_shipment_is_ready():
    row = only_row(run([fixture("nearearth_confirmation"), fixture("nearearth_shipment")]))
    assert row["status"] == "ready"
    assert row["tracking"][0] == {"tracking_number": "9200190327410419944933", "carrier": "USPS"}


# ─── What the report must not do ──────────────────────────────────────────────

def test_newsletter_with_a_portland_address_is_not_an_order():
    newsletter = message("img", "Image Comics <news@imagecomics.com>", "New this week",
                         "Order #5 is out now!\nImage Comics\n2701 NW Vaughn St\nPortland, OR 97210")
    payload = run([newsletter])
    assert payload["rows"] == [] and payload["skipped"] == []


def test_non_shopify_order_email_is_unparsed():
    plain = ("Barnes & Noble Order Confirmation #8001180496\n"
             "Ship to: 12826 NE Airport Way, Portland OR 97230\nItem: A Book  $20.00\n")
    row = only_row(run([message("bn", "B&N <noreply@bn.test>",
                                "Barnes & Noble Order Confirmation #8001180496", plain)]))
    assert row["status"] == "unparsed"
    assert row["order_number"] == "8001180496"
    assert "Order summary" in row["reason"]


def test_html_only_order_email_is_unparsed():
    conf = fixture("hart_confirmation")
    conf["payload"]["parts"] = conf["payload"]["parts"][1:]
    row = only_row(run([conf]))
    assert row["status"] == "unparsed"
    assert row["reason"] == "no text/plain part"


def test_two_tracking_numbers_give_hold_with_no_value():
    second = variant(fixture("hart_shipment"), "hart-2",
                     plain=plain_of(fixture("hart_shipment")).replace(HART_TRACKING, FOUND_TRACKING))
    row = only_row(run([fixture("hart_confirmation"), fixture("hart_shipment"), second]))
    assert row["status"] == "hold"
    assert {t["tracking_number"] for t in row["tracking"]} == {HART_TRACKING, FOUND_TRACKING}
    assert "declared_value_cents" not in row
    assert "command" not in row


@pytest.mark.parametrize("from_header, subject_order", [
    # Another Shopify store: same digits, different full sender address.
    ("Other Store <store+999@t.shopifyemail.com>", "#75151"),
    # Same store, a different order whose number starts with the same digits.
    ("Hart Audio Cables <orders@hartaudiocables.com>", "#751510"),
])
def test_shipping_email_for_another_order_does_not_link(from_header, subject_order):
    ship = fixture("hart_shipment")
    plain = plain_of(ship).replace("Order #75151", f"Order {subject_order}")
    other = variant(ship, "other", plain=plain, from_header=from_header,
                    subject=f"A shipment from order {subject_order} is on the way")
    row = only_row(run([fixture("hart_confirmation"), other]))
    assert row["status"] == "waiting_for_tracking"
    assert row["tracking"] == []


def test_shipping_email_with_address_and_no_order_is_an_orphan():
    plain = ("Your order is on the way\nOrder #9999\n"
             "USPS tracking number: " + FOUND_TRACKING + "\n"
             "Shipping to 12826 NE Airport Way, Portland OR 97230\n")
    orphan = message("orph", "Some Shop <ship@some-shop.test>",
                     "A shipment from order #9999 is on the way", plain)
    row = only_row(run([orphan]))
    assert row["status"] == "orphan_shipment"
    assert row["tracking"] == [{"tracking_number": FOUND_TRACKING, "carrier": "USPS"}]
    assert row["merchant"] == "Some Shop"


def test_sock_plus_comic_gets_no_category():
    assert eb.propose_category(["Running Socks", "Absolute Batman #1"], CATEGORIES) is None
    plain = plain_of(fixture("hart_confirmation")).replace(
        "RPL-HC-15: Dual Lemo 2-pin Cable for Focal Utopia", "Batman Socks")
    conf = variant(fixture("hart_confirmation"), "mixed", plain=plain)
    row = only_row(run([conf, fixture("hart_shipment")]))
    assert row["status"] == "needs_category"
    assert "category" not in row
    assert "command" not in row


def test_sgd_total_is_unparsed():
    plain = plain_of(fixture("foundobjects_confirmation")).replace("$68.88 USD", "$68.88 SGD")
    row = only_row(run([variant(fixture("foundobjects_confirmation"), "sgd", plain=plain)]))
    assert row["status"] == "unparsed"
    assert "SGD" in row["reason"]


def test_ledger_hit_is_already_submitted():
    row = only_row(run([fixture("bigtime_confirmation"), fixture("bigtime_shipment")],
                       days=180, ledger={BIGTIME_TRACKING}))
    assert row["status"] == "already_submitted"
    assert len(row["items"]) == 4
    assert row["category"]["id"] == "1063"


def test_order_shipped_before_the_window_is_named_not_counted():
    payload = run([fixture("bigtime_confirmation"), fixture("bigtime_shipment")], days=45,
                  ledger={BIGTIME_TRACKING})
    assert payload["rows"] == []
    assert payload["shipped_before_window"] == [{
        "merchant": "BIG TIME COLLECTIBLES",
        "order_number": "147708",
        "last_email_at": "2026-07-01",
        "tracking": [{"tracking_number": BIGTIME_TRACKING, "carrier": "USPS",
                      "in_ledger": True}],
    }]
    text = eb.render_text(payload)
    assert "Shipped before the 45-day window" in text
    assert f"BIG TIME COLLECTIBLES #147708 | last email 2026-07-01 | USPS {BIGTIME_TRACKING} (in CLI ledger)" in text


def test_own_replies_and_ezship_mail_are_excluded_from_the_query():
    fetch = fake_gmail([fixture("hart_confirmation"), fixture("hart_shipment")])
    eb.collect(180, 90, 500, set(), CATEGORIES, fetch=fetch, now=NOW)
    for query in fetch.calls:
        for term in ("-from:me", "-from:ebay.com", "-from:ezbuy.com", "newer_than:180d"):
            assert term in query
    assert 'from:orders@hartaudiocables.com "75151"' in fetch.calls[1]


# ─── Units ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("total, minimum, cents", [
    ("109.00", 200, 5450),
    ("68.88", 200, 3444),
    ("0.05", 0, 3),      # 2.5 cents rounds half up
    ("2.00", 200, 200),  # raised to the category minimum
])
def test_declared_value(total, minimum, cents):
    assert eb.declared_cents(Decimal(total), minimum) == cents


def test_whole_word_category_match():
    assert eb.propose_category(["Leather Notebook"], CATEGORIES) is None
    assert eb.propose_category(["Some Title 1:25 Ratio"], CATEGORIES) == ("1063", "Books")


def test_every_category_rule_exists_in_the_saved_list():
    for cat_id, _pattern in eb.CATEGORY_RULES:
        assert cat_id in CATEGORIES
    assert CATEGORIES["1063"]["name"] == "Books"


def test_category_list_ships_in_the_wheel():
    pyproject = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())
    assert "src/ezship_categories.json" in pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]["include"]
    assert pyproject["project"]["scripts"]["ezship-bound"] == "ezship_bound:main"


# ─── Exit codes ───────────────────────────────────────────────────────────────

def _main_with(monkeypatch, tmp_path, fetch, capsys):
    monkeypatch.setattr(eb, "fetch_messages", fetch)
    code = eb.main(["--ledger", str(tmp_path / "ledger.json"), "--json", "--days", "3650"])
    return code, capsys.readouterr()


def test_main_exit_codes(monkeypatch, tmp_path, capsys):
    good = [fixture("hart_confirmation"), fixture("hart_shipment")]
    code, out = _main_with(monkeypatch, tmp_path, fake_gmail(good), capsys)
    assert code == 0
    assert json.loads(out.out)["rows"][0]["status"] == "ready"

    code, _ = _main_with(monkeypatch, tmp_path, fake_gmail(good, truncate=True), capsys)
    assert code == 2

    code, out = _main_with(monkeypatch, tmp_path, fake_gmail(good, fail=True), capsys)
    assert code == 1
    assert "gws failed" in out.err

    sgd = variant(fixture("foundobjects_confirmation"), "sgd",
                  plain=plain_of(fixture("foundobjects_confirmation")).replace("USD", "SGD"))
    code, _ = _main_with(monkeypatch, tmp_path, fake_gmail([sgd]), capsys)
    assert code == 3


def test_text_report_names_each_field():
    text = eb.render_text(run([fixture("hart_confirmation"), fixture("hart_shipment")]))
    assert "[ready] Hart Audio Cables #75151" in text
    assert "Cables (1013)" in text and "$54.50" in text
    assert "CLI submissions only" in text
