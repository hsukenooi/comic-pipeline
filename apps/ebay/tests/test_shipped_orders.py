"""Tests for ebay-shipped (BUI-807), the tracking-number source.

Everything here runs against the sanitized fixtures in
tests/fixtures/shipped_orders/ — captured shapes of the two real eBay mails,
never live Gmail. See that directory's README for what was redacted.

The load-bearing property under test is negative: the tool must never invent a
tracking number. A message it cannot read confidently has to surface as
`unparsed`, not vanish and not be guessed.
"""

import base64
import json
import re
from pathlib import Path

import pytest

import shipped_orders

FIXTURES = Path(__file__).parent / "fixtures" / "shipped_orders"

# The fixtures' invented numbers, kept here so a fixture edit that changes them
# fails loudly instead of silently weakening an assertion.
UPS_GOOD = "1Z99X99A11QQ123459"
UPS_BAD = "1Z99X99A11QQ123451"
USPS_GOOD = "9405511899223197428490"

# BUI-916's labelled-field fixtures.
LABEL_FEDEX = "770012340000"                    # generic_delivery_update_labeled
LABEL_USPS_HUMAN = "9400111111111111111114"     # human_seller_labeled
LABEL_USPS_MERCHANT = "92020190000000000000001237"  # merchant_shipping_confirmation
# An invented carrier no rule here will ever claim — used to pin the
# `unknown_carrier` path itself, independent of which real-world carriers
# happen to have a rule. merchant_unknown_carrier used to carry the Shopee
# SPX number below before BUI-967 gave SPX a shape rule; it was repointed at
# this value so the two cases (recognized vs. genuinely unmodelled) stay
# distinguishable.
LABEL_UNKNOWN = "QXPRESS1234567890123"          # merchant_unknown_carrier
LABEL_SPX = "SPXSG065899567827"                 # merchant_shopee_labeled (BUI-967)


def load(name):
    return json.loads((FIXTURES / f"{name}.json").read_text())


def _seller_message_with(message_id, tracking_numbers, seller="splitshop"):
    """Build a seller-shape message carrying N carrier tracking URLs.

    Synthesised rather than captured: a split order has not shown up in this
    account's mail yet, so there is no real payload to sanitise — but the
    behaviour has to be pinned before one does.
    """
    anchors = "".join(
        f'<a href="http://wwwapps.ups.com/WebTracking/track?track=yes'
        f'&amp;trackNums={tn}">Track My Package</a>'
        for tn in tracking_numbers
    )
    html = f"<html><body>{anchors}<p>Shipped Via</p><p>UPS</p></body></html>"
    return {
        "id": message_id,
        "threadId": message_id,
        "internalDate": "1755094955000",
        "payload": {
            "mimeType": "text/html",
            "headers": [
                {"name": "From", "value": f"eBay - {seller} <deadbeef@members.ebay.com>"},
                {"name": "Subject", "value": "Your order has shipped"},
            ],
            "body": {"data": base64.urlsafe_b64encode(html.encode()).decode()},
        },
    }


# ─── Check digits ─────────────────────────────────────────────────────────────

class TestUpsCheckDigit:
    def test_accepts_valid_number(self):
        assert shipped_orders.ups_check_digit_ok(UPS_GOOD)

    def test_rejects_wrong_check_digit(self):
        assert not shipped_orders.ups_check_digit_ok(UPS_BAD)

    @pytest.mark.parametrize("bad", [
        "1Z99X99A11QQ12345",     # one char short
        "1Z99X99A11QQ1234599",   # one char long
        "2Z99X99A11QQ123459",    # wrong prefix
        "1Z99X99A11QQ12345X",    # non-digit check position
        "",
    ])
    def test_rejects_malformed(self, bad):
        assert not shipped_orders.ups_check_digit_ok(bad)

    def test_catches_most_single_character_corruptions(self):
        """The check digit is a strong but deliberately-not-perfect guard.

        A weighted mod-10 sum cannot detect every substitution: at a
        double-weighted position, changing a character by 5 leaves the sum
        unchanged mod 10. Measured here at ~88%. That is fine, and the test
        pins the real number rather than a flattering claim — the primary
        guard is that the number is lifted verbatim out of the carrier's own
        URL, so there is no transcription step for a slip to enter through.
        """
        total = undetected = 0
        alphabet = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        for i in range(2, len(UPS_GOOD) - 1):
            for ch in alphabet:
                if ch == UPS_GOOD[i]:
                    continue
                total += 1
                if shipped_orders.ups_check_digit_ok(UPS_GOOD[:i] + ch + UPS_GOOD[i + 1:]):
                    undetected += 1
        assert total > 500
        assert (total - undetected) / total > 0.85

    def test_catches_every_adjacent_transposition(self):
        for i in range(2, len(UPS_GOOD) - 2):
            if UPS_GOOD[i] == UPS_GOOD[i + 1]:
                continue
            swapped = UPS_GOOD[:i] + UPS_GOOD[i + 1] + UPS_GOOD[i] + UPS_GOOD[i + 2:]
            assert not shipped_orders.ups_check_digit_ok(swapped)

    def test_truncated_number_never_validates(self):
        """The realistic extraction failure is a cut-short URL parameter."""
        for cut in range(1, 8):
            assert not shipped_orders.ups_check_digit_ok(UPS_GOOD[:-cut])


class TestUspsCheckDigit:
    def test_accepts_valid_number(self):
        assert shipped_orders.usps_check_digit_ok(USPS_GOOD)

    def test_rejects_wrong_check_digit(self):
        wrong = USPS_GOOD[:-1] + str((int(USPS_GOOD[-1]) + 1) % 10)
        assert not shipped_orders.usps_check_digit_ok(wrong)

    @pytest.mark.parametrize("bad", ["1234", "9" * 21, "abcdefghijklmnopqrstuv"])
    def test_rejects_wrong_length_or_alpha(self, bad):
        assert not shipped_orders.usps_check_digit_ok(bad)


# ─── Extraction ───────────────────────────────────────────────────────────────

