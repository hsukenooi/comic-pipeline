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
from pathlib import Path

import pytest

import shipped_orders

FIXTURES = Path(__file__).parent / "fixtures" / "shipped_orders"

# The fixtures' invented numbers, kept here so a fixture edit that changes them
# fails loudly instead of silently weakening an assertion.
UPS_GOOD = "1Z99X99A11QQ123459"
UPS_BAD = "1Z99X99A11QQ123451"
USPS_GOOD = "9405511899223197428490"


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
            {"tracking_number": UPS_GOOD, "carrier": "UPS", "carrier_verified": True}
        ]
        assert record["seller"] == "timemachinecomics"
        assert record["order_number"] == "02-15039-80305"
        assert "UPS" in record["shipped_via"]

    def test_generic_ebay_mail_is_unparsed_never_guessed(self):
        record = shipped_orders.parse_message(load("generic_carrier_no_tracking"))
        assert record["kind"] == "unparsed"
        assert "tracking" not in record
        assert "no carrier tracking URL" in record["reason"]

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


# ─── CLI ──────────────────────────────────────────────────────────────────────

class TestMain:
    @pytest.fixture
    def stub_gmail(self, monkeypatch):
        def install(names):
            monkeypatch.setattr(
                shipped_orders, "fetch_messages",
                lambda query, max_results: [load(n) for n in names])
        return install

    def test_json_output_shape(self, stub_gmail, tmp_path, capsys):
        stub_gmail(["members_shipped_ups", "members_delivered_ups"])
        code = shipped_orders.main(["--json", "--ledger", str(tmp_path / "l.json")])
        payload = json.loads(capsys.readouterr().out)
        assert code == shipped_orders._EXIT_OK
        assert payload["counts"] == {"new": 1, "already_submitted": 0, "unparsed": 0}
        assert payload["orders"][0]["tracking_number"] == UPS_GOOD
        assert payload["orders"][0]["carrier"] == "UPS"
        assert payload["orders"][0]["seller"] == "timemachinecomics"

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
        assert payload["counts"] == {"new": 0, "already_submitted": 1, "unparsed": 0}

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

    def test_table_summarises_structural_misses(self, stub_gmail, tmp_path, capsys):
        """eBay's own mail never carries a number; say so once, with a count."""
        stub_gmail(["generic_carrier_no_tracking"])
        shipped_orders.main(["--ledger", str(tmp_path / "l.json")])
        out = capsys.readouterr().out
        assert "carry no tracking number at all" in out
        assert "1 eBay-sent shipping mail" in out

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

    def test_days_flag_shapes_the_query(self, monkeypatch, tmp_path, capsys):
        seen = {}

        def capture(query, max_results):
            seen["query"] = query
            return []

        monkeypatch.setattr(shipped_orders, "fetch_messages", capture)
        shipped_orders.main(["--json", "--days", "7", "--ledger", str(tmp_path / "l.json")])
        assert "newer_than:7d" in seen["query"]

    def test_version_flag_exits_zero(self, capsys):
        with pytest.raises(SystemExit) as exc:
            shipped_orders.main(["--version"])
        assert exc.value.code == 0
        assert "ebay-shipped" in capsys.readouterr().out