class TestExtractTracking:
    def test_reads_number_from_ups_url(self):
        found, rejected = shipped_orders.extract_tracking(
            f'<a href="http://wwwapps.ups.com/WebTracking/track?track=yes'
            f'&amp;trackNums={UPS_GOOD}">Track</a>'
        )
        assert found == [("UPS", UPS_GOOD, True)]
        assert rejected == []

    def test_reads_number_from_usps_url(self):
        found, _ = shipped_orders.extract_tracking(
            f'<a href="https://tools.usps.com/go/TrackConfirmAction?tLabels={USPS_GOOD}">x</a>'
        )
        assert found == [("USPS", USPS_GOOD, True)]

    def test_bad_check_digit_is_rejected_not_returned(self):
        found, rejected = shipped_orders.extract_tracking(
            f'href="http://wwwapps.ups.com/WebTracking/track?trackNums={UPS_BAD}"'
        )
        assert found == []
        assert rejected == [("UPS", UPS_BAD)]

    def test_ignores_long_digit_runs_in_body_text(self):
        """Item ids and order numbers are long digit runs; none may be mistaken
        for tracking. This is the real generic-mail failure mode."""
        body = (
            "<p>Item ID:</p><p>137588733156</p>"
            "<p>Order number:</p><p>02-15039-80305</p>"
            "<p>transactionId=10083008602520</p>"
            "<p>bu=43849650087&crd=20260813194444</p>"
        )
        found, rejected = shipped_orders.extract_tracking(body)
        assert found == []
        assert rejected == []

    def test_ignores_carrier_logo_url_without_tracking_param(self):
        body = ('<img src="http://se-branded-tracking-shipengine.s3-website-us-east-1'
                '.amazonaws.com/emails/carrier_logos/ups.png">')
        assert shipped_orders.extract_tracking(body) == ([], [])

    def test_ebay_marketing_tracking_url_is_not_a_carrier(self):
        body = ('<img src="https://www.ebayadservices.com/marketingtracking/v1/'
                'impression?mkevt=4&siteId=0&bu=43849650087">')
        assert shipped_orders.extract_tracking(body) == ([], [])

    def test_deduplicates_repeated_url(self):
        """The same anchor appears ~6x in a real message (mso conditionals)."""
        url = f'href="http://wwwapps.ups.com/WebTracking/track?trackNums={UPS_GOOD}"'
        found, _ = shipped_orders.extract_tracking(" ".join([url] * 6))
        assert found == [("UPS", UPS_GOOD, True)]


class TestExtractLabeledTracking:
    """BUI-916's second source: a number after an explicit tracking label."""

    @pytest.mark.parametrize("text", [
        f"Tracking Number: {USPS_GOOD}\n",
        f"Tracking number:\n\n {USPS_GOOD} \n",            # eBay's cell split
        f"Here is your tracking number:\n\n{USPS_GOOD}\n",  # a human typing
        f"USPS tracking number: {USPS_GOOD}\n",
        f"Tracking no. {USPS_GOOD}\n",
        f"Tracking #: {USPS_GOOD}\n",
    ])
    def test_reads_the_number_after_a_label(self, text):
        found, rejected = shipped_orders.extract_labeled_tracking(text)
        assert found == [("USPS", USPS_GOOD, True)]
        assert rejected == []

    def test_reads_a_number_printed_in_groups(self):
        """USPS numbers are routinely printed four digits at a time."""
        grouped = " ".join(USPS_GOOD[i:i + 4] for i in range(0, len(USPS_GOOD), 4))
        found, _ = shipped_orders.extract_labeled_tracking(
            f"Tracking Number: {grouped}\n")
        assert found == [("USPS", USPS_GOOD, True)]

    def test_trailing_prose_on_the_same_line_does_not_swallow_the_number(self):
        found, _ = shipped_orders.extract_labeled_tracking(
            f"Tracking number: {USPS_GOOD} arriving Monday\n")
        assert found == [("USPS", USPS_GOOD, True)]

    def test_prose_after_a_label_is_not_a_candidate_at_all(self):
        """"all tracking numbers are forwarded at time of shipping" is a real
        sentence in this mailbox. It must not even become a warning."""
        found, rejected = shipped_orders.extract_labeled_tracking(
            "all tracking numbers are forwarded at time of shipping. Thanks\n")
        assert found == []
        assert rejected == []

    def test_a_bare_digit_run_without_a_label_is_never_read(self):
        """The whole guard: position, not shape. Item ids look identical."""
        found, rejected = shipped_orders.extract_labeled_tracking(
            f"Item ID: 137588733156\nOrder total: {USPS_GOOD}\n")
        assert found == []
        assert rejected == []

    def test_a_number_failing_its_carrier_rule_is_never_returned(self):
        wrong = USPS_GOOD[:-1] + str((int(USPS_GOOD[-1]) + 1) % 10)
        found, rejected = shipped_orders.extract_labeled_tracking(
            f"Tracking Number: {wrong}\n")
        assert found == []
        assert rejected == [wrong]

    def test_a_shape_only_carrier_cannot_rescue_a_failed_check_digit(self):
        """A 22-digit USPS number with a broken check digit also satisfies
        FedEx's `\\d{22}`. Trying rules in order would relabel it FedEx and emit
        it unverified — a guessed number wearing the wrong carrier's name."""
        wrong = USPS_GOOD[:-1] + str((int(USPS_GOOD[-1]) + 1) % 10)
        assert len(wrong) == 22
        assert shipped_orders.fedex_shape_ok(wrong)      # the trap is real
        assert shipped_orders.classify_tracking(wrong) is None
        assert shipped_orders.classify_tracking(USPS_GOOD) == ("USPS", True)

    def test_a_corrupted_ups_number_is_not_relabelled(self):
        assert shipped_orders.classify_tracking(UPS_BAD) is None
        assert shipped_orders.classify_tracking(UPS_GOOD) == ("UPS", True)

    def test_a_genuine_shape_only_carrier_still_classifies(self):
        assert shipped_orders.classify_tracking(LABEL_FEDEX) == ("FEDEX", False)

    def test_a_shopee_spx_number_now_classifies(self):
        """BUI-967: SPX used to be this module's standing unmodelled-carrier
        example (see LABEL_UNKNOWN); it now gets its own shape rule."""
        assert shipped_orders.classify_tracking(LABEL_SPX) == ("SHOPEE", False)

    def test_a_shopee_spx_label_is_read_as_a_row_not_a_warning(self):
        found, rejected = shipped_orders.extract_labeled_tracking(
            f"SPX Express tracking number: {LABEL_SPX}\n")
        assert found == [("SHOPEE", LABEL_SPX, False)]
        assert rejected == []

    def test_an_unmodelled_carrier_is_reported_not_trusted(self):
        found, rejected = shipped_orders.extract_labeled_tracking(
            f"QXpress tracking number: {LABEL_UNKNOWN}\n")
        assert found == []
        assert rejected == [LABEL_UNKNOWN]

    def test_the_same_label_in_two_mime_parts_warns_once(self):
        """decode_body concatenates every part, so a multipart message renders
        the same label twice. Two warnings for one number reads as two problems.
        """
        text = (f"QXpress tracking number: {LABEL_UNKNOWN}\n"
                f"Items in this shipment\n"
                f"QXpress tracking number: {LABEL_UNKNOWN} Items in this\n")
        _found, rejected = shipped_orders.extract_labeled_tracking(text)
        assert rejected == [LABEL_UNKNOWN]


class TestExtractShipments:
    def test_carrier_url_wins_over_the_same_number_labelled(self):
        """A merchant mail carries both. The stronger provenance must be the one
        reported, or a verbatim-from-URL number would be tagged as rendered."""
        body = (f"USPS tracking number: {USPS_GOOD}\n"
                f'<a href="https://tools.usps.com/go/TrackConfirmAction?tLabels={USPS_GOOD}">x</a>')
        found, url_rejected, label_rejected = shipped_orders.extract_shipments(
            body, shipped_orders._plain_text(body))
        assert found == [("USPS", USPS_GOOD, True, shipped_orders.SOURCE_CARRIER_URL)]
        assert url_rejected == []
        assert label_rejected == []

    def test_both_sources_contribute_distinct_numbers(self):
        body = (f'<a href="http://wwwapps.ups.com/WebTracking/track?trackNums={UPS_GOOD}">x</a>'
                f"\nTracking Number: {USPS_GOOD}\n")
        found, _, _ = shipped_orders.extract_shipments(
            body, shipped_orders._plain_text(body))
        assert sorted((t, s) for _c, t, _v, s in found) == sorted([
            (UPS_GOOD, shipped_orders.SOURCE_CARRIER_URL),
            (USPS_GOOD, shipped_orders.SOURCE_LABEL),
        ])

    def test_reading_one_number_does_not_silence_an_unrelated_bad_one(self):
        """A successful read must not suppress a *different* number that failed
        validation — that would turn a real alarm into an invisible miss."""
        body = (f"Tracking Number: {USPS_GOOD}\n"
                f'<a href="https://tools.usps.com/go/TrackConfirmAction?tLabels={USPS_GOOD}">x</a>'
                f'<a href="http://wwwapps.ups.com/WebTracking/track?trackNums={UPS_BAD}">y</a>')
        found, url_rejected, _ = shipped_orders.extract_shipments(
            body, shipped_orders._plain_text(body))
        assert {t for _c, t, _v, _s in found} == {USPS_GOOD}
        assert url_rejected == [("UPS", UPS_BAD)]

    def test_a_number_read_successfully_is_not_also_reported_as_a_failure(self):
        """The same number reachable both ways is one shipment, not a row plus
        a warning about itself."""
        body = (f"Tracking Number: {USPS_GOOD}\n"
                f'<a href="https://tools.usps.com/go/TrackConfirmAction?tLabels={USPS_GOOD}">x</a>')
        _found, url_rejected, label_rejected = shipped_orders.extract_shipments(
            body, shipped_orders._plain_text(body))
        assert url_rejected == []
        assert label_rejected == []

    def test_the_label_gap_does_not_leap_a_blank_region(self):
        """The bound exists so a label cannot claim a number paragraphs away.
        Widest gap measured across 1704 real messages: 5 characters."""
        found, rejected = shipped_orders.extract_labeled_tracking(
            "Tracking Number:" + "\n" * 20 + f"{USPS_GOOD}\n")
        assert found == []
        assert rejected == []


# ─── Shipping-subject heuristic ────────────────────────────────────────────────

class TestIsShippingMail:
    """BUI-967: bare `package` anywhere in a subject used to qualify a message
    as a shipping notice — including mail that has nothing to do with a
    physical shipment."""

    @pytest.mark.parametrize("subject", [
        "Run failed: build and package",
        "[my-org/my-repo] Package published to npm",
        "Security alert: 1 vulnerable package in your repo",
    ])
    def test_bare_package_in_an_unrelated_subject_does_not_qualify(self, subject):
        assert not shipped_orders.is_shipping_mail(subject, "")

    @pytest.mark.parametrize("subject", [
        "Your package is now with its carrier!",
        "Your package has been delivered",
        "Your package is estimated to arrive Wednesday, August 19",
        "Your package has movement",  # matches only via "your package" itself
    ])
    def test_your_package_still_qualifies(self, subject):
        assert shipped_orders.is_shipping_mail(subject, "")


# ─── Seller / message parsing ─────────────────────────────────────────────────

class TestParseSeller:
    def test_reads_seller_from_members_display_name(self):
        assert shipped_orders.parse_seller(
            "eBay - timemachinecomics <a1b2@members.ebay.com>") == "timemachinecomics"

    def test_returns_none_for_ebay_own_mail(self):
        assert shipped_orders.parse_seller("eBay <ebay@ebay.com>") is None

    @pytest.mark.parametrize("value", ["", "random sender <x@y.com>"])
    def test_returns_none_for_unrelated(self, value):
        assert shipped_orders.parse_seller(value) is None


class TestParseMessage:
    def test_seller_shipped_mail_yields_a_row(self):
        record = shipped_orders.parse_message(load("members_shipped_ups"))
        assert record["kind"] == "rows"
        assert record["tracking"] == [
            {"tracking_number": UPS_GOOD, "carrier": "UPS", "carrier_verified": True,
             "source": shipped_orders.SOURCE_CARRIER_URL}
        ]
        assert record["seller"] == "timemachinecomics"
        assert record["order_number"] == "02-15039-80305"
        assert "UPS" in record["shipped_via"]

    def test_generic_ebay_mail_is_unparsed_never_guessed(self):
        record = shipped_orders.parse_message(load("generic_carrier_no_tracking"))
        assert record["kind"] == "unparsed"
        assert "tracking" not in record
        assert "appears nowhere in this message" in record["reason"]

    def test_bad_check_digit_message_is_unparsed_with_the_number_named(self):
        record = shipped_orders.parse_message(load("members_bad_check_digit"))
        assert record["kind"] == "unparsed"
        assert "tracking" not in record
        assert UPS_BAD in record["reason"]

    def test_unparsed_classes_are_named_not_derived_from_prose(self):
        """Presentation switches on this field; a reworded reason must not
        silently reclassify a validation failure as routine noise."""
        assert (shipped_orders.parse_message(load("generic_carrier_no_tracking"))
                ["unparsed_class"] == shipped_orders.UNPARSED_NO_TRACKING_URL)
        assert (shipped_orders.parse_message(load("members_bad_check_digit"))
                ["unparsed_class"] == shipped_orders.UNPARSED_VALIDATION_FAILED)

    def test_saved_search_blast_is_ignored(self):
        record = shipped_orders.parse_message(load("not_a_shipping_mail"))
        assert record["kind"] == "ignore"

    def test_usps_seller_mail_parses(self):
        record = shipped_orders.parse_message(load("members_shipped_usps"))
        assert record["kind"] == "rows"
        assert record["tracking"][0]["carrier"] == "USPS"
        assert record["tracking"][0]["tracking_number"] == USPS_GOOD
        assert record["seller"] == "othercomicshop"

    def test_multipart_body_is_walked(self):
        """The generic shape is multipart/alternative; both parts must decode."""
        body = shipped_orders.decode_body(load("generic_carrier_no_tracking")["payload"])
        assert "Shipped with UPS" in body      # text/plain part
        assert "FetchOrderDetails" in body      # text/html part


class TestParseMessageLabeledSource:
    """The four shapes BUI-807's eBay-only, URL-only reader could not see."""

    def test_ebay_delivery_update_yields_a_row_from_the_label(self):
        """eBay's own mail DOES name the number in this subject class — and the
        old classifier dropped the whole message as `ignore` before extraction.
        """
        record = shipped_orders.parse_message(load("generic_delivery_update_labeled"))
        assert record["kind"] == "rows"
        assert record["tracking"] == [{
            "tracking_number": LABEL_FEDEX, "carrier": "FEDEX",
            "carrier_verified": False, "source": shipped_orders.SOURCE_LABEL,
        }]
        assert record["item_id"] == "800373841070"
        assert record["transaction_id"] == "10082434181125"

    def test_the_fixture_reproduces_the_real_label_gap(self):
        """The fixture's *rendered* whitespace gap between label and number has
        to match the real message's, or it stops testing the bound at all: a
        hand-typed fixture with extra blank lines would quietly demand a looser
        regex than reality needs. Measured on the real message: 5 characters.
        """
        body = shipped_orders.decode_body(
            load("generic_delivery_update_labeled")["payload"])
        text = shipped_orders._plain_text(body)
        gap = re.search(r"Tracking number:([\s ]*)770012340000", text)
        assert gap is not None
        assert len(gap.group(1)) == 5

    def test_human_seller_mail_parses_despite_a_non_shipping_subject(self):
        """Extraction runs before `is_shipping_mail`. Gating on the subject
        ("Re: Want List For …") would lose this real number outright."""
        message = load("human_seller_labeled")
        hdrs = shipped_orders.headers_of(message)
        assert not shipped_orders.is_shipping_mail(hdrs["Subject"], "")
        record = shipped_orders.parse_message(message)
        assert record["kind"] == "rows"
        assert record["tracking"][0]["tracking_number"] == LABEL_USPS_HUMAN
        assert record["seller"] == "Example Comic Art"

    def test_non_ebay_merchant_mail_is_named_by_its_display_name(self):
        record = shipped_orders.parse_message(load("merchant_shipping_confirmation"))
        assert record["kind"] == "rows"
        assert record["tracking"][0]["tracking_number"] == LABEL_USPS_MERCHANT
        assert record["seller"] == "Example Books"
        assert record["item_id"] is None

    def test_unmodelled_carrier_is_unparsed_with_the_number_named(self):
        record = shipped_orders.parse_message(load("merchant_unknown_carrier"))
        assert record["kind"] == "unparsed"
        assert record["unparsed_class"] == shipped_orders.UNPARSED_UNKNOWN_CARRIER
        assert "tracking" not in record
        assert record["candidates"] == [LABEL_UNKNOWN]

    def test_shopee_spx_label_yields_a_row_not_an_unknown_carrier_warning(self):
        """BUI-967: this used to be the unmodelled-carrier fixture's own
        number (see merchant_unknown_carrier, now repointed at an invented
        carrier) — SPX now has a shape rule, so it must read as a shipment."""
        record = shipped_orders.parse_message(load("merchant_shopee_labeled"))
        assert record["kind"] == "rows"
        assert record["tracking"] == [{
            "tracking_number": LABEL_SPX, "carrier": "SHOPEE",
            "carrier_verified": False, "source": shipped_orders.SOURCE_LABEL,
        }]
        assert record["seller"] == "Shopee"

    def test_prose_after_a_label_is_ignored_not_warned_about(self):
        record = shipped_orders.parse_message(load("label_prose_not_a_number"))
        assert record["kind"] == "ignore"


class TestParseMerchant:
    def test_reads_a_quoted_display_name(self):
        assert shipped_orders.parse_merchant(
            '"Example Books" <orders@t.example-books.test>') == "Example Books"

    def test_reads_an_unquoted_display_name(self):
        assert shipped_orders.parse_merchant(
            "Example Coffee <orders@example-coffee.test>") == "Example Coffee"

    @pytest.mark.parametrize("header", [
        "eBay <ebay@ebay.com>",
        "eBay - timemachinecomics <a1b2@members.ebay.com>",
    ])
    def test_ebay_senders_are_not_merchants(self, header):
        """"eBay" is not who shipped, and the members shape already has a real
        seller login that `parse_seller` reads."""
        assert shipped_orders.parse_merchant(header) is None

    def test_returns_none_without_a_display_name(self):
        assert shipped_orders.parse_merchant("<bare@example.test>") is None


# ─── Ledger ───────────────────────────────────────────────────────────────────

class TestLoadLedger:
    def test_missing_file_is_an_empty_set(self, tmp_path):
        assert shipped_orders.load_ledger(tmp_path / "nope.json") == set()

    def test_reads_keys_as_tracking_numbers(self, tmp_path):
        path = tmp_path / "ledger.json"
        path.write_text(json.dumps({UPS_GOOD: {"submittedAt": "2026-07-07T02:01:10Z"}}))
        assert shipped_orders.load_ledger(path) == {UPS_GOOD}

    def test_corrupt_ledger_raises_rather_than_reoffering_everything(self, tmp_path):
        path = tmp_path / "ledger.json"
        path.write_text("{not json")
        with pytest.raises(shipped_orders.ShippedOrdersError):
            shipped_orders.load_ledger(path)

    def test_wrong_shape_raises(self, tmp_path):
        path = tmp_path / "ledger.json"
        path.write_text(json.dumps([UPS_GOOD]))
        with pytest.raises(shipped_orders.ShippedOrdersError):
            shipped_orders.load_ledger(path)

    def test_reading_does_not_write_the_ledger(self, tmp_path):
        path = tmp_path / "ledger.json"
        original = json.dumps({UPS_GOOD: {"submittedAt": "2026-07-07T02:01:10Z"}})
        path.write_text(original)
        before = path.stat().st_mtime_ns
        shipped_orders.load_ledger(path)
        assert path.read_text() == original
        assert path.stat().st_mtime_ns == before


# ─── Collection ───────────────────────────────────────────────────────────────

class TestCollect:
    def test_lifecycle_mails_collapse_to_one_row(self):
        messages = [load("members_shipped_ups"), load("members_delivered_ups")]
        rows, skipped, unparsed = shipped_orders.collect(messages, set())
        assert len(rows) == 1
        row = rows[0]
        assert row["tracking_number"] == UPS_GOOD
        assert len(row["message_ids"]) == 2
        # earliest sighting kept as first_seen_at, latest subject as status
        assert row["first_seen_at"] < row["sent_at"]
        assert "delivered" in row["subject"].lower()

    def test_already_submitted_number_is_skipped(self):
        messages = [load("members_shipped_ups")]
        rows, skipped, _ = shipped_orders.collect(messages, {UPS_GOOD})
        assert rows == []
        assert len(skipped) == 1

    def test_ledger_case_is_normalized_end_to_end(self, tmp_path):
        """A lowercase ledger key must still suppress the row.

        Extraction upper-cases, so the ledger has to as well or a number
        already submitted would be re-offered forever.
        """
        path = tmp_path / "ledger.json"
        path.write_text(json.dumps({UPS_GOOD.lower(): {"submittedAt": "2026-08-13T00:00:00Z"}}))
        submitted = shipped_orders.load_ledger(path)
        rows, skipped, _ = shipped_orders.collect([load("members_shipped_ups")], submitted)
        assert rows == []
        assert len(skipped) == 1

    def test_unparsed_messages_are_reported_not_dropped(self):
        messages = [load("generic_carrier_no_tracking"), load("members_bad_check_digit")]
        rows, _, unparsed = shipped_orders.collect(messages, set())
        assert rows == []
        assert len(unparsed) == 2

    def test_ignored_mail_is_neither_row_nor_warning(self):
        rows, skipped, unparsed = shipped_orders.collect([load("not_a_shipping_mail")], set())
        assert (rows, skipped, unparsed) == ([], [], [])

    def test_split_order_yields_one_row_per_shipment(self):
        """A single mail announcing two packages must produce two rows.

        Folding them into one silently drops a real package — the exact miss
        this tool exists to prevent. Regression guard for the first cut, which
        kept only the first number.
        """
        message = _seller_message_with(
            "m-split",
            [UPS_GOOD, "1Z77T45B22ZZ765437"],
        )
        rows, _, unparsed = shipped_orders.collect([message], set())
        assert unparsed == []
        assert sorted(r["tracking_number"] for r in rows) == sorted(
            [UPS_GOOD, "1Z77T45B22ZZ765437"])
        assert all(r["seller"] == "splitshop" for r in rows)

    def test_split_order_dedupes_each_shipment_independently(self):
        """Re-seeing the same split mail must not duplicate either row."""
        message = _seller_message_with("m-split", [UPS_GOOD, "1Z77T45B22ZZ765437"])
        rows, _, _ = shipped_orders.collect([message, message], set())
        assert len(rows) == 2
        assert all(len(r["message_ids"]) == 1 for r in rows)

    def test_rows_are_sorted_by_first_sighting(self):
        messages = [load("members_shipped_usps"), load("members_shipped_ups")]
        rows, _, _ = shipped_orders.collect(messages, set())
        assert [r["first_seen_at"] for r in rows] == sorted(r["first_seen_at"] for r in rows)

    def test_both_sources_land_in_one_run(self):
        rows, _, unparsed = shipped_orders.collect(
            [load("members_shipped_ups"), load("human_seller_labeled"),
             load("merchant_shipping_confirmation"),
             load("generic_delivery_update_labeled")], set())
        assert unparsed == []
        assert {r["tracking_number"]: r["source"] for r in rows} == {
            UPS_GOOD: shipped_orders.SOURCE_CARRIER_URL,
            LABEL_USPS_HUMAN: shipped_orders.SOURCE_LABEL,
            LABEL_USPS_MERCHANT: shipped_orders.SOURCE_LABEL,
            LABEL_FEDEX: shipped_orders.SOURCE_LABEL,
        }


class TestGroupUncovered:
    """One entry per ORDER, because that is the unit of the promise."""

    def _generic(self, message_id, item_id, txn_id, subject, internal_date):
        html = (f'<a href="https://www.ebay.com/vod/FetchOrderDetails?itemId={item_id}'
                f'&amp;transactionId={txn_id}">Track order</a>')
        return {
            "id": message_id, "threadId": message_id, "internalDate": internal_date,
            "payload": {
                "mimeType": "text/html",
                "headers": [{"name": "From", "value": "eBay <ebay@ebay.com>"},
                            {"name": "Subject", "value": subject}],
                "body": {"data": base64.urlsafe_b64encode(html.encode()).decode()},
            },
        }

    def test_one_orders_lifecycle_mails_collapse_to_one_named_order(self):
        messages = [
            self._generic("m1", "800373841070", "10082434181125",
                          "Your package is now with its carrier!", "1753718932000"),
            self._generic("m2", "800373841070", "10082434181125",
                          "Your order's been delivered", "1753918932000"),
        ]
        _rows, _skipped, unparsed = shipped_orders.collect(messages, set())
        assert len(unparsed) == 2
        grouped = shipped_orders.group_uncovered(unparsed)
        assert len(grouped) == 1
        assert grouped[0]["item_id"] == "800373841070"
        assert grouped[0]["transaction_id"] == "10082434181125"
        assert grouped[0]["message_ids"] == ["m1", "m2"]
        # latest subject is the current status, earliest sighting is the date
        assert "delivered" in grouped[0]["subject"]
        assert grouped[0]["first_seen_at"] < grouped[0]["last_seen_at"]

    def test_different_orders_stay_separate(self):
        messages = [
            self._generic("m1", "800373841070", "10082434181125",
                          "Your package is now with its carrier!", "1753718932000"),
            self._generic("m2", "298398131656", "10081716014808",
                          "Your order's been delivered", "1753718932000"),
        ]
        _rows, _skipped, unparsed = shipped_orders.collect(messages, set())
        assert len(shipped_orders.group_uncovered(unparsed)) == 2

    def test_mail_without_an_order_id_is_identified_by_itself(self):
        """Never merge two unidentifiable shipments into one — that would hide
        one of them, which is the miss this whole section exists to prevent."""
        _rows, _skipped, unparsed = shipped_orders.collect(
            [load("merchant_unknown_carrier")], set())
        grouped = shipped_orders.group_uncovered(unparsed)
        assert len(grouped) == 1
        assert grouped[0]["item_id"] is None
        assert grouped[0]["message_ids"] == ["fixture-unknown-carrier"]


# ─── Gmail access guards ──────────────────────────────────────────────────────

class TestGmailAccess:
    def test_only_read_methods_are_permitted(self):
        for method in ("send", "modify", "trash", "delete", "batchModify", "insert"):
            with pytest.raises(shipped_orders.ShippedOrdersError, match="refusing non-read"):
                shipped_orders._run_gws(method, {"userId": "me"})

    def test_keyring_banner_before_json_is_stripped(self, monkeypatch):
        class Proc:
            returncode = 0
            stdout = 'Using keyring backend: keyring\n{"messages": [{"id": "abc"}]}'
            stderr = ""

        monkeypatch.setattr(shipped_orders.subprocess, "run", lambda *a, **k: Proc())
        assert shipped_orders._run_gws("list", {}) == {"messages": [{"id": "abc"}]}

    def test_missing_gws_binary_raises(self, monkeypatch):
        def boom(*a, **k):
            raise FileNotFoundError

        monkeypatch.setattr(shipped_orders.subprocess, "run", boom)
        with pytest.raises(shipped_orders.ShippedOrdersError, match="not on PATH"):
            shipped_orders._run_gws("list", {})

    def test_non_json_output_raises_rather_than_returning_empty(self, monkeypatch):
        class Proc:
            returncode = 1
            stdout = ""
            stderr = "auth required"

        monkeypatch.setattr(shipped_orders.subprocess, "run", lambda *a, **k: Proc())
        with pytest.raises(shipped_orders.ShippedOrdersError):
            shipped_orders._run_gws("list", {})

    def test_personal_profile_is_selected_by_default(self, monkeypatch):
        monkeypatch.delenv(shipped_orders.GWS_CONFIG_DIR_ENV, raising=False)
        env = shipped_orders._gws_env()
        assert env["GOOGLE_WORKSPACE_CLI_CONFIG_DIR"].endswith("gws-personal")

    def test_profile_is_overridable(self, monkeypatch):
        monkeypatch.setenv(shipped_orders.GWS_CONFIG_DIR_ENV, "/tmp/other")
        assert shipped_orders._gws_env()["GOOGLE_WORKSPACE_CLI_CONFIG_DIR"] == "/tmp/other"


class TestFetchMessages:
    """The concurrent fetch must not soften any failure (BUI-916)."""

    @staticmethod
    def _gws_stub(pages, get_result):
        calls = {"list": 0}

        def fake(method, params):
            if method == "list":
                page = pages[calls["list"]]
                calls["list"] += 1
                return page
            return get_result(params["id"])
        return fake, calls

    def test_pages_the_listing_past_the_api_ceiling(self, monkeypatch):
        """messages.list caps a page at 500; without the pageToken loop a
        --max-results of 600 would silently return 500 and look complete."""
        first = {"messages": [{"id": f"a{i}"} for i in range(500)], "nextPageToken": "t"}
        second = {"messages": [{"id": f"b{i}"} for i in range(100)]}
        fake, _calls = self._gws_stub([first, second], lambda i: {"id": i})
        monkeypatch.setattr(shipped_orders, "_run_gws", fake)
        ids, truncated = shipped_orders.list_message_ids("q", 600)
        assert len(ids) == 600
        assert truncated is False

    def test_a_short_window_is_reported_as_truncated(self, monkeypatch):
        page = {"messages": [{"id": f"a{i}"} for i in range(10)], "nextPageToken": "more"}
        fake, _calls = self._gws_stub([page], lambda i: {"id": i})
        monkeypatch.setattr(shipped_orders, "_run_gws", fake)
        ids, truncated = shipped_orders.list_message_ids("q", 10)
        assert len(ids) == 10
        assert truncated is True

    def test_an_exhausted_listing_is_not_truncated(self, monkeypatch):
        page = {"messages": [{"id": "a"}, {"id": "b"}]}
        fake, _calls = self._gws_stub([page], lambda i: {"id": i})
        monkeypatch.setattr(shipped_orders, "_run_gws", fake)
        assert shipped_orders.list_message_ids("q", 50) == (["a", "b"], False)

    @pytest.mark.parametrize("workers", [1, 4])
    def test_every_listed_message_is_fetched(self, monkeypatch, workers):
        page = {"messages": [{"id": f"m{i}"} for i in range(25)]}
        fake, _calls = self._gws_stub([page], lambda i: {"id": i})
        monkeypatch.setattr(shipped_orders, "_run_gws", fake)
        fetched = shipped_orders.fetch_messages("q", 100, workers)
        assert [m["id"] for m in fetched.messages] == [f"m{i}" for i in range(25)]
        assert fetched.listed == 25
        assert fetched.truncated is False

    @pytest.mark.parametrize("workers", [1, 4])
    def test_one_failed_fetch_aborts_the_whole_run(self, monkeypatch, workers):
        """A partial batch is the dangerous outcome: the orders that failed to
        fetch would be indistinguishable from orders that do not exist."""
        page = {"messages": [{"id": f"m{i}"} for i in range(12)]}

        def get_result(message_id):
            if message_id == "m7":
                raise shipped_orders.ShippedOrdersError("gws timed out")
            return {"id": message_id}

        fake, _calls = self._gws_stub([page], get_result)
        monkeypatch.setattr(shipped_orders, "_run_gws", fake)
        with pytest.raises(shipped_orders.ShippedOrdersError, match="timed out"):
            shipped_orders.fetch_messages("q", 100, workers)

    def test_no_results_is_an_empty_fetch_not_an_error(self, monkeypatch):
        fake, _calls = self._gws_stub([{}], lambda i: {"id": i})
        monkeypatch.setattr(shipped_orders, "_run_gws", fake)
        assert shipped_orders.fetch_messages("q", 100) == shipped_orders.Fetched([], False, 0)


# ─── CLI ──────────────────────────────────────────────────────────────────────

class TestMain:
    @pytest.fixture
    def stub_gmail(self, monkeypatch):
        def install(names, truncated=False):
            messages = [load(n) for n in names]

            def fake(query, max_results, max_workers=None):
                return shipped_orders.Fetched(messages, truncated, len(messages))

            monkeypatch.setattr(shipped_orders, "fetch_messages", fake)
        return install

    def test_json_output_shape(self, stub_gmail, tmp_path, capsys):
        stub_gmail(["members_shipped_ups", "members_delivered_ups"])
        code = shipped_orders.main(["--json", "--ledger", str(tmp_path / "l.json")])
        payload = json.loads(capsys.readouterr().out)
        assert code == shipped_orders._EXIT_OK
        assert payload["counts"] == {"new": 1, "already_submitted": 0,
                                     "unparsed": 0, "uncovered_orders": 0}
        assert payload["truncated"] is False
        assert payload["orders"][0]["tracking_number"] == UPS_GOOD
        assert payload["orders"][0]["carrier"] == "UPS"
        assert payload["orders"][0]["seller"] == "timemachinecomics"
        assert payload["orders"][0]["source"] == shipped_orders.SOURCE_CARRIER_URL

    def test_unparsed_mail_sets_a_distinct_exit_code(self, stub_gmail, tmp_path, capsys):
        stub_gmail(["generic_carrier_no_tracking"])
        code = shipped_orders.main(["--json", "--ledger", str(tmp_path / "l.json")])
        payload = json.loads(capsys.readouterr().out)
        assert code == shipped_orders._EXIT_UNPARSED
        assert payload["counts"]["unparsed"] == 1
        assert payload["orders"] == []

    def test_gmail_failure_exits_nonzero_and_prints_nothing_to_stdout(
            self, monkeypatch, tmp_path, capsys):
        def boom(*a, **k):
            raise shipped_orders.ShippedOrdersError("gws is not authenticated")

        monkeypatch.setattr(shipped_orders, "fetch_messages", boom)
        code = shipped_orders.main(["--json", "--ledger", str(tmp_path / "l.json")])
        captured = capsys.readouterr()
        assert code == shipped_orders._EXIT_HARD_FAILURE
        # An unreachable Gmail must never look like "no new orders".
        assert captured.out.strip() == ""
        assert "not authenticated" in captured.err

    def test_corrupt_ledger_fails_the_run_rather_than_reoffering(
            self, stub_gmail, tmp_path, capsys):
        """An unreadable ledger must not degrade into "everything is new"."""
        ledger = tmp_path / "l.json"
        ledger.write_text("{corrupt")
        stub_gmail(["members_shipped_ups"])
        code = shipped_orders.main(["--json", "--ledger", str(ledger)])
        captured = capsys.readouterr()
        assert code == shipped_orders._EXIT_HARD_FAILURE
        assert captured.out.strip() == ""
        assert "ledger" in captured.err

    def test_ledger_entries_are_skipped(self, stub_gmail, tmp_path, capsys):
        ledger = tmp_path / "l.json"
        ledger.write_text(json.dumps({UPS_GOOD: {"submittedAt": "2026-08-13T00:00:00Z"}}))
        stub_gmail(["members_shipped_ups"])
        shipped_orders.main(["--json", "--ledger", str(ledger)])
        payload = json.loads(capsys.readouterr().out)
        assert payload["counts"] == {"new": 0, "already_submitted": 1,
                                     "unparsed": 0, "uncovered_orders": 0}

    def test_all_flag_ignores_the_ledger(self, stub_gmail, tmp_path, capsys):
        ledger = tmp_path / "l.json"
        ledger.write_text(json.dumps({UPS_GOOD: {"submittedAt": "2026-08-13T00:00:00Z"}}))
        stub_gmail(["members_shipped_ups"])
        shipped_orders.main(["--json", "--all", "--ledger", str(ledger)])
        payload = json.loads(capsys.readouterr().out)
        assert payload["counts"]["new"] == 1

    def test_human_table_names_the_number_carrier_and_seller(
            self, stub_gmail, tmp_path, capsys):
        stub_gmail(["members_shipped_ups"])
        shipped_orders.main(["--ledger", str(tmp_path / "l.json")])
        out = capsys.readouterr().out
        assert UPS_GOOD in out
        assert "UPS" in out
        assert "timemachinecomics" in out

    def test_table_names_every_order_it_cannot_cover(self, stub_gmail, tmp_path, capsys):
        """BUI-916's bar: an uncovered order is NAMED, never counted away.

        A collapsed count is what made the misses invisible — a missing order
        and no order looked the same. The eBay item id is the name.
        """
        stub_gmail(["generic_carrier_no_tracking"])
        shipped_orders.main(["--ledger", str(tmp_path / "l.json")])
        out = capsys.readouterr().out
        assert "CANNOT be covered" in out
        assert "1 shipped eBay order(s)" in out
        assert "eBay item 137588733156" in out

    def test_days_flag_is_parenthesised_against_or_precedence(
            self, monkeypatch, tmp_path):
        """Gmail binds a bare trailing `newer_than:` to the last OR branch only.

        DEFAULT_QUERY is a top-level OR chain, so an unparenthesised join would
        apply the date filter to one clause out of four and silently sweep in
        years of unwindowed mail.
        """
        seen = {}

        def capture(query, max_results, max_workers=None):
            seen["query"] = query
            return shipped_orders.Fetched([], False, 0)

        monkeypatch.setattr(shipped_orders, "fetch_messages", capture)
        shipped_orders.main(["--json", "--days", "7", "--ledger", str(tmp_path / "l.json")])
        assert seen["query"] == f"({shipped_orders.DEFAULT_QUERY}) newer_than:7d"
        assert seen["query"].endswith(") newer_than:7d")

    def test_table_lists_every_validation_failure_individually(
            self, stub_gmail, tmp_path, capsys):
        """The rare, alarming class must never be collapsed into a count."""
        stub_gmail(["members_bad_check_digit"])
        shipped_orders.main(["--ledger", str(tmp_path / "l.json")])
        out = capsys.readouterr().out
        assert "FAILED validation" in out
        assert UPS_BAD in out
        assert "timemachinecomics" in out

    def test_json_keeps_the_full_unparsed_list_even_when_table_summarises(
            self, stub_gmail, tmp_path, capsys):
        stub_gmail(["generic_carrier_no_tracking", "members_bad_check_digit"])
        shipped_orders.main(["--json", "--ledger", str(tmp_path / "l.json")])
        payload = json.loads(capsys.readouterr().out)
        assert payload["counts"]["unparsed"] == 2
        assert len(payload["unparsed"]) == 2
        assert all("reason" in r for r in payload["unparsed"])

    def test_version_flag_exits_zero(self, capsys):
        with pytest.raises(SystemExit) as exc:
            shipped_orders.main(["--version"])
        assert exc.value.code == 0
        assert "ebay-shipped" in capsys.readouterr().out

    def test_a_labelled_number_reaches_the_table_tagged_as_such(
            self, stub_gmail, tmp_path, capsys):
        stub_gmail(["human_seller_labeled"])
        code = shipped_orders.main(["--ledger", str(tmp_path / "l.json")])
        out = capsys.readouterr().out
        assert code == shipped_orders._EXIT_OK
        assert LABEL_USPS_HUMAN in out
        assert shipped_orders.SOURCE_LABEL in out
        assert "Example Comic Art" in out

    def test_a_long_usps_number_does_not_break_the_columns(
            self, stub_gmail, tmp_path, capsys):
        """A 26-digit impb number overflowing its column pushes every later
        field out of alignment on exactly the rows that matter most."""
        stub_gmail(["merchant_shipping_confirmation"])
        shipped_orders.main(["--ledger", str(tmp_path / "l.json")])
        lines = capsys.readouterr().out.splitlines()
        header = next(ln for ln in lines if ln.startswith("TRACKING NUMBER"))
        row = next(ln for ln in lines if ln.startswith(LABEL_USPS_MERCHANT))
        assert row.index("USPS") == header.index("CARRIER")

    def test_a_truncated_window_is_loud_and_exits_two(
            self, stub_gmail, tmp_path, capsys):
        """A short window looks exactly like a quiet week. It must not."""
        stub_gmail(["members_shipped_ups"], truncated=True)
        code = shipped_orders.main(["--ledger", str(tmp_path / "l.json")])
        out = capsys.readouterr().out
        assert code == shipped_orders._EXIT_TRUNCATED
        assert "TRUNCATED" in out
        assert "--max-results" in out

    def test_truncation_is_machine_readable_in_json(
            self, stub_gmail, tmp_path, capsys):
        """An unattended caller must be able to refuse a short window without
        parsing English out of the table."""
        stub_gmail(["members_shipped_ups"], truncated=True)
        code = shipped_orders.main(["--json", "--ledger", str(tmp_path / "l.json")])
        payload = json.loads(capsys.readouterr().out)
        assert code == shipped_orders._EXIT_TRUNCATED
        assert payload["truncated"] is True
        assert payload["messages_examined"] == 1

    def test_truncation_outranks_unparsed_in_the_exit_code(
            self, stub_gmail, tmp_path, capsys):
        """An incomplete window cannot even enumerate what it missed, so it is
        the louder of the two failures."""
        stub_gmail(["generic_carrier_no_tracking"], truncated=True)
        code = shipped_orders.main(["--json", "--ledger", str(tmp_path / "l.json")])
        capsys.readouterr()
        assert code == shipped_orders._EXIT_TRUNCATED

    def test_uncovered_orders_are_in_the_json_payload(
            self, stub_gmail, tmp_path, capsys):
        stub_gmail(["generic_carrier_no_tracking"])
        shipped_orders.main(["--json", "--ledger", str(tmp_path / "l.json")])
        payload = json.loads(capsys.readouterr().out)
        assert payload["counts"]["uncovered_orders"] == 1
        assert payload["uncovered_orders"][0]["item_id"] == "137588733156"

    def test_non_ebay_misses_are_not_sent_to_the_ebay_order_page(
            self, stub_gmail, tmp_path, capsys):
        """Advice that cannot be followed is worse than none: an Amazon
        shipment has no eBay order page to look it up on."""
        stub_gmail(["merchant_unknown_carrier", "generic_carrier_no_tracking"])
        shipped_orders.main(["--ledger", str(tmp_path / "l.json")])
        out = capsys.readouterr().out
        assert "recover each from its eBay order page" in out
        ebay_section = out.index("shipped eBay order(s) CANNOT be covered")
        assert out.index("eBay item 137588733156") > ebay_section

    def test_bad_worker_count_is_rejected(self, tmp_path):
        with pytest.raises(SystemExit):
            shipped_orders.main(["--max-workers", "0",
                                 "--ledger", str(tmp_path / "l.json")])

    def test_help_documents_every_exit_code(self, capsys):
        with pytest.raises(SystemExit):
            shipped_orders.main(["--help"])
        out = capsys.readouterr().out
        for code in ("0", "1", "2", "3"):
            assert f"{code} " in out
        assert "Exit codes" in out
        # The EZShip seam is in apps/ezship, not here — say so where an
        # operator wiring up a nightly job will read it (BUI-916 / BUI-159).
        assert "never talks to EZShip" in out
